"""A new app-server cannot prove that the old one's OS processes stopped."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

from codex_local_mcp.processes import MESSAGE
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.store import SCHEMA_V1, Store
from codex_local_mcp.tools import BY_NAME, dispatch, validate
from tests.helpers import ReconciliationClock
from tests.test_service import ServiceHarness


class ProcessRecoveryTest(ServiceHarness):
    automatic_reconciliation = True

    async def assert_quarantined(self, task):
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            await self.service.ensure_backend()
            await self.reconciliation_idle()
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            claim = next(c for c in self.store.claims(task_id=task["id"]) if c["kind"] == "uncertain")
            self.assertEqual(claim["data"]["reason"], "processes_unverified")
            self.assertEqual(claim["data"]["error"], MESSAGE)
            before = self.service.backend.next_id
            revisions = [(c["kind"], c["revision"]) for c in self.store.claims(task_id=task["id"])]
            for _ in range(3):
                self.service.periodic_reconciliation(task["id"])
                clock.advance()
                await self.reconciliation_idle()
            self.assertFalse(clock.pending)
            self.assertEqual(self.service.backend.next_id, before)
            self.assertEqual([(c["kind"], c["revision"]) for c in self.store.claims(task_id=task["id"])], revisions)
            with self.assertRaisesRegex(BridgeError, "system process list") as refused:
                await self.start_task("hold", key="blocked-old-processes")
            self.assertNotIn("codex_terminals", str(refused.exception))
            self.assertNotIn("codex_terminals", json.dumps(refused.exception.next_steps))
            with self.assertRaisesRegex(BridgeError, "system process list"):
                await self.service.send(task["id"], "hold")
            await self.service.wait_for_reconciliation([task["id"]])
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            self.assertEqual(self.service.backend.next_id, before)

    async def test_background_process_record_survives_restart_until_public_resume(self):
        task = await self.start_task("hold_command_background")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: any(e["kind"] == "item/started" and
                e["data"].get("item", {}).get("id") == "running-command" for e in self.store.events(0, 200, task_id=task["id"])["events"])), 5)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.reconciliation_idle()
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        result = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "operation_id": "ack-old-processes"})
        validate(result, BY_NAME["codex_resume"]["outputSchema"])
        self.assertFalse(result["workspace_reserved"])
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertTrue(result["process_state"]["commands_preview"])
        self.assertIn("processes_unverified", result["reconciliation"]["unknown"])
        self.assertIsNone(result["reconciliation"]["native"]["background_terminals"])
        self.assertFalse(result["reconciliation"]["native"]["background_terminals_checked"])
        self.assertEqual(result["reconciliation"]["native"]["current_backend_background_terminals"], 0)
        await self.service.backend.close()
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        self.assertIsNone(report["native"]["background_terminals"])
        self.assertFalse(report["native"]["background_terminals_checked"])
        self.assertIsNone(report["native"]["current_backend_background_terminals"])
        await self.service.resume(task["id"])
        compact = self.service.get(task["id"], view="compact")
        self.assertNotIn("commands_preview", compact["process_state"])
        await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "operation_id": "after-ack"})
        self.assertNotIn("processes_unverified", self.service.get(task["id"], view="reconciliation")["reconciliation"]["unknown"])

    async def test_foreground_crash_is_interrupted_in_storage_but_does_not_release(self):
        task = await self.start_task("hold_command")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: any(e["kind"] == "item/started" and
                e["data"].get("item", {}).get("id") == "running-command" for e in self.store.events(0, 200, task_id=task["id"])["events"])), 5)
        self.service.backend.process.kill()
        await asyncio.wait_for(self.service.backend.reader_task, 5)
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        native = await self.service.backend.call("thread/turns/list", {"threadId": task["thread_id"], "limit": 1,
            "sortDirection": "desc", "itemsView": "summary"})
        self.assertEqual(native["data"][0]["status"], "interrupted")
        self.assertFalse(self.service.loaded)
        result = await self.service.resume(task["id"])
        self.assertFalse(result["workspace_reserved"])
        self.assertIn("processes_unverified", result["reconciliation"]["unknown"])
        self.assertIsNone(result["reconciliation"]["native"]["background_terminals"])
        await self.start_task("hold", key="new-family-after-crash-ack")

    async def test_command_previews_are_bounded_redacted_preserved_and_absent_from_outcomes(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        original = self.service.backend.call
        secret = "sk-abcdefghijklmnopqrstuv"
        async def terminals(method, params, **kw):
            if method == "thread/backgroundTerminals/list":
                return {"data": [{"processId": str(i), "command": f"echo {secret} {i} " + "x" * 300,
                    "itemId": str(i), "cwd": task["project"]} for i in range(8)], "nextCursor": None}
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", terminals):
            await self.service.reconcile_claims(task["id"], explicit=True)
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        state = self.service.get(task["id"])["process_state"]
        self.assertEqual(len(state["commands_preview"]), 5)
        self.assertTrue(state["commands_truncated"])
        self.assertNotIn(secret, json.dumps(state))
        self.assertIn("[REDACTED]", state["commands_preview"][0])
        self.assertTrue(all(len(c) <= 256 for c in state["commands_preview"]))
        outcome = await self.service.events(task_id=task["id"], view="outcome")
        self.assertNotIn("commands_preview", json.dumps(outcome))
        result = await self.service.resume(task["id"])
        self.assertEqual(result["process_state"]["commands_preview"], state["commands_preview"])
        self.assertTrue(result["process_state"]["commands_truncated"])

    async def test_root_resume_does_not_acknowledge_a_childs_previous_processes(self):
        root = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(root["id"]))), 5)
        request = self.service.requests(root["id"])[0]
        child = self.service.task(request["task_id"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(root["id"], ("completed",))
        await self.reconciliation_idle()
        self.store.claim(child["id"], "background", "active", {"commands_preview": ["known child command"]})
        await self.service.close()
        self.service = self.new_service()
        result = await self.service.resume(root["id"])
        self.assertTrue(result["workspace_reserved"])
        self.assertFalse(self.service.get(child["id"])["process_state"]["acknowledged"])
        steps = result["reconciliation"]["next_steps"]
        self.assertTrue(any(s["tool"] == "codex_resume" and s["arguments"]["task_id"] == child["id"]
            and "system process list" in s["reason"] for s in steps))
        result = await self.service.resume(child["id"])
        self.assertFalse(result["workspace_reserved"])

    async def test_failed_resume_and_internal_controls_cannot_acknowledge_previous_processes(self):
        task = await self.start_task("hold")
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        gate = self.gate_call("lost_ack", "thread/resume", error=TimeoutError("Lost resume acknowledgement"))
        resuming = asyncio.create_task(self.service.resume(task["id"]))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(resuming, 5)
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
        with self.assertRaisesRegex(BridgeError, "system process list"):
            await self.service.resume(task["id"], rebuild=False)
        with self.assertRaisesRegex(BridgeError, "system process list"):
            await self.service.archive(task["id"])
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_legacy_completed_releases_but_legacy_running_is_quarantined(self):
        for index, running in enumerate((False, True)):
            with self.subTest(running=running):
                task = await self.start_task("hold" if running else "echo", path="legacy-" + str(index), key="legacy-" + str(index))
                if not running:
                    task = await self.until(task["id"], ("completed",))
                legacy = {k: v for k, v in task.items() if k not in ("process_state", "background_observation", "project_id", "revision", "workspace_claims", "workspace_reserved", "pending_requests")}
                await self.service.close()
                self.store.close()
                path = self.state / "state.sqlite3"
                path.unlink()
                os.close(private_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
                with closing(sqlite3.connect(path)) as db:
                    for statement in SCHEMA_V1:
                        db.execute(statement)
                    db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(legacy)))
                    db.commit()
                self.store = Store(self.state)
                self.service = self.new_service()
                await self.service.reconcile_startup()
                await self.reconciliation_idle()
                self.assertEqual(self.service.get(task["id"])["workspace_reserved"], running)

    async def test_upgrade_with_an_unfinished_journal_command_retains_ownership(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.store.event(task["id"], "item/started", {"threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"type": "commandExecution", "id": "unclosed-command", "status": "inProgress", "command": "unfinished legacy command"}})
        await self.service.close()
        # Model the records a pre-claims release could leave at upgrade time.
        self.store.update("tasks", task["id"], {"process_state": None, "background_observation": None})
        for claim in self.store.claims(task_id=task["id"]):
            self.store.claim(task["id"], claim["kind"], "inactive")
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "schema_upgrade"})
        self.service = self.new_service()
        await self.assert_quarantined(task)
        self.assertIn("unfinished legacy command", self.service.get(task["id"])["process_state"]["commands_preview"])

    async def test_explicit_delete_acknowledges_process_warning_atomically(self):
        task = await self.start_task("hold_command_background")
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.reconciliation_idle()
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        result = await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(result["native_conversation"], "deleted")
        self.assertTrue(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="new-family-after-process-delete")

    async def test_positive_terminal_list_is_persisted_before_unrelated_reconciliation_can_fail(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        original = self.service.backend.call
        async def partial(method, params, **kw):
            if method == "thread/backgroundTerminals/list":
                return {"data": [{"processId": "old-process", "command": "known background command", "itemId": "old-item", "cwd": task["project"]}], "nextCursor": None}
            if method == "thread/goal/get":
                raise BridgeError("Goal check unavailable", code="transport_unavailable")
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", partial):
            await self.service.terminals(task["id"])
        claim = next(c for c in self.store.claims(task_id=task["id"]) if c["kind"] == "background")
        self.assertEqual(claim["state"], "active")
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        self.assertIn("known background command", self.service.get(task["id"])["process_state"]["commands_preview"])

    async def test_a_later_empty_terminal_list_supersedes_prior_process_evidence(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        original, active = self.service.backend.call, True
        async def partial(method, params, **kw):
            if method == "thread/backgroundTerminals/list":
                return {"data": [{"processId": "old", "command": "finished background", "itemId": "old", "cwd": task["project"]}] if active else [], "nextCursor": None}
            if method == "thread/goal/get":
                raise BridgeError("Goal temporarily unavailable", code="transport_unavailable")
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", partial):
            await self.service.terminals(task["id"])
            active = False
            await self.service.terminals(task["id"])
        await self.service.close()
        self.service = self.new_service()
        await self.service.reconcile_startup()
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertFalse((self.service.get(task["id"]).get("process_state") or {}).get("previous_backend_unverified"))

    async def test_positive_terminal_evidence_survives_a_claim_revision_conflict(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.service.automatic_reconciliation = False
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        original = self.service.backend.call
        async def concurrent_change(method, params, **kw):
            if method == "thread/backgroundTerminals/list":
                self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
                return {"data": [{"processId": "running", "command": "live despite revision conflict", "itemId": "running", "cwd": task["project"]}], "nextCursor": None}
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", concurrent_change):
            await self.service.reconcile_claims(task["id"])
        self.assertEqual(self.service.claim_snapshot(task["id"], "background")["state"], "active")
        self.assertTrue(self.service.get(task["id"])["background_observation"]["active"])
        await self.service.close()
        self.service = self.new_service()
        await self.assert_quarantined(task)
        self.assertIn("live despite revision conflict", self.service.get(task["id"])["process_state"]["commands_preview"])

    async def test_an_older_terminal_reply_cannot_replace_a_newer_empty_observation(self):
        task = await self.start_task("hold_command_background")
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.reconciliation_idle()
        older = self.gate_call("older_terminal_list", "thread/backgroundTerminals/list")
        listing = asyncio.create_task(self.service.terminals(task["id"]))
        await older.wait()
        await self.service.terminals(task["id"], "clean")
        self.assertFalse(self.service.get(task["id"])["background_observation"]["active"])
        older.release.set()
        await asyncio.wait_for(listing, 5)
        self.assertFalse(self.service.get(task["id"])["background_observation"]["active"])
