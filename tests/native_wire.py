"""Record real fake-backend subprocess pipes without touching runner stdio."""

import asyncio
import copy
import json
import re
import tempfile
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.backend import AppServer, RpcError
from codex_local_mcp.config import load_config
from codex_local_mcp.store import Store
from tests.helpers import ControlledService, install_fake_codex, normalise_bridge_version, temp_root, write_config

OBSERVATIONS = frozenset({"thread/goal/get", "thread/queue/list", "thread/backgroundTerminals/list",
    "thread/read", "thread/turns/list", "thread/items/list", "thread/list"})


class RecordingWriter:
    def __init__(self, writer, backend):
        self.writer, self.backend = writer, backend

    def write(self, data):
        self.writer.write(data)
        message = json.loads(data)
        self.backend.record("outbound", message)
        if "method" in message and "id" in message:
            self.backend.request_methods[message["id"]] = message["method"]
            if self.backend.checkpoint_context.get():
                self.backend.checkpoint_ids.add(message["id"])

    def __getattr__(self, name):
        return getattr(self.writer, name)


class RecordingReader:
    def __init__(self, reader, backend):
        self.reader, self.backend = reader, backend

    async def readline(self):
        line = await self.reader.readline()
        if not line:
            return line
        message = json.loads(line)
        if "method" in message:
            self.backend.validate_inbound(message)
            self.backend.record("inbound", message)
            return line
        # Explicit response faults are used only by negative contract tests.
        method = self.backend.request_methods.get(message.get("id"))
        if method in self.backend.method_errors:
            message = {"id": message["id"], "error": self.backend.method_errors[method]}
        elif method == "thread/start" and "result" in message and self.backend.omit_safety_fields:
            message = copy.deepcopy(message)
            for field in self.backend.omit_safety_fields:
                message["result"].pop(field, None)
        else:
            self.backend.validate_inbound(message)
            if method not in OBSERVATIONS or message.get("id") in self.backend.checkpoint_ids:
                self.backend.record("inbound", message)
            return line
        self.backend.validate_inbound(message)
        self.backend.record("inbound", message)
        return json.dumps(message).encode() + b"\n"

    def __getattr__(self, name):
        return getattr(self.reader, name)


class RecordingProcess:
    def __init__(self, process, backend):
        self.process = process
        self.stdin = RecordingWriter(process.stdin, backend)
        self.stdout = RecordingReader(process.stdout, backend)

    def __getattr__(self, name):
        return getattr(self.process, name)


class VersionProcess:
    def __init__(self, process, backend):
        self.process, self.backend = process, backend

    async def communicate(self):
        out, err = await self.process.communicate()
        if self.backend.reported_version is not None:
            out = self.backend.reported_version.encode()
        return out, err

    def __getattr__(self, name):
        return getattr(self.process, name)


