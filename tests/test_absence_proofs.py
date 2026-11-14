"""Local retirement of unreadable native history needs two independent reads."""

from unittest.mock import patch

from codex_local_mcp.backend import OptionalCapabilityUnavailable, RpcError
from codex_local_mcp.security import BridgeError
from tests.test_missing_conversations import MissingConversationHarness


class AbsenceProofTest(MissingConversationHarness):
    async def test_single_transient_error_does_not_retire_existing_unloaded_history(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.reconcile_claims(task["id"], explicit=True)
        await self.service.close()
        self.service = self.new_service()
        await self.service.ensure_backend()
        original, reads, deletes = self.service.backend.call, [], []
        async def transient(method, params, **kwargs):
            if method == "thread/read" and params["threadId"] == task["thread_id"]:
                reads.append(method)
                if len(reads) == 1:
                    raise RpcError({"code": -32603, "message": "Temporary read failure"})
            if method == "thread/delete":
                deletes.append(params)
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", transient), self.assertRaises(BridgeError):
            await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(len(reads), 2)
        self.assertEqual(deletes, [])
        self.assertFalse(self.service.get(task["id"]).get("deleted", False))

    async def test_second_unreadable_rpc_error_is_required_and_sufficient(self):
        task = await self.missing()
        original, reads, deletes = self.service.backend.call, [], []
        async def unreadable(method, params, **kwargs):
            if method == "thread/read" and params["threadId"] == task["thread_id"]:
                reads.append(method)
                raise RpcError({"code": -32603, "message": "Still unreadable"})
            if method == "thread/delete":
                deletes.append(params)
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", unreadable):
            result = await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(len(reads), 2)
        self.assertEqual(deletes, [])
        self.assertEqual(result["native_conversation"], "unavailable")
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_unusable_second_read_is_never_absence_proof(self):
        task = await self.missing()
        original = self.service.backend.call
        cases = [RpcError({"code": code, "message": "Read refused"}) for code in (-32601, -32602)]
        cases += [OptionalCapabilityUnavailable("thread/read", "fixture", {}),
            BridgeError("Disconnected", code="transport_unavailable"), BridgeError("No ACK", code="native_timeout"),
            TimeoutError("Read timeout"), OSError("Read disconnected")]
        for failure in cases:
            reads, deletes = [], []
            async def failed(method, params, **kwargs):
                if method == "thread/read" and params["threadId"] == task["thread_id"]:
                    reads.append(method)
                    if len(reads) == 2:
                        raise failure
                    raise RpcError({"code": -32600, "message": "Unreadable"})
                if method == "thread/delete":
                    deletes.append(params)
                return await original(method, params, **kwargs)
            with self.subTest(failure=type(failure).__name__), patch.object(self.service.backend, "call", failed):
                with self.assertRaises(BridgeError):
                    await self.service.delete(task["id"], task["thread_id"])
                self.assertEqual(len(reads), 2)
                self.assertEqual(deletes, [])
                self.assertFalse(self.service.get(task["id"]).get("deleted", False))
                self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_second_read_and_loaded_check_follow_all_present_delete_acks(self):
        task = await self.missing()
        original, calls = self.service.backend.call, []
        async def recorded(method, params, **kwargs):
            calls.append(method)
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", recorded):
            await self.service.delete(task["id"], task["thread_id"])
        self.assertEqual(calls[-2:], ["thread/loaded/list", "thread/read"])
