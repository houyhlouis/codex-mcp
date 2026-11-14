"""Structural fixtures drive only a private, ordinary fake-backed Service."""
import asyncio
import copy
import tempfile
from pathlib import Path

from codex_local_mcp.config import load_config
from codex_local_mcp.incidents import validate_incident
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store
from codex_local_mcp.tools import dispatch
from tests.helpers import ControlledService, install_fake_codex, temp_root, write_config


class ReplayGate:
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()


class IncidentReplay:
    def __init__(self, fixture, *, injections=(), gates=None):
        self.fixture = copy.deepcopy(fixture)
        self.fixture["replay"]["injections"].extend(injections)
        validate_incident(self.fixture)
        self.gates = gates or {}
        self.trace, self.references, self.tasks, self.tokens = [], {}, {}, {}

    def materialize(self, value, key=""):
        if isinstance(value, dict):
            if "$reference" in value:
                return int(value["$reference"].removeprefix("ref-"))
            if "$omitted" in value:
                kind = value["$omitted"]["type"]
                if key in ("cwd", "project", "path", "native_cwd"):
                    return str(self.project)
                return {"string": "omitted", "integer": 0, "number": 0.0, "boolean": False, "null": None, "array": [], "object": {}}[kind]
            return {name: self.materialize(child, name) for name, child in value.items() if not name.startswith("field_")}
        if isinstance(value, list):
            return [self.materialize(child, key) for child in value]
        if isinstance(value, str) and value.startswith("ref-"):
            return self.references.setdefault(value, "incident-" + value)
        return value

    async def run(self):
        return await asyncio.wait_for(self._run(), 45)

    async def _run(self):
        with tempfile.TemporaryDirectory(prefix="replay-", dir=temp_root()) as directory:
            base = Path(directory).resolve()
            state = base / "state"
            state.mkdir(mode=0o700)
            config_path = base / "config.toml"
            write_config(config_path, state, install_fake_codex(base))
            config = load_config(str(config_path))
            store = Store(state)
            service = ControlledService(config, store)
            crashed, drop_ack, incomplete = False, False, False
            try:
                for index, record in enumerate(self.fixture["tasks"]):
                    self.project = base / f"project-{index}"
                    service.projects("create", str(self.project))
                    task = await dispatch(service, "codex_start", {"prompt": "echo", "workdir": str(self.project), "operation_id": f"replay-bootstrap-{index}"})
                    async with service.changed:
                        await asyncio.wait_for(service.changed.wait_for(lambda: service.task(task["id"])["status"] == "completed"), 5)
                    self.tasks[record["id"]] = task["id"]
                    self.references[record["id"]] = task["id"]
                    if isinstance(record.get("thread_id"), str):
                        self.references[record["thread_id"]] = task["thread_id"]
                    _, self.tokens[task["id"]] = service.reserve_turn(task["id"])
                original_requests = {r["id"]: r for r in self.fixture["requests"]}
                events = self.fixture["events"]
                for index in range(len(events) + 1):
                    dropped = False
                    for fault in self.fixture["replay"]["injections"]:
                        if fault["at"] != index:
                            continue
                        action = fault["action"]
                        self.trace.append(action)
                        if action == "delay":
                            gate = self.gates.setdefault(fault.get("gate", "gate-0"), ReplayGate())
                            gate.entered.set()
                            await asyncio.wait_for(gate.release.wait(), 5)
                        elif action == "drop_ack":
                            drop_ack = True
                        elif action == "drop":
                            dropped = True
                        elif action == "crash":
                            await service.backend.close()
                            await service.on_exit("Incident replay injected disconnect")
                            await service.close()
                            store.close()
                            store = Store(state)
                            service = ControlledService(config, store)
                            crashed = True
                    if index == len(events) or crashed:
                        break
                    event = events[index]
                    target = self.tasks.get(event["task"])
                    if not target:
                        raise BridgeError("Incident event has no selected task", code="invalid_arguments")
                    record = service.task(target)
                    self.project = Path(record["project"])
                    if dropped:
                        await service.on_message({"method": "bridge/event_gap", "params": {"threadId": record["thread_id"],
                            "turnId": record.get("turn_id"), "reason": "event_ingress_budget", "dropped_events": 1, "event_gap_possible": True}})
                        continue
                    kind, data = event["kind"], event["data"]
                    self.trace.append(kind)
                    if self.fixture["partial"] and isinstance(data, dict) and "fragment" in data:
                        incomplete = True
                        with store.transaction():
                            service.lifecycle.gap(target, "incident_export_partial", disconnected=True)
                            store.event(target, "bridge/event_gap", {"reason": "incident_export_partial", "event_gap_possible": True})
                        self.trace.append("bridge/event_gap")
                        continue
                    if kind == "bridge/request":
                        source = original_requests.get(data.get("id"), data)
                        params = self.materialize(source.get("params", {}))
                        params["threadId"] = record["thread_id"]
                        await service.on_message({"method": source["method"], "id": self.materialize(source.get("rpc_id", source["id"])), "params": params})
                    elif kind == "bridge/disconnected":
                        await service.on_exit("Incident replay recorded disconnect")
                    elif kind == "bridge/event_gap":
                        await service.on_message({"method": kind, "params": {"threadId": record["thread_id"],
                            "turnId": record.get("turn_id"), "reason": "event_ingress_budget", "dropped_events": 1, "event_gap_possible": True}})
                    elif not kind.startswith("bridge/") and kind != "initialized":
                        params = self.materialize(data)
                        if not isinstance(params, dict):
                            raise BridgeError("Incident native parameters must be an object", code="invalid_arguments")
                        params["threadId"] = record["thread_id"]
                        if kind == "thread/started":
                            continue  # Bootstrap already verified this owned thread.
                        await service.on_message({"method": kind, "params": params})
                if not crashed and not incomplete:
                    for target, token in self.tokens.items():
                        current = service.task(target)
                        if current.get("turn_id") and not drop_ack:
                            await service.ack_turn(target, {"turn": {"id": current["turn_id"]}}, token=token)
                        elif drop_ack:
                            service.lifecycle.uncertain(target, token=token)
                return {"trace": self.trace, "acknowledged": not drop_ack and not crashed and not incomplete, "restarted": crashed,
                    "tasks": [{"id": alias, "status": service.task(key)["status"],
                        "evidence_complete": service.task(key)["evidence_complete"],
                        "requests": [req["status"] for _, req in store.records("requests", task_id=key)]}
                        for alias, key in self.tasks.items()]}
            finally:
                for gate in self.gates.values():
                    gate.release.set()
                await asyncio.wait_for(service.close(), 15)
                store.close()
