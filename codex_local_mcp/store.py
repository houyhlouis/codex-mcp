"""A single broker owns the SQLite connection. Events and state commit together."""

import json
import hashlib
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .security import BridgeError, private_file, private_dir, redact
from .lifecycle import ACTIVE, STATUSES, TERMINAL
from .tools import json_text, result_size, request_summary, DETAIL_CHARS
from .evidence import Evidence, migrate as migrate_evidence, observe
from .supervision import migrate as migrate_supervision, touch, STREAM_ONLY
from .decisions import public_summary
from .resources import Resources, migrate as migrate_resources, migrate_projections as migrate_resource_projections
from .execution_plans import migrate as migrate_plans

SCHEMA_VERSION = 11
APPLICATION_ID = 0x434D4350
BACKUP_LIMIT = 3
SCHEMA_V1 = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE projects (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL)",
    "CREATE INDEX events_task ON events(task_id,seq)",
    "CREATE TABLE requests (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT, task_id TEXT)",
)
V1_COLUMNS = {"tasks": {"id", "data"}, "projects": {"id", "data"},
              "metadata": {"key", "value"}, "requests": {"id", "data"},
              "events": {"seq", "task_id", "kind", "at", "data"},
              "operations": {"id", "fingerprint", "state", "result", "task_id"}}


def project_record(path):
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-mcp:" + os.path.normcase(str(path))))
    return {"id": key, "project_id": key, "path": str(path), "created_at": time.time()}


def migrate_projects(db):
    for row in db.execute("SELECT id,data FROM tasks").fetchall():
        task = json.loads(row["data"])
        if not task.get("project_id"):
            project = project_record(task["project"])
            db.execute("INSERT OR IGNORE INTO projects(id,data) VALUES (?,?)", (project["id"], json.dumps(project)))
            task["project_id"] = project["id"]
            task.pop("root", None)
            task.pop("path", None)
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), row["id"]))
    db.execute("DELETE FROM metadata WHERE key='workspace_scope'")


def migrate_revisions(db):
    for table in ("tasks", "requests", "projects"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    db.execute("""CREATE TABLE turns (
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT NOT NULL,
        error TEXT, UNIQUE(task_id,turn_id))""")
    for row in db.execute("SELECT id,data FROM tasks").fetchall():
        task = json.loads(row["data"])
        task.update(projection_turn_id=task.get("turn_id"), evidence_complete=False,
                    evidence_gaps=["legacy_state"])
        if task.get("thread_id") and task.get("turn_id"):
            status = task["status"] if task["status"] in TERMINAL else "inProgress"
            task["turn_status"] = status
            db.execute("INSERT INTO turns(task_id,thread_id,turn_id,status,error) VALUES (?,?,?,?,?)",
                       (task["id"], task["thread_id"], task["turn_id"], status, json.dumps(task.get("error"))))
        db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task), row["id"]))


