"""Read-only local diagnostics. Never reads authentication files or runs a turn."""
import asyncio
import errno
import importlib.util
import json
import os
import socket
import sqlite3
import stat
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from . import __version__
from . import procscan
from .broker import ensure_broker, read_health, read_status
from .config import Config, environment_identity, load_config
from .contract import (MCP_VERSIONS, CODEX_MIN_VERSION, CODEX_TESTED_VERSIONS, NATIVE_METHODS,
                       NATIVE_NOTIFICATIONS, NATIVE_SERVER_REQUESTS, REQUIRED, codex_compatibility, parse_codex_version)
from .platforms import IS_WINDOWS, default_state_dir, executable_argv, executable_provenance, _path_directories, path_search_provenance
from .security import BridgeError, read_config_file, private_dir, private_file, checked_directory, redact
from .store import Store, SCHEMA_VERSION, APPLICATION_ID

STEPS = {
    "bridge_version": "Nothing to do.",
    "python_version": "Install Python 3.11 or newer and start the server with it.",
    "codex_tested": "Nothing to do.",
    "codex_untested": "This Codex version is newer than the ones tested. It normally works. If something behaves oddly, try a tested version.",
    "codex_unsupported": "Update Codex (codex update) to a supported version before starting tasks.",
    "codex_version_unchecked": "Run the Codex program with --version in this terminal to see why it does not answer.",
    "binary_resolved": "Nothing to do.",
    "binary_missing": "Install Codex, or set codex_binary in the settings file to the full path of the Codex program.",
    "binary_not_executable": "Set codex_binary to the Codex program itself: an executable file, or a Python launcher that can be read.",
    "binary_relative": "Set codex_binary to a full path. The background server runs in another folder, so a relative path does not work.",
    "binary_invalid": "Set codex_binary to a plain command name such as codex, or to the full path of the Codex program.",
    "path_ignored": "Empty and relative PATH entries are skipped for safety. Put the folder that contains Codex into PATH with its full path, or set codex_binary.",
    "config_ok": "Nothing to do.",
    "config_invalid": "Correct the settings file. Nothing is started until it is valid.",
    "state_missing": "Nothing to do. The folder is created the first time the server starts.",
    "state_ok": "Nothing to do.",
    "state_unsafe": "Use a state folder that belongs to your account and that nobody else can open, or let the server create the default one.",
    "state_unreadable": "Run doctor as the same user account that runs your MCP client, and check that this account can open the state folder.",
    "endpoint_ok": "Nothing to do.",
    "endpoint_too_long": "Set state_dir in the settings file to a shorter path.",
    "ipc_available": "Nothing to do.",
    "ipc_unavailable": "Local sockets are blocked here. Run doctor in the same environment as your MCP client; a sandbox or container may forbid them.",
    "ipc_unchecked": "Fix the state folder or temporary folder problem above, then run doctor again.",
    "broker_absent": "Nothing to do. The background server starts when it is first needed.",
    "broker_healthy": "Nothing to do.",
    "broker_unreachable": "Check whether the background server is still working (server.py status). If it hangs, stop it with server.py stop.",
    "broker_identity_mismatch": "Stop the old background server with server.py stop, using the same --config it was started with. The next call starts a new one.",
    "broker_environment_mismatch": "Stop the background server and start it again from the environment (CODEX_HOME) you want it to use.",
    "broker_codex_version_mismatch": "Codex was updated while the background server was running. Let active tasks finish, then run server.py stop.",
    "broker_identity_unchecked": "The running background server is from an older version. Stop it with that version's server.py stop.",
    "broker_unchecked": "Fix the state folder problem reported above, then run doctor again.",
    "broker_stale_socket": "A socket file was left behind. Make sure no background server is running for this state folder; the next start replaces it.",
    "broker_stale_lock": "Nothing to do. The lock file is not in use.",
    "broker_start_failed": "Fix the problems reported above, then run doctor --start-broker again.",
    "database_absent": "Nothing to do. The database is created the first time the server starts.",
    "database_ok": "Nothing to do.",
    "database_corrupt": "Do not delete this database. Stop the server and restore a backup into a new state folder (see the documentation on backups).",
    "database_newer": "This database was written by a newer version. Use that version, or restore an older backup into a new state folder.",
    "database_older": "Nothing to do. The database is upgraded automatically at the next start, after a backup has been saved next to it.",
    "database_layout_invalid": "This does not look like an intact Codex MCP database. Check that state_dir points to the right folder. Do not overwrite the file.",
    "database_unchecked": "Fix the state folder problem reported above, then run doctor again.",
    "toolchain_readable": "Nothing to do.",
    "toolchain_unreadable": "Correct toolchain_paths in the settings file: each entry must be the full path of an existing folder you can read.",
    "signin_ok": "Nothing to do.",
    "signin_missing": "Sign in with codex login, then run doctor again.",
    "signin_unchecked": "To check the sign-in as well, run doctor --start-broker. That starts the background server and Codex, but no model.",
    "native_contract": "Nothing to do.",
    "schema_inventory_ok": "Nothing to do.",
    "schema_incompatible": "This Codex version does not offer something the server needs. Install a tested Codex version.",
    "schema_unchecked": "To compare the server's messages with this Codex version, run doctor --schema. No model is used.",
    "schema_wire_ok": "Nothing to do.",
    "schema_wire_unchecked": "The full message check needs a source checkout and the optional jsonschema package.",
    "schema_failed": "Codex could not generate its schema, or an optional development package is missing. See the message above.",
    "probe_cleanup_failed": "A temporary test folder could not be removed. Delete only the folder named in the message, after the check has finished.",
    "process_check_available": "Nothing to do.",
    "process_check_not_linux": "Nothing to do.",
    "process_check_disabled": "Nothing to do.",
    "process_check_unreadable": "Check that /proc is readable, then run doctor again.",
    "process_check_unchecked": "Fix the state folder or database problem reported above, then run doctor again.",
    "sandbox_processes_running": "Nothing to do.",
    "sandbox_processes_untracked": "Stop these processes with kill PID. Nothing else is needed.",
}
VERSION_TIMEOUT = 10
LEVEL = {"ok": 0, "info": 0, "warning": 1, "error": 2}
REPORT_SCHEMA = {"type": "object", "required": ["format", "bridge_version", "summary", "exit_code", "facts", "findings"],
    "additionalProperties": False, "properties": {
        "format": {"const": 1}, "bridge_version": {"type": "string"},
        "summary": {"enum": list(LEVEL)}, "exit_code": {"enum": [0, 1, 2]}, "facts": {"type": "object"},
        "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["severity", "code", "message", "next_step"], "properties": {
                "severity": {"enum": list(LEVEL)}, "code": {"enum": list(STEPS)},
                "message": {"type": "string"}, "next_step": {"type": "string"}}}}}}


