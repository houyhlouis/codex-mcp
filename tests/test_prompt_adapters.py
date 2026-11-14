"""Person-only file review and supervision views through both MCP adapters."""

import asyncio
import base64
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from codex_local_mcp.evidence import artifact
from codex_local_mcp.mcp_modern import META_SERVER
from codex_local_mcp.protocol_tasks import ProtocolTasks, read_execution
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import BY_NAME, FRAME_LIMIT, RESULT_BUDGET, dispatch, encoded_size, validate
from tests import test_mcp_modern as modern
from tests.mcp_wire import RecordingMCP
from tests.test_service import ServiceHarness

FORM_CAPS, TASK_CAPS = modern.FORM_CAPS, modern.TASK_CAPS


class PromptAdapterTest(ServiceHarness):
    request, task, code = modern.ModernMCPTest.request, modern.ModernMCPTest.task, staticmethod(modern.ModernMCPTest.code)

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
        if method == "review":
            return await dispatch(self.service, "codex_respond", args, human=True)
        self.assertEqual(method, "tool")
        return await dispatch(self.service, name, args)

    async def proposal(self, *, execution=False, redacted=True):
        if execution:
            created = await self.task("hold")
            record = read_execution(self.store, created["taskId"])
            task = self.service.task(record["bridge_task_id"])
        else:
            created = None
            self.serial += 1
            task = await self.start_task("hold", "prompt-" + str(self.serial), "prompt-" + str(self.serial))
        self.serial += 1
        item = "patch-" + str(self.serial)
        params = {"threadId": task["thread_id"], "turnId": task["turn_id"]}
        await self.service.on_message({"method": "item/started", "params": {**params, "item": {
            "id": item, "type": "fileChange", "changes": [{"path": "source.txt", "kind": {"type": "update"},
                "diff": "+ password=hidden-fixture-value" if redacted else "+ plain fixture"}]}}})
        await self.service.on_message({"id": 9900 + self.serial, "method": "item/fileChange/requestApproval",
            "params": {**params, "itemId": item}})
        return created, task, self.service.requests(task["id"])[-1]

    def legacy(self, *, form=True, version="2025-11-25"):
        adapter = RecordingMCP()
        adapter.config, adapter.version, adapter.ready = self.cfg, version, True
        adapter.capabilities = {"elicitation": {"form": {}}} if form else {}
        return adapter

    async def legacy_call(self, adapter, name, args):
        self.serial += 1
        identifier = "legacy-" + str(self.serial)
        await asyncio.wait_for(adapter.handle({"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
            "params": {"name": name, "arguments": args}}), 10)
        return next(frame["message"] for frame in reversed(adapter.frames) if frame["message"].get("id") == identifier)

    async def stop_review(self, task):
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)

    async def legacy_prompt(self, request, *, version="2025-11-25"):
        adapter = self.legacy(version=version)
        running = asyncio.create_task(self.legacy_call(adapter, "codex_review_request", {"pending_request_id": request["id"]}))
        self.addAsyncCleanup(self.stop_review, running)
        await asyncio.wait_for(adapter.elicited.wait(), 5)
        prompt = next(frame["message"]["params"] for frame in adapter.frames if frame["message"].get("method") == "elicitation/create")
        return adapter, running, prompt

    async def modern_prompt(self, created, request, *, caps=FORM_CAPS):
        if created:
            response = await self.request("tasks/get", {"taskId": created["taskId"]}, caps)
        else:
            response = await self.request("tools/call", {"name": "codex_review_request", "arguments": {"pending_request_id": request["id"]}}, caps)
        self.assertNotIn("error", response, response)
        return response["result"]

    async def modern_answer(self, created, request, prompt, *, decision="accept", caps=FORM_CAPS):
        key = next(iter(prompt["inputRequests"]))
        responses = {key: {"action": "accept", "content": {"decision": decision}}}
        if created:
            return await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": responses}, caps)
        return await self.request("tools/call", {"name": "codex_review_request", "arguments": {"pending_request_id": request["id"]},
            "requestState": prompt["requestState"], "inputResponses": responses}, caps)

    def warning(self, prompt, request):
        message = prompt["message"]
        notice = ("Codex requests human review. Nothing is approved by default.\ncontent_redacted: true\nredaction_count: 1\n"
            + request["review_warning"])
        self.assertTrue(message.startswith(notice), message[:400])
        self.assertNotIn("hidden-fixture-value", message)
        self.assertIn(request["proposal_hash"], message)
        self.assertIn("accept", prompt["requestedSchema"]["properties"]["decision"]["enum"])

    def payload(self, response, name, *, success=True):
        self.assertNotIn("error", response, response)
        self.assertLessEqual(encoded_size(response), FRAME_LIMIT)
        result = response["result"]
        self.assertEqual(result.get("resultType", "complete"), "complete")
        self.assertEqual(result["isError"], not success)
        value = result.get("structuredContent", json.loads(result["content"][0]["text"]))
        self.assertEqual(json.loads(result["content"][0]["text"]), value)
        self.assertEqual(value["ok"], success)
        validate(value, BY_NAME[name]["outputSchema"])
        return value

    async def receipts(self, task):
        response = await self.request("tools/call", {"name": "codex_get", "arguments": {"task_id": task["id"], "view": "permissions"}}, {})
        return self.payload(response, "codex_get")["decision_receipts"]

    async def test_legacy_redacted_prompt_accept_and_denial_record_host_origin(self):
        for version in ("2025-06-18", "2025-11-25"):
            for decision in ("accept", "decline"):
                with self.subTest(version=version, decision=decision):
                    _, task, request = await self.proposal()
                    adapter, running, prompt = await self.legacy_prompt(request, version=version)
                    self.warning(prompt, request)
                    adapter.pending_ui[adapter.ui_id].set_result({"result": {"action": "accept", "content": {"decision": decision}}})
                    result = self.payload(await asyncio.wait_for(running, 5), "codex_review_request")
                    receipt = result["receipt"]
                    self.assertEqual(receipt["decision_source"], "host_elicitation")
                    self.assertEqual(receipt["proposal_hash"], request["proposal_hash"])
                    self.assertEqual(receipt["content_redacted"], True)
                    self.assertEqual(receipt["redaction_count"], 1)
                    self.assertEqual(receipt["delivery"], "sent")
                    self.assertEqual(await self.receipts(task), [receipt])

    async def test_modern_redacted_review_and_task_forms_accept_with_host_origin(self):
        for execution in (False, True):
            with self.subTest(execution=execution):
                created, task, request = await self.proposal(execution=execution)
                prompt = await self.modern_prompt(created, request)
                self.warning(next(iter(prompt["inputRequests"].values()))["params"], request)
                if created:
                    self.assertEqual(prompt["status"], "input_required")
                    details = prompt["_meta"]["codex-local-mcp/pendingRequests"][0]
                    self.assertTrue(details["person_review_required"])
                    self.assertEqual(details["review_warning"], request["review_warning"])
                answered = await self.modern_answer(created, request, prompt)
                self.assertNotIn("error", answered, answered)
                receipt = (await self.receipts(task))[0]
                self.assertEqual(receipt["decision_source"], "host_elicitation")
                self.assertEqual(receipt["proposal_hash"], request["proposal_hash"])
                self.assertTrue(receipt["content_redacted"])
                self.assertTrue(receipt["one_shot"])
                self.assertFalse(receipt["reusable"])
                if not created:
                    self.assertEqual(self.payload(answered, "codex_review_request")["receipt"], receipt)

    async def test_no_form_capability_keeps_redacted_requests_pending_with_guidance(self):
        _, _, request = await self.proposal()
        adapter = self.legacy(form=False)
        pending = self.payload(await self.legacy_call(adapter, "codex_review_request", {"request_id": request["id"]}), "codex_review_request")
        self.assertEqual(pending["status"], "pending")
        self.assertIn("local review command", pending["next_step"])
        self.assertTrue(pending["request"]["person_review_required"])
        self.assertFalse(adapter.elicited.is_set())
        for execution in (False, True):
            with self.subTest(execution=execution):
                created, task, request = await self.proposal(execution=execution)
                pending = await self.modern_prompt(created, request, caps=TASK_CAPS)
                if created:
                    self.assertEqual(pending["status"], "input_required")
                    self.assertEqual(pending["inputRequests"], {})
                    self.assertIn("local review", pending["statusMessage"])
                    summary = pending["_meta"]["codex-local-mcp/pendingRequests"][0]
                    denied = await self.request("tasks/update", {"taskId": created["taskId"], "inputResponses": {
                        summary["input_key"]: {"action": "accept", "content": {"decision": "accept"}}}}, TASK_CAPS)
                    self.assertEqual(self.code(denied), "human_review_required")
                else:
                    payload = pending["structuredContent"]
                    self.assertEqual(payload["status"], "pending")
                    self.assertIn("local review command", payload["next_step"])
                    summary = payload["request"]
                self.assertTrue(summary["content_redacted"])
                self.assertEqual(summary["redaction_count"], 1)
                self.assertTrue(summary["person_review_required"])
                self.assertEqual(summary["review_warning"], request["review_warning"])
                self.assertEqual(self.store.get("requests", request["id"])["status"], "pending")
                self.assertEqual(await self.receipts(task), [])

    async def test_private_review_source_cannot_be_set_through_either_tool_adapter(self):
        _, _, request = await self.proposal()
        adapter = self.legacy()
        self.service.config = replace(self.cfg, approval_relay=True)
        for name in ("codex_respond", "codex_review_request"):
            args = {"pending_request_id": request["id"], "review_source": "local_terminal"}
            if name == "codex_respond":
                args.update(response={"decision": "accept"}, proposal_hash=request["proposal_hash"])
            for modern in (False, True):
                with self.subTest(name=name, modern=modern):
                    response = (await self.request("tools/call", {"name": name, "arguments": args}) if modern else
                        await self.legacy_call(adapter, name, args))
                    self.assertEqual(response["error"]["data"]["error"]["code"], "invalid_arguments")
        args = {"request_id": request["id"], "proposal_hash": request["proposal_hash"],
            "response": {"decision": "accept", "review_source": "host_elicitation"}}
        for modern in (False, True):
            with self.subTest(nested=True, modern=modern):
                response = (await self.request("tools/call", {"name": "codex_respond", "arguments": args}) if modern else
                    await self.legacy_call(adapter, "codex_respond", args))
                self.assertEqual(self.payload(response, "codex_respond", success=False)["error"]["code"], "invalid_response")
        self.assertEqual(self.store.get("requests", request["id"])["status"], "pending")

    async def test_main_agent_person_review_error_is_unchanged_and_denial_remains_available(self):
        self.service.config = replace(self.cfg, approval_relay=True)
        for modern in (False, True):
            with self.subTest(modern=modern):
                _, _, request = await self.proposal()
                args = {"pending_request_id": request["id"], "proposal_hash": request["proposal_hash"], "response": {"decision": "accept"}}
                with patch.object(self.service.backend, "respond", wraps=self.service.backend.respond) as sender:
                    response = (await self.request("tools/call", {"name": "codex_respond", "arguments": args}, {}) if modern else
                        await self.legacy_call(self.legacy(form=False), "codex_respond", args))
                    sender.assert_not_awaited()
                error = self.payload(response, "codex_respond", success=False)["error"]
                self.assertEqual(error["code"], "person_review_required")
                self.assertEqual(error["message"], "A person must review this redacted file-change proposal through the client prompt or local terminal review; approval_relay cannot accept it")
                self.assertFalse(error["uncertain"])
                args["response"] = {"decision": "decline"}
                denied = (await self.request("tools/call", {"name": "codex_respond", "arguments": args}, {}) if modern else
                    await self.legacy_call(self.legacy(form=False), "codex_respond", args))
                self.assertEqual(self.payload(denied, "codex_respond")["receipt"]["decision_source"], "main_agent_denial")

    async def revise(self, task, request):
        await self.service.on_message({"method": "item/fileChange/patchUpdated", "params": {"threadId": task["thread_id"],
            "turnId": task["turn_id"], "itemId": request["params"]["itemId"], "changes": [{"path": "source.txt",
                "kind": {"type": "update"}, "diff": "+ password=second-hidden-fixture-value"}]}})

    async def test_redacted_proposal_changed_after_display_is_refused_on_both_paths(self):
        _, task, request = await self.proposal()
        adapter, running, _ = await self.legacy_prompt(request)
        await self.revise(task, request)
        adapter.pending_ui[adapter.ui_id].set_result({"result": {"action": "accept", "content": {"decision": "accept"}}})
        error = self.payload(await asyncio.wait_for(running, 5), "codex_review_request", success=False)["error"]
        self.assertEqual(error["code"], "stale_proposal")
        self.assertEqual(await self.receipts(task), [])
        for execution in (False, True):
            with self.subTest(execution=execution):
                created, task, request = await self.proposal(execution=execution)
                prompt = await self.modern_prompt(created, request)
                await self.revise(task, request)
                response = await self.modern_answer(created, request, prompt)
                self.assertEqual(self.code(response), "stale_proposal")
                self.assertEqual(self.store.get("requests", request["id"])["status"], "pending")
                self.assertEqual(await self.receipts(task), [])

    async def test_oversized_redacted_proposals_keep_terminal_guidance_without_acceptance(self):
        _, _, request = await self.proposal()
        adapter = self.legacy()
        with patch("codex_local_mcp.mcp.RESULT_BUDGET", 32):
            pending = await asyncio.wait_for(adapter.review(request["id"]), 5)
        self.assertIn("acceptance is disabled", pending["next_step"])
        self.assertIn("local review command", pending["next_step"])
        self.assertFalse(adapter.elicited.is_set())
        for execution in (False, True):
            with self.subTest(execution=execution):
                created, task, request = await self.proposal(execution=execution)
                with patch("codex_local_mcp.mcp.RESULT_BUDGET", 32):
                    prompt = await self.modern_prompt(created, request)
                    form = next(iter(prompt["inputRequests"].values()))["params"]
                    self.assertEqual(form["requestedSchema"]["properties"]["decision"]["enum"], ["decline", "cancel"])
                    self.assertIn(request["review_warning"], form["message"])
                    self.assertIn("redaction_count: 1", form["message"])
                    self.assertIn("No acceptance is available; review in the terminal.", form["message"])
                    response = await self.modern_answer(created, request, prompt)
                    self.assertEqual(self.code(response), "human_review_required")
                    self.assertEqual(await self.receipts(task), [])

    async def test_modern_form_denials_record_host_origin_only_for_an_issued_current_form(self):
        for execution in (False, True):
            with self.subTest(execution=execution):
                created, task, request = await self.proposal(execution=execution)
                prompt = await self.modern_prompt(created, request)
                self.assertNotIn("error", await self.modern_answer(created, request, prompt, decision="decline"))
                self.assertEqual((await self.receipts(task))[0]["decision_source"], "host_elicitation")
        created, task, request = await self.proposal(execution=True)
        prompt = await self.modern_prompt(created, request)
        self.assertNotIn("error", await self.modern_answer(created, request, prompt, decision="decline", caps=TASK_CAPS))
        self.assertEqual((await self.receipts(task))[0]["decision_source"], "main_agent_denial")

    async def test_modern_new_views_keep_results_fields_paging_and_bounds(self):
        task = await self.start_task("echo")
        await self.until(task["id"], ("completed",))
        await asyncio.wait_for(self.service.resume(task["id"]), 10)
        data = b"fixture evidence\n" * 100
        with self.store.transaction():
            reference = artifact(self.store.db, task["id"], task["turn_id"], data)
        for caps in ({}, FORM_CAPS):
            for view, field in (("reconciliation", "reconciliation"), ("permissions", "permission_view"), ("result", "result"), ("artifact", "artifact")):
                with self.subTest(caps=caps, view=view):
                    args = {"task_id": task["id"], "view": view}
                    if view == "artifact":
                        args.update(artifact_id=reference["artifact_id"], limit=16, offset=1)
                    response = await self.request("tools/call", {"name": "codex_get", "arguments": args}, caps)
                    value = self.payload(response, "codex_get")
                    self.assertEqual(value["view"], view)
                    self.assertIn(field, value)
                    self.assertIn(META_SERVER, response["result"]["_meta"])
                    self.assertLess(encoded_size(response["result"]), RESULT_BUDGET)
                    if view == "artifact":
                        self.assertEqual(base64.b64decode(value["artifact"]["data"]), data[1:17])
                        self.assertEqual(value["artifact"]["next_offset"], 17)
                    if view == "reconciliation":
                        self.assertFalse(value["reconciliation"]["automatic_retry"])
                        self.assertFalse(value["reconciliation"]["replay_permitted"])
        other = await self.start_task("echo", "other-view", "other-view")
        await self.until(other["id"], ("completed",))
        args = {"view": "outcome", "task_ids": [task["id"], other["id"]]}
        first = self.payload(await self.request("tools/call", {"name": "codex_events", "arguments": args}), "codex_events")
        self.assertEqual({outcome["id"] for outcome in first["outcomes"]}, set(args["task_ids"]))
        self.assertTrue(all(outcome["result_ref"]["arguments"]["view"] == "result" for outcome in first["outcomes"]))
        unchanged = self.payload(await self.request("tools/call", {"name": "codex_events", "arguments": {**args, "cursors": first["cursors"]}}), "codex_events")
        self.assertEqual(unchanged["outcomes"], [])
        self.assertEqual(unchanged["cursors"], first["cursors"])

    async def test_modern_cursor_errors_keep_journal_and_outcome_recovery_fields(self):
        task = await self.start_task("hold")
        seq = self.store.db.execute("SELECT MIN(seq) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0]
        with self.store.transaction():
            self.store.db.execute("INSERT INTO expired_event_ranges VALUES (?,?,?)", (seq, seq, task["id"]))
            self.store.db.execute("DELETE FROM events WHERE seq=?", (seq,))
        for args, details in (({"task_id": task["id"], "after": 0}, {"resume_after": seq}),
                ({"task_id": task["id"], "view": "outcome", "cursors": {task["id"]: "invalid"}}, {"cursor_kind": "outcome"}),
                ({"task_id": task["id"], "view": "outcome", "requests_cursor": "invalid"}, {"cursor_kind": "requests", "resume_cursor": None})):
            with self.subTest(args=args):
                response = await self.request("tools/call", {"name": "codex_events", "arguments": args})
                error = self.payload(response, "codex_events", success=False)["error"]
                self.assertEqual(error["code"], "cursor_expired")
                self.assertFalse(error["uncertain"])
                for key, value in details.items():
                    self.assertEqual(error[key], value)
                self.assertTrue(error["next_steps"])

    async def test_modern_view_errors_preserve_code_uncertainty_and_extra_fields(self):
        for view, code in (("reconciliation", "state_invalid"), ("permissions", "person_review_required"),
                ("result", "result_expired"), ("artifact", "artifact_version_mismatch")):
            with self.subTest(view=view):
                error = BridgeError("Fixture view failure", code=code, uncertain=True,
                    details={"resume_after": 17, "applied": False}, ids={"task_id": "fixture-task"})
                args = {"task_id": "fixture-task", "view": view}
                if view == "artifact":
                    args["artifact_id"] = "fixture-artifact"
                with patch.object(self.service, "get", side_effect=error):
                    response = await self.request("tools/call", {"name": "codex_get", "arguments": args})
                self.assertEqual(self.payload(response, "codex_get", success=False)["error"], error.as_dict())
        error = BridgeError("Fixture person gate", code="person_review_required")
        with patch.object(self.manager, "handle", AsyncMock(side_effect=error)):
            response = await self.request("tasks/update", {"taskId": "fixture-task", "inputResponses": {}})
        self.assertEqual(response["error"]["code"], -32602)
        expected = error.as_dict()
        expected["ids"]["mcp_task_id"] = "fixture-task"
        self.assertEqual(response["error"]["data"]["error"], expected)

    async def test_modern_new_views_cannot_exceed_the_adapter_result_budget(self):
        for view in ("reconciliation", "outcome", "permissions", "result", "artifact"):
            with self.subTest(view=view):
                name = "codex_events" if view == "outcome" else "codex_get"
                args = {"task_id": "fixture-task", "view": view}
                if view == "artifact":
                    args["artifact_id"] = "fixture-artifact"
                with patch("codex_local_mcp.mcp_modern.RESULT_BUDGET", 1024), \
                        patch("codex_local_mcp.mcp_modern.rpc", AsyncMock(return_value={"view": view, "fixture": "x" * 4096})):
                    response = await self.request("tools/call", {"name": name, "arguments": args})
                self.assertEqual(self.code(response), "result_too_large")
                self.assertLess(encoded_size(response), 2048)
