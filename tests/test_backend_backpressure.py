import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from codex_local_mcp.backend import AppServer, EventBuffer, EventBudgetExceeded, EVENT_GAP_METHOD
from codex_local_mcp.security import BridgeError
from tests.helpers import FaultGate
from tests.native_wire import wait_for
from tests.test_service import ServiceHarness


def encoded(message):
    return json.dumps(message).encode() + b"\n"


def progress(index=0, text="x" * 1024):
    return {"method": "item/agentMessage/delta", "params": {
        "threadId": "thread", "turnId": "turn", "itemId": "text", "delta": text, "index": index}}


class MemoryWriter:
    def __init__(self):
        self.requests = asyncio.Queue()

    def write(self, data):
        self.requests.put_nowait(json.loads(data))

    async def drain(self):
        pass


class MemoryProcess:
    def __init__(self):
        self.stdout, self.stdin = asyncio.StreamReader(), MemoryWriter()
        self.returncode = None

    def terminate(self):
        self.returncode = -15
        self.stdout.feed_eof()

    kill = terminate

    async def wait(self):
        return self.returncode


class EventBufferTest(unittest.IsolatedAsyncioTestCase):
    async def test_age_bytes_and_close_are_accounted_for(self):
        queue = EventBuffer(4, 2048)
        message = progress(text="small")
        with patch("codex_local_mcp.backend.time.monotonic", return_value=100.0):
            queue.put_nowait(message)
        with patch("codex_local_mcp.backend.time.monotonic", return_value=105.0):
            self.assertEqual(queue.backlog["oldest_age_seconds"], 5.0)
            self.assertEqual(queue.backlog["bytes"], len(encoded(message)))
        self.assertEqual(await asyncio.wait_for(queue.get(), 1), message)
        self.assertEqual(queue.backlog["bytes"], 0)
        queue.close()
        self.assertIsNone(await asyncio.wait_for(queue.get(), 1))

    async def test_large_progress_becomes_a_small_explicit_gap(self):
        queue = EventBuffer(4, 512)
        queue.put_nowait(progress(text="x" * 8192))
        self.assertLessEqual(queue.backlog["bytes"], 512)
        marker = await asyncio.wait_for(queue.get(), 1)
        self.assertEqual(marker["method"], EVENT_GAP_METHOD)
        self.assertEqual(marker["params"]["dropped_events"], 1)
        self.assertTrue(marker["params"]["event_gap_possible"])

    async def test_gap_compaction_does_not_cross_control_barriers(self):
        queue = EventBuffer(8, 4096)
        start = {"method": "item/started", "params": {"threadId": "thread", "turnId": "turn", "item": {"id": "patch"}}}
        update = {"method": "item/fileChange/patchUpdated", "params": {"threadId": "thread", "itemId": "patch", "changes": []}}
        approval = {"id": 7, "method": "item/fileChange/requestApproval", "params": {"threadId": "thread", "itemId": "patch"}}
        completion = {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}}
        for _ in range(20):
            queue.put_nowait(progress())
        queue.put_nowait(start)
        queue.put_nowait(update)
        for _ in range(20):
            queue.put_nowait(progress())
        queue.put_nowait(approval)
        queue.put_nowait(completion)
        messages = []
        while queue.qsize():
            messages.append(await asyncio.wait_for(queue.get(), 1))
        self.assertEqual([message for message in messages if message["method"] not in (EVENT_GAP_METHOD, "item/agentMessage/delta")],
                         [start, update, approval, completion])
        first_gap = next(index for index, message in enumerate(messages) if message["method"] == EVENT_GAP_METHOD)
        self.assertLess(first_gap, messages.index(start))
        self.assertEqual(sum(message["params"]["dropped_events"] for message in messages if message["method"] == EVENT_GAP_METHOD),
                         queue.dropped_events)

    async def test_requests_and_unknown_notifications_are_never_replaceable(self):
        for method in ("item/agentMessage/delta", "future/proposal", "bridge/event_gap"):
            queue = EventBuffer(2, 4096)
            for identifier in range(2):
                queue.put_nowait({"id": identifier, "method": method, "params": {"threadId": "thread"}})
            with self.assertRaises(EventBudgetExceeded):
                queue.put_nowait({"id": 3, "method": method, "params": {"threadId": "thread"}})
            self.assertEqual(queue.dropped_events, 0)
            self.assertEqual(queue.backlog["events"], 0)

    async def test_gap_contexts_stay_on_the_correct_side_of_a_turn_boundary(self):
        queue = EventBuffer(3, 1024)
        queue.put_nowait(progress(text="x" * 8192))
        boundary = {"method": "turn/started", "params": {"threadId": "thread", "turn": {"id": "next"}}}
        queue.put_nowait(boundary)
        next_progress = progress(text="x" * 8192)
        next_progress["params"]["turnId"] = "next"
        queue.put_nowait(next_progress)
        first, middle, last = [await asyncio.wait_for(queue.get(), 1) for _ in range(3)]
        self.assertEqual(first["params"]["turnId"], "turn")
        self.assertEqual(middle, boundary)
        self.assertEqual(last["params"]["turnId"], "next")

    async def test_byte_budget_alone_can_exhaust_protected_events(self):
        queue = EventBuffer(100, 512)
        with self.assertRaises(EventBudgetExceeded):
            queue.put_nowait({"method": "item/fileChange/patchUpdated", "params": {"diff": "x" * 1024}})
        self.assertEqual(queue.backlog["bytes"], 0)
        for count, size in ((0, 1), (1, 0), (True, 512)):
            with self.assertRaises(ValueError):
                EventBuffer(count, size)


class ReaderBackpressureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backends, self.gates = [], []

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.release.set()
        for backend in self.backends:
            await asyncio.wait_for(backend.close(), 5)

    def backend(self, callback, *, count=16, size=4096):
        exits = []
        exited = asyncio.Event()
        async def on_exit(reason):
            exits.append(reason)
            exited.set()
        backend = AppServer(None, callback, on_exit, event_max_count=count, event_max_bytes=size)
        backend.process = MemoryProcess()
        backend.event_task = asyncio.create_task(backend._dispatch_events())
        backend.reader_task = asyncio.create_task(backend._read())
        self.backends.append(backend)
        return backend, exited, exits

    def gate(self, name):
        gate = FaultGate(name)
        self.gates.append(gate)
        return gate

    def assert_budget(self, backend):
        self.assertLessEqual(backend.backlog["events"], backend.event_max_count)
        self.assertLessEqual(backend.backlog["bytes"], backend.event_max_bytes)

    async def test_5000_progress_events_keep_rpc_responses_and_controls_ordered(self):
        gate, finished = self.gate("callback"), asyncio.Event()
        seen = []
        async def callback(message):
            if not seen:
                await gate.hit()
            seen.append(message)
            if message["method"] == "turn/completed":
                finished.set()
        backend, _, _ = self.backend(callback, size=1024 * 1024)
        backend.process.stdout.feed_data(encoded({"method": "turn/started", "params": {"threadId": "thread", "turn": {"id": "turn"}}}))
        await gate.wait()
        call = asyncio.create_task(backend.call("fixture/flood", {}, timeout=3))
        request = await asyncio.wait_for(backend.process.stdin.requests.get(), 3)
        controls = [{"method": method, "params": {"threadId": "thread", "turnId": "turn", "sequence": index}}
                    for index, method in enumerate(("item/started", "item/fileChange/patchUpdated", "item/fileChange/requestApproval", "turn/completed"))]
        controls[2]["id"] = 77
        backend.process.stdout.feed_data(b"".join(encoded(progress(index)) for index in range(5000))
            + b"".join(encoded(message) for message in controls) + encoded({"id": request["id"], "result": {"ok": True}}))
        self.assertEqual(await asyncio.wait_for(call, 5), {"ok": True})
        self.assert_budget(backend)
        self.assertGreater(backend.backlog["dropped_events"], 0)
        gate.release.set()
        await asyncio.wait_for(finished.wait(), 5)
        self.assertEqual([message for message in seen if message in controls], controls)
        kept = sum(message["method"] == "item/agentMessage/delta" for message in seen)
        dropped = sum(message["params"]["dropped_events"] for message in seen if message["method"] == EVENT_GAP_METHOD)
        self.assertEqual(kept + dropped, 5000)

    async def test_metadata_rpc_awaited_by_callback_arrives_during_flood(self):
        gate, received = self.gate("metadata callback"), asyncio.Event()
        first = True
        async def callback(message):
            nonlocal first
            if first:
                first = False
                self.assertEqual(await backend.call("thread/read", {"threadId": "thread"}, timeout=3), {"metadata": True})
                received.set()
                await gate.hit()
        backend, _, _ = self.backend(callback)
        backend.process.stdout.feed_data(encoded(progress()))
        request = await asyncio.wait_for(backend.process.stdin.requests.get(), 3)
        backend.process.stdout.feed_data(b"".join(encoded(progress(index)) for index in range(5000))
                                        + encoded({"id": request["id"], "result": {"metadata": True}}))
        await asyncio.wait_for(received.wait(), 5)
        await gate.wait()
        self.assert_budget(backend)
        gate.release.set()

    async def test_5000_unknown_events_fail_closed_instead_of_queueing_4999(self):
        gate = self.gate("stalled callback")
        async def callback(message):
            await gate.hit()
        backend, exited, reasons = self.backend(callback)
        backend.process.stdout.feed_data(encoded(progress()))
        await gate.wait()
        call = asyncio.create_task(backend.call("fixture/flood", {}, timeout=3))
        await asyncio.wait_for(backend.process.stdin.requests.get(), 3)
        backend.process.stdout.feed_data(b"".join(encoded({"method": "fixture/event", "params": {"i": i, "text": "x" * 1024}})
                                                 for i in range(5000)))
        with self.assertRaisesRegex(BridgeError, "event_gap_possible"):
            await asyncio.wait_for(call, 5)
        await asyncio.wait_for(exited.wait(), 5)
        self.assertTrue(backend.closed)
        self.assertIsNotNone(backend.process.returncode)
        self.assert_budget(backend)
        self.assertGreater(backend.events.maxsize, 0)
        self.assertEqual(len(reasons), 1)
        self.assertIn("event ingress budget exceeded", reasons[0])

    async def test_orderly_shutdown_cancels_stalled_callback_and_pending_rpc(self):
        gate = self.gate("shutdown")
        async def callback(message):
            await gate.hit()
        backend, exited, reasons = self.backend(callback)
        backend.process.stdout.feed_data(encoded(progress()))
        await gate.wait()
        call = asyncio.create_task(backend.call("thread/read", {}, timeout=3))
        await asyncio.wait_for(backend.process.stdin.requests.get(), 3)
        await asyncio.wait_for(backend.close(), 5)
        with self.assertRaises(BridgeError):
            await asyncio.wait_for(call, 5)
        await asyncio.wait_for(exited.wait(), 5)
        self.assertTrue(backend.reader_task.done())
        self.assertTrue(backend.event_task.done())
        self.assertEqual(len(reasons), 1)

    async def test_protected_overflow_cancels_callback_waiting_for_metadata(self):
        async def callback(message):
            await backend.call("thread/read", {}, timeout=3)
        backend, exited, reasons = self.backend(callback, count=2)
        backend.process.stdout.feed_data(encoded(progress()))
        await asyncio.wait_for(backend.process.stdin.requests.get(), 3)
        backend.process.stdout.feed_data(b"".join(encoded({"method": "item/completed", "params": {"index": i}})
                                                 for i in range(3)))
        await asyncio.wait_for(exited.wait(), 5)
        self.assertFalse(backend.pending)
        self.assertIn("event_gap_possible", reasons[0])

    async def test_slow_storage_callback_has_bounded_ingress(self):
        storage = self.gate("storage")
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE events(kind TEXT)")
        first, committed = True, asyncio.Event()
        async def callback(message):
            nonlocal first
            if first:
                first = False
                await storage.hit()
            with database:
                database.execute("INSERT INTO events VALUES (?)", (message["method"],))
            committed.set()
        backend, _, _ = self.backend(callback)
        try:
            backend.process.stdout.feed_data(encoded(progress()))
            await storage.wait()
            call = asyncio.create_task(backend.call("thread/read", {}, timeout=3))
            request = await asyncio.wait_for(backend.process.stdin.requests.get(), 3)
            backend.process.stdout.feed_data(b"".join(encoded(progress(index)) for index in range(5000))
                                            + encoded({"id": request["id"], "result": {}}))
            await asyncio.wait_for(call, 5)
            self.assertEqual(database.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            self.assert_budget(backend)
            storage.release.set()
            await asyncio.wait_for(committed.wait(), 5)
            await asyncio.wait_for(backend.close(), 5)
        finally:
            database.close()


class ServiceEventGapTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.backend = AppServer(self.cfg, self.service.on_message, self.service.on_exit,
                                        event_max_count=16, event_max_bytes=4096)

    def inject(self, task, count, method="item/agentMessage/delta"):
        self.service.backend.process.stdout.feed_data(b"".join(encoded({"method": method, "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "itemId": "text", "delta": "x" * 1024}})
            for _ in range(count)))

    async def test_progress_gap_is_journaled_and_marks_evidence_incomplete(self):
        task = await self.start_task("hold")
        gate = self.gate_callback("storage", "item/agentMessage/delta")
        self.inject(task, 1)
        await gate.wait()
        self.inject(task, 5000)
        await self.service.backend.call("thread/read", {"threadId": task["thread_id"]}, timeout=3)
        self.assertGreater(self.service.backend.backlog["dropped_events"], 0)
        gate.release.set()
        await wait_for(self.service, lambda: "event_ingress_budget" in self.service.get(task["id"])["evidence_gaps"])
        self.service.backend.process.stdout.feed_data(encoded({"method": "turn/completed", "params": {
            "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed", "items": []}}}))
        done = await self.until(task["id"], ("completed",))
        self.assertFalse(done["evidence_complete"])
        gaps = [row for row in self.store.events(0, 200)["events"] if row["kind"] == EVENT_GAP_METHOD]
        self.assertTrue(gaps)
        self.assertTrue(all(row["data"]["event_gap_possible"] for row in gaps))

    async def test_protected_overflow_recovers_tasks_and_expires_requests(self):
        task = await self.start_task("approval")
        waiting = await self.until(task["id"], ("waiting",))
        request = waiting["pending_requests"][0]
        gate = self.gate_callback("callback", "item/agentMessage/delta")
        self.inject(task, 1)
        await gate.wait()
        self.inject(task, 100, "item/fileChange/patchUpdated")
        recovered = await self.until(task["id"], ("needs_resume",))
        self.assertFalse(recovered["evidence_complete"])
        self.assertEqual(self.store.get("requests", request["id"])["status"], "expired")
        with self.assertRaises(BridgeError):
            await self.service.respond(request["id"], {"decision": "accept"}, human=True)
        events = [row for row in self.store.events(0, 200)["events"] if row["kind"] == "bridge/disconnected"]
        self.assertTrue(events[-1]["data"]["event_gap_possible"])
        self.assertIn("event ingress budget exceeded", events[-1]["data"]["reason"])

    async def test_request_using_internal_marker_name_is_rejected(self):
        task = await self.start_task("hold")
        with patch.object(self.service.backend, "respond", AsyncMock()) as respond:
            await self.service.on_message({"id": 99, "method": EVENT_GAP_METHOD, "params": {
                "threadId": task["thread_id"], "turnId": task["turn_id"]}})
        respond.assert_awaited_once_with(99, error={"code": -32601,
            "message": "Unsupported or unowned request; no permission granted"})

    async def test_gap_keeps_implicit_turn_evidence_incomplete_after_late_start(self):
        task = await self.start_task("hold")
        gate = self.gate_callback("callback", "item/agentMessage/delta")
        self.inject(task, 1)
        await gate.wait()
        future = {**task, "turn_id": "implicit-next"}
        self.inject(future, 5000)
        for method, status in (("turn/started", "inProgress"), ("turn/completed", "completed")):
            self.service.backend.process.stdout.feed_data(encoded({"method": method, "params": {
                "threadId": task["thread_id"], "turn": {"id": "implicit-next", "status": status, "items": []}}}))
        await self.service.backend.call("thread/read", {"threadId": task["thread_id"]}, timeout=3)
        gate.release.set()
        done = await self.until(task["id"], ("completed",))
        self.assertEqual(done["turn_id"], "implicit-next")
        self.assertFalse(done["evidence_complete"])
        self.assertIn("event_ingress_budget", done["evidence_gaps"])
