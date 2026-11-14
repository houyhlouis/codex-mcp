"""Fail-closed release and acknowledged execution ownership with injected scans."""

import asyncio
import copy
import sys
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp.backend import RpcError
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.helpers import ReconciliationClock
from tests.helpers import link_or_skip
from tests.test_service import ServiceHarness
from tests.test_workspace_processes import observation


class ScanReviewTest(ServiceHarness):
    automatic_reconciliation = True

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.config = replace(self.cfg, sandbox_process_check=True)
        self.result = observation()
        async def scan(projects):
            return copy.deepcopy(self.result)
        self.service.process_scanner = scan
        self.clock = ReconciliationClock(self.service)
        self.service.reconciliation_timer = self.clock.later

    async def finish_with_scan(self, result):
        task = await self.start_task("hold_command")
        self.result = result
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.reconciliation_idle()
        return task

    async def test_first_unreadable_release_keeps_a_claim_without_churn(self):
        task = await self.finish_with_scan(observation(available=False, reason="proc_unreadable"))
        claims = self.store.claims(task_id=task["id"])
        self.assertEqual([(c["kind"], c["state"], c["data"]["reason"]) for c in claims],
            [("background", "uncertain", "process_check_unavailable")])
        writes = self.store.db.total_changes
        for delay in (5, 10, 20):
            self.assertEqual([seconds for _, seconds, _ in self.clock.pending], [delay])
            self.clock.advance()
            await self.reconciliation_idle()
            self.assertEqual(self.store.db.total_changes, writes)
        with self.assertRaisesRegex(BridgeError, "process list could not be read.*doctor"):
            await self.start_task(key="unreadable")

    async def test_partial_positive_retains_release_and_blocks_new_family(self):
        task = await self.finish_with_scan(observation(1234, available=False, reason="proc_unreadable"))
        claim = self.store.claims(task_id=task["id"])[0]
        self.assertEqual(claim["data"]["pids"], [1234])
        self.assertEqual(claim["state"], "active")
        with self.assertRaisesRegex(BridgeError, "PID 1234"):
            await self.start_task(key="partial-owner")

    async def test_partial_positive_without_owner_is_a_finished_rejection(self):
        self.result = observation(1234, available=False, reason="proc_unreadable")
        with self.assertRaisesRegex(BridgeError, "PID 1234.*No task accounts"):
            await self.start_task(key="partial-unowned")
        row = self.store.db.execute("SELECT state FROM operations WHERE id='partial-unowned'").fetchone()
        self.assertEqual(row[0], "done")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    async def test_member_deletion_does_not_expose_a_survivor_on_first_failed_scan(self):
        parent = await self.start_task()
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        for index, result in enumerate((observation(available=False, reason="proc_unreadable"),
                observation(1234, available=False, reason="proc_unreadable"))):
            self.result = observation()
            child = await self.service.fork(parent["id"])
            self.service.lifecycle.metadata(child["id"], {"parent_task_id": parent["id"], "root_task_id": parent["id"]})
            self.result = result
            await self.service.delete(child["id"], child["thread_id"])
            await self.reconciliation_idle()
            self.assertFalse(self.store.claims(task_id=child["id"]))
            claims = self.store.claims(task_id=parent["id"])
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["data"]["source"], "os_process_scan")
            with self.assertRaises(BridgeError) as error:
                await self.start_task(key=f"member-{index}")
            self.assertEqual(error.exception.code, "workspace_busy")
            self.result = observation()
            await self.service.reconcile_claims(parent["id"], explicit=True)

    async def test_rejected_first_start_input_is_not_execution_proof(self):
        parent = await self.start_task()
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        fork = await self.service.fork(parent["id"])
        original = self.service.backend.call
        async def lost(method, params, **kwargs):
            if method == "turn/start":
                kwargs["before_send"]()
                raise TimeoutError("Unknown start outcome")
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", lost), self.assertRaises(TimeoutError):
            await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "lost-first-start"})
        self.assertFalse(self.service.family_executed(fork["id"], fork["project"]))
        self.assertIsNone(self.store.task_head(fork["id"]).get("workspace_execution_project"))
        await self.service.resume(fork["id"])
        self.result = observation(1234)
        with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
            await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "retry-lost-first-start"})

    async def test_successful_start_marks_execution_only_after_acknowledgement(self):
        gate = self.gate_call("execution-ack", "turn/start")
        starting = asyncio.create_task(self.start_task("hold"))
        try:
            await gate.wait()
            task = next(t for _, t in self.store.records("tasks", light=True))
            self.assertIsNone(task.get("workspace_execution_project"))
        finally:
            gate.release.set()
        task = await asyncio.wait_for(starting, 5)
        self.assertEqual(self.store.task_head(task["id"])["workspace_execution_project"], task["project"])

    async def test_real_scanner_rejects_alias_paths_at_admission(self):
        from codex_local_mcp import procscan
        project = self.root / "alias-project"
        project.mkdir()
        alias = self.root / "alias"
        link_or_skip(self, alias, project)
        proc = self.base / "fake-proc"
        entry = proc / "1234"
        entry.mkdir(parents=True)
        (entry / "stat").write_bytes(b"1234 (sandbox) S " + b"0 " * 18 + b"88 0\n")
        async def scan(projects):
            self.assertEqual(projects, [str(project.resolve())])
            return procscan.scan(projects, proc_root=proc)
        self.service.process_scanner = scan
        spellings = ["/" + str(project), str(project) + "/", str(alias)]
        if sys.platform != "linux":
            spellings.pop(0)
        for index, spelling in enumerate(spellings):
            (entry / "cmdline").write_bytes(b"codex-linux-sandbox\0--command-cwd\0" + spelling.encode() + b"\0")
            with self.assertRaisesRegex(BridgeError, "PID 1234.*No task accounts"):
                await self.start_task(path="alias-project", key=f"alias-work-{index}")

    async def test_rejected_or_unknown_queue_reorder_cannot_grant_fork_ownership(self):
        parent = await self.start_task()
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        fork = await self.service.fork(parent["id"])
        original = self.service.backend.call
        for number, error in enumerate((RpcError({"code": -32600, "message": "rejected"}), TimeoutError("lost ack"))):
            async def reject(method, params, **kwargs):
                if method == "thread/queue/reorder":
                    kwargs["before_send"]()
                    raise error
                return await original(method, params, **kwargs)
            with patch.object(self.service.backend, "call", reject), self.assertRaises(type(error)):
                await dispatch(self.service, "codex_queue", {"task_id": fork["id"], "action": "reorder",
                    "ordered_ids": ["missing"], "request_id": f"queue-no-work-{number}"})
            self.assertIsNone(self.store.task_head(fork["id"]).get("workspace_execution_project"))
            self.result = observation(1234)
            with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
                await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold",
                    "request_id": f"fork-no-work-{number}"})
            self.result = observation()

    async def test_budget_only_goal_edit_cannot_bypass_first_work_admission(self):
        parent = await self.start_task()
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        fork = await self.service.fork(parent["id"])
        # Native activity does not establish a bridge-acknowledged execution.
        await self.service.backend.call("thread/goal/set", {"threadId": fork["thread_id"], "origin": "user",
            "objective": "Native goal", "status": "active"})
        self.result = observation(1234)
        with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
            await dispatch(self.service, "codex_goal", {"task_id": fork["id"], "action": "set", "token_budget": 10,
                "user_requested": True, "request_id": "goal-budget-no-work"})
        self.assertIsNone(self.store.task_head(fork["id"]).get("workspace_execution_project"))
