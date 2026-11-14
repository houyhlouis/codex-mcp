"""One atomic migration adds retention ownership without losing older state."""

import json
import base64
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import store as storage
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.retention import Retention
from tests.helpers import temp_root, SCHEMA10_LAYOUT_BYTES, SCHEMA11_LAYOUT_BYTES


class State7Test(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.path = self.state / "state.sqlite3"

    def fixture(self, version):
        os.close(private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            for statement in storage.SCHEMA_V1:
                db.execute(statement)
            task = {"id": "legacy", "project": str(self.base / "project"), "thread_id": "thread", "turn_id": "turn", "status": "completed", "summary": "claim", "unknown": {"keep": True}}
            db.execute("INSERT INTO tasks VALUES ('legacy',?)", (json.dumps(task),))
            db.execute("INSERT INTO operations VALUES ('done','done-fp','done','{}','legacy')")
            db.execute("INSERT INTO operations VALUES ('running','run-fp','running',NULL,'legacy')")
            db.execute("INSERT INTO requests VALUES ('pending',?)", (json.dumps({"id": "pending", "task_id": "legacy", "status": "pending", "params": {}, "created_at": 1}),))
            db.execute("INSERT INTO metadata VALUES ('mcp_execution:opaque',?)", (json.dumps({"future": {"version": 9}}),))
            params = {"turnId": "turn", "item": {"id": "cmd", "type": "commandExecution", "command": "offline", "exitCode": 0}}
            db.execute("INSERT INTO events VALUES (17,'legacy','item/completed',1,?)", (json.dumps(params),))
            db.execute("INSERT INTO events VALUES (18,'legacy','future/event',2,?)", (json.dumps({"turn": ["unknown"]}),))
            if version > 1:
                db.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES (1,'v0.1.1',0)")
                for number in range(2, version + 1):
                    name, migrate = storage.MIGRATIONS[number]
                    migrate(db)
                    db.execute("INSERT INTO schema_migrations VALUES (?,?,0)", (number, name))
                db.execute(f"PRAGMA application_id={storage.APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={version}")
            db.commit()

    def test_schema6_upgrade_preserves_history_identity_pending_keys_and_metadata(self):
        self.check_upgrade(6)

    def test_v011_upgrade_preserves_history_identity_pending_keys_and_metadata(self):
        self.check_upgrade(1)

    def check_upgrade(self, version):
        self.fixture(version)
        with closing(storage.Store(self.state)) as store:
            self.assertEqual(store.schema_version, storage.SCHEMA_VERSION)
            self.assertEqual(store.db.execute("SELECT name FROM schema_migrations WHERE version=7").fetchone()[0], "retention_and_turn_evidence")
            self.assertEqual(store.get("tasks", "legacy")["unknown"], {"keep": True})
            self.assertEqual(store.get("requests", "pending")["status"], "pending")
            self.assertEqual({tuple(r) for r in store.db.execute("SELECT id,fingerprint,state FROM operations")}, {("done", "done-fp", "done"), ("running", "run-fp", "running")})
            self.assertEqual(store.db.execute("SELECT value FROM metadata WHERE key='mcp_execution:opaque'").fetchone()[0], '{"future": {"version": 9}}')
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM evidence_entries").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
            result = store.evidence.result("legacy", "turn")
            self.assertIn("historical_evidence", result["evidence_completeness"]["gaps"])
            self.assertFalse(result["evidence_completeness"]["complete"])
            evidence = store.db.execute("SELECT * FROM evidence_entries").fetchone()
            self.assertEqual(evidence["source_seq"], 17)
            self.assertEqual(json.loads(evidence["data"])["reported"]["command"], "offline")
            self.assertEqual(store.highwater(), 18)
            with store.transaction():
                self.assertEqual(store.event("legacy", "future", {}), 19)
            os.close(private_file(store.backup_path, os.O_RDONLY))
            with closing(sqlite3.connect(store.backup_path)) as copy:
                self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 0 if version == 1 else version)
        before = list(self.state.glob("*.bak"))
        with closing(storage.Store(self.state)) as store:
            self.assertIsNone(store.backup_path)
        self.assertEqual(list(self.state.glob("*.bak")), before)

    def test_interrupted_schema7_rolls_back_and_retry_works(self):
        self.fixture(6)
        def interrupted(db):
            db.execute("CREATE TABLE artifacts (id TEXT)")
            db.execute("DELETE FROM operations")
            raise OSError("simulated disk full")
        with patch.dict(storage.MIGRATIONS, {7: ("retention_and_turn_evidence", interrupted)}):
            with self.assertRaises(BridgeError) as caught:
                storage.Store(self.state)
        self.assertEqual(caught.exception.code, "state_migration_failed")
        with closing(sqlite3.connect(self.path)) as previous:
            self.assertEqual(previous.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(previous.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 2)
            self.assertIsNone(previous.execute("SELECT name FROM sqlite_master WHERE name='artifacts'").fetchone())
        with closing(storage.Store(self.state)) as current:
            self.assertEqual(current.schema_version, storage.SCHEMA_VERSION)

    def large_history(self, count=64, size=256 * 1024):
        self.fixture(6)
        with closing(sqlite3.connect(self.path)) as db:
            for number in range(count):
                params = {"threadId": "thread", "turnId": "turn", "item": {"id": f"large-{number}", "type": "commandExecution",
                    "command": "offline", "status": "completed", "exitCode": 0, "aggregatedOutput": "history:" + "x" * size}}
                db.execute("INSERT INTO events(task_id,kind,at,data) VALUES ('legacy','item/completed',1,?)", (json.dumps(params),))
            other = {"turnId": "unrequested", "item": {"id": "other", "type": "agentMessage", "text": "y" * size}}
            db.execute("INSERT INTO events(task_id,kind,at,data) VALUES ('legacy','item/completed',1,?)", (json.dumps(other),))
            db.commit()

    def test_large_schema6_upgrade_never_reads_or_duplicates_journal_payloads(self):
        self.large_history()
        before = self.path.stat().st_size
        original = storage.MIGRATIONS[7][1]
        def no_journal_read(db):
            def authorize(action, table, column, database, trigger):
                history_access = action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
                return sqlite3.SQLITE_DENY if history_access and table == "events" else sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            try:
                original(db)
            finally:
                db.set_authorizer(None)
        with patch.dict(storage.MIGRATIONS, {7: ("retention_and_turn_evidence", no_journal_read)}):
            with closing(storage.Store(self.state)) as store:
                for table in ("artifacts", "evidence_entries", "turn_results", "evidence_history", "operation_times"):
                    self.assertEqual(store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 67)
        self.assertLessEqual(self.path.stat().st_size - before, 256 * 1024 + SCHEMA10_LAYOUT_BYTES + SCHEMA11_LAYOUT_BYTES)
        with closing(storage.Store(self.state)) as store:
            with patch("codex_local_mcp.evidence.read_project_file", side_effect=AssertionError("Historical files must not be captured")):
                result = store.evidence.result("legacy", "turn")
            self.assertEqual(result["counts"]["observed_commands"], 65)
            self.assertIn("historical_evidence", result["evidence_completeness"]["gaps"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM artifacts WHERE content IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM evidence_entries WHERE turn_id='unrequested'").fetchone()[0], 0)
            with patch.object(store.evidence, "hydrate_history", side_effect=AssertionError("History should be loaded once")):
                self.assertEqual(store.evidence.result("legacy", "turn")["result_id"], result["result_id"])

    def test_old_turn_without_a_lifecycle_row_is_loaded_without_changing_turn_order(self):
        self.fixture(6)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO events(task_id,kind,at,data) VALUES ('legacy','item/completed',1,?)",
                (json.dumps({"turnId": "older", "item": {"id": "claim", "type": "agentMessage", "text": "Old report"}}),))
            db.execute("INSERT INTO events(task_id,kind,at,data) VALUES ('legacy','turn/completed',1,?)",
                (json.dumps({"turn": {"id": "older", "status": "completed"}}),))
            db.commit()
        with closing(storage.Store(self.state)) as store:
            before = [tuple(row) for row in store.db.execute("SELECT * FROM turns")]
            result = store.evidence.result("legacy", "older")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["agent_claims"][0]["reported"]["text"], "Old report")
            self.assertIn("historical_evidence", result["evidence_completeness"]["gaps"])
            self.assertEqual([tuple(row) for row in store.db.execute("SELECT * FROM turns")], before)

    def test_lazy_historical_content_keeps_redaction_and_uses_only_source_references(self):
        self.fixture(6)
        params = {"turnId": "turn", "item": {"id": "private", "type": "agentMessage", "text": "password=old-secret\n" + "x" * 20000}}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO events(task_id,kind,at,data) VALUES ('legacy','item/completed',1,?)", (json.dumps(params),))
            db.commit()
        with closing(storage.Store(self.state)) as store:
            value = store.evidence.result("legacy", "turn")
            ref = value["agent_claims"][0]["reported"]
            self.assertTrue(ref["content_redacted"])
            response = store.evidence.read(store.task_head("legacy"), ref["artifact_id"])
            content = base64.b64decode(response["data"])
            self.assertNotIn(b"old-secret", content)
            self.assertIn(b"[REDACTED]", content)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM artifacts WHERE content IS NOT NULL").fetchone()[0], 0)

    def test_legacy_operation_retention_age_uses_the_migration_without_a_backfill(self):
        self.fixture(6)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO operations VALUES ('legacy-unscoped','fingerprint','done','{}',NULL)")
            db.commit()
        with closing(storage.Store(self.state)) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM operation_times").fetchone()[0], 0)
            migrated_at = store.db.execute("SELECT applied_at FROM schema_migrations WHERE version=7").fetchone()[0]
            young = Retention(store, clock=lambda: migrated_at + 100).plan(200)
            self.assertEqual(young["counts"]["operations"], 0)
            old = Retention(store, clock=lambda: migrated_at + 300).plan(200)
            self.assertEqual(old["counts"]["operations"], 1)
