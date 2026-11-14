"""Operator-only Git worktrees. Shared Git operations never run task commands."""

import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from functools import lru_cache, wraps
from pathlib import Path

from .platforms import IS_WINDOWS, _path_directories, canonical_system_path, lock_file, unlock_file
from .security import BridgeError, checked_directory, private_dir, private_file, validate_windows_path, within

GIT_MIN_VERSION = (2, 36, 0)
GIT_TIMEOUT = 30
OUTPUT_BYTES = 8 * 1024 * 1024
HANDOFF_BYTES = 1024 * 1024
METADATA = "codex-local-mcp-worktrees"
UNSAFE_PATH = "The repository or helper metadata could not be accessed safely; inspect it before retrying."
CODES = frozenset(("not_git", "base_unknown", "name_invalid", "target_exists", "branch_exists",
    "unsafe_config", "filters_configured", "dirty", "unmerged_commits", "not_managed", "locked",
    "git_failed", "git_missing", "git_too_old"))


class WorktreeError(Exception):
    def __init__(self, code, message, *, details=None):
        if code not in CODES:
            raise ValueError("Unknown worktree error code")
        self.code, self.message, self.details = code, message, dict(details or {})
        super().__init__(message)

    def as_dict(self):
        return {"code": self.code, "message": self.message, **self.details}


def _binary_identity(binary):
    info = Path(binary).stat()
    change = info.st_ctime_ns
    if hasattr(info, "st_file_attributes"):
        from .windows import file_change_time
        change = file_change_time(binary)
    return (binary, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            change, info.st_mode, getattr(info, "st_file_attributes", 0))


@lru_cache(maxsize=16)
def _check_version(identity):
    binary = identity[0]
    version, _ = _git(["--version"], Path(binary).parent, binary=binary, maximum=4096)
    if _binary_identity(binary) != identity:
        raise WorktreeError("git_failed", "Git changed during version verification; retry after the installation finishes.")
    match = re.match(rb"git version (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < GIT_MIN_VERSION:
        raise WorktreeError("git_too_old", "Git 2.36.0 or newer is required.")


def _git(args, cwd, *, write=False, index=None, maximum=OUTPUT_BYTES, allowed=(0,), binary=None, audit_keys=None,
         git_dir=None, work_tree=None):
    """The only Git process boundary; readers retain at most their byte cap."""
    context = {"git_dir": git_dir, "work_tree": work_tree}
    if work_tree is not None:
        _worktree_pointer(Path(work_tree), Path(git_dir))
    if audit_keys is not None and set(_config(cwd, **context)) - set(audit_keys):
        raise WorktreeError("unsafe_config", "Git command configuration changed; inspect a new plan before continuing.")
    if binary is None:
        directories, _ = _path_directories()
        # A bare which() can prepend cwd on Windows, even with an explicit PATH.
        binary = next((found for directory in directories
            if (found := shutil.which(str(directory / ("git.exe" if IS_WINDOWS else "git"))))), None)
        if not binary or not Path(binary).is_absolute() or IS_WINDOWS and Path(binary).suffix.lower() != ".exe":
            raise WorktreeError("git_missing", "Git was not found on absolute PATH entries; install Git and use its full folder in PATH.")
        binary = str(Path(binary).resolve())
        _check_version(_binary_identity(binary))
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE=os.devnull, GIT_ATTR_NOSYSTEM="1")
    if not write:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    with tempfile.TemporaryDirectory(prefix="codex-git-") as temporary:
        directory = Path(temporary)
        private_dir(directory)
        hooks, config = directory / "hooks", directory / "config"
        private_dir(hooks)
        os.close(private_file(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(config))
        options = {"core.hooksPath": str(hooks), "core.fsmonitor": "false", "core.untrackedCache": "false",
            "diff.external": "", "credential.helper": "", "protocol.allow": "never", "submodule.recurse": "false",
            "core.splitIndex": "false", "index.sparse": "false", "maintenance.auto": "false", "gc.auto": "0", "core.attributesFile": str(config)}
        command = [binary, "--no-pager"]
        if git_dir is not None:
            command.append("--git-dir=" + str(git_dir))
        if work_tree is not None:
            command.append("--work-tree=" + str(work_tree))
        for key, value in options.items():
            command.extend(["-c", key + "=" + value])
        command.extend(args)
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise WorktreeError("git_failed", "Git could not be started; check the program and repository folder.") from exc
        buffers, overflow, unreadable = [bytearray(), bytearray()], threading.Event(), threading.Event()
        def read(stream, destination, cap):
            try:
                with stream:
                    while chunk := stream.read(8192):
                        if len(destination) + len(chunk) > cap:
                            overflow.set()
                            try:
                                process.kill()
                            except OSError:
                                pass
                            return
                        destination.extend(chunk)
            except OSError:
                unreadable.set()
        readers = [threading.Thread(target=read, args=(stream, buffers[number], cap), daemon=True)
            for number, (stream, cap) in enumerate(((process.stdout, maximum), (process.stderr, 65536)))]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise WorktreeError("git_failed", "Git timed out; inspect the worktree before retrying.") from exc
        finally:
            for reader in readers:
                reader.join(timeout=5)
        if overflow.is_set():
            raise WorktreeError("git_failed", "Git output exceeds the byte limit; no truncated result was returned.")
        if any(reader.is_alive() for reader in readers):
            raise WorktreeError("git_failed", "Git output did not close; inspect the worktree before retrying.")
        if unreadable.is_set():
            raise WorktreeError("git_failed", "Git output could not be read; no partial result was returned.")
        if process.returncode not in allowed:
            raise WorktreeError("git_failed", f"Git {args[0]} failed (exit {process.returncode}); inspect the worktree before retrying.")
        return bytes(buffers[0]), process.returncode


def _text(value):
    return os.fsdecode(value.rstrip(b"\n"))


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}", value) or ".." in value:
        raise WorktreeError("name_invalid", "Use a name of 1-64 letters, digits, dots, underscores or hyphens; do not start with a dot or hyphen, or include '..'.")
    return value


