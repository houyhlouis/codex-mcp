"""A causal root from another thread is not this thread's outer execution."""

import asyncio
import json
from unittest.mock import patch

from codex_local_mcp import native_turns
from codex_local_mcp.backend import RpcError
from codex_local_mcp.tools import dispatch
from tests import test_nested_turns as nested_cases
from tests.test_service import ServiceHarness


class TurnScopeTest(ServiceHarness):
    last_event = nested_cases.NestedTurnTest.last_event
    event_after = nested_cases.NestedTurnTest.event_after
    idle_task = nested_cases.NestedTurnTest.idle_task

    async def completed_family(self, prompt="native_children"):
        parent = await self.start_task(prompt)
        await self.until(parent["id"], ("waiting", "running"))
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        request = self.service.requests(parent["id"])[0]
        child = self.service.get(request["task_id"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))
        await self.service.reconcile_claims(parent["id"], explicit=True)
        return self.service.get(parent["id"]), self.service.get(child["id"])

    async def test_child_with_parent_causal_root_completes_and_allows_fork(self):
        parent, child = await self.completed_family()
        self.assertEqual(child["status"], "completed")
        turn = self.last_event(child["id"], "turn/completed")["turn"]
        self.assertEqual(turn["rootTurnId"], parent["turn_id"])
        self.assertNotEqual(turn["id"], parent["turn_id"])
        self.assertEqual(child["turn_id"], turn["id"])
        history = await self.service.history(child["id"])
        self.assertEqual(history["data"][0]["logical_turn_id"], turn["id"])
        self.assertEqual(child["family_usage"]["turns"], 1)
        self.assertEqual(child["family_usage"]["native_turns"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns WHERE task_id=?", (child["id"],)).fetchone()[0], 1)
        result = self.service.get(child["id"], view="result")["result"]
        self.assertEqual(result["turn_id"], turn["id"])
        self.assertTrue(result["observed_commands"])
        await self.service.reconcile_claims(parent["id"], explicit=True)
        fork = await dispatch(self.service, "codex_fork", {"task_id": parent["id"], "request_id": "family-fork"})
        self.assertEqual(fork["forked_from_task_id"], parent["id"])

    async def test_early_unknown_review_start_waits_for_local_root_evidence(self):
        task = await self.idle_task()
        self.service.resources.set_budget(task["id"], {"max_turns": 2})
        items = self.gate_callback("first-root-item", "item/started")
        ack = self.gate_call("early-root-ack", "review/start")
        after = self.store.highwater()
        running = asyncio.create_task(dispatch(self.service, "codex_review", {"task_id": task["id"],
            "target": "custom", "instructions": "review_early_start", "request_id": "early-review"}))
        try:
            await ack.wait()
            await items.wait()
            started = self.last_event(task["id"], "turn/started", after)["turn"]
            current = self.service.get(task["id"])
            self.assertEqual(current["turn_id"], task["turn_id"])
            self.assertIsNone(self.service.lifecycle.turn(task["id"], started["id"]))
            self.assertIsNone(self.service.lifecycle.turn(task["id"], started["rootTurnId"]))
            items.release.set()
            completed = await self.event_after(task["id"], "turn/completed", after)
            self.assertEqual(self.service.get(task["id"])["turn_id"], completed["turn"]["id"])
        finally:
            items.release.set()
            ack.release.set()
            await asyncio.wait_for(running, 5)
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["family_usage"]["turns"], 2)
        self.assertIsNone(self.service.lifecycle.turn(task["id"], started["id"]))
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_grandchild_causal_root_is_not_the_parent_or_grandparents_execution(self):
        parent, grandchild = await self.completed_family("native_grandchildren")
        self.assertEqual(grandchild["status"], "completed")
        members = [self.service.get(key) for key in self.service.family_ids(parent["id"])]
        self.assertEqual(len(members), 3)
        self.assertTrue(all(m["status"] == "completed" for m in members))
        self.assertEqual(parent["family_usage"]["turns"], 1)
        self.assertEqual(parent["family_usage"]["native_turns"], 2)
        self.assertEqual(self.last_event(grandchild["id"], "turn/completed")["turn"]["rootTurnId"], parent["turn_id"])

    async def test_cross_thread_root_does_not_overwrite_a_later_child_turn(self):
        parent, child = await self.completed_family()
        later = await dispatch(self.service, "codex_send", {"task_id": child["id"], "prompt": "hold", "request_id": "child-follow-up"})
        old = self.last_event(child["id"], "turn/completed")["turn"]
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": child["thread_id"], "turn": old}})
        current = self.service.get(child["id"])
        self.assertEqual((current["turn_id"], current["turn_status"], current["status"]), (later["turn_id"], "inProgress", "running"))
        self.assertEqual(self.service.get(parent["id"])["turn_id"], parent["turn_id"])

    async def test_child_inline_review_with_foreign_root_is_held_until_resume(self):
        parent, child = await self.completed_family()
        after = self.store.highwater()
        await dispatch(self.service, "codex_review", {"task_id": child["id"], "target": "custom",
            "instructions": "review_parent_root", "request_id": "child-review"})
        completed = await self.event_after(child["id"], "turn/completed", after)
        current = self.service.get(child["id"])
        self.assertEqual(completed["turn"]["rootTurnId"], parent["turn_id"])
        self.assertEqual(current["status"], "needs_resume")
        self.assertTrue(current["workspace_reserved"])
        self.assertIn(child["id"], self.service.protocol_quarantine)
        self.assertNotEqual(current["turn_status"], "completed")
        errors = self.last_event(child["id"], "bridge/protocol_error")
        self.assertIn("codex_resume, then codex_history", json.dumps(errors))
        resumed = await dispatch(self.service, "codex_resume", {"task_id": child["id"], "request_id": "resume-child-review"})
        self.assertEqual((resumed["turn_id"], resumed["turn_status"]), (completed["turn"]["id"], "completed"))
        self.assertNotIn(child["id"], self.service.protocol_quarantine)
        self.assertFalse(resumed["workspace_reserved"])
        self.assertEqual(resumed["family_usage"]["turns"], 2)
        self.assertEqual(resumed["family_usage"]["native_turns"], 1)

    async def test_matching_native_and_root_ids_never_delete_the_resource_record(self):
        parent, child = await self.completed_family()
        self.service.resources.begin(child, parent["turn_id"], source="recovery")
        self.service.resources.reconcile(child["id"], {"id": parent["turn_id"], "rootTurnId": parent["turn_id"], "status": "inProgress"})
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM resource_turns WHERE task_id=? AND turn_id=?",
            (child["id"], parent["turn_id"])).fetchone())

    async def test_copied_parent_history_row_cannot_make_child_causal_root_local(self):
        parent, child = await self.completed_family()
        turn = self.last_event(child["id"], "turn/completed")["turn"]
        parent_turn = self.last_event(parent["id"], "turn/completed")["turn"]
        original = self.service.backend.call
        async def copied_history(method, params, **kwargs):
            if method == "thread/turns/list" and params["threadId"] == child["thread_id"]:
                return {"data": [turn, parent_turn], "nextCursor": None}
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", copied_history):
            history = await self.service.history(child["id"])
        self.assertEqual(history["data"][0]["logical_turn_id"], turn["id"])
        scope = native_turns.for_task(self.store, child["id"])
        scope.known.add(parent["turn_id"])
        self.assertEqual(native_turns.logical_id(turn, scope), turn["id"])

    async def test_fork_inherited_history_does_not_own_the_source_threads_turn(self):
        source = await self.idle_task()
        fork = await dispatch(self.service, "codex_fork", {"task_id": source["id"], "request_id": "scope-fork"})
        scope = native_turns.for_task(self.store, fork["id"])
        scope.known.add(source["turn_id"])
        turn = {"id": "fork-own-turn", "rootTurnId": source["turn_id"], "status": "completed", "items": []}
        self.assertEqual(native_turns.logical_id(turn, scope), turn["id"])
        self.assertFalse(native_turns.nested(turn, scope))

    async def test_unknown_causal_root_without_inline_review_is_its_own_turn(self):
        parent, child = await self.completed_family()
        turn = {**self.last_event(child["id"], "turn/completed")["turn"], "rootTurnId": "unrecorded-causal-root"}
        original = self.service.backend.call
        async def history(method, params, **kwargs):
            if method == "thread/turns/list" and params["threadId"] == child["thread_id"]:
                return {"data": [turn], "nextCursor": "more-history"}
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", history):
            latest = await self.service.latest_turn(child["id"])
            await self.service.on_message({"method": "turn/completed", "params": {"threadId": child["thread_id"], "turn": turn}})
        self.assertEqual(latest["id"], turn["id"])
        self.assertEqual(self.service.get(child["id"])["turn_status"], "completed")
        self.assertNotIn(child["id"], self.service.protocol_quarantine)

    async def test_accepted_queue_slot_cannot_defer_an_ordinary_child_turn(self):
        parent, child = await self.completed_family()
        await dispatch(self.service, "codex_queue", {"task_id": child["id"], "action": "add", "prompt": "next",
            "request_id": "accepted-child-queue"})
        turn = {"id": "queued-child-turn", "rootTurnId": "unknown-causal-root", "status": "inProgress", "items": []}
        await self.service.on_message({"method": "turn/started", "params": {"threadId": child["thread_id"], "turn": turn}})
        self.assertEqual(self.service.get(child["id"])["turn_id"], turn["id"])
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": child["thread_id"],
            "turn": {**turn, "status": "completed"}}})
        self.assertEqual(self.service.get(child["id"])["turn_status"], "completed")
        self.assertNotIn(child["id"], self.service.protocol_quarantine)

    async def test_current_turn_identity_is_local_without_a_derived_turn_row(self):
        previous = await self.idle_task()
        task = self.service.lifecycle.create({**previous, "id": "current-only-task", "root_task_id": "current-only-task",
            "thread_id": "current-only-thread", "turn_id": "current-only-turn"})
        self.assertIsNone(self.service.lifecycle.turn(task["id"], task["turn_id"]))
        scope = native_turns.for_task(self.store, task["id"])
        self.assertEqual(native_turns.logical_id({"id": "nested", "rootTurnId": task["turn_id"]}, scope), task["turn_id"])

    async def test_rejected_review_does_not_quarantine_later_native_child_work(self):
        parent, child = await self.completed_family()
        original = self.service.backend.call
        async def reject_review(method, params, **kwargs):
            if method == "review/start":
                kwargs["before_send"]()
                raise RpcError({"code": -32602, "message": "Review rejected"})
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", reject_review), self.assertRaises(RpcError):
            await dispatch(self.service, "codex_review", {"task_id": child["id"], "request_id": "rejected-review"})
        self.assertNotIn(child["id"], self.service.inline_reviews)
        turn = {"id": "native-child-next", "rootTurnId": parent["turn_id"], "status": "inProgress", "items": []}
        await self.service.on_message({"method": "turn/started", "params": {"threadId": child["thread_id"], "turn": turn}})
        self.assertEqual(self.service.get(child["id"])["turn_id"], turn["id"])
        self.assertNotIn(child["id"], self.service.protocol_quarantine)
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": child["thread_id"],
            "turn": {**turn, "status": "completed"}}})
        self.assertEqual(self.service.get(child["id"])["status"], "completed")

    async def test_lost_review_ack_without_root_evidence_stays_held_until_resume(self):
        task = await self.idle_task()
        items = self.gate_callback("unseen-root-item", "item/started")
        ack = self.gate_call("lost-review-ack", "review/start", error=TimeoutError())
        original = self.service.backend.call
        after = self.store.highwater()
        running = asyncio.create_task(dispatch(self.service, "codex_review", {"task_id": task["id"],
            "target": "custom", "instructions": "review_early_start", "request_id": "lost-early-review"}))
        try:
            await ack.wait()
            await items.wait()
            started = self.last_event(task["id"], "turn/started", after)["turn"]
            async def unresolved_history(method, params, **kwargs):
                if method == "thread/turns/list":
                    return {"data": [started], "nextCursor": None}
                return await original(method, params, **kwargs)
            ack.release.set()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(running, 5)
            with patch.object(self.service.backend, "call", unresolved_history):
                await self.service.reconcile_claims(task["id"], explicit=True)
            current = self.service.get(task["id"])
            self.assertTrue(current["workspace_reserved"])
            self.assertNotEqual(current["turn_id"], started["id"])
            self.assertIsNone(self.service.lifecycle.turn(task["id"], started["id"]))
            self.assertIsNone(self.service.lifecycle.turn(task["id"], started["rootTurnId"]))
        finally:
            items.release.set()
            ack.release.set()
        await self.event_after(task["id"], "turn/completed", after)
        resumed = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "request_id": "resume-lost-review"})
        self.assertEqual(resumed["turn_id"], started["rootTurnId"])
        self.assertEqual(resumed["turn_status"], "completed")
        self.assertFalse(resumed["workspace_reserved"])
