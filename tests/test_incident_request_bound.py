import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.incidents import export_incident, fixture_bytes, preview, validate_incident, write_export
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store
from tests.helpers import temp_root, write_config
from tests.incident_replay import IncidentReplay


class IncidentRequestBoundTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.task("finished")

    def task(self, key):
        self.store.save("tasks", {"id": key, "thread_id": f"thread-{key}", "status": "completed",
                                  "created_at": 1, "updated_at": 1, "turn_id": None})

    def request(self, key, owner="finished", *, status="answered", kind="parent_question", params=None):
        self.store.save("requests", {"id": key, "task_id": owner, "status": status, "kind": kind,
                                     "params": params or {}, "created_at": 1})

    def test_audit_boundary_and_confirmed_cli_write(self):
        with self.store.transaction():
            for i in range(1000):
                self.request(f"request-{i}")
        complete = export_incident(self.store, ["finished"], limit=1)
        self.assertEqual(len(complete["requests"]), 1000)
        self.assertFalse(complete["partial"])
        self.assertNotIn("omitted_requests", preview(complete))
        self.assertNotIn("omitted_requests", complete)

        self.request("request-1000")
        fixture = export_incident(self.store, ["finished"], limit=1)
        self.assertEqual(len(fixture["requests"]), 1000)
        self.assertTrue(fixture["partial"])
        self.assertEqual(fixture["omitted_requests"], 1)
        replay = asyncio.run(IncidentReplay(fixture).run())
        self.assertEqual([task["id"] for task in replay["tasks"]], [fixture["tasks"][0]["id"]])

        config = self.base / "config.toml"
        write_config(config, self.base / "state", self.base / "missing-codex")
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "server.py"), "--config", str(config),
                   "incident-export", "--task-id", "finished", "--limit", "1"]
        report = json.loads(subprocess.run([*command, "--json"], check=True, capture_output=True, timeout=30).stdout)
        self.assertEqual(report["preview_id"], preview(fixture)["preview_id"])
        self.assertEqual(report["omitted_requests"], 1)
        destination = self.base / "incident.json"
        receipt = subprocess.run([*command, "--output", str(destination), "--confirm-preview", report["preview_id"]],
                                 check=True, capture_output=True, timeout=30)
        self.assertIn(b"1000 requests included (1 omitted)", receipt.stdout)
        self.assertEqual(destination.read_bytes(), fixture_bytes(fixture))
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), report["preview_id"])

        self.request("request-1001")
        newer = export_incident(self.store, ["finished"], limit=1)
        self.assertEqual(newer, {**fixture, "omitted_requests": 2})
        with self.assertRaises(BridgeError) as caught:
            write_export(newer, self.base / "stale.json", report["preview_id"])
        self.assertEqual(caught.exception.code, "incident_preview_mismatch")

    def test_global_selection_prioritizes_pending_window_then_recent(self):
        self.task("other")
        self.task("unselected")
        private_turn = "private-selected-turn"
        private_text = "private request contents"
        with self.store.transaction():
            self.request("old-pending", status="pending", kind="permissions")
            self.request("response-in-window", kind="elicitation")
            self.request("request-in-window", kind="file_approval")
            self.request("same-turn", kind="command_approval", params={"turnId": private_turn, "text": private_text})
            self.request("wrong-owner", "other", kind="user_input", params={"turnId": private_turn})
            self.request("outside-window", kind="user_input", params={"turnId": "old-turn"})
            for i in range(1010):
                self.request(f"history-{i}", "other" if i % 2 else "finished")
            self.request("recent-history", "other", kind="user_input")
            self.request("recent-pending", "other", status="pending", kind="permissions")
            self.request("outside-selection", "unselected", status="pending")
            before = self.store.event("finished", "item/started", {"turnId": "old-turn"})
            self.store.event("finished", "bridge/request", {"id": "request-in-window"})
            self.store.event("finished", "bridge/response_sent", {"request_id": "response-in-window"})
            self.store.event("finished", "turn/started", {"turn": {"id": private_turn}})

        fixture = export_incident(self.store, ["finished", "other", "finished"], after=before, limit=3)
        requests = fixture["requests"]
        self.assertEqual(len(requests), 1000)
        self.assertEqual(fixture["omitted_requests"], 18)
        self.assertTrue(fixture["partial"])
        self.assertEqual(len(fixture["tasks"]), 2)
        pending = [r for r in requests if r["status"] == "pending"]
        self.assertEqual([r["task_id"] for r in pending], [t["id"] for t in fixture["tasks"]])
        self.assertEqual([r["kind"] for r in pending], ["permissions", "permissions"])
        by_id = {r["id"]: r for r in requests}
        start, response, turn = (event["data"] for event in fixture["events"])
        self.assertEqual(by_id[start["id"]]["kind"], "file_approval")
        self.assertEqual(by_id[response["request_id"]]["kind"], "elicitation")
        in_turn = [r for r in requests if r["params"].get("turnId") == turn["turn"]["id"]]
        self.assertEqual([r["kind"] for r in in_turn], ["command_approval"])
        self.assertEqual([r["params"] for r in requests if r["kind"] == "user_input"], [{}])
        encoded = fixture_bytes(fixture)
        for private in (private_turn, private_text, "response-in-window", "outside-selection", "recent-pending"):
            self.assertNotIn(private.encode(), encoded)

        # Force the priority tiers to compete for the last slot.
        with patch("codex_local_mcp.incidents.MAX_REQUESTS", 3):
            tight = export_incident(self.store, ["finished", "other"], after=before, limit=3)
        self.assertCountEqual([r["kind"] for r in tight["requests"]], ["permissions", "permissions", "file_approval"])

    def test_request_sizes_can_exhaust_other_budgets_before_the_count_limit(self):
        with self.store.transaction():
            for i in range(20):
                self.request(f"past-{i}", params={"text": "private"})
            self.request("pending", status="pending")
        full = export_incident(self.store, ["finished"])
        for budget, maximum in (("MAX_NODES", 40), ("MAX_BYTES", len(fixture_bytes(full)) // 3)):
            with self.subTest(budget=budget), patch(f"codex_local_mcp.incidents.{budget}", maximum):
                fixture = export_incident(self.store, ["finished"])
                self.assertTrue(fixture["partial"])
                self.assertEqual(fixture["omitted_requests"], 21 - len(fixture["requests"]))
                self.assertGreater(fixture["omitted_requests"], 0)
                self.assertIn("pending", [r["status"] for r in fixture["requests"]])
                validate_incident(fixture)
                with self.assertRaises(BridgeError) as caught:
                    validate_incident(full)
                self.assertEqual(caught.exception.code, "result_too_large")

    def test_optional_count_is_strict_and_requires_partial(self):
        fixture = export_incident(self.store, ["finished"])
        for fields in ({"partial": False, "omitted_requests": 1}, {"partial": True, "unexpected": 1},
                       *({"partial": True, "omitted_requests": v} for v in (0, -1, True, "private", 1.5, 2**63))):
            with self.subTest(fields=fields), self.assertRaises(BridgeError) as caught:
                validate_incident({**fixture, **fields})
            self.assertEqual(caught.exception.code, "invalid_arguments")

    def test_unrepresentable_stored_identity_gives_export_diagnostics(self):
        self.store.update("tasks", "finished", {f"unknown-{i}": True for i in range(257)})
        with self.assertRaises(BridgeError) as caught:
            export_incident(self.store, ["finished"], limit=1)
        self.assertEqual(caught.exception.code, "state_invalid")
        self.assertIn("server.py doctor", str(caught.exception))
        self.assertIn("server.py doctor", caught.exception.next_steps[0])

    def test_event_alone_over_budget_gives_a_working_cursor_option(self):
        data = {}
        for _ in range(25):
            data = {"text": data}
        seq = self.store.event("finished", "bridge/input", data)
        with self.assertRaises(BridgeError) as caught:
            export_incident(self.store, ["finished"], limit=1)
        self.assertEqual(caught.exception.code, "result_too_large")
        self.assertIn("--after", caught.exception.next_steps[0])
        self.assertEqual(export_incident(self.store, ["finished"], after=seq, limit=1)["events"], [])
