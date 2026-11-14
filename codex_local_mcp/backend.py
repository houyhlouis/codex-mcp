"""Only documented app-server RPCs; Codex owns authentication and execution."""

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .security import BridgeError, redact
from .platforms import IS_WINDOWS, executable_argv, codex_environment
from .contract import NATIVE_METHODS, OPTIONAL, REPLACEABLE_PROGRESS, codex_compatibility, method_unavailable

MAX_FRAME = 16 * 1024 * 1024
EVENT_MAX_COUNT = 1024
EVENT_MAX_BYTES = 4 * 1024 * 1024
EVENT_GAP_METHOD = "bridge/event_gap"


class EventBudgetExceeded(BridgeError):
    code = "native_protocol_incompatible"


class NativeCompatibilityError(BridgeError):
    code = "native_protocol_incompatible"

    def __init__(self, method, version, reason):
        super().__init__(f"{self.code}: {method or '<invalid method>'} in {version or 'this Codex build'}: {reason}. "
                         "Inspect codex_status and codex_history before resuming", details={"method": method, "codex_version": version})


@dataclass
class QueuedEvent:
    message: dict
    size: int
    queued_at: float
    gap: bool = False


class EventBuffer:
    """Bound queued wire bytes without ever blocking the response reader."""
    def __init__(self, max_count=EVENT_MAX_COUNT, max_bytes=EVENT_MAX_BYTES):
        if type(max_count) is not int or type(max_bytes) is not int or min(max_count, max_bytes) < 1:
            raise ValueError("Event count and byte budgets must be positive integers")
        self.maxsize, self.max_bytes = max_count, max_bytes
        self._items = deque()
        self._bytes = self.dropped_events = 0
        self._ready = asyncio.Event()
        self._closed = False

    def qsize(self):
        return len(self._items)

    @property
    def backlog(self):
        return {"events": self.qsize(), "bytes": self._bytes,
                "oldest_age_seconds": max(0.0, time.monotonic() - self._items[0].queued_at) if self._items else 0.0,
                "dropped_events": self.dropped_events}

    @staticmethod
    def _size(message):
        return len(json.dumps(message, ensure_ascii=False).encode("utf-8")) + 1

    @staticmethod
    def _progress(message):
        params = message.get("params")
        return ("id" not in message and message.get("method") in REPLACEABLE_PROGRESS
                and isinstance(params, dict) and isinstance(params.get("threadId"), str)
                and isinstance(params.get("turnId"), (str, type(None))))

    @staticmethod
    def _context(message):
        params = message["params"]
        return params.get("threadId"), params.get("turnId")

    def put_nowait(self, message, size=None):
        if self._closed:
            raise EventBudgetExceeded("Codex event ingress is closed; event_gap_possible")
        entry = QueuedEvent(message, self._size(message) if size is None else size, time.monotonic())
        if self.qsize() < self.maxsize and self._bytes + entry.size <= self.max_bytes:
            self._items.append(entry)
            self._bytes += entry.size
        else:
            compacted = deque()
            for candidate in (*self._items, entry):
                value = candidate.message
                progress = self._progress(value)
                gap = candidate.gap
                if progress or gap:
                    count = 1 if progress else value["params"]["dropped_events"]
                    if progress:
                        self.dropped_events += 1
                    if (compacted and compacted[-1].gap
                            and self._context(compacted[-1].message) == self._context(value)):
                        marker = compacted[-1]
                        marker.message["params"]["dropped_events"] += count
                        marker.size = self._size(marker.message)
                        continue
                    thread_id, turn_id = self._context(value)
                    marker = {"method": EVENT_GAP_METHOD, "params": {
                        "threadId": thread_id, "turnId": turn_id, "reason": "event_ingress_budget",
                        "dropped_events": count, "event_gap_possible": True}}
                    compacted.append(QueuedEvent(marker, self._size(marker), candidate.queued_at, gap=True))
                else:
                    compacted.append(candidate)
            self._items = compacted
            self._bytes = sum(item.size for item in compacted)
            if self.qsize() > self.maxsize or self._bytes > self.max_bytes:
                self.close()
                raise EventBudgetExceeded("Codex event ingress budget exceeded; event_gap_possible; resume and inspect codex_history")
        self._ready.set()

    async def get(self):
        while not self._items:
            if self._closed:
                return None
            self._ready.clear()
            await self._ready.wait()
        entry = self._items.popleft()
        self._bytes -= entry.size
        return entry.message

    def close(self):
        self._closed = True
        self._items.clear()
        self._bytes = 0
        self._ready.set()


