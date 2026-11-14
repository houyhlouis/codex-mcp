"""Proposal identity and path boundaries independent of the native sandbox."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp.cli import review as terminal_review
from codex_local_mcp.mcp import MCP
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.helpers import link_or_skip
from tests.test_service import ServiceHarness


class ApprovalProposalTest(ServiceHarness):
    async def proposal(self, task, changes, item_id="patch", rpc_id=9970):
        common = {"threadId": task["thread_id"], "turnId": task["turn_id"]}
        await self.service.on_message({"method": "item/started", "params": {**common, "item": {
            "id": item_id, "type": "fileChange", "changes": changes}}})
        await self.service.on_message({"id": rpc_id, "method": "item/fileChange/requestApproval", "params": {
            **common, "itemId": item_id}})
        return self.service.requests(task["id"])[-1]

    async def test_rename_destination(self):
        task = await self.start_task("hold")
        request = {"kind": "file_approval", "task_id": task["id"]}
        params = {"proposedChanges": [{"path": "source.txt", "kind": {
            "type": "update", "move_path": str(self.base / "outside.txt")}, "diff": "+ test"}]}
        with self.assertRaisesRegex(BridgeError, "outside"):
            self.service.validate_response(request, params, {"decision": "accept"})

    async def test_empty_permission_denial(self):
        task = await self.start_task("permission")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        response = {"permissions": {"network": {"enabled": False}}, "scope": "turn"}
        result = self.service.validate_response(req, self.service.raw_requests[req["id"]], response)
        self.assertEqual(result, {"permissions": {}, "scope": "turn"})
        self.assertTrue(self.service.is_denial("permissions", result))
        await self.service.respond(req["id"], response)

    async def test_patch_update_rejects_stale_review(self):
        task = await self.start_task("hold")
        change = {"path": "source.txt", "kind": {"type": "update"}, "diff": "+ first"}
        req = await self.proposal(task, [change])
        shown_hash = req["proposal_hash"]
        await self.service.on_message({"method": "item/fileChange/patchUpdated", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "patch",
            "changes": [{**change, "diff": "+ changed"}]}})
        fresh = self.service.public_request(self.service.review_request(req["id"]))
        self.assertGreater(fresh["proposal_revision"], req["proposal_revision"])
        self.assertNotEqual(fresh["proposal_hash"], shown_hash)
        with self.assertRaisesRegex(BridgeError, "stale"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=shown_hash)
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")
        await self.service.on_message({"method": "item/fileChange/patchUpdated", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "patch",
            "changes": [{**change, "kind": {"type": "update", "move_path": str(self.base / "outside")}}]}})
        with self.assertRaisesRegex(BridgeError, "stale"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=shown_hash)
        current = self.service.review_request(req["id"])
        with self.assertRaisesRegex(BridgeError, "outside"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=current["proposal_hash"])

    async def test_file_accept_requires_displayed_hash_on_every_route(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, [{"path": "a.txt", "kind": {"type": "add"}, "diff": "+ a"}])
        sent = []
        original = self.service.backend.respond
        async def record(*args, **kwargs):
            sent.append((args, kwargs))
            await original(*args, **kwargs)
        self.service.backend.respond = record
        for human, relay in ((False, False), (True, False), (False, True)):
            self.service.config = replace(self.cfg, approval_relay=relay)
            with self.subTest(human=human, relay=relay):
                with self.assertRaisesRegex(BridgeError, "stale or unknown"):
                    await dispatch(self.service, "codex_respond", {
                        "request_id": req["id"], "response": {"decision": "accept"}}, human=human)
                self.assertEqual(sent, [])
        self.service.config = self.cfg
        with self.assertRaisesRegex(BridgeError, "Human review"):
            await self.service.respond(req["id"], {"decision": "accept"}, proposal_hash=req["proposal_hash"])
        result = await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        self.assertEqual(len(sent), 1)
        receipt = [e["data"] for e in self.store.events(0, 200)["events"] if e["kind"] == "bridge/response_sent"][-1]
        self.assertEqual(receipt, {"request_id": req["id"], "kind": "file_approval", "human_review": True,
            "generation": self.service.backend.generation, "thread_id": task["thread_id"],
            "turn_id": task["turn_id"], "item_id": "patch", "proposal_revision": req["proposal_revision"],
            "proposal_hash": req["proposal_hash"], "decision": "accept", "decision_source": "human",
            "receipt_id": result["receipt"]["receipt_id"], "decided_at": result["receipt"]["decided_at"]})
        self.assertNotIn("response", receipt)

    async def test_rename_paths_and_unknown_shapes_fail_closed(self):
        task = await self.start_task("hold")
        req = {"kind": "file_approval", "task_id": task["id"]}
        outside = str(self.base / "outside.txt")
        for change, grant_root in (
            ({"path": "a", "kind": {"type": "update", "move_path": "../escape"}, "diff": ""}, None),
            ({"path": "../escape", "kind": {"type": "delete"}, "diff": ""}, None),
            ({"path": outside, "kind": {"type": "add"}, "diff": ""}, None),
            ({"path": "a", "kind": {"type": "update", "move_path": "C:\\other\\a"}, "diff": ""}, None),
            ({"path": "a", "kind": {"type": "future"}, "diff": ""}, None),
            ({"path": "a", "kind": {"type": "update", "other_path": "a"}, "diff": ""}, None),
            ({"path": "a", "kind": {"type": "add", "move_path": "b"}, "diff": ""}, None),
            ({"path": "a", "kind": {"type": "update", "move_path": 12}, "diff": ""}, None),
            ({"path": "a", "kind": {"type": "add"}, "diff": ""}, outside),
            ({"path": "a", "diff": ""}, None),
            ({"path": None, "kind": {"type": "add"}, "diff": ""}, None),
            (None, None),
        ):
            params = {"proposedChanges": [change], "grantRoot": grant_root}
            with self.subTest(params=params):
                with self.assertRaises(BridgeError):
                    self.service.validate_response(req, params, {"decision": "accept"})
                for decision in ("decline", "cancel"):
                    self.assertEqual(self.service.validate_response(req, params, {"decision": decision}), {"decision": decision})

    async def test_symlink_destination_rejected(self):
        task = await self.start_task("hold")
        link_or_skip(self, self.root / "a" / "escape", self.base)
        req = await self.proposal(task, [{"path": "a", "kind": {"type": "update", "move_path": "escape/file"}, "diff": ""}])
        with self.assertRaisesRegex(BridgeError, "outside"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])

    async def test_valid_relative_rename_uses_verified_native_cwd(self):
        task = await self.start_task("hold")
        native_cwd = self.root / "a" / "nested"
        native_cwd.mkdir()
        # Models a verified native child whose cwd is below its project root.
        with self.store.db:
            self.store.update("tasks", task["id"], {"native_cwd": str(native_cwd)})
        req = await self.proposal(task, [{"path": "../source.txt", "kind": {"type": "update", "move_path": "dest.txt"}, "diff": "+ safe"}])
        result = await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        self.assertEqual(result["status"], "sent")

    async def test_request_cwd_must_match_verified_cwd(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, [{"path": "a", "kind": {"type": "add"}, "diff": ""}])
        self.service.raw_requests[req["id"]]["cwd"] = str(self.base)
        with self.assertRaisesRegex(BridgeError, "unverified"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])

    async def test_proposals_do_not_scan_journal_or_collide_across_turns(self):
        task = await self.start_task("hold")
        changes = [{"path": "a", "kind": {"type": "add"}, "diff": "+ first"}]
        req = await self.proposal(task, changes)
        with self.store.db:
            for i in range(210):
                self.store.event(task["id"], "item/started", {"item": {"id": str(i), "type": "agentMessage"}})
        # A request for the same item retains its native proposal despite journal size.
        common = {"threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "patch"}
        await self.service.on_message({"id": 9971, "method": "item/fileChange/requestApproval", "params": common})
        self.assertEqual(self.service.requests(task["id"])[-1]["proposal_hash"], req["proposal_hash"])
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        next_task = await self.service.send(task["id"], "hold")
        await self.service.on_message({"id": 9972, "method": "item/fileChange/requestApproval", "params": {
            "threadId": next_task["thread_id"], "turnId": next_task["turn_id"], "itemId": "patch"}})
        unknown = self.service.requests(task["id"])[-1]
        self.assertIsNone(unknown["proposal_hash"])
        with self.assertRaises(BridgeError):
            await self.service.respond(unknown["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        await self.service.respond(unknown["id"], {"decision": "decline"})
        fresh = await self.proposal(next_task, changes, rpc_id=9973)
        self.assertNotEqual(req["proposal_hash"], fresh["proposal_hash"])
        self.assertGreater(fresh["proposal_revision"], req["proposal_revision"])

    async def test_stale_generation_cannot_approve(self):
        task = await self.start_task("hold")
        changes = [{"path": "a", "kind": {"type": "add"}, "diff": ""}]
        req = await self.proposal(task, changes)
        await self.service.backend.close()
        await self.service.resume(task["id"])
        with self.assertRaisesRegex(BridgeError, "expired"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=req["proposal_hash"])
        self.assertFalse(self.service.file_proposals)

    async def test_empty_permission_subsets_and_real_grants(self):
        task = await self.start_task("hold")
        req = {"kind": "permissions", "task_id": task["id"]}
        requested = {"permissions": {"network": {"enabled": True}, "fileSystem": {"read": ["a"], "write": ["b"],
            "entries": [{"path": {"type": "path", "path": "c"}, "access": "write"}]}}}
        for grants in ({}, None, False, [], {"network": None}, {"network": False}, {"network": []}, {"network": {}},
                       {"network": {"enabled": False}}, {"network": {"enabled": None}}, {"fileSystem": None},
                       {"fileSystem": {}}, {"fileSystem": []}, {"fileSystem": False},
                       {"fileSystem": {"read": [], "write": None, "entries": []}},
                       {"fileSystem": {"read": False, "write": {}}}):
            with self.subTest(grants=grants):
                result = self.service.validate_response(req, requested, {"permissions": grants})
                self.assertEqual(result, {"permissions": {}, "scope": "turn"})
                self.assertTrue(self.service.is_denial("permissions", result))
        self.assertEqual(self.service.validate_response(req, requested, {}), {"permissions": {}, "scope": "turn"})
        for grants in ({"network": {"enabled": True}}, {"network": {}, "fileSystem": {"read": ["a"]}},
                       {"network": {"enabled": False}, "fileSystem": {"write": ["b"]}},
                       {"fileSystem": {"entries": requested["permissions"]["fileSystem"]["entries"]}}):
            with self.subTest(grants=grants):
                result = self.service.validate_response(req, requested, {"permissions": grants})
                self.assertFalse(self.service.is_denial("permissions", result))
        for grants in ({"future": None}, {"network": {"future": False}}, {"fileSystem": {"future": []}},
                       {"network": {"enabled": "true"}}, {"network": {"enabled": 0}},
                       {"fileSystem": {"write": ["not-requested"]}},
                       {"fileSystem": {"entries": [{"path": {"type": "glob", "path": "*"}, "access": "write"}]}}):
            with self.subTest(grants=grants), self.assertRaises(BridgeError):
                self.service.validate_response(req, requested, {"permissions": grants})

    async def test_positive_permission_grants_preserve_restrictive_entries(self):
        task = await self.start_task("hold")
        grants = {"fileSystem": {"entries": [
            {"path": {"type": "path", "path": "build"}, "access": "write"},
            {"path": {"type": "path", "path": "build/private"}, "access": "deny"}]}}
        req = {"kind": "permissions", "task_id": task["id"]}
        result = self.service.validate_response(req, {"permissions": grants}, {"permissions": grants})
        self.assertEqual(result["permissions"], grants)
        self.assertFalse(self.service.is_denial("permissions", result))

    async def review_rpc(self, config, method, **kwargs):
        # Same dispatch and human flag used by the broker's review route.
        return await dispatch(self.service, "codex_respond" if method == "review" else kwargs["name"],
                              kwargs["args"], human=method == "review")

    async def test_mcp_elicitation_preserves_displayed_proposal(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, [{"path": "a", "kind": {"type": "add"}, "diff": "+ first"}])
        adapter = MCP(self.cfg)
        adapter.version, adapter.capabilities = "2025-11-25", {"elicitation": {"form": {}}}
        shown = asyncio.Event()
        async def write(message):
            self.assertIn(req["proposal_hash"], message["params"]["message"])
            self.assertIn('"proposal_revision":', message["params"]["message"])
            self.ui_id = message["id"]
            shown.set()
        adapter.write = write
        with patch("codex_local_mcp.mcp.rpc", self.review_rpc):
            review = asyncio.create_task(adapter.review(req["id"]))
            await asyncio.wait_for(shown.wait(), 5)
            adapter.pending_ui[self.ui_id].set_result({"result": {"action": "accept", "content": {"decision": "accept"}}})
            self.assertEqual((await review)["status"], "sent")

    async def test_terminal_review_preserves_displayed_proposal(self):
        task = await self.start_task("hold")
        req = await self.proposal(task, [{"path": "a", "kind": {"type": "update", "move_path": "b"}, "diff": ""}])
        with patch("codex_local_mcp.cli.rpc", self.review_rpc), patch("codex_local_mcp.cli.sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="accept"), patch("builtins.print") as printed:
            self.assertEqual((await terminal_review(self.cfg, req["id"]))["status"], "sent")
        displayed = json.loads(printed.call_args.args[0])
        self.assertEqual(displayed["proposal_hash"], req["proposal_hash"])

    async def test_patch_update_while_waiting_for_write_lock_rejects_before_send(self):
        task = await self.start_task("file_approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        backend = self.service.backend
        reached = asyncio.Event()
        original = backend.respond
        async def entering(*args, **kwargs):
            reached.set()
            await original(*args, **kwargs)
        backend.respond = entering
        with patch.object(backend.process.stdin, "write", wraps=backend.process.stdin.write) as written:
            async with backend.write_lock:
                responding = asyncio.create_task(self.service.respond(req["id"], {"decision": "accept"},
                    human=True, proposal_hash=req["proposal_hash"]))
                await asyncio.wait_for(reached.wait(), 5)
                self.assertEqual(self.store.get("requests", req["id"])["status"], "answering")
                await self.service.on_message({"method": "item/fileChange/patchUpdated", "params": {
                    "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "patch",
                    "changes": [{"path": "new.txt", "kind": {"type": "add"}, "diff": "+ changed while locked"}]}})
            with self.assertRaisesRegex(BridgeError, "stale or unknown"):
                await responding
            # Automatic workspace checks may write read-only RPCs while the
            # approval waits. No native response, including a grant, may pass.
            self.assertTrue(all("method" in json.loads(call.args[0]) for call in written.call_args_list))
        fresh = self.service.review_request(req["id"])
        self.assertNotEqual(fresh["proposal_hash"], req["proposal_hash"])
        await self.service.respond(req["id"], {"decision": "accept"}, human=True, proposal_hash=fresh["proposal_hash"])
        self.assertEqual((await self.until(task["id"], ("completed",)))["status"], "completed")
