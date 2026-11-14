"""Explicit operator maintenance of bridge state; never native history or projects."""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from .evidence import canonical
from .lifecycle import TERMINAL
from .platforms import lock_file, unlock_file
from .security import BridgeError, checked_directory, private_dir, private_file, within
from .store import Store
from .supervision import touch
from .protocol_tasks import expired_execution_keys, delete_expired_execution_keys
from . import guidance_retention
from .resources import TABLES as RESOURCE_TABLES
from .execution_plans import TABLES as EXECUTION_PLAN_TABLES, EXPIRED_PREFIX

PLAN_SECONDS = 3600
PLAN_LIMIT = 32
PLAN_STORAGE_BYTES = 16 * 1024 * 1024
OWNED_TABLES = ("tasks", "projects", "requests", "events", "operations", "operation_times", "metadata", "turns",
                "admissions", "workspace_claims", "artifacts", "evidence_entries", "turn_results", "managed_images",
                "operation_tombstones", "expired_event_ranges", "result_expirations", "retention_state", "evidence_history",
                "supervision_state", "task_changes", "reconciliation_reports", "recovered_items", "operation_details", "decision_receipts")
PROTECTED_CLASSES = ["nonterminal_task_families", "pending_requests", "unresolved_operations", "workspace_claims",
                     "retained_results", "native_image_history", "untracked_legacy_images", "unknown_metadata_references", "operation_tombstones", "delegation_contracts", "dependency_plans", "plan_tombstones"]
OWNED_TABLES += RESOURCE_TABLES + EXECUTION_PLAN_TABLES


def duration(value):
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw]?)", value or "")
    if not match:
        raise BridgeError("Use a positive duration such as 30d or 12h; a bare number means days", code="invalid_arguments")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 86400}[match[2]]


@contextmanager
def offline(config):
    private_dir(config.state_dir)
    descriptors = []
    try:
        for name in ("startup.lock", "broker.lock"):
            fd = private_file(config.state_dir / name, os.O_CREAT | os.O_RDWR)
            try:
                lock_file(fd)
            except BlockingIOError as exc:
                os.close(fd)
                raise BridgeError("Pruning requires a stopped broker. Run codex-local-mcp --config <same-config> stop, then retry; no broker was stopped", code="maintenance_busy") from exc
            except BaseException:
                os.close(fd)
                raise
            descriptors.append(fd)
        yield
    finally:
        for fd in reversed(descriptors):
            try:
                unlock_file(fd)
            finally:
                os.close(fd)


