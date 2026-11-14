"""Phase 2 edge regressions: task inputs, diagnostics and private exports."""
import asyncio
import errno
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_local_mcp import diagnostics, windows
from codex_local_mcp.incidents import export_incident, preview, write_export
from codex_local_mcp.protocol_tasks import ProtocolTasks, read_execution
from codex_local_mcp.retention import backup
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store, SCHEMA_VERSION
from codex_local_mcp.tools import dispatch, encoded_size
from tests import test_mcp_modern as modern
from tests import test_diagnostics as doctor
from tests.helpers import temp_root
from tests.mcp_wire import RecordingMCP
from tests.incident_replay import IncidentReplay
from tests.test_service import ServiceHarness


class TaskReviewTest(ServiceHarness):
    request, task, input_task, code = (modern.ModernMCPTest.request, modern.ModernMCPTest.task,
        modern.ModernMCPTest.input_task, staticmethod(modern.ModernMCPTest.code))

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.adapter = RecordingMCP()
        self.adapter.config = self.cfg
        self.manager, self.serial = ProtocolTasks(self.service), 0
        self.bootstrap = patch("codex_local_mcp.mcp_modern.ensure_broker", AsyncMock())
        self.channel = patch("codex_local_mcp.mcp_modern.rpc", self.exchange)
        self.bootstrap.start()
        self.channel.start()

    async def asyncTearDown(self):
        self.channel.stop()
        self.bootstrap.stop()
        await asyncio.wait_for(super().asyncTearDown(), 15)

    async def exchange(self, config, method, *, name=None, args=None, **kwargs):
        return await self.manager.handle(name, args) if method == "mcp_tasks" else await dispatch(self.service, name, args)

    async def test_invalid_later_input_does_not_send_an_earlier_answer(self):
        created, record, pending = await self.input_task()
        key = next(iter(pending["inputRequests"]))
        request_id = self.service.requests(record["bridge_task_id"])[0]["id"]
        response = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
            key: {"action": "accept", "content": {"decision": "accept"}},
            "unissued": {"action": "accept", "content": {"decision": "accept"}}}})
        self.assertEqual(self.code(response), "stale_request")
        self.assertEqual(self.store.get("requests", request_id)["status"], "pending")
        self.assertFalse(any(event["kind"] == "bridge/response_sent"
            for event in self.store.events(0, 100)["events"]))
        self.assertFalse(read_execution(self.store, created["taskId"])["inputs"][key]["consumed"])

    async def test_thread_scoped_elicitation_is_visible_but_cannot_gain_task_turn_authority(self):
        created = await self.task("hold")
        record = read_execution(self.store, created["taskId"])
        await self.service.on_message({"id": 777, "method": "mcpServer/elicitation/request", "params": {
            "threadId": record["thread_id"], "serverName": "fixture", "mode": "form", "message": "Offline form",
            "requestedSchema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}})
        pending = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(pending["status"], "input_required")
        self.assertEqual(pending["inputRequests"], {})
        detail = pending["_meta"]["codex-local-mcp/pendingRequests"][0]
        positive = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
            detail["input_key"]: {"action": "accept", "content": {"answer": "Person's answer"}}}})
        self.assertEqual(self.code(positive), "human_review_required")
        self.assertEqual(self.store.get("requests", detail["id"])["status"], "pending")
        denied = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
            detail["input_key"]: {"action": "accept", "content": {"decision": "decline"}}}})
        self.assertNotIn("error", denied)

    async def test_modern_catalog_budget_includes_required_server_metadata(self):
        catalog = (await self.request("tools/list", caps={}))["result"]
        without_metadata = {key: value for key, value in catalog.items() if key != "_meta"}
        with patch("codex_local_mcp.mcp_modern.MCP_MODERN_CATALOG_BUDGET", encoded_size(without_metadata)):
            response = await self.request("tools/list", caps={})
        self.assertEqual(self.code(response), "result_too_large")

    async def test_invalid_execution_metadata_is_rejected_before_accepting_native_work(self):
        self.store.db.execute("INSERT INTO metadata VALUES ('mcp_execution:invalid','{}')")
        project = self.root / "metadata"
        self.service.projects("create", str(project))
        response = await self.request("tools/call", {"name": "codex_start", "arguments": {
            "workdir": str(project), "prompt": "hold", "operation_id": "invalid-metadata"}})
        self.assertEqual(response["result"]["structuredContent"]["error"]["code"], "state_invalid")
        self.assertEqual(self.store.all("tasks"), [])
        self.assertFalse(self.service.backend.alive)

    async def test_failure_after_a_native_answer_keeps_partial_outcome_uncertain(self):
        created, record, _ = await self.input_task()
        await self.service.on_message({"id": 778, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": record["thread_id"], "turnId": record["turn_id"], "itemId": "second", "command": "fixture"}})
        pending = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(len(pending["inputRequests"]), 2)
        gate = self.gate_callback("batch-completion", "turn/completed")
        attempts = []
        async def responding(service, name, args, **kwargs):
            attempts.append(args["pending_request_id"])
            if len(attempts) == 2:
                raise BridgeError("Injected response refusal", code="stale_request")
            return await dispatch(service, name, args, **kwargs)
        try:
            with patch("codex_local_mcp.protocol_tasks.dispatch", responding):
                response = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
                    key: {"action": "accept", "content": {"decision": "decline"}} for key in pending["inputRequests"]}})
            error = response["error"]["data"]["error"]
            self.assertEqual(error["code"], "stale_request")
            self.assertTrue(error["uncertain"])
            self.assertFalse(error["retryable"])
            self.assertEqual(len(attempts), 2)
            self.assertIn(self.store.get("requests", attempts[0])["status"], ("answered", "resolved"))
            receipts = [event["data"]["request_id"] for event in self.store.events(0, 100)["events"]
                if event["kind"] == "bridge/response_sent"]
            self.assertEqual(receipts, [attempts[0]])
        finally:
            gate.release.set()

    async def test_new_storage_error_codes_keep_recovery_details_through_modern_tools(self):
        codes = ("state_invalid", "state_newer_schema", "state_migration_failed", "duplicate_thread_id", "operation_expired",
            "cursor_expired", "maintenance_busy", "prune_plan_invalid", "artifact_not_found", "artifact_version_mismatch",
            "artifact_too_large", "result_expired", "maintenance_failed")
        for code in codes:
            with self.subTest(code=code):
                error = BridgeError("Injected storage failure", code=code, details={"resume_after": 12, "applied": False})
                with patch.object(self.service, "get", side_effect=error):
                    response = await self.request("tools/call", {"name": "codex_get", "arguments": {"task_id": "missing"}})
                self.assertEqual(response["result"]["structuredContent"]["error"], error.as_dict())
                self.assertTrue(response["result"]["isError"])

    async def test_cancel_ack_does_not_override_completion_that_won_the_race(self):
        created = await self.task("hold")
        record = read_execution(self.store, created["taskId"])
        gate = self.gate_callback("late-interrupted", "turn/completed")
        observed, processed = self.service.backend.on_message, asyncio.Event()
        async def after_callback(message):
            await observed(message)
            if message.get("method") == "turn/completed":
                processed.set()
        self.service.backend.on_message = after_callback
        try:
            await self.request("tasks/cancel", {"taskId": created["taskId"]})
            await gate.wait()
            await self.service.on_message({"method": "turn/completed", "params": {"threadId": record["thread_id"],
                "turn": {"id": record["turn_id"], "status": "completed", "items": []}}})
            winner = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
            self.assertEqual(winner["status"], "completed")
            self.assertFalse(winner["result"]["isError"])
            gate.release.set()
            await asyncio.wait_for(processed.wait(), 5)
            self.assertEqual((await self.request("tasks/get", {"taskId": created["taskId"]}))["result"], winner)
        finally:
            gate.release.set()

    async def test_operation_retry_recovers_the_same_execution_handle(self):
        first = await self.task("echo")
        record = read_execution(self.store, first["taskId"])
        task = await self.until(record["bridge_task_id"], ("completed",))
        params = {"name": "codex_send", "arguments": {"task_id": task["id"], "prompt": "echo", "operation_id": "retried-follow-up"}}
        created = (await self.request("tools/call", params))["result"]
        await self.until(task["id"], ("completed",))
        response = (await self.request("tools/call", params))["result"]
        self.assertEqual(response["resultType"], "task")
        self.assertEqual(response["taskId"], created["taskId"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM metadata WHERE key GLOB 'mcp_execution:*'").fetchone()[0], 2)

    async def test_cached_follow_up_receipt_survives_deletion_without_restarting_native(self):
        first = await self.task("echo")
        record = read_execution(self.store, first["taskId"])
        task = await self.until(record["bridge_task_id"], ("completed",))
        params = {"name": "codex_send", "arguments": {"task_id": task["id"], "prompt": "echo", "operation_id": "cached-follow-up"}}
        created = (await self.request("tools/call", params))["result"]
        await self.until(task["id"], ("completed",))
        await dispatch(self.service, "codex_delete", {"task_id": task["id"], "confirm_thread_id": task["thread_id"], "operation_id": "delete-cached-task"})
        with patch.object(self.service.backend, "call", wraps=self.service.backend.call) as native:
            response = await self.request("tools/call", params)
        self.assertNotIn("error", response)
        self.assertEqual(response["result"]["taskId"], created["taskId"])
        self.assertFalse(any(call.args[0] == "turn/start" for call in native.call_args_list))


class DoctorReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-", dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / "state"
        store = Store(self.state)
        store.close()

    def report(self):
        findings, facts = [], {}
        diagnostics.database_health(self.state, lambda level, code, message: findings.append((level, code, message)), facts)
        return findings, facts["database"]

    def test_doctor_counts_explicit_backups_as_well_as_migration_backups(self):
        made = backup(SimpleNamespace(state_dir=self.state))
        findings, facts = self.report()
        self.assertIn(Path(made["backup"]).name, [row["name"] for row in facts["backups"]])
        self.assertTrue(any(code == "database_ok" for _, code, _ in findings))

    def test_damaged_current_layout_is_not_reported_healthy(self):
        for mutation in ("DELETE FROM schema_migrations WHERE version=7", "DROP TABLE artifacts"):
            with self.subTest(mutation=mutation):
                path = self.state / "state.sqlite3"
                original = path.read_bytes()
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(mutation)
                findings, facts = self.report()
                self.assertEqual(facts["schema_version"], SCHEMA_VERSION)
                self.assertTrue(any(code == "database_layout_invalid" for _, code, _ in findings), findings)
                self.assertFalse(any(code == "database_ok" for _, code, _ in findings))
                path.write_bytes(original)

    def test_windows_byte_lock_access_error_means_held_not_unknown(self):
        path = self.state / "broker.lock"
        path.write_bytes(b"x")
        fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0, locking=lambda *args: (_ for _ in ()).throw(PermissionError(errno.EACCES, "lock held")))
        with patch("codex_local_mcp.diagnostics.IS_WINDOWS", True), patch.dict("sys.modules", {"msvcrt": fake}), \
                patch("codex_local_mcp.windows.open_fd", side_effect=lambda path, flags, **kwargs: os.open(path, flags)):
            self.assertEqual(diagnostics.lock_status(path), "held")


class PipeReviewTest(unittest.TestCase):
    def test_temporary_io_admission_exhaustion_does_not_permanently_kill_listener(self):
        server = object.__new__(windows.PipeServer)
        closed, attempts = [], []
        server.listener = windows.OwnedPipe(12, closed.append)
        server.stopped = threading.Event()
        # No Win32 calls: the first accept cannot acquire a bounded I/O slot.
        def operation(*args):
            attempts.append(True)
            if len(attempts) == 1:
                budget = windows.AdmissionBudget(1, windows.IO_BUDGET.message)
                budget.acquire()
                budget.acquire()
            server.stopped.set()
            raise ConnectionAbortedError("Stopped after the slot became available")
        with patch("codex_local_mcp.windows.NativeOperation", side_effect=operation):
            server.accept()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(closed, [12])


class DoctorStatusReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_status_reply_stays_a_finding_and_valid_json_report(self):
        case = doctor.DoctorTest()
        await case.asyncSetUp()
        try:
            case.health_mock.side_effect = None
            case.health_mock.return_value = case.healthy(backend=True)
            case.status_mock.side_effect = json.JSONDecodeError("Truncated status", "{", 1)
            report = await asyncio.wait_for(case.report(), 15)
            self.assertIn("signin_unchecked", case.codes(report))
            self.assertNotIn("Truncated status", json.dumps(report))
            case.start_mock.assert_not_awaited()
        finally:
            await asyncio.wait_for(case.asyncTearDown(), 15)


class ExportFileReviewTest(unittest.TestCase):
    def test_windows_existing_file_error_remains_a_file_exists_error(self):
        from tests.incident_samples import sample_incidents
        fixture = next(iter(sample_incidents().values()))
        with tempfile.TemporaryDirectory(dir=temp_root()) as directory:
            destination = Path(directory).resolve() / "exists.json"
            destination.write_bytes(b"original")
            failure = BridgeError("Expected a private user-owned file: exists.json")
            failure.__cause__ = FileExistsError("already exists")
            with patch("codex_local_mcp.incidents.private_file", side_effect=failure):
                with self.assertRaises(FileExistsError):
                    write_export(fixture, destination, preview(fixture)["preview_id"])
            self.assertEqual(destination.read_bytes(), b"original")


class FragmentExportReviewTest(ServiceHarness):
    async def test_large_journal_fragment_exports_and_replays_as_missing_evidence(self):
        task = await self.start_task("echo")
        await self.until(task["id"], ("completed",))
        with self.store.transaction():
            seq = self.store.event(task["id"], "turn/diff/updated", {"threadId": task["thread_id"],
                "turnId": task["turn_id"], "diff": "PRIVATE_FRAGMENT_" * 500000})
        fixture = export_incident(self.store, [task["id"]], after=seq - 1, limit=1)
        self.assertTrue(fixture["partial"])
        event = fixture["events"][0]
        self.assertEqual(event["task"], fixture["tasks"][0]["id"])
        self.assertEqual(event["kind"], "turn/diff/updated")
        self.assertIn("fragment", event["data"])
        self.assertNotIn("PRIVATE_FRAGMENT_", json.dumps(fixture))
        replay = await IncidentReplay(fixture).run()
        self.assertFalse(replay["acknowledged"])
        self.assertFalse(replay["tasks"][0]["evidence_complete"])
        self.assertEqual(replay["tasks"][0]["status"], "needs_resume")

    async def test_expired_export_cursor_gives_the_actual_cli_resume_option(self):
        task = await self.start_task("echo")
        await self.until(task["id"], ("completed",))
        first = self.store.db.execute("SELECT MIN(seq) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        with self.store.transaction():
            self.store.db.execute("INSERT INTO expired_event_ranges VALUES (?,?,?)", (first, first, task["id"]))
            self.store.db.execute("DELETE FROM events WHERE seq=?", (first,))
        with self.assertRaises(BridgeError) as caught:
            export_incident(self.store, [task["id"]])
        self.assertEqual(caught.exception.code, "cursor_expired")
        self.assertIn("--after " + str(first), str(caught.exception))
        self.assertEqual(caught.exception.details["resume_after"], first)