def _path(value):
    try:
        original = Path(value).expanduser()
        if not original.is_absolute():
            original = Path.cwd() / original
        original = canonical_system_path(original)
        if IS_WINDOWS:
            # Git's commondir uses ../... Validate every non-parent name, then
            # walk the original components below before normalising parents.
            lexical = Path(original.anchor).joinpath(*(part for part in original.parts[1:] if part != ".."))
            validate_windows_path(str(lexical))
        current = Path(original.anchor)
        for part in original.parts[1:]:
            current = current.parent if part == ".." else current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorktreeError("unsafe_config", UNSAFE_PATH)
        result = Path(os.path.abspath(original))
        if any(ord(char) < 32 for char in str(result)):
            raise ValueError()
        return canonical_system_path(result)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise WorktreeError("unsafe_config", "Use repository and worktree paths without control characters.") from exc
    except (OSError, BridgeError) as exc:
        raise WorktreeError("unsafe_config", UNSAFE_PATH) from exc


def _directory(path, *, create=False, owned=False, missing=False):
    """Check original components before canonicalisation, including absent targets."""
    path = _path(path)
    existing = path
    if missing:
        while not os.path.lexists(existing):
            existing = existing.parent
    try:
        checked_directory(existing if missing else path, create=create)
        if owned:
            info = existing.stat() if missing else path.stat()
            if IS_WINDOWS:
                from .windows import api, open_handle
                handle = open_handle(existing if missing else path, directory=True)
                try:
                    api().check_access(handle, private=False)
                finally:
                    api().kernel.CloseHandle(handle)
            elif info.st_uid != os.getuid():
                raise OSError("Directory owner differs")
    except (OSError, BridgeError) as exc:
        raise WorktreeError("unsafe_config", UNSAFE_PATH) from exc
    return path


def _identity(common):
    info = _directory(common, owned=True).stat()
    return {"device": info.st_dev, "inode": info.st_ino}


def _plain_file(path, maximum=8192):
    """Read Git metadata without following links, including its parent chain."""
    _directory(path.parent, owned=True)
    if IS_WINDOWS:
        from .windows import api, open_fd
        fd = open_fd(path, os.O_RDONLY)
        try:
            import msvcrt
            api().check_access(msvcrt.get_osfhandle(fd), private=False)
        except BaseException:
            os.close(fd)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not IS_WINDOWS and info.st_uid != os.getuid():
            raise WorktreeError("unsafe_config", UNSAFE_PATH)
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise WorktreeError("unsafe_config", UNSAFE_PATH)
    return data


def _pointer(path, prefix=b""):
    raw = _plain_file(path).strip()
    if not raw.startswith(prefix) or b"\0" in raw:
        raise WorktreeError("unsafe_config", UNSAFE_PATH)
    value = Path(os.fsdecode(raw[len(prefix):]).strip())
    return _directory(value if value.is_absolute() else path.parent / value, owned=True)


def _source_git(target):
    metadata = target / ".git"
    return _directory(metadata, owned=True) if stat.S_ISDIR(metadata.lstat().st_mode) else _pointer(metadata, b"gitdir: ")