class RpcError(BridgeError):
    code = "native_rejected"
    def __init__(self, error):
        self.error = redact(error)
        super().__init__(json.dumps(self.error), details={"native_code": self.error.get("code") if isinstance(self.error, dict) else None})


class OptionalCapabilityUnavailable(RpcError):
    code = "optional_native_capability_unavailable"
    next_step = "Use another supported control, or install a tested Codex version and restart the broker"

    def __init__(self, method, version, error):
        self.method, self.version, self.error = method, version, redact(error)
        BridgeError.__init__(self, f"{self.code}: {method} is unavailable in {version or 'this Codex build'}. {self.next_step}")

    def as_dict(self):
        return {**super().as_dict(), "method": self.method, "codex_version": self.version,
                "next_step": self.next_step}


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
    def __init__(self, config, on_message, on_exit, *, event_max_count=EVENT_MAX_COUNT, event_max_bytes=EVENT_MAX_BYTES):
        self.config, self.on_message, self.on_exit = config, on_message, on_exit
        self.process = None
        self.pending = {}
        self.next_id = 0
        self.reader_task = self.stderr_task = None
        self.event_task = None
        self.event_max_count, self.event_max_bytes = event_max_count, event_max_bytes
        self.events = EventBuffer(event_max_count, event_max_bytes)
        self._event_in_flight = False
        self.event_failure = None
        self.stderr = deque(maxlen=20)
        self.write_lock = asyncio.Lock()
        self.version = None
        self.compatibility = {"version": None, "status": "unknown", "reason": "Codex version has not been checked"}
        self.unavailable_methods = set()
        self.generation = 0
        self.closed = False

    @property
    def alive(self):
        return self.process is not None and self.process.returncode is None and not self.closed

    def profile(self, access):
        return f"local_mcp_{access}_{self.config.fingerprint[:12]}"

    @property
    def backlog(self):
        return self.events.backlog

    async def start(self):
        version = await asyncio.create_subprocess_exec(*executable_argv(self.config.codex), "--version",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(version.communicate(), 10)
        except TimeoutError:
            version.kill()
            await version.wait()
            raise BridgeError("Codex version check timed out", code='native_timeout')
        except BaseException:
            if version.returncode is None:
                try:
                    version.kill()
                except ProcessLookupError:
                    pass
            await version.wait()
            raise
        self.version = out.decode().strip()
        self.compatibility = codex_compatibility(self.version)
        if self.compatibility["status"] == "unsupported":
            raise BridgeError(self.compatibility["reason"], code='native_protocol_incompatible')
        self.unavailable_methods.clear()
        self.event_failure = None
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
        self.events = EventBuffer(self.event_max_count, self.event_max_bytes)
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
                    raise BridgeError("The legacy unelevated Windows sandbox cannot enforce denied reads. Select a working native MXC/elevated sandbox in Codex before using the bridge", code='native_protocol_incompatible')
        except Exception as exc:
            await self.close()
            raise BridgeError(f"Codex app-server startup failed: {exc}. " + "\n".join(self.stderr), code='transport_unavailable') from exc

    async def send(self, message, *, before_send=None):
        if not self.alive:
            raise BridgeError("Codex connection is unavailable", code='transport_unavailable')
        process, generation = self.process, self.generation
        async with self.write_lock:
            if not self.alive or self.process is not process or self.generation != generation:
                raise BridgeError("Codex connection changed before native write; resume before retrying", code='transport_unavailable')
            if before_send is not None:
                before_send()
            data = (json.dumps(message, ensure_ascii=False) + "\n").encode()
            observer = getattr(self, "on_write", None)
            if observer is not None:
                observer()
            process.stdin.write(data)
            await process.stdin.drain()

    async def call(self, method, params, timeout=30, *, before_send=None):
        def check_available():
            if method in self.unavailable_methods:
                raise OptionalCapabilityUnavailable(method, self.version, {})
            if before_send is not None:
                before_send()
        if method in self.unavailable_methods:
            raise OptionalCapabilityUnavailable(method, self.version, {})
        process, generation = self.process, self.generation
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params}, before_send=check_available)
            result = await asyncio.wait_for(future, timeout)
            if self.process is not process or self.generation != generation:
                raise BridgeError("Codex connection changed while awaiting native response; resume before retrying", code='transport_unavailable')
            # A late success cannot undo an unavailable observation from this
            # connection. Reconnecting resets the capability observations.
            return result
        except RpcError as exc:
            if self.process is not process or self.generation != generation:
                raise BridgeError("Codex connection changed while awaiting native response; resume before retrying", code='transport_unavailable') from exc
            if NATIVE_METHODS.get(method) == OPTIONAL and method_unavailable(exc.error):
                self.unavailable_methods.add(method)
                raise OptionalCapabilityUnavailable(method, self.version, exc.error) from exc
            raise
        except TimeoutError as exc:
            raise BridgeError(f"Codex {method} response timed out; action outcome may be uncertain. Do not blindly retry", code='native_timeout') from exc
        finally:
            self.pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                # Disconnect can cancel the callback waiting on this future.
                future.exception()

    async def respond(self, request_id, result=None, error=None, *, before_send=None):
        await self.send({"id": request_id, "error": error} if error else {"id": request_id, "result": result},
                        before_send=before_send)

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
                    raise NativeCompatibilityError("<frame>", self.version, "frame must be an object")
                if "method" not in message and "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RpcError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self.events.put_nowait(message, len(line))
        except EventBudgetExceeded as exc:
            reason = str(exc)
            self.closed = True
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
        except (NativeCompatibilityError, ValueError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, NativeCompatibilityError) else str(
                NativeCompatibilityError("<frame>", self.version, f"invalid native frame ({type(exc).__name__})"))
            self.closed = True
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
        except (Exception, asyncio.CancelledError) as exc:
            reason = f"Codex stream stopped: {type(exc).__name__}"
        finally:
            if self.event_failure is not None:
                reason = self.event_failure
            self.closed = True
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(BridgeError(reason, code='transport_unavailable'))
            if (self.events.qsize() or self._event_in_flight) and "event_gap_possible" not in reason:
                reason += "; event_gap_possible"
            self.events.close()
            try:
                if self.event_task:
                    if not self.event_task.done():
                        self.event_task.cancel()
                    await asyncio.gather(self.event_task, return_exceptions=True)
            finally:
                # Invalidate sessions and approvals even during cancellation.
                await self.on_exit(reason)

    async def _dispatch_events(self):
        # Ordered callbacks can query child metadata without deadlocking the
        # RPC response reader. Failure closes the backend, never grants access.
        try:
            while (message := await self.events.get()) is not None:
                self._event_in_flight = True
                try:
                    await self.on_message(message)
                finally:
                    self._event_in_flight = False
        except Exception as exc:
            self.closed = True
            self.event_failure = redact(str(exc)) if isinstance(exc, NativeCompatibilityError) else "Codex event processing failed; event_gap_possible"
            self.stderr.append(redact(f"Event processing failed: {exc}"))
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 5)
        for task in (self.reader_task, self.stderr_task, self.event_task):
            if task and task is not asyncio.current_task():
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.events.close()
