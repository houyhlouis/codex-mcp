import asyncio
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_local_mcp import mcp
from codex_local_mcp.cli import review as terminal_review
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import (dispatch, result_size, tool_result, RESULT_BUDGET,
                                   FRAME_LIMIT)
from tests.test_service import ServiceHarness


class ResponseBudgetTest(ServiceHarness):
    async def proposal(self, task, patch, item="patch", rpc_id=9000):
        common = {"threadId": task["thread_id"], "turnId": task["turn_id"]}
        await self.service.on_message({"method": "item/started", "params": {**common,
            "item": {"id": item, "type": "fileChange", "changes": [
                {"path": "safe.txt", "kind": {"type": "add"}, "diff": patch}]}}})
        await self.service.on_message({"id": rpc_id, "method": "item/fileChange/requestApproval",
            "params": {**common, "itemId": item}})
        request_id = next(reversed(self.service.raw_requests))
        request = self.store.get("requests", request_id)
        self.assertEqual(request["task_id"], task["id"])
        self.assertEqual(request["params"]["itemId"], item)
        return self.service.public_request(request)

    async def exchange(self, config, method, **kwargs):
        return await dispatch(self.service, "codex_respond" if method == "review" else kwargs["name"],
                              kwargs["args"], human=method == "review")

    async def frame(self, value, version="2025-11-25"):
        adapter = mcp.MCP(self.cfg)
        adapter.version = version
        output = io.BytesIO()
        with patch.object(mcp.sys, "stdout", SimpleNamespace(buffer=output)):
            await adapter.result("budget", tool_result(value, structured=version >= "2025-06-18"))
        raw = output.getvalue()
        self.assertLessEqual(len(raw), FRAME_LIMIT)
        reader = asyncio.StreamReader(limit=FRAME_LIMIT)
        reader.feed_data(raw)
        reader.feed_eof()
        decoded = json.loads(await reader.readline())["result"]
        self.assertEqual(json.loads(decoded["content"][0]["text"]), value)
        if version >= "2025-06-18":
            self.assertEqual(decoded["structuredContent"], value)
        return len(raw)

    async def test_sixteen_two_mib_approvals_fit_complete_old_and_new_frames(self):
        task = await self.start_task("hold")
        for i in range(16):
            await self.proposal(task, "x" * (2 * 1024 * 1024), f"patch-{i}", 9000 + i)
        after = self.store.highwater()
        page = await dispatch(self.service, "codex_events", {"after": after, "task_id": task["id"]})
        self.assertEqual(len(page["pending_requests"]), 16)
        self.assertFalse(page["pending_requests_has_more"])
        self.assertTrue(all(r["details_truncated"] and r["preview_truncated"] for r in page["pending_requests"]))
        self.assertTrue(all("params" not in r for r in page["pending_requests"]))
        for version in ("2024-11-05", "2025-06-18", "2025-11-25"):
            await self.frame(page, version)
        for name, values in (("codex_get", {"task_id": task["id"]}), ("codex_list", {}),
                             ("codex_status", {}), ("codex_requests", {"task_id": task["id"]})):
            result = await dispatch(self.service, name, values)
            self.assertLessEqual(result_size(result), RESULT_BUDGET)
            await self.frame(result)
        request = page["pending_requests"][0]
        full = await dispatch(self.service, "codex_review_request", {"request_id": request["id"]})
        self.assertEqual(len(full["params"]["proposedChanges"][0]["diff"]), 2 * 1024 * 1024)
        await self.frame(full)

    async def test_multibyte_and_escaped_detail_pages_reassemble_exactly(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, '\u754c"\\\n' * 400000)
        first = await dispatch(self.service, "codex_review_request", {"request_id": req["id"], "offset": 0})
        self.assertIn("detail", first)
        parts, offset, digest = [], 0, first["detail_sha256"]
        while True:
            page = await dispatch(self.service, "codex_review_request", {
                "request_id": req["id"], "offset": offset, "limit": 262144})
            await self.frame(page)
            self.assertEqual(page["detail_sha256"], digest)
            parts.append(page["detail"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        text = "".join(parts)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
        self.assertEqual(json.loads(text), self.service.public_request(self.service.review_request(req["id"])))

    async def test_oversized_journal_event_keeps_cursor_until_last_fragment(self):
        payload = {"text": '\u754c"\\\n' * 500000}
        with self.store.transaction():
            sequence = self.store.event(None, "large", payload)
            following = self.store.event(None, "after", {"seen": True})
        offset, fragments, digest = 0, [], None
        while True:
            page = await dispatch(self.service, "codex_events", {"after": 0, "event_offset": offset,
                "event_seq": sequence})
            await self.frame(page)
            fragment = page["events"][0]
            self.assertEqual(fragment["seq"], sequence)
            fragments.append(fragment["fragment"])
            digest = digest or fragment["fragment_sha256"]
            self.assertEqual(fragment["fragment_sha256"], digest)
            if not page["next_event_offset"]:
                self.assertEqual(page["cursor"], sequence)
                break
            self.assertEqual(page["cursor"], 0)
            self.assertTrue(page["has_more"])
            offset = page["next_event_offset"]
        text = "".join(fragments)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
        self.assertEqual(json.loads(text)["data"], payload)
        final = await self.service.events(after=sequence)
        self.assertEqual([event["seq"] for event in final["events"]], [following])

    async def test_soft_oversized_event_is_whole_and_advances(self):
        with self.store.transaction():
            sequence = self.store.event(None, "soft", {"text": "x" * (2 * 1024 * 1024)})
        page = await self.service.events()
        self.assertEqual(page["events"][0]["data"]["text"], "x" * (2 * 1024 * 1024))
        self.assertEqual(page["cursor"], sequence)
        await self.frame(page)

    async def test_hundreds_of_child_requests_remain_discoverable_while_earlier_resolve(self):
        task = await self.start_task("hold")
        parent = self.service.task(task["id"])
        with self.store.transaction():
            for i in range(220):
                child = {**parent, "id": f"child-{i}", "thread_id": f"native-{i}", "status": "idle",
                    "parent_task_id": task["id"], "root_task_id": task["id"], "turn_id": None}
                self.service.lifecycle.create(child)
                self.store.save("requests", {"id": f"request-{i}", "task_id": child["id"], "kind": "parent_question",
                    "status": "pending", "blocking": True, "requires_human": False,
                    "params": {"question": "Which option?"}, "generation": self.service.backend.generation})
        seen, cursor = [], None
        while True:
            page = await dispatch(self.service, "codex_requests", {"task_id": task["id"], "limit": 17,
                **({"cursor": cursor} if cursor else {})})
            await self.frame(page)
            seen.extend(r["id"] for r in page["requests"])
            with self.store.transaction():
                for req in page["requests"]:
                    self.store.update("requests", req["id"], {"status": "resolved"})
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(len(seen), 220)
        self.assertEqual(len(set(seen)), 220)
        children, offset = [], 0
        while True:
            page = await self.service.subagents(task["id"], refresh=False, offset=offset, limit=31)
            await self.frame(page)
            children.extend(c["id"] for c in page["subagents"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(len(set(children)), 220)

    async def test_get_list_and_history_mark_projection_truncation(self):
        task = await self.start_task("hold")
        self.service.lifecycle.project(task["id"], task["turn_id"], {"diff": "x" * (5 * 1024 * 1024)})
        for name, values in (("codex_get", {"task_id": task["id"]}), ("codex_list", {})):
            result = await dispatch(self.service, name, values)
            await self.frame(result)
            record = result if name == "codex_get" else result["tasks"][0]
            self.assertTrue(record["truncated"])
            self.assertIn("/diff", record["truncated_fields"])
        original = self.service.backend.call
        async def large_history(method, params, **kwargs):
            if method == "thread/items/list":
                return {"data": [{"item": {"text": '\u754c"\\' * 2000000}}], "nextCursor": "next"}
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", large_history):
            history = await dispatch(self.service, "codex_history", {"task_id": task["id"], "kind": "items"})
        self.assertTrue(history["data"][0]["truncated"])
        self.assertEqual(history["nextCursor"], "next")
        await self.frame(history)

    async def test_changed_detail_page_disables_acceptance(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, "x" * 100000)
        calls = 0
        async def changed(config, method, **kwargs):
            nonlocal calls
            args = {**kwargs["args"], "offset": kwargs["args"].get("offset", 0), "limit": 1000}
            result = await dispatch(self.service, kwargs["name"], args)
            calls += 1
            if calls == 2:
                result["detail_sha256"] = "changed"
            return result
        with self.assertRaisesRegex(BridgeError, "changed while fetching"):
            await mcp.fetch_review_request(self.cfg, req["id"], changed)

    async def test_complete_large_mcp_proposal_is_shown_before_acceptance(self):
        task = await self.start_task("hold")
        patch_text = "x" * (2 * 1024 * 1024)
        req = await self.proposal(task, patch_text)
        adapter = mcp.MCP(self.cfg)
        adapter.version, adapter.capabilities = "2025-11-25", {"elicitation": {"form": {}}}
        shown = []
        async def present(message):
            raw = (json.dumps(message, ensure_ascii=False) + "\n").encode()
            reader = asyncio.StreamReader(limit=FRAME_LIMIT)
            reader.feed_data(raw)
            reader.feed_eof()
            decoded = json.loads(await reader.readline())
            self.assertIn("\ncontent_redacted: false\nredaction_count: 0\n", decoded["params"]["message"])
            content = json.loads(decoded["params"]["message"].rsplit("\n", 1)[1])
            self.assertEqual(content["params"]["proposedChanges"][0]["diff"], patch_text)
            self.assertEqual(content["proposal_hash"], req["proposal_hash"])
            shown.append(content["id"])
            adapter.pending_ui[message["id"]].set_result({"result": {
                "action": "accept", "content": {"decision": "accept"}}})
        adapter.write = present
        with patch("codex_local_mcp.mcp.rpc", self.exchange):
            result = await adapter.review(req["id"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(shown, [req["id"]])

    async def test_mcp_call_routes_bounded_old_and_structured_results(self):
        task = await self.start_task("hold")
        await self.proposal(task, "x" * 100000)
        for version in ("2024-11-05", "2025-06-18"):
            adapter = mcp.MCP(self.cfg)
            adapter.version = version
            output = io.BytesIO()
            async def ready(config):
                return {}
            with patch("codex_local_mcp.mcp.ensure_broker", ready), patch("codex_local_mcp.mcp.rpc", self.exchange), \
                    patch.object(mcp.sys, "stdout", SimpleNamespace(buffer=output)):
                await adapter.call_tool("pending", {"name": "codex_requests", "arguments": {"task_id": task["id"]}})
            reader = asyncio.StreamReader(limit=FRAME_LIMIT)
            reader.feed_data(output.getvalue())
            reader.feed_eof()
            result = json.loads(await reader.readline())["result"]
            self.assertFalse(result["isError"])
            content = json.loads(result["content"][0]["text"])
            self.assertTrue(content["requests"][0]["details_truncated"])
            self.assertEqual("structuredContent" in result, version >= "2025-06-18")

    async def test_large_complete_terminal_review_and_ui_refusal(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, "x" * (9 * 1024 * 1024))
        adapter = mcp.MCP(self.cfg)
        adapter.version, adapter.capabilities = "2025-11-25", {"elicitation": {"form": {}}}
        # Terminal review below exercises full paging at this size. Reuse its
        # input here to isolate the UI frame refusal from repeated page decoding.
        with patch("codex_local_mcp.mcp.fetch_review_request", AsyncMock(return_value=req)), \
                patch.object(adapter, "write") as shown:
            result = await asyncio.wait_for(adapter.review(req["id"]), 5)
        self.assertEqual(result["status"], "pending")
        self.assertIn("acceptance is disabled", result["next_step"])
        shown.assert_not_called()
        with patch("codex_local_mcp.cli.rpc", self.exchange), patch("codex_local_mcp.cli.sys.stdin.isatty", return_value=True), \
                patch("builtins.print") as printed, patch("builtins.input", return_value="accept"):
            sent = await terminal_review(self.cfg, req["id"])
        self.assertEqual(sent["status"], "sent")
        displayed = json.loads(printed.call_args.args[0])
        self.assertEqual(displayed["params"]["proposedChanges"][0]["diff"], "x" * (9 * 1024 * 1024))
        self.assertEqual(displayed["proposal_hash"], req["proposal_hash"])