class RecordingAppServer(AppServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frames = []
        self.flow = "initialize"
        self.reported_version = None
        self.request_methods = {}
        self.method_errors = {}
        self.omit_safety_fields = set()
        self.inbound_validator = None
        self.checkpoint_context = ContextVar("native_wire_checkpoint", default=False)
        self.checkpoint_ids = set()

    def validate_inbound(self, message):
        if self.inbound_validator:
            if "method" in message:
                key = "inbound:" + ("request:" if "id" in message else "notification:") + message["method"]
            else:
                key = "inbound:" + ("error:" if "error" in message else "response:") + self.request_methods[message["id"]]
            self.inbound_validator(key, message)

    async def checkpoint(self, method, params):
        token = self.checkpoint_context.set(True)
        try:
            return await self.call(method, params)
        finally:
            self.checkpoint_context.reset(token)

    def record(self, direction, message):
        self.frames.append({"flow": self.flow, "direction": direction,
                            "message": json.loads(json.dumps(message))})

    async def start(self):
        create = asyncio.create_subprocess_exec

        async def recorded(*args, **kwargs):
            process = await create(*args, **kwargs)
            return VersionProcess(process, self) if "--version" in args else RecordingProcess(process, self)

        with patch("codex_local_mcp.backend.asyncio.create_subprocess_exec", recorded):
            try:
                await asyncio.wait_for(super().start(), 15)
            except BaseException:
                await self.close()
                raise

    async def call(self, method, params, timeout=5, *, before_send=None):
        return await super().call(method, params, timeout=min(timeout, 5), before_send=before_send)

    async def close(self):
        await asyncio.wait_for(super().close(), 15)


async def wait_for(service, predicate):
    async with service.changed:
        await asyncio.wait_for(service.changed.wait_for(predicate), 5)


async def completed(service, task_id, status="completed"):
    await wait_for(service, lambda: service.task(task_id)["status"] == status)
    # Capture metadata at explicit flow boundaries, never from a timer or an
    # unrelated control's callback carrying a different flow label.
    await service.reconcile_claims(task_id)
    await settled(service)


async def settled(service):
    await asyncio.wait_for(service.reconciliation_idle(), 10)


def normalise_frames(frames, base):
    identifiers = {}

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            value = value.replace(str(base), "/fixture")
            value = re.sub(r"local_mcp_(read|write)_[a-f0-9]{12}", r"local_mcp_\1_fixture", value)
            def identifier(match):
                return identifiers.setdefault(match[0], f"00000000-0000-4000-8000-{len(identifiers) + 1:012d}")
            return re.sub(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identifier, value)
        return value

    # Wire fixtures pin shapes and values, not scheduler-dependent polling
    # frequency, RPC numbering, or the interleaving of independent callbacks.
    observations = OBSERVATIONS
    normalised, requests, replies, pending = [], set(), set(), {}
    for frame in normalise_bridge_version(replace(frames)):
        message = frame["message"]
        if frame["direction"] == "outbound" and "method" in message and "id" in message:
            pending[message["id"]] = (frame["flow"], message["method"])
            message["id"] = 0
            if message["method"] in observations:
                key = json.dumps(frame, sort_keys=True)
                if key in requests:
                    continue
                requests.add(key)
        elif frame["direction"] == "inbound" and "method" not in message:
            frame["flow"], frame["reply_to"] = pending[message["id"]]
            if frame["reply_to"] == "thread/loaded/list" and "result" in message:
                message["result"]["data"].sort()
            message["id"] = 0
            key = json.dumps(frame, sort_keys=True)
            if key in replies:
                continue
            replies.add(key)
        normalised.append(frame)
    normalised.sort(key=lambda frame: (frame["flow"],
        0 if frame["direction"] == "inbound" else 1, json.dumps(frame["message"], sort_keys=True)))
    request_id = 0
    for frame in normalised:
        if frame["direction"] == "outbound" and "method" in frame["message"] and "id" in frame["message"]:
            request_id += 1
            frame["message"]["id"] = request_id
    return normalised


async def capture_native_wire(inbound_validator=None):
    return await asyncio.wait_for(_capture_native_wire(inbound_validator), 60)


async def _capture_native_wire(inbound_validator=None):
    with tempfile.TemporaryDirectory(prefix="wire-", dir=temp_root()) as directory:
        base = Path(directory).resolve()
        binary = install_fake_codex(base)
        state = base / "state"
        state.mkdir(mode=0o700)
        config_path = base / "config.toml"
        write_config(config_path, state, binary)
        config = load_config(str(config_path))
        store = Store(state)
        service = ControlledService(config, store, backend_factory=RecordingAppServer)
        backend = service.backend
        backend.inbound_validator = inbound_validator
        try:
            project = base / "project"
            service.projects("create", str(project))
            task = await service.start("echo", "wire-start", workdir=str(project), approval_mode="ask")
            task_id = task["id"]
            await completed(service, task_id)
            for prompt, response in (("approval", {"decision": "decline"}),
                                     ("parent", {"text": "Run offline checks"}),
                                     ("question", {"answers": {"choice": {"answers": ["offline"]}}}),
                                     ("permission", {"permissions": {}, "scope": "turn"}),
                                     ("file_approval", {"decision": "decline"}),
                                     ("fail", None),
                                     ("unknown", None)):
                backend.flow = prompt
                await service.send(task_id, prompt)
                if response is not None:
                    await wait_for(service, lambda: bool(service.requests(task_id)))
                    request = service.requests(task_id)[0]
                    await service.respond(request["id"], response, human=True)
                await completed(service, task_id, "failed" if prompt == "fail" else "completed")

            backend.flow = "native-children"
            await service.send(task_id, "native_children")
            await wait_for(service, lambda: bool(service.requests(task_id)))
            await service.respond(service.requests(task_id)[0]["id"], {"decision": "decline"}, human=True)
            await completed(service, task_id)

            backend.flow = "interrupt-and-recovery"
            await service.send(task_id, "hold")
            await wait_for(service, lambda: service.task(task_id)["status"] == "running")
            active = service.task(task_id)
            await service.settings(task_id, scope="current", expected_turn_id=active["turn_id"], effort="high")
            await service.send(task_id, "steer", mode="steer", expected_turn_id=active["turn_id"])
            await service.interrupt(task_id, active["turn_id"])
            await completed(service, task_id, "interrupted")
            await backend.close()
            await service.ensure_backend()
            stored = await backend.checkpoint("thread/read", {"threadId": active["thread_id"], "includeTurns": False})
            if stored["thread"]["status"] != {"type": "notLoaded"} or stored["thread"]["turns"]:
                raise RuntimeError("Stored thread was unexpectedly loaded during the offline capture")
            try:
                await backend.checkpoint("thread/backgroundTerminals/list", {"threadId": active["thread_id"], "cursor": None, "limit": 50})
            except RpcError as exc:
                if exc.error.get("code") != -32600:
                    raise
            else:
                raise RuntimeError("Unloaded terminal listing did not reproduce the native refusal")
            await service.resume(task_id)

            backend.flow = "missing-conversation"
            for method in ("thread/read", "thread/resume"):
                try:
                    await backend.checkpoint(method, {"threadId": "missing-thread"})
                except RpcError as exc:
                    if exc.error.get("code") != -32600:
                        raise
                else:
                    raise RuntimeError("An unknown native conversation was accepted")
            await backend.checkpoint("thread/loaded/list", {"cursor": None, "limit": 200})

            backend.flow = "crash-and-stored-recovery"
            await service.send(task_id, "hold")
            await wait_for(service, lambda: service.task(task_id)["status"] == "running")
            backend.process.kill()
            await asyncio.wait_for(backend.reader_task, 5)
            await service.ensure_backend()
            history = await backend.checkpoint("thread/turns/list", {"threadId": active["thread_id"],
                "limit": 1, "sortDirection": "desc", "itemsView": "summary"})
            if history["data"][0]["status"] != "interrupted":
                raise RuntimeError("Crash history was not interrupted before resume")
            listed = await backend.checkpoint("thread/loaded/list", {"cursor": None, "limit": 200})
            if listed["data"]:
                raise RuntimeError("A stored conversation was loaded by reconciliation")
            await service.resume(task_id)

            backend.flow = "optional-controls"
            await service.history(task_id)
            await service.history(task_id, kind="items")
            await service.capabilities(task_id)
            await service.skills(task_id)
            await service.usage()
            await service.status()
            await service.settings(task_id, scope="next", collaboration_mode="plan", model="small")
            await service.goal(task_id, action="set", objective="Offline fixture", status="paused", user_requested=True)
            await service.goal(task_id)
            await service.goal(task_id, action="clear", user_requested=True)
            await service.terminals(task_id)
            await service.terminals(task_id, action="terminate", process_id="fixture")
            await service.terminals(task_id, action="clean")
            queued = await service.queue(task_id, action="add", prompt="queued")
            queued_id = queued["queuedSubmission"]["id"]
            await service.queue(task_id, action="list")
            await service.queue(task_id, action="update", queued_id=queued_id, prompt="updated")
            await service.queue(task_id, action="reorder", ordered_ids=[queued_id])
            await service.queue(task_id, action="delete", queued_id=queued_id)
            queued = await service.queue(task_id, action="add", prompt="queued again")
            await service.queue(task_id, action="start", queued_id=queued["queuedSubmission"]["id"])
            await completed(service, task_id)
            await service.review(task_id)
            await completed(service, task_id)
            await service.compact(task_id)
            await completed(service, task_id)
            fork = await service.fork(task_id, name="Fixture fork")
            await service.archive(fork["id"])
            await service.archive(fork["id"], action="unarchive")
            await service.delete(fork["id"], confirm_thread_id=fork["thread_id"])
            turns = await service.history(task_id)
            await service.revert(task_id, turns["data"][-1]["id"], confirm_history_only=True)
            await settled(service)
            backend.flow = "observation-checkpoints"
            for method in sorted(OBSERVATIONS):
                params = {} if method == "thread/list" else {"threadId": service.task(task_id)["thread_id"]}
                await backend.checkpoint(method, params)
            await settled(service)
            return {"format": 1, "codex_version": backend.version,
                    "source": "Raw AppServer subprocess pipes using the ordinary tests.fake_codex",
                    "normalisation": "Temporary paths, UUIDs, profile fingerprints and bridge version; canonical flow order; observation replies pinned only at explicit settled checkpoints",
                    "messages": normalise_frames(backend.frames, base)}
        finally:
            await service.close()
            store.close()
