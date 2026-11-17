"""Caught IPC errors must not retain files or their owners through tracebacks."""

import asyncio
import io
import json
import os
import stat
import tempfile
import unittest
import weakref
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp import broker, platforms
from codex_local_mcp.backend import AppServer, RpcError
from codex_local_mcp.security import BridgeError
from codex_local_mcp.service import WorkspaceBusy
from tests.helpers import temp_root, thread_wakeups
from tests.test_pipe_lifetimes import join_threads, observe_threads, reference_counting_only
from tests.test_service import ServiceHarness


class BackendLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_error_releases_backend_process_and_future_without_gc(self):
        references = []
        class Process:
            returncode = None
        class Backend(AppServer):
            async def send(self, message, **kwargs):
                future = self.pending[message["id"]]
                references.append(("RPC future", weakref.ref(future)))
                future.set_exception(RpcError({"code": -32600, "message": "Rejected"}))
        async def run():
            backend = Backend(None, None, None)
            backend.process = Process()
            references.extend((("backend", weakref.ref(backend)), ("process", weakref.ref(backend.process))))
            try:
                await backend.call("thread/read", {})
            except RpcError as exc:
                self.assertEqual(exc.error, {"code": -32600, "message": "Rejected"})
                frames = []
                traceback = exc.__traceback__
                while traceback is not None:
                    frames.append(traceback.tb_frame.f_code.co_name)
                    traceback = traceback.tb_next
                self.assertIn("call", frames)  # Keep traceback locations useful.
            else:
                self.fail("Native error was not reported")
            self.assertFalse(backend.pending)
        with reference_counting_only():
            await run()
            for kind, reference in references:
                self.assertIsNone(reference(), f"{kind} retained without cyclic GC")

    async def test_stdio_frame_error_releases_stream_and_pump_without_gc(self):
        threads = []
        async def run():
            source = io.BytesIO(b"x" * 20)
            reference = weakref.ref(source)
            try:
                async for _ in platforms.stdio_lines(source, limit=10):
                    self.fail("Oversized input was yielded")
            except ValueError as exc:
                self.assertEqual(str(exc), "Input frame exceeds size limit")
            else:
                self.fail("Oversized input was not reported")
            await asyncio.sleep(0)
            join_threads(self, threads)
            return reference
        with reference_counting_only(), thread_wakeups(), \
                patch.object(platforms.threading, "Thread", observe_threads(threads)):
            reference = await run()
            await asyncio.sleep(0)  # Finish the queue-delivery callbacks.
            self.assertIsNone(reference(), "Stdio frame error retained its input file")
            for thread in threads:
                self.assertIsNone(thread(), "Stdio error retained its pump thread")

    async def test_reader_does_not_retain_the_last_failed_rpc_while_running(self):
        references = []
        class Process:
            returncode = None
            def __init__(self):
                self.stdout = asyncio.StreamReader()
        class Backend(AppServer):
            async def send(self, message, **kwargs):
                references.append(weakref.ref(self.pending[message["id"]]))
                self.process.stdout.feed_data((json.dumps({"id": message["id"],
                    "error": {"code": -32600, "message": "Rejected"}}) + "\n").encode())
        async def exited(reason):
            pass
        async def run():
            backend = Backend(None, None, exited)
            backend.process = Process()
            backend.reader_task = asyncio.create_task(backend._read())
            try:
                try:
                    await backend.call("thread/read", {})
                except RpcError:
                    pass
                else:
                    self.fail("Native error was not reported")
                await asyncio.sleep(0)
                self.assertFalse(backend.reader_task.done())
                self.assertIsNone(references[0](), "Reader retained the last failed RPC future")
            finally:
                backend.reader_task.cancel()
                await asyncio.gather(backend.reader_task, return_exceptions=True)
        with reference_counting_only():
            await run()

    async def test_broker_rejection_releases_service_and_store_without_gc(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        references, responses = [], []
        entered, settled = asyncio.Event(), asyncio.Event()
        class Store:
            def __init__(self, path):
                references.append(("store", weakref.ref(self)))
                self.closed = False
            def close(self):
                self.closed = True
        class Service:
            def __init__(self, config, store):
                references.append(("service", weakref.ref(self)))
                self.store = store
            async def reconcile_startup(self):
                pass
            async def close(self):
                pass
        class Server:
            def close(self):
                pass
            async def wait_closed(self):
                pass
        class Writer:
            def __init__(self, failed=False):
                self.closed = asyncio.get_running_loop().create_future()
                if failed:
                    self.closed.set_exception(BrokenPipeError("Peer disconnected during close"))
                else:
                    self.closed.set_result(None)
            def get_extra_info(self, name):
                return None
            def write(self, payload):
                responses.append(payload)
            async def drain(self):
                pass
            def close(self):
                pass
            async def wait_closed(self):
                await self.closed
        async def start(handler, address, **kwargs):
            reader, writer = asyncio.StreamReader(), Writer(failed=True)
            references.extend((("disconnected reader", weakref.ref(reader)), ("disconnected writer", weakref.ref(writer))))
            reader.set_exception(BrokenPipeError("Peer disconnected during read"))
            await handler(reader, writer)
            del reader, writer
            reader, writer = asyncio.StreamReader(), Writer()
            reader.feed_data(b'{"config":"test","method":"tool","name":"codex_get","args":{"cancel":true}}\n')
            client = asyncio.create_task(handler(reader, writer))
            references.append(("cancelled client task", weakref.ref(client)))
            client.add_done_callback(lambda task: settled.set())
            await asyncio.wait_for(entered.wait(), 3)
            client.cancel()
            # A pipe completion callback observes cancelled(), without taking
            # the Task's saved CancelledError through result()/exception().
            await asyncio.wait_for(settled.wait(), 3)
            self.assertTrue(client.cancelled())
            del client, reader, writer
            for method in ("tool", "stop"):
                reader = asyncio.StreamReader()
                reader.feed_data((f'{{"config":"test","method":"{method}","name":"codex_get"}}\n').encode())
                await handler(reader, Writer())
            return Server()
        async def reject(*args, **kwargs):
            if args[2].get("cancel"):
                entered.set()
                await asyncio.Event().wait()
            raise BridgeError("Expected refusal", code="invalid_arguments")
        with tempfile.TemporaryDirectory(dir=temp_root()) as directory:
            config = SimpleNamespace(state_dir=Path(directory), socket=Path(directory) / "socket",
                                     endpoint="unused", fingerprint="test")
            loop = asyncio.get_running_loop()
            with reference_counting_only(), patch.object(broker, "IS_WINDOWS", False), \
                    patch.object(broker, "lock_file", lambda fd: None), \
                    patch.object(broker, "check_peer", lambda sock: None), \
                    patch.object(broker, "Store", Store), patch.object(broker, "Service", Service), \
                    patch.object(broker, "dispatch", reject), \
                    patch.object(broker.asyncio, "start_unix_server", start, create=True), \
                    patch.object(broker.os, "chmod", lambda *args: None), \
                    patch.object(loop, "add_signal_handler", lambda *args: None), \
                    patch("codex_local_mcp.protocol_tasks.ProtocolTasks", lambda service: None):
                await broker.run_broker(config)
                await asyncio.sleep(0)  # Release shutdown's gather-delivery callback.
                self.assertIn(b"Expected refusal", responses[0])
                self.assertIn(b'"stopping": true', responses[1])
                for kind, reference in references:
                    self.assertIsNone(reference(), f"Broker rejection retained its {kind}")


class UnixClientLifetimeTest(unittest.IsolatedAsyncioTestCase):
    async def io_fault(self, *, diagnostic, read_error, close_error):
        references, closed = [], []
        uid = getattr(os, "getuid", lambda: 0)()
        class Socket:
            def lstat(self):
                return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=uid)
        class Writer:
            def __init__(self):
                self.closed = asyncio.get_running_loop().create_future()
                if close_error:
                    self.closed.set_exception(BrokenPipeError("Close failed"))
                else:
                    self.closed.set_result(None)
            def get_extra_info(self, name):
                return None
            def write(self, payload):
                pass
            async def drain(self):
                pass
            def close(self):
                closed.append(True)
            async def wait_closed(self):
                await self.closed
        async def connect(*args, **kwargs):
            reader, writer = asyncio.StreamReader(), Writer()
            references.extend((("reader", weakref.ref(reader)), ("writer", weakref.ref(writer))))
            if read_error:
                reader.set_exception(BrokenPipeError("Read failed"))
            else:
                reader.feed_data(b'{"result": {}}\n')
            return reader, writer
        with tempfile.TemporaryDirectory(dir=temp_root()) as directory:
            config = SimpleNamespace(state_dir=Path(directory), socket=Socket(), endpoint="unused", fingerprint="test")
            with reference_counting_only(), patch.object(broker, "IS_WINDOWS", False), \
                    patch.object(broker, "check_peer", lambda sock: None), \
                    patch.object(broker.os, "getuid", lambda: uid, create=True), \
                    patch.object(broker.asyncio, "open_unix_connection", connect, create=True):
                try:
                    if diagnostic:
                        await broker._read_diagnostic(config, b"request", 1)
                    else:
                        await broker.rpc(config, "ping", timeout=1)
                except BrokenPipeError:
                    pass
                else:
                    self.fail("Unix client did not report its I/O error")
                await asyncio.sleep(0)
                self.assertEqual(closed, [True])
                for kind, reference in references:
                    self.assertIsNone(reference(), f"Unix client error retained its {kind}")

    async def test_rpc_read_error_releases_streams_without_gc(self):
        await self.io_fault(diagnostic=False, read_error=True, close_error=False)

    async def test_rpc_close_error_releases_streams_without_gc(self):
        await self.io_fault(diagnostic=False, read_error=False, close_error=True)

    async def test_rpc_read_and_close_errors_release_streams_without_gc(self):
        await self.io_fault(diagnostic=False, read_error=True, close_error=True)

    async def test_diagnostic_read_error_releases_streams_without_gc(self):
        await self.io_fault(diagnostic=True, read_error=True, close_error=False)

    async def test_diagnostic_close_error_releases_streams_without_gc(self):
        await self.io_fault(diagnostic=True, read_error=False, close_error=True)

    async def test_diagnostic_read_and_close_errors_release_streams_without_gc(self):
        await self.io_fault(diagnostic=True, read_error=True, close_error=True)


