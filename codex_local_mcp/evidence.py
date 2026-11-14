"""Turn evidence and immutable, task-scoped bytes. Native reports are not proof."""

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from .security import BridgeError, read_project_file, redact
from .lifecycle import TERMINAL
from . import native_turns

RESULT_VERSION = 1
INLINE_BYTES = 16 * 1024
ARTIFACT_BYTES = 32 * 1024 * 1024
READ_BYTES = 64 * 1024
MAX_READ_BYTES = 256 * 1024
CATEGORIES = ("agent_claims", "observed_commands", "reported_changes", "artifacts", "limitations")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def migrate(db):
    for statement in (
        "CREATE TABLE artifacts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_id TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, content BLOB, path TEXT, source_seq INTEGER, created_at REAL NOT NULL, file_version TEXT, redaction_count INTEGER NOT NULL, source_locator TEXT)",
        "CREATE INDEX artifacts_owner ON artifacts(task_id,turn_id)",
        "CREATE TABLE evidence_entries (task_id TEXT NOT NULL, turn_id TEXT NOT NULL, category TEXT NOT NULL, item_key TEXT NOT NULL, source_seq INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(task_id,turn_id,category,item_key))",
        "CREATE TABLE turn_results (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL, sha256 TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(task_id,turn_id,revision))",
        "CREATE INDEX results_turn ON turn_results(task_id,turn_id,revision)",
        "CREATE TABLE managed_images (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, operation_id TEXT, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE operation_tombstones (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT, expired_at REAL NOT NULL)",
        "CREATE TABLE operation_times (id TEXT PRIMARY KEY, updated_at REAL NOT NULL)",
        "CREATE TRIGGER operation_created AFTER INSERT ON operations BEGIN INSERT OR REPLACE INTO operation_times VALUES (NEW.id, CAST(strftime('%s','now') AS REAL)); END",
        "CREATE TRIGGER operation_updated AFTER UPDATE ON operations BEGIN INSERT OR REPLACE INTO operation_times VALUES (NEW.id, CAST(strftime('%s','now') AS REAL)); END",
        "CREATE TRIGGER operation_removed AFTER DELETE ON operations BEGIN DELETE FROM operation_times WHERE id=OLD.id; END",
        "CREATE TABLE retention_plans (id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL, snapshot TEXT NOT NULL, policy TEXT NOT NULL, data TEXT NOT NULL, committed_at REAL)",
        "CREATE TABLE expired_event_ranges (first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL, task_id TEXT, PRIMARY KEY(first_seq,last_seq))",
        "CREATE INDEX expired_event_last ON expired_event_ranges(last_seq)",
        "CREATE INDEX expired_event_task ON expired_event_ranges(task_id,last_seq)",
        "CREATE TABLE result_expirations (task_id TEXT NOT NULL, turn_id TEXT NOT NULL, expired_at REAL NOT NULL, PRIMARY KEY(task_id,turn_id))",
        "CREATE TABLE retention_state (id INTEGER PRIMARY KEY CHECK(id=1), request_epoch INTEGER NOT NULL, historical_turn_ordinal INTEGER NOT NULL)",
        "INSERT INTO retention_state SELECT 1,0,COALESCE(MAX(ordinal),0) FROM turns",
        "CREATE TABLE evidence_history (task_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(task_id,turn_id))",
    ):
        db.execute(statement)


def artifact(db, task_id, turn_id, data, *, source_seq=None, path=None, live=False, file_version=None, redaction_count=0, locator=None):
    if len(data) > ARTIFACT_BYTES:
        raise BridgeError("Artifact exceeds the 32 MiB capture limit", code="artifact_too_large")
    digest = hashlib.sha256(data).hexdigest()
    kind = "live_file" if live else "captured"
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([task_id, turn_id, kind, path, digest, redaction_count,
                                                           source_seq if locator else None, locator])))
    if live:
        existing = db.execute("SELECT id FROM artifacts WHERE task_id=? AND turn_id=? AND kind=? AND path=? AND file_version=? AND sha256=?",
                              (task_id, turn_id, kind, path, file_version, digest)).fetchone()
        # No public identifier or hash may encode the unredacted secret bytes.
        identity = existing[0] if existing else str(uuid.uuid4())
    db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        identity, task_id, turn_id, kind, digest, len(data), None if live or locator else data, path, source_seq, time.time(),
        file_version if live else None, redaction_count, json.dumps(locator) if locator else None))
    return {"artifact_id": identity, "kind": kind, "sha256": digest, "size_bytes": len(data),
            "task_id": task_id, "turn_id": turn_id, "content_redacted": bool(redaction_count), "redaction_count": redaction_count,
            **({"path": path} if path else {})}


