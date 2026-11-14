"""Expire owned contract metadata only in an operator-reviewed prune."""

import hashlib
import json
import math
import re

from .evidence import canonical
from .security import BridgeError

SOURCES = (("tasks", "data"), ("operations", "result"), ("operation_details", "data"))


def identity(record):
    if (not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1 or record.get("kind") != "delegation"
            or record.get("authority") != "guidance_only" or not isinstance(record.get("scope"), dict)
            or not isinstance(record["scope"].get("task_id"), str)
            or not isinstance(record.get("context_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["context_hash"])):
        return None
    return record["scope"]["task_id"], record["context_hash"]


def records(table, value):
    if not isinstance(value, dict):
        return
    fields = ("contract",) if table == "operation_details" else ("delegation_contract", "delegation_contract_summary")
    for field in fields:
        if identity(value.get(field)):
            yield field, value[field]
    if table != "operation_details" and isinstance(value.get("delegation_contracts"), list):
        for index, record in enumerate(value["delegation_contracts"]):
            if identity(record):
                yield f"delegation_contracts/{index}", record


def groups(db):
    result = {}
    for table, column in SOURCES:
        for row in db.execute(f"SELECT id,{column} value FROM {table} WHERE {column} IS NOT NULL ORDER BY rowid"):
            value = json.loads(row["value"])
            for path, record in records(table, value):
                result.setdefault(identity(record), []).append([table, row["id"], path, record])
    return result


def candidates(db, tasks, protected, cutoff):
    selected, held_count, held_bytes = [], 0, 0
    for key, copies in sorted(groups(db).items()):
        if key[0] not in tasks:
            continue
        if key[0] in protected:
            held_count += 1
            held_bytes += sum(len(canonical(copy[3])) for copy in copies)
            continue
        if all(type(copy[3].get("created_at")) in (float, int) and math.isfinite(copy[3]["created_at"])
               and copy[3]["created_at"] < cutoff for copy in copies):
            selected.append({"task_id": key[0], "context_hash": key[1], "record_hash": hashlib.sha256(canonical(copies)).hexdigest()})
    return selected, held_count, held_bytes


def strip(table, value, selected):
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for field in (("contract",) if table == "operation_details" else ("delegation_contract", "delegation_contract_summary")):
        if identity(value.get(field)) in selected:
            result[field] = None
    if table != "operation_details" and isinstance(value.get("delegation_contracts"), list):
        result["delegation_contracts"] = [r for r in value["delegation_contracts"] if identity(r) not in selected]
    return result


def rewrites(db, selected):
    result = []
    for table, column in SOURCES:
        for row in db.execute(f"SELECT id,{column} value FROM {table} WHERE {column} IS NOT NULL ORDER BY rowid"):
            value = json.loads(row["value"])
            updated = strip(table, value, selected)
            if value != updated:
                text = json.dumps(updated)
                result.append((table, column, row["id"], value, updated, max(0, len(row["value"].encode()) - len(text.encode()))))
    return result


def apply(store, entries):
    if not entries:
        return
    if not store.db.in_transaction:
        raise ValueError("Contract expiry requires the caller's transaction")
    current = groups(store.db)
    for entry in entries:
        copies = current.get((entry["task_id"], entry["context_hash"]))
        if not copies or hashlib.sha256(canonical(copies)).hexdigest() != entry["record_hash"]:
            raise BridgeError("Delegation contracts changed; create a new dry-run plan", code="prune_plan_invalid")
    selected = {(e["task_id"], e["context_hash"]) for e in entries}
    for table, column, key, before, after, _ in rewrites(store.db, selected):
        if table == "tasks":
            store.update("tasks", key, {k: after[k] for k in after if after[k] != before.get(k)})
        else:
            store.db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (json.dumps(after), key))