def migrate_admissions(db):
    db.execute("ALTER TABLE turns ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    db.execute("""CREATE TABLE admissions (
        operation_id TEXT PRIMARY KEY, family_id TEXT NOT NULL, project TEXT NOT NULL)""")


def migrate_claims(db):
    db.execute("""CREATE TABLE workspace_claims (
        task_id TEXT NOT NULL, kind TEXT NOT NULL, family_id TEXT NOT NULL,
        project TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
        revision INTEGER NOT NULL, PRIMARY KEY(task_id,kind))""")
    db.execute("CREATE INDEX claims_family ON workspace_claims(family_id,state)")
    # Older releases did not persist native goals or background ownership.
    for row in db.execute("SELECT data FROM tasks").fetchall():
        task = json.loads(row[0])
        if task.get("thread_id") and not task.get("deleted") and not task.get("archived"):
            db.execute("INSERT INTO workspace_claims VALUES (?,?,?,?,?,?,1)",
                (task["id"], "uncertain", task.get("root_task_id", task["id"]), task["project"],
                 "uncertain", json.dumps({"reason": "schema_upgrade"})))


TASK_COLUMNS = ("thread_id", "parent_task_id", "root_task_id", "status", "project_id", "project", "archived", "deleted")
REQUEST_COLUMNS = ("task_id", "status", "kind", "generation", "rpc_key", "thread_id", "turn_id", "item_id")
TASK_HEAD = frozenset("id thread_id parent_task_id root_task_id project_id project status turn_id turn_status "
    "projection_turn_id created_at updated_at archived deleted name nickname agent_role native_agent_path "
    "native_cwd native_status native_child verified_generation session_id can_accept_direct_input approval_mode approvals_reviewer "
    "model effort collaboration_mode service_tier access approval_policy evidence_complete evidence_gaps "
    "diff_turn_id active_settings_turn_id turn_start queue_reservation usage_scope delegation_contract_summary process_state background_observation process_scan workspace_execution_project".split())


def indexed_values(table, value, revision):
    if table == "tasks":
        identity = value.get("thread_id")
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise BridgeError("Invalid task native thread identity", code='state_invalid')
        fields = [value.get(k) for k in TASK_COLUMNS]
        fields[2] = value.get("root_task_id") or value["id"]
        fields[-2:] = [int(bool(value.get(k))) for k in ("archived", "deleted")]
        head = {k: v for k, v in value.items() if k in TASK_HEAD}
        if head.get("process_state"):
            head["process_state"] = {k: v for k, v in head["process_state"].items() if k not in ("commands_preview", "commands_truncated")}
        if head.get("background_observation"):
            head["background_observation"] = {k: v for k, v in head["background_observation"].items() if k != "commands_preview"}
        head["counts"] = {k: len(value.get(k) or []) for k in ("files", "commands", "plan")}
        head["has_diff"] = bool(value.get("diff"))
        head["has_error"] = bool(value.get("error"))
    else:
        params = value.get("params") or {}
        fields = [value.get("task_id"), value.get("status"), value.get("kind"), str(value["generation"]) if value.get("generation") is not None else None,
                  json.dumps(value.get("rpc_id")), *(params.get(k) for k in ("threadId", "turnId", "itemId"))]
        head = request_summary({**{k: v for k, v in value.items() if k not in ("rpc_id", "generation")}, "revision": revision})
    head["revision"] = revision
    return (*fields, json.dumps(head))


def migrate_indexes(db):
    for table, columns in (("tasks", TASK_COLUMNS), ("requests", REQUEST_COLUMNS)):
        for column in columns:
            kind = "INTEGER NOT NULL DEFAULT 0" if column in ("archived", "deleted") else "TEXT"
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        db.execute(f"ALTER TABLE {table} ADD COLUMN projection TEXT NOT NULL DEFAULT '{{}}'")
        for row in db.execute(f"SELECT id,data,revision FROM {table}"):
            value = json.loads(row["data"])
            assignments = ",".join(f"{c}=?" for c in (*columns, "projection"))
            db.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*indexed_values(table, value, row["revision"]), row["id"]))
    duplicate = db.execute("SELECT thread_id FROM tasks WHERE thread_id IS NOT NULL GROUP BY thread_id HAVING COUNT(*)>1 LIMIT 1").fetchone()
    if duplicate:
        owners = [r[0] for r in db.execute("SELECT id FROM tasks WHERE thread_id=? ORDER BY id LIMIT 32", (duplicate[0],))]
        raise BridgeError("Duplicate native thread id in task records; keep the database and backup and repair the conflicting task identities before retrying",
                          code="duplicate_thread_id", ids={"thread_id": duplicate[0], "task_ids": owners})
    for statement in (
        "CREATE UNIQUE INDEX tasks_native_thread ON tasks(thread_id) WHERE thread_id IS NOT NULL",
        "CREATE INDEX tasks_parent ON tasks(parent_task_id)", "CREATE INDEX tasks_root ON tasks(root_task_id,status)",
        "CREATE INDEX tasks_status ON tasks(status)", "CREATE INDEX tasks_project ON tasks(project_id,archived,deleted)",
        "CREATE INDEX tasks_visible ON tasks(deleted)", "CREATE INDEX requests_pending ON requests(status,task_id)",
        "CREATE INDEX requests_native ON requests(generation,rpc_key,status)",
        "CREATE INDEX requests_proposal ON requests(thread_id,turn_id,item_id,kind,status)",
        "CREATE INDEX claims_active ON workspace_claims(task_id) WHERE state!='inactive'",
        "CREATE INDEX claims_live_family ON workspace_claims(family_id,task_id) WHERE state!='inactive'"):
        db.execute(statement)


MIGRATIONS = {2: ("project_ids", migrate_projects), 3: ("record_revisions_and_turns", migrate_revisions),
              4: ("operation_admissions_and_turn_revisions", migrate_admissions),
              5: ("workspace_claims", migrate_claims), 6: ("indexed_identity_and_projections", migrate_indexes),
              7: ("retention_and_turn_evidence", migrate_evidence),
              8: ("reconciliation_outcomes_and_receipts", migrate_supervision),
              9: ("family_resource_accounting_and_budgets", migrate_resources),
              10: ("resource_budget_projections", migrate_resource_projections),
              11: ("dependency_plans", migrate_plans)}


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        path = directory / "state.sqlite3"
        private_dir(directory)
        version, empty = 0, True
        if path.exists():
            os.close(private_file(path, os.O_RDONLY))
            # Inspect before any write, including journal-mode changes. Unknown
            # newer layouts must remain available to the release that owns them.
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as probe:
                    version, empty = self._inspect(probe)
            except sqlite3.DatabaseError as exc:
                raise BridgeError("Unrecognized state database layout; keep this directory and use a compatible codex-mcp release", code='state_invalid') from exc
        os.close(private_file(path, os.O_CREAT | os.O_RDWR))
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.backup_path = None
        self._savepoint = 0
        self._bookkeeping = []
        try:
            if not empty and version < SCHEMA_VERSION:
                self.backup_path = self._backup(directory, version)
            if empty or version < SCHEMA_VERSION:
                self._migrate(empty, version)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            self.db.close()
            raise
        self.schema_version = SCHEMA_VERSION
        self.directory = directory
        self.evidence = Evidence(self)
        self.resources = Resources(self) if self.schema_version >= 9 else None

    @staticmethod
    def _inspect(db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        application = db.execute("PRAGMA application_id").fetchone()[0]
        if version < 0:
            raise BridgeError("Unrecognized state database layout; keep this directory and use a compatible codex-mcp release", code='state_invalid')
        if version > SCHEMA_VERSION:
            raise BridgeError(f"State database schema version {version} is newer than supported version {SCHEMA_VERSION}. "
                "Use a newer codex-mcp release, or restore a pre-upgrade backup into a separate state directory. This database was not changed", code='state_newer_schema')
        if application not in (0, APPLICATION_ID):
            raise BridgeError("State database belongs to another application; select a separate state directory", code='state_invalid')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not tables and version == 0:
            return 0, True
        if not all(name in tables and columns <= {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
                   for name, columns in V1_COLUMNS.items()):
            raise BridgeError("Unrecognized state database layout; keep this directory and use a compatible codex-mcp release", code='state_invalid')
        if version >= 2:
            if application != APPLICATION_ID or "schema_migrations" not in tables:
                raise BridgeError("State database migration ledger is missing or inconsistent; restore a pre-upgrade backup into a separate state directory", code='state_invalid')
            applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
            if applied != set(range(1, version + 1)):
                raise BridgeError("State database migration ledger is missing or inconsistent; restore a pre-upgrade backup into a separate state directory", code='state_invalid')
        required = {}
        if version >= 3:
            required = {table: {"revision"} for table in ("tasks", "requests", "projects")}
            required["turns"] = {"ordinal", "task_id", "thread_id", "turn_id", "status", "error"}
        if version >= 4:
            required["turns"].add("revision")
            required["admissions"] = {"operation_id", "family_id", "project"}
        if version >= 5:
            required["workspace_claims"] = {"task_id", "kind", "family_id", "project", "state", "data", "revision"}
        if version >= 6:
            required["tasks"].update((*TASK_COLUMNS, "projection"))
            required["requests"].update((*REQUEST_COLUMNS, "projection"))
        if version >= 7:
            required.update({"artifacts": {"id", "task_id", "turn_id", "kind", "sha256", "content", "file_version", "redaction_count", "source_locator"},
                             "turn_results": {"id", "task_id", "turn_id", "revision", "data"},
                             "evidence_entries": {"task_id", "turn_id", "category", "item_key", "data"},
                             "managed_images": {"id", "path", "state", "revision"},
                             "operation_tombstones": {"id", "fingerprint"}, "operation_times": {"id", "updated_at"},
                             "retention_plans": {"id", "expires_at", "snapshot", "data"},
                             "expired_event_ranges": {"first_seq", "last_seq", "task_id"},
                             "result_expirations": {"task_id", "turn_id", "expired_at"},
                             "retention_state": {"id", "request_epoch", "historical_turn_ordinal"},
                             "evidence_history": {"task_id", "turn_id", "status"}})
        if version >= 8:
            required.update({"supervision_state": {"id", "identity", "counter", "retention_epoch"},
                "task_changes": {"task_id", "own_seq", "family_seq"},
                "reconciliation_reports": {"task_id", "revision", "task_revision", "created_at", "data"},
                "recovered_items": {"task_id", "turn_id", "item_id", "sha256", "source_seq"},
                "operation_details": {"id", "task_id", "name", "created_at", "data"},
                "decision_receipts": {"id", "request_id", "task_id", "turn_id", "created_at", "data"}})
        if version >= 9:
            required.update({"resource_families": {"family_id", "budget", "stop", "revision"},
                "resource_threads": {"task_id", "thread_id", "tokens", "token_turn", "gaps", "historical", "goal"},
                "resource_turns": {"task_id", "turn_id", "started_at", "ended_at", "seconds", "seconds_source", "connection", "server_started", "unknown"},
                "resource_reservations": {"id", "family_id", "task_id", "turns", "children", "state", "token", "native_key", "turn_id", "after_turn_id", "budget", "queue_id", "queue_owner"},
                "resource_storage": {"task_id", "journal_bytes", "artifact_bytes", "initialized"},
                "resource_interrupts": {"family_id", "task_id", "turn_id", "state"}})
        if version >= 10:
            required["resource_threads"].add("token_baseline")
            required["resource_totals"] = {"task_id", "server_turns", "native_turns", "closed_seconds", "closed_unknown", "observed_turns"}
        if version >= 11:
            required["plans"] = {"id", "spec", "created_at", "updated_at", "revision", "done", "outcome", "cancel_requested", "decision"}
            required["plan_nodes"] = {"plan_id", "node_id", "position", "status", "since", "task_id", "turn_id", "operation_id", "attempt", "result_ref", "data"}
        if any(not columns <= {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
               for table, columns in required.items()):
            raise BridgeError("Unrecognized state database layout; keep this directory and use a compatible codex-mcp release", code='state_invalid')
        return version or 1, False

    def _backup(self, directory, version):
        path = directory / f"state.sqlite3.pre-v{version}-{time.time_ns()}.bak"
        created = False
        try:
            descriptor = private_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            created = True
            os.close(descriptor)
            with closing(sqlite3.connect(path)) as destination:
                self.db.backup(destination)
            backups = [p for p in directory.glob("state.sqlite3.pre-v*-*.bak")
                       if p != path and p.name.rsplit("-", 1)[-1].removesuffix(".bak").isdigit()]
            backups.sort(key=lambda p: int(p.name.rsplit("-", 1)[-1].removesuffix(".bak")), reverse=True)
            for old in backups[BACKUP_LIMIT - 1:]:
                if old.is_file() and not old.is_symlink():
                    old.unlink()
            return path
        except BaseException as exc:
            if created:
                path.unlink(missing_ok=True)
            if not isinstance(exc, Exception):
                raise
            raise BridgeError("Could not back up state database; no migration was applied", code='state_migration_failed') from exc

    def _migrate(self, empty, version):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if empty:
                for statement in SCHEMA_V1:
                    self.db.execute(statement)
                version = 1
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1,'v0.1.1',?)", (time.time(),))
            for number in range(version + 1, SCHEMA_VERSION + 1):
                name, migrate = MIGRATIONS[number]
                migrate(self.db)
                self.db.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (number, name, time.time()))
                self.db.execute(f"PRAGMA user_version={number}")
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.commit()
        except BaseException as exc:
            self.db.rollback()
            if not isinstance(exc, Exception):
                raise
            detail = f" {exc}" if isinstance(exc, BridgeError) else ""
            raise BridgeError("State database migration failed; previous state was preserved. Keep this directory and any pre-migration backup; retry with a fixed release" + detail,
                code="duplicate_thread_id" if isinstance(exc, BridgeError) and exc.code == "duplicate_thread_id" else "state_migration_failed",
                ids=exc.ids if isinstance(exc, BridgeError) else None) from exc

    @contextmanager
    def transaction(self):
        check = getattr(self, "before_write", None)
        if check is not None:
            check()
        self._savepoint += 1
        name = f"state_{self._savepoint}"
        before = self.db.total_changes
        self.db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO {name}")
            self.db.execute(f"RELEASE {name}")
            raise
        else:
            self.db.execute(f"RELEASE {name}")
        finally:
            callback = getattr(self, "on_write", None)
            if callback is not None and self.db.total_changes != before:
                callback()

    @staticmethod
    def _record(table, value, key=None):
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"] or (key is not None and value["id"] != key):
            raise BridgeError(f"Invalid {table[:-1]} record identity", code='state_invalid')
        if "status" in value and not isinstance(value["status"], str):
            raise BridgeError(f"Invalid {table[:-1]} status", code='state_invalid')
        if table == "tasks" and "status" in value and value["status"] not in STATUSES:
            raise BridgeError("Unknown task lifecycle status", code='state_invalid')
        if "turn_id" in value and value["turn_id"] is not None and not isinstance(value["turn_id"], str):
            raise BridgeError(f"Invalid {table[:-1]} turn identity", code='state_invalid')
        return value

    def _decode(self, table, row):
        try:
            value = self._record(table, json.loads(row["data"]), row["id"])
        except (ValueError, TypeError) as exc:
            raise BridgeError(f"Invalid stored {table[:-1]} JSON", code='state_invalid') from exc
        if type(row["revision"]) is not int or row["revision"] < 1:
            raise BridgeError(f"Invalid {table[:-1]} revision", code='state_invalid')
        return {**value, "revision": row["revision"]}

    def save(self, table, value):
        with self.transaction():
            return self._save(table, value)

    def _save(self, table, value):
        assert table in ("tasks", "requests", "projects")
        value = dict(self._record(table, value))
        row = self.db.execute(f"SELECT revision FROM {table} WHERE id=?", (value["id"],)).fetchone()
        if row is not None and value.get("revision") != row[0]:
            raise BridgeError(f"Stale {table[:-1]} revision; reload before updating", code='state_invalid')
        if table == "tasks" and value.get("project") and not value.get("project_id"):
            project = project_record(value["project"])
            if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (project["id"],)).fetchone():
                self.save("projects", project)
            value["project_id"] = project["id"]
            value.pop("root", None)
            value.pop("path", None)
        revision = row[0] + 1 if row else 1
        value.pop("revision", None)
        stored = redact(value)
        columns = (*(TASK_COLUMNS if table == "tasks" else REQUEST_COLUMNS), "projection") if table != "projects" else ()
        values = indexed_values(table, stored, revision) if columns else ()
        names = ("id", "data", "revision", *columns)
        try:
            cursor = self.db.execute(f"INSERT INTO {table}({','.join(names)}) VALUES ({','.join('?' for _ in names)}) ON CONFLICT(id) DO UPDATE SET " +
                ",".join(f"{c}=excluded.{c}" for c in names[1:]) + f" WHERE {table}.revision=?",
                (value["id"], json.dumps(stored), revision, *values, row[0] if row else 0))
        except sqlite3.IntegrityError as exc:
            raise BridgeError("Duplicate native thread id; a native conversation can belong to only one task", code='duplicate_thread_id') from exc
        if cursor.rowcount != 1:
            raise BridgeError(f"Stale {table[:-1]} revision; reload before updating", code='state_invalid')
        self._remember_bookkeeping(table, value["id"], revision, False)
        if table in ("tasks", "requests"):
            touch(self, value["id"] if table == "tasks" else value.get("task_id"))
        return {**value, "revision": revision}

    def get(self, table, key):
        assert table in ("tasks", "requests", "projects")
        row = self.db.execute(f"SELECT id,data,revision FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise BridgeError(f"Unknown {table[:-1]} id", code='not_found')
        return self._decode(table, row)

    def update(self, table, key, fields, *, expected=None):
        """Change owned fields on the latest record, without an intervening await.

        The broker owns this connection on one event loop. Callers keep this
        synchronous operation inside their event/state transaction. A condition
        mismatch returns None instead of rolling a newer projection backwards.
        """
        with self.transaction():
            return self._update(table, key, fields, expected=expected)

    def _update(self, table, key, fields, *, expected=None):
        value = self.get(table, key)
        if expected is not None and any(value.get(k) != v for k, v in expected.items()):
            return None
        if "id" in fields and fields["id"] != key:
            raise BridgeError("Record identity cannot change", code='state_invalid')
        revision = value["revision"]
        bookkeeping = table == "tasks" and bool(self._bookkeeping) and all(k in ("revision", "updated_at", "process_scan", "projection_sources")
            or k in value and value[k] == v for k, v in fields.items())
        value.update({k: v for k, v in fields.items() if k != "revision"})
        self._record(table, value, key)
        value.pop("revision")
        stored = redact(value)
        columns = (*(TASK_COLUMNS if table == "tasks" else REQUEST_COLUMNS), "projection") if table != "projects" else ()
        values = indexed_values(table, stored, revision + 1) if columns else ()
        try:
            cursor = self.db.execute(f"UPDATE {table} SET data=?,revision=revision+1" +
                ''.join(f",{c}=?" for c in columns) + " WHERE id=? AND revision=?", (json.dumps(stored), *values, key, revision))
        except sqlite3.IntegrityError as exc:
            raise BridgeError("Duplicate native thread id; a native conversation can belong to only one task", code='duplicate_thread_id') from exc
        if cursor.rowcount != 1:
            raise BridgeError(f"Stale {table[:-1]} revision; reload before updating", code='state_invalid')
        self._remember_bookkeeping(table, key, revision + 1, bookkeeping)
        if table in ("tasks", "requests"):
            touch(self, key if table == "tasks" else value.get("task_id"))
        return {**value, "revision": revision + 1}

    def _remember_bookkeeping(self, table, key, revision, internal):
        identity = (table, key, revision)
        task_id = key[0] if table == "claims" else key
        for ids, writes in self._bookkeeping:
            if task_id in ids:
                writes.discard(identity)  # A rolled-back revision may be reused.
                if internal:
                    writes.add(identity)

    @contextmanager
    def bookkeeping_scope(self, task_ids):
        # Only active revision guards retain this small write provenance. No
        # task payloads or historical records are copied into the observer.
        scope = (frozenset(task_ids), set())
        self._bookkeeping.append(scope)
        try:
            yield
        finally:
            self._bookkeeping = [active for active in self._bookkeeping if active is not scope]

    def bookkeeping_only(self, table, key, before, after):
        """Accept only a complete, locally observed chain; missing history fails closed."""
        return after >= before and all(any((table, key, revision) in writes for _, writes in self._bookkeeping)
            for revision in range(before + 1, after + 1))

    def all(self, table):
        assert table in ("tasks", "requests", "projects")
        return [self._decode(table, row) for row in self.db.execute(f"SELECT id,data,revision FROM {table} ORDER BY rowid")]

    def task_head(self, key):
        row = self.db.execute("SELECT id,revision,projection FROM tasks WHERE id=?", (key,)).fetchone()
        if row is None:
            raise BridgeError("Unknown task id", code='not_found')
        return self._projection("tasks", row)

    def task_for_thread(self, thread_id):
        row = self.db.execute("SELECT id,revision,projection FROM tasks WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        value = self._projection("tasks", row)
        if value.get("thread_id") != thread_id:
            raise BridgeError("Indexed task identity is inconsistent; check/rebuild storage projections", code="state_invalid")
        return value

    def _projection(self, table, row):
        try:
            value = self._record(table, json.loads(row["projection"]), row["id"])
        except (ValueError, TypeError) as exc:
            raise BridgeError("Invalid stored projection; check/rebuild storage projections", code="state_invalid") from exc
        if type(value.get("revision")) is not int or value["revision"] != row["revision"] or row["revision"] < 1:
            raise BridgeError("Indexed record revision is inconsistent; check/rebuild storage projections", code="state_invalid")
        return public_summary(value) if table == "requests" else value

    @staticmethod
    def family_query():
        return "WITH RECURSIVE family(id,depth) AS (SELECT ?,0 UNION ALL SELECT t.id,f.depth+1 FROM tasks t JOIN family f ON t.parent_task_id=f.id WHERE f.depth<32) "

    def family_ids(self, task_id):
        return {r[0] for r in self.db.execute(self.family_query() + "SELECT DISTINCT id FROM family", (task_id,))}

    def records(self, table, *, statuses=None, task_id=None, family_id=None, kind=None, generation=None,
                rpc_id=None, thread_id=None, turn_id=None, item_id=None, light=False, after=None, reverse=False,
                limit=None, offset=0, visible=False, recovery=False, exclude_id=None, archived=None, sort_ids=False):
        assert table in ("tasks", "requests", "projects")
        where, args = ["1=1"], []
        if after is not None:
            where.append("r.rowid>?")
            args.append(after)
        prefix = ""
        if family_id is not None:
            prefix = self.family_query()
            args.insert(0, family_id)
            where.append("r." + ("id" if table == "tasks" else "task_id") + " IN (SELECT id FROM family)")
        filters = {"status": statuses, "task_id": [task_id] if task_id else None, "kind": [kind] if kind else None,
                   "generation": [str(generation)] if generation is not None else None,
                   "rpc_key": [json.dumps(rpc_id)] if rpc_id is not None else None,
                   "thread_id": [thread_id] if thread_id else None, "turn_id": [turn_id] if turn_id else None,
                   "item_id": [item_id] if item_id else None}
        for name, values in filters.items():
            if values is not None:
                where.append(f"r.{name} IN ({','.join('?' for _ in values)})")
                args.extend(values)
        if visible:
            where.append("r.deleted=0")
        if archived is not None:
            where.append("r.archived=?")
            args.append(int(archived))
        if exclude_id is not None:
            where.append("r.id!=?")
            args.append(exclude_id)
        if recovery:
            where.append("r.id IN (SELECT id FROM tasks WHERE status IN (" + ','.join('?' for _ in ACTIVE) + ") UNION SELECT task_id FROM workspace_claims WHERE state!='inactive')")
            args.extend(sorted(ACTIVE))
        columns = "r.rowid AS position,r.id,r.revision,r.projection" if light else "r.rowid AS position,r.id,r.data,r.revision"
        sql = prefix + f"SELECT {columns} FROM {table} r WHERE " + " AND ".join(where) + (" ORDER BY r.id" if sort_ids else " ORDER BY r.rowid DESC" if reverse else " ORDER BY r.rowid")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend((limit, offset))
        for row in self.db.execute(sql, args):
            yield row["position"], self._projection(table, row) if light else self._decode(table, row)

    def request_count(self, family_id=None):
        if family_id is None:
            return self.db.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]
        return self.db.execute(self.family_query() + "SELECT COUNT(*) FROM requests WHERE status='pending' AND task_id IN (SELECT id FROM family)", (family_id,)).fetchone()[0]

    def family_stats(self, task_id):
        row = self.db.execute(self.family_query() + "SELECT COUNT(*) AS total,MAX(status IN (" + ','.join('?' for _ in ACTIVE) + ")) AS active FROM tasks WHERE id IN (SELECT id FROM family)", (task_id, *sorted(ACTIVE))).fetchone()
        return row["total"], bool(row["active"])

    def claim_task_ids(self):
        return [r[0] for r in self.db.execute("SELECT DISTINCT task_id FROM workspace_claims WHERE state!='inactive'")]

    def unscanned_archives(self):
        rows = self.db.execute("""SELECT id,revision,projection FROM tasks
            WHERE archived=1 AND deleted=0 AND thread_id IS NOT NULL
            AND json_extract(projection,'$.process_scan.version') IS NOT 1
            AND EXISTS (SELECT 1 FROM json_each(tasks.projection,'$.evidence_gaps') WHERE value='legacy_state')
            AND NOT EXISTS (SELECT 1 FROM workspace_claims c WHERE c.task_id=tasks.id AND state!='inactive')""")
        for row in rows:
            yield self._projection("tasks", row)

    def check_projections(self, *, rebuild=False):
        """Check derived selectors; an explicit repair preserves JSON and revisions."""
        changed = []
        with self.transaction():
            for table, columns in (("tasks", TASK_COLUMNS), ("requests", REQUEST_COLUMNS)):
                for row in self.db.execute(f"SELECT * FROM {table}"):
                    expected = indexed_values(table, self._decode(table, row), row["revision"])
                    if tuple(row[c] for c in (*columns, "projection")) != expected:
                        changed.append({"table": table, "id": row["id"]})
                        if rebuild:
                            self.db.execute(f"UPDATE {table} SET " + ','.join(f"{c}=?" for c in (*columns, "projection")) + " WHERE id=?", (*expected, row["id"]))
        return {"consistent": not changed, "mismatches": changed, "rebuilt": bool(changed and rebuild)}

    def claims(self, *, task_id=None, family_id=None, include_inactive=False, offset=0, limit=None):
        query, args = "SELECT * FROM workspace_claims WHERE 1=1", []
        if task_id is not None:
            query += " AND task_id=?"
            args.append(task_id)
        if family_id is not None:
            query += " AND family_id=?"
            args.append(family_id)
        if not include_inactive:
            query += " AND state!='inactive'"
        query += " ORDER BY task_id,kind"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            args.extend((limit, offset))
        return [{**dict(row), "data": json.loads(row["data"])} for row in self.db.execute(query, args)]

    def claim_count(self, family_id):
        return self.db.execute("SELECT COUNT(*) FROM workspace_claims WHERE family_id=? AND state!='inactive'", (family_id,)).fetchone()[0]

    def claim(self, task_id, kind, state, data=None, *, expected_revision=None, if_changed=False):
        if kind not in ("turn", "queue", "goal", "uncertain", "background") or state not in ("active", "uncertain", "inactive"):
            raise BridgeError("Unknown workspace claim kind or state", code='state_invalid')
        task = self.task_head(task_id)
        if not task.get("project") or (task.get("deleted") and state != "inactive"):
            return False
        with self.transaction():
            row = self.db.execute("SELECT * FROM workspace_claims WHERE task_id=? AND kind=?", (task_id, kind)).fetchone()
            revision = row["revision"] if row else 0
            if expected_revision is not None and revision != expected_revision:
                return False
            safe = redact(data or {})
            previous = json.loads(row["data"]) if row else {}
            if (if_changed and row and row["state"] == state and json.loads(row["data"]) == safe
                    and row["family_id"] == task.get("root_task_id", task_id) and row["project"] == task["project"]):
                return True
            comparable = lambda value: {k: v for k, v in value.items() if k != "error"}
            bookkeeping = (row is None and state == "inactive") or (row is not None and row["state"] == state
                and row["family_id"] == task.get("root_task_id", task_id) and row["project"] == task["project"]
                and comparable(previous) == comparable(safe))
            self.db.execute("""INSERT INTO workspace_claims VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(task_id,kind) DO UPDATE SET state=excluded.state,data=excluded.data,
                revision=excluded.revision""", (task_id, kind, task.get("root_task_id", task_id),
                task["project"], state, json.dumps(safe), revision + 1))
            self._remember_bookkeeping("claims", (task_id, kind), revision + 1, bookkeeping)
            touch(self, task_id)
        return True

    def event(self, task_id, kind, data, *, content_redacted=False):
        with self.transaction():
            return self._event(task_id, kind, data, content_redacted=content_redacted)

    def _event(self, task_id, kind, data, *, content_redacted=False):
        safe = redact(data)
        content_redacted = content_redacted or safe != data
        data = safe
        cursor = self.db.execute("INSERT INTO events(task_id,kind,at,data) VALUES (?,?,?,?)",
                                 (task_id, kind, time.time(), json.dumps(data)))
        observe(self.db, task_id, kind, data, cursor.lastrowid, redaction_count=int(content_redacted))
        if kind not in STREAM_ONLY:
            touch(self, task_id)
        return cursor.lastrowid

    def highwater(self):
        return self.db.execute("SELECT MAX(n) FROM (SELECT COALESCE(MAX(seq),0) n FROM events UNION ALL SELECT COALESCE(MAX(last_seq),0) FROM expired_event_ranges)").fetchone()[0]

    def events(self, after, limit, task_id=None, task_ids=None, *, event_offset=0, event_seq=0, compact=False, family_root=None):
        missing_query = "SELECT first_seq,last_seq FROM expired_event_ranges WHERE last_seq>?"
        missing_args = [after]
        if family_root is not None:
            missing_query = self.family_query() + missing_query + " AND task_id IN (SELECT id FROM family)"
            missing_args.insert(0, family_root)
        elif task_ids:
            missing_query += " AND task_id IN (" + ",".join("?" for _ in task_ids) + ")"
            missing_args.extend(task_ids)
        elif task_id:
            missing_query += " AND task_id=?"
            missing_args.append(task_id)
        missing = self.db.execute(missing_query + " ORDER BY last_seq LIMIT 1", missing_args).fetchone()
        def expired():
            raise BridgeError("Event cursor has expired; resume explicitly after error.resume_after to acknowledge the missing journal data", code="cursor_expired",
                              details={"resume_after": missing["last_seq"]},
                              ids={"task_id": task_id} if task_id else None)
        if missing and missing["first_seq"] <= after + 1:
            expired()
        if after > self.highwater():
            raise BridgeError("Cursor is ahead of this journal; check the state directory", code='state_invalid')
        columns = "seq,task_id,kind,at,LENGTH(CAST(data AS BLOB)) AS content_bytes" if compact else "*"
        query = f"SELECT {columns} FROM events WHERE seq>?"
        args = [after]
        if family_root is not None:
            query = self.family_query() + query + " AND task_id IN (SELECT id FROM family)"
            args.insert(0, family_root)
        elif task_ids:
            query += " AND task_id IN (" + ",".join("?" for _ in task_ids) + ")"
            args.extend(task_ids)
        elif task_id:
            query += " AND task_id=?"
            args.append(task_id)
        rows = self.db.execute(query + " ORDER BY seq LIMIT ?", (*args, limit + 1))
        events, size, more = [], 0, False
        partial, next_offset = False, 0
        for row in rows:
            if missing and row["seq"] >= missing["first_seq"]:
                if not events:
                    expired()
                more = True
                break
            if compact:
                if len(events) == limit:
                    more = True
                    break
                events.append({**dict(row), "data_omitted": True})
                continue
            if len(events) == limit or (events and size + len(row["data"]) > 1024 * 1024):
                more = True
                break
            entry = {**dict(row), "data": json.loads(row["data"])}
            if event_offset or result_size({"events": [entry]}) > 6 * 1024 * 1024:
                if events:
                    more = True
                    break
                if event_offset and event_seq != row["seq"]:
                    raise BridgeError("Event fragment identity changed; restart this event at offset 0", code='state_invalid')
                content = json_text(entry)
                if event_offset >= len(content):
                    raise BridgeError("Event fragment offset is beyond this event; restart at offset 0", code='state_invalid')
                end = min(len(content), event_offset + DETAIL_CHARS)
                partial = end < len(content)
                events.append({"seq": row["seq"], "fragment": content[event_offset:end],
                    "fragment_offset": event_offset, "fragment_total_chars": len(content),
                    "fragment_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "fragment_complete": not partial})
                # The current SQL page can contain more events after this one.
                more = partial or any(True for _ in rows)
                next_offset = end if partial else 0
                break
            if events and result_size({"events": [*events, entry]}) > 6 * 1024 * 1024:
                more = True
                break
            size += len(row["data"])
            events.append(entry)
        if event_offset and not events:
            raise BridgeError("Event fragment is no longer in this page; restart at offset 0", code='state_invalid')
        if missing and not more:
            if not events:
                expired()
            more = True
        # With a filter, consuming through highwater skips only irrelevant events.
        cursor = after if partial else (events[-1]["seq"] if more else self.highwater())
        return {"events": events, "cursor": cursor, "has_more": more,
                "next_event_offset": next_offset, "event_seq": events[-1]["seq"] if partial else None}

    def close(self):
        self.db.close()
