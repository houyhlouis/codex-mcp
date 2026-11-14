"""Deletion tolerates bridge bookkeeping, never concurrent lifecycle work."""

import asyncio
from unittest.mock import patch

from codex_local_mcp import processes
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from codex_local_mcp.service import ADMISSION_RECONCILE_SECONDS
from tests.helpers import FaultGate, ReconciliationClock
from tests.test_missing_conversations import MissingConversationHarness


class DeleteBookkeepingTest(MissingConversationHarness):
    async def test_real_notification_during_first_successful_read_refuses_before_delete(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"], explicit=True)
        gate = self.gate_call("first-successful-read", "thread/read")
        original, deletes = self.service.backend.call, []
        async def recorded(method, params, **kwargs):
            if method == "thread/delete":
                deletes.append(params)
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", recorded):
            deleting = asyncio.create_task(dispatch(self.service, "codex_delete", {"task_id": task["id"],
                "confirm_thread_id": task["thread_id"], "request_id": "first-read-notification"}))
            await gate.wait()
            await self.service.on_message({"method": "thread/tokenUsage/updated", "params": {
                "threadId": task["thread_id"], "tokenUsage": {"total": {"totalTokens": 234}}}})
            gate.release.set()
            with self.assertRaises(BridgeError) as error:
                await asyncio.wait_for(deleting, 5)
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertEqual(deletes, [])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))

    async def fresh_missing(self):
        task = await self.missing()
        await self.service.close()
        self.service = self.new_service()
        self.service.automatic_reconciliation = True
        clock = ReconciliationClock(self.service)
        self.service.reconciliation_timer = clock.later
        return task

    async def test_first_delete_waits_for_startup_outside_control_locks(self):
        task = await self.fresh_missing()
        gate = self.gates["startup"] = FaultGate("startup")
        original = self.service.backend.call
        async def gated(method, params, **kwargs):
            if method == "thread/read":
                if self.service.current_operation.get() is None and not gate.used:
                    await gate.hit()
                else:
                    await gate.release.wait()
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", gated):
            deleting = asyncio.create_task(dispatch(self.service, "codex_delete", {"task_id": task["id"],
                "confirm_thread_id": task["thread_id"], "request_id": "first-delete"}))
            try:
                await gate.wait()
                self.assertFalse(deleting.done())
                self.assertFalse(self.service.admission_lock.locked())
                self.assertFalse(any(lock.locked() for lock in self.service.family_locks.values()))
                self.assertFalse(self.store.db.in_transaction)
            finally:
                gate.release.set()
            result = await asyncio.wait_for(deleting, 5)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["native_conversation"], "unavailable")
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_late_startup_cache_write_between_absence_proof_and_commit_is_retried(self):
        task = await self.missing()
        self.service.lifecycle.metadata(task["id"], {"process_scan": None})
        original, reads, deletes = self.service.backend.call, [], []
        async def refreshing(method, params, **kwargs):
            if method == "thread/delete":
                deletes.append(params)
            if method == "thread/read":
                reads.append(method)
                if len(reads) == 2:
                    processes.command_evidence(self.service, self.store.task_head(task["id"]), False)
                    claim = self.service.claim_snapshot(task["id"], "uncertain")
                    self.store.claim(task["id"], "uncertain", claim["state"], claim["data"])
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", refreshing):
            result = await dispatch(self.service, "codex_delete", {"task_id": task["id"],
                "confirm_thread_id": task["thread_id"], "request_id": "late-cache"})
        self.assertTrue(result["deleted"])
        self.assertEqual(result["native_conversation"], "unavailable")
        self.assertGreaterEqual(len(reads), 3)
        self.assertEqual(deletes, [])

    async def test_new_claim_reason_during_final_read_still_refuses(self):
        task = await self.missing()
        original, reads = self.service.backend.call, []
        async def changed(method, params, **kwargs):
            if method == "thread/read":
                reads.append(method)
                if len(reads) == 2:
                    self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "new-execution-intent"})
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", changed), self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_delete", {"task_id": task["id"],
                "confirm_thread_id": task["thread_id"], "request_id": "real-change"})
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_revert_applies_its_projection_after_a_cache_write_during_ack(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("revert-cache", "thread/revert")
        reverting = asyncio.create_task(self.service.revert(task["id"], task["turn_id"], True))
        await gate.wait()
        self.service.lifecycle.metadata(task["id"], {"process_scan": {"version": 99}})
        gate.release.set()
        await asyncio.wait_for(reverting, 5)
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], "idle")
        self.assertIsNone(current["turn_id"])
        self.assertEqual(current["commands"], [])

    async def test_bookkeeping_churn_is_bounded_and_never_calls_native_delete_for_absence(self):
        task = await self.missing()
        original, reads, deletes = self.service.backend.call, [], []
        async def churning(method, params, **kwargs):
            if method == "thread/delete":
                deletes.append(params)
            if method == "thread/read":
                reads.append(method)
                self.service.lifecycle.metadata(task["id"], {"process_scan": {"version": len(reads)}})
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", churning), self.assertRaises(BridgeError) as error:
            await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertLessEqual(len(reads), 4)
        self.assertEqual(deletes, [])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))

    async def test_startup_wait_ends_when_this_family_finishes_not_the_whole_startup_worker(self):
        first = await self.missing()
        second = await self.start_task(path="b", key="other-family")
        await self.until(second["id"], ("completed",))
        await self.service.close()
        self.store.claim(second["id"], "uncertain", "uncertain", {"reason": "schema_upgrade"})
        self.service = self.new_service()
        gates = [self.gates.setdefault(name, FaultGate(name)) for name in ("first-startup", "other-startup")]
        original = self.service.backend.call
        async def gated(method, params, **kwargs):
            if method == "thread/read" and self.service.current_operation.get() is None:
                for task, gate in zip((first, second), gates):
                    if params["threadId"] == task["thread_id"] and not gate.used:
                        await gate.hit()
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", gated), patch.object(self.store, "claim_task_ids", return_value=[first["id"], second["id"]]):
            startup = asyncio.create_task(self.service.reconcile_startup())
            deleting = None
            try:
                await gates[0].wait()
                deleting = asyncio.create_task(dispatch(self.service, "codex_delete", {"task_id": first["id"],
                    "confirm_thread_id": first["thread_id"], "request_id": "scoped-startup-delete"}))
                gates[0].release.set()
                await gates[1].wait()
                result = await asyncio.wait_for(deleting, 5)
                self.assertTrue(result["deleted"])
                self.assertFalse(startup.done())
            finally:
                for gate in gates:
                    gate.release.set()
                await asyncio.gather(startup, *([deleting] if deleting else []), return_exceptions=True)

    async def test_wait_deadline_leaves_startup_running_and_does_not_send_delete(self):
        task = await self.fresh_missing()
        gate = self.gate_call("deadline-startup", "thread/read", before=True)
        original, deadlines, deletes = self.service.backend.call, [], []
        native_wait_for = asyncio.wait_for
        async def deadline(coroutine, timeout):
            if getattr(coroutine, "cr_code", None) and coroutine.cr_code.co_qualname.endswith("settle_family.<locals>.wait"):
                deadlines.append(timeout)
                waiting = asyncio.create_task(coroutine)
                await native_wait_for(gate.entered.wait(), 5)
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                raise TimeoutError("Injected family deadline")
            return await native_wait_for(coroutine, timeout)
        async def recorded(method, params, **kwargs):
            if method == "thread/delete":
                deletes.append(params)
            return await original(method, params, **kwargs)
        with patch("codex_local_mcp.service.asyncio.wait_for", deadline), patch.object(self.service.backend, "call", recorded):
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_delete", {"task_id": task["id"],
                    "confirm_thread_id": task["thread_id"], "request_id": "bounded-delete"})
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertEqual(deadlines, [ADMISSION_RECONCILE_SECONDS])
        self.assertTrue(any(not job.done() for job in self.service.reconciliation_tasks))
        self.assertFalse(any(lock.locked() for lock in self.service.family_locks.values()))
        self.assertEqual(deletes, [])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))
        gate.release.set()
        await self.reconciliation_idle()

    async def test_completed_native_delete_is_not_sent_again_after_late_bookkeeping(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        original, deletes = self.service.backend.call, []
        async def changed(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "thread/delete":
                deletes.append(params)
                self.service.lifecycle.metadata(task["id"], {"process_scan": {"version": 99}})
            return result
        with patch.object(self.service.backend, "call", changed):
            result = await self.service.delete(task["id"], task["thread_id"])
        self.assertTrue(result["deleted"])
        self.assertEqual(result["native_conversation"], "deleted")
        self.assertEqual(len(deletes), 1)

    async def assert_real_change_refuses(self, mutation):
        task = await self.missing()
        original, reads = self.service.backend.call, []
        async def changed(method, params, **kwargs):
            if method == "thread/read":
                reads.append(method)
                if len(reads) == 2:
                    mutation(task)
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", changed), self.assertRaises(BridgeError) as error:
            await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))
        self.assertFalse(self.store._bookkeeping)

    async def test_explicit_resume_receipt_conflicts_even_without_a_task_revision_change(self):
        await self.assert_real_change_refuses(lambda task: self.store.event(task["id"], "bridge/resumed", {"message": "Explicit resume completed"}))

    async def test_unknown_compatible_fields_are_not_bookkeeping(self):
        await self.assert_real_change_refuses(lambda task: self.store.update("tasks", task["id"], {"future_field": "new value"}))

    async def test_a_real_write_then_restore_cannot_be_hidden_by_equal_final_values(self):
        def restored(task):
            self.service.lifecycle.metadata(task["id"], {"name": "changed"})
            self.service.lifecycle.metadata(task["id"], {"name": task.get("name")})
        await self.assert_real_change_refuses(restored)

    async def test_rolled_back_cache_revision_cannot_hide_a_real_write_reusing_it(self):
        def rolled_back(task):
            try:
                with self.store.transaction():
                    self.service.lifecycle.metadata(task["id"], {"process_scan": {"version": 99}})
                    raise ValueError("Injected rollback")
            except ValueError:
                pass
            self.service.lifecycle.metadata(task["id"], {"name": "real change"})
        await self.assert_real_change_refuses(rolled_back)
