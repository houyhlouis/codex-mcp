"""Offline MCP metadata expiry planning and transaction-owned deletion."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from codex_local_mcp.protocol_tasks import (
    PREFIX, REVIEW_AGE_SECONDS, TERMINAL_AGE_SECONDS,
    delete_expired_execution_keys, expired_execution_keys,
)
from codex_local_mcp.store import Store
from tests.helpers import temp_root


class ExecutionExpiryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "state"
        self.store = Store(self.directory)
        self.addCleanup(self.store.close)
        self.now = 100000.0

    def record(self, number=1, **changes):
        return {"id": f"{number:032x}", "state": "completed", "revision": 1,
            "created_at": 1, "updated_at": 2, "terminal_at": self.now - TERMINAL_AGE_SECONDS,
            "tool_name": "codex_start", "operation_id": "operation", "bridge_task_id": "task",
            "thread_id": "thread", "turn_id": "turn", "generation": "generation",
            "cancel_requested": False, "inputs": {}, **changes}

    def save(self, record, *, key=None, raw=None):
        key = PREFIX + record["id"] if key is None else key
        with self.store.transaction():
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(record) if raw is None else raw))
        return key

    def rows(self):
        return [tuple(row) for row in self.store.db.execute("SELECT key,value FROM metadata ORDER BY key")]

    def test_mixed_records_and_boundaries_are_read_only(self):
        expired = [self.save(self.record())]
        expired.append(self.save(self.record(2, state="cancelled")))
        expired.append(self.save(self.record(3, state="failed")))
        expired.append(self.save(self.record(4, state="input_required", tool_name="codex_review_request", turn_id=None,
            created_at=self.now - REVIEW_AGE_SECONDS, expires_at=self.now)))
        self.save(self.record(5, terminal_at=self.now - TERMINAL_AGE_SECONDS + 0.001))
        self.save(self.record(6, state="working"))
        self.save(self.record(7, state="input_required", tool_name="codex_review_request", expires_at=self.now + 0.001))
        self.save(self.record(8, state="working", inputs={"old-form": {"expires_at": 1}}))
        before, changes = self.rows(), self.store.db.total_changes
        plan = expired_execution_keys(self.store, self.now)
        self.assertEqual(list(plan), expired)
        self.assertTrue(all(len(digest) == 64 for digest in plan.values()))
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertFalse(self.store.db.in_transaction)

    def test_unknown_malformed_and_foreign_records_are_never_selected_or_deleted(self):
        expired = self.save(self.record())
        malformed = [
            self.record(2, tool_name="future_tool"), self.record(3, future_layout=True),
            self.record(4, terminal_at="yesterday"), self.record(5, terminal_at=float("nan")),
            self.record(6, revision=True), self.record(7, inputs=[]),
            self.record(8, tool_name="codex_review_request", expires_at="yesterday"),
            self.record(9, tool_name="codex_review_request", expires_at=float("inf")),
            self.record(10, tool_name="codex_review_request"), self.record(11, expires_at=1),
            self.record(12, created_at=10 ** 400), self.record(13, inputs={"large": "x" * 65536}),
            self.record(21, operation_id=None), self.record(22, bridge_task_id=[]), self.record(23, thread_id=""),
            self.record(24, turn_id=None), self.record(25, generation=True), self.record(26, cancel_requested="false"),
            self.record(27, inputs={"form": "not a binding"}),
        ]
        for value in malformed:
            self.save(value)
        self.save(self.record(14), raw="{bad json")
        self.save(self.record(15), key=PREFIX + "foreign")
        self.save(self.record(16), key=PREFIX + "f" * 32)  # Mismatched identity.
        self.save(self.record(17), key="other_namespace:" + "0" * 32)
        self.save(self.record(18), key="mcp_execution_future:" + "0" * 32)
        self.save(self.record(19), raw="null")
        self.save(self.record(20), raw='{"id":' + '[' * 1100 + '0' + ']' * 1100 + '}')
        self.assertEqual(list(expired_execution_keys(self.store, self.now)), [expired])
        # Even supplied fingerprints cannot make the apply helper own foreign data.
        forged = {key: hashlib.sha256(raw.encode()).hexdigest() for key, raw in self.rows()}
        with self.store.transaction():
            self.assertEqual(delete_expired_execution_keys(self.store, list(forged), self.now, expected=forged), [expired])
        self.assertEqual(len(self.rows()), len(forged) - 1)

    def test_changed_still_expired_and_changed_live_records_survive_apply(self):
        unchanged = self.save(self.record())
        changed = self.save(self.record(2))
        now_live = self.save(self.record(3))
        removed = self.save(self.record(4))
        plan = expired_execution_keys(self.store, self.now)
        self.save(self.record(2, revision=2))
        self.save(self.record(3, state="working", revision=2))
        with self.store.transaction():
            self.store.db.execute("DELETE FROM metadata WHERE key=?", (removed,))
        added = self.save(self.record(5))
        with self.store.transaction():
            self.assertEqual(delete_expired_execution_keys(self.store, list(plan), self.now, expected=plan), [unchanged])
        self.assertEqual({key for key, _ in self.rows()}, {changed, now_live, added})

    def test_current_expiry_is_rechecked_and_exact_bytes_are_bound(self):
        key = self.save(self.record())
        plan = expired_execution_keys(self.store, self.now)
        with self.store.transaction():
            self.assertEqual(delete_expired_execution_keys(self.store, [key], self.now - 1, expected=plan), [])
        self.save(self.record(), raw=json.dumps(self.record(), separators=(",", ":")))
        with self.store.transaction():
            self.assertEqual(delete_expired_execution_keys(self.store, [key], self.now, expected=plan), [])

    def test_apply_requires_transaction_and_rollback_belongs_to_caller(self):
        key = self.save(self.record())
        plan = expired_execution_keys(self.store, self.now)
        with self.assertRaisesRegex(ValueError, "caller's transaction"):
            delete_expired_execution_keys(self.store, [key], self.now, expected=plan)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction():
                self.assertEqual(delete_expired_execution_keys(self.store, [key, key], self.now, expected=plan), [key])
                self.assertTrue(self.store.db.in_transaction)
                raise RuntimeError("rollback")
        self.assertEqual(list(expired_execution_keys(self.store, self.now)), [key])

    def test_missing_or_wrong_snapshot_preserves_rows(self):
        key = self.save(self.record())
        for expected in ({}, {key: "0" * 64}):
            with self.subTest(expected=expected), self.store.transaction():
                self.assertEqual(delete_expired_execution_keys(self.store, [key], self.now, expected=expected), [])
        self.assertEqual(len(self.rows()), 1)

    def test_plan_is_usable_on_read_only_database_and_survives_serialization(self):
        key = self.save(self.record())
        uri = (self.directory / "state.sqlite3").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            plan = json.loads(json.dumps(expired_execution_keys(SimpleNamespace(db=db), self.now)))
            self.assertFalse(db.in_transaction)
        with self.store.transaction():
            self.assertEqual(delete_expired_execution_keys(self.store, [key], self.now, expected=plan), [key])

    def test_time_must_be_finite(self):
        for invalid in (None, True, "100000", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(now=invalid), self.assertRaisesRegex(ValueError, "must be finite"):
                expired_execution_keys(self.store, invalid)
            with self.store.transaction(), self.assertRaisesRegex(ValueError, "must be finite"):
                delete_expired_execution_keys(self.store, [], invalid, expected={})


if __name__ == "__main__":
    unittest.main()
