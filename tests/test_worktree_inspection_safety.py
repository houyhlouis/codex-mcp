"""Working-tree inspection holds both the project and its shared Git directory."""

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import worktrees as git
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.helpers import thread_wakeups
from tests import test_managed_worktrees as worktree_cases
from tests.test_service import ServiceHarness


class InspectionSafetyTest(ServiceHarness):
    git = worktree_cases.ManagedWorktreesTest.git
    call = worktree_cases.ManagedWorktreesTest.call
    planned = worktree_cases.ManagedWorktreesTest.planned
    created = worktree_cases.ManagedWorktreesTest.created
    task_in = worktree_cases.ManagedWorktreesTest.task_in

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
        self.repo = self.root / "repository"
        self.repo.mkdir(mode=0o700)
        self.enterContext(worktree_cases.isolated_git(self.base))
        await self.git("init", "--initial-branch=main")
        await self.git("config", "user.name", "Offline tests")
        await self.git("config", "user.email", "offline@example.invalid")
        (self.repo / "tracked.txt").write_text("original\n")
        await self.git("add", "-A", "--", ".")
        await self.git("commit", "-m", "fixture")

    async def test_busy_worktree_has_metadata_only_inspection(self):
        _, created = await self.created()
        task = await self.task_in(created, "hold")
        with patch.object(git, "inspect", side_effect=AssertionError("Busy worktree must not be read by Git")):
            result = await self.call("inspect", project_id=created["project_id"])
        self.assertFalse(result["working_tree_checked"])
        self.assertIsNone(result["dirty"])
        self.assertEqual(result["working_tree_error"]["code"], "workspace_busy")
        self.assertEqual(self.service.get(task["id"])["status"], "running")
        self.assertFalse(self.service.project_operations)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))

    async def test_target_processes_or_unavailable_scan_leave_metadata_only(self):
        _, created = await self.created()
        for scan in ({"available": True, "reason": "ok", "processes": [{"pid": 1234}], "truncated": False},
                {"available": False, "reason": "proc_unreadable", "processes": [], "truncated": False}):
            with self.subTest(scan=scan), patch.object(self.service, "scan_workspace", return_value=scan), \
                    patch.object(git, "inspect", side_effect=AssertionError("Unsafe worktree must not be read by Git")):
                result = await self.call("inspect", project_id=created["project_id"])
            self.assertFalse(result["working_tree_checked"])
            self.assertEqual(result["working_tree_error"]["code"], "workspace_busy")
            self.assertFalse(self.service.project_operations)

    async def test_inspection_reservation_blocks_new_work_until_git_finishes(self):
        _, created = await self.created()
        entered, release = asyncio.Event(), threading.Event()
        original = git.inspect
        def gated(path):
            self.worktree_loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise AssertionError("Inspection gate was not released")
            return original(path)
        with patch.object(git, "inspect", side_effect=gated):
            inspecting = asyncio.create_task(self.call("inspect", project_id=created["project_id"]))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                with self.assertRaises(BridgeError) as caught:
                    await asyncio.wait_for(dispatch(self.service, "codex_start", {"project_id": created["project_id"],
                        "prompt": "hold", "operation_id": "during-inspection"}), 5)
                self.assertEqual(caught.exception.code, "workspace_busy")
            finally:
                release.set()
                await asyncio.wait_for(inspecting, 10)
        self.assertFalse(self.service.project_operations)
        task = await self.task_in(created, "hold", key="after-inspection")
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