def os_error(exc):
    number = getattr(exc, "errno", None)
    return {"errno": number, "winerror": getattr(exc, "winerror", None),
            "message": os.strerror(number) if number is not None else type(exc).__name__}


def readable_size(size):
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def lock_status(path):
    try:
        if IS_WINDOWS:
            from .windows import open_fd
            fd = open_fd(path, os.O_RDONLY, private=True)
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return "unknown"
        if IS_WINDOWS:
            import msvcrt
            if not info.st_size:
                return "free"
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    except BlockingIOError:
        return "held"
    except OSError as exc:
        # The CRT reports a nonblocking byte-lock conflict as EACCES on
        # Windows, rather than Python's POSIX BlockingIOError.
        return "held" if IS_WINDOWS and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) else "unknown"
    finally:
        os.close(fd)


async def ipc_probe(directory):
    if IS_WINDOWS:
        from .windows import PipeServer
        async def unused(payload):
            return b"{}"
        server = PipeServer(r"\\.\pipe\codex-mcp-doctor-" + uuid.uuid4().hex, unused, 1024)
        server.start()
        server.close()
        await server.wait_closed()
    else:
        path = directory / (uuid.uuid4().hex[:8] + ".s")
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        created = False
        try:
            endpoint.bind(str(path))
            created = True
            endpoint.listen(1)
        finally:
            endpoint.close()
            if created:
                path.unlink()


def inspect_state(state):
    info = state.lstat()
    facts = {"exists": True, "mode": oct(stat.S_IMODE(info.st_mode)), "owner_uid": None if IS_WINDOWS else info.st_uid,
             "ownership_check": "Windows owner/DACL" if IS_WINDOWS else "POSIX uid/mode"}
    safe = stat.S_ISDIR(info.st_mode) and not state.is_symlink()
    if safe:
        checked_directory(state)
    if IS_WINDOWS and safe:
        from .windows import open_handle, api
        handle = open_handle(state, directory=True)
        try:
            api().check_access(handle)
        finally:
            api().kernel.CloseHandle(handle)
    elif not IS_WINDOWS:
        safe = safe and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
    return facts, safe


