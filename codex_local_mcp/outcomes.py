"""Bounded state observation; outcome cursors do not consume detailed replay."""

import asyncio
import base64
import hashlib
import hmac
import json

from .lifecycle import TERMINAL
from .security import BridgeError
from .tools import result_size, REQUEST_PAGE_BUDGET

TASK_LIMIT = 50
OUTCOME_BUDGET = 2 * 1024 * 1024


def cursor(identity, task_id, descendants, seq, claims):
    body = json.dumps([task_id, descendants, seq, identity[1], claims], separators=(",", ":")).encode()
    signature = hmac.new(identity[0].encode(), body, hashlib.sha256).hexdigest()
    return "outcome:" + base64.urlsafe_b64encode(body).decode() + "." + signature


def previous(token, identity, task_id, descendants, current):
    if token is None:
        return None
    try:
        if not token.startswith("outcome:") or len(token) > 2048:
            raise ValueError
        encoded, signature = token[8:].split(".")
        body = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if not hmac.compare_digest(signature, hmac.new(identity[0].encode(), body, hashlib.sha256).hexdigest()):
            raise ValueError
        values = json.loads(body)
        if (len(values) not in (4, 5) or values[:2] != [task_id, descendants] or values[3] != identity[1]
                or type(values[2]) is not int or not 0 <= values[2] <= current
                or (len(values) == 5 and (not isinstance(values[4], str) or len(values[4]) != 64
                    or any(c not in "0123456789abcdef" for c in values[4])))):
            raise ValueError
        return values[2], values[4] if len(values) == 5 else None
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("Outcome cursor is invalid or belongs to another task, scope or database; restart without outcome cursors",
                          code="cursor_expired", details={"cursor_kind": "outcome"},
                          next_steps=["Restart codex_events view=outcome without cursors"], ids={"task_id": task_id}) from None


def request_page(service, heads, *, global_scope, descendants, cursor_value, limit):
    identity = list(service.store.db.execute("SELECT identity,retention_epoch FROM supervision_state WHERE id=1").fetchone())
    signature = hashlib.sha256(json.dumps([identity, "*" if global_scope else sorted(h["id"] for h in heads), descendants]).encode()).hexdigest()[:24]
    epoch = service.store.db.execute("SELECT request_epoch FROM retention_state WHERE id=1").fetchone()[0]
    prefix = f"outcome_requests:{epoch}:{signature}:"
    after = 0
    if cursor_value is not None:
        try:
            if not cursor_value.startswith(prefix):
                raise ValueError
            after = int(cursor_value[len(prefix):])
            if not 0 <= after <= service.store.db.execute("SELECT COALESCE(MAX(rowid),0) FROM requests").fetchone()[0]:
                raise ValueError
        except (ValueError, TypeError):
            raise BridgeError("Outcome request cursor expired or its scope changed; restart without requests_cursor", code="cursor_expired",
                              details={"cursor_kind": "requests", "resume_cursor": None}) from None
    where, args = ["r.status='pending'"], []
    if not global_scope:
        roots, exact, subtrees = [], [], []
        for h in heads:
            if not descendants:
                exact.append(h["id"])
            elif not h.get("parent_task_id"):
                roots.append(h["id"])
            else:
                subtrees.append(h["id"])
        choices = []
        if roots:
            choices.append("t.root_task_id IN (" + ",".join("?" for _ in roots) + ")")
            args.extend(roots)
        if exact:
            choices.append("r.task_id IN (" + ",".join("?" for _ in exact) + ")")
            args.extend(exact)
        for subtree in subtrees:
            choices.append("""EXISTS(WITH RECURSIVE lineage(id,parent_task_id,depth) AS (
                SELECT t.id,t.parent_task_id,0 UNION ALL SELECT p.id,p.parent_task_id,l.depth+1
                FROM tasks p JOIN lineage l ON p.id=l.parent_task_id WHERE l.depth<32)
                SELECT 1 FROM lineage WHERE id=?)""")
            args.append(subtree)
        where.append("(" + " OR ".join(choices) + ")")
    query = " FROM requests r JOIN tasks t ON t.id=r.task_id WHERE " + " AND ".join(where)
    total = service.store.db.execute("SELECT COUNT(*)" + query, args).fetchone()[0]
    rows = service.store.db.execute("SELECT r.id,r.revision,r.projection,r.rowid seq" + query + " AND r.rowid>? ORDER BY r.rowid LIMIT ?", (*args, after, limit + 1)).fetchall()
    result, last = [], after
    for row in rows[:limit]:
        summary = service.store._projection("requests", row)
        if result and result_size({"pending_requests": [*result, summary]}) > REQUEST_PAGE_BUDGET:
            break
        result.append(summary)
        last = row["seq"]
    more = any(r["seq"] > last for r in rows)
    return {"pending_requests": result, "pending_requests_total": total,
            "pending_requests_has_more": more, "pending_requests_next_cursor": prefix + str(last) if more else None}


