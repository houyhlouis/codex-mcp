"""Plain doctor wording without changing its structured report contract."""

import copy
import json
import os
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

from codex_local_mcp import diagnostics, store as storage
from codex_local_mcp.contract import codex_compatibility
from codex_local_mcp.security import BridgeError
from codex_local_mcp.store import Store
from tests.helpers import write_config
from tests import test_diagnostics as doctor


EXPECTED_STEPS = {
    "bridge_version": "Nothing to do.",
    "python_version": "Install Python 3.11 or newer and start the server with it.",
    "codex_tested": "Nothing to do.",
    "codex_untested": "This Codex version is newer than the ones tested. It normally works. If something behaves oddly, try a tested version.",
    "codex_unsupported": "Update Codex (codex update) to a supported version before starting tasks.",
    "codex_version_unchecked": "Run the Codex program with --version in this terminal to see why it does not answer.",
    "binary_resolved": "Nothing to do.",
    "binary_missing": "Install Codex, or set codex_binary in the settings file to the full path of the Codex program.",
    "binary_not_executable": "Set codex_binary to the Codex program itself: an executable file, or a Python launcher that can be read.",
    "binary_relative": "Set codex_binary to a full path. The background server runs in another folder, so a relative path does not work.",
    "binary_invalid": "Set codex_binary to a plain command name such as codex, or to the full path of the Codex program.",
    "path_ignored": "Empty and relative PATH entries are skipped for safety. Put the folder that contains Codex into PATH with its full path, or set codex_binary.",
    "config_ok": "Nothing to do.",
    "config_invalid": "Correct the settings file. Nothing is started until it is valid.",
    "state_missing": "Nothing to do. The folder is created the first time the server starts.",
    "state_ok": "Nothing to do.",
    "state_unsafe": "Use a state folder that belongs to your account and that nobody else can open, or let the server create the default one.",
    "state_unreadable": "Run doctor as the same user account that runs your MCP client, and check that this account can open the state folder.",
    "endpoint_ok": "Nothing to do.",
    "endpoint_too_long": "Set state_dir in the settings file to a shorter path.",
    "ipc_available": "Nothing to do.",
    "ipc_unavailable": "Local sockets are blocked here. Run doctor in the same environment as your MCP client; a sandbox or container may forbid them.",
    "ipc_unchecked": "Fix the state folder or temporary folder problem above, then run doctor again.",
    "broker_absent": "Nothing to do. The background server starts when it is first needed.",
    "broker_healthy": "Nothing to do.",
    "broker_unreachable": "Check whether the background server is still working (server.py status). If it hangs, stop it with server.py stop.",
    "broker_identity_mismatch": "Stop the old background server with server.py stop, using the same --config it was started with. The next call starts a new one.",
    "broker_environment_mismatch": "Stop the background server and start it again from the environment (CODEX_HOME) you want it to use.",
    "broker_codex_version_mismatch": "Codex was updated while the background server was running. Let active tasks finish, then run server.py stop.",
    "broker_identity_unchecked": "The running background server is from an older version. Stop it with that version's server.py stop.",
    "broker_unchecked": "Fix the state folder problem reported above, then run doctor again.",
    "broker_stale_socket": "A socket file was left behind. Make sure no background server is running for this state folder; the next start replaces it.",
    "broker_stale_lock": "Nothing to do. The lock file is not in use.",
    "broker_start_failed": "Fix the problems reported above, then run doctor --start-broker again.",
    "database_absent": "Nothing to do. The database is created the first time the server starts.",
    "database_ok": "Nothing to do.",
    "database_corrupt": "Do not delete this database. Stop the server and restore a backup into a new state folder (see the documentation on backups).",
    "database_newer": "This database was written by a newer version. Use that version, or restore an older backup into a new state folder.",
    "database_older": "Nothing to do. The database is upgraded automatically at the next start, after a backup has been saved next to it.",
    "database_layout_invalid": "This does not look like an intact Codex MCP database. Check that state_dir points to the right folder. Do not overwrite the file.",
    "database_unchecked": "Fix the state folder problem reported above, then run doctor again.",
    "toolchain_readable": "Nothing to do.",
    "toolchain_unreadable": "Correct toolchain_paths in the settings file: each entry must be the full path of an existing folder you can read.",
    "signin_ok": "Nothing to do.",
    "signin_missing": "Sign in with codex login, then run doctor again.",
    "signin_unchecked": "To check the sign-in as well, run doctor --start-broker. That starts the background server and Codex, but no model.",
    "native_contract": "Nothing to do.",
    "schema_inventory_ok": "Nothing to do.",
    "schema_incompatible": "This Codex version does not offer something the server needs. Install a tested Codex version.",
    "schema_unchecked": "To compare the server's messages with this Codex version, run doctor --schema. No model is used.",
    "schema_wire_ok": "Nothing to do.",
    "schema_wire_unchecked": "The full message check needs a source checkout and the optional jsonschema package.",
    "schema_failed": "Codex could not generate its schema, or an optional development package is missing. See the message above.",
    "probe_cleanup_failed": "A temporary test folder could not be removed. Delete only the folder named in the message, after the check has finished.",
    "process_check_available": "Nothing to do.",
    "process_check_not_linux": "Nothing to do.",
    "process_check_disabled": "Nothing to do.",
    "process_check_unreadable": "Check that /proc is readable, then run doctor again.",
    "process_check_unchecked": "Fix the state folder or database problem reported above, then run doctor again.",
    "sandbox_processes_running": "Nothing to do.",
    "sandbox_processes_untracked": "Stop these processes with kill PID. Nothing else is needed.",
}
FORBIDDEN = r"\b(?:bridge|broker frontend|identity inputs|operator-owned|native contract|fingerprint)\b"


