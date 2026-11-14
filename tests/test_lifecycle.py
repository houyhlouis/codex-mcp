import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.lifecycle import Lifecycle
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store
from tests.helpers import temp_root
from tests.test_service import ServiceHarness


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.addCleanup(lambda: self.store.close())
        self.life = Lifecycle(self.store)
        self.life.create({"id": "task", "thread_id": "thread", "turn_id": None, "status": "idle", "future": {"keep": 1}})

    def task(self):
        return self.store.get("tasks", "task")

    def test_terminal_facts_survive_duplicate_events_late_acks_and_restart(self):
        self.life.begin("task", "one")
        self.life.complete("task", "one", "completed")
        self.life.begin("task", "two")
        self.life.project("task", "two", {"diff": "+ two", "diff_turn_id": "two"})
        self.store.close()
        self.store = Store(self.directory)
        self.life = Lifecycle(self.store)
        self.life.begin("task", "one", source="ack")
        self.life.complete("task", "one", "failed", {"message": "late failure"})
        self.assertEqual(self.life.turn("task", "one")["status"], "completed")
        self.assertEqual(self.task()["turn_id"], "two")
        self.assertEqual(self.task()["projection_turn_id"], "two")
        self.assertEqual(self.task()["diff"], "+ two")

    def test_duplicate_completed_items_do_not_duplicate_command_evidence(self):
        self.life.begin("task", "one")
        item = {"id": "command", "type": "commandExecution", "command": "test", "exitCode": 0}
        self.life.item("task", "one", item)
        self.life.item("task", "one", item)
        self.life.item("task", "one", {**item, "exitCode": 1})
        self.life.complete("task", "one", "completed")
        self.life.begin("task", "one")
        self.life.interrupting(self.life.identity("task"))
        self.assertEqual(len(self.task()["commands"]), 1)
        self.assertEqual(self.task()["commands"][0]["exitCode"], 0)
        self.assertEqual(self.task()["status"], "completed")
        self.assertEqual(self.task()["turn_status"], "completed")

    def test_unknown_late_ack_cannot_replace_a_newer_started_turn(self):
        previous, token = self.life.reserve("task")
        self.life.begin("task", "native-new")
        self.life.begin("task", "late-ack", source="ack", token=token)
        self.assertEqual(self.task()["turn_id"], "native-new")
        self.assertIsNotNone(self.life.turn("task", "late-ack"))
        self.life.begin("task", "late-ack")
        self.assertEqual(self.task()["turn_id"], "native-new")

    def test_projection_identity_revisions_and_unknown_fields_survive_transitions(self):
        self.life.begin("task", "one")
        revision = self.task()["revision"]
        self.life.project("task", "old", {"diff": "stale"})
        self.assertEqual(self.task()["revision"], revision)
        self.life.metadata("task", {"name": "new"})
        self.assertGreater(self.task()["revision"], revision)
        self.assertEqual(self.task()["future"], {"keep": 1})
        with self.assertRaises(BridgeError):
            self.life.metadata("task", {"turn_id": "other"})
        with self.assertRaises(BridgeError):
            self.life.metadata("task", {"summary": "unbound"})
        with self.assertRaises(BridgeError):
            self.life.attach("task", "different-thread")

    def test_recovery_marks_evidence_incomplete_and_a_new_turn_resets_it(self):
        self.life.begin("task", "one")
        self.life.gap("task", "backend_disconnected", disconnected=True)
        self.assertEqual(self.task()["status"], "needs_resume")
        task = self.life.recover("task", {"id": "one", "status": "completed", "items": []}, False,
                                 expected_revision=self.task()["revision"])
        self.assertFalse(task["evidence_complete"])
        self.assertEqual(task["turn_status"], "completed")
        self.life.begin("task", "two")
        self.assertTrue(self.task()["evidence_complete"])
        self.assertEqual(self.task()["evidence_gaps"], [])

    def test_recovery_and_revert_ignore_concurrently_changed_revisions(self):
        self.life.begin("task", "one")
        revision = self.task()["revision"]
        self.life.begin("task", "two")
        self.life.recover("task", {"id": "one", "status": "completed"}, False, expected_revision=revision)
        self.life.reverted("task", revision)
        self.assertEqual(self.task()["turn_id"], "two")
        self.assertEqual(self.task()["status"], "running")

    def test_wait_resolution_and_interrupt_cannot_reopen_a_terminal_turn(self):
        self.life.begin("task", "one")
        identity = self.life.identity("task")
        self.life.wait("task", "one")
        self.life.complete("task", "one", "interrupted")
        self.life.wait("task", "one")
        self.life.answered("task", "one", False)
        self.life.interrupting(identity)
        self.assertEqual(self.task()["status"], "interrupted")

    def test_deleted_task_ignores_late_native_turns(self):
        self.life.begin("task", "one")
        self.life.complete("task", "one", "completed")
        self.life.archive("task", "delete")
        self.life.begin("task", "late")
        self.life.complete("task", "late", "completed")
        self.assertEqual(self.task()["status"], "deleted")
        self.assertEqual(self.task()["turn_id"], "one")

    def test_new_fork_identity_does_not_inherit_parent_projection_binding(self):
        self.life.begin("task", "one")
        child = self.life.create({**self.task(), "id": "fork", "thread_id": "fork-thread", "turn_id": None, "status": "idle"})
        self.assertIsNone(child["projection_turn_id"])
        self.assertIsNone(child["turn_status"])

    def test_terminal_errors_are_redacted_in_turn_facts(self):
        self.life.begin("task", "one")
        self.life.complete("task", "one", "failed", {"access_token": "private-value"})
        self.assertNotIn("private-value", self.life.turn("task", "one")["error"])

    def test_history_terminal_fact_beats_older_resume_activity_snapshot(self):
        self.life.begin("task", "one")
        self.life.gap("task", "backend_disconnected", disconnected=True)
        result = self.life.recover("task", {"id": "one", "status": "completed"}, True,
                                   expected_revision=self.task()["revision"])
        self.assertEqual(result["turn_status"], "completed")
        self.assertNotEqual(result["status"], "running")

    def test_recovery_repairs_a_stale_projection_of_an_already_terminal_turn(self):
        self.life.begin("task", "one")
        self.life.complete("task", "one", "interrupted")
        self.life._write("task", {"status": "needs_resume", "turn_status": "inProgress"})
        result = self.life.recover("task", {"id": "one", "status": "interrupted"}, False,
                                   expected_revision=self.task()["revision"])
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["turn_status"], "interrupted")
        self.assertEqual(result["turn_id"], "one")

    def test_recovery_rebinds_a_retained_terminal_turn_after_history_revert(self):
        self.life.begin("task", "one")
        self.life.complete("task", "one", "interrupted")
        self.life.begin("task", "two")
        self.life.complete("task", "two", "completed")
        self.life.reverted("task", self.task()["revision"])
        result = self.life.recover("task", {"id": "one", "status": "interrupted"}, False,
                                   expected_revision=self.task()["revision"])
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["turn_id"], "one")
        self.assertEqual(result["projection_turn_id"], "one")
        self.assertEqual(result["turn_status"], "interrupted")
        self.assertFalse(result["evidence_complete"])

    def test_returned_projection_lists_cannot_change_a_later_turn_reset(self):
        snapshot = self.life.begin("task", "one")
        snapshot["files"].append("uncommitted")
        self.life.complete("task", "one", "completed")
        self.life.begin("task", "two")
        self.assertEqual(self.task()["files"], [])

    def test_bounded_command_projection_marks_evidence_incomplete(self):
        self.life.begin("task", "one")
        for number in range(101):
            self.life.item("task", "one", {"id": str(number), "type": "commandExecution", "exitCode": 0})
        self.assertEqual(len(self.task()["commands"]), 100)
        self.assertFalse(self.task()["evidence_complete"])
        self.assertIn("command_limit", self.task()["evidence_gaps"])

    def test_unbound_legacy_diff_and_settings_are_hidden_without_dropping_data(self):
        self.life.begin("task", "one")
        with self.store.transaction():
            self.store.update("tasks", "task", {"diff": "+ legacy", "diff_turn_id": None,
                "active_settings": {"approval_mode": "approve_for_me"}})
        view = self.life.view(self.task())
        self.assertEqual(view["diff"], "")
        self.assertEqual(view["active_settings"], {})
        self.assertEqual(self.task()["diff"], "+ legacy")
        self.assertEqual(self.task()["active_settings"]["approval_mode"], "approve_for_me")


