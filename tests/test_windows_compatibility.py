"""Windows semantics reproduced without native handles or a Windows runner."""

import ctypes
import io
import ntpath
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_local_mcp import retention, windows, worktrees
from codex_local_mcp.security import validate_windows_path
from tests.test_documentation_contract import documentation_issues


class WindowsCompatibilityTest(unittest.TestCase):
    def test_git_commondir_parent_components_use_the_checked_windows_walk(self):
        with tempfile.TemporaryDirectory() as temporary:
            common = Path(temporary) / ".git"
            admin = common / "worktrees" / "trial"
            admin.mkdir(parents=True)
            # Keep Linux filesystem operations, with the real Windows lexical
            # validation applied to an equivalent local-drive spelling.
            def validate(value):
                validate_windows_path("C:\\" + value.lstrip("/").replace("/", "\\"))
            with patch.object(worktrees, "IS_WINDOWS", True), patch.object(worktrees, "validate_windows_path", side_effect=validate):
                self.assertEqual(worktrees._path(admin / ".." / ".."), common)
                with self.assertRaises(worktrees.WorktreeError):
                    worktrees._path(common / "CON" / ".." / "safe")
                linked = common / "junction"
                linked.mkdir()
                original = Path.lstat
                def lstat(path, *args, **kwargs):
                    if path == linked:
                        return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                    return original(path, *args, **kwargs)
                with patch.object(Path, "lstat", lstat), self.assertRaises(worktrees.WorktreeError):
                    worktrees._path(linked / ".." / "safe")

    def test_git_forward_slashes_and_drive_case_match_registered_windows_paths(self):
        class WindowsPath(PureWindowsPath):
            def is_dir(self):
                return True
        entry = {"worktree": "c:/Repo With Spaces/trial", "branch": "refs/heads/codex/trial"}
        record = {"path": r"C:\Repo With Spaces\trial", "common_dir": r"C:\Repo With Spaces\main\.git", "branch": "codex/trial"}
        with patch.object(worktrees, "Path", WindowsPath), patch.object(worktrees.os.path, "normcase", ntpath.normcase), \
                patch.object(worktrees, "_entries", return_value=[entry]), patch.object(worktrees, "_branch", return_value="commit"):
            self.assertTrue(worktrees._exists(record)["registered"])

    def test_windows_change_time_query_closes_the_native_handle_on_success_and_failure(self):
        for readable in (True, False):
            closed = Mock()
            def query(handle, kind, target, size):
                self.assertEqual(kind, 0)
                target._obj.changed = 123456
                return readable
            def check(ok):
                if not ok:
                    raise OSError("Cannot query file identity")
            api = SimpleNamespace(kernel=SimpleNamespace(GetFileInformationByHandleEx=query, CloseHandle=closed), check=check)
            with self.subTest(readable=readable), patch.object(windows, "api", return_value=api), \
                    patch.object(windows, "open_handle", return_value=123) as opened:
                if readable:
                    self.assertEqual(windows.file_change_time("git.exe"), 123456)
                else:
                    with self.assertRaises(OSError):
                        windows.file_change_time("git.exe")
            opened.assert_called_once_with("git.exe", share=7, access=0x80)
            closed.assert_called_once_with(123)

    def access(self, owner, entries, *, private=True):
        acl = windows.ACL()
        acl.count = len(entries)
        buffers = [ctypes.create_string_buffer(32) for _ in entries]
        labels = {1: owner}
        for buffer, (sid, mask) in zip(buffers, entries):
            ctypes.c_uint32.from_address(ctypes.addressof(buffer) + 4).value = mask
            labels[ctypes.addressof(buffer) + 8] = sid
        def security(handle, kind, flags, owner_out, group, dacl_out, sacl, descriptor):
            ctypes.cast(owner_out, ctypes.POINTER(ctypes.c_void_p))[0] = 1
            ctypes.cast(dacl_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(acl)
            return 0
        def ace(dacl, index, target):
            ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffers[index])
            return True
        api = windows.API.__new__(windows.API)
        api.advapi = SimpleNamespace(GetSecurityInfo=security, GetAce=ace)
        api.kernel = SimpleNamespace(LocalFree=Mock())
        api.sid_text = lambda pointer: labels[pointer.value]
        with patch.object(windows, "current_sid", return_value="current-user"):
            api.check_access(100, private=private)
        api.kernel.LocalFree.assert_called_once()

    def test_python_private_directory_owner_rights_is_allowed_for_verified_owners(self):
        for owner in ("current-user", "S-1-5-32-544"):
            with self.subTest(owner=owner):
                self.access(owner, [("S-1-3-4", 0x1F01FF), ("S-1-5-18", 0x1F01FF)])

    def test_owner_rights_does_not_allow_a_foreign_owner_or_foreign_writer(self):
        for owner, entries in (("foreign-user", [("S-1-3-4", 0x1F01FF)]),
                ("current-user", [("S-1-3-4", 0x1F01FF), ("foreign-user", 0x1F01FF)])):
            for private in (True, False):
                with self.subTest(owner=owner, private=private), self.assertRaises(PermissionError):
                    self.access(owner, entries, private=private)

    def test_prune_expiry_does_not_roundtrip_a_pre_epoch_local_wall_time(self):
        class WindowsDateTime(datetime):
            @classmethod
            def fromtimestamp(cls, value, tz=None):
                if tz is None:
                    raise OSError("Windows local mktime cannot represent this pre-epoch wall time")
                return datetime.fromtimestamp(value, tz)
        report = {"dry_run": True, "estimated_reclaimable_bytes": 0, "retained_payload_bytes": 0,
            "plan_id": "review", "vacuum_needed": False, "expires_at": 13600.0, "counts": {}, "protected": {}}
        with patch.object(retention, "datetime", WindowsDateTime):
            output = retention.render(report, now=10000)
        local = datetime.fromtimestamp(13600, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
        self.assertIn(local, output)
        self.assertIn("60 min left", output)

    def test_documentation_order_does_not_use_native_case_folding(self):
        class WindowsOrderPath(type(Path())):
            def __lt__(self, other):
                return str(self).casefold() < str(other).casefold()
        from codex_local_mcp.tools import TOOLS
        with tempfile.TemporaryDirectory() as temporary:
            root = WindowsOrderPath(temporary)
            (root / "docs").mkdir()
            (root / "README.md").write_text(f"# Root\n{len(TOOLS)} tools\n[bad](missing.md)\n", encoding="utf-8")
            (root / "README.zh-CN.md").write_text("# Root\n", encoding="utf-8")
            (root / "docs" / "example.md").write_text("[bad](missing.md)\n", encoding="utf-8")
            self.assertEqual([issue.path for issue in documentation_issues(root)], ["README.md", "docs/example.md"])


class GitBinaryCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.binary = self.directory / ("git.exe" if worktrees.IS_WINDOWS else "git")
        self.binary.write_bytes(b"git version 2.53.0\n")
        self.binary.chmod(0o700)
        self.versions = 0
        test = self
        class Process:
            returncode = 0
            def __init__(self, command, **kwargs):
                if "--version" in command:
                    test.versions += 1
                    value = test.binary.read_bytes()
                else:
                    value = b""
                self.stdout, self.stderr = io.BytesIO(value), io.BytesIO()
            def wait(self, timeout):
                return 0
        self.enterContext(patch.object(worktrees, "_path_directories", return_value=([self.directory], False)))
        self.enterContext(patch.object(worktrees.shutil, "which", return_value=str(self.binary)))
        self.enterContext(patch.object(worktrees.subprocess, "Popen", side_effect=Process))

    def test_repeated_calls_check_one_unchanged_binary_version(self):
        for _ in range(5):
            worktrees._git(["status"], self.directory)
        self.assertEqual(self.versions, 1)

    def test_replaced_binary_with_same_size_and_mtime_is_version_checked_again(self):
        worktrees._git(["status"], self.directory)
        info = self.binary.stat()
        replacement = self.directory / "replacement"
        replacement.write_bytes(b"git version 2.35.0\n")
        replacement.chmod(0o700)
        os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
        replacement.replace(self.binary)
        with self.assertRaises(worktrees.WorktreeError) as caught:
            worktrees._git(["status"], self.directory)
        self.assertEqual(caught.exception.code, "git_too_old")
        self.assertEqual(self.versions, 2)

    def test_windows_creation_time_is_not_used_as_file_change_time(self):
        info = self.binary.stat()
        fixed = SimpleNamespace(**{key: getattr(info, key) for key in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")}, st_file_attributes=0)
        with patch.object(Path, "stat", return_value=fixed), patch.object(windows, "file_change_time", create=True, side_effect=(100, 101)):
            first = worktrees._binary_identity(str(self.binary))
            second = worktrees._binary_identity(str(self.binary))
        self.assertNotEqual(first, second)
