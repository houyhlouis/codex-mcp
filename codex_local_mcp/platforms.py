"""Small cross-platform process, locking and stdio primitives."""
import asyncio
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def canonical_system_path(path):
    """Resolve only macOS's root-owned /tmp, /var and /etc aliases."""
    if sys.platform == "darwin":
        for name in ("tmp", "var", "etc"):
            alias, target = Path("/") / name, Path("/private") / name
            if path == alias or alias in path.parents:
                if alias.is_symlink() and alias.lstat().st_uid == 0 and alias.resolve() == target:
                    return target / path.relative_to(alias)
    return path


def default_state_dir():
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "CodexMCP"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "codex-mcp"


def lock_file(fd):
    if IS_WINDOWS:
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError("Local broker lock is held") from exc
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(fd):
    """Release a lock taken by lock_file before closing its descriptor.

    Windows may release a lock left on a closed handle only after a delay,
    which could make a broker started immediately afterwards fail.
    """
    if IS_WINDOWS:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def process_running(pid):
    """Best-effort liveness check that never signals or terminates the process."""
    if IS_WINDOWS:
        from .windows import process_running as windows_process_running
        return windows_process_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def unix_peer_uid(sock):
    import socket
    import struct
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if hasattr(sock, "getpeereid"):
        return sock.getpeereid()[0]
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError(ctypes.get_errno(), "Cannot identify Unix-socket peer")
        return uid.value
    raise OSError("This platform has no supported peer identity check")


def detached_process_options():
    if IS_WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def executable_argv(binary):
    # Allows an explicitly configured Python launcher, including the offline
    # fixture. Never uses a shell or passes model prompts as command arguments.
    return [sys.executable, binary] if Path(binary).suffix.lower() == ".py" else [binary]


def _path_directories():
    key = next((k for k in os.environ if k.lower() == "path"), "PATH") if IS_WINDOWS else "PATH"
    directories, ignored = [], False
    for directory in os.environ.get(key, "").split(os.pathsep):
        if IS_WINDOWS:
            directory = directory.strip().strip('"')
        if not directory or not Path(directory).is_absolute():
            ignored = True
            continue
        directories.append(Path(directory))
    return directories, ignored


def find_on_path(name):
    """Look up a command using only absolute PATH entries on every platform."""
    suffixes = [""]
    if IS_WINDOWS and not Path(name).suffix:
        suffixes = [ext.lower() for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    directories, _ = _path_directories()
    for directory in directories:
        for suffix in suffixes:
            candidate = directory / (name + suffix)
            if candidate.is_file() and (IS_WINDOWS or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def resolve_codex(binary):
    candidate = Path(binary).expanduser()
    # The broker reloads configuration from a different working directory.
    explicit_path = candidate.is_absolute()
    if not explicit_path and (binary.startswith("~") or candidate.drive or os.sep in binary
                              or bool(os.altsep and os.altsep in binary)):
        raise ValueError(f"codex_binary={binary!r} is a relative path; the broker runs from a different "
                         "working directory. Set codex_binary to an absolute path")
    if explicit_path:
        found = str(candidate) if candidate.is_file() else None
    else:
        found = find_on_path(binary)
    if not found:
        message = (f"Codex executable not found for codex_binary={binary!r}; "
                   "install Codex or set codex_binary to an absolute path")
        if not explicit_path and _path_directories()[1]:
            message += ". Empty or relative PATH entries were ignored"
        raise ValueError(message)
    path = Path(found).resolve()
    if not IS_WINDOWS or path.suffix.lower() in (".exe", ".py"):
        return str(path)
    # Resolve npm's trusted installation layout to its native binary instead of
    # running a .cmd/.bat shim through cmd.exe with configuration arguments.
    if path.suffix.lower() in (".cmd", ".bat", ".ps1", ".js") or not path.suffix:
        bases = {path.parent, path.parent.parent, path.parent.parent.parent}
        packages = set()
        for base in bases:
            for modules in (base / "node_modules", base):
                scope = modules / "@openai"
                packages.update((scope / "codex", scope / "codex-win32-x64", scope / "codex-win32-arm64"))
                packages.update((scope / "codex" / "node_modules" / "@openai" / "codex-win32-x64",
                                 scope / "codex" / "node_modules" / "@openai" / "codex-win32-arm64"))
        triples = ["aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc"] if platform.machine().lower() in ("arm64", "aarch64") else ["x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"]
        for triple in triples:
            for package in sorted(packages):
                for folder in ("bin", "codex"):
                    native = package / "vendor" / triple / folder / "codex.exe"
                    if native.is_file():
                        return str(native.resolve())
    raise ValueError("Set codex_binary to native codex.exe; no supported native executable was found beside the launcher")


def path_search_provenance():
    key = next((k for k in os.environ if k.lower() == "path"), "PATH") if IS_WINDOWS else "PATH"
    entries = os.environ.get(key, "").split(os.pathsep)
    entries = [entry.strip().strip('"') for entry in entries] if IS_WINDOWS else entries
    empty = sum(not entry for entry in entries)
    relative = sum(bool(entry) and not Path(entry).is_absolute() for entry in entries)
    return {"ignored_path_entries": bool(empty or relative), "ignored_empty_entries": empty, "ignored_relative_entries": relative}


def executable_provenance(binary):
    """Describe the existing lookup policy without executing a candidate."""
    candidate = Path(binary).expanduser()
    explicit = candidate.is_absolute()
    found = str(candidate) if explicit else find_on_path(binary)
    result = {"configured": binary, "lookup": "absolute" if explicit else "absolute_PATH",
              "path_directory": os.path.normpath(str(Path(found).parent.absolute())) if found and not explicit else None,
              **path_search_provenance(), "resolved": None}
    result["resolved"] = resolve_codex(binary)
    return result


def codex_environment(binary):
    env = dict(os.environ)
    base = Path(binary).parent.parent
    additions = [str(p) for p in (base / "codex-path", base / "path") if p.is_dir()]
    if additions:
        key = next((key for key in env if key.lower() == "path"), "PATH")
        env[key] = os.pathsep.join([*additions, env.get(key, "")])
    return env


async def stdio_lines(stream, limit=1024 * 1024):
    """UTF-8 framing over ordinary stdin works with Windows pipes and consoles.

    A daemon pump avoids unsupported connect_read_pipe on Windows and does not
    keep the process alive during interpreter shutdown.
    """
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=32)
    stopping = threading.Event()

    def pump():
        try:
            while not stopping.is_set():
                line = stream.readline(limit + 1)
                if isinstance(line, str):
                    line = line.encode("utf-8")
                item = ValueError("Input frame exceeds size limit") if len(line) > limit else line
                future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                while not stopping.is_set():
                    try:
                        future.result(timeout=0.1)
                        break
                    except TimeoutError:
                        continue
                if stopping.is_set():
                    future.cancel()
                if not line or isinstance(item, Exception):
                    break
        except (RuntimeError, OSError, ValueError):
            if not stopping.is_set() and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(queue.put(b""), loop)

    threading.Thread(target=pump, daemon=True, name="mcp-stdin").start()
    try:
        while True:
            item = await queue.get()
            if isinstance(item, Exception):
                raise item
            if not item:
                break
            yield item
    finally:
        stopping.set()