def backup(config, destination=None):
    source = config.state_dir / "state.sqlite3"
    checked_directory(config.state_dir)
    os.close(private_file(source, os.O_RDONLY))
    if destination is None:
        target = config.state_dir / f"state.sqlite3.backup-{time.time_ns()}.bak"
    else:
        target = Path(destination)
        if not target.is_absolute() or ".." in target.parts:
            raise BridgeError("Backup destination must be an absolute file path in a private directory", code="invalid_arguments")
    if target.exists() or target.is_symlink():
        raise BridgeError("Backup destination already exists; choose a new file path", code="backup_destination_exists")
    created = False
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            Store._inspect(reader)
            for row in reader.execute("SELECT data FROM projects"):
                path = (json.loads(row[0]) or {}).get("path")
                if path and within(target.resolve(), Path(path).resolve()):
                    raise BridgeError("Backup destination must not be inside a project directory", code="invalid_arguments")
            private_dir(target.parent)
            try:
                descriptor = private_file(target, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (OSError, BridgeError) as exc:
                if isinstance(exc, FileExistsError) or isinstance(exc.__cause__, FileExistsError) or target.exists() or target.is_symlink():
                    raise BridgeError("Backup destination already exists; choose a new file path", code="backup_destination_exists") from exc
                raise
            created = True
            os.close(descriptor)
            with closing(sqlite3.connect(target)) as copy:
                reader.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BridgeError("Backup failed its integrity check; no usable backup was created", code="state_invalid")
        return {"backup": str(target), "size_bytes": target.stat().st_size, "integrity_check": "ok", "online_safe": True}
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


class Retention:
    def __init__(self, store, *, clock=time.time):
        self.store, self.db, self.clock = store, store.db, clock

    def snapshot(self):
        digest = hashlib.sha256()
        digest.update(canonical([self.db.execute("PRAGMA user_version").fetchone()[0],
                                 [list(r) for r in self.db.execute("SELECT * FROM sqlite_sequence ORDER BY name")]]))
        for table in OWNED_TABLES:
            digest.update(table.encode())
            for row in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                values = [hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else v for v in row]
                digest.update(canonical(values))
        return digest.hexdigest()

    def _selection(self, cutoff, max_bytes, *, expired_executions=()):
        protected, meta_refs = set(), set()
        expired_bytes = 0
        tasks = {row["id"]: dict(row) for row in self.db.execute("SELECT id,root_task_id,status,deleted,thread_id FROM tasks")}
        for row in self.db.execute("SELECT key,value FROM metadata"):
            if row["key"] in expired_executions:
                expired_bytes += len(row["key"].encode()) + len(row["value"].encode())
                continue
            try:
                meta_refs.update(strings(json.loads(row["value"])))
            except (ValueError, TypeError):
                meta_refs.add(row["value"])
        metadata_refs = set(meta_refs)
        expired_plans, plan_bytes, held_plan_bytes, plan_tasks = [], 0, 0, set()
        for row in self.db.execute("SELECT id,done,updated_at,LENGTH(CAST(spec||decision AS BLOB))+80 bytes FROM plans"):
            nodes = list(self.db.execute("SELECT task_id,result_ref,LENGTH(CAST(data||COALESCE(result_ref,'') AS BLOB))+160 bytes FROM plan_nodes WHERE plan_id=?", (row["id"],)))
            size = row["bytes"] + sum(n["bytes"] for n in nodes)
            plan_bytes += size
            if row["done"] and row["updated_at"] < cutoff and row["id"] not in meta_refs:
                expired_plans.append(row["id"])
                continue
            held_plan_bytes += size
            plan_tasks.update(n["task_id"] for n in nodes if n["task_id"])
            for node in nodes:
                if node["result_ref"]:
                    meta_refs.update(strings(json.loads(node["result_ref"])))
            meta_refs.update(r[0] for r in self.db.execute("SELECT id FROM operations WHERE id LIKE ?", ("plan:" + row["id"] + ":%",)))
        unresolved_rows = list(self.db.execute("SELECT id,task_id,state FROM operations WHERE state!='done'"))
        unresolved = {r["id"]: r["task_id"] for r in unresolved_rows}
        live_families = {t["root_task_id"] or t["id"] for t in tasks.values() if not t["deleted"]}
        protected_operations = {r["task_id"] for r in unresolved_rows if r["state"] == "running"
            or r["task_id"] not in tasks or (tasks[r["task_id"]]["root_task_id"] or r["task_id"]) in live_families}
        seeds = {
            "nonterminal_task_families": {t["id"] for t in tasks.values() if t["status"] not in TERMINAL | {"archived", "deleted"}},
            "pending_requests": {r[0] for r in self.db.execute("SELECT task_id FROM requests WHERE status IN ('pending','answering')")},
            "unresolved_operations": protected_operations,
            "workspace_claims": {r[0] for r in self.db.execute("SELECT task_id FROM workspace_claims WHERE state!='inactive'")},
            "unknown_metadata_references": meta_refs & tasks.keys(),
            "dependency_plans": plan_tasks,
        }
        families = {}
        roots = set()
        for reason, ids in seeds.items():
            held_roots = {tasks[t]["root_task_id"] or t for t in ids if t in tasks}
            roots.update(held_roots)
            families[reason] = {t["id"] for t in tasks.values() if (t["root_task_id"] or t["id"]) in held_roots}
            protected.update(families[reason])
        contracts, held_contracts, held_contract_bytes = guidance_retention.candidates(self.db, tasks, protected, cutoff)
        contract_keys = {(e["task_id"], e["context_hash"]) for e in contracts}
        contract_rewrites = guidance_retention.rewrites(self.db, contract_keys)
        contract_bytes = sum(row[5] for row in contract_rewrites)
        extra_contract_bytes = sum(row[5] for row in contract_rewrites if row[0] != "operations")
        operation_contract_bytes = {row[2]: row[5] for row in contract_rewrites if row[0] == "operations"}
        # Compatible extensions in retained records can own scoped references.
        reference_ids = {r[0] for table in ("artifacts", "turn_results", "managed_images") for r in self.db.execute(f"SELECT id FROM {table}")}
        reference_ids.update(r[0] for r in self.db.execute("SELECT path FROM managed_images"))
        def linked_sequences(value):
            if isinstance(value, dict):
                if type(value.get("event_seq")) is int:
                    yield str(value["event_seq"])
                for child in value.values():
                    yield from linked_sequences(child)
            elif isinstance(value, list):
                for child in value:
                    yield from linked_sequences(child)
        for table, column in (("tasks", "data"), ("projects", "data"), ("requests", "data"), ("operations", "result"),
                              ("reconciliation_reports", "data"), ("decision_receipts", "data"), ("operation_details", "data")):
            for row in self.db.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
                value = guidance_retention.strip(table, json.loads(row[0]), contract_keys)
                if table in ("tasks", "operations"):
                    value.pop("process_scan", None)  # A scan watermark owns no journal content.
                refs = (set(strings(value)) & reference_ids) | set(linked_sequences(value))
                meta_refs.update(refs)
                metadata_refs.update(refs)
        # Unknown compatible request extensions may contain evidence references.
        for row in self.db.execute("SELECT id,data FROM requests"):
            value = json.loads(row["data"])
            meta_refs.update(v for v in strings(value) if v not in tasks and v != row["id"])
        selected = {k: [] for k in ("turn_results", "requests", "operations", "events", "artifacts", "managed_images", "evidence_entries", "recovered_items")}
        selected["execution_metadata"] = list(expired_executions)
        selected["plans"] = expired_plans
        selected["delegation_contracts"] = contracts
        sizes = {
            "turn_results": "LENGTH(CAST(data AS BLOB))", "requests": "LENGTH(CAST(data AS BLOB))",
            "operations": "LENGTH(CAST(COALESCE(result,'') AS BLOB))", "events": "LENGTH(CAST(data AS BLOB))",
            "artifacts": "COALESCE(LENGTH(content),0)", "managed_images": "size_bytes", "evidence_entries": "LENGTH(CAST(data AS BLOB))",
            "recovered_items": "LENGTH(CAST(task_id||turn_id||item_id||sha256 AS BLOB))+8"}
        total = expired_bytes + plan_bytes + extra_contract_bytes + sum(self.db.execute(f"SELECT COALESCE(SUM({expr}),0) FROM {table}").fetchone()[0] for table, expr in sizes.items())
        plan_reclaimed = plan_bytes - held_plan_bytes
        remaining, reclaimed = total - expired_bytes - contract_bytes - plan_reclaimed, expired_bytes + contract_bytes + plan_reclaimed
        def choose(table, identity, size):
            nonlocal remaining, reclaimed
            if max_bytes is not None and remaining <= max_bytes:
                return False
            selected[table].append(identity)
            remaining -= size or 0
            reclaimed += size or 0
            return True
        for row in self.db.execute("SELECT *,LENGTH(CAST(data AS BLOB)) bytes FROM turn_results WHERE created_at<? ORDER BY created_at,id", (cutoff,)):
            if row["task_id"] not in protected and row["id"] not in meta_refs and row["turn_id"] not in meta_refs:
                choose("turn_results", row["id"], row["bytes"])
        retained_turns, sources, artifact_refs = set(), set(), set()
        for row in self.db.execute("SELECT id,task_id,turn_id,data FROM turn_results"):
            if row["id"] not in selected["turn_results"]:
                retained_turns.add((row["task_id"], row["turn_id"]))
                value = json.loads(row["data"])
                def references(node):
                    if isinstance(node, dict):
                        if type(node.get("event_seq")) is int:
                            sources.add(node["event_seq"])
                        if isinstance(node.get("artifact_id"), str):
                            artifact_refs.add(node["artifact_id"])
                        for v in node.values():
                            references(v)
                    elif isinstance(node, list):
                        for v in node:
                            references(v)
                references(value)
        result_sources = set(sources)
        for row in self.db.execute("SELECT id,task_id,data FROM requests WHERE status NOT IN ('pending','answering')"):
            value = json.loads(row["data"])
            if row["task_id"] not in protected and row["id"] not in meta_refs and value.get("created_at", self.clock()) < cutoff:
                choose("requests", row["id"], len(row["data"].encode()))
        for row in self.db.execute("SELECT id,data FROM requests"):
            if row["id"] not in selected["requests"]:
                meta_refs.update(strings(json.loads(row["data"])))
        # Legacy operations have no trustworthy age; use the migration time
        # without copying every historical operation during the upgrade.
        operation_age = "COALESCE(t.updated_at,(SELECT applied_at FROM schema_migrations WHERE version=7))"
        for row in self.db.execute(f"SELECT o.*,{operation_age} updated_at FROM operations o LEFT JOIN operation_times t ON t.id=o.id WHERE state='done' AND {operation_age}<? ORDER BY updated_at,o.id", (cutoff,)):
            if row["task_id"] not in protected and row["id"] not in meta_refs:
                choose("operations", row["id"], len((row["result"] or "").encode()) - operation_contract_bytes.get(row["id"], 0))
        for row in self.db.execute("SELECT e.*,LENGTH(CAST(e.data AS BLOB)) bytes FROM evidence_entries e JOIN events v ON v.seq=e.source_seq WHERE v.at<? ORDER BY v.at", (cutoff,)):
            if row["task_id"] not in protected and (row["task_id"], row["turn_id"]) not in retained_turns:
                choose("evidence_entries", [row[k] for k in ("task_id", "turn_id", "category", "item_key")], row["bytes"])
        retained_entry_refs = set()
        chosen_entries = {tuple(v) for v in selected["evidence_entries"]}
        for row in self.db.execute("SELECT * FROM evidence_entries"):
            if tuple(row[k] for k in ("task_id", "turn_id", "category", "item_key")) not in chosen_entries:
                sources.add(row["source_seq"])
                retained_entry_refs.update(strings(json.loads(row["data"])))
        artifact_refs.update(retained_entry_refs)
        for row in self.db.execute("SELECT id,task_id,turn_id,COALESCE(LENGTH(content),0) size_bytes,created_at,source_seq FROM artifacts ORDER BY created_at,id"):
            removed = False
            if (row["created_at"] < cutoff and row["task_id"] not in protected and row["id"] not in artifact_refs | meta_refs
                    and (row["task_id"], row["turn_id"]) not in retained_turns):
                removed = choose("artifacts", row["id"], row["size_bytes"])
            if not removed:
                sources.add(row["source_seq"])
                if row["id"] in artifact_refs or (row["task_id"], row["turn_id"]) in retained_turns:
                    result_sources.add(row["source_seq"])
        for row in self.db.execute("SELECT seq,task_id,LENGTH(CAST(data AS BLOB)) bytes FROM events WHERE at<? ORDER BY seq", (cutoff,)):
            if row["task_id"] not in protected and row["seq"] not in sources and str(row["seq"]) not in meta_refs:
                choose("events", row["seq"], row["bytes"])
        expired_sources = set(selected["events"])
        for row in self.db.execute("SELECT r.*," + sizes["recovered_items"] + " bytes FROM recovered_items r"):
            key = [row[k] for k in ("task_id", "turn_id", "item_id")]
            if row["source_seq"] in expired_sources:
                # Removing a source must remove its disposable cache atomically,
                # including when the source already met the byte target.
                selected["recovered_items"].append(key)
                remaining -= row["bytes"]
                reclaimed += row["bytes"]
            elif row["task_id"] not in protected and not self.db.execute("SELECT 1 FROM events WHERE seq=?", (row["source_seq"],)).fetchone():
                choose("recovered_items", key, row["bytes"])
        for row in self.db.execute("SELECT * FROM managed_images WHERE created_at<? ORDER BY created_at,id", (cutoff,)):
            # Offline bridge state cannot prove absence of external native forks.
            native_hold = row["state"] == "native_history"
            if (row["task_id"] not in protected and row["operation_id"] not in unresolved and not native_hold
                    and row["id"] not in meta_refs and row["path"] not in meta_refs
                    and not any(task_id == row["task_id"] for task_id, _ in retained_turns)):
                choose("managed_images", row["id"], row["size_bytes"])
        reasons = {
            "nonterminal_task_families": "Task families are unfinished. Finish or interrupt their work, then make a new plan",
            "pending_requests": "Requests need answers. Review them or cancel their tasks, then make a new plan",
            "unresolved_operations": "Unknown operation records and their references stay retained. Deleted families' other data may expire; other families stay protected",
            "workspace_claims": "Workspace work is active or unverified. Inspect codex_get workspace_claims; use codex_resume to re-check",
            "retained_results": "Retained results need this evidence. Keep it, or choose a policy that also expires the results",
            "native_image_history": "Submitted images may be in native history or external forks. Keep them; this prune cannot remove them",
            "untracked_legacy_images": "Image ownership is unknown. Keep these files; this prune cannot remove them",
            "unknown_metadata_references": "Other records reference these bytes. Keep them until their owning feature releases the references",
            "operation_tombstones": "Expired keys prevent duplicate execution. Keep them; use a new operation_id for new work",
            "delegation_contracts": "Active family work or unresolved decisions still need these contracts. Finish/reconcile the family, then make a new plan",
            "dependency_plans": "Dependency plans still need these tasks and references. Finish, cancel or reconcile the plan, then use an age policy that expires it",
            "plan_tombstones": "Expired plan identities prevent repeated work. Keep them; change the plan's name to request a new plan",
        }
        if any(json.loads(row[0]).get("reason") == "schema_upgrade" for row in
               self.db.execute("SELECT data FROM workspace_claims WHERE state!='inactive'")):
            reasons["workspace_claims"] = ("Some tasks have not been re-checked since the upgrade. Start the server once or resume these tasks, then make a new plan")
        held = {key: {"items": 0, "bytes": 0, "reason": reasons[key]} for key in PROTECTED_CLASSES}
        held["delegation_contracts"].update(items=held_contracts, bytes=held_contract_bytes)
        held["dependency_plans"].update(items=self.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] - len(expired_plans), bytes=held_plan_bytes)
        def count(key, size):
            held[key]["items"] += 1
            held[key]["bytes"] += size or 0
        for table, expression in sizes.items():
            columns = "*" if table != "events" else "*,seq AS id"
            for row in self.db.execute(f"SELECT {columns},{expression} payload_bytes FROM {table}"):
                fields = ("task_id", "turn_id", "category", "item_key") if table == "evidence_entries" else ("task_id", "turn_id", "item_id") if table == "recovered_items" else None
                identity = [row[k] for k in fields] if fields else row["id"]
                if identity in selected[table]:
                    continue
                for reason, ids in families.items():
                    if row["task_id"] in ids:
                        count(reason, row["payload_bytes"])
                if ((table == "turn_results") or (table in ("artifacts", "evidence_entries") and (row["task_id"], row["turn_id"]) in retained_turns)
                        or (table == "events" and row["seq"] in result_sources)):
                    count("retained_results", row["payload_bytes"])
                if table == "managed_images" and row["state"] == "native_history":
                    count("native_image_history", row["payload_bytes"])
                if (not fields and row["id"] in metadata_refs) or (table == "managed_images" and row["path"] in metadata_refs):
                    count("unknown_metadata_references", row["payload_bytes"])
                if table == "operations" and row["id"] in unresolved and row["task_id"] not in families["unresolved_operations"]:
                    count("unresolved_operations", row["payload_bytes"])
        for row in self.db.execute("SELECT key,value FROM metadata"):
            if row["key"] not in expired_executions:
                count("plan_tombstones" if row["key"].startswith(EXPIRED_PREFIX) else "unknown_metadata_references", len(row["key"].encode()) + len(row["value"].encode()))
        for row in self.db.execute("SELECT * FROM operation_tombstones"):
            count("operation_tombstones", len(canonical(list(row))))
        directory = self.store.directory / "images"
        if directory.exists():
            checked_directory(directory)
            owned = {r[0] for r in self.db.execute("SELECT path FROM managed_images")}
            for path in directory.iterdir():
                if str(path) not in owned:
                    count("untracked_legacy_images", path.lstat().st_size)
        return selected, {"managed_payload_bytes": total, "retained_payload_bytes": remaining, "estimated_reclaimable_bytes": reclaimed,
                          "delegation_contract_bytes": contract_bytes, "delegation_contracts": contracts,
                          "protected_task_families": len(roots), "byte_target_met": max_bytes is None or remaining <= max_bytes,
                          "protected": held, "protected_counts_overlap": True}

    def _cleanup_plans(self):
        with self.store.transaction():
            self.db.execute("DELETE FROM retention_plans WHERE expires_at<=? OR committed_at IS NOT NULL", (self.clock(),))

    def plan(self, older_than, max_bytes=None):
        if not isinstance(older_than, (int, float)) or older_than <= 0 or (max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0)):
            raise BridgeError("Retention needs a positive age and a nonnegative byte target", code="invalid_arguments")
        now = self.clock()
        self._cleanup_plans()
        with self.store.transaction():
            existing = self.db.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(data AS BLOB))+LENGTH(CAST(policy AS BLOB))+LENGTH(snapshot)+LENGTH(id)),0) FROM retention_plans").fetchone()
            if existing[0] >= PLAN_LIMIT:
                raise BridgeError("Prune plan storage is full; apply a reviewed plan or wait for its expiry", code="prune_plan_limit")
            expired = expired_execution_keys(self.store, now)
            selected, summary = self._selection(now - older_than, max_bytes, expired_executions=expired)
            identity = str(uuid.uuid4())
            policy = {"older_than_seconds": older_than, "max_bytes": max_bytes, "cutoff": now - older_than}
            report = {"plan_id": identity, "dry_run": True, "expires_at": now + PLAN_SECONDS, "policy": policy,
                "counts": {k: len(v) for k, v in selected.items()}, **summary, "protected_classes": PROTECTED_CLASSES,
                "operation_tombstones_added": len(selected["operations"]),
                "plan_tombstones_added": len(selected["plans"]),
                "tombstones_kept": True, "vacuum_needed": bool(summary["estimated_reclaimable_bytes"])}
            snapshot, policy_text = self.snapshot(), json.dumps(policy)
            data = json.dumps({"selected": selected, "report": report, "expired_executions": expired})
            if existing[1] + len(data.encode()) + len(policy_text.encode()) + len(snapshot) + len(identity) > PLAN_STORAGE_BYTES:
                raise BridgeError("Prune plan storage is full; use a smaller selection or wait for plan expiry", code="prune_plan_limit")
            self.db.execute("INSERT INTO retention_plans VALUES (?,?,?,?,?,?,NULL)",
                (identity, now, now + PLAN_SECONDS, snapshot, policy_text, data))
        return report

    def commit(self, plan_id, *, compact=False):
        self._applied = False
        try:
            self._cleanup_plans()
            return self._commit(plan_id, compact=compact)
        except sqlite3.Error as exc:
            self.db.rollback()
            message = ("Prune committed, but follow-up cleanup or VACUUM failed; inspect state before further maintenance" if self._applied
                       else "Prune failed; no changes were committed. Keep the state directory and create a new dry-run plan")
            raise BridgeError(message, code="maintenance_failed", details={"applied": self._applied}, ids={"plan_id": plan_id}) from exc

    def _commit(self, plan_id, *, compact=False):
        with self.store.transaction():
            plan = self.db.execute("SELECT * FROM retention_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan or plan["committed_at"] is not None or self.clock() >= plan["expires_at"] or self.snapshot() != plan["snapshot"]:
                raise BridgeError("Prune plan is unknown, expired, already applied or changed; create a new dry-run plan", code="prune_plan_invalid", ids={"plan_id": plan_id})
            saved = json.loads(plan["data"])
            selected = saved["selected"]
            executions = selected.get("execution_metadata", [])
            deleted = delete_expired_execution_keys(self.store, executions, self.clock(), expected=saved.get("expired_executions", {}))
            if set(deleted) != set(executions):
                raise BridgeError("Expired MCP execution metadata changed; create a new dry-run plan", code="prune_plan_invalid", ids={"plan_id": plan_id})
            guidance_retention.apply(self.store, selected.get("delegation_contracts", []))
            for identity in selected.get("plans", []):
                outcome = self.db.execute("SELECT outcome FROM plans WHERE id=? AND done=1", (identity,)).fetchone()[0]
                value = {"plan_id": identity, "outcome": outcome, "pruned_at": self.clock()}
                self.db.execute("INSERT INTO metadata(key,value) VALUES (?,?)", (EXPIRED_PREFIX + identity, json.dumps(value)))
                self.db.execute("DELETE FROM plan_nodes WHERE plan_id=?", (identity,))
                self.db.execute("DELETE FROM plans WHERE id=? AND done=1", (identity,))
            for key in selected["managed_images"]:
                row = self.db.execute("SELECT * FROM managed_images WHERE id=?", (key,)).fetchone()
                if not self._image_matches(row):
                    raise BridgeError("Managed image changed after the prune plan; create a new dry-run plan", code="prune_plan_invalid", ids={"plan_id": plan_id})
            # Full snapshot validation includes references, revisions and unknown metadata.
            # Keep a family's own execution identity when its older input/ACK
            # evidence expires. Native history copied by forks is not proof.
            for _, task in self.store.records("tasks", light=True):
                if task.get("workspace_execution_project") == task["project"]:
                    continue
                owned = self.db.execute("""SELECT 1 FROM operation_details WHERE task_id=?
                    AND json_extract(data,'$.operation_outcome.acknowledgement')='acknowledged'
                    AND json_extract(data,'$.acknowledged_turn.turn_id') IS NOT NULL LIMIT 1""", (task["id"],)).fetchone()
                if owned:
                    self.store.update("tasks", task["id"], {"workspace_execution_project": task["project"]})
            for key in selected["operations"]:
                self.db.execute("INSERT INTO operation_tombstones SELECT id,fingerprint,task_id,? FROM operations WHERE id=?", (self.clock(), key))
                self.db.execute("DELETE FROM operations WHERE id=?", (key,))
                self.db.execute("DELETE FROM operation_details WHERE id=?", (key,))
            expired_turns = set()
            for key in selected["turn_results"]:
                row = self.db.execute("SELECT task_id,turn_id FROM turn_results WHERE id=?", (key,)).fetchone()
                expired_turns.add(tuple(row))
            for key in selected["evidence_entries"]:
                self.db.execute("DELETE FROM evidence_entries WHERE task_id=? AND turn_id=? AND category=? AND item_key=?", key)
                expired_turns.add(tuple(key[:2]))
            changed_tasks = {task_id for task_id, _ in expired_turns}
            for key in selected["requests"]:
                row = self.db.execute("SELECT task_id FROM requests WHERE id=?", (key,)).fetchone()
                changed_tasks.add(row[0])
            for table in ("turn_results", "requests", "artifacts"):
                self.db.executemany(f"DELETE FROM {table} WHERE id=?", ((key,) for key in selected[table]))
            for task_id, turn_id in expired_turns:
                if not self.db.execute("SELECT 1 FROM turn_results WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone():
                    self.db.execute("INSERT OR REPLACE INTO result_expirations VALUES (?,?,?)", (task_id, turn_id, self.clock()))
            first = last = owner = None
            for seq in sorted(selected["events"]):
                event_owner = self.db.execute("SELECT task_id FROM events WHERE seq=?", (seq,)).fetchone()[0]
                if last is not None and (seq != last + 1 or owner != event_owner):
                    self.db.execute("INSERT OR IGNORE INTO expired_event_ranges VALUES (?,?,?)", (first, last, owner))
                    first = None
                if first is None:
                    first, owner = seq, event_owner
                last = seq
            if last is not None:
                self.db.execute("INSERT OR IGNORE INTO expired_event_ranges VALUES (?,?,?)", (first, last, owner))
            if "recovered_items" not in selected and selected["events"]:
                raise BridgeError("Recovery cache selection is missing; create a new dry-run plan", code="prune_plan_invalid", ids={"plan_id": plan_id})
            self.db.executemany("DELETE FROM recovered_items WHERE task_id=? AND turn_id=? AND item_id=?", selected.get("recovered_items", []))
            self.db.executemany("DELETE FROM events WHERE seq=?", ((key,) for key in selected["events"]))
            self.db.executemany("UPDATE managed_images SET state='pending_delete',revision=revision+1 WHERE id=?", ((key,) for key in selected["managed_images"]))
            if selected["requests"] or compact:
                self.db.execute("UPDATE retention_state SET request_epoch=request_epoch+1 WHERE id=1")
            if any(selected.values()) or compact:
                self.db.execute("UPDATE supervision_state SET retention_epoch=retention_epoch+1 WHERE id=1")
            for task_id in changed_tasks:
                touch(self.store, task_id)
            self.db.execute("DELETE FROM retention_plans WHERE id=?", (plan_id,))
        self._applied = True
        # File cleanup follows the atomic DB commit. A crash leaves only harmless
        # private files registered as pending_delete, retried on the next apply.
        cleanup_failures, image_reclaimed, planned_image_bytes = [], 0, 0
        for row in self.db.execute("SELECT * FROM managed_images WHERE state='pending_delete'").fetchall():
            if row["id"] not in selected["managed_images"]:
                continue
            planned_image_bytes += row["size_bytes"]
            path = Path(row["path"])
            try:
                if path.parent != self.store.directory / "images":
                    raise BridgeError("Managed image path is outside the private image directory", code="state_invalid")
                checked_directory(path.parent)
                if path.exists() or path.is_symlink():
                    if not self._image_matches(row):
                        raise BridgeError("Managed image changed during cleanup", code="prune_plan_invalid")
                    os.close(private_file(path, os.O_RDONLY))
                    path.unlink()
                    image_reclaimed += row["size_bytes"]
                with self.store.transaction():
                    self.db.execute("DELETE FROM managed_images WHERE id=? AND state='pending_delete'", (row["id"],))
            except (OSError, BridgeError):
                cleanup_failures.append(row["id"])
        if compact:
            self.db.execute("VACUUM")
        return {**saved["report"], "dry_run": False, "applied": True, "compacted": compact,
                "vacuum_needed": saved["report"]["vacuum_needed"] and not compact, "image_cleanup_pending": cleanup_failures,
                "reclaimed_payload_bytes": saved["report"]["estimated_reclaimable_bytes"] - planned_image_bytes + image_reclaimed}

    def _image_matches(self, row):
        path = Path(row["path"])
        if path.parent != self.store.directory / "images":
            return False
        try:
            checked_directory(path.parent)
            if not path.exists() and not path.is_symlink():
                return True
            with os.fdopen(private_file(path, os.O_RDONLY), "rb") as stream:
                if os.fstat(stream.fileno()).st_size != row["size_bytes"]:
                    return False
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            return digest == row["sha256"]
        except (OSError, BridgeError):
            return False


def readable_bytes(size):
    if size < 1024:
        return f"{size} B"
    for exponent, unit in enumerate(("KiB", "MiB", "GiB", "TiB", "PiB"), 1):
        if size < 1024 ** (exponent + 1) or unit == "PiB":
            return f"{size / 1024 ** exponent:.1f} {unit} ({size:,} bytes)"


def render(report, *, now=None):
    if "backup" in report:
        return f"Backup: {report['backup']} ({report['size_bytes']} bytes; integrity_check=ok)"
    size = report["estimated_reclaimable_bytes"] if report["dry_run"] else report["reclaimed_payload_bytes"]
    label = "Reclaimable" if report["dry_run"] else "Reclaimed"
    expires = datetime.fromtimestamp(report["expires_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
    remaining = max(0, math.ceil((report["expires_at"] - (time.time() if now is None else now)) / 60))
    lines = [f"Prune plan: {report['plan_id']} ({'dry run' if report['dry_run'] else 'applied'})",
             f"{label} payload: {readable_bytes(size)}; estimated retained: {readable_bytes(report['retained_payload_bytes'])}",
             f"VACUUM needed: {str(report['vacuum_needed']).lower()}; expiry: {expires} ({remaining} min left)"]
    lines += [f"{key}: {count}" + (f" ({readable_bytes(report['delegation_contract_bytes'])})" if key == "delegation_contracts" else "")
              for key, count in report["counts"].items()]
    lines += [f"Contract task {entry['task_id']}: {entry['context_hash']}" for entry in report.get("delegation_contracts", [])]
    for key, value in report["protected"].items():
        if not value["items"]:
            continue
        lines.append(f"Protected {key}: {value['items']} items, {readable_bytes(value['bytes'])}; {value['reason']}")
    if report["dry_run"]:
        lines.append(f"Commit with prune --apply --plan-id {report['plan_id']}; --compact explicitly runs VACUUM")
    if report.get("image_cleanup_pending"):
        lines.append("Image cleanup pending: " + ", ".join(report["image_cleanup_pending"]))
        lines.append("Close programs holding these private image files; create a new dry-run plan to retry cleanup")
    return "\n".join(lines)
