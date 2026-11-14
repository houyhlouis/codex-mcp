import asyncio
import sys
import warnings
from unittest.mock import patch

from tests.test_service import ServiceHarness


class PlanShutdownTest(ServiceHarness):
    async def test_cancelled_start_reaps_the_version_probe_process(self):
        ready, started = asyncio.Event(), []
        original = asyncio.create_subprocess_exec
        async def version(*args, **kwargs):
            self.assertEqual(args[-1], "--version")
            process = await original(sys.executable, "-c", "import threading; print('ready',flush=True); threading.Event().wait()",
                stdout=kwargs["stdout"], stderr=kwargs["stderr"])
            started.append(process)
            self.assertEqual((await process.stdout.readline()).rstrip(b"\r\n"), b"ready")
            ready.set()
            return process
        with warnings.catch_warnings(record=True) as observed, patch("asyncio.create_subprocess_exec", version):
            warnings.simplefilter("always", ResourceWarning)
            job = asyncio.create_task(self.service.backend.start())
            try:
                await asyncio.wait_for(ready.wait(), 5)
            finally:
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(job, 5)
            self.assertEqual(len(started), 1)
            self.assertIsNotNone(started[0].returncode)
            self.assertFalse([w for w in observed if w.category is ResourceWarning])
