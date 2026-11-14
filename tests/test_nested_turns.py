"""Nested native work shares a root identity without supplying its terminal fact."""

import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import native_turns
from codex_local_mcp.contract import native_message_error
from codex_local_mcp.security import BridgeError
from codex_local_mcp.security import private_file
from codex_local_mcp.store import Store, SCHEMA_V1
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class NestedTurnTest(ServiceHarness):
    def last_event(self, task_id, method, after=0):
        row = self.store.db.execute("SELECT data FROM events WHERE task_id=? AND kind=? AND seq>? ORDER BY seq DESC LIMIT 1",
            (task_id, method, after)).fetchone()
        return json.loads(row[0]) if row else None

    async def event_after(self, task_id, method, after=0):
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: self.last_event(task_id, method, after)), 5)
        return self.last_event(task_id, method, after)

    async def idle_task(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        return self.service.get(task["id"])

    async def check_review(self, *, early=False):
        task = await self.idle_task()
        (Path(task["project"]) / "sample.py").write_text("# captured review file\n", encoding="utf-8")
        self.service.automatic_reconciliation = True
        self.service.resources.set_budget(task["id"], {"max_turns": 2})
        after = self.store.db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        gate = self.gate_call("review-ack", "review/start") if early else None
        reviewing = asyncio.create_task(dispatch(self.service, "codex_review", {"task_id": task["id"],
            "target": "custom", "instructions": "review_before_ack" if early else "Review changes", "request_id": "review"}))
        try:
            if gate:
                await gate.wait()
            completed = await self.event_after(task["id"], "turn/completed", after)
            root_id = completed["turn"]["id"]
            started = self.last_event(task["id"], "turn/started", after)["turn"]
            self.assertNotEqual(started["id"], root_id)
            self.assertEqual(started["rootTurnId"], root_id)
            current = self.service.get(task["id"])
            self.assertEqual((current["turn_id"], current["turn_status"], current["status"]), (root_id, "completed", "completed"))
        finally:
            if gate:
                gate.release.set()
            await asyncio.wait_for(reviewing, 5)
        current = self.service.get(task["id"])
        self.assertEqual(current["turn_id"], root_id)
        self.assertEqual(current["family_usage"]["turns"], 2)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM turns WHERE task_id=? AND turn_id=?", (task["id"], started["id"])).fetchone())
        result = self.service.get(task["id"], view="result")["result"]
        self.assertEqual(result["turn_id"], root_id)
        self.assertTrue(result["observed_commands"])
        self.assertTrue(result["reported_changes"])
        self.assertTrue(result["artifacts"])
        for entry in [*result["observed_commands"], *result["reported_changes"], *result["artifacts"]]:
            self.assertEqual(entry["source"]["turn_id"], root_id)
        details = json.loads(self.store.db.execute("SELECT data FROM operation_details WHERE id='review'").fetchone()[0])
        self.assertEqual(details["acknowledged_turn"]["turn_id"], root_id)
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_review", {"task_id": task["id"], "request_id": "over-budget"})
        self.assertEqual(error.exception.code, "budget_exceeded")
        another = await self.start_task(key="independent")
        self.assertNotEqual(another["id"], task["id"])

    async def test_inline_review_completes_on_root_and_counts_once(self):
        await self.check_review()

    async def test_root_completion_before_review_ack_preserves_terminal_result(self):
        await self.check_review(early=True)

    async def test_idle_fork_review_completes_and_releases_its_family(self):
        source = await self.idle_task()
        fork = await dispatch(self.service, "codex_fork", {"task_id": source["id"], "request_id": "fork"})
        self.service.automatic_reconciliation = True
        after = self.store.db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        await dispatch(self.service, "codex_review", {"task_id": fork["id"], "request_id": "fork-review"})
        completed = await self.event_after(fork["id"], "turn/completed", after)
        current = self.service.get(fork["id"])
        self.assertEqual((current["turn_id"], current["status"]), (completed["turn"]["id"], "completed"))
        self.assertEqual(current["family_usage"]["turns"], 1)
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(fork["id"])["workspace_reserved"])

    async def test_nested_terminal_does_not_complete_root_or_expire_approval(self):
        task = await self.start_task("approval")
        await self.until(task["id"], ("waiting",))
        request = self.service.requests(task["id"])[0]
        for method, status in (("turn/started", "inProgress"), ("turn/completed", "completed")):
            await self.service.on_message({"method": method, "params": {"threadId": task["thread_id"],
                "turn": {"id": "nested-review", "rootTurnId": task["turn_id"], "status": status, "items": []}}})
        current = self.service.get(task["id"])
        self.assertEqual((current["turn_id"], current["status"], current["turn_status"]), (task["turn_id"], "waiting", "inProgress"))
        self.assertEqual(self.service.requests(task["id"])[0]["id"], request["id"])
        self.assertEqual(current["family_usage"]["turns"], 1)
        self.assertEqual(self.last_event(task["id"], "turn/completed")["turn"]["id"], "nested-review")
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(task["id"], ("completed",))

    async def test_late_nested_events_cannot_replace_a_newer_root(self):
        previous = await self.idle_task()
        current = await dispatch(self.service, "codex_send", {"task_id": previous["id"], "prompt": "hold", "request_id": "newer-root"})
        for method, status in (("turn/started", "inProgress"), ("turn/completed", "completed")):
            await self.service.on_message({"method": method, "params": {"threadId": current["thread_id"],
                "turn": {"id": "late-nested", "rootTurnId": previous["turn_id"], "status": status, "items": []}}})
        shown = self.service.get(current["id"])
        self.assertEqual((shown["turn_id"], shown["status"], shown["turn_status"]), (current["turn_id"], "running", "inProgress"))
        self.assertEqual(shown["family_usage"]["turns"], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns WHERE task_id=?", (current["id"],)).fetchone()[0], 2)

    async def test_plan_review_binds_root_and_succeeds(self):
        project = self.service.projects("create", str(self.root / "planned"))
        spec = {"version": 1, "name": "Nested review", "nodes": [
            {"id": "edit", "action": "start", "target": {"project_id": project["id"]}, "prompt": "echo"},
            {"id": "review", "action": "review", "target": {"node": "edit"}, "after": ["edit"], "prompt": "Review changes"}]}
        plan = await dispatch(self.service, "codex_plans", {"action": "submit", "spec": spec, "request_id": "plan"})
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.store.db.execute(
                "SELECT 1 FROM events WHERE kind='turn/completed' AND json_extract(data,'$.turn.rootTurnId') IS NOT NULL").fetchone())), 10)
        await asyncio.wait_for(self.service.scheduler.idle(), 5)
        final = self.service.scheduler.inspect(plan["plan_id"])
        self.assertTrue(final["done"], final)
        self.assertEqual(final["outcome"], "succeeded")
        node = final["nodes"][1]
        completed = self.last_event(node["task_id"], "turn/completed")["turn"]
        self.assertEqual(node["turn_id"], completed["id"])
        self.assertEqual(node["result_ref"]["turn_id"], completed["id"])
        self.assertEqual(self.service.get(node["task_id"])["family_usage"]["turns"], 2)

    async def test_nested_ack_is_bound_to_root_without_an_extra_turn(self):
        task = await self.idle_task()
        _, token = self.service.reserve_turn(task["id"])
        self.service.begin_turn(task["id"], "root-ack", source="event")
        await self.service.ack_turn(task["id"], {"turn": {"id": "nested-ack", "rootTurnId": "root-ack",
            "status": "inProgress", "items": []}}, token=token)
        current = self.service.get(task["id"])
        self.assertEqual(current["turn_id"], "root-ack")
        self.assertIsNone(self.service.lifecycle.turn(task["id"], "nested-ack"))

    async def test_restart_during_nested_review_recovers_root_without_recounting(self):
        task = await self.idle_task()
        gate = self.gate_callback("review-running", "item/reasoning/textDelta")
        await dispatch(self.service, "codex_review", {"task_id": task["id"], "target": "custom",
            "instructions": "hold", "request_id": "held-review"})
        await gate.wait()
        started = self.last_event(task["id"], "turn/started")["turn"]
        gate.release.set()
        await self.service.close()
        self.service = self.new_service()
        resumed = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "request_id": "resume-review"})
        self.assertEqual((resumed["turn_id"], resumed["turn_status"], resumed["status"]), (started["rootTurnId"], "interrupted", "idle"))
        self.assertFalse(resumed["workspace_reserved"])
        self.assertEqual(resumed["family_usage"]["turns"], 2)
        self.assertEqual(resumed["reconciliation"]["native"]["latest_turn"]["id"], started["rootTurnId"])

    async def test_nested_only_terminal_history_cannot_release_root(self):
        task = await self.start_task("hold")
        original = self.service.backend.call
        async def nested_only(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "thread/turns/list":
                return {"data": [{"id": "nested-only", "rootTurnId": task["turn_id"], "status": "completed", "items": []}], "nextCursor": None}
            if method == "thread/read":
                reply["thread"]["status"] = {"type": "idle"}
            return reply
        with patch.object(self.service.backend, "call", nested_only):
            await self.service.reconcile_claims(task["id"], explicit=True)
        current = self.service.get(task["id"])
        self.assertTrue(current["workspace_reserved"])
        self.assertEqual(current["turn_id"], task["turn_id"])
        self.assertEqual(current["turn_status"], "inProgress")

    async def reviewed_task(self):
        task = await self.idle_task()
        after = self.store.highwater()
        await dispatch(self.service, "codex_review", {"task_id": task["id"], "request_id": "root-proof-review"})
        await self.event_after(task["id"], "turn/completed", after)
        return self.service.get(task["id"]), self.last_event(task["id"], "turn/started")["turn"]

    def only_nested_history(self, task, nested):
        original = self.service.backend.call
        async def history(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "thread/turns/list" and params["threadId"] == task["thread_id"]:
                return {"data": [{**nested, "status": "completed", "items": []}], "nextCursor": None}
            return reply
        return history

    async def test_journal_root_completion_releases_when_history_only_has_nested_row(self):
        task, nested = await self.reviewed_task()
        self.assertTrue(task["workspace_reserved"])
        with patch.object(self.service.backend, "call", self.only_nested_history(task, nested)):
            observed = await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(observed["latest_turn"]["id"], task["turn_id"])
        self.assertEqual(observed["latest_turn"]["source"], "journal")
        event = self.store.db.execute("SELECT kind,data FROM events WHERE seq=?", (observed["latest_turn"]["event_seq"],)).fetchone()
        self.assertEqual(event["kind"], "turn/completed")
        self.assertEqual(json.loads(event["data"])["turn"]["id"], task["turn_id"])

    async def test_root_completion_before_a_newer_nested_start_cannot_release(self):
        task, nested = await self.reviewed_task()
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"],
            "turn": {**nested, "id": "newer-nested-start"}}})
        with patch.object(self.service.backend, "call", self.only_nested_history(task, nested)):
            await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.assertNotIn(task["id"], self.service.native_observations)

    async def test_old_root_proof_survives_restart_without_clearing_process_uncertainty(self):
        task, nested = await self.reviewed_task()
        original = self.service.backend.call
        async def tracked_terminal(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "thread/backgroundTerminals/list":
                return {"data": [{"processId": "tracked", "command": "fixture dev server", "cwd": task["project"]}], "nextCursor": None}
            return reply
        with patch.object(self.service.backend, "call", tracked_terminal):
            terminals = await self.service.terminals(task["id"])
        self.assertEqual(len(terminals["data"]), 1)
        previous, process = self.service.backend, self.service.backend.process
        await self.service.close()
        self.service = self.new_service()
        await self.service.ensure_backend()
        self.assertIsNot(previous, self.service.backend)
        self.assertIsNot(process, self.service.backend.process)
        self.assertIsNotNone(process.returncode)
        with patch.object(self.service.backend, "call", self.only_nested_history(task, nested)):
            await self.service.reconcile_claims(task["id"], explicit=True)
            current = self.service.get(task["id"])
            self.assertEqual(current["turn_status"], "completed")
            self.assertTrue(current["workspace_reserved"])
            self.assertFalse(current["process_state"]["acknowledged"])
            self.assertTrue(any(c["data"].get("reason") == "processes_unverified" for c in current["workspace_claims"]))
            resumed = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "request_id": "acknowledge-old-processes"})
        self.assertTrue(resumed["process_state"]["acknowledged"])
        self.assertFalse(resumed["workspace_reserved"])
        self.assertEqual(resumed["turn_status"], "completed")
        self.assertEqual(resumed["reconciliation"]["native"]["latest_turn"]["source"], "journal")
        self.assertFalse(resumed["reconciliation"]["native"]["background_terminals_checked"])

    async def test_journal_fallback_cannot_cross_a_backend_change_during_history_read(self):
        task, nested = await self.reviewed_task()
        gate = self.gate_call("root-proof-generation", "thread/turns/list")
        generation = self.service.backend.generation
        with patch.object(self.service.backend, "call", self.only_nested_history(task, nested)):
            checking = asyncio.create_task(self.service.reconcile_claims(task["id"], explicit=True))
            try:
                await gate.wait()
                self.service.backend.generation = "changed-during-history-read"
            finally:
                gate.release.set()
                await asyncio.wait_for(checking, 5)
                self.service.backend.generation = generation
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.assertNotIn(task["id"], self.service.native_observations)

    async def test_journal_fallback_rechecks_revision_after_native_history_await(self):
        task, nested = await self.reviewed_task()
        gate = self.gate_call("root-proof-history", "thread/turns/list")
        with patch.object(self.service.backend, "call", self.only_nested_history(task, nested)):
            checking = asyncio.create_task(self.service.reconcile_claims(task["id"], explicit=True))
            try:
                await gate.wait()
                self.service.lifecycle.metadata(task["id"], {"name": "Newer metadata"})
            finally:
                gate.release.set()
                await asyncio.wait_for(checking, 5)
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(self.service.get(task["id"])["name"], "Newer metadata")

    async def test_history_preserves_native_ids_and_labels_the_root(self):
        task = await self.idle_task()
        original = self.service.backend.call
        async def history(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "thread/turns/list":
                reply = {"data": [{"id": "nested-history", "rootTurnId": task["turn_id"], "status": "completed", "items": []}], "nextCursor": None}
            return reply
        with patch.object(self.service.backend, "call", history):
            result = await self.service.history(task["id"])
        self.assertEqual(result["data"][0]["id"], "nested-history")
        self.assertEqual(result["data"][0]["rootTurnId"], task["turn_id"])
        self.assertEqual(result["data"][0]["logical_turn_id"], task["turn_id"])

    async def test_paged_recovery_prefers_actual_root_status_and_duration(self):
        task = await self.start_task("hold")
        original, pages = self.service.backend.call, []
        root = {"id": task["turn_id"], "rootTurnId": task["turn_id"], "status": "interrupted", "durationMs": 2000, "items": []}
        async def history(method, params, **kwargs):
            if method == "thread/turns/list":
                pages.append(params)
                return {"data": [root], "nextCursor": None} if params.get("cursor") else {"data": [
                    {"id": "nested-history", "rootTurnId": task["turn_id"], "status": "completed", "durationMs": 999000, "items": []}], "nextCursor": "root-page"}
            reply = await original(method, params, **kwargs)
            if method == "thread/read":
                reply["thread"]["status"] = {"type": "idle"}
            return reply
        with patch.object(self.service.backend, "call", history):
            resumed = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "request_id": "paged-resume"})
        self.assertEqual((resumed["turn_id"], resumed["turn_status"], resumed["status"]), (task["turn_id"], "interrupted", "idle"))
        self.assertEqual(resumed["family_usage"]["active_seconds"], 2)
        self.assertEqual(resumed["family_usage"]["turns"], 1)
        self.assertEqual(pages[1]["cursor"], "root-page")
        self.assertTrue(all(p["itemsView"] == "summary" for p in pages))

    async def test_nested_turn_start_response_does_not_consume_another_slot(self):
        original = self.service.backend.call
        async def nested_ack(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "turn/start":
                root = reply["turn"]["id"]
                reply["turn"] = {**reply["turn"], "id": "nested-response", "rootTurnId": root}
            return reply
        with patch.object(self.service.backend, "call", nested_ack):
            task = await self.start_task()
            done = await self.until(task["id"], ("completed",))
        self.assertEqual(done["turn_id"], self.last_event(task["id"], "turn/completed")["turn"]["id"])
        self.assertEqual(done["family_usage"]["turns"], 1)
        self.assertIsNone(self.service.lifecycle.turn(task["id"], "nested-response"))

    async def test_compaction_and_queue_starts_share_root_identity(self):
        for action in ("compact", "queue"):
            with self.subTest(action=action):
                task = await self.start_task(path=action, key=action)
                await self.until(task["id"], ("completed",))
                original = self.service.backend.on_message
                async def nested_start(message):
                    if message.get("method") == "turn/started" and message["params"]["threadId"] == task["thread_id"]:
                        turn = message["params"]["turn"]
                        message = {**message, "params": {**message["params"], "turn": {**turn,
                            "id": "nested-" + action, "rootTurnId": turn["id"]}}}
                    await original(message)
                after = self.store.db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
                with patch.object(self.service.backend, "on_message", nested_start):
                    if action == "compact":
                        await dispatch(self.service, "codex_compact", {"task_id": task["id"], "request_id": "compact-turn"})
                    else:
                        await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "add", "prompt": "echo", "request_id": "queued-turn"})
                        await dispatch(self.service, "codex_queue", {"task_id": task["id"], "action": "start", "request_id": "queue-start"})
                    completed = await self.event_after(task["id"], "turn/completed", after)
                current = self.service.get(task["id"])
                self.assertEqual((current["turn_id"], current["turn_status"]), (completed["turn"]["id"], "completed"))
                self.assertEqual(current["family_usage"]["turns"], 2)
                self.assertIsNone(self.service.lifecycle.turn(task["id"], "nested-" + action))

    async def test_goal_continuation_counts_one_logical_turn(self):
        task = await self.idle_task()
        await dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set", "objective": "Continue",
            "status": "active", "user_requested": True, "request_id": "goal"})
        for method, turn in (("turn/started", {"id": "goal-nested", "rootTurnId": "goal-root", "status": "inProgress"}),
                             ("turn/completed", {"id": "goal-root", "rootTurnId": "goal-root", "status": "completed"})):
            await self.service.on_message({"method": method, "params": {"threadId": task["thread_id"], "turn": {**turn, "items": []}}})
        current = self.service.get(task["id"])
        self.assertEqual((current["turn_id"], current["status"]), ("goal-root", "completed"))
        self.assertEqual(current["family_usage"]["turns"], 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns WHERE task_id=?", (task["id"],)).fetchone()[0], 2)

    async def test_nested_duration_and_historical_terminal_are_not_root_evidence(self):
        task = await self.start_task("hold")
        nested = {"id": "nested-terminal", "rootTurnId": task["turn_id"], "status": "completed", "items": [], "durationMs": 999000}
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": task["thread_id"], "turn": nested}})
        self.service.resources.complete(task["id"], task["turn_id"], nested)
        with self.store.transaction():
            self.store.evidence.hydrate_history(task["id"], task["turn_id"], "inProgress")
        self.assertEqual(self.store.db.execute("SELECT status FROM evidence_history WHERE task_id=? AND turn_id=?", (task["id"], task["turn_id"])).fetchone()[0], "inProgress")
        self.assertIsNone(self.store.db.execute("SELECT ended_at FROM resource_turns WHERE task_id=? AND turn_id=?", (task["id"], task["turn_id"])).fetchone()[0])

    async def test_revert_refuses_a_nested_native_id_without_sending_revert(self):
        task = await self.idle_task()
        original, sent = self.service.backend.call, []
        async def history(method, params, **kwargs):
            sent.append(method)
            reply = await original(method, params, **kwargs)
            if method == "thread/turns/list" and params.get("limit") == 100:
                return {"data": [{"id": "nested-boundary", "rootTurnId": task["turn_id"], "status": "completed", "items": []}], "nextCursor": None}
            return reply
        with patch.object(self.service.backend, "call", history), self.assertRaises(BridgeError) as error:
            await self.service.revert(task["id"], "nested-boundary", True)
        self.assertEqual(error.exception.code, "invalid_arguments")
        self.assertNotIn("thread/revert", sent)

    async def test_recovery_repairs_only_a_journal_proven_old_nested_projection(self):
        task = await self.idle_task()
        root = {"id": task["turn_id"], "status": "completed", "items": []}
        # Reconstruct the old split projection, retaining its raw journal fact.
        with self.store.transaction():
            self.store.event(task["id"], "turn/started", {"threadId": task["thread_id"], "turn": {
                "id": "old-nested", "rootTurnId": task["turn_id"], "status": "inProgress", "items": []}})
            self.service.lifecycle.begin(task["id"], "old-nested", source="event")
        count = self.store.db.execute("SELECT COUNT(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        current = self.store.get("tasks", task["id"])
        self.service.lifecycle.recover(task["id"], root, False, expected_revision=current["revision"])
        fixed = self.service.get(task["id"])
        self.assertEqual((fixed["turn_id"], fixed["turn_status"], fixed["status"]), (task["turn_id"], "completed", "idle"))
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM resource_turns WHERE task_id=? AND turn_id='old-nested'", (task["id"],)).fetchone())
        self.assertEqual(fixed["family_usage"]["turns"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0], count)
        self.service.lifecycle.begin(task["id"], "unproven-turn", source="event")
        current = self.store.get("tasks", task["id"])
        self.service.lifecycle.recover(task["id"], root, False, expected_revision=current["revision"])
        self.assertEqual(self.service.get(task["id"])["turn_id"], "unproven-turn")
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_v011_running_nested_review_recovers_then_completes_on_root(self):
        task = await self.idle_task()
        gate = self.gate_callback("legacy-review-running", "item/reasoning/textDelta")
        await dispatch(self.service, "codex_review", {"task_id": task["id"], "target": "custom", "instructions": "hold", "request_id": "legacy-review"})
        await gate.wait()
        event = self.last_event(task["id"], "turn/started")
        nested, root_id = event["turn"]["id"], event["turn"]["rootTurnId"]
        gate.release.set()
        await self.service.close()
        self.store.close()
        path = self.state / "state.sqlite3"
        path.unlink()
        os.close(private_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        db = sqlite3.connect(path)
        try:
            for statement in SCHEMA_V1:
                db.execute(statement)
            legacy = {key: value for key, value in task.items() if key not in (
                "project_id", "revision", "workspace_claims", "workspace_reserved", "process_state", "process_scan", "background_observation")}
            legacy.update(turn_id=nested, status="running")
            legacy.pop("turn_status", None)
            db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(legacy)))
            db.execute("INSERT INTO events VALUES (41,?,'turn/started',1,?)", (task["id"], json.dumps(event)))
            db.commit()
        finally:
            db.close()
        self.store = Store(self.state)
        self.service = self.new_service()
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        original = self.service.backend.call
        async def root_still_running(method, params, **kwargs):
            if method == "thread/turns/list":
                if params.get("cursor"):
                    return {"data": [{"id": root_id, "rootTurnId": root_id, "status": "inProgress", "items": []}], "nextCursor": None}
                return {"data": [{"id": nested, "rootTurnId": root_id, "status": "inProgress", "items": []}], "nextCursor": "root"}
            reply = await original(method, params, **kwargs)
            if method == "thread/read":
                reply["thread"]["status"] = {"type": "active"}
            return reply
        with patch.object(self.service.backend, "call", root_still_running):
            resumed = await dispatch(self.service, "codex_resume", {"task_id": task["id"], "request_id": "legacy-resume"})
        self.assertEqual((resumed["turn_id"], resumed["turn_status"]), (root_id, "inProgress"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns WHERE task_id=?", (task["id"],)).fetchone()[0], 1)
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": task["thread_id"],
            "turn": {"id": root_id, "rootTurnId": root_id, "status": "completed", "items": []}}})
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        self.assertEqual(self.service.get(task["id"])["status"], "completed")
        another = await self.start_task(key="after-legacy-review")
        self.assertNotEqual(another["id"], task["id"])


