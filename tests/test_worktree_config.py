"""Worktree defaults and private-root validation do not create folders."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
from pathlib import Path

from codex_local_mcp.config import checked_worktree_root, load_config
from codex_local_mcp.platforms import IS_WINDOWS
from codex_local_mcp.security import BridgeError
from tests.helpers import install_fake_codex, link_or_skip, temp_root, write_config


class WorktreeConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.path = self.base / "settings.toml"
        write_config(self.path, self.base / "state", install_fake_codex(self.base))

    def test_defaults_and_fingerprint(self):
        config = load_config(str(self.path))
        self.assertFalse(config.worktrees)
        self.assertIsNone(config.worktree_root)
        self.assertNotEqual(config.fingerprint, replace(config, worktrees=True).fingerprint)
        self.assertNotEqual(config.fingerprint, replace(config, worktree_root=self.base / "worktrees").fingerprint)
        self.path.write_text(self.path.read_text() + 'worktrees = true\nworktree_root = ' + repr(str(self.base / "new")) + '\n')
        loaded = load_config(str(self.path))
        self.assertTrue(loaded.worktrees)
        self.assertEqual(loaded.worktree_root, self.base / "new")
        self.assertFalse(loaded.worktree_root.exists())

    def test_invalid_values(self):
        original = self.path.read_text()
        for value in ('"yes"', '1', '[]'):
            self.path.write_text(original + "worktrees = " + value + "\n")
            with self.subTest(value=value), self.assertRaisesRegex(BridgeError, "worktrees must be boolean"):
                load_config(str(self.path))
        for value in ('"relative"', '1', 'true'):
            self.path.write_text(original + "worktree_root = " + value + "\n")
            with self.subTest(value=value), self.assertRaisesRegex(BridgeError, "worktree_root must be an absolute private directory"):
                load_config(str(self.path))
        with self.assertRaises(BridgeError):
            checked_worktree_root(str(Path.home()))
        with self.assertRaises(BridgeError):
            checked_worktree_root(str(self.base / ".." / "escape"))

    def test_existing_root_is_private_and_links_are_refused(self):
        directory = self.base / "private"
        directory.mkdir(mode=0o700)
        if IS_WINDOWS:
            from codex_local_mcp.security import private_dir
            private_dir(directory)
        self.assertEqual(checked_worktree_root(str(directory)), directory)
        alias = self.base / "alias"
        link_or_skip(self, alias, directory)
        with self.assertRaises(BridgeError):
            checked_worktree_root(str(alias))
        if not IS_WINDOWS:
            directory.chmod(0o755)
            with self.assertRaisesRegex(BridgeError, "private permissions"):
                checked_worktree_root(str(directory))
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o755)

    def test_windows_root_check_is_readonly_closes_handle_and_has_plain_error(self):
        directory = self.base / "windows-root"
        directory.mkdir(mode=0o700)
        api = Mock()
        with patch("codex_local_mcp.config.checked_directory", return_value=directory), \
                patch("codex_local_mcp.config.IS_WINDOWS", True), patch("codex_local_mcp.windows.open_handle", return_value=123) as opened, \
                patch("codex_local_mcp.windows.api", return_value=api):
            self.assertEqual(checked_worktree_root(str(directory)), directory)
            opened.assert_called_once_with(directory, directory=True)
            api.check_access.assert_called_once_with(123)
            api.kernel.CloseHandle.assert_called_once_with(123)
            api.check_access.side_effect = PermissionError("Do not expose native ACL internals")
            with self.assertRaisesRegex(BridgeError, "worktree_root must be owned") as caught:
                checked_worktree_root(str(directory))
        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertNotIn("ACL internals", str(caught.exception))