class DoctorLayoutTest(unittest.TestCase):
    def report(self, findings=(), **facts):
        severity = max((diagnostics.LEVEL[item["severity"]] for item in findings), default=0)
        return {"format": 1, "bridge_version": "0.2.0", "summary": "error" if severity == 2 else "warning" if severity else "ok",
            "exit_code": severity, "facts": facts, "findings": list(findings)}

    def finding(self, code, severity, message):
        return {"code": code, "severity": severity, "message": message, "next_step": diagnostics.STEPS[code]}

    def test_every_next_step_equals_the_owners_exact_text(self):
        self.assertEqual(diagnostics.STEPS, EXPECTED_STEPS)

    def test_json_shape_and_severities_are_unchanged_with_the_new_code_registered(self):
        self.assertEqual(diagnostics.LEVEL, {"ok": 0, "info": 0, "warning": 1, "error": 2})
        self.assertEqual(diagnostics.REPORT_SCHEMA, {"type": "object",
            "required": ["format", "bridge_version", "summary", "exit_code", "facts", "findings"],
            "additionalProperties": False, "properties": {
                "format": {"const": 1}, "bridge_version": {"type": "string"},
                "summary": {"enum": ["ok", "info", "warning", "error"]}, "exit_code": {"enum": [0, 1, 2]},
                "facts": {"type": "object"}, "findings": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["severity", "code", "message", "next_step"], "properties": {
                        "severity": {"enum": ["ok", "info", "warning", "error"]}, "code": {"enum": list(EXPECTED_STEPS)},
                        "message": {"type": "string"}, "next_step": {"type": "string"}}}}}})

    def test_first_two_lines_counts_pluralisation_and_explicit_start(self):
        warning = self.finding("path_ignored", "warning", "PATH has one relative entry.")
        problem = self.finding("binary_missing", "error", "Codex was not found.")
        for findings, summary in (([], "everything looks fine"), ([warning], "1 warning"),
                ([warning, warning], "2 warnings"), ([problem], "1 problem, 0 warnings"),
                ([problem, warning], "1 problem, 1 warning"), ([problem, problem, warning], "2 problems, 1 warning")):
            with self.subTest(summary=summary):
                report = self.report(findings)
                diagnostics.validate_report(report)
                lines = diagnostics.render(report).splitlines()
                self.assertEqual(lines[:2], ["Codex MCP doctor 0.2.0: " + summary, "This check uses no model and starts nothing."])
        self.assertEqual(diagnostics.render(self.report(probes={"start_broker": True})).splitlines()[1],
            "This check uses no model. It started the background server and Codex because of --start-broker.")

    def test_sections_order_remediation_and_passed_messages(self):
        report = self.report([
            self.finding("python_version", "ok", "Python 3.11.18."),
            self.finding("native_contract", "info", "The server checks each task's permissions."),
            self.finding("binary_missing", "error", "No Codex program was found for 'codex'."),
            self.finding("path_ignored", "warning", "PATH contains 1 empty entries and 0 relative entries. They were skipped."),
            self.finding("state_missing", "info", "The state folder /fixture/state does not exist."),
        ])
        actual = diagnostics.render(report)
        expected = ["Codex MCP doctor 0.2.0: 1 problem, 1 warning", "This check uses no model and starts nothing.", "", "Problems",
            report["findings"][2]["message"], "  What to do: " + EXPECTED_STEPS["binary_missing"], "", "Warnings",
            report["findings"][3]["message"], "  What to do: " + EXPECTED_STEPS["path_ignored"], "", "Notes",
            report["findings"][1]["message"], report["findings"][4]["message"],
            "", "Passed", "Python 3.11.18.", "", "Details", "Settings file: none"]
        self.assertEqual(actual, "\n".join(expected))
        self.assertNotIn(EXPECTED_STEPS["python_version"], actual)  # Passed entries never have instructions.
        self.assertNotIn("What to do: Nothing to do.\n", actual)
        self.assertNotRegex(actual, FORBIDDEN)
        self.assertNotRegex(actual, r"(?m)^\[(ok|info|warning|error)\]")
        for code in diagnostics.STEPS:
            self.assertNotIn(code, actual)

    def test_details_are_labelled_known_values_without_private_or_unknown_fields(self):
        hidden = "DO_NOT_PRINT_88751"
        report = self.report(config_path="/fixture/settings.toml", state_dir="/fixture/state", endpoint={"path": "/fixture/state/server.sock"},
            executable={"configured": "codex", "resolved": "/fixture/bin/codex", "lookup": "absolute_PATH", "path_directory": "/fixture/bin"},
            environment_identity={"codex_home": "/fixture/home", "profile_selection": hidden, "profile_selection_digest": hidden},
            minimum_codex_version="0.167.0", tested_codex_versions=["0.167.0"], supported_mcp_revisions=["2025-11-25", "2026-07-28"],
            database={"schema_version": 8, "supported_schema": 8, "bytes": 213804646, "backups": [{}, {}],
                "counts": {"tasks": 2, "requests": 3, "open_claims": 0, "events": 40}},
            fingerprint=hidden, fingerprint_inputs={"max_tasks": 4, "approval_relay": False, "allow_external_tools": True, "sandbox_process_check": True, "untrusted": hidden},
            broker={"pid": hidden, "message": hidden}, secret=hidden)
        before = copy.deepcopy(report)
        with patch("codex_local_mcp.diagnostics.IS_WINDOWS", False):
            details = diagnostics.render(report).split("\nDetails\n")[1]
        self.assertEqual(details.splitlines(), ["Settings file: /fixture/settings.toml", "State folder: /fixture/state",
            "Socket path: /fixture/state/server.sock", "Codex setting (codex_binary): 'codex'", "Codex program: /fixture/bin/codex",
            "Found through: PATH in /fixture/bin", "CODEX_HOME: /fixture/home", "Minimum Codex version: 0.167.0",
            "Tested Codex versions: 0.167.0", "MCP versions: 2025-11-25, 2026-07-28", "Database layout version: 8 (supported: 8)",
            "Database size: 203.9 MiB", "Database backups: 2", "Tasks: 2", "Requests: 3", "Open workspace claims: 0", "Events: 40",
            "Maximum simultaneous tasks (max_tasks): 4", "Approval relay (approval_relay): false", "External tools (allow_external_tools): true",
            "Check for leftover sandbox processes (sandbox_process_check): true"])
        self.assertNotIn(hidden, details)
        self.assertNotRegex(details, FORBIDDEN)
        self.assertEqual(report, before)
        self.assertIn(hidden, json.dumps(report))  # Rendering leaves machine-readable facts intact.
        unknown = diagnostics.render(self.report(state_dir=None, endpoint={"path": ""}, database={"counts": {"open_claims": None}}))
        self.assertEqual(unknown.split("\nDetails\n")[1], "Settings file: none")
        self.assertNotIn("could not check", unknown)
        self.assertNotIn("None", unknown)

    def test_windows_pipe_and_full_path_details_and_readable_size_units(self):
        report = self.report(endpoint={"path": r"\\.\pipe\fixture"}, executable={"configured": r"C:\Tools\codex.exe",
            "resolved": r"C:\Tools\codex.exe", "lookup": "absolute"})
        with patch("codex_local_mcp.diagnostics.IS_WINDOWS", True):
            rendered = diagnostics.render(report)
        self.assertIn(r"Pipe path: \\.\pipe\fixture", rendered)
        self.assertIn("Found through: full path", rendered)
        for size, text in ((0, "0 bytes"), (1023, "1023 bytes"), (1536, "1.5 KiB"), (1024 ** 3, "1.0 GiB")):
            self.assertEqual(diagnostics.readable_size(size), text)


class DoctorWordingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = doctor.DoctorTest()
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    def check_words(self, report):
        diagnostics.validate_report(report)
        actual = diagnostics.render(report)
        self.assertNotRegex(actual.lower(), FORBIDDEN)
        self.assertNotRegex(actual, r"(?m)^\[(ok|info|warning|error)\]")
        for finding in report["findings"]:
            self.assertTrue(finding["message"].endswith("."), finding)
            self.assertEqual(finding["next_step"], EXPECTED_STEPS[finding["code"]])
            self.assertNotIn(finding["code"], actual)
            if finding["next_step"].startswith("Nothing to do."):
                self.assertIn(finding["severity"], ("ok", "info"), finding)
        return actual

    async def test_human_report_avoids_jargon_and_untrusted_error_text(self):
        self.check_words(await self.case.report())
        hidden = "REMOTE_ERROR_MUST_NOT_APPEAR_88751"
        text = hidden + " bridge broker frontend identity inputs operator-owned native contract fingerprint"
        for code in ("broker_identity_mismatch", "broker_identity_unchecked"):
            with self.subTest(code=code):
                self.case.health_mock.side_effect = BridgeError(text, code=code)
                report = await self.case.report()
                self.assertNotIn(hidden, json.dumps(report))
                self.check_words(report)
        with patch("codex_local_mcp.diagnostics.load_config", side_effect=BridgeError(text)):
            report = await self.case.report()
        self.assertNotIn(hidden, json.dumps(report))
        self.check_words(report)

    async def test_mismatches_name_only_local_fields_and_keep_json_facts(self):
        health = self.case.healthy()
        health["fingerprint_inputs"].update(bridge_version="older", max_tasks=9)
        health["environment_identity"].update(codex_home=str(self.case.base / "other-home"),
            profile_selection="REMOTE_PROFILE_MUST_NOT_APPEAR", profile_selection_digest="REMOTE_DIGEST_MUST_NOT_APPEAR")
        self.case.health_mock.side_effect = None
        self.case.health_mock.return_value = health
        report = await self.case.report()
        actual = self.check_words(report)
        self.assertIn("The differences are server version, max_tasks.", actual)
        self.assertIn("The differences are CODEX_HOME, Codex profile.", actual)
        self.assertEqual(report["facts"]["broker"]["different_fingerprint_inputs"], ["bridge_version", "max_tasks"])
        self.assertIn(report["facts"]["fingerprint"], json.dumps(report))
        self.assertNotIn(report["facts"]["fingerprint"], actual)
        self.assertNotIn("REMOTE_PROFILE", actual)
        self.assertNotIn("REMOTE_DIGEST", actual)

    async def test_environment_tokens_and_account_fields_stay_out_of_both_outputs(self):
        hidden = "SEED_SECRET_MUST_NOT_APPEAR_88751"
        self.case.health_mock.side_effect = None
        self.case.health_mock.return_value = self.case.healthy(backend=True)
        self.case.status_mock.return_value = {"signed_in": False, "account": {"token": hidden}, "unexpected": hidden}
        with patch.dict(os.environ, {"OPENAI_API_KEY": hidden, "GITHUB_TOKEN": hidden, "CODEX_PROFILE": hidden}):
            report = await self.case.report()
        actual = self.check_words(report)
        self.assertNotIn(hidden, actual)
        self.assertNotIn(hidden, json.dumps(report))
        self.assertEqual(report["format"], 1)
        self.assertEqual(set(report), {"format", "bridge_version", "summary", "exit_code", "facts", "findings"})
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("Codex reports that you are not signed in.", actual)

    async def test_connection_timeout_has_a_plain_message_with_the_same_severity(self):
        self.case.ipc_mock.side_effect = TimeoutError()
        report = await self.case.report()
        actual = self.check_words(report)
        finding = next(item for item in report["findings"] if item["code"] == "ipc_unavailable")
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(finding["message"], "The local connection check timed out.")
        self.assertNotIn("TimeoutError", actual)

    async def test_fresh_installation_has_no_repairs_for_an_absent_database_or_server(self):
        state = self.case.base / "fresh-state"
        write_config(self.case.path, state, self.case.binary)
        with patch.dict(os.environ, {"PATH": str(self.case.base)}):
            report = await self.case.report()
        actual = self.check_words(report)
        self.assertEqual(report["exit_code"], 0)
        self.assertNotIn("\nProblems\n", actual)
        self.assertNotIn("\nWarnings\n", actual)
        by_code = {item["code"]: item for item in report["findings"]}
        for code, message in (("database_absent", "There is no database yet."), ("broker_absent", "No background server is running.")):
            self.assertEqual(by_code[code]["severity"], "info")
            self.assertEqual(by_code[code]["message"], message)
            self.assertEqual(by_code[code]["next_step"], EXPECTED_STEPS[code])
            lines = actual.splitlines()
            self.assertFalse(lines[lines.index(message) + 1].startswith("  What to do:"))
        for code in ("database_unchecked", "broker_identity_unchecked", "broker_unchecked"):
            self.assertNotIn(code, by_code)
        self.assertFalse(state.exists())
        self.case.health_mock.assert_not_awaited()
        self.case.start_mock.assert_not_awaited()

    async def test_automatic_database_upgrade_is_a_note_and_preserves_layout_facts(self):
        for version in (0, storage.SCHEMA_VERSION - 1):
            with self.subTest(version=version):
                state = self.case.base / ("upgrade-state-" + str(version))
                if version == 0:
                    state.mkdir(mode=0o700)
                    path = state / "state.sqlite3"
                    os.close(storage.private_file(path, os.O_CREAT | os.O_RDWR))
                    with closing(sqlite3.connect(path)) as database:
                        for statement in storage.SCHEMA_V1:
                            database.execute(statement)
                        database.commit()
                else:
                    with patch.object(storage, "SCHEMA_VERSION", version), closing(Store(state)):
                        pass
                write_config(self.case.path, state, self.case.binary)
                before = {path.name: path.read_bytes() for path in state.iterdir()}
                with patch.dict(os.environ, {"PATH": str(self.case.base)}):
                    report = await self.case.report()
                actual = self.check_words(report)
                finding = next(item for item in report["findings"] if item["code"] == "database_older")
                facts = report["facts"]["database"]
                message = "The database was written by an older version and will be upgraded automatically at the next start " + f"({diagnostics.readable_size(facts['bytes'])})."
                self.assertEqual(finding, {"code": "database_older", "severity": "info", "message": message,
                    "next_step": EXPECTED_STEPS["database_older"]})
                self.assertEqual(report["exit_code"], 0)
                self.assertEqual(report["summary"], "ok")
                self.assertTrue(actual.splitlines()[0].endswith("everything looks fine"))
                self.assertNotIn("\nProblems\n", actual)
                self.assertNotIn("\nWarnings\n", actual)
                notes = actual.split("\nNotes\n", 1)[1].split("\nPassed\n", 1)[0]
                self.assertIn(message, notes)
                self.assertNotIn("What to do: " + EXPECTED_STEPS["database_older"], actual)
                self.assertEqual(facts["schema_version"], version)
                self.assertEqual(facts["supported_schema"], storage.SCHEMA_VERSION)
                self.assertIn(f"Database layout version: {version} (supported: {storage.SCHEMA_VERSION})", actual)
                self.assertEqual({path.name: path.read_bytes() for path in state.iterdir()}, before)
                self.case.start_mock.assert_not_awaited()

    async def test_unsafe_and_unreadable_state_use_the_matching_codes_and_steps(self):
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                values = {"side_effect": PermissionError(13, "UNTRUSTED_PERMISSION_TEXT")} if unreadable else {
                    "return_value": ({"exists": True, "mode": "0o755"}, False)}
                with patch("codex_local_mcp.diagnostics.inspect_state", **values), \
                        patch("codex_local_mcp.diagnostics.database_health") as database:
                    report = await self.case.report()
                actual = self.check_words(report)
                by_code = {item["code"]: item for item in report["findings"]}
                self.assertEqual(report["exit_code"], 2)
                self.assertIn("state_unreadable" if unreadable else "state_unsafe", by_code)
                for code, message in (("database_unchecked", "The database was not checked because the state folder cannot be used."),
                        ("broker_unchecked", "The background server was not checked because the state folder cannot be used.")):
                    self.assertEqual(by_code[code]["severity"], "info")
                    self.assertEqual(by_code[code]["message"], message)
                    self.assertEqual(by_code[code]["next_step"], "Fix the state folder problem reported above, then run doctor again.")
                self.assertNotIn("broker_identity_unchecked", by_code)
                self.assertNotIn("database_absent", by_code)
                self.assertNotIn("broker_absent", by_code)
                self.assertNotIn("UNTRUSTED_PERMISSION_TEXT", actual)
                database.assert_not_called()
                self.case.health_mock.assert_not_awaited()

    async def test_version_finding_does_not_repeat_the_resolved_program_path(self):
        for version, code, severity, message in (
                ("0.167.0", "codex_tested", "ok", "Codex 0.167.0 is a tested version."),
                ("0.168.0", "codex_untested", "warning", "Codex 0.168.0 has not been tested with this server."),
                ("0.166.0", "codex_unsupported", "error", "Codex 0.166.0 is older than the supported minimum 0.167.0.")):
            with self.subTest(version=version), patch("codex_local_mcp.diagnostics.version_probe", AsyncMock(return_value=codex_compatibility(version))):
                report = await self.case.report()
                finding = next(item for item in report["findings"] if item["code"] == code)
                self.assertEqual(finding["message"], message)
                self.assertEqual(finding["severity"], severity)
                self.assertNotIn(str(self.case.binary), finding["message"])
                self.assertTrue(any(item["code"] == "binary_resolved" and str(self.case.binary) in item["message"] for item in report["findings"]))

    async def test_unreadable_state_is_not_treated_as_missing_for_explicit_start(self):
        original = Path.exists
        def exists(path):
            return False if path == self.case.state else original(path)
        with patch("codex_local_mcp.diagnostics.inspect_state", side_effect=PermissionError(13, "denied")), patch.object(Path, "exists", exists):
            report = await self.case.report(start_broker=True)
        by_code = {item["code"]: item for item in report["findings"]}
        self.assertIn("state_unreadable", by_code)
        self.assertIn("broker_unchecked", by_code)
        self.assertNotIn("broker_absent", by_code)
        self.case.start_mock.assert_not_awaited()
        self.case.health_mock.assert_not_awaited()

    async def test_unchecked_connection_messages_name_the_actual_path_or_temporary_problem(self):
        endpoints = (("x" * 300, "too long"), (None, "unavailable"))
        for endpoint, reason in endpoints:
            with self.subTest(reason=reason):
                values = {"side_effect": PermissionError(1, "UNTRUSTED_ENDPOINT_TEXT")} if endpoint is None else {"return_value": endpoint}
                with patch.object(diagnostics.Config, "endpoint", new_callable=PropertyMock, **values):
                    report = await self.case.report()
                by_code = {item["code"]: item for item in report["findings"]}
                self.assertEqual(by_code["ipc_unchecked"]["message"], f"Local connections were not checked because the connection path is {reason}.")
                self.assertEqual(by_code["broker_unchecked"]["message"], f"The background server was not checked because the connection path is {reason}.")
                self.assertNotIn("broker_identity_unchecked", by_code)
                self.assertNotIn("UNTRUSTED_ENDPOINT_TEXT", json.dumps(report))
                self.case.health_mock.assert_not_awaited()
        with patch("codex_local_mcp.diagnostics.tempfile.TemporaryDirectory", side_effect=PermissionError(13, "denied")):
            report = await self.case.report()
        self.assertEqual(next(item for item in report["findings"] if item["code"] == "ipc_unchecked")["message"],
            "Local connections were not checked because a private temporary folder is unavailable.")

    async def test_only_missing_server_metadata_uses_the_older_server_code(self):
        health = self.case.healthy()
        self.case.health_mock.side_effect = None
        self.case.health_mock.return_value = health
        with patch("codex_local_mcp.diagnostics.load_config", side_effect=BridgeError("private settings failure")):
            report = await self.case.report()
        by_code = {item["code"]: item for item in report["findings"]}
        self.assertNotIn("broker_identity_unchecked", by_code)
        self.assertEqual(by_code["broker_unchecked"]["message"],
            "The background server's settings were not compared because the settings file is invalid.")
        del health["fingerprint_inputs"]
        report = await self.case.report()
        by_code = {item["code"]: item for item in report["findings"]}
        self.assertEqual(by_code["broker_identity_unchecked"]["message"], "The background server cannot report how it was started.")
        self.assertNotIn("broker_unchecked", by_code)

    async def test_database_busy_reports_its_cause_without_echoing_sqlite_text(self):
        store = Store(self.case.state)
        store.close()
        error = sqlite3.OperationalError("PRIVATE_SQLITE_ERROR_TEXT")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        findings = []
        with patch("codex_local_mcp.diagnostics.sqlite3.connect", side_effect=error):
            diagnostics.database_health(self.case.state, lambda *item: findings.append(item), {})
        self.assertEqual(findings, [("warning", "database_unchecked", f"The database at {self.case.state / 'state.sqlite3'} is busy.")])


if __name__ == "__main__":
    unittest.main()
