"""Broker admission, diagnostics and native startup under different Codex homes."""
import asyncio
import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from codex_local_mcp import broker, config as settings, windows
from codex_local_mcp.backend import AppServer
from codex_local_mcp.diagnostics import diagnose
from codex_local_mcp.security import BridgeError, project_path
from tests.helpers import install_fake_codex, link_or_skip, temp_root, write_config


class BrokerHomeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="home-", dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "Home-A"
        self.home.mkdir()
        self.other = self.base / "Home-B"
        self.other.mkdir()
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.path = self.base / "settings.toml"
        write_config(self.path, self.state, install_fake_codex(self.base))

    def config_for(self, home, **env):
        with patch.dict(os.environ, {"CODEX_HOME": str(home), **env}):
            return settings.load_config(self.path)

    def test_home_is_fixed_at_config_load_and_profile_and_secrets_are_excluded(self):
        with chdir(self.base):
            first = self.config_for(self.home.name, CODEX_PROFILE="first", OPENAI_API_KEY="fixture-one")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.other)}):
            self.assertEqual(first.fingerprint_inputs["codex_home"], os.path.normcase(str(self.home)))
            self.assertNotEqual(first.fingerprint, settings.load_config(self.path).fingerprint)
            with self.assertRaisesRegex(BridgeError, "Codex credentials"):
                project_path(first, self.home)
        same = self.config_for(str(self.home) + os.sep, CODEX_PROFILE="second", OPENAI_API_KEY="fixture-two")
        self.assertEqual(first.fingerprint, same.fingerprint)

    def test_default_empty_and_tilde_home_have_one_identity(self):
        with patch.dict(os.environ, {"HOME": str(self.base), "USERPROFILE": str(self.base)}):
            os.environ.pop("CODEX_HOME", None)
            default = settings.load_config(self.path)
            self.assertNotIn("CODEX_HOME", default.subprocess_environment())
            for spelling in ("", Path("~") / ".codex", self.base / ".codex"):
                with self.subTest(spelling=spelling):
                    self.assertEqual(default.fingerprint, self.config_for(spelling).fingerprint)

    def test_symlink_has_the_target_identity(self):
        link = self.base / "home-link"
        link_or_skip(self, link, self.home)
        self.assertEqual(self.config_for(link).fingerprint, self.config_for(self.home).fingerprint)

    @unittest.skipUnless(os.name == "nt", "Windows case normalization")
    def test_windows_home_case_has_one_identity(self):
        self.assertEqual(self.config_for(str(self.home).swapcase()).fingerprint, self.config_for(self.home).fingerprint)

    async def test_pipe_admission_uses_the_home_before_dispatch_and_leaves_health_and_stop_available(self):
        first = self.config_for(self.home)
        with chdir(self.base):
            equivalent = self.config_for(self.home.name + os.sep)
        other = self.config_for(self.other)
        ready = asyncio.get_running_loop().create_future()

        def pipe(address, handle_payload, limit):
            ready.set_result(handle_payload)
            return Mock(wait_closed=AsyncMock())

        async def exchange(address, payload, limit, timeout):
            self.assertEqual(address, first.endpoint)
            return await ready.result()(payload)

        dispatch = AsyncMock(return_value={"reached": True})
        self.addCleanup(os.umask, os.umask(0o077))
        sid = windows.current_sid() if os.name == "nt" else "fixture-user"
        with (patch.object(broker, "IS_WINDOWS", True), patch.object(settings, "IS_WINDOWS", True),
              patch.object(windows, "current_sid", return_value=sid),
              patch.object(windows, "PipeServer", pipe), patch.object(windows, "exchange", exchange),
              patch.object(broker.signal, "signal"), patch.object(broker, "dispatch", dispatch),
              patch.dict(os.environ, {"CODEX_HOME": str(self.other)})):
            running = asyncio.create_task(broker.run_broker(first))
            try:
                await asyncio.wait_for(ready, 30)
                self.assertEqual((await broker.ensure_broker(equivalent))["config"], first.fingerprint)
                task = {"task_id": "fixture-task"}
                for call, ids in ((lambda: broker.ensure_broker(other), {}),
                                  (lambda: broker.rpc(other, "tool", name="codex_get", args=task), task)):
                    with self.assertRaises(BridgeError) as caught:
                        await call()
                    error = caught.exception
                    self.assertEqual(error.code, "broker_identity_mismatch")
                    self.assertEqual(error.ids, ids)
                    self.assertFalse(error.retryable or error.uncertain)
                    self.assertNotIn("codex_home_digest", error.as_dict())
                    self.assertEqual(
                        str(error), "The running broker uses a different Codex home (CODEX_HOME). "
                        "Stop it with `python server.py stop` (add the same --config option if you use one), then retry. "
                        "Use separate state directories (state_dir) to run different Codex homes at the same time.")
                    for path in (first.codex_home, other.codex_home):
                        self.assertNotIn(path, str(error))
                dispatch.assert_not_awaited()
                health = await broker.read_health(other)
                self.assertEqual(health["environment_identity"]["codex_home"], first.codex_home)
                self.assertEqual(health["fingerprint_inputs"]["codex_home"], first.codex_home)
                self.assertFalse(health["backend_alive"])
                with patch("codex_local_mcp.diagnostics.ipc_probe", AsyncMock()):
                    report = await diagnose(self.path)
                self.assertEqual(report["facts"]["broker"]["different_fingerprint_inputs"], ["codex_home"])
                self.assertEqual(report["facts"]["environment_identity"]["codex_home"], other.codex_home)
                self.assertEqual(report["facts"]["broker"]["codex_home"], first.codex_home)
                mismatch = next(f for f in report["findings"] if f["code"] == "broker_identity_mismatch")
                self.assertIn("The differences are CODEX_HOME.", mismatch["message"])
                self.assertEqual(await broker.rpc(equivalent, "tool", name="codex_status"), {"reached": True})
                dispatch.assert_awaited_once()
                self.assertTrue((await broker.rpc(other, "stop"))["stopping"])
                await asyncio.wait_for(running, 30)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    async def test_detached_broker_and_native_process_receive_the_captured_absolute_home(self):
        with chdir(self.base):
            config = self.config_for(self.home.name)
        with patch.dict(os.environ, {"CODEX_HOME": str(self.other), "CODEX_PROFILE": "fixture-profile"}):
            with (patch.object(broker, "rpc", AsyncMock(side_effect=[
                    FileNotFoundError(), FileNotFoundError(), {"config": config.fingerprint}])),
                  patch.object(broker.subprocess, "Popen") as spawn):
                await broker.ensure_broker(config)
                self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_HOME"], os.path.normcase(str(self.home)))

            backend = AppServer(config, AsyncMock(), AsyncMock())
            with patch.object(asyncio, "create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as launch:
                try:
                    await backend.start()
                    version_probe = next(call for call in launch.call_args_list if "--version" in call.args)
                    app_server = next(call for call in launch.call_args_list if "app-server" in call.args)
                    for call in (version_probe, app_server):
                        self.assertEqual(call.kwargs["env"]["CODEX_HOME"], os.path.normcase(str(self.home)))
                    self.assertNotIn("--profile", app_server.args)
                    self.assertFalse(any("fixture-profile" in arg for arg in app_server.args))
                finally:
                    await backend.close()
