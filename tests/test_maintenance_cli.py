"""Operator maintenance is explicit, offline for pruning and never an MCP tool."""

import io
import json
import os
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.cli import main
from codex_local_mcp.store import Store
from codex_local_mcp.tools import BY_NAME
from tests.helpers import temp_root


class MaintenanceCLITest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.temp = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.config = self.base / "operator.toml"
        # These commands must work without an installed Codex executable.
        self.config.write_text('state_dir = ' + json.dumps(str(self.state)) + '\ncodex_binary = ' + json.dumps(str(self.base / "missing-codex")) + '\n', encoding="utf-8")
        self.config.chmod(0o600)
        with closing(Store(self.state)) as store, store.transaction():
            store.event(None, "old", {"text": "retained"})
            store.db.execute("UPDATE events SET at=1")

    def command(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["codex-local-mcp", "--config", str(self.config), *args]), redirect_stdout(output), redirect_stderr(error):
            try:
                main()
            except SystemExit as exc:
                return exc.code, output.getvalue(), error.getvalue()
        return 0, output.getvalue(), error.getvalue()

    def test_prune_requires_reviewed_plan_and_has_human_and_json_output(self):
        self.assertNotIn("codex_prune", BY_NAME)
        self.assertNotIn("codex_backup", BY_NAME)
        code, output, error = self.command("prune", "--older-than", "30d", "--json")
        self.assertEqual((code, error), (0, ""))
        plan = json.loads(output)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["counts"]["events"], 1)
        self.assertIn("native_image_history", plan["protected"])
        with closing(Store(self.state)) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        code, output, error = self.command("prune", "--apply", "--plan-id", plan["plan_id"], "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(json.loads(output)["applied"])
        code, output, error = self.command("prune", "--older-than", "12h")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("Protected native_image_history:", output)
        self.assertIn("--apply --plan-id", output)
        code, output, error = self.command("backup", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["integrity_check"], "ok")

    def test_malformed_or_changed_policies_fail_without_mutating(self):
        for args, message in (
                (("prune",), "Dry-run prune requires --older-than"),
                (("prune", "--older-than", "0"), "Use a positive duration"),
                (("prune", "--apply"), "prune --apply requires --plan-id"),
                (("prune", "--apply", "--plan-id", "unknown", "--older-than", "1d"), "does not accept policy changes"),
                (("prune", "--older-than", "1d", "--compact"), "--compact require --apply"),
                (("prune", "--older-than", "1d", "--max-bytes", "-1"), "nonnegative byte target")):
            with self.subTest(args=args):
                code, output, error = self.command(*args)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn(message, error)
        with closing(Store(self.state)) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
