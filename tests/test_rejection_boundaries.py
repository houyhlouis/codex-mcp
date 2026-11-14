"""A cached rejection owns neither late workers nor project registration."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class RejectionBoundaryTest(ServiceHarness):
    async def test_child_cannot_change_local_state_after_parent_rejection(self):
        task = await self.start_task("hold")
        entered, release = asyncio.Event(), asyncio.Event()
        workers = []
        async def child():
            entered.set()
            await release.wait()
            self.service.lifecycle.metadata(task["id"], {"name": "late rejected worker"})
        async def refused():
            workers.append(asyncio.create_task(child()))
            await entered.wait()
            raise BridgeError("Preflight refusal", code="state_conflict")
        with self.assertRaisesRegex(BridgeError, "Preflight refusal"):
            await self.service.operation("rejected-parent", "codex_rename", {"task_id": task["id"]}, refused)
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id='rejected-parent'").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])
        before = self.store.get("tasks", task["id"])
        release.set()
        outcome = (await asyncio.gather(*workers, return_exceptions=True))[0]
        self.assertIsInstance(outcome, BridgeError)
        self.assertEqual(outcome.code, "state_conflict")
        self.assertEqual(self.store.get("tasks", task["id"]), before)

    async def test_capacity_rejection_does_not_register_a_new_project(self):
        self.service.config = replace(self.cfg, max_tasks=1)
        await self.start_task("hold")
        path = self.root / "unregistered"
        path.mkdir(mode=0o700)
        before = self.store.all("projects")
        native = self.service.backend.next_id
        args = {"workdir": str(path), "prompt": "never sent", "operation_id": "refused-project"}
        for _ in range(2):
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, "codex_start", args)
            self.assertEqual(error.exception.code, "workspace_busy")
        self.assertEqual(self.store.all("projects"), before)
        self.assertEqual(self.service.backend.next_id, native)
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id='refused-project'").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])

    async def test_deferred_capacity_rejection_does_not_register_a_new_project(self):
        self.service.config = replace(self.cfg, max_tasks=1)
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        path = self.root / "unregistered"
        path.mkdir(mode=0o700)
        before = self.store.all("projects")
        async def still_active(ids):
            self.store.claim(task["id"], "background", "active")
        with patch.object(self.service, "wait_for_reconciliation", still_active), self.assertRaises(BridgeError):
            await dispatch(self.service, "codex_start", {"workdir": str(path), "prompt": "never sent", "operation_id": "deferred-project"})
        self.assertEqual(self.store.all("projects"), before)
