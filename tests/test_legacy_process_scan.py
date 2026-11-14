"""Legacy process evidence is complete or unknown, never a payload sample."""

import json
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

from codex_local_mcp import processes
from codex_local_mcp.retention import Retention
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.store import SCHEMA_V1, Store
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class LegacyProcessScanTest(ServiceHarness):
    async def fixture(self, items, *, archived=False, legacy_status=None):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"], explicit=True)
        if archived:
            await self.service.archive(task["id"])
        task = self.service.task(task["id"])
        await self.service.close()
        self.store.close()
        legacy = {k: v for k, v in task.items() if k not in ("revision", "project_id", "process_state", "background_observation",
            "process_scan", "projection_turn_id", "turn_status", "evidence_gaps", "evidence_complete")}
        if legacy_status is not None:
            legacy["status"] = legacy_status
        path = self.state / "state.sqlite3"
        path.unlink()
        os.close(private_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        with closing(sqlite3.connect(path)) as db:
            for statement in SCHEMA_V1:
                db.execute(statement)
            db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(legacy)))
            db.executemany("INSERT INTO events(task_id,kind,at,data) VALUES (?,?,1,?)",
                ((task["id"], method, json.dumps({"threadId": task["thread_id"], "turnId": task["turn_id"], "item": item})) for method, item in items))
            db.commit()
        self.store = Store(self.state)
        return task

    async def open_and_reconcile(self):
        self.service = self.new_service()
        await self.service.reconcile_startup()
        await self.reconciliation_idle()

    @staticmethod
    def command(identity="command", **fields):
        return {"id": identity, "type": "commandExecution", "command": "fixture command", "cwd": ".", "status": "inProgress", **fields}

    async def test_oversized_unfinished_command_keeps_the_project(self):
        task = await self.fixture([("item/started", self.command(command="x" * 20000))])
        await self.open_and_reconcile()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="blocked-large-command")

    async def test_unfinished_command_behind_more_than_one_hundred_events_keeps_the_project(self):
        items = [("item/started", self.command())]
        items.extend(("item/completed", {"id": f"message-{i}", "type": "agentMessage", "text": "later message"}) for i in range(150))
        task = await self.fixture(items)
        await self.open_and_reconcile()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_large_completion_is_not_mistaken_for_an_unfinished_command(self):
        task = await self.fixture([("item/started", self.command()),
            ("item/completed", self.command(status="completed", exitCode=0, aggregatedOutput="x" * 70000))])
        await self.open_and_reconcile()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-large-completion")

    async def test_escaped_completion_larger_than_a_transport_frame_is_still_metadata(self):
        # Native UTF-8 can fit its frame while the ASCII journal encoding is larger.
        task = await self.fixture([("item/started", self.command()),
            ("item/completed", self.command(status="completed", exitCode=0, aggregatedOutput="界" * 6000000))])
        await self.open_and_reconcile()
        current = self.service.get(task["id"])
        self.assertTrue(current["process_scan"]["complete"])
        self.assertFalse(current["workspace_reserved"])

    async def test_six_thousand_events_scan_no_python_payloads_and_cache_the_decision(self):
        def items():
            for index in range(3000):
                item = self.command(str(index))
                yield "item/started", item
                yield "item/completed", {**item, "status": "completed", "exitCode": 0,
                    "aggregatedOutput": "x" * (70000 if index % 100 == 0 else 10)}
        task = await self.fixture(items())
        original, decoded = json.loads, []
        def observed(value, *args, **kwargs):
            if isinstance(value, str) and value.startswith('{"threadId"'):
                decoded.append(len(value))
            return original(value, *args, **kwargs)
        with patch.object(processes.json, "loads", observed):
            await self.open_and_reconcile()
        self.assertEqual(decoded, [])
        current = self.service.get(task["id"])
        self.assertFalse(current["workspace_reserved"])
        self.assertTrue(current["process_scan"]["complete"])
        self.assertEqual(current["process_scan"]["unfinished"], 0)
        self.assertNotIn("process_scan", self.service.get(task["id"], view="compact"))
        self.assertNotIn("process_scan", json.dumps(await self.service.events(task_id=task["id"], view="outcome")))
        await self.service.close()
        with patch.object(processes, "scan", side_effect=AssertionError("Cached evidence must not be rescanned"), create=True):
            self.service = self.new_service()
            processes.capture(self.service, self.store.task_head(task["id"]))
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_row_bound_is_unknown_and_does_not_repeat_on_restart(self):
        task = await self.fixture([("item/completed", self.command(str(i), status="completed", exitCode=0)) for i in range(25)])
        with patch.object(processes, "SCAN_ROWS", 10, create=True):
            await self.open_and_reconcile()
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertFalse(current["process_scan"]["complete"])
        self.assertEqual(current["process_scan"]["reason"], "row_limit")
        await self.service.close()
        with patch.object(processes, "scan", side_effect=AssertionError("Unknown evidence must not be retried automatically")):
            self.service = self.new_service()
            await self.service.reconcile_startup()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_time_bound_is_unknown_without_sleeping(self):
        task = await self.fixture([("item/completed", self.command(str(i), status="completed", exitCode=0)) for i in range(25)])
        ticks = iter([0, 3])
        with patch.object(processes, "monotonic", side_effect=lambda: next(ticks, 3), create=True):
            await self.open_and_reconcile()
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertEqual(current["process_scan"]["reason"], "time_limit")

    async def test_invalid_item_type_is_unknown_instead_of_no_process_evidence(self):
        task = await self.fixture([("item/started", self.command(type={"legacy": "unknown"}))])
        await self.open_and_reconcile()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(self.service.get(task["id"])["process_scan"]["reason"], "invalid_event")

    async def test_cached_scan_is_not_a_retention_reference(self):
        task = await self.fixture([("item/started", self.command()),
            ("item/completed", self.command(status="completed", exitCode=0, aggregatedOutput="x" * 70000))])
        await self.open_and_reconcile()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        cache = self.service.get(task["id"])["process_scan"]
        with self.store.transaction():
            self.store.db.execute("INSERT INTO operations VALUES ('cached-scan','fp','done',?,?)", (json.dumps({"process_scan": cache}), task["id"]))
        policy = Retention(self.store, clock=lambda: 10**12)
        plan = policy.plan(100)
        self.assertEqual(plan["counts"]["events"], 2)
        policy.commit(plan["plan_id"])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM events WHERE task_id=?", (task["id"],)).fetchone())

    async def test_fork_does_not_inherit_its_sources_scan(self):
        task = await self.fixture([("item/completed", self.command(status="completed", exitCode=0))])
        await self.open_and_reconcile()
        self.assertTrue(self.service.get(task["id"])["process_scan"]["complete"])
        child = await self.service.fork(task["id"])
        self.assertFalse(child.get("process_scan"))

    async def test_archived_unfinished_legacy_task_names_a_working_unarchive_resume_path(self):
        task = await self.fixture([("item/started", self.command())], archived=True)
        await self.open_and_reconcile()
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertTrue(current["archived"])
        listed = next(t for t in self.service.list_tasks()["tasks"] if t["id"] == task["id"])
        self.assertTrue(listed["archived"])
        claim = next(c for c in current["workspace_claims"] if c["data"].get("reason") == "processes_unverified")
        self.assertIn(task["id"], claim["data"]["error"])
        self.assertIn("codex_archive action=unarchive, then codex_resume", claim["data"]["error"])
        with self.assertRaises(BridgeError) as failure:
            await self.start_task("hold", key="blocked-by-archive")
        self.assertIn(task["id"], str(failure.exception))
        self.assertIn("codex_archive action=unarchive, then codex_resume", str(failure.exception))
        self.assertIn("confirm_thread_id=" + task["thread_id"], str(failure.exception))
        with self.assertRaises(BridgeError):
            await dispatch(self.service, "codex_resume", {"task_id": task["id"], "operation_id": "archived-resume-refused"})
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id='archived-resume-refused'").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])
        await dispatch(self.service, "codex_archive", {"task_id": task["id"], "action": "unarchive", "operation_id": "restore-legacy"})
        result = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "operation_id": "ack-legacy"})
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertFalse(result["workspace_reserved"])
        await self.start_task("hold", key="after-archive-ack")

    async def test_archived_unfinished_legacy_task_can_be_confirmed_deleted_directly(self):
        task = await self.fixture([("item/started", self.command())], archived=True)
        await self.open_and_reconcile()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        result = await dispatch(self.service, "codex_delete", {"task_id": task["id"], "confirm_thread_id": task["thread_id"], "operation_id": "delete-archive"})
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-archive-delete")

    async def test_archived_finished_legacy_task_is_free_and_scanned_only_once(self):
        task = await self.fixture([("item/started", self.command()),
            ("item/completed", self.command(status="completed", exitCode=0, aggregatedOutput="x" * 70000))], archived=True)
        await self.open_and_reconcile()
        current = self.service.get(task["id"])
        self.assertTrue(current["archived"])
        self.assertFalse(current["workspace_reserved"])
        self.assertTrue(current["process_scan"]["complete"])
        await self.service.close()
        with patch.object(processes, "scan", side_effect=AssertionError("Finished archives must stay cached")):
            self.service = self.new_service()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_legacy_idle_completed_history_is_not_running_only_because_of_migration(self):
        task = await self.fixture([("item/started", self.command()),
            ("item/completed", self.command(status="completed", exitCode=0, aggregatedOutput="x" * 70000))], legacy_status="idle")
        await self.open_and_reconcile()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertTrue(self.service.get(task["id"])["process_scan"]["complete"])

    async def test_many_archives_cache_without_starting_native_or_repeating_scans(self):
        task = await self.fixture([], archived=True)
        source = self.store.get("tasks", task["id"])
        with self.store.transaction():
            for number in range(250):
                identity, turn = f"archived-{number}", f"turn-{number}"
                value = {**source, "id": identity, "thread_id": f"thread-{number}", "root_task_id": identity,
                    "parent_task_id": None, "turn_id": turn, "projection_turn_id": turn, "process_scan": None}
                value.pop("revision", None)
                self.store.save("tasks", value)
                self.store.db.execute("INSERT INTO events(task_id,kind,at,data) VALUES (?,'item/completed',1,?)",
                    (identity, json.dumps({"threadId": value["thread_id"], "turnId": turn,
                        "item": self.command(status="completed", exitCode=0)})))
        self.service = self.new_service()
        self.assertFalse(self.service.backend.alive)
        self.assertFalse(self.store.claims())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(projection,'$.process_scan.complete')=1").fetchone()[0], 251)
        self.assertTrue(all("process_scan" not in t for t in self.service.list_tasks(view="compact")["tasks"]))
        await self.service.close()
        with patch.object(processes, "scan", side_effect=AssertionError("Cached archives scanned twice")):
            self.service = self.new_service()
        self.assertFalse(self.store.claims())
