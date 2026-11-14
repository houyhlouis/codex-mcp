"""Outcome waits use indexed state, independent cursors and one deadline."""

import asyncio
import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

from codex_local_mcp.security import BridgeError
from codex_local_mcp import outcomes
from codex_local_mcp.retention import Retention
from codex_local_mcp.tools import BY_NAME, dispatch, result_size, validate
from tests.test_service import ServiceHarness


class OutcomeTest(ServiceHarness):
    def tasks(self, count, *, parent=None):
        project = self.root / "observed"
        project.mkdir(exist_ok=True)
        result = []
        with self.store.transaction():
            for i in range(count):
                identity = f"observed-{i}"
                result.append(self.store.save("tasks", {"id": identity, "thread_id": f"native-{i}",
                    "project": str(project), "status": "completed", "turn_id": f"turn-{i}", "turn_status": "completed",
                    "root_task_id": parent or identity, "parent_task_id": parent,
                    "summary": "large" * 5000, "diff": "unchanged" * 5000, "evidence_complete": True}))
        return result

    async def outcome(self, **values):
        return await dispatch(self.service, "codex_events", {"view": "outcome", **values})

    async def waiting(self, values):
        entered = asyncio.Event()
        original = self.service.changed.wait
        async def wait():
            entered.set()
            return await original()
        with patch.object(self.service.changed, "wait", wait):
            running = asyncio.create_task(self.outcome(**values, wait_seconds=25))
            await asyncio.wait_for(entered.wait(), 5)
        self.addAsyncCleanup(self.stop_wait, running)
        return running

    async def stop_wait(self, running):
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    async def test_batch_is_bounded_and_cursors_do_not_skip_unseen_changes(self):
        tasks = self.tasks(50)
        ids = [t["id"] for t in tasks]
        first = await self.outcome(task_ids=ids, limit=13)
        seen, page = set(), first
        for _ in range(4):
            seen.update(t["id"] for t in page["outcomes"])
            validate(page, BY_NAME["codex_events"]["outputSchema"])
            page = await self.outcome(task_ids=ids, limit=13, cursors=page["cursors"])
        self.assertEqual(seen, set(ids))
        self.assertEqual(page["outcomes"], [])
        self.assertFalse(page["has_more"])
        self.assertLess(result_size(first), 100000)
        self.assertNotIn("summary", first["outcomes"][0])
        self.assertEqual(first["outcomes"][0]["result_ref"]["arguments"]["view"], "result")
        self.assertFalse(self.store.db.execute("SELECT 1 FROM turn_results").fetchone())
        self.assertEqual(first["cursor"], 0)  # Outcome observation never consumes journal replay.
        with self.assertRaisesRegex(BridgeError, "task_ids: array length outside bounds"):
            await self.outcome(task_ids=ids + ["another"])

    async def test_deleted_task_wakes_batch_without_destroying_other_cursors(self):
        tasks = self.tasks(2)
        ids = [t["id"] for t in tasks]
        initial = await self.outcome(task_ids=ids)
        running = await self.waiting({"task_ids": ids, "cursors": initial["cursors"]})
        self.service.lifecycle.archive(ids[1], "delete")
        changed = await asyncio.wait_for(running, 5)
        self.assertEqual([t["id"] for t in changed["outcomes"]], [ids[1]])
        self.assertTrue(changed["outcomes"][0]["deleted"])
        self.assertEqual(changed["cursors"][ids[0]], initial["cursors"][ids[0]])

    async def test_no_history_decodes_or_native_calls_as_history_grows(self):
        tasks = self.tasks(1000)
        identity = tasks[0]["id"]
        with self.store.transaction():
            self.store.db.executemany("INSERT INTO events(task_id,kind,at,data) VALUES (?,'old',1,?)",
                ((identity, json.dumps({"data": "x" * 2048})) for _ in range(1000)))
        with patch.object(self.service.backend, "call", AsyncMock(side_effect=AssertionError("No native reads"))), \
                patch.object(self.store, "_decode", wraps=self.store._decode) as decode, \
                patch.object(self.store, "all", side_effect=AssertionError("No full scans")):
            result = await self.outcome(task_ids=[identity])
        self.assertEqual([c.args[0] for c in decode.call_args_list], ["projects"])
        self.assertEqual(len(result["outcomes"]), 1)
        self.assertEqual(result["events"], [])

    async def test_family_changes_and_pending_decisions_remain_discoverable(self):
        parent = self.tasks(1)[0]
        project = parent["project"]
        with self.store.transaction():
            child = self.store.save("tasks", {"id": "child", "thread_id": "native-child", "project": project,
                "parent_task_id": parent["id"], "root_task_id": parent["id"], "status": "waiting"})
            for i in range(61):
                self.store.save("requests", {"id": f"pending-{i}", "task_id": child["id"], "status": "pending",
                    "kind": "parent_question", "blocking": True, "requires_human": False, "params": {"text": "?"}})
        page = await self.outcome(task_id=parent["id"], requests_limit=17)
        seen, cursor = set(), None
        for _ in range(4):
            seen.update(r["id"] for r in page["pending_requests"])
            cursor = page["pending_requests_next_cursor"]
            if cursor:
                page = await self.outcome(task_id=parent["id"], cursors=page["cursors"],
                    requests_limit=17, requests_cursor=cursor)
        self.assertEqual(len(seen), 61)
        self.assertIsNone(cursor)
        self.assertEqual(page["suggested_wait_seconds"], 0)
        first = await self.outcome(task_id=parent["id"])
        exact = await self.outcome(task_id=parent["id"], include_descendants=False)
        self.store.update("tasks", child["id"], {"name": "changed"})
        self.assertTrue((await self.outcome(task_id=parent["id"], cursors=first["cursors"]))["outcomes"])
        self.assertFalse((await self.outcome(task_id=parent["id"], include_descendants=False, cursors=exact["cursors"]))["outcomes"])

    async def test_invalid_cursor_and_replay_arguments_fail_explicitly(self):
        tasks = self.tasks(2)
        first = await self.outcome(task_id=tasks[0]["id"])
        for values in ({"task_id": tasks[0]["id"], "cursors": {tasks[0]["id"]: "broken"}},
                       {"task_id": tasks[1]["id"], "cursors": {tasks[1]["id"]: first["cursors"][tasks[0]["id"]]}}):
            with self.subTest(values=values), self.assertRaises(BridgeError) as error:
                await self.outcome(**values)
            self.assertEqual(error.exception.code, "cursor_expired")
        for field in ("after", "event_seq", "event_offset"):
            with self.assertRaisesRegex(BridgeError, "uses cursors"):
                await self.outcome(task_id=tasks[0]["id"], **{field: 1})
        with self.assertRaisesRegex(BridgeError, "tasks_offset applies only"):
            await self.outcome(task_id=tasks[0]["id"], tasks_offset=1)

    async def test_cancellation_and_unrelated_changes_do_not_release_or_cancel_work(self):
        tasks = self.tasks(2)
        ids = [t["id"] for t in tasks]
        first = await self.outcome(task_id=ids[0])
        running = await self.waiting({"task_id": ids[0], "cursors": first["cursors"]})
        self.store.update("tasks", ids[1], {"name": "unrelated"})
        await self.service.wake()
        self.assertFalse(running.done())
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.store.update("tasks", ids[0], {"name": "after cancellation"})
        result = await self.outcome(task_id=ids[0], cursors=first["cursors"])
        self.assertEqual([t["id"] for t in result["outcomes"]], [ids[0]])

    async def test_global_task_paging_and_rollback_preserve_change_tokens(self):
        self.tasks(51)
        first = await self.outcome()
        self.assertEqual(len(first["outcomes"]), 50)
        last = await self.outcome(tasks_offset=first["tasks_next_offset"])
        self.assertEqual(len(last["outcomes"]), 1)
        identity = first["outcomes"][0]["id"]
        try:
            with self.store.transaction():
                self.store.update("tasks", identity, {"name": "rolled back"})
                raise RuntimeError("crash")
        except RuntimeError:
            pass
        result = await self.outcome(task_id=identity, cursors={identity: first["cursors"][identity]})
        self.assertEqual(result["outcomes"], [])

    async def test_one_deadline_bounds_the_whole_batch_even_after_unrelated_wakes(self):
        task = self.tasks(1)[0]
        initial = await self.outcome(task_id=task["id"])
        ticks, waits = iter((0, 0, 7, 26)), []
        async def unrelated_wakeup(coroutine, timeout):
            coroutine.close()
            waits.append(timeout)
        with patch.object(outcomes.asyncio, "wait_for", unrelated_wakeup):
            result = await outcomes.wait(self.service, task_id=task["id"], cursors=initial["cursors"],
                wait_seconds=100, _clock=lambda: next(ticks))
        self.assertEqual(waits, [25, 18])
        self.assertEqual(result["outcomes"], [])

    async def test_retention_is_explicit_without_advancing_detailed_replay(self):
        task = self.tasks(1)[0]
        seq = self.store.event(task["id"], "old", {"text": "old progress"})
        self.store.db.execute("UPDATE events SET at=1")
        initial = await self.outcome(task_id=task["id"])
        retention = Retention(self.store, clock=lambda: 10000)
        plan = retention.plan(100)
        retention.commit(plan["plan_id"])
        with self.assertRaises(BridgeError) as stale:
            await self.outcome(task_id=task["id"], cursors=initial["cursors"])
        self.assertEqual(stale.exception.code, "cursor_expired")
        result = await self.outcome(task_id=task["id"])
        self.assertEqual(result["journal_retention"]["expired_through"], seq)
        self.assertEqual(result["cursor"], 0)
        self.assertEqual(result["journal_highwater"], seq)
        with self.assertRaises(BridgeError) as error:
            await self.service.events(after=0, task_id=task["id"])
        self.assertEqual(error.exception.code, "cursor_expired")

    async def test_stream_chunks_coalesce_but_remain_in_detailed_replay(self):
        task = self.tasks(1)[0]
        first = await self.outcome(task_id=task["id"])
        seq = self.store.event(task["id"], "item/agentMessage/delta", {"turnId": task["turn_id"], "delta": "progress"})
        observed = await self.outcome(task_id=task["id"], cursors=first["cursors"])
        self.assertFalse(observed["outcomes"])
        replay = await self.service.events(after=seq - 1, task_id=task["id"])
        self.assertEqual(replay["events"][0]["data"]["delta"], "progress")

    async def test_request_cursor_is_bound_to_database_and_selected_subtree(self):
        parent = self.tasks(1)[0]
        with self.store.transaction():
            for identity, ancestor in (("child", parent["id"]), ("grandchild", "child"), ("sibling", parent["id"])):
                self.store.save("tasks", {"id": identity, "thread_id": "native-" + identity, "project": parent["project"],
                    "status": "waiting", "parent_task_id": ancestor, "root_task_id": parent["id"]})
            for i, owner in enumerate(("child", "grandchild", "sibling")):
                self.store.save("requests", {"id": f"pending-{i}", "task_id": owner, "status": "pending",
                    "kind": "parent_question", "params": {"question": "?"}})
        page = await self.outcome(task_id="child", requests_limit=1)
        self.assertEqual(page["pending_requests_total"], 2)
        cursor = page["pending_requests_next_cursor"]
        next_page = await self.outcome(task_id="child", requests_limit=1, requests_cursor=cursor)
        self.assertEqual({page["pending_requests"][0]["task_id"], next_page["pending_requests"][0]["task_id"]}, {"child", "grandchild"})
        self.store.db.execute("UPDATE supervision_state SET identity='another-database'")
        with self.assertRaises(BridgeError) as stale:
            await self.outcome(task_id="child", requests_cursor=cursor)
        self.assertEqual(stale.exception.code, "cursor_expired")

    async def test_gap_previews_cannot_truncate_scope_ids_or_cursors(self):
        task = self.tasks(1)[0]
        self.store.update("tasks", task["id"], {"evidence_complete": False, "evidence_gaps": ["long gap " * 1000] * 100})
        page = await self.outcome(task_id=task["id"])
        self.assertTrue(page["outcomes"][0]["evidence_gaps_truncated"])
        self.assertEqual(len(page["outcomes"][0]["evidence_gaps"]), 20)
        self.assertFalse((await self.outcome(task_id=task["id"], cursors=page["cursors"]))["outcomes"])

    async def test_each_sibling_claim_change_wakes_an_exact_scope_wait_once(self):
        parent = self.tasks(1)[0]
        for identity in ("child-a", "child-b"):
            self.store.save("tasks", {"id": identity, "thread_id": "native-" + identity, "project": parent["project"],
                "status": "completed", "parent_task_id": parent["id"], "root_task_id": parent["id"]})
        page = await self.outcome(task_id="child-b", include_descendants=False)
        for state in ("uncertain", "active", "active", "inactive"):
            running = await self.waiting({"task_id": "child-b", "include_descendants": False, "cursors": page["cursors"]})
            self.store.claim("child-a", "background", state, {"reason": "fixture"})
            page = await asyncio.wait_for(running, 5)
            self.assertEqual([entry["id"] for entry in page["outcomes"]], ["child-b"])
            self.assertEqual(page["outcomes"][0]["workspace_reserved"], state != "inactive")
            self.assertEqual((await self.outcome(task_id="child-b", include_descendants=False, cursors=page["cursors"]))["outcomes"], [])
        self.store.update("tasks", "child-a", {"name": "unrelated metadata"})
        self.assertEqual((await self.outcome(task_id="child-b", include_descendants=False, cursors=page["cursors"]))["outcomes"], [])

    async def test_fifty_outcomes_query_active_claims_once_without_inactive_history(self):
        tasks = self.tasks(1000)
        with self.store.transaction():
            self.store.db.executemany("INSERT INTO workspace_claims VALUES (?,?,?,?,'inactive','{}',1)",
                ((t["id"], "turn", tasks[0]["id"], t["project"]) for t in tasks))
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            page = await self.outcome(task_ids=[t["id"] for t in tasks[:50]])
        finally:
            self.store.db.set_trace_callback(None)
        queries = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "workspace_claims" in s]
        self.assertEqual(len(queries), 1)
        self.assertIn("claims_live_family", queries[0])
        self.assertTrue(all(not item["workspace_reserved"] for item in page["outcomes"]))

    async def test_pre_fingerprint_cursor_gets_one_extra_observation_then_settles(self):
        task = self.tasks(1)[0]
        page = await self.outcome(task_id=task["id"], include_descendants=False)
        identity = tuple(self.store.db.execute("SELECT identity,retention_epoch FROM supervision_state WHERE id=1").fetchone())
        seq = self.store.db.execute("SELECT own_seq FROM task_changes WHERE task_id=?", (task["id"],)).fetchone()[0]
        body = json.dumps([task["id"], False, seq, identity[1]], separators=(",", ":")).encode()
        token = "outcome:" + base64.urlsafe_b64encode(body).decode() + "." + hmac.new(identity[0].encode(), body, hashlib.sha256).hexdigest()
        changed = await self.outcome(task_id=task["id"], include_descendants=False, cursors={task["id"]: token})
        self.assertEqual(len(changed["outcomes"]), 1)
        self.assertEqual((await self.outcome(task_id=task["id"], include_descendants=False, cursors=changed["cursors"]))["outcomes"], [])

    async def test_terminal_handoff_does_not_pin_a_result_before_late_evidence(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        first = await self.outcome(task_id=task["id"])
        captured = first["outcomes"][0]["result_ref"]["captured_result_id"]
        await self.service.on_message({"method": "turn/diff/updated", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "diff": "+ late evidence"}})
        page = await self.outcome(task_id=task["id"], cursors=first["cursors"])
        ref = page["outcomes"][0]["result_ref"]
        self.assertNotIn("result_id", ref["arguments"])
        result = await dispatch(self.service, ref["tool"], ref["arguments"])
        self.assertNotEqual(result["result"]["result_id"], captured)
        self.assertIn("+ late evidence", json.dumps(result))
