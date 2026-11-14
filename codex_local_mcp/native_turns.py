"""Native Turn objects can describe nested work in one logical root turn."""

import json

from .security import BridgeError

NESTED_REVERT = "Revert requires a root turn id; use logical_turn_id from codex_history"


class Scope:
    """Turn ids belong to threads; a causal root may belong to another one."""
    def __init__(self, db=None, task_id=None, thread_id=None, *, known=()):
        self.db, self.task_id, self.thread_id = db, task_id, thread_id
        self.known = set(known)

    def _other_owners(self, turn_id):
        owner = self.db.execute("""SELECT t.id,t.thread_id FROM tasks t JOIN turns u ON u.task_id=t.id
            AND u.thread_id=t.thread_id WHERE t.id!=? AND t.thread_id!=? AND u.turn_id=? LIMIT 1""",
            (self.task_id, self.thread_id, turn_id)).fetchone()
        if owner:
            return owner
        # Older projections can be wrong; raw starts still identify an owner
        # within a native subagent family without decoding journal payloads.
        return self.db.execute("""SELECT t.id,t.thread_id FROM tasks t
            WHERE t.root_task_id=(SELECT root_task_id FROM tasks WHERE id=?) AND t.id!=? AND t.thread_id!=?
            AND EXISTS(SELECT 1 FROM events e WHERE e.task_id=t.id AND e.kind='turn/started'
                AND json_extract(e.data,'$.threadId')=t.thread_id AND json_extract(e.data,'$.turn.id')=?)
            LIMIT 1""", (self.task_id, self.task_id, self.thread_id, turn_id)).fetchone()

    def _physical(self, turn_id):
        return self.db.execute("""SELECT 1 FROM events WHERE task_id=? AND kind='turn/started'
            AND json_extract(data,'$.threadId')=? AND json_extract(data,'$.turn.id')=? LIMIT 1""",
            (self.task_id, self.thread_id, turn_id)).fetchone() or self.db.execute("""SELECT 1 FROM operation_details
            WHERE task_id=? AND json_extract(data,'$.acknowledged_turn.thread_id')=?
            AND json_extract(data,'$.acknowledged_turn.native_turn_id')=? LIMIT 1""",
            (self.task_id, self.thread_id, turn_id)).fetchone()

    def owns(self, turn_id):
        if self.db is None:
            return turn_id in self.known
        recorded = turn_id in self.known or self.db.execute("SELECT 1 FROM turns WHERE task_id=? AND thread_id=? AND turn_id=?",
            (self.task_id, self.thread_id, turn_id)).fetchone() or self.db.execute("""SELECT 1 FROM tasks
            WHERE id=? AND thread_id=? AND json_extract(projection,'$.turn_id')=?""",
            (self.task_id, self.thread_id, turn_id)).fetchone()
        acknowledged = self.db.execute("""SELECT 1 FROM operation_details WHERE task_id=?
            AND json_extract(data,'$.acknowledged_turn.thread_id')=?
            AND json_extract(data,'$.acknowledged_turn.turn_id')=? LIMIT 1""",
            (self.task_id, self.thread_id, turn_id)).fetchone() if not recorded else None
        if not recorded and not acknowledged:
            return bool(self._physical(turn_id))
        # Derived rows and inherited history may copy another thread's turn.
        # Raw native starts/ACKs disambiguate that thread ownership.
        return not self._other_owners(turn_id) or bool(self._physical(turn_id))

    def foreign(self, turn_id):
        return self.db is not None and not self.owns(turn_id) and bool(self._other_owners(turn_id))


