"""Opt-in broker worktrees use real, isolated Git and the fake app-server."""

import asyncio
import base64
import hashlib
import os
import subprocess
import threading
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import worktrees as git
from codex_local_mcp.platforms import IS_WINDOWS
from codex_local_mcp.security import BridgeError, project_path
from codex_local_mcp.tools import arguments, dispatch
from tests.helpers import thread_wakeups
from tests.test_service import ServiceHarness


@contextmanager
def isolated_git(base):
    config = base / "global.gitconfig"
    config.write_text("")
    original = subprocess.Popen
    def isolated(command, **kwargs):
        if Path(command[0]).name.lower() in ("git", "git.exe"):
            kwargs["env"] = {**kwargs.get("env", os.environ), "GIT_CONFIG_GLOBAL": str(config),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": str(base.parent)}
        return original(command, **kwargs)
    with patch.object(git.subprocess, "Popen", side_effect=isolated):
        yield


class ManagedWorktreesTest(ServiceHarness):
    def _tearDownAsyncioRunner(self):
        loop = getattr(self, "worktree_loop", None)
        if loop is not None and not loop.is_closed():
            with thread_wakeups(loop):
                return super()._tearDownAsyncioRunner()
        return super()._tearDownAsyncioRunner()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.worktree_loop = asyncio.get_running_loop()
        self.enterContext(thread_wakeups())
        await self.service.close()
        self.cfg = replace(self.cfg, worktrees=True)
        self.service = self.new_service()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir(mode=0o700)
        self.enterContext(isolated_git(self.base))
        await self.git("init", "--initial-branch=main")
        await self.git("config", "user.name", "Offline tests")
        await self.git("config", "user.email", "offline@example.invalid")
        (self.repo / "tracked.txt").write_text("original\n")
        await self.git("add", "-A", "--", ".")
        await self.git("commit", "-m", "fixture")

    async def git(self, *args, cwd=None):
        return (await asyncio.to_thread(git._git, list(args), cwd or self.repo, write=True))[0]

    async def call(self, action, **args):
        return await dispatch(self.service, "codex_worktrees", {"action": action, **args})

    async def planned(self, name="trial"):
        return await self.call("plan", repo=str(self.repo), name=name)

    async def created(self, name="trial", key=None):
        plan = await self.planned(name)
        result = await self.call("create", repo=str(self.repo), name=name, plan_id=plan["plan_id"], operation_id=key or name)
        return plan, result

    async def task_in(self, project, prompt="echo", key="start", **extra):
        result = await dispatch(self.service, "codex_start", {"project_id": project["project_id"],
            "prompt": prompt, "operation_id": key, **extra})
        if prompt != "hold":
            await self.until(result["id"], ("completed",))
            await self.service.reconcile_claims(result["id"])
        return result

    async def test_disabled_actions_and_inspect_existing(self):
        _, created = await self.created()
        self.service.config = replace(self.cfg, worktrees=False)
        for action, args in (("plan", {"repo": str(self.repo), "name": "disabled"}),
                ("create", {"repo": str(self.repo), "name": "disabled", "plan_id": "a" * 64, "request_id": "disabled"}),
                ("handoff", {"task_id": "missing"}),
                ("cleanup", {"project_id": created["project_id"], "request_id": "disabled-clean"})):
            with self.subTest(action=action), self.assertRaisesRegex(BridgeError, "worktrees = true") as caught:
                await self.call(action, **args)
            self.assertEqual(caught.exception.code, "worktrees_disabled")
        self.assertTrue((await self.call("inspect", project_id=created["project_id"]))["managed"])

    async def test_plan_create_reuse_inspect_and_removed_history(self):
        planned, created = await self.created()
        self.assertEqual(created["project_id"], self.service.select_project(workdir=created["path"])["id"])
        self.assertEqual(self.store.get("projects", created["project_id"])["worktree"]["base"], planned["base"])
        result = await self.call("inspect", path=created["path"])
        self.assertEqual(result["shared_resources"][0]["path"], str(self.repo / ".git"))
        self.assertEqual(result["shared_resources"][0]["task_access"], "not_granted")
        self.assertEqual(result["tasks"], [])
        task = await self.task_in(created)
        result = await self.call("inspect", project_id=created["project_id"])
        self.assertEqual(result["tasks"][0]["id"], task["id"])
        removed = await self.call("cleanup", project_id=created["project_id"], operation_id="remove")
        self.assertTrue(removed["removed"])
        self.assertTrue(self.store.get("projects", created["project_id"])["removed"])
        self.assertTrue(self.service.get(task["id"])["project_removed"])
        for view in ("default", "compact"):
            self.assertTrue(self.service.list_tasks(view=view)["tasks"][0]["project_removed"])
        self.assertTrue((await self.call("inspect", project_id=created["project_id"]))["removed"])
        with self.assertRaisesRegex(BridgeError, "project was removed"):
            self.service.projects("create", created["path"])
        self.assertFalse(Path(created["path"]).exists())
        _, recreated = await self.created(key="recreated")
        self.assertEqual(recreated["project_id"], created["project_id"])
        self.assertFalse(self.store.get("projects", created["project_id"])["removed"])

    async def test_existing_target_and_stale_plan_never_create(self):
        plan = await self.planned()
        Path(plan["path"]).mkdir(parents=True)
        with self.assertRaises(BridgeError) as caught:
            await self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="exists")
        self.assertEqual(caught.exception.code, "worktree_target_exists")
        Path(plan["path"]).rmdir()
        plan = await self.planned()
        (self.repo / "tracked.txt").write_text("advance\n")
        await self.git("add", "-A", "--", ".")
        await self.git("commit", "-m", "advance")
        with self.assertRaises(BridgeError) as caught:
            await self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="stale")
        self.assertEqual(caught.exception.code, "worktree_plan_mismatch")
        self.assertFalse(Path(plan["path"]).exists())

    async def test_cli_lock_is_shared_and_rejection_is_finished(self):
        plan = await self.planned()
        lock = Path(plan["common_dir"]) / git.METADATA / "locks"
        with git._lock(Path(plan["common_dir"]), lock):
            with self.assertRaises(BridgeError) as caught:
                await self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="locked")
        self.assertEqual(caught.exception.code, "worktree_locked")
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='locked'").fetchone()[0], "done")
        self.assertFalse(Path(plan["path"]).exists())

    async def test_inspection_of_git_locked_worktree_keeps_existing_interrupt_available(self):
        _, created = await self.created()
        task = await self.task_in(created, "hold")
        await self.git("worktree", "lock", "--", created["path"])
        inspected = await self.call("inspect", project_id=created["project_id"])
        self.assertEqual(inspected["registration"], "locked")
        await self.service.interrupt(task["id"], task["turn_id"])
        self.assertEqual((await self.until(task["id"], ("interrupted",)))["status"], "interrupted")

    async def test_cleanup_claim_process_and_dirty_refusals(self):
        _, created = await self.created()
        task = await self.task_in(created, "hold")
        with self.assertRaises(BridgeError) as caught:
            await self.call("cleanup", project_id=created["project_id"], request_id="active")
        self.assertEqual(caught.exception.code, "workspace_busy")
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.service.reconcile_claims(task["id"])
        scan = {"available": True, "reason": "ok", "processes": [{"pid": 1234}], "truncated": False}
        with patch.object(self.service, "scan_workspace", return_value=scan):
            with self.assertRaisesRegex(BridgeError, "kill PID"):
                await self.call("cleanup", project_id=created["project_id"], request_id="process")
        (Path(created["path"]) / "tracked.txt").write_text("unsaved\n")
        with self.assertRaises(BridgeError) as caught:
            await self.call("cleanup", project_id=created["project_id"], request_id="dirty")
        self.assertEqual(caught.exception.code, "worktree_dirty")
        self.assertEqual((Path(created["path"]) / "tracked.txt").read_text(), "unsaved\n")

    async def test_handoff_artifact_is_scoped_hashed_and_retained(self):
        _, created = await self.created()
        task = await self.task_in(created)
        (Path(created["path"]) / "tracked.txt").write_text("handoff content\n")
        handoff = await self.call("handoff", task_id=task["id"])
        self.assertNotIn("patch", handoff)
        artifact = self.service.get(task["id"], view="artifact", artifact_id=handoff["artifact_id"])["artifact"]
        data = base64.b64decode(artifact["data"])
        self.assertIsNone(artifact["next_offset"])
        self.assertEqual(len(data), handoff["size_bytes"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), handoff["sha256"])
        self.assertIn(b"handoff content", data)
        result = self.service.get(task["id"], view="result", result_id=handoff["result_id"])["result"]
        self.assertIn(handoff["artifact_id"], [a["artifact_id"] for a in result["artifacts"]])
        other = await self.start_task(path="other", key="other")
        await self.until(other["id"], ("completed",))
        with self.assertRaises(BridgeError):
            self.service.get(other["id"], view="artifact", artifact_id=handoff["artifact_id"])
        (Path(created["path"]) / "tracked.txt").write_text("original\n")
        await self.call("cleanup", project_id=created["project_id"], request_id="handoff-cleanup")
        retained = self.service.get(task["id"], view="artifact", artifact_id=handoff["artifact_id"])["artifact"]
        self.assertEqual(base64.b64decode(retained["data"]), data)

    async def test_permissions_never_grant_common_directory(self):
        _, created = await self.created()
        task = await self.task_in(created)
        permission = self.service.get(task["id"], view="permissions")
        boundary = permission["permission_view"]["configured_boundary"]
        self.assertEqual(boundary["shared_resources"][0]["task_access"], "not_granted")
        self.assertNotIn(created["common_dir"], boundary["write_roots"])
        options = await self.service.thread_options(self.service.task(task["id"]))
        self.assertNotIn(created["common_dir"], options.get("runtimeWorkspaceRoots", []))
        for outside in (Path(created["common_dir"]) / "config", Path(created["common_dir"]) / "refs" / "heads" / "main"):
            with self.subTest(outside=outside), self.assertRaisesRegex(BridgeError, "outside"):
                self.service.validate_response({"kind": "file_approval", "task_id": task["id"]},
                    {"proposedChanges": [{"path": str(outside), "kind": {"type": "update"}, "diff": "+ forbidden"}]},
                    {"decision": "accept"})

    async def test_separate_worktrees_run_in_parallel_and_budget_applies(self):
        _, first = await self.created("first")
        _, second = await self.created("second")
        a = await self.task_in(first, "hold", key="start-first")
        b = await self.task_in(second, "hold", key="start-second")
        await dispatch(self.service, "codex_settings", {"task_id": b["id"], "scope": "next", "budget": {"max_turns": 1}, "request_id": "limit"})
        self.assertNotEqual(a["project"], b["project"])
        await self.service.interrupt(b["id"], b["turn_id"])
        await self.until(b["id"], ("interrupted",))
        with self.assertRaises(BridgeError) as caught:
            await dispatch(self.service, "codex_send", {"task_id": b["id"], "prompt": "next", "request_id": "budget-next"})
        self.assertEqual(caught.exception.code, "budget_exceeded")

    async def test_worktree_action_validation_drops_only_defaults(self):
        parsed = arguments("codex_worktrees", {"action": "inspect", "path": str(self.repo), "keep_branch": False, "base": "HEAD"})
        self.assertNotIn("keep_branch", parsed)
        self.assertNotIn("base", parsed)
        with self.assertRaisesRegex(BridgeError, "does not apply"):
            arguments("codex_worktrees", {"action": "inspect", "path": str(self.repo), "keep_branch": True})

    async def test_create_finished_before_registration_is_recovered_without_replay(self):
        plan = await self.planned()
        args = {"repo": str(self.repo), "name": "trial", "plan_id": plan["plan_id"], "request_id": "crashed-create"}
        with patch.object(self.service.managed_worktrees, "registered", side_effect=OSError("Registration interrupted")):
            with self.assertRaises(OSError):
                await self.call("create", **args)
        self.assertTrue(Path(plan["path"]).is_dir())
        self.assertFalse(self.store.db.execute("SELECT id FROM projects WHERE json_extract(data,'$.path')=?", (plan["path"],)).fetchone())
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id=?", (args["request_id"],)).fetchone()[0], "uncertain")
        self.assertTrue(self.store.db.execute("SELECT * FROM admissions WHERE operation_id=?", (args["request_id"],)).fetchone())
        await self.service.close()
        self.service = self.new_service()
        self.assertIn(args["request_id"], self.service.project_operations)
        with self.assertRaisesRegex(BridgeError, "codex_worktrees action=inspect"):
            await dispatch(self.service, "codex_start", {"workdir": plan["path"], "prompt": "hold", "request_id": "uninspected"})
        with patch.object(git, "create", side_effect=AssertionError("Uncertain creation was replayed")):
            with self.assertRaises(BridgeError):
                await self.call("create", **args)
            recovered = await self.call("inspect", path=plan["path"])
        self.assertTrue(recovered["managed"])
        self.assertEqual(recovered["pending_operation_ids"], [args["request_id"]])
        self.assertEqual(recovered["released_operation_ids"], [args["request_id"]])
        self.assertEqual(self.service.select_project(project_id=recovered["project_id"])["path"], plan["path"])
        await self.task_in(recovered, key="recovered-start")
        await self.service.close()
        self.service = self.new_service()
        self.assertNotIn(args["request_id"], self.service.project_operations)

    async def test_receipt_only_failed_create_is_inspectable_and_cleanup_works(self):
        plan = await self.planned()
        original = git._git
        def failed(args, *values, **kwargs):
            if args[:2] == ["worktree", "add"]:
                raise git.WorktreeError("git_failed", "Injected interruption before registration")
            return original(args, *values, **kwargs)
        with patch.object(git, "_git", failed), self.assertRaises(BridgeError):
            await self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="receipt-create")
        inspected = await self.call("inspect", path=plan["path"])
        self.assertFalse(inspected["managed"])
        self.assertEqual(inspected["registration"], "incomplete")
        self.assertEqual(inspected["released_operation_ids"], ["receipt-create"])
        with self.assertRaisesRegex(BridgeError, "incomplete"):
            await dispatch(self.service, "codex_start", {"project_id": inspected["project_id"], "prompt": "hold", "request_id": "incomplete-task"})
        with self.assertRaisesRegex(BridgeError, "incomplete"):
            self.service.projects("create", inspected["path"])
        self.assertFalse(Path(inspected["path"]).exists())
        cleaned = await self.call("cleanup", project_id=inspected["project_id"], request_id="receipt-clean")
        self.assertTrue(cleaned["receipt_removed"])
        self.assertFalse(git._locator(Path(plan["path"])).exists())

    async def test_create_reservation_blocks_start_while_git_is_in_flight(self):
        plan = await self.planned()
        entered, release = asyncio.Event(), threading.Event()
        loop, original = asyncio.get_running_loop(), git.create
        def paused(*args, **kwargs):
            report = original(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("Test gate was not released")
            return report
        with patch.object(git, "create", paused):
            creating = asyncio.create_task(self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="gated-create"))
            duplicate = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                with self.assertRaises(BridgeError) as caught:
                    await dispatch(self.service, "codex_start", {"workdir": plan["path"], "prompt": "hold", "request_id": "during-create"})
                self.assertEqual(caught.exception.code, "workspace_busy")
                duplicate = asyncio.create_task(self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], operation_id="gated-create"))
            finally:
                release.set()
                result = await asyncio.wait_for(creating, 5)
                if duplicate:
                    self.assertEqual(result, await asyncio.wait_for(duplicate, 5))
        self.assertFalse(self.store.db.execute("SELECT * FROM admissions").fetchall())

    async def test_shutdown_settles_git_and_reports_uncertainty_without_registration(self):
        plan = await self.planned()
        entered, release = asyncio.Event(), threading.Event()
        loop, original = asyncio.get_running_loop(), git.create
        def paused(*args, **kwargs):
            report = original(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("Test gate was not released")
            return report
        with patch.object(git, "create", paused):
            creating = asyncio.create_task(self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="closing-create"))
            await asyncio.wait_for(entered.wait(), 5)
            closing = asyncio.create_task(self.service.close())
            loop.call_soon(release.set)
            await asyncio.wait_for(closing, 5)
            with self.assertRaises(BridgeError) as caught:
                await creating
        self.assertEqual(caught.exception.code, "worktree_interrupted")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='closing-create'").fetchone()[0], "uncertain")
        self.assertFalse(self.service.managed_worktrees.jobs)
        self.assertFalse(self.service.managed_worktrees.calls)
        self.assertFalse(self.store.db.execute("SELECT id FROM projects").fetchall())
        self.assertTrue(Path(plan["path"]).exists())
        self.service = self.new_service()
        self.assertTrue((await self.call("inspect", path=plan["path"]))["managed"])

    async def test_cleanup_finished_before_registration_retains_target_until_inspected(self):
        _, created = await self.created()
        original = self.store.update
        def failed(table, identity, fields=None, **kwargs):
            if table == "projects" and fields and fields.get("removed"):
                raise OSError("Injected registration failure after cleanup")
            return original(table, identity, fields, **kwargs)
        with patch.object(self.store, "update", failed), self.assertRaises(OSError):
            await self.call("cleanup", project_id=created["project_id"], request_id="uncertain-clean")
        self.assertFalse(Path(created["path"]).exists())
        self.assertIn("uncertain-clean", self.service.project_operations)
        await self.service.close()
        self.service = self.new_service()
        self.assertIn("uncertain-clean", self.service.project_operations)
        with patch.object(git, "cleanup", side_effect=AssertionError("Cleanup was replayed")):
            report = await self.call("inspect", project_id=created["project_id"])
        self.assertTrue(report["removed"])
        self.assertEqual(report["released_operation_ids"], ["uncertain-clean"])
        self.assertNotIn("uncertain-clean", self.service.project_operations)
        self.assertTrue(self.store.get("projects", created["project_id"])["removed"])

    async def test_partial_cleanup_is_inspected_then_explicitly_preserves_branch(self):
        _, created = await self.created()
        original = git._git
        def failed(args, *values, **kwargs):
            if args[:2] == ["branch", "-d"]:
                raise git.WorktreeError("git_failed", "Injected failure before branch deletion")
            return original(args, *values, **kwargs)
        with patch.object(git, "_git", failed), self.assertRaises(BridgeError):
            await self.call("cleanup", project_id=created["project_id"], request_id="partial-clean")
        self.assertIn("partial-clean", self.service.project_operations)
        inspected = await self.call("inspect", project_id=created["project_id"])
        self.assertEqual(inspected["released_operation_ids"], ["partial-clean"])
        self.assertEqual(inspected["registration"], "incomplete")
        cleaned = await self.call("cleanup", project_id=created["project_id"], keep_branch=True, request_id="keep-after-failure")
        self.assertTrue(cleaned["receipt_removed"])
        self.assertTrue(cleaned["branch_kept"])

    async def test_handoff_refuses_oversized_patch_and_preserves_files(self):
        _, created = await self.created()
        task = await self.task_in(created)
        changed = Path(created["path"]) / "large.txt"
        changed.write_text("large output\n" * (git.HANDOFF_BYTES // 10))
        before = self.store.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        with self.assertRaises(BridgeError) as caught:
            await self.call("handoff", task_id=task["id"])
        self.assertEqual(caught.exception.code, "worktree_git_failed")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], before)
        self.assertTrue(changed.exists())

    async def test_custom_root_and_protected_paths(self):
        root = self.root / "private worktrees"
        self.service.config = replace(self.cfg, worktree_root=root)
        planned, created = await self.created()
        self.assertEqual(planned["path"], str(root / "trial"))
        if not IS_WINDOWS:
            self.assertEqual(Path(created["path"]).parent.stat().st_mode & 0o777, 0o700)
        self.service.config = replace(self.cfg, worktree_root=self.state / "unsafe")
        with self.assertRaisesRegex(BridgeError, "state"):
            await self.planned("unsafe")
        self.assertFalse((self.state / "unsafe").exists())

    async def test_common_git_directory_is_reserved_only_during_helper_and_uses_no_task_slot(self):
        plan = await self.planned()
        self.service.config = replace(self.cfg, max_tasks=1)
        entered, release = asyncio.Event(), threading.Event()
        loop, original = asyncio.get_running_loop(), git.create
        def paused(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("Test gate was not released")
            return original(*args, **kwargs)
        with patch.object(git, "create", paused):
            creating = asyncio.create_task(self.call("create", repo=str(self.repo), name="trial", plan_id=plan["plan_id"], request_id="common-create"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                with self.assertRaises(BridgeError) as caught:
                    await dispatch(self.service, "codex_start", {"workdir": str(self.repo), "prompt": "hold", "request_id": "common-task"})
                self.assertEqual(caught.exception.code, "workspace_busy")
                task = await self.start_task("hold", path="unrelated", key="unrelated")
                self.assertEqual(task["status"], "running")
            finally:
                release.set()
                await asyncio.wait_for(creating, 5)

    async def test_main_checkout_claim_blocks_sensitive_calls_and_inspect_reports_unknown(self):
        _, created = await self.created()
        task = await self.task_in(created)
        main = await dispatch(self.service, "codex_start", {"workdir": str(self.repo), "prompt": "hold", "request_id": "main"})
        planned = await self.planned("other")
        for action, args in (("create", {"repo": str(self.repo), "name": "other", "plan_id": planned["plan_id"], "request_id": "blocked-create"}),
                ("handoff", {"task_id": task["id"]}),
                ("cleanup", {"project_id": created["project_id"], "request_id": "blocked-cleanup"})):
            with self.subTest(action=action), self.assertRaisesRegex(BridgeError, "main checkout to be idle") as caught:
                await self.call(action, **args)
            self.assertIn(main["id"], caught.exception.ids["task_ids"])
        inspected = await self.call("inspect", project_id=created["project_id"])
        self.assertIsNone(inspected["dirty"])
        self.assertIsNone(inspected["untracked_count"])
        self.assertFalse(inspected["working_tree_checked"])
        self.assertIn(main["id"], inspected["working_tree_reason"])
        await self.task_in(created, key="parallel-after-inspect")
        await self.service.interrupt(main["id"], main["turn_id"])
        await self.until(main["id"], ("interrupted",))
        await self.service.reconcile_claims(main["id"])
        self.assertFalse((await self.call("inspect", project_id=created["project_id"]))["dirty"])
        self.assertTrue((await self.call("handoff", task_id=task["id"]))["artifact_id"])

    async def test_common_git_process_check_blocks_and_reports_unknown_when_unavailable(self):
        _, created = await self.created()
        common = created["common_dir"]
        disabled = {"available": False, "reason": "disabled", "processes": [], "truncated": False}
        for result in ({"available": True, "reason": "ok", "processes": [{"pid": 4321}], "truncated": False},
                {"available": False, "reason": "proc_unreadable", "processes": [], "truncated": False}):
            async def scanned(project):
                return result if project == common else disabled
            with self.subTest(result=result), patch.object(self.service, "scan_workspace", side_effect=scanned):
                with self.assertRaisesRegex(BridgeError, "main checkout to be idle"):
                    await self.call("cleanup", project_id=created["project_id"], request_id="blocked-" + result["reason"])
                inspected = await self.call("inspect", project_id=created["project_id"])
                self.assertIsNone(inspected["dirty"])
                self.assertFalse(inspected["working_tree_checked"])
        self.assertFalse(self.service.project_operations)

    async def test_operation_key_cannot_remove_an_inspection_reservation(self):
        project = self.service.projects("create", str(self.root / "unrelated"))
        async with self.service.managed_worktrees.common_gate(self.repo / ".git"):
            key = self.store.db.execute("SELECT operation_id FROM admissions").fetchone()[0]
            with self.assertRaises(BridgeError) as caught:
                await dispatch(self.service, "codex_start", {"project_id": project["id"], "prompt": "hold", "request_id": key})
            self.assertEqual(caught.exception.code, "operation_conflict")
            self.assertTrue(self.store.db.execute("SELECT 1 FROM admissions WHERE operation_id=?", (key,)).fetchone())
            self.assertIn(key, self.service.project_operations)

    async def test_future_project_checks_do_not_create_directories(self):
        future = self.root / "future" / "leaf"
        with self.assertRaises(BridgeError):
            project_path(self.cfg, str(future))
        self.assertEqual(project_path(self.cfg, str(future), allow_missing=True), future)
        self.assertFalse(future.parent.exists())
        for path in (Path.home(), self.state / "nested", self.state.parent):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                project_path(self.cfg, str(path), allow_missing=True)
