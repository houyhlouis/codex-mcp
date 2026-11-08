"""Portable fixture installation for source ZIPs and Windows-created Git commits."""

import shutil
import json
import sys
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


async def wait_for_broker_stop(config):
    import asyncio
    import os
    from codex_local_mcp.platforms import lock_file
    from codex_local_mcp.security import private_file
    for _ in range(300):
        fd = None
        try:
            fd = private_file(config.state_dir / "broker.lock", os.O_RDWR)
            lock_file(fd)
            return
        except FileNotFoundError:
            return
        except BlockingIOError:
            await asyncio.sleep(0.05)
        finally:
            if fd is not None:
                os.close(fd)
    raise AssertionError("Broker did not release its lock after shutdown")


def link_or_skip(test, link, target, directory=None):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir() if directory is None else directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            test.skipTest("Windows symlink privilege/Developer Mode is unavailable")
        raise
