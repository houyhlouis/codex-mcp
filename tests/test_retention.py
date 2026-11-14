"""Explicit retention protects references and rejects changed or expired plans."""

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp import store as storage
from codex_local_mcp.evidence import artifact
from codex_local_mcp.lifecycle import Lifecycle
from codex_local_mcp.platforms import lock_file, unlock_file
from codex_local_mcp.retention import PLAN_SECONDS, Retention, backup, duration, offline, render
from codex_local_mcp.security import BridgeError, private_file
from codex_local_mcp.tools import dispatch
from tests.helpers import temp_root
from tests.test_service import ServiceHarness


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.project = self.base / "project"
        self.project.mkdir(mode=0o700)
        self.store = storage.Store(self.state)
        self.addCleanup(lambda: self.store.close())
        self.config = SimpleNamespace(state_dir=self.state)
        self.clock = 10000.0
        self.retention = Retention(self.store, clock=lambda: self.clock)

    def task(self, identity="finished", status="completed", **fields):
        return self.store.save("tasks", {"id": identity, "thread_id": "native-" + identity, "project": str(self.project),
            "status": status, "created_at": 1, "updated_at": 1, "turn_id": None, **fields})

    def operation(self, identity="key", task_id="finished", state="done"):
        self.store.db.execute("INSERT INTO operations VALUES (?, 'fingerprint', ?, ?, ?)", (identity, state, json.dumps({"summary": "old"}), task_id))
        self.store.db.execute("UPDATE operation_times SET updated_at=1 WHERE id=?", (identity,))

    def event(self, identity="finished", content="saved"):
        seq = self.store.event(identity, "bridge/test", {"content": content})
        self.store.db.execute("UPDATE events SET at=1 WHERE seq=?", (seq,))
        return seq

    def image(self, task_id="finished", state="prepared"):
        directory = self.state / "images"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (task_id + ".png")
        with os.fdopen(private_file(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), "wb") as stream:
            stream.write(b"image")
        self.store.db.execute("INSERT INTO managed_images VALUES (?,?,?,?,?,?,?,?,1)",
            ("image-" + task_id, task_id, None, str(path), hashlib.sha256(b"image").hexdigest(), 5, state, 1))
        return path

    def test_duration_is_explicit_and_positive(self):
        for value, seconds in (("30", 2592000), ("30d", 2592000), ("12h", 43200), ("2w", 1209600), ("3m", 180), ("1s", 1)):
            self.assertEqual(duration(value), seconds)
        for bad in ("0", "-2d", "1.5d", "1 day", "01d", "1H", "", "1h2m"):
            with self.subTest(value=bad), self.assertRaises(BridgeError):
                duration(bad)

    def test_expiring_acknowledgement_keeps_the_familys_own_execution_identity(self):
        self.task()
        self.operation()
        self.store.db.execute("INSERT INTO operation_details (id,name,task_id,created_at,data) VALUES (?,?,?,?,?)",
            ("key", "codex_start", "finished", 1, json.dumps({"operation_outcome": {"acknowledgement": "acknowledged"},
                "acknowledged_turn": {"turn_id": "turn-1"}})))
        seq = self.store.event("finished", "bridge/input", {"text": "old work"})
        self.store.db.execute("UPDATE events SET at=1 WHERE seq=?", (seq,))
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["events"], 1)
        self.retention.commit(plan["plan_id"])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM events WHERE seq=?", (seq,)).fetchone())
        self.assertEqual(self.store.task_head("finished")["workspace_execution_project"], str(self.project))

    def test_expiring_unacknowledged_input_does_not_grant_execution_identity(self):
        self.task()
        seq = self.store.event("finished", "bridge/input", {"text": "never acknowledged"})
        self.store.db.execute("UPDATE events SET at=1 WHERE seq=?", (seq,))
        plan = self.retention.plan(100)
        self.retention.commit(plan["plan_id"])
        self.assertIsNone(self.store.task_head("finished").get("workspace_execution_project"))

    def test_prune_removes_recovery_cache_with_source_events_and_accounts_for_its_bytes(self):
        with self.store.transaction():
            self.task()
            for index in range(50):
                seq = self.event(content=str(index))
                self.store.db.execute("INSERT INTO recovered_items VALUES ('finished','old-turn',?,?,?)", (str(index), "a" * 64, seq))
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["events"], 50)
        self.assertEqual(plan["counts"]["recovered_items"], 50)
        event_bytes = self.store.db.execute("SELECT SUM(LENGTH(CAST(data AS BLOB))) FROM events").fetchone()[0]
        self.assertGreater(plan["estimated_reclaimable_bytes"], event_bytes)
        self.retention.commit(plan["plan_id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM recovered_items").fetchone()[0], 0)

    def test_recovery_cache_cleanup_is_atomic_and_protected_sources_keep_their_cache(self):
        with self.store.transaction():
            self.task()
            seq = self.event()
            self.store.db.execute("INSERT INTO recovered_items VALUES ('finished','old-turn','item',?,?)", ("a" * 64, seq))
            self.store.db.execute("INSERT INTO metadata VALUES ('source-reference',?)", (json.dumps({"event_seq": str(seq)}),))
        held = self.retention.plan(100)
        self.assertEqual(held["counts"].get("recovered_items", 0), 0)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM metadata WHERE key='source-reference'")
        plan = self.retention.plan(100)
        with self.store.transaction():
            self.store.db.execute("CREATE TRIGGER fail_source_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'injected prune crash'); END")
        with self.assertRaises(BridgeError):
            self.retention.commit(plan["plan_id"])
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE seq=?", (seq,)).fetchone())
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM recovered_items WHERE source_seq=?", (seq,)).fetchone())

    def test_retained_reconciliation_sources_are_protected_and_receipts_invalidate_plans(self):
        from codex_local_mcp.supervision import save_receipt
        with self.store.transaction():
            self.task()
            seq = self.event()
            self.store.db.execute("INSERT INTO reconciliation_reports VALUES ('finished',1,1,1,?)",
                (json.dumps({"projection_sources": {"commands": [{"source": "journal", "event_seq": seq}]}}),))
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["events"], 0)
        with self.store.transaction():
            save_receipt(self.store, {"request_id": "pending-id", "task_id": "finished", "turn_id": None,
                "decision_source": "main_agent_denial", "decision": "decline", "proposal_hash": None})
        with self.assertRaises(BridgeError) as error:
            self.retention.commit(plan["plan_id"])
        self.assertEqual(error.exception.code, "prune_plan_invalid")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 1)

    def test_dry_run_persists_plan_only_and_commit_keeps_dedup_tombstones(self):
        with self.store.transaction():
            self.task()
            self.operation()
            seq = self.event()
        before = self.retention.snapshot()
        report = self.retention.plan(100)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["counts"]["operations"], 1)
        self.assertEqual(self.retention.snapshot(), before)
        self.assertIn("dry run", render(report))
        applied = self.retention.commit(report["plan_id"])
        self.assertFalse(applied["dry_run"])
        self.assertTrue(applied["vacuum_needed"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT fingerprint FROM operation_tombstones WHERE id='key'").fetchone()[0], "fingerprint")
        self.assertEqual(self.store.highwater(), seq)
        with self.store.transaction():
            self.assertGreater(self.store.event("finished", "new", {}), seq)
        with self.assertRaises(BridgeError) as expired:
            self.store.events(0, 100)
        self.assertEqual(expired.exception.code, "cursor_expired")
        self.assertEqual(expired.exception.as_dict()["resume_after"], seq)
        self.assertEqual(len(self.store.events(seq, 100)["events"]), 1)
        with self.assertRaises(BridgeError):
            self.retention.commit(report["plan_id"])

    def test_unseen_retained_events_before_a_gap_are_delivered_first(self):
        with self.store.transaction():
            self.task("live", "running")
            self.task()
            first = self.event("live")
            expired = self.event()
            last = self.event("live")
        plan = self.retention.plan(100)
        self.retention.commit(plan["plan_id"])
        page = self.store.events(0, 100)
        self.assertEqual([r["seq"] for r in page["events"]], [first])
        self.assertEqual(page["cursor"], first)
        with self.assertRaises(BridgeError) as caught:
            self.store.events(first, 100)
        self.assertEqual(caught.exception.details["resume_after"], expired)
        self.assertEqual([r["seq"] for r in self.store.events(expired, 100)["events"]], [last])
        scoped = self.store.events(0, 100, task_id="live")
        self.assertEqual([r["seq"] for r in scoped["events"]], [first, last])
        self.assertEqual(scoped["cursor"], last)

    def test_protected_families_requests_operations_claims_and_metadata(self):
        with self.store.transaction():
            self.task()
            self.task("live", "running")
            self.task("child", root_task_id="live", parent_task_id="live")
            self.task("asked")
            self.task("unresolved")
            self.task("claimed")
            self.task("meta")
            self.store.save("requests", {"id": "ask", "task_id": "asked", "status": "pending", "created_at": 1})
            self.operation("running", "unresolved", "running")
            self.store.claim("claimed", "queue", "uncertain")
            self.store.db.execute("INSERT INTO metadata VALUES ('mcp_execution:edge',?)", (json.dumps({"task_id": "meta", "unknown": [1, 2]}),))
            for task_id in ("finished", "live", "child", "asked", "unresolved", "claimed", "meta"):
                self.event(task_id)
        original_meta = list(self.store.db.execute("SELECT * FROM metadata"))
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["events"], 1)
        self.retention.commit(plan["plan_id"])
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT task_id FROM events")}, {"live", "child", "asked", "unresolved", "claimed", "meta"})
        self.assertEqual([tuple(r) for r in self.store.db.execute("SELECT * FROM metadata")], [tuple(r) for r in original_meta])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations WHERE state='running'").fetchone()[0], 1)

    def test_plan_survives_process_loss_but_changed_revision_or_reference_is_refused(self):
        with self.store.transaction():
            self.task()
            self.event()
        plan = self.retention.plan(100)
        self.store.close()
        self.store = storage.Store(self.state)
        self.retention = Retention(self.store, clock=lambda: self.clock)
        self.retention.commit(plan["plan_id"])
        with self.store.transaction():
            self.event()
        plan = self.retention.plan(100)
        with closing(sqlite3.connect(self.state / "state.sqlite3")) as concurrent:
            concurrent.execute("UPDATE tasks SET revision=revision+1 WHERE id='finished'")
            concurrent.commit()
        with self.assertRaises(BridgeError) as changed:
            self.retention.commit(plan["plan_id"])
        self.assertEqual(changed.exception.code, "prune_plan_invalid")
        plan = self.retention.plan(100)
        with self.store.transaction():
            self.store.db.execute("INSERT INTO metadata VALUES ('mcp_execution:new','{}')")
        with self.assertRaises(BridgeError):
            self.retention.commit(plan["plan_id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_expiry_byte_target_and_unknown_plan_are_explicit(self):
        with self.store.transaction():
            self.task()
            for _ in range(3):
                self.event(content="x" * 1000)
        plan = self.retention.plan(100, max_bytes=1500)
        self.assertEqual(plan["counts"]["events"], 2)
        self.assertTrue(plan["byte_target_met"])
        self.clock += PLAN_SECONDS
        for identity in (plan["plan_id"], "unknown"):
            with self.assertRaises(BridgeError) as caught:
                self.retention.commit(identity)
            self.assertEqual(caught.exception.code, "prune_plan_invalid")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)

    def test_interrupted_commit_rolls_back_tombstones_events_and_file_changes(self):
        with self.store.transaction():
            self.task()
            self.operation()
            self.event()
            image = self.image()
            self.store.db.execute("CREATE TRIGGER disk_full BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'disk full'); END")
        plan = self.retention.plan(100)
        with self.assertRaises(BridgeError) as failed:
            self.retention.commit(plan["plan_id"])
        self.assertEqual(failed.exception.code, "maintenance_failed")
        self.assertFalse(failed.exception.details["applied"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operation_tombstones").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT state FROM operations").fetchone()[0], "done")
        self.assertEqual(image.read_bytes(), b"image")
        self.assertIsNone(self.store.db.execute("SELECT committed_at FROM retention_plans WHERE id=?", (plan["plan_id"],)).fetchone()[0])
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_retained_result_protects_sources_and_artifacts_then_expires_together(self):
        with self.store.transaction():
            self.task(turn_id="turn", projection_turn_id="turn", evidence_complete=True)
            self.store.db.execute("INSERT INTO turns(task_id,thread_id,turn_id,status) VALUES ('finished','native-finished','turn','completed')")
            seq = self.store.event("finished", "item/completed", {"turnId": "turn", "item": {"id": "claim", "type": "agentMessage", "text": "x" * 20000}})
            self.store.db.execute("UPDATE events SET at=1")
            self.store.evidence.capture("finished", "turn")
            self.store.db.execute("UPDATE turn_results SET created_at=9999")
            self.store.db.execute("UPDATE artifacts SET created_at=1")
        protected = self.retention.plan(100)
        self.assertEqual(protected["counts"]["events"], 0)
        self.assertEqual(protected["counts"]["artifacts"], 0)
        stored = self.store.db.execute("SELECT content,source_seq,source_locator FROM artifacts").fetchone()
        self.assertIsNone(stored["content"])
        self.assertEqual(stored["source_seq"], seq)
        self.assertIsNotNone(stored["source_locator"])
        self.retention.commit(protected["plan_id"])
        with self.store.transaction():
            self.store.db.execute("UPDATE turn_results SET created_at=1")
        expired = self.retention.plan(100)
        self.assertEqual(expired["counts"]["artifacts"], 1)
        self.assertEqual(expired["counts"]["events"], 1)
        self.retention.commit(expired["plan_id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        with self.assertRaises(BridgeError) as caught:
            self.store.evidence.result("finished", "turn")
        self.assertEqual(caught.exception.code, "result_expired")

    def test_young_journal_reference_protects_its_old_source_without_a_result(self):
        with self.store.transaction():
            self.task(turn_id="turn")
            seq = self.store.event("finished", "item/completed", {"turnId": "turn", "item": {"id": "claim", "type": "agentMessage", "text": "x" * 20000}})
            self.store.db.execute("UPDATE events SET at=1")
            self.store.db.execute("DELETE FROM evidence_entries")
            self.store.db.execute("UPDATE artifacts SET created_at=9999")
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["artifacts"], 0)
        self.assertEqual(plan["counts"]["events"], 0)
        self.retention.commit(plan["plan_id"])
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE seq=?", (seq,)).fetchone())

    def test_human_plan_formats_local_expiry_sizes_and_upgrade_recovery(self):
        with self.store.transaction():
            self.task()
            self.event(content="x" * (2 * 1024 * 1024))
            self.store.claim("finished", "uncertain", "uncertain", {"reason": "schema_upgrade"})
        plan = self.retention.plan(100)
        raw = json.dumps(plan, sort_keys=True)
        output = render(plan, now=self.clock)
        local = datetime.fromtimestamp(plan["expires_at"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
        self.assertIn(local, output)
        self.assertIn("60 min left", output)
        self.assertIn("MiB", output)
        self.assertIn("not been re-checked since the upgrade", output)
        self.assertIn("Start the server once or resume these tasks", output)
        self.assertIn("make a new plan", output)
        self.assertEqual(json.dumps(plan, sort_keys=True), raw)
        self.assertIsInstance(plan["expires_at"], float)
        self.assertIsInstance(plan["protected"]["workspace_claims"]["bytes"], int)
        self.assertIn("0 min left", render(plan, now=plan["expires_at"] + 10))

    def test_compatible_task_extensions_keep_their_scoped_references(self):
        with self.store.transaction():
            self.task()
            ref = artifact(self.store.db, "finished", "old-turn", b"retained bytes")
            self.store.db.execute("UPDATE artifacts SET created_at=1")
            self.store.update("tasks", "finished", {"future": {"reference": ref["artifact_id"]}})
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["artifacts"], 0)
        self.retention.commit(plan["plan_id"])
        self.assertEqual(self.store.db.execute("SELECT content FROM artifacts WHERE id=?", (ref["artifact_id"],)).fetchone()[0], b"retained bytes")

    def test_submitted_images_stay_protected_after_deletion_and_prepared_images_can_expire(self):
        with self.store.transaction():
            self.task()
            image = self.image(state="native_history")
        self.assertEqual(self.retention.plan(100)["counts"]["managed_images"], 0)
        with self.store.transaction():
            self.store.update("tasks", "finished", {"status": "deleted", "deleted": True})
            self.task("fork", forked_from_task_id="finished")
        self.assertEqual(self.retention.plan(100)["counts"]["managed_images"], 0)
        with self.store.transaction():
            self.store.update("tasks", "fork", {"status": "deleted", "deleted": True})
            self.task("never", "deleted", deleted=True)
            prepared = self.image("never")
        plan = self.retention.plan(100)
        self.assertEqual(plan["counts"]["managed_images"], 1)
        self.assertEqual(plan["protected"]["native_image_history"]["items"], 1)
        self.assertEqual(plan["protected"]["native_image_history"]["bytes"], 5)
        self.assertIn("external forks", plan["protected"]["native_image_history"]["reason"])
        prepared.write_bytes(b"other")
        with self.assertRaises(BridgeError) as changed:
            self.retention.commit(plan["plan_id"])
        self.assertEqual(changed.exception.code, "prune_plan_invalid")
        prepared.write_bytes(b"image")
        plan = self.retention.plan(100)
        self.retention.commit(plan["plan_id"])
        self.assertFalse(prepared.exists())
        self.assertTrue(image.exists())

    def test_backup_is_consistent_private_online_and_never_overwrites_or_enters_project(self):
        with self.store.transaction():
            self.task()
            self.operation()
        fd = private_file(self.state / "broker.lock", os.O_CREAT | os.O_RDWR)
        lock_file(fd)
        try:
            with self.assertRaises(BridgeError) as busy:
                with offline(self.config):
                    self.fail("An active broker lock must refuse pruning")
            self.assertEqual(busy.exception.code, "maintenance_busy")
            report = backup(self.config)
        finally:
            unlock_file(fd)
            os.close(fd)
        path = Path(report["backup"])
        os.close(private_file(path, os.O_RDONLY))
        with closing(sqlite3.connect(path)) as copied:
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copied.execute("SELECT id FROM operations").fetchone()[0], "key")
        before = path.read_bytes()
        with self.assertRaises(BridgeError) as existing:
            backup(self.config, str(path))
        self.assertEqual(existing.exception.code, "backup_destination_exists")
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(BridgeError, "must not be inside a project"):
            backup(self.config, str(self.project / "backup.sqlite3"))
        self.assertFalse((self.project / "backup.sqlite3").exists())
        with self.assertRaises(BridgeError):
            backup(self.config, "relative.sqlite3")
        with offline(self.config):
            pass

    def test_backup_windows_helper_existing_file_race_is_the_same_refusal(self):
        target = self.state / "raced-copy.bak"
        original = private_file
        def windows_helper(path, flags):
            if flags & os.O_EXCL:
                Path(path).write_bytes(b"existing content")
                Path(path).chmod(0o600)
                try:
                    raise FileExistsError("Windows create-new refusal")
                except FileExistsError as cause:
                    raise BridgeError("Expected a private user-owned file") from cause
            return original(path, flags)
        with patch("codex_local_mcp.retention.private_file", windows_helper), self.assertRaises(BridgeError) as existing:
            backup(self.config, str(target))
        self.assertEqual(existing.exception.code, "backup_destination_exists")
        self.assertEqual(str(existing.exception), "Backup destination already exists; choose a new file path")
        self.assertEqual(target.read_bytes(), b"existing content")

    def test_plan_housekeeping_preserves_live_plans_and_bounds_storage(self):
        from codex_local_mcp import retention
        first = self.retention.plan(100)
        with self.store.transaction():
            for identity, expires, applied in (("expired", self.clock, None), ("applied", self.clock + 100, self.clock)):
                self.store.db.execute("INSERT INTO retention_plans VALUES (?,?,?,?,?,?,?)",
                    (identity, 1, expires, "old", "{}", "x" * 4096, applied))
        with patch.object(retention, "PLAN_LIMIT", 2):
            second = self.retention.plan(100)
            with self.assertRaises(BridgeError) as full:
                self.retention.plan(100)
        self.assertEqual(full.exception.code, "prune_plan_limit")
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT id FROM retention_plans")}, {first["plan_id"], second["plan_id"]})
        self.retention.commit(first["plan_id"])
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT id FROM retention_plans")}, {second["plan_id"]})
        self.clock += PLAN_SECONDS
        with self.assertRaises(BridgeError):
            self.retention.commit(second["plan_id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM retention_plans").fetchone()[0], 0)
        with patch.object(retention, "PLAN_STORAGE_BYTES", 1), self.assertRaises(BridgeError) as size:
            self.retention.plan(100)
        self.assertEqual(size.exception.code, "prune_plan_limit")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM retention_plans").fetchone()[0], 0)

    def execution(self, identity, task_id, *, expired=True, **changes):
        value = {"id": identity, "state": "completed", "revision": 1, "created_at": 1, "updated_at": 2,
            "terminal_at": self.clock - 86400 if expired else self.clock, "tool_name": "codex_start", "operation_id": "op-" + identity,
            "bridge_task_id": task_id, "thread_id": "native-" + task_id, "turn_id": "turn", "generation": 1,
            "cancel_requested": False, "inputs": {}, **changes}
        key = "mcp_execution:" + identity
        self.store.db.execute("INSERT INTO metadata VALUES (?,?)", (key, json.dumps(value)))
        return key

    def test_expired_execution_metadata_no_longer_protects_but_live_and_foreign_records_do(self):
        with self.store.transaction():
            events = {}
            for task_id in ("expired", "live", "foreign", "claim"):
                self.task(task_id)
                events[task_id] = self.event(task_id)
            expired = self.execution("1" * 32, "expired")
            live = self.execution("2" * 32, "live", expired=False)
            held = self.execution("3" * 32, "claim")
            self.store.db.execute("INSERT INTO metadata VALUES ('other:owner',?)", (json.dumps({"task_id": "foreign"}),))
            self.store.claim("claim", "goal", "active")
        before = self.retention.snapshot()
        report = self.retention.plan(100)
        self.assertEqual(self.retention.snapshot(), before)
        saved = json.loads(self.store.db.execute("SELECT data FROM retention_plans WHERE id=?", (report["plan_id"],)).fetchone()[0])
        self.assertEqual(set(saved["expired_executions"]), {expired, held})
        self.assertEqual(saved["selected"]["events"], [events["expired"]])
        self.assertEqual(report["counts"]["execution_metadata"], 2)
        self.retention.commit(report["plan_id"])
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT key FROM metadata")}, {live, "other:owner"})
        self.assertEqual({r[0] for r in self.store.db.execute("SELECT task_id FROM events")}, {"live", "foreign", "claim"})

    def test_expiry_recheck_skips_are_refused_and_all_deletions_roll_back(self):
        with self.store.transaction():
            self.task()
            seq = self.event()
            key = self.execution("1" * 32, "finished")
            older = self.execution("2" * 32, "finished", terminal_at=self.clock - 86401)
        plan = self.retention.plan(100)
        self.clock -= 1  # The same unchanged record is no longer expired.
        with self.assertRaises(BridgeError) as stale:
            self.retention.commit(plan["plan_id"])
        self.assertEqual(stale.exception.code, "prune_plan_invalid")
        self.assertTrue(self.store.db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone())
        self.assertTrue(self.store.db.execute("SELECT 1 FROM metadata WHERE key=?", (older,)).fetchone())
        self.assertTrue(self.store.db.execute("SELECT 1 FROM events WHERE seq=?", (seq,)).fetchone())

    def contract(self, task_id="finished", *, created_at=1, evidence=None):
        from codex_local_mcp.decisions import summary
        record = {"version": 1, "id": "inspect", "kind": "delegation", "goal": "Inspect the target", "authority": "guidance_only",
            "scope": {"task_id": task_id, "root_task_id": task_id, "thread_id": "native-" + task_id, "turn_id": None, "generation": 1, "operation_id": "contract-op"},
            "created_at": created_at, "expires_at": None, "evidence": evidence or [], "delivery": {"state": "acknowledged"}}
        record["context_hash"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        seq = self.store.event(task_id, "bridge/delegation_contract", record)
        self.store.db.execute("UPDATE events SET at=1 WHERE seq=?", (seq,))
        record["source"] = {"event_seq": seq}
        self.store.update("tasks", task_id, {"delegation_contract": record, "delegation_contract_summary": summary(record),
            "delegation_contracts": [{**summary(record), "evidence": evidence or []}]})
        return record

    def test_contracts_of_active_or_unresolved_families_are_protected(self):
        with self.store.transaction():
            for task_id, status in (("running", "running"), ("pending", "completed"), ("claim", "completed"), ("operation", "completed")):
                self.task(task_id, status)
                self.contract(task_id)
            self.store.save("requests", {"id": "question", "task_id": "pending", "status": "pending", "params": {}})
            self.store.claim("claim", "goal", "active")
            self.operation(task_id="operation", state="uncertain")
        report = self.retention.plan(100)
        self.assertEqual(report["counts"]["delegation_contracts"], 0)
        self.assertEqual(report["protected"]["delegation_contracts"]["items"], 4)
        self.assertIn("Finish/reconcile the family", render(report))

    def test_contract_changes_between_plan_and_apply_refuse_the_plan(self):
        with self.store.transaction():
            self.task()
            record = self.contract()
        report = self.retention.plan(100)
        self.assertEqual(report["delegation_contracts"][0]["context_hash"], record["context_hash"])
        self.assertGreater(report["delegation_contract_bytes"], 0)
        self.assertIn(record["context_hash"], render(report))
        self.store.update("tasks", "finished", {"delegation_contract": {**record, "goal": "Changed"}})
        with self.assertRaises(BridgeError) as changed:
            self.retention.commit(report["plan_id"])
        self.assertEqual(changed.exception.code, "prune_plan_invalid")
        self.assertEqual(self.store.get("tasks", "finished")["delegation_contract"]["goal"], "Changed")

    def test_contract_only_evidence_expires_in_the_same_plan_and_not_before(self):
        with self.store.transaction():
            self.task(turn_id="turn", turn_status="completed")
            source = self.event()
            ref = artifact(self.store.db, "finished", "turn", b"owned evidence", source_seq=source)
            self.store.db.execute("UPDATE artifacts SET created_at=1")
            value = {"format_version": 1, "task_id": "finished", "turn_id": "turn", "artifacts": [ref]}
            digest = hashlib.sha256(json.dumps(value).encode()).hexdigest()
            self.store.db.execute("INSERT INTO turn_results VALUES ('result','finished','turn',1,1,?,?)", (digest, json.dumps(value)))
            evidence = [{"task_id": "finished", "turn_id": "turn", "result_id": "result", "artifact_id": ref["artifact_id"], "sha256": ref["sha256"]}]
            record = self.contract(created_at=self.clock - 5, evidence=evidence)
            # Retained operation snapshots and descriptions also contain owned contracts.
            self.operation()
            self.store.db.execute("UPDATE operations SET result=?", (json.dumps({"delegation_contract": record}),))
            self.store.db.execute("INSERT INTO operation_details VALUES ('key','finished','codex_delegate',1,?)", (json.dumps({"contract": record}),))
        before = self.retention.plan(100)
        self.assertEqual(before["counts"]["delegation_contracts"], 0)
        self.assertEqual(before["counts"]["turn_results"], 0)
        self.assertEqual(before["counts"]["artifacts"], 0)
        self.clock += 200
        report = self.retention.plan(100)
        self.assertEqual(report["counts"]["delegation_contracts"], 1)
        self.assertEqual(report["counts"]["turn_results"], 1)
        self.assertEqual(report["counts"]["artifacts"], 1)
        self.retention.commit(report["plan_id"])
        self.assertIsNone(self.store.get("tasks", "finished")["delegation_contract"])
        self.assertEqual(self.store.get("tasks", "finished")["delegation_contracts"], [])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM artifacts").fetchone())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM turn_results").fetchone())

    def test_compaction_is_explicit(self):
        with self.store.transaction():
            self.task()
            self.event(content="x" * 10000)
        plan = self.retention.plan(100)
        self.assertFalse(self.retention.commit(plan["plan_id"], compact=True)["vacuum_needed"])
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_file_cleanup_is_retained_and_revalidated_on_the_next_apply(self):
        with self.store.transaction():
            self.task()
            image = self.image()
        plan = self.retention.plan(100)
        with patch.object(Path, "unlink", side_effect=OSError("file busy")):
            applied = self.retention.commit(plan["plan_id"])
        self.assertEqual(applied["image_cleanup_pending"], ["image-finished"])
        self.assertEqual(applied["reclaimed_payload_bytes"], 0)
        self.assertIn("Image cleanup pending:", render(applied))
        self.assertTrue(image.exists())
        with self.store.transaction():
            self.store.db.execute("INSERT INTO metadata VALUES ('mcp_execution:hold',?)", (json.dumps({"image": str(image)}),))
        blocked = self.retention.plan(100)
        self.assertEqual(blocked["counts"]["managed_images"], 0)
        self.retention.commit(blocked["plan_id"])
        self.assertTrue(image.exists())
        with self.store.transaction():
            self.store.db.execute("DELETE FROM metadata WHERE key='mcp_execution:hold'")
        final = self.retention.plan(100)
        self.retention.commit(final["plan_id"])
        self.assertFalse(image.exists())

    def test_backup_write_failure_removes_partial_copy_and_leaves_source_usable(self):
        with self.store.transaction():
            self.task()
        original_connect = sqlite3.connect
        class FailingReader:
            def __init__(self, db):
                object.__setattr__(self, "db", db)
            def __getattr__(self, name):
                return getattr(self.db, name)
            def __setattr__(self, name, value):
                setattr(self.db, name, value)
            def backup(self, destination):
                raise OSError("disk full")
        def connect(path, *args, **kw):
            db = original_connect(path, *args, **kw)
            return FailingReader(db) if "mode=ro" in str(path) else db
        target = self.state / "failed-copy.bak"
        with patch("codex_local_mcp.retention.sqlite3.connect", connect):
            with self.assertRaises(OSError):
                backup(self.config, str(target))
        self.assertFalse(target.exists())
        self.assertEqual(self.store.get("tasks", "finished")["status"], "completed")
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class ImageOwnershipTest(ServiceHarness):
    async def test_request_cursors_expire_on_prune_or_compaction_instead_of_reusing_rowids(self):
        task = await self.start_task("hold")
        with self.store.transaction():
            for i in range(3):
                self.store.save("requests", {"id": "pending-" + str(i), "task_id": task["id"], "status": "pending", "kind": "parent_question", "params": {"question": str(i)}})
        first = self.service.request_page(task["id"], limit=1)
        cursor = first["next_cursor"]
        retention = Retention(self.store)
        plan = retention.plan(1)
        retention.commit(plan["plan_id"], compact=True)
        with self.assertRaises(BridgeError) as expired:
            self.service.request_page(task["id"], cursor, 1)
        self.assertEqual(expired.exception.code, "cursor_expired")
        self.assertEqual(expired.exception.details["cursor_kind"], "requests")
        page = self.service.request_page(task["id"], limit=1)
        following = self.service.request_page(task["id"], page["next_cursor"], 1)
        self.assertNotEqual(page["requests"][0]["id"], following["requests"][0]["id"])

    async def test_late_validation_failure_never_leaves_an_orphan_image(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        project = Path(task["project"])
        (project / "valid.png").write_bytes(b"image")
        with self.assertRaises(BridgeError):
            await self.service.build_input(task, "image", images=["valid.png"], skills=["unknown"])
        with self.assertRaises(BridgeError):
            await self.service.build_input(task, "image", images=["valid.png", "invalid.txt"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM managed_images").fetchone()[0], 0)
        self.assertEqual(list((self.state / "images").glob("*")), [])

    async def test_native_submission_marks_images_before_lost_ack(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        (Path(task["project"]) / "valid.png").write_bytes(b"image")
        gate = self.gate_call("lost", "turn/start", error=TimeoutError("lost ACK"))
        sending = asyncio.create_task(dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "images": ["valid.png"], "operation_id": "image-send"}))
        await gate.wait()
        row = self.store.db.execute("SELECT * FROM managed_images").fetchone()
        self.assertEqual(row["state"], "native_history")
        self.assertEqual(row["operation_id"], "image-send")
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await sending
        self.assertTrue(Path(row["path"]).exists())

    async def test_expired_operation_never_calls_handler_again(self):
        with self.store.transaction():
            self.store.db.execute("INSERT INTO operation_tombstones VALUES ('expired','fingerprint',NULL,1)")
        called = False
        async def handler():
            nonlocal called
            called = True
        with self.assertRaises(BridgeError) as caught:
            await self.service.operation("expired", "internal", {}, handler)
        self.assertFalse(called)
        self.assertEqual(caught.exception.code, "operation_expired")
