"""Native evidence releases routine workspace ownership without manual recovery."""

import asyncio
import json
import os
import sqlite3
from unittest.mock import AsyncMock, patch

from codex_local_mcp.backend import RpcError
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.service import WorkspaceBusy, ADMISSION_RECONCILE_SECONDS
from codex_local_mcp.store import SCHEMA_V1, Store
from codex_local_mcp.tools import dispatch
from tests.helpers import FaultGate, ReconciliationClock, read_fake_history
from tests.test_service import ServiceHarness


class ClaimReconciliationTest(ServiceHarness):
    automatic_reconciliation = True

    async def wait_for(self, predicate):
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(predicate), 5)

    async def settled(self):
        await self.reconciliation_idle()

    async def two_idle_families(self):
        first = await self.start_task()
        await self.until(first["id"], ("completed",))
        await self.settled()
        second = await self.start_task(key="second-idle-family")
        await self.until(second["id"], ("completed",))
        await self.settled()
        return first, second

    async def test_direct_resume_waits_for_other_family_startup_check_outside_admission(self):
        first, second = await self.two_idle_families()
        self.store.claim(first["id"], "background", "uncertain", {"reason": "turn_completion"})
        await self.service.close()
        self.service = self.new_service()
        gate = self.gate_call("stored-thread-check", "thread/read", before=True)
        resuming = asyncio.create_task(self.service.resume(second["id"]))
        await gate.wait()
        self.assertFalse(resuming.done())
        self.assertFalse(self.service.admission_lock.locked())
        gate.release.set()
        result = await asyncio.wait_for(resuming, 5)
        self.assertEqual(result["id"], second["id"])
        self.assertFalse(self.service.get(first["id"])["workspace_reserved"])

    async def test_direct_resume_still_refuses_a_confirmed_other_family_goal(self):
        first, second = await self.two_idle_families()
        await self.service.goal(first["id"], "set", objective="Keep the workspace", status="active", user_requested=True)
        await self.service.close()
        self.service = self.new_service()
        with self.assertRaises(WorkspaceBusy) as busy:
            await self.service.resume(second["id"])
        self.assertIn("codex_goal", str(busy.exception))
        self.assertTrue(any(c["kind"] == "goal" and c["state"] == "active" for c in self.store.claims(task_id=first["id"])))

    async def test_direct_resume_has_one_bounded_wait_and_does_not_cancel_native_reconciliation(self):
        first, second = await self.two_idle_families()
        self.store.claim(first["id"], "background", "uncertain", {"reason": "turn_completion"})
        await self.service.close()
        self.service = self.new_service()
        gate = self.gate_call("blocked-startup-read", "thread/read", before=True)
        native_wait_for, deadlines = asyncio.wait_for, []
        async def deadline(coroutine, timeout):
            if getattr(coroutine, "cr_code", None) and coroutine.cr_code.co_qualname.endswith("wait_for_reconciliation.<locals>.wait"):
                deadlines.append(timeout)
                pending = asyncio.create_task(coroutine)
                await native_wait_for(gate.entered.wait(), 5)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise TimeoutError("Injected admission deadline")
            return await native_wait_for(coroutine, timeout)
        with patch("codex_local_mcp.service.asyncio.wait_for", deadline):
            with self.assertRaises(WorkspaceBusy):
                await self.service.resume(second["id"])
        self.assertEqual(deadlines, [ADMISSION_RECONCILE_SECONDS])
        self.assertTrue(self.service.get(first["id"])["workspace_reserved"])
        self.assertTrue(any(not task.done() for task in self.service.reconciliation_tasks))
        gate.release.set()
        await self.settled()

    async def test_startup_releases_upgrade_claims_for_finished_tasks_without_resume(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        await self.service.close()
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "schema_upgrade"})
        self.service = self.new_service()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        original, calls = self.service.backend.call, []
        async def read_only(method, params, **kw):
            calls.append(method)
            self.assertNotIn(method, ("thread/resume", "thread/start", "turn/start"))
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", read_only), patch.object(self.service, "resume", AsyncMock(side_effect=AssertionError("Startup must not require codex_resume"))):
            await asyncio.wait_for(self.service.reconcile_startup(), 10)
            await self.settled()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(self.service.loaded, set())
        native = await original("thread/read", {"threadId": task["thread_id"], "includeTurns": False})
        self.assertEqual(native["thread"]["status"], {"type": "notLoaded"})
        self.assertEqual(native["thread"]["turns"], [])
        self.assertNotIn("thread/backgroundTerminals/list", calls)
        with self.assertRaises(RpcError) as unloaded:
            await original("thread/backgroundTerminals/list", {"threadId": task["thread_id"]})
        self.assertEqual(unloaded.exception.error, {"code": -32600, "message": "thread not found: " + task["thread_id"]})

    async def test_stored_nonterminal_turn_remains_owned_without_loading_or_resuming(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        await self.service.close()
        history = read_fake_history(self.state)
        history[task["thread_id"]]["turns"][-1]["status"] = "inProgress"
        history[task["thread_id"]]["fixture_stale_in_progress"] = True
        (self.state / "fake-history.json").write_text(json.dumps(history), encoding="utf-8")
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "schema_upgrade"})
        self.service = self.new_service()
        await self.service.reconcile_startup()
        await self.settled()
        claims = self.service.get(task["id"])["workspace_claims"]
        self.assertTrue(claims)
        self.assertTrue(any(c["data"].get("reason") == "conversation_not_loaded" for c in claims))
        self.assertEqual(self.service.loaded, set())
        with self.assertRaisesRegex(BridgeError, "active in this project"):
            await self.start_task("hold", key="blocked-by-stored-running-turn")

    async def test_unknown_stored_conversation_keeps_a_plain_reason_without_raw_rpc_json(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        await self.service.close()
        (self.state / "fake-history.json").write_text("{}", encoding="utf-8")
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        self.service = self.new_service()
        await self.service.reconcile_startup()
        await self.settled()
        claim = next(c for c in self.store.claims(task_id=task["id"]) if c["kind"] == "uncertain")
        self.assertEqual(claim["data"]["reason"], "stored_state_unavailable")
        self.assertEqual(claim["data"]["error"], "Stored conversation could not be read; workspace ownership was retained")
        self.assertNotIn("{", claim["data"]["error"])
        self.assertEqual(self.service.loaded, set())

    async def test_stored_read_for_another_thread_cannot_release_ownership(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "schema_upgrade"})
        original = self.service.backend.call
        async def wrong_thread(method, params, **kw):
            result = await original(method, params, **kw)
            if method == "thread/read":
                result = {"thread": {**result["thread"], "id": "different-conversation"}}
            return result
        with patch.object(self.service.backend, "call", wrong_thread):
            await self.service.reconcile_claims(task["id"])
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_unadopted_stored_descendants_are_checked_without_loading_and_keep_active_intent(self):
        parent = await self.start_task("native_children")
        waiting = await self.until(parent["id"], ("running", "waiting"))
        await self.wait_for(lambda: bool(self.service.requests(parent["id"])))
        request = self.service.requests(parent["id"])[0]
        child = self.service.task(request["task_id"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))
        await self.settled()
        await self.service.close()
        with self.store.transaction():
            self.store.db.execute("DELETE FROM workspace_claims WHERE task_id=?", (child["id"],))
            self.store.db.execute("DELETE FROM tasks WHERE id=?", (child["id"],))
        for active in (False, True):
            if active:
                await self.service.close()
                history = read_fake_history(self.state)
                history[child["thread_id"]]["goal"] = {"status": "active", "objective": "Hold", "createdAt": 0,
                    "updatedAt": 0, "tokensUsed": 0, "timeUsedSeconds": 0}
                (self.state / "fake-history.json").write_text(json.dumps(history), encoding="utf-8")
            self.store.claim(parent["id"], "uncertain", "uncertain", {"reason": "schema_upgrade"})
            self.service = self.new_service()
            original = self.service.backend.call
            async def read_only(method, params, **kw):
                self.assertNotIn(method, ("thread/resume", "thread/start", "turn/start"))
                return await original(method, params, **kw)
            with patch.object(self.service.backend, "call", read_only):
                await self.service.reconcile_startup()
                await self.settled()
            self.assertEqual(self.service.loaded, set())
            self.assertEqual(self.service.get(parent["id"])["workspace_reserved"], active)

    async def command(self, background=False):
        task = await self.start_task("hold_command_background" if background else "hold_command")
        await self.wait_for(lambda: any(e["kind"] == "item/started" and
            e["data"].get("item", {}).get("id") == "running-command"
            for e in self.store.events(0, 200, task_id=task["id"])["events"]))
        await self.settled()
        return task

    def post_turn_gate(self, task, name="post_turn", error=None):
        gate = self.gates[name] = FaultGate(name, error)
        original = self.service.backend.call
        async def gated(method, params, **kwargs):
            if (method == "thread/goal/get" and params.get("threadId") == task["thread_id"]
                    and self.service.task(task["id"])["turn_status"] in ("completed", "failed", "interrupted")
                    and not gate.used):
                await gate.hit()
            return await original(method, params, **kwargs)
        self.service.backend.call = gated
        return gate

    async def test_interrupted_command_then_new_root_in_the_same_project(self):
        task = await self.command()
        await self.service.send(task["id"], "Stop after interruption", "steer", task["turn_id"])
        await self.service.interrupt(task["id"], task["turn_id"])
        ended = await self.until(task["id"], ("interrupted",))
        self.assertEqual(ended["commands"][0]["exitCode"], 130)
        following = await self.start_task("hold", key="next-root")
        self.assertNotEqual(following["id"], task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_completed_and_failed_turns_release_without_resume(self):
        for prompt in ("echo", "fail"):
            with self.subTest(prompt=prompt):
                task = await self.start_task(prompt, path=prompt, key=prompt)
                await self.until(task["id"], ("completed", "failed"))
                following = await self.start_task("hold", path=prompt, key=prompt + "-next")
                self.assertNotEqual(following["id"], task["id"])

    async def test_admission_waits_for_post_turn_check_without_holding_its_lock(self):
        task = await self.command()
        gate = self.post_turn_gate(task)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await gate.wait()
        joined = asyncio.Event()
        original = self.service.wait_for_reconciliation
        async def waiting(*args, **kwargs):
            self.assertFalse(self.service.admission_lock.locked())
            joined.set()
            return await original(*args, **kwargs)
        with patch.object(self.service, "wait_for_reconciliation", waiting):
            starting = asyncio.create_task(self.start_task("hold", key="waiting-root"))
            await asyncio.wait_for(joined.wait(), 5)
            self.assertFalse(starting.done())
            gate.release.set()
            following = await asyncio.wait_for(starting, 5)
        self.assertNotEqual(following["id"], task["id"])

    async def test_live_background_is_refused_with_owner_and_recovery_steps(self):
        task = await self.command(background=True)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        with self.assertRaises(BridgeError) as blocked:
            await self.start_task("hold", key="blocked-background")
        message = str(blocked.exception)
        self.assertTrue(message.startswith("Another task is active in this project. Use a separate project/worktree or finish it first"))
        for value in (task["id"], "background", "workspace_claims", "codex_get", "codex_resume"):
            self.assertIn(value, message)
        self.assertIn(f"For task {task['id']}, use codex_terminals to list, then terminate each process_id or clean.", message)
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_refusal_names_the_recovery_tool_for_each_claim_kind(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        leading = "Another task is active in this project. Use a separate project/worktree or finish it first"
        instructions = {
            "background": "use codex_terminals to list, then terminate each process_id or clean.",
            "goal": "use codex_goal to pause or clear only on the user's request.",
            "queue": "use codex_queue to list/delete queued entries.",
        }
        for kind, instruction in instructions.items():
            with self.subTest(kind=kind):
                claim = {"task_id": task["id"], "family_id": task["id"], "project": task["project"],
                         "kind": kind, "state": "active", "data": {}}
                error = self.service.workspace_busy(leading, [claim], [])
                self.assertEqual(str(error), leading + f". Blocking workspace claims: task {task['id']} [{kind}=active]. "
                    + f"Inspect codex_get for workspace_claims. For task {task['id']}, {instruction} "
                    + "Use codex_resume with a new request_id to re-check native state.")
                self.assertEqual(error.blocking_claims, (claim,))
                self.assertFalse(error.pending_admissions)

    async def test_recovery_guidance_includes_kinds_beyond_the_summary_limit(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        claims = []
        for index in range(9):
            owner = f"owner-{index}"
            self.service.lifecycle.create({**self.service.task(task["id"]), "id": owner,
                "thread_id": None, "turn_id": None, "status": "completed",
                "parent_task_id": task["id"], "root_task_id": task["id"]})
            claims.append({"task_id": owner, "family_id": task["id"], "project": task["project"],
                "kind": "turn" if index < 8 else "background", "state": "active", "data": {}})
        error = self.service.workspace_busy("Another task is active in this project", claims, [])
        self.assertIn("1 more tasks", str(error))
        self.assertIn("For task owner-8, use codex_terminals", str(error))

    async def stop_background(self, action, *, stale_read=False):
        task = await self.command(background=True)
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            await self.service.interrupt(task["id"], task["turn_id"])
            await self.wait_for(lambda: self.service.task(task["id"]).get("native_status") == {"type": "idle"})
            await self.settled()
            with self.assertRaisesRegex(BridgeError, "codex_terminals"):
                await self.start_task("hold", key="background-blocked")
            terminals = await dispatch(self.service, "codex_terminals", {"task_id": task["id"], "action": "list"})
            self.assertEqual(terminals["data"][0]["itemId"], "running-command")
            await self.settled()
            ready = asyncio.Event()
            async def released():
                async with self.service.changed:
                    ready.set()
                    await self.service.changed.wait_for(lambda: not self.service.get(task["id"])["workspace_reserved"])
            waiter = asyncio.create_task(released())
            await asyncio.wait_for(ready.wait(), 5)
            original = self.service.backend.call
            stale = False
            async def reading(method, params, **kwargs):
                nonlocal stale
                result = await original(method, params, **kwargs)
                if stale_read and method == "thread/backgroundTerminals/list" and not stale:
                    stale = True
                    return {"data": terminals["data"], "nextCursor": None}
                return result
            try:
                with patch.object(self.service.backend, "call", reading):
                    # Direct callers have no operation callback to wake this
                    # waiter or start a check; the terminal control must do it.
                    await self.service.terminals(task["id"], action, process_id="background" if action == "terminate" else None)
                    await asyncio.wait_for(waiter, 5)
                    await self.settled()
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
            self.assertFalse(clock.pending)
            following = await self.start_task("hold", key="after-background-stop")
            self.assertNotEqual(following["id"], task["id"])

    async def test_terminal_controls_wake_waiters_after_releasing_the_claim(self):
        await self.stop_background("terminate")

    async def test_terminal_cleanup_rechecks_immediately_after_a_stale_list(self):
        await self.stop_background("clean", stale_read=True)

    async def test_background_disappearance_is_observed_automatically(self):
        task = await self.command(background=True)
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            await self.service.interrupt(task["id"], task["turn_id"])
            await self.until(task["id"], ("interrupted",))
            await asyncio.wait_for(clock.ready.wait(), 5)
            # Simulate native exit without a bridge tool or lifecycle event.
            await self.service.backend.call("thread/backgroundTerminals/terminate", {
                "threadId": task["thread_id"], "processId": "background"})
            self.assertEqual(clock.pending[0][1], 5)
            clock.advance()
            await self.wait_for(lambda: not self.service.get(task["id"])["workspace_reserved"])

    async def test_reserved_idle_poll_cost_and_stop_after_direct_reconciliation(self):
        task = await self.command(background=True)
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            await self.service.interrupt(task["id"], task["turn_id"])
            await self.wait_for(lambda: self.service.task(task["id"]).get("native_status") == {"type": "idle"})
            await self.settled()
            self.assertEqual([(task_id, seconds) for task_id, seconds, _ in clock.pending], [(task["id"], 5)])
            calls = []
            original = self.service.backend.call
            async def counting(method, params, **kwargs):
                calls.append(method)
                return await original(method, params, **kwargs)
            with patch.object(self.service.backend, "call", counting):
                clock.advance()
                await self.settled()
                self.assertEqual(calls, ["thread/read", "thread/goal/get", "thread/queue/list", "thread/turns/list",
                                         "thread/backgroundTerminals/list", "thread/list", "thread/read"])
                self.assertEqual(len(clock.pending), 1)
                await self.service.terminals(task["id"], "terminate", process_id="background")
                self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
                self.assertFalse(clock.pending)
                self.assertFalse(self.service.reconciliation_timers)
                calls.clear()
                for _ in range(3):
                    clock.advance()
                # A timer already dequeued before cancellation must also make
                # no native calls after the final claim has been cleared.
                self.service.periodic_reconciliation(task["id"])
                await self.settled()
                self.assertEqual(calls, [])

    async def test_reserved_idle_child_poll_cost(self):
        parent = await self.start_task("native_children")
        await self.wait_for(lambda: bool(self.service.requests(parent["id"])))
        request = self.service.requests(parent["id"])[0]
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))
        await self.settled()
        clock = ReconciliationClock(self.service)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            await self.service.goal(request["task_id"], "set", objective="Continue", status="active", user_requested=True)
            await self.settled()
            self.assertEqual([(task_id, seconds) for task_id, seconds, _ in clock.pending], [(request["task_id"], 5)])
            calls = []
            original = self.service.backend.call
            async def counting(method, params, **kwargs):
                calls.append(method)
                return await original(method, params, **kwargs)
            with patch.object(self.service.backend, "call", counting):
                clock.advance()
                await self.settled()
            self.assertEqual(calls, ["thread/read", "thread/goal/get", "thread/queue/list", "thread/turns/list",
                                     "thread/backgroundTerminals/list", "thread/read"])

    async def test_unarchived_notification_wakes_a_pending_periodic_check(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.archive(task["id"])
        await self.settled()
        clock = ReconciliationClock(self.service)
        original = self.service.backend.call
        async def unavailable(method, params, **kwargs):
            if method == "thread/goal/get":
                raise BridgeError("Fixture read temporarily unavailable")
            return await original(method, params, **kwargs)
        with patch.object(self.service, "reconciliation_timer", clock.later):
            self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "unarchive"})
            with patch.object(self.service.backend, "call", unavailable):
                self.service.schedule_reconciliation(task["id"])
                await self.settled()
            self.assertEqual(len(clock.pending), 1)
            await self.service.backend.call("thread/unarchive", {"threadId": task["thread_id"]})
            await self.service.on_message({"method": "thread/unarchived", "params": {"threadId": task["thread_id"]}})
            self.assertFalse(clock.pending)
            await self.wait_for(lambda: not self.service.get(task["id"])["workspace_reserved"])

    async def test_unavailable_native_check_retains_ownership_and_explains_it(self):
        task = await self.command()
        original = self.service.backend.call
        async def unavailable(method, params, **kwargs):
            if method == "thread/goal/get":
                raise BridgeError("Fixture backend unavailable")
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", unavailable):
            await self.service.interrupt(task["id"], task["turn_id"])
            await self.until(task["id"], ("interrupted",))
            with self.assertRaises(BridgeError) as blocked:
                await self.start_task("hold", key="unavailable-root")
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            message = str(blocked.exception)
            for value in (task["id"], "uncertain", "Fixture backend unavailable", "codex_resume"):
                self.assertIn(value, message)

    async def test_disconnected_backend_keeps_claims_until_native_reads_can_run(self):
        task = await self.command()
        gate = self.post_turn_gate(task)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await gate.wait()
        await self.service.backend.close()
        with patch.object(self.service.backend, "start", side_effect=BridgeError("Fixture backend cannot restart")):
            gate.release.set()
            await self.settled()
            with self.assertRaisesRegex(BridgeError, "Fixture backend cannot restart"):
                await self.start_task("hold", key="disconnected-root")
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="reconnected-root")
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_dirty_check_during_worker_exit_is_not_stranded(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        gate = self.gates["worker_exit"] = FaultGate("worker_exit")
        original = self.service.reconcile_claims
        async def reconcile(*args, **kwargs):
            result = await original(*args, **kwargs)
            if asyncio.current_task() is self.service.reconciliation_by_task.get(task["id"]) and not gate.used:
                await gate.hit()
            return result
        with patch.object(self.service, "reconcile_claims", reconcile):
            self.service.schedule_reconciliation(task["id"])
            await gate.wait()
            self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
            self.service.schedule_reconciliation(task["id"])
            gate.release.set()
            await self.wait_for(lambda: not self.service.get(task["id"])["workspace_reserved"])

    async def test_duplicate_completion_cannot_reserve_archived_or_deleted_tasks(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.store.claims(task_id=task["id"]))
        for action in ("archive", "delete"):
            self.service.lifecycle.archive(task["id"], action)
            await self.service.on_message({"method": "turn/completed", "params": {
                "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}}})
            self.assertFalse(self.store.claims(task_id=task["id"]))

    async def test_rejected_start_is_reconciled_automatically(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        gate = self.gate_call("rejected_turn", "turn/start", before=True,
            error=RpcError({"code": -32602, "message": "Rejected fixture turn"}))
        sending = asyncio.create_task(dispatch(self.service, "codex_send", {
            "task_id": task["id"], "prompt": "hold", "request_id": "rejected-turn"}))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(RpcError):
            await sending
        following = await self.start_task("hold", key="after-rejection")
        self.assertNotEqual(following["id"], task["id"])

    async def test_applied_turn_start_error_retains_the_native_running_turn(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        started = self.gate_callback("early_started", "turn/started")
        ack = self.gate_call("applied_error", "turn/start",
            error=RpcError({"code": -32603, "message": "Applied start; acknowledgement failed"}))
        sending = asyncio.create_task(self.service.send(task["id"], "hold"))
        await started.wait()
        await ack.wait()
        ack.release.set()
        with self.assertRaises(RpcError):
            await sending
        with self.assertRaises(BridgeError) as blocked:
            await self.start_task("hold", key="applied-error-root")
        self.assertIn(task["id"], str(blocked.exception))
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        started.release.set()

    async def test_goal_pause_does_not_release_unverified_background_work(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.goal(task["id"], "set", objective="Continue", status="active", user_requested=True)
        await self.settled()
        original = self.service.backend.call
        async def unavailable(method, params, **kwargs):
            if method == "thread/backgroundTerminals/list":
                raise BridgeError("Cannot inspect native background terminals")
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", unavailable):
            await self.service.goal(task["id"], "set", status="paused", user_requested=True)
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            with self.assertRaisesRegex(BridgeError, "Cannot inspect native background terminals"):
                await self.start_task("hold", key="unverified-background")
        await self.start_task("hold", key="verified-background")

    async def test_finished_threads_are_reconciled_after_backend_restart(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        await self.service.close()
        self.service = self.new_service()
        following = await self.start_task("hold", key="after-restart")
        self.assertNotEqual(following["id"], task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_finished_v011_task_is_reconciled_after_upgrade_without_resume(self):
        task = await self.start_task()
        task = await self.until(task["id"], ("completed",))
        await self.service.close()
        self.store.close()
        path = self.state / "state.sqlite3"
        path.unlink()
        os.close(private_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        db = sqlite3.connect(path)
        try:
            for statement in SCHEMA_V1:
                db.execute(statement)
            legacy = {k: v for k, v in task.items() if k not in (
                "project_id", "revision", "workspace_claims", "workspace_reserved", "pending_requests")}
            db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(legacy)))
            db.execute("INSERT INTO events VALUES (41,?,'saved',1,'{}')", (task["id"],))
            db.commit()
        finally:
            db.close()
        self.store = Store(self.state)
        self.service = self.new_service()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        original = self.service.backend.call
        async def read_only(method, params, **kw):
            self.assertNotIn(method, ("thread/resume", "thread/start", "turn/start"))
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", read_only):
            await asyncio.wait_for(self.service.reconcile_startup(), 10)
            await self.settled()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(self.service.loaded, set())
        following = await self.start_task("hold", key="after-upgrade")
        self.assertNotEqual(following["id"], task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertTrue(self.store.backup_path.is_file())
        self.assertEqual(self.store.events(0, 1)["events"][0]["seq"], 41)

    async def test_admission_timeout_keeps_claim_and_does_not_cancel_the_check(self):
        task = await self.command()
        gate = self.post_turn_gate(task)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await gate.wait()
        with patch("codex_local_mcp.service.ADMISSION_RECONCILE_SECONDS", 0):
            with self.assertRaisesRegex(BridgeError, "Native check did not finish within 0 seconds"):
                await self.start_task("hold", key="bounded-wait")
        self.assertFalse(self.service.reconciliation_by_task[task["id"]].done())
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(len(read_fake_history(self.state)), 1)
        gate.release.set()
        await self.wait_for(lambda: not self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-bounded-wait")

    async def test_lost_empty_thread_start_ack_does_not_reserve_the_project(self):
        gate = self.gate_call("lost_thread_ack", "thread/start", error=TimeoutError("Lost thread ACK"))
        starting = asyncio.create_task(self.start_task())
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await starting
        await self.settled()
        self.assertFalse(self.store.claims())
        await self.start_task("hold", key="after-thread-failure")

    async def test_lost_unarchive_ack_is_reconciled_without_resume(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.archive(task["id"])
        await self.settled()
        clock = ReconciliationClock(self.service)
        passes, count = [], 0
        for i in range(3):
            gate = self.gates[f"restore_pass_{i}"] = FaultGate(f"restore_pass_{i}")
            passes.append(gate)
        original = self.service.backend.call
        async def reading(method, params, **kwargs):
            nonlocal count
            if method == "thread/goal/get" and asyncio.current_task() is self.service.reconciliation_by_task.get(task["id"]):
                index, count = count, count + 1
                if index < len(passes):
                    await passes[index].hit()
            return await original(method, params, **kwargs)
        # Exhaust the worker's burst while a lost restore ACK establishes new
        # intent. Its cleanup must run promptly, without a five-second poll.
        with patch.object(self.service, "reconciliation_timer", clock.later), patch.object(self.service.backend, "call", reading):
            worker = self.service.schedule_reconciliation(task["id"])
            for i, gate in enumerate(passes[:2]):
                await gate.wait()
                self.service.lifecycle.metadata(task["id"], {"name": f"restore revision {i}"})
                gate.release.set()
            await passes[2].wait()
            gate = self.gate_call("lost_restore_ack", "thread/unarchive", error=TimeoutError("Lost restore ACK"))
            restoring = asyncio.create_task(self.service.archive(task["id"], "unarchive"))
            await gate.wait()
            gate.release.set()
            with self.assertRaises(TimeoutError):
                await restoring
            passes[2].release.set()
            await asyncio.wait_for(worker, 5)
            self.assertFalse(clock.pending, "New work was deferred to the periodic poll")
            await self.wait_for(lambda: not self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-restore")

    async def test_dormant_fork_archive_and_delete_do_not_reserve_the_project(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.settled()
        forked = await self.service.fork(task["id"])
        await self.service.archive(task["id"])
        await self.service.delete(forked["id"], forked["thread_id"])
        await self.settled()
        self.assertFalse(self.store.claims())
        await self.start_task("hold", key="after-dormant-family")

    async def test_unresolved_child_keeps_ownership_until_native_family_finishes(self):
        parent = await self.start_task("native_children")
        await self.wait_for(lambda: bool(self.service.requests(parent["id"])))
        request = self.service.requests(parent["id"])[0]
        await self.service.on_message({"method": "turn/completed", "params": {
            "threadId": parent["thread_id"], "turn": {"id": parent["turn_id"], "status": "completed"}}})
        with self.assertRaises(BridgeError) as blocked:
            await self.start_task("hold", key="child-pending")
        self.assertIn(request["task_id"], str(blocked.exception))
        self.assertTrue(self.service.get(parent["id"])["workspace_reserved"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.wait_for(lambda: not self.service.get(parent["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-child")

    async def test_goal_disable_fences_older_passive_observations(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.goal(task["id"], "set", objective="Continue", status="active", user_requested=True)
        await self.settled()
        older = self.gate_call("old_goal_observation", "thread/goal/get")
        observing = asyncio.create_task(self.service.reconcile_claims(task["id"]))
        await older.wait()
        disable = self.gate_call("disable_goal", "thread/goal/set", before=True)
        disabling = asyncio.create_task(self.service.goal(task["id"], "set", status="complete", user_requested=True))
        await disable.wait()
        pending = self.service.claim_snapshot(task["id"], "goal")
        older.release.set()
        await asyncio.wait_for(observing, 5)
        self.assertEqual(self.service.claim_snapshot(task["id"], "goal")["revision"], pending["revision"])
        disable.release.set()
        await asyncio.wait_for(disabling, 5)
        self.assertNotIn("goal", {c["kind"] for c in self.service.get(task["id"])["workspace_claims"]})
        await self.start_task("hold", key="after-goal-disable")

    async def test_two_waiting_starts_cannot_both_acquire_the_same_project(self):
        task = await self.command()
        gate = self.post_turn_gate(task)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await gate.wait()
        waiting = asyncio.Event()
        original = self.service.wait_for_reconciliation
        calls = 0
        async def joining(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                waiting.set()
            return await original(*args, **kwargs)
        with patch.object(self.service, "wait_for_reconciliation", joining):
            contenders = [asyncio.create_task(self.start_task("hold", key=f"competing-{i}")) for i in range(2)]
            await asyncio.wait_for(waiting.wait(), 5)
            gate.release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(*contenders, return_exceptions=True), 5)
        self.assertEqual(sum(isinstance(value, dict) for value in outcomes), 1)
        self.assertEqual(sum(isinstance(value, BridgeError) for value in outcomes), 1)

    async def test_cancelled_admission_waiter_does_not_cancel_accepted_start(self):
        task = await self.command()
        gate = self.post_turn_gate(task)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await gate.wait()
        joined = asyncio.Event()
        original = self.service.wait_for_reconciliation
        async def joining(*args, **kwargs):
            joined.set()
            return await original(*args, **kwargs)
        with patch.object(self.service, "wait_for_reconciliation", joining):
            caller = asyncio.create_task(self.start_task("hold", key="detached-start"))
            await asyncio.wait_for(joined.wait(), 5)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            gate.release.set()
            following = await self.start_task("hold", key="detached-start")
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='detached-start'").fetchone()[0], "done")
        self.assertEqual(len(read_fake_history(self.state)), 2)