def for_task(store, task_id):
    row = store.db.execute("SELECT thread_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    return Scope(store.db, task_id, row[0] if row else None)


def logical_id(turn, scope=None):
    if not isinstance(turn, dict) or not turn:
        return None
    if not isinstance(turn.get("id"), str) or not turn["id"]:
        raise BridgeError("Native turn identity is incomplete; workspace ownership was retained", code="state_conflict")
    value = turn.get("rootTurnId")
    if value is not None and (not isinstance(value, str) or not value):
        raise BridgeError("Native turn identity is incomplete; workspace ownership was retained", code="state_conflict")
    return value if value and value != turn["id"] and scope is not None and scope.owns(value) else turn["id"]


def nested(turn, scope=None):
    return bool(isinstance(turn, dict) and turn.get("rootTurnId") and turn.get("id") != logical_id(turn, scope))


def event_id(params, scope=None):
    return logical_id(params.get("turn"), scope) or params.get("turnId")


def root_view(turn, scope=None):
    """A nested start proves activity, never root completion or root timing."""
    if not nested(turn, scope):
        return turn
    if turn.get("status") != "inProgress":
        raise BridgeError("Native history has no root turn status; workspace ownership was retained. Inspect codex_history and retry codex_resume",
            code="state_conflict")
    return {"id": logical_id(turn, scope), "rootTurnId": logical_id(turn, scope), "status": "inProgress", "items": []}


def recorded_root(store, task, *, known=()):
    row = store.db.execute("""SELECT json_extract(data,'$.turn.rootTurnId') FROM events
        WHERE task_id=? AND kind='turn/started' AND json_extract(data,'$.threadId')=?
        AND json_extract(data,'$.turn.id')=? ORDER BY seq DESC LIMIT 1""",
        (task["id"], task.get("thread_id"), task.get("turn_id"))).fetchone()
    scope = for_task(store, task["id"])
    scope.known.update(known)
    return row[0] if row and isinstance(row[0], str) and row[0] and row[0] != task.get("turn_id") and scope.owns(row[0]) else None


def terminal_evidence(store, task, turn_id):
    """Root completion survives restart; process uncertainty is separate."""
    if turn_id != task.get("turn_id"):
        return None
    row = store.db.execute("""SELECT seq,json_extract(data,'$.turn.id','$.turn.rootTurnId','$.turn.status',
        '$.turn.startedAt','$.turn.completedAt','$.turn.durationMs') proof FROM events
        WHERE task_id=? AND kind='turn/completed' AND json_extract(data,'$.threadId')=?
        AND json_extract(data,'$.turn.id')=?
        AND json_extract(data,'$.turn.status') IN ('completed','failed','interrupted') ORDER BY seq DESC LIMIT 1""",
        (task["id"], task["thread_id"], turn_id)).fetchone()
    if not row or store.db.execute("""SELECT 1 FROM events WHERE task_id=? AND kind='turn/started' AND seq>?
        AND json_extract(data,'$.threadId')=? AND json_extract(data,'$.turn.rootTurnId')=?
        AND json_extract(data,'$.turn.id')!=? LIMIT 1""", (task["id"], row["seq"], task["thread_id"], turn_id, turn_id)).fetchone():
        return None
    fields = ("id", "rootTurnId", "status", "startedAt", "completedAt", "durationMs")
    turn = dict(zip(fields, json.loads(row["proof"])))
    if nested(turn, for_task(store, task["id"])):
        return None
    return {**turn, "items": [], "_source_event_seq": row["seq"]}


async def latest(backend, thread_id, *, scope=None, root_evidence=None):
    """Prefer the root row, with bounded reads of summary history only."""
    params = {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "summary"}
    scope = scope or Scope(thread_id=thread_id)
    first, cursor, seen = None, None, set()
    root = None
    for page_number in range(5):
        page = await backend.call("thread/turns/list", {**params, **({"cursor": cursor, "limit": 100} if page_number else {})})
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise BridgeError("Native turn history is incomplete; workspace ownership was retained", code="state_conflict")
        if len(page["data"]) > (1 if page_number == 0 else 100):
            raise BridgeError("Native turn history is incomplete; workspace ownership was retained", code="state_conflict")
        for turn in page["data"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                raise BridgeError("Native turn history is incomplete; workspace ownership was retained", code="state_conflict")
            if first is None:
                first = turn
                root = turn.get("rootTurnId")
            scope.known.add(turn["id"])
            if not root or root == first["id"] or scope.foreign(root):
                return first
            if turn["id"] == root and not nested(turn, scope):
                return turn
        cursor = page.get("nextCursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    if first and nested(first, scope) and root_evidence:
        proven = root_evidence(logical_id(first, scope))
        if proven is not None:
            return proven
    return root_view(first, scope)


async def require_root(backend, thread_id, turn_id, *, scope=None):
    """Destructive history controls must not guess a nested-id boundary."""
    cursor, seen = None, set()
    scope = scope or Scope(thread_id=thread_id)
    for _ in range(5):
        page = await backend.call("thread/turns/list", {"threadId": thread_id, "cursor": cursor,
            "limit": 100, "sortDirection": "desc", "itemsView": "summary"})
        for turn in page.get("data", []):
            if turn.get("id") == turn_id:
                if nested(turn, scope):
                    raise BridgeError(NESTED_REVERT, code="invalid_arguments")
                return
        cursor = page.get("nextCursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise BridgeError("Turn was not found in bounded native history; inspect codex_history before reverting", code="not_found")
