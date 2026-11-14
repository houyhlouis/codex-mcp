"""Unittest discovery with immediate tracebacks and per-test hang diagnostics."""

import faulthandler
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_DUMP_SECONDS = 180


class DiagnosticResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []
        self.current = None

    def finish_test(self):
        if self.current is not None:
            self.timings.append((time.perf_counter() - self.started, self.current.id()))
            self.current = None

    def startTest(self, test):
        self.finish_test()
        self.current = test
        self.started = time.perf_counter()
        faulthandler.dump_traceback_later(TEST_DUMP_SECONDS, repeat=True, file=sys.stderr, exit=False)
        super().startTest(test)
        # Leave the watchdog armed through cleanup after stopTest(), including
        # IsolatedAsyncioTestCase's executor shutdown. The next test re-arms it.

    def immediate(self, label, test, err):
        self.stream.writeln()
        self.stream.writeln(self.separator1)
        self.stream.writeln(f"{label}: {self.getDescription(test)} (immediate traceback)")
        self.stream.writeln(self._exc_info_to_string(err, test))
        self.stream.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.immediate("FAIL", test, err)

    def addError(self, test, err):
        super().addError(test, err)
        self.immediate("ERROR", test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.immediate("FAIL" if issubclass(err[0], test.failureException) else "ERROR", subtest, err)


class DiagnosticRunner(unittest.TextTestRunner):
    resultclass = DiagnosticResult

    def run(self, test):
        try:
            result = super().run(test)
        finally:
            faulthandler.cancel_dump_traceback_later()
        result.finish_test()
        self.stream.writeln("\n25 slowest tests:")
        for seconds, identity in sorted(result.timings, reverse=True)[:25]:
            self.stream.writeln(f"{seconds:9.3f}s  {identity}")
        self.stream.flush()
        return result


def main():
    unittest.main(module=None, argv=[sys.argv[0], "discover", "-v", *sys.argv[1:]], testRunner=DiagnosticRunner)


if __name__ == "__main__":
    main()
