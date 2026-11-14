import errno
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import store as storage
from codex_local_mcp.security import BridgeError, private_file
from tests.helpers import temp_root


class PlanStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        self.path = self.state / "state.sqlite3"

    def previous(self):
        with patch.object(storage, "SCHEMA_VERSION", 10):
            return storage.Store(self.state)

    def test_schema11_never_reads_history_and_adds_only_fixed_layout_space(self):
        previous = self.previous()
        with previous.transaction():
            previous.db.executemany("INSERT INTO events(task_id,kind,at,data) VALUES (NULL,'fixture',1,?)", [(json.dumps({"output": "x" * 1024 * 1024}),)] * 32)
            previous.db.execute("INSERT INTO operations VALUES ('key','fingerprint','done','{}',NULL)")
        seq = previous.highwater()
        previous.close()
        before, start = self.path.stat().st_size, time.perf_counter()
        migration = storage.MIGRATIONS[11][1]
        def guarded(db):
            def authorized(action, table, *_):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in ("events", "tasks", "operations") else sqlite3.SQLITE_OK
            db.set_authorizer(authorized)
            try:
                migration(db)
            finally:
                db.set_authorizer(None)
        with patch.dict(storage.MIGRATIONS, {11: ("dependency_plans", guarded)}), patch.object(storage.Store, "_decode", side_effect=AssertionError("Migration decoded history")):
            current = storage.Store(self.state)
        self.addCleanup(current.close)
        elapsed = time.perf_counter() - start
        current.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = self.path.stat().st_size
        self.assertEqual(current.schema_version, 11)
        self.assertLessEqual(after - before, 64 * 1024)
        self.assertEqual(current.highwater(), seq)
        self.assertEqual(current.db.execute("SELECT fingerprint FROM operations").fetchone()[0], "fingerprint")
        for table in ("plans", "plan_nodes"):
            self.assertEqual(current.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        with closing(sqlite3.connect(current.backup_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM events").fetchone()[0], 32)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        print(f"schema10->11: {before} -> {after} bytes; {elapsed:.4f}s; 32 MiB historical journal unchanged")

    def test_direct_v011_upgrade_preserves_keys_and_sequences_and_does_not_repeat(self):
        self.addCleanup(os.umask, os.umask(0o022))
        os.close(private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        with closing(sqlite3.connect(self.path)) as db:
            for statement in storage.SCHEMA_V1:
                db.execute(statement)
            db.execute("INSERT INTO operations VALUES ('old-key','old-fingerprint','running',NULL,NULL)")
            db.execute("INSERT INTO events(seq,kind,at,data) VALUES (55,'fixture',1,'{}')")
            db.commit()
        with closing(storage.Store(self.state)) as current:
            self.assertEqual(current.schema_version, 11)
            self.assertEqual(current.highwater(), 55)
            self.assertEqual(current.db.execute("SELECT fingerprint FROM operations").fetchone()[0], "old-fingerprint")
        backups = list(self.state.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        storage.Store(self.state).close()
        self.assertEqual(list(self.state.glob("*.bak")), backups)
        with patch.object(storage, "SCHEMA_VERSION", 10), self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_newer_schema")

    def test_failed_schema11_migration_rolls_back_and_incomplete_layout_is_refused(self):
        self.previous().close()
        migration = storage.MIGRATIONS[11][1]
        def interrupted(db):
            migration(db)
            raise OSError(errno.ENOSPC, "Injected disk full")
        with patch.dict(storage.MIGRATIONS, {11: ("dependency_plans", interrupted)}), self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_migration_failed")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='plans'").fetchone())
        current = storage.Store(self.state)
        current.db.execute("ALTER TABLE plan_nodes DROP COLUMN attempt")
        current.close()
        with self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_invalid")
