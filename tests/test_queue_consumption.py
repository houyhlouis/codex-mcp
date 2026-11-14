"""Acknowledged queued work owns its execution even across delayed refreshes."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp.backend import RpcError
from codex_local_mcp.tools import dispatch
from tests.helpers import FaultGate
from tests.test_service import ServiceHarness
from tests.test_workspace_processes import observation


class QueueConsumptionTest(ServiceHarness):
    async def idle_fork(self):
        parent = await self.start_task()
        await self.until(parent["id"], ("completed",))
        return await self.service.fork(parent["id"])

    async def test_consumption_during_first_post_ack_refresh_records_execution(self):
        self.service.config = replace(self.cfg, sandbox_process_check=True)
        result = observation()
        async def scan(projects):
            return result
        self.service.process_scanner = scan
        fork = await self.idle_fork()
        self.assertIsNone(fork["workspace_execution_project"])
        gate = self.gates["post-ack-refresh"] = FaultGate("post-ack-refresh")
        original, acknowledged = self.service.backend.call, False
        async def delayed(method, params, **kwargs):
            nonlocal acknowledged
            if method == "thread/queue/list" and acknowledged and not gate.used:
                await gate.hit()
            reply = await original(method, params, **kwargs)
            if method == "thread/queue/add":
                acknowledged = True
            return reply
        with patch.object(self.service.backend, "call", delayed):
            adding = asyncio.create_task(dispatch(self.service, "codex_queue", {"task_id": fork["id"],
                "action": "add", "prompt": "hold_command", "request_id": "queued-first-work"}))
            try:
                await gate.wait()
                started = await original("thread/queue/start", {"threadId": fork["thread_id"]})
                async with self.service.changed:
                    await asyncio.wait_for(self.service.changed.wait_for(
                        lambda: self.store.task_head(fork["id"]).get("turn_id") == started["turn"]["id"]), 5)
                marker = self.store.task_head(fork["id"]).get("workspace_execution_project")
            finally:
                gate.release.set()
                await asyncio.wait_for(adding, 5)
        self.assertEqual(marker, fork["project"])
        result = observation(1234)
        await self.service.interrupt(fork["id"], started["turn"]["id"])
        await self.until(fork["id"], ("interrupted",))
        await self.service.reconcile_claims(fork["id"], explicit=True)
        following = await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold",
            "request_id": "owning-follow-up"})
        self.assertEqual(following["status"], "running")

    async def test_rejected_add_does_not_accept_the_reservation_or_grant_ownership(self):
        fork = await self.idle_fork()
        original = self.service.backend.call
        async def rejected(method, params, **kwargs):
            if method == "thread/queue/add":
                kwargs["before_send"]()
                raise RpcError({"code": -32600, "message": "Rejected queued input"})
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", rejected), self.assertRaises(RpcError):
            await dispatch(self.service, "codex_queue", {"task_id": fork["id"], "action": "add",
                "prompt": "hold", "request_id": "rejected-queued-work"})
        current = self.store.task_head(fork["id"])
        self.assertIsNone(current.get("workspace_execution_project"))
        self.assertIsNone(current.get("queue_reservation"))

    async def test_applied_add_with_lost_ack_does_not_grant_ownership(self):
        fork = await self.idle_fork()
        original = self.service.backend.call
        async def lost(method, params, **kwargs):
            reply = await original(method, params, **kwargs)
            if method == "thread/queue/add":
                raise TimeoutError("Accepted queued input lost its acknowledgement")
            return reply
        with patch.object(self.service.backend, "call", lost), self.assertRaises(TimeoutError):
            await dispatch(self.service, "codex_queue", {"task_id": fork["id"], "action": "add",
                "prompt": "hold", "request_id": "unknown-queued-work"})
        current = self.store.task_head(fork["id"])
        self.assertIsNone(current.get("workspace_execution_project"))
        self.assertEqual(current["queue_reservation"]["outcome"], "uncertain")
        native = await original("thread/queue/list", {"threadId": fork["thread_id"]})
        self.assertEqual([q["clientUserMessageId"] for q in native["data"]], ["unknown-queued-work"])