def database_health(state, add, facts):
    path = state / "state.sqlite3"
    database = facts["database"] = {"supported_schema": SCHEMA_VERSION, "exists": path.exists(),
        "backups": [{"name": p.name, "bytes": p.lstat().st_size} for p in sorted({
            *state.glob("state.sqlite3.pre-v*-*.bak"), *state.glob("state.sqlite3.backup-*.bak")})
            if p.is_file() and not p.is_symlink()]}
    if not path.exists():
        add("info", "database_absent", f"No database exists at {path}.")
        return []
    if path.is_symlink() or not path.is_file():
        add("error", "database_layout_invalid", f"The database path {path} is not a regular file, or it is a link.")
        return
    database["bytes"] = path.stat().st_size
    db = None
    try:
        os.close(private_file(path, os.O_RDONLY))
        wal = (state / "state.sqlite3-wal").exists()
        if wal and not (state / "state.sqlite3-shm").exists():
            add("info", "database_unchecked", f"The database at {path} has a write log but no shared-memory file. This check did not create one.")
            return
        immutable = not wal and lock_status(state / "broker.lock") in ("free", "absent")
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=1)
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("PRAGMA query_only=ON")
        version = database["schema_version"] = db.execute("PRAGMA user_version").fetchone()[0]
        integrity = database["quick_check"] = db.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            add("error", "database_corrupt", f"The database at {path} failed its integrity check.")
            return
        if version > SCHEMA_VERSION:
            add("error", "database_newer", f"The database at {path} uses layout version {version}, but this server supports up to {SCHEMA_VERSION} ({readable_size(database['bytes'])}).")
            return
        application = db.execute("PRAGMA application_id").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if application not in (0, APPLICATION_ID) or not {"tasks", "requests", "events"} <= tables:
            add("error", "database_layout_invalid", f"The file at {path} is not a recognized Codex MCP database.")
            return
        try:
            _, empty = Store._inspect(db)
        except BridgeError:
            add("error", "database_layout_invalid", f"The database at {path} has layout or upgrade records that do not match version {version}.")
            return
        if empty:
            add("error", "database_layout_invalid", f"The file at {path} is not a recognized Codex MCP database.")
            return
        database["counts"] = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in ("tasks", "requests", "events")}
        database["counts"]["open_claims"] = db.execute("SELECT count(*) FROM workspace_claims WHERE state!='inactive'").fetchone()[0] if "workspace_claims" in tables else None
        add("info" if version < SCHEMA_VERSION else "ok", "database_older" if version < SCHEMA_VERSION else "database_ok",
            f"The database was written by an older version and will be upgraded automatically at the next start ({readable_size(database['bytes'])})."
            if version < SCHEMA_VERSION else f"The database uses layout version {version}, and its check passed ({readable_size(database['bytes'])}).")
        projects = []
        for row in db.execute("SELECT data FROM projects"):
            try:
                value = json.loads(row[0])
                project = value.get("path") if isinstance(value, dict) else None
                if not isinstance(project, str) or "\x00" in project or not Path(project).is_absolute():
                    return None
                projects.append(project)
            except (ValueError, TypeError):
                return None
        return list(dict.fromkeys(projects))
    except sqlite3.OperationalError as exc:
        code = "database_unchecked" if getattr(exc, "sqlite_errorcode", None) in (
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH, sqlite3.SQLITE_READONLY) else "database_layout_invalid"
        reason = {sqlite3.SQLITE_BUSY: "is busy", sqlite3.SQLITE_LOCKED: "is locked",
            sqlite3.SQLITE_INTERRUPT: "took too long to check", sqlite3.SQLITE_CANTOPEN: "could not be opened",
            sqlite3.SQLITE_PERM: "could not be read with this account's access", sqlite3.SQLITE_AUTH: "could not be read with this account's access",
            sqlite3.SQLITE_READONLY: "could not be checked through a read-only connection"}.get(getattr(exc, "sqlite_errorcode", None))
        add("warning" if code == "database_unchecked" else "error", code,
            f"The database at {path} {reason}." if reason else f"The database at {path} could not be fully checked.")
    except (sqlite3.DatabaseError, OSError):
        add("error", "database_corrupt", f"The file at {path} could not be read as a healthy SQLite database.")
    except BridgeError:
        add("error", "database_unchecked", f"Access to the database at {path} could not be verified.")
    finally:
        if db is not None:
            db.close()


