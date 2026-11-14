"""A refused operation key never represents uncertain native work."""

import asyncio
import json
from unittest.mock import patch

from codex_local_mcp.security import BridgeError
from codex_local_mcp.retention import Retention
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class AdmissionRejectionTest(ServiceHarness):
    async def rejected_twice(self, tool, args):
        errors = []
        for _ in range(2):
            with self.assertRaises(BridgeError) as error:
                await dispatch(self.service, tool, args)
            errors.append(error.exception.as_dict())
        self.assertEqual(errors[0], errors[1])
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id=?", (args["operation_id"],)).fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM admissions WHERE operation_id=?", (args["operation_id"],)).fetchone())
        self.assertFalse(any(lock.locked() for lock in self.service.family_locks.values()))
        if args.get("task_id"):
            report = self.service.get(args["task_id"], view="reconciliation")["reconciliation"]
            operation = next(op for op in report["operations"] if op["operation_id"] == args["operation_id"])
            self.assertEqual(operation["acknowledgement"], "rejected")
        return errors[0]

    async def test_synchronous_admission_refusal_is_finished_and_cached(self):
        task = await self.start_task("hold")
        error = await self.rejected_twice("codex_start", {"workdir": task["project"], "prompt": "other", "operation_id": "refused-start"})
        self.assertEqual(error["code"], "workspace_busy")
        row = self.store.db.execute("SELECT task_id FROM operation_details WHERE id='refused-start'").fetchone()
        self.assertIsNone(row[0])

    async def test_resume_admission_refusal_is_finished_and_eligible_for_expiry(self):
        first = await self.start_task()
        await self.until(first["id"], ("completed",))
        await self.service.reconcile_claims(first["id"])
        second = await self.start_task("hold", key="second-family")
        await self.rejected_twice("codex_resume", {"task_id": first["id"], "operation_id": "refused-resume"})
        await self.rejected_twice("codex_start", {"workdir": first["project"], "prompt": "other", "operation_id": "refused-new-start"})
        await self.service.interrupt(second["id"], second["turn_id"])
        await self.until(second["id"], ("interrupted",))
        await self.service.reconcile_claims(second["id"])
        with self.store.transaction():
            self.store.db.execute("UPDATE operation_times SET updated_at=1")
        plan = Retention(self.store, clock=lambda: 10**12).plan(100)
        Retention(self.store, clock=lambda: 10**12).commit(plan["plan_id"])
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM operation_tombstones WHERE id='refused-new-start'").fetchone())
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM operation_tombstones WHERE id='refused-resume'").fetchone())

    async def test_deferred_admission_refusal_is_finished_and_cached(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.store.claim(task["id"], "background", "uncertain", {"reason": "turn_completion"})
        async def still_active(ids):
            self.store.claim(task["id"], "background", "active")
        with patch.object(self.service, "wait_for_reconciliation", still_active):
            await self.rejected_twice("codex_start", {"workdir": task["project"], "prompt": "other", "operation_id": "deferred-start"})

    async def test_preflight_controls_are_finished_without_new_claims(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"])
        cases = (("codex_interrupt", {"expected_turn_id": "stale"}),
                 ("codex_delete", {"confirm_thread_id": "wrong"}),
                 ("codex_send", {"prompt": "steer", "mode": "steer", "expected_turn_id": "stale"}))
        for index, (tool, extra) in enumerate(cases):
            before = self.store.claims(task_id=task["id"])
            with self.subTest(tool=tool):
                await self.rejected_twice(tool, {"task_id": task["id"], "operation_id": f"preflight-{index}", **extra})
                self.assertEqual(self.store.claims(task_id=task["id"]), before)
        self.service.lifecycle.archive(task["id"], "archive")
        before = self.store.claims(task_id=task["id"])
        await self.rejected_twice("codex_send", {"task_id": task["id"], "prompt": "never sent", "operation_id": "archived-preflight"})
        self.assertEqual(self.store.claims(task_id=task["id"]), before)

    async def test_partial_admission_failure_releases_the_reservation(self):
        original = self.service._admit
        def refused(*args):
            original(*args)
            raise BridgeError("Rejected after reservation", code="state_conflict")
        self.service.projects("create", str(self.root / "a"))
        with patch.object(self.service, "_admit", refused):
            await self.rejected_twice("codex_start", {"workdir": str(self.root / "a"), "prompt": "never sent", "operation_id": "reserved-then-refused"})
        self.assertFalse(self.store.claims())
        await self.start_task("hold", key="after-admission-rejection")

    async def test_historical_uncertain_rejections_are_not_reclassified_or_replayed(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        error = BridgeError("Old workspace refusal", code="workspace_busy").as_dict()
        with self.store.transaction():
            self.store.db.execute("INSERT INTO operations VALUES ('old-refusal','fp','uncertain',?,?)", (json.dumps({"error_data": error}), task["id"]))
        before = self.store.db.execute("SELECT * FROM operations WHERE id='old-refusal'").fetchone()
        self.assertEqual(next(o for o in self.service.get(task["id"], view="reconciliation")["reconciliation"]["operations"]
            if o["operation_id"] == "old-refusal")["acknowledgement"], "unknown")
        self.assertEqual(tuple(before), tuple(self.store.db.execute("SELECT * FROM operations WHERE id='old-refusal'").fetchone()))

    async def test_attempted_write_in_child_task_and_local_change_keep_uncertainty(self):
        task = await self.start_task("hold")
        async def write_then_refuse():
            await asyncio.create_task(self.service.backend.call("model/list", {}))
            raise BridgeError("Rejected after a native request", code="state_conflict")
        async def change_then_refuse():
            self.service.lifecycle.metadata(task["id"], {"name": "Committed metadata"})
            raise BridgeError("Rejected after a local change", code="state_conflict")
        for key, function in (("child-write", write_then_refuse), ("local-write", change_then_refuse)):
            with self.subTest(key=key), self.assertRaises(BridgeError):
                await self.service.operation(key, "codex_rename", {"task_id": task["id"]}, function)
            self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id=?", (key,)).fetchone()[0], "uncertain")
