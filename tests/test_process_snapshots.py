"""Process snapshots disclose their age without scanning on native events."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from tests.helpers import FaultGate
from tests.test_service import ServiceHarness
from tests.test_workspace_processes import observation


class ProcessSnapshotsTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.config = replace(self.cfg, sandbox_process_check=True)
        self.service.process_scanner = AsyncMock(return_value=observation())

    async def snapshot(self, task, when=1234):
        with patch("codex_local_mcp.service.time.time", return_value=when):
            result, _ = await self.service.prepare_release_scan(task["id"], force=True)
        self.assertIsInstance(result, dict)
        return self.service.get(task["id"])["process_state"]["os_process_check"]

    async def test_native_terminal_observation_invalidates_an_earlier_zero(self):
        task = await self.start_task("hold_command_background")
        before = await self.snapshot(task)
        self.assertEqual(before["processes_total"], 0)
        self.assertEqual(before.get("checked_at"), 1234)
        self.service.process_scanner.reset_mock()
        with patch.object(self.service, "scan_workspace", side_effect=AssertionError("A terminal observation scanned /proc")):
            terminals = await self.service.terminals(task["id"])
        self.assertTrue(terminals["data"])
        self.service.process_scanner.assert_not_awaited()
        after = self.service.get(task["id"])["process_state"]["os_process_check"]
        self.assertEqual(after["reason"], "not_checked")
        self.assertIsNone(after["processes_total"])
        self.assertEqual(after["checked_at"], 1234)
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        self.assertEqual(report["os_process_check"], after)

    async def test_new_native_command_marks_the_snapshot_stale_without_a_scan(self):
        task = await self.start_task("hold")
        before = await self.snapshot(task)
        with patch.object(self.service, "process_scanner", side_effect=AssertionError("Event handling scanned /proc")):
            await self.service.on_message({"method": "item/started", "params": {"threadId": task["thread_id"],
                "turnId": task["turn_id"], "item": {"type": "commandExecution", "id": "later-command",
                "command": "echo synthetic", "cwd": task["project"], "status": "inProgress",
                "processId": "fixture-pid", "aggregatedOutput": None, "exitCode": None, "durationMs": None}, "startedAtMs": 0}})
        current = self.service.get(task["id"])["process_state"]["os_process_check"]
        self.assertEqual(current["reason"], "not_checked")
        self.assertIsNone(current["processes_total"])
        self.assertEqual(current["checked_at"], before["checked_at"])

    async def test_scan_started_before_native_activity_is_not_presented_as_fresh(self):
        task = await self.start_task("hold")
        gate = self.gates["scan-activity"] = FaultGate("scan-activity")
        async def scanning(projects):
            await gate.hit()
            return observation()
        self.service.process_scanner = scanning
        pending = asyncio.create_task(self.service.scan_workspace(task["project"]))
        try:
            await gate.wait()
            await self.service.on_message({"method": "thread/status/changed", "params": {
                "threadId": task["thread_id"], "status": {"type": "active", "activeFlags": []}}})
        finally:
            gate.release.set()
        # Even a late publisher of this result cannot make the old zero current.
        self.service.os_process_observations[task["project"]] = await asyncio.wait_for(pending, 5)
        current = self.service.get(task["id"])["process_state"]["os_process_check"]
        self.assertEqual(current["reason"], "not_checked")
        self.assertIsNone(current["processes_total"])
        self.assertIsInstance(current["checked_at"], (int, float))

    async def test_backend_generation_change_invalidates_the_previous_snapshot(self):
        task = await self.start_task("hold")
        before = await self.snapshot(task)
        self.service.backend.generation += 1
        current = self.service.get(task["id"])["process_state"]["os_process_check"]
        self.assertEqual(current["reason"], "not_checked")
        self.assertIsNone(current["processes_total"])
        self.assertEqual(current["checked_at"], before["checked_at"])

    async def test_delete_results_timestamp_saved_snapshots_and_label_unknown_age(self):
        for index, checked_at in enumerate((42, None)):
            task = await self.start_task(key=f"delete-snapshot-{index}")
            await self.until(task["id"], ("completed",))
            stored = {"available": True, "reason": "ok", "processes_total": 0, "pids": [], "truncated": False}
            if checked_at is not None:
                stored["checked_at"] = checked_at
            self.service.lifecycle.metadata(task["id"], {"process_state": {"os_process_check": stored}})
            result = await self.service.delete(task["id"], task["thread_id"])
            snapshot = result["process_state"]["os_process_check"]
            self.assertIn("checked_at", snapshot)
            self.assertEqual(snapshot["checked_at"], checked_at)
            if checked_at is None:
                self.assertEqual(snapshot["reason"], "not_checked")
                self.assertIsNone(snapshot["processes_total"])
            self.assertEqual(self.service.get(task["id"])["process_state"]["os_process_check"], snapshot)
