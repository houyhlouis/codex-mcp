"""Linux process detection uses bounded, private fake procfs records only."""

import json
import os
import sys
import tempfile
import unittest
from contextlib import chdir, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_local_mcp import procscan
from tests.helpers import temp_root
from tests.helpers import link_or_skip


class ProcscanTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=temp_root())
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.project = self.base / "project with spaces (界)"
        self.project.mkdir()
        self.uid = self.proc.stat().st_uid

    def process(self, pid, args=None, *, start=123456, comm="sandbox (inner) worker)"):
        directory = self.proc / str(pid)
        directory.mkdir()
        if args is None:
            args = ["/installed/codex-linux-sandbox", "--sandbox-policy-cwd", str(self.project),
                "--permission-profile", '{"token":"PRIVATE_PROFILE_CANARY"}', "--", "sh", "-c", "PRIVATE_COMMAND_CANARY"]
        data = args if isinstance(args, bytes) else b"\x00".join(value.encode("utf-8", errors="surrogateescape") for value in args) + b"\x00"
        (directory / "cmdline").write_bytes(data)
        (directory / "stat").write_bytes(f"{pid} ({comm}) S ".encode() + b"0 " * 18 + str(start).encode() + b" 0\n")
        return directory

    def scan(self, projects=None, **kwargs):
        return procscan.scan([self.project] if projects is None else projects, proc_root=self.proc, uid=self.uid, **kwargs)

    def test_outer_inner_and_both_flag_forms_have_only_process_identity_and_paths(self):
        self.process(101)
        self.process(102, ["codex-linux-sandbox", "--sandbox-policy-cwd=" + str(self.project),
            "--command-cwd=" + str(self.project / "child")], start=987654)
        result = self.scan()
        self.assertEqual({key: value for key, value in result.items() if key != "processes"},
            {"available": True, "reason": "ok", "truncated": False, "scanned": 2})
        self.assertEqual([item["pid"] for item in result["processes"]], [101, 102])
        self.assertEqual(result["processes"][0], {"pid": 101, "start_time": 123456,
            "policy_cwd": str(self.project), "command_cwd": None, "project": str(self.project)})
        self.assertEqual(result["processes"][1]["start_time"], 987654)
        self.assertEqual(result["processes"][1]["command_cwd"], str(self.project / "child"))
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertNotIn("permission-profile", json.dumps(result))

    def test_same_inside_around_and_sibling_use_the_existing_overlap_rule(self):
        paths = [self.project, self.project / "nested", self.project.parent, self.base / (self.project.name + "-sibling")]
        for pid, path in enumerate(paths, 201):
            self.process(pid, ["codex-linux-sandbox", "--command-cwd", str(path)])
        result = self.scan()
        self.assertEqual([item["pid"] for item in result["processes"]], [201, 202, 203])
        other = self.base / "other"
        self.process(205, ["codex-linux-sandbox", "--command-cwd", str(other)])
        result = self.scan([str(self.project), str(other)])
        self.assertEqual(result["processes"][-1]["project"], str(other))

    def test_relative_flags_and_odd_paths_are_normalized_without_resolving_files(self):
        value = self.project.name + "/missing/../child\nfolder"
        self.process(301, ["codex-linux-sandbox", "--command-cwd", value])
        original = str(self.project) + os.sep + "."
        with (chdir(self.base), patch.object(Path, "resolve", side_effect=AssertionError("No filesystem path resolution")),
                patch.object(os.path, "realpath", side_effect=AssertionError("Lexical matches need no resolution"))):
            result = self.scan([original])
        self.assertEqual(result["processes"][0]["project"], original)
        self.assertEqual(result["processes"][0]["command_cwd"], str(self.project / "child\nfolder"))

    def test_surrogateescaped_paths_round_trip(self):
        value = str(self.project / "name-\udcff")
        self.process(302, ["codex-linux-sandbox", "--command-cwd=" + value])
        self.assertEqual(self.scan()["processes"][0]["command_cwd"], value)

    @unittest.skipUnless(sys.platform == "linux", "Linux // root normalization")
    def test_double_leading_slash_and_trailing_slash_match(self):
        self.process(303, ["codex-linux-sandbox", "--command-cwd", "/" + str(self.project) + "/"])
        result = self.scan()
        self.assertEqual([p["pid"] for p in result["processes"]], [303])
        self.assertEqual(result["processes"][0]["command_cwd"], str(self.project))

    def test_symlinked_process_cwd_matches_resolved_project(self):
        alias = self.base / "alias"
        link_or_skip(self, alias, self.project)
        self.process(304, ["codex-linux-sandbox", "--command-cwd", str(alias)])
        self.assertEqual([p["pid"] for p in self.scan()["processes"]], [304])

    def test_resolution_is_cached_and_capped_and_failure_is_not_clear(self):
        outside = self.base / "outside"
        outside.mkdir()
        for pid in range(400, 410):
            self.process(pid, ["codex-linux-sandbox", "--command-cwd", str(outside)])
        with patch.object(os.path, "realpath", wraps=os.path.realpath) as resolve:
            self.assertTrue(self.scan()["available"])
            self.assertEqual(resolve.call_count, 2)  # One root and one spelling, not ten PIDs.
        for pid in range(410, 480):
            path = self.base / str(pid)
            path.mkdir()
            self.process(pid, ["codex-linux-sandbox", "--command-cwd", str(path)])
        with patch.object(os.path, "realpath", wraps=os.path.realpath) as resolve:
            self.assertFalse(self.scan()["available"])
            self.assertEqual(resolve.call_count, procscan.RESOLUTION_LIMIT)
        with patch.object(os.path, "realpath", side_effect=OSError("Cannot inspect link")):
            self.assertEqual(self.scan()["reason"], "proc_unreadable")

    def test_describe_partial_positive_names_the_known_pid(self):
        self.process(305)
        result = self.scan()
        result.update(available=False, reason="proc_unreadable")
        self.assertEqual(procscan.describe(result), "At least 1 Codex sandbox process is still running in this project (PID 305).")

    def test_exact_argv0_and_real_wrapper_flags_are_required(self):
        cases = [
            ["different", "codex-linux-sandbox", "--command-cwd", str(self.project)],
            ["codex-linux-sandbox-other", "--command-cwd", str(self.project)],
            ["codex-linux-sandbox"],
            ["codex-linux-sandbox", "--", "--command-cwd", str(self.project)],
            ["codex-linux-sandbox", "--command-cwd"],
            ["codex-linux-sandbox", "--command-cwd="],
            ["codex-linux-sandbox", "--permission-profile", '{"command":"--command-cwd ' + str(self.project) + '"}'],
        ]
        for pid, args in enumerate(cases, 401):
            self.process(pid, args)
        self.assertEqual(self.scan()["processes"], [])

    def test_unreadable_vanished_wrong_owner_and_nonprocess_entries_are_skipped(self):
        good = self.process(501)
        self.process(502)
        self.process(503)
        self.process(504)
        (self.proc / "not-a-pid").mkdir()
        (self.proc / "505").write_text("not a directory")
        entries = []
        with os.scandir(self.proc) as iterator:
            for entry in iterator:
                info = entry.stat(follow_symlinks=False)
                entry_stat = Mock(return_value=SimpleNamespace(st_mode=info.st_mode, st_uid=self.uid + int(entry.name == "502")))
                if entry.name == "503":
                    entry_stat.side_effect = FileNotFoundError()
                entries.append(SimpleNamespace(name=entry.name, path=entry.path, stat=entry_stat))
        original = Path.open
        def opened(path, *args, **kwargs):
            if path == self.proc / "504" / "cmdline":
                raise PermissionError("unreadable fixture process")
            return original(path, *args, **kwargs)
        with patch.object(procscan.os, "scandir", return_value=nullcontext(entries)), patch.object(Path, "open", opened):
            result = self.scan()
        self.assertEqual([item["pid"] for item in result["processes"]], [int(good.name)])
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "proc_unreadable")
        for entry in entries:
            if entry.name.isdecimal():
                entry.stat.assert_called_once_with(follow_symlinks=False)

    def test_malformed_stat_and_cmdline_files_are_skipped(self):
        for pid, data in ((601, b"missing parentheses"), (602, b"602 (comm) S 1 2"),
                (603, b"603 (comm) S " + b"0 " * 18 + b"bad"),
                (604, b"999 (comm) S " + b"0 " * 18 + b"1"),
                (605, b"605 (" + b"x" * procscan.STAT_BYTES + b")")):
            directory = self.process(pid)
            (directory / "stat").write_bytes(data)
        self.process(606, b"codex-linux-sandbox --command-cwd " + os.fsencode(self.project))
        self.process(607, b"")
        self.process(608, b"\x00")
        result = self.scan()
        self.assertEqual(result["processes"], [])
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "proc_unreadable")

    def test_large_cmdlines_only_use_complete_prefix_arguments_and_bounded_reads(self):
        self.process(701, ["codex-linux-sandbox", "--command-cwd", str(self.project),
            "--permission-profile", "PRIVATE_" + "x" * (procscan.CMDLINE_BYTES * 2)])
        self.process(702, ["codex-linux-sandbox", "--permission-profile", "x" * (procscan.CMDLINE_BYTES * 2),
            "--command-cwd", str(self.project)])
        original, reads = Path.open, []
        class Reader:
            def __init__(self, path):
                self.path, self.stream = path, original(path, "rb")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def read(self, size):
                reads.append((self.path.name, size))
                return self.stream.read(size)
        with patch.object(Path, "open", lambda path, *args, **kwargs: Reader(path)):
            result = self.scan()
        self.assertEqual([item["pid"] for item in result["processes"]], [701])
        self.assertEqual(reads.count(("cmdline", procscan.CMDLINE_BYTES)), 2)
        self.assertEqual(reads.count(("stat", procscan.STAT_BYTES + 1)), 1)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_limit_and_description_are_bounded(self):
        for pid in range(801, 813):
            self.process(pid)
        result = self.scan(limit=2)
        self.assertEqual(len(result["processes"]), 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(procscan.describe(result).startswith("At least 2 Codex sandbox processes"))
        self.assertFalse(self.scan(limit=12)["truncated"])
        description = procscan.describe(self.scan(limit=12))
        self.assertIn("PID 801, 802, 803, 804, 805, 806, 807, 808, 809, 810 and 2 more", description)
        self.assertNotIn("811", description)
        zero = self.scan(limit=0)
        self.assertEqual(zero["processes"], [])
        self.assertTrue(zero["truncated"])
        self.assertIn("truncated", procscan.describe(zero))

    def test_unavailable_empty_nonlinux_and_disabled_do_not_read_process_arguments(self):
        with patch.object(procscan.os, "scandir", side_effect=PermissionError()):
            self.assertEqual(self.scan()["reason"], "proc_unreadable")
        self.assertEqual(procscan.scan([], proc_root=self.proc / "missing")["reason"], "proc_unreadable")
        with patch.object(Path, "open", side_effect=AssertionError("No process reads")):
            self.assertEqual(self.scan([])["scanned"], 0)
        with patch.object(procscan.sys, "platform", "win32"), patch.object(procscan.os, "scandir") as directory:
            result = procscan.scan([self.project])
            self.assertEqual(result["reason"], "not_linux")
            directory.assert_not_called()
        with patch.object(procscan, "scan", side_effect=AssertionError("Disabled check must not scan")):
            result = procscan.scan_for(SimpleNamespace(sandbox_process_check=False), [self.project])
        self.assertEqual(result, {"available": False, "reason": "disabled", "processes": [], "truncated": False, "scanned": 0})
        self.assertEqual(procscan.AVAILABLE_REASONS, ("ok", "not_linux", "proc_unreadable", "disabled"))


if __name__ == "__main__":
    unittest.main()
