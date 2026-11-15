"""Reproduce Windows fixture behavior without changing Git's safety checks."""

import ctypes
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp import worktrees
from tests.helpers import thread_wakeups
from tests import test_worktrees as cases
from tests import test_windows_pipes as pipes


class WorktreePortabilityTest(unittest.TestCase):
    def run_case(self, name):
        result = unittest.TestResult()
        cases.WorktreesTest(name).run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertFalse(result.skipped, result.skipped)
        self.assertFalse(result.errors, result.errors)
        self.assertFalse(result.failures, result.failures)

    def test_invalid_ref_names_are_refused_before_any_filesystem_access(self):
        with patch.object(worktrees, "_repository", side_effect=AssertionError("Repository accessed for an invalid name")):
            for name in ("name.", "name.lock"):
                with self.subTest(name=name):
                    with self.assertRaises(worktrees.WorktreeError) as caught:
                        worktrees.plan("unused", name)
                    self.assertEqual(caught.exception.code, "name_invalid")

    def test_pointer_tampering_opens_existing_hidden_gitfiles(self):
        original = Path.open
        def hidden(path, mode="r", *args, **kwargs):
            if path.name == ".git" and "w" in mode:
                raise PermissionError("CREATE_ALWAYS cannot overwrite a hidden Windows file")
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, "open", hidden):
            for name in ("test_changed_worktree_git_pointer_is_refused_before_any_git_in_the_worktree",
                    "test_interrupted_registration_can_be_inspected_without_following_a_different_pointer",
                    "test_pointer_changed_after_the_check_cannot_redirect_the_native_command"):
                with self.subTest(name=name):
                    self.run_case(name)

    def test_hook_positive_controls_use_literal_lf_script_bytes(self):
        original = Path.write_text
        def windows_text(path, data, *args, **kwargs):
            if path.name in ("post-checkout", "reference-transaction", "pre-auto-gc", "monitor"):
                kwargs["newline"] = ""
                data = data.replace("\n", "\r\n")
            return original(path, data, *args, **kwargs)
        with patch.object(Path, "write_text", windows_text):
            self.run_case("test_hooks_fsmonitor_and_external_diff_never_run")

    def test_monitor_canary_quotes_shell_command_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "shell paths with spaces\\and-backslashes"
            parent.mkdir(parents=True)
            with patch.object(cases, "temp_root", return_value=str(parent)):
                self.run_case("test_hooks_fsmonitor_and_external_diff_never_run")

    def test_binary_marker_fixture_uses_exact_bytes_despite_text_translation(self):
        original = Path.write_text
        def windows_text(path, data, *args, **kwargs):
            if path.name == "tracked.txt":
                kwargs["newline"] = ""
                data = data.replace("\n", "\r\n")
            return original(path, data, *args, **kwargs)
        with patch.object(Path, "write_text", windows_text):
            self.run_case("test_handoff_binary_marker_must_be_a_header_not_file_content")


class PipeMeasurementTest(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, leak=0):
        state = {"count": 100, "warm": False, "cycles": 0}
        def count(process, target):
            target._obj.value = state["count"]
            return True
        def connect(address):
            if not state["warm"]:
                state["count"] += 24  # Cold executor / pipe machinery handles.
                state["warm"] = True
            state["count"] += leak
            return 1
        class Pipe:
            def __init__(self, handle):
                pass
            def close(self):
                pass
        class Server:
            def __init__(self, address, *args):
                self.address, self.workers = address, set()
                self.thread = SimpleNamespace(is_alive=lambda: False)
            def start(self):
                state["cycles"] += 1
            def close(self):
                pass
            async def wait_closed(self):
                pass
        async def exchange(*args):
            return b"{}"
        api = SimpleNamespace(bind=lambda *args: None, check=lambda ok: None,
            kernel=SimpleNamespace(GetProcessHandleCount=count, GetCurrentProcess=lambda: 1))
        fake = SimpleNamespace(api=lambda: api, PipeServer=Server, OwnedPipe=Pipe, connect_pipe=connect,
            exchange=exchange, CLIENT_BUDGET=SimpleNamespace(active=0),
            Overlapped=ctypes.c_byte * (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20))
        # Replace the test's API reference, leaving any native completion reaper
        # from a previous Windows test on the real product API.
        with thread_wakeups(), patch.object(pipes, "win", fake):
            case = pipes.WindowsPipeTest("test_early_disconnect_repeated_stop_start_and_handle_count_stability")
            await case.test_early_disconnect_repeated_stop_start_and_handle_count_stability()
        return state

    async def test_cold_executor_handles_are_excluded_but_all_eight_cycles_are_measured(self):
        state = await self.exercise()
        self.assertEqual(state["cycles"], 9)  # One warm-up, then eight measured cycles.

    async def test_per_iteration_handle_growth_is_still_refused(self):
        with self.assertRaisesRegex(AssertionError, "baseline=.*samples="):
            await self.exercise(leak=1)
