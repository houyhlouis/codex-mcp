"""Only documented app-server RPCs; Codex owns authentication and execution."""

import asyncio
import json
import os
import re
from collections import deque
from pathlib import Path

from . import __version__
from .security import BridgeError, redact
from .platforms import IS_WINDOWS, executable_argv, codex_environment

MAX_FRAME = 16 * 1024 * 1024


class RpcError(BridgeError):
    def __init__(self, error):
        self.error = redact(error)
        super().__init__(json.dumps(self.error))


def toml_inline(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + toml_inline(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(toml_inline(v) for v in value) + "]"
    return json.dumps(value)


def profile_config(config):
    # Distinct names avoid inheriting similarly named user profiles.
    suffix = config.fingerprint[:12]
    profiles = {}
    for mode in ("read", "write"):
        fs = {":minimal": "read", ":workspace_roots": mode,
              # Standalone Codex installs under ~/.codex. Bubblewrap must be
              # able to execute this exact runtime binary, without granting
              # access to the surrounding home/auth/session directories.
              config.codex: "read", str(config.state_dir): "deny"}
        if config.path:
            fs[str(config.path)] = "deny"
        fs.update({str(p): "read" for p in config.toolchain_paths})
        profiles[f"local_mcp_{mode}_{suffix}"] = {"filesystem": fs, "network": {"enabled": False}}
    return profiles


class AppServer:
    def __init__(self, config, on_message, on_exit):
        self.config, self.on_message, self.on_exit = config, on_message, on_exit
        self.process = None
        self.pending = {}
        self.next_id = 0
        self.reader_task = self.stderr_task = None
        self.event_task = None
        self.events = asyncio.Queue()
        self.stderr = deque(maxlen=20)
        self.write_lock = asyncio.Lock()
        self.version = None
        self.generation = 0
        self.closed = False

    @property
    def alive(self):
        return self.process is not None and self.process.returncode is None and not self.closed

    def profile(self, access):
        return f"local_mcp_{access}_{self.config.fingerprint[:12]}"

    async def start(self):
        version = await asyncio.create_subprocess_exec(*executable_argv(self.config.codex), "--version",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(version.communicate(), 10)
        except TimeoutError:
            version.kill()
            await version.wait()
            raise BridgeError("Codex version check timed out")
        self.version = out.decode().strip()
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", self.version)
        if not match or tuple(map(int, match.groups())) < (0, 167, 0):
            raise BridgeError("Codex 0.167.0 or newer is required for the verified permission-profile protocol")
        overrides = {"permissions": profile_config(self.config),
                     "default_permissions": self.profile("read"),
                     "shell_environment_policy.inherit": "core",
                     "shell_environment_policy.ignore_default_excludes": False}
        # These restrict optional capabilities; existing policies, requirements,
        # rules, hooks and authentication are still loaded and enforced by Codex.
        if not self.config.allow_external_tools:
            overrides.update({"features.apps": False, "features.plugins": False})
        args = [*executable_argv(self.config.codex), "app-server", "--listen", "stdio://"]
        for key, value in overrides.items():
            args.extend(["-c", key + "=" + toml_inline(value)])
        self.closed = False
        self.generation += 1
        self.process = await asyncio.create_subprocess_exec(*args, cwd=self.config.state_dir, env=codex_environment(self.config.codex),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME)
        self.events = asyncio.Queue()
        self.event_task = asyncio.create_task(self._dispatch_events())
        self.reader_task = asyncio.create_task(self._read())
        self.stderr_task = asyncio.create_task(self._stderr())
        try:
            await self.call("initialize", {"clientInfo": {"name": "codex_local_mcp",
                "title": "Codex Local MCP", "version": __version__},
                "capabilities": {"experimentalApi": True}}, timeout=30)
            await self.send({"method": "initialized", "params": {}})
            if IS_WINDOWS:
                effective = await self.call("config/read", {"includeLayers": False})
                mode = (effective.get("config", {}).get("windows") or {}).get("sandbox")
                if mode == "unelevated":
                    raise BridgeError("The legacy unelevated Windows sandbox cannot enforce denied reads. Select a working native MXC/elevated sandbox in Codex before using the bridge")
        except Exception as exc:
            await self.close()
            raise BridgeError(f"Codex app-server startup failed: {exc}. " + "\n".join(self.stderr)) from exc

    async def send(self, message):
        if not self.alive:
            raise BridgeError("Codex connection is unavailable")
        async with self.write_lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()

    async def call(self, method, params, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise BridgeError(f"Codex {method} response timed out; action outcome may be uncertain. Do not blindly retry") from exc
        finally:
            self.pending.pop(request_id, None)

    async def respond(self, request_id, result=None, error=None):
        await self.send({"id": request_id, "error": error} if error else {"id": request_id, "result": result})

    async def _stderr(self):
        try:
            while line := await self.process.stderr.readline():
                self.stderr.append(redact(line.decode(errors="replace").strip())[:4000])
        except (ValueError, OSError):
            self.stderr.append("Codex stderr stream was interrupted")

    async def _read(self):
        reason = "Codex app-server disconnected"
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid Codex frame")
                if "method" not in message and "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RpcError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self.events.put_nowait(message)
        except (Exception, asyncio.CancelledError) as exc:
            reason = f"Codex stream stopped: {type(exc).__name__}"
        finally:
            self.closed = True
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(BridgeError(reason))
            self.events.put_nowait(None)
            try:
                if self.event_task:
                    await asyncio.gather(self.event_task, return_exceptions=True)
            finally:
                # close() can cancel the reader while callbacks are draining.
                # Always invalidate loaded sessions and pending approvals.
                await self.on_exit(reason)

    async def _dispatch_events(self):
        # Ordered callbacks can query child metadata without deadlocking the
        # RPC response reader. Failure closes the backend, never grants access.
        try:
            while (message := await self.events.get()) is not None:
                await self.on_message(message)
        except Exception as exc:
            self.stderr.append(redact(f"Event processing failed: {exc}"))
            if self.process and self.process.returncode is None:
                self.process.terminate()

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader_task, self.stderr_task, self.event_task):
            if task and task is not asyncio.current_task():
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.closed = True
