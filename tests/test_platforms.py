import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.config import Config, load_config
from codex_local_mcp.platforms import (IS_WINDOWS, canonical_system_path, executable_argv,
    lock_file, resolve_codex, stdio_lines, unix_peer_uid)
from codex_local_mcp.security import BridgeError, private_dir, private_file, validate_windows_path
from tests.helpers import install_fake_codex, write_config, temp_root
from tests.test_service import ServiceHarness


class ProjectSelectionTest(ServiceHarness):
    async def test_start_with_project_id_and_unicode_directory(self):
        path = self.base / "agent created 空间" / "project"
        selected = self.service.projects(action="create", path=str(path))
        task = await self.service.start(prompt="echo", request_id="unicode", project_id=selected["project_id"])
        self.assertEqual(task["project"], str(path))
        self.assertEqual((await self.until(task["id"]))["status"], "completed")

    async def test_directory_selection_is_explicit(self):
        with self.assertRaises(BridgeError):
            await self.service.start(prompt="echo", request_id="missing")
        path = self.base / "selected"
        descriptor = self.service.projects("create", str(path))
        with self.assertRaises(BridgeError):
            self.service.select_project(str(path), descriptor["project_id"])
        with self.assertRaises(BridgeError):
            self.service.select_project(workdir="relative/path")

    async def test_private_state_config_and_home_cannot_be_projects(self):
        for path in (self.state, self.base, Path.home(), self.state / "nested"):
            with self.assertRaises(BridgeError):
                self.service.projects("create", str(path))

    async def test_configuration_has_no_workspace_requirements(self):
        config = load_config(str(self.config_file))
        self.assertFalse(hasattr(config, "roots"))
        with patch("codex_local_mcp.config.resolve_codex", return_value=config.codex):
            default = load_config()
        self.assertIsNone(default.path)
        self.assertTrue(default.state_dir.is_absolute())

    async def test_legacy_workspace_config_has_actionable_migration_error(self):
        self.config_file.write_text(self.config_file.read_text() + '\n[workspace_roots]\nold = "/unused"\n', encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "workdir or project_id"):
            load_config(str(self.config_file))

    async def test_old_tasks_migrate_without_a_root_map(self):
        selected = self.service.projects("create", str(self.root / "legacy"))
        with self.store.db:
            self.store.save("tasks", {"id": "legacy", "thread_id": None, "root": "old", "path": "legacy",
                "project": selected["path"], "status": "failed"})
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('workspace_scope',?)", ('{"old":"/unused"}',))
        from codex_local_mcp.service import Service
        recovered = Service(self.cfg, self.store)
        self.assertEqual(recovered.task("legacy")["project_id"], selected["project_id"])
        self.assertNotIn("root", recovered.task("legacy"))
        await recovered.close()


class PlatformTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_handles_batched_utf8_and_bounds(self):
        source = io.BytesIO('first\n第二条\nthird\n'.encode())
        self.assertEqual([line async for line in stdio_lines(source)], [b"first\n", '第二条\n'.encode(), b"third\n"])
        with self.assertRaises(ValueError):
            [line async for line in stdio_lines(io.BytesIO(b"x" * 20), limit=10)]

    async def test_windows_paths_reject_remote_devices_and_alternate_streams(self):
        for value in [r"C:\Work\project", r"D:\工作\project with spaces"]:
            validate_windows_path(value)
        for value in [r"C:relative", r"\\host\share\project", r"\\?\C:\project", r"\\.\pipe\anything",
                      r"C:\work\..\outside", r"C:\work\file:secret", r"C:\work\CON", "C:\\work\\trailing. "]:
            with self.assertRaises(BridgeError, msg=value):
                validate_windows_path(value)

    async def test_python_fixture_is_launched_without_shell(self):
        self.assertEqual(executable_argv("fake.py"), [sys.executable, "fake.py"])
        self.assertEqual(executable_argv("codex.exe"), ["codex.exe"])

    async def test_npm_windows_shim_resolves_to_native_executable(self):
        with tempfile.TemporaryDirectory(dir=temp_root()) as directory:
            root = Path(directory)
            shim = root / "codex.cmd"
            shim.write_text("not executed")
            native = root / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"fixture")
            with patch("codex_local_mcp.platforms.IS_WINDOWS", True):
                self.assertEqual(resolve_codex(str(shim)), str(native.resolve()))
                native.unlink()
                with self.assertRaisesRegex(ValueError, "native codex.exe"):
                    resolve_codex(str(shim))

    async def test_windows_endpoint_is_private_and_account_specific(self):
        with patch("codex_local_mcp.config.IS_WINDOWS", True):
            config = Config(None, Path(tempfile.gettempdir()) / "state", "codex.exe")
            with patch("codex_local_mcp.windows.current_sid", return_value="S-1-5-21-1000"):
                first = config.endpoint
            with patch("codex_local_mcp.windows.current_sid", return_value="S-1-5-21-2000"):
                second = config.endpoint
            self.assertTrue(first.startswith("\\\\.\\pipe\\codex-mcp-"))
            self.assertNotEqual(first, second)

    @unittest.skipIf(IS_WINDOWS, "Unix peer credentials")
    async def test_unix_socket_peer_is_current_user(self):
        left, right = socket.socketpair()
        try:
            self.assertEqual(unix_peer_uid(left), os.getuid())
            self.assertEqual(unix_peer_uid(right), os.getuid())
        finally:
            left.close()
            right.close()

    @unittest.skipUnless(sys.platform == "darwin", "macOS system aliases")
    async def test_macos_system_aliases_are_canonicalized(self):
        self.assertEqual(canonical_system_path(Path("/tmp/example")), Path("/private/tmp/example"))


@unittest.skipUnless(IS_WINDOWS, "Requires native Windows")
class NativeWindowsTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_acl_and_native_pipe_roundtrip(self):
        from codex_local_mcp.windows import PipeServer, exchange, current_sid
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            private_dir(state)
            fd = private_file(state / "private.txt", os.O_CREAT | os.O_RDWR)
            os.close(fd)
            address = "\\\\.\\pipe\\codex-mcp-test-" + hashlib.sha256((current_sid() + directory).encode()).hexdigest()[:20]
            async def respond(payload):
                return json.dumps({"echo": json.loads(payload)}).encode()
            server = PipeServer(address, respond, 4096)
            server.start()
            try:
                response = await exchange(address, '{"text":"中文"}'.encode(), 4096, 3)
                self.assertEqual(json.loads(response)["echo"]["text"], "中文")
            finally:
                server.close()
                await server.wait_closed()
