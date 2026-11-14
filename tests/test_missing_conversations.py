"""Missing stored conversations stay owned until a checked explicit deletion."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from codex_local_mcp.backend import OptionalCapabilityUnavailable, RpcError
from codex_local_mcp.retention import Retention
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch, result_size, RESULT_BUDGET
from tests.helpers import ReconciliationClock
from tests.test_service import ServiceHarness


class MissingConversationHarness(ServiceHarness):
    async def contact_lost(self, *, missing=True):
        task = await self.start_task("hold")
        await self.service.close()
        if missing:
            self.store.update("tasks", task["id"], {"thread_id": "missing-running-conversation"})
        self.service = self.new_service()
        await self.service.ensure_backend()
        return self.service.get(task["id"])

    async def missing(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.close()
        self.store.update("tasks", task["id"], {"thread_id": "missing-conversation"})
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "stored_state_unavailable",
            "error": "Stored conversation could not be read; workspace ownership was retained"})
        self.service = self.new_service()
        await self.service.ensure_backend()
        await self.service.reconcile_claims(task["id"], explicit=True)
        return self.service.get(task["id"])


class MissingConversationTest(MissingConversationHarness):
    async def test_goal_pause_retries_a_conflicting_older_failure_observation(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"])
        await self.service.goal(task["id"], "set", objective="Continue", user_requested=True)
        gate = self.gate_call("older-failure-during-pause", "thread/goal/get")
        pausing = asyncio.create_task(self.service.goal(task["id"], "set", status="paused", user_requested=True))
        await gate.wait()
        self.service.reconciliation_failed(task["id"], "native_state_changed", error="Older native snapshot changed")
        gate.release.set()
        await asyncio.wait_for(pausing, 5)
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_unknown_resume_uses_native_error_and_bridge_resume_is_plain(self):
        task = await self.missing()
        with self.assertRaises(RpcError) as native:
            await self.service.backend.call("thread/resume", {"threadId": task["thread_id"], "excludeTurns": True})
        self.assertEqual(native.exception.error, {"code": -32600, "message": "no rollout found for thread id " + task["thread_id"]})
        with self.assertRaises(BridgeError) as error:
            await self.service.resume(task["id"])
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertIn("codex_delete", str(error.exception))
        self.assertNotIn('{"code"', str(error.exception))
        with self.assertRaises(BridgeError) as archived:
            await self.service.archive(task["id"])
        self.assertEqual(archived.exception.code, "state_conflict")
        self.assertIn("codex_delete", str(archived.exception))
        with self.assertRaises(BridgeError) as busy:
            await self.start_task("hold", key="missing-refusal")
        self.assertEqual(busy.exception.code, "workspace_busy")
        self.assertIn("confirm_thread_id=" + task["thread_id"], str(busy.exception))
        steps = self.service.get(task["id"], view="reconciliation")["reconciliation"]["next_steps"]
        self.assertTrue(any(step["tool"] == "codex_delete" and step["arguments"]["confirm_thread_id"] == task["thread_id"] for step in steps))

    async def test_exact_delete_of_proven_unavailable_task_releases_and_can_expire_its_data(self):
        task = await self.missing()
        original, native_deletes = self.service.backend.call, []
        async def observed(method, params, **kw):
            if method == "thread/delete":
                native_deletes.append(params)
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", observed):
            result = await dispatch(self.service, "codex_delete", {"task_id": task["id"],
                "confirm_thread_id": task["thread_id"], "operation_id": "forget-missing"})
        self.assertEqual(native_deletes, [])
        self.assertEqual(result["native_conversation"], "unavailable")
        self.assertEqual(result["native_conversations_unavailable"], 1)
        current = self.service.get(task["id"])
        self.assertTrue(current["deleted"])
        self.assertFalse(current["workspace_reserved"])
        self.assertEqual(current["native_conversation"], "unavailable")
        deleted = [e for e in self.store.events(0, 200, task_id=task["id"])["events"] if e["kind"] == "bridge/deleted"]
        self.assertEqual(deleted[-1]["data"]["native_conversation"], "unavailable")
        # A pre-existing uncertain operation remains unknown, but no longer
        # owns the rest of an explicitly deleted family's payload.
        with self.store.transaction():
            self.store.db.execute("INSERT INTO operations VALUES ('old-uncertain','fp','uncertain','{}',?)", (task["id"],))
            self.store.db.execute("UPDATE events SET at=1")
            self.store.db.execute("UPDATE turn_results SET created_at=1")
            self.store.db.execute("UPDATE operation_times SET updated_at=1")
        policy = Retention(self.store, clock=lambda: 10**12)
        plan = policy.plan(100)
        self.assertGreater(plan["counts"]["events"], 0)
        self.assertEqual(plan["protected"]["unresolved_operations"]["items"], 1)
        policy.commit(plan["plan_id"])
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='old-uncertain'").fetchone()[0], "uncertain")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0], 0)
        following = await self.start_task("hold", key="after-forgetting")
        self.assertNotEqual(following["id"], task["id"])

    async def test_delete_refuses_unknown_loaded_ids_and_reads_every_page(self):
        task = await self.missing()
        original, cursors = self.service.backend.call, []
        async def unknown_loaded(method, params, **kw):
            if method == "thread/loaded/list":
                cursors.append(params.get("cursor"))
                return {"data": [] if not params.get("cursor") else ["unadopted-loaded-thread"],
                    "nextCursor": "last" if not params.get("cursor") else None}
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", unknown_loaded), self.assertRaises(BridgeError) as refused:
            await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(refused.exception.code, "state_conflict")
        self.assertEqual(cursors, [None, "last"])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_loaded_or_capability_failures_cannot_prove_absence(self):
        task = await self.missing()
        original = self.service.backend.call
        for variant in ("loaded", "list_failed", "read_capability", "missing_cursor", "bad_cursor"):
            async def unavailable(method, params, **kw):
                if method == "thread/loaded/list":
                    if variant == "missing_cursor":
                        return {"data": []}
                    if variant == "bad_cursor":
                        return {"data": [], "nextCursor": 0}
                    if variant == "list_failed":
                        raise RpcError({"code": -32601, "message": "fixture unavailable"})
                    return {"data": [task["thread_id"]] if variant == "loaded" else [], "nextCursor": None}
                if method == "thread/read" and variant == "read_capability":
                    raise RpcError({"code": -32601, "message": "fixture unavailable"})
                return await original(method, params, **kw)
            with self.subTest(variant=variant), patch.object(self.service.backend, "call", unavailable):
                with self.assertRaises(BridgeError):
                    await self.service.delete(task["id"], task["thread_id"])
                self.assertFalse(self.service.get(task["id"]).get("deleted", False))
                self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_delete_revalidates_loaded_state_and_local_revision(self):
        task = await self.missing()
        original = self.service.backend.call
        for variant in ("loaded", "changed"):
            calls = 0
            async def changed(method, params, **kw):
                nonlocal calls
                if method == "thread/loaded/list":
                    calls += 1
                    if calls == 2:
                        if variant == "loaded":
                            return {"data": [task["thread_id"]], "nextCursor": None}
                        self.service.lifecycle.metadata(task["id"], {"name": "Changed during proof"})
                    return {"data": [], "nextCursor": None}
                return await original(method, params, **kw)
            with self.subTest(variant=variant), patch.object(self.service.backend, "call", changed):
                with self.assertRaises(BridgeError):
                    await self.service.delete(task["id"], task["thread_id"])
                self.assertFalse(self.service.get(task["id"]).get("deleted", False))

    async def test_wrong_confirmation_busy_family_and_running_operation_refuse_before_io(self):
        task = await self.missing()
        with patch.object(self.service.backend, "call", AsyncMock(side_effect=AssertionError("No native I/O"))):
            with self.assertRaises(BridgeError):
                await self.service.delete(task["id"], "wrong")
            self.service.lifecycle.begin(task["id"], "busy-turn")
            with self.assertRaises(BridgeError):
                await self.service.delete(task["id"], task["thread_id"])
            self.service.lifecycle.complete(task["id"], "busy-turn", "interrupted")
            with self.store.transaction():
                self.store.db.execute("INSERT INTO operations VALUES ('inflight','fp','running',NULL,?)", (task["id"],))
            with self.assertRaises(BridgeError):
                await self.service.delete(task["id"], task["thread_id"])

    async def test_absence_proof_is_bound_to_the_record_before_the_native_read(self):
        task = await self.missing()
        original = self.service.backend.call
        async def changed(method, params, **kw):
            if method == "thread/read":
                self.store.update("tasks", task["id"], {"thread_id": "different-native-identity"})
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", changed), self.assertRaisesRegex(BridgeError, "changed during deletion"):
            await self.service.delete(task["id"], task["thread_id"])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_absent_parent_can_delete_a_known_idle_child_and_refuses_an_active_one(self):
        root = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(root["id"]))), 5)
        request = self.service.requests(root["id"])[0]
        child = self.service.task(request["task_id"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(root["id"], ("completed",))
        await self.service.backend.call("thread/delete", {"threadId": root["thread_id"]})
        self.store.update("tasks", root["id"], {"thread_id": "missing-parent"})
        self.store.claim(root["id"], "uncertain", "uncertain", {"reason": "stored_state_unavailable"})
        original, deleted = self.service.backend.call, []
        async def observed(method, params, **kw):
            if method == "thread/delete":
                deleted.append(params["threadId"])
            return await original(method, params, **kw)
        self.store.claim(child["id"], "goal", "active")
        self.service.lifecycle.begin(child["id"], "busy-child")
        with self.assertRaises(BridgeError):
            await self.service.delete(root["id"], "missing-parent")
        self.service.lifecycle.complete(child["id"], "busy-child", "interrupted")
        with patch.object(self.service.backend, "call", observed):
            result = await self.service.delete(root["id"], "missing-parent")
        self.assertEqual(result["native_conversation"], "unavailable")
        self.assertEqual(result["native_conversations_unavailable"], 1)
        self.assertEqual(deleted, [child["thread_id"]])
        self.assertTrue(self.service.get(child["id"])["deleted"])
        self.assertEqual(self.service.get(child["id"])["native_conversation"], "deleted")
        self.assertFalse(self.service.get(root["id"])["workspace_reserved"])

    async def test_unavailable_delete_transaction_rolls_back_a_local_failure(self):
        task = await self.missing()
        original = self.store.event
        def fail_event(task_id, kind, data, **kw):
            if kind == "bridge/deleted":
                raise OSError("Injected database failure")
            return original(task_id, kind, data, **kw)
        before = self.store.claims(task_id=task["id"])
        with patch.object(self.store, "event", fail_event), self.assertRaises(OSError):
            await self.service.delete(task["id"], task["thread_id"])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))
        self.assertEqual(self.store.claims(task_id=task["id"]), before)

    async def test_confirmed_delete_of_absent_contact_lost_task_keeps_the_process_warning(self):
        task = await self.contact_lost()
        self.assertEqual(task["status"], "needs_resume")
        result = await dispatch(self.service, "codex_delete", {"task_id": task["id"],
            "confirm_thread_id": task["thread_id"], "operation_id": "delete-lost-running"})
        self.assertEqual(result["native_conversation"], "unavailable")
        self.assertTrue(result["processes_unverified"])
        self.assertTrue(result["process_state"]["previous_backend_unverified"])
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertEqual(result["process_states_total"], 1)
        self.assertEqual(result["process_states"][0]["task_id"], task["id"])
        event = next(e for e in self.store.events(0, 100, task_id=task["id"])["events"] if e["kind"] == "bridge/deleted")
        self.assertEqual(event["data"]["process_state"], result["process_state"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-lost-running-deletion")

    async def test_readable_contact_lost_task_still_requires_resume(self):
        task = await self.contact_lost(missing=False)
        with self.assertRaisesRegex(BridgeError, "system process list"):
            await self.service.delete(task["id"], task["thread_id"])
        self.assertFalse(self.service.get(task["id"]).get("deleted"))
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_current_running_work_is_refused_even_with_a_previous_process_marker(self):
        task = await self.contact_lost()
        self.service.lifecycle.begin(task["id"], "current-turn")
        self.service.lifecycle.metadata(task["id"], {"verified_generation": self.service.backend.generation})
        with patch.object(self.service.backend, "call", AsyncMock(side_effect=AssertionError("No native request"))):
            with self.assertRaises(BridgeError):
                await self.service.delete(task["id"], task["thread_id"])
        self.assertFalse(self.service.get(task["id"]).get("deleted"))

    async def test_contact_lost_delete_refuses_an_unknown_loaded_conversation(self):
        task = await self.contact_lost()
        original = self.service.backend.call
        async def unknown(method, params, **kw):
            if method == "thread/loaded/list":
                return {"data": ["unknown-loaded-conversation"], "nextCursor": None}
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", unknown), self.assertRaisesRegex(BridgeError, "unadopted"):
            await self.service.delete(task["id"], task["thread_id"])
        self.assertFalse(self.service.get(task["id"]).get("deleted"))
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])

    async def test_family_deletion_bounds_its_process_warning_summaries(self):
        root = await self.missing()
        source = self.service.task(root["id"])
        for index in range(110):
            self.service.lifecycle.create({**source, "id": f"missing-child-{index}", "thread_id": f"missing-native-{index}",
                "parent_task_id": root["id"], "root_task_id": root["id"], "process_state": {
                    "previous_backend_unverified": True, "acknowledged": False,
                    "commands_preview": [("界\\\"" * 100)[:256]] * 5, "commands_truncated": True}})
        result = await self.service.delete(root["id"], root["thread_id"])
        self.assertTrue(result["processes_unverified"])
        self.assertEqual(result["process_states_total"], 110)
        self.assertTrue(result["process_states_truncated"])
        self.assertLess(len(result["process_states"]), 100)
        self.assertLess(result_size(result), RESULT_BUDGET)
        self.assertFalse(self.service.get(root["id"])["workspace_reserved"])


class ReconciliationRetryTest(MissingConversationHarness):
    automatic_reconciliation = True

    async def test_family_change_during_a_failed_read_is_rechecked_without_waiting_for_a_timer(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.service.lifecycle.create({**self.service.task(task["id"]), "id": "projection-child", "thread_id": None,
            "turn_id": None, "status": "idle", "parent_task_id": task["id"], "root_task_id": task["id"]})
        gate = self.gate_call("obsolete-family-read", "thread/goal/get", before=True,
            error=RpcError({"code": -32600, "message": "An obsolete fixture read failed"}))
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
            self.service.schedule_reconciliation(task["id"])
            await gate.wait()
            revision = self.service.task(task["id"])["revision"]
            self.service.lifecycle.metadata("projection-child", {"name": "New child evidence"})
            self.assertEqual(self.service.task(task["id"])["revision"], revision)
            gate.release.set()
            await self.reconciliation_idle()
            self.assertFalse(clock.pending)
            self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_repeated_failures_do_not_write_emit_or_advance_outcomes(self):
        task = await self.missing()
        await self.reconciliation_idle()
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            for reason in ("stored_state_unavailable", "native_state_unavailable", "unknown_native_intent", "reconciliation_failed",
                           "conversation_not_loaded", "native_state_changed"):
                self.service.reconciliation_failed(task["id"], reason, error="same failure")
                before = self.store.db.total_changes
                initial = await self.service.events(task_id=task["id"], view="outcome")
                self.service.reconciliation_failed(task["id"], reason, error="same failure")
                self.assertEqual(self.store.db.total_changes, before)
                page = await self.service.events(task_id=task["id"], view="outcome", cursors=initial["cursors"])
                self.assertEqual(page["outcomes"], [])

    async def test_failure_retry_backoff_and_reset_are_driven_without_sleep(self):
        task = await self.missing()
        await self.reconciliation_idle()
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            await self.service.wait_for_reconciliation([task["id"]])
            await self.reconciliation_idle()
            before = self.store.db.total_changes
            for _ in range(7):
                clock.advance()
                await self.reconciliation_idle()
            self.assertEqual([entry[1] for entry in clock.calls], [5, 10, 20, 40, 80, 160, 300, 300])
            self.assertEqual(self.store.db.total_changes, before)
            self.service.lifecycle.metadata(task["id"], {"name": "New metadata"})
            await self.reconciliation_idle()
            self.assertEqual(clock.pending[-1][1], 5)
            clock.advance()
            await self.reconciliation_idle()
            self.assertEqual(clock.pending[-1][1], 10)
            self.store.claim(task["id"], "queue", "uncertain", {"reason": "new_queue_fact"})
            await self.reconciliation_idle()
            self.assertEqual(clock.pending[-1][1], 5)
            await self.service.wait_for_reconciliation([task["id"]])
            await self.reconciliation_idle()
            self.assertEqual(clock.pending[-1][1], 5)

    async def test_successor_gap_keeps_ownership_when_final_thread_status_changed(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.store.claim(task["id"], "queue", "active")
        original, consumed, reads = self.service.backend.call, False, []
        async def gap(method, params, **kw):
            nonlocal consumed
            result = await original(method, params, **kw)
            if method == "thread/queue/list":
                consumed = True
            if method == "thread/read":
                reads.append(consumed)
                if consumed:
                    result = {"thread": {**result["thread"], "status": {"type": "active", "activeFlags": []}}}
            return result
        with patch.object(self.service.backend, "call", gap):
            await self.service.wait_for_reconciliation([task["id"]])
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            with self.assertRaisesRegex(BridgeError, "active in this project"):
                await self.start_task("hold", key="successor-gap")
        self.assertIn(False, reads)
        self.assertIn(True, reads)

    async def test_loaded_transition_repeats_with_a_real_terminal_read(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        original, reads, terminals = self.service.backend.call, 0, []
        async def newly_loaded(method, params, **kw):
            nonlocal reads
            result = await original(method, params, **kw)
            if method == "thread/read":
                reads += 1
                result = {"thread": {**result["thread"], "status": {"type": "notLoaded" if reads == 1 else "idle"}}}
            if method == "thread/backgroundTerminals/list":
                terminals.append(True)
                return {"data": [{"processId": "live", "command": "fixture", "cwd": task["project"], "itemId": "live"}], "nextCursor": None}
            return result
        with patch.object(self.service.backend, "call", newly_loaded):
            observed = await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertGreaterEqual(reads, 4)
        self.assertTrue(terminals)
        self.assertEqual(observed["background_terminals"], 1)
        self.assertTrue(any(c["kind"] == "background" and c["state"] == "active" for c in self.store.claims(task_id=task["id"])))

    async def test_unchanged_failure_does_not_use_dirty_wakeups_to_bypass_backoff(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        original, reads = self.service.backend.call, 0
        clock = ReconciliationClock(self.service)
        async def unstable(method, params, **kw):
            nonlocal reads
            result = await original(method, params, **kw)
            if method == "thread/read":
                reads += 1
                result = {"thread": {**result["thread"], "status": {"type": "idle" if reads % 2 else "active", "activeFlags": []}}}
            return result
        with patch.object(self.service.backend, "call", unstable), patch.object(self.service, "reconciliation_timer", clock.later):
            self.service.schedule_reconciliation(task["id"])
            await self.reconciliation_idle()
            self.assertEqual(reads, 2)
            self.assertEqual(clock.pending[-1][1], 5)
            before = self.store.db.total_changes
            clock.advance()
            await self.reconciliation_idle()
            self.assertEqual(reads, 4)
            self.assertEqual(clock.pending[-1][1], 10)
            self.assertEqual(self.store.db.total_changes, before)

    async def test_parent_checks_do_not_bypass_a_failing_childs_backoff(self):
        root = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(root["id"]))), 5)
        request = self.service.requests(root["id"])[0]
        child = self.service.task(request["task_id"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(root["id"], ("completed",))
        await self.reconciliation_idle()
        self.store.claim(child["id"], "background", "uncertain", {"reason": "turn_completion"})
        original, reads = self.service.backend.call, 0
        clock = ReconciliationClock(self.service)
        async def unavailable(method, params, **kw):
            nonlocal reads
            if method == "thread/read" and params.get("threadId") == child["thread_id"]:
                reads += 1
                raise RpcError({"code": -32600, "message": "Unavailable stored fixture"})
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", unavailable), patch.object(self.service, "reconciliation_timer", clock.later):
            self.service.schedule_reconciliation(child["id"])
            await self.reconciliation_idle()
            before = reads
            for _ in range(3):
                await self.service.reconcile_claims(root["id"])
            self.assertEqual(reads, before)
            self.assertTrue(self.service.get(root["id"])["workspace_reserved"])
            await self.service.wait_for_reconciliation([child["id"]])
            self.assertEqual(reads, before + 1)
