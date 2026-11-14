"""Selectors stay consistent with JSON and do not decode historical bodies."""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_local_mcp import store as storage
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.tools import dispatch
from tests.helpers import temp_root
from tests.test_service import ServiceHarness


class IndexedStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "state"
        self.directory.mkdir(mode=0o700)

    def fixture(self, version, duplicate=False):
        path = self.directory / "state.sqlite3"
        os.close(private_file(path, os.O_CREAT | os.O_RDWR))
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            for statement in storage.SCHEMA_V1:
                db.execute(statement)
            for i in range(300):
                value = {"id": str(i), "project": str(Path(self.temp.name) / "project"),
                         "thread_id": "thread-" + str(0 if duplicate and i == 1 else i),
                         "status": "completed", "summary": "x" * 8192, "unknown": {"keep": True},
                         "archived": i % 3 == 0, "deleted": i % 7 == 0}
                if i:
                    value.update(parent_task_id="0", root_task_id="0")
                db.execute("INSERT INTO tasks VALUES (?,?)", (str(i), json.dumps(value)))
                request = {"id": "r" + str(i), "task_id": str(i), "status": "pending" if i % 2 else "expired",
                           "kind": "parent_question", "params": {"threadId": value["thread_id"], "turnId": "t"},
                           "generation": "g", "rpc_id": i, "unknown": i}
                db.execute("INSERT INTO requests VALUES (?,?)", (request["id"], json.dumps(request)))
            db.execute("INSERT INTO operations VALUES ('key','fingerprint','done','{}','0')")
            db.execute("INSERT INTO events VALUES (77,'0','saved',1,'{}')")
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
        finally:
            db.close()

    def test_v1_v5_and_v6_backfill_preserves_all_records_and_backups(self):
        for version in (1, 5, 6):
            with self.subTest(version=version):
                self.fixture(version)
                store = storage.Store(self.directory)
                try:
                    self.assertTrue(store.check_projections()["consistent"])
                    self.assertEqual(store.schema_version, storage.SCHEMA_VERSION)
                    self.assertEqual(len(store.family_ids("0")), 300)
                    self.assertEqual(store.request_count("0"), 150)
                    self.assertEqual(store.task_for_thread("thread-299")["id"], "299")
                    self.assertNotIn("summary", store.task_head("299"))
                    self.assertEqual(store.get("tasks", "299")["unknown"], {"keep": True})
                    self.assertEqual(store.db.execute("SELECT fingerprint FROM operations").fetchone()[0], "fingerprint")
                    self.assertEqual(store.highwater(), 77)
                    backup = store.backup_path
                    with closing(sqlite3.connect(backup)) as saved:
                        self.assertEqual(saved.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 300)
                finally:
                    store.close()
                reopened = storage.Store(self.directory)
                self.assertIsNone(reopened.backup_path)
                reopened.close()
                for path in self.directory.iterdir():
                    path.unlink()

    def test_duplicate_thread_migration_is_refused_atomically(self):
        self.fixture(5, duplicate=True)
        with self.assertRaisesRegex(BridgeError, "Duplicate native thread id") as duplicate:
            storage.Store(self.directory)
        self.assertEqual(duplicate.exception.code, "duplicate_thread_id")
        self.assertEqual(duplicate.exception.ids, {"thread_id": "thread-0", "task_ids": ["0", "1"]})
        with closing(sqlite3.connect(self.directory / "state.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertNotIn("thread_id", {r[1] for r in db.execute("PRAGMA table_info(tasks)")})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 300)

    def test_failure_after_partial_index_backfill_rolls_back(self):
        self.fixture(5)
        def disk_full(db):
            storage.migrate_indexes(db)
            db.execute("UPDATE tasks SET status='running' WHERE id='0'")
            raise sqlite3.OperationalError("database or disk is full")
        with patch.dict(storage.MIGRATIONS, {6: ("indexed_identity_and_projections", disk_full)}):
            with self.assertRaisesRegex(BridgeError, "previous state was preserved") as failed:
                storage.Store(self.directory)
            self.assertEqual(failed.exception.code, "state_migration_failed")
        with closing(sqlite3.connect(self.directory / "state.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertNotIn("projection", {r[1] for r in db.execute("PRAGMA table_info(tasks)")})
            self.assertEqual(json.loads(db.execute("SELECT data FROM tasks WHERE id='0'").fetchone()[0])["status"], "completed")
            self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 5)
        store = storage.Store(self.directory)
        try:
            self.assertTrue(store.check_projections()["consistent"])
        finally:
            store.close()

    def test_atomic_selectors_unique_identity_and_explicit_repair(self):
        store = storage.Store(self.directory)
        try:
            with store.transaction():
                store.save("tasks", {"id": "root", "status": "completed", "thread_id": "native", "project": str(self.directory.parent / "project")})
                store.save("tasks", {"id": "child", "status": "idle", "parent_task_id": "root", "root_task_id": "root"})
                store.update("tasks", "child", {"archived": True, "deleted": True, "thread_id": "child-native"})
            self.assertTrue(store.check_projections()["consistent"])
            with self.assertRaisesRegex(BridgeError, "Duplicate native thread id"):
                store.update("tasks", "child", {"thread_id": "native"})
            self.assertEqual(store.task_head("child")["thread_id"], "child-native")
            store.db.execute("UPDATE tasks SET status='running',projection='{}' WHERE id='child'")
            with self.assertRaises(BridgeError) as invalid:
                store.task_for_thread("child-native")
            self.assertEqual(invalid.exception.code, "state_invalid")
            self.assertFalse(store.check_projections()["consistent"])
            self.assertTrue(store.check_projections(rebuild=True)["rebuilt"])
            self.assertTrue(store.check_projections()["consistent"])
            self.assertEqual(store.task_head("child")["status"], "idle")
        finally:
            store.close()


class IndexedServiceTest(ServiceHarness):
    async def test_family_event_paging_does_not_expand_sql_parameters(self):
        project = self.service.select_project(str(self.root / "family"), create=True)
        for i in range(40):
            value = {"id": str(i), "thread_id": "family-" + str(i), "status": "completed", "project": project["path"], "project_id": project["id"]}
            if i:
                value.update(parent_task_id="0", root_task_id="0")
            self.store.save("tasks", value)
            self.store.event(str(i), "saved", {})
        previous = self.store.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 16)
        try:
            result = await dispatch(self.service, "codex_events", {"task_id": "0", "view": "compact"})
            self.assertEqual(len(result["events"]), 40)
            self.assertEqual(result["cursor"], self.store.highwater())
        finally:
            self.store.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)

    async def test_adoption_import_and_recovery_keep_selectors_consistent(self):
        task = await self.start_task("native_children")
        # A parent can still be running while its child waits for approval.
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.request_page(task["id"])["requests"])), 5)
        request = self.service.request_page(task["id"])["requests"][0]
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(task["id"], ("completed",))
        await self.service.resume(task["id"])
        self.assertEqual(len(self.store.family_ids(task["id"])), 2)
        self.assertTrue(self.store.check_projections()["consistent"])
        await self.service.backend.close()
        await self.service.resume(task["id"])
        self.assertTrue(self.store.check_projections()["consistent"])
        options = await self.service.thread_options(self.service.task(task["id"]))
        native = await self.service.backend.call("thread/start", options)
        imported = await dispatch(self.service, "codex_sessions", {"workdir": task["project"], "action": "import",
            "thread_id": native["thread"]["id"], "confirm_stopped": True, "operation_id": "import"})
        self.assertEqual(self.store.task_for_thread(native["thread"]["id"])["id"], imported["id"])
        self.assertTrue(self.store.check_projections()["consistent"])

    async def test_notification_routing_decodes_a_constant_number_of_bodies(self):
        project = self.service.select_project(str(self.root / "project"), create=True)
        with self.store.transaction():
            for i in range(1000):
                self.store.save("tasks", {"id": str(i), "thread_id": "native-" + str(i),
                    "project": project["path"], "project_id": project["id"], "status": "completed", "summary": "x" * 8192})
        with patch.object(self.store, "_decode", wraps=self.store._decode) as decode:
            self.assertEqual(self.service.task_for_thread("native-999")["id"], "999")
            self.assertEqual(decode.call_count, 0)
            self.service.get("999")
            self.assertLessEqual(decode.call_count, 4)
        with patch.object(self.store, "all", side_effect=AssertionError("historical scan")):
            self.service.request_page("999")
            self.assertEqual(len(self.service.list_tasks(limit=3)["tasks"]), 3)
            self.assertEqual(self.service.family_ids("999"), {"999"})
        with patch.object(self.store, "_decode", wraps=self.store._decode) as decode:
            page = self.service.list_tasks(offset=900, limit=3)
            self.assertEqual(len(page["tasks"]), 3)
            self.assertEqual(decode.call_count, 4)  # Three bodies and one project descriptor.
        self.assertTrue(self.store.check_projections()["consistent"])

    async def test_reconciliation_reads_only_metadata_and_claims(self):
        project = self.service.select_project(str(self.root / "reconcile"), create=True)
        self.store.save("tasks", {"id": "idle", "thread_id": "native", "status": "idle", "project": project["path"],
            "project_id": project["id"], "summary": "x" * 1000000})
        responses = {"thread/goal/get": {"goal": {"status": "active"}}, "thread/queue/list": {"data": []},
            "thread/backgroundTerminals/list": {"data": []}, "thread/list": {"data": []}, "thread/turns/list": {"data": []},
            "thread/read": {"thread": {"id": "native", "cwd": project["path"], "status": {"type": "idle"}}}}
        fake = SimpleNamespace(alive=True, generation="generation", version="fixture", unavailable_methods=set(),
            call=AsyncMock(side_effect=lambda method, params: responses[method]))
        with patch.object(self.service, "backend", fake), patch.object(self.store, "_decode", wraps=self.store._decode) as decode:
            await self.service.reconcile_claims("idle")
        self.assertFalse(any(c.args[0] == "tasks" for c in decode.call_args_list))
        self.assertEqual([c["kind"] for c in self.store.claims(task_id="idle")], ["goal"])

    async def test_actual_event_cost_does_not_grow_with_history(self):
        project = self.service.select_project(str(self.root / "event-project"), create=True)
        counts = []
        for size in (10, 1000):
            with self.store.transaction():
                for i in range(10 if size == 1000 else 0, size):
                    self.store.save("tasks", {"id": str(i), "thread_id": "event-" + str(i),
                        "project": project["path"], "project_id": project["id"], "status": "completed", "summary": "x" * 8192})
            with patch.object(self.store, "_decode", wraps=self.store._decode) as decode:
                await self.service.on_message({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": "event-0", "tokenUsage": {"total": {"totalTokens": size}}}})
                counts.append(decode.call_count)
            self.assertEqual(self.store.get("tasks", "0")["usage"]["total"]["totalTokens"], size)
        self.assertEqual(counts[0], counts[1])
        self.assertLessEqual(counts[0], 5)
