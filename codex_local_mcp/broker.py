"""Private local IPC: Unix sockets or Windows named pipes, never TCP."""

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from .security import BridgeError, private_dir, private_file, redact
from .service import Service
from .backend import OptionalCapabilityUnavailable
from .store import Store
from .tools import dispatch, FRAME_LIMIT, IDENTITY_FIELDS
from .platforms import (IS_WINDOWS, lock_file, unlock_file, detached_process_options,
                        process_running, unix_peer_uid)
from . import __version__
from .config import environment_identity

MAX_FRAME = FRAME_LIMIT


def check_peer(sock):
    if IS_WINDOWS:
        raise BridgeError("Windows uses identity-checked named pipes")
    uid = unix_peer_uid(sock)
    if uid != os.getuid():
        raise BridgeError("Broker accepts only its owner's local processes")


async def run_broker(config):
    os.umask(0o077)
    private_dir(config.state_dir)
    lock = private_file(config.state_dir / "broker.lock", os.O_CREAT | os.O_RDWR)
    try:
        lock_file(lock)
    except BlockingIOError:
        os.close(lock)
        raise BridgeError("Broker is already running")
    store = None
    try:
        store = Store(config.state_dir)
        service = Service(config, store)
    except BaseException:
        if store is not None:
            store.close()
        release_lock(lock)
        raise
    stopping = asyncio.Event()
    from .protocol_tasks import ProtocolTasks
    protocol_tasks = ProtocolTasks(service)
    clients = set()
    recovery = asyncio.create_task(service.reconcile_startup())

    async def handle_payload(raw):
        request = {}
        current = asyncio.current_task()
        clients.add(current)
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or (request.get("config") != config.fingerprint and request.get("method") not in ("stop", "health")):
                raise BridgeError("Configuration differs from the running broker; stop it and reconnect", code="broker_identity_mismatch")
            method = request.get("method")
            if stopping.is_set() and method != "stop":
                raise BridgeError("Broker is stopping; reconnect after shutdown completes")
            if method == "ping":
                result = {"pid": os.getpid(), "config": config.fingerprint}
            elif method == "health":
                result = health_snapshot(config, service)
            elif method == "mcp_tasks":
                result = await protocol_tasks.handle(request.get("name"), request.get("args", {}))
            elif method == "stop":
                # The caller uses the PID to wait for the process to exit;
                # Windows keeps broker.log locked until then.
                result = {"stopping": True, "pid": os.getpid()}
                stopping.set()
            elif method == "review":
                # The local CLI and MCP elicitation adapter use this route after
                # human interaction. This is not a security boundary against the
                # same OS account, which already controls all of these processes.
                result = await dispatch(service, "codex_respond", request["args"], human=True)
            elif method == "tool":
                result = await dispatch(service, request["name"], request.get("args", {}))
            else:
                raise BridgeError("Unknown broker operation")
            response = {"result": redact(result)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = {"error": redact(str(exc))[:16384]}
            error = exc if isinstance(exc, BridgeError) else BridgeError(str(exc), code="native_timeout" if isinstance(exc, TimeoutError) else "transport_unavailable" if isinstance(exc, OSError) else "internal_error")
            args = request.get("args") if isinstance(request, dict) else None
            if isinstance(args, dict):
                for key in (*IDENTITY_FIELDS, "operation_id", "pending_request_id"):
                    if args.get(key):
                        error.ids.setdefault(key, args[key])
                if args.get("request_id"):
                    pending = request.get("method") == "review" or request.get("name") in ("codex_respond", "codex_review_request")
                    error.ids.setdefault("pending_request_id" if pending else "operation_id", args["request_id"])
            response["error_data"] = error.as_dict()
        finally:
            clients.discard(current)
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        if len(encoded) + 1 > MAX_FRAME:
            error = BridgeError("Result exceeds the transport frame limit; request a smaller page", code="result_too_large")
            return json.dumps({"error": str(error), "error_data": error.as_dict()}).encode("utf-8")
        return encoded

    async def handle(reader, writer):
        try:
            check_peer(writer.get_extra_info("socket"))
            raw = await asyncio.wait_for(reader.readline(), 10)
            result = await handle_payload(raw)
            writer.write(result + b"\n")
            await writer.drain()
        except (BridgeError, OSError, TimeoutError, ValueError):
            # Rejected peers, oversized frames and vanished clients only end
            # this connection; they must not log unhandled-task tracebacks.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    server = None
    try:
        if IS_WINDOWS:
            from .windows import PipeServer
            server = PipeServer(config.endpoint, handle_payload, MAX_FRAME)
            server.start()
        else:
            if config.socket.exists() or config.socket.is_symlink():
                info = config.socket.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise BridgeError("Refusing to remove an unexpected broker socket path")
                config.socket.unlink()
            server = await asyncio.start_unix_server(handle, config.endpoint, limit=MAX_FRAME)
            os.chmod(config.socket, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            if IS_WINDOWS:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stopping.set))
            else:
                loop.add_signal_handler(sig, stopping.set)
        await stopping.wait()
    finally:
        if server:
            server.close()
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        await service.close()
        for client in list(clients):
            client.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if server:
            await server.wait_closed()
        store.close()
        if server and not IS_WINDOWS:
            config.socket.unlink(missing_ok=True)
        release_lock(lock)


