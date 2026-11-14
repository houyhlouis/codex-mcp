"""Optional server settings. Work directories are selected by MCP callers."""
import hashlib
import json
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .platforms import IS_WINDOWS, default_state_dir, resolve_codex
from .security import BridgeError, within, read_config_file, checked_directory
from .resources import validate_budget


@dataclass(frozen=True)
class Config:
    path: Path | None
    state_dir: Path
    codex: str
    max_tasks: int = 4
    approval_relay: bool = False
    allow_external_tools: bool = False
    toolchain_paths: tuple[Path, ...] = ()
    sandbox_process_check: bool = True
    budget: dict | None = None
    worktrees: bool = False
    worktree_root: Path | None = None

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
    def fingerprint_inputs(self):
        return {"bridge_version": __version__, "config_path": str(self.path) if self.path else None,
                "state_dir": str(self.state_dir), "codex": self.codex, "max_tasks": self.max_tasks,
                "approval_relay": self.approval_relay, "allow_external_tools": self.allow_external_tools,
                "toolchain_paths": [str(p) for p in self.toolchain_paths],
                "sandbox_process_check": self.sandbox_process_check, "budget": self.budget or {},
                "worktrees": self.worktrees, "worktree_root": str(self.worktree_root) if self.worktree_root else None}

    @property
    def fingerprint(self):
        data = list(self.fingerprint_inputs.values())
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def environment_identity():
    """Only home paths and opaque profile selection, never auth/environment dumps."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
    profile = os.environ.get("CODEX_PROFILE")
    return {"codex_home": os.path.normcase(os.path.realpath(home)),
            "profile_selection": "bridge-managed permission profiles; no CLI --profile",
            "profile_selection_digest": hashlib.sha256(profile.encode()).hexdigest() if profile is not None else None}


def checked_worktree_root(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise BridgeError("worktree_root must be an absolute private directory", code="invalid_arguments")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise BridgeError("worktree_root must be an absolute private directory", code="invalid_arguments")
    if within(Path.home().resolve(), path.resolve()):
        raise BridgeError("worktree_root must use a dedicated private directory, not the home directory or a filesystem root", code="invalid_arguments")
    existing = path
    while not os.path.lexists(existing):
        existing = existing.parent
    checked_directory(existing)
    if path.exists():
        if IS_WINDOWS:
            from .windows import api, open_handle
            try:
                handle = open_handle(path, directory=True)
                try:
                    api().check_access(handle)
                finally:
                    api().kernel.CloseHandle(handle)
            except OSError as exc:
                raise BridgeError("worktree_root must be owned by this user with private permissions", code="invalid_arguments") from exc
        else:
            info = path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise BridgeError("worktree_root must be owned by this user with private permissions (0700 on POSIX)", code="invalid_arguments")
    return path.resolve()


def load_config(filename=None, legacy_stop=False):
    path = Path(filename).expanduser().absolute() if filename else None
    data = tomllib.loads(read_config_file(path)) if path else {}
    if legacy_stop:
        data.pop("workspace_roots", None)
    if "workspace_roots" in data:
        raise BridgeError("workspace_roots was removed. The main agent must select/create a project and pass workdir or project_id to each task")
    allowed = {"state_dir", "codex_binary", "max_tasks", "approval_relay", "allow_external_tools", "toolchain_paths", "sandbox_process_check", "budget", "worktrees", "worktree_root"}
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
        raise BridgeError("codex_binary must be a bare command name or an absolute path")
    try:
        executable = resolve_codex(binary)
    except ValueError as exc:
        if not legacy_stop:
            raise BridgeError(str(exc)) from exc
        executable = binary
    maximum = data.get("max_tasks", 4)
    if type(maximum) is not int or not 1 <= maximum <= 32:
        raise BridgeError("max_tasks must be between 1 and 32")
    for key, default in (("approval_relay", False), ("allow_external_tools", False), ("sandbox_process_check", True), ("worktrees", False)):
        if type(data.get(key, default)) is not bool:
            raise BridgeError(f"{key} must be boolean")
    toolchains = data.get("toolchain_paths", [])
    if not isinstance(toolchains, list) or any(not isinstance(p, str) or not Path(p).expanduser().is_absolute() for p in toolchains):
        raise BridgeError("toolchain_paths must be an array of absolute paths")
    resolved = []
    for entry in toolchains:
        try:
            resolved.append(Path(entry).expanduser().resolve(strict=True))
        except OSError as exc:
            raise BridgeError(f"toolchain_paths entry does not exist or is inaccessible: {entry}") from exc
    toolchains = tuple(resolved)
    if any(within(Path.home().resolve(), p) or within(state, p) or (path and within(path, p)) for p in toolchains):
        raise BridgeError("Toolchain paths must not contain the home directory, configuration or broker state")
    return Config(path.resolve() if path else None, state, executable, maximum,
                  data.get("approval_relay", False), data.get("allow_external_tools", False), toolchains,
                  data.get("sandbox_process_check", True), validate_budget(data.get("budget", {})),
                  data.get("worktrees", False), checked_worktree_root(data.get("worktree_root")))