def _worktree_pointer(target, git_dir):
    _directory(target, owned=True)
    if _source_git(target) != git_dir:
        raise WorktreeError("unsafe_config", UNSAFE_PATH)


def _administration(record):
    """Find registration from the common directory, never from a mutable .git pointer."""
    common, target = Path(record["common_dir"]), Path(record["path"])
    entry = _exists(record)["entry"]
    if not entry:
        return None
    if entry.get("branch") != "refs/heads/" + record["branch"]:
        raise WorktreeError("not_managed", "The worktree registration or codex branch does not match its helper receipt.")
    directory = _directory(common / "worktrees", owned=True)
    with os.scandir(directory) as entries:
        for item in entries:
            admin = _directory(item.path, owned=True)
            raw = _plain_file(admin / "gitdir").strip()
            linked = _path(os.fsdecode(raw))
            if linked == target / ".git":
                if _pointer(admin / "commondir") != common:
                    raise WorktreeError("not_managed", "The common Git directory does not match its helper receipt.")
                return admin
    raise WorktreeError("not_managed", "The worktree registration or codex branch does not match its helper receipt.")


def _context(record):
    return {"git_dir": Path(record["git_dir"]), "work_tree": Path(record["path"])} if record.get("git_dir") else {}


def _source_context(path):
    if os.path.lexists(_locator(path)):
        return _context(_load(path)[1])
    return {"git_dir": _source_git(path), "work_tree": path}


def _object_store(common):
    """Never import objects through alternates or filesystem links."""
    objects = _directory(common / "objects", owned=True)
    if os.path.lexists(objects / "info" / "alternates"):
        raise WorktreeError("unsafe_config", "Git object alternates are not supported; prepare this worktree manually instead.")
    pending, remaining = [objects], OUTPUT_BYTES
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                remaining -= len(os.fsencode(entry.name)) + 1
                if remaining < 0:
                    raise WorktreeError("unsafe_config", "Git object storage exceeds the inspection limit; prepare this worktree manually instead.")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise WorktreeError("unsafe_config", "Git object storage must not contain symlinks or junctions.")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(info.st_mode):
                    raise WorktreeError("unsafe_config", "Git object storage must use regular files and directories.")


def _config(cwd, *, git_dir=None, work_tree=None):
    context = {"git_dir": git_dir, "work_tree": work_tree}
    common, _ = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd, **context)
    common = _directory(_text(common), owned=True)
    local, _ = _git(["rev-parse", "--path-format=absolute", "--git-path", "config.worktree"], cwd, **context)
    for path in (common / "config", Path(_text(local))):
        if os.path.lexists(path):
            _plain_file(path, OUTPUT_BYTES)
    if os.path.lexists(common / "info"):
        _directory(common / "info", owned=True)
        for name in ("attributes", "exclude"):
            path = common / "info" / name
            if os.path.lexists(path):
                _plain_file(path, OUTPUT_BYTES)
    raw, _ = _git(["config", "--null", "--show-scope", "--name-only", "--list"], cwd, **context)
    fields = raw.split(b"\0")
    keys = sorted(set(os.fsdecode(key).lower() for scope, key in zip(fields[::2], fields[1::2]) if scope != b"command"))
    if any(key.startswith("filter.") for key in keys):
        raise WorktreeError("filters_configured", "Git filters are configured; prepare this worktree manually instead.")
    if any(key == "include.path" or key.startswith("includeif.") for key in keys):
        raise WorktreeError("unsafe_config", "Included Git configuration is not supported; prepare this worktree manually instead.")
    promisor = [key for key in keys if re.fullmatch(r"remote\..+\.promisor", key)]
    if "extensions.partialclone" in keys or any(_git(["config", "--bool", "--get", key], cwd, **context)[0].strip() != b"false" for key in promisor):
        raise WorktreeError("unsafe_config", "Partial-clone repositories are not supported; no objects will be fetched.")
    if "core.sparsecheckout" in keys and _git(["config", "--bool", "--get", "core.sparseCheckout"], cwd, **context)[0].strip() != b"false":
        raise WorktreeError("unsafe_config", "Sparse-checkout configuration is not supported; prepare this worktree manually instead.")
    commands = [key for key in keys if key.startswith("alias.") or re.fullmatch(
        r"(?:core\.(?:sshcommand|gitproxy|askpass|editor|pager|hookspath|fsmonitor|alternaterefscommand)|sequence\.editor|"
        r"diff\.external|diff\..+\.(?:command|textconv)|merge\..+\.driver|gpg(?:\..+)?\.program|gpg\.ssh\.defaultkeycommand|"
        r"interactive\.difffilter|(?:diff|merge)tool\..+\.(?:cmd|path)|web\.browser|browser\..+\.(?:cmd|path)|"
        r"uploadpack\.packobjectshook|pager\..+|credential(?:\..+)?\.helper|submodule\..+\.update)", key, re.DOTALL)]
    overridden = {"core.hookspath", "core.fsmonitor", "core.pager", "diff.external", "credential.helper"}
    if any(key not in overridden and not key.startswith("pager.") for key in commands):
        raise WorktreeError("unsafe_config", "Git command configuration is not supported; prepare this worktree manually instead.")
    _object_store(common)
    return commands


