"""Explicit recovery observations. An acknowledgement is not completed work."""

import asyncio
import hashlib
import json
import time

from .evidence import canonical
from .lifecycle import TERMINAL
from .security import BridgeError, redact
from .supervision import touch
from . import processes, workspace_processes, native_turns
from .tools import bounded_result

JOURNAL_LIMIT = 150
ITEM_LIMIT = 100
OPERATION_LIMIT = 20
OPERATION_ROW_BYTES = 65536
OPERATION_BYTES = 256 * 1024
RECOVERY_SECONDS = 5


def operation_outcome(store, key, acknowledgement, error_code=None):
    row = store.db.execute("SELECT data FROM operation_details WHERE id=?", (key,)).fetchone()
    if row:
        details = {**json.loads(row[0]), "operation_outcome": {"acknowledgement": acknowledgement, "error_code": error_code}}
        store.db.execute("UPDATE operation_details SET data=? WHERE id=?", (json.dumps(details), key))


def projection_item(item):
    kind = item.get("type")
    fields = {"commandExecution": ("id", "type", "command", "cwd", "status", "exitCode", "durationMs"),
              "agentMessage": ("id", "type", "text"), "fileChange": ("id", "type", "status")}.get(kind)
    if fields is None or not isinstance(item.get("id"), str):
        return None
    result = {k: item[k] for k in fields if k in item}
    if kind == "agentMessage":
        text = result.get("text", "")
        result.update(text=text[:16384], text_truncated=len(text) > 16384)
    if kind == "fileChange":
        changes = item.get("changes") or []
        result["changes"] = [{k: c[k] for k in ("path", "kind") if k in c} for c in changes[:200] if isinstance(c, dict)]
        result["changes_truncated"] = len(changes) > 200
    result["projection_only"] = True
    return redact(result)


