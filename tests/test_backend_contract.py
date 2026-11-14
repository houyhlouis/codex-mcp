import asyncio
from unittest.mock import AsyncMock

from codex_local_mcp.backend import OptionalCapabilityUnavailable, RpcError
from codex_local_mcp.security import BridgeError
from tests.native_wire import RecordingAppServer, completed, wait_for
from tests.test_service import ServiceHarness
from tests.helpers import FaultGate


class BackendContractTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.backend = RecordingAppServer(self.cfg, self.service.on_message, self.service.on_exit)
        self.backend = self.service.backend

    async def test_start_records_exact_tested_and_untested_versions(self):
        for version, status in (("codex-cli 0.167.0", "tested"),
                                ("codex-cli 0.167.0-alpha.4", "untested"),
                                ("codex-cli 0.168.0", "untested")):
            backend = RecordingAppServer(self.cfg, AsyncMock(), AsyncMock())
            backend.reported_version = version
            try:
                await backend.start()
                self.assertEqual(backend.version, version)
                self.assertEqual(backend.compatibility["version"], version)
                self.assertEqual(backend.compatibility["status"], status)
                self.assertTrue(backend.compatibility["reason"])
            finally:
                await backend.close()

    async def test_old_and_unparseable_versions_never_start_app_server(self):
        for version in ("codex-cli 0.166.999", "codex-cli 0.166.0-alpha.4", "garbage 0.167.0"):
            backend = RecordingAppServer(self.cfg, AsyncMock(), AsyncMock())
            backend.reported_version = version
            with self.assertRaises(BridgeError):
                await backend.start()
            self.assertIsNone(backend.process)
            self.assertEqual(backend.compatibility["status"], "unsupported")
            self.assertFalse(backend.frames)

    async def test_optional_method_has_a_stable_error_and_observed_availability(self):
        await self.service.ensure_backend()
        method = "account/rateLimits/read"
        for error in ({"code": -32601, "message": "Method not found"},
                      {"code": -32000, "message": "Unsupported method"}):
            self.backend.method_errors[method] = error
            with self.assertRaises(OptionalCapabilityUnavailable) as caught:
                await self.backend.call(method, {})
            self.assertEqual(caught.exception.code, "optional_native_capability_unavailable")
            self.assertEqual(caught.exception.method, method)
            self.assertEqual(caught.exception.version, "codex-cli 0.167.0")
            self.assertIsInstance(caught.exception, RpcError)
            self.assertIn(method, self.backend.unavailable_methods)
            self.backend.method_errors.clear()
            frames = len(self.backend.frames)
            with self.assertRaises(OptionalCapabilityUnavailable) as cached:
                await self.backend.call(method, {})
            self.assertEqual(cached.exception.as_dict(), caught.exception.as_dict())
            self.assertEqual(len(self.backend.frames), frames)
            await self.backend.close()
            await self.backend.start()
            self.assertFalse(self.backend.unavailable_methods)
            await self.backend.call(method, {})

    async def test_late_success_preserves_connection_unavailability(self):
        await self.service.ensure_backend()
        method = "account/rateLimits/read"
        gate = self.gates["late_success"] = FaultGate("late_success")
        original = self.backend.send
        async def send(message, **kwargs):
            await original(message, **kwargs)
            if message.get("method") == method and not gate.used:
                await asyncio.shield(self.backend.pending[message["id"]])
                await gate.hit()
        self.backend.send = send
        older = asyncio.create_task(self.backend.call(method, {}))
        await gate.wait()
        self.backend.method_errors[method] = {"code": -32601, "message": "Method not found"}
        with self.assertRaises(OptionalCapabilityUnavailable):
            await self.backend.call(method, {})
        gate.release.set()
        self.assertIn("rateLimits", await asyncio.wait_for(older, 5))
        self.assertIn(method, self.backend.unavailable_methods)
        self.backend.method_errors.clear()
        before = len(self.backend.frames)
        with self.assertRaises(OptionalCapabilityUnavailable):
            await self.backend.call(method, {})
        self.assertEqual(len(self.backend.frames), before)

    async def test_waiting_write_rechecks_observed_unavailability(self):
        await self.service.ensure_backend()
        method = "account/rateLimits/read"
        gate = self.gates["unavailable_reply"] = FaultGate("unavailable_reply")
        waiting = asyncio.Event()
        original = self.backend.send
        async def send(message, **kwargs):
            if message.get("method") == method and gate.used:
                waiting.set()
            await original(message, **kwargs)
            if message.get("method") == method and not gate.used:
                await gate.hit()
        self.backend.send = send
        self.backend.method_errors[method] = {"code": -32601, "message": "Method not found"}
        unsupported = asyncio.create_task(self.backend.call(method, {}))
        await gate.wait()
        async with self.backend.write_lock:
            queued = asyncio.create_task(self.backend.call(method, {}))
            await asyncio.wait_for(waiting.wait(), 5)
            gate.release.set()
            with self.assertRaises(OptionalCapabilityUnavailable):
                await unsupported
            frames = len(self.backend.frames)
        with self.assertRaises(OptionalCapabilityUnavailable):
            await asyncio.wait_for(queued, 5)
        self.assertEqual(len(self.backend.frames), frames)

    async def test_required_unknown_and_invalid_argument_errors_stay_rpc_errors(self):
        await self.service.ensure_backend()
        options = {"cwd": str(self.root), "permissions": self.backend.profile("write"),
                   "runtimeWorkspaceRoots": [str(self.root)], "approvalPolicy": "on-request",
                   "config": {'mcp_servers."unrelated".enabled': False}}
        for method, error in (("thread/start", {"code": -32601, "message": "Method not found"}),
                              ("future/control", {"code": -32601, "message": "Method not found"}),
                              ("account/rateLimits/read", {"code": -32602, "message": "Invalid params"})):
            self.backend.method_errors[method] = error
            with self.assertRaises(RpcError) as caught:
                await self.backend.call(method, options if method == "thread/start" else {})
            self.assertIs(type(caught.exception), RpcError)
            self.assertNotIn(method, self.backend.unavailable_methods)

    async def check_missing_safety(self, field):
        self.backend.reported_version = "codex-cli 0.168.0"
        self.backend.omit_safety_fields.add(field)
        self.service.projects("create", str(self.root / "safety"))
        with self.assertRaises(BridgeError):
            await self.service.start("must not execute", "missing-safety", workdir=str(self.root / "safety"), approval_mode="ask")
        self.assertEqual(self.backend.compatibility["status"], "untested")
        self.assertFalse(any(frame["message"].get("method") == "turn/start" for frame in self.backend.frames))

    async def test_missing_permission_profile_blocks_execution(self):
        await self.check_missing_safety("activePermissionProfile")

    async def test_missing_cwd_blocks_execution(self):
        await self.check_missing_safety("cwd")

    async def test_missing_workspace_roots_blocks_execution(self):
        await self.check_missing_safety("runtimeWorkspaceRoots")

    async def test_missing_approval_policy_blocks_execution(self):
        await self.check_missing_safety("approvalPolicy")

    async def test_missing_requested_approval_reviewer_blocks_execution(self):
        await self.check_missing_safety("approvalsReviewer")

    async def test_unknown_optional_event_field_is_tolerated(self):
        task = await self.start_task("hold")
        await self.backend.on_message({"method": "turn/plan/updated", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"],
            "plan": [{"step": "Future optional field", "status": "inProgress"}],
            "futureOptional": {"value": True}}})
        await wait_for(self.service, lambda: any(step.get("step") == "Future optional field"
                                               for step in self.service.task(task["id"])["plan"]))
        self.assertTrue(self.backend.alive)
        await self.service.interrupt(task["id"], task["turn_id"])
        await completed(self.service, task["id"], "interrupted")

    async def test_unknown_server_request_fails_closed_on_the_wire(self):
        task = await self.start_task("unknown")
        await completed(self.service, task["id"])
        errors = [frame["message"] for frame in self.backend.frames if frame["direction"] == "outbound"
                  and "error" in frame["message"]]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"]["code"], -32601)
        self.assertEqual(errors[0]["error"]["message"], "Unsupported or unowned request; no permission granted")
        self.assertNotIn("result", errors[0])
        self.assertFalse(self.service.requests(task["id"]))