def _repository(repo):
    path = _path(repo)
    if not path.is_dir():
        raise WorktreeError("not_git", "The repository folder does not exist or is not a Git working tree.")
    _directory(path, owned=True)
    context = _source_context(path) if os.path.lexists(path / ".git") else {}
    if not context:
        for parent in path.parents:
            if os.path.lexists(_locator(parent)) and os.path.lexists(parent / ".git"):
                context = _source_context(parent)
                break
    raw, code = _git(["rev-parse", "--is-bare-repository"], path, allowed=(0, 128), **context)
    if code or raw.strip() != b"false":
        raise WorktreeError("not_git", "Select a non-bare Git working tree.")
    top, _ = _git(["rev-parse", "--path-format=absolute", "--show-toplevel"], path, **context)
    top = _directory(_text(top), owned=True)
    context = context or _source_context(top)
    bare, _ = _git(["config", "--bool", "--get", "core.bare"], path, allowed=(0, 1), **context)
    if bare.strip() == b"true":
        raise WorktreeError("not_git", "Select a non-bare Git working tree.")
    common, _ = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path, **context)
    return top, _directory(_text(common), owned=True), _config(path, **context), context


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _errors(function):
    @wraps(function)
    def called(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorktreeError:
            raise
        except (OSError, BridgeError, ValueError, TypeError, KeyError) as exc:
            raise WorktreeError("unsafe_config", UNSAFE_PATH) from exc
    return called


def _branch(common, branch):
    raw, code = _git(["rev-parse", "--verify", "--end-of-options", "refs/heads/" + branch], common, allowed=(0, 128))
    return _text(raw) if not code else None


def _entries(common):
    raw, _ = _git(["worktree", "list", "--porcelain", "-z"], common)
    entries, item = [], {}
    for field in raw.split(b"\0"):
        if not field:
            if item:
                entries.append(item)
                item = {}
            continue
        key, _, value = field.partition(b" ")
        item[os.fsdecode(key)] = os.fsdecode(value)
    if item:
        entries.append(item)
    return entries


@_errors
def plan(repo, name, base="HEAD", *, root=None) -> dict:
    name = _name(name)
    top, common, commands, context = _repository(repo)
    if not isinstance(base, str) or not 1 <= len(base) <= 1024 or "\0" in base:
        raise WorktreeError("base_unknown", "The base is not a known Git commit.")
    raw, code = _git(["rev-parse", "--verify", "--end-of-options", base + "^{commit}"], top, allowed=(0, 128), **context)
    if code or not re.fullmatch(rb"[a-f0-9]{40}|[a-f0-9]{64}", raw.strip()):
        raise WorktreeError("base_unknown", "The base is not a known Git commit.")
    parent = _directory(_path(root) if root is not None else top.parent / (top.name + ".worktrees"), owned=True, missing=True)
    target, branch = _path(parent / name), "codex/" + name
    if within(target, common):
        raise WorktreeError("unsafe_config", "The worktree target must be outside the common Git directory.")
    _, code = _git(["check-ref-format", "refs/heads/" + branch], top, allowed=(0, 1), **context)
    if code:
        raise WorktreeError("name_invalid", "The name cannot form a Git branch; choose another name.")
    if os.path.lexists(target) or os.path.lexists(_locator(target)):
        raise WorktreeError("target_exists", "The target or its helper receipt already exists; inspect it or choose another name.")
    if _branch(common, branch):
        raise WorktreeError("branch_exists", "The codex branch already exists; choose another name.")
    tree, _ = _git(["ls-tree", "-r", "-z", _text(raw)], top, **context)
    submodules = [os.fsdecode(entry.partition(b"\t")[2]) for entry in tree.split(b"\0") if entry.startswith(b"160000 ")]
    result = {"format": 1, "repo": str(top), "common_dir": str(common), "common_identity": _identity(common), "name": name, "base_ref": base,
        "repo_git_dir": str(context["git_dir"]), "repo_git_identity": _identity(context["git_dir"]),
        "base": _text(raw), "path": str(target), "root": str(parent), "branch": branch,
        "submodules": submodules, "submodules_initialised": False, "command_config_keys": commands,
        "notes": ["Submodules will not be initialised.", "Symlinks are checked out using Git's settings.",
            "This worktree shares the common Git directory and refs."]}
    return {**result, "plan_id": _digest(result)}


def _locator(path):
    return path.parent / ("." + path.name + ".codex-worktree.json")


def _private(path):
    try:
        _directory(path, create=True, owned=True)
        private_dir(path)
    except (OSError, BridgeError) as exc:
        raise WorktreeError("unsafe_config", "The helper folder must be private and owned by your account.") from exc


def _write(path, value):
    data = json.dumps(value, sort_keys=True).encode()
    if len(data) > 65536:
        raise WorktreeError("unsafe_config", "The helper receipt exceeds 65536 bytes; prepare this worktree manually instead.")
    try:
        _directory(path.parent, owned=True)
        with os.fdopen(private_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, BridgeError) as exc:
        raise WorktreeError("unsafe_config", "The helper receipt could not be written safely; inspect the worktree before retrying.") from exc


def _read(path):
    try:
        _directory(path.parent, owned=True)
        with os.fdopen(private_file(path, os.O_RDONLY | (0 if IS_WINDOWS else os.O_NONBLOCK)), "rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError()
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, BridgeError, ValueError) as exc:
        raise WorktreeError("not_managed", "No intact private helper receipt was found for this worktree.") from exc


def _process_start(pid):
    """None means exited; 'unknown' never permits stealing a lock."""
    try:
        if sys.platform == "linux":
            with open(f"/proc/{pid}/stat", "rb") as stream:
                data = stream.read(4096)
            fields = data.rpartition(b")")[2].split()
            return None if fields[0] in (b"Z", b"X") else int(fields[19])
        if IS_WINDOWS:
            from ctypes import wintypes as w
            class FileTime(ctypes.Structure):
                _fields_ = [("low", w.DWORD), ("high", w.DWORD)]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
            kernel.GetProcessTimes.argtypes, kernel.GetProcessTimes.restype = [w.HANDLE, *([ctypes.POINTER(FileTime)] * 4)], w.BOOL
            kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
            kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
            handle = kernel.OpenProcess(0x101000, False, pid)
            if not handle:
                return None if ctypes.get_last_error() in (87, 1168) else "unknown"
            try:
                wait = kernel.WaitForSingleObject(handle, 0)
                if wait != 258:
                    return None if wait == 0 else "unknown"
                created, exited, system, user = (FileTime() for _ in range(4))
                if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                    return "unknown"
                return created.high << 32 | created.low
            finally:
                kernel.CloseHandle(handle)
        if sys.platform == "darwin":
            class BsdInfo(ctypes.Structure):
                _fields_ = [("ids", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("counts", ctypes.c_uint32 * 6), ("seconds", ctypes.c_uint64), ("micros", ctypes.c_uint64)]
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            lib.proc_pidinfo.argtypes, lib.proc_pidinfo.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int], ctypes.c_int
            info = BsdInfo()
            if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
                return None if ctypes.get_errno() in (errno.ESRCH, errno.ENOENT) else "unknown"
            if info.ids[1] == 5:  # SZOMB; it can no longer own a live operation.
                return None
            return info.seconds * 1000000 + info.micros
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError):
        pass
    return "unknown"