async def rpc(config, method, *, name=None, args=None, timeout=90):
    private_dir(config.state_dir)
    identities = {k: v for k, v in (args or {}).items() if k in (*IDENTITY_FIELDS, "operation_id", "pending_request_id")}
    if (args or {}).get("request_id"):
        identities["pending_request_id" if method == "review" or name in ("codex_respond", "codex_review_request") else "operation_id"] = args["request_id"]
    payload = json.dumps({"config": config.fingerprint, "method": method, "name": name,
                          "args": args or {}}, ensure_ascii=False).encode("utf-8")
    if IS_WINDOWS:
        from .windows import exchange
        try:
            response = json.loads(await exchange(config.endpoint, payload, MAX_FRAME, timeout))
        except BridgeError as exc:
            raise BridgeError(str(exc), code="transport_unavailable", ids=identities) from exc
        if "error" in response:
            raise response_error(response)
        return response["result"]
    info = config.socket.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("Broker socket is not private or user-owned")
    reader, writer = await asyncio.open_unix_connection(str(config.socket), limit=MAX_FRAME)
    try:
        check_peer(writer.get_extra_info("socket"))
        writer.write(payload + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise BridgeError("Broker disconnected; reconnect and inspect task state before retrying", code="transport_unavailable", ids=identities)
        response = json.loads(line)
        if "error" in response:
            raise response_error(response)
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def health_snapshot(config, service):
    backend = service.backend
    return {"pid": os.getpid(), "bridge_version": __version__, "schema_version": service.store.schema_version,
            "fingerprint": config.fingerprint, "fingerprint_inputs": config.fingerprint_inputs,
            "environment_identity": environment_identity(), "backend_alive": backend.alive,
            "backend_generation": backend.generation,
            "compatibility": dict(backend.compatibility) if backend.version else None}


class HealthUnavailable(BridgeError):
    def __init__(self, code):
        super().__init__(code, code=code)


async def read_health(config, timeout=2):
    """Read-only health request; no directory creation or broker/native startup."""
    payload = json.dumps({"config": config.fingerprint, "method": "health"}).encode()
    return await _read_diagnostic(config, payload, timeout)


async def read_status(config, timeout=40):
    """Existing status tool over verified IPC, without repairing state permissions."""
    payload = json.dumps({"config": config.fingerprint, "method": "tool", "name": "codex_status", "args": {}}).encode()
    return await _read_diagnostic(config, payload, timeout)


async def _read_diagnostic(config, payload, timeout):
    if IS_WINDOWS:
        from .windows import exchange
        raw = await exchange(config.endpoint, payload, 1024 * 1024, timeout)
    else:
        info = config.socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError("Broker socket is not private or user-owned")
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(config.endpoint, limit=1024 * 1024), timeout)
        try:
            check_peer(writer.get_extra_info("socket"))
            writer.write(payload + b"\n")
            await asyncio.wait_for(writer.drain(), timeout)
            raw = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout)
    response = json.loads(raw)
    if not isinstance(response, dict):
        raise BridgeError("Broker health returned an invalid response")
    if "error" in response:
        if response_error(response).code == "broker_identity_mismatch":
            raise HealthUnavailable("broker_identity_mismatch")
        raise HealthUnavailable("broker_identity_unchecked")
    if not isinstance(response.get("result"), dict):
        raise BridgeError("Broker health returned an invalid response")
    return response["result"]


