"""Pipe owners must not need cyclic GC to release native resources."""

import asyncio
import gc
import threading
import unittest
import weakref
from concurrent.futures import Future
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp import windows as win
from tests.helpers import thread_wakeups
from tests.test_pipe_control import FakeOperation


@contextmanager
def reference_counting_only():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def observe_threads(references):
    original = threading.Thread
    def create(*args, **kwargs):
        thread = original(*args, **kwargs)
        references.append(weakref.ref(thread))
        return thread
    return create


def join_threads(case, references):
    for reference in references:
        thread = reference()
        if thread is not None:
            thread.join(3)
            case.assertFalse(thread.is_alive(), "Pipe thread did not finish")


class PipeLifetimeTest(unittest.TestCase):
    def assert_released(self, references):
        for kind, reference in references:
            self.assertIsNone(reference(), f"{kind} retained without cyclic GC")

    def test_cancelled_wait_releases_operation_without_gc(self):
        def run():
            operation, cancelled = FakeOperation(), threading.Event()
            cancelled.set()
            reference = weakref.ref(operation)
            try:
                win.wait_io(operation, 1, cancelled, clock=lambda: operation.now)
            except ConnectionAbortedError:
                pass
            else:
                self.fail("Cancellation was not reported")
            self.assertEqual((operation.cancels, operation.closes), (1, 1))
            return reference
        with reference_counting_only():
            self.assertIsNone(run()(), "Cancelled operation retained by its traceback")

    def test_timed_out_wait_releases_operation_without_gc(self):
        def run():
            operation = FakeOperation()
            reference = weakref.ref(operation)
            try:
                win.wait_io(operation, 0.1, clock=lambda: operation.now)
            except TimeoutError:
                pass
            else:
                self.fail("Timeout was not reported")
            self.assertEqual((operation.cancels, operation.closes), (1, 1))
            return reference
        with reference_counting_only():
            self.assertIsNone(run()(), "Timed-out operation retained by its traceback")

    def test_stopped_listener_releases_server_operation_and_thread_without_gc(self):
        entered, proceed = threading.Event(), threading.Event()
        references, threads = [], []
        def make_pipe(server, first=False):
            pipe = win.OwnedPipe(123, lambda raw: None)
            references.append(("listener pipe", weakref.ref(pipe)))
            return pipe
        def begin(pipe, kind):
            operation = FakeOperation()
            operation.pipe = pipe
            references.append(("connect operation", weakref.ref(operation)))
            def wait():
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError("Listener gate was not released")
            operation.wait_hook = wait
            return operation
        def run():
            server = win.PipeServer("unused", None, 4096)
            references.append(("server", weakref.ref(server)))
            server.start()
            try:
                self.assertTrue(entered.wait(3), "Listener did not start")
            finally:
                server.close()
                proceed.set()
                join_threads(self, threads)
        with reference_counting_only(), \
                patch.object(win, "api", lambda: SimpleNamespace(process_sid=lambda **kwargs: None)), \
                patch.object(win.PipeServer, "make_pipe", make_pipe), \
                patch.object(win.asyncio, "get_running_loop", lambda: None), \
                patch.object(win, "NativeOperation", begin), \
                patch.object(win.threading, "Thread", observe_threads(threads)):
            run()
            self.assert_released(references + [("listener thread", ref) for ref in threads])

    def test_closed_unstarted_server_releases_its_thread_target_without_gc(self):
        def run():
            server = win.PipeServer("unused", None, 4096)
            reference = weakref.ref(server)
            server.close()
            return reference
        with reference_counting_only(), \
                patch.object(win, "api", lambda: SimpleNamespace(process_sid=lambda **kwargs: None)), \
                patch.object(win.PipeServer, "make_pipe", lambda server, **kwargs: win.OwnedPipe(123, lambda raw: None)), \
                patch.object(win.asyncio, "get_running_loop", lambda: None):
            self.assertIsNone(run()(), "Unstarted listener target retained its server")

    def test_failed_listener_start_releases_server_without_gc(self):
        def refused(thread):
            raise RuntimeError("Thread creation failed")
        def run():
            server = win.PipeServer("unused", None, 4096)
            reference = weakref.ref(server)
            try:
                server.start()
            except RuntimeError:
                pass
            else:
                self.fail("Thread creation did not fail")
            self.assertTrue(server.listener.closed)
            return reference
        with reference_counting_only(), \
                patch.object(win, "api", lambda: SimpleNamespace(process_sid=lambda **kwargs: None)), \
                patch.object(win.PipeServer, "make_pipe", lambda server, **kwargs: win.OwnedPipe(123, lambda raw: None)), \
                patch.object(win.asyncio, "get_running_loop", lambda: None), \
                patch.object(win.threading.Thread, "start", refused):
            self.assertIsNone(run()(), "Failed listener target retained its server")

    def test_failed_listener_thread_construction_closes_its_pipe(self):
        closed = []
        def refused(**kwargs):
            raise RuntimeError("Thread creation failed")
        with reference_counting_only(), \
                patch.object(win, "api", lambda: SimpleNamespace(process_sid=lambda **kwargs: None)), \
                patch.object(win.PipeServer, "make_pipe", lambda server, **kwargs: win.OwnedPipe(123, closed.append)), \
                patch.object(win.asyncio, "get_running_loop", lambda: None), \
                patch.object(win.threading, "Thread", refused):
            server = win.PipeServer("unused", None, 4096)
            reference = weakref.ref(server)
            try:
                server.start()
            except RuntimeError:
                pass
            else:
                self.fail("Thread creation did not fail")
            del server
            self.assertIsNone(reference())
            self.assertEqual(closed, [123])

    def serve_fault(self, phase):
        references, closed = [], []
        async def handler(request):
            return b"{}"
        def submit(coroutine, loop):
            coroutine.close()
            class ImmediateFuture(Future):
                def result(self, timeout=None):
                    return super().result(timeout=0)
            future = ImmediateFuture() if phase == "handler_timeout" else Future()
            if phase == "handler":
                future.set_exception(ValueError("Handler rejected the request"))
            elif phase not in ("cancelled", "handler_timeout"):
                future.set_result(b"{}")
            references.append(("handler future", weakref.ref(future)))
            return future
        def read(handle, maximum, timeout, cancelled):
            if phase == "read" or maximum == 16 and phase == "ACK":
                raise BrokenPipeError("Peer disconnected")
            return b"ACK" if maximum == 16 else b"request"
        def write(*args, **kwargs):
            if phase == "write":
                raise BrokenPipeError("Peer disconnected")
        def run():
            server = object.__new__(win.PipeServer)
            server.handler, server.loop, server.maximum, server.write_timeout = handler, None, 4096, 1
            server.workers, server.workers_lock, server.cancelled = set(), threading.Lock(), threading.Event()
            server.budget = win.AdmissionBudget(1, "busy")
            if phase == "cancelled":
                server.cancelled.set()
            pipe = win.OwnedPipe(123, closed.append)
            references.extend((("server", weakref.ref(server)), ("request pipe", weakref.ref(pipe))))
            worker = threading.Thread(target=server.serve, args=(pipe, server.budget.acquire()))
            references.append(("request thread", weakref.ref(worker)))
            server.workers.add(worker)
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(server.workers)
            self.assertTrue(pipe.closed)
            self.assertEqual(server.budget.active, 0)
        fake_api = SimpleNamespace(kernel=SimpleNamespace(DisconnectNamedPipe=lambda raw: None))
        ticks = iter(range(0, 10000, 121))
        with patch.object(win, "api", lambda: fake_api), \
                patch.object(win, "read_line", read), patch.object(win, "write_line", write), \
                patch.object(win.time, "monotonic", lambda: next(ticks)), \
                patch.object(win.asyncio, "run_coroutine_threadsafe", submit):
            run()
        self.assertEqual(closed, [123])
        return references

    def test_serve_early_disconnect_releases_owners_without_gc(self):
        with reference_counting_only():
            self.assert_released(self.serve_fault("read"))

    def test_serve_handler_error_releases_future_and_server_without_gc(self):
        with reference_counting_only():
            self.assert_released(self.serve_fault("handler"))

    def test_serve_write_disconnect_releases_owners_without_gc(self):
        with reference_counting_only():
            self.assert_released(self.serve_fault("write"))

    def test_serve_ack_disconnect_releases_owners_without_gc(self):
        with reference_counting_only():
            self.assert_released(self.serve_fault("ACK"))

    def test_serve_cancellation_releases_owners_without_gc(self):
        with reference_counting_only():
            self.assert_released(self.serve_fault("cancelled"))

    def test_serve_handler_timeout_releases_owners_without_gc(self):
        with reference_counting_only():
            self.assert_released(self.serve_fault("handler_timeout"))

    def test_cancel_reaper_retains_only_until_native_completion(self):
        entered, proceed, threads = threading.Event(), threading.Event(), []
        class Operation(FakeOperation):
            def __init__(self):
                super().__init__(cancel_completes=False)
                self.pipe = win.OwnedPipe(123, lambda raw: None)
                self.overlapped = SimpleNamespace(hEvent=456)
        def wait(handle, milliseconds):
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("Completion gate was not released")
            return 0
        def run():
            operation = Operation()
            reference = weakref.ref(operation)
            win._retain_cancelled(operation)
            self.assertTrue(entered.wait(3), "Reaper did not start")
            self.assertIn(operation, win._PENDING)
            self.assertTrue(operation.pipe.closed)
            return reference
        with reference_counting_only(), \
                patch.object(win, "api", lambda: SimpleNamespace(kernel=SimpleNamespace(WaitForSingleObject=wait))), \
                patch.object(win.threading, "Thread", observe_threads(threads)):
            try:
                reference = run()
                self.assertIsNotNone(reference(), "Live OVERLAPPED ownership was released early")
            finally:
                proceed.set()
                join_threads(self, threads)
            self.assert_released([("cancelled operation", reference)] + [("reaper thread", ref) for ref in threads])


class PipeClientLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def exchange_fault(self, phase):
        references, threads, closed = [], [], []
        original_pipe = win.OwnedPipe
        def pipe(raw):
            value = original_pipe(raw, closed.append)
            references.append(("client pipe", weakref.ref(value)))
            return value
        def connect(*args):
            if phase == "connect":
                raise FileNotFoundError("Broker is not running")
            return 123
        def write(handle, payload, *args, **kwargs):
            if phase == "write" and payload != b"ACK" or phase == "ACK" and payload == b"ACK":
                raise TimeoutError("Pipe write timed out")
        def read(*args):
            if phase == "read":
                raise BrokenPipeError("Peer disconnected")
            return b"{}"
        loop = asyncio.get_running_loop()
        original_future, original_event = loop.create_future, threading.Event
        def future():
            value = original_future()
            references.append(("client future", weakref.ref(value)))
            return value
        def event():
            value = original_event()
            references.append(("client event", weakref.ref(value)))
            return value
        async def run():
            try:
                await win.exchange("unused", b"request", 4096, 1)
            except OSError as exc:
                expected = FileNotFoundError if phase == "connect" else BrokenPipeError if phase == "read" else TimeoutError
                self.assertIsInstance(exc, expected)
            else:
                self.fail("Client did not report its error")
        with thread_wakeups(), patch.object(win, "OwnedPipe", pipe), \
                patch.object(win, "connect_pipe", connect), patch.object(win, "write_line", write), \
                patch.object(win, "read_line", read), patch.object(loop, "create_future", future), \
                patch.object(win.threading, "Event", event), \
                patch.object(win.threading, "Thread", observe_threads(threads)):
            await run()
            join_threads(self, threads)
            await asyncio.sleep(0)  # Drop the completed future's delivery callback.
        self.assertEqual(win.CLIENT_BUDGET.active, 0)
        self.assertEqual(closed, [] if phase == "connect" else [123])
        for kind, reference in references + [("client thread", ref) for ref in threads]:
            self.assertIsNone(reference(), f"{phase}: {kind} retained without cyclic GC")

    async def test_exchange_connect_error_releases_owners_without_gc(self):
        with reference_counting_only():
            await self.exchange_fault("connect")

    async def test_exchange_write_timeout_releases_owners_without_gc(self):
        with reference_counting_only():
            await self.exchange_fault("write")

    async def test_exchange_read_disconnect_releases_owners_without_gc(self):
        with reference_counting_only():
            await self.exchange_fault("read")

    async def test_exchange_ack_timeout_releases_owners_without_gc(self):
        with reference_counting_only():
            await self.exchange_fault("ACK")

    async def cancelled_exchange(self, *, expire=False):
        entered, expiry, threads, references = asyncio.Event(), asyncio.Event(), [], []
        loop, wait_for = asyncio.get_running_loop(), asyncio.wait_for
        def connect(address, timeout, cancelled):
            operation = FakeOperation()
            references.append(("connect operation", weakref.ref(operation)))
            def wait():
                loop.call_soon_threadsafe(entered.set)
                if not cancelled.wait(3):
                    raise AssertionError("Client cancellation did not reach its operation")
            operation.wait_hook = wait
            return win.wait_io(operation, 10, cancelled, clock=lambda: operation.now)
        async def deadline(future, timeout):
            await wait_for(expiry.wait(), 3)
            future.cancel()
            raise TimeoutError("Client deadline expired")
        timeout_patch = patch.object(win.asyncio, "wait_for", deadline) if expire else nullcontext()
        with thread_wakeups(), patch.object(win, "connect_pipe", connect), timeout_patch, \
                patch.object(win.threading, "Thread", observe_threads(threads)):
            client = asyncio.create_task(win.exchange("unused", b"request", 4096, 5))
            references.append(("client task", weakref.ref(client)))
            try:
                await wait_for(entered.wait(), 3)
                if expire:
                    expiry.set()
                else:
                    client.cancel()
                try:
                    await client
                except TimeoutError:
                    self.assertTrue(expire)
                except asyncio.CancelledError:
                    self.assertFalse(expire)
                else:
                    self.fail("Client did not cancel or time out")
            finally:
                if not client.done():
                    client.cancel()
                    await asyncio.gather(client, return_exceptions=True)
                client = None
                join_threads(self, threads)
            await asyncio.sleep(0)
        self.assertEqual(win.CLIENT_BUDGET.active, 0)
        for kind, reference in references + [("client thread", ref) for ref in threads]:
            self.assertIsNone(reference(), f"{kind} retained after client cancellation without cyclic GC")

    async def test_exchange_cancellation_releases_task_operation_and_thread_without_gc(self):
        with reference_counting_only():
            await self.cancelled_exchange()

    async def test_exchange_deadline_releases_task_operation_and_thread_without_gc(self):
        with reference_counting_only():
            await self.cancelled_exchange(expire=True)
