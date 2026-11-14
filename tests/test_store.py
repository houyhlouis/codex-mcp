import errno
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import store as storage
from codex_local_mcp.security import BridgeError
from tests.helpers import temp_root


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "state"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "state.sqlite3"

    def legacy(self, wal=False):
        os.close(storage.private_file(self.path, os.O_CREAT | os.O_RDWR))
        db = sqlite3.connect(self.path)
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
        db.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                kind TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX events_task ON events(task_id,seq);
            CREATE TABLE requests (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, task_id TEXT);
        """)
        task = {"id": "old-task", "thread_id": "native-thread", "turn_id": "native-turn", "status": "completed",
                "project": str(Path(self.temp.name) / "project"), "root": "legacy", "path": "project",
                "summary": "Saved summary", "commands": [], "files": [], "future_field": {"keep": [1, 2]}}
        db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(task)))
        db.execute("INSERT INTO requests VALUES ('request',?)", (json.dumps({"id": "request", "status": "pending", "future": 1}),))
        db.execute("INSERT INTO metadata VALUES ('workspace_scope','{}')")
        db.execute("INSERT INTO metadata VALUES ('future_metadata','preserve')")
        db.execute("INSERT INTO operations VALUES ('operation-key','fp','done','{\"saved\":true}','old-task')")
        db.execute("INSERT INTO events VALUES (7,'old-task','saved',1,'{}')")
        db.execute("INSERT INTO events VALUES (21,'old-task','deleted-tail',2,'{}')")
        db.execute("DELETE FROM events WHERE seq=21")
        db.commit()
        return db

    def open_store(self):
        result = storage.Store(self.directory)
        self.addCleanup(result.close)
        return result

    def test_empty_directory_uses_latest_version_and_ledger_without_backup(self):
        store = self.open_store()
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], storage.SCHEMA_VERSION)
        self.assertEqual([r[0] for r in store.db.execute("SELECT version FROM schema_migrations ORDER BY version")], list(range(1, storage.SCHEMA_VERSION + 1)))
        self.assertIsNone(store.backup_path)
        self.assertEqual(list(self.directory.glob("*.bak")), [])

    def test_inspection_refuses_reconciliation_reports_without_created_at(self):
        store = self.open_store()
        store.db.execute("ALTER TABLE reconciliation_reports RENAME TO damaged_reports")
        store.db.execute("CREATE TABLE reconciliation_reports (task_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,task_revision INTEGER NOT NULL,data TEXT NOT NULL)")
        store.db.execute("DROP TABLE damaged_reports")
        store.close()
        with self.assertRaises(BridgeError) as refused:
            self.open_store()
        self.assertEqual(refused.exception.code, "state_invalid")

    def test_real_legacy_schema_preserves_records_operations_and_event_sequence(self):
        self.legacy().close()
        store = self.open_store()
        task = store.get("tasks", "old-task")
        self.assertEqual(task["summary"], "Saved summary")
        self.assertEqual(task["future_field"], {"keep": [1, 2]})
        self.assertNotIn("root", task)
        self.assertNotIn("path", task)
        self.assertEqual(store.get("projects", task["project_id"])["path"], task["project"])
        self.assertEqual(task["projection_turn_id"], "native-turn")
        self.assertFalse(task["evidence_complete"])
        self.assertEqual(store.get("requests", "request")["future"], 1)
        row = store.db.execute("SELECT * FROM operations").fetchone()
        self.assertEqual(tuple(row), ("operation-key", "fp", "done", '{"saved":true}', "old-task"))
        self.assertEqual(store.highwater(), 7)
        with store.transaction():
            self.assertEqual(store.event("old-task", "next", {}), 22)
        self.assertEqual(store.db.execute("SELECT value FROM metadata WHERE key='future_metadata'").fetchone()[0], "preserve")

    def test_backup_is_private_and_contains_committed_wal_rows(self):
        writer = self.legacy(wal=True)
        self.addCleanup(writer.close)
        store = self.open_store()
        with closing(sqlite3.connect(store.backup_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT id FROM operations").fetchone()[0], "operation-key")
            self.assertEqual(backup.execute("SELECT seq FROM events").fetchone()[0], 7)
            self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())
        if os.name != "nt":
            self.assertEqual(store.backup_path.stat().st_mode & 0o777, 0o600)

    def test_mid_migration_exception_rolls_back_all_schema_and_data_changes(self):
        self.legacy().close()
        def interrupted(db):
            db.execute("CREATE TABLE interrupted_write (value TEXT)")
            db.execute("DELETE FROM operations")
            raise RuntimeError("Injected crash point")
        with patch.dict(storage.MIGRATIONS, {3: ("interrupted", interrupted)}):
            with self.assertRaisesRegex(BridgeError, "previous state was preserved"):
                storage.Store(self.directory)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='interrupted_write'").fetchone())
            self.assertEqual(db.execute("SELECT id FROM operations").fetchone()[0], "operation-key")
            self.assertIn("root", json.loads(db.execute("SELECT data FROM tasks").fetchone()[0]))
        self.assertEqual(self.open_store().schema_version, storage.SCHEMA_VERSION)

    def test_disk_full_rolls_back_and_backup_count_is_bounded(self):
        self.legacy().close()
        def full(db):
            db.execute("ALTER TABLE tasks ADD COLUMN partial TEXT")
            raise sqlite3.OperationalError("database or disk is full")
        with patch.dict(storage.MIGRATIONS, {3: ("full", full)}):
            for _ in range(storage.BACKUP_LIMIT + 2):
                with self.assertRaisesRegex(BridgeError, "previous state was preserved"):
                    storage.Store(self.directory)
        self.assertEqual(len(list(self.directory.glob("*.bak"))), storage.BACKUP_LIMIT)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertNotIn("partial", [r[1] for r in db.execute("PRAGMA table_info(tasks)")])
            self.assertEqual(db.execute("SELECT id FROM operations").fetchone()[0], "operation-key")

    def test_backup_failure_prevents_migration(self):
        self.legacy().close()
        original = storage.private_file
        def no_space(path, flags):
            if str(path).endswith(".bak"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return original(path, flags)
        with patch.object(storage, "private_file", no_space), self.assertRaisesRegex(BridgeError, "no migration was applied"):
            storage.Store(self.directory)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_backup_name_collision_does_not_remove_an_existing_backup(self):
        self.legacy().close()
        previous = self.directory / "state.sqlite3.pre-v1-123.bak"
        os.close(storage.private_file(previous, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        previous.write_bytes(b"existing private backup")
        with patch.object(storage.time, "time_ns", return_value=123):
            with self.assertRaisesRegex(BridgeError, "no migration was applied"):
                storage.Store(self.directory)
        self.assertEqual(previous.read_bytes(), b"existing private backup")

    def test_retention_keeps_new_backup_even_if_wall_clock_moves_backwards(self):
        original = self.legacy()
        try:
            for number in range(storage.BACKUP_LIMIT):
                path = self.directory / f"state.sqlite3.pre-v9-{900 + number}.bak"
                os.close(storage.private_file(path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
                with closing(sqlite3.connect(path)) as destination:
                    original.backup(destination)
        finally:
            original.close()
        with patch.object(storage.time, "time_ns", return_value=1):
            store = self.open_store()
        self.assertTrue(store.backup_path.exists())
        self.assertEqual(len(list(self.directory.glob("*.bak"))), storage.BACKUP_LIMIT)

    def test_inconsistent_known_ledger_is_refused(self):
        store = self.open_store()
        with store.transaction():
            store.db.execute("DELETE FROM schema_migrations WHERE version=2")
        store.close()
        with self.assertRaisesRegex(BridgeError, "ledger is missing or inconsistent"):
            storage.Store(self.directory)

    def test_newer_database_is_refused_without_changes(self):
        db = self.legacy()
        db.execute("PRAGMA user_version=99")
        db.commit()
        db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(BridgeError, "version 99 is newer.*separate state directory"):
            storage.Store(self.directory)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob("*.bak")), [])

    def test_foreign_database_is_refused(self):
        os.close(storage.private_file(self.path, os.O_CREAT | os.O_RDWR))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE foreign_data (value TEXT)")
        with self.assertRaisesRegex(BridgeError, "Unrecognized state database"):
            storage.Store(self.directory)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA application_id=123")
        with self.assertRaisesRegex(BridgeError, "another application"):
            storage.Store(self.directory)

    def test_revisions_reject_stale_snapshots_and_preserve_unknown_fields(self):
        store = self.open_store()
        with store.transaction():
            first = store.save("tasks", {"id": "task", "status": "idle", "unknown": {"keep": True}})
            current = store.update("tasks", "task", {"name": "new"})
        self.assertEqual(current["revision"], first["revision"] + 1)
        with self.assertRaisesRegex(BridgeError, "Stale task revision"):
            store.save("tasks", {**first, "name": "stale"})
        with store.transaction():
            self.assertIsNone(store.update("tasks", "task", {"name": "stale"}, expected={"revision": first["revision"]}))
        self.assertEqual(store.get("tasks", "task")["unknown"], {"keep": True})
        self.assertEqual(store.get("tasks", "task")["name"], "new")

    def test_invalid_identity_is_rejected_at_read_and_write_boundaries(self):
        store = self.open_store()
        with self.assertRaises(BridgeError):
            store.save("tasks", {"id": None})
        with store.transaction():
            store.save("tasks", {"id": "task"})
            store.db.execute("UPDATE tasks SET data='[]' WHERE id='task'")
        with self.assertRaisesRegex(BridgeError, "Invalid task record identity"):
            store.get("tasks", "task")