def response_error(response):
    data = response.get("error_data")
    if data is None and isinstance(response.get("error"), dict):
        data = response["error"]
    if (isinstance(data, dict) and data.get("code") == OptionalCapabilityUnavailable.code
            and isinstance(data.get("method"), str)
            and isinstance(data.get("codex_version"), (str, type(None)))):
        error = OptionalCapabilityUnavailable(data["method"], data.get("codex_version"), {})
        error.ids.update(data.get("ids", {}))
        error.retryable = data.get("retryable", error.retryable)
        error.uncertain = data.get("uncertain", error.uncertain)
        error.next_steps = data.get("next_steps", error.next_steps)
        return error
    if isinstance(data, dict) and data.get("code") in BridgeError.CODES and {"message", "retryable", "uncertain", "next_steps"} <= data.keys():
        return BridgeError.from_dict(data)
    message = data.get("message", response.get("error")) if isinstance(data, dict) else response.get("error")
    code = data.get("code") if isinstance(data, dict) else None
    # Only legacy uncoded replies need text recognition. A structured code
    # always takes precedence over broker-supplied prose.
    legacy_identity = data is None and isinstance(message, str) and message.startswith((
        "Configuration differs", "A broker started by a different version or configuration"))
    if code in ("broker_identity_mismatch", "config_mismatch") or legacy_identity:
        return BridgeError(str(message), code="broker_identity_mismatch")
    return BridgeError(str(message))


def release_lock(fd):
    try:
        unlock_file(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def broker_lock_free(config):
    """True when no broker holds the state directory's lock."""
    try:
        fd = private_file(config.state_dir / "broker.lock", os.O_RDWR)
    except (FileNotFoundError, BridgeError):
        return True
    try:
        try:
            lock_file(fd)
        except (BlockingIOError, PermissionError):
            return False
        unlock_file(fd)
        return True
    finally:
        os.close(fd)


async def stop_broker(config, timeout=15):
    """Stop the broker and wait until it has actually shut down.

    The broker releases its lock at the end of shutdown, but the process can
    live a little longer. On Windows its open files (for example broker.log)
    cannot be deleted or replaced until it exits, so wait for the process too.
    """
    try:
        result = await rpc(config, "stop")
    except (FileNotFoundError, ConnectionRefusedError):
        return {"stopping": False, "message": "Broker is not running"}
    pid = result.get("pid")
    deadline = time.monotonic() + timeout
    while not broker_lock_free(config) or (IS_WINDOWS and pid and process_running(pid)):
        if time.monotonic() >= deadline:
            raise BridgeError("Broker accepted the stop request but is still shutting down; try again shortly")
        await asyncio.sleep(0.05)
    return {"stopped": True, "pid": pid}


async def ping_existing(config):
    try:
        return await rpc(config, "ping", timeout=2)
    except BridgeError as exc:
        if exc.code == "broker_identity_mismatch":
            raise BridgeError("A broker started by a different version or configuration is still running for this "
                              "state directory. Stop it with `python server.py stop` (add the same --config option "
                              "if you use one), then retry", code="broker_identity_mismatch") from exc
        raise


async def ensure_broker(config):
    private_dir(config.state_dir)
    try:
        return await ping_existing(config)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    # A separate startup lock avoids two clients spawning competing daemons.
    fd = private_file(config.state_dir / "startup.lock", os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                lock_file(fd)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeError("Timed out waiting for broker startup")
                await asyncio.sleep(0.1)
        try:
            return await ping_existing(config)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        log = private_file(config.state_dir / "broker.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        try:
            command = [sys.executable, "-m", "codex_local_mcp"]
            if config.path:
                command.extend(["--config", str(config.path)])
            command.append("broker")
            child = subprocess.Popen(command,
                cwd=str(Path(__file__).resolve().parent.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                close_fds=True, **detached_process_options())
        finally:
            os.close(log)
        while time.monotonic() < deadline:
            try:
                result = await rpc(config, "ping", timeout=2)
                # Do not wait for the daemon lifetime. Reap only a child that
                # lost the startup race; a live daemon is intentionally detached.
                child.poll()
                return result
            except (FileNotFoundError, ConnectionRefusedError):
                if child.poll() is not None:
                    raise BridgeError("Broker failed to start; inspect the private broker.log")
                await asyncio.sleep(0.1)
        raise BridgeError("Broker did not become ready; inspect the private broker.log")
    finally:
        if locked:
            release_lock(fd)
        else:
            os.close(fd)
