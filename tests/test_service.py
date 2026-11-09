import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_local_mcp.config import load_config
from codex_local_mcp.security import BridgeError, project_path, redact
from codex_local_mcp.service import Service
from codex_local_mcp.store import Store
from codex_local_mcp.tools import arguments, dispatch
from tests.helpers import install_fake_codex, write_config, link_or_skip, temp_root


class ServiceHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cmcp-", dir=temp_root())
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "projects"
        self.root.mkdir()
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        fixture = install_fake_codex(self.base)
        self.config_file = self.base / "config.toml"
        write_config(self.config_file, self.state, fixture)
        self.cfg = load_config(str(self.config_file))
        self.store = Store(self.state)
        self.service = Service(self.cfg, self.store)

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.temp.cleanup()

    async def start_task(self, prompt="echo", path="a", key="start"):
        self.service.projects("create", str(self.root / path))
        return await dispatch(self.service, "codex_start", {"workdir": str(self.root / path),
            "prompt": prompt, "request_id": key})

    async def until(self, task_id, states=("completed", "failed", "interrupted", "waiting", "needs_resume")):
        for _ in range(100):
            task = self.service.get(task_id)
            if task["status"] in states:
                return task
            await asyncio.sleep(0.02)
        self.fail("Task did not reach expected state")

