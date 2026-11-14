import asyncio
import json
from unittest.mock import AsyncMock, patch

from codex_local_mcp.backend import OptionalCapabilityUnavailable, RpcError
from codex_local_mcp.broker import response_error
from codex_local_mcp.contract import CODEX_MIN_VERSION, CODEX_TESTED_VERSIONS, MCP_VERSIONS, NATIVE_METHODS
from codex_local_mcp.mcp import VERSIONS
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import TOOLS, dispatch, error_envelope
from tests.helpers import FaultGate, ReconciliationClock
from tests.mcp_wire import RecordingMCP
from tests.native_wire import RecordingAppServer
from tests.test_service import ServiceHarness


class ServiceContractTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.backend = RecordingAppServer(self.cfg, self.service.on_message, self.service.on_exit)
        self.backend = self.service.backend
        self.rejected, self.rejection_count = {}, {}
        original = self.backend.send

        async def send(message, **kwargs):
            method = message.get("method")
            if method in self.rejected and "id" in message:
                gate = self.rejected[method]
                if gate is not None:
                    await gate.hit()
                self.rejection_count[method] = self.rejection_count.get(method, 0) + 1
                # A transport error reply, without executing the rejected RPC.
                self.backend.pending[message["id"]].set_exception(
                    RpcError({"code": -32601, "message": "Method not found"}))
                return
            await original(message, **kwargs)

        self.backend.send = send

    async def settled(self):
        await self.reconciliation_idle()

    async def idle_task(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"])
        await self.settled()
        return self.service.task(task["id"])

    def assert_optional(self, exc, method):
        self.assertIsInstance(exc, OptionalCapabilityUnavailable)
        expected = {
            "code": "optional_native_capability_unavailable", "method": method,
            "codex_version": "codex-cli 0.167.0", "message": str(exc),
            "next_step": OptionalCapabilityUnavailable.next_step}
        data = exc.as_dict()
        self.assertEqual({k: data[k] for k in expected}, expected)
        self.assertFalse(data["retryable"])
        self.assertFalse(data["uncertain"])
        self.assertTrue(data["next_steps"])
        self.assertIsInstance(data["ids"], dict)
        self.assertIn(OptionalCapabilityUnavailable.next_step, str(exc))

    async def test_status_and_capabilities_report_contract_and_observed_backlog(self):
        task = await self.idle_task()
        self.rejected["account/rateLimits/read"] = None
        with self.assertRaises(OptionalCapabilityUnavailable):
            await self.service.usage()
        self.rejected["collaborationMode/list"] = None
        for result in (await dispatch(self.service, "codex_status", {}),
                       await dispatch(self.service, "codex_capabilities", {"task_id": task["id"]})):
            self.assertEqual(result["minimum_codex_version"], CODEX_MIN_VERSION)
            self.assertEqual(result["tested_codex_versions"], list(CODEX_TESTED_VERSIONS))
            self.assertEqual(result["supported_mcp_revisions"], list(MCP_VERSIONS))
            self.assertEqual(result["compatibility"], self.backend.compatibility)
            self.assertEqual(result["compatibility"]["status"], "tested")
            self.assertIn("account/rateLimits/read", result["unavailable_optional_methods"])
            self.assertEqual(set(result["ingress_backlog"]), {"events", "bytes", "oldest_age_seconds", "dropped_events"})
        capabilities = await self.service.capabilities(task["id"])
        self.assertEqual(capabilities["native_methods"], NATIVE_METHODS)
        self.assertEqual(capabilities["collaboration_modes"]["error"]["code"], OptionalCapabilityUnavailable.code)
        self.assertIn("collaborationMode/list", capabilities["unavailable_optional_methods"])
        self.rejected["account/read"] = None
        status = await self.service.status()
        self.assertIsNone(status["signed_in"])
        self.assertEqual(status["account_error"]["method"], "account/read")

    async def test_untested_version_is_reported_exactly_by_both_tools(self):
        self.backend.reported_version = "codex-cli 0.168.0-beta.2+vendor.1"
        task = await self.idle_task()
        for result in (await self.service.status(), await self.service.capabilities(task["id"])):
            self.assertEqual(result["compatibility"]["status"], "untested")
            self.assertEqual(result["compatibility"]["version"], self.backend.reported_version)
            self.assertIn("safety acknowledgements", result["compatibility"]["reason"])

    async def test_every_optional_control_rejects_without_phantom_state(self):
        task = await self.idle_task()
        cases = [
            ("codex_queue", "thread/queue/add", {"action": "add", "prompt": "never execute"}),
            ("codex_queue", "thread/queue/start", {"action": "start"}),
            ("codex_queue", "thread/queue/update", {"action": "update", "queued_id": "missing", "prompt": "never execute"}),
            ("codex_queue", "thread/queue/delete", {"action": "delete", "queued_id": "missing"}),
            ("codex_queue", "thread/queue/reorder", {"action": "reorder", "ordered_ids": ["missing"]}),
            ("codex_queue", "thread/queue/list", {"action": "list"}),
            ("codex_goal", "thread/goal/set", {"action": "set", "objective": "Explicit fixture goal", "user_requested": True}),
            ("codex_goal", "thread/goal/set", {"action": "set", "status": "paused", "user_requested": True}),
            ("codex_goal", "thread/goal/clear", {"action": "clear", "user_requested": True}),
            ("codex_goal", "thread/goal/get", {"action": "get"}),
            ("codex_review", "review/start", {}),
            ("codex_compact", "thread/compact/start", {}),
            ("codex_fork", "thread/fork", {}),
            ("codex_settings", "thread/settings/update", {"scope": "next", "effort": "high"}),
            ("codex_terminals", "thread/backgroundTerminals/list", {}),
            ("codex_terminals", "thread/backgroundTerminals/terminate", {"action": "terminate", "process_id": "missing"}),
            ("codex_terminals", "thread/backgroundTerminals/clean", {"action": "clean"}),
            ("codex_rename", "thread/name/set", {"name": "never applied"}),
            ("codex_skills", "skills/list", {}),
            ("codex_subagents", "thread/list", {}),
            ("codex_revert", "thread/revert", {"before_turn_id": task["turn_id"], "confirm_history_only": True}),
            ("codex_archive", "thread/archive", {"action": "archive"}),
            ("codex_archive", "thread/unarchive", {"action": "unarchive"}),
            ("codex_delete", "thread/delete", {"confirm_thread_id": task["thread_id"]}),
        ]
        for index, (tool, method, options) in enumerate(cases):
            with self.subTest(tool=tool, method=method, options=options):
                self.rejected.clear()
                if index:
                    # Each subcase models an independent native capability
                    # profile; observations persist until a new connection.
                    await self.backend.close()
                    await self.backend.start()
                    await self.service.resume(task["id"])
                gate = self.gates[f"optional-{index}"] = FaultGate(f"optional-{index}")
                self.rejected[method] = gate
                key = f"unsupported-{index}"
                args = {"task_id": task["id"], **options}
                schema = next(tool_schema["inputSchema"] for tool_schema in TOOLS if tool_schema["name"] == tool)
                if "request_id" in schema["properties"] and not (options.get("action") in ("get", "list") or
                        (tool == "codex_terminals" and options.get("action", "list") == "list")):
                    args["request_id"] = key
                before = self.service.task(task["id"])
                claims = self.store.claims(task_id=task["id"])
                pending = asyncio.create_task(dispatch(self.service, tool, args))
                try:
                    await gate.wait()
                except BaseException:
                    gate.release.set()
                    pending.cancel()
                    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 5)
                    raise
                finally:
                    gate.release.set()
                with self.assertRaises(OptionalCapabilityUnavailable) as caught:
                    await asyncio.wait_for(pending, 5)
                self.assert_optional(caught.exception, method)
                after = self.service.task(task["id"])
                for field in ("status", "turn_id", "turn_start", "queue_reservation", "queue_uncertain", "effort"):
                    self.assertEqual(after.get(field), before.get(field), field)
                self.assertEqual(self.store.claims(task_id=task["id"]), claims)
                self.assertEqual(len(self.store.all("tasks")), 1)
                self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)
                self.assertFalse(self.service.intent_inflight)
                # A repeat of a rejected mutation must preserve its stable error.
                if self.store.db.execute("SELECT 1 FROM operations WHERE id=?", (key,)).fetchone():
                    count = self.rejection_count[method]
                    with self.assertRaises(OptionalCapabilityUnavailable) as repeated:
                        await dispatch(self.service, tool, args)
                    self.assertEqual(repeated.exception.as_dict(), caught.exception.as_dict())
                    self.assertEqual(self.rejection_count[method], count)

    async def test_unavailable_current_settings_preserves_running_turn(self):
        task = await self.start_task("hold")
        self.rejected["turn/settings/update"] = None
        before = self.service.task(task["id"])
        with self.assertRaises(OptionalCapabilityUnavailable) as caught:
            await dispatch(self.service, "codex_settings", {"task_id": task["id"], "request_id": "current",
                "scope": "current", "expected_turn_id": before["turn_id"], "effort": "high"})
        self.assert_optional(caught.exception, "turn/settings/update")
        after = self.service.task(task["id"])
        self.assertEqual(after["status"], "running")
        self.assertEqual(after["effort"], before["effort"])
        self.assertEqual(after.get("active_settings"), before.get("active_settings"))

    async def test_unavailable_name_during_start_leaves_a_verified_idle_thread(self):
        self.rejected["thread/name/set"] = None
        self.service.projects("create", str(self.root / "named"))
        with self.assertRaises(OptionalCapabilityUnavailable) as caught:
            await dispatch(self.service, "codex_start", {"request_id": "named", "prompt": "never execute",
                "workdir": str(self.root / "named"), "name": "unsupported"})
        self.assert_optional(caught.exception, "thread/name/set")
        task = self.store.all("tasks")[0]
        self.assertEqual(task["status"], "idle")
        self.assertTrue(task["thread_id"])
        self.assertEqual(task["verified_generation"], self.backend.generation)
        self.assertFalse(self.store.claims(task_id=task["id"]))
        self.assertFalse(any(frame["message"].get("method") == "turn/start" for frame in self.backend.frames))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)

    async def test_unavailable_control_keeps_preexisting_queue_and_goal_claims(self):
        task = await self.idle_task()
        await self.service.queue(task["id"], action="add", prompt="queued")
        before = self.service.task(task["id"])
        queue = self.service.claim_snapshot(task["id"], "queue")
        self.rejected["thread/queue/add"] = None
        with self.assertRaises(OptionalCapabilityUnavailable):
            await self.service.queue(task["id"], action="add", prompt="rejected")
        self.assertEqual(self.service.task(task["id"])["queue_reservation"], before["queue_reservation"])
        restored = self.service.claim_snapshot(task["id"], "queue")
        self.assertEqual((restored["state"], restored["data"]), (queue["state"], queue["data"]))
        self.rejected.clear()
        await self.service.goal(task["id"], action="set", objective="Explicit goal", user_requested=True)
        goal = self.service.claim_snapshot(task["id"], "goal")
        self.rejected["thread/goal/set"] = None
        with self.assertRaises(OptionalCapabilityUnavailable):
            await self.service.goal(task["id"], action="set", objective="rejected", user_requested=True)
        restored = self.service.claim_snapshot(task["id"], "goal")
        self.assertEqual((restored["state"], restored["data"]), (goal["state"], goal["data"]))

    async def test_rejected_control_does_not_overwrite_newer_turn_observation(self):
        task = await self.idle_task()
        gate = self.gates["review"] = FaultGate("review")
        self.rejected["review/start"] = gate
        pending = asyncio.create_task(dispatch(self.service, "codex_review", {"task_id": task["id"], "request_id": "review"}))
        try:
            await gate.wait()
            await self.service.on_message({"method": "turn/started", "params": {
                "threadId": task["thread_id"], "turn": {"id": "newer-turn"}}})
        finally:
            gate.release.set()
        with self.assertRaises(OptionalCapabilityUnavailable):
            await asyncio.wait_for(pending, 5)
        self.assertEqual(self.service.task(task["id"])["turn_id"], "newer-turn")
        self.assertEqual(self.service.task(task["id"])["status"], "running")
        self.assertEqual(self.service.claim_snapshot(task["id"], "turn")["state"], "active")

    async def test_required_method_not_found_remains_a_hard_error(self):
        await self.service.ensure_backend()
        self.rejected["thread/start"] = None
        with self.assertRaises(RpcError) as caught:
            await self.start_task()
        self.assertIs(type(caught.exception), RpcError)
        self.assertNotIn("thread/start", self.backend.unavailable_methods)
        self.assertFalse(any(frame["message"].get("method") == "turn/start" for frame in self.backend.frames))

    async def test_optional_error_survives_ipc_and_every_mcp_revision(self):
        self.assertIs(VERSIONS, MCP_VERSIONS)
        error = OptionalCapabilityUnavailable("account/rateLimits/read", "codex-cli 0.168.0", {})
        response = json.loads(json.dumps({"error": str(error), "error_data": error.as_dict()}))
        restored = response_error(response)
        self.assertEqual(restored.as_dict(), error.as_dict())
        self.assertIs(type(response_error({"error": "ordinary failure"})), BridgeError)
        for version in MCP_VERSIONS:
            adapter = RecordingMCP()
            adapter.version, adapter.ready = version, True
            with patch("codex_local_mcp.mcp.ensure_broker", AsyncMock()), \
                    patch("codex_local_mcp.mcp.rpc", AsyncMock(side_effect=restored)):
                await asyncio.wait_for(adapter.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "codex_usage", "arguments": {}}}), 5)
            result = adapter.frames[-1]["message"]["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"]), error_envelope(error))
            self.assertEqual("structuredContent" in result, version >= "2025-06-18")

    async def test_malformed_notification_is_quarantined_to_one_task_until_verified_resume(self):
        task = await self.start_task("approval")
        pending = (await self.until(task["id"]))["pending_requests"][0]
        other = await self.start_task("hold", path="b", key="other")
        await self.service.on_message({"method": "turn/completed", "params": {
            "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": {"invalid": True}}}})
        broken = self.service.task(task["id"])
        self.assertEqual(broken["status"], "needs_resume")
        self.assertFalse(broken["evidence_complete"])
        self.assertIn("native_protocol_error", broken["evidence_gaps"])
        self.assertEqual(self.store.get("requests", pending["id"])["status"], "expired")
        with self.assertRaises(BridgeError):
            await self.service.respond(pending["id"], {"decision": "accept"}, human=True)
        # Even a plausible later completion cannot release a quarantined claim.
        await self.service.on_message({"method": "turn/completed", "params": {
            "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}}})
        self.assertEqual(self.service.task(task["id"])["status"], "needs_resume")
        self.assertTrue(self.store.claims(task_id=task["id"]))
        for method, options in ((self.service.queue, {"action": "add", "prompt": "must not execute"}),
                                (self.service.goal, {"action": "set", "objective": "must not execute", "user_requested": True})):
            with self.assertRaisesRegex(BridgeError, "call codex_resume"):
                await method(task["id"], **options)
        self.assertTrue(self.backend.alive)
        self.assertEqual(self.service.task(other["id"])["status"], "running")
        await self.service.interrupt(other["id"], other["turn_id"])
        self.assertEqual((await self.until(other["id"], ("interrupted",)))["status"], "interrupted")
        await self.service.resume(task["id"])
        self.assertNotIn(task["id"], self.service.protocol_quarantine)
        self.assertEqual(self.service.task(task["id"])["status"], "running")
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))

    async def test_each_malformed_server_request_is_rejected_without_a_grant(self):
        task = await self.start_task("hold")
        cases = [
            ("item/commandExecution/requestApproval", {"itemId": 1}),
            ("item/fileChange/requestApproval", {"itemId": "file", "turnId": []}),
            ("item/permissions/requestApproval", {"permissions": []}),
            ("item/tool/requestUserInput", {"itemId": "q", "isBlocking": True, "questions": [1]}),
            ("item/tool/call", {"callId": "c", "tool": "ask_parent", "arguments": {"question": []}}),
            ("mcpServer/elicitation/request", {"mode": "form", "requestedSchema": []}),
            ("item/fileChange/requestApproval", {"itemId": "file", "availableDecisions": {"accept": True}}),
            ("item/permissions/requestApproval", {"permissions": {"network": {"enabled": "yes"}}}),
            ("item/tool/requestUserInput", {"itemId": "q", "isBlocking": [], "questions": []}),
        ]
        for index, (method, fields) in enumerate(cases):
            with self.subTest(method=method):
                await self.service.on_message({"id": f"malformed-{index}", "method": method,
                    "params": {"threadId": task["thread_id"], "turnId": task["turn_id"], **fields}})
                response = self.backend.frames[-1]["message"]
                self.assertEqual(response["id"], f"malformed-{index}")
                self.assertEqual(response["error"]["code"], -32602)
                self.assertIn("no permission granted", response["error"]["message"])
                self.assertNotIn("result", response)
        self.assertFalse(self.service.requests(task["id"]))
        self.assertTrue(self.backend.alive)
        await self.service.on_message({"id": "unowned-malformed", "method": "item/tool/call", "params": []})
        self.assertEqual(self.backend.frames[-1]["message"]["error"]["code"], -32602)
        self.assertTrue(self.backend.alive)

    async def test_incompatible_notification_fields_are_reported_without_shared_disconnect(self):
        task = await self.start_task("hold")
        cases = [
            ("item/completed", {"item": {"id": "cmd", "type": "commandExecution", "exitCode": 0}}),
            ("item/completed", {"item": {"id": "file", "type": "fileChange", "changes": [{"path": 1}]}}),
            ("item/started", {"item": []}),
            ("item/fileChange/patchUpdated", {"itemId": "file", "changes": "invalid"}),
            ("turn/plan/updated", {"plan": [{"step": "missing status"}]}),
            ("turn/diff/updated", {"diff": {}}),
            ("thread/tokenUsage/updated", {"tokenUsage": []}),
            ("thread/status/changed", {"status": []}),
            ("serverRequest/resolved", {"requestId": []}),
        ]
        for method, fields in cases:
            with self.subTest(method=method):
                await self.service.on_message({"method": method, "params": {
                    "threadId": task["thread_id"], "turnId": task["turn_id"], **fields}})
                row = self.store.db.execute("SELECT data FROM events WHERE kind='bridge/protocol_error' ORDER BY seq DESC LIMIT 1").fetchone()
                error = json.loads(row[0])
                self.assertEqual(error["method"], method)
                self.assertEqual(error["scope"], "task")
                self.assertIn("must be", error["reason"])
                self.assertTrue(self.backend.alive)
        self.assertEqual(self.service.task(task["id"])["status"], "needs_resume")

    async def test_optional_metadata_unavailable_during_notification_is_contained(self):
        self.service.automatic_reconciliation = True
        task = await self.idle_task()
        await self.service.queue(task["id"], action="add", prompt="queued")
        other = await self.start_task("hold", path="b", key="other")
        await self.reconciliation_idle()
        self.rejected["thread/queue/list"] = None
        await self.service.on_message({"method": "thread/queue/changed", "params": {"threadId": task["thread_id"]}})
        self.assertEqual(self.service.task(task["id"])["status"], "needs_resume")
        self.assertTrue(self.service.task(task["id"])["queue_reservation"])
        self.assertTrue(self.store.claims(task_id=task["id"]))
        self.assertEqual(self.service.task(other["id"])["status"], "running")
        self.assertTrue(self.backend.alive)
        self.assertIn("thread/queue/list", (await self.service.status())["unavailable_optional_methods"])

    async def test_unavailable_metadata_keeps_ownership_without_background_retries(self):
        self.service.automatic_reconciliation = True
        task = await self.idle_task()
        clock = ReconciliationClock(self.service)
        method = "thread/queue/list"
        with patch.object(self.service, "reconciliation_timer", clock.later):
            queued = await self.service.queue(task["id"], "add", "queued")
            await self.reconciliation_idle()
            await self.backend.call("thread/queue/delete", {"threadId": task["thread_id"],
                "queuedSubmissionId": queued["queuedSubmission"]["id"]})
            self.rejected[method] = None
            await self.service.reconcile_claims(task["id"])
            await self.reconciliation_idle()
            self.assertEqual(self.rejection_count[method], 1)
            self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            self.rejected.clear()  # Native replies could succeed; no re-probe until reconnect.
            before = self.backend.next_id
            for _ in range(3):
                clock.advance()
                self.service.schedule_reconciliation(task["id"])
                await self.reconciliation_idle()
                await self.service.reconcile_claims(task["id"])
                with self.assertRaises(OptionalCapabilityUnavailable):
                    await self.backend.call(method, {"threadId": task["thread_id"]})
                self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
            self.assertEqual(self.backend.next_id, before)
            self.assertFalse(clock.pending)
            self.assertIn(method, (await self.service.status())["unavailable_optional_methods"])
            await self.backend.close()
            await self.service.ensure_backend()
            await self.reconciliation_idle()
            self.assertNotIn(method, self.backend.unavailable_methods)
            self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_protocol_error_expires_a_positive_reply_waiting_for_write_lock(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        req = self.store.get("requests", req["id"])
        positive_ready = asyncio.Event()
        rejected = self.gates["malformed-response"] = FaultGate("malformed-response")
        original = self.backend.respond

        async def respond(request_id, result=None, error=None, **kwargs):
            if request_id == req["rpc_id"] and result == {"decision": "accept"}:
                positive_ready.set()
            if request_id == "malformed":
                await rejected.hit()
            await original(request_id, result, error, **kwargs)

        self.backend.respond = respond
        await self.backend.write_lock.acquire()
        owned_lock, pending = True, []
        try:
            approving = asyncio.create_task(self.service.respond(req["id"], {"decision": "accept"}, human=True))
            pending.append(approving)
            await asyncio.wait_for(positive_ready.wait(), 5)
            malformed = asyncio.create_task(self.service.on_message({"id": "malformed",
                "method": "item/permissions/requestApproval", "params": {
                    "threadId": task["thread_id"], "turnId": task["turn_id"], "permissions": []}}))
            pending.append(malformed)
            await rejected.wait()
            self.assertEqual(self.store.get("requests", req["id"])["status"], "expired")
            self.backend.write_lock.release()
            owned_lock = False
            with self.assertRaises(BridgeError):
                await asyncio.wait_for(approving, 5)
            rejected.release.set()
            await asyncio.wait_for(malformed, 5)
            self.assertFalse(any(frame["message"].get("id") == req["rpc_id"] and "result" in frame["message"]
                                 for frame in self.backend.frames if frame["direction"] == "outbound"))
        finally:
            if owned_lock:
                self.backend.write_lock.release()
            rejected.release.set()
            for future in pending:
                if not future.done():
                    future.cancel()
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)

    async def test_optional_fields_and_unknown_notifications_are_preserved(self):
        task = await self.start_task("hold")
        for method, fields in (("turn/plan/updated", {"plan": [{"step": "Check", "status": "pending", "future": []}]}),
                               ("future/optionalNotification", {"future": {"value": True}})):
            await self.service.on_message({"method": method, "params": {
                "threadId": task["thread_id"], "turnId": task["turn_id"], **fields, "extra": {"unknown": []}}})
        self.assertNotIn(task["id"], self.service.protocol_quarantine)
        self.assertEqual(self.service.task(task["id"])["status"], "running")
        rows = self.store.db.execute("SELECT kind FROM events WHERE task_id=?", (task["id"],)).fetchall()
        self.assertIn("future/optionalNotification", [row[0] for row in rows])

    async def test_unattributable_malformed_notification_disconnects_explicitly(self):
        task = await self.start_task("hold")
        exited, reasons = asyncio.Event(), []
        original = self.backend.on_exit

        async def on_exit(reason):
            reasons.append(reason)
            await original(reason)
            exited.set()

        self.backend.on_exit = on_exit
        self.backend.events.put_nowait({"method": "turn/completed", "params": {
            "threadId": "unowned-thread", "turn": {"id": "unknown", "status": []}}})
        await asyncio.wait_for(exited.wait(), 5)
        self.assertFalse(self.backend.alive)
        self.assertIn("native_protocol_incompatible", reasons[0])
        self.assertEqual(self.service.task(task["id"])["status"], "needs_resume")
        row = self.store.db.execute("SELECT data FROM events WHERE kind='bridge/protocol_error' ORDER BY seq DESC LIMIT 1").fetchone()
        data = json.loads(row[0])
        self.assertEqual(data["scope"], "backend")
        self.assertEqual(set(data), {"code", "method", "codex_version", "reason", "scope", "event_gap_possible", "recovery"})
