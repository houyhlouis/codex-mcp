"""Schema 10 projects resource rows atomically; migrations never read the journal."""

import errno
import json
import os
import random
import sqlite3
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from codex_local_mcp import store as storage
from codex_local_mcp.retention import Retention
from codex_local_mcp.security import BridgeError, private_file
from tests import test_resource_storage


class BudgetProjectionStorageTest(unittest.TestCase):
    setUp = test_resource_storage.ResourceStorageTest.setUp
    task = test_resource_storage.ResourceStorageTest.task

    def version9(self):
        with patch.object(storage, "SCHEMA_VERSION", 9):
            previous = storage.Store(self.state)
        previous.save("tasks", self.task())
        return previous

    def check_totals(self, store):
        names = ("server_turns", "native_turns", "closed_seconds", "closed_unknown", "observed_turns")
        raw = {row["task_id"]: list(row)[1:] for row in store.db.execute("""SELECT task_id,SUM(server_started),SUM(1-server_started),
            SUM(CASE WHEN ended_at IS NOT NULL THEN COALESCE(seconds,0) ELSE 0 END),
            SUM(CASE WHEN ended_at IS NOT NULL AND seconds IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN started_at IS NOT NULL THEN 1 ELSE 0 END) FROM resource_turns GROUP BY task_id""")}
        cached = {row["task_id"]: [row[name] for name in names] for row in store.db.execute("SELECT * FROM resource_totals")}
        for identity in set(raw) | set(cached):
            for column, expected, actual in zip(names, raw.get(identity, [0] * 5), cached.get(identity, [0] * 5)):
                with self.subTest(task=identity, column=column):
                    self.assertAlmostEqual(actual, expected, places=6)

    def test_schema9_upgrade_backfills_only_resource_rows_and_keeps_consistent_backup(self):
        previous = self.version9()
        with previous.transaction():
            previous.db.execute("INSERT INTO resource_threads VALUES ('old','native-old',?,1,'[]',2,NULL)", (json.dumps({"input": 100, "output": 20, "total": 120}),))
            previous.db.executemany("INSERT INTO resource_turns(task_id,turn_id,started_at,ended_at,seconds,server_started) VALUES ('old',?,1,2,1,1)",
                (("turn-" + str(n),) for n in range(10000)))
            previous.db.executemany("INSERT INTO events(task_id,kind,at,data) VALUES ('old','fixture',1,?)", [(json.dumps({"output": "x" * 1024 * 1024}),)] * 32)
            previous.db.execute("INSERT INTO operations VALUES ('key','preserved','running',NULL,'old')")
        last = previous.highwater()
        previous.close()
        before, started = self.path.stat().st_size, time.perf_counter()
        migration = storage.MIGRATIONS[10][1]
        def no_journal_read(db):
            def authorized(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and table == "events":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorized)
            try:
                migration(db)
            finally:
                db.set_authorizer(None)
        with patch.dict(storage.MIGRATIONS, {10: ("resource_budget_projections", no_journal_read)}), \
                patch.object(storage.Store, "_decode", side_effect=AssertionError("Migration decoded a record")):
            current = storage.Store(self.state)
        self.addCleanup(current.close)
        elapsed = time.perf_counter() - started
        current.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = self.path.stat().st_size
        self.assertEqual(current.schema_version, storage.SCHEMA_VERSION)
        self.assertLessEqual(after - before, 128 * 1024)
        self.assertEqual(current.highwater(), last)
        self.assertEqual(current.db.execute("SELECT fingerprint FROM operations WHERE id='key'").fetchone()[0], "preserved")
        self.check_totals(current)
        baseline = json.loads(current.db.execute("SELECT token_baseline FROM resource_threads WHERE task_id='old'").fetchone()[0])
        self.assertIsNone(baseline["tokens"])
        self.assertTrue(baseline["frozen"])
        with closing(sqlite3.connect(current.backup_path)) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM events").fetchone()[0], 32)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        print(f"schema9->{storage.SCHEMA_VERSION}: {before} -> {after} bytes; {elapsed:.4f}s; 10000 turns and 32 MiB journal")

    def test_direct_v011_upgrade_keeps_operation_keys_sequences_and_unknown_usage(self):
        os.close(private_file(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR))
        with closing(sqlite3.connect(self.path)) as db:
            for statement in storage.SCHEMA_V1:
                db.execute(statement)
            db.execute("INSERT INTO tasks VALUES ('old',?)", (json.dumps(self.task()),))
            db.execute("INSERT INTO operations VALUES ('key','original','running',NULL,'old')")
            db.execute("INSERT INTO events(seq,task_id,kind,at,data) VALUES (27,'old','fixture',1,'{}')")
            db.commit()
        current = storage.Store(self.state)
        self.addCleanup(current.close)
        self.assertEqual(current.schema_version, storage.SCHEMA_VERSION)
        self.assertEqual(current.highwater(), 27)
        self.assertEqual(current.db.execute("SELECT fingerprint FROM operations WHERE id='key'").fetchone()[0], "original")
        self.assertIn("not_counted_before_upgrade", current.resources.usage("old")["gaps"])
        self.check_totals(current)
        self.assertIsNotNone(current.backup_path)

    def test_schema9_import_is_identified_by_its_operation_even_without_task_flag(self):
        previous = self.version9()
        with previous.transaction():
            previous.db.execute("INSERT INTO resource_threads VALUES ('old','native-old',?,0,'[]',0,NULL)", (json.dumps({"input": 40000, "output": 0, "total": 40000}),))
            previous.db.execute("INSERT INTO resource_turns(task_id,turn_id,ended_at,seconds) VALUES ('old','imported-history',1,1)")
            previous.db.execute("INSERT INTO operations VALUES ('import','fingerprint','done','{}','old')")
            previous.db.execute("INSERT INTO operation_details VALUES ('import',NULL,'codex_sessions',1,?)", (json.dumps({"arguments": {"action": "import"}}),))
        previous.close()
        current = storage.Store(self.state)
        self.addCleanup(current.close)
        usage = current.resources.usage("old")
        self.assertEqual(usage["tokens"]["total"], 40000)
        self.assertIn("inherited_token_baseline_unknown", usage["gaps"])
        self.assertIsNone(current.resources.breach(usage, {"max_tokens": 1}))

    def test_failed_schema10_rolls_back_and_reopen_does_not_repeat_backup(self):
        self.version9().close()
        migration = storage.MIGRATIONS[10][1]
        def interrupted(db):
            migration(db)
            raise OSError(errno.ENOSPC, "Injected disk full")
        with patch.dict(storage.MIGRATIONS, {10: ("resource_budget_projections", interrupted)}), self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_migration_failed")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertNotIn("token_baseline", [row[1] for row in db.execute("PRAGMA table_info(resource_threads)")])
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_totals'").fetchone())
        storage.Store(self.state).close()
        backups = sorted(self.state.glob("*.bak"))
        storage.Store(self.state).close()
        self.assertEqual(sorted(self.state.glob("*.bak")), backups)
        with patch.object(storage, "SCHEMA_VERSION", 9), self.assertRaises(BridgeError) as error:
            storage.Store(self.state)
        self.assertEqual(error.exception.code, "state_newer_schema")

    def test_randomized_projection_updates_roll_back_and_match_after_prune(self):
        current = storage.Store(self.state)
        self.addCleanup(current.close)
        for identity in ("a", "b"):
            current.save("tasks", {**self.task(), "id": identity, "thread_id": "native-" + identity})
        rng, present, number = random.Random(315), {}, 0
        for _ in range(1000):
            choice = rng.choice(("insert", "update", "delete")) if present else "insert"
            ended = rng.choice((None, 10.0))
            values = (rng.choice((1.0, None)), ended, rng.choice((None, 2.5)) if ended else None,
                rng.randrange(2), rng.randrange(2), rng.choice((current.resources.connection, "old")))
            with current.transaction():
                if choice == "insert":
                    number += 1
                    turn, task = str(number), rng.choice(("a", "b"))
                    current.db.execute("INSERT INTO resource_turns(task_id,turn_id,started_at,ended_at,seconds,server_started,unknown,connection) VALUES (?,?,?,?,?,?,?,?)", (task, turn, *values))
                    present[turn] = task
                else:
                    turn = rng.choice(list(present))
                    task = present[turn]
                    if choice == "delete":
                        current.db.execute("DELETE FROM resource_turns WHERE task_id=? AND turn_id=?", (task, turn))
                        del present[turn]
                    else:
                        new_task = rng.choice(("a", "b"))
                        current.db.execute("UPDATE resource_turns SET task_id=?,started_at=?,ended_at=?,seconds=?,server_started=?,unknown=?,connection=? WHERE task_id=? AND turn_id=?", (new_task, *values, task, turn))
                        present[turn] = new_task
            self.check_totals(current)
        with self.assertRaises(RuntimeError):
            with current.transaction():
                current.db.execute("DELETE FROM resource_turns")
                raise RuntimeError("Injected crash")
        self.check_totals(current)
        with current.transaction():
            current.event("a", "bridge/fixture", {"text": "prunable"})
            current.db.execute("UPDATE events SET at=1")
        retention = Retention(current, clock=lambda: 10000)
        plan = retention.plan(100)
        self.assertEqual(plan["counts"]["events"], 1)
        retention.commit(plan["plan_id"])
        self.assertFalse(current.db.execute("SELECT 1 FROM events").fetchone())
        self.check_totals(current)
