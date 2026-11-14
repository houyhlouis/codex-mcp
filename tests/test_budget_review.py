"""Budget boundaries use gates, observed counters and an explicit timer clock."""

import asyncio
from unittest.mock import patch

from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests import test_resources
from tests.test_service import ServiceHarness


class BudgetClock:
    def __init__(self, service, now):
        self.service, self.now, self.entries = service, now, []

    def later(self, family, seconds, token):
        handle = asyncio.get_running_loop().create_future()
        self.entries.append((family, self.now() + seconds, token, handle))
        return handle

    @property
    def pending(self):
        return [row for row in self.entries if not row[3].done()]

    def advance(self):
        for family, deadline, token, handle in self.pending:
            if deadline <= self.now():
                handle.set_result(None)
                self.service.budget_expired(family, token)


class BudgetReviewTest(ServiceHarness):
    defaults = test_resources.ResourceTest.defaults
    settings = test_resources.ResourceTest.settings
    tokens = test_resources.ResourceTest.tokens
    budgets_idle = test_resources.ResourceTest.budgets_idle

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = 100.0
        self.service.resources.clock = lambda: self.now
        self.clock = BudgetClock(self.service, lambda: self.now)
        self.service.budget_later = self.clock.later

    async def test_idle_delegation_cannot_reuse_completed_expected_turn(self):
        self.defaults(max_turns=1)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_delegate", {"task_id": task["id"], "prompt": "hold",
                "expected_turn_id": task["turn_id"], "request_id": "stale-delegate"})
        self.assertEqual(error.exception.code, "stale_turn")
        self.assertEqual(self.service.get(task["id"])["family_usage"]["turns"], 1)

    async def test_completion_between_delegation_admission_and_dispatch_cannot_start_a_turn(self):
        self.defaults(max_turns=1)
        task = await self.start_task("hold")
        admitted = asyncio.Event()
        original = self.service.resources.admit
        def admission(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "racing-delegate":
                admitted.set()
            return result
        lock = self.service.family_locks.setdefault(task["id"], asyncio.Lock())
        await lock.acquire()
        with patch.object(self.service.resources, "admit", admission):
            delegation = asyncio.create_task(dispatch(self.service, "codex_delegate", {"task_id": task["id"],
                "prompt": "hold", "expected_turn_id": task["turn_id"], "request_id": "racing-delegate"}))
            try:
                await asyncio.wait_for(admitted.wait(), 5)
                await self.service.interrupt(task["id"], task["turn_id"])
                await self.until(task["id"], ("interrupted",))
            finally:
                lock.release()
            with self.assertRaises(BridgeError) as error:
                await asyncio.wait_for(delegation, 5)
        self.assertEqual(error.exception.code, "stale_turn")
        self.assertEqual(self.service.get(task["id"])["family_usage"]["turns"], 1)

    async def test_idle_goal_activation_needs_a_free_turn_before_native_io(self):
        self.defaults(max_turns=1)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Goal admission reached native I/O")):
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "status": "active",
                    "objective": "Explicit work", "user_requested": True, "request_id": "goal-cap"})
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)

    async def test_competing_goal_activations_cannot_take_the_same_last_slot(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("goal-slot", "thread/goal/set", before=True)
        args = {"task_id": task["id"], "action": "set", "status": "active", "objective": "Explicit work", "user_requested": True}
        first = asyncio.create_task(dispatch(self.service, "codex_goal", {**args, "request_id": "first-goal"}))
        try:
            await gate.wait()
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_goal", {**args, "request_id": "second-goal"})
            self.assertEqual(error.exception.code, "budget_exceeded")
        finally:
            gate.release.set()
            await asyncio.wait_for(first, 5)
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)
        await dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "status": "paused", "user_requested": True, "request_id": "pause-goal"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)

    async def test_completion_at_the_goal_write_upgrades_or_rejects_its_slot_atomically(self):
        self.defaults(max_turns=1)
        task = await self.start_task("hold")
        gate = self.gate_call("goal-write", "thread/goal/set", before=True)
        enabling = asyncio.create_task(dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "status": "active",
            "objective": "Explicit work", "user_requested": True, "request_id": "goal-race"}))
        try:
            await gate.wait()
            await self.service.interrupt(task["id"], task["turn_id"])
            await self.until(task["id"], ("interrupted",))
        finally:
            gate.release.set()
        with self.assertRaises(BridgeError) as error:
            await asyncio.wait_for(enabling, 5)
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertFalse([claim for claim in self.store.claims(task_id=task["id"]) if claim["kind"] == "goal"])
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)
        self.assertIsNone((await self.service.goal(task["id"]))["goal"])

    async def test_goal_start_before_ack_converts_once_and_later_native_continuations_do_not(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("goal-ack", "thread/goal/set")
        enabling = asyncio.create_task(dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "status": "active",
            "objective": "Explicit work", "user_requested": True, "request_id": "early-goal"}))
        try:
            await gate.wait()
            # The fake has no autonomous goal runner; emit its successor through
            # the native fixture, without a bridge start or resource reservation.
            await self.service.backend.call("turn/start", {"threadId": task["thread_id"],
                "input": [{"type": "text", "text": "hold", "text_elements": []}]})
            async with self.service.changed:
                await asyncio.wait_for(self.service.changed.wait_for(lambda: self.service.get(task["id"])["turn_id"] != task["turn_id"]), 5)
            usage = self.service.get(task["id"])["family_usage"]
            self.assertEqual((usage["turns"], usage["native_turns"], usage["reserved_turns"]), (2, 0, 0))
        finally:
            gate.release.set()
            await asyncio.wait_for(enabling, 5)
        current = self.service.get(task["id"])
        await self.service.interrupt(task["id"], current["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.service.backend.call("turn/start", {"threadId": task["thread_id"],
            "input": [{"type": "text", "text": "hold", "text_elements": []}]})
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: self.service.get(task["id"])["turn_id"] != current["turn_id"]), 5)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual((usage["turns"], usage["native_turns"], usage["reserved_turns"]), (2, 1, 0))

    async def test_lost_goal_ack_keeps_slot_and_confirmed_rejection_releases_it(self):
        from codex_local_mcp.backend import RpcError
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        original = self.service.backend.call
        async def rejected(method, params, **kwargs):
            if method == "thread/goal/set":
                kwargs["before_send"]()
                self.service.native_write()
                raise RpcError({"code": -32600, "message": "Rejected before enabling"})
            return await original(method, params, **kwargs)
        args = {"task_id": task["id"], "action": "set", "status": "active", "objective": "Explicit work", "user_requested": True}
        with patch.object(self.service.backend, "call", rejected), self.assertRaises(RpcError):
            await dispatch(self.service, "codex_goal", {**args, "request_id": "rejected-goal"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)
        async def lost(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "thread/goal/set":
                raise TimeoutError("Applied goal lost ACK")
            return result
        with patch.object(self.service.backend, "call", lost), self.assertRaises(TimeoutError):
            await dispatch(self.service, "codex_goal", {**args, "request_id": "lost-goal"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)

    async def test_goal_activation_does_not_steal_a_consumed_queues_slot(self):
        self.defaults(max_turns=3)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "status": "active",
            "objective": "Explicit goal", "user_requested": True, "request_id": "goal-before-queue"})
        await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "queued-after-goal"})
        await self.service.backend.call("thread/queue/start", {"threadId": task["thread_id"]})
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: self.service.get(task["id"])["turn_id"] != task["turn_id"]), 5)
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_reservations WHERE id='queued-after-goal' AND turns>0").fetchone())
        current = self.service.get(task["id"])
        await self.service.interrupt(task["id"], current["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "status": "paused", "user_requested": True, "request_id": "pause-after-queue"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)
        follow = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "remaining-slot"})
        self.assertEqual(follow["family_usage"]["turns"], 3)

    async def test_time_budget_expires_without_any_native_notification(self):
        task = await self.start_task("hold_command")
        await self.settings(task, {"max_active_seconds": 5})
        await self.budgets_idle()
        self.assertEqual(len(self.clock.pending), 1)
        self.assertEqual(self.clock.pending[0][1], 105)
        self.now = 105
        self.clock.advance()
        await self.budgets_idle()
        shown = await self.until(task["id"], ("interrupted",))
        self.assertEqual(shown["budget_stop"]["what"], "max_active_seconds")
        self.assertEqual(shown["budget_stop"]["observed"], 5)
        self.assertTrue(shown["budget_stop"]["may_overshoot"])
        self.assertFalse(self.clock.pending)

    async def test_time_timer_coalesces_changes_and_clears_without_native_calls(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_active_seconds": 5})
        await self.budgets_idle()
        first = self.clock.pending[0]
        self.now = 101
        for number in range(1, 6):
            await self.tokens(task, number)
        await self.budgets_idle()
        self.assertEqual(self.clock.pending, [first])
        await self.settings(task, {"max_active_seconds": 10}, "raise-time")
        await self.budgets_idle()
        self.assertTrue(first[3].cancelled())
        self.assertEqual(self.clock.pending[0][1], 110)
        await self.settings(task, {}, "clear-time")
        self.assertFalse(self.clock.pending)
        self.now = 1000
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Cleared timer sent native I/O")):
            self.clock.advance()
            self.service.budget_expired(task["id"], first[2])
            await self.budgets_idle()
        self.assertIsNone(self.service.get(task["id"])["budget_stop"])

    async def test_time_deadline_accounts_for_parallel_family_intervals_and_completion(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_active_seconds": 10})
        await self.budgets_idle()
        child = {**self.service.task(task["id"]), "id": "child", "thread_id": "native-child",
            "parent_task_id": task["id"], "root_task_id": task["id"], "status": "idle", "turn_id": None, "native_child": True}
        self.service.lifecycle.create(child)
        self.service.lifecycle.begin("child", "child-turn", source="event")
        self.service.schedule_budget_check("child")
        await self.budgets_idle()
        self.assertEqual(len(self.clock.pending), 1)
        self.assertEqual(self.clock.pending[0][1], 105)
        self.now = 102
        self.service.lifecycle.complete("child", "child-turn", "completed")
        self.service.schedule_budget_check("child")
        await self.budgets_idle()
        self.assertEqual(self.clock.pending[0][1], 108)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.budgets_idle()
        self.assertFalse(self.clock.pending)

    async def test_time_expiry_during_approval_never_answers_and_interrupts_only_once(self):
        task = await self.start_task("approval")
        await self.until(task["id"], ("waiting",))
        await self.settings(task, {"max_active_seconds": 5})
        await self.budgets_idle()
        timer = self.clock.pending[0]
        gate = self.gate_call("time-interrupt", "turn/interrupt", before=True)
        with patch.object(self.service.backend, "respond", side_effect=AssertionError("Timer answered an approval")):
            self.now = 105
            self.clock.advance()
            try:
                await gate.wait()
                self.service.budget_expired(task["id"], timer[2])
                self.now = 109
                self.clock.advance()
            finally:
                gate.release.set()
            await self.budgets_idle()
            await self.until(task["id"], ("interrupted",))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_interrupts WHERE task_id=?", (task["id"],)).fetchone()[0], 1)

    async def test_close_cancels_deadline_and_restart_unknown_time_cannot_rearm_it(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_active_seconds": 5})
        await self.budgets_idle()
        pending = self.clock.pending[0]
        await self.service.close()
        self.assertTrue(pending[3].cancelled())
        self.service = self.new_service()
        self.service.resources.clock = lambda: self.now
        self.service.budget_later = self.clock.later
        self.service.schedule_budget_check(task["id"])
        await self.budgets_idle()
        self.assertIsNone(self.service.get(task["id"])["family_usage"]["active_seconds"])
        self.assertFalse(self.clock.pending)

    async def test_native_deletion_cancels_time_deadline_without_inventing_duration(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_active_seconds": 5})
        await self.budgets_idle()
        self.assertEqual(len(self.clock.pending), 1)
        await self.service.on_message({"method": "thread/deleted", "params": {"threadId": task["thread_id"]}})
        await self.budgets_idle()
        self.assertFalse(self.clock.pending)
        self.assertIsNone(self.service.get(task["id"])["family_usage"]["active_seconds"])

    async def test_fork_counter_excludes_the_exact_inherited_baseline(self):
        task = await self.start_task("hold")
        await self.tokens(task, 15375, 10000, 5375)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "fork"})
        await self.tokens(fork, 15375, 10000, 5375, turn_id=task["turn_id"])
        self.assertEqual(self.service.get(fork["id"])["family_usage"]["tokens"]["total"], 0)
        await self.settings(fork, {"max_tokens": 16000})
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "fork-own"})
        await self.tokens(following, 30773, 20000, 10773,
            last={"totalTokens": 15398, "inputTokens": 10000, "outputTokens": 5398})
        await self.budgets_idle()
        shown = self.service.get(fork["id"])
        self.assertEqual(shown["family_usage"]["tokens"], {"input": 10000, "output": 5398, "total": 15398})
        self.assertIsNone(shown["budget_stop"])
        self.assertIn("inherited_history_not_counted", shown["family_usage"]["gaps"])

    async def test_unknown_fork_baseline_is_reported_and_never_stops_on_inherited_tokens(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "unknown-fork"})
        await self.settings(fork, {"max_tokens": 1})
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "unknown-own"})
        await self.tokens(following, 30773, last={"totalTokens": 15398, "inputTokens": 15398})
        await self.budgets_idle()
        shown = self.service.get(fork["id"])
        self.assertEqual(shown["family_usage"]["tokens"]["total"], 30773)
        self.assertIn("inherited_token_baseline_unknown", shown["family_usage"]["gaps"])
        self.assertIsNone(shown["budget_stop"])

    async def test_parent_baseline_is_used_even_when_fork_first_reports_after_own_start(self):
        task = await self.start_task("hold")
        await self.tokens(task, 15375)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "parent-baseline"})
        await self.settings(fork, {"max_tokens": 16000})
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "first-own"})
        await self.tokens(following, 30773, last={"totalTokens": 15398, "inputTokens": 15398})
        await self.budgets_idle()
        self.assertEqual(self.service.get(fork["id"])["family_usage"]["tokens"]["total"], 15398)
        self.assertIsNone(self.service.get(fork["id"])["budget_stop"])
        await self.service.close()
        self.service = self.new_service()
        self.service.resources.clock = lambda: self.now
        shown = self.service.get(fork["id"])
        self.assertEqual(shown["family_usage"]["tokens"]["total"], 15398)
        self.assertIn("usage_restart_gap", shown["family_usage"]["gaps"])
        await self.service.resume(fork["id"])
        await self.tokens(following, 30773, last={"totalTokens": 15398, "inputTokens": 15398})
        shown = self.service.get(fork["id"])
        self.assertEqual(shown["family_usage"]["tokens"]["total"], 15398)
        self.assertNotIn("usage_restart_gap", shown["family_usage"]["gaps"])

    async def test_past_turn_fork_does_not_use_the_parents_later_counter(self):
        task = await self.start_task("hold")
        await self.tokens(task, 15375)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        next_parent = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "parent-next"})
        await self.tokens(next_parent, 30773)
        await self.service.interrupt(task["id"], next_parent["turn_id"])
        await self.until(task["id"], ("interrupted",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "last_turn_id": task["turn_id"], "request_id": "past-fork"})
        self.assertIn("inherited_token_baseline_unknown", fork["family_usage"]["gaps"])
        await self.tokens(fork, 15375, turn_id=task["turn_id"])
        self.assertEqual(self.service.get(fork["id"])["family_usage"]["tokens"]["total"], 0)
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "past-own"})
        await self.tokens(following, 30773)
        self.assertEqual(self.service.get(fork["id"])["family_usage"]["tokens"]["total"], 15398)

    async def test_proven_rejection_before_first_work_allows_a_later_baseline(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "rejected-fork"})
        resource = self.service.resources
        resource.admit("never-sent", fork["id"], "codex_send", {"task_id": fork["id"]}, {})
        resource.bind("never-sent", fork["id"], token="known-not-sent")
        resource.finish("never-sent", rejected=True)
        await self.tokens(fork, 15375, turn_id="inherited")
        self.assertEqual(self.service.get(fork["id"])["family_usage"]["tokens"]["total"], 0)

    async def test_counter_below_known_baseline_is_unknown_instead_of_a_budget_stop(self):
        task = await self.start_task("hold")
        await self.tokens(task, 15375)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "low-counter-fork"})
        await self.settings(fork, {"max_tokens": 1})
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "low-counter-own"})
        await self.tokens(following, 1000)
        await self.budgets_idle()
        shown = self.service.get(fork["id"])
        self.assertIn("usage_below_baseline", shown["family_usage"]["gaps"])
        self.assertEqual(shown["family_usage"]["tokens"]["total"], 0)
        self.assertIsNone(shown["budget_stop"])

    async def test_recovered_work_cannot_be_mistaken_for_a_pre_work_baseline(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "recovered-fork"})
        await self.settings(fork, {"max_tokens": 1})
        self.service.lifecycle.begin(fork["id"], "missed-start", source="recovery")
        recovered = self.service.get(fork["id"])
        await self.tokens(recovered, 30773)
        self.service.lifecycle.complete(fork["id"], "missed-start", "completed", native={"durationMs": 1000})
        await self.tokens(recovered, 30773)
        await self.budgets_idle()
        shown = self.service.get(fork["id"])
        self.assertEqual(shown["family_usage"]["tokens"]["total"], 30773)
        self.assertIn("inherited_token_baseline_unknown", shown["family_usage"]["gaps"])
        self.assertIsNone(shown["budget_stop"])

    async def test_native_child_of_a_fork_does_not_inherit_its_accounting_baseline(self):
        task = await self.start_task("hold")
        await self.tokens(task, 15375)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        fork = await dispatch(self.service, "codex_fork", {"task_id": task["id"], "request_id": "family-fork"})
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "native_children", "request_id": "family-own"})
        child, _ = await test_resources.ResourceTest.child_request(self, following)
        await self.tokens(following, 30773)
        await self.tokens(child, 11)
        shown = self.service.get(fork["id"])
        self.assertEqual(shown["family_usage"]["tokens"]["total"], 15398 + 11)
        self.assertNotIn("inherited_token_baseline_unknown", shown["family_usage"]["gaps"])

    async def test_import_uses_the_counter_observed_before_its_first_own_turn(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"])
        native = await self.service.backend.call("thread/start", await self.service.thread_options(task))
        imported = await self.service.sessions(workdir=task["project"], action="import", thread_id=native["thread"]["id"], confirm_stopped=True)
        await self.tokens(imported, 15375, turn_id="inherited-turn")
        self.assertEqual(self.service.get(imported["id"])["family_usage"]["tokens"]["total"], 0)
        following = await dispatch(self.service, "codex_send", {"task_id": imported["id"], "prompt": "hold", "request_id": "import-own"})
        await self.tokens(following, 30773)
        self.assertEqual(self.service.get(imported["id"])["family_usage"]["tokens"]["total"], 15398)

    async def test_soft_checkpoint_vm_cost_does_not_grow_with_closed_turns(self):
        task = await self.start_task("hold")
        async def measured():
            steps = 0
            def progressed():
                nonlocal steps
                steps += 1
                return 0
            self.store.db.set_progress_handler(progressed, 1)
            try:
                await self.service.check_budget(task["id"])
            finally:
                self.store.db.set_progress_handler(None, 0)
            return steps
        policies = {"tokens": {"max_tokens": 10**12}, "tokens_and_time": {"max_tokens": 10**12, "max_active_seconds": 20000000}}
        before = {}
        for name, policy in policies.items():
            await self.settings(task, policy, "bench-before-" + name)
            await self.budgets_idle()
            before[name] = await measured()
        with self.store.transaction():
            self.store.db.executemany("""INSERT INTO resource_turns(task_id,turn_id,started_at,ended_at,seconds,server_started)
                VALUES (?,?,1,2,1,1)""", ((task["id"], "closed-" + str(number)) for number in range(10000)))
        for name, policy in policies.items():
            await self.settings(task, policy, "bench-after-" + name)
            await self.budgets_idle()
            after = await measured()
            print(f"soft checkpoint VM steps ({name}): 0 closed={before[name]}; 10000 closed={after}")
            self.assertLessEqual(after, before[name] + 1000)