@contextmanager
def _lock(common, lock_dir):
    directory = _path(lock_dir)
    _private(directory)
    key = _digest(os.path.normcase(str(common)))
    path = directory / (key + ".lock")
    guard = private_file(directory / (key + ".guard"), os.O_CREAT | os.O_RDWR)
    acquired, owned = False, False
    try:
        try:
            lock_file(guard)
            acquired = True
        except BlockingIOError as exc:
            raise WorktreeError("locked", "Another worktree operation holds this repository lock.") from exc
        if path.exists() or path.is_symlink():
            try:
                previous = _read(path)
            except WorktreeError as exc:
                raise WorktreeError("locked", "The repository lock has an unknown owner; inspect it before retrying.") from exc
            pid, started = previous.get("pid"), previous.get("start_time")
            if type(pid) is not int or pid <= 0 or type(started) is not int:
                raise WorktreeError("locked", "The repository lock has an unknown owner; inspect it before retrying.")
            observed = _process_start(pid)
            if observed == "unknown" or observed == started:
                raise WorktreeError("locked", "Another worktree operation holds this repository lock.")
            path.unlink()
        start = _process_start(os.getpid())
        if type(start) is not int:
            raise WorktreeError("locked", "The process start time could not be verified; no repository lock was taken.")
        _write(path, {"pid": os.getpid(), "start_time": start})
        owned = True
        yield
    finally:
        try:
            if owned:
                current = _read(path) if path.exists() else None
                if current is not None and current != {"pid": os.getpid(), "start_time": start}:
                    raise WorktreeError("locked", "The repository lock changed owner; its file was left alone.")
                path.unlink(missing_ok=True)
        finally:
            try:
                if acquired:
                    unlock_file(guard)
            finally:
                os.close(guard)