def journal(service, task_id, turn_id):
    result, omitted, size = [], False, 0
    scope = native_turns.for_task(service.store, task_id)
    rows = service.store.db.execute("SELECT seq,kind,LENGTH(CAST(data AS BLOB)) bytes FROM events WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task_id, JOURNAL_LIMIT + 1)).fetchall()
    for row in rows[:JOURNAL_LIMIT]:
        if row["bytes"] > 65536 or size + row["bytes"] > 2 * 1024 * 1024:
            omitted = True
            continue
        size += row["bytes"]
        data = json.loads(service.store.db.execute("SELECT data FROM events WHERE seq=?", (row["seq"],)).fetchone()[0])
        if not isinstance(data, dict) or native_turns.event_id(data, scope) != turn_id:
            continue
        result.append({"event_seq": row["seq"], "method": row["kind"], "data": data})
    return list(reversed(result)), omitted or len(rows) > JOURNAL_LIMIT


def operations(service, task_id):
    rows = service.store.db.execute("""SELECT o.id,o.state,o.task_id,d.name,d.created_at,
        LENGTH(CAST(COALESCE(o.result,'') AS BLOB)) result_bytes,LENGTH(CAST(COALESCE(d.data,'') AS BLOB)) description_bytes FROM operations o
        LEFT JOIN operation_details d ON d.id=o.id WHERE o.id IN
        (SELECT id FROM operations WHERE task_id=? UNION SELECT id FROM operation_details WHERE task_id=?)
        ORDER BY o.rowid DESC LIMIT ?""",
        (task_id, task_id, OPERATION_LIMIT + 1)).fetchall()
    result, remaining = [], OPERATION_BYTES
    def payload(table, column, key, size):
        nonlocal remaining
        if size > min(OPERATION_ROW_BYTES, remaining):
            return {}, True
        remaining -= size
        value = service.store.db.execute(f"SELECT {column} FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(value[0]) if value and value[0] else {}, False
    for row in rows[:OPERATION_LIMIT]:
        if row["id"] == service.current_operation.get():
            continue
        details, requested_omitted = payload("operation_details", "data", row["id"], row["description_bytes"])
        stored, cached_omitted = payload("operations", "result", row["id"], row["result_bytes"])
        turn = details.pop("acknowledged_turn", None)
        outcome = details.pop("operation_outcome", {})
        error = stored.get("error_data") or {}
        acknowledgement = outcome.get("acknowledgement") or ("acknowledged" if row["state"] == "done" and not cached_omitted else "pending" if row["state"] == "running" else "unknown")
        if stored.get("operation_rejected") is True:
            acknowledgement = "rejected"
        elif turn:
            acknowledgement = "acknowledged"
        elif (row["state"] != "done" and error.get("uncertain") is False
                and error.get("code") in ("native_rejected", "optional_native_capability_unavailable")):
            acknowledgement = "rejected"
        result.append({"operation_id": row["id"], "tool": row["name"], "state": row["state"],
            "requested": details, "acknowledgement": acknowledgement,
            "acknowledgement_proves_completion": False,
            "acknowledged_turn_id": turn.get("turn_id") if turn else stored.get("turn_id"),
            "acknowledged_task_id": turn.get("task_id") if turn else stored.get("id", row["task_id"]),
            "acknowledged_thread_id": turn.get("thread_id") if turn else stored.get("thread_id"),
            "turn_link_source": "native_acknowledgement" if turn else "cached_acknowledgement" if stored.get("turn_id") else None,
            "cached_result_omitted": cached_omitted, "requested_omitted": requested_omitted,
            "result_bytes": row["result_bytes"], "error_code": error.get("code", outcome.get("error_code")),
            "source": "operation_record", "automatic_retry": False})
    return result, len(rows) > OPERATION_LIMIT


def report(service, task_id):
    task = service.task(task_id, allow_deleted=True, light=True)
    row = service.store.db.execute("SELECT * FROM reconciliation_reports WHERE task_id=?", (task_id,)).fetchone()
    previous = json.loads(row["data"]) if row else {}
    ops, more = operations(service, task_id)
    unknown = list(previous.get("unknown", []))
    native = previous.get("native")
    if not native:
        unknown.append("native_state_not_checked")
    elif native.get("generation") != service.backend.generation or not service.backend.alive or task_id not in service.loaded:
        unknown.append("native_connection_changed")
        native = {**native, "background_terminals": None, "background_terminals_checked": False,
            "current_backend_background_terminals": None}
    stale = bool(row and row["task_revision"] != task["revision"])
    if stale:
        unknown.append("task_changed_since_report")
    claims = service.store.claims(family_id=task.get("root_task_id", task_id), limit=100)
    if service.store.db.execute("SELECT 1 FROM resource_interrupts WHERE family_id=? AND state!='acknowledged' LIMIT 1", (service.resources.family(task_id),)).fetchone():
        unknown.append("budget_interrupt_unverified")
    process_claims = [c for c in claims if c["data"].get("reason") == processes.REASON]
    if processes.relevant(task) or process_claims:
        unknown.append(processes.REASON)
        if native:
            native = {**native, "background_terminals": None, "background_terminals_checked": False}
    if any(c["state"] == "uncertain" for c in claims):
        unknown.append("execution_intent_uncertain")
    if any(o["acknowledgement"] in ("unknown", "pending") for o in ops):
        unknown.append("operation_acknowledgement_unknown")
    for op in ops:
        args = op["requested"].get("arguments", {})
        execution = op["tool"] in ("codex_start", "codex_send", "codex_delegate", "codex_review", "codex_compact")
        linked = op["turn_link_source"] == "native_acknowledgement" or (execution and args.get("mode") != "steer"
            and not (op["tool"] == "codex_delegate" and args.get("expected_turn_id")))
        op["effect_observed"] = bool(native and any(q.get("clientUserMessageId") == op["operation_id"] for q in native.get("queue", [])))
        if op["effect_observed"]:
            op["effect_source"] = "native_queue"
        elif (linked and op["acknowledgement"] == "acknowledged" and op["acknowledged_turn_id"] and op["acknowledged_thread_id"]):
            observed = service.store.db.execute("SELECT status FROM turns WHERE task_id=? AND thread_id=? AND turn_id=?",
                (op["acknowledged_task_id"], op["acknowledged_thread_id"], op["acknowledged_turn_id"])).fetchone()
            if observed and observed[0] in TERMINAL | {"inProgress"}:
                op.update(effect_observed=True, effect_source="turn_record", effect_turn_status=observed[0])
        op["effect_observation"] = "observed" if op["effect_observed"] else "not_established"
        if not op["effect_observed"]:
            op["effect_observation_note"] = "No linked native effect has been established; this does not mean no work happened"
    if any(op["cached_result_omitted"] or op["requested_omitted"] for op in ops):
        unknown.append("operation_record_budget")
    expired = service.store.db.execute("SELECT COUNT(*) FROM requests WHERE task_id=? AND status='expired'", (task_id,)).fetchone()[0]
    actions = [{"tool": "codex_events" if task.get("deleted") else "codex_history", "arguments": {"task_id": task_id},
        "reason": "Inspect the retained audit journal" if task.get("deleted") else "Inspect retained native history before new work"}]
    if not task.get("deleted") and not task.get("archived"):
        actions.append({"tool": "codex_resume", "arguments": {"task_id": task_id}, "requires_new_operation_id": True,
                        "reason": "Re-check and rebuild available evidence; no prompt is replayed"})
    for claim in claims:
        owner = service.store.task_head(claim["task_id"])
        if workspace_processes.held(claim):
            actions.append({"action": "stop_processes", "task_id": owner["id"], "pids": claim["data"]["pids"],
                "reason": workspace_processes.message(claim)})
        elif claim["data"].get("reason") == "stored_state_unavailable":
            actions.append({"tool": "codex_delete", "arguments": {"task_id": owner["id"], "confirm_thread_id": owner["thread_id"]},
                "requires_new_operation_id": True, "reason": "Explicitly delete the unavailable conversation to free its project; its audit is retained"})
        elif claim["data"].get("reason") == processes.REASON:
            if owner.get("archived"):
                actions += [{"tool": "codex_archive", "arguments": {"task_id": owner["id"], "action": "unarchive"},
                    "requires_new_operation_id": True, "reason": processes.archived_guidance(owner)},
                    {"tool": "codex_delete", "arguments": {"task_id": owner["id"], "confirm_thread_id": owner["thread_id"]},
                    "requires_new_operation_id": True, "reason": "Explicitly delete this archived conversation to free its project; its audit is retained"}]
            actions.append({"tool": "codex_resume", "arguments": {"task_id": owner["id"]}, "requires_new_operation_id": True,
                "reason": processes.warning(owner)})
    return {"format_version": 1, "task_id": task_id, "thread_id": task.get("thread_id"), "turn_id": task.get("turn_id"),
        "status": task["status"], "checked_at": row["created_at"] if row else None,
        "report_revision": row["revision"] if row else 0, "report_stale": stale,
        "native": native, "operations": [bounded_result(o, 8192) for o in ops], "operations_has_more": more,
        "workspace_claims": claims, "expired_requests": expired,
        "projection_sources": previous.get("projection_sources", {}), "rebuilt": previous.get("rebuilt", False),
        "unknown": list(dict.fromkeys([*unknown, *task.get("evidence_gaps", [])])),
        "evidence_complete": bool(task.get("evidence_complete")), "process_state": service.process_view(task),
        "os_process_check": service.process_check_view(task),
        "family_usage": service.resources.usage(task_id), "budget": service.resources.policy(service.resources.family(task_id))[0],
        "budget_stop": service.resources.policy(service.resources.family(task_id))[1],
        "next_steps": actions, "automatic_retry": False, "replay_permitted": False}


async def reconcile(service, task_id, *, failure=None):
    head = service.task(task_id, allow_deleted=True, light=True)
    generation = service.backend.generation
    unknown, native_items, partial = [], [], False
    if failure is not None:
        unknown.append("resume_failed:" + getattr(failure, "code", type(failure).__name__))
    elif head.get("turn_id") and head.get("verified_generation") == generation and service.backend.alive:
        try:
            async with asyncio.timeout(RECOVERY_SECONDS):
                page = await service.backend.call("thread/items/list", {"threadId": head["thread_id"], "turnId": head["turn_id"],
                    "cursor": None, "limit": ITEM_LIMIT, "sortDirection": "asc"})
            partial = bool(page.get("nextCursor")) or len(page.get("data", [])) > ITEM_LIMIT
            for entry in page.get("data", [])[:ITEM_LIMIT]:
                if entry.get("turnId") not in (None, head["turn_id"]):
                    partial = True
                    continue
                item = projection_item(entry.get("item", entry))
                if item:
                    native_items.append(item)
        except (BridgeError, OSError, TimeoutError, KeyError, TypeError, AttributeError) as exc:
            unknown.append("native_items_unavailable:" + getattr(exc, "code", type(exc).__name__))
    sources, fields, rebuilt = {}, {}, False
    native_supplied = False
    with service.store.transaction():
        current = service.task(task_id, allow_deleted=True)
        if current["revision"] != head["revision"] or generation != service.backend.generation:
            unknown.append("concurrent_update")
        elif failure is None and current.get("turn_id"):
            entries, journal_partial = journal(service, task_id, current["turn_id"])
            if journal_partial:
                unknown.append("journal_window_incomplete")
            known = {}
            for entry in entries:
                if entry["method"] in ("item/started", "item/completed", "bridge/recovered_item"):
                    item = entry["data"].get("item") or {}
                    kind = item.get("type")
                    if kind in ("commandExecution", "fileChange", "agentMessage") and item.get("id"):
                        previous = known.get(item["id"])
                        if not previous or entry["method"] != "item/started" or (previous[1].get("method") != "item/completed" and previous[0].get("status") not in ("completed", "failed", "declined")):
                            known[item["id"]] = (item, {"source": "native_history" if entry["method"] == "bridge/recovered_item" else "journal",
                                "event_seq": entry["event_seq"], "turn_id": current["turn_id"], "method": entry["method"]})
                elif entry["method"] == "turn/diff/updated":
                    fields.update(diff=entry["data"].get("diff", ""), diff_turn_id=current["turn_id"])
                    sources["diff"] = {"source": "journal", "event_seq": entry["event_seq"], "turn_id": current["turn_id"]}
                elif entry["method"] == "turn/plan/updated":
                    fields["plan"] = entry["data"].get("plan", [])
                    sources["plan"] = {"source": "journal", "event_seq": entry["event_seq"], "turn_id": current["turn_id"]}
            for item in native_items:
                previous = known.get(item["id"])
                if previous and previous[1].get("method") == "item/completed":
                    if projection_item(previous[0]) != item:
                        unknown.append("journal_native_disagreement")
                    continue
                if previous and item["type"] == "commandExecution" and (previous[0].get("exitCode") is not None or previous[0].get("status") in ("completed", "failed", "declined")):
                    if item.get("exitCode") not in (None, previous[0].get("exitCode")):
                        unknown.append("journal_native_disagreement")
                    continue
                digest = hashlib.sha256(canonical(item)).hexdigest()
                cached = service.store.db.execute("SELECT sha256,source_seq FROM recovered_items WHERE task_id=? AND turn_id=? AND item_id=?",
                    (task_id, current["turn_id"], item["id"])).fetchone()
                seq = cached["source_seq"] if cached and cached["sha256"] == digest and service.store.db.execute("SELECT 1 FROM events WHERE seq=?", (cached["source_seq"],)).fetchone() else None
                if seq is None:
                    seq = service.store.event(task_id, "bridge/recovered_item", {"threadId": current["thread_id"], "turnId": current["turn_id"],
                        "item": item, "source": "native_history", "observed_at": time.time()})
                    service.store.db.execute("INSERT OR REPLACE INTO recovered_items VALUES (?,?,?,?,?)", (task_id, current["turn_id"], item["id"], digest, seq))
                if not previous or previous[1]["source"] != "journal" or previous[0].get("status") not in ("completed", "failed", "declined"):
                    known[item["id"]] = (item, {"source": "native_history", "event_seq": seq, "turn_id": current["turn_id"]})
                    native_supplied = True
            commands, files = {c.get("id"): c for c in current.get("commands", [])}, set(current.get("files", []))
            command_sources, file_sources = [], []
            summaries = []
            for item, source in known.values():
                if item["type"] == "commandExecution":
                    previous = commands.get(item["id"], {})
                    if previous.get("status") in ("completed", "failed", "declined") and item.get("status") not in ("completed", "failed", "declined"):
                        unknown.append("retained_native_disagreement")
                        continue
                    commands[item["id"]] = {k: item[k] for k in ("id", "command", "cwd", "status", "exitCode", "durationMs") if k in item}
                    command_sources.append(source)
                elif item["type"] == "fileChange":
                    files.update(c["path"] for c in item.get("changes", []) if isinstance(c, dict) and isinstance(c.get("path"), str))
                    file_sources.append(source)
                elif item["type"] == "agentMessage" and item.get("text"):
                    summaries.append((item, source))
                    if item.get("text_truncated"):
                        unknown.append("summary_truncated")
            if summaries:
                observed = [entry for entry in summaries if entry[1]["source"] == "journal"]
                item, source = (observed or summaries)[-1]
                fields["summary"], sources["summary"] = item["text"], source
            fields.update(commands=list(commands.values())[-100:], files=sorted(files)[:200])
            if len(commands) > 100 or len(files) > 200 or partial:
                unknown.append("native_projection_incomplete")
            sources.update(commands=command_sources[:100], files=file_sources[:100])
            fields["projection_sources"] = sources
            service.lifecycle.project(task_id, current["turn_id"], fields)
            if native_supplied or unknown or not current.get("evidence_complete"):
                service.lifecycle.gap(task_id, "reconciliation_partial")
            rebuilt = True
        else:
            unknown.append("projection_not_rebuilt")
        latest = service.store.task_head(task_id)
        data = {"native": service.native_observations.get(task_id), "unknown": unknown,
                "projection_sources": sources, "rebuilt": rebuilt}
        row = service.store.db.execute("SELECT revision FROM reconciliation_reports WHERE task_id=?", (task_id,)).fetchone()
        service.store.db.execute("INSERT OR REPLACE INTO reconciliation_reports VALUES (?,?,?,?,?)",
            (task_id, row[0] + 1 if row else 1, latest["revision"], time.time(), json.dumps(redact(data))))
        service.store.event(task_id, "bridge/reconciliation", {"turnId": latest.get("turn_id"), "rebuilt": rebuilt,
            "unknown": unknown, "projection_sources": sources, "automatic_retry": False})
        touch(service.store, task_id)
    return report(service, task_id)
