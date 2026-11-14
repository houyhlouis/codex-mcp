"""Read Linux sandbox process identities without signalling or running commands."""

import os
import errno
import stat
import sys
from pathlib import Path

from .security import within

AVAILABLE_REASONS = ("ok", "not_linux", "proc_unreadable", "disabled")
CMDLINE_BYTES = 64 * 1024
STAT_BYTES = 4096
FLAGS = ("--sandbox-policy-cwd", "--command-cwd")
RESOLUTION_LIMIT = 64


def _result(reason="ok"):
    return {"available": reason == "ok", "reason": reason, "processes": [], "truncated": False, "scanned": 0}


def _absolute(value):
    if not value or "\x00" in value:
        return None
    # Linux gives // no separate root, unlike POSIX's optional // semantics.
    absolute = os.path.abspath(value)
    if sys.platform == "linux":
        absolute = "/" + absolute.lstrip("/")
    return Path(absolute)


def _paths(data):
    # Discard a partial final argument, including when a long command was capped.
    if b"\x00" not in data:
        return None
    argv = [part.decode("utf-8", errors="surrogateescape") for part in data.rsplit(b"\x00", 1)[0].split(b"\x00")]
    if not argv or os.path.basename(argv[0]) != "codex-linux-sandbox":
        return None
    paths = dict.fromkeys(FLAGS)
    for index, argument in enumerate(argv[1:], 1):
        if argument == "--":
            break
        name, separator, value = argument.partition("=")
        if name not in paths:
            continue
        if not separator:
            value = argv[index + 1] if index + 1 < len(argv) and not argv[index + 1].startswith("--") else ""
        paths[name] = _absolute(value)
    return paths if any(paths.values()) else None


def _start_time(data, pid):
    head, separator, tail = data.rpartition(b")")
    identity, opening, _ = head.partition(b"(")
    fields = tail.split()
    if not separator or not opening or int(identity.strip()) != pid or len(fields) < 20:
        raise ValueError("Malformed process stat")
    value = int(fields[19])  # tail starts at field 3; starttime is field 22.
    if value < 0:
        raise ValueError("Malformed process start time")
    return value


def scan(projects, *, proc_root="/proc", uid=None, limit=64):
    """Prefer lexical paths; resolve unmatched cwd spellings once per scan.

    Each same-user process gets one directory stat and a bounded cmdline read.
    Only matching candidates need the additional bounded stat-file read.
    An explicit alternate proc_root also permits portable fake-proc tests.
    """
    if sys.platform != "linux" and os.fspath(proc_root) == "/proc":
        return _result("not_linux")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    uid = getattr(os, "getuid", lambda: 0)() if uid is None else uid
    selected = []
    for value in projects:
        original = os.fspath(value)
        if isinstance(original, str) and Path(original).is_absolute():
            path = _absolute(original)
            if path is not None:
                selected.append((original, path))
    result = _result()
    resolved_projects, resolved_paths = None, {}
    def resolved_path(path):
        if path not in resolved_paths:
            if len(resolved_paths) >= RESOLUTION_LIMIT:
                result.update(available=False, reason="proc_unreadable")
                return None
            try:
                resolved_paths[path] = _absolute(os.path.realpath(path, strict=True))
            except (OSError, ValueError):
                resolved_paths[path] = None
                result.update(available=False, reason="proc_unreadable")
        return resolved_paths[path]

    def matching_project(paths):
        nonlocal resolved_projects
        for original, root in selected:
            if any(path is not None and (within(path, root) or within(root, path)) for path in paths.values()):
                return original
        if resolved_projects is None:
            resolved_projects = [(original, resolved_path(root)) for original, root in selected]
        for path in paths.values():
            if path is None:
                continue
            resolved = resolved_path(path)
            if resolved is not None:
                for original, root in resolved_projects:
                    if root is not None and (within(resolved, root) or within(root, resolved)):
                        return original
        return None
    try:
        with os.scandir(proc_root) as entries:
            if not selected:
                return result
            for entry in entries:
                if not entry.name.isascii() or not entry.name.isdecimal():
                    continue
                try:
                    pid = int(entry.name)
                    if pid <= 0:
                        continue
                    result["scanned"] += 1
                    info = entry.stat(follow_symlinks=False)
                    if info.st_uid != uid or not stat.S_ISDIR(info.st_mode):
                        continue
                    directory = Path(entry.path)
                    with (directory / "cmdline").open("rb") as stream:
                        paths = _paths(stream.read(CMDLINE_BYTES))
                    if paths is None:
                        continue
                    project = matching_project(paths)
                    if project is None:
                        continue
                    with (directory / "stat").open("rb") as stream:
                        data = stream.read(STAT_BYTES + 1)
                    if len(data) > STAT_BYTES:
                        result.update(available=False, reason="proc_unreadable")
                        continue
                    started = _start_time(data, pid)
                    if len(result["processes"]) >= limit:
                        result["truncated"] = True
                        break
                    result["processes"].append({"pid": pid, "start_time": started,
                        "policy_cwd": str(paths[FLAGS[0]]) if paths[FLAGS[0]] is not None else None,
                        "command_cwd": str(paths[FLAGS[1]]) if paths[FLAGS[1]] is not None else None,
                        "project": project})
                except OSError as exc:
                    # A vanished PID is harmless. A failed read of a process
                    # that may still exist cannot prove the project is clear.
                    if not isinstance(exc, (FileNotFoundError, ProcessLookupError)) and exc.errno not in (errno.ENOENT, errno.ESRCH):
                        result.update(available=False, reason="proc_unreadable")
                    continue
                except (ValueError, OverflowError):
                    result.update(available=False, reason="proc_unreadable")
                    continue
    except (OSError, ValueError):
        result.update(available=False, reason="proc_unreadable")
    result["processes"].sort(key=lambda process: process["pid"])
    return result


def scan_for(config, projects):
    if not config.sandbox_process_check:
        return _result("disabled")
    return scan(projects)


def describe(result):
    if not result.get("available") and not result["processes"] and not result["truncated"]:
        return {"not_linux": "The leftover-process check is available only on Linux.",
            "disabled": "The leftover-process check is turned off in the settings file.",
            "proc_unreadable": "The Linux process check could not read /proc."}[result["reason"]]
    processes = result["processes"]
    if not processes:
        return ("Codex sandbox processes are still running in this project; the process list was truncated."
            if result["truncated"] else "No Codex sandbox processes were found in this project.")
    count = len(processes)
    pids = ", ".join(str(process["pid"]) for process in processes[:10])
    if count > 10:
        pids += f" and {count - 10} more"
    return (f"{'At least ' if result['truncated'] or not result.get('available') else ''}{count} Codex sandbox process{'es are' if count != 1 else ' is'} "
        f"still running in this project (PID {pids}).")
