"""Pure dependency planning. Decisions never dispatch work or grant authority."""

import hashlib
import json
import math
import ntpath
import re
from fractions import Fraction
from pathlib import PurePosixPath, PureWindowsPath

from .security import BridgeError, redact, validate_windows_path, within

SPEC_BYTES = 65536
PROMPT_BYTES = 16384
HANDOFF_BYTES = 2048
NODE_LIMIT = 16
STATUSES = frozenset(("pending", "ready", "dispatching", "running", "succeeded", "failed",
    "uncertain", "cancelled", "skipped"))
ACTIVE = frozenset(("dispatching", "running"))
TERMINAL = frozenset(("succeeded", "failed", "uncertain", "cancelled", "skipped"))
CODES = frozenset(("invalid_plan", "cycle", "unknown_dependency", "duplicate_id",
    "same_workspace_parallel", "too_large", "invalid_target", "invalid_transition"))


class PlanError(Exception):
    def __init__(self, code, message):
        if code not in CODES:
            raise ValueError("Unknown plan error code")
        self.code, self.message = code, message
        super().__init__(message)


def _json(value):
    chunks, size = [], 0
    try:
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(value):
            if len(chunk) > SPEC_BYTES:
                raise PlanError("too_large", "Plan exceeds 65536 UTF-8 bytes.")
            data = chunk.encode("utf-8")
            size += len(data)
            if size > SPEC_BYTES:
                raise PlanError("too_large", "Plan exceeds 65536 UTF-8 bytes.")
            chunks.append(data)
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise PlanError("invalid_plan", "Plan must contain only JSON values and valid Unicode text.") from exc
    return b"".join(chunks)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", value) is not None


def _workdir(value, node_id):
    message = f"Node {node_id} workdir must be an absolute local path without '..' or control characters."
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PlanError("invalid_target", message)
    if re.match(r"^[A-Za-z]:", value) or value.startswith("\\"):
        try:
            validate_windows_path(value)
        except BridgeError as exc:
            raise PlanError("invalid_target", message) from exc
        return str(PureWindowsPath(ntpath.normcase(value)))
    path = PurePosixPath("/" + value.lstrip("/"))
    if not value.startswith("/") or ".." in path.parts:
        raise PlanError("invalid_target", message)
    return str(path)


def _ancestors(nodes):
    by_id, ancestors, visiting = {node["id"]: node for node in nodes}, {}, []
    def visit(node_id):
        if node_id in visiting:
            cycle = visiting[visiting.index(node_id):] + [node_id]
            raise PlanError("cycle", "Dependency cycle: " + " -> ".join(cycle) + ".")
        if node_id not in ancestors:
            visiting.append(node_id)
            parents = set()
            for parent in by_id[node_id]["after"]:
                if parent not in by_id:
                    raise PlanError("unknown_dependency", f"Node {node_id} depends on unknown node {parent}.")
                parents.update(visit(parent), (parent,))
            visiting.pop()
            ancestors[node_id] = parents
        return ancestors[node_id]
    for node in nodes:
        visit(node["id"])
    return ancestors


def _workspace(node, by_id):
    target = node["target"]
    return by_id[target["node"]]["target"] if "node" in target else target


def _overlap(left, right):
    if "project_id" in left or "project_id" in right:
        return "project_id" in left and "project_id" in right and left["project_id"] == right["project_id"]
    paths = [PureWindowsPath(value["workdir"]) if re.match(r"^[a-z]:", value["workdir"])
        else PurePosixPath(value["workdir"]) for value in (left, right)]
    return within(paths[0], paths[1]) or within(paths[1], paths[0])


