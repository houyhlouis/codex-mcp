"""Portable fixture installation for source ZIPs and Windows-created Git commits."""

import json
import shutil
import sys
import time
from pathlib import Path

from codex_local_mcp.security import private_dir


def temp_root():
    return "/tmp" if sys.platform == "darwin" else None


def install_fake_codex(directory: Path) -> Path:
    private_dir(directory)
    destination = directory / "fake_codex.py"
    shutil.copyfile(Path(__file__).with_name("fake_codex.py"), destination)
    destination.chmod(0o700)
    return destination


def write_config(path, state, binary=None):
    values = {"state_dir": str(state)}
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
