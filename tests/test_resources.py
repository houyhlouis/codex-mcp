"""Family accounting uses observed events; limits never imply rollback or billing."""

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp.evidence import artifact
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import arguments, dispatch, encoded_size
from tests.test_service import ServiceHarness


class ResourceTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = 100.0
        self.service.resources.clock = lambda: self.now

    def defaults(self, **budget):
        self.service.config = replace(self.cfg, budget=budget)
        self.service.resources.defaults = budget

    async def settings(self, task, budget, key="budget"):
        return await dispatch(self.service, "codex_settings", {"task_id": task["id"], "scope": "next",
            "budget": budget, "request_id": key})

    async def tokens(self, task, total, inputs=None, outputs=None, turn_id=None, last=None):
        values = {"totalTokens": total, "inputTokens": total if inputs is None else inputs,
            "outputTokens": 0 if outputs is None else outputs, "cachedInputTokens": 0, "reasoningOutputTokens": 0}
        await self.service.on_message({"method": "thread/tokenUsage/updated", "params": {"threadId": task["thread_id"],
            "turnId": turn_id or task["turn_id"], "tokenUsage": {"total": values, "last": {**values, **(last or {})}, "modelContextWindow": None}}})

    async def budgets_idle(self):
        async with asyncio.timeout(5):
            while self.service.budget_tasks:
                await asyncio.gather(*list(self.service.budget_tasks.values()))
                settled = asyncio.Event()
                asyncio.get_running_loop().call_soon(settled.set)
                await settled.wait()  # Let the completed worker remove itself.

    async def test_running_time_terminal_duration_and_duplicate_usage_are_truthful(self):
        task = await self.start_task("hold")
        self.now += 5
        await self.tokens(task, 15, 10, 5)
        await self.tokens(task, 15, 10, 5)
        shown = self.service.get(task["id"])
        usage = shown["family_usage"]
        self.assertEqual(usage["active_seconds"], 5)
        self.assertEqual(usage["turns"], 1)
        self.assertEqual(usage["tokens"], {"input": 10, "output": 5, "total": 15})
        self.assertTrue(usage["complete"])
        self.assertEqual(shown["usage"]["total"]["totalTokens"], 15)
        self.assertEqual(self.store.db.execute("SELECT thread_id FROM resource_threads WHERE task_id=?", (task["id"],)).fetchone()[0], task["thread_id"])
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        self.now += 20
        self.assertEqual(self.service.get(task["id"])["family_usage"]["active_seconds"], 5)

    async def test_decreasing_and_delayed_usage_is_flagged_and_does_not_decrease_totals(self):
        task = await self.start_task("hold")
        await self.tokens(task, 30, 20, 10)
        await self.tokens(task, 10, 7, 3)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual(usage["tokens"]["total"], 30)
        self.assertIn("usage_out_of_order", usage["gaps"])
        self.assertFalse(usage["complete"])

    async def test_known_lifecycle_gaps_keep_family_accounting_incomplete(self):
        task = await self.start_task("hold")
        await self.tokens(task, 10)
        self.assertTrue(self.service.get(task["id"])["family_usage"]["complete"])
        self.service.lifecycle.gap(task["id"], "event_ingress_budget")
        usage = self.service.get(task["id"])["family_usage"]
        self.assertFalse(usage["complete"])
        self.assertIn("native_evidence_gap", usage["gaps"])
        self.assertEqual(usage["tokens"]["total"], 10)

    async def test_old_turn_usage_is_delayed_until_a_current_turn_report(self):
        task = await self.start_task("hold")
        await self.tokens(task, 10)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        following = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "next-turn"})
        await self.tokens(task, 10)
        self.assertIn("usage_delayed", self.service.get(task["id"])["family_usage"]["gaps"])
        await self.tokens(following, 20)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertNotIn("usage_delayed", usage["gaps"])
        self.assertEqual(usage["tokens"]["total"], 20)

    async def child_request(self, parent):
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        request = self.service.requests(parent["id"])[0]
        return self.service.get(request["task_id"]), request

    async def test_child_usage_aggregates_and_missing_thread_usage_is_unknown(self):
        parent = await self.start_task("native_children")
        child, _ = await self.child_request(parent)
        await self.tokens(parent, 7, 5, 2)
        self.assertIsNone(self.service.get(parent["id"])["family_usage"]["tokens"])
        await self.tokens(child, 11, 8, 3)
        root_usage = self.service.get(parent["id"])["family_usage"]
        self.assertEqual(root_usage["tokens"], {"input": 13, "output": 5, "total": 18})
        self.assertEqual(root_usage, self.service.get(child["id"])["family_usage"])

    async def test_native_goal_budget_is_labelled_not_a_family_billing_cap(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.goal(task["id"], "set", objective="Explicit goal", status="paused", token_budget=123, user_requested=True)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual(usage["token_budget_preference"], "native_goal")
        self.assertEqual(usage["native_goal_budgets"][0]["token_budget"], 123)
        self.assertEqual(usage["native_goal_budgets"][0]["source"], "native_goal")

    async def test_concurrent_turn_admissions_reserve_the_last_hard_slot(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("last-turn", "turn/start", before=True)
        first = asyncio.create_task(dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "last-slot"}))
        try:
            await gate.wait()
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "over-cap"})
            self.assertEqual(error.exception.code, "budget_exceeded")
            row = self.store.db.execute("SELECT state,result FROM operations WHERE id='over-cap'").fetchone()
            self.assertEqual(row["state"], "done")
            self.assertTrue(json.loads(row["result"])["operation_rejected"])
        finally:
            gate.release.set()
            await asyncio.wait_for(first, 5)
        self.assertEqual(self.service.get(task["id"])["family_usage"]["turns"], 2)

    async def test_completion_before_ack_counts_once_and_keeps_the_hard_cap(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("completed-before-ack", "turn/start")
        following = asyncio.create_task(dispatch(self.service, "codex_send", {
            "task_id": task["id"], "prompt": "echo", "request_id": "early-completion"}))
        try:
            await gate.wait()
            await self.until(task["id"], ("completed",))
            self.assertEqual(self.service.get(task["id"])["family_usage"]["turns"], 2)
        finally:
            gate.release.set()
            await asyncio.wait_for(following, 5)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual(usage["turns"], 2)
        self.assertEqual(usage["reserved_turns"], 0)
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "over-after-ack"})
        self.assertEqual(error.exception.code, "budget_exceeded")

    async def test_concurrent_delegation_reserves_a_child_slot_until_its_turn_ends(self):
        self.defaults(max_children=1)
        task = await self.start_task("hold")
        gate = self.gate_call("child-admission", "turn/steer", before=True)
        args = {"task_id": task["id"], "prompt": "Ask one child to inspect", "expected_turn_id": task["turn_id"]}
        first = asyncio.create_task(dispatch(self.service, "codex_delegate", {**args, "request_id": "child-one"}))
        try:
            await gate.wait()
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_delegate", {**args, "request_id": "child-two"})
            self.assertEqual(error.exception.details["what"], "max_children")
        finally:
            gate.release.set()
            await asyncio.wait_for(first, 5)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_children"], 0)

    async def test_native_children_over_cap_are_reported_and_not_hidden(self):
        self.defaults(max_children=1)
        task = await self.start_task("native_grandchildren")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: self.store.family_stats(task["id"])[0] >= 3), 5)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertGreater(usage["children"], 1)
        self.assertTrue(usage["native_limit_violations"][0]["not_preventable"])

    async def test_budget_on_a_child_is_refused_before_native_io(self):
        parent = await self.start_task("native_children")
        child, _ = await self.child_request(parent)
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Child budget reached native I/O")):
            with self.assertRaisesRegex(BridgeError, "only on the root task"):
                await self.settings(child, {"max_turns": 2})

    async def test_queue_slots_survive_lost_ack_and_a_confirmed_drain_releases_them(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        original = self.service.backend.call
        async def lost(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "thread/queue/add":
                raise TimeoutError("Applied queue add lost ACK")
            return reply
        with patch.object(self.service.backend, "call", lost), self.assertRaises(TimeoutError):
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "queued-unknown"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "queued-extra"})
        self.assertEqual(error.exception.code, "budget_exceeded")
        pending = (await self.service.queue(task["id"]))["data"][0]
        await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "delete", "queued_id": pending["id"], "request_id": "drained"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)

    async def test_confirmed_rejected_queue_add_does_not_leave_a_budget_slot(self):
        from codex_local_mcp.backend import RpcError
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        original = self.service.backend.call
        async def rejected(method, params, **kwargs):
            if method == "thread/queue/add":
                kwargs["before_send"]()
                raise RpcError({"code": -32600, "message": "rejected"})
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", rejected), self.assertRaises(RpcError):
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "queued-rejected"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)

    async def test_competing_queue_starts_cannot_borrow_the_same_reserved_turn(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        queued = await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add",
            "prompt": "hold", "request_id": "reserved-queue"})
        params = {"task_id": task["id"], "action": "start", "queued_id": queued["queuedSubmission"]["id"]}
        gate = self.gate_call("queued-last-turn", "thread/queue/start", before=True)
        first = asyncio.create_task(dispatch(self.service, "codex_queue", {**params, "request_id": "consume-one"}))
        try:
            await gate.wait()
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_queue", {**params, "request_id": "consume-two"})
            self.assertEqual(error.exception.code, "budget_exceeded")
        finally:
            gate.release.set()
            await asyncio.wait_for(first, 5)
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual(usage["turns"], 2)
        self.assertEqual(usage["reserved_turns"], 0)

    async def test_unknown_queue_start_cannot_lend_its_slot_to_a_second_start(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        queued = await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add",
            "prompt": "hold", "request_id": "queue-uncertain-start"})
        original = self.service.backend.call
        async def unknown(method, params, **kwargs):
            if method == "thread/queue/start":
                kwargs["before_send"]()
                self.service.native_write()
                raise TimeoutError("Queue start may have reached Codex")
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", unknown), self.assertRaises(TimeoutError):
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "start", "request_id": "unknown-start"})
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "start", "request_id": "unsafe-extra-start"})
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)
        await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "delete",
            "queued_id": queued["queuedSubmission"]["id"], "request_id": "cancel-unstarted-entry"})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 0)

    async def test_optional_queue_start_rejection_preserves_the_accepted_queue_slot(self):
        from codex_local_mcp.backend import OptionalCapabilityUnavailable
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add",
            "prompt": "hold", "request_id": "queue-before-unsupported-start"})
        original = self.service.backend.call
        async def unavailable(method, params, **kwargs):
            if method == "thread/queue/start":
                raise OptionalCapabilityUnavailable(method, self.service.backend.version, {})
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", unavailable), self.assertRaises(OptionalCapabilityUnavailable):
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "start", "request_id": "unsupported-start"})
        reservation = self.store.db.execute("SELECT turns,queue_owner FROM resource_reservations WHERE id='queue-before-unsupported-start'").fetchone()
        self.assertEqual(tuple(reservation), (1, None))
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)

    async def test_direct_reservation_and_usage_writes_commit_before_close(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.service.reserve_turn(task["id"])
        self.assertFalse(self.store.db.in_transaction)
        await self.tokens(task, 9)
        self.assertFalse(self.store.db.in_transaction)
        await self.service.close()
        self.service = self.new_service()
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual(usage["tokens"]["total"], 9)
        self.assertIn("usage_restart_gap", usage["gaps"])

    async def test_failed_input_validation_does_not_label_a_later_read_as_execution(self):
        task = await self.start_task("hold")
        context = {"key": "test-guard", "phase": "control", "native_write": False, "execution_write": False}
        key = self.service.current_operation.set(context["key"])
        operation = self.service.operation_context.set(context)
        try:
            with patch.object(self.service, "retain_input_images", side_effect=BridgeError("Input was rejected")):
                with self.assertRaises(BridgeError):
                    self.service.input_guard(task["id"], self.service.backend.generation, [])
            self.service.native_write()  # A recovery read would still be native I/O.
            self.assertTrue(context["native_write"])
            self.assertFalse(context["execution_write"])
        finally:
            self.service.operation_context.reset(operation)
            self.service.current_operation.reset(key)

    async def test_soft_stop_during_approval_interrupts_once_and_allows_recovery(self):
        task = await self.start_task("approval")
        await self.until(task["id"], ("waiting",))
        await self.settings(task, {"max_tokens": 10})
        await self.budgets_idle()
        gate = self.gate_call("budget-interrupt", "turn/interrupt", before=True)
        original, interrupts = self.service.backend.call, []
        async def observed(method, params, **kwargs):
            if method == "turn/interrupt":
                interrupts.append(dict(params))
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", observed), patch.object(self.service.backend, "respond", side_effect=AssertionError("Budget granted an approval")):
            await self.tokens(task, 11)
            try:
                await gate.wait()
                await self.tokens(task, 11)
                stopped = self.service.get(task["id"])["budget_stop"]
                self.assertEqual(stopped["what"], "max_tokens")
                self.assertTrue(stopped["may_overshoot"])
                self.assertIn("may overshoot", stopped["message"])
            finally:
                gate.release.set()
            await self.budgets_idle()
        await self.until(task["id"], ("interrupted",))
        self.assertEqual(len(interrupts), 1)
        resumed = await self.service.resume(task["id"])
        self.assertTrue(resumed["budget_stop"]["active"])
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "stopped"})
        self.assertEqual(error.exception.code, "budget_exceeded")
        await self.settings(task, {"max_tokens": 100}, "raised")
        following = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "allowed"})
        self.assertEqual(following["status"], "running")

    async def test_active_time_threshold_during_a_running_command_uses_normal_interrupt(self):
        task = await self.start_task("hold_command")
        await self.settings(task, {"max_active_seconds": 5})
        await self.budgets_idle()
        self.now = 106
        await self.tokens(task, 1)
        await self.budgets_idle()
        await self.until(task["id"], ("interrupted",))
        shown = self.service.get(task["id"])
        self.assertEqual(shown["budget_stop"]["what"], "max_active_seconds")
        self.assertEqual(shown["budget_stop"]["observed"], 6)
        result = self.service.get(task["id"], view="result")
        self.assertTrue(result["budget_stop"]["may_overshoot"])
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        self.assertEqual(report["budget_stop"]["reason"], "budget")

    async def test_output_threshold_records_observed_bytes_and_clear_allows_new_work(self):
        task = await self.start_task("hold_command")
        before = self.service.get(task["id"])["family_usage"]["output_bytes"]["total"]
        limit = before + 10000
        await self.settings(task, {"max_output_bytes": limit})
        await self.budgets_idle()
        self.store.event(task["id"], "bridge/large-output", {"text": "x" * 20000})
        await self.tokens(task, 1)
        await self.budgets_idle()
        await self.until(task["id"], ("interrupted",))
        shown = self.service.get(task["id"])
        self.assertEqual(shown["budget_stop"]["what"], "max_output_bytes")
        self.assertGreater(shown["budget_stop"]["observed"], limit)
        self.assertGreaterEqual(shown["family_usage"]["output_bytes"]["total"], shown["budget_stop"]["observed"])
        await self.settings(task, {}, "clear")
        result = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "after-clear"})
        self.assertEqual(result["status"], "running")

    async def test_stream_and_pending_review_bytes_are_budget_checkpoints(self):
        for source in ("stream", "approval"):
            with self.subTest(source=source):
                task = await self.start_task("hold_command", source, "bytes-" + source)
                before = self.service.get(task["id"])["family_usage"]["output_bytes"]["total"]
                await self.settings(task, {"max_output_bytes": before + 10000}, "limit-" + source)
                await self.budgets_idle()
                params = {"threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "budget-" + source}
                if source == "stream":
                    message = {"method": "item/agentMessage/delta", "params": {**params, "delta": "x" * 20000}}
                else:
                    message = {"id": 99172, "method": "item/commandExecution/requestApproval",
                        "params": {**params, "command": "echo fixture", "cwd": task["project"], "reason": "x" * 20000}}
                with patch.object(self.service.backend, "respond", side_effect=AssertionError("Storage budget sent an approval")):
                    await self.service.on_message(message)
                    await self.budgets_idle()
                stopped = self.service.get(task["id"])["budget_stop"]
                self.assertIsNotNone(stopped)
                self.assertEqual(stopped["what"], "max_output_bytes")
                await self.until(task["id"], ("interrupted",))

    async def test_pending_denial_is_allowed_while_a_budget_interrupt_is_in_flight(self):
        task = await self.start_task("approval")
        waiting = await self.until(task["id"], ("waiting",))
        await self.settings(task, {"max_tokens": 1})
        await self.budgets_idle()
        gate = self.gate_call("denial-during-budget", "turn/interrupt", before=True)
        await self.tokens(task, 2)
        try:
            await gate.wait()
            await self.service.respond(waiting["pending_requests"][0]["id"], {"decision": "decline"})
        finally:
            gate.release.set()
        await self.budgets_idle()
        self.assertEqual((await self.until(task["id"], ("completed", "interrupted")))["budget_stop"]["reason"], "budget")

    async def test_lost_budget_interrupt_ack_is_reported_and_never_replayed(self):
        task = await self.start_task("hold_command")
        await self.settings(task, {"max_tokens": 1})
        await self.budgets_idle()
        original, sent = self.service.backend.call, []
        async def dropped(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "turn/interrupt":
                sent.append(params["turnId"])
                raise TimeoutError("Applied interrupt lost acknowledgement")
            return result
        with patch.object(self.service.backend, "call", dropped):
            await self.tokens(task, 2)
            await self.budgets_idle()
            await self.until(task["id"], ("interrupted",))
            await self.tokens(task, 2)
            await self.budgets_idle()
        self.assertEqual(sent, [task["turn_id"]])
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        self.assertIn("budget_interrupt_unverified", report["unknown"])
        self.assertFalse(report["automatic_retry"])
        self.assertTrue(report["budget_stop"]["may_overshoot"])

    async def test_unknown_tokens_do_not_trigger_a_stop(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_tokens": 1})
        await self.budgets_idle()
        self.assertIsNone(self.service.get(task["id"])["family_usage"]["tokens"])
        self.assertIsNone(self.service.get(task["id"])["budget_stop"])

    async def test_unknown_usage_cannot_clear_a_proven_stop_with_an_unchanged_budget(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_tokens": 10})
        await self.budgets_idle()
        await self.tokens(task, 100)
        await self.budgets_idle()
        await self.until(task["id"], ("interrupted",))
        await self.tokens(task, 5)
        self.assertIn("usage_out_of_order", self.service.get(task["id"])["family_usage"]["gaps"])
        for key, limit in (("unchanged", 10), ("still-under-proof", 20)):
            await self.settings(task, {"max_tokens": limit}, key)
            self.assertTrue(self.service.get(task["id"])["budget_stop"]["active"])
        await self.settings(task, {"max_tokens": 100}, "covers-proven-usage")
        self.assertFalse(self.service.get(task["id"])["budget_stop"]["active"])

    async def test_usage_for_an_unobserved_turn_is_incomplete_and_cannot_stop_work(self):
        task = await self.start_task("hold")
        await self.settings(task, {"max_tokens": 1})
        await self.tokens(task, 10, turn_id="unobserved-turn")
        await self.budgets_idle()
        shown = self.service.get(task["id"])
        self.assertIn("usage_turn_unknown", shown["family_usage"]["gaps"])
        self.assertFalse(shown["family_usage"]["complete"])
        self.assertIsNone(shown["budget_stop"])

    async def test_journal_and_artifact_bytes_are_counted_once_and_follow_pruning(self):
        task = await self.start_task("hold")
        with self.store.transaction():
            seq = self.store.event(task["id"], "bridge/storage-fixture", {"text": "界\n\"" * 50})
            saved = artifact(self.store.db, task["id"], task["turn_id"], b"captured once")
            artifact(self.store.db, task["id"], task["turn_id"], b"not another copy", source_seq=seq, locator=["text"])
        usage = self.service.get(task["id"])["family_usage"]
        expected = self.store.db.execute("SELECT SUM(LENGTH(CAST(data AS BLOB))) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        self.assertEqual(usage["output_bytes"], {"journal": expected, "artifacts": 13, "total": expected + 13})
        with self.store.transaction():
            self.store.db.execute("DELETE FROM artifacts WHERE id=?", (saved["artifact_id"],))
            self.store.db.execute("DELETE FROM events WHERE seq=?", (seq,))
        updated = self.service.get(task["id"])["family_usage"]["output_bytes"]
        self.assertEqual(updated["artifacts"], 0)
        self.assertLess(updated["journal"], expected)

    async def test_restart_open_turn_time_is_unknown_until_native_duration_is_reconciled(self):
        task = await self.start_task("hold")
        self.now = 105
        self.service.resources.disconnect()
        self.assertIsNone(self.service.get(task["id"])["family_usage"]["active_seconds"])
        self.service.resources.complete(task["id"], task["turn_id"], {"durationMs": 7000}, recovered=True)
        self.assertEqual(self.service.get(task["id"])["family_usage"]["active_seconds"], 7)

    async def test_running_native_start_time_can_reconcile_a_restart_gap(self):
        task = await self.start_task("hold")
        self.service.resources.disconnect()
        self.assertIsNone(self.service.get(task["id"])["family_usage"]["active_seconds"])
        with patch("codex_local_mcp.resources.time.time", return_value=1000):
            self.service.resources.reconcile(task["id"], {"id": task["turn_id"], "status": "inProgress", "startedAt": 993})
        self.assertEqual(self.service.get(task["id"])["family_usage"]["active_seconds"], 7)
        self.now += 2
        self.assertEqual(self.service.get(task["id"])["family_usage"]["active_seconds"], 9)

    async def test_native_duration_replaces_an_observed_interval_once(self):
        task = await self.start_task("hold")
        self.now = 105
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        self.assertEqual(self.service.get(task["id"])["family_usage"]["active_seconds"], 5)
        turn = {"id": task["turn_id"], "status": "interrupted", "durationMs": 7000}
        with self.store.transaction():
            self.service.resources.reconcile(task["id"], turn, idle=True)
        self.assertEqual(self.service.get(task["id"])["family_usage"]["active_seconds"], 7)
        revision = self.store.db.execute("SELECT revision FROM resource_families WHERE family_id=?", (task["id"],)).fetchone()[0]
        with self.store.transaction():
            self.service.resources.reconcile(task["id"], turn, idle=True)
        self.assertEqual(self.store.db.execute("SELECT revision FROM resource_families WHERE family_id=?", (task["id"],)).fetchone()[0], revision)

    async def test_real_backend_restart_keeps_hard_counts_and_reconciles_native_timing(self):
        self.defaults(max_turns=1)
        task = await self.start_task("hold_command")
        await self.service.close()
        self.service = self.new_service()
        shown = self.service.get(task["id"])
        self.assertIsNone(shown["family_usage"]["active_seconds"])
        self.assertEqual(shown["family_usage"]["turns"], 1)
        original, sent = self.service.backend.call, []
        async def timed(method, params, **kwargs):
            sent.append(method)
            reply = await original(method, params, **kwargs)
            if method == "thread/turns/list":
                for turn in reply["data"]:
                    if turn["id"] == task["turn_id"]:
                        turn["durationMs"] = 7000
            return reply
        with patch.object(self.service.backend, "call", timed):
            resumed = await self.service.resume(task["id"])
        self.assertEqual(resumed["family_usage"]["active_seconds"], 7)
        self.assertNotIn("turn/start", sent)
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "restart-cap"})
        self.assertEqual(error.exception.code, "budget_exceeded")

    async def test_native_goal_continuations_are_counted_separately(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.goal(task["id"], "set", objective="Explicit continuation", status="active", user_requested=True)
        self.assertEqual(self.service.get(task["id"])["family_usage"]["reserved_turns"], 1)
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"],
            "turn": {"id": "goal-activation-turn", "status": "inProgress", "items": []}}})
        activation = self.service.get(task["id"])["family_usage"]
        self.assertEqual((activation["turns"], activation["native_turns"], activation["reserved_turns"]), (2, 0, 0))
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": task["thread_id"],
            "turn": {"id": "goal-activation-turn", "status": "completed", "items": []}}})
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"],
            "turn": {"id": "native-goal-continuation", "status": "inProgress", "items": []}}})
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual(usage["turns"], 2)
        self.assertEqual(usage["native_turns"], 1)

    async def test_goal_activation_at_the_turn_cap_is_rejected(self):
        self.defaults(max_turns=1)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        with self.assertRaises(BridgeError) as error:
            await self.service.goal(task["id"], "set", objective="Explicit continuation", status="active", user_requested=True)
        self.assertEqual(error.exception.code, "budget_exceeded")

    async def test_goal_continuation_cannot_consume_a_still_pending_queue_budget_slot(self):
        self.defaults(max_turns=2)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        queued = await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add",
            "prompt": "hold", "request_id": "pending-during-goal"})
        turn = {"id": "native-goal-continuation", "status": "inProgress", "items": []}
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"], "turn": turn}})
        self.service.resources.reconcile(task["id"], turn, queue=[queued["queuedSubmission"]])
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual((usage["turns"], usage["native_turns"], usage["reserved_turns"]), (1, 1, 1))
        self.assertIn("queued_turn_unconfirmed", usage["gaps"])
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "queue-still-owns-slot"})
        self.assertEqual(error.exception.code, "budget_exceeded")
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": task["thread_id"],
            "turn": {**turn, "status": "completed"}}})
        next_turn = {"id": "consumed-queue-turn", "status": "inProgress", "items": []}
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"], "turn": next_turn}})
        self.service.resources.reconcile(task["id"], next_turn, queue=[])
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual((usage["turns"], usage["native_turns"], usage["reserved_turns"]), (2, 1, 0))
        self.assertNotIn("queued_turn_unconfirmed", usage["gaps"])

    async def test_one_observed_queue_turn_cannot_release_two_reserved_hard_slots(self):
        self.defaults(max_turns=3)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        for key in ("first-queued", "second-queued"):
            await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add",
                "prompt": "hold", "request_id": key})
        turn = {"id": "one-observed-successor", "status": "inProgress", "items": []}
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"], "turn": turn}})
        self.service.resources.reconcile(task["id"], turn, queue=[])
        usage = self.service.get(task["id"])["family_usage"]
        self.assertEqual((usage["turns"], usage["reserved_turns"]), (2, 1))
        self.assertIn("queued_turn_unconfirmed", usage["gaps"])
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "not-a-free-slot"})
        self.assertEqual(error.exception.code, "budget_exceeded")

    async def test_outcome_accounting_is_bounded_and_changes_with_usage(self):
        task = await self.start_task("hold")
        first = await self.service.events(task_id=task["id"], view="outcome")
        await self.tokens(task, 9)
        changed = await self.service.events(task_id=task["id"], view="outcome", cursors=first["cursors"])
        usage = changed["outcomes"][0]["family_usage"]
        self.assertEqual(usage["tokens"]["total"], 9)
        self.assertLess(encoded_size(usage), 1024)

    def test_budget_fields_are_closed_positive_bounded_integers(self):
        base = {"task_id": "root", "scope": "next", "request_id": "key"}
        for budget in ({"max_turns": 0}, {"max_tokens": True}, {"unknown": 1}, {"max_active_seconds": -1}, {"max_children": 10001}):
            with self.subTest(budget=budget), self.assertRaises(BridgeError):
                arguments("codex_settings", {**base, "budget": budget})
        self.assertEqual(arguments("codex_settings", {**base, "budget": {}})["budget"], {})


