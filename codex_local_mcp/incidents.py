"""Explicit structural incident export. No content opt-in and no upload path."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .contract import NATIVE_METHODS, NATIVE_NOTIFICATIONS, NATIVE_SERVER_REQUESTS
from .security import BridgeError, checked_directory, private_file, redact
from .store import Store, SCHEMA_VERSION

FORMAT = 1
MAX_TASKS, MAX_EVENTS, MAX_BYTES, MAX_NODES, MAX_DEPTH = 8, 1000, 2 * 1024 * 1024, 30000, 24
BRIDGE_KINDS = frozenset("task_created input start_failed recovery resumed disconnected request response_sent request_rejected "
    "protocol_error event_gap steered settings_changed forked renamed archive unarchive deleted reverted imported subagent_discovered".split())
METHODS = frozenset((*NATIVE_METHODS, *NATIVE_NOTIFICATIONS, *NATIVE_SERVER_REQUESTS,
    *("bridge/" + value for value in BRIDGE_KINDS)))
KEYS = frozenset("id task_id thread_id turn_id item_id request_id operation_id parent_task_id root_task_id project_id "
    "threadId turnId itemId requestId callId sessionId parentThreadId generation verified_generation rpc_id "
    "method params kind type status source thread turn item items text summary command commands files cwd project path diff changes "
    "error message code reason question questions options label description header answers permissions network enabled "
    "fileSystem read write scope origin statusMessage requires_human blocking isBlocking canAcceptDirectInput "
    "created_at updated_at at seq name nickname model effort access approval_policy approval_mode approvals_reviewer "
    "availableDecisions decision content contentItems arguments tool success result input data usage tokenUsage "
    "startedAtMs completedAtMs commandActions exitCode durationMs aggregatedOutput plan step move_path phase "
    "event_gap_possible evidence_complete evidence_gaps turn_status projection_turn_id proposal_hash proposal_revision "
    "details_truncated detail_complete content_redacted startedAt completedAt activeFlags request_id_kind revision "
    "can_accept_direct_input requiresOpenaiAuth activePermissionProfile runtimeWorkspaceRoots approvalPolicy "
    "reasoningEffort modelProvider cliVersion createdAt updatedAt ephemeral preview projectId session_id "
    "contentIndex delta permissionsProfileId sandbox details truncated_fields omitted_fields fragment key value".split())
ID_KEYS = frozenset("id task_id thread_id turn_id item_id request_id operation_id parent_task_id root_task_id project_id "
    "threadId turnId itemId requestId callId sessionId parentThreadId generation verified_generation rpc_id "
    "session_id projection_turn_id proposal_hash".split())
ENUMS = frozenset("starting queued running waiting interrupting uncertain needs_resume idle archived deleted completed failed interrupted "
    "pending answering resolved expired sent inProgress active paused blocked cancelled input_required working "
    "agentMessage commandExecution fileChange dynamicToolCall userMessage reasoning appServer readOnly "
    "command_approval file_approval parent_question user_input permissions elicitation inputText text update add delete "
    "final_answer commentary turn user native_timeout transport_unavailable native_protocol_incompatible "
    "event_ingress_budget recovery unknown".split())
ENUM_KEYS = frozenset({"status", "type", "kind", "method", "scope", "origin", "phase", "reason", "turn_status"})


def omitted(value):
    kind = {str: "string", bool: "boolean", int: "integer", float: "number", list: "array", dict: "object", type(None): "null"}.get(type(value))
    if kind is None:
        raise BridgeError("Incident contains an unsupported value", code="invalid_arguments")
    result = {"type": kind}
    if value is not None:
        result["length"] = len(value) if isinstance(value, (str, list, dict)) else len(json.dumps(value))
    return {"$omitted": result}


class StructuralExport:
    def __init__(self):
        self.references, self.nodes, self.partial = {}, 0, False

    def ref(self, value):
        if value is None:
            return None
        key = (type(value).__name__, str(value))
        return self.references.setdefault(key, f"ref-{len(self.references) + 1:04d}")

    def value(self, value, key="", depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise BridgeError("Incident structure exceeds the export budget; select fewer events", code="result_too_large")
        if isinstance(value, dict):
            if len(value) > 256:
                self.partial = True
                return omitted(value)
            result = {}
            for index, (name, child) in enumerate(value.items()):
                if name in KEYS:
                    result[name] = self.value(child, name, depth + 1)
                else:
                    result[f"field_{index}"] = {"key": omitted(name), "value": self.value(child, "", depth + 1)}
            return result
        if isinstance(value, list):
            if len(value) > 256:
                self.partial = True
                return omitted(value)
            return [self.value(child, key, depth + 1) for child in value]
        if key in ID_KEYS and type(value) in (str, int):
            reference = self.ref(value)
            return {"$reference": reference, "type": "integer"} if type(value) is int else reference
        if key in ENUM_KEYS and isinstance(value, str) and (value in ENUMS or value in METHODS):
            return value
        if key == "code" and (value in BridgeError.CODES if isinstance(value, str) else type(value) is int and -32700 <= value <= -32000):
            return value
        if key in {"blocking", "isBlocking", "requires_human", "content_redacted", "evidence_complete", "event_gap_possible"} and type(value) is bool:
            return value
        return omitted(value)


def export_incident(store, task_ids, *, after=0, limit=200):
    if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= MAX_TASKS
            or any(not isinstance(key, str) or not key for key in task_ids)
            or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= MAX_EVENTS):
        raise BridgeError("Select 1..8 task ids, a nonnegative cursor and 1..1000 events", code="invalid_arguments")
    structure = StructuralExport()
    tasks, requests, events = [], [], []
    for key in dict.fromkeys(task_ids):
        tasks.append(structure.value(store.get("tasks", key)))
        for _, req in store.records("requests", task_id=key, limit=MAX_EVENTS + 1):
            requests.append(structure.value(req))
    try:
        page = store.events(after, limit, task_ids=list(dict.fromkeys(task_ids)))
    except BridgeError as exc:
        resume = exc.details.get("resume_after")
        if exc.code == "cursor_expired" and type(resume) is int:
            raise BridgeError(f"Incident journal cursor expired; preview again with --after {resume}",
                code=exc.code, details=exc.details, ids=exc.ids, next_steps=[f"Preview again with --after {resume}"]) from exc
        raise
    for index, entry in enumerate(page["events"]):
        if "fragment" in entry:
            # Fragment pages intentionally omit ownership fields. Read only
            # their identity, never reconstruct or retain the private fragment.
            identity = store.db.execute("SELECT task_id,kind FROM events WHERE seq=?", (entry["seq"],)).fetchone()
            if not identity or identity[0] not in task_ids:
                raise BridgeError("Incident fragment identity changed; preview again", code="state_conflict")
            entry = {**entry, "task_id": identity[0], "kind": identity[1]}
            structure.partial = True
        kind = entry.get("kind")
        if kind not in METHODS:
            kind = "bridge/unknown"
        events.append({"order": index, "task": structure.ref(entry.get("task_id")), "kind": kind,
                       "data": structure.value(entry.get("data", {"fragment": entry.get("fragment", "")}))})
    value = redact({"format": FORMAT, "purpose": "codex-local-mcp structural incident; fake harness only",
        "bridge_version": __version__, "schema_version": store.schema_version,
        "content_omitted": True, "tasks": tasks, "requests": requests, "events": events,
        "partial": bool(page["has_more"] or page.get("next_event_offset") or structure.partial),
        "replay": {"consumer": "deterministic-fake-only", "injections": []}})
    validate_incident(value)
    return value


def fixture_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def preview(value):
    data = fixture_bytes(value)
    return {"preview_id": hashlib.sha256(data).hexdigest(), "bytes": len(data), "content_omitted": True,
            "tasks": len(value["tasks"]), "events": len(value["events"]), "requests": len(value["requests"]),
            "partial": value["partial"], "fixture": value}


def write_export(value, output, confirmation):
    report = preview(value)
    if confirmation != report["preview_id"]:
        raise BridgeError("Preview confirmation is missing or the incident changed; preview again", code="incident_preview_mismatch")
    path = Path(output).expanduser().absolute()
    checked_directory(path.parent)
    try:
        descriptor = private_file(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except BridgeError as exc:
        # The Windows private-file helper wraps OS failures; preserve the
        # cross-platform exclusive-creation result without a racy exists check.
        if isinstance(exc.__cause__, FileExistsError):
            raise exc.__cause__ from None
        raise
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(fixture_bytes(value))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {key: item for key, item in report.items() if key != "fixture"} | {"written": True}


def validate_incident(value):
    if (not isinstance(value, dict) or set(value) != {"format", "purpose", "bridge_version", "schema_version", "content_omitted", "tasks", "requests", "events", "partial", "replay"}
            or type(value.get("format")) is not int or value["format"] != FORMAT or value.get("content_omitted") is not True
            or not isinstance(value.get("tasks"), list) or not 1 <= len(value["tasks"]) <= MAX_TASKS
            or not isinstance(value.get("requests"), list) or len(value["requests"]) > MAX_EVENTS
            or not isinstance(value.get("events"), list) or len(value["events"]) > MAX_EVENTS
            or type(value.get("partial")) is not bool or value.get("purpose") != "codex-local-mcp structural incident; fake harness only"
            or not isinstance(value.get("bridge_version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", value["bridge_version"])
            or type(value.get("schema_version")) is not int):
        raise BridgeError("Unsupported or malformed structural incident fixture", code="invalid_arguments")
    replay = value["replay"]
    if (not isinstance(replay, dict) or set(replay) != {"consumer", "injections"}
            or replay.get("consumer") != "deterministic-fake-only" or not isinstance(replay.get("injections"), list)):
        raise BridgeError("Incident replay is restricted to the deterministic fake harness", code="invalid_arguments")
    if len(fixture_bytes(value)) > MAX_BYTES:
        raise BridgeError("Incident exceeds the 2 MiB export budget", code="result_too_large")
    aliases = set()
    for record in value["tasks"]:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not re.fullmatch(r"ref-[0-9]{4,}", record["id"]) or record["id"] in aliases:
            raise BridgeError("Malformed incident task identity", code="invalid_arguments")
        aliases.add(record["id"])
    if any(not isinstance(record, dict) or not isinstance(record.get("id"), str) for record in value["requests"]):
        raise BridgeError("Malformed incident request identity", code="invalid_arguments")
    for index, event in enumerate(value["events"]):
        if (not isinstance(event, dict) or set(event) != {"order", "task", "kind", "data"} or type(event["order"]) is not int or event["order"] != index
                or event["kind"] not in METHODS | {"bridge/unknown"} or event["task"] not in aliases):
            raise BridgeError("Malformed incident event ordering", code="invalid_arguments")
    for injection in replay["injections"]:
        if (not isinstance(injection, dict) or set(injection) - {"action", "at", "gate"}
                or injection.get("action") not in ("delay", "drop", "crash", "drop_ack")
                or type(injection.get("at")) is not int or not 0 <= injection["at"] <= len(value["events"])
                or ("gate" in injection and (not isinstance(injection["gate"], str) or not re.fullmatch(r"gate-[0-9]{1,2}", injection["gate"])) )):
            raise BridgeError("Malformed incident fault injection", code="invalid_arguments")
    nodes = 0
    def check(item, key="", depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise BridgeError("Incident structure exceeds the replay budget", code="result_too_large")
        if isinstance(item, dict):
            if "$reference" in item:
                if (set(item) != {"$reference", "type"} or item["type"] != "integer"
                        or not isinstance(item["$reference"], str) or not re.fullmatch(r"ref-[0-9]{4,}", item["$reference"])):
                    raise BridgeError("Malformed typed incident reference", code="invalid_arguments")
                return
            if "$omitted" in item:
                placeholder = item["$omitted"]
                if (set(item) != {"$omitted"} or not isinstance(placeholder, dict)
                        or set(placeholder) not in ({"type"}, {"type", "length"})
                        or placeholder.get("type") not in ("string", "integer", "number", "boolean", "null", "array", "object")
                        or ("length" in placeholder and (type(placeholder["length"]) is not int or not 0 <= placeholder["length"] <= 2**63 - 1))):
                    raise BridgeError("Malformed typed incident placeholder", code="invalid_arguments")
                return
            if len(item) > 256:
                raise BridgeError("Incident object exceeds 256 fields", code="result_too_large")
            for field, child in item.items():
                if field not in KEYS and not re.fullmatch(r"field_[0-9]+", field):
                    raise BridgeError("Incident contains an unrecognized structural field", code="invalid_arguments")
                check(child, field, depth + 1)
        elif isinstance(item, list):
            if len(item) > 256:
                raise BridgeError("Incident array exceeds 256 entries", code="result_too_large")
            for child in item:
                check(child, key, depth + 1)
        elif item is None and key in ID_KEYS:
            return
        elif key in ID_KEYS and isinstance(item, str) and re.fullmatch(r"ref-[0-9]{4,}", item):
            return
        elif key in ENUM_KEYS and isinstance(item, str) and item in ENUMS | METHODS:
            return
        elif key == "code" and (item in BridgeError.CODES if isinstance(item, str) else type(item) is int and -32700 <= item <= -32000):
            return
        elif key in {"blocking", "isBlocking", "requires_human", "content_redacted", "evidence_complete", "event_gap_possible"} and type(item) is bool:
            return
        else:
            raise BridgeError("Incident content must use typed placeholders", code="invalid_arguments")
    for item in (*value["tasks"], *value["requests"], *(event["data"] for event in value["events"])):
        check(item)
    return value


@contextmanager
def readonly_store(config):
    """Borrow Store readers on a read-only connection, without its migrating constructor."""
    from .diagnostics import inspect_state, lock_status
    _, safe = inspect_state(config.state_dir)
    if not safe:
        raise BridgeError("Incident state directory is not private", code="state_invalid")
    path = config.state_dir / "state.sqlite3"
    os.close(private_file(path, os.O_RDONLY))
    if (config.state_dir / "state.sqlite3-wal").exists() and not (config.state_dir / "state.sqlite3-shm").exists():
        raise BridgeError("Incident database WAL cannot be read without creating shared memory", code="state_conflict")
    immutable = not (config.state_dir / "state.sqlite3-wal").exists() and lock_status(config.state_dir / "broker.lock") in ("free", "absent")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        version, empty = Store._inspect(db)
        if empty or version != SCHEMA_VERSION:
            raise BridgeError("Incident export needs a database matching this bridge; no migration was performed", code="state_invalid")
        store = object.__new__(Store)
        store.db, store.schema_version, store._savepoint = db, version, 0
        db.execute("BEGIN")
        yield store
    except sqlite3.DatabaseError as exc:
        raise BridgeError("Incident database read failed; inspect doctor before exporting", code="state_invalid") from exc
    finally:
        db.close()
