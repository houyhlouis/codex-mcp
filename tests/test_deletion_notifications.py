"""Native deletion events and explicit deletion share one terminal outcome."""

import asyncio
from unittest.mock import patch

from codex_local_mcp import processes
from codex_local_mcp.security import BridgeError
from tests.test_service import ServiceHarness


class DeletionNotificationTest(ServiceHarness):
    async def pending(self, *, child=False):
        root = await self.start_task("native_children" if child else "echo")
        if child:
            async with self.service.changed:
                await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(root["id"]))), 5)
            request = self.service.requests(root["id"])[0]
            task = self.service.task(request["task_id"])
            await self.service.respond(request["id"], {"decision": "decline"})
        else:
            task = root
        await self.until(root["id"], ("completed",))
        for key in self.service.family_ids(root["id"]):
            await self.service.reconcile_claims(key, explicit=True)
        self.store.claim(task["id"], "background", "active", {"commands_preview": ["known previous command"]})
        await self.service.close()
        self.service = self.new_service()
        await self.service.ensure_backend()
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
        return root, self.service.get(task["id"])

    async def notified(self, task):
        await self.service.on_message({"method": "thread/deleted", "params": {"threadId": task["thread_id"]}})

    async def test_requested_notification_before_ack_and_reconciliation_is_atomic(self):
        _, task = await self.pending()
        gate = self.gate_call("delete_ack", "thread/delete")
        deleting = asyncio.create_task(self.service.delete(task["id"], task["thread_id"]))
        try:
            await gate.wait()
            self.service.automatic_reconciliation = True
            await self.notified(task)
            await self.reconciliation_idle()
            current = self.service.get(task["id"])
            self.assertTrue(current["deleted"])
            self.assertFalse(current["workspace_reserved"])
            self.assertTrue(current["process_state"]["acknowledged"])
            self.assertEqual(current["process_state"]["deletion_outcome"], "confirmed_delete")
        finally:
            gate.release.set()
            outcome = (await asyncio.gather(deleting, return_exceptions=True))[0]
        if isinstance(outcome, BaseException):
            raise outcome
        self.assertTrue(outcome["deleted"])
        await self.start_task("hold", key="after-delete")

    async def test_late_notification_after_lost_delete_ack_keeps_the_confirmation(self):
        _, task = await self.pending()
        gate = self.gate_call("lost_delete_ack", "thread/delete", error=TimeoutError("Lost delete acknowledgement"))
        deleting = asyncio.create_task(self.service.delete(task["id"], task["thread_id"]))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await deleting
        await self.notified(task)
        current = self.service.get(task["id"])
        self.assertTrue(current["deleted"])
        self.assertFalse(current["workspace_reserved"])
        self.assertTrue(current["process_state"]["acknowledged"])
        self.assertEqual(current["process_state"]["deletion_outcome"], "confirmed_delete")

    async def test_unsolicited_notification_retains_warning_without_ownership(self):
        _, task = await self.pending()
        await self.notified(task)
        await self.service.reconcile_claims(task["id"], explicit=True)
        current = self.service.get(task["id"])
        self.assertTrue(current["deleted"])
        self.assertFalse(current["workspace_reserved"])
        self.assertFalse(current["process_state"]["acknowledged"])
        self.assertEqual(current["process_state"]["deletion_outcome"], "native_deleted_unacknowledged")
        self.assertEqual(current["process_state"]["commands_preview"], ["known previous command"])
        event = next(e for e in reversed(self.store.events(0, 200, task_id=task["id"])["events"]) if e["kind"] == "bridge/deleted")
        self.assertEqual(event["data"]["process_state"], current["process_state"])
        self.assertFalse(self.store.claim(task["id"], "turn", "active"))
        self.assertFalse(self.store.claims(task_id=task["id"]))
        await self.start_task("hold", key="after-external-delete")

    async def test_confirmed_tombstone_acknowledges_locally_without_native_calls(self):
        _, task = await self.pending()
        await self.notified(task)
        before = self.service.backend.next_id
        result = await self.service.delete(task["id"], task["thread_id"])
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertEqual(result["process_state"]["deletion_outcome"], "confirmed_delete")
        self.assertEqual(self.service.backend.next_id, before)
        self.assertFalse(self.store.claims(task_id=task["id"]))

    async def test_child_notification_does_not_delete_its_parent(self):
        parent, child = await self.pending(child=True)
        await self.notified(child)
        await self.service.reconcile_claims(child["id"], explicit=True)
        self.assertFalse(self.service.get(parent["id"]).get("deleted", False))
        result = self.service.get(child["id"])
        self.assertTrue(result["deleted"])
        self.assertFalse(self.store.claims(task_id=child["id"]))
        self.assertFalse(result["process_state"]["acknowledged"])
        self.assertEqual(result["process_state"]["deletion_outcome"], "native_deleted_unacknowledged")

    async def test_requested_family_delete_acknowledges_a_notified_pending_child(self):
        parent, child = await self.pending(child=True)
        gate = self.gate_call("family_delete_ack", "thread/delete")
        deleting = asyncio.create_task(self.service.delete(parent["id"], parent["thread_id"]))
        try:
            await gate.wait()
            self.service.automatic_reconciliation = True
            await self.notified(child)
            await self.reconciliation_idle()
            self.assertTrue(self.service.get(child["id"])["process_state"]["acknowledged"])
        finally:
            gate.release.set()
            outcome = (await asyncio.gather(deleting, return_exceptions=True))[0]
        if isinstance(outcome, BaseException):
            raise outcome
        self.assertTrue(outcome["deleted"])
        self.assertTrue(self.service.get(child["id"])["deleted"])
        self.assertFalse(self.service.get(parent["id"])["workspace_reserved"])

    async def test_unsolicited_parent_notification_does_not_release_child_ownership(self):
        parent, child = await self.pending(child=True)
        await self.notified(parent)
        self.assertTrue(self.service.get(parent["id"])["deleted"])
        self.assertFalse(self.service.get(child["id"]).get("deleted", False))
        self.assertTrue(self.store.claims(task_id=child["id"]))
        self.assertFalse(self.service.get(child["id"])["process_state"]["acknowledged"])

    async def test_deletion_of_a_quarantined_task_still_releases_its_own_claims(self):
        _, task = await self.pending()
        self.service.protocol_quarantine.add(task["id"])
        await self.notified(task)
        self.assertTrue(self.service.get(task["id"])["deleted"])
        self.assertFalse(self.store.claims(task_id=task["id"]))

    async def test_deletion_expires_a_pending_approval(self):
        task = await self.start_task("approval")
        request = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        await self.notified(task)
        self.assertEqual(self.store.get("requests", request["id"])["status"], "expired")
        self.assertNotIn(request["id"], self.service.raw_requests)
        with self.assertRaises(BridgeError) as failure:
            await self.service.respond(request["id"], {"decision": "decline"})
        self.assertEqual(failure.exception.code, "stale_request")

    async def test_orphan_process_claim_is_repaired_and_its_family_owner_is_visible(self):
        parent, child = await self.pending(child=True)
        self.store.update("tasks", child["id"], {"process_state": None})
        await self.service.close()
        self.service = self.new_service()
        self.assertTrue(self.service.get(child["id"])["process_state"]["previous_backend_unverified"])
        root = self.service.get(parent["id"])
        self.assertFalse(root.get("process_state"))
        claim = next(c for c in root["workspace_claims"] if c["data"].get("reason") == "processes_unverified")
        self.assertEqual(claim["task_id"], child["id"])
        self.assertIn("codex_resume", claim["data"]["error"])
        report = self.service.get(parent["id"], view="reconciliation")["reconciliation"]
        self.assertTrue(any(step["tool"] == "codex_resume" and step["arguments"]["task_id"] == child["id"] for step in report["next_steps"]))

    async def test_unsolicited_deletion_preserves_an_orphan_process_warning(self):
        _, task = await self.pending()
        self.store.update("tasks", task["id"], {"process_state": None})
        await self.notified(task)
        state = self.service.get(task["id"])["process_state"]
        self.assertFalse(state["acknowledged"])
        self.assertEqual(state["deletion_outcome"], "native_deleted_unacknowledged")
        self.assertFalse(self.store.claims(task_id=task["id"]))

    async def test_deletion_state_redacts_before_bounding_compatible_fields(self):
        _, task = await self.pending()
        task = self.service.task(task["id"])
        task["process_state"] = {**task["process_state"], "future_context": "eyJ" + "x" * 3000 + ".part.signature"}
        state = processes.deletion_state(self.store, task, requested=False)
        self.assertEqual(state["future_context"], "[REDACTED]")

    async def test_late_activity_for_a_deleted_child_does_not_load_it(self):
        _, child = await self.pending(child=True)
        await self.notified(child)
        native = self.service.backend.next_id
        await self.service.on_message({"method": "thread/status/changed", "params": {"threadId": child["thread_id"], "status": {"type": "active", "activeFlags": []}}})
        self.assertEqual(self.service.backend.next_id, native)
        self.assertFalse(self.store.claims(task_id=child["id"]))

    async def test_late_archive_and_unarchive_cannot_reserve_a_deleted_task(self):
        _, task = await self.pending()
        await self.notified(task)
        for method in ("thread/archived", "thread/unarchived"):
            await self.service.on_message({"method": method, "params": {"threadId": task["thread_id"]}})
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertTrue(self.service.get(task["id"])["deleted"])
        self.assertFalse(self.store.claims(task_id=task["id"]))

    async def test_deletion_state_and_claims_roll_back_together_on_write_failure(self):
        _, task = await self.pending()
        before = self.service.get(task["id"])
        original = self.store.claim
        def failed(*args, **kwargs):
            result = original(*args, **kwargs)
            raise OSError("Injected delete write failure")
        with patch.object(self.store, "claim", failed), self.assertRaises(OSError):
            self.service.lifecycle.archive(task["id"], "delete")
        current = self.service.get(task["id"])
        self.assertEqual(current.get("deleted"), before.get("deleted"))
        self.assertEqual(current["workspace_claims"], before["workspace_claims"])
        self.assertEqual(current["process_state"], before["process_state"])
