import threading
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp.windows import AdmissionBudget, CANCEL_GRACE, NativeOperation, OwnedPipe, PipeServer, read_line, write_line, wait_io


class FakeOperation:
    def __init__(self, *, complete_at=None, cancel_completes=True, error=None):
        self.now, self.complete_at = 0.0, complete_at
        self.cancel_completes, self.error = cancel_completes, error
        self.done = self.abandoned = False
        self.cancels = self.closes = 0
        self.wait_hook = None

    def poll(self):
        if self.error:
            self.done = True
            raise self.error
        if self.done or (self.complete_at is not None and self.now >= self.complete_at):
            self.done = True
            return 7
        return None

    def wait(self, seconds):
        self.now += seconds
        if self.wait_hook:
            self.wait_hook()

    def cancel(self):
        self.cancels += 1
        if self.cancel_completes:
            self.done = True

    def abandon(self):
        self.abandoned = True

    def close(self):
        self.closes += 1


class PipeControlTest(unittest.TestCase):
    def test_peer_disappearing_during_identity_check_does_not_kill_listener(self):
        closed, disconnected, calls = [], [], []
        server = object.__new__(PipeServer)
        server.listener = OwnedPipe(123, closed.append)
        server.stopped = threading.Event()
        def peer(handle):
            calls.append(handle)
            if len(calls) == 2:
                server.stopped.set()
            raise BrokenPipeError("Peer exited during identity lookup")
        fake_api = SimpleNamespace(kernel=SimpleNamespace(DisconnectNamedPipe=disconnected.append))
        with patch("codex_local_mcp.windows.api", return_value=fake_api), \
                patch("codex_local_mcp.windows.NativeOperation", side_effect=lambda *args: FakeOperation(complete_at=0)), \
                patch("codex_local_mcp.windows.verify_peer", side_effect=peer):
            server.accept()
        self.assertEqual(calls, [123, 123])
        self.assertEqual(disconnected, [123, 123])
        self.assertEqual(closed, [123])

    def test_no_io_is_issued_after_a_deadline_or_cancellation(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("codex_local_mcp.windows.NativeOperation") as begin:
            with self.assertRaises(ConnectionAbortedError):
                write_line(None, b"request", 1, cancelled)
            with self.assertRaises(TimeoutError):
                read_line(None, 100, 0)
        begin.assert_not_called()

    def test_overlapped_io_uses_null_sync_count_and_queries_actual_count(self):
        closed, calls = [], []
        def transfer(handle, buffer, size, synchronous_count, overlapped):
            self.assertIsNone(synchronous_count)
            self.assertIsNotNone(overlapped)
            calls.append(size)
            ctypes.memmove(buffer, b"reply", 5)
            return True
        def result(handle, overlapped, count, wait):
            self.assertFalse(wait)
            count._obj.value = 5
            return True
        kernel = SimpleNamespace(CreateEventW=lambda *args: 99, ReadFile=transfer, WriteFile=transfer,
                                 GetOverlappedResult=result, CloseHandle=closed.append)
        with patch("codex_local_mcp.windows.api", return_value=SimpleNamespace(kernel=kernel, handle=lambda value: value)):
            for kind in ("read", "write"):
                pipe = OwnedPipe(123, lambda value: None)
                operation = NativeOperation(pipe, kind, data=b"hello" if kind == "write" else None, size=5)
                self.assertEqual(wait_io(operation, 1), b"reply" if kind == "read" else 5)
        self.assertEqual(calls, [5, 5])
        self.assertEqual(closed, [99, 99])

    def test_success_and_partial_progress_do_not_cancel(self):
        operation = FakeOperation(complete_at=0.05)
        self.assertEqual(wait_io(operation, 1, clock=lambda: operation.now), 7)
        self.assertEqual((operation.cancels, operation.closes), (0, 1))

    def test_each_phase_deadline_cancels_and_drains(self):
        for phase in ("connect", "write", "read", "ACK"):
            with self.subTest(phase=phase):
                operation = FakeOperation()
                with self.assertRaisesRegex(TimeoutError, f"{phase} deadline exceeded"):
                    wait_io(operation, 0.1, phase=phase, clock=lambda: operation.now)
                self.assertEqual((operation.cancels, operation.closes), (1, 1))
                self.assertFalse(operation.abandoned)
                self.assertLessEqual(operation.now, 0.1 + CANCEL_GRACE)

    def test_cancellation_at_each_phase_is_bounded(self):
        for phase in ("connect", "write", "read", "ACK"):
            with self.subTest(phase=phase):
                cancelled, operation = threading.Event(), FakeOperation()
                operation.wait_hook = cancelled.set
                with self.assertRaisesRegex(ConnectionAbortedError, "operation cancelled"):
                    wait_io(operation, 10, cancelled, phase=phase, clock=lambda: operation.now)
                self.assertEqual((operation.cancels, operation.closes), (1, 1))

    def test_cancel_racing_normal_completion_still_reports_cancellation(self):
        cancelled, operation = threading.Event(), FakeOperation(complete_at=0)
        cancelled.set()
        with self.assertRaises(ConnectionAbortedError):
            wait_io(operation, 10, cancelled, clock=lambda: operation.now)
        self.assertEqual(operation.closes, 1)

    def test_pending_cancel_retains_ownership_instead_of_freeing_live_buffer(self):
        operation = FakeOperation(cancel_completes=False)
        with self.assertRaises(TimeoutError):
            wait_io(operation, 0.1, clock=lambda: operation.now)
        self.assertTrue(operation.abandoned)
        self.assertEqual(operation.closes, 0)
        self.assertLessEqual(operation.now, 0.1 + CANCEL_GRACE + 0.02)

    def test_completion_during_owner_transfer_does_not_double_close(self):
        operation = FakeOperation(cancel_completes=False)
        def completed_reaper():
            operation.abandoned = operation.done = True
            operation.close()
        operation.abandon = completed_reaper
        with self.assertRaises(TimeoutError):
            wait_io(operation, 0.1, clock=lambda: operation.now)
        self.assertEqual(operation.closes, 1)

    def test_failed_cancel_keeps_live_operation_owned(self):
        operation = FakeOperation(cancel_completes=False)
        def failed_cancel():
            raise OSError("Cancellation failed")
        operation.cancel = failed_cancel
        with self.assertRaises(OSError):
            wait_io(operation, 0.1, clock=lambda: operation.now)
        self.assertTrue(operation.abandoned)
        self.assertEqual(operation.closes, 0)

    def test_early_disconnect_releases_operation_once(self):
        operation = FakeOperation(error=BrokenPipeError("peer disconnected"))
        with self.assertRaises(BrokenPipeError):
            wait_io(operation, 1, clock=lambda: operation.now)
        self.assertEqual((operation.cancels, operation.closes), (0, 1))

    def test_handle_has_one_owner_and_close_is_idempotent(self):
        closed = []
        pipe = OwnedPipe(123, closed.append)
        pipe.close()
        pipe.close()
        self.assertEqual(closed, [123])

    def test_admission_is_bounded_and_reusable_after_release(self):
        budget = AdmissionBudget(2, "budget exhausted")
        one, two = budget.acquire(), budget.acquire()
        with self.assertRaisesRegex(ConnectionError, "budget exhausted"):
            budget.acquire()
        one()
        with self.assertRaises(RuntimeError):
            one()
        three = budget.acquire()
        two()
        three()
        self.assertEqual(budget.active, 0)
        for invalid in (0, -1, 253, True):
            with self.assertRaises(ValueError):
                AdmissionBudget(invalid, "invalid")
