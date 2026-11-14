"""Remember process uncertainty across native backend generations, never scan OS processes."""

import json
import hashlib
import sqlite3
from time import monotonic

from .security import BridgeError, redact
from .lifecycle import TERMINAL

REASON = "processes_unverified"
MESSAGE = ("Commands started before the restart may still be running in this project. "
           "Codex can no longer see or stop them. Check the system process list, stop what remains, then call codex_resume")
SCAN_ROWS = 100000
SCAN_SECONDS = 2


def archived_guidance(task):
    return (f"For archived task {task['id']}, use codex_archive action=unarchive, then codex_resume; "
            f"or use codex_delete with confirm_thread_id={task['thread_id']}")


def warning(task):
    return MESSAGE + (". " + archived_guidance(task) if task.get("archived") else "")


def pending(task):
    state = task.get("process_state") or {}
    acknowledged = (state.get("acknowledged") and "acknowledged_turn_status" in state
        and state.get("acknowledged_turn_status") in (*TERMINAL, "inProgress", None)
        and type(state.get("acknowledged_event_seq")) is int and state["acknowledged_event_seq"] >= 0)
    return bool(state.get("previous_backend_unverified") and not acknowledged)


def relevant(task):
    state = task.get("process_state") or {}
    return bool(state.get("previous_backend_unverified") and
        (pending(task) or state.get("acknowledged_turn_id") == task.get("turn_id")))


def preview(values):
    values = [redact(value) for value in values if isinstance(value, str) and value]
    return list(dict.fromkeys(value[:256] for value in values))[:5], len(values) > 5 or any(len(value) > 256 for value in values)


def observe_terminals(service, task_id, entries, generation, token, *, complete=False):
    task = service.store.task_head(task_id)
    if task.get("deleted") or (not entries and not complete) or token < service.terminal_observations.get(task_id, 0):
        return False
    service.terminal_observations[task_id] = token
    if not entries and not task.get("background_observation"):
        return False  # No prior positive evidence needs superseding on disk.
    service.invalidate_process_check(task["project"])
    commands, truncated = preview([entry.get("command") for entry in entries if isinstance(entry, dict)])
    data = {"source": "native_terminal_observation", "commands_preview": commands, "commands_truncated": truncated}
    observed = {"active": bool(entries), "commands_preview": commands, "commands_truncated": truncated}
    observed["sha256"] = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
    with service.store.transaction():
        # Newest positive evidence owns this field, even if an unrelated claim
        # update happened during the read. Never release from this list alone.
        applied = bool(entries) and service.store.claim(task_id, "background", "active", data, if_changed=True)
        if (task.get("background_observation") or {}).get("sha256") != observed["sha256"]:
            service.lifecycle.metadata(task_id, {"background_observation": observed})
    if applied and (not service.backend.alive or generation != service.backend.generation):
        capture(service, service.store.task_head(task_id))
    return applied


