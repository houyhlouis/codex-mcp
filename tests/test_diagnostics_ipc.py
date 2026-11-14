"""Real local broker health, using only the fake Codex and throw-away state."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.broker import ensure_broker, read_health
from codex_local_mcp.config import load_config
from codex_local_mcp.diagnostics import diagnose
from tests.helpers import install_fake_codex, stop_test_broker, temp_root, write_config


class DoctorIPCTest(unittest.IsolatedAsyncioTestCase):
    async def test_health_ignores_fingerprint_for_diagnosis_and_never_starts_codex_by_default(self):
        with tempfile.TemporaryDirectory(prefix="dr-ipc-", dir=temp_root()) as directory:
            base = Path(directory).resolve()
            state = base / "state"
            state.mkdir(mode=0o700)
            binary = install_fake_codex(base)
            config_path = base / "config.toml"
            write_config(config_path, state, binary)
            config = load_config(str(config_path))
            with patch.dict(os.environ, {"CODEX_HOME": str(base / "home-a")}):
                try:
                    await asyncio.wait_for(ensure_broker(config), 20)
                    health = await asyncio.wait_for(read_health(replace(config, max_tasks=9)), 5)
                    self.assertEqual(health["fingerprint_inputs"]["max_tasks"], 4)
                    self.assertFalse(health["backend_alive"])
                    with patch.dict(os.environ, {"CODEX_HOME": str(base / "home-b"), "CODEX_PROFILE": "different-profile"}):
                        report = await asyncio.wait_for(diagnose(str(config_path)), 15)
                    self.assertIn("broker_environment_mismatch", {f["code"] for f in report["findings"]})
                    self.assertFalse((await asyncio.wait_for(read_health(config), 5))["backend_alive"])
                    report = await asyncio.wait_for(diagnose(str(config_path), start_broker=True), 45)
                    self.assertIn("signin_ok", {f["code"] for f in report["findings"]})
                    self.assertTrue((await asyncio.wait_for(read_health(config), 5))["backend_alive"])
                    self.assertEqual(report["facts"]["database"]["counts"]["tasks"], 0)
                    self.assertNotIn("different-profile", json.dumps(report))
                finally:
                    await asyncio.wait_for(stop_test_broker(config), 20)
