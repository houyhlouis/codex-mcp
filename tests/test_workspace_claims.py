import asyncio
import json
from unittest.mock import patch

from codex_local_mcp.backend import RpcError
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store
from codex_local_mcp import store as storage
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class WorkspaceClaimsTest(ServiceHarness):
    automatic_reconciliation = True

    def kinds(self, task):
        return {c["kind"] for c in self.service.get(task["id"])["workspace_claims"]}

    async def goal(self, task, status="active", key="goal"):
        return await dispatch(self.service, "codex_goal", {"task_id": task["id"], "action": "set",
            "objective": "Continue", "status": status, "user_requested": True, "request_id": key})

    async def completed(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        return task

    async def test_active_goal_owns_the_gap_and_pause_releases_only_goal(self):
        task = await self.completed()
        await self.goal(task)
        self.assertIn("goal", self.kinds(task))
        with self.assertRaisesRegex(BridgeError, "active in this project"):
            await self.start_task("hold", key="overlap")
        await self.service.queue(task["id"], "add", "successor")
        await self.goal(task, "paused", "pause")
        self.assertNotIn("goal", self.kinds(task))
        self.assertIn("queue", self.kinds(task))
        queued = (await self.service.queue(task["id"]))["data"][0]
        await self.service.queue(task["id"], "delete", queued_id=queued["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        await self.start_task("hold", key="after-drain")

    async def test_claim_exists_before_enabling_and_survives_lost_goal_ack(self):
        task = await self.completed()
        gate = self.gate_call("goal_ack", "thread/goal/set", error=TimeoutError("Lost goal acknowledgement"))
        enabling = asyncio.create_task(self.goal(task))
        await gate.wait()
        self.assertIn("goal", self.kinds(task))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="overlap")
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await enabling
        self.assertIn("goal", self.kinds(task))
        await self.service.resume(task["id"])
        self.assertIn("goal", self.kinds(task))
        self.assertNotIn("uncertain", self.kinds(task))
        await self.service.goal(task["id"], "clear", user_requested=True)
        await self.start_task("hold", key="after-clear")

    async def test_rejected_enable_releases_claim_only_after_native_reconciliation(self):
        task = await self.completed()
        gate = self.gate_call("reject_goal", "thread/goal/set", before=True,
            error=RpcError({"code": -32602, "message": "Goal rejected"}))
        enabling = asyncio.create_task(self.goal(task))
        await gate.wait()
        self.assertIn("goal", self.kinds(task))
        gate.release.set()
        with self.assertRaises(RpcError):
            await enabling
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_restart_preserves_goal_and_reconciles_dormant_conversations(self):
        task = await self.completed()
        await self.goal(task)
        await self.service.close()
        self.service = self.new_service()
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="restart-overlap")
        await self.service.reconcile_startup()
        self.assertIn("goal", self.kinds(task))
        await self.goal(task, "blocked", "blocked")
        await self.service.resume(task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_pause_and_completion_do_not_release_background_execution(self):
        task = await self.completed()
        await self.goal(task)
        original = self.service.backend.call
        async def background(method, params, **kwargs):
            if method == "thread/backgroundTerminals/list":
                return {"data": [{"processId": "background", "command": "fixture", "itemId": "command",
                                  "cwd": task["project"]}], "nextCursor": None}
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", background):
            await self.service.resume(task["id"])
            await self.goal(task, "complete", "complete")
            self.assertEqual(self.kinds(task), {"background"})
            with self.assertRaises(BridgeError):
                await self.start_task("hold", key="background-overlap")
        await self.service.resume(task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_queue_successor_gap_and_lost_ack_keep_workspace_owned(self):
        task = await self.completed()
        gate = self.gate_call("queue_ack", "thread/queue/add", error=TimeoutError("Lost queue acknowledgement"))
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold", operation_id="queued"))
        await gate.wait()
        self.assertIn("queue", self.kinds(task))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="queue-gap")
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        await self.service.resume(task["id"])
        self.assertIn("queue", self.kinds(task))
        queued = (await self.service.queue(task["id"]))["data"][0]
        await self.service.queue(task["id"], "start", queued_id=queued["id"])
        self.assertIn("turn", self.kinds(task))
        current = self.service.get(task["id"])
        await self.service.interrupt(task["id"], current["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.service.resume(task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_concurrent_reconciliation_cannot_release_a_newer_enable(self):
        task = await self.completed()
        gate = self.gate_call("stale_goal_read", "thread/goal/get")
        reconciling = asyncio.create_task(self.service.reconcile_claims(task["id"]))
        await gate.wait()
        await self.goal(task)
        gate.release.set()
        await reconciling
        self.assertIn("goal", self.kinds(task))

    async def test_crash_before_and_after_goal_enable_acknowledgement(self):
        for before in (True, False):
            with self.subTest(before_native_enable=before):
                task = await self.start_task(path=f"crash-{before}", key=f"start-{before}")
                await self.until(task["id"], ("completed",))
                gate = self.gate_call(f"enable-{before}", "thread/goal/set", before=before)
                caller = asyncio.create_task(self.goal(task, key=f"enable-{before}"))
                await gate.wait()
                await self.service.close()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                self.service = self.new_service()
                await self.service.reconcile_startup()
                if before:
                    self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
                else:
                    self.assertIn("goal", self.kinds(task))
                    with self.assertRaises(BridgeError):
                        await self.start_task(path=f"crash-{before}", key=f"overlap-{before}")
                    await self.service.goal(task["id"], "clear", user_requested=True)
                gate.release.set()

    async def test_native_goal_successor_gap_and_pause_preserve_running_claim(self):
        task = await self.completed()
        await self.goal(task)
        gate = self.gate_callback("goal_successor_started", "turn/started")
        await self.service.backend.call("turn/start", {"threadId": task["thread_id"],
            "input": [{"type": "text", "text": "hold"}]})
        await gate.wait()
        self.assertEqual(self.service.get(task["id"])["status"], "completed")
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="successor-gap")
        gate.release.set()
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(
                lambda: self.service.get(task["id"])["status"] == "running"), 5)
        await self.goal(task, "paused", "pause-successor")
        self.assertEqual(self.kinds(task), {"turn"})
        current = self.service.get(task["id"])
        await self.service.interrupt(task["id"], current["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.service.resume(task["id"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_schema_four_unknown_goal_is_discovered_without_releasing_workspace(self):
        task = await self.completed()
        await self.goal(task)
        await self.service.close()
        saved = self.store.get("tasks", task["id"])
        self.store.close()
        (self.state / "state.sqlite3").unlink()
        with patch.object(storage, "SCHEMA_VERSION", 4):
            legacy = Store(self.state)
            try:
                with legacy.transaction():
                    legacy.db.execute("INSERT INTO tasks(id,data,revision) VALUES (?,?,?)", (task["id"], json.dumps(saved), saved["revision"]))
                    descriptor = storage.project_record(saved["project"])
                    legacy.db.execute("INSERT INTO projects(id,data,revision) VALUES (?,?,1)", (descriptor["id"], json.dumps(descriptor)))
            finally:
                legacy.close()
        self.store = Store(self.state)
        self.assertTrue(self.store.backup_path.is_file())
        self.service = self.new_service()
        self.assertIn("uncertain", self.kinds(task))
        await self.service.reconcile_startup()
        self.assertEqual(self.kinds(task), {"goal"})
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="migrated-overlap")

    async def test_pending_enable_cannot_be_released_by_an_older_goal_get(self):
        task = await self.completed()
        gate = self.gate_call("before_goal_enable", "thread/goal/set", before=True)
        enabling = asyncio.create_task(self.goal(task))
        await gate.wait()
        self.assertIsNone((await self.service.goal(task["id"]))["goal"])
        await self.service.reconcile_claims(task["id"])
        self.assertIn("goal", self.kinds(task))
        gate.release.set()
        await enabling
        self.assertIn("goal", self.kinds(task))

    async def test_unknown_native_state_retains_uncertain_ownership(self):
        task = await self.completed()
        self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "test"})
        original = self.service.backend.call
        async def unknown(method, params, **kwargs):
            if method == "thread/goal/get":
                return {}  # Missing is not proof that a native goal is absent.
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", unknown):
            with self.assertRaises(BridgeError) as failure:
                await self.service.resume(task["id"])
            self.assertEqual(failure.exception.code, "state_conflict")
            self.assertTrue(failure.exception.retryable)
        self.assertIn("uncertain", self.kinds(task))

    async def test_queue_reserves_before_its_history_lookup(self):
        task = await self.completed()
        gate = self.gate_call("queue_history", "thread/turns/list", before=True)
        adding = asyncio.create_task(self.service.queue(task["id"], "add", "hold"))
        await gate.wait()
        self.assertIn("queue", self.kinds(task))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="queue-history-overlap")
        gate.release.set()
        await adding

    async def test_restore_cannot_enable_intent_under_another_root_lease(self):
        task = await self.completed()
        await self.service.archive(task["id"])
        await self.start_task("hold", key="new-owner")
        with self.assertRaisesRegex(BridgeError, "active in this project"):
            await dispatch(self.service, "codex_archive", {"task_id": task["id"],
                "action": "unarchive", "request_id": "restore"})
        self.assertTrue(self.service.get(task["id"])["archived"])

    async def test_unrecorded_native_child_goal_is_recovered_with_the_family(self):
        parent = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(
                lambda: bool(self.service.requests(parent["id"]))), 5)
        request = self.service.requests(parent["id"])[0]
        child = self.service.task(request["task_id"])
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        # Model a broker that crashed before recording this native descendant.
        with self.store.transaction():
            for table in ("workspace_claims", "turns"):
                self.store.db.execute(f"DELETE FROM {table} WHERE task_id=?", (child["id"],))
            self.store.db.execute("DELETE FROM tasks WHERE id=?", (child["id"],))
            for request in self.store.all("requests"):
                if request["task_id"] == child["id"]:
                    self.store.db.execute("DELETE FROM requests WHERE id=?", (request["id"],))
        await self.service.backend.call("thread/goal/set", {"threadId": child["thread_id"],
            "objective": "Native child continuation", "status": "active", "origin": "user"})
        await self.service.resume(parent["id"])
        await self.reconciliation_idle()
        claims = self.service.get(parent["id"])["workspace_claims"]
        self.assertTrue(any(c["kind"] == "goal" and c["task_id"] != parent["id"] for c in claims))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="child-goal-overlap")

    async def test_turn_rpc_rejection_cannot_release_an_applied_native_turn(self):
        task = await self.completed()
        started = self.gate_callback("delayed_turn_started", "turn/started")
        acknowledgement = self.gate_call("applied_turn_error", "turn/start",
            error=RpcError({"code": -32603, "message": "Applied turn, response failed"}))
        operation = asyncio.create_task(self.service.turn_control(task, "turn/start", {
            "threadId": task["thread_id"], "input": [{"type": "text", "text": "hold"}]}))
        await started.wait()
        await acknowledgement.wait()
        acknowledgement.release.set()
        with self.assertRaises(RpcError):
            await operation
        self.assertIn("turn", self.kinds(task))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="applied-error-overlap")
        started.release.set()

    async def test_pending_turn_rpc_cannot_be_reconciled_as_dormant(self):
        task = await self.completed()
        gate = self.gate_call("pending_turn", "turn/start", before=True)
        starting = asyncio.create_task(self.service.turn_control(task, "turn/start", {
            "threadId": task["thread_id"], "input": [{"type": "text", "text": "hold"}]}))
        await gate.wait()
        await self.service.reconcile_claims(task["id"])
        self.assertIn("turn", self.kinds(task))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="pending-turn-overlap")
        gate.release.set()
        await starting

    async def test_start_ack_without_turn_identity_waits_for_explicit_recovery(self):
        task = await self.completed()
        original = self.service.backend.call
        async def acknowledgement(method, params, **kwargs):
            if method == "thread/compact/start":
                return {}  # A start ACK does not itself establish a turn identity.
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", acknowledgement):
            await self.service.compact(task["id"])
        await self.service.reconcile_claims(task["id"])
        self.assertIn("turn", self.kinds(task))
        with self.assertRaises(BridgeError):
            await self.start_task("hold", key="unidentified-turn-overlap")
        await self.service.resume(task["id"])
        self.assertEqual(self.service.get(task["id"])["status"], "idle")
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