def file_bytes(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0
    safe = redact(text)
    try:
        value = json.loads(data)
        sanitized = redact(value)
        if sanitized != value:
            safe = json.dumps(sanitized, ensure_ascii=False)
    except RecursionError:
        return b"[REDACTED]", 1
    except ValueError:
        pass
    return safe.encode("utf-8"), int(safe != text)


def pack(db, task_id, turn_id, value, seq, redaction_count=0, *, method, path=()):
    if isinstance(value, (dict, list)) and len(canonical(value)) > INLINE_BYTES:
        data = canonical(value)
        if len(data) > ARTIFACT_BYTES:
            return {"content_omitted": True, "reason": "artifact_too_large", "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
        locator = {"format": 1, "method": method, "path": list(path), "encoding": "json"}
        return {**artifact(db, task_id, turn_id, data, source_seq=seq, redaction_count=redaction_count, locator=locator), "encoding": "json"}
    if isinstance(value, str) and len(value.encode("utf-8")) > INLINE_BYTES:
        data = value.encode("utf-8")
        if len(data) > ARTIFACT_BYTES:
            return {"content_omitted": True, "reason": "artifact_too_large", "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
        locator = {"format": 1, "method": method, "path": list(path), "encoding": "utf-8"}
        return {**artifact(db, task_id, turn_id, data, source_seq=seq, redaction_count=redaction_count, locator=locator), "encoding": "utf-8"}
    if isinstance(value, dict):
        return {k: pack(db, task_id, turn_id, v, seq, redaction_count, method=method, path=(*path, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [pack(db, task_id, turn_id, v, seq, redaction_count, method=method, path=(*path, i)) for i, v in enumerate(value)]
    return value


def put(db, task_id, turn_id, category, key, seq, data):
    if category == "observed_commands" and data.get("source", {}).get("method") == "item/started":
        previous = db.execute("SELECT data FROM evidence_entries WHERE task_id=? AND turn_id=? AND category=? AND item_key=?",
                              (task_id, turn_id, category, str(key))).fetchone()
        if previous and json.loads(previous[0]).get("source", {}).get("method") == "item/completed":
            return
    db.execute("INSERT INTO evidence_entries VALUES (?,?,?,?,?,?) ON CONFLICT(task_id,turn_id,category,item_key) "
               "DO UPDATE SET source_seq=excluded.source_seq,data=excluded.data WHERE excluded.source_seq>=evidence_entries.source_seq",
               (task_id, turn_id, category, str(key), seq, json.dumps(data)))


def reported_value(method, params):
    item = params.get("item") or {}
    category, value = None, None
    if method in ("item/completed", "bridge/recovered_item") or (method == "item/started" and item.get("type") == "commandExecution"):
        category = {"agentMessage": "agent_claims", "commandExecution": "observed_commands", "fileChange": "reported_changes"}.get(item.get("type"))
        value = item
    elif method == "turn/diff/updated":
        category, value = "reported_changes", {"diff": params.get("diff", ""), "type": "native_diff"}
    elif method == "item/fileChange/patchUpdated":
        category, value = "reported_changes", {"changes": params.get("changes"), "type": "native_patch"}
    elif method in ("bridge/event_gap", "bridge/protocol_error"):
        category, value = "limitations", {"reason": method}
    return category, value


def observe(db, task_id, method, params, seq, *, capture_files=True, redaction_count=0):
    if not task_id or not isinstance(params, dict):
        return
    task = db.execute("SELECT thread_id,project,projection FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return
    scope = native_turns.Scope(db, task_id, task["thread_id"])
    turn_id = native_turns.event_id(params, scope)
    if not turn_id:
        return
    item = params.get("item") or {}
    item_id = item.get("id") or params.get("itemId")
    source = {"event_seq": seq, "method": method, "task_id": task_id, "thread_id": task["thread_id"],
              "turn_id": turn_id, "item_id": item_id}
    category, value = reported_value(method, params)
    if category:
        put(db, task_id, turn_id, category, item_id or method, seq,
            {"source": source, "reported": pack(db, task_id, turn_id, value, seq, redaction_count, method=method),
             "content_redacted": bool(redaction_count), "redaction_count": redaction_count})
    if capture_files and method == "item/completed" and item.get("type") == "fileChange":
        for change in item.get("changes") or []:
            paths = [change.get("path"), (change.get("kind") or {}).get("move_path")]
            for path in dict.fromkeys(p for p in paths if isinstance(p, str)):
                try:
                    cwd = json.loads(task["projection"]).get("native_cwd") or task["project"]
                    absolute = Path(path) if Path(path).is_absolute() else Path(cwd) / path
                    relative = str(absolute.relative_to(task["project"]))
                    data = read_project_file(task["project"], relative, ARTIFACT_BYTES)
                    version = hashlib.sha256(data).hexdigest()
                    data, redactions = file_bytes(data)
                    for live in (False, True):
                        ref = artifact(db, task_id, turn_id, data, source_seq=seq, path=relative, live=live,
                                       file_version=version, redaction_count=redactions)
                        put(db, task_id, turn_id, "artifacts", ref["artifact_id"], seq, {**ref, "source": source})
                except (BridgeError, OSError, ValueError):
                    put(db, task_id, turn_id, "limitations", "file:" + path, seq,
                        {"source": source, "path": path, "reason": "File bytes were not captured; native changes are reports"})


class Evidence:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def hydrate_history(self, task_id, turn_id, fallback_status):
        status, seen = fallback_status, False
        scope = native_turns.for_task(self.store, task_id)
        processed = {row[0] for row in self.db.execute("SELECT source_seq FROM evidence_entries WHERE task_id=? AND turn_id=?", (task_id, turn_id))}
        methods = ("item/started", "item/completed", "turn/diff/updated", "item/fileChange/patchUpdated",
                   "bridge/event_gap", "bridge/protocol_error", "turn/completed")
        for row in self.db.execute("SELECT seq,kind,data FROM events WHERE task_id=? AND kind IN (" +
                                   ",".join("?" for _ in methods) + ") ORDER BY seq", (task_id, *methods)):
            try:
                original = json.loads(row["data"])
                native_turn = original.get("turn") or {}
                if native_turns.event_id(original, scope) != turn_id:
                    continue
                seen = True
                safe = redact(original)
                # Historical paths cannot capture the bytes that existed then.
                if row["seq"] not in processed:
                    observe(self.db, task_id, row["kind"], safe, row["seq"], capture_files=False,
                            redaction_count=int(safe != original))
                if row["kind"] == "turn/completed" and not native_turns.nested(native_turn, scope) and native_turn.get("status") in TERMINAL:
                    status = native_turn["status"]
            except (ValueError, TypeError, AttributeError, KeyError):
                continue  # Preserve unknown journal shapes without inventing facts.
        if not seen and status is None:
            raise BridgeError("No observed result for this task and turn", code="not_found")
        self.db.execute("INSERT INTO evidence_history VALUES (?,?,?)", (task_id, turn_id, status or "unknown"))

    def capture(self, task_id, turn_id, *, hydrate=False):
        if not turn_id:
            raise BridgeError("No observed result for this task and turn", code="not_found")
        turn = self.db.execute("SELECT * FROM turns WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone()
        if self.db.execute("SELECT 1 FROM result_expirations WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone():
            raise BridgeError("Turn result has expired under the operator retention policy", code="result_expired", ids={"task_id": task_id, "turn_id": turn_id})
        head = self.store.task_head(task_id)
        historical_limit = self.db.execute("SELECT historical_turn_ordinal FROM retention_state WHERE id=1").fetchone()[0]
        history = self.db.execute("SELECT status FROM evidence_history WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone()
        historical = turn is None or turn["ordinal"] <= historical_limit or history is not None
        if hydrate and historical and history is None:
            self.hydrate_history(task_id, turn_id, turn["status"] if turn else None)
            history = self.db.execute("SELECT status FROM evidence_history WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone()
        if not turn:
            if not history:
                raise BridgeError("No observed result for this task and turn", code="not_found")
            turn = {"thread_id": head.get("thread_id"), "status": history["status"]}
        previous = self.db.execute("SELECT * FROM turn_results WHERE task_id=? AND turn_id=? ORDER BY revision DESC LIMIT 1", (task_id, turn_id)).fetchone()
        old = json.loads(previous["data"]) if previous else {}
        current = head.get("turn_id") == turn_id
        gaps = list(head.get("evidence_gaps", [])) if current else old.get("evidence_completeness", {}).get("gaps", ["historical_evidence"])
        if historical and "historical_evidence" not in gaps:
            gaps = [*gaps, "historical_evidence"]
        prior_completeness = old.get("evidence_completeness", {})
        complete = not historical and (bool(head.get("evidence_complete")) if current else prior_completeness.get("events_complete", prior_completeness.get("complete", False)))
        value = {"format_version": RESULT_VERSION, "task_id": task_id, "thread_id": turn["thread_id"], "turn_id": turn_id,
                 "status": turn["status"], "evidence_completeness": {"complete": complete, "gaps": gaps},
                 **{key: [] for key in CATEGORIES}}
        for row in self.db.execute("SELECT category,data FROM evidence_entries WHERE task_id=? AND turn_id=? ORDER BY source_seq,category,item_key", (task_id, turn_id)):
            value[row["category"]].append(json.loads(row["data"]))
        known = {ref["artifact_id"] for ref in value["artifacts"]}
        for row in self.db.execute("SELECT id,kind,sha256,size_bytes,path,source_seq,redaction_count FROM artifacts WHERE task_id=? AND turn_id=? ORDER BY id", (task_id, turn_id)):
            if row["id"] not in known:
                value["artifacts"].append({"artifact_id": row["id"], "kind": row["kind"], "sha256": row["sha256"], "size_bytes": row["size_bytes"],
                    "content_redacted": bool(row["redaction_count"]), "redaction_count": row["redaction_count"],
                    "source": {"event_seq": row["source_seq"], "task_id": task_id, "thread_id": turn["thread_id"], "turn_id": turn_id}})
        def omitted(node):
            if isinstance(node, dict):
                return bool(node.get("content_omitted")) or any(omitted(v) for v in node.values())
            return isinstance(node, list) and any(omitted(v) for v in node)
        capture_gaps = []
        if omitted(value):
            capture_gaps.append("artifact_too_large")
        if any(entry.get("reason", "").startswith("File bytes were not captured") for entry in value["limitations"]):
            capture_gaps.append("file_bytes_unavailable")
        gaps = list(dict.fromkeys([*gaps, *capture_gaps]))
        value["evidence_completeness"] = {"complete": complete and not gaps, "gaps": gaps,
            "events_complete": complete, "artifacts_complete": not capture_gaps}
        value["limitations"].append({"reason": "Agent claims, native file reports and command exit codes are evidence, not verification of correctness"})
        if gaps:
            value["limitations"].append({"reason": "Evidence may be incomplete", "gaps": gaps})
        digest = hashlib.sha256(canonical(value)).hexdigest()
        if previous and previous["sha256"] == digest:
            return {**value, "result_id": previous["id"], "revision": previous["revision"], "sha256": digest}
        revision = previous["revision"] + 1 if previous else 1
        result_id = str(uuid.uuid4())
        self.db.execute("INSERT INTO turn_results VALUES (?,?,?,?,?,?,?)", (result_id, task_id, turn_id, revision, time.time(), digest, json.dumps(value)))
        return {**value, "result_id": result_id, "revision": revision, "sha256": digest}

    def result(self, task_id, turn_id, offset=0, limit=50, result_id=None):
        if type(offset) is not int or offset < 0:
            raise BridgeError("Result offset must be a nonnegative integer", code="invalid_arguments")
        if not 1 <= limit <= 200:
            raise BridgeError("codex_get view=result limit must be between 1 and 200", code="invalid_arguments")
        with self.store.transaction():
            if result_id is None:
                value = self.capture(task_id, turn_id, hydrate=True)
            else:
                row = self.db.execute("SELECT * FROM turn_results WHERE id=? AND task_id=?", (result_id, task_id)).fetchone()
                if not row or (turn_id and row["turn_id"] != turn_id):
                    raise BridgeError("Result version is not retained for this task and turn", code="not_found")
                value = {**json.loads(row["data"]), "result_id": row["id"], "revision": row["revision"], "sha256": row["sha256"]}
        # One position pages the combined categories; nothing is silently lost.
        entries = [(key, entry) for key in CATEGORIES for entry in value[key]]
        page = entries[offset:offset + limit]
        from .tools import result_size
        while True:
            result = {**value, **{key: [v for k, v in page if k == key] for key in CATEGORIES},
                      "counts": {key: len(value[key]) for key in CATEGORIES},
                      "next_offset": offset + len(page) if offset + len(page) < len(entries) else None}
            if result_size({"result": result}) <= 2 * 1024 * 1024:
                return result
            if len(page) <= 1:
                raise BridgeError("Result entry exceeds the response budget", code="result_too_large")
            page.pop()

    def read(self, task, artifact_id, offset=0, limit=READ_BYTES):
        if type(offset) is not int or offset < 0:
            raise BridgeError("Artifact offset must be a nonnegative integer", code="invalid_arguments")
        if not 1 <= limit <= MAX_READ_BYTES:
            raise BridgeError("Artifact limit must be between 1 and 262144 bytes", code="invalid_arguments")
        row = self.db.execute("SELECT * FROM artifacts WHERE id=? AND task_id=?", (artifact_id, task["id"])).fetchone()
        if not row:
            raise BridgeError("Artifact is not retained for this task", code="artifact_not_found", ids={"artifact_id": artifact_id, "task_id": task["id"]})
        if row["kind"] == "live_file":
            try:
                data = read_project_file(task["project"], row["path"], ARTIFACT_BYTES)
            except (OSError, BridgeError) as exc:
                raise BridgeError("Live file no longer matches the captured artifact version", code="artifact_version_mismatch", ids={"artifact_id": artifact_id}) from exc
            if hashlib.sha256(data).hexdigest() != row["file_version"]:
                raise BridgeError("Live file no longer matches the captured artifact version", code="artifact_version_mismatch", ids={"artifact_id": artifact_id})
            data, _ = file_bytes(data)
        elif row["source_locator"] is not None:
            data = self.journal_bytes(row)
        else:
            data = bytes(row["content"])
        if hashlib.sha256(data).hexdigest() != row["sha256"] or len(data) != row["size_bytes"]:
            raise BridgeError("Live file no longer matches the captured artifact version" if row["kind"] == "live_file" else "Captured artifact failed its integrity check",
                              code="artifact_version_mismatch", ids={"artifact_id": artifact_id})
        if offset < 0 or offset > len(data):
            raise BridgeError("Artifact offset is beyond its bytes", code="invalid_arguments")
        end = min(len(data), offset + limit)
        return {"artifact_id": artifact_id, "task_id": task["id"], "turn_id": row["turn_id"], "kind": row["kind"],
                "sha256": row["sha256"], "size_bytes": len(data), "encoding": "base64", "offset": offset,
                "content_redacted": bool(row["redaction_count"]), "redaction_count": row["redaction_count"],
                "data": base64.b64encode(data[offset:end]).decode("ascii"), "next_offset": end if end < len(data) else None}

    def journal_bytes(self, artifact):
        event = self.db.execute("SELECT task_id,kind,data FROM events WHERE seq=?", (artifact["source_seq"],)).fetchone()
        ids = {"artifact_id": artifact["id"], "event_seq": artifact["source_seq"]}
        if not event:
            raise BridgeError("Journal artifact source event is no longer available", code="artifact_version_mismatch", ids=ids)
        try:
            locator = json.loads(artifact["source_locator"])
            params = redact(json.loads(event["data"]))
            turn_id = native_turns.event_id(params, native_turns.for_task(self.store, artifact["task_id"]))
            if (locator["format"] != 1 or locator["method"] != event["kind"] or event["task_id"] != artifact["task_id"]
                    or turn_id != artifact["turn_id"] or not isinstance(locator["path"], list)):
                raise ValueError
            category, value = reported_value(event["kind"], params)
            if category is None:
                raise ValueError
            for key in locator["path"]:
                if not ((isinstance(value, dict) and isinstance(key, str)) or
                        (isinstance(value, list) and type(key) is int and key >= 0)):
                    raise ValueError
                value = value[key]
            if locator["encoding"] == "json":
                data = canonical(value)
            elif locator["encoding"] == "utf-8" and isinstance(value, str):
                data = value.encode("utf-8")
            else:
                raise ValueError
            if len(data) != artifact["size_bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                raise ValueError
            return data
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError) as exc:
            raise BridgeError("Journal artifact source no longer matches its captured version", code="artifact_version_mismatch", ids=ids) from exc
