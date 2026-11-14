import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, Mock

from codex_local_mcp import security
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.service import Service
from codex_local_mcp.backend import AppServer, RpcError
from codex_local_mcp.store import Store, SCHEMA_V1, MIGRATIONS, SCHEMA_VERSION
from tests.helpers import FaultGate, link_or_skip, temp_root
from tests.test_service import ServiceHarness


class PhaseOneReviewTest(ServiceHarness):
    async def request(self, task, kind="permissions", rpc_id=9900, **extra):
        method = {"permissions": "item/permissions/requestApproval", "command": "item/commandExecution/requestApproval"}[kind]
        params = {"threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "review-item", **extra}
        await self.service.on_message({"id": rpc_id, "method": method, "params": params})
        return self.service.requests(task["id"])[-1]

    async def settled(self, task):
        await self.until(task["id"], ("completed",))
        await self.drain_reconciliation()

    async def drain_reconciliation(self):
        await self.reconciliation_idle()

    async def quarantine(self, task):
        await self.service.on_message({"method": "turn/completed", "params": {
            "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "future-status"}}})
        self.assertIn(task["id"], self.service.protocol_quarantine)

    async def test_permissions_use_verified_native_cwd(self):
        task = await self.start_task("hold")
        project = Path(task["project"])
        (project / "allowed").mkdir()
        nested = project / "nested"
        nested.mkdir()
        link_or_skip(self, nested / "allowed", self.base)
        # A verified child may have a cwd below its family's workspace root.
        self.service.lifecycle.metadata(task["id"], {"native_cwd": str(nested)})
        req = await self.request(task, cwd=str(nested), permissions={"fileSystem": {"read": ["allowed"]}})
        with self.assertRaisesRegex(BridgeError, "outside|within"):
            await self.service.respond(req["id"], {"permissions": {"fileSystem": {"read": ["allowed"]}}}, human=True)

    async def test_windows_drive_relative_approval_paths_fail_before_joining(self):
        task = await self.start_task("hold")
        # Keep the host filesystem while exercising the Windows syntax branch.
        # PureWindowsPath joining can make C:relative look project-relative.
        with patch("codex_local_mcp.service.os", SimpleNamespace(name="nt")):
            for value in ("C:relative", "C:..\\outside", "D:relative"):
                with self.subTest(path=value, kind="file"):
                    req = {"kind": "file_approval", "task_id": task["id"]}
                    params = {"proposedChanges": [{"path": value, "kind": {"type": "add"}, "diff": "+ fixture"}]}
                    with self.assertRaisesRegex(BridgeError, "Drive-relative"):
                        self.service.validate_response(req, params, {"decision": "accept"})
                    self.assertEqual(self.service.validate_response(req, params, {"decision": "decline"}), {"decision": "decline"})
                with self.subTest(path=value, kind="permissions"):
                    req = {"kind": "permissions", "task_id": task["id"]}
                    grants = {"fileSystem": {"read": [value]}}
                    with self.assertRaisesRegex(BridgeError, "Drive-relative"):
                        self.service.validate_response(req, {"permissions": grants}, {"permissions": grants})
                    self.assertEqual(self.service.validate_response(req, {"permissions": grants}, {}), {"permissions": {}, "scope": "turn"})

    async def test_permission_paths_are_rechecked_at_native_write(self):
        task = await self.start_task("hold")
        project = Path(task["project"])
        inside = project / "inside"
        inside.mkdir()
        link = project / "approved"
        link_or_skip(self, link, inside)
        grants = {"fileSystem": {"read": ["approved"]}}
        req = await self.request(task, cwd=str(project), permissions=grants)
        entered, original = asyncio.Event(), self.service.backend.respond
        async def waiting(*args, **kwargs):
            entered.set()
            await original(*args, **kwargs)
        self.service.backend.respond = waiting
        with patch.object(self.service.backend.process.stdin, "write", wraps=self.service.backend.process.stdin.write) as written:
            async with self.service.backend.write_lock:
                answering = asyncio.create_task(self.service.respond(req["id"], {"permissions": grants}, human=True))
                await asyncio.wait_for(entered.wait(), 5)
                link.unlink()
                link_or_skip(self, link, self.base)
            with self.assertRaisesRegex(BridgeError, "outside|within"):
                await answering
            self.assertTrue(all("method" in json.loads(call.args[0]) for call in written.call_args_list))
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")
        self.assertEqual((await self.service.respond(req["id"], {"permissions": {}}))["status"], "sent")

    async def test_positive_answer_waits_for_verified_generation_but_denial_is_available(self):
        task = await self.start_task("hold")
        req = await self.request(task, "command", command="printf reviewed", cwd=task["project"])
        self.service.lifecycle.metadata(task["id"], {"verified_generation": self.service.backend.generation - 1})
        with self.assertRaisesRegex(BridgeError, "unverified|verified"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True)
        self.assertEqual((await self.service.respond(req["id"], {"decision": "decline"}))["status"], "sent")

    async def test_enabling_goal_or_queue_reverifies_a_restarted_session(self):
        for control in ("goal", "queue"):
            for stale_generation in (False, True):
                with self.subTest(control=control, stale_generation=stale_generation):
                    key = f"{control}-{stale_generation}"
                    task = await self.start_task(path=key, key=key)
                    await self.settled(task)
                    if stale_generation:
                        self.service.lifecycle.metadata(task["id"], {"verified_generation": self.service.backend.generation - 1})
                    else:
                        self.service.loaded.discard(task["id"])
                    with patch.object(self.service, "verify_thread", side_effect=BridgeError("Rejected unverified scope")):
                        with self.assertRaisesRegex(BridgeError, "unverified scope"):
                            if control == "goal":
                                await self.service.goal(task["id"], "set", objective="Explicit goal", status="active", user_requested=True)
                            else:
                                await self.service.queue(task["id"], "add", "queued")

    async def test_duplicate_native_request_id_cannot_retarget_an_old_review(self):
        first = await self.start_task("hold")
        second = await self.start_task("hold", path="second", key="second")
        req = await self.request(first, "command", command="printf first", cwd=first["project"])
        await self.service.on_message({"id": 9900, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": second["thread_id"], "turnId": second["turn_id"], "itemId": "other", "command": "different command"}})
        with self.assertRaisesRegex(BridgeError, "resolved|expired|protocol"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True)

    async def test_background_reconciliation_does_not_release_protocol_quarantine(self):
        self.service.automatic_reconciliation = True
        task = await self.start_task()
        await self.settled(task)
        await self.quarantine(task)
        self.service.schedule_reconciliation(task["id"])
        await self.reconciliation_idle()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_child_discovery_cannot_implicitly_resume_a_quarantined_child(self):
        parent = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        child = self.service.task(self.service.requests(parent["id"])[0]["task_id"])
        await self.quarantine(child)
        await self.service.subagents(parent["id"])
        self.assertNotIn(child["id"], self.service.loaded)
        self.assertIn(child["id"], self.service.protocol_quarantine)
        self.assertEqual(self.service.task(child["id"])["status"], "needs_resume")

    async def test_child_adoption_cannot_clear_a_protocol_error_during_native_reads(self):
        for index, method in enumerate(("thread/resume", "thread/turns/list")):
            with self.subTest(method=method):
                parent = await self.start_task("native_children", path=f"adoption-{index}", key=f"adoption-{index}")
                async with self.service.changed:
                    await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
                child = self.service.task(self.service.requests(parent["id"])[0]["task_id"])
                await self.drain_reconciliation()
                native = (await self.service.backend.call("thread/read", {"threadId": child["thread_id"]}))["thread"]
                self.service.loaded.discard(child["id"])
                gate = self.gate_call(f"adoption-read-{index}", method)
                adopting = asyncio.create_task(self.service.adopt_thread(native, set()))
                await gate.wait()
                await self.quarantine(child)
                gate.release.set()
                with self.assertRaisesRegex(BridgeError, "protocol error"):
                    await asyncio.wait_for(adopting, 5)
                self.assertNotIn(child["id"], self.service.loaded)
                self.assertEqual(self.service.task(child["id"])["status"], "needs_resume")

    async def test_quarantine_at_native_write_prevents_goal_queue_and_turn_execution(self):
        backend = self.service.backend
        for control, method in (("goal", "thread/goal/set"), ("queue", "thread/queue/add"), ("send", "turn/start")):
            with self.subTest(control=control):
                task = await self.start_task(path=f"write-{control}", key=f"write-{control}")
                await self.settled(task)
                gate = self.gates[f"write-{control}"] = FaultGate(f"write-{control}")
                original = backend.send
                async def gated(message, **kwargs):
                    if message.get("method") == method:
                        await gate.hit()
                    await original(message, **kwargs)
                with patch.object(backend, "send", gated), \
                        patch.object(backend.process.stdin, "write", wraps=backend.process.stdin.write) as written:
                    if control == "goal":
                        call = self.service.goal(task["id"], "set", objective="Explicit goal", status="active", user_requested=True)
                    elif control == "queue":
                        call = self.service.queue(task["id"], "add", "hold")
                    else:
                        call = self.service.send(task["id"], "hold")
                    enabling = asyncio.create_task(call)
                    await gate.wait()
                    await self.quarantine(task)
                    gate.release.set()
                    with self.assertRaisesRegex(BridgeError, "protocol error|unverified"):
                        await asyncio.wait_for(enabling, 5)
                    self.assertFalse(any(json.loads(c.args[0]).get("method") == method for c in written.call_args_list))

    async def test_late_start_ack_does_not_erase_protocol_error(self):
        task = await self.start_task()
        await self.settled(task)
        event = self.gate_callback("start_event", "turn/started")
        ack = self.gate_call("start_ack", "turn/start")
        starting = asyncio.create_task(self.service.turn_control(task, "turn/start", {
            "threadId": task["thread_id"], "input": [{"type": "text", "text": "hold"}]}))
        await event.wait()
        await ack.wait()
        await self.quarantine(task)
        ack.release.set()
        await starting
        latest = self.service.get(task["id"])
        self.assertEqual(latest["status"], "needs_resume")
        self.assertFalse(latest["evidence_complete"])
        self.assertIn("native_protocol_error", latest["evidence_gaps"])

    async def test_resume_cannot_clear_a_newer_protocol_error(self):
        task = await self.start_task()
        await self.settled(task)
        await self.quarantine(task)
        gate = self.gate_call("recovery_history", "thread/turns/list")
        resuming = asyncio.create_task(self.service.resume(task["id"]))
        await gate.wait()
        await self.quarantine(task)
        gate.release.set()
        with self.assertRaisesRegex(BridgeError, "protocol error"):
            await resuming
        self.assertIn(task["id"], self.service.protocol_quarantine)

    async def test_scope_ack_from_an_old_generation_is_not_marked_verified(self):
        task = await self.start_task()
        await self.settled(task)
        generation = self.service.backend.generation
        self.service.loaded.discard(task["id"])
        gate = self.gate_call("scope_ack", "thread/resume")
        resuming = asyncio.create_task(self.service.resume(task["id"]))
        await gate.wait()
        self.service.backend.generation += 1
        gate.release.set()
        with self.assertRaisesRegex(BridgeError, "connection changed"):
            await resuming
        self.assertEqual(self.service.task(task["id"])["verified_generation"], generation)
        self.assertNotIn(task["id"], self.service.loaded)

    async def test_concurrent_fake_requests_keep_their_own_native_ids(self):
        first = await self.start_task("approval")
        req1 = (await self.until(first["id"], ("waiting",)))["pending_requests"][0]
        second = await self.start_task("approval", path="second", key="second")
        req2 = (await self.until(second["id"], ("waiting",)))["pending_requests"][0]
        await self.service.respond(req1["id"], {"decision": "decline"})
        await self.until(first["id"], ("completed",))
        self.assertEqual([r["id"] for r in self.service.requests(second["id"])], [req2["id"]])
        await self.service.respond(req2["id"], {"decision": "decline"})
        await self.until(second["id"], ("completed",))

    async def test_completed_proposals_are_released_and_late_updates_do_not_retain_them(self):
        task = await self.start_task("file_approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        self.assertTrue(self.service.file_proposals)
        await self.service.respond(req["id"], {"decision": "decline"})
        await self.until(task["id"], ("completed",))
        self.assertFalse(self.service.file_proposals)
        await self.service.on_message({"method": "item/fileChange/patchUpdated", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": req["params"]["itemId"],
            "changes": [{"path": "safe.txt", "kind": {"type": "add"}, "diff": "+ late"}]}})
        self.assertFalse(self.service.file_proposals)

    async def test_failed_native_response_does_not_retain_expired_raw_content(self):
        task = await self.start_task("file_approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        with patch.object(self.service.backend, "respond", AsyncMock(side_effect=BridgeError("Injected native write failure"))):
            with self.assertRaisesRegex(BridgeError, "write failure"):
                await self.service.respond(req["id"], {"decision": "decline"})
        self.assertEqual(self.store.get("requests", req["id"])["status"], "expired")
        self.assertNotIn(req["id"], self.service.raw_requests)


class PayloadReviewTest(unittest.TestCase):
    def test_nested_payload_does_not_rescan_patch_text_at_every_parent(self):
        pattern = SimpleNamespace(sub=Mock(side_effect=security._SECRETS[0].sub))
        value = {"items": [{"changes": [{"diff": "x" * 65536 + "sk-"}]}]}
        with patch.object(security, "_SECRETS", [pattern]):
            self.assertEqual(Service.safe_payload(value), value)
        self.assertEqual(pattern.sub.call_count, 1)

    def test_payload_redaction_and_reasoning_removal_preserve_nested_public_fields(self):
        value = {"items": [{"type": "reasoning", "content": ["private"], "summary": "Bearer abcdefghijklmnop"},
                           {"changes": [{"path": "safe.txt", "diff": "+ safe\n+ password=secret"}],
                            "future": {"access_token": "private token", "value": [True, None, 42]}}]}
        expected = {"items": [{"type": "reasoning", "summary": "[REDACTED]"},
                              {"changes": [{"path": "safe.txt", "diff": "+ safe\n+ [REDACTED]"}],
                               "future": {"access_token": "[REDACTED]", "value": [True, None, 42]}}]}
        self.assertEqual(Service.safe_payload(value), expected)
        self.assertEqual(value["items"][0]["content"], ["private"])
        self.assertEqual(value["items"][1]["future"]["access_token"], "private token")


class NativeWriteReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_generation_ack_cannot_change_a_new_connections_capabilities(self):
        for outcome in ({"goal": None}, RpcError({"code": -32601, "message": "Method not found"})):
            with self.subTest(outcome=type(outcome).__name__):
                sent = asyncio.Event()
                backend = AppServer(SimpleNamespace(), AsyncMock(), AsyncMock())
                process = SimpleNamespace(returncode=None)
                backend.process, backend.generation = process, 1
                async def send(message, **kwargs):
                    sent.set()
                backend.send = send
                try:
                    calling = asyncio.create_task(backend.call("thread/goal/get", {}))
                    await asyncio.wait_for(sent.wait(), 5)
                    backend.generation += 1
                    future = backend.pending[backend.next_id]
                    if isinstance(outcome, Exception):
                        future.set_exception(outcome)
                    else:
                        future.set_result(outcome)
                    with self.assertRaisesRegex(BridgeError, "connection changed"):
                        await asyncio.wait_for(calling, 5)
                    self.assertFalse(backend.unavailable_methods)
                    self.assertFalse(backend.pending)
                finally:
                    backend.process = None
                    await backend.close()

    async def test_waiting_native_write_cannot_move_to_a_new_process(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class GateLock:
            async def __aenter__(self):
                entered.set()
                await release.wait()
            async def __aexit__(self, *args):
                pass
        backend = AppServer(SimpleNamespace(), AsyncMock(), AsyncMock())
        first = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))
        second = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()))
        backend.process, backend.generation, backend.write_lock = first, 1, GateLock()
        try:
            sending = asyncio.create_task(backend.send({"id": 1, "method": "thread/goal/set", "params": {}}))
            await asyncio.wait_for(entered.wait(), 5)
            backend.process, backend.generation = second, 2
            release.set()
            with self.assertRaisesRegex(BridgeError, "connection changed"):
                await sending
            first.stdin.write.assert_not_called()
            second.stdin.write.assert_not_called()
        finally:
            backend.process = None
            await backend.close()


class MigrationReviewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))

    def legacy(self, directory):
        os.close(private_file(directory / "state.sqlite3", os.O_CREAT | os.O_RDWR))
        db = sqlite3.connect(directory / "state.sqlite3")
        for statement in SCHEMA_V1:
            db.execute(statement)
        task = {"id": "legacy", "thread_id": "native", "turn_id": "turn", "status": "running",
                "project": str(directory / "project"), "summary": "saved", "future": {"keep": True},
                "model": "test-model", "effort": "medium", "access": "write", "created_at": 1,
                "commands": [], "files": [], "plan": [], "usage": None}
        db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(task)))
        db.execute("INSERT INTO requests VALUES (?,?)", ("request", json.dumps({"id": "request", "task_id": "legacy", "status": "pending"})))
        db.execute("INSERT INTO operations VALUES ('operation','fingerprint','running',NULL,'legacy')")
        db.execute("INSERT INTO events VALUES (41,'legacy','saved',1,'{}')")
        db.commit()
        db.close()

    async def test_active_legacy_upgrade_recovers_and_second_open_does_not_migrate(self):
        with tempfile.TemporaryDirectory(dir=temp_root()) as name:
            directory = Path(name)
            self.legacy(directory)
            store = Store(directory)
            try:
                service = Service(SimpleNamespace(), store)
                try:
                    self.assertEqual(store.get("tasks", "legacy")["status"], "needs_resume")
                    self.assertEqual(store.get("requests", "request")["status"], "expired")
                    self.assertEqual(store.db.execute("SELECT state FROM operations WHERE id='operation'").fetchone()[0], "uncertain")
                    self.assertEqual(store.db.execute("SELECT fingerprint,task_id FROM operations WHERE id='operation'").fetchone()[:], ("fingerprint", "legacy"))
                    self.assertTrue(store.claims(task_id="legacy"))
                    self.assertTrue(store.get("tasks", "legacy")["future"]["keep"])
                    self.assertFalse(service.backend.alive)
                finally:
                    await service.close()
                ledger = [tuple(row) for row in store.db.execute("SELECT * FROM schema_migrations ORDER BY version")]
                backups = list(directory.glob("*.bak"))
                self.assertEqual(len(backups), 1)
            finally:
                store.close()
            reopened = Store(directory)
            try:
                self.assertIsNone(reopened.backup_path)
                self.assertEqual(reopened.schema_version, SCHEMA_VERSION)
                self.assertEqual([tuple(row) for row in reopened.db.execute("SELECT * FROM schema_migrations ORDER BY version")], ledger)
                self.assertEqual(list(directory.glob("*.bak")), backups)
                self.assertTrue(reopened.db.execute("SELECT 1 FROM events WHERE seq=41").fetchone())
            finally:
                reopened.close()

    async def test_keyboard_interrupt_rolls_back_and_closes_the_migration_connection(self):
        with tempfile.TemporaryDirectory(dir=temp_root()) as name:
            directory = Path(name)
            self.legacy(directory)
            connections = []
            def interrupted(db):
                connections.append(db)
                db.execute("CREATE TABLE interrupted(value TEXT)")
                raise KeyboardInterrupt()
            try:
                with patch.dict(MIGRATIONS, {2: ("interrupt", interrupted)}):
                    with self.assertRaises(KeyboardInterrupt):
                        Store(directory)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connections[0].execute("SELECT 1")
                with closing(sqlite3.connect(directory / "state.sqlite3")) as unchanged:
                    self.assertEqual(unchanged.execute("PRAGMA user_version").fetchone()[0], 0)
                    self.assertFalse(unchanged.execute("SELECT 1 FROM sqlite_master WHERE name='interrupted'").fetchone())
            finally:
                for connection in connections:
                    connection.close()