def _load(path):
    target = _path(path)
    record = _read(_locator(target))
    if record.get("path") != str(target) or not isinstance(record.get("common_dir"), str):
        raise WorktreeError("not_managed", "This path does not match its helper receipt.")
    if record.get("format") != 1 or record.get("branch") != "codex/" + _name(record.get("name")):
        raise WorktreeError("not_managed", "This path does not match its helper receipt.")
    if (record.get("name") != target.name or record.get("root") != str(target.parent)
            or not isinstance(record.get("base"), str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", record["base"])):
        raise WorktreeError("not_managed", "This path does not match its helper receipt.")
    canonical = {k: v for k, v in record.items() if k not in ("plan_id", "state", "git_dir", "git_identity")}
    if _digest(canonical) != record.get("plan_id"):
        raise WorktreeError("not_managed", "This path does not match its helper receipt.")
    common = _directory(record["common_dir"], owned=True)
    if str(common) != record["common_dir"] or _identity(common) != record.get("common_identity"):
        raise WorktreeError("not_managed", "The common Git directory does not match its helper receipt.")
    if os.path.lexists(target) and not target.is_dir():
        raise WorktreeError("not_managed", "The target is not a managed worktree directory; nothing was changed.")
    admin = _administration(record)
    if admin:
        if record.get("git_dir") and (record["git_dir"] != str(admin) or record.get("git_identity") != _identity(admin)):
            raise WorktreeError("not_managed", "The Git worktree directory does not match its helper receipt.")
        if not record.get("git_dir") and record.get("state") != "creating":
            raise WorktreeError("not_managed", "The Git worktree directory does not match its helper receipt.")
        record = {**record, "git_dir": str(admin), "git_identity": _identity(admin)}
        if os.path.lexists(target) and target.is_dir() and not target.is_symlink():
            _worktree_pointer(target, admin)
    source = record.get("repo")
    if not isinstance(source, str):
        raise WorktreeError("not_managed", "The repository does not match its helper receipt.")
    if os.path.lexists(source):
        source_dir = _source_git(Path(source))
        source_common = _pointer(source_dir / "commondir") if os.path.lexists(source_dir / "commondir") else source_dir
        if (source_common != common or record.get("repo_git_dir") != str(source_dir)
                or record.get("repo_git_identity") != _identity(source_dir)):
            raise WorktreeError("not_managed", "The repository does not match its helper receipt.")
    return target, record


def _save_registration(target):
    _, record = _load(target)
    if not record.get("git_dir"):
        return
    record["state"] = "registered"
    path = _locator(target)
    with tempfile.TemporaryDirectory(prefix=".codex-worktree-", dir=target.parent) as temporary:
        staged = Path(temporary) / "receipt"
        _write(staged, record)
        _read(path)
        _directory(path.parent, owned=True)
        os.replace(staged, path)


def _exists(record):
    target, common = Path(record["path"]), Path(record["common_dir"])
    entries = _entries(common)
    entry = next((item for item in entries if os.path.normcase(item.get("worktree", "")) == os.path.normcase(str(target))), None)
    return {"directory_exists": target.is_dir(), "registered": entry is not None,
        "branch_exists": bool(_branch(common, record["branch"])), "entry": entry}


@contextmanager
def _mutation(record):
    try:
        yield
    except (WorktreeError, OSError, BridgeError) as exc:
        error = exc if isinstance(exc, WorktreeError) else WorktreeError("unsafe_config",
            "The repository or helper metadata could not be accessed safely; inspect it before retrying.")
        try:
            state = {key: value for key, value in _exists(record).items() if key != "entry"}
        except (WorktreeError, OSError):
            state = {"directory_exists": None, "registered": None, "branch_exists": None}
        error.details.update(state, path=record["path"])
        error.message = str(error) + " Inspect the reported directory, registration and branch state before retrying."
        error.args = (error.message,)
        if error is exc:
            raise
        raise error from exc


@_errors
def create(plan, *, lock_dir) -> dict:
    if not isinstance(plan, dict) or any(not isinstance(plan.get(key), str) for key in ("repo", "name", "base_ref", "root", "plan_id", "common_dir")):
        raise WorktreeError("unsafe_config", "The worktree plan is invalid; create a new plan.")
    proposed = dict(plan)
    current = globals()["plan"](proposed["repo"], proposed["name"], proposed["base_ref"], root=proposed["root"])
    if current != proposed:
        raise WorktreeError("unsafe_config", "The worktree plan changed; inspect a new plan before creating it.")
    with _mutation(current), _lock(Path(current["common_dir"]), lock_dir):
        current = globals()["plan"](proposed["repo"], proposed["name"], proposed["base_ref"], root=proposed["root"])
        if current != proposed:
            raise WorktreeError("unsafe_config", "The worktree plan changed; inspect a new plan before creating it.")
        target, common = Path(current["path"]), Path(current["common_dir"])
        _private(target.parent)
        if os.path.lexists(target) or os.path.lexists(_locator(target)):
            raise WorktreeError("target_exists", "The target or its helper receipt already exists; inspect it or choose another name.")
        record = {**current, "state": "creating"}
        _write(_locator(target), record)
        context = {"git_dir": Path(current["repo_git_dir"]), "work_tree": Path(current["repo"])}
        try:
            _git(["worktree", "add", "-b", current["branch"], "--", str(target), current["base"]], current["repo"], write=True,
                audit_keys=current["command_config_keys"], **context)
        finally:
            _save_registration(target)
        return inspect(target)


@_errors
def inspect(path) -> dict:
    target, record = _load(path)
    context = _context(record)
    common = Path(record["common_dir"])
    commands = _config(common)
    state = _exists(record)
    entry = state.pop("entry")
    managed = bool(entry and entry.get("branch") == "refs/heads/" + record["branch"] and state["branch_exists"])
    if not managed:
        raise WorktreeError("not_managed", "The worktree registration or codex branch does not match its helper receipt.", details=state)
    if target.is_symlink() or os.path.lexists(target) and not target.is_dir():
        raise WorktreeError("not_managed", "The target is not a managed worktree directory; nothing was changed.", details=state)
    head = entry.get("HEAD")
    if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", head):
        raise WorktreeError("not_managed", "The worktree registration or codex branch does not match its helper receipt.")
    base, _ = _git(["merge-base", "--", record["base"], head], common)
    ahead, _ = _git(["rev-list", "--count", record["base"] + ".." + head], common)
    registration = "locked" if "locked" in entry else "missing directory" if not state["directory_exists"] else "prunable" if "prunable" in entry else "ok"
    untracked, dirty, ignored, initialised, nonempty, index_flags = [], False, [], [], [], []
    if state["directory_exists"]:
        top, actual, local_commands, _ = _repository(target)
        commands = sorted(set(commands) | set(local_commands))
        if top != target or actual != common:
            raise WorktreeError("not_managed", "The worktree registration or codex branch does not match its helper receipt.")
        staged, _ = _git(["ls-files", "--stage", "-z"], target, audit_keys=commands, **context)
        tags, _ = _git(["ls-files", "-v", "-z"], target, audit_keys=commands, **context)
        index_flags = [os.fsdecode(item[2:]) for item in tags.split(b"\0") if item and (item[:1].islower() or item[:1] == b"S")]
        for item in staged.split(b"\0"):
            if item.startswith(b"160000 "):
                name = os.fsdecode(item.partition(b"\t")[2])
                module = target / name
                if module.is_symlink() or os.path.lexists(module) and not module.is_dir():
                    nonempty.append(name)
                elif module.is_dir():
                    with os.scandir(module) as contents:
                        if next(contents, None) is not None:
                            nonempty.append(name)
                    if (module / ".git").exists():
                        initialised.append(name)
        status, _ = _git(["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=all"], target, audit_keys=commands, **context)
        entries = iter(status.split(b"\0"))
        for item in entries:
            if item.startswith(b"2 "):
                next(entries, None)  # A rename's original path is a separate NUL field.
            elif item.startswith(b"? "):
                untracked.append(os.fsdecode(item[2:]))
        ignored_raw, _ = _git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], target, audit_keys=commands, **context)
        ignored = [os.fsdecode(item) for item in ignored_raw.split(b"\0") if item]
        dirty = bool(status or ignored or nonempty or index_flags)
    return {**{key: record[key] for key in ("repo", "common_dir", "name", "path", "branch", "plan_id")},
        **state, "managed": True, "registration": registration, "head": head, "base": _text(base),
        "recorded_base": record["base"], "ahead": int(ahead), "dirty": dirty, "untracked_count": len(untracked),
        "untracked": untracked, "ignored_count": len(ignored), "initialised_submodules": initialised,
        "nonempty_submodules": nonempty, "index_flags": index_flags,
        **({"git_dir": record["git_dir"]} if record.get("git_dir") else {}),
        "command_config_keys": commands}