class NativeTurnContractTest(unittest.TestCase):
    def test_pinned_turn_root_is_optional_nullable_and_shared(self):
        schema = json.loads((Path(__file__).with_name("fixtures") / "native-schema.json").read_text(encoding="utf-8"))
        turn = schema["definitions"]["Turn"]
        self.assertNotIn("rootTurnId", turn["required"])
        self.assertEqual(set(turn["properties"]["rootTurnId"]["type"]), {"string", "null"})
        for value in ({"id": "root"}, {"id": "root", "rootTurnId": None}, {"id": "nested", "rootTurnId": "root"}):
            self.assertEqual(native_turns.logical_id(value, native_turns.Scope(known=("root",))), "root")

    def test_root_identity_validation_accepts_nested_but_rejects_malformed_scope(self):
        params = {"threadId": "thread", "turnId": "root", "turn": {"id": "nested", "rootTurnId": "root", "items": [], "status": "inProgress"}}
        self.assertIsNone(native_message_error({"method": "turn/started", "params": params}))
        for value in ("", False, [], {}):
            with self.subTest(root=value):
                invalid = {**params, "turn": {**params["turn"], "rootTurnId": value}}
                self.assertIsNotNone(native_message_error({"method": "turn/started", "params": invalid}))
        self.assertEqual(native_message_error({"method": "turn/started", "params": {**params, "turnId": "another-root"}}), "turn identities disagree")

    def test_logical_root_does_not_accept_a_missing_or_invalid_native_id(self):
        for turn in ({"rootTurnId": "root"}, {"id": "", "rootTurnId": "root"}, {"id": False, "rootTurnId": "root"}):
            with self.subTest(turn=turn), self.assertRaises(BridgeError) as error:
                native_turns.logical_id(turn)
            self.assertEqual(error.exception.code, "state_conflict")


class NativeTurnHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_root_lookup_has_a_fixed_page_bound_and_never_uses_nested_timing(self):
        calls = []
        class History:
            async def call(self, method, params):
                calls.append((method, params))
                return {"data": [{"id": "nested", "rootTurnId": "root", "status": "inProgress", "items": [],
                    "durationMs": 999, "startedAt": 1}], "nextCursor": str(len(calls))}
        turn = await native_turns.latest(History(), "thread", scope=native_turns.Scope(known=("root",)))
        self.assertEqual(turn, {"id": "root", "rootTurnId": "root", "status": "inProgress", "items": []})
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(p["itemsView"] == "summary" for _, p in calls))

    async def test_repeated_cursor_does_not_loop_or_prove_terminal_root(self):
        calls = []
        class History:
            async def call(self, method, params):
                calls.append(params)
                return {"data": [{"id": "nested", "rootTurnId": "root", "status": "completed", "items": []}], "nextCursor": "same"}
        with self.assertRaises(BridgeError) as error:
            await native_turns.latest(History(), "thread", scope=native_turns.Scope(known=("root",)))
        self.assertEqual(error.exception.code, "state_conflict")
        self.assertEqual(len(calls), 2)
