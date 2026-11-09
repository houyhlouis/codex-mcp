import asyncio
import json

from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch, arguments
from tests.test_service import ServiceHarness
from tests.helpers import link_or_skip, read_fake_history


class ExtensionsTest(ServiceHarness):
    async def idle(self):
        task = await self.start_task()
        return await self.until(task["id"])

    async def test_native_approve_for_me_is_explicit_and_verified(self):
        self.service.projects("create", str(self.root / "a"))
        task = await dispatch(self.service, "codex_start", {"workdir": str(self.root / "a"), "prompt": "echo",
            "approval_mode": "approve_for_me", "request_id": "auto"})
        self.assertEqual(task["approval_mode"], "approve_for_me")
        self.assertEqual(task["approvals_reviewer"], "auto_review")
        native = read_fake_history(self.state)[task["thread_id"]]
        self.assertEqual(native["options"]["approvalsReviewer"], "auto_review")
        self.assertEqual(native["options"]["approvalPolicy"], "on-request")
        self.assertNotIn("sandbox", native["options"])

    async def test_next_turn_settings_and_plan_mode(self):
        task = await self.idle()
        result = await dispatch(self.service, "codex_settings", {"task_id": task["id"], "scope": "next",
            "model": "small", "approval_mode": "approve_for_me", "collaboration_mode": "plan", "request_id": "settings"})
        self.assertEqual(result["model"], "small")
        self.assertEqual(result["effort"], "low")
        self.assertEqual(result["collaboration_mode"], "plan")
        await self.service.send(task["id"], "next")
        self.assertEqual((await self.until(task["id"]))["approval_mode"], "approve_for_me")

    async def test_current_turn_settings_require_live_turn(self):
        task = await self.start_task("hold")
        with self.assertRaises(BridgeError):
            await self.service.settings(task["id"], "current", "wrong", approval_mode="approve_for_me")
        changed = await self.service.settings(task["id"], "current", task["turn_id"], approval_mode="approve_for_me")
        self.assertEqual(changed["active_settings"]["approval_mode"], "approve_for_me")
        self.assertEqual(changed["approval_mode"], "ask")
        with self.assertRaises(BridgeError):
            await self.service.settings(task["id"], "current", task["turn_id"], access="read")

    async def test_service_tier_validated_against_catalog(self):
        task = await self.idle()
        await self.service.settings(task["id"], "next", service_tier="priority")
        with self.assertRaises(BridgeError):
            await self.service.settings(task["id"], "next", service_tier="invented")

    async def test_native_child_approval_reaches_parent(self):
        parent = await self.start_task("native_children")
        for _ in range(100):
            requests = self.service.requests(parent["id"])
            if requests:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(requests)
        req = requests[0]
        self.assertNotEqual(req["task_id"], parent["id"])
        self.assertEqual(req["parent_task_id"], parent["id"])
        child = self.service.get(req["task_id"])
        self.assertTrue(child["native_child"])
        self.assertEqual(child["project"], parent["project"])
        await self.service.respond(req["id"], {"decision": "accept"}, human=True)
        await self.until(parent["id"], ("completed",))
        page = await self.service.events(task_id=parent["id"])
        self.assertTrue(any(e["task_id"] == child["id"] for e in page["events"]))
        page = await self.service.events(task_id=parent["id"], include_descendants=False)
        self.assertTrue(all(e["task_id"] == parent["id"] for e in page["events"]))
        listed = await self.service.subagents(parent["id"])
        self.assertEqual(len(listed["subagents"]), 1)

    async def test_native_grandchild_is_verified_transitively(self):
        parent = await self.start_task("native_grandchildren")
        for _ in range(100):
            requests = self.service.requests(parent["id"])
            if requests:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(requests)
        self.assertEqual(len(self.service.family_ids(parent["id"])), 3)
        self.assertEqual(requests[0]["root_task_id"], parent["id"])
        await self.service.respond(requests[0]["id"], {"decision": "cancel"})
        await self.until(parent["id"], ("completed",))

    async def test_native_children_resubscribe_after_backend_restart(self):
        parent = await self.start_task("native_children")
        for _ in range(100):
            requests = self.service.requests(parent["id"])
            if requests:
                break
            await asyncio.sleep(0.02)
        await self.service.respond(requests[0]["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))
        child_id = requests[0]["task_id"]
        await self.service.backend.close()
        self.assertNotIn(child_id, self.service.loaded)
        await self.service.subagents(parent["id"])
        self.assertIn(child_id, self.service.loaded)
        self.assertEqual(len(self.service.family_ids(parent["id"])), 2)

    async def test_child_outside_project_is_not_adopted(self):
        parent = await self.start_task("native_outside")
        await self.until(parent["id"], ("completed",))
        self.assertEqual(len(self.service.family_ids(parent["id"])), 1)
        self.assertFalse(self.service.requests())
        with self.assertRaisesRegex(BridgeError, "outside"):
            await self.service.delete(parent["id"], parent["thread_id"])

    async def test_child_direct_input_capability_is_respected(self):
        parent = await self.start_task("native_children")
        for _ in range(100):
            children = self.service.get(parent["id"])["subagents"]
            if children:
                break
            await asyncio.sleep(0.02)
        child = self.service.task(children[0]["id"])
        child["can_accept_direct_input"] = False
        with self.store.db:
            self.store.save("tasks", child)
        with self.assertRaisesRegex(BridgeError, "parent"):
            await self.service.send(child["id"], "message")

    async def test_fork_rename_archive_restore_delete(self):
        original = await self.idle()
        fork = await dispatch(self.service, "codex_fork", {"task_id": original["id"], "name": "branch", "request_id": "fork"})
        self.assertNotEqual(original["thread_id"], fork["thread_id"])
        self.assertEqual(fork["forked_from_task_id"], original["id"])
        self.assertEqual(len((await self.service.history(fork["id"]))["data"]), 1)
        await self.service.rename(fork["id"], "renamed")
        self.assertEqual(self.service.get(fork["id"])["name"], "renamed")
        await self.service.archive(fork["id"])
        with self.assertRaises(BridgeError):
            await self.service.send(fork["id"], "cannot run archived")
        await self.service.archive(fork["id"], "unarchive")
        with self.assertRaises(BridgeError):
            await self.service.delete(fork["id"], "wrong")
        await self.service.delete(fork["id"], fork["thread_id"])
        self.assertEqual(self.service.get(fork["id"])["status"], "deleted")
        with self.assertRaises(BridgeError):
            await self.service.resume(fork["id"])
        self.assertTrue((await self.service.events(task_id=fork["id"]))["events"])

    async def test_active_lifecycle_changes_are_rejected(self):
        task = await self.start_task("hold")
        for call in (lambda: self.service.fork(task["id"]), lambda: self.service.archive(task["id"]),
                     lambda: self.service.delete(task["id"], task["thread_id"])):
            with self.assertRaises(BridgeError):
                await call()

    async def test_queue_all_mutations_and_start(self):
        task = await self.idle()
        one = await self.service.queue(task["id"], "add", "one")
        two = await self.service.queue(task["id"], "add", "two")
        first, second = one["queuedSubmission"]["id"], two["queuedSubmission"]["id"]
        await self.service.queue(task["id"], "update", "changed", first)
        await self.service.queue(task["id"], "reorder", ordered_ids=[second, first])
        rows = (await self.service.queue(task["id"]))["data"]
        self.assertEqual([q["id"] for q in rows], [second, first])
        await self.service.queue(task["id"], "delete", queued_id=second)
        await self.service.queue(task["id"], "start", queued_id=first)
        self.assertIn("changed", (await self.until(task["id"]))["summary"])

    async def test_review_compact_revert_and_diff(self):
        task = await self.idle()
        self.assertIn("example", (await self.service.diff(task["id"]))["diff"])
        await self.service.review(task["id"], "custom", instructions="Check correctness")
        reviewed = await self.until(task["id"])
        self.assertIn("review", reviewed["summary"])
        await self.service.compact(task["id"])
        await self.until(task["id"])
        with self.assertRaises(BridgeError):
            await self.service.revert(task["id"], reviewed["turn_id"], False)
        await self.service.revert(task["id"], reviewed["turn_id"], True)
        self.assertEqual(len((await self.service.history(task["id"]))["data"]), 1)

    async def test_goal_requires_explicit_intent_and_no_implicit_budget(self):
        task = await self.idle()
        with self.assertRaises(BridgeError):
            await self.service.goal(task["id"], "set", objective="Work forever")
        args = arguments("codex_goal", {"task_id": task["id"], "action": "set", "objective": "Finish", "user_requested": True, "request_id": "goal"})
        self.assertNotIn("token_budget", args)
        result = await dispatch(self.service, "codex_goal", args)
        self.assertNotIn("tokenBudget", result["goal"])
        await self.service.goal(task["id"], "clear", user_requested=True)
        self.assertIsNone((await self.service.goal(task["id"]))["goal"])

    async def test_rich_input_confined_and_skills_catalog_validated(self):
        task = await self.idle()
        project = self.root / "a"
        (project / "context.txt").write_text("Reference context")
        (project / "image.png").write_bytes(b"fixture-image")
        inputs = await self.service.build_input(task, "look", ["image.png"], ["context.txt"], ["fixture-skill"])
        self.assertEqual([v["type"] for v in inputs], ["text", "text", "localImage", "skill"])
        snapshot = __import__("pathlib").Path(inputs[2]["path"])
        self.assertEqual(snapshot.parent, self.state / "images")
        self.assertEqual(snapshot.read_bytes(), b"fixture-image")
        (project / "image.png").unlink()
        link_or_skip(self, project / "image.png", self.config_file)
        self.assertEqual(snapshot.read_bytes(), b"fixture-image")
        with self.assertRaises(BridgeError):
            await self.service.build_input(task, "look", ["image.png"])
        link_or_skip(self, project / "escape", self.base)
        for path in ["../config.toml", str(self.config_file), "escape/config.toml"]:
            with self.assertRaises(BridgeError):
                await self.service.build_input(task, "look", context_files=[path])
        with self.assertRaises(BridgeError):
            await self.service.build_input(task, "look", skills=["invented"])

    async def test_scoped_native_session_discovery_and_import(self):
        task = await self.idle()
        native = await self.service.backend.call("thread/start", await self.service.thread_options(task))
        key = native["thread"]["id"]
        listing = await self.service.sessions(workdir=str(self.root / "a"))
        self.assertIn(key, [t["id"] for t in listing["sessions"]])
        with self.assertRaises(BridgeError):
            await self.service.sessions(workdir=str(self.root / "a"), action="import", thread_id=key)
        imported = await self.service.sessions(workdir=str(self.root / "a"), action="import", thread_id=key, confirm_stopped=True)
        self.assertEqual(imported["thread_id"], key)
        self.service.projects("create", str(self.root / "b"))
        with self.assertRaises(BridgeError):
            await self.service.sessions(workdir=str(self.root / "b"), action="import", thread_id=key, confirm_stopped=True)

    async def test_capabilities_skills_usage_and_terminals(self):
        task = await self.idle()
        self.assertIn("approve_for_me", (await self.service.capabilities(task["id"]))["approval_modes"])
        self.assertTrue((await self.service.skills(task["id"]))["data"])
        self.assertIn("rateLimits", await self.service.usage())
        self.assertEqual((await self.service.terminals(task["id"]))["data"], [])
        with self.assertRaises(BridgeError):
            await self.service.terminals(task["id"], "terminate")

    async def test_new_controls_do_not_accept_raw_rpc_or_full_access(self):
        for tool, args in [("codex_settings", {"task_id": "x", "scope": "next", "access": "danger-full-access"}),
                           ("codex_settings", {"task_id": "x", "approval_mode": "never"}),
                           ("codex_terminals", {"task_id": "x", "action": "exec", "command": "anything"})]:
            with self.assertRaises(BridgeError):
                arguments(tool, args)
        task = await self.idle()
        with self.assertRaisesRegex(BridgeError, "request_id"):
            await dispatch(self.service, "codex_settings", {"task_id": task["id"], "scope": "next", "effort": "high"})