@contextmanager
def _fresh_index(target, report):
    # Never copy skip-worktree, assume-unchanged or cached stat information.
    with tempfile.TemporaryDirectory(prefix="codex-git-index-") as temporary:
        directory = Path(temporary)
        _private(directory)
        index = directory / "index"
        _git(["read-tree", "HEAD"], target, write=True, index=index, audit_keys=report["command_config_keys"], **_context(report))
        _git(["add", "-A", "--", "."], target, write=True, index=index, audit_keys=report["command_config_keys"], **_context(report))
        yield index


@_errors
def handoff(path, *, max_bytes) -> dict:
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise WorktreeError("git_failed", "max_bytes must be between 1 and 67108864.")
    report = inspect(path)
    if report["registration"] != "ok" or report["nonempty_submodules"]:
        raise WorktreeError("unsafe_config", "Handoff needs an available, unlocked worktree without files inside submodules.")
    target = Path(report["path"])
    with _fresh_index(target, report) as index:
        args = ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames"]
        patch, _ = _git([*args, report["base"], "--"], target, index=index, maximum=max_bytes, audit_keys=report["command_config_keys"], **_context(report))
        names, _ = _git([*args, "--name-only", "-z", report["base"], "--"], target, index=index, audit_keys=report["command_config_keys"], **_context(report))
        files = [os.fsdecode(item) for item in names.split(b"\0") if item]
        # Re-read state: a patch must not silently name a different HEAD.
        current = inspect(target)
        if current["head"] != report["head"]:
            raise WorktreeError("git_failed", "The worktree HEAD changed during handoff; retry after it stops changing.")
        return {"patch": patch, "base": report["base"], "head": report["head"], "files": files,
            "untracked": [item for item in report["untracked"] if item in files], "binary": b"\nGIT binary patch\n" in patch,
            "truncated": False}


