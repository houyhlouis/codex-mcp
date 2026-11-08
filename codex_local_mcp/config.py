"""Optional server settings. Work directories are selected by MCP callers."""
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .platforms import IS_WINDOWS, default_state_dir, resolve_codex
from .security import BridgeError, within, read_config_file, checked_directory


@dataclass(frozen=True)
class Config:
    path: Path | None
    state_dir: Path
    codex: str
    max_tasks: int = 4
    approval_relay: bool = False
    allow_external_tools: bool = False
    toolchain_paths: tuple[Path, ...] = ()

    @property
    def socket(self):
        return self.state_dir / "broker.sock"

    @property
    def endpoint(self):
        if IS_WINDOWS:
            from .windows import current_sid
            identity = current_sid() + "|" + os.path.normcase(str(self.state_dir))
            return "\\\\.\\pipe\\codex-mcp-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        return str(self.socket)

    @property
    def fingerprint(self):
        data = [__version__, str(self.path) if self.path else None, str(self.state_dir),
                self.codex, self.max_tasks, self.approval_relay, self.allow_external_tools,
                [str(p) for p in self.toolchain_paths]]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def load_config(filename=None, legacy_stop=False):
    path = Path(filename).expanduser().absolute() if filename else None
    data = tomllib.loads(read_config_file(path)) if path else {}
    if legacy_stop:
        data.pop("workspace_roots", None)
    if "workspace_roots" in data:
        raise BridgeError("workspace_roots was removed. The main agent must select/create a project and pass workdir or project_id to each task")
    allowed = {"state_dir", "codex_binary", "max_tasks", "approval_relay", "allow_external_tools", "toolchain_paths"}
    if set(data) - allowed:
        raise BridgeError("Unknown configuration keys: " + ", ".join(sorted(set(data) - allowed)))
    value = data.get("state_dir", str(default_state_dir()))
    if not isinstance(value, str):
        raise BridgeError("state_dir must be a path string")
    state = Path(value).expanduser()
    if not state.is_absolute() or state.is_symlink() or within(Path.home().resolve(), state.resolve()):
        raise BridgeError("Use a dedicated absolute private state directory, not the home directory or a filesystem root")
    # Do not follow a junction/symlink hidden in an existing parent.
    parent = state
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    checked_directory(parent)
    state = state.resolve()
    if not IS_WINDOWS and len(os.fsencode(state / "broker.sock")) > 100:
        raise BridgeError("state_dir is too long for a Unix socket path (100 byte limit)")
    binary = data.get("codex_binary", "codex")
    if not isinstance(binary, str):
        raise BridgeError("codex_binary must be an executable path or command name")
    try:
        executable = resolve_codex(binary)
    except ValueError as exc:
        if not legacy_stop:
            raise BridgeError(str(exc)) from exc
        executable = binary
    maximum = data.get("max_tasks", 4)
    if type(maximum) is not int or not 1 <= maximum <= 32:
        raise BridgeError("max_tasks must be between 1 and 32")
    for key in ("approval_relay", "allow_external_tools"):
        if type(data.get(key, False)) is not bool:
            raise BridgeError(f"{key} must be boolean")
    toolchains = data.get("toolchain_paths", [])
    if not isinstance(toolchains, list) or any(not isinstance(p, str) or not Path(p).expanduser().is_absolute() for p in toolchains):
        raise BridgeError("toolchain_paths must be an array of absolute paths")
    toolchains = tuple(Path(p).expanduser().resolve(strict=True) for p in toolchains)
    if any(within(Path.home().resolve(), p) or within(state, p) or (path and within(path, p)) for p in toolchains):
        raise BridgeError("Toolchain paths must not contain the home directory, configuration or broker state")
    return Config(path.resolve() if path else None, state, executable, maximum,
                  data.get("approval_relay", False), data.get("allow_external_tools", False), toolchains)
