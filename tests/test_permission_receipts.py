"""Decision receipts and redacted review authorization share the service gate."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp.cli import review as terminal_review
from codex_local_mcp.mcp import MCP
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class PermissionReceiptTest(ServiceHarness):
    async def proposal(self, task, *, redacted=True, rpc_id=9970):
        changes = [{"path": "source.txt", "kind": {"type": "update", "move_path": "destination.txt"},
            "diff": "+ password=hidden-fixture-value" if redacted else "+ plain value"}]
        common = {"threadId": task["thread_id"], "turnId": task["turn_id"]}
        await self.service.on_message({"method": "item/started", "params": {**common, "item": {
            "id": "patch", "type": "fileChange", "changes": changes}}})
        await self.service.on_message({"id": rpc_id, "method": "item/fileChange/requestApproval", "params": {**common, "itemId": "patch"}})
        return self.service.requests(task["id"])[-1]

    async def review_rpc(self, config, method, **kw):
        return await dispatch(self.service, "codex_respond" if method == "review" else kw["name"], kw["args"], human=method == "review")

    async def test_redacted_relay_accept_is_refused_but_denial_is_allowed(self):
        task = await self.start_task("hold")
        self.service.config = replace(self.cfg, approval_relay=True)
        req = await self.proposal(task)
        self.assertTrue(req["content_redacted"])
        self.assertEqual(req["redaction_count"], 1)
        summary = self.service.request_page(task["id"])["requests"][0]
        self.assertTrue(summary["person_review_required"])
        self.assertIn("1 redacted fields", summary["review_warning"])
        self.assertEqual(summary["permission_summary"]["destinations"], 1)
        with patch.object(self.service.backend, "respond", wraps=self.service.backend.respond) as sender:
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_respond", {"request_id": req["id"], "response": {"decision": "accept"}, "proposal_hash": req["proposal_hash"]})
            self.assertEqual(error.exception.code, "person_review_required")
            sender.assert_not_awaited()
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")
        denied = await self.service.respond(req["id"], {"decision": "decline"})
        self.assertEqual(denied["receipt"]["decision_source"], "main_agent_denial")
        self.assertEqual(denied["receipt"]["proposal_hash"], req["proposal_hash"])

    async def test_mcp_prompt_displays_hidden_count_and_accepts_human_decision(self):
        task = await self.start_task("hold")
        req = await self.proposal(task)
        adapter = MCP(self.cfg)
        adapter.version, adapter.capabilities = "2025-11-25", {"elicitation": {"form": {}}}
        shown = asyncio.Event()
        ui_id = None
        async def write(message):
            nonlocal ui_id
            self.assertIn(req["review_warning"], message["params"]["message"])
            self.assertIn('"redaction_count": 1', message["params"]["message"])
            self.assertNotIn("hidden-fixture-value", message["params"]["message"])
            ui_id = message["id"]
            shown.set()
        adapter.write = write
        with patch("codex_local_mcp.mcp.rpc", self.review_rpc):
            review = asyncio.create_task(adapter.review(req["id"]))
            try:
                await asyncio.wait_for(shown.wait(), 5)
                adapter.pending_ui[ui_id].set_result({"result": {"action": "accept", "content": {"decision": "accept"}}})
                result = await asyncio.wait_for(review, 5)
            finally:
                if not review.done():
                    review.cancel()
                await asyncio.gather(review, return_exceptions=True)
        self.assertEqual(result["receipt"]["decision_source"], "host_elicitation")
        self.assertEqual(result["receipt"]["delivery"], "sent")

    async def test_terminal_displays_redaction_warning_and_records_its_source(self):
        task = await self.start_task("hold")
        req = await self.proposal(task)
        with patch("codex_local_mcp.cli.rpc", self.review_rpc), patch("codex_local_mcp.cli.sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="accept"), patch("builtins.print") as printed:
            result = await terminal_review(self.cfg, req["id"])
        shown = json.loads(printed.call_args.args[0])
        self.assertEqual(shown["redaction_count"], 1)
        self.assertEqual(shown["review_warning"], req["review_warning"])
        self.assertEqual(result["receipt"]["decision_source"], "local_terminal")

    async def test_new_prompt_hint_is_private_and_legacy_relay_still_works(self):
        task = await self.start_task("hold")
        req = await self.proposal(task)
        args = {"request_id": req["id"], "response": {"decision": "accept"}, "proposal_hash": req["proposal_hash"], "review_source": "host_elicitation"}
        with self.assertRaises(BridgeError):
            await dispatch(self.service, "codex_respond", args)
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"decision": "accept"}, proposal_hash=req["proposal_hash"], review_source="host_elicitation")
        result = await dispatch(self.service, "codex_respond", args, human=True)
        self.assertEqual(result["receipt"]["decision_source"], "host_elicitation")
        req = await self.proposal(task, redacted=False, rpc_id=9971)
        self.service.config = replace(self.cfg, approval_relay=True)
        result = await self.service.respond(req["id"], {"decision": "accept"}, proposal_hash=req["proposal_hash"])
        self.assertEqual(result["receipt"]["decision_source"], "operator_relay")

    async def test_malformed_trusted_review_source_is_rejected_before_a_decision(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, redacted=False)
        for source in ([], {}, 1, True, "operator_relay"):
            with self.subTest(source=source), patch.object(self.service.backend, "respond", wraps=self.service.backend.respond) as sent:
                with self.assertRaises(BridgeError) as error:
                    await dispatch(self.service, "codex_respond", {"request_id": req["id"], "response": {"decision": "accept"},
                        "proposal_hash": req["proposal_hash"], "review_source": source}, human=True)
                self.assertEqual(error.exception.code, "invalid_arguments")
                self.assertEqual(str(error.exception), "review_source is private to a trusted human review route")
                sent.assert_not_awaited()
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")
        self.assertEqual(self.service.get(task["id"], view="permissions")["decision_receipts"], [])

    async def test_scope_lists_normalized_rename_and_effective_settings(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, redacted=False)
        scope = req["permission_view"]
        self.assertEqual(scope["sources"][0]["normalized"], str(self.root / "a" / "source.txt"))
        self.assertEqual(scope["destinations"][0]["normalized"], str(self.root / "a" / "destination.txt"))
        self.assertTrue(scope["scope_verified"])
        self.assertFalse(scope["session_grant"])
        self.assertEqual(scope["settings"]["requested"]["approval_mode"], "inherit")
        self.assertEqual(scope["settings"]["inherited"]["approval_mode"], "ask")
        self.assertTrue(scope["settings"]["effective"]["verified"])
        denied = await self.service.respond(req["id"], {"decision": "decline"})
        view = await dispatch(self.service, "codex_get", {"task_id": task["id"], "view": "permissions", "limit": 1})
        self.assertEqual(view["decision_receipts"], [denied["receipt"]])
        self.assertIsNone(view["receipts_next_offset"])
        self.assertEqual((await self.service.settings(task["id"]))["permission_view"]["format_version"], 1)

    async def test_network_request_renders_host_scope_without_granting_it(self):
        task = await self.start_task("hold")
        await self.service.on_message({"id": 9980, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "network-command",
            "networkApprovalContext": {"host": "Example.COM.", "protocol": "https"}}})
        request = self.service.requests(task["id"])[-1]
        network = request["permission_view"]["network"]
        self.assertEqual(network["requested_host"], "Example.COM.")
        self.assertEqual(network["host"], "example.com")
        self.assertEqual(network["scope"], "specified_host")
        self.assertEqual(network["protocol"], "https")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 0)

    async def test_command_permission_view_uses_and_checks_the_requested_cwd(self):
        task = await self.start_task("hold")
        outside = self.root / "other-project"
        outside.mkdir()
        for index, (cwd, verified) in enumerate(((task["project"], True), (str(outside), False), ("\x00", False))):
            with self.subTest(cwd=cwd):
                await self.service.on_message({"id": 9920 + index, "method": "item/commandExecution/requestApproval", "params": {
                    "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "cwd-command-" + str(index), "cwd": cwd}})
                request = self.service.requests(task["id"])[-1]
                scope = request["permission_view"]
                self.assertEqual(scope["requested_cwd"], cwd)
                self.assertEqual(scope["scope_verified"], verified)
                self.assertEqual(scope["cwd"], None if "\x00" in cwd else cwd)
                if not verified:
                    self.assertTrue(scope["unknown"])

    async def test_permission_grant_is_one_turn_and_receipt_contains_no_answer(self):
        task = await self.start_task("permission")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        detail = self.service.public_request(self.service.review_request(req["id"]))
        self.assertTrue(detail["permission_view"]["network"]["requested"])
        result = await self.service.respond(req["id"], {"permissions": {"network": {"enabled": True}}}, human=True)
        self.assertEqual(result["receipt"]["authorization_scope"], "turn")
        self.assertTrue(result["receipt"]["one_shot"])
        self.assertFalse(result["receipt"]["reusable"])
        self.assertTrue(result["receipt"]["proposal_known"])
        for key in ("response", "answers", "params", "content"):
            self.assertNotIn(key, result["receipt"])
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"permissions": {"network": {"enabled": True}}}, human=True)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 1)

    async def test_lost_delivery_and_restart_keep_uncertain_receipt_without_replaying(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, redacted=False)
        original = self.service.backend.respond
        async def lose(*args, **kw):
            await original(*args, **kw)
            raise TimeoutError("Injected lost delivery acknowledgement")
        with patch.object(self.service.backend, "respond", lose), self.assertRaises(TimeoutError):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        await self.service.close()
        self.service = self.new_service()
        receipts = self.service.get(task["id"], view="permissions")["decision_receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["delivery"], "uncertain")
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])

    async def test_stale_proposal_has_no_receipt_or_positive_response(self):
        task = await self.start_task("hold")
        req = await self.proposal(task)
        await self.proposal(task, redacted=False, rpc_id=9971)
        with self.assertRaises(BridgeError) as error:
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        self.assertEqual(error.exception.code, "stale_proposal")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 0)

    async def test_native_reviewer_receipt_is_not_subject_to_human_gate(self):
        task = await self.start_task("hold")
        req = await self.proposal(task)
        params = {"threadId": task["thread_id"], "turnId": task["turn_id"], "reviewId": "review-1", "targetItemId": "patch",
            "action": {"type": "applyPatch", "cwd": task["project"], "files": ["source.txt"]}, "startedAtMs": 10,
            "completedAtMs": 20, "decisionSource": "agent", "review": {"status": "approved", "rationale": "secret explanation"}}
        for _ in range(2):
            await self.service.on_message({"method": "item/autoApprovalReview/completed", "params": params})
        receipts = self.service.get(task["id"], view="permissions")["decision_receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["decision_source"], "codex_reviewer")
        self.assertEqual(receipts[0]["proposal_hash"], req["proposal_hash"])
        self.assertNotIn("secret explanation", json.dumps(receipts))
        self.assertEqual(receipts[0]["delivery"], "observed")

    async def test_late_native_review_gets_a_receipt_without_changing_the_new_turn(self):
        task = await self.start_task("hold")
        await self.proposal(task, redacted=False)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        following = await self.service.send(task["id"], "hold")
        params = {"threadId": task["thread_id"], "turnId": task["turn_id"], "reviewId": "late-review", "targetItemId": "patch",
            "action": {"type": "applyPatch", "cwd": task["project"], "files": ["source.txt"]}, "startedAtMs": 10,
            "completedAtMs": 20, "decisionSource": "agent", "review": {"status": "approved", "rationale": "not for the receipt"}}
        for _ in range(2):
            await self.service.on_message({"method": "item/autoApprovalReview/completed", "params": params})
        receipts = self.service.get(task["id"], view="permissions")["decision_receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["turn_id"], task["turn_id"])
        self.assertEqual(receipts[0]["decision_source"], "codex_reviewer")
        self.assertEqual(receipts[0]["delivery"], "observed")
        self.assertNotIn("not for the receipt", json.dumps(receipts))
        self.assertEqual(self.service.get(task["id"])["turn_id"], following["turn_id"])
        self.assertIsNone(self.service.get(task["id"])["last_automatic_review"])

    async def test_receipt_write_failure_prevents_native_decision(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, redacted=False)
        with patch("codex_local_mcp.service.save_receipt", side_effect=OSError("Injected full disk")), \
                patch.object(self.service.backend.process.stdin, "write", wraps=self.service.backend.process.stdin.write) as written:
            with self.assertRaises(OSError):
                await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        self.assertFalse(any(json.loads(call.args[0]).get("result") == {"decision": "accept"} for call in written.call_args_list))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 0)

    async def test_answers_are_absent_from_the_durable_receipt(self):
        task = await self.start_task("question")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        await self.service.respond(req["id"], {"answers": {"choice": {"answers": ["private-answer-not-for-audit"]}}}, human=True)
        saved = self.store.db.execute("SELECT data FROM decision_receipts").fetchone()[0]
        self.assertNotIn("private-answer-not-for-audit", saved)
        self.assertEqual(json.loads(saved)["decision"], "answer")

    async def test_current_reviewer_explanation_resets_with_the_turn(self):
        task = await self.start_task("hold")
        await self.service.settings(task["id"], "current", task["turn_id"], approval_mode="approve_for_me")
        scope = (await self.service.settings(task["id"]))["permission_view"]
        self.assertEqual(scope["requested"]["approval_mode"], "inherit")
        self.assertEqual(scope["effective"]["approval_mode"], "approve_for_me")
        self.assertEqual(scope["effective"]["approvals_reviewer"], "auto_review")
        self.assertEqual(scope["current_turn_overrides"]["turn_id"], task["turn_id"])
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.service.send(task["id"], "hold")
        scope = (await self.service.settings(task["id"]))["permission_view"]
        self.assertEqual(scope["effective"]["approvals_reviewer"], "user")
        self.assertEqual(scope["current_turn_overrides"], {})

    async def test_settings_inherit_is_refused_before_io_and_keeps_existing_permission_explanations(self):
        task = await self.start_task("hold")
        await self.service.settings(task["id"], "current", task["turn_id"], approval_mode="approve_for_me")
        before_task = self.service.task(task["id"])
        before_view = self.service.get(task["id"], view="permissions")["permission_view"]
        before_changes = self.store.db.total_changes
        for scope in ("next", "current"):
            args = {"task_id": task["id"], "scope": scope, "approval_mode": "inherit", "operation_id": "invalid-inherit-" + scope}
            if scope == "current":
                args["expected_turn_id"] = task["turn_id"]
            with self.subTest(scope=scope), patch.object(self.service.backend, "call", side_effect=AssertionError("No native I/O for invalid inherit")):
                for direct in (True, False):
                    with self.subTest(direct=direct):
                        with self.assertRaises(BridgeError) as error:
                            if direct:
                                await self.service.settings(task["id"], scope, task["turn_id"], approval_mode="inherit")
                            else:
                                await dispatch(self.service, "codex_settings", args)
                        self.assertEqual(error.exception.code, "invalid_arguments")
                        self.assertEqual(str(error.exception), "approval_mode inherit applies only when a task is started; choose ask or approve_for_me")
        self.assertEqual(self.store.db.total_changes, before_changes)
        self.assertEqual(self.service.task(task["id"]), before_task)
        self.assertEqual(self.service.get(task["id"], view="permissions")["permission_view"], before_view)