@_errors
def cleanup(path, *, lock_dir, keep_branch=False) -> dict:
    target, record = _load(path)
    if type(keep_branch) is not bool:
        raise WorktreeError("unsafe_config", "keep_branch must be boolean.")
    with _mutation(record), _lock(Path(record["common_dir"]), lock_dir):
        state = _exists(record)
        if not state["directory_exists"] and not state["registered"] and (not state["branch_exists"] or keep_branch):
            _locator(target).unlink()
            return {"path": str(target), "branch": record["branch"], "removed": False,
                "receipt_removed": True, "branch_kept": state["branch_exists"]}
        report = inspect(target)
        if report["registration"] == "locked":
            raise WorktreeError("locked", "Git has locked this worktree; unlock it explicitly before cleanup.")
        if report["dirty"]:
            raise WorktreeError("dirty", "The worktree has unsaved or ignored files, or files inside submodules; nothing was removed.")
        if report["ahead"] and not keep_branch:
            raise WorktreeError("unmerged_commits", "The worktree has commits outside its base; use keep_branch=True to remove only its directory.")
        if state["directory_exists"]:
            with _fresh_index(target, report) as index:
                _, changed = _git(["diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", "HEAD", "--"], target,
                    index=index, allowed=(0, 1), audit_keys=report["command_config_keys"], **_context(report))
            if changed or inspect(target)["dirty"]:
                raise WorktreeError("dirty", "The worktree has unsaved or ignored files, or files inside submodules; nothing was removed.")
        _git(["worktree", "remove", "--", str(target)], record["common_dir"], write=True, audit_keys=report["command_config_keys"])
        if not keep_branch:
            _git(["branch", "-d", "--", record["branch"]], record["common_dir"], write=True, audit_keys=report["command_config_keys"])
        _locator(target).unlink()
        return {"path": str(target), "branch": record["branch"], "removed": True, "branch_kept": keep_branch}


def json_result(result):
    return {**result, "patch": base64.b64encode(result["patch"]).decode("ascii"), "patch_encoding": "base64"} if "patch" in result else result


def render(result):
    if "patch" in result:
        return (f"Handoff: {len(result['files'])} files, {len(result['patch'])} bytes.\n"
            f"Base: {result['base']}\nHEAD: {result['head']}\n" + result["patch"].decode("utf-8", errors="replace"))
    return "\n".join(f"{key}: {json.dumps(value, ensure_ascii=True) if isinstance(value, (list, dict, bool)) else value}"
        for key, value in result.items())
