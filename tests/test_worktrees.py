"""Real Git only in private temporary repositories; no model or broker calls."""

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from codex_local_mcp import cli, worktrees as w
from tests.helpers import link_or_skip, temp_root


class WorktreesTest(unittest.TestCase):
    def setUp(self):
        directories, _ = w._path_directories()
        if not shutil.which("git", path=os.pathsep.join(map(str, directories))):
            self.skipTest("Real Git is not installed on absolute PATH entries")
        temporary = tempfile.TemporaryDirectory(prefix="worktree-", dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "répository with spaces"
        self.repo.mkdir(mode=0o700)
        self.locks = self.base / "locks"
        self.global_config = self.base / "global.gitconfig"
        self.global_config.write_text("")
        home_directory = tempfile.TemporaryDirectory(prefix="worktree-home-", dir=temp_root())
        self.addCleanup(home_directory.cleanup)
        home = Path(home_directory.name)
        self.enterContext(patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"), "GIT_CONFIG_GLOBAL": str(self.global_config)}))
        self.calls = []
        self.global_contents = []
        native_popen = subprocess.Popen
        def isolated(command, **kwargs):
            self.calls.append((list(command), dict(kwargs["env"]), kwargs["cwd"]))
            global_path = kwargs["env"].get("GIT_CONFIG_GLOBAL")
            self.global_contents.append(Path(global_path).read_bytes() if global_path else None)
            # Keep non-repository fixtures out of the checkout containing .tmp.
            kwargs["env"] = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(self.global_config),
                **kwargs["env"], "GIT_CEILING_DIRECTORIES": str(self.base.parent)}
            return native_popen(command, **kwargs)
        self.popen = patch.object(w.subprocess, "Popen", side_effect=isolated)
        self.popen.start()
        self.addCleanup(self.popen.stop)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Offline tests")
        self.git("config", "user.email", "offline@example.invalid")
        (self.repo / "tracked.txt").write_text("original\n")
        (self.repo / "deleted.txt").write_text("delete me\n")
        self.commit("initial fixture")

    def git(self, *args, cwd=None, allowed=(0,)):
        return w._git(list(args), cwd or self.repo, write=True, allowed=allowed)[0]

    def commit(self, message, cwd=None):
        self.git("add", "-A", "--", ".", cwd=cwd)
        self.git("commit", "-m", message, cwd=cwd)

    def prepared(self, name="trial", **kwargs):
        plan = w.plan(self.repo, name, **kwargs)
        return plan, w.create(plan, lock_dir=self.locks)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(w.WorktreeError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def snapshot(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    def test_plan_is_read_only_and_uses_canonical_identity(self):
        before = self.snapshot(self.repo)
        planned = w.plan(self.repo, "trial")
        self.assertEqual(planned, w.plan(self.repo / ".", "trial"))
        self.assertEqual(planned["path"], str(self.repo.parent / (self.repo.name + ".worktrees") / "trial"))
        self.assertEqual(planned["branch"], "codex/trial")
        self.assertEqual(planned["common_dir"], str(self.repo / ".git"))
        self.assertEqual(len(planned["base"]), 40)
        self.assertEqual(len(planned["plan_id"]), 64)
        self.assertFalse(Path(planned["root"]).exists())
        self.assertEqual(self.snapshot(self.repo), before)

    def test_non_git_and_bare_repositories(self):
        self.assert_code("not_git", w.plan, self.base, "trial")
        bare = self.base / "bare"
        bare.mkdir()
        self.git("init", "--bare", cwd=bare)
        self.assert_code("not_git", w.plan, bare, "trial")

    def test_name_and_base_validation(self):
        for name in ("", ".hidden", "-option", "a..b", "a/b", "a b", "a" * 65, "界"):
            with self.subTest(name=name):
                self.assert_code("name_invalid", w.plan, self.repo, name)
        for name in ("a", "_leading", "a.b-c_1", "a" * 64):
            self.assertEqual(w.plan(self.repo, name)["name"], name)
        for name in ("name.", "name.lock"):
            self.assert_code("name_invalid", w.plan, self.repo, name)
        for base in ("missing-reference", "--output=anything", "HEAD:tracked.txt"):
            self.assert_code("base_unknown", w.plan, self.repo, "trial", base)

    def test_existing_target_branch_and_foreign_receipt_refuse(self):
        planned = w.plan(self.repo, "trial")
        target = Path(planned["path"])
        target.mkdir(parents=True)
        self.assert_code("target_exists", w.plan, self.repo, "trial")
        target.rmdir()
        self.git("branch", "codex/trial")
        self.assert_code("branch_exists", w.plan, self.repo, "trial")

    def test_dirty_main_is_unchanged_and_custom_root_has_spaces(self):
        (self.repo / "tracked.txt").write_text("main unsaved\n")
        before = self.snapshot(self.repo)
        planned, created = self.prepared(root=self.base / "isolated 界")
        self.assertTrue(created["managed"])
        self.assertFalse(created["dirty"])
        self.assertEqual((Path(created["path"]) / "tracked.txt").read_text(), "original\n")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "main unsaved\n")
        self.assertTrue(all(path in self.snapshot(self.repo) and self.snapshot(self.repo)[path] == data for path, data in before.items()))
        self.assertEqual(planned["common_dir"], created["common_dir"])

    def test_custom_root_parent_components_are_normalised_before_recording(self):
        child = self.base / "child"
        child.mkdir()
        planned, created = self.prepared(root=child / "..")
        self.assertEqual(planned["root"], str(self.base))
        self.assertEqual(created["path"], str(self.base / "trial"))
        self.assertTrue(created["managed"])

    def test_custom_root_symlink_is_refused_without_writing(self):
        root, alias = self.base / "physical-root", self.base / "root-alias"
        root.mkdir(mode=0o700)
        link_or_skip(self, alias, root)
        error = self.assert_code("unsafe_config", w.plan, self.repo, "trial", root=alias)
        self.assertEqual(str(error), w.UNSAFE_PATH)
        self.assertEqual(list(root.iterdir()), [])

    def test_default_root_and_nested_parent_symlinks_are_refused(self):
        root = self.base / "elsewhere"
        root.mkdir(mode=0o700)
        alias = self.repo.parent / (self.repo.name + ".worktrees")
        link_or_skip(self, alias, root)
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")
        self.assert_code("unsafe_config", w.plan, self.repo, "trial", root=alias / "nested")
        self.assert_code("unsafe_config", w.plan, self.repo, "trial", root=alias / ".." / "hidden")
        self.assertEqual(list(root.iterdir()), [])

    def test_root_replaced_after_plan_is_refused_before_creation(self):
        root = self.base / "root"
        root.mkdir(mode=0o700)
        planned = w.plan(self.repo, "trial", root=root)
        kept = self.base / "kept"
        root.rename(kept)
        link_or_skip(self, root, kept)
        self.assert_code("unsafe_config", w.create, planned, lock_dir=self.locks)
        self.assertEqual(list(kept.iterdir()), [])

    def test_receipt_parent_replaced_by_symlink_is_refused(self):
        _, created = self.prepared()
        target = Path(created["path"])
        root, kept = target.parent, self.base / "kept"
        root.rename(kept)
        link_or_skip(self, root, kept)
        for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                (w.cleanup, {"lock_dir": self.locks})):
            with self.subTest(function=function.__name__):
                self.assert_code("unsafe_config", function, target, **options)
        self.assertEqual((kept / target.name / "tracked.txt").read_text(), "original\n")

    @unittest.skipIf(w.IS_WINDOWS, "POSIX directory ownership check")
    def test_target_parent_must_be_owned_by_the_user(self):
        with patch.object(w.os, "getuid", return_value=os.getuid() + 1):
            self.assert_code("unsafe_config", w._directory, self.base, owned=True)

    def test_windows_network_and_device_paths_are_refused_before_filesystem_access(self):
        # Pure paths have no lstat: reaching the filesystem would fail this test.
        class PureFilePath(PureWindowsPath):
            def expanduser(self):
                return self
        with patch.object(w, "IS_WINDOWS", True), patch.object(w, "Path", PureFilePath):
            for value in (r"\\server\share\repo", r"\\?\C:\repo", r"C:\repo\NUL"):
                with self.subTest(path=value):
                    self.assert_code("unsafe_config", w._path, value)

    def test_stale_or_tampered_plan_does_not_create(self):
        planned = w.plan(self.repo, "trial")
        (self.repo / "tracked.txt").write_text("new base\n")
        self.commit("advance base")
        self.assert_code("unsafe_config", w.create, planned, lock_dir=self.locks)
        self.assertFalse(Path(planned["path"]).exists())
        planned = w.plan(self.repo, "trial")
        planned["path"] = str(self.base / "unexpected")
        self.assert_code("unsafe_config", w.create, planned, lock_dir=self.locks)
        self.assert_code("unsafe_config", w.create, {}, lock_dir=self.locks)
        large = w.plan(self.repo, "oversized")
        large["notes"] = ["x" * 65536]
        with patch.object(w, "plan", return_value=large):
            self.assert_code("unsafe_config", w.create, large, lock_dir=self.locks)
        self.assertFalse(Path(large["path"]).exists())
        self.assertFalse(w._locator(Path(large["path"])).exists())
        self.assertIsNone(w._branch(Path(large["common_dir"]), large["branch"]))

    def test_filters_and_lfs_are_refused_before_checkout(self):
        for key, value in (("filter.test.smudge", "touch never"), ("filter.test.clean", "cat"),
                ("filter.test.process", "never"), ("filter.lfs.process", "git-lfs filter-process")):
            with self.subTest(key=key):
                self.git("config", key, value)
                self.assert_code("filters_configured", w.plan, self.repo, "trial")
                self.git("config", "--unset-all", key)

    def test_user_and_system_configuration_are_excluded_from_every_command(self):
        self.global_config.write_text('[filter "global"]\n clean = not-a-program\n')
        (Path.home() / ".gitconfig").write_text('[include]\n path = does-not-exist\n')
        system = self.base / "system.gitconfig"
        system.write_text('[filter "system"]\n process = not-a-program\n')
        with patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": str(system)}):
            _, created = self.prepared()
            w.handoff(created["path"], max_bytes=10000)
            w.cleanup(created["path"], lock_dir=self.locks)
        for _, env, _ in self.calls:
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertNotEqual(env["GIT_CONFIG_GLOBAL"], str(self.global_config))
            self.assertNotIn("GIT_CONFIG_SYSTEM", env)
        self.assertTrue(self.global_contents)
        self.assertTrue(all(value == b"" for value in self.global_contents))

    def test_local_includes_are_refused_even_when_the_included_file_is_safe(self):
        included = self.base / "included.cfg"
        included.write_text('[user]\n name = Harmless fixture\n')
        self.git("config", "include.path", str(included))
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")
        self.assertFalse((self.base / (self.repo.name + ".worktrees")).exists())

    def test_global_attributes_do_not_change_the_handoff(self):
        attributes = Path(os.environ["XDG_CONFIG_HOME"]) / "git" / "attributes"
        attributes.parent.mkdir(parents=True)
        attributes.write_text("*.txt -diff\n")
        _, created = self.prepared()
        (Path(created["path"]) / "tracked.txt").write_text("plain text change\n")
        report = w.handoff(created["path"], max_bytes=10000)
        self.assertFalse(report["binary"])
        self.assertIn(b"+plain text change", report["patch"])

    def test_worktree_local_filters_and_includes_are_audited(self):
        _, created = self.prepared()
        self.git("config", "extensions.worktreeConfig", "true")
        config = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "config.worktree", cwd=created["path"]).strip().decode())
        included = self.base / "included.cfg"
        included.write_text("")
        for contents, code in (("[include]\n path = " + json.dumps(str(included)) + "\n", "unsafe_config"),
                ('[filter "local"]\n clean = not-a-program\n', "filters_configured")):
            config.write_text(contents)
            for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                    (w.cleanup, {"lock_dir": self.locks})):
                with self.subTest(contents=contents, function=function.__name__):
                    self.assert_code(code, function, created["path"], **options)
        self.assertTrue(Path(created["path"]).is_dir())

    def test_main_worktree_configuration_is_audited_before_checkout(self):
        self.git("config", "extensions.worktreeConfig", "true")
        (self.repo / ".git" / "config.worktree").write_text('[include]\n path = missing.cfg\n')
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")

    def test_local_configuration_symlinks_and_hardlinks_are_refused(self):
        config, kept = self.repo / ".git" / "config", self.base / "saved-config"
        config.rename(kept)
        link_or_skip(self, config, kept)
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")
        config.unlink()
        os.link(kept, config)
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")

    def test_shared_attributes_cannot_be_redirected_through_a_link(self):
        attributes, kept = self.repo / ".git" / "info" / "attributes", self.base / "attributes"
        kept.write_text("*.txt text\n")
        link_or_skip(self, attributes, kept)
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")

    @unittest.skipIf(w.IS_WINDOWS, "POSIX FIFO safety")
    def test_non_regular_receipt_is_refused_without_waiting_for_a_writer(self):
        _, created = self.prepared()
        receipt = w._locator(Path(created["path"]))
        receipt.unlink()
        os.mkfifo(receipt, 0o600)
        # O_NONBLOCK is a property of the real open, not a wall-clock assertion.
        native_open, flags = os.open, []
        def opened(path, value, *args, **kwargs):
            if Path(path) == receipt:
                flags.append(value)
                self.assertTrue(value & os.O_NONBLOCK)
            return native_open(path, value, *args, **kwargs)
        with patch.object(w.os, "open", side_effect=opened):
            self.assert_code("not_managed", w.inspect, created["path"])
        self.assertTrue(flags)

    def test_late_filter_cannot_execute_during_checkout_handoff_or_status(self):
        (self.repo / ".gitattributes").write_text("*.txt filter=late\n")
        self.commit("filter attribute without a configured program")
        planned = w.plan(self.repo, "late-checkout")
        _, created = self.prepared("late-existing")
        marker, script = self.base / "must-not-execute", self.base / "filter.py"
        script.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text("executed")\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n')
        command = " ".join(shlex.quote(str(value)) for value in (sys.executable, script, marker))
        original = w._git
        for sensitive, function, args, kwargs in (
                (("worktree", "add"), w.create, (planned,), {"lock_dir": self.locks}),
                (("add",), w.handoff, (created["path"],), {"max_bytes": 100000}),
                (("status",), w.inspect, (created["path"],), {})):
            def raced(values, *rest, **options):
                if tuple(values[:len(sensitive)]) == sensitive:
                    original(["config", "filter.late.smudge", command], self.repo, write=True)
                    original(["config", "filter.late.clean", command], self.repo, write=True)
                return original(values, *rest, **options)
            (Path(created["path"]) / "tracked.txt").write_text("changed\n")
            with self.subTest(sensitive=sensitive), patch.object(w, "_git", raced):
                self.assert_code("filters_configured", function, *args, **kwargs)
            self.assertFalse(marker.exists())
            original(["config", "--remove-section", "filter.late"], self.repo, write=True)

    def test_new_command_key_before_cleanup_is_refused_by_reaudit(self):
        _, created = self.prepared()
        original = w._git
        def raced(args, *values, **kwargs):
            if args[:2] == ["worktree", "remove"]:
                original(["config", "core.sshCommand", "must-not-run"], self.repo, write=True)
            return original(args, *values, **kwargs)
        with patch.object(w, "_git", raced):
            self.assert_code("unsafe_config", w.cleanup, created["path"], lock_dir=self.locks)
        self.assertTrue(Path(created["path"]).exists())

    def test_filters_added_after_create_block_inspect_handoff_and_cleanup(self):
        _, created = self.prepared()
        self.git("config", "filter.lfs.smudge", "must-not-run")
        for function, kwargs in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}), (w.cleanup, {"lock_dir": self.locks})):
            self.assert_code("filters_configured", function, created["path"], **kwargs)
        self.assertTrue(Path(created["path"]).exists())

    def test_conditional_filters_cannot_activate_only_on_the_new_branch(self):
        conditional = self.base / "conditional.gitconfig"
        conditional.write_text('[filter "late"]\n smudge = must-not-run\n')
        self.git("config", "includeIf.onbranch:codex/*.path", str(conditional))
        self.assert_code("unsafe_config", w.plan, self.repo, "trial")

    def test_unoverridden_command_keys_are_refused_without_exposing_values(self):
        for key in ("core.sshCommand", "core.editor", "sequence.editor", "gpg.program", "credential.test.helper",
                "diff.test.textconv", "diff.test.command", "merge.test.driver", "submodule.test.update",
                "gpg.ssh.defaultKeyCommand", "uploadpack.packObjectsHook", "interactive.diffFilter",
                "difftool.test.cmd", "mergetool.test.path", "browser.test.cmd", "web.browser"):
            with self.subTest(key=key):
                self.git("config", key, "PRIVATE_DO_NOT_PRINT")
                error = self.assert_code("unsafe_config", w.plan, self.repo, "trial")
                self.assertNotIn("PRIVATE_DO_NOT_PRINT", json.dumps(error.as_dict()))
                self.git("config", "--unset-all", key)

    def test_hooks_fsmonitor_and_external_diff_never_run(self):
        (self.repo / ".gitattributes").write_text("*.txt diff=marker\n")
        self.commit("attribute fixture")
        hooks, marker = self.base / "hooks", self.base / "executed-marker"
        hooks.mkdir()
        body = "#!/bin/sh\nprintf armed > " + shlex.quote(marker.as_posix()) + "\n"
        for name in ("post-checkout", "reference-transaction", "pre-auto-gc"):
            script = hooks / name
            script.write_text(body)
            script.chmod(0o700)
        fsmonitor = self.base / "monitor"
        fsmonitor.write_text(body)
        fsmonitor.chmod(0o700)
        self.git("config", "core.hooksPath", str(hooks))
        self.git("config", "core.fsmonitor", str(fsmonitor))
        self.git("config", "diff.external", str(fsmonitor))
        # Positive controls: these very scripts can run through real Git.
        self.git("-c", "core.fsmonitor=" + str(fsmonitor), "status", "--porcelain")
        self.assertTrue(marker.exists())
        marker.unlink()
        control = self.base / "hook-control"
        self.git("-c", "core.hooksPath=" + str(hooks), "worktree", "add", "-b", "control", str(control))
        self.assertTrue(marker.exists())
        marker.unlink()
        _, created = self.prepared()
        self.git("config", "diff.marker.textconv", str(fsmonitor))
        self.assert_code("unsafe_config", w.handoff, created["path"], max_bytes=100000)
        self.assertFalse(marker.exists())
        self.git("config", "--unset-all", "diff.marker.textconv")
        w.inspect(created["path"])
        (Path(created["path"]) / "tracked.txt").write_text("handoff change\n")
        w.handoff(created["path"], max_bytes=100000)
        self.assert_code("dirty", w.cleanup, created["path"], lock_dir=self.locks)
        (Path(created["path"]) / "tracked.txt").write_text("original\n")
        w.cleanup(created["path"], lock_dir=self.locks)
        self.assertFalse(marker.exists())

    def test_symlinks_are_git_entries_not_followed_content(self):
        outside = self.base / "outside"
        outside.write_text("private outside content")
        link_or_skip(self, self.repo / "link", outside)
        self.git("config", "core.symlinks", "true")
        self.commit("symlink fixture")
        _, created = self.prepared()
        target = Path(created["path"])
        self.assertTrue((target / "link").is_symlink())
        (target / "link").unlink()
        (target / "link").symlink_to(self.base / "another-target")
        result = w.handoff(target, max_bytes=100000)
        self.assertIn("link", result["files"])
        self.assertNotIn(b"private outside content", result["patch"])

    def test_receipt_common_directory_identity_is_required_even_with_valid_checksum(self):
        _, created = self.prepared()
        target = Path(created["path"])
        receipt = w._locator(target)
        record = json.loads(receipt.read_text())
        record["common_identity"]["inode"] += 1
        record["plan_id"] = w._digest({k: v for k, v in record.items() if k not in ("plan_id", "state", "git_dir", "git_identity")})
        receipt.write_text(json.dumps(record))
        for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                (w.cleanup, {"lock_dir": self.locks})):
            with self.subTest(function=function.__name__):
                error = self.assert_code("not_managed", function, target, **options)
                self.assertEqual(str(error), "The common Git directory does not match its helper receipt.")
        self.assertEqual((target / "tracked.txt").read_text(), "original\n")

    def test_receipt_base_is_a_commit_id_before_any_revision_command(self):
        _, created = self.prepared()
        receipt = w._locator(Path(created["path"]))
        record = json.loads(receipt.read_text())
        record["base"] = "not-a-commit"
        record["plan_id"] = w._digest({k: v for k, v in record.items() if k not in ("plan_id", "state", "git_dir", "git_identity")})
        receipt.write_text(json.dumps(record))
        self.calls.clear()
        self.assert_code("not_managed", w.inspect, created["path"])
        self.assertFalse(any("merge-base" in command or "rev-list" in command for command, _, _ in self.calls))

    def test_replacement_common_directory_at_the_same_path_is_refused(self):
        _, created = self.prepared()
        common = Path(created["common_dir"])
        old = self.base / "previous-common"
        common.rename(old)
        shutil.copytree(old, common)
        self.assert_code("not_managed", w.inspect, created["path"])
        self.assertTrue(Path(created["path"]).exists())

    def test_receipt_must_match_the_worktrees_actual_repository(self):
        _, first = self.prepared("first")
        second = self.base / "second-repository"
        second.mkdir(mode=0o700)
        self.git("init", "--initial-branch=main", cwd=second)
        record = json.loads(w._locator(Path(first["path"])).read_text())
        common = second / ".git"
        record.update(common_dir=str(common), common_identity=w._identity(common))
        record["plan_id"] = w._digest({k: v for k, v in record.items() if k not in ("plan_id", "state", "git_dir", "git_identity")})
        w._locator(Path(first["path"])).write_text(json.dumps(record))
        for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                (w.cleanup, {"lock_dir": self.locks})):
            with self.subTest(function=function.__name__):
                self.assert_code("not_managed", function, first["path"], **options)
        self.assertTrue(Path(first["path"]).exists())

    def test_managed_git_calls_use_the_recorded_git_directory_explicitly(self):
        _, created = self.prepared()
        target = Path(created["path"])
        record = json.loads(w._locator(target).read_text())
        self.assertEqual(record["state"], "registered")
        self.assertEqual(record["git_dir"], created["git_dir"])
        self.assertEqual(record["git_identity"], w._identity(Path(created["git_dir"])))
        self.calls.clear()
        w.handoff(target, max_bytes=10000)
        w.plan(target, "child", root=self.base / "children")
        nested = target / "nested"
        nested.mkdir()
        w.plan(nested, "nested-child", root=self.base / "children")
        worktree_calls = [command for command, _, cwd in self.calls if Path(cwd) in (target, nested)]
        self.assertTrue(worktree_calls)
        for command in worktree_calls:
            self.assertIn("--git-dir=" + record["git_dir"], command)
            self.assertIn("--work-tree=" + str(target), command)

    def test_changed_worktree_git_pointer_is_refused_before_any_git_in_the_worktree(self):
        _, created = self.prepared()
        target = Path(created["path"])
        (target / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        self.calls.clear()
        for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                (w.cleanup, {"lock_dir": self.locks}), (w.plan, {"name": "child"})):
            with self.subTest(function=function.__name__):
                self.assert_code("unsafe_config", function, target, **options)
        self.assertFalse(any(Path(cwd) == target for _, _, cwd in self.calls))
        self.assertTrue((target / "tracked.txt").exists())

    def test_pointer_changed_after_the_check_cannot_redirect_the_native_command(self):
        _, created = self.prepared()
        target = Path(created["path"])
        original, seen = w.subprocess.Popen, []
        def changed(command, **options):
            if "status" in command and not seen:
                seen.append(command)
                (target / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
            return original(command, **options)
        with patch.object(w.subprocess, "Popen", side_effect=changed):
            self.assert_code("unsafe_config", w.inspect, target)
        self.assertEqual(len(seen), 1)
        self.assertIn("--git-dir=" + created["git_dir"], seen[0])
        self.assertIn("--work-tree=" + str(target), seen[0])
        self.assertEqual((target / "tracked.txt").read_text(), "original\n")

    def test_worktree_administrative_directory_identity_is_verified(self):
        _, created = self.prepared()
        receipt = w._locator(Path(created["path"]))
        record = json.loads(receipt.read_text())
        record["git_identity"]["inode"] += 1
        receipt.write_text(json.dumps(record))
        error = self.assert_code("not_managed", w.inspect, created["path"])
        self.assertEqual(str(error), "The Git worktree directory does not match its helper receipt.")
        del record["git_dir"]
        receipt.write_text(json.dumps(record))
        self.assert_code("not_managed", w.inspect, created["path"])

    def test_interrupted_registration_can_be_inspected_without_following_a_different_pointer(self):
        planned = w.plan(self.repo, "trial")
        with patch.object(w, "_save_registration", side_effect=w.WorktreeError("git_failed", "Interrupted receipt update")):
            self.assert_code("git_failed", w.create, planned, lock_dir=self.locks)
        record = json.loads(w._locator(Path(planned["path"])).read_text())
        self.assertEqual(record["state"], "creating")
        self.assertNotIn("git_dir", record)
        self.assertTrue(w.inspect(planned["path"])["managed"])
        (Path(planned["path"]) / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        self.assert_code("unsafe_config", w.inspect, planned["path"])

    def test_receipt_symlink_is_not_accepted(self):
        _, created = self.prepared()
        receipt = w._locator(Path(created["path"]))
        saved = self.base / "saved-receipt"
        receipt.rename(saved)
        link_or_skip(self, receipt, saved)
        for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                (w.cleanup, {"lock_dir": self.locks})):
            with self.subTest(function=function.__name__):
                self.assert_code("not_managed", function, created["path"], **options)
        self.assertTrue(Path(created["path"]).exists())

    @unittest.skipIf(w.IS_WINDOWS, "POSIX receipt permissions")
    def test_receipt_must_be_private_and_user_owned(self):
        _, created = self.prepared()
        receipt = w._locator(Path(created["path"]))
        receipt.chmod(0o644)
        self.assert_code("not_managed", w.inspect, created["path"])
        receipt.chmod(0o600)
        self.assertTrue(w.inspect(created["path"])["managed"])
        native_fstat = os.fstat
        def foreign(fd):
            info = native_fstat(fd)
            if info.st_ino == receipt.stat().st_ino:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info
        with patch.object(w.os, "fstat", side_effect=foreign):
            self.assert_code("not_managed", w.inspect, created["path"])

    def test_object_alternates_are_refused_before_reading_working_files(self):
        _, created = self.prepared()
        outside = self.base / "outside-objects"
        outside.mkdir(mode=0o700)
        (Path(created["common_dir"]) / "objects" / "info" / "alternates").write_text(str(outside) + "\n")
        self.calls.clear()
        for function, options in ((w.inspect, {}), (w.handoff, {"max_bytes": 10000}),
                (w.cleanup, {"lock_dir": self.locks})):
            with self.subTest(function=function.__name__):
                self.assert_code("unsafe_config", function, created["path"], **options)
        self.assertFalse(any(command[-1:] == ["HEAD"] or "status" in command or "diff" in command for command, _, _ in self.calls))
        self.assertEqual(list(outside.iterdir()), [])

    def test_object_storage_symlinks_and_the_inspection_limit_are_refused(self):
        _, created = self.prepared()
        outside = self.base / "outside-objects"
        outside.mkdir(mode=0o700)
        link = Path(created["common_dir"]) / "objects" / "linked"
        link_or_skip(self, link, outside)
        self.assert_code("unsafe_config", w.handoff, created["path"], max_bytes=10000)
        link.unlink()
        with patch.object(w, "OUTPUT_BYTES", 1):
            self.assert_code("unsafe_config", w.inspect, created["path"])

    def test_git_symlink_false_is_preserved(self):
        outside = self.base / "outside"
        outside.write_text("outside content")
        link_or_skip(self, self.repo / "link", outside)
        self.git("config", "core.symlinks", "true")
        self.commit("symlink fixture")
        self.git("config", "core.symlinks", "false")
        _, created = self.prepared()
        copied = Path(created["path"]) / "link"
        self.assertFalse(copied.is_symlink())
        self.assertEqual(copied.read_text(), str(outside))

    def test_submodule_is_reported_and_not_initialised(self):
        head = self.git("rev-parse", "HEAD").strip().decode()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.repo / ".gitmodules").write_text('[submodule "module"]\n path = module\n url = https://invalid.example/module\n')
        self.git("add", ".gitmodules")
        self.git("commit", "-m", "submodule fixture")
        planned, created = self.prepared()
        self.assertEqual(planned["submodules"], ["module"])
        self.assertFalse(planned["submodules_initialised"])
        self.assertFalse((Path(created["path"]) / "module" / ".git").exists())
        self.assertFalse(created["dirty"])

    def test_unsaved_files_in_uninitialised_submodule_survive_cleanup(self):
        head = self.git("rev-parse", "HEAD").strip().decode()
        self.git("update-index", "--add", "--cacheinfo", "160000," + head + ",module")
        (self.repo / ".gitmodules").write_text('[submodule "module"]\n path = module\n url = https://invalid.example/module\n')
        self.git("add", ".gitmodules")
        self.git("commit", "-m", "submodule fixture")
        _, created = self.prepared()
        target = Path(created["path"])
        unsaved = target / "module" / "unsaved.txt"
        unsaved.write_text("must survive")
        # Git itself reports this clean and would remove the file without --force.
        self.assertEqual(self.git("status", "--porcelain=v2", "-z", "--untracked-files=all", cwd=target), b"")
        report = w.inspect(target)
        self.assertTrue(report["dirty"])
        self.assertEqual(report["nonempty_submodules"], ["module"])
        self.assert_code("dirty", w.cleanup, target, lock_dir=self.locks)
        self.assert_code("unsafe_config", w.handoff, target, max_bytes=100000)
        self.assertEqual(unsaved.read_text(), "must survive")

    def test_live_stale_and_reused_pid_locks(self):
        common = Path(w.plan(self.repo, "trial")["common_dir"])
        w._private(self.locks)
        path = self.locks / (w._digest(os.path.normcase(str(common))) + ".lock")
        started = w._process_start(os.getpid())
        self.assertIsInstance(started, int)
        w._write(path, {"pid": os.getpid(), "start_time": started})
        self.assert_code("locked", w.create, w.plan(self.repo, "trial"), lock_dir=self.locks)
        self.assertTrue(path.exists())
        path.unlink()
        w._write(path, {"pid": os.getpid(), "start_time": started - 1})
        _, created = self.prepared()
        self.assertFalse(path.exists())
        w.cleanup(created["path"], lock_dir=self.locks)
        w._write(path, {"pid": 2147483646, "start_time": 1})
        self.prepared("after-stale")
        self.assertFalse(path.exists())

    def test_lock_guard_serialises_helpers_and_unknown_identity_refuses(self):
        planned = w.plan(self.repo, "trial")
        with w._lock(Path(planned["common_dir"]), self.locks):
            self.assert_code("locked", w.create, planned, lock_dir=self.locks)
        with patch.object(w, "_process_start", return_value="unknown"):
            self.assert_code("locked", w.create, planned, lock_dir=self.locks)

    def test_failure_after_registration_is_inspectable_and_cleanup_is_explicit(self):
        planned, original = w.plan(self.repo, "trial"), w._git
        def failed(args, *values, **kwargs):
            result = original(args, *values, **kwargs)
            if args[:2] == ["worktree", "add"]:
                raise w.WorktreeError("git_failed", "Injected crash after registration")
            return result
        with patch.object(w, "_git", failed):
            error = self.assert_code("git_failed", w.create, planned, lock_dir=self.locks)
        self.assertTrue(error.details["directory_exists"])
        self.assertTrue(error.details["registered"])
        self.assertTrue(error.details["branch_exists"])
        self.assertEqual(w.inspect(planned["path"])["registration"], "ok")
        self.assertTrue(w.cleanup(planned["path"], lock_dir=self.locks)["removed"])

    def test_failure_before_registration_leaves_a_receipt_that_cleanup_can_remove(self):
        planned, original = w.plan(self.repo, "trial"), w._git
        def failed(args, *values, **kwargs):
            if args[:2] == ["worktree", "add"]:
                raise w.WorktreeError("git_failed", "Injected crash before registration")
            return original(args, *values, **kwargs)
        with patch.object(w, "_git", failed):
            error = self.assert_code("git_failed", w.create, planned, lock_dir=self.locks)
        self.assertEqual({k: error.details[k] for k in ("directory_exists", "registered", "branch_exists")},
            {"directory_exists": False, "registered": False, "branch_exists": False})
        self.assertTrue(w.cleanup(planned["path"], lock_dir=self.locks)["receipt_removed"])
        self.prepared()

    def test_replaced_worktree_path_never_deletes_the_replacement(self):
        _, created = self.prepared()
        target = Path(created["path"])
        shutil.rmtree(target)
        target.write_text("unsaved replacement file")
        self.assert_code("not_managed", w.cleanup, target, lock_dir=self.locks)
        self.assertEqual(target.read_text(), "unsaved replacement file")

    def test_missing_directory_registration_is_reported_without_global_prune(self):
        _, created = self.prepared()
        shutil.rmtree(created["path"])
        self.assertEqual(w.inspect(created["path"])["registration"], "missing directory")
        try:
            result = w.cleanup(created["path"], lock_dir=self.locks)
        except w.WorktreeError as exc:
            self.assertEqual(exc.code, "git_failed")
            self.assertTrue(exc.details["registered"])
        else:
            self.assertTrue(result["removed"])
        self.assertFalse(any("prune" in call[0] for call in self.calls))

    def test_handoff_includes_all_changes_without_changing_status_or_indexes(self):
        _, created = self.prepared()
        target = Path(created["path"])
        (target / "tracked.txt").write_text("modified\n")
        (target / "deleted.txt").unlink()
        (target / "binary.bin").write_bytes(b"\x00\x01\xffpayload")
        (target / "untracked 界.txt").write_text("untracked\n")
        (target / "staged.txt").write_text("staged\n")
        self.git("add", "staged.txt", cwd=target)
        before_status = self.git("status", "--porcelain=v2", "-z", "--untracked-files=all", cwd=target)
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=target).strip().decode())
        indexes = ((self.repo / ".git" / "index").read_bytes(), index.read_bytes())
        contents = self.snapshot(target)
        result = w.handoff(target, max_bytes=100000)
        self.assertEqual(set(result["files"]), {"tracked.txt", "deleted.txt", "binary.bin", "untracked 界.txt", "staged.txt"})
        self.assertEqual(set(result["untracked"]), {"binary.bin", "untracked 界.txt"})
        self.assertTrue(result["binary"])
        self.assertIn(b"GIT binary patch\n", result["patch"])
        self.assertFalse(result["truncated"])
        self.assertEqual(self.snapshot(target), contents)
        self.assertEqual(((self.repo / ".git" / "index").read_bytes(), index.read_bytes()), indexes)
        self.assertEqual(self.git("status", "--porcelain=v2", "-z", "--untracked-files=all", cwd=target), before_status)

    def test_handoff_refuses_over_cap_without_returning_a_prefix(self):
        _, created = self.prepared()
        (Path(created["path"]) / "tracked.txt").write_text("large payload\n" * 2000)
        error = self.assert_code("git_failed", w.handoff, created["path"], max_bytes=100)
        self.assertIn("byte limit", str(error))
        self.assert_code("git_failed", w.handoff, created["path"], max_bytes=0)

    def test_handoff_binary_marker_must_be_a_header_not_file_content(self):
        _, created = self.prepared()
        (Path(created["path"]) / "tracked.txt").write_text("GIT binary patch\n")
        result = w.handoff(created["path"], max_bytes=100000)
        self.assertIn(b"+GIT binary patch\n", result["patch"])
        self.assertFalse(result["binary"])

    def test_rename_original_path_cannot_be_mistaken_for_an_untracked_record(self):
        _, created = self.prepared()
        original = w._git
        def renamed(args, *values, **kwargs):
            if args[:2] == ["status", "--porcelain=v2"]:
                return b"2 R. N... 100644 100644 100644 " + b"0" * 40 + b" " + b"0" * 40 + b" R100 new\0? old\0? actual\0", 0
            return original(args, *values, **kwargs)
        with patch.object(w, "_git", renamed):
            report = w.inspect(created["path"])
        self.assertTrue(report["dirty"])
        self.assertEqual(report["untracked_count"], 1)
        self.assertEqual(report["untracked"], ["actual"])

    def test_cleanup_refuses_dirty_ignored_and_own_commits_and_can_keep_branch(self):
        _, created = self.prepared()
        target = Path(created["path"])
        (target / "untracked").write_text("unsaved")
        self.assert_code("dirty", w.cleanup, target, lock_dir=self.locks)
        (target / "untracked").unlink()
        (target / ".gitignore").write_text("ignored\n")
        self.commit("ignore fixture", cwd=target)
        (target / "ignored").write_text("must survive")
        self.assert_code("dirty", w.cleanup, target, lock_dir=self.locks, keep_branch=True)
        self.assertTrue((target / "ignored").exists())
        (target / "ignored").unlink()
        self.assert_code("unmerged_commits", w.cleanup, target, lock_dir=self.locks)
        self.assertGreater(w.inspect(target)["ahead"], 0)
        result = w.cleanup(target, lock_dir=self.locks, keep_branch=True)
        self.assertTrue(result["branch_kept"])
        self.assertFalse(target.exists())
        self.assertTrue(self.git("show-ref", "--verify", "refs/heads/codex/trial"))
        self.assertFalse(any("-D" in call[0] for call in self.calls))

    def test_hidden_index_flags_never_allow_cleanup_and_handoff_includes_the_changes(self):
        for number, flag in enumerate(("--assume-unchanged", "--skip-worktree")):
            with self.subTest(flag=flag):
                _, created = self.prepared("flags-" + str(number))
                target = Path(created["path"])
                self.git("update-index", flag, "tracked.txt", cwd=target)
                (target / "tracked.txt").write_text("unsaved file must survive\n")
                index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=target).strip().decode())
                previous = index.read_bytes()
                self.assertEqual(self.git("status", "--porcelain=v2", "-z", cwd=target), b"")
                report = w.inspect(target)
                self.assertTrue(report["dirty"])
                self.assertEqual(report["index_flags"], ["tracked.txt"])
                self.assert_code("dirty", w.cleanup, target, lock_dir=self.locks, keep_branch=True)
                self.assertEqual((target / "tracked.txt").read_text(), "unsaved file must survive\n")
                handoff = w.handoff(target, max_bytes=10000)
                self.assertIn(b"+unsaved file must survive", handoff["patch"])
                self.assertEqual(index.read_bytes(), previous)

    def test_fresh_comparison_detects_changes_hidden_by_cached_stat_information(self):
        _, created = self.prepared()
        target = Path(created["path"])
        self.git("config", "core.trustctime", "false", cwd=target)
        tracked = target / "tracked.txt"
        timestamp = 1_000_000_000_000_000_000
        os.utime(tracked, ns=(timestamp, timestamp))
        self.git("update-index", "--refresh", cwd=target)
        tracked.write_text("modified\n")  # Same length, inode, mode and mtime.
        os.utime(tracked, ns=(timestamp, timestamp))
        self.assertEqual(self.git("status", "--porcelain=v2", "-z", cwd=target), b"")
        self.assert_code("dirty", w.cleanup, target, lock_dir=self.locks)
        self.assertEqual(tracked.read_text(), "modified\n")

    def test_cleanup_removes_only_clean_helper_created_branch(self):
        _, created = self.prepared()
        self.assertTrue(w.cleanup(created["path"], lock_dir=self.locks)["removed"])
        self.assertFalse(Path(created["path"]).exists())
        self.assertFalse(w._branch(self.repo / ".git", created["branch"]))
        branch_calls = [call[0] for call in self.calls if "branch" in call[0] and "-d" in call[0]]
        self.assertEqual(len(branch_calls), 1)
        foreign = self.base / "manual"
        self.git("worktree", "add", "-b", "codex/manual", str(foreign))
        self.assert_code("not_managed", w.inspect, foreign)
        self.assert_code("not_managed", w.cleanup, foreign, lock_dir=self.locks)
        self.assertTrue(foreign.exists())

    def test_cleanup_survives_removal_of_its_linked_source(self):
        _, source = self.prepared("source")
        planned = w.plan(source["path"], "child", root=self.base / "child-worktrees")
        child = w.create(planned, lock_dir=self.locks)
        w.cleanup(source["path"], lock_dir=self.locks)
        self.assertTrue(w.inspect(child["path"])["managed"])
        self.assertTrue(w.cleanup(child["path"], lock_dir=self.locks)["removed"])

    def test_repository_with_a_separate_git_directory_keeps_its_binding(self):
        common = self.base / "separate.git"
        (self.repo / ".git").rename(common)
        (self.repo / ".git").write_text("gitdir: " + str(common) + "\n")
        _, created = self.prepared()
        self.assertEqual(created["common_dir"], str(common))
        add = next(command for command, _, _ in self.calls if "worktree" in command and "add" in command)
        self.assertIn("--git-dir=" + str(common), add)
        self.assertIn("--work-tree=" + str(self.repo), add)
        self.assertTrue(w.inspect(created["path"])["managed"])
        self.assertTrue(w.cleanup(created["path"], lock_dir=self.locks)["removed"])

    def test_keep_branch_recovers_cleanup_after_directory_was_removed(self):
        _, created = self.prepared()
        original = w._git
        def failed(args, *values, **kwargs):
            if args[:2] == ["branch", "-d"]:
                raise w.WorktreeError("git_failed", "Injected failure before branch deletion")
            return original(args, *values, **kwargs)
        with patch.object(w, "_git", failed):
            self.assert_code("git_failed", w.cleanup, created["path"], lock_dir=self.locks)
        self.assertFalse(Path(created["path"]).exists())
        recovered = w.cleanup(created["path"], lock_dir=self.locks, keep_branch=True)
        self.assertTrue(recovered["receipt_removed"])
        self.assertTrue(recovered["branch_kept"])
        self.assertIsNotNone(w._branch(Path(created["common_dir"]), created["branch"]))

    def test_git_worktree_lock_refuses_cleanup(self):
        _, created = self.prepared()
        self.git("worktree", "lock", created["path"])
        self.assertEqual(w.inspect(created["path"])["registration"], "locked")
        self.assert_code("locked", w.cleanup, created["path"], lock_dir=self.locks)

    def test_partial_sparse_and_metadata_targets_are_refused(self):
        self.assert_code("unsafe_config", w.plan, self.repo, "trial", root=self.repo / ".git" / "new")
        for key, value in (("extensions.partialClone", "origin"), ("remote.origin.promisor", "true"), ("core.sparseCheckout", "true")):
            self.git("config", key, value)
            self.assert_code("unsafe_config", w.plan, self.repo, "trial")
            self.git("config", "--unset-all", key)

    def test_malformed_and_unreadable_lock_owner_are_not_stolen(self):
        planned = w.plan(self.repo, "trial")
        w._private(self.locks)
        path = self.locks / (w._digest(os.path.normcase(planned["common_dir"])) + ".lock")
        w._write(path, {"pid": os.getpid(), "start_time": "unknown"})
        self.assert_code("locked", w.create, planned, lock_dir=self.locks)
        path.unlink()
        w._write(path, {"pid": os.getpid(), "start_time": 123})
        with patch.object(w, "_process_start", return_value="unknown"):
            self.assert_code("locked", w.create, planned, lock_dir=self.locks)
        self.assertTrue(path.exists())

    def test_lock_release_error_closes_the_guard_and_reports_created_resources(self):
        original_open, original_unlink, descriptors = w.private_file, Path.unlink, []
        def opened(path, flags):
            fd = original_open(path, flags)
            if path.suffix == ".guard":
                descriptors.append(fd)
            return fd
        def removed(path, *args, **kwargs):
            if path.suffix == ".lock" and path.parent == self.locks:
                raise PermissionError("Injected lock release failure")
            return original_unlink(path, *args, **kwargs)
        planned = w.plan(self.repo, "trial")
        with patch.object(w, "private_file", opened), patch.object(Path, "unlink", removed):
            error = self.assert_code("unsafe_config", w.create, planned, lock_dir=self.locks)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertTrue(all(error.details[key] for key in ("directory_exists", "registered", "branch_exists")))

    def test_lock_file_replaced_by_another_owner_is_left_alone(self):
        planned = w.plan(self.repo, "trial")
        path = self.locks / (w._digest(os.path.normcase(planned["common_dir"])) + ".lock")
        with self.assertRaises(w.WorktreeError) as error:
            with w._lock(Path(planned["common_dir"]), self.locks):
                path.write_text(json.dumps({"pid": os.getpid() + 1, "start_time": 123}))
        self.assertEqual(error.exception.code, "locked")
        self.assertEqual(json.loads(path.read_text())["pid"], os.getpid() + 1)

    def test_environment_is_sanitised_and_every_git_call_has_safety_options(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.base / "wrong"), "GIT_WORK_TREE": "/wrong",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/wrong"}):
            self.prepared()
        for command, env, cwd in self.calls:
            self.assertNotIn("GIT_DIR", env)
            self.assertNotIn("GIT_WORK_TREE", env)
            self.assertNotIn("GIT_CONFIG_COUNT", env)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["LC_ALL"], "C")
            self.assertEqual(env["GIT_ATTR_NOSYSTEM"], "1")
            self.assertTrue(Path(cwd).is_absolute())
            for option in ("core.fsmonitor=false", "core.untrackedCache=false", "diff.external=", "credential.helper=", "protocol.allow=never"):
                self.assertIn(option, command)
            self.assertTrue(any(value.startswith("core.hooksPath=") for value in command))

    def test_git_missing_old_version_and_output_limits(self):
        with patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", "relative"])}):
            self.assert_code("git_missing", w.plan, self.repo, "trial")
        original = w._git
        def old(args, cwd, **kwargs):
            if args == ["--version"]:
                return b"git version 2.35.9\n", 0
            return original(args, cwd, **kwargs)
        w._check_version.cache_clear()
        with patch.object(w, "_git", old):
            self.assert_code("git_too_old", w.plan, self.repo, "trial")
        self.assert_code("git_failed", w._git, ["log", "--format=%B"], self.repo, maximum=1)

    def test_windows_which_never_gets_a_bare_command_that_can_prepend_cwd(self):
        directory = self.base / "absolute-bin"
        directory.mkdir()
        candidate = directory / "git.exe"
        candidate.write_bytes(b"resolution fixture; never executed")
        seen = []
        def which(command, **kwargs):
            seen.append(command)
            return "." + os.sep + "git.exe" if command == "git" else str(candidate)
        class Process:
            returncode = 0
            def __init__(self, *args, **kwargs):
                self.stdout, self.stderr = io.BytesIO(b"git version 2.53.0\n"), io.BytesIO()
            def wait(self, timeout):
                return 0
        with patch.object(w, "IS_WINDOWS", True), patch.object(w, "_path_directories", return_value=([directory], False)), patch.object(w.shutil, "which", side_effect=which), patch.object(w.subprocess, "Popen", side_effect=Process):
            version, _ = w._git(["--version"], self.repo)
        self.assertTrue(version.startswith(b"git version "))
        self.assertTrue(seen)
        self.assertTrue(all(command == str(candidate) for command in seen))

    def test_git_deadline_and_failed_pipe_read_are_explicit_refusals(self):
        class Process:
            def __init__(self, failed=False):
                self.stdout, self.stderr = io.BytesIO(), io.BytesIO()
                self.returncode, self.waits, self.kills = 0, 0, 0
                if failed:
                    class Broken(io.BytesIO):
                        def read(self, size):
                            raise OSError("Injected pipe read failure")
                    self.stdout = Broken()
            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1 and not failed:
                    raise subprocess.TimeoutExpired("git", timeout)
                return self.returncode
            def kill(self):
                self.kills += 1
                self.returncode = -9
        for failed, message in ((False, "timed out"), (True, "no partial result")):
            process = Process(failed)
            with patch.object(w.subprocess, "Popen", return_value=process):
                error = self.assert_code("git_failed", w._git, ["status"], self.repo, binary="/trusted/git")
            self.assertIn(message, str(error))
            self.assertEqual(process.kills, 0 if failed else 1)
        class Stuck(Process):
            def wait(self, timeout):
                raise subprocess.TimeoutExpired("git", timeout)
        with patch.object(w.subprocess, "Popen", return_value=Stuck()):
            error = self.assert_code("git_failed", w._git, ["status"], self.repo, binary="/trusted/git")
        self.assertIn("timed out", str(error))

    def test_cli_plain_json_and_errors_without_loading_bridge_config(self):
        def run(*args):
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", ["codex-local-mcp", "worktree", *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                cli.main()
            return output.getvalue(), errors.getvalue()
        plain, errors = run("plan", "--repo", str(self.repo), "--name", "cli")
        self.assertIn("branch: codex/cli", plain)
        self.assertEqual(errors, "")
        data = json.loads(run("plan", "--repo", str(self.repo), "--name", "cli", "--json")[0])
        created = json.loads(run("create", "--repo", str(self.repo), "--name", "cli", "--plan-id", data["plan_id"], "--json")[0])
        self.assertEqual(json.loads(run("inspect", "--path", created["path"], "--json")[0])["registration"], "ok")
        handoff = json.loads(run("handoff", "--path", created["path"], "--json")[0])
        self.assertEqual(handoff["patch_encoding"], "base64")
        self.assertEqual(handoff["patch"], "")
        self.assertIn("removed: true", run("cleanup", "--path", created["path"])[0])
        with self.assertRaises(SystemExit) as caught:
            run("inspect", "--path", created["path"], "--json")
        self.assertEqual(caught.exception.code, 1)

    def test_cli_plain_failure_reports_resources_left_after_registration(self):
        original = w._git
        def failed(args, *values, **kwargs):
            result = original(args, *values, **kwargs)
            if args[:2] == ["worktree", "add"]:
                raise w.WorktreeError("git_failed", "Injected crash after registration")
            return result
        errors = io.StringIO()
        with patch.object(sys, "argv", ["codex-local-mcp", "worktree", "create", "--repo", str(self.repo), "--name", "partial"]), patch.object(w, "_git", failed), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 1)
        for value in ("directory_exists: true", "registered: true", "branch_exists: true"):
            self.assertIn(value, errors.getvalue())
        target = Path(w.plan(self.repo, "different")["root"]) / "partial"
        self.assertTrue(w.inspect(target)["managed"])


if __name__ == "__main__":
    unittest.main()
