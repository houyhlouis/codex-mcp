"""Guidance expiry and typed decisions remain accurate through MCP adapters."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from codex_local_mcp.protocol_tasks import ProtocolTasks, read_execution
from codex_local_mcp.tools import dispatch
from tests import test_mcp_modern as modern
from tests.mcp_wire import RecordingMCP
from tests.test_service import ServiceHarness


class ModernGuidanceReviewTest(ServiceHarness):
    request, task = modern.ModernMCPTest.request, modern.ModernMCPTest.task

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.adapter = RecordingMCP()
        self.adapter.config = self.cfg
        self.manager, self.serial = ProtocolTasks(self.service), 0
        self.channels = [patch("codex_local_mcp." + module + "." + name, value)
            for module in ("mcp", "mcp_modern") for name, value in (
                ("ensure_broker", AsyncMock()), ("rpc", self.exchange))]
        for channel in self.channels:
            channel.start()

    async def asyncTearDown(self):
        try:
            await asyncio.wait_for(super().asyncTearDown(), 15)
        finally:
            for channel in reversed(self.channels):
                channel.stop()

    async def exchange(self, config, method, *, name=None, args=None):
        if method == "mcp_tasks":
            return await self.manager.handle(name, args)
        return await dispatch(self.service, name, args)

    async def question(self, task, *, large=False):
        self.serial += 1
        await self.service.on_message({"id": 9000 + self.serial, "method": "item/tool/call", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "tool": "ask_parent", "callId": "review-question-" + str(self.serial),
            "arguments": {"question": "Which offline check?", "envelope": {"version": 1, "id": "choose-check",
                "goal": "g" * 4096 if large else "Choose an offline check", "ttl_seconds": 60,
                "options": [{"id": "unit", "label": "Unit tests"}]}}}})
        return self.service.requests(task["id"])[-1]

    async def test_task_and_review_pending_summaries_refresh_guidance_expiry(self):
        for large in (False, True):
            with self.subTest(large=large):
                created = await self.task("hold")
                execution = read_execution(self.store, created["taskId"])
                task = self.service.task(execution["bridge_task_id"])
                request = await self.question(task, large=large)
                first = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
                self.assertFalse(first["_meta"]["codex-local-mcp/pendingRequests"][0]["decision_summary"]["expired"])
                with patch("codex_local_mcp.decisions.time.time", return_value=request["decision_envelope"]["expires_at"]):
                    task_result = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
                    summary = task_result["_meta"]["codex-local-mcp/pendingRequests"][0]
                    self.assertEqual(summary["details_truncated"], large)
                    self.assertTrue(summary["decision_summary"]["expired"])
                    self.assertFalse(summary["decision_summary"]["typed_response_allowed"])
                    for caps in ({}, modern.FORM_CAPS):
                        review = (await self.request("tools/call", {"name": "codex_review_request",
                            "arguments": {"request_id": request["id"]}}, caps))["result"]["structuredContent"]["request"]
                        self.assertTrue(review["decision_summary"]["expired"])
                        self.assertFalse(review["decision_summary"]["typed_response_allowed"])
                        if "decision_envelope" in review:
                            self.assertTrue(review["decision_envelope"]["expired"])
                self.assertEqual(self.store.get("requests", request["id"])["status"], "pending")

    async def test_completed_task_result_refreshes_cached_delegation_contract_expiry(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        args = {"task_id": task["id"], "prompt": "Inspect offline results", "operation_id": "review-contract",
            "contract": {"version": 1, "id": "offline-contract", "ttl_seconds": 60}}
        created = (await self.request("tools/call", {"name": "codex_delegate", "arguments": args}))["result"]
        self.assertEqual(created["resultType"], "task")
        await self.until(task["id"], ("completed",))
        first = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]["result"]["structuredContent"]
        self.assertFalse(first["delegation_contract"]["expired"])
        clock = first["delegation_contract"]["expires_at"]
        with patch("codex_local_mcp.decisions.time.time", return_value=clock), \
                patch.object(self.service.backend, "call", AsyncMock(side_effect=AssertionError("Expiry inspection must not send native work"))):
            cached = await dispatch(self.service, "codex_delegate", args)
            self.assertTrue(cached["delegation_contract"]["expired"])
            result = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]["result"]["structuredContent"]
        self.assertTrue(result["delegation_contract"]["expired"])
        self.assertTrue(result["delegation_contract_summary"]["expired"])
        self.assertTrue(all(record["expired"] for record in result["delegation_contracts"]))
        self.assertEqual(first["native_execution"], result["native_execution"])

    async def test_both_adapters_preserve_typed_decision_errors_and_receipt_authority(self):
        task = await self.start_task("hold")
        request = await self.question(task)
        decision = {"version": 1, "id": request["decision_envelope"]["id"],
            "context_hash": request["decision_envelope"]["context_hash"], "option_id": "not-offered"}
        args = {"request_id": request["id"], "response": {"text": "Choose a check", "decision": decision}}
        legacy = RecordingMCP()
        legacy.config, legacy.version, legacy.ready = self.cfg, "2025-11-25", True
        for old in (False, True):
            with self.subTest(legacy=old):
                if old:
                    await asyncio.wait_for(legacy.handle({"jsonrpc": "2.0", "id": "typed", "method": "tools/call",
                        "params": {"name": "codex_respond", "arguments": args}}), 5)
                    result = legacy.frames[-1]["message"]["result"]
                else:
                    result = (await self.request("tools/call", {"name": "codex_respond", "arguments": args}))["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["error"]["code"], "decision_mismatch")
                self.assertFalse(result["structuredContent"]["error"]["uncertain"])
        decision["option_id"] = "unit"
        result = (await self.request("tools/call", {"name": "codex_respond", "arguments": args}))["result"]
        self.assertFalse(result["isError"])
        receipt = result["structuredContent"]["receipt"]
        self.assertEqual(receipt["decision"], "answer")
        self.assertEqual(receipt["decision_source"], "main_agent")
        self.assertNotIn("Choose a check", json.dumps(receipt))