class OperationLifetimeTest(ServiceHarness):
    async def settle_fault(self, kind):
        finished, references = asyncio.Event(), []
        loop, wait_for = asyncio.get_running_loop(), asyncio.wait_for
        original_create = loop.create_task
        def create(coroutine, **kwargs):
            task = original_create(coroutine, **kwargs)
            if coroutine.cr_code.co_name == "wait" and coroutine.cr_code.co_filename.endswith("service.py"):
                references.append(weakref.ref(task))
            return task
        async def expired(coroutine, timeout):
            coroutine.close()
            raise TimeoutError("Injected family-settle deadline")
        async def work():
            if kind == "background":
                with self.service.track_background(self.service.workspace_scan_calls, "project"):
                    raise BridgeError("Expected background failure")
            else:
                await self.service.settle_family("missing-task")
        deadline = patch("codex_local_mcp.service.asyncio.wait_for", expired) if kind == "deadline" else nullcontext()
        with reference_counting_only(), deadline, patch.object(loop, "create_task", create):
            task = asyncio.create_task(work())
            references.append(weakref.ref(task))
            task.add_done_callback(lambda done: finished.set())
            await wait_for(finished.wait(), 3)
            self.assertIsNotNone(task.exception())
            del task
            await asyncio.sleep(0)
            for reference in references:
                self.assertIsNone(reference(), f"{kind} failure retained its task without cyclic GC")

    async def test_family_settle_deadline_releases_caller_without_gc(self):
        await self.settle_fault("deadline")

    async def test_family_settle_lookup_error_releases_both_tasks_without_gc(self):
        await self.settle_fault("lookup")

    async def test_background_context_does_not_retain_a_failed_task(self):
        await self.settle_fault("background")

    async def operation_fault(self, stage):
        service = self.service
        calls = []
        class Action:
            async def __call__(self):
                if stage == "control":
                    raise BridgeError("Expected control refusal", code="state_conflict")
                return {}
        def admit(*args):
            calls.append("admit")
            if stage == "admission":
                raise BridgeError("Expected admission refusal", code="state_conflict")
            if stage == "deferred" and len(calls) == 1:
                raise WorkspaceBusy("Expected transient hold", reconcile_ids=["task"])
            return "family"
        async def checked(*args):
            pass
        async def run():
            action = Action()
            reference = weakref.ref(action)
            try:
                result = await service.operation("lifetime", "codex_start", {}, action)
            except BridgeError as exc:
                self.assertNotEqual(stage, "deferred")
                self.assertIn("Expected", str(exc))
            else:
                self.assertEqual(stage, "deferred")
                self.assertEqual(result, {})
            return reference
        with reference_counting_only(), patch.object(service, "_admit", admit), \
                patch.object(service, "wait_for_reconciliation", checked), \
                patch.object(service, "check_new_family", checked), \
                patch.object(service.scheduler, "schedule", lambda **kwargs: None):
            reference = await run()
            await asyncio.sleep(0)  # The completion callback removes the owned task.
            self.assertFalse(service.operation_tasks)
            self.assertIsNone(reference(), f"{stage} error retained the operation callable")

    async def test_admission_refusal_releases_operation_callable_without_gc(self):
        await self.operation_fault("admission")

    async def test_deferred_admission_releases_operation_callable_without_gc(self):
        await self.operation_fault("deferred")

    async def test_control_refusal_releases_operation_callable_without_gc(self):
        await self.operation_fault("control")

    async def test_response_error_releases_handler_without_gc(self):
        class Respond:
            async def __call__(self, *args, **kwargs):
                raise BridgeError("Expected response refusal", code="stale_request")
        async def run():
            respond = Respond()
            reference = weakref.ref(respond)
            with patch.object(self.service, "review_request", lambda key: {}), \
                    patch.object(self.service, "_respond", respond):
                try:
                    await self.service.respond("request", {})
                except BridgeError as exc:
                    self.assertEqual(exc.code, "stale_request")
                else:
                    self.fail("Response was not refused")
            return reference
        with reference_counting_only():
            reference = await run()
            await asyncio.sleep(0)
            self.assertFalse(self.service.response_tasks)
            self.assertIsNone(reference(), "Response error retained its handler")