class LifecycleIntegrationTest(ServiceHarness):
    async def test_adoption_resume_ack_preserves_a_concurrent_child_rename(self):
        parent = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        child = self.service.get(self.service.requests(parent["id"])[0]["task_id"])
        native = (await self.service.backend.call("thread/read", {"threadId": child["thread_id"], "includeTurns": False}))["thread"]
        self.service.loaded.discard(child["id"])
        gate = self.gate_call("adoption_resume", "thread/resume")
        adopting = asyncio.create_task(self.service.adopt_thread(native, set()))
        await gate.wait()
        await self.service.rename(child["id"], "new-child-name")
        gate.release.set()
        self.assertEqual((await adopting)["name"], "new-child-name")

    async def test_resume_ack_preserves_native_status_that_changed_and_changed_back(self):
        task = await self.start_task("hold")
        initial = self.service.get(task["id"])["native_status"]
        self.service.loaded.discard(task["id"])
        self.service.lifecycle.uncertain(task["id"])
        gate = self.gate_call("resume_ack", "thread/resume")
        resuming = asyncio.create_task(self.service.resume(task["id"]))
        await gate.wait()
        for status in ({"type": "active"}, initial):
            await self.service.on_message({"method": "thread/status/changed", "params": {
                "threadId": task["thread_id"], "status": status}})
        gate.release.set()
        self.assertEqual((await resuming)["native_status"], initial)

    async def test_unknown_review_ack_and_delayed_events_preserve_newer_turn(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        callback = self.gate_callback("review_started", "turn/started")
        ack = self.gate_call("review_ack", "review/start")
        reviewing = asyncio.create_task(self.service.review(task["id"]))
        await ack.wait()
        await callback.wait()
        await self.service.on_message({"method": "turn/started", "params": {
            "threadId": task["thread_id"], "turn": {"id": "native-next", "status": "inProgress"}}})
        ack.release.set()
        self.assertEqual((await reviewing)["turn_id"], "native-next")
        callback.release.set()
        # The old review's completion journal entry is the worker's drain gate.
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: any(
                e["kind"] == "turn/completed" and e["data"].get("turn", {}).get("id") not in (task["turn_id"], "native-next")
                for e in self.store.events(0, 200)["events"])), 5)
        self.assertEqual(self.service.get(task["id"])["turn_id"], "native-next")

    async def test_event_and_new_turn_projection_roll_back_together(self):
        task = await self.start_task("hold")
        before = self.service.task(task["id"])
        with patch.object(self.store, "event", side_effect=RuntimeError("Injected journal failure")):
            with self.assertRaisesRegex(RuntimeError, "journal failure"):
                await self.service.on_message({"method": "turn/started", "params": {
                    "threadId": task["thread_id"], "turn": {"id": "new", "status": "inProgress"}}})
        self.assertEqual(self.service.task(task["id"]), before)
        self.assertIsNone(self.service.lifecycle.turn(task["id"], "new"))

    async def test_controls_never_replace_an_existing_task_or_request_record(self):
        original = self.store.save
        def insert_only(table, value):
            if table in ("tasks", "requests"):
                self.assertIsNone(self.store.db.execute(f"SELECT id FROM {table} WHERE id=?", (value["id"],)).fetchone(),
                    "Controls must use field transitions, never whole-record replacement")
            return original(table, value)
        with patch.object(self.store, "save", insert_only):
            task = await self.start_task("hold")
            gate = self.gate_call("rename_ack", "thread/name/set")
            renaming = asyncio.create_task(self.service.rename(task["id"], "new"))
            await gate.wait()
            await self.service.on_message({"method": "turn/diff/updated", "params": {
                "threadId": task["thread_id"], "turnId": task["turn_id"], "diff": "+ progress"}})
            gate.release.set()
            await renaming
            await self.service.settings(task["id"], "current", task["turn_id"], effort="high")
            await self.service.interrupt(task["id"], task["turn_id"])
            await self.until(task["id"], ("interrupted",))
            await self.service.settings(task["id"], "next", effort="medium")
            await self.service.send(task["id"], "approval")
            request = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
            await self.service.respond(request["id"], {"decision": "decline"})
            completed = await self.until(task["id"], ("completed",))
            fork = await self.service.fork(task["id"], "fork")
            await self.service.archive(fork["id"])
            await self.service.archive(fork["id"], "unarchive")
            await self.service.delete(fork["id"], fork["thread_id"])
            queued = await self.service.queue(task["id"], "add", "queued")
            await self.service.queue(task["id"], "start", queued_id=queued["queuedSubmission"]["id"])
            await self.until(task["id"], ("completed",))
            await self.service.review(task["id"])
            await self.until(task["id"], ("completed",))
            await self.service.compact(task["id"])
            await self.until(task["id"], ("completed",))
            await self.service.revert(task["id"], completed["turn_id"], True)
            await self.service.backend.close()
            await self.service.resume(task["id"])
            parent = await self.start_task("native_children", path="b", key="parent")
            async with self.service.changed:
                await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
            request = self.service.requests(parent["id"])[0]
            await self.service.respond(request["id"], {"decision": "decline"})
            await self.until(parent["id"], ("completed",))