class BudgetCatalogTest(unittest.IsolatedAsyncioTestCase):
    async def test_modern_catalog_refusal_names_the_enforced_budget(self):
        from codex_local_mcp.contract import MCP_MODERN_CATALOG_BUDGET
        from codex_local_mcp.mcp_modern import ModernMCP
        adapter = ModernMCP(SimpleNamespace(config=None))
        with self.assertRaisesRegex(BridgeError, f"Modern MCP tool catalog exceeds {MCP_MODERN_CATALOG_BUDGET} bytes") as error:
            await adapter.result("tools", {"padding": "x" * MCP_MODERN_CATALOG_BUDGET}, catalog=True)
        self.assertEqual(error.exception.code, "result_too_large")

    def test_fixture_normalisation_preserves_resource_facts(self):
        from tests.modern_wire import normalise_modern
        from pathlib import Path
        usage = {"active_seconds": 2.85, "known_active_seconds": 1.12, "turns": 2,
            "tokens": {"input": 5, "output": 3, "total": 8}, "complete": False, "gaps": ["usage_delayed"],
            "output_bytes": {"journal": 3000, "artifacts": 5000, "total": 8000}}
        value = normalise_modern({"family_usage": usage}, Path.cwd())["family_usage"]
        self.assertEqual(value["active_seconds"], 1)
        self.assertEqual(value["output_bytes"], {"journal": 1, "artifacts": 1, "total": 2})
        for key in ("turns", "tokens", "complete", "gaps"):
            self.assertEqual(value[key], usage[key])