class ServiceTest(ServiceHarness):
    async def test_complete_summary_and_evidence(self):
        task = await self.start_task()
        done = await self.until(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertIn("Fixture summary", done["summary"])
        self.assertEqual(done["commands"][0]["exitCode"], 0)
        self.assertEqual(done["files"], ["sample.py"])

    async def test_dynamic_models_and_efforts(self):
        first = await self.service.models(limit=1)
        second = await self.service.models(first["nextCursor"], limit=1)
        self.assertEqual(second["data"][0]["model"], "small")
        with self.assertRaises(BridgeError):
            await self.service.choose_model("invented")
        with self.assertRaises(BridgeError):
            await self.service.choose_model("small", "high")
        self.assertEqual(await self.service.choose_model("small"), ("small", "low"))

    async def test_idempotent_start_and_conflicting_key(self):
        first = await self.start_task()
        second = await self.start_task()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.all("tasks")), 1)
        with self.assertRaisesRegex(BridgeError, "different arguments"):
            await self.start_task("different")

    async def test_follow_up_preserves_conversation_and_changes_model(self):
        task = await self.start_task()
        await self.until(task["id"])
        nxt = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "next",
                             "model": "small", "request_id": "next"})
        self.assertEqual(nxt["thread_id"], task["thread_id"])
        self.assertEqual(nxt["effort"], "low")
        await self.until(task["id"])
        history = await self.service.history(task["id"])
        self.assertEqual(len(history["data"]), 2)

    async def test_multiple_projects_and_same_project_exclusion(self):
        first = await self.start_task("hold")
        second = await self.start_task("hold", "b", "second")
        self.assertNotEqual(first["thread_id"], second["thread_id"])
        with self.assertRaisesRegex(BridgeError, "active in this project"):
            await self.start_task("hold", "a", "third")

    async def test_steer_and_interrupt_require_current_turn(self):
        task = await self.start_task("hold")
        with self.assertRaises(BridgeError):
            await self.service.send(task["id"], "steer", "steer", "wrong")
        result = await self.service.send(task["id"], "steer", "steer", task["turn_id"])
        self.assertEqual(result["status"], "running")
        await self.service.interrupt(task["id"], task["turn_id"])
        result = await self.until(task["id"])
        self.assertEqual(result["status"], "interrupted")

    async def test_parent_question_round_trip(self):
        task = await self.start_task("parent")
        waiting = await self.until(task["id"])
        req = waiting["pending_requests"][0]
        self.assertFalse(req["requires_human"])
        await self.service.respond(req["id"], {"text": "Run the unit tests"})
        done = await self.until(task["id"])
        self.assertEqual(done["status"], "completed")
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"text": "duplicate"})

    async def test_approval_is_not_automatically_granted(self):
        task = await self.start_task("approval")
        waiting = await self.until(task["id"])
        req = waiting["pending_requests"][0]
        with self.assertRaisesRegex(BridgeError, "Human review"):
            await self.service.respond(req["id"], {"decision": "accept"})
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"decision": "acceptForSession"}, human=True)
        self.assertEqual(self.service.get(task["id"])["status"], "waiting")
        await self.service.respond(req["id"], {"decision": "accept"}, human=True)
        self.assertEqual((await self.until(task["id"]))["status"], "completed")

    async def test_denial_allowed_without_human_channel(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"]))["pending_requests"][0]
        await self.service.respond(req["id"], {"decision": "decline"})
        self.assertEqual((await self.until(task["id"]))["status"], "completed")

    async def test_questions_validate_and_do_not_log_answer(self):
        task = await self.start_task("question")
        req = (await self.until(task["id"]))["pending_requests"][0]
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"answers": {}}, human=True)
        secret = "answer-not-in-bridge-journal"
        await self.service.respond(req["id"], {"answers": {"choice": {"answers": [secret]}}}, human=True)
        # The bridge's own response record contains no answer. Codex history may.
        records = [e for e in self.store.events(0, 200)["events"] if e["kind"] == "bridge/response_sent"]
        self.assertNotIn(secret, json.dumps(records))

    async def test_permission_grants_are_subsets_and_turn_scoped(self):
        task = await self.start_task("permission")
        req = (await self.until(task["id"]))["pending_requests"][0]
        for response in ({"permissions": {"fileSystem": {"write": ["/"]}}},
                         {"permissions": {}, "scope": "session"}):
            with self.assertRaises(BridgeError):
                await self.service.respond(req["id"], response, human=True)
        await self.service.respond(req["id"], {"permissions": {"network": {"enabled": True}}}, human=True)
        self.assertEqual((await self.until(task["id"]))["status"], "completed")

    async def test_unknown_request_fails_closed(self):
        task = await self.start_task("unknown")
        done = await self.until(task["id"])
        self.assertEqual(done["status"], "completed")
        events = await self.service.events(task_id=task["id"])
        self.assertIn("bridge/request_rejected", [e["kind"] for e in events["events"]])

    async def test_event_pagination_no_gaps_and_no_raw_reasoning(self):
        task = await self.start_task()
        await self.until(task["id"])
        cursor, ids, kinds = 0, [], []
        while True:
            page = await self.service.events(after=cursor, limit=2, task_id=task["id"])
            ids += [e["seq"] for e in page["events"]]
            kinds += [e["kind"] for e in page["events"]]
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(ids, sorted(set(ids)))
        self.assertNotIn("item/reasoning/textDelta", kinds)
        self.assertFalse((await self.service.events(after=cursor))["events"])
        with self.assertRaises(BridgeError):
            await self.service.events(after=cursor + 100)

    async def test_replay_survives_store_reopen(self):
        task = await self.start_task()
        await self.until(task["id"])
        before = self.store.events(0, 200)
        await self.service.close()
        self.store.close()
        self.store = Store(self.state)
        self.service = Service(self.cfg, self.store)
        self.assertEqual(before, self.store.events(0, 200))
        await self.service.resume(task["id"])
        self.assertEqual(len((await self.service.history(task["id"]))["data"]), 1)

    async def test_backend_disconnect_expires_approvals(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"]))["pending_requests"][0]
        self.service.backend.process.kill()
        await self.until(task["id"], ("needs_resume",))
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True)
        await self.service.resume(task["id"])
        self.assertEqual(self.service.get(task["id"])["status"], "idle")

    async def test_failure_is_not_success(self):
        task = await self.start_task("fail")
        done = await self.until(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["message"], "Fixture failure")

    async def test_auth_status_hides_identity_and_credentials(self):
        status = await self.service.status()
        self.assertEqual(status["authentication_type"], "chatgpt")
        self.assertNotIn("not-exposed", json.dumps(status))

    async def test_relative_traversal_and_symlink_projects_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        link_or_skip(self, self.root / "escape", outside)
        link_or_skip(self, self.root / "dangling", self.base / "absent", directory=True)
        for path in ("../outside", "escape", str(self.root / "escape"), str(self.root / "escape/new"), str(self.root / "dangling/new")):
            with self.assertRaises(BridgeError):
                self.service.projects("create", path)
        self.assertFalse((outside / "new").exists())
        self.assertEqual(self.service.projects("select", str(outside))["path"], str(outside))

    async def test_replaced_project_denied_on_follow_up(self):
        task = await self.start_task()
        await self.until(task["id"])
        (self.root / "a").rmdir()
        link_or_skip(self, self.root / "a", self.base)
        with self.assertRaises(BridgeError):
            await self.service.send(task["id"], "next")

    async def test_validation_rejects_unsafe_extra_fields(self):
        for values in ({"task_id": "x", "shell": "rm"}, {"task_id": 12}):
            with self.assertRaises(BridgeError):
                arguments("codex_get", values)
        with self.assertRaises(BridgeError):
            arguments("codex_events", {"wait_seconds": 100})
        with self.assertRaises(BridgeError):
            arguments("codex_events", {"limit": True})
        with self.assertRaises(BridgeError):
            arguments("command/exec", {})

    async def test_redaction(self):
        value = {"access_token": "secret", "log": "Authorization: Bearer abcd1234 sk-abcdefghijklmnopqrstuv"}
        out = json.dumps(redact(value))
        self.assertNotIn("secret", out)
        self.assertNotIn("abcd1234", out)
        self.assertNotIn("sk-", out)

    async def test_nested_projects_cannot_run_concurrently(self):
        await self.start_task("hold", "a", "outer")
        with self.assertRaises(BridgeError):
            await self.start_task("hold", "a/nested", "inner")

    async def test_operator_enabled_approval_relay(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"]))["pending_requests"][0]
        self.service.config = replace(self.cfg, approval_relay=True)
        await self.service.respond(req["id"], {"decision": "accept"})
        self.assertEqual((await self.until(task["id"]))["status"], "completed")

    async def test_cancel_only_denial_option(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"]))["pending_requests"][0]
        self.service.raw_requests[req["id"]]["availableDecisions"] = ["accept", "cancel"]
        with self.assertRaisesRegex(BridgeError, "not offered"):
            await self.service.respond(req["id"], {"decision": "decline"})
        await self.service.respond(req["id"], {"decision": "cancel"})

    async def test_file_approval_contains_patch_and_rejects_outside_path(self):
        task = await self.start_task("hold")
        common = {"threadId": task["thread_id"], "turnId": task["turn_id"]}
        await self.service.on_message({"method": "item/started", "params": {**common, "item": {
            "id": "patch", "type": "fileChange", "changes": [{"path": "../../outside", "diff": "+ test"}]}}})
        await self.service.on_message({"id": 9999, "method": "item/fileChange/requestApproval",
                                       "params": {**common, "itemId": "patch"}})
        req = self.service.requests(task["id"])[0]
        self.assertEqual(req["params"]["proposedChanges"][0]["diff"], "+ test")
        with self.assertRaisesRegex(BridgeError, "outside"):
            await self.service.respond(req["id"], {"decision": "accept"}, human=True)

    async def test_secret_answers_never_enter_backend(self):
        task = await self.start_task("question")
        req = (await self.until(task["id"]))["pending_requests"][0]
        self.service.raw_requests[req["id"]]["questions"][0]["isSecret"] = True
        with self.assertRaisesRegex(BridgeError, "Secret answers"):
            await self.service.respond(req["id"], {"answers": {"choice": {"answers": ["a-secret"]}}}, human=True)

    async def test_nonblocking_question_does_not_stop_running_status(self):
        task = await self.start_task("hold")
        await self.service.on_message({"id": 9998, "method": "item/tool/requestUserInput", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "q",
            "isBlocking": False, "questions": [{"id": "choice", "header": "Choice", "question": "Which?"}]}})
        self.assertEqual(self.service.get(task["id"])["status"], "running")
        self.assertFalse(self.service.requests(task["id"])[0]["blocking"])

    async def test_projects_are_chosen_by_main_agent_without_config_roots(self):
        second = self.base / "another-location" / "project"
        descriptor = self.service.projects("create", str(second))
        self.assertEqual(descriptor["path"], str(second))
        self.assertTrue(second.is_dir())
        self.assertEqual(self.service.select_project(project_id=descriptor["project_id"])["path"], str(second))
        self.assertNotIn("workspace_roots", self.config_file.read_text())

    async def test_lost_start_response_is_not_replayed(self):
        original = self.service.backend.call
        async def lose(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "turn/start":
                raise BridgeError("Simulated lost acknowledgement")
            return result
        self.service.backend.call = lose
        with self.assertRaises(BridgeError):
            await self.start_task("hold")
        with self.assertRaisesRegex(BridgeError, "uncertain"):
            await self.start_task("hold")
        self.assertEqual(len(self.store.all("tasks")), 1)

    async def test_documented_denial_shapes_need_no_human_channel(self):
        # The tool reference shows grants with "scope": "turn"; an empty grant
        # in that shape is still a denial and must not require human review.
        task = await self.start_task("permission")
        req = (await self.until(task["id"]))["pending_requests"][0]
        with self.assertRaisesRegex(BridgeError, "Human review"):
            await self.service.respond(req["id"], {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
        await self.service.respond(req["id"], {"permissions": {}, "scope": "turn"})
        self.assertEqual((await self.until(task["id"]))["status"], "completed")
        hold = await self.start_task("hold", path="b", key="hold")
        await self.service.on_message({"id": 9997, "method": "mcpServer/elicitation/request", "params": {
            "threadId": hold["thread_id"], "turnId": hold["turn_id"], "mode": "form",
            "message": "Connect?", "requestedSchema": {"type": "object", "properties": {}}}})
        req = self.service.requests(hold["id"])[0]
        await self.service.respond(req["id"], {"action": "decline", "content": None})
        self.assertEqual(self.store.get("requests", req["id"])["status"], "answered")

    async def test_relative_permission_paths_resolve_inside_the_project(self):
        task = await self.start_task("hold")
        requested = {"fileSystem": {"write": ["build", "../escape"]}}
        await self.service.on_message({"id": 9996, "method": "item/permissions/requestApproval", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "p", "permissions": requested}})
        req = self.service.requests(task["id"])[0]
        with self.assertRaisesRegex(BridgeError, "within this task's project"):
            await self.service.respond(req["id"], {"permissions": {"fileSystem": {"write": ["../escape"]}}}, human=True)
        # Relative to the project, not to the broker's working directory.
        result = await self.service.respond(req["id"], {"permissions": {"fileSystem": {"write": ["build"]}}}, human=True)
        self.assertEqual(result["status"], "sent")

    async def test_failed_operation_key_reports_the_original_error(self):
        self.service.projects("create", str(self.root / "a"))
        args = {"workdir": str(self.root / "a"), "prompt": "echo", "request_id": "bad-model", "model": "invented"}
        with self.assertRaisesRegex(BridgeError, "visible Codex catalog"):
            await dispatch(self.service, "codex_start", args)
        with self.assertRaisesRegex(BridgeError, "uncertain.*visible Codex catalog.*codex_list"):
            await dispatch(self.service, "codex_start", args)

    async def test_default_service_tier_is_not_sent_as_a_tier_id(self):
        self.service.projects("create", str(self.root / "a"))
        task = await dispatch(self.service, "codex_start", {"workdir": str(self.root / "a"), "prompt": "echo",
            "request_id": "tier", "service_tier": "default"})
        self.assertIsNone(task["service_tier"])
        await self.until(task["id"])
        changed = await self.service.settings(task["id"], "next", service_tier="priority")
        self.assertEqual(changed["service_tier"], "priority")
        changed = await self.service.settings(task["id"], "next", service_tier="default")
        self.assertIsNone(changed["service_tier"])
        self.assertNotIn("serviceTier", self.service.turn_options(self.service.task(task["id"])))

    async def test_large_events_paginate_by_bytes_without_loss(self):
        with self.store.db:
            for i in range(4):
                self.store.event(None, "large", {"i": i, "text": "x" * 600000})
        first = self.store.events(0, 100)
        self.assertEqual(len(first["events"]), 1)
        self.assertTrue(first["has_more"])
        ids, cursor = [], 0
        while True:
            page = self.store.events(cursor, 100)
            ids += [e["data"]["i"] for e in page["events"]]
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(ids, [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
