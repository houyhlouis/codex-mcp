"""Resume commits proven turn facts and process acknowledgement together."""

import asyncio
from unittest.mock import patch

from codex_local_mcp import processes
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness
from tests.helpers import ReconciliationClock


class ResumeProjectionTest(ServiceHarness):
    async def running(self, *, child=False, background=False, path="a", key="start"):
        task = await self.start_task("native_children" if child else "hold_command_background" if background else "hold_command", path=path, key=key)
        if child:
            async with self.service.changed:
                await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(task["id"]))), 5)
            request = self.service.requests(task["id"])[0]
            child_id = request["task_id"]
            await self.service.respond(request["id"], {"decision": "decline"})
            await self.until(task["id"], ("completed",))
            for identity in self.service.family_ids(task["id"]):
                await self.service.reconcile_claims(identity, explicit=True)
            task = await self.service.send(child_id, "hold_command_background" if background else "hold_command")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: any(e["kind"] == "item/started" and
                e["data"].get("turnId") == task["turn_id"] and e["data"].get("item", {}).get("id") == "running-command"
                for e in self.store.events(0, 200, task_id=task["id"])["events"])), 5)
        self.service.lifecycle.project(task["id"], task["turn_id"], {"summary": "Partial answer before disconnect"})
        await self.restart()
        return self.service.get(task["id"])

    async def restart(self):
        await self.service.close()
        self.service = self.new_service()
        await self.service.ensure_backend()

    async def resumed_during_metadata_change(self, task, state="needs_resume"):
        self.service.lifecycle._write(task["id"], {"status": state})
        gate = self.gate_call("history_metadata", "thread/turns/list")
        pending = asyncio.create_task(dispatch(self.service, "codex_resume", {
            "task_id": task["id"], "operation_id": "resume-" + task["id"] + state}))
        await gate.wait()
        self.service.lifecycle.metadata(task["id"], {"name": "Metadata arrived during recovery"})
        gate.release.set()
        return await asyncio.wait_for(pending, 5)

    def assert_recovered(self, result, task):
        current = self.service.get(task["id"])
        for record in (result, current):
            self.assertEqual(record["status"], "idle")
            self.assertEqual(record["turn_status"], "interrupted")
            self.assertEqual(record["turn_id"], task["turn_id"])
            self.assertEqual(record["projection_turn_id"], task["turn_id"])
            self.assertEqual(record["verified_generation"], self.service.backend.generation)
            self.assertEqual(record["name"], "Metadata arrived during recovery")
            self.assertFalse(record["workspace_reserved"])
            self.assertEqual(record["workspace_claims"], [])
            self.assertTrue(record["process_state"]["acknowledged"])
            self.assertEqual(record["summary"], "Partial answer before disconnect")
            self.assertFalse(record["evidence_complete"])
            self.assertIn("broker_restart", record["evidence_gaps"])
        self.assertEqual(result["reconciliation"]["native"]["latest_turn"]["status"], "interrupted")
        self.assertIn("processes_unverified", result["reconciliation"]["unknown"])

    async def test_root_recovery_states_refresh_despite_metadata_changes(self):
        for state in ("needs_resume", "uncertain", "starting"):
            with self.subTest(state=state):
                task = await self.running(path=state, key="start-" + state)
                result = await self.resumed_during_metadata_change(task, state)
                self.assert_recovered(result, task)
                await self.restart()
                self.assertTrue(self.service.get(task["id"])["process_state"]["acknowledged"])
                self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_child_recovery_refreshes_without_changing_family_linkage(self):
        task = await self.running(child=True)
        result = await self.resumed_during_metadata_change(task)
        self.assert_recovered(result, task)
        self.assertEqual(result["parent_task_id"], task["parent_task_id"])
        self.assertEqual(result["root_task_id"], task["root_task_id"])

    async def test_foreground_and_background_acknowledgements_survive_two_more_restarts(self):
        for background in (False, True):
            with self.subTest(background=background):
                task = await self.running(background=background, path=str(background), key="start-" + str(background))
                result = await self.resumed_during_metadata_change(task)
                self.assert_recovered(result, task)
                for _ in range(2):
                    await self.restart()
                    current = self.service.get(task["id"])
                    self.assertEqual(current["status"], "idle")
                    self.assertEqual(current["turn_status"], "interrupted")
                    self.assertTrue(current["process_state"]["acknowledged"])
                    self.assertFalse(current["workspace_reserved"])

    async def test_persistent_recovery_conflict_is_retryable_and_never_acknowledged(self):
        task = await self.running()
        original, reads = self.service.backend.call, []
        async def changing(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "thread/turns/list":
                reads.append(method)
                self.service.lifecycle.metadata(task["id"], {"name": "Concurrent revision " + str(len(reads))})
            return result
        with patch.object(self.service.backend, "call", changing), self.assertRaises(BridgeError) as error:
            await self.service.resume(task["id"])
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertTrue(error.exception.retryable)
        self.assertLessEqual(len(reads), 3)
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])

    async def test_acknowledgement_write_failure_rolls_back_turn_and_claim_release(self):
        task = await self.running()
        before = self.service.get(task["id"])
        acknowledge = processes.acknowledge
        def failed(service, task_id):
            acknowledge(service, task_id)
            raise OSError("Injected acknowledgement write failure")
        with patch.object(processes, "acknowledge", failed), self.assertRaises(OSError):
            await self.service.resume(task["id"])
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], before["status"])
        self.assertEqual(current["turn_status"], before["turn_status"])
        self.assertFalse(current["process_state"]["acknowledged"])
        self.assertTrue(current["workspace_reserved"])

    async def test_acknowledgement_covers_old_status_and_command_evidence(self):
        task = await self.running()
        result = await self.resumed_during_metadata_change(task)
        marker = result["process_state"]
        self.assertEqual(marker["acknowledged_turn_status"], "interrupted")
        self.assertIsInstance(marker["acknowledged_event_seq"], int)
        # A projection restored from an older snapshot must not undo the newer
        # acknowledgement or reinterpret the old command as fresh execution.
        self.service.lifecycle._write(task["id"], {"status": "needs_resume", "turn_status": "inProgress"})
        self.store.claim(task["id"], "turn", "active", {"turn_id": task["turn_id"]})
        await self.restart()
        await self.service.reconcile_startup()
        self.assertTrue(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_new_unfinished_command_after_acknowledgement_reserves_again(self):
        task = await self.running()
        await self.resumed_during_metadata_change(task)
        await self.service.on_message({"method": "item/started", "params": {"threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"type": "commandExecution", "id": "later-command", "command": "later command", "status": "inProgress"}}})
        await self.restart()
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertFalse(current["process_state"]["acknowledged"])
        self.assertIn("later command", current["process_state"]["commands_preview"])

    async def test_new_start_reservation_is_not_covered_by_an_old_acknowledgement(self):
        task = await self.running()
        await self.resumed_during_metadata_change(task)
        self.service.lifecycle.reserve(task["id"])
        await self.restart()
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertFalse(current["process_state"]["acknowledged"])

    async def test_another_explicit_resume_acknowledges_later_verified_command_evidence(self):
        task = await self.running()
        first = await self.resumed_during_metadata_change(task)
        await self.service.on_message({"method": "item/started", "params": {"threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"type": "commandExecution", "id": "later-command", "command": "later command", "status": "inProgress"}}})
        result = await self.service.resume(task["id"])
        self.assertGreater(result["process_state"]["acknowledged_event_seq"], first["process_state"]["acknowledged_event_seq"])
        await self.restart()
        current = self.service.get(task["id"])
        self.assertTrue(current["process_state"]["acknowledged"])
        self.assertFalse(current["workspace_reserved"])

    async def test_new_positive_terminal_observation_after_acknowledgement_reserves_again(self):
        task = await self.running()
        await self.resumed_during_metadata_change(task)
        original = self.service.backend.call
        async def terminals(method, params, **kwargs):
            if method == "thread/backgroundTerminals/list":
                return {"data": [{"processId": "later", "itemId": "later", "command": "later background", "cwd": task["project"]}], "nextCursor": None}
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", terminals):
            await self.service.terminals(task["id"])
        await self.restart()
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertFalse(current["process_state"]["acknowledged"])
        self.assertIn("later background", current["process_state"]["commands_preview"])

    async def test_acknowledgement_without_a_boundary_is_retained_safely(self):
        task = await self.running()
        state = self.service.task(task["id"])["process_state"]
        self.service.lifecycle.metadata(task["id"], {"process_state": {**state, "acknowledged": True, "acknowledged_turn_id": task["turn_id"]}})
        await self.restart()
        current = self.service.get(task["id"])
        self.assertFalse(current["process_state"]["acknowledged"])
        self.assertTrue(current["workspace_reserved"])

    async def test_duplicate_old_command_does_not_reopen_an_acknowledgement(self):
        task = await self.running()
        await self.resumed_during_metadata_change(task)
        await self.service.on_message({"method": "item/started", "params": {"threadId": task["thread_id"], "turnId": task["turn_id"],
            "item": {"type": "commandExecution", "id": "running-command", "command": "fixture sleep", "status": "inProgress"}}})
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "startup"})
        await self.restart()
        await self.service.reconcile_startup()
        self.assertTrue(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_delayed_command_for_an_older_turn_cannot_reserve_the_current_workspace(self):
        task = await self.running()
        await self.resumed_during_metadata_change(task)
        self.store.db.execute("INSERT INTO turns(task_id,thread_id,turn_id,status) VALUES (?,?,?,'completed')",
            (task["id"], task["thread_id"], "older-turn"))
        await self.service.on_message({"method": "item/started", "params": {"threadId": task["thread_id"], "turnId": "older-turn",
            "item": {"type": "commandExecution", "id": "older-command", "command": "old command", "status": "inProgress"}}})
        current = self.service.get(task["id"])
        self.assertEqual(current["turn_id"], task["turn_id"])
        self.assertFalse(current["workspace_reserved"])
        self.assertTrue(current["process_state"]["acknowledged"])

    async def test_native_work_still_running_at_acknowledgement_reserves_again(self):
        task = await self.start_task("hold_command")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: any(e["kind"] == "item/started" and
                e["data"].get("item", {}).get("id") == "running-command" for e in self.store.events(0, 200, task_id=task["id"])["events"])), 5)
        processes.capture(self.service, self.service.task(task["id"]))
        result = await self.service.resume(task["id"])
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertEqual(result["process_state"]["acknowledged_turn_status"], "inProgress")
        self.assertTrue(result["workspace_reserved"])
        await self.restart()
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_close_cancels_a_long_backoff_and_an_inflight_read_without_clock_advance(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        clock = ReconciliationClock(self.service)
        handle = clock.later(task["id"], 300)
        self.service.reconciliation_timers[task["id"]] = handle
        self.service.automatic_reconciliation = True
        gate = self.gate_call("close_during_read", "thread/read", before=True)
        self.service.schedule_reconciliation(task["id"])
        await gate.wait()
        # Include a future retry as well as the live gated read.
        handle = clock.later(task["id"], 300)
        self.service.reconciliation_timers[task["id"]] = handle
        await asyncio.wait_for(self.service.close(), 5)
        self.assertTrue(handle.cancelled())
        self.assertFalse(gate.release.is_set())
        self.assertFalse(self.service.reconciliation_tasks)
        self.assertFalse(clock.pending)
