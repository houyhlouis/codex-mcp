import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.platforms import IS_WINDOWS, find_on_path, resolve_codex
from tests.helpers import link_or_skip, temp_root


class ExecutableResolutionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project"
        self.tools = self.base / "installed tools"
        self.project.mkdir()
        self.tools.mkdir()
        self.name = "codex.exe" if IS_WINDOWS else "codex"

    def executable(self, path):
        path.write_text("# Never executed by these tests\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_empty_dot_and_relative_path_entries_are_ignored(self):
        self.executable(self.project / self.name)
        relative = self.project / "bin"
        relative.mkdir()
        self.executable(relative / self.name)
        absolute = str(self.tools)
        paths = ("", ".", "bin", "./bin", "../project/bin",
                 os.pathsep.join([".", absolute]),
                 os.pathsep.join(["", absolute]),
                 os.pathsep.join([absolute, ""]),
                 os.pathsep.join([absolute, "", absolute]),
                 os.pathsep.join(["bin", absolute]))
        with chdir(self.project):
            for value in paths:
                with self.subTest(PATH=value), patch.dict(os.environ, {"PATH": value}):
                    self.assertIsNone(find_on_path(self.name))
                    with self.assertRaises(ValueError) as caught:
                        resolve_codex(self.name)
                    message = str(caught.exception)
                    self.assertIn("PATH", message)
                    self.assertIn("ignored", message)
                    self.assertIn("codex_binary", message)
                    self.assertIn("absolute path", message)

    def test_absolute_candidate_wins_over_ignored_entries(self):
        self.executable(self.project / self.name)
        installed = self.executable(self.tools / self.name)
        relative = self.project / "bin"
        relative.mkdir()
        self.executable(relative / self.name)
        value = os.pathsep.join(["", ".", "bin", str(self.tools), ""])
        with chdir(self.project), patch.dict(os.environ, {"PATH": value}):
            self.assertEqual(find_on_path(self.name), str(installed))
            self.assertEqual(resolve_codex(self.name), str(installed.resolve()))

    def test_missing_binary_reports_ignored_entries_without_a_matching_file(self):
        value = os.pathsep.join(["", "missing-relative-directory", str(self.tools)])
        with chdir(self.project), patch.dict(os.environ, {"PATH": value}):
            with self.assertRaisesRegex(ValueError, "codex_binary.*absolute path.*PATH entries were ignored"):
                resolve_codex(self.name)

    def test_symlinked_installed_launcher_resolves_to_its_real_path(self):
        target = self.executable(self.base / self.name)
        link = self.tools / self.name
        link_or_skip(self, link, target)
        with chdir(self.project), patch.dict(os.environ, {"PATH": str(self.tools)}):
            self.assertEqual(resolve_codex(self.name), str(target.resolve()))

    @unittest.skipIf(IS_WINDOWS, "POSIX executable permissions")
    def test_non_executable_file_and_directory_are_skipped(self):
        unreadable = self.base / "not executable"
        directories = self.base / "directories"
        unreadable.mkdir()
        directories.mkdir()
        (unreadable / self.name).write_text("# Not executable\n", encoding="utf-8")
        (unreadable / self.name).chmod(0o600)
        (directories / self.name).mkdir()
        installed = self.executable(self.tools / self.name)
        value = os.pathsep.join([str(unreadable), str(directories), str(self.tools)])
        with patch.dict(os.environ, {"PATH": value}):
            self.assertEqual(resolve_codex(self.name), str(installed.resolve()))
        with patch.dict(os.environ, {"PATH": str(unreadable)}):
            self.assertIsNone(find_on_path(self.name))

    def test_windows_suffix_search_filters_all_unsafe_entries(self):
        (self.project / "codex.exe").write_bytes(b"not executed")
        relative = self.project / "bin"
        relative.mkdir()
        (relative / "codex.cmd").write_text("not executed", encoding="utf-8")
        shim = self.tools / "codex.cmd"
        shim.write_text("not executed", encoding="utf-8")
        value = os.pathsep.join(["", ".", "bin", '"' + str(self.tools) + '"', ""])
        with chdir(self.project), patch("codex_local_mcp.platforms.IS_WINDOWS", True), \
                patch.dict(os.environ, {"PATH": value, "PATHEXT": ".EXE;.CMD"}):
            self.assertEqual(find_on_path("codex"), str(shim))
            self.assertEqual(find_on_path("codex.cmd"), str(shim))

    @unittest.skipUnless(IS_WINDOWS, "Native Windows relative paths")
    def test_windows_relative_paths_are_rejected(self):
        for value in (r".\bin\codex.exe", r"bin/codex.exe", r"C:bin\codex.exe", r"C:codex.exe", r"\bin\codex.exe"):
            with self.subTest(binary=value), self.assertRaisesRegex(ValueError, "codex_binary.*absolute path"):
                resolve_codex(value)
