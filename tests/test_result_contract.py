"""Versioned results, identities and recovery errors use one additive contract."""

import asyncio
import ast
import hashlib
import json
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from codex_local_mcp.broker import response_error, rpc
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import (BY_NAME, TOOLS, arguments, dispatch, error_envelope,
    ERROR_RESULT, result_size, tool_catalog, validate)
from tests.mcp_wire import RecordingMCP
from tests.test_service import ServiceHarness
from tests.helpers import thread_wakeups


class ContractSchemaTest(unittest.TestCase):
    def test_every_explicit_error_code_is_registered(self):
        package = Path(__file__).resolve().parents[1] / "codex_local_mcp"
        codes = set()
        for path in package.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.keyword) and node.arg == "code":
                    values = [node.value]
                    while values:
                        value = values.pop()
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            codes.add(value.value)
                        elif isinstance(value, ast.IfExp):
                            values.extend((value.body, value.orelse))
        self.assertTrue(codes)
        self.assertLessEqual(codes, set(BridgeError.CODES))
    def test_catalog_and_error_registry_snapshot(self):
        fixture = json.loads((Path(__file__).with_name("fixtures") / "result-contract.json").read_text())
        self.assertEqual(fixture["result_version"], 1)
        self.assertEqual(fixture["errors"], {code: {"when": row[0], "retryable": row[1], "uncertain": row[2], "next_steps": list(row[3])}
            for code, row in BridgeError.CODES.items()})
        self.assertEqual(len(TOOLS), 34)
        for descriptor in TOOLS:
            self.assertIn("outputSchema", descriptor)
            self.assertEqual(descriptor["annotations"]["readOnlyHint"], descriptor["annotations"]["idempotentHint"])
            for code in BridgeError.CODES:
                value = error_envelope(BridgeError("fixture", code=code))
                validate(value["error"], ERROR_RESULT)
                validate(value, descriptor["outputSchema"])
                self.assertEqual(response_error({"error": "fixture", "error_data": value["error"]}).as_dict(), value["error"])

    def test_old_catalogs_omit_output_schemas(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            self.assertEqual(all("outputSchema" in t for t in tool_catalog(version)), version >= "2025-06-18")

    def test_inapplicable_schema_defaults_are_dropped(self):
        for view in ("default", "compact"):
            self.assertNotIn("offset", arguments("codex_get", {"task_id": "t", "view": view, "offset": 0}))
        for name, fields in (("codex_queue", {"task_id": "t", "action": "add", "prompt": "q"}),
                             ("codex_terminals", {"task_id": "t", "action": "clean"}),
                             ("codex_sessions", {"action": "import", "workdir": "/project", "thread_id": "n", "confirm_stopped": True})):
            with self.subTest(tool=name):
                self.assertNotIn("limit", arguments(name, {**fields, "limit": 50}))
                with self.assertRaisesRegex(BridgeError, "argument limit does not apply"):
                    arguments(name, {**fields, "limit": 49})

    def test_alias_conflicts_and_action_fields_fail_precisely(self):
        for name, alias in (("codex_start", "operation_id"), ("codex_respond", "pending_request_id")):
            supplied = {"prompt": "x"} if name == "codex_start" else {"response": {"decision": "decline"}}
            with self.assertRaisesRegex(BridgeError, alias + " and request_id must match") as caught:
                arguments(name, {**supplied, alias: "a", "request_id": "b"})
            self.assertEqual(caught.exception.code, "invalid_arguments")
            self.assertEqual(arguments(name, {**supplied, alias: "a", "request_id": "a"})["request_id"], "a")
        for name, args, field in (
                ("codex_projects", {"action": "list", "path": "/unused"}, "path"),
                ("codex_settings", {"task_id": "t", "scope": "get", "model": "x"}, "model"),
                ("codex_queue", {"task_id": "t", "action": "delete", "queued_id": "q", "prompt": "x"}, "prompt"),
                ("codex_terminals", {"task_id": "t", "action": "clean", "process_id": "p"}, "process_id"),
                ("codex_goal", {"task_id": "t", "action": "clear", "objective": "x"}, "objective"),
                ("codex_sessions", {"workdir": "/unused", "action": "list", "thread_id": "t"}, "thread_id")):
            with self.subTest(tool=name), self.assertRaisesRegex(BridgeError, "argument " + field + " does not apply"):
                arguments(name, args)
        with self.assertRaisesRegex(BridgeError, "argument process_id is required"):
            arguments("codex_terminals", {"task_id": "t", "action": "terminate"})
        with self.assertRaises(BridgeError) as goal:
            arguments("codex_goal", {"task_id": "t", "action": "set", "objective": "Work"})
        self.assertEqual(goal.exception.code, "user_request_required")
        with self.assertRaisesRegex(BridgeError, "at least one setting"):
            arguments("codex_settings", {"task_id": "t", "scope": "next", "operation_id": "unused"})


class ResultContractTest(ServiceHarness):
    def _tearDownAsyncioRunner(self):
        loop = getattr(self, "worktree_loop", None)
        if loop is not None and not loop.is_closed():
            with thread_wakeups(loop):
                return super()._tearDownAsyncioRunner()
        return super()._tearDownAsyncioRunner()

    async def test_dropped_defaults_never_enter_handler_or_fingerprint(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        args = {"task_id": task["id"], "action": "clean", "operation_id": "default-retry"}
        with patch.object(self.service, "terminals", AsyncMock(return_value={"cleaned": True})) as handler:
            first = await dispatch(self.service, "codex_terminals", {**args, "limit": 50})
            again = await dispatch(self.service, "codex_terminals", args)
        self.assertEqual(first, again)
        handler.assert_awaited_once_with(task_id=task["id"], action="clean")
        canonical = {"task_id": task["id"], "action": "clean", "request_id": "default-retry"}
        expected = hashlib.sha256(json.dumps(["codex_terminals", canonical], sort_keys=True).encode()).hexdigest()
        self.assertEqual(self.store.db.execute("SELECT fingerprint FROM operations WHERE id='default-retry'").fetchone()[0], expected)

    async def test_unexpected_failures_never_claim_a_known_outcome(self):
        self.assertEqual(error_envelope(RuntimeError("failed"))["error"]["code"], "internal_error")
        self.assertTrue(error_envelope(RuntimeError("failed"))["error"]["uncertain"])
        async def fail():
            raise RuntimeError("Unexpected local failure")
        with self.assertRaises(RuntimeError):
            await self.service.operation("unexpected", "internal", {}, fail)
        with self.assertRaises(BridgeError) as repeated:
            await self.service.operation("unexpected", "internal", {}, fail)
        self.assertEqual(repeated.exception.code, "internal_error")
        self.assertTrue(repeated.exception.uncertain)
        self.assertFalse(repeated.exception.retryable)

    async def test_unknown_project_identifies_the_selector_without_native_io(self):
        with patch.object(self.service.backend, "call", AsyncMock()) as native:
            with self.assertRaises(BridgeError) as caught:
                await dispatch(self.service, "codex_start", {"project_id": "missing-project", "prompt": "echo", "operation_id": "project-error"})
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(caught.exception.ids, {"project_id": "missing-project", "operation_id": "project-error"})
        native.assert_not_awaited()

    async def test_pipe_rpc_preserves_errors_and_missing_broker_bootstrap(self):
        args = {"task_id": "task", "operation_id": "operation"}
        original = BridgeError("stale", code="stale_turn", ids=args)
        packet = json.dumps({"error": str(original), "error_data": original.as_dict()}).encode()
        with patch("codex_local_mcp.broker.IS_WINDOWS", True):
            with patch("codex_local_mcp.windows.exchange", AsyncMock(return_value=packet)):
                with self.assertRaises(BridgeError) as restored:
                    await rpc(self.cfg, "tool", name="codex_interrupt", args=args)
                self.assertEqual(restored.exception.as_dict(), original.as_dict())
            with patch("codex_local_mcp.windows.exchange", AsyncMock(side_effect=BridgeError("Pipe disconnected"))):
                with self.assertRaises(BridgeError) as disconnected:
                    await rpc(self.cfg, "tool", name="codex_interrupt", args=args)
                self.assertEqual(disconnected.exception.code, "transport_unavailable")
                self.assertTrue(disconnected.exception.uncertain)
                self.assertEqual(disconnected.exception.ids, args)
            with patch("codex_local_mcp.windows.exchange", AsyncMock(side_effect=FileNotFoundError("No broker"))):
                with self.assertRaises(FileNotFoundError):
                    await rpc(self.cfg, "tool", name="codex_interrupt", args=args)

    async def checked(self, tool_name, **args):
        result = await dispatch(self.service, tool_name, args)
        validate(result, BY_NAME[tool_name]["outputSchema"], "result")
        self.seen.add(tool_name)
        return result

    async def test_real_results_of_every_tool_validate(self):
        from codex_local_mcp import worktrees as git
        from tests.test_managed_worktrees import isolated_git
        self.enterContext(isolated_git(self.base))
        self.enterContext(thread_wakeups())
        self.worktree_loop = asyncio.get_running_loop()
        self.service.config = replace(self.cfg, worktrees=True)
        self.seen = set()
        async def call(tool_name, **args):
            if "operation_id" in BY_NAME[tool_name]["inputSchema"]["properties"] and not (
                    tool_name in ("codex_settings", "codex_goal", "codex_queue", "codex_terminals", "codex_sessions") and
                    args.get("scope", args.get("action", "get" if tool_name in ("codex_settings", "codex_goal") else "list")) in ("get", "list")):
                args.setdefault("operation_id", "contract-" + str(len(self.store.db.execute("SELECT id FROM operations").fetchall())))
            return await self.checked(tool_name, **args)
        project = await call("codex_projects", action="create", path=str(self.root / "contract"))
        for args in (["init", "--initial-branch=main"], ["config", "user.name", "Offline"],
                     ["config", "user.email", "offline@example.invalid"], ["commit", "--allow-empty", "-m", "fixture"]):
            await asyncio.to_thread(git._git, args, project["path"], write=True)
        await self.checked("codex_worktrees", action="plan", project_id=project["id"], name="contract")
        await call("codex_projects")
        await call("codex_status")
        await call("codex_models")
        task = await call("codex_start", project_id=project["id"], prompt="echo")
        tid = task["id"]
        await self.until(tid, ("completed",))
        for name in ("codex_get", "codex_history", "codex_requests", "codex_capabilities", "codex_settings", "codex_subagents", "codex_skills", "codex_diff", "codex_goal", "codex_queue", "codex_terminals"):
            await call(name, task_id=tid)
        await call("codex_list")
        await self.checked("codex_plans", action="list")
        await call("codex_events", task_id=tid)
        await call("codex_usage")
        await call("codex_sessions", project_id=project["id"])
        await call("codex_rename", task_id=tid, name="Contract")
        await call("codex_resume", task_id=tid)
        await call("codex_send", task_id=tid, prompt="parent")
        pending = (await self.until(tid, ("waiting",)))["pending_requests"][0]
        await call("codex_review_request", pending_request_id=pending["id"], offset=0)
        await call("codex_respond", pending_request_id=pending["id"], response={"text": "Offline checks"})
        await self.until(tid, ("completed",))
        await call("codex_delegate", task_id=tid, prompt="Offline delegation")
        await self.until(tid, ("completed",))
        await call("codex_send", task_id=tid, prompt="hold")
        active = self.service.get(tid)
        await call("codex_interrupt", task_id=tid, expected_turn_id=active["turn_id"])
        await self.until(tid, ("interrupted",))
        await call("codex_review", task_id=tid)
        await self.until(tid, ("completed",))
        await call("codex_compact", task_id=tid)
        await self.until(tid, ("completed",))
        fork = await call("codex_fork", task_id=tid)
        await call("codex_archive", task_id=fork["id"])
        await call("codex_archive", task_id=fork["id"], action="unarchive")
        await call("codex_delete", task_id=fork["id"], confirm_thread_id=fork["thread_id"])
        turns = await call("codex_history", task_id=tid)
        await call("codex_revert", task_id=tid, before_turn_id=turns["data"][-1]["id"], confirm_history_only=True)
        self.assertEqual(self.seen, set(BY_NAME))
        self.assertTrue(self.store.check_projections()["consistent"])

    async def test_operation_aliases_share_work_and_errors_keep_context(self):
        self.service.projects("create", str(self.root / "alias"))
        args = {"workdir": str(self.root / "alias"), "prompt": "hold"}
        first = await dispatch(self.service, "codex_start", {**args, "operation_id": "same"})
        again = await dispatch(self.service, "codex_start", {**args, "request_id": "same"})
        self.assertEqual(first, again)
        self.assertEqual(first["operation_id"], "same")
        self.assertEqual(first["request_id_kind"], "operation")
        with self.assertRaises(BridgeError) as caught:
            await dispatch(self.service, "codex_interrupt", {"task_id": first["id"], "expected_turn_id": "stale", "operation_id": "wrong"})
        self.assertEqual(caught.exception.code, "stale_turn")
        self.assertEqual(caught.exception.ids, {"operation_id": "wrong", "task_id": first["id"], "expected_turn_id": "stale"})
        with self.assertRaises(BridgeError) as conflict:
            await dispatch(self.service, "codex_start", {**args, "prompt": "other", "operation_id": "same"})
        self.assertEqual(conflict.exception.code, "operation_conflict")

    async def test_busy_admission_identifies_the_operation_before_a_task_exists(self):
        self.service.projects("create", str(self.root / "admission"))
        gate = self.gate_call("model", "model/list", before=True)
        args = {"workdir": str(self.root / "admission"), "prompt": "hold"}
        first = asyncio.create_task(dispatch(self.service, "codex_start", {**args, "operation_id": "admitting"}))
        await gate.wait()
        try:
            with self.assertRaises(BridgeError) as caught:
                await dispatch(self.service, "codex_start", {**args, "operation_id": "refused"})
            data = caught.exception.as_dict()
            self.assertEqual(data["code"], "workspace_busy")
            self.assertEqual(data["ids"]["operation_ids"], ["admitting"])
            self.assertTrue(data["ids"]["family_ids"])
            self.assertTrue(data["pending_admissions"])
        finally:
            gate.release.set()
            await asyncio.wait_for(first, 5)

    async def test_legacy_operation_fingerprints_survive_action_validation(self):
        for name, fields in (("codex_queue", {"action": "add", "prompt": "queued"}),
                             ("codex_terminals", {"action": "clean"}),
                             ("codex_sessions", {"action": "import", "thread_id": "native", "confirm_stopped": True, "archived": False})):
            with self.subTest(tool=name):
                key = "legacy-" + name
                args = {"task_id": "task", **fields, "request_id": key} if name != "codex_sessions" else {"workdir": str(self.root), **fields, "request_id": key}
                legacy = {**args, "limit": 50}
                fingerprint = hashlib.sha256(json.dumps([name, legacy], sort_keys=True).encode()).hexdigest()
                self.store.db.execute("INSERT INTO operations VALUES (?,?, 'done',?,NULL)", (key, fingerprint, json.dumps({"saved": True})))
                result = await dispatch(self.service, name, args)
                self.assertTrue(result["saved"])
                self.assertEqual(result["operation_id"], key)

    async def test_lost_ack_error_and_stale_request_are_structured_without_retry(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("lost-queue", "thread/queue/add", error=TimeoutError("Lost queue acknowledgement"))
        args = {"task_id": task["id"], "action": "add", "prompt": "queued", "operation_id": "lost"}
        adding = asyncio.create_task(dispatch(self.service, "codex_queue", args))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await adding
        with self.assertRaises(BridgeError) as caught:
            await dispatch(self.service, "codex_queue", args)
        self.assertEqual(caught.exception.code, "native_timeout")
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.ids["operation_id"], "lost")
        self.assertEqual(len((await self.service.queue(task["id"]))["data"]), 1)
        waiting = await self.start_task("parent", path="questions", key="question")
        request = (await self.until(waiting["id"], ("waiting",)))["pending_requests"][0]
        await dispatch(self.service, "codex_respond", {"pending_request_id": request["id"], "response": {"text": "answer"}})
        with self.assertRaises(BridgeError) as stale:
            await dispatch(self.service, "codex_respond", {"request_id": request["id"], "response": {"text": "answer"}})
        self.assertEqual(stale.exception.code, "stale_request")

    async def test_compact_views_skip_large_bodies_and_preserve_cursors(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.store.update("tasks", task["id"], {"summary": "large" * 10000, "diff": "x" * 100000})
        with patch.object(self.store, "get", wraps=self.store.get) as read:
            compact = await dispatch(self.service, "codex_get", {"task_id": task["id"], "view": "compact"})
            self.assertFalse(any(c.args[0] == "tasks" for c in read.call_args_list))
        self.assertNotIn("summary", compact)
        self.assertNotIn("diff", compact)
        self.assertTrue(compact["has_diff"])
        normal = await dispatch(self.service, "codex_get", {"task_id": task["id"]})
        self.assertLess(result_size(compact), result_size(normal) // 5)
        cursor = self.store.highwater()
        self.store.event(task["id"], "large", {"diff": "escaped\"\n雪" * 100000})
        with patch.object(self.store, "get", wraps=self.store.get) as read:
            page = await dispatch(self.service, "codex_events", {"task_id": task["id"], "after": cursor, "view": "compact", "limit": 1})
        self.assertFalse(any(c.args[0] == "tasks" for c in read.call_args_list))
        self.assertEqual(page["cursor"], cursor + 1)
        self.assertTrue(page["events"][0]["data_omitted"])
        self.assertNotIn("data", page["events"][0])
        self.assertLess(result_size(page), 10000)
        listing = await dispatch(self.service, "codex_list", {"view": "compact"})
        self.assertNotIn("summary", listing["tasks"][0])

    async def test_redaction_is_explicit_on_summary_detail_and_patch_refresh(self):
        task = await self.start_task("file_approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        params = self.service.raw_requests[req["id"]]
        changes = [{"path": "safe.txt", "kind": {"type": "update"}, "diff": "+ password=never-display"}]
        self.service.update_proposal(self.service.task(task["id"]), params, params["itemId"], changes)
        summary = self.service.request_page(task["id"])["requests"][0]
        detail = await dispatch(self.service, "codex_review_request", {"pending_request_id": req["id"]})
        for result in (summary, detail):
            self.assertTrue(result["content_redacted"])
            self.assertEqual(result["redaction_count"], 1)
            self.assertNotIn("never-display", json.dumps(result))
        with self.assertRaises(BridgeError) as caught:
            await dispatch(self.service, "codex_respond", {"pending_request_id": req["id"], "proposal_hash": detail["proposal_hash"], "response": {"decision": "accept"}})
        self.assertEqual(caught.exception.code, "person_review_required")

    async def test_optional_missing_read_explains_unverified_workspace(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        self.service.backend.unavailable_methods.add("thread/queue/list")
        await self.service.reconcile_claims(task["id"])
        with self.assertRaises(BridgeError) as caught:
            self.service.capacity(task["project"])
        error = caught.exception.as_dict()
        self.assertEqual(error["code"], "workspace_read_unavailable")
        self.assertTrue(error["uncertain"])
        self.assertIn("optional native read API thread/queue/list is unavailable", error["message"])
        self.assertEqual(error["unavailable_methods"], ["thread/queue/list"])


class MCPResultContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_errors_and_schemas_keep_old_and_modern_protocol_semantics(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            adapter = RecordingMCP()
            adapter.version, adapter.ready = version, True
            error = BridgeError("Lost native acknowledgement", code="native_timeout", ids={"task_id": "t"})
            with patch("codex_local_mcp.mcp.ensure_broker", AsyncMock()), patch("codex_local_mcp.mcp.rpc", AsyncMock(side_effect=error)):
                await adapter.call_tool("call", {"name": "codex_usage", "arguments": {}})
            result = adapter.frames[-1]["message"]["result"]
            self.assertTrue(result["isError"])
            body = json.loads(result["content"][0]["text"])
            self.assertEqual(body["error"], {**error.as_dict(), "retryable": True})
            validate(body, BY_NAME["codex_usage"]["outputSchema"])
            if version >= "2025-06-18":
                self.assertEqual(result["structuredContent"], body)
            else:
                self.assertNotIn("structuredContent", result)
            await adapter.handle({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
            self.assertEqual(adapter.frames[-1]["message"]["result"]["tools"], tool_catalog(version))
