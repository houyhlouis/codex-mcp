"""Deterministic interleavings for task projections and uncertain native calls."""

import asyncio

from codex_local_mcp.backend import RpcError
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.helpers import FaultGate
from tests.test_service import ServiceHarness


class StabilizationTest(ServiceHarness):
    async def notify(self, task, method, **params):
        await self.service.on_message({"method": method, "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], **params}})

    async def complete(self, task):
        await self.notify(task, "item/completed", item={"id": "cmd", "type": "commandExecution", "exitCode": 0,
            "command": "printf offline", "cwd": task["project"], "status": "completed"})
        await self.notify(task, "item/completed", item={"id": "patch", "type": "fileChange",
            "changes": [{"path": "new.py", "kind": {"type": "update"}, "diff": "+ new"}]})
        await self.notify(task, "turn/diff/updated", diff="+ new")
        await self.notify(task, "item/completed", item={"id": "summary", "type": "agentMessage", "text": "Done"})
        await self.notify(task, "turn/completed", turn={"id": task["turn_id"], "status": "completed"})

    async def test_rename_event_lost_update(self):
        task = await self.start_task("hold")
        gate = self.gate_call("rename_ack", "thread/name/set")
        rename = asyncio.create_task(self.service.rename(task["id"], "new-name"))
        await gate.wait()
        await self.complete(task)
        gate.release.set()
        renamed = await rename
        self.assertEqual(renamed["name"], "new-name")
        self.assertEqual(renamed["status"], "completed")
        self.assertEqual(renamed["summary"], "Done")
        self.assertEqual(renamed["files"], ["new.py"])
        self.assertEqual(renamed["commands"][0]["exitCode"], 0)
        self.assertEqual(renamed["diff"], "+ new")
        self.assertIn("turn/completed", [e["kind"] for e in self.store.events(0, 200)["events"]])

    async def test_rename_preserves_approval_and_progress(self):
        task = await self.start_task("hold")
        gate = self.gate_call("rename_ack", "thread/name/set")
        rename = asyncio.create_task(self.service.rename(task["id"], "new-name"))
        await gate.wait()
        await self.notify(task, "turn/plan/updated", plan=[{"step": "New progress", "status": "inProgress"}])
        await self.service.on_message({"id": 9980, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "approval"}})
        gate.release.set()
        renamed = await rename
        self.assertEqual(renamed["status"], "waiting")
        self.assertEqual(renamed["plan"], [{"step": "New progress", "status": "inProgress"}])
        self.assertEqual(len(renamed["pending_requests"]), 1)

    async def test_followup_stale_diff(self):
        task = await self.start_task()
        await self.until(task["id"])
        previous = await self.service.diff(task["id"])
        await self.service.send(task["id"], "hold")
        current = await self.service.diff(task["id"])
        self.assertNotEqual(previous["turn_id"], current["turn_id"])
        self.assertEqual(current["diff"], "")
        self.assertEqual(current["files"], [])

    async def test_followup_stale_current_settings(self):
        task = await self.start_task("hold")
        await self.service.settings(task["id"], "current", task["turn_id"], approval_mode="approve_for_me")
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.service.send(task["id"], "hold")
        settings = await self.service.settings(task["id"])
        self.assertEqual(settings["approval_mode"], "ask")
        self.assertFalse(settings.get("active_settings"))

    async def test_failed_queue_add_ghost_reservation(self):
        task = await self.start_task()
        previous = await self.until(task["id"])
        gate = self.gate_call("queue_rejection", "thread/queue/add", before=True,
                              error=RpcError({"code": -32602, "message": "Confirmed rejection"}))
        adding = asyncio.create_task(dispatch(self.service, "codex_queue", {
            "task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "queue-reject"}))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(RpcError):
            await adding
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], previous["status"])
        self.assertEqual(current["turn_id"], previous["turn_id"])
        self.assertEqual(current["summary"], previous["summary"])
        self.assertEqual((await self.service.queue(task["id"]))["data"], [])
        self.assertEqual((await self.service.resume(task["id"]))["status"], previous["status"])
        with self.assertRaisesRegex(BridgeError, "Previous operation"):
            await dispatch(self.service, "codex_queue", {
                "task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "queue-reject"})

    async def test_completion_before_start_ack(self):
        gate = self.gate_call("start_ack", "turn/start")
        starting = asyncio.create_task(self.start_task())
        await gate.wait()
        task = self.store.all("tasks")[0]
        done = await self.until(task["id"], ("completed",))
        gate.release.set()
        result = await starting
        for key in ("status", "summary", "commands", "files", "diff", "plan"):
            self.assertEqual(result[key], done[key])

    async def test_lost_queue_ack_keeps_reservation(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("lost_queue_ack", "thread/queue/add", error=TimeoutError("Dropped ACK"))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="lost"))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        self.assertEqual(self.service.get(task["id"])["status"], "queued")
        self.assertTrue(self.service.get(task["id"])["queue_uncertain"])
        resumed = await self.service.resume(task["id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(len((await self.service.queue(task["id"]))["data"]), 1)

    async def test_callback_crash_marks_event_gap(self):
        gate = self.gate_callback("callback_crash", "item/completed", error=RuntimeError("Injected callback crash"))
        task = await self.start_task()
        await gate.wait()
        gate.release.set()
        await self.until(task["id"], ("needs_resume",))
        gaps = [e for e in self.store.events(0, 200)["events"] if e["kind"] == "bridge/disconnected"]
        self.assertTrue(gaps[-1]["data"]["event_gap_possible"])

    async def test_native_turn_starts_clear_local_fields_once_and_keep_cumulative_usage(self):
        task = await self.start_task("hold")
        await self.complete(task)
        with self.store.db:
            self.store.update("tasks", task["id"], {"active_settings": {"approval_mode": "approve_for_me", "turn_id": task["turn_id"]},
                "last_automatic_review": {"old": True}, "error": {"message": "old failure"}})
        await self.notify(task, "thread/tokenUsage/updated", tokenUsage={"total": {"totalTokens": 123}})
        for origin in ("queue", "goal", "review", "compact", "native"):
            with self.subTest(origin=origin):
                task = {**task, "turn_id": origin + "-turn"}
                await self.notify(task, "turn/started", turn={"id": task["turn_id"], "status": "inProgress"})
                current = self.service.get(task["id"])
                for key, empty in (("summary", ""), ("files", []), ("commands", []), ("plan", []), ("diff", ""),
                                   ("error", None), ("active_settings", {}), ("last_automatic_review", None)):
                    self.assertEqual(current[key], empty, key)
                self.assertEqual(current["usage"], {"total": {"totalTokens": 123}})
                self.assertEqual(current["usage_scope"], "cumulative")
                await self.complete(task)
                await self.notify(task, "turn/started", turn={"id": task["turn_id"], "status": "inProgress"})
                await self.service.ack_turn(task["id"], {"turn": {"id": task["turn_id"]}})
                self.assertEqual(self.service.get(task["id"])["status"], "completed")
                self.assertEqual((await self.service.diff(task["id"]))["diff_turn_id"], task["turn_id"])
                self.assertEqual(self.service.get(task["id"])["diff"], "+ new")

    async def test_late_ack_does_not_replace_a_newer_turn(self):
        task = await self.start_task("hold")
        original = task["turn_id"]
        task = {**task, "turn_id": "native-next"}
        await self.notify(task, "turn/started", turn={"id": task["turn_id"], "status": "inProgress"})
        await self.notify(task, "turn/diff/updated", diff="+ next")
        await self.service.ack_turn(task["id"], {"turn": {"id": original}})
        self.assertEqual(self.service.get(task["id"])["turn_id"], "native-next")
        self.assertEqual(self.service.get(task["id"])["diff"], "+ next")

    async def test_current_setting_ack_does_not_attach_to_next_turn(self):
        task = await self.start_task("hold")
        gate = self.gate_call("current_settings_ack", "turn/settings/update")
        setting = asyncio.create_task(self.service.settings(task["id"], "current", task["turn_id"], approval_mode="approve_for_me"))
        await gate.wait()
        next_turn = {**task, "turn_id": "native-next"}
        await self.notify(next_turn, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        gate.release.set()
        self.assertFalse((await setting)["active_settings"])

    async def test_next_setting_ack_preserves_completion(self):
        task = await self.start_task("hold")
        gate = self.gate_call("settings_ack", "thread/settings/update")
        setting = asyncio.create_task(self.service.settings(task["id"], "next", approval_mode="approve_for_me"))
        await gate.wait()
        await self.complete(task)
        gate.release.set()
        result = await setting
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["diff"], "+ new")
        self.assertEqual(result["approval_mode"], "approve_for_me")

    async def test_interrupt_ack_for_previous_turn_preserves_next_turn(self):
        task = await self.start_task("hold")
        gate = self.gate_call("interrupt_ack", "turn/interrupt")
        interrupting = asyncio.create_task(self.service.interrupt(task["id"], task["turn_id"]))
        await gate.wait()
        await self.until(task["id"], ("interrupted",))
        await self.notify({**task, "turn_id": "native-next"}, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        gate.release.set()
        self.assertEqual((await interrupting)["status"], "running")

    async def test_queue_consumed_before_add_ack_preserves_completion(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("queue_ack", "thread/queue/add")
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "consumed", operation_id="consumed"))
        await gate.wait()
        await self.service.backend.call("thread/queue/start", {"threadId": task["thread_id"]})
        done = await self.until(task["id"], ("completed",))
        gate.release.set()
        await adding
        current = self.service.get(task["id"])
        for key in ("status", "turn_id", "diff", "commands", "files", "summary"):
            self.assertEqual(current[key], done[key])
        self.assertFalse(current.get("queue_reservation"))

    async def test_rejected_queue_add_does_not_rollback_newer_completion(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("queue_rejection", "thread/queue/add", before=True,
                              error=RpcError({"code": -32602, "message": "Rejected"}))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="rejected"))
        await gate.wait()
        next_task = {**task, "turn_id": "native-next"}
        await self.notify(next_task, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        await self.complete(next_task)
        gate.release.set()
        with self.assertRaises(RpcError):
            await adding
        current = self.service.get(task["id"])
        self.assertEqual(current["turn_id"], "native-next")
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["summary"], "Done")

    async def test_resume_reconciles_unknown_empty_queue_without_replaying(self):
        task = await self.start_task()
        previous = await self.until(task["id"])
        gate = self.gate_call("unknown_queue_ack", "thread/queue/add", before=True, error=TimeoutError("Unknown outcome"))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="unknown"))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        self.assertEqual(self.service.get(task["id"])["status"], "queued")
        self.assertTrue(self.service.get(task["id"])["queue_uncertain"])
        self.assertEqual((await self.service.resume(task["id"]))["status"], previous["status"])
        self.assertEqual(len((await self.service.history(task["id"]))["data"]), 1)

    async def test_queue_reconciliation_does_not_replace_progress_during_history_read(self):
        task = await self.start_task()
        await self.until(task["id"])
        await self.service.queue(task["id"], "add", "hold")
        gate = self.gate_call("queue_history_ack", "thread/turns/list")
        syncing = asyncio.create_task(self.service.sync_queue(task["id"], reconcile=True))
        await gate.wait()
        next_task = {**task, "turn_id": "native-next"}
        await self.notify(next_task, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        await self.complete(next_task)
        gate.release.set()
        await syncing
        self.assertEqual(self.service.get(task["id"])["status"], "completed")
        self.assertEqual(self.service.get(task["id"])["diff"], "+ new")

    async def test_turn_control_rejection_rolls_back_only_owned_fields(self):
        task = await self.start_task()
        previous = await self.until(task["id"])
        gate = self.gate_call("review_rejection", "review/start", before=True,
                              error=RpcError({"code": -32602, "message": "Rejected"}))
        reviewing = asyncio.create_task(self.service.review(task["id"]))
        await gate.wait()
        await self.service.rename(task["id"], "new-name")
        await self.notify(previous, "turn/diff/updated", diff="+ late progress")
        gate.release.set()
        with self.assertRaises(RpcError):
            await reviewing
        current = self.service.get(task["id"])
        self.assertEqual(current["name"], "new-name")
        self.assertEqual(current["diff"], "+ late progress")
        self.assertEqual(current["status"], "completed")

    async def test_resume_history_ack_preserves_newer_completion(self):
        task = await self.start_task("hold")
        self.service.loaded.discard(task["id"])
        with self.store.db:
            self.store.update("tasks", task["id"], {"status": "uncertain"})
        gate = self.gate_call("resume_history_ack", "thread/turns/list")
        resuming = asyncio.create_task(self.service.resume(task["id"]))
        await gate.wait()
        await self.complete(task)
        gate.release.set()
        result = await resuming
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "Done")
        self.assertEqual(result["diff"], "+ new")

    async def test_review_and_compact_completion_before_ack(self):
        task = await self.start_task()
        await self.until(task["id"])
        for method, control in (("review/start", self.service.review), ("thread/compact/start", self.service.compact)):
            with self.subTest(method=method):
                gate = self.gate_call(method + "_ack", method)
                controlling = asyncio.create_task(control(task["id"]))
                await gate.wait()
                done = await self.until(task["id"], ("completed",))
                gate.release.set()
                result = await controlling
                self.assertEqual(result["turn_id"], done["turn_id"])
                self.assertEqual(result["files"], done["files"])
                self.assertEqual(result["diff"], done["diff"])

    async def test_early_turn_events_survive_started_and_ack(self):
        task = await self.start_task("hold")
        next_task = {**task, "turn_id": "early-next"}
        await self.notify(next_task, "thread/tokenUsage/updated", tokenUsage={"total": {"totalTokens": 250}})
        await self.notify(next_task, "turn/diff/updated", diff="+ early")
        await self.notify(next_task, "item/completed", item={"id": "cmd", "type": "commandExecution", "exitCode": 0,
            "command": "printf offline", "cwd": next_task["project"], "status": "completed"})
        await self.notify(next_task, "turn/started", turn={"id": "early-next", "status": "inProgress"})
        await self.service.ack_turn(task["id"], {"turn": {"id": "early-next"}})
        current = self.service.get(task["id"])
        self.assertEqual(current["diff"], "+ early")
        self.assertEqual(len(current["commands"]), 1)
        self.assertEqual(current["usage"], {"total": {"totalTokens": 250}})

    async def test_native_turn_during_model_lookup_is_not_overwritten_by_followup(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("model_lookup_ack", "model/list")
        sending = asyncio.create_task(self.service.send(task["id"], "hold"))
        await gate.wait()
        next_task = {**task, "turn_id": "native-next"}
        await self.notify(next_task, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        await self.notify(next_task, "turn/diff/updated", diff="+ native")
        gate.release.set()
        with self.assertRaisesRegex(BridgeError, "became active"):
            await sending
        self.assertEqual(self.service.get(task["id"])["turn_id"], "native-next")
        self.assertEqual(self.service.get(task["id"])["diff"], "+ native")

    async def test_accepted_queue_add_reported_as_error_keeps_ownership(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("queue_error_after_side_effect", "thread/queue/add",
            error=RpcError({"code": -32603, "message": "Failure after side effect"}))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="applied"))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(RpcError):
            await adding
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["queue_reservation"]["outcome"], "accepted")
        self.assertEqual(len((await self.service.queue(task["id"]))["data"]), 1)

    async def test_unarchive_ack_preserves_new_turn_progress(self):
        task = await self.start_task()
        await self.until(task["id"])
        await self.service.archive(task["id"])
        gate = self.gate_call("unarchive_ack", "thread/unarchive")
        restoring = asyncio.create_task(self.service.archive(task["id"], "unarchive"))
        await gate.wait()
        next_task = {**task, "turn_id": "native-next"}
        await self.notify(next_task, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        await self.notify(next_task, "turn/diff/updated", diff="+ progress")
        gate.release.set()
        result = await restoring
        self.assertFalse(result["archived"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["diff"], "+ progress")

    async def test_revert_ack_does_not_erase_new_turn_evidence(self):
        task = await self.start_task()
        task = await self.until(task["id"])
        gate = self.gate_call("revert_ack", "thread/revert")
        reverting = asyncio.create_task(self.service.revert(task["id"], task["turn_id"], True))
        await gate.wait()
        next_task = {**task, "turn_id": "native-next"}
        await self.notify(next_task, "turn/started", turn={"id": "native-next", "status": "inProgress"})
        await self.complete(next_task)
        gate.release.set()
        await reverting
        result = self.service.get(task["id"])
        self.assertEqual(result["turn_id"], "native-next")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["diff"], "+ new")

    async def test_response_ack_preserves_completion_and_request_expiry(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        original = self.service.backend.respond
        gate = self.gates["response_ack"] = FaultGate("response_ack")
        async def delayed(*args, **kwargs):
            await original(*args, **kwargs)
            await gate.hit()
        self.service.backend.respond = delayed
        responding = asyncio.create_task(self.service.respond(req["id"], {"decision": "accept"}, human=True))
        await gate.wait()
        done = await self.until(task["id"], ("completed",))
        gate.release.set()
        await responding
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["diff"], done["diff"])
        self.assertIn(self.store.get("requests", req["id"])["status"], ("expired", "resolved"))

    async def test_child_adoption_history_ack_preserves_newer_completion(self):
        parent = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        req = self.service.requests(parent["id"])[0]
        child = self.service.get(req["task_id"])
        self.service.loaded.discard(child["id"])
        native = (await self.service.backend.call("thread/read", {"threadId": child["thread_id"], "includeTurns": False}))["thread"]
        gate = self.gate_call("child_history_ack", "thread/turns/list")
        adopting = asyncio.create_task(self.service.adopt_thread(native, set()))
        await gate.wait()
        await self.complete(child)
        gate.release.set()
        result = await adopting
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "Done")
        self.assertEqual(result["diff"], "+ new")

    async def test_lost_queue_ack_cannot_be_replayed_with_same_operation_key(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("lost_queue_ack", "thread/queue/add", error=TimeoutError("Dropped ACK"))
        args = {"task_id": task["id"], "action": "add", "prompt": "hold", "request_id": "lost-queue"}
        adding = asyncio.create_task(dispatch(self.service, "codex_queue", args))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        with self.assertRaisesRegex(BridgeError, "Previous operation.*uncertain"):
            await dispatch(self.service, "codex_queue", args)
        self.assertEqual(len((await self.service.queue(task["id"]))["data"]), 1)

    async def test_active_turn_completion_during_lost_queue_ack_keeps_ownership(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        gate = self.gate_call("active_queue_ack", "thread/queue/add", error=TimeoutError("Dropped ACK"))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="active-queued"))
        await gate.wait()
        await self.service.respond(req["id"], {"decision": "accept"}, human=True)
        await self.until(task["id"], ("queued",))
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        current = self.service.get(task["id"])
        self.assertEqual(current["status"], "queued")
        self.assertTrue(current["queue_uncertain"])
        with self.assertRaisesRegex(BridgeError, "active in this project"):
            await self.start_task("hold", key="overlap")
        self.assertEqual((await self.service.resume(task["id"]))["status"], "queued")
        self.assertEqual(len((await self.service.queue(task["id"]))["data"]), 1)

    async def test_duplicate_completion_during_lost_queue_ack_keeps_ownership(self):
        task = await self.start_task()
        task = await self.until(task["id"])
        gate = self.gate_call("terminal_queue_ack", "thread/queue/add", error=TimeoutError("Dropped ACK"))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="terminal-queued"))
        await gate.wait()
        await self.notify(task, "turn/completed", turn={"id": task["turn_id"], "status": "completed"})
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        self.assertEqual(self.service.get(task["id"])["status"], "queued")
        self.assertTrue(self.service.get(task["id"])["queue_uncertain"])

    async def test_rejection_with_missing_native_history_keeps_uncertain_reservation(self):
        task = await self.start_task()
        await self.until(task["id"])
        gate = self.gate_call("queue_rejection", "thread/queue/add", before=True,
            error=RpcError({"code": -32602, "message": "Rejected"}))
        original = self.service.backend.call
        async def missing_history(method, params, **kwargs):
            if method == "thread/turns/list" and gate.used:
                return {"data": [], "nextCursor": None}
            return await original(method, params, **kwargs)
        self.service.backend.call = missing_history
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="missing-history"))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(RpcError):
            await adding
        self.assertEqual(self.service.get(task["id"])["status"], "queued")
        self.assertTrue(self.service.get(task["id"])["queue_uncertain"])
        with self.assertRaises(BridgeError) as failure:
            await self.service.resume(task["id"])
        self.assertEqual(failure.exception.code, "state_conflict")
        self.assertTrue(failure.exception.retryable)
        self.assertEqual(self.service.get(task["id"])["status"], "queued")
        self.assertTrue(self.service.get(task["id"])["queue_uncertain"])
        self.service.backend.call = original
        self.assertEqual((await self.service.resume(task["id"]))["status"], "completed")
