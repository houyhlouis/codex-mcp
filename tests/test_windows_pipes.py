"""Native pipe fault tests. Every wait is bounded; run in Windows CI."""
import asyncio
import ctypes
import json
import os
import threading
import unittest
import uuid
from ctypes import wintypes as w
from unittest.mock import patch

from codex_local_mcp import windows as win


@unittest.skipUnless(os.name == "nt", "Requires native Windows named pipes")
class WindowsPipeTest(unittest.IsolatedAsyncioTestCase):
    def address(self):
        return r"\\.\pipe\codex-mcp-fault-" + uuid.uuid4().hex

    async def stop(self, server):
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)
        self.assertFalse(server.thread.is_alive())
        self.assertFalse(server.workers)

    def raw_server(self, address):
        with win.api().descriptor() as attrs:
            return win.OwnedPipe(win.api().handle(win.api().kernel.CreateNamedPipeW(address,
                3 | win.FILE_FLAG_OVERLAPPED | 0x80000, 0x8, 1, 65536, 65536, 1000, ctypes.byref(attrs))))

    async def test_nonreading_peer_hits_write_deadline_and_releases_worker(self):
        loop, entered, finished = asyncio.get_running_loop(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def handler(payload):
            nonlocal calls
            calls += 1
            entered.set()
            return b"x" * (1024 * 1024) if calls == 1 else b"{}"
        class ObservedServer(win.PipeServer):
            def serve(self, *args):
                try:
                    super().serve(*args)
                finally:
                    loop.call_soon_threadsafe(finished.set)
        server = ObservedServer(self.address(), handler, 2 * 1024 * 1024, worker_limit=1, write_timeout=0.5)
        server.start()
        peer = None
        try:
            peer = win.OwnedPipe(await asyncio.wait_for(asyncio.to_thread(win.connect_pipe, server.address), 3))
            await asyncio.wait_for(asyncio.to_thread(win.write_line, peer, b"request", 1), 3)
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(finished.wait(), 4)
            self.assertEqual(server.budget.active, 0)
            self.assertEqual(await asyncio.wait_for(win.exchange(server.address, b"again", 4096, 3), 5), b"{}")
        finally:
            if peer:
                peer.close()
            await self.stop(server)

    async def test_worker_budget_exhaustion_returns_a_clear_error(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(payload):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return b"{}"
        server = win.PipeServer(self.address(), handler, 4096, worker_limit=1)
        server.start()
        first = asyncio.create_task(win.exchange(server.address, b"first", 4096, 6))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            rejected = json.loads(await asyncio.wait_for(win.exchange(server.address, b"second", 4096, 3), 5))
            self.assertEqual(rejected["error"]["code"], "pipe_budget_exhausted")
            self.assertIn("worker budget exhausted", rejected["error"]["message"])
            release.set()
            self.assertEqual(await asyncio.wait_for(first, 5), b"{}")
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 5)
            await self.stop(server)

    async def test_cancel_client_during_busy_connect(self):
        address, entered = self.address(), asyncio.Event()
        server, peer = self.raw_server(address), None
        original, loop = win.connect_pipe, asyncio.get_running_loop()
        try:
            peer = win.OwnedPipe(original(address))
            def connect(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                return original(*args, **kwargs)
            with patch.object(win, "connect_pipe", connect):
                client = asyncio.create_task(win.exchange(address, b"request", 4096, 5))
                await asyncio.wait_for(entered.wait(), 2)
                client.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(client, 4)
            self.assertEqual(win.CLIENT_BUDGET.active, 0)
        finally:
            if peer:
                peer.close()
            server.close()

    async def test_cancel_client_during_blocked_write(self):
        address, entered = self.address(), asyncio.Event()
        server = self.raw_server(address)
        original, loop = win.NativeOperation, asyncio.get_running_loop()
        def operation(pipe, kind, *args, **kwargs):
            result = original(pipe, kind, *args, **kwargs)
            if kind == "write" and not result.done and threading.current_thread().name == "mcp-pipe-client":
                loop.call_soon_threadsafe(entered.set)
            return result
        try:
            with patch.object(win, "NativeOperation", operation):
                client = asyncio.create_task(win.exchange(address, b"x" * 1024 * 1024, 4096, 5))
                await asyncio.wait_for(entered.wait(), 3)
                client.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(client, 4)
            self.assertEqual(win.CLIENT_BUDGET.active, 0)
        finally:
            server.close()

    async def test_cancel_client_during_read_and_ack(self):
        for phase in ("read", "ACK"):
            with self.subTest(phase=phase):
                entered, gate, response_release = asyncio.Event(), threading.Event(), asyncio.Event()
                loop, original = asyncio.get_running_loop(), win.NativeOperation
                async def handler(payload):
                    if phase == "read":
                        await asyncio.wait_for(response_release.wait(), 5)
                    return b"{}"
                server = win.PipeServer(self.address(), handler, 4096)
                server.start()
                def operation(pipe, kind, *args, **kwargs):
                    value = original(pipe, kind, *args, **kwargs)
                    matched = kind == "read" if phase == "read" else kind == "write" and kwargs.get("data") == b"ACK\n"
                    if matched and threading.current_thread().name == "mcp-pipe-client":
                        loop.call_soon_threadsafe(entered.set)
                        if not gate.wait(4):
                            if value.done:
                                value.close()
                            else:
                                try:
                                    value.cancel()
                                finally:
                                    value.abandon()
                            raise TimeoutError("Test phase gate was not released")
                    return value
                client = None
                try:
                    with patch.object(win, "NativeOperation", operation):
                        client = asyncio.create_task(win.exchange(server.address, b"request", 4096, 6))
                        await asyncio.wait_for(entered.wait(), 3)
                        client.cancel()
                        gate.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(client, 4)
                finally:
                    gate.set()
                    response_release.set()
                    if client and not client.done():
                        client.cancel()
                        await asyncio.wait_for(asyncio.gather(client, return_exceptions=True), 5)
                    await self.stop(server)

    async def test_early_disconnect_repeated_stop_start_and_handle_count_stability(self):
        api = win.api()
        api.bind(api.kernel, "GetProcessHandleCount", [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL)
        def handles():
            count = w.DWORD()
            api.check(api.kernel.GetProcessHandleCount(api.kernel.GetCurrentProcess(), ctypes.byref(count)))
            return count.value
        async def handler(payload):
            return b"{}"
        address, baseline = self.address(), handles()
        self.assertEqual(ctypes.sizeof(win.Overlapped), 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)
        for _ in range(8):
            server = win.PipeServer(address, handler, 4096)
            server.start()
            try:
                peer = win.OwnedPipe(await asyncio.wait_for(asyncio.to_thread(win.connect_pipe, address), 3))
                peer.close()  # disconnect before sending any request
                self.assertEqual(await asyncio.wait_for(win.exchange(address, b"next", 4096, 3), 5), b"{}")
            finally:
                await self.stop(server)  # also cancels the idle accept operation
        self.assertEqual(win.CLIENT_BUDGET.active, 0)
        self.assertLessEqual(handles(), baseline + 6)