def scan(db, task_id, turn_id, after=0):
    """Read only command identity/status, with a deadline and a row bound."""
    deadline, reason, commands = monotonic() + SCAN_SECONDS, "complete", {}
    def expired():
        return int(monotonic() >= deadline)
    db.set_progress_handler(expired, 1000)
    try:
        rows = db.execute("""WITH scoped AS (
            SELECT seq,kind,data body
            FROM events WHERE task_id=? AND kind IN ('item/started','item/completed'))
            SELECT seq,kind,json_extract(body,'$.turnId') turn_id,
                json_type(body,'$.item') item_shape,
                CASE WHEN json_type(body,'$.item.type')='text' AND LENGTH(json_extract(body,'$.item.type'))<=64
                    THEN json_extract(body,'$.item.type') END item_type,
                CASE WHEN json_type(body,'$.item.id')='text' AND LENGTH(json_extract(body,'$.item.id'))<=1024
                    THEN json_extract(body,'$.item.id') END item_id,
                CASE WHEN json_type(body,'$.item.status')='text' AND LENGTH(json_extract(body,'$.item.status'))<=16
                    THEN json_extract(body,'$.item.status') END status,
                CASE WHEN json_type(body,'$.item.command')='text' AND LENGTH(json_extract(body,'$.item.command'))<=4096
                    THEN json_extract(body,'$.item.command') END command
            FROM scoped WHERE json_extract(body,'$.turnId')=? OR json_type(body,'$.turnId') IS NOT 'text'
            ORDER BY seq DESC LIMIT ?""", (task_id, turn_id, SCAN_ROWS + 1))
        for number, row in enumerate(rows):
            if number == SCAN_ROWS:
                reason = "row_limit"
                break
            if expired():
                reason = "time_limit"
                break
            if row["turn_id"] != turn_id or row["item_shape"] != "object" or not isinstance(row["item_type"], str):
                reason = "invalid_event"
                continue
            if row["item_type"] != "commandExecution":
                continue
            if not row["item_id"] or row["status"] not in ("inProgress", "completed", "failed", "declined"):
                reason = "invalid_event"
                continue
            entry = commands.setdefault(row["item_id"], {"first": row["seq"], "finished": False, "command": row["command"]})
            entry["first"] = min(entry["first"], row["seq"])
            entry["finished"] |= row["status"] != "inProgress"
            if entry["command"] is None:
                entry["command"] = row["command"]
    except sqlite3.DatabaseError:
        reason = "time_limit" if expired() else "invalid_event"
    finally:
        db.set_progress_handler(None, 0)
    unfinished = [v for v in commands.values() if not v["finished"] and v["first"] > after]
    previews, truncated = preview([v["command"] for v in unfinished if v["command"] is not None])
    return {"complete": reason == "complete", "unfinished": len(unfinished) if reason == "complete" else None,
            "reason": reason}, previews, truncated or any(v["command"] is None for v in unfinished)


def command_evidence(service, task, covered):
    store, task_id = service.store, task["id"]
    event_seq = store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=? AND kind IN ('item/started','item/completed')", (task_id,)).fetchone()[0]
    cached = task.get("process_scan")
    key = {"version": 1, "turn_id": task.get("turn_id"), "event_seq": event_seq}
    after = (task.get("process_state") or {}).get("acknowledged_event_seq", 0) if covered else 0
    if isinstance(cached, dict) and all(cached.get(k) == v for k, v in key.items()):
        return cached, [], False
    if covered and event_seq <= after:
        result, commands, truncated = {"complete": True, "unfinished": 0, "reason": "acknowledged"}, [], False
    else:
        result, commands, truncated = scan(store.db, task_id, task.get("turn_id"), after)
    result = {**key, **result}
    service.lifecycle.metadata(task_id, {"process_scan": result})
    return result, commands, truncated


def capture(service, task):
    if task.get("deleted"):
        return
    if pending(task):
        state = task.get("process_state") or {}
        if state.get("acknowledged"):
            service.lifecycle.metadata(task["id"], {"process_state": {**service.store.get("tasks", task["id"])["process_state"], "acknowledged": False}})
        return
    orphan = next((c for c in service.store.claims(task_id=task["id"]) if c["data"].get("reason") == REASON), None)
    if orphan and not (task.get("process_state") or {}).get("previous_backend_unverified"):
        hold(service, task["id"], {"previous_backend_unverified": True, "acknowledged": False,
            "commands_preview": orphan["data"].get("commands_preview", []), "commands_truncated": orphan["data"].get("commands_truncated", False)})
        return
    state = task.get("process_state") or {}
    covered = (state.get("acknowledged") and state.get("acknowledged_turn_id") == task.get("turn_id")
        and state.get("acknowledged_turn_status") in TERMINAL | {None})
    claims = service.store.claims(task_id=task["id"])
    observation = service.native_observations.get(task["id"], {})
    background = task.get("background_observation") or {}
    terminals_empty = background.get("active") is False
    inferred_progress = task["status"] in TERMINAL | {"idle", "archived"} and "legacy_state" in task.get("evidence_gaps", [])
    live = (bool(task.get("turn_start")) or not covered and
        (task["status"] in ("running", "starting", "interrupting") or (task.get("turn_status") == "inProgress" and not inferred_progress))
        or any(c["kind"] in ("turn", "background") and c["state"] == "active" and not
            ((c["kind"] == "turn" and covered and c["data"].get("turn_id") == task.get("turn_id")) or
             (c["kind"] == "background" and (c["data"].get("source") == "os_process_scan" and covered or
                c["data"].get("source") == "native_terminal_observation" and terminals_empty))) for c in claims)
        or background.get("active") is True or bool(observation.get("current_backend_background_terminals") and not terminals_empty))
    commands, truncated = [], False
    if background.get("active"):
        saved = service.store.get("tasks", task["id"])["background_observation"]
        commands.extend(saved.get("commands_preview", []))
        truncated |= bool(saved.get("commands_truncated"))
    for claim in claims:
        if claim["kind"] == "background" and terminals_empty:
            continue
        commands.extend(claim["data"].get("commands_preview", []))
        truncated |= bool(claim["data"].get("commands_truncated"))
    evidence, observed, shortened = command_evidence(service, task, covered)
    commands.extend(observed)
    truncated |= shortened or not evidence["complete"]
    # Older layouts have no process claims. An unfinished command in their
    # journal is positive evidence too, unless this generation observed idle.
    checked_idle = (observation.get("generation") == service.backend.generation
        and observation.get("thread_status") == "idle" and observation.get("background_terminals_checked")
        and observation.get("current_backend_background_terminals") == 0)
    if not live and evidence["complete"] and (not evidence["unfinished"] or (not covered and checked_idle)):
        return
    commands, shortened = preview(commands)
    state = {"previous_backend_unverified": True, "acknowledged": False,
        "commands_preview": commands, "commands_truncated": truncated or shortened}
    with service.store.transaction():
        service.lifecycle.metadata(task["id"], {"process_state": state})
        hold(service, task["id"], state)


