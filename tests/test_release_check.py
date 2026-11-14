import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import __version__
from scripts.release_check import inspect_wheel, inspect_sdist, check, console_entry, release_version


class ReleaseCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.modules = {"codex_local_mcp/__init__.py", "codex_local_mcp/diagnostics.py"}

    def tearDown(self):
        self.temp.cleanup()

    def wheel(self, changes=None, omit=()):
        metadata = f"Name: codex-local-mcp\nVersion: {__version__}\n"
        files = {name: b"# module" for name in self.modules}
        files["codex_local_mcp/__init__.py"] = f"__version__ = {__version__!r}\n".encode()
        files.update({f"codex_local_mcp-{__version__}.dist-info/METADATA": metadata.encode(),
            f"codex_local_mcp-{__version__}.dist-info/licenses/LICENSE": b"MIT",
            f"codex_local_mcp-{__version__}.dist-info/entry_points.txt": b"[console_scripts]\ncodex-local-mcp = codex_local_mcp.cli:main\n"})
        files.update(changes or {})
        path = self.directory / "fixture.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                if name not in omit:
                    archive.writestr(name, data)
        return path

    def sdist(self, *, version=__version__, missing=None):
        files = {name: b"# module" for name in self.modules}
        files["codex_local_mcp/__init__.py"] = f"__version__ = {version!r}\n".encode()
        files.update({"PKG-INFO": f"Name: codex-local-mcp\nVersion: {version}\n".encode(),
            "pyproject.toml": f'[project]\nname="codex-local-mcp"\nversion="{version}"\ndependencies=[]\n'.encode(),
            "README.md": b"Readme", "LICENSE": b"MIT"})
        path = self.directory / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                if name != missing:
                    info = tarfile.TarInfo("codex_local_mcp-" + version + "/" + name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        return path

    def test_current_versions_agree_and_wheel_sdist_inspection_accepts_complete_artifacts(self):
        self.assertEqual(release_version(), __version__)
        inspect_wheel(self.wheel(), __version__, self.modules)
        inspect_sdist(self.sdist(), __version__, self.modules)

    def test_wheel_rejects_missing_license_entrypoint_or_module(self):
        for name in (f"codex_local_mcp-{__version__}.dist-info/licenses/LICENSE",
                     f"codex_local_mcp-{__version__}.dist-info/entry_points.txt", "codex_local_mcp/diagnostics.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_wheel(self.wheel(omit=(name,)), __version__, self.modules)

    def test_wheel_rejects_wrong_version_dependency_and_secret_or_fixture_content(self):
        meta = f"codex_local_mcp-{__version__}.dist-info/METADATA"
        for changes in ({meta: b"Name: codex-local-mcp\nVersion: 9.0.0\n"},
                        {meta: f"Name: codex-local-mcp\nVersion: {__version__}\nRequires-Dist: forbidden\n".encode()},
                        {"tests/fixture.py": b"x"}, {"codex_local_mcp/fixtures/native.json": b"{}"},
                        {"state/state.sqlite3": b"x"}, {"codex_local_mcp/.env": b"x"},
                        {"codex_local_mcp/__init__.py": b"__version__ = '9.0.0'\n"},
                        {"codex_local_mcp/auth.json": b"x"}, {"../escaped.py": b"x"}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                inspect_wheel(self.wheel(changes), __version__, self.modules)

    def test_sdist_rejects_wrong_version_or_missing_build_inputs(self):
        for name in ("pyproject.toml", "LICENSE", "README.md", "codex_local_mcp/diagnostics.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_sdist(self.sdist(missing=name), __version__, self.modules)
        with self.assertRaises(ValueError):
            inspect_sdist(self.sdist(version="9.0.0"), __version__, self.modules)

    def test_build_absence_has_clear_skip_and_ci_can_require_it(self):
        with patch("scripts.release_check.importlib.util.find_spec", return_value=None), patch("builtins.print") as output:
            self.assertEqual(check(), 0)
            self.assertEqual(check(require_build=True), 1)
        self.assertTrue(any("SKIP build" in call.args[0] for call in output.call_args_list))

    def test_console_entry_handles_pip_target_layout_on_windows_and_posix(self):
        for windows, folder in ((True, "bin"), (True, "Scripts"), (False, "bin")):
            with self.subTest(windows=windows, folder=folder):
                target = self.directory / (str(windows) + folder)
                (target / folder).mkdir(parents=True)
                entry = target / folder / ("codex-local-mcp.exe" if windows else "codex-local-mcp")
                entry.touch()
                self.assertEqual(console_entry(target, windows=windows), entry)
