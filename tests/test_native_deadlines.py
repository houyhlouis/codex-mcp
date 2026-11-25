"""Native RPC deadlines at the shared writer and the durable service boundary."""

import asyncio
import json
import unittest
import weakref
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

from codex_local_mcp.backend import AppServer, NATIVE_RPC_TIMEOUT
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.helpers import FaultGate
from tests.test_backend_backpressure import MemoryProcess, encoded
from tests.test_pipe_lifetimes import reference_counting_only
from tests.test_service import ServiceHarness


@contextmanager
def rpc_clock():
    # Advance real loop deadlines at known I/O boundaries. Watchdogs still tick.
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0

    def advance(seconds):
        nonlocal offset
        offset += seconds

    with patch.object(loop, "time", lambda: real_time() + offset), \
            patch.object(loop, "slow_callback_duration", float("inf")):
        yield advance


class NativeDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = AppServer(None, AsyncMock(), AsyncMock())
        self.backend.process = MemoryProcess()
        self.writer = self.backend.process.stdin
        self.backend.reader_task = asyncio.create_task(self.backend._read())
        self.backend.event_task = asyncio.create_task(self.backend._dispatch_events())
        self.addAsyncCleanup(self.backend.close)
        self.gate = FaultGate("stdin drain")

    async def test_stalled_drain_releases_queued_interrupt_response_and_inflight_call(self):
        backend = self.backend
        interrupt_guard, response_guard = Mock(), Mock()
        # Recovery must release the in-flight read even if stdout stays open.
        with rpc_clock() as advance, patch.object(backend.process.stdout, "feed_eof"):
            reading = asyncio.create_task(backend.call("thread/read", {}, timeout=120))
            self.assertEqual((await asyncio.wait_for(self.writer.requests.get(), 5))["method"], "thread/read")
            self.writer.drain = self.gate.hit
            starting = asyncio.create_task(backend.call("turn/start", {}, timeout=20))
            await self.gate.wait()
            interrupting = asyncio.create_task(backend.call("turn/interrupt", {}, timeout=5, before_send=interrupt_guard))
            responding = asyncio.create_task(backend.respond(7, {"decision": "accept"}, before_send=response_guard))
            await asyncio.sleep(0)  # Both writers have reached the occupied lock.
            futures = list(backend.pending.values())
            self.assertEqual(len(futures), 3)

            advance(6)
            with self.assertRaises(BridgeError) as caught:
                await asyncio.wait_for(interrupting, 5)
            self.assertEqual(caught.exception.code, "native_timeout")
            self.assertTrue(caught.exception.retryable)
            self.assertFalse(caught.exception.uncertain)
            self.assertTrue(backend.alive)
            self.assertTrue(backend.write_lock.locked())

            advance(16)
            submitted, queued, inflight = await asyncio.wait_for(
                asyncio.gather(starting, responding, reading, return_exceptions=True), 5)
            await asyncio.wait_for(backend.reader_task, 5)
        self.assertEqual((submitted.code, submitted.retryable, submitted.uncertain), ("native_timeout", False, True))
        self.assertEqual((queued.code, queued.retryable, queued.uncertain), ("transport_unavailable", True, False))
        self.assertEqual((inflight.code, inflight.retryable, inflight.uncertain), ("transport_unavailable", False, True))
        interrupt_guard.assert_not_called()
        response_guard.assert_not_called()
        self.assertEqual(self.writer.requests.get_nowait()["method"], "turn/start")
        self.assertTrue(self.writer.requests.empty())
        self.assertFalse(backend.pending)
        self.assertTrue(all(future.done() for future in futures))
        self.assertFalse(backend.write_lock.locked())
        self.assertFalse(backend.alive)
        self.assertTrue(backend.event_task.done())
        backend.on_exit.assert_awaited_once()

    async def test_expired_budget_skips_guard_even_with_free_lock(self):
        guard = Mock()
        with self.assertRaises(BridgeError) as caught:
            await self.backend.call("turn/start", {}, timeout=0, before_send=guard)
        self.assertEqual((caught.exception.code, caught.exception.retryable, caught.exception.uncertain),
                         ("native_timeout", True, False))
        guard.assert_not_called()
        self.assertTrue(self.writer.requests.empty())
        self.assertTrue(self.backend.alive)
        self.assertFalse(self.backend.pending)

    async def test_notification_deadline_after_guard_is_uncertain(self):
        with rpc_clock() as advance, self.assertRaises(BridgeError) as caught:
            await self.backend.send({"method": "initialized", "params": {}},
                                    before_send=lambda: advance(NATIVE_RPC_TIMEOUT + 1))
        self.assertEqual((caught.exception.code, caught.exception.retryable, caught.exception.uncertain),
                         ("native_timeout", False, True))
        self.assertTrue(self.writer.requests.empty())
        self.assertFalse(self.backend.alive)
        self.assertFalse(self.backend.write_lock.locked())

    async def test_cancellation_in_drain_fences_connection_and_cleans_up(self):
        self.writer.drain = self.gate.hit
        call = asyncio.create_task(self.backend.call("turn/start", {}))
        await self.gate.wait()
        future = next(iter(self.backend.pending.values()))
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertFalse(self.backend.alive)
        self.assertFalse(self.backend.write_lock.locked())
        self.assertFalse(self.backend.pending)
        self.assertTrue(future.done())

    async def test_old_write_deadline_leaves_replacement_connection_alive(self):
        self.writer.drain = self.gate.hit
        with rpc_clock() as advance:
            call = asyncio.create_task(self.backend.call("turn/start", {}))
            await self.gate.wait()
            await self.backend.close()
            self.backend.process = MemoryProcess()
            self.backend.generation += 1
            self.backend.closed = False
            advance(NATIVE_RPC_TIMEOUT + 1)
            with self.assertRaises(BridgeError) as caught:
                await asyncio.wait_for(call, 5)
        self.assertTrue(caught.exception.uncertain)
        self.assertTrue(self.backend.alive)
        self.assertTrue(self.backend.process.stdin.requests.empty())
        self.assertFalse(self.backend.pending)

    async def test_write_and_reply_timeouts_release_failed_tasks_without_cyclic_gc(self):
        async def expire(advance):
            call = asyncio.create_task(self.backend.call("turn/start", {}))
            reference = weakref.ref(call)
            await asyncio.sleep(0)
            self.assertEqual(len(self.backend.pending), 1)
            advance(NATIVE_RPC_TIMEOUT + 1)
            try:
                async with asyncio.timeout(5):
                    await call
            except BridgeError as exc:
                self.assertEqual(exc.code, "native_timeout")
            else:
                self.fail("Expected a native timeout")
            del call
            await asyncio.sleep(0)
            self.assertIsNone(reference())
            self.assertFalse(self.backend.pending)
            self.assertTrue(self.backend.alive)

        with reference_counting_only(), rpc_clock() as advance:
            async with self.backend.write_lock:
                await expire(advance)
            await expire(advance)

    async def wait_for_reply_after_slow_write(self, advance):
        self.writer.drain = self.gate.hit
        async with self.backend.write_lock:
            call = asyncio.create_task(self.backend.call("turn/start", {}, timeout=100))
            await asyncio.sleep(0)
            self.assertEqual(len(self.backend.pending), 1)
            advance(40)
        await self.gate.wait()
        advance(40)
        self.gate.release.set()
        await asyncio.sleep(0)
        self.assertFalse(self.backend.write_lock.locked())
        return call, self.writer.requests.get_nowait()["id"]

    async def test_reply_within_remaining_budget_succeeds(self):
        with rpc_clock() as advance:
            call, request_id = await self.wait_for_reply_after_slow_write(advance)
            advance(10)
            self.backend.process.stdout.feed_data(encoded({"id": request_id, "result": {"ok": True}}))
            self.assertEqual(await asyncio.wait_for(call, 5), {"ok": True})
        self.assertTrue(self.backend.alive)
        self.assertFalse(self.backend.pending)

    async def test_reply_wait_uses_time_left_after_lock_and_drain(self):
        process, generation = self.backend.process, self.backend.generation
        with rpc_clock() as advance:
            call, _ = await self.wait_for_reply_after_slow_write(advance)
            advance(30)
            with self.assertRaisesRegex(BridgeError, "turn/start response timed out") as caught:
                await asyncio.wait_for(call, 5)
        self.assertEqual((caught.exception.code, caught.exception.retryable, caught.exception.uncertain),
                         ("native_timeout", False, True))
        self.assertTrue(self.backend.alive)
        self.assertFalse(self.backend.pending)

        following = asyncio.create_task(self.backend.call("model/list", {}))
        request = await asyncio.wait_for(self.writer.requests.get(), 5)
        self.assertEqual(request["method"], "model/list")
        process.stdout.feed_data(encoded({"id": request["id"], "result": {"data": []}}))
        self.assertEqual(await asyncio.wait_for(following, 5), {"data": []})
        self.assertIs(self.backend.process, process)
        self.assertEqual(self.backend.generation, generation)
        self.assertFalse(self.backend.pending)

    async def test_reply_timeout_drops_late_reply_and_preserves_other_pending_call(self):
        backend = self.backend
        process, generation = backend.process, backend.generation
        with rpc_clock() as advance:
            other = asyncio.create_task(backend.call("thread/read", {}, timeout=120))
            other_id = (await asyncio.wait_for(self.writer.requests.get(), 5))["id"]
            try:
                slow = asyncio.create_task(backend.call("turn/start", {}, timeout=20))
                expired_id = (await asyncio.wait_for(self.writer.requests.get(), 5))["id"]
                advance(30)
                with self.assertRaises(BridgeError) as caught:
                    await asyncio.wait_for(slow, 5)
                self.assertEqual(str(caught.exception),
                                 "Codex turn/start response timed out; action outcome may be uncertain. Do not blindly retry")
                self.assertEqual(set(backend.pending), {other_id})
                self.assertFalse(backend.pending[other_id].done())
                # The reader must consume the late frame before the other reply.
                process.stdout.feed_data(encoded({"id": expired_id, "result": {"turn": "late"}})
                                         + encoded({"id": other_id, "result": {"thread": "current"}}))
                self.assertEqual(await asyncio.wait_for(other, 5), {"thread": "current"})
            finally:
                other.cancel()
                await asyncio.gather(other, return_exceptions=True)
        self.assertIsNone(process.returncode)
        self.assertIs(backend.process, process)
        self.assertEqual(backend.generation, generation)
        self.assertTrue(backend.alive)
        self.assertFalse(backend.reader_task.done())
        backend.on_message.assert_not_awaited()
        backend.on_exit.assert_not_awaited()
        self.assertFalse(self.backend.pending)