async def schema_probe(binary, add, facts):
    try:
        # Keep nested offline fixture state below the Unix socket path limit.
        with tempfile.TemporaryDirectory(prefix="cds-") as directory:
            root = Path(directory).resolve()
            private_dir(root)
            output, home, temporary = root / "schemas", root / "codex-home", root / "tmp"
            for path in (output, home, temporary):
                path.mkdir(mode=0o700)
            env = dict(os.environ, CODEX_HOME=str(home), TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
            env.pop("CODEX_PROFILE", None)
            run = await asyncio.create_subprocess_exec(*executable_argv(binary), "app-server", "generate-json-schema", "--experimental", "--out", str(output),
                cwd=root, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                code = await asyncio.wait_for(run.wait(), 60)
            except BaseException:
                run.kill()
                await asyncio.wait_for(run.wait(), 5)
                raise
            if code:
                raise ValueError("Schema generator failed")
            documents = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in output.rglob("*.json")}
            missing = []
            optional = []
            for name, inventory in (("ClientRequest", NATIVE_METHODS), ("ServerRequest", NATIVE_SERVER_REQUESTS), ("ServerNotification", NATIVE_NOTIFICATIONS)):
                document = documents.get(name)
                if not isinstance(document, dict) or not isinstance(document.get("oneOf"), list):
                    raise ValueError("Generated schema inventory format is not recognized")
                methods = set()
                for branch in document["oneOf"]:
                    if not isinstance(branch, dict) or not isinstance(branch.get("properties"), dict):
                        raise ValueError("Generated schema inventory format is not recognized")
                    method_schema = branch["properties"].get("method")
                    if not isinstance(method_schema, dict) or not isinstance(method_schema.get("enum"), list) or not all(isinstance(method, str) for method in method_schema["enum"]):
                        raise ValueError("Generated schema inventory format is not recognized")
                    methods.update(method_schema["enum"])
                for method, requirement in inventory.items():
                    if method == "initialized":
                        continue
                    if method not in methods:
                        (missing if requirement == REQUIRED else optional).append(method)
            facts["schema"] = {"generated": True, "required_methods_missing": missing, "optional_methods_missing": optional,
                               "wire_checked": False, "checked": "generated method inventory; no runtime scope acknowledgement was tested"}
            add("error" if missing else "ok", "schema_incompatible" if missing else "schema_inventory_ok",
                f"Codex is missing {len(missing)} required operations and {len(optional)} optional operations.")
            script = Path(__file__).resolve().parent.parent / "scripts" / "check_protocol.py"
            if not script.is_file() or importlib.util.find_spec("jsonschema") is None:
                add("info", "schema_wire_unchecked", "The full message check was not run because the source checkout or optional jsonschema package is missing.")
                return
            process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--codex", binary,
                cwd=root, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                code = await asyncio.wait_for(process.wait(), 90)
            except BaseException:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
                raise
            facts["schema"]["wire_checked"] = code == 0
            add("ok" if code == 0 else "error", "schema_wire_ok" if code == 0 else "schema_failed",
                "The server's recorded messages match this Codex version." if code == 0 else "The full message check failed.")
    except (OSError, ValueError, TimeoutError):
        add("error", "schema_failed", "The Codex message check could not finish. No model was used.")


async def version_probe(binary, directory):
    home, temporary = directory / "codex-home", directory / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    env = dict(os.environ, CODEX_HOME=str(home), TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
    env.pop("CODEX_PROFILE", None)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*executable_argv(binary), "--version", cwd=directory, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(process.communicate(), VERSION_TIMEOUT)
        if process.returncode:
            raise ValueError("Version probe failed")
        value = output.decode().strip()
        parse_codex_version(value)
        return codex_compatibility(value)
    finally:
        if process and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)


async def diagnose(filename=None, *, start_broker=False, schema=False):
    manager, directory, error = None, None, None
    try:
        manager = tempfile.TemporaryDirectory(prefix="cdoctor-")
        directory = Path(manager.name).resolve()
        private_dir(directory)
    except (OSError, ValueError, BridgeError) as exc:
        error = os_error(exc)
        directory = None
    report = None
    try:
        report = await _diagnose(filename, start_broker=start_broker, schema=schema,
                                 probe_directory=directory, temporary_error=error)
        return report
    finally:
        if manager is not None:
            try:
                manager.cleanup()
                if report is not None:
                    report["facts"].setdefault("temporary_probes", {}).update(isolated=directory is not None, removed=True)
            except OSError as exc:
                if report is None:
                    raise
                report["facts"]["temporary_probes"] = {"isolated": True, "removed": False,
                    "path": str(Path(manager.name).absolute()), "os_error": os_error(exc)}
                report["findings"].append({"severity": "warning", "code": "probe_cleanup_failed",
                    "message": f"The temporary test folder {report['facts']['temporary_probes']['path']} could not be removed.",
                    "next_step": STEPS["probe_cleanup_failed"]})
                report["exit_code"] = max(1, report["exit_code"])
                report["summary"] = "error" if report["exit_code"] == 2 else "warning"


async def _diagnose(filename=None, *, start_broker=False, schema=False, probe_directory=None, temporary_error=None):
    findings, facts = [], {"python_version": sys.version.split()[0], "supported_mcp_revisions": list(MCP_VERSIONS),
        "minimum_codex_version": CODEX_MIN_VERSION, "tested_codex_versions": list(CODEX_TESTED_VERSIONS),
        "environment_identity": environment_identity(), "probes": {"start_broker": start_broker, "schema": schema},
        "temporary_probes": {"isolated": probe_directory is not None, "removed": probe_directory is None},
        "codex_compatibility": {"version": None, "status": "could_not_check", "reason": "Codex version could not be checked"}}
    if temporary_error is not None:
        facts["temporary_probes"]["creation_error"] = temporary_error
    def add(severity, code, message):
        findings.append({"severity": severity, "code": code, "message": redact(message), "next_step": STEPS[code]})
    add("ok", "bridge_version", f"Codex MCP server {__version__}.")
    add("ok" if sys.version_info >= (3, 11) else "error", "python_version", f"Python {facts['python_version']}.")
    path, data, config = Path(filename).expanduser().absolute() if filename else None, {}, None
    try:
        data = tomllib.loads(read_config_file(path)) if path else {}
        config = load_config(filename)
        add("ok", "config_ok", f"The settings file is {config.path}." if config.path else "No settings file was supplied. The server uses its default settings.")
    except (BridgeError, OSError, ValueError) as exc:
        add("error", "config_invalid", f"The settings file {path} is invalid." if path else "The default settings are invalid.")
    facts["config_path"] = str(config.path if config else Path(os.path.normpath(str(path)))) if path else None
    state_value = data.get("state_dir", str(default_state_dir()))
    state = config.state_dir if config else Path(state_value).expanduser().absolute() if isinstance(state_value, str) else default_state_dir().absolute()
    valid_state_value = isinstance(state_value, str)
    facts["state_dir"] = str(state) if valid_state_value else None
    binary, resolved = data.get("codex_binary", "codex"), None
    directories, ignored = _path_directories()
    facts["executable"] = {"configured": binary if isinstance(binary, str) else None, **path_search_provenance()}
    if ignored:
        add("warning", "path_ignored", f"PATH contains {facts['executable']['ignored_empty_entries']} empty entries and {facts['executable']['ignored_relative_entries']} relative entries. They were skipped.")
    try:
        if not isinstance(binary, str):
            raise ValueError("Invalid binary setting")
        provenance = executable_provenance(binary)
        facts["executable"] = provenance
        resolved = provenance["resolved"]
        if not os.access(resolved, os.R_OK if Path(resolved).suffix.lower() == ".py" else os.X_OK) or not Path(resolved).is_file():
            add("error", "binary_not_executable", f"The Codex program at {resolved} cannot be run or read.")
            resolved = None
        else:
            add("ok", "binary_resolved", f"The Codex program is {resolved}. " + (
                f"It was found on PATH in {provenance['path_directory']}." if provenance["path_directory"] else "It was selected by its full path."))
    except (OSError, ValueError) as exc:
        candidate = Path(binary).expanduser() if isinstance(binary, str) else None
        relative = candidate is not None and not candidate.is_absolute() and (binary.startswith("~") or candidate.drive or os.sep in binary or (os.altsep and os.altsep in binary))
        try:
            nonexecutable = candidate is not None and ((candidate.is_absolute() and candidate.is_file()) or any((directory / binary).is_file() for directory in directories))
        except (OSError, ValueError):
            nonexecutable = False
        code = "binary_relative" if relative else "binary_invalid" if "native executable" in str(exc) else "binary_not_executable" if nonexecutable else "binary_missing" if isinstance(binary, str) else "binary_invalid"
        messages = {"binary_relative": f"The Codex setting {binary!r} is a relative path.",
            "binary_invalid": f"The Codex setting {binary!r} does not name a supported program." if isinstance(binary, str) else "The Codex setting must be a command name or a full path.",
            "binary_not_executable": f"The Codex setting {binary!r} points to a file that cannot be run or read.",
            "binary_missing": f"No Codex program was found for {binary!r}."}
        add("error", code, messages[code])
    if resolved and probe_directory is not None:
        try:
            compatibility = facts["codex_compatibility"] = await version_probe(resolved, probe_directory)
            status = compatibility["status"]
            version = parse_codex_version(compatibility["version"]).version
            messages = {"tested": f"Codex {version} is a tested version.",
                "untested": f"Codex {version} has not been tested with this server.",
                "unsupported": f"Codex {version} is older than the supported minimum {CODEX_MIN_VERSION}."}
            add({"tested": "ok", "untested": "warning", "unsupported": "error"}[status], "codex_" + status, messages[status])
        except (OSError, ValueError, TimeoutError) as exc:
            facts["version_probe_error"] = os_error(exc)
            add("warning", "codex_version_unchecked",
                f"Codex at {resolved} did not answer --version within {VERSION_TIMEOUT} seconds." if isinstance(exc, TimeoutError) else
                f"Codex at {resolved} could not be run to query its version." if isinstance(exc, OSError) else
                f"Codex at {resolved} did not return a usable version.")
    elif resolved:
        facts["version_probe_error"] = temporary_error
        add("warning", "codex_version_unchecked", f"Codex at {resolved} was not checked because a private temporary folder is unavailable.")
    else:
        add("info", "codex_version_unchecked", "The Codex version was not checked because no Codex program was found.")
    safe_state, state_missing = False, False
    try:
        if not valid_state_value:
            raise BridgeError("Invalid state path")
        facts["state"], safe_state = inspect_state(state)
        add("ok" if safe_state else "error", "state_ok" if safe_state else "state_unsafe",
            f"The state folder {state} is private to your account." if safe_state else f"The state folder {state} is not private to your account.")
    except FileNotFoundError:
        state_missing = True
        facts["state"] = {"exists": False}
        add("info", "state_missing", f"The state folder {state} does not exist.")
    except (OSError, ValueError, BridgeError):
        safe_state = False
        add("error", "state_unreadable", f"The state folder {state} could not be opened or its access could not be verified.")
    enabled = data.get("sandbox_process_check", True)
    probe_config = config or Config(path, state, resolved or "codex",
        sandbox_process_check=enabled if type(enabled) is bool else True)
    try:
        endpoint = probe_config.endpoint if valid_state_value else ""
    except (OSError, ValueError) as exc:
        endpoint = ""
        facts["ipc_error"] = os_error(exc)
        add("warning", "ipc_unavailable", f"The local connection path could not be determined: {facts['ipc_error']['message']}.")
    endpoint_ok = bool(endpoint) and (len(endpoint) <= 256 if IS_WINDOWS else len(os.fsencode(endpoint)) <= 100)
    facts["endpoint"] = {"path": endpoint, "fits_platform_limit": endpoint_ok, "limit": 256 if IS_WINDOWS else 100}
    if endpoint:
        kind, length, unit = ("pipe", len(endpoint), "characters") if IS_WINDOWS else ("socket", len(os.fsencode(endpoint)), "bytes")
        add("ok" if endpoint_ok else "error", "endpoint_ok" if endpoint_ok else "endpoint_too_long",
            f"The {kind} path {endpoint} {'fits the limit' if endpoint_ok else 'is too long'} ({length}/{facts['endpoint']['limit']} {unit}).")
    facts["fingerprint"] = config.fingerprint if config else None
    facts["fingerprint_inputs"] = config.fingerprint_inputs if config else None
    if probe_directory is not None and endpoint_ok:
        try:
            await ipc_probe(probe_directory)
            add("ok", "ipc_available", "The server can create a local pipe." if IS_WINDOWS else "The server can create a local socket.")
        except (OSError, TimeoutError) as exc:
            facts["ipc_error"] = os_error(exc)
            add("warning", "ipc_unavailable", "The local connection check timed out." if isinstance(exc, TimeoutError)
                else f"Local {'pipes' if IS_WINDOWS else 'sockets'} are unavailable here: {facts['ipc_error']['message']}.")
    else:
        reason = "the connection path is too long" if endpoint and not endpoint_ok else "the connection path is unavailable" if not endpoint else "a private temporary folder is unavailable"
        add("info", "ipc_unchecked", f"Local connections were not checked because {reason}.")
    projects = None
    if safe_state:
        projects = database_health(state, add, facts)
    elif state_missing:
        projects = []
        add("info", "database_absent", "There is no database yet.")
    else:
        add("info", "database_unchecked", "The database was not checked because the state folder cannot be used.")
    toolchains = data.get("toolchain_paths", [])
    facts["toolchains"] = []
    if isinstance(toolchains, list):
        for value in toolchains:
            valid = isinstance(value, str) and Path(value).expanduser().is_absolute()
            try:
                item = Path(value).expanduser().resolve() if valid else None
                readable = item is not None and item.exists() and os.access(item, os.R_OK | (os.X_OK if item.is_dir() else 0))
            except OSError:
                item, readable = None, False
            facts["toolchains"].append({"path": str(item) if item else None, "exists": item.exists() if item else False, "readable": readable})
            add("ok" if readable else "error", "toolchain_readable" if readable else "toolchain_unreadable",
                f"The tools folder {item} is readable." if readable else f"The tools folder {item} is missing or cannot be read." if item else "A tools folder setting is not a full path.")
    health = None
    if start_broker and config and endpoint_ok and (safe_state or state_missing):
        missing_before_start = state_missing
        try:
            await asyncio.wait_for(ensure_broker(config), 20)
        except (BridgeError, OSError, ValueError, TimeoutError):
            add("error", "broker_start_failed", "The background server could not be started.")
        if missing_before_start and state.exists():
            state_missing = False
            findings[:] = [finding for finding in findings if finding["code"] not in ("state_missing", "database_unchecked", "database_absent")]
            try:
                facts["state"], safe_state = inspect_state(state)
                facts["state"]["created_by_explicit_start"] = True
                add("ok" if safe_state else "error", "state_ok" if safe_state else "state_unsafe",
                    f"The state folder {state} was created and is private to your account." if safe_state else f"The state folder {state} was created, but it is not private to your account.")
                if safe_state:
                    projects = database_health(state, add, facts)
                else:
                    add("info", "database_unchecked", "The database was not checked because the state folder cannot be used.")
            except (BridgeError, OSError):
                safe_state = False
                add("error", "state_unreadable", f"The state folder {state} was created, but its access could not be checked.")
                add("info", "database_unchecked", "The database was not checked because the state folder cannot be used.")
    if endpoint_ok and safe_state:
        try:
            health = await read_health(probe_config)
            facts["broker"] = {key: health[key] for key in ("pid", "bridge_version", "schema_version", "backend_alive", "backend_generation") if key in health}
            add("ok", "broker_healthy", "The background server answered.")
            compatibility = health.get("compatibility")
            if isinstance(compatibility, dict):
                try:
                    parse_codex_version(compatibility.get("version"))
                    facts["broker"]["codex_compatibility"] = codex_compatibility(compatibility["version"])
                    local = facts["codex_compatibility"]["version"]
                    if local is not None and local != compatibility["version"]:
                        add("warning", "broker_codex_version_mismatch", f"The background server uses Codex {parse_codex_version(compatibility['version']).version}, but this MCP client found Codex {parse_codex_version(local).version}.")
                except ValueError:
                    pass
            inputs = health.get("fingerprint_inputs")
            if config and isinstance(inputs, dict):
                different = [key for key, value in config.fingerprint_inputs.items() if inputs.get(key) != value]
                if different:
                    facts["broker"]["different_fingerprint_inputs"] = different
                    labels = {"bridge_version": "server version", "config_path": "settings file", "state_dir": "state folder",
                        "codex": "Codex program", "max_tasks": "max_tasks", "approval_relay": "approval_relay",
                        "allow_external_tools": "allow_external_tools", "toolchain_paths": "toolchain_paths"}
                    labels["sandbox_process_check"] = "sandbox_process_check"
                    add("error", "broker_identity_mismatch", "A background server started by a different version or with different settings is still running. The differences are "
                        + ", ".join(labels.get(key, "other settings") for key in different) + ".")
            elif not isinstance(inputs, dict):
                add("info", "broker_identity_unchecked", "The background server cannot report how it was started.")
            else:
                add("info", "broker_unchecked", "The background server's settings were not compared because the settings file is invalid.")
            identity = health.get("environment_identity")
            if isinstance(identity, dict):
                different = [key for key, value in facts["environment_identity"].items() if identity.get(key) != value]
                broker_home = identity.get("codex_home")
                if isinstance(broker_home, str) and Path(broker_home).is_absolute():
                    facts["broker"]["codex_home"] = os.path.normcase(os.path.realpath(broker_home))
                if different:
                    facts["broker"]["different_environment_inputs"] = different
                    labels = list(dict.fromkeys("CODEX_HOME" if key == "codex_home" else "Codex profile" for key in different))
                    add("warning", "broker_environment_mismatch", "The background server uses a different CODEX_HOME or Codex profile. The differences are " + ", ".join(labels) + ".")
        except FileNotFoundError:
            held = lock_status(state / "broker.lock") == "held"
            add("warning" if held else "info", "broker_unreachable" if held else "broker_absent",
                "The background server's lock is in use, but its connection path is missing." if held else "No background server is running.")
        except (OSError, BridgeError, ValueError, TimeoutError) as exc:
            lock = lock_status(state / "broker.lock")
            stale = not IS_WINDOWS and probe_config.socket.exists() and stat.S_ISSOCK(probe_config.socket.lstat().st_mode) and lock in ("free", "absent")
            known = getattr(exc, "code", None)
            if known in ("broker_identity_mismatch", "broker_identity_unchecked"):
                facts["broker"] = {"reachable": True, "identity_checked": False}
                add("error" if known == "broker_identity_mismatch" else "warning", known,
                    "A background server started by a different version or with different settings is still running." if known == "broker_identity_mismatch"
                    else "The background server answered, but it cannot report how it was started.")
                facts["broker_error"] = {"code": known}
            else:
                add("warning", "broker_stale_socket" if stale else "broker_unreachable",
                    f"A socket file was left at {probe_config.socket}, but no background server answered." if stale else "The background server did not answer.")
                facts["broker_error"] = os_error(exc)
        lock = facts["broker_lock"] = lock_status(state / "broker.lock")
        if lock == "free" and not health:
            add("info", "broker_stale_lock", "The background server's lock file exists, but it is not in use.")
    else:
        if state_missing:
            add("info", "broker_absent", "No background server is running.")
        else:
            reason = "the state folder cannot be used" if not safe_state else "the connection path is too long" if endpoint else "the connection path is unavailable"
            add("info", "broker_unchecked", f"The background server was not checked because {reason}.")
    sandbox = procscan.scan_for(probe_config, projects or [])
    broker_running = True if health or facts.get("broker", {}).get("reachable") or facts.get("broker_lock") == "held" else (
        False if state_missing or facts.get("broker_lock") in ("free", "absent") else None)
    facts["sandbox_process_check"] = {**sandbox, "enabled": probe_config.sandbox_process_check,
        "projects_known": projects is not None, "projects_checked": len(projects or []), "broker_running": broker_running}
    if not sandbox["available"]:
        code = {"not_linux": "process_check_not_linux", "disabled": "process_check_disabled", "proc_unreadable": "process_check_unreadable"}[sandbox["reason"]]
        add("info", code, procscan.describe(sandbox))
    elif projects is None:
        add("info", "process_check_unchecked", "The Linux process check is available, but saved project folders could not be read.")
    else:
        message = "The Linux process check is available."
        if not sandbox["processes"]:
            message += f" No Codex sandbox processes were found for {len(projects)} saved project folder{'s' if len(projects) != 1 else ''}."
        add("info", "process_check_available", message)
        for project in dict.fromkeys(item["project"] for item in sandbox["processes"]):
            found = {**sandbox, "processes": [item for item in sandbox["processes"] if item["project"] == project]}
            message = f"Project {project}: " + procscan.describe(found)
            if broker_running is False:
                add("warning", "sandbox_processes_untracked", message + " No background server is running.")
            else:
                add("info", "sandbox_processes_running", message + (" They may belong to current work in the background server."
                    if broker_running else " The background server could not be checked; these processes may belong to current work."))
    if config and health and (health.get("backend_alive") is True or start_broker) and not facts.get("broker", {}).get("different_fingerprint_inputs"):
        try:
            status = await read_status(config, timeout=40)
            signed_in = status.get("signed_in")
            facts["unavailable_optional_methods"] = [method for method in status.get("unavailable_optional_methods", []) if method in NATIVE_METHODS]
            facts["ingress_backlog"] = {key: value for key, value in status.get("ingress_backlog", {}).items()
                if key in ("events", "bytes", "oldest_age_seconds", "dropped_events") and type(value) in (int, float)}
            facts["signed_in"] = signed_in if type(signed_in) is bool else None
            add("ok" if signed_in is True else "warning" if signed_in is False else "info", "signin_ok" if signed_in is True else "signin_missing" if signed_in is False else "signin_unchecked", "Codex reports that you are signed in." if signed_in else "Codex reports that you are not signed in." if signed_in is False else "Codex did not report whether you are signed in.")
        except (BridgeError, OSError, ValueError, TimeoutError):
            add("warning", "signin_unchecked", "Codex did not answer the sign-in check.")
    else:
        reason = "the settings file is invalid" if config is None else "no background server connection is available" if not health else (
            "the background server uses different settings" if facts.get("broker", {}).get("different_fingerprint_inputs") else "Codex is not running")
        add("info", "signin_unchecked", f"Sign-in was not checked because {reason}.")
    facts["native_methods"] = dict(NATIVE_METHODS)
    add("info", "native_contract", "The server checks each task's folder and permissions before it runs. This check does not start a task.")
    if schema and resolved:
        await schema_probe(resolved, add, facts)
    else:
        add("info", "schema_unchecked", "The Codex message format was not checked because --schema was not given." if not schema else "No Codex program was available to check its message format.")
    worst = max((LEVEL[item["severity"]] for item in findings), default=0)
    return {"format": 1, "bridge_version": __version__, "summary": "error" if worst == 2 else "warning" if worst else "ok",
            "exit_code": worst, "facts": facts, "findings": findings}


def render(report):
    facts = report["facts"]
    problems = sum(item["severity"] == "error" for item in report["findings"])
    warnings = sum(item["severity"] == "warning" for item in report["findings"])
    summary = (f"{problems} problem{'s' if problems != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}" if problems else
        f"{warnings} warning{'s' if warnings != 1 else ''}" if warnings else "everything looks fine")
    lines = [f"Codex MCP doctor {report['bridge_version']}: {summary}",
        "This check uses no model. It started the background server and Codex because of --start-broker." if facts.get("probes", {}).get("start_broker")
        else "This check uses no model and starts nothing."]
    for heading, severity in (("Problems", "error"), ("Warnings", "warning"), ("Notes", "info"), ("Passed", "ok")):
        entries = [item for item in report["findings"] if item["severity"] == severity]
        if entries:
            lines.extend(("", heading))
            for item in entries:
                lines.append(item["message"])
                if severity != "ok" and not item["next_step"].startswith("Nothing to do."):
                    lines.append("  What to do: " + item["next_step"])
    lines.extend(("", "Details"))
    def detail(label, value):
        if value is not None and value != "":
            lines.append(f"{label}: {value}")
    detail("Settings file", facts.get("config_path") or "none")
    detail("State folder", facts.get("state_dir"))
    detail("Pipe path" if IS_WINDOWS else "Socket path", facts.get("endpoint", {}).get("path"))
    executable = facts.get("executable", {})
    detail("Codex setting (codex_binary)", repr(executable["configured"]) if executable.get("configured") is not None else None)
    detail("Codex program", executable.get("resolved"))
    if executable.get("lookup") == "absolute":
        detail("Found through", "full path")
    elif executable.get("lookup") == "absolute_PATH" and executable.get("path_directory"):
        detail("Found through", "PATH in " + executable["path_directory"])
    detail("CODEX_HOME", facts.get("environment_identity", {}).get("codex_home"))
    detail("Minimum Codex version", facts.get("minimum_codex_version"))
    detail("Tested Codex versions", ", ".join(facts.get("tested_codex_versions", [])))
    detail("MCP versions", ", ".join(facts.get("supported_mcp_revisions", [])))
    database = facts.get("database", {})
    version = database.get("schema_version")
    detail("Database layout version", (f"{version} (supported: {database['supported_schema']})" if database.get("supported_schema") is not None else version) if version is not None else None)
    detail("Database size", readable_size(database["bytes"]) if database.get("bytes") is not None else None)
    detail("Database backups", len(database["backups"]) if "backups" in database else None)
    for key, label in (("tasks", "Tasks"), ("requests", "Requests"), ("open_claims", "Open workspace claims"), ("events", "Events")):
        detail(label, database.get("counts", {}).get(key))
    inputs = facts.get("fingerprint_inputs")
    if inputs:
        for key, label in (("max_tasks", "Maximum simultaneous tasks (max_tasks)"),
                ("approval_relay", "Approval relay (approval_relay)"), ("allow_external_tools", "External tools (allow_external_tools)"),
                ("sandbox_process_check", "Check for leftover sandbox processes (sandbox_process_check)")):
            detail(label, json.dumps(inputs[key]) if inputs.get(key) is not None else None)
    return "\n".join(lines)


def validate_report(report):
    """Stdlib check of REPORT_SCHEMA and severity consistency for installed smoke tests."""
    if not isinstance(report, dict) or set(report) != set(REPORT_SCHEMA["required"]):
        raise ValueError("Invalid doctor report fields")
    if (type(report["format"]) is not int or report["format"] != 1 or not isinstance(report["bridge_version"], str)
            or not isinstance(report["facts"], dict) or not isinstance(report["findings"], list)):
        raise ValueError("Invalid doctor report types")
    worst = 0
    for finding in report["findings"]:
        if (not isinstance(finding, dict) or set(finding) != {"severity", "code", "message", "next_step"}
                or any(not isinstance(value, str) for value in finding.values())
                or finding["severity"] not in LEVEL or finding["code"] not in STEPS or not finding["next_step"]):
            raise ValueError("Invalid doctor finding")
        worst = max(worst, LEVEL[finding["severity"]])
    if (type(report["exit_code"]) is not int or report["exit_code"] != worst
            or report["summary"] != ("error" if worst == 2 else "warning" if worst else "ok")):
        raise ValueError("Invalid doctor severity summary")
