"""Extract live helpers only; scans and signals always use injected identities."""

import ast
import asyncio
import copy
import io
import os
import signal
import unittest
from enum import IntEnum
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

TEST_SIGNALS = IntEnum("TestSignals", {"SIGTERM": 15, "SIGKILL": 9})
SIGNALS = SimpleNamespace(SIGTERM=TEST_SIGNALS.SIGTERM, SIGKILL=TEST_SIGNALS.SIGKILL, Signals=TEST_SIGNALS)


def live_helpers():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_controls.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"check", "record_sandbox_processes", "disposable_process", "stop_sandbox_processes"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    namespace = {"sys": SimpleNamespace(platform="linux"), "os": SimpleNamespace(path=os.path, sep=os.sep,
        kill=Mock(), pidfd_open=Mock(return_value=50), close=Mock()),
        "signal": SimpleNamespace(**vars(SIGNALS), pidfd_send_signal=Mock()), "sandbox_scan": AsyncMock(), "process_grace": AsyncMock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class LiveProcessCanaryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.namespace = live_helpers()
        self.scan = self.namespace["sandbox_scan"]
        self.record = self.namespace["record_sandbox_processes"]
        self.service = SimpleNamespace(config=SimpleNamespace(sandbox_process_check=True), backend=SimpleNamespace(version="codex-cli 0.167.0"))
        self.task = {"project": str(Path.cwd() / "disposable-project")}
        self.result = {"available": True, "reason": "ok", "processes": [{"pid": 1234, "start_time": 88,
            "policy_cwd": self.task["project"], "command_cwd": self.task["project"], "project": self.task["project"]}], "truncated": False, "scanned": 2}
        self.scan.return_value = self.result

    async def test_running_canary_and_cleanup_record_process_identity_and_print_pids(self):
        report, output = {}, io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(await self.record(self.service, self.task, report, running=True))
            self.assertTrue(await self.record(self.service, self.task, report))
        self.assertEqual(report, {"sandbox_processes_running": self.result, "sandbox_processes_after_cleanup": self.result})
        self.assertIn("after native terminal cleanup: PID 1234", output.getvalue())
        self.scan.assert_awaited_with(self.task["project"])

    async def test_renamed_process_failure_names_the_exact_codex_version(self):
        self.scan.return_value = {**self.result, "processes": []}
        with self.assertRaisesRegex(RuntimeError, "codex-cli 0.167.0.*did not find codex-linux-sandbox.*renamed"):
            await self.record(self.service, self.task, {}, running=True)

    async def test_unavailable_is_reported_and_does_not_offer_process_cleanup(self):
        self.scan.return_value = {**self.result, "available": False, "reason": "proc_unreadable", "processes": []}
        report = {}
        with redirect_stdout(io.StringIO()):
            self.assertFalse(await self.record(self.service, self.task, report, running=True))
        self.assertEqual(report["sandbox_processes_running"]["reason"], "proc_unreadable")

    async def test_nonlinux_and_disabled_never_scan(self):
        self.namespace["sys"].platform = "win32"
        self.assertFalse(await self.record(self.service, self.task, {}, running=True))
        self.namespace["sys"].platform = "linux"
        self.service.config.sandbox_process_check = False
        self.assertFalse(await self.record(self.service, self.task, {}, running=True))
        self.scan.assert_not_called()

    async def test_signals_only_current_exact_disposable_identity_with_a_two_second_grace(self):
        report = {}
        await self.namespace["stop_sandbox_processes"](self.task["project"], report)
        self.namespace["signal"].pidfd_send_signal.assert_any_call(50, TEST_SIGNALS.SIGTERM, None, 0)
        self.namespace["signal"].pidfd_send_signal.assert_any_call(50, TEST_SIGNALS.SIGKILL, None, 0)
        self.namespace["os"].kill.assert_not_called()
        self.assertEqual(self.namespace["os"].close.call_count, 2)
        self.namespace["process_grace"].assert_awaited_once_with()
        self.assertEqual([e["signal"] for e in report["sandbox_process_signals"]], ["SIGTERM", "SIGKILL"])
        self.assertTrue(all(e["result"] == "sent" for e in report["sandbox_process_signals"]))
        self.assertEqual(self.scan.await_count, 3)

    async def test_pidfd_delivery_never_targets_a_replacement_after_revalidation(self):
        self.result["processes"][0]["command_cwd"] = self.task["project"]
        self.scan.side_effect = lambda project: copy.deepcopy(self.result)
        handles, delivered = {}, []
        def opened(pid, flags=0):
            handle = len(handles) + 50
            handles[handle] = self.result["processes"][0]["start_time"]
            return handle
        def sent(handle, number, info=None, flags=0):
            # PID reuse after the last scan must not retarget the signal.
            self.result["processes"][0]["start_time"] = 99
            delivered.append(handles[handle])
        self.namespace["os"].pidfd_open = Mock(side_effect=opened)
        self.namespace["os"].close = Mock()
        self.namespace["signal"] = SimpleNamespace(**vars(SIGNALS), pidfd_send_signal=Mock(side_effect=sent))
        await self.namespace["stop_sandbox_processes"](self.task["project"], {})
        self.assertEqual(delivered, [88])
        self.namespace["os"].kill.assert_not_called()
        self.assertEqual(self.namespace["os"].close.call_count, 2)

    async def test_pid_reuse_path_change_and_exit_never_signal_the_replacement(self):
        for mutation in ({"start_time": 99}, {"command_cwd": str(Path.cwd())}, None):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.result)
                if mutation is None:
                    changed["processes"] = []
                else:
                    changed["processes"][0].update(mutation)
                self.scan.side_effect = [self.result, changed, changed]
                self.namespace["signal"].pidfd_send_signal.reset_mock()
                report = {}
                await self.namespace["stop_sandbox_processes"](self.task["project"], report)
                self.namespace["signal"].pidfd_send_signal.assert_not_called()
                self.assertTrue(all(e["result"] == "identity_changed_or_exited" for e in report["sandbox_process_signals"]))

    async def test_ancestor_scope_unavailable_and_truncation_refuse_before_any_signal(self):
        unsafe = copy.deepcopy(self.result)
        unsafe["processes"][0]["policy_cwd"] = str(Path(self.task["project"]).parent)
        for result in (unsafe, {**self.result, "available": False}, {**self.result, "truncated": True}):
            self.scan.return_value = result
            with self.assertRaisesRegex(RuntimeError, "Cannot safely stop|Refusing to signal"):
                await self.namespace["stop_sandbox_processes"](self.task["project"], {})
            self.namespace["os"].kill.assert_not_called()
            self.namespace["signal"].pidfd_send_signal.assert_not_called()

    async def test_unavailable_revalidation_stops_without_signalling(self):
        self.scan.side_effect = [self.result, {**self.result, "available": False}]
        with self.assertRaisesRegex(RuntimeError, "Cannot re-validate"):
            await self.namespace["stop_sandbox_processes"](self.task["project"], {})
        self.namespace["os"].kill.assert_not_called()
        self.namespace["signal"].pidfd_send_signal.assert_not_called()
        self.namespace["os"].close.assert_called_once_with(50)

    async def test_missing_pidfd_api_or_kernel_support_refuses_without_signalling(self):
        for missing in ("pidfd_open", "pidfd_send_signal", "kernel_open", "kernel_send"):
            self.namespace = live_helpers()
            self.namespace["sandbox_scan"].return_value = self.result
            if missing == "pidfd_open":
                self.namespace["os"].pidfd_open = None
            elif missing == "pidfd_send_signal":
                self.namespace["signal"].pidfd_send_signal = None
            elif missing == "kernel_open":
                self.namespace["os"].pidfd_open.side_effect = OSError("not supported")
            else:
                self.namespace["signal"].pidfd_send_signal.side_effect = OSError("not supported")
            report = {}
            with redirect_stdout(io.StringIO()):
                self.assertFalse(await self.namespace["stop_sandbox_processes"](self.task["project"], report))
            self.assertEqual(report["sandbox_cleanup_pids"], [1234])
            self.namespace["os"].kill.assert_not_called()
            if missing == "kernel_send":
                self.namespace["os"].close.assert_called_once_with(50)

    def test_script_signals_only_through_pidfd_and_skips_only_if_unavailable(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "live_controls.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            calls = {ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
            if "signal.pidfd_send_signal" in calls:
                self.assertEqual(getattr(node, "name", None), "stop_sandbox_processes")
            self.assertTrue(calls.isdisjoint({"os.kill", "os.killpg", "subprocess.run", "subprocess.Popen", "os.system"}))
        text = path.read_text(encoding="utf-8")
        self.assertIn('if not await stop_sandbox_processes(first["project"], report):', text)
        self.assertIn('if task is None and report.get("boundary_check_skipped"):', text)
        self.assertIn("await asyncio.sleep(2)", text)


if __name__ == "__main__":
    unittest.main()
