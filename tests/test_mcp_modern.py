import asyncio
import copy
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from codex_local_mcp.contract import MCP_MODERN_VERSION, MCP_TASKS_EXTENSION, MCP_LEGACY_VERSIONS, MCP_MODERN_CATALOG_BUDGET
from codex_local_mcp.mcp_modern import META_VERSION, META_CAPABILITIES, META_SERVER
from codex_local_mcp.protocol_tasks import ProtocolTasks, read_execution, update_execution
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store
from codex_local_mcp.tools import dispatch, encoded_size, RESULT_BUDGET
from tests.mcp_wire import RecordingMCP
from tests.test_service import ServiceHarness

TASK_CAPS = {"extensions": {MCP_TASKS_EXTENSION: {}}}
FORM_CAPS = {**TASK_CAPS, "elicitation": {"form": {}}}


class ModernMCPTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.adapter = RecordingMCP()
        self.adapter.config = self.cfg
        self.manager = ProtocolTasks(self.service)
        self.serial = 0
        self.bootstrap = patch("codex_local_mcp.mcp_modern.ensure_broker", AsyncMock())
        self.channel = patch("codex_local_mcp.mcp_modern.rpc", self.exchange)
        self.bootstrap_mock = self.bootstrap.start()
        self.channel.start()

    async def asyncTearDown(self):
        self.channel.stop()
        self.bootstrap.stop()
        await asyncio.wait_for(super().asyncTearDown(), 15)

    async def exchange(self, config, method, *, name=None, args=None, **kwargs):
        if method == "mcp_tasks":
            return await self.manager.handle(name, args)
        self.assertEqual(method, "tool")
        return await dispatch(self.service, name, args)

    async def request(self, method, params=None, caps=None, *, version=MCP_MODERN_VERSION):
        self.serial += 1
        request_id = "modern-" + str(self.serial)
        value = {**(params or {}), "_meta": {META_VERSION: version, META_CAPABILITIES: FORM_CAPS if caps is None else caps}}
        await asyncio.wait_for(self.adapter.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": value}), 10)
        return next(frame["message"] for frame in reversed(self.adapter.frames) if frame["message"].get("id") == request_id)

    async def task(self, prompt="hold", *, caps=None, name="codex_start", **extra):
        key = "modern-start-" + str(self.serial)
        project = self.root / key
        self.service.projects("create", str(project))
        response = await self.request("tools/call", {"name": name, "arguments": {"workdir": str(project), "prompt": prompt,
            "operation_id": key, **extra}}, caps)
        self.assertNotIn("error", response, response)
        return response["result"]

    @staticmethod
    def code(response):
        return response["error"]["data"]["error"]["code"]

    async def input_task(self, prompt="approval", *, caps=None):
        created = await self.task(prompt)
        record = read_execution(self.store, created["taskId"])
        await self.until(record["bridge_task_id"], ("waiting",))
        response = await self.request("tasks/get", {"taskId": created["taskId"]}, caps)
        self.assertNotIn("error", response, response)
        return created, record, response["result"]

    async def test_discovery_metadata_versions_and_catalog_do_not_start_native(self):
        discover = (await self.request("server/discover", caps={}))["result"]
        self.assertEqual(discover["resultType"], "complete")
        self.assertIn(MCP_MODERN_VERSION, discover["supportedVersions"])
        self.assertEqual(discover["capabilities"]["extensions"], {MCP_TASKS_EXTENSION: {}})
        self.assertIn(META_SERVER, discover["_meta"])
        catalog = (await self.request("tools/list", caps={}))["result"]
        self.assertLessEqual(encoded_size(catalog), MCP_MODERN_CATALOG_BUDGET)
        self.assertTrue(all("outputSchema" in tool for tool in catalog["tools"]))
        self.bootstrap_mock.assert_not_awaited()
        self.assertFalse(self.service.backend.alive)
        response = await self.request("server/discover", version="2099-01-01")
        self.assertEqual(response["error"]["code"], -32022)
        self.assertEqual(self.code(response), "mcp_version_unsupported")
        self.assertEqual(response["error"]["data"]["requested"], "2099-01-01")
        await self.adapter.handle({"jsonrpc": "2.0", "id": "malformed", "method": "server/discover", "params": {}})
        self.assertEqual(self.adapter.frames[-1]["message"]["error"]["code"], -32602)
        await self.adapter.handle({"jsonrpc": "2.0", "id": "fallback", "method": "initialize", "params": {
            "protocolVersion": MCP_MODERN_VERSION, "clientInfo": {"name": "dual", "version": "1"}, "capabilities": {}}})
        self.assertEqual(self.adapter.frames[-1]["message"]["result"]["protocolVersion"], MCP_LEGACY_VERSIONS[-1])

    async def test_capabilities_are_per_request_and_missing_extension_never_gets_a_handle(self):
        plain = await self.task("echo", caps={})
        self.assertEqual(plain["resultType"], "complete")
        self.assertNotIn("taskId", plain)
        created = await self.task("hold")
        response = await self.request("tasks/get", {"taskId": created["taskId"]}, {})
        self.assertEqual(response["error"]["code"], -32021)
        self.assertEqual(self.code(response), "mcp_capability_required")
        self.assertEqual(response["error"]["data"]["requiredCapabilities"], TASK_CAPS)
        self.assertNotIn("error", await self.request("tasks/get", {"taskId": created["taskId"]}))
        self.assertEqual(self.code(await self.request("tasks/result", {"taskId": created["taskId"]})), "unsupported_task_operation")
        self.assertEqual(self.code(await self.request("tasks/get", {"taskId": "does-not-exist"})), "not_found")

    async def test_completed_turn_is_immutable_when_conversation_gets_another_turn(self):
        created = await self.task("echo")
        record = read_execution(self.store, created["taskId"])
        await self.until(record["bridge_task_id"], ("completed",))
        first = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(first["status"], "completed")
        second = (await self.request("tools/call", {"name": "codex_send", "arguments": {"task_id": record["bridge_task_id"],
            "prompt": "hold", "operation_id": "second-turn"}}))["result"]
        self.assertNotEqual(created["taskId"], second["taskId"])
        final = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(first, final)
        self.assertEqual(final["result"]["structuredContent"]["native_execution"]["turn_id"], record["turn_id"])

    async def test_task_human_form_relay_is_one_shot_and_separate_from_tool_arguments(self):
        created, record, pending = await self.input_task()
        self.assertEqual(pending["status"], "input_required")
        key = next(iter(pending["inputRequests"]))
        self.assertIn("accept", pending["inputRequests"][key]["params"]["requestedSchema"]["properties"]["decision"]["enum"])
        for args in ({"pending_request_id": self.service.requests(record["bridge_task_id"])[0]["id"], "inputResponses": {key: {"action": "accept"}}},):
            response = await self.request("tools/call", {"name": "codex_review_request", "arguments": args})
            self.assertEqual(self.code(response), "invalid_arguments")
        payload = {"taskId": created["taskId"], "inputResponses": {key: {"action": "accept", "content": {"decision": "accept"}}}}
        self.assertEqual((await self.request("tasks/update", payload))["result"]["resultType"], "complete")
        response = await self.request("tasks/update", payload)
        self.assertEqual(self.code(response), "stale_request")
        await self.until(record["bridge_task_id"], ("completed",))
        receipts = [event for event in self.store.events(0, 100)["events"] if event["kind"] == "bridge/response_sent"]
        self.assertTrue(receipts[-1]["data"]["human_review"])

    async def test_no_form_capability_means_no_accept_but_denials_remain_possible(self):
        created, record, pending = await self.input_task(caps=TASK_CAPS)
        self.service.config = replace(self.service.config, approval_relay=True)
        self.assertEqual(pending["inputRequests"], {})
        key = pending["_meta"]["codex-local-mcp/pendingRequests"][0]["input_key"]
        positive = {"taskId": created["taskId"], "inputResponses": {key: {"action": "accept", "content": {"decision": "accept"}}}}
        response = await self.request("tasks/update", positive, TASK_CAPS)
        self.assertEqual(self.code(response), "human_review_required")
        self.assertEqual(self.service.task(record["bridge_task_id"])["status"], "waiting")
        positive["inputResponses"][key]["content"]["decision"] = "decline"
        self.assertNotIn("error", await self.request("tasks/update", positive, TASK_CAPS))

    async def test_parent_question_uses_ordinary_tool_and_never_a_person_form(self):
        created, record, pending = await self.input_task("parent")
        self.assertEqual(pending["inputRequests"], {})
        request_id = self.service.requests(record["bridge_task_id"])[0]["id"]
        response = await self.request("tools/call", {"name": "codex_respond", "arguments": {"pending_request_id": request_id,
            "response": {"text": "Run offline checks"}}})
        self.assertFalse(response["result"]["isError"])
        await self.until(record["bridge_task_id"], ("completed",))

    async def test_mrtr_review_requires_server_state_and_binds_the_pending_request(self):
        task = await self.start_task("approval")
        request_id = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]["id"]
        base = {"name": "codex_review_request", "arguments": {"pending_request_id": request_id}}
        no_form = (await self.request("tools/call", base, {}))["result"]
        self.assertEqual(no_form["resultType"], "complete")
        issued = (await self.request("tools/call", base))["result"]
        self.assertEqual(issued["resultType"], "input_required")
        key = next(iter(issued["inputRequests"]))
        retry = {**base, "requestState": issued["requestState"], "inputResponses": {key: {"action": "accept", "content": {"decision": "accept"}}}}
        wrong = {**retry, "arguments": {"pending_request_id": "another-request"}}
        self.assertEqual(self.code(await self.request("tools/call", wrong)), "stale_request")
        self.assertEqual((await self.request("tools/call", retry))["result"]["structuredContent"]["status"], "sent")
        self.assertEqual(self.code(await self.request("tools/call", retry)), "stale_request")

    async def test_stale_generation_and_changed_file_proposal_grant_nothing(self):
        created, record, pending = await self.input_task("file_approval")
        key = next(iter(pending["inputRequests"]))
        payload = {"taskId": created["taskId"], "inputResponses": {key: {"action": "accept", "content": {"decision": "accept"}}}}
        generation = self.service.backend.generation
        with patch.object(self.service.backend, "generation", generation + 1):
            self.assertEqual(self.code(await self.request("tasks/update", payload)), "stale_request")
        request = self.service.requests(record["bridge_task_id"])[0]
        with self.store.transaction():
            self.store.update("requests", request["id"], {"proposal_hash": "changed"})
        self.assertEqual(self.code(await self.request("tasks/update", payload)), "stale_proposal")
        self.assertEqual(self.store.get("requests", request["id"])["status"], "pending")

    async def test_file_proposal_is_rechecked_at_the_native_write_and_can_be_reviewed_again(self):
        created, record, pending = await self.input_task("file_approval")
        key = next(iter(pending["inputRequests"]))
        request = self.service.requests(record["bridge_task_id"])[0]
        entered = asyncio.Event()
        original = self.service.backend.respond
        async def responding(*args, **kwargs):
            entered.set()
            return await original(*args, **kwargs)
        self.service.backend.respond = responding
        payload = {"taskId": created["taskId"], "inputResponses": {key: {"action": "accept", "content": {"decision": "accept"}}}}
        running = None
        try:
            async with self.service.backend.write_lock:
                running = asyncio.create_task(self.request("tasks/update", payload))
                await asyncio.wait_for(entered.wait(), 5)
                await self.service.on_message({"method": "item/fileChange/patchUpdated", "params": {
                    "threadId": record["thread_id"], "turnId": record["turn_id"], "itemId": request["params"]["itemId"],
                    "changes": [{"path": "source.txt", "kind": {"type": "update"}, "diff": "+ revised"}]}})
            response = await asyncio.wait_for(running, 5)
            self.assertEqual(self.code(response), "stale_proposal")
            self.assertEqual(self.store.get("requests", request["id"])["status"], "pending")
            refreshed = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
            self.assertNotEqual(next(iter(refreshed["inputRequests"])), key)
        finally:
            if running:
                await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 5)

    async def test_human_answer_is_not_stored_in_protocol_metadata_and_unissued_input_is_refused(self):
        created, record, pending = await self.input_task("question")
        key = next(iter(pending["inputRequests"]))
        response = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
            "not-issued": {"action": "accept", "content": {"choice": "untrusted"}}}})
        self.assertEqual(self.code(response), "stale_request")
        answer = "HUMAN_ANSWER_NOT_IN_METADATA_7021"
        response = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
            key: {"action": "accept", "content": {"choice": answer}}}})
        self.assertNotIn("error", response)
        metadata = [row[0] for row in self.store.db.execute("SELECT value FROM metadata WHERE key GLOB 'mcp_execution:*'")]
        self.assertNotIn(answer, json.dumps(metadata))
        self.assertNotIn(record["bridge_task_id"] + "/", json.dumps(metadata))

    async def test_mrtr_denial_without_form_and_unknown_input_keys_are_safe(self):
        task = await self.start_task("approval")
        request_id = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]["id"]
        params = {"name": "codex_review_request", "arguments": {"pending_request_id": request_id}}
        issued = (await self.request("tools/call", params))["result"]
        key = next(iter(issued["inputRequests"]))
        response = await self.request("tools/call", {**params, "requestState": issued["requestState"],
            "inputResponses": {key: {"action": "accept", "content": {"decision": "decline"}}}}, {})
        self.assertEqual(response["result"]["structuredContent"]["status"], "sent")
        created = await self.task()
        response = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {"unknown": {"action": "cancel"}}})
        self.assertNotIn("error", response)

    async def test_redacted_form_keeps_acceptance_and_oversized_form_does_not(self):
        created, record, pending = await self.input_task()
        request = self.service.requests(record["bridge_task_id"])[0]
        with self.store.transaction():
            self.store.update("requests", request["id"], {"content_redacted": True})
        response = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        form = next(iter(response["inputRequests"].values()))["params"]
        self.assertIn("accept", form["requestedSchema"]["properties"]["decision"]["enum"])
        self.assertIn("content_redacted: true", form["message"])
        with self.store.transaction():
            self.store.update("requests", request["id"], {"content_redacted": False})
        with patch("codex_local_mcp.mcp.RESULT_BUDGET", 32):
            response = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        form = next(iter(response["inputRequests"].values()))["params"]
        self.assertNotIn("accept", form["requestedSchema"]["properties"]["decision"]["enum"])

    async def test_cancel_ack_keeps_claim_until_completion_and_cannot_cancel_next_turn(self):
        created = await self.task("hold_command")
        record = read_execution(self.store, created["taskId"])
        gate = self.gate_callback("terminal", "turn/completed")
        cancelled = await self.request("tasks/cancel", {"taskId": created["taskId"]})
        await gate.wait()
        self.assertEqual(cancelled["result"]["resultType"], "complete")
        current = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(current["status"], "working")
        self.assertIn("not yet confirmed", current["statusMessage"])
        self.assertTrue(self.store.claims(task_id=record["bridge_task_id"]))
        gate.release.set()
        await self.until(record["bridge_task_id"], ("interrupted",))
        self.assertEqual((await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]["status"], "cancelled")
        await self.request("tools/call", {"name": "codex_send", "arguments": {"task_id": record["bridge_task_id"], "prompt": "hold", "operation_id": "next-after-cancel"}})
        self.assertNotIn("error", await self.request("tasks/cancel", {"taskId": created["taskId"]}))
        self.assertEqual(self.service.task(record["bridge_task_id"])["status"], "running")

    async def test_restart_recovers_same_handle_without_starting_native_on_poll(self):
        created, record, pending = await self.input_task()
        await self.service.close()
        self.store.close()
        self.store = Store(self.state)
        self.service = self.new_service()
        self.manager = ProtocolTasks(self.service)
        response = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(response["status"], "working")
        self.assertIn("needs recovery", response["statusMessage"])
        self.assertFalse(self.service.backend.alive)
        await self.service.resume(record["bridge_task_id"])
        self.assertEqual((await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]["status"], "cancelled")

    async def test_result_limit_is_a_small_protocol_error_and_next_request_still_works(self):
        created = await self.task("echo")
        record = read_execution(self.store, created["taskId"])
        await self.until(record["bridge_task_id"], ("completed",))
        with patch.object(self.manager, "final_result", return_value={"content": [{"type": "text", "text": "x" * RESULT_BUDGET}]}):
            response = await self.request("tasks/get", {"taskId": created["taskId"]})
        self.assertEqual(self.code(response), "result_too_large")
        self.assertLess(encoded_size(response), 4096)
        self.assertEqual((await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]["status"], "completed")

    async def test_native_failure_is_a_completed_error_result_with_operation_identity(self):
        created = await self.task("fail")
        record = read_execution(self.store, created["taskId"])
        await self.until(record["bridge_task_id"], ("failed",))
        result = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["result"]["isError"])
        value = result["result"]["structuredContent"]
        self.assertFalse(value["ok"])
        self.assertEqual(value["operation_id"], record["operation_id"])
        self.assertEqual(value["native_execution"]["turn_id"], record["turn_id"])

    async def test_optional_native_error_keeps_the_modern_versioned_error_contract(self):
        from codex_local_mcp.backend import OptionalCapabilityUnavailable
        error = OptionalCapabilityUnavailable("account/rateLimits/read", "codex-cli 0.168.0", {})
        with patch.object(self.service, "usage", AsyncMock(side_effect=error)):
            response = await self.request("tools/call", {"name": "codex_usage", "arguments": {}})
        result = response["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["ok"])
        self.assertEqual(result["structuredContent"]["error"], error.as_dict())

    async def test_expired_human_form_is_refused_and_reissued_with_a_new_key(self):
        created, record, pending = await self.input_task()
        key = next(iter(pending["inputRequests"]))
        stored = read_execution(self.store, created["taskId"])
        ledger = copy.deepcopy(stored["inputs"])
        ledger[key]["expires_at"] = 0
        update_execution(self.store, stored["id"], {"inputs": ledger}, expected=stored["revision"])
        response = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
            key: {"action": "accept", "content": {"decision": "accept"}}}})
        self.assertEqual(self.code(response), "stale_request")
        refreshed = (await self.request("tasks/get", {"taskId": created["taskId"]}))["result"]
        self.assertNotEqual(key, next(iter(refreshed["inputRequests"])))

    async def test_metadata_caps_age_cas_terminal_immutability_and_namespace_isolation(self):
        key, other, third = "a" * 32, "b" * 32, "c" * 32
        self.store.db.execute("INSERT INTO metadata VALUES ('core-owned','keep')")
        with patch("codex_local_mcp.protocol_tasks.EXECUTION_LIMIT", 2):
            first = update_execution(self.store, key, {"state": "working", "inputs": {}}, now=0)
            update_execution(self.store, other, {"state": "working", "inputs": {}}, now=0)
            with self.assertRaises(BridgeError) as exhausted:
                update_execution(self.store, third, {"state": "working", "inputs": {}}, now=0)
            self.assertEqual(exhausted.exception.code, "result_too_large")
            first = update_execution(self.store, key, {"state": "completed"}, expected=first["revision"], now=1)
            with self.assertRaises(BridgeError):
                update_execution(self.store, key, {"state": "working"}, expected=first["revision"], now=2)
            with self.assertRaises(BridgeError) as expired:
                read_execution(self.store, key, now=86401)
            self.assertEqual(expired.exception.code, "not_found")
            update_execution(self.store, third, {"state": "working", "inputs": {}}, now=86401)
        with self.assertRaises(BridgeError) as conflict:
            update_execution(self.store, other, {"state": "working"}, expected=999, now=1)
        self.assertEqual(conflict.exception.code, "state_conflict")
        self.assertEqual(self.store.db.execute("SELECT value FROM metadata WHERE key='core-owned'").fetchone()[0], "keep")

    async def test_unobserved_completions_are_retired_even_when_new_handles_hit_quota(self):
        created = await self.task("echo")
        record = read_execution(self.store, created["taskId"])
        await self.until(record["bridge_task_id"], ("completed",))
        self.assertEqual(read_execution(self.store, created["taskId"])["state"], "working")
        with patch("codex_local_mcp.protocol_tasks.EXECUTION_LIMIT", 1):
            with self.assertRaises(BridgeError):
                update_execution(self.store, "f" * 32, {"state": "working", "inputs": {}})
            final = read_execution(self.store, created["taskId"])
            self.assertEqual(final["state"], "completed")
            update_execution(self.store, "f" * 32, {"state": "working", "inputs": {}}, now=final["terminal_at"] + 86400)

    async def test_cancel_of_an_unpolled_old_completion_cannot_interrupt_a_follow_up(self):
        created = await self.task("echo")
        record = read_execution(self.store, created["taskId"])
        await self.until(record["bridge_task_id"], ("completed",))
        await dispatch(self.service, "codex_send", {"task_id": record["bridge_task_id"], "prompt": "hold", "operation_id": "unpolled-followup"})
        self.assertNotIn("error", await self.request("tasks/cancel", {"taskId": created["taskId"]}))
        self.assertEqual(self.service.task(record["bridge_task_id"])["status"], "running")