def hold(service, task_id, state=None):
    task = service.store.task_head(task_id)
    if task.get("deleted"):
        return
    if state is None:
        current = service.claim_snapshot(task_id, "uncertain")
        state = current["data"] if current and current["data"].get("reason") == REASON else service.store.get("tasks", task_id).get("process_state", {})
    with service.store.transaction():
        if not (task.get("process_state") or {}).get("previous_backend_unverified"):
            commands, truncated = preview(state.get("commands_preview", []))
            state = {"previous_backend_unverified": True, "acknowledged": False,
                "commands_preview": commands, "commands_truncated": truncated or bool(state.get("commands_truncated"))}
            service.lifecycle.metadata(task_id, {"process_state": state})
        service.store.claim(task_id, "uncertain", "uncertain", {"reason": REASON, "error": warning(task),
            "commands_preview": state.get("commands_preview", []), "commands_truncated": state.get("commands_truncated", False)}, if_changed=True)


def require_acknowledgement(task):
    if pending(task):
        raise BridgeError(warning(task), code="state_conflict", ids={"task_id": task["id"], "thread_id": task.get("thread_id")})


def acknowledge(service, task_id):
    task = service.store.task_head(task_id)
    if (task.get("process_state") or {}).get("previous_backend_unverified"):
        state = service.store.get("tasks", task_id)["process_state"]
        event_seq = service.store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        service.lifecycle.metadata(task_id, {"process_scan": None, "process_state": {**state, "acknowledged": True,
            "acknowledged_turn_id": task.get("turn_id"), "acknowledged_turn_status": task.get("turn_status"),
            "acknowledged_event_seq": event_seq}})


def deletion_state(store, task, *, requested):
    from .tools import bounded_result
    state = task.get("process_state")
    if not state or not state.get("previous_backend_unverified"):
        claim = next((c for c in store.claims(task_id=task["id"]) if c["data"].get("reason") == REASON), None)
        if not claim:
            return state
        state = {"previous_backend_unverified": True, "acknowledged": False,
            "commands_preview": claim["data"].get("commands_preview", []), "commands_truncated": claim["data"].get("commands_truncated", False)}
    commands, truncated = preview(state.get("commands_preview", []))
    state = {**state, "commands_preview": commands, "commands_truncated": truncated or bool(state.get("commands_truncated"))}
    if state.get("deletion_outcome") == "confirmed_delete":
        return bounded_result(redact(state), 4096)
    if requested:
        state.update(acknowledged=True, acknowledged_turn_id=task.get("turn_id"),
            acknowledged_turn_status=task.get("turn_status"),
            acknowledged_event_seq=store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=?", (task["id"],)).fetchone()[0],
            deletion_outcome="confirmed_delete")
    else:
        state.update(deletion_outcome="native_deleted_unacknowledged" if pending({**task, "process_state": state}) else "native_deleted")
    return bounded_result(redact(state), 4096)
