import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_local_mcp import __version__
from codex_local_mcp.config import environment_identity, load_config
from codex_local_mcp.broker import HealthUnavailable, health_snapshot, read_health, read_status, ping_existing
from codex_local_mcp.security import BridgeError
from codex_local_mcp.diagnostics import REPORT_SCHEMA, STEPS, diagnose, render, schema_probe, validate_report
from codex_local_mcp.contract import NATIVE_METHODS, NATIVE_NOTIFICATIONS, NATIVE_SERVER_REQUESTS
from codex_local_mcp.platforms import IS_WINDOWS
from codex_local_mcp.store import Store, SCHEMA_VERSION
from tests.helpers import install_fake_codex, temp_root, write_config


class DoctorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doctor-", dir=temp_root())
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.binary = install_fake_codex(self.base)
        self.path = self.base / "config.toml"
        write_config(self.path, self.state, self.binary)
        self.config = load_config(str(self.path))
        self.ipc = patch("codex_local_mcp.diagnostics.ipc_probe", AsyncMock())
        self.health = patch("codex_local_mcp.diagnostics.read_health", AsyncMock(side_effect=FileNotFoundError()))
        self.start = patch("codex_local_mcp.diagnostics.ensure_broker", AsyncMock())
        self.status = patch("codex_local_mcp.diagnostics.read_status", AsyncMock())
        self.ipc_mock, self.health_mock, self.start_mock, self.status_mock = [item.start() for item in (self.ipc, self.health, self.start, self.status)]

    async def asyncTearDown(self):
        for item in (self.status, self.start, self.health, self.ipc):
            item.stop()
        self.temp.cleanup()

    async def report(self, **kwargs):
        report = await asyncio.wait_for(diagnose(str(self.path), **kwargs), 15)
        validate_report(report)
        self.assertEqual(report["exit_code"], max({"ok": 0, "info": 0, "warning": 1, "error": 2}[f["severity"]] for f in report["findings"]))
        self.assertTrue(all(f["code"] in STEPS and f["next_step"] for f in report["findings"]))
        return report

    @staticmethod
    def codes(report):
        return {item["code"] for item in report["findings"]}

    def healthy(self, *, backend=False):
        return {"pid": 123, "bridge_version": __version__, "schema_version": SCHEMA_VERSION,
                "fingerprint": self.config.fingerprint, "fingerprint_inputs": self.config.fingerprint_inputs,
                "environment_identity": environment_identity(), "backend_alive": backend, "backend_generation": 1}

    async def test_default_never_starts_broker_or_codex_and_does_not_mutate_database(self):
        store = Store(self.state)
        store.close()
        path = self.state / "state.sqlite3"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        files = {p.name for p in self.state.iterdir()}
        self.health_mock.side_effect = None
        self.health_mock.return_value = self.healthy()
        report = await self.report()
        self.assertIn("codex_tested", self.codes(report))
        self.assertIn("database_ok", self.codes(report))
        self.assertIn("signin_unchecked", self.codes(report))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual({p.name for p in self.state.iterdir()}, files)
        self.start_mock.assert_not_awaited()
        self.status_mock.assert_not_awaited()

    async def test_missing_state_is_created_only_by_explicit_start_and_report_is_refreshed(self):
        state = self.base / "new-state"
        write_config(self.path, state, self.binary)
        report = await self.report()
        self.assertIn("state_missing", self.codes(report))
        self.assertFalse(state.exists())
        self.start_mock.assert_not_awaited()
        async def start(config):
            config.state_dir.mkdir(mode=0o700)
            store = Store(config.state_dir)
            store.close()
        self.start_mock.side_effect = start
        report = await self.report(start_broker=True)
        self.assertTrue(report["facts"]["state"]["exists"])
        self.assertTrue(report["facts"]["state"]["created_by_explicit_start"])
        self.assertNotIn("state_missing", self.codes(report))
        self.assertNotIn("database_absent", self.codes(report))
        self.assertNotIn("broker_unchecked", self.codes(report))
        self.assertIn("database_ok", self.codes(report))

    async def test_missing_binary_is_a_finding_not_a_traceback(self):
        write_config(self.path, self.state, self.base / "missing")
        report = await self.report()
        self.assertIn("binary_missing", self.codes(report))
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("full path", next(f["next_step"] for f in report["findings"] if f["code"] == "binary_missing"))

    async def test_unsupported_untested_and_uncheckable_versions_are_distinct(self):
        launcher = self.base / "version.py"
        for version, code in (("codex-cli 0.166.0", "codex_unsupported"),
                              ("codex-cli 0.168.0-alpha.4", "codex_untested"),
                              ("SECRET_GARBAGE_VERSION", "codex_version_unchecked")):
            with self.subTest(version=version):
                launcher.write_text(f"print({version!r})\n")
                write_config(self.path, self.state, launcher)
                report = await self.report()
                self.assertIn(code, self.codes(report))
                self.assertEqual(report["facts"]["codex_compatibility"]["status"],
                    {"codex_unsupported": "unsupported", "codex_untested": "untested", "codex_version_unchecked": "could_not_check"}[code])
                if code == "codex_version_unchecked":
                    self.assertNotIn(version, json.dumps(report))

    @unittest.skipIf(IS_WINDOWS, "POSIX execute permissions")
    async def test_nonexecutable_and_unsafe_path_do_not_execute_a_planted_file(self):
        planted = self.base / "codex"
        planted.write_text("must never execute")
        planted.chmod(0o600)
        self.path.write_text(f'state_dir={json.dumps(str(self.state))}\ncodex_binary="codex"\n')
        with patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", "relative", str(self.base)])}):
            report = await self.report()
        self.assertIn("binary_not_executable", self.codes(report))
        self.assertIn("path_ignored", self.codes(report))
        self.assertTrue(report["facts"]["executable"]["ignored_path_entries"])
        self.assertEqual(planted.read_text(), "must never execute")

    async def test_relative_binary_is_rejected_with_a_safe_next_step(self):
        self.path.write_text(f'state_dir={json.dumps(str(self.state))}\ncodex_binary="./bin/codex.py"\n')
        report = await self.report()
        self.assertIn("binary_relative", self.codes(report))
        self.assertEqual(report["facts"]["executable"]["configured"], "./bin/codex.py")
        self.assertIn("'./bin/codex.py'", render(report))

    async def test_socket_creation_denied_is_reported_as_environment_unavailable(self):
        self.ipc_mock.side_effect = PermissionError(1, "Operation not permitted")
        report = await self.report()
        self.assertIn("ipc_unavailable", self.codes(report))
        self.assertEqual(report["facts"]["ipc_error"]["errno"], 1)
        self.assertIn(f"Local {'pipes' if IS_WINDOWS else 'sockets'} are unavailable here: {os.strerror(1)}.", render(report))

    @unittest.skipIf(IS_WINDOWS, "Unix socket path length")
    async def test_long_endpoint_is_reported_even_when_config_cannot_load(self):
        self.state = self.base / ("x" * 90)
        self.state.mkdir(mode=0o700)
        write_config(self.path, self.state, self.binary)
        report = await self.report()
        self.assertIn("endpoint_too_long", self.codes(report))
        self.assertFalse(report["facts"]["endpoint"]["fits_platform_limit"])
        self.ipc_mock.assert_not_awaited()

    @unittest.skipIf(IS_WINDOWS, "POSIX uid and mode")
    async def test_wrong_mode_and_owner_are_reported_without_repair(self):
        self.state.chmod(0o755)
        report = await self.report()
        self.assertIn("state_unsafe", self.codes(report))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)
        self.assertNotEqual(self.ipc_mock.call_args.args[0], self.state)
        self.state.chmod(0o700)
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1) if path == self.state else info
        with patch.object(Path, "lstat", lstat):
            report = await self.report()
        self.assertIn("state_unsafe", self.codes(report))

    async def test_corrupt_and_newer_databases_are_not_changed(self):
        database = self.state / "state.sqlite3"
        database.write_bytes(b"not a database")
        database.chmod(0o600)
        report = await self.report()
        self.assertIn("database_corrupt", self.codes(report))
        self.assertEqual(database.read_bytes(), b"not a database")
        database.unlink()
        db = sqlite3.connect(database)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        db.close()
        database.chmod(0o600)
        before = database.read_bytes()
        report = await self.report()
        self.assertIn("database_newer", self.codes(report))
        self.assertEqual(database.read_bytes(), before)

    async def test_database_counts_and_backups_are_metadata_only(self):
        store = Store(self.state)
        store.close()
        (self.state / "state.sqlite3.pre-v4-123.bak").write_bytes(b"backup")
        report = await self.report()
        self.assertEqual(report["facts"]["database"]["counts"], {"tasks": 0, "requests": 0, "events": 0, "open_claims": 0})
        self.assertEqual(report["facts"]["database"]["backups"][0]["name"], "state.sqlite3.pre-v4-123.bak")

    async def test_wal_without_shared_memory_is_not_opened_or_changed(self):
        store = Store(self.state)
        store.close()
        wal = self.state / "state.sqlite3-wal"
        wal.write_bytes(b"not opened by doctor")
        before = {path.name: path.read_bytes() for path in self.state.iterdir()}
        report = await self.report()
        self.assertIn("database_unchecked", self.codes(report))
        self.assertEqual({path.name: path.read_bytes() for path in self.state.iterdir()}, before)

    async def test_broker_identity_and_environment_mismatch_name_only_safe_inputs(self):
        health = self.healthy()
        health["fingerprint_inputs"]["max_tasks"] = 9
        health["environment_identity"]["codex_home"] = str(self.base / "other-home")
        health["environment_identity"]["profile_selection_digest"] = "different"
        self.health_mock.side_effect = None
        self.health_mock.return_value = health
        report = await self.report()
        self.assertIn("broker_identity_mismatch", self.codes(report))
        self.assertIn("broker_environment_mismatch", self.codes(report))
        self.assertEqual(report["facts"]["broker"]["different_fingerprint_inputs"], ["max_tasks"])
        self.status_mock.assert_not_awaited()

    async def test_old_broker_identity_refusal_is_reported_without_echoing_its_error(self):
        self.health_mock.side_effect = HealthUnavailable("broker_identity_mismatch")
        report = await self.report()
        self.assertIn("broker_identity_mismatch", self.codes(report))
        self.assertTrue(report["facts"]["broker"]["reachable"])

    async def test_coded_identity_errors_never_echo_broker_messages(self):
        for code in ("broker_identity_mismatch", "broker_identity_unchecked"):
            with self.subTest(code=code):
                self.health_mock.side_effect = BridgeError("remote-message-must-not-appear", code=code)
                report = await self.report()
                self.assertIn(code, self.codes(report))
                self.assertEqual(report["facts"]["broker_error"], {"code": code})
                self.assertNotIn("remote-message-must-not-appear", json.dumps(report))

    @unittest.skipIf(IS_WINDOWS, "Unix stale socket/lock metadata")
    async def test_stale_socket_and_free_lock_are_reported_without_deletion(self):
        self.config.socket.touch(mode=0o600)
        lock = self.state / "broker.lock"
        lock.touch(mode=0o600)
        self.health_mock.side_effect = ConnectionRefusedError(111, "connection refused")
        with patch("codex_local_mcp.diagnostics.stat.S_ISSOCK", return_value=True):
            report = await self.report()
        self.assertIn("broker_stale_socket", self.codes(report))
        self.assertIn("broker_stale_lock", self.codes(report))
        self.assertTrue(lock.exists())
        self.assertTrue(self.config.socket.exists())

    async def test_existing_backend_no_signin_and_explicit_start(self):
        self.health_mock.side_effect = None
        self.health_mock.return_value = self.healthy(backend=True)
        self.status_mock.return_value = {"signed_in": False, "account": {"token": "NEVER_PRINT_ACCOUNT_TOKEN"}}
        report = await self.report()
        self.assertIn("signin_missing", self.codes(report))
        self.assertNotIn("NEVER_PRINT_ACCOUNT_TOKEN", json.dumps(report))
        self.start_mock.assert_not_awaited()
        self.health_mock.return_value = self.healthy(backend=False)
        report = await self.report(start_broker=True)
        self.start_mock.assert_awaited_once()
        self.assertIn("signin_missing", self.codes(report))

    async def test_toolchain_missing_and_no_environment_secrets_in_either_rendering(self):
        with self.path.open("a") as stream:
            stream.write(f'toolchain_paths=[{json.dumps(str(self.base / "missing-toolchain"))}]\n')
        secret = "SEED_SECRET_NEVER_VISIBLE_771001"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret, "GITHUB_TOKEN": secret, "CODEX_PROFILE": secret,
                                     "CODEX_HOME": str(self.base / "codex-home")}):
            report = await self.report()
        self.assertIn("toolchain_unreadable", self.codes(report))
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, render(report))
        self.assertNotIn("OPENAI_API_KEY", json.dumps(report))
        self.assertEqual(report["facts"]["environment_identity"]["codex_home"], os.path.normcase(str(self.base / "codex-home")))

    async def test_json_report_validates_and_cli_never_needs_real_stdin(self):
        report = await self.report()
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        if jsonschema:
            jsonschema.Draft7Validator(REPORT_SCHEMA).validate(report)
        process = await asyncio.create_subprocess_exec(sys.executable, "server.py", "--config", str(self.path), "doctor", "--json",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), 15)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        actual = json.loads(out)
        self.assertEqual(actual["exit_code"], process.returncode)
        self.assertNotIn(b"Traceback", err)
        if jsonschema:
            jsonschema.Draft7Validator(REPORT_SCHEMA).validate(actual)

    async def test_schema_inventory_without_source_validator_is_explicitly_partial(self):
        probes = []
        async def create(*args, **kwargs):
            probes.append(Path(kwargs["cwd"]))
            self.assertEqual(kwargs["stdin"], asyncio.subprocess.DEVNULL)
            self.assertTrue(Path(kwargs["env"]["CODEX_HOME"]).is_relative_to(probes[-1]))
            self.assertTrue(Path(kwargs["env"]["TMPDIR"]).is_relative_to(probes[-1]))
            self.assertNotIn("CODEX_PROFILE", kwargs["env"])
            directory = Path(args[args.index("--out") + 1])
            for name, methods in (("ClientRequest", NATIVE_METHODS), ("ServerRequest", NATIVE_SERVER_REQUESTS),
                                  ("ServerNotification", NATIVE_NOTIFICATIONS)):
                (directory / (name + ".json")).write_text(json.dumps({"oneOf": [
                    {"properties": {"method": {"enum": [method]}}} for method in methods]}))
            return SimpleNamespace(wait=AsyncMock(return_value=0))
        facts, findings = {}, []
        with patch("codex_local_mcp.diagnostics.asyncio.create_subprocess_exec", side_effect=create), \
                patch("codex_local_mcp.diagnostics.importlib.util.find_spec", return_value=None):
            await asyncio.wait_for(schema_probe(str(self.binary), lambda severity, code, message: findings.append(code), facts), 5)
        self.assertIn("schema_inventory_ok", findings)
        self.assertIn("schema_wire_unchecked", findings)
        self.assertFalse(facts["schema"]["wire_checked"])
        self.assertTrue(probes and not probes[0].exists())

    async def test_version_probe_real_home_is_untouched_and_temporary_directory_is_removed(self):
        actual_home = self.base / "actual-home"
        actual_home.mkdir()
        (actual_home / "existing").write_text("canary")
        launcher = self.base / "writing-version.py"
        real_temporary = tempfile.TemporaryDirectory
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                created = []
                def temporary(*args, **kwargs):
                    manager = real_temporary(*args, **kwargs)
                    created.append(Path(manager.name))
                    return manager
                launcher.write_text("import os,sys\nfrom pathlib import Path\n"
                    "Path(os.environ['CODEX_HOME'],'written-by-version').write_text('probe')\n"
                    "print('codex-cli 0.167.0')\n" + f"sys.exit({exit_code})\n")
                write_config(self.path, self.state, launcher)
                with patch.dict(os.environ, {"CODEX_HOME": str(actual_home)}), \
                        patch("codex_local_mcp.diagnostics.tempfile.TemporaryDirectory", side_effect=temporary):
                    report = await self.report()
                self.assertEqual(len(created), 1)
                self.assertFalse(created[0].exists())
                self.assertTrue(report["facts"]["temporary_probes"]["removed"])
                self.assertEqual({path.name for path in actual_home.iterdir()}, {"existing"})
                self.assertIn("codex_tested" if exit_code == 0 else "codex_version_unchecked", self.codes(report))

    async def test_version_probe_timeout_cleans_private_directory_and_temp_failure_is_a_finding(self):
        created, killed = [], []
        async def create(*args, **kwargs):
            root = Path(kwargs["cwd"])
            created.append(root)
            Path(kwargs["env"]["CODEX_HOME"], "written-by-probe").write_text("probe")
            gate = asyncio.Event()
            process = SimpleNamespace(returncode=None)
            async def communicate():
                await gate.wait()
            def kill():
                killed.append(True)
                process.returncode = -9
            process.communicate, process.kill, process.wait = communicate, kill, AsyncMock(return_value=-9)
            return process
        with patch("codex_local_mcp.diagnostics.asyncio.create_subprocess_exec", side_effect=create), \
                patch("codex_local_mcp.diagnostics.VERSION_TIMEOUT", 0.01):
            report = await self.report()
        self.assertIn("codex_version_unchecked", self.codes(report))
        self.assertEqual(killed, [True])
        self.assertTrue(created and not created[0].exists())
        with patch("codex_local_mcp.diagnostics.tempfile.TemporaryDirectory", side_effect=PermissionError(13, "denied")):
            report = await self.report()
        self.assertIn("codex_version_unchecked", self.codes(report))
        self.assertEqual(report["facts"]["version_probe_error"]["errno"], 13)


class HealthHelperTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_status_path_does_not_repair_or_create_state(self):
        config = SimpleNamespace(fingerprint="identity", endpoint="private-pipe")
        with patch("codex_local_mcp.broker.IS_WINDOWS", True), \
                patch("codex_local_mcp.broker.private_dir") as private, \
                patch("codex_local_mcp.windows.exchange", AsyncMock(return_value=b'{"result":{"signed_in":false}}')) as exchange:
            self.assertEqual(await asyncio.wait_for(read_status(config), 2), {"signed_in": False})
            private.assert_not_called()
            self.assertEqual(json.loads(exchange.call_args.args[1]),
                {"config": "identity", "method": "tool", "name": "codex_status", "args": {}})

    async def test_health_client_tolerates_legacy_and_structured_errors(self):
        config = SimpleNamespace(fingerprint="different", endpoint="private-pipe")
        mismatch = BridgeError("remote-message-must-not-appear", code="broker_identity_mismatch")
        other = BridgeError("Configuration differs from the running broker", code="state_invalid")
        cases = (
            ({"error": "Configuration differs from the running broker"}, "broker_identity_mismatch"),
            ({"error": {"code": "config_mismatch", "message": "arbitrary remote message"}}, "broker_identity_mismatch"),
            ({"error": {"code": "method_not_found", "message": "Unknown broker operation"}}, "broker_identity_unchecked"),
            ({"error": str(mismatch), "error_data": mismatch.as_dict()}, "broker_identity_mismatch"),
            ({"error": str(other), "error_data": other.as_dict()}, "broker_identity_unchecked"),
        )
        for response, expected in cases:
            with patch("codex_local_mcp.broker.IS_WINDOWS", True), \
                    patch("codex_local_mcp.windows.exchange", AsyncMock(return_value=json.dumps(response).encode())) as exchange:
                with self.assertRaises(HealthUnavailable) as caught:
                    await read_health(config)
                self.assertEqual(json.loads(exchange.call_args.args[1])["method"], "health")
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("remote-message-must-not-appear", str(caught.exception))

    async def test_ping_refusals_use_codes_and_fixed_local_guidance(self):
        for code, message in (("broker_identity_mismatch", "remote-message-must-not-appear"),
                              ("state_invalid", "Configuration differs from the running broker")):
            error = BridgeError(message, code=code)
            with patch("codex_local_mcp.broker.rpc", AsyncMock(side_effect=error)):
                with self.assertRaises(BridgeError) as caught:
                    await ping_existing(SimpleNamespace())
                self.assertEqual(caught.exception.code, code)
                if code == "broker_identity_mismatch":
                    self.assertNotIn(message, str(caught.exception))
                else:
                    self.assertIs(caught.exception, error)

    async def test_snapshot_contains_only_nonsecret_metadata_and_does_not_start_backend(self):
        config = SimpleNamespace(fingerprint="identity", fingerprint_inputs={"max_tasks": 4})
        backend = SimpleNamespace(alive=False, generation=0, version=None)
        service = SimpleNamespace(backend=backend, store=SimpleNamespace(schema_version=SCHEMA_VERSION))
        report = health_snapshot(config, service)
        self.assertEqual(set(report), {"pid", "bridge_version", "schema_version", "fingerprint", "fingerprint_inputs",
                                       "environment_identity", "backend_alive", "backend_generation", "compatibility"})
        self.assertFalse(report["backend_alive"])
        self.assertIsNone(report["compatibility"])
