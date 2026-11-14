import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.config import load_config
from codex_local_mcp.platforms import IS_WINDOWS
from codex_local_mcp.security import BridgeError
from tests.helpers import link_or_skip, temp_root, write_config


class ConfigBinaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.client = self.base / "client"
        self.settings = self.base / "settings"
        self.package = self.base / "package"
        self.tools = self.base / "installed tools"
        for directory in (self.client, self.settings, self.package, self.tools):
            directory.mkdir()
        self.config_file = self.settings / "config.toml"
        self.state = self.base / "s"
        self.binary = self.tools / "codex.py"
        self.binary.write_text("# Never executed by these tests\n", encoding="utf-8")

    def configs_from_each_cwd(self):
        configs = []
        for directory in (self.client, self.settings, self.package):
            with chdir(directory):
                configs.append(load_config(str(self.config_file)))
        return configs

    def assert_canonical_identity(self, configs, binary):
        self.assertEqual([config.codex for config in configs], [str(binary.resolve())] * len(configs))
        self.assertEqual(len({config.fingerprint for config in configs}), 1)

    def test_absolute_binary_with_spaces_is_independent_of_cwd(self):
        write_config(self.config_file, self.state, self.binary)
        self.assert_canonical_identity(self.configs_from_each_cwd(), self.binary)

    def test_sandbox_process_check_defaults_to_true_and_changes_broker_identity(self):
        write_config(self.config_file, self.state, self.binary, sandbox_process_check=None)
        default = load_config(str(self.config_file))
        self.assertTrue(default.sandbox_process_check)
        self.assertTrue(default.fingerprint_inputs["sandbox_process_check"])
        with self.config_file.open("a", encoding="utf-8") as stream:
            stream.write("sandbox_process_check = false\n")
        disabled = load_config(str(self.config_file))
        self.assertFalse(disabled.sandbox_process_check)
        self.assertFalse(disabled.fingerprint_inputs["sandbox_process_check"])
        self.assertNotEqual(disabled.fingerprint, default.fingerprint)
        write_config(self.config_file, self.state, self.binary, sandbox_process_check=None)
        with self.config_file.open("a", encoding="utf-8") as stream:
            stream.write("sandbox_process_check = true\n")
        self.assertEqual(load_config(str(self.config_file)).fingerprint, default.fingerprint)

    def test_sandbox_process_check_requires_a_boolean_even_for_legacy_stop(self):
        for value in ('"true"', "1", "0", "[]", '"false"'):
            write_config(self.config_file, self.state, self.binary, sandbox_process_check=None)
            with self.config_file.open("a", encoding="utf-8") as stream:
                stream.write("sandbox_process_check = " + value + "\n")
            for legacy in (False, True):
                with self.subTest(value=value, legacy_stop=legacy), self.assertRaisesRegex(BridgeError, "sandbox_process_check must be boolean"):
                    load_config(str(self.config_file), legacy_stop=legacy)

    def test_bare_binary_on_absolute_path_has_one_identity_across_cwds(self):
        name = "codex.exe" if IS_WINDOWS else "codex"
        installed = self.tools / name
        installed.write_text("# Never executed\n", encoding="utf-8")
        installed.chmod(0o700)
        planted = self.client / name
        planted.write_text("# Never executed\n", encoding="utf-8")
        planted.chmod(0o700)
        write_config(self.config_file, self.state, "codex")
        value = os.pathsep.join([".", "", str(self.tools)])
        with patch.dict(os.environ, {"PATH": value, "PATHEXT": ".EXE;.CMD"}):
            self.assert_canonical_identity(self.configs_from_each_cwd(), installed)

    def test_tilde_is_expanded_before_the_absolute_path_rule(self):
        write_config(self.config_file, self.state, "~/installed tools/codex.py")
        expanduser = os.path.expanduser
        with patch("os.path.expanduser", side_effect=lambda value:
                   str(self.base) + value[1:] if value.startswith("~") else expanduser(value)):
            expanded = self.configs_from_each_cwd()
        write_config(self.config_file, self.state, self.binary)
        absolute = self.configs_from_each_cwd()
        self.assert_canonical_identity(expanded + absolute, self.binary)

    def test_symlinked_absolute_binary_has_the_target_fingerprint(self):
        link = self.tools / "linked codex.py"
        link_or_skip(self, link, self.binary)
        write_config(self.config_file, self.state, link)
        linked = self.configs_from_each_cwd()
        write_config(self.config_file, self.state, self.binary)
        direct = self.configs_from_each_cwd()
        self.assert_canonical_identity(linked + direct, self.binary)

    def test_relative_binary_is_rejected_in_client_and_broker_cwds(self):
        (self.client / "bin").mkdir()
        (self.client / "bin" / "codex.py").write_text("# Never executed\n", encoding="utf-8")
        for binary in ("./bin/codex.py", "bin/codex.py", "../client/bin/codex.py"):
            write_config(self.config_file, self.state, binary)
            messages = []
            for directory in (self.client, self.settings, self.package):
                with self.subTest(binary=binary, cwd=str(directory)), chdir(directory):
                    with self.assertRaises(BridgeError) as caught:
                        load_config(str(self.config_file))
                    message = str(caught.exception)
                    self.assertIn("codex_binary", message)
                    self.assertIn(repr(binary), message)
                    self.assertIn("broker", message)
                    self.assertIn("different working directory", message)
                    self.assertIn("absolute path", message)
                    messages.append(message)
            self.assertEqual(len(set(messages)), 1)

    def test_invalid_absolute_binary_reports_its_value_and_remedy(self):
        missing = str(self.tools / "missing codex.py")
        write_config(self.config_file, self.state, missing)
        with self.assertRaises(BridgeError) as caught:
            load_config(str(self.config_file))
        message = str(caught.exception)
        self.assertIn("not found", message)
        self.assertIn("codex_binary", message)
        self.assertIn(repr(missing), message)
        self.assertIn("absolute path", message)

    def test_stop_still_loads_legacy_configs_with_unresolvable_binaries(self):
        (self.client / "bin").mkdir()
        (self.client / "bin" / "codex.py").write_text("# Never executed\n", encoding="utf-8")
        for binary in ("./bin/codex.py", "missing-codex", str(self.tools / "missing.py")):
            write_config(self.config_file, self.state, binary)
            with self.config_file.open("a", encoding="utf-8") as stream:
                stream.write('\n[workspace_roots]\nold = "/unused"\n')
            configs = []
            for directory in (self.client, self.settings, self.package):
                with self.subTest(binary=binary, cwd=str(directory)), chdir(directory), \
                        patch.dict(os.environ, {"PATH": str(self.tools)}):
                    config = load_config(str(self.config_file), legacy_stop=True)
                    self.assertEqual(config.codex, binary)
                    configs.append(config)
            self.assertEqual(len({config.fingerprint for config in configs}), 1)
