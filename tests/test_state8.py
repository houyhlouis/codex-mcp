"""Supervision metadata is atomic and costs no historical journal reads."""

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
from tests.helpers import temp_root, SCHEMA10_LAYOUT_BYTES, SCHEMA11_LAYOUT_BYTES


class State8Test(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.path = self.state / "state.sqlite3"

    def previous(self):
        with patch.object(storage, "SCHEMA_VERSION", 7), closing(storage.Store(self.state)) as store:
            with store.transaction():
                store.save("tasks", {"id": "old", "thread_id": "native-old", "turn_id": "turn-old",
                    "status": "completed", "project": str(Path(self.temp.name) / "project"), "future": {"keep": True}})
                store.db.execute("INSERT INTO operations VALUES ('op','fingerprint','uncertain',NULL,'old')")
                store.db.execute("INSERT INTO metadata VALUES ('mcp_execution:opaque','{\"keep\":true}')")
                store.db.executemany("INSERT INTO events(task_id,kind,at,data) VALUES ('old','large',1,?)",
                    ((json.dumps({"text": "x" * (256 * 1024)}),) for _ in range(32)))

    def test_schema7_upgrade_never_reads_journal_and_preserves_every_record(self):
        self.previous()
        before = self.path.stat().st_size
        original = storage.MIGRATIONS[8][1]
        def migration(db):
            def authorize(action, table, column, database, trigger):
                return sqlite3.SQLITE_DENY if table == "events" else sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            try:
                original(db)
            finally:
                db.set_authorizer(None)
        with patch.dict(storage.MIGRATIONS, {8: ("reconciliation_outcomes_and_receipts", migration)}), closing(storage.Store(self.state)) as store:
            self.assertEqual(store.schema_version, storage.SCHEMA_VERSION)
            self.assertEqual(store.get("tasks", "old")["future"], {"keep": True})
            self.assertEqual(store.highwater(), 32)
            self.assertEqual(store.db.execute("SELECT fingerprint FROM operations").fetchone()[0], "fingerprint")
            self.assertEqual(store.db.execute("SELECT value FROM metadata").fetchone()[0], '{"keep":true}')
            for table in ("task_changes", "reconciliation_reports", "recovered_items", "operation_details", "decision_receipts", "artifacts", "evidence_entries"):
                self.assertEqual(store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertLessEqual(store.db.execute("PRAGMA page_count").fetchone()[0] * store.db.execute("PRAGMA page_size").fetchone()[0] - before, 128 * 1024 + SCHEMA10_LAYOUT_BYTES + SCHEMA11_LAYOUT_BYTES)
            with closing(sqlite3.connect(store.backup_path)) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 7)
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            identity = store.db.execute("SELECT identity FROM supervision_state").fetchone()[0]
        with closing(storage.Store(self.state)) as reopened:
            self.assertIsNone(reopened.backup_path)
            self.assertEqual(reopened.db.execute("SELECT identity FROM supervision_state").fetchone()[0], identity)

    def test_failed_migration_rolls_back_all_supervision_tables(self):
        self.previous()
        original = storage.MIGRATIONS[8][1]
        def interrupted(db):
            original(db)
            db.execute("DELETE FROM operations")
            raise OSError("simulated full disk")
        with patch.dict(storage.MIGRATIONS, {8: ("reconciliation_outcomes_and_receipts", interrupted)}), self.assertRaises(BridgeError):
            storage.Store(self.state)
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(old.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
            self.assertFalse(old.execute("SELECT 1 FROM sqlite_master WHERE name='supervision_state'").fetchone())
        with closing(storage.Store(self.state)) as current:
            self.assertEqual(current.schema_version, storage.SCHEMA_VERSION)
