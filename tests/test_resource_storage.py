"""Schema 9 adds bounded accounting metadata without rewriting history."""

import errno
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import store as storage
from codex_local_mcp.config import load_config
from codex_local_mcp.security import BridgeError, private_dir, private_file
from tests.helpers import temp_root, write_config


class ResourceStorageTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        temporary = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.state = self.base / "state"
        private_dir(self.state)
        self.path = self.state / "state.sqlite3"

    def task(self):
        return {"id": "old", "thread_id": "native-old", "project": str(self.base / "project"), "status": "completed",
            "turn_id": "old-turn", "created_at": 1, "updated_at": 2, "summary": "retained", "future_field": {"kept": True}}

    def version8(self):
        with patch.object(storage, "SCHEMA_VERSION", 8):
            db = storage.Store(self.state)
        db.save("tasks", self.task())
        return db

    def test_upgrade_schema8_with_large_events_does_not_copy_or_decode_them(self):
        previous = self.version8()
        body = json.dumps({"threadId": "native-old", "turnId": "old-turn", "item": {
            "id": "large", "type": "commandExecution", "aggregatedOutput": "x" * (1024 * 1024)}})
        with previous.transaction():
            previous.db.executemany("INSERT INTO events(task_id,kind,at,data) VALUES ('old','item/completed',1,?)", [(body,)] * 32)
            previous.db.execute("INSERT INTO operations VALUES ('old-key','unchanged','running',NULL,'old')")
        last = previous.highwater()
        previous.close()
        before = self.path.stat().st_size
        started = time.perf_counter()
        with patch.object(storage.Store, "_decode", side_effect=AssertionError("Migration decoded a payload")):
            current = storage.Store(self.state)
        self.addCleanup(current.close)
        elapsed = time.perf_counter() - started
        current.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = self.path.stat().st_size
        self.assertEqual(current.schema_version, storage.SCHEMA_VERSION)
        for table in ("resource_families", "resource_threads", "resource_turns", "resource_reservations", "resource_storage", "resource_interrupts", "resource_totals"):
            self.assertEqual(current.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.assertLessEqual(after - before, 256 * 1024)
        self.assertEqual(current.highwater(), last)
        self.assertEqual(current.db.execute("SELECT fingerprint FROM operations WHERE id='old-key'").fetchone()[0], "unchanged")
        self.assertTrue(current.get("tasks", "old")["future_field"]["kept"])
        with closing(sqlite3.connect(current.backup_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM events").fetchone()[0], 32)
        print(f"schema8->{storage.SCHEMA_VERSION}: {before} -> {after} bytes; {elapsed:.4f}s; 32 MiB historical payloads retained once")

    def test_legacy_upgrade_preserves_events_operations_and_marks_old_usage_unknown(self):
        os.close(private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        with closing(sqlite3.connect(self.path)) as db:
            for statement in storage.SCHEMA_V1:
                db.execute(statement)
            db.execute("INSERT INTO tasks VALUES (?,?)", ("old", json.dumps(self.task())))
            db.execute("INSERT INTO operations VALUES ('old-key','fingerprint','running',NULL,'old')")
            db.execute("INSERT INTO events(seq,task_id,kind,at,data) VALUES (17,'old','bridge/input',1,'{}')")
            db.commit()
        current = storage.Store(self.state)
        self.addCleanup(current.close)
        usage = current.resources.usage("old")
        self.assertIn("not_counted_before_upgrade", usage["gaps"])
        self.assertFalse(usage["complete"])
        self.assertEqual(current.highwater(), 17)
        self.assertEqual(current.db.execute("SELECT fingerprint FROM operations WHERE id='old-key'").fetchone()[0], "fingerprint")

    def test_failed_migration_rolls_back_and_second_open_does_not_back_up_again(self):
        self.version8().close()
        migration = storage.MIGRATIONS[9][1]
        def interrupted(db):
            migration(db)
            raise OSError(errno.ENOSPC, "Injected full disk")
        with patch.dict(storage.MIGRATIONS, {9: ("interrupted", interrupted)}), self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_migration_failed")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_turns'").fetchone())
        current = storage.Store(self.state)
        current.close()
        backups = sorted(self.state.glob("*.bak"))
        reopened = storage.Store(self.state)
        reopened.close()
        self.assertEqual(sorted(self.state.glob("*.bak")), backups)
        with patch.object(storage, "SCHEMA_VERSION", 8), self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_newer_schema")


class BudgetConfigTest(unittest.TestCase):
    def test_defaults_are_opt_in_validated_and_part_of_the_broker_identity(self):
        with tempfile.TemporaryDirectory(dir=temp_root()) as name:
            base = Path(name).resolve()
            config = base / "settings.toml"
            write_config(config, base / "state", Path(sys.executable).resolve())
            original = load_config(str(config))
            self.assertEqual(original.budget, {})
            with config.open("a") as file:
                file.write("\n[budget]\nmax_turns = 2\nmax_tokens = 1000\n")
            limited = load_config(str(config))
            self.assertEqual(limited.budget, {"max_turns": 2, "max_tokens": 1000})
            self.assertNotEqual(original.fingerprint, limited.fingerprint)
            for field in ("max_turns = 0", "max_tokens = true", "unknown = 1", "max_children = 10001"):
                write_config(config, base / "state", Path(sys.executable).resolve())
                with config.open("a") as file:
                    file.write("\n[budget]\n" + field + "\n")
                with self.assertRaises(BridgeError):
                    load_config(str(config))