class ServiceDeadlineTest(ServiceHarness):
    async def stall_write(self, tool, args, *, human=False):
        writer = self.service.backend.process.stdin
        gate = FaultGate("buffered write")
        # Model a pipe that accepted the frame into its buffer and cannot drain.
        with patch.object(writer, "write") as write, patch.object(writer, "drain", gate.hit), rpc_clock() as advance:
            caller = asyncio.create_task(dispatch(self.service, tool, args, human=human))
            await gate.wait()
            advance(NATIVE_RPC_TIMEOUT + 1)
            with self.assertRaises(BridgeError) as caught:
                await asyncio.wait_for(caller, 5)
            write.assert_called_once()
        self.assertEqual((caught.exception.code, caught.exception.retryable, caught.exception.uncertain),
                         ("native_timeout", False, True))
        return json.loads(write.call_args.args[0])

    async def test_submitted_mutation_is_recorded_as_uncertain_and_never_replayed(self):
        task = await self.start_task("hold")
        args = {"task_id": task["id"], "expected_turn_id": task["turn_id"], "operation_id": "halt"}
        frame = await self.stall_write("codex_interrupt", args)
        self.assertEqual(frame["method"], "turn/interrupt")
        await self.until(task["id"], ("needs_resume",))
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        operation = next(op for op in report["operations"] if op["operation_id"] == "halt")
        self.assertEqual((operation["state"], operation["acknowledgement"]), ("uncertain", "unknown"))
        self.assertFalse(operation["automatic_retry"])
        self.assertIn("backend_disconnected", report["unknown"])
        self.assertFalse(self.service.operation_tasks)
        with patch.object(self.service.backend, "call", AsyncMock()) as native, self.assertRaises(BridgeError) as repeat:
            await dispatch(self.service, "codex_interrupt", args)
        native.assert_not_awaited()
        self.assertTrue(repeat.exception.uncertain)

    async def test_stalled_approval_expires_with_one_uncertain_receipt(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        args = {"pending_request_id": req["id"], "response": {"decision": "accept"}}
        frame = await self.stall_write("codex_respond", args, human=True)
        self.assertEqual(frame, {"id": self.store.get("requests", req["id"])["rpc_id"], "result": args["response"]})
        await self.until(task["id"], ("needs_resume",))
        self.assertEqual(self.store.get("requests", req["id"])["status"], "expired")
        receipts = self.service.get(task["id"], view="permissions")["decision_receipts"]
        self.assertEqual([r["delivery"] for r in receipts], ["uncertain"])
        self.assertFalse(self.service.response_tasks)
        with patch.object(self.service.backend, "respond", AsyncMock()) as native, self.assertRaises(BridgeError):
            await dispatch(self.service, "codex_respond", args, human=True)
        native.assert_not_awaited()

    async def test_approval_timeout_before_write_keeps_live_request_pending(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        args = {"pending_request_id": req["id"], "response": {"decision": "accept"}}
        backend = self.service.backend
        async with backend.write_lock:
            with patch("codex_local_mcp.backend.NATIVE_RPC_TIMEOUT", 0.01), \
                    patch.object(backend.process.stdin, "write") as write, self.assertRaises(BridgeError) as caught:
                await asyncio.wait_for(dispatch(self.service, "codex_respond", args, human=True), 5)
            write.assert_not_called()
        self.assertEqual((caught.exception.code, caught.exception.retryable, caught.exception.uncertain),
                         ("native_timeout", True, False))
        self.assertTrue(backend.alive)
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")
        self.assertEqual(self.service.get(task["id"], view="permissions")["decision_receipts"], [])
        sent = await dispatch(self.service, "codex_respond", args, human=True)
        self.assertEqual(sent["receipt"]["delivery"], "sent")