def snapshot(service, task_ids, task_id, cursors, descendants, tasks_offset, limit, requests_cursor, requests_limit):
    explicit = task_ids if task_ids is not None else [task_id] if task_id else None
    ids = list(dict.fromkeys(explicit)) if explicit is not None else None
    selector = "t.deleted=0" if ids is None else "t.id IN (" + ",".join("?" for _ in ids) + ")"
    rows = service.store.db.execute("""SELECT t.id,t.revision,t.projection,c.own_seq,c.family_seq
        FROM tasks t LEFT JOIN task_changes c ON c.task_id=t.id WHERE """ + selector + " ORDER BY t.rowid DESC LIMIT ? OFFSET ?",
        (*(ids or []), TASK_LIMIT + 1, tasks_offset if ids is None else 0)).fetchall()
    details, projects = {row["id"]: row for row in rows}, {}
    order = ids if ids is not None else [r["id"] for r in rows[:TASK_LIMIT]]
    heads = []
    for identity in order:
        if identity not in details:
            raise BridgeError("Unknown task id", code="not_found", ids={"task_id": identity})
        heads.append(service.checked_task(service.store._projection("tasks", details[identity]), allow_deleted=True, projects=projects))
    next_offset = tasks_offset + len(heads) if ids is None and len(rows) > TASK_LIMIT else None
    if ids is not None:
        if set(cursors) - {h["id"] for h in heads}:
            raise BridgeError("Outcome cursors must name only the selected task_ids", code="invalid_arguments")
    terminals = [(head["id"], head["turn_id"]) for head in heads if head.get("turn_id") and head.get("turn_status") in TERMINAL]
    references = {}
    if terminals:
        found = service.store.db.execute("WITH wanted(task_id,turn_id) AS (VALUES " + ",".join("(?,?)" for _ in terminals) + """ )
            SELECT w.task_id,EXISTS(SELECT 1 FROM result_expirations x WHERE x.task_id=w.task_id AND x.turn_id=w.turn_id) expired,
            (SELECT id FROM turn_results r WHERE r.task_id=w.task_id AND r.turn_id=w.turn_id ORDER BY r.revision DESC LIMIT 1) result_id
            FROM wanted w""", [value for entry in terminals for value in entry]).fetchall()
        references = {r["task_id"]: r for r in found}
    identity = list(service.store.db.execute("SELECT identity,retention_epoch FROM supervision_state WHERE id=1").fetchone())
    families = {head.get("root_task_id") or head["id"] for head in heads}
    claim_hashes = {family: hashlib.sha256() for family in families}
    claim_counts = dict.fromkeys(families, 0)
    if families:
        rows_claims = service.store.db.execute("""SELECT family_id,task_id,kind,state,revision FROM workspace_claims
            INDEXED BY claims_live_family WHERE state!='inactive' AND family_id IN (""" + ",".join("?" for _ in families) + ") ORDER BY family_id,task_id,kind", tuple(sorted(families)))
        for claim in rows_claims:
            family = claim["family_id"]
            claim_hashes[family].update(json.dumps(list(claim), separators=(",", ":")).encode())
            claim_counts[family] += 1
    selected, next_cursors, skipped, usages = [], dict(cursors), False, {}
    for head in heads:
        row = details[head["id"]]
        seq = row["family_seq" if descendants else "own_seq"] or 0
        family = head.get("root_task_id") or head["id"]
        if family not in usages:
            usages[family] = service.resources.usage(head["id"], compact=True)
        resource_revision = service.store.db.execute("SELECT revision FROM resource_families WHERE family_id=?", (family,)).fetchone()
        claims = hashlib.sha256((claim_hashes[family].hexdigest() + ":" + str(resource_revision[0] if resource_revision else 0)).encode()).hexdigest()
        old = previous(cursors.get(head["id"]), identity, head["id"], descendants, seq)
        token = cursor(identity, head["id"], descendants, seq, claims)
        if old is None or old != (seq, claims):
            if len(selected) >= min(limit, TASK_LIMIT):
                skipped = True
                continue  # Do not advance a cursor for a summary not returned.
            ref = None
            if head["id"] in references:
                stored = references[head["id"]]
                ref = {"tool": "codex_get", "arguments": {"task_id": head["id"], "view": "result", "turn_id": head["turn_id"]},
                       "available": not bool(stored["expired"]), "lazy": stored["result_id"] is None and not stored["expired"]}
                if stored["result_id"] and not stored["expired"]:
                    ref["captured_result_id"] = stored["result_id"]
            gaps = head.get("evidence_gaps", [])
            item = {"id": head["id"], "status": head["status"], "turn_id": head.get("turn_id"), "turn_status": head.get("turn_status"),
                "revision": head["revision"], "counts": head.get("counts", {}), "deleted": bool(head.get("deleted")),
                "archived": bool(head.get("archived")), "has_error": bool(head.get("has_error")), "has_diff": bool(head.get("has_diff")),
                "evidence_complete": head.get("evidence_complete"), "evidence_gaps": [str(gap)[:128] for gap in gaps[:20]],
                "evidence_gaps_truncated": len(gaps) > 20 or any(len(str(gap)) > 128 for gap in gaps[:20]),
                "workspace_reserved": bool(claim_counts[family]),
                "family_usage": usages[family],
                "budget_stop": service.resources.policy(family)[1],
                "result_ref": ref, "cursor": token}
            if result_size(item) > 16384:
                raise BridgeError("Outcome task metadata exceeds its budget; inspect codex_get for this task", code="result_too_large", ids={"task_id": head["id"]})
            selected.append(item)
        next_cursors[head["id"]] = token
    page = request_page(service, heads, global_scope=explicit is None, descendants=descendants,
                        cursor_value=requests_cursor, limit=requests_limit)
    expired_through = service.store.db.execute("SELECT COALESCE(MAX(last_seq),0) FROM expired_event_ranges").fetchone()[0]
    value = {"view": "outcome", "events": [], "outcomes": selected, "cursors": {h["id"]: next_cursors[h["id"]] for h in heads if h["id"] in next_cursors},
        "cursor": 0, "journal_highwater": service.store.highwater(),
        "has_more": skipped or page["pending_requests_has_more"] or next_offset is not None,
        "tasks_next_offset": next_offset, "coalesced": True, "cursor_kind": "outcome",
        "journal_retention": {"expired_through": expired_through, "scope": "global"},
        "suggested_wait_seconds": 0 if page["pending_requests_total"] else 1 if any(h["status"] not in TERMINAL | {"deleted", "archived"} for h in heads) else 5,
        **page}
    if result_size(value) > OUTCOME_BUDGET:
        raise BridgeError("Outcome page exceeds its budget; reduce limit or requests_limit", code="result_too_large")
    return value


async def wait(service, *, task_ids=None, task_id=None, cursors=None, include_descendants=True, wait_seconds=0,
               tasks_offset=0, limit=50, requests_cursor=None, requests_limit=50, _clock=None):
    if task_ids is not None and (not 1 <= len(task_ids) <= TASK_LIMIT or task_id is not None):
        raise BridgeError("Use task_id or between 1 and 50 task_ids for outcome observation", code="invalid_arguments")
    if tasks_offset and (task_id is not None or task_ids is not None):
        raise BridgeError("tasks_offset applies only to outcome observation without task_id or task_ids", code="invalid_arguments")
    clock = _clock or asyncio.get_running_loop().time
    deadline = clock() + min(wait_seconds, 25)
    async with service.changed:
        while True:
            result = snapshot(service, task_ids, task_id, cursors or {}, include_descendants, tasks_offset,
                              limit, requests_cursor, requests_limit)
            remaining = deadline - clock()
            if result["outcomes"] or result["pending_requests_total"] or result["has_more"] or remaining <= 0:
                return result
            try:
                await asyncio.wait_for(service.changed.wait(), remaining)
            except TimeoutError:
                return result