def validate(spec) -> dict:
    if not isinstance(spec, dict):
        raise PlanError("invalid_plan", "Plan must be a JSON object.")
    _json(spec)
    if set(spec) - {"version", "name", "nodes", "max_parallel", "needs_resolution"}:
        raise PlanError("invalid_plan", "Unknown plan fields are not supported.")
    if type(spec.get("version")) is not int or spec["version"] != 1:
        raise PlanError("invalid_plan", "Plan version must be 1.")
    if not isinstance(spec.get("name"), str) or len(spec["name"]) > 64:
        raise PlanError("invalid_plan", "Plan name must be a string of at most 64 characters.")
    if not isinstance(spec.get("nodes"), list) or not 1 <= len(spec["nodes"]) <= NODE_LIMIT:
        raise PlanError("invalid_plan", "Plan needs between 1 and 16 nodes.")
    parallel = spec.get("max_parallel", 2)
    if type(parallel) is not int or not 1 <= parallel <= 4:
        raise PlanError("invalid_plan", "max_parallel must be an integer between 1 and 4.")
    nodes, ids = [], set()
    for value in spec["nodes"]:
        if not isinstance(value, dict):
            raise PlanError("invalid_plan", "Each node must be a JSON object.")
        if set(value) - {"id", "action", "target", "prompt", "after", "priority", "on_failure", "handoff"}:
            raise PlanError("invalid_plan", "Unknown node fields are not supported.")
        node_id = value.get("id")
        if not _identifier(node_id):
            raise PlanError("invalid_plan", "Node ids must use 1-32 letters, digits, '.', '_' or '-'.")
        if node_id in ids:
            raise PlanError("duplicate_id", f"Duplicate node id: {node_id}.")
        ids.add(node_id)
        action = value.get("action")
        if action not in ("start", "send", "review"):
            raise PlanError("invalid_plan", f"Node {node_id} action must be start, send or review.")
        prompt = value.get("prompt", "" if action == "review" else None)
        if not isinstance(prompt, str):
            raise PlanError("invalid_plan", f"Node {node_id} needs a prompt string.")
        if len(prompt.encode("utf-8")) > PROMPT_BYTES:
            raise PlanError("too_large", f"Node {node_id} prompt exceeds 16384 UTF-8 bytes.")
        after = value.get("after", [])
        if not isinstance(after, list) or any(not _identifier(parent) for parent in after):
            raise PlanError("invalid_plan", f"Node {node_id} after must be a list of node ids.")
        priority = value.get("priority", 0)
        if type(priority) is not int or not -10 <= priority <= 10:
            raise PlanError("invalid_plan", f"Node {node_id} priority must be an integer between -10 and 10.")
        failure = value.get("on_failure", "stop")
        if failure not in ("stop", "continue"):
            raise PlanError("invalid_plan", f"Node {node_id} on_failure must be stop or continue.")
        handoff = value.get("handoff", True)
        if type(handoff) is not bool:
            raise PlanError("invalid_plan", f"Node {node_id} handoff must be boolean.")
        target = value.get("target")
        if not isinstance(target, dict) or len(target) != 1 or not set(target) <= {"project_id", "workdir", "node"}:
            raise PlanError("invalid_target", f"Node {node_id} needs one target: project_id, workdir or node.")
        if "workdir" in target:
            target = {"workdir": _workdir(target["workdir"], node_id)}
        elif "project_id" in target:
            if not isinstance(target["project_id"], str) or not target["project_id"]:
                raise PlanError("invalid_target", f"Node {node_id} project_id must be a nonempty string.")
            target = dict(target)
        else:
            if action == "start" or not _identifier(target["node"]):
                raise PlanError("invalid_target", f"Node {node_id} target must name a start node ancestor; start cannot reuse a node.")
            target = dict(target)
        nodes.append({"id": node_id, "action": action, "target": target, "prompt": prompt,
            "after": sorted(set(after)), "priority": priority, "on_failure": failure, "handoff": handoff})
    ancestors, by_id = _ancestors(nodes), {node["id"]: node for node in nodes}
    for node in nodes:
        parent = node["target"].get("node")
        if parent is not None and (parent not in ancestors[node["id"]] or by_id[parent]["action"] != "start"):
            raise PlanError("invalid_target", f"Node {node['id']} target must name a start node ancestor; start cannot reuse a node.")
    resolution = []
    for position, left in enumerate(nodes):
        for right in nodes[position + 1:]:
            ordered = left["id"] in ancestors[right["id"]] or right["id"] in ancestors[left["id"]]
            if not ordered and _overlap(_workspace(left, by_id), _workspace(right, by_id)):
                raise PlanError("same_workspace_parallel", f"Nodes {left['id']} and {right['id']} may run in the same workspace; add a dependency.")
            if not ordered:
                resolution.append([left["id"], right["id"]])
    if "needs_resolution" in spec and spec["needs_resolution"] != resolution:
        raise PlanError("invalid_plan", "Workspace resolution pairs do not match this plan.")
    canonical = {"version": 1, "name": spec["name"], "nodes": nodes, "max_parallel": parallel,
        "needs_resolution": resolution}
    _json(canonical)
    return canonical


