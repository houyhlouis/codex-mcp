"""Small indexed metadata for observation, reconciliation and decision receipts."""

import json
import time
import uuid

from .security import redact

STREAM_ONLY = frozenset({"item/agentMessage/delta", "item/commandExecution/outputDelta"})


def migrate(db):
    for statement in (
        "CREATE TABLE supervision_state (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, counter INTEGER NOT NULL, retention_epoch INTEGER NOT NULL)",
        "CREATE TABLE task_changes (task_id TEXT PRIMARY KEY, own_seq INTEGER NOT NULL, family_seq INTEGER NOT NULL)",
        "CREATE TABLE reconciliation_reports (task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, task_revision INTEGER NOT NULL, created_at REAL NOT NULL, data TEXT NOT NULL)",
        "CREATE TABLE recovered_items (task_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL, sha256 TEXT NOT NULL, source_seq INTEGER NOT NULL, PRIMARY KEY(task_id,turn_id,item_id))",
        "CREATE TABLE operation_details (id TEXT PRIMARY KEY, task_id TEXT, name TEXT NOT NULL, created_at REAL NOT NULL, data TEXT NOT NULL)",
        "CREATE INDEX operation_details_task ON operation_details(task_id,created_at)",
        "CREATE INDEX operations_task ON operations(task_id)",
        "CREATE TABLE decision_receipts (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL, turn_id TEXT, created_at REAL NOT NULL, data TEXT NOT NULL)",
        "CREATE INDEX receipts_task ON decision_receipts(task_id,created_at)",
    ):
        db.execute(statement)
    db.execute("INSERT INTO supervision_state VALUES (1,?,0,0)", (str(uuid.uuid4()),))


def touch(store, task_id):
    if store.schema_version < 8 or not task_id:
        return
    db = store.db
    if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
        return
    db.execute("UPDATE supervision_state SET counter=counter+1 WHERE id=1")
    # Propagate to ancestors, not through all historical descendants.
    ancestors = db.execute("""WITH RECURSIVE ancestors(id,parent_task_id,depth) AS (
        SELECT id,parent_task_id,0 FROM tasks WHERE id=? UNION ALL
        SELECT t.id,t.parent_task_id,a.depth+1 FROM tasks t JOIN ancestors a ON t.id=a.parent_task_id WHERE a.depth<32)
        SELECT id FROM ancestors""", (task_id,)).fetchall()
    for row in ancestors:
        db.execute("""INSERT INTO task_changes VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            own_seq=task_changes.own_seq+excluded.own_seq,
            family_seq=task_changes.family_seq+1""", (row[0], int(row[0] == task_id), 1))
    callback = getattr(store, "on_change", None)
    if callback is not None:
        for row in ancestors:
            callback(row[0])


def request_description(name, args):
    fields = ("action", "scope", "mode", "expected_turn_id", "model", "effort", "access", "approval_mode", "queued_id", "name")
    result = {k: args[k] for k in fields if k in args}
    if "prompt" in args:
        result.update(prompt_preview=args["prompt"][:512], prompt_truncated=len(args["prompt"]) > 512)
    return {"tool": name, "arguments": result}


def save_receipt(store, receipt):
    with store.transaction():
        return _save_receipt(store, receipt)


def _save_receipt(store, receipt):
    previous = store.db.execute("SELECT data FROM decision_receipts WHERE request_id=?", (receipt["request_id"],)).fetchone()
    if previous:
        return json.loads(previous[0])
    receipt = redact({**receipt, "receipt_id": str(uuid.uuid4()), "decided_at": time.time(), "one_shot": True, "reusable": False})
    store.db.execute("INSERT INTO decision_receipts VALUES (?,?,?,?,?,?)", (receipt["receipt_id"], receipt["request_id"],
        receipt["task_id"], receipt.get("turn_id"), receipt["decided_at"], json.dumps(receipt)))
    touch(store, receipt["task_id"])
    return receipt
