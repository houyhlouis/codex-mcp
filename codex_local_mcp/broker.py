"""Private local IPC: Unix sockets or Windows named pipes, never TCP."""

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time

from .security import BridgeError, private_dir, private_file, redact
from .service import Service
from .store import Store
from .tools import dispatch
from .platforms import IS_WINDOWS, lock_file, detached_process_options, unix_peer_uid

MAX_FRAME = 32 * 1024 * 1024


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
    store = Store(config.state_dir)
    service = Service(config, store)
    stopping = asyncio.Event()
    clients = set()

    async def handle_payload(raw):
        current = asyncio.current_task()
        clients.add(current)
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or (request.get("config") != config.fingerprint and request.get("method") != "stop"):
                raise BridgeError("Configuration differs from the running broker; stop it and reconnect")
            method = request.get("method")
            if stopping.is_set() and method != "stop":
                raise BridgeError("Broker is stopping; reconnect after shutdown completes")
            if method == "ping":
                result = {"pid": os.getpid(), "config": config.fingerprint}
            elif method == "stop":
                result = {"stopping": True}
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
            response = {"error": redact(str(exc))}
        finally:
            clients.discard(current)
        return json.dumps(response, ensure_ascii=False).encode("utf-8")

    async def handle(reader, writer):
        try:
            check_peer(writer.get_extra_info("socket"))
            raw = await asyncio.wait_for(reader.readline(), 10)
            result = await handle_payload(raw)
            writer.write(result + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError, TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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
        await service.close()
        for client in list(clients):
            client.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if server:
            await server.wait_closed()
        store.close()
        if server and not IS_WINDOWS:
            config.socket.unlink(missing_ok=True)
        os.close(lock)


async def rpc(config, method, *, name=None, args=None, timeout=90):
    private_dir(config.state_dir)
    payload = json.dumps({"config": config.fingerprint, "method": method, "name": name,
                          "args": args or {}}, ensure_ascii=False).encode("utf-8")
    if IS_WINDOWS:
        from .windows import exchange
        response = json.loads(await exchange(config.endpoint, payload, MAX_FRAME, timeout))
        if "error" in response:
            raise BridgeError(response["error"])
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
            raise BridgeError("Broker disconnected; reconnect and inspect task state before retrying")
        response = json.loads(line)
        if "error" in response:
            raise BridgeError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


async def ensure_broker(config):
    private_dir(config.state_dir)
    try:
        return await rpc(config, "ping", timeout=2)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    # A separate startup lock avoids two clients spawning competing daemons.
    fd = private_file(config.state_dir / "startup.lock", os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                lock_file(fd)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeError("Timed out waiting for broker startup")
                await asyncio.sleep(0.1)
        try:
            return await rpc(config, "ping", timeout=2)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        log = private_file(config.state_dir / "broker.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        try:
            command = [sys.executable, "-m", "codex_local_mcp"]
            if config.path:
                command.extend(["--config", str(config.path)])
            command.append("broker")
            child = subprocess.Popen(command,
                cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
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
        os.close(fd)