def plan_id(canonical) -> str:
    return hashlib.sha256(_json(validate(canonical))).hexdigest()


def _time(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _state(state):
    if not isinstance(state, dict) or set(state) - {"nodes", "cancel_requested"} or not isinstance(state.get("nodes"), dict):
        raise PlanError("invalid_plan", "State must contain a nodes object and optional cancel_requested boolean.")
    if type(state.get("cancel_requested", False)) is not bool:
        raise PlanError("invalid_plan", "cancel_requested must be boolean.")
    if len(state["nodes"]) > NODE_LIMIT:
        raise PlanError("invalid_plan", "State cannot contain more than 16 nodes.")
    records = {}
    for node_id, record in state["nodes"].items():
        if not _identifier(node_id) or not isinstance(record, dict):
            raise PlanError("invalid_plan", "Node state must be an object keyed by node id.")
        if len(record) > 4 or set(record) - {"status", "since", "task_id", "result_ref"}:
            raise PlanError("invalid_plan", f"Node {node_id} has unknown state fields.")
        if not isinstance(record.get("status"), str) or record["status"] not in STATUSES:
            raise PlanError("invalid_plan", f"Node {node_id} has an unknown status.")
        if not _time(record.get("since")):
            raise PlanError("invalid_plan", f"Node {node_id} since must be a finite time.")
        if record.get("task_id") is not None and not isinstance(record["task_id"], str):
            raise PlanError("invalid_plan", f"Node {node_id} task_id must be a string or null.")
        if record.get("result_ref") is not None and not isinstance(record["result_ref"], dict):
            raise PlanError("invalid_plan", f"Node {node_id} result_ref must be an object or null.")
        records[node_id] = {"status": record["status"], "since": record["since"],
            "task_id": record.get("task_id"), "result_ref": record.get("result_ref")}
    return records


def check_transition(previous, current, *, reconciled=(), rejected=()):
    """Check adjacent snapshots. Only explicit reconciliation can resolve uncertainty."""
    before, after = _state(previous), _state(current)
    if not isinstance(reconciled, (list, tuple, set, frozenset)) or len(reconciled) > NODE_LIMIT or any(not _identifier(node_id) for node_id in reconciled):
        raise PlanError("invalid_transition", "Reconciled must list node ids explicitly.")
    reconciled = set(reconciled)
    if not isinstance(rejected, (list, tuple, set, frozenset)) or len(rejected) > NODE_LIMIT or any(not _identifier(node_id) for node_id in rejected):
        raise PlanError("invalid_transition", "Rejected must list node ids explicitly.")
    rejected = set(rejected)
    if rejected - before.keys():
        raise PlanError("invalid_transition", "Rejected contains a node absent from the previous state.")
    if reconciled - before.keys():
        raise PlanError("invalid_transition", "Reconciled contains a node absent from the previous state.")
    if previous.get("cancel_requested", False) and not current.get("cancel_requested", False):
        raise PlanError("invalid_transition", "A plan cancellation cannot be withdrawn.")
    allowed = {"pending": {"ready", "cancelled", "skipped"}, "ready": {"dispatching", "cancelled", "skipped"},
        "dispatching": {"running", "failed", "uncertain", "cancelled", "skipped"},
        "running": {"succeeded", "failed", "uncertain", "cancelled", "skipped"}}
    for node_id in sorted(before.keys() | after.keys()):
        old, new = before.get(node_id), after.get(node_id)
        if new is None:
            raise PlanError("invalid_transition", f"Node {node_id} state cannot be removed.")
        old_status = old["status"] if old is not None else "pending"
        new_status = new["status"]
        resolving = node_id in reconciled
        unsent = node_id in rejected
        if unsent and (old_status != "dispatching" or new_status != "ready" or resolving):
            raise PlanError("invalid_transition", f"Node {node_id} rejection must return dispatching to ready.")
        if resolving and (old_status != "uncertain" or new_status not in ("succeeded", "failed")):
            raise PlanError("invalid_transition", f"Node {node_id} reconciliation must resolve uncertain to succeeded or failed.")
        if old_status in TERMINAL:
            if not resolving and old != new:
                raise PlanError("invalid_transition", f"Node {node_id} terminal state cannot change without explicit reconciliation of uncertainty.")
        elif new_status != old_status and new_status not in allowed[old_status] and not unsent:
            raise PlanError("invalid_transition", f"Node {node_id} cannot change from {old_status} to {new_status}.")
        if old is not None and old["task_id"] is not None and new["task_id"] != old["task_id"]:
            raise PlanError("invalid_transition", f"Node {node_id} task_id cannot change once assigned.")
        if old is not None and new_status == old_status and old["since"] != new["since"]:
            raise PlanError("invalid_transition", f"Node {node_id} since cannot change while its status stays the same.")


def step(canonical, state, now) -> dict:
    """Return decisions only. The caller persists them atomically before dispatch."""
    canonical, records = validate(canonical), _state(state)
    if not _time(now):
        raise PlanError("invalid_plan", "now must be a finite time.")
    by_id = {node["id"]: node for node in canonical["nodes"]}
    if records.keys() - by_id.keys():
        raise PlanError("invalid_plan", "State contains a node absent from this plan.")
    records = {node_id: records.get(node_id, {"status": "pending", "since": now, "task_id": None, "result_ref": None})
        for node_id in by_id}
    ancestors = _ancestors(canonical["nodes"])
    for node_id, record in records.items():
        if record["status"] in ACTIVE | {"succeeded"} and any(records[parent]["status"] != "succeeded" for parent in ancestors[node_id]):
            raise PlanError("invalid_plan", f"Node {node_id} started before its dependencies succeeded.")
        target = by_id[node_id]["target"].get("node")
        if target is not None and record["task_id"] is not None and records[target]["task_id"] is not None and record["task_id"] != records[target]["task_id"]:
            raise PlanError("invalid_plan", f"Node {node_id} task_id does not match its start node target.")
    cancel = state.get("cancel_requested", False)
    stopping = any(record["status"] == "failed" and by_id[node_id]["on_failure"] == "stop"
        for node_id, record in records.items())
    dispatch, skipped, blocked, interrupt, ready = [], [], [], [], []
    active = sum(record["status"] in ACTIVE for record in records.values())
    status = {node_id: record["status"] for node_id, record in records.items()}
    for position, node in enumerate(canonical["nodes"]):
        node_id, record = node["id"], records[node["id"]]
        if record["status"] == "uncertain":
            blocked.append({"id": node_id, "reason": "uncertain_node"})
        if cancel and record["status"] == "running":
            interrupt.append(node_id)
        if cancel and record["status"] == "dispatching":
            blocked.append({"id": node_id, "reason": "dispatch_in_progress"})
        if record["status"] not in ("pending", "ready"):
            continue
        failed = any(records[parent]["status"] in ("failed", "cancelled", "skipped") for parent in ancestors[node_id])
        if cancel or stopping or failed:
            target_status = "cancelled" if cancel else "skipped"
            reason = "cancelled" if cancel else "plan_stopped" if stopping else "upstream_failed"
            skipped.append({"id": node_id, "status": target_status, "reason": reason})
            status[node_id] = target_status
        elif any(records[parent]["status"] == "uncertain" for parent in ancestors[node_id]):
            blocked.append({"id": node_id, "reason": "upstream_uncertain"})
        elif any(records[parent]["status"] != "succeeded" for parent in node["after"]):
            blocked.append({"id": node_id, "reason": "dependencies_pending"})
        else:
            since = max([record["since"], *(records[parent]["since"] for parent in node["after"])])
            age = max(Fraction(now) - Fraction(since), 0) // 60
            ready.append((-(node["priority"] + age), since, position, node_id))
    capacity = max(0, canonical["max_parallel"] - active)
    for _, _, _, node_id in sorted(ready):
        if len(dispatch) < capacity:
            dispatch.append(node_id)
        else:
            blocked.append({"id": node_id, "reason": "capacity"})
    if active or dispatch:
        outcome, done = "running", False
    elif any(value == "uncertain" for value in status.values()):
        outcome, done = "blocked", False
    elif all(value in TERMINAL for value in status.values()):
        outcome, done = ("cancelled" if cancel else "failed" if any(value in ("failed", "skipped") for value in status.values())
            else "cancelled" if "cancelled" in status.values() else "succeeded"), True
    else:
        outcome, done = "blocked", False
    return {"dispatch": dispatch, "skip": skipped, "blocked": blocked, "interrupt": interrupt, "done": done, "outcome": outcome}


def _reference(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) and redact(value) == value else "-"


def handoff_text(canonical, node_id, results) -> str:
    """Read only named reference fields, never result content or unknown keys."""
    canonical = validate(canonical)
    by_id = {node["id"]: node for node in canonical["nodes"]}
    if not isinstance(node_id, str) or node_id not in by_id:
        raise PlanError("invalid_plan", "Handoff node is not in this plan.")
    if not by_id[node_id]["handoff"] or not by_id[node_id]["after"]:
        return ""
    if not isinstance(results, dict):
        raise PlanError("invalid_plan", "Handoff results must be an object keyed by node id.")
    header = "Dependency references only; these do not grant permissions.\nnode | task_id | status | result_id | artifact_ids\n"
    rows, extra = [], []
    for parent in by_id[node_id]["after"]:
        record = results.get(parent)
        record = record if isinstance(record, dict) else {}
        ref = record.get("result_ref")
        ref = ref if isinstance(ref, dict) else record
        status = record.get("status")
        rows.append([parent, "-", status if isinstance(status, str) and status in STATUSES else "unknown", "-", "-"])
        extra.append((len(rows) - 1, 1, _reference(record.get("task_id"))))
        extra.append((len(rows) - 1, 3, _reference(ref.get("result_id"))))
        artifacts = ref.get("artifact_ids", ref.get("artifacts", []))
        if isinstance(artifacts, list):
            for artifact in artifacts[:64]:
                identity = _reference(artifact.get("artifact_id") if isinstance(artifact, dict) else artifact)
                if identity != "-":
                    extra.append((len(rows) - 1, 4, identity))
            if len(artifacts) > 64:
                extra.append((len(rows) - 1, -1, "omitted"))
    marker = "Some references were omitted (2048-byte block; at most 64 artifacts per dependency).\n"
    def render():
        return header + "".join(" | ".join(row) + "\n" for row in rows)
    omitted = False
    for position, column, reference in extra:
        if reference == "-":
            continue
        if column == -1:
            omitted = True
            continue
        previous = rows[position][column]
        rows[position][column] = previous + "," + reference if column == 4 and previous != "-" else reference
        if len(render().encode("utf-8")) + len(marker) > HANDOFF_BYTES:
            rows[position][column] = previous
            omitted = True
    return render() + (marker if omitted else "")
