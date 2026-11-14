import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_tests


class TestRunnerTest(unittest.TestCase):
    def test_failures_errors_and_subtests_print_before_the_test_finishes(self):
        stream, observed = io.StringIO(), []
        class Broken(unittest.TestCase):
            def runTest(self):
                for value in (1, 2):
                    with self.subTest(value=value):
                        if value == 1:
                            self.fail("subtest assertion")
                        raise RuntimeError("subtest error")
                    observed.append(stream.getvalue())
                raise ValueError("test error")
        with patch.object(run_tests.faulthandler, "dump_traceback_later") as arm, patch.object(run_tests.faulthandler, "cancel_dump_traceback_later") as cancel:
            result = run_tests.DiagnosticRunner(stream=stream, verbosity=2).run(Broken())
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(result.errors), 2)
        self.assertIn("AssertionError: subtest assertion", observed[0])
        self.assertIn("RuntimeError: subtest error", observed[1])
        self.assertIn("ValueError: test error", stream.getvalue())
        self.assertIn("25 slowest tests:", stream.getvalue())
        arm.assert_called_once_with(180, repeat=True, file=sys.stderr, exit=False)
        self.assertEqual(cancel.call_count, 1)

    def test_watchdog_covers_cleanup_after_unittest_stop_test(self):
        stream, observed = io.StringIO(), []
        with patch.object(run_tests.faulthandler, "dump_traceback_later"), patch.object(run_tests.faulthandler, "cancel_dump_traceback_later") as cancel:
            class Finalizing(unittest.TestCase):
                def runTest(self):
                    pass
                def run(self, result=None):
                    value = super().run(result)
                    # IsolatedAsyncioTestCase closes its Runner here, after
                    # TestCase has already called result.stopTest().
                    observed.append(cancel.call_count)
                    return value
            result = run_tests.DiagnosticRunner(stream=stream).run(Finalizing())
        self.assertEqual(observed, [0])
        self.assertEqual(len(result.timings), 1)
        self.assertEqual(cancel.call_count, 1)

    def test_discovery_and_exit_status_match_unittest(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "test_sample.py"
            for body, expected in (("pass", 0), ("self.fail('visible failure')", 1), ("raise RuntimeError('visible error')", 1)):
                fixture.write_text("import unittest\nclass Example(unittest.TestCase):\n def test_sample(self): " + body + "\n", encoding="utf-8")
                outputs = []
                for command in ([sys.executable, "-B", str(script)], [sys.executable, "-B", "-m", "unittest", "discover", "-v"]):
                    result = subprocess.run([*command, "-s", str(root), "-t", str(root)], capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertIn("Ran 1 test", result.stderr)
                    outputs.append(result.stderr)
                if expected:
                    self.assertIn("immediate traceback", outputs[0])
