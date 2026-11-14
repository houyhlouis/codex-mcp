"""Doctor reports process checks from read-only project metadata and fake procfs."""

import json
import os
import unittest
from contextlib import closing
from unittest.mock import patch

from codex_local_mcp import diagnostics, procscan
from codex_local_mcp.config import load_config
from codex_local_mcp.store import Store
from tests import test_diagnostics as doctor
from tests.helpers import write_config


class ProcessDoctorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = doctor.DoctorTest()
        await self.case.asyncSetUp()
        # This fixture explicitly tests the product default against fake procfs.
        write_config(self.case.path, self.case.state, self.case.config.codex, sandbox_process_check=None)
        self.case.config = load_config(str(self.case.path))
        self.proc = self.case.base / "proc"
        self.proc.mkdir()
        self.projects = [self.case.base / name for name in ("first-project", "second-project")]
        with closing(Store(self.case.state)) as store:
            for index, project in enumerate(self.projects):
                project.mkdir()
                store.save("projects", {"id": "project-" + str(index), "path": str(project)})
        scan = procscan.scan
        self.probe = patch.object(procscan, "scan", side_effect=lambda projects: scan(projects,
            proc_root=self.proc, uid=self.proc.stat().st_uid))
        self.scanner = self.probe.start()

    async def asyncTearDown(self):
        try:
            self.probe.stop()
        finally:
            await self.case.asyncTearDown()

    def process(self, pid, project):
        directory = self.proc / str(pid)
        directory.mkdir()
        args = ["/installed/codex-linux-sandbox", "--command-cwd", str(project),
            "--permission-profile", '{"password":"PRIVATE_PROFILE_661"}', "--", "PRIVATE_COMMAND_662"]
        (directory / "cmdline").write_bytes(b"\x00".join(value.encode() for value in args) + b"\x00")
        (directory / "stat").write_bytes(f"{pid} (sandbox (inner)) S ".encode() + b"0 " * 18 + b"88 0\n")

    async def report(self):
        with patch.dict(os.environ, {"PATH": str(self.case.base)}):
            report = await self.case.report()
        diagnostics.validate_report(report)
        return report

    async def test_available_clear_check_is_info_and_preserves_the_database(self):
        before = {path.name: path.read_bytes() for path in self.case.state.iterdir()}
        report = await self.report()
        facts = report["facts"]["sandbox_process_check"]
        self.assertEqual(facts, {"available": True, "reason": "ok", "processes": [], "truncated": False, "scanned": 0,
            "enabled": True, "projects_known": True, "projects_checked": 2, "broker_running": False})
        finding = next(item for item in report["findings"] if item["code"] == "process_check_available")
        self.assertEqual(finding["severity"], "info")
        self.assertEqual(finding["message"], "The Linux process check is available. No Codex sandbox processes were found for 2 saved project folders.")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.case.state.iterdir()}, before)
        self.case.start_mock.assert_not_awaited()
        self.assertEqual(set(report), {"format", "bridge_version", "summary", "exit_code", "facts", "findings"})

    async def test_processes_for_each_known_project_warn_without_a_server(self):
        for pid, project in zip((1234, 1240), self.projects):
            self.process(pid, project)
        report = await self.report()
        findings = [item for item in report["findings"] if item["code"] == "sandbox_processes_untracked"]
        self.assertEqual(len(findings), 2)
        self.assertEqual(report["exit_code"], 1)
        for finding, pid, project in zip(findings, (1234, 1240), self.projects):
            self.assertEqual(finding, {"code": "sandbox_processes_untracked", "severity": "warning",
                "message": f"Project {project}: 1 Codex sandbox process is still running in this project (PID {pid}). No background server is running.",
                "next_step": "Stop these processes with kill PID. Nothing else is needed."})
        self.assertEqual(report["facts"]["sandbox_process_check"]["projects_checked"], 2)
        self.assertNotIn("PRIVATE_", json.dumps(report))
        self.assertNotIn("PRIVATE_", diagnostics.render(report))
        self.assertIn("What to do: Stop these processes with kill PID.", diagnostics.render(report))

    async def test_running_server_processes_are_info_and_do_not_request_a_kill(self):
        self.process(1234, self.projects[0])
        self.case.health_mock.side_effect = None
        self.case.health_mock.return_value = self.case.healthy()
        report = await self.report()
        finding = next(item for item in report["findings"] if item["code"] == "sandbox_processes_running")
        self.assertEqual(finding["severity"], "info")
        self.assertTrue(finding["message"].endswith("They may belong to current work in the background server."))
        self.assertEqual(finding["next_step"], "Nothing to do.")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["facts"]["sandbox_process_check"]["broker_running"])
        self.assertNotIn("kill PID", diagnostics.render(report))

    async def test_disabled_is_shown_and_never_scans(self):
        with self.case.path.open("a", encoding="utf-8") as stream:
            stream.write("sandbox_process_check = false\n")
        report = await self.report()
        finding = next(item for item in report["findings"] if item["code"] == "process_check_disabled")
        self.assertEqual(finding["message"], "The leftover-process check is turned off in the settings file.")
        self.assertEqual(finding["severity"], "info")
        self.assertFalse(report["facts"]["sandbox_process_check"]["enabled"])
        self.assertIn("Check for leftover sandbox processes (sandbox_process_check): false", diagnostics.render(report))
        self.scanner.assert_not_called()

    async def test_unavailable_reasons_have_plain_messages_and_no_warning(self):
        for reason, code, message in (
                ("not_linux", "process_check_not_linux", "The leftover-process check is available only on Linux."),
                ("proc_unreadable", "process_check_unreadable", "The Linux process check could not read /proc.")):
            with self.subTest(reason=reason):
                self.scanner.side_effect = None
                self.scanner.return_value = {"available": False, "reason": reason, "processes": [], "truncated": False, "scanned": 0}
                report = await self.report()
                finding = next(item for item in report["findings"] if item["code"] == code)
                self.assertEqual(finding["message"], message)
                self.assertEqual(finding["severity"], "info")
                self.assertEqual(report["exit_code"], 0)

    async def test_unreadable_database_does_not_claim_project_folders_were_checked(self):
        path = self.case.state / "state.sqlite3"
        path.write_bytes(b"corrupt database")
        report = await self.report()
        finding = next(item for item in report["findings"] if item["code"] == "process_check_unchecked")
        self.assertEqual(finding["message"], "The Linux process check is available, but saved project folders could not be read.")
        self.assertFalse(report["facts"]["sandbox_process_check"]["projects_known"])
        self.scanner.assert_called_once_with([])

    async def test_changed_process_setting_is_named_in_server_mismatch(self):
        self.case.health_mock.side_effect = None
        self.case.health_mock.return_value = self.case.healthy()
        with self.case.path.open("a", encoding="utf-8") as stream:
            stream.write("sandbox_process_check = false\n")
        report = await self.report()
        self.assertEqual(report["facts"]["broker"]["different_fingerprint_inputs"], ["sandbox_process_check"])
        finding = next(item for item in report["findings"] if item["code"] == "broker_identity_mismatch")
        self.assertIn("The differences are sandbox_process_check.", finding["message"])


if __name__ == "__main__":
    unittest.main()
