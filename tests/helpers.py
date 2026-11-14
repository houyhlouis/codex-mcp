"""Portable fixture installation for source ZIPs and Windows-created Git commits."""

import asyncio
import json
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from codex_local_mcp.security import private_dir
from codex_local_mcp.service import Service

# Fixed schema-10 cost for the new tables, indexes and triggers.
SCHEMA10_LAYOUT_BYTES = 16 * 1024
# Additional allowance for schema-11 dependency-plan tables and indexes.
SCHEMA11_LAYOUT_BYTES = 32 * 1024


class ControlledService(Service):
    """Direct-control tests defer unsolicited checks; admission still verifies."""
    automatic_reconciliation = False
    _admission_checks = 0

    def schedule_reconciliation(self, task_id, *, explicit=False):
        if self.automatic_reconciliation or self._admission_checks:
            return super().schedule_reconciliation(task_id, explicit=explicit)

    async def wait_for_reconciliation(self, task_ids):
        self._admission_checks += 1
        try:
            return await super().wait_for_reconciliation(task_ids)
        finally:
            self._admission_checks -= 1


class FaultGate:
    """One named interception point; release, drop an ACK, or crash a callback."""
    def __init__(self, name, error=None):
        self.name, self.error = name, error
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.used = False

    async def hit(self):
        self.used = True
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error

    async def wait(self):
        await asyncio.wait_for(self.entered.wait(), 5)


class ReconciliationClock:
    """Explicitly advance scheduled workspace checks without wall-clock waits."""
    def __init__(self, service):
        self.service, self.calls = service, []
        self.ready = asyncio.Event()

    def later(self, task_id, seconds):
        handle = asyncio.get_running_loop().create_future()
        self.calls.append((task_id, seconds, handle))
        self.ready.set()
        return handle

    @property
    def pending(self):
        return [(task_id, seconds, handle) for task_id, seconds, handle in self.calls if not handle.done()]

    def advance(self):
        for task_id, _, handle in self.pending:
            handle.set_result(None)
            self.service.periodic_reconciliation(task_id)


@contextmanager
def thread_wakeups(loop=None):
    """Wake sandbox selectors when their cross-thread self-pipe is denied.

    Only tests exercising a real worker thread need this. Gates, not ticks,
    determine the interleaving and every assertion.
    """
    loop = loop or asyncio.get_running_loop()
    handle = None
    def tick():
        nonlocal handle
        handle = loop.call_later(0.01, tick)
    tick()
    try:
        yield
    finally:
        handle.cancel()


def normalise_bridge_version(value):
    """Only bridge identification metadata is volatile across releases."""
    if isinstance(value, list):
        return [normalise_bridge_version(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalise_bridge_version(item) for key, item in value.items()}
    for field, name in (("clientInfo", "codex_local_mcp"), ("serverInfo", "codex-local-mcp")):
        info = result.get(field)
        if isinstance(info, dict) and info.get("name") == name and "version" in info:
            info["version"] = "<bridge-version>"
    return result


def temp_root():
    return "/tmp" if sys.platform == "darwin" else None


def install_fake_codex(directory: Path) -> Path:
    private_dir(directory)
    destination = directory / "fake_codex.py"
    shutil.copyfile(Path(__file__).with_name("fake_codex.py"), destination)
    destination.chmod(0o700)
    return destination


def write_config(path, state, binary=None, *, sandbox_process_check=False):
    # A test project can be inside the host agent's sandbox policy cwd.
    # Process-scan tests inject their own observations instead.
    values = {"state_dir": str(state)}
    if sandbox_process_check is not None:
        values["sandbox_process_check"] = sandbox_process_check
    if binary:
        values["codex_binary"] = str(binary)
    path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items()), encoding="utf-8")
    path.chmod(0o600)


async def stop_test_broker(config):
    """Stop a test broker and wait until its process has exited.

    Waiting only for the lock is not enough on Windows: the exiting broker
    still holds broker.log open, so deleting the temporary directory fails.
    """
    from codex_local_mcp.broker import stop_broker
    return await stop_broker(config, timeout=30)


def read_fake_history(state):
    """Read the fake Codex thread store while the fixture may be rewriting it."""
    path = Path(state) / "fake-history.json"
    for _ in range(200):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.01)
    return json.loads(path.read_text(encoding="utf-8"))


def link_or_skip(test, link, target, directory=None):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir() if directory is None else directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            test.skipTest("Windows symlink privilege/Developer Mode is unavailable")
        raise
