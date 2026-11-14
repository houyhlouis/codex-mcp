"""Check live-script failure handling without importing or running live code."""

import ast
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import signal
import sys
import time
import tomllib
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_local_mcp.config import load_config
from codex_local_mcp.security import BridgeError, project_path, read_config_file
from codex_local_mcp.service import Service, WorkspaceBusy
from codex_local_mcp.tools import dispatch
from codex_local_mcp.retention import offline
from codex_local_mcp.store import Store
from tests.helpers import ReconciliationClock, write_config
from tests.test_service import ServiceHarness
from tests.test_procscan_live import SIGNALS


class LiveScriptChecksTest(unittest.TestCase):
    def scripts(self):
        for path in sorted((Path(__file__).resolve().parents[1] / "scripts").glob("live_*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"))

    def test_failed_checks_exit_nonzero_even_with_python_optimization(self):
        for path, tree in self.scripts():
            self.assertFalse(any(isinstance(node, ast.Assert) for node in ast.walk(tree)), path.name)
            helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "check")
            # Compile only the pure check helper, never a live script or run().
            code = ast.unparse(helper) + '\ncheck(False, "offline failed check")\n'
            for optimize in (False, True):
                with self.subTest(script=path.name, optimize=optimize):
                    result = subprocess.run([sys.executable, *(["-O"] if optimize else []), "-c", code],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"offline failed check", result.stderr)

    def test_entrypoints_propagate_failures_from_an_offline_stub(self):
        for path, tree in self.scripts():
            entry = next(node for node in tree.body if isinstance(node, ast.If) and
                         ast.unparse(node.test) == "__name__ == '__main__'")
            # The stub raises before any live imports, configuration, or native I/O.
            code = ('import argparse, asyncio\nasync def run(*args):\n'
                    '    raise RuntimeError("offline entry failure")\n' + ast.unparse(entry))
            with self.subTest(script=path.name):
                result = subprocess.run([sys.executable, "-c", code, "--config", "unused", "--workdir", "unused"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"offline entry failure", result.stderr)

    def test_controls_failure_prints_and_records_the_blocking_claims(self):
        path, tree = next((path, tree) for path, tree in self.scripts() if path.name == "live_controls.py")
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "report_failure")
        namespace = {"json": json}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(path), "exec"), namespace)
        claims = [{"task_id": "interrupted-task", "kind": "background", "state": "active"}]
        service = SimpleNamespace(store=SimpleNamespace(claims=lambda: claims), safe_payload=Service.safe_payload)
        report, output = {}, io.StringIO()
        with contextlib.redirect_stdout(output):
            namespace["report_failure"](service, report, RuntimeError("Another task is active in this project"))
        self.assertFalse(report["passed"])
        self.assertEqual(report["blocking_workspace_claims"], claims)
        self.assertIn("Blocking workspace claims:", output.getvalue())
        self.assertIn("interrupted-task", output.getvalue())
        self.assertIn("background", output.getvalue())


class LiveControlsRecoveryTest(ServiceHarness):
    """Only the extracted helper runs, with the ordinary fake backend."""
    automatic_reconciliation = True

    async def asyncSetUp(self):
        await super().asyncSetUp()
        path = Path(__file__).resolve().parents[1] / "scripts" / "live_controls.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ("check", "record_sandbox_processes", "start_after_interrupt", "disposable_process", "stop_sandbox_processes")]
        positive = {"available": True, "reason": "ok", "scanned": 1, "truncated": False,
            "processes": [{"pid": 1234, "start_time": 99, "policy_cwd": "/fixture", "command_cwd": "/fixture", "project": "/fixture"}]}
        self.process_scan = Mock(side_effect=[positive, {**positive, "processes": []}])
        async def scan(project):
            return self.process_scan([project])
        namespace = {"asyncio": asyncio, "json": json, "dispatch": dispatch, "WorkspaceBusy": WorkspaceBusy,
            "sys": SimpleNamespace(platform="linux"), "sandbox_scan": scan,
            "signal": SimpleNamespace(**vars(SIGNALS), pidfd_send_signal=Mock()),
            "os": SimpleNamespace(path=os.path, sep=os.sep, pidfd_open=Mock(return_value=50), close=Mock()), "process_grace": AsyncMock()}
        exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), "exec"), namespace)
        self.live_namespace = namespace
        self.start_after_interrupt = namespace["start_after_interrupt"]

    async def interrupted(self, background=True):
        task = await self.start_task("hold_command_background" if background else "hold_command")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: any(
                row["kind"] == "item/started" and row["data"].get("item", {}).get("id") == "running-command"
                for row in self.store.events(0, 200, task_id=task["id"])["events"])), 5)
        await self.service.interrupt(task["id"], task["turn_id"])
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(
                lambda: self.service.task(task["id"]).get("native_status") == {"type": "idle"}), 5)
        await self.reconciliation_idle()
        return task

    def arguments(self, task):
        return {"workdir": task["project"], "prompt": "hold", "request_id": "second-root"}

    async def test_background_refusal_is_reported_and_cleanup_admits_a_second_root(self):
        task = await self.interrupted()
        report = {"checks": []}
        with contextlib.redirect_stdout(io.StringIO()):
            following = await self.start_after_interrupt(self.service, task, self.arguments(task), report)
        self.assertNotEqual(following["id"], task["id"])
        self.assertEqual(report["background_release_path"], "terminals_stopped")
        self.assertEqual(report["checks"], ["background execution keeps the project reserved",
            "listed interrupted task background terminals", "requested background terminal termination and cleanup",
            "workspace released after background cleanup", "second root admitted after background cleanup"])
        self.assertEqual(report["background_terminals"][0]["itemId"], "running-command")
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_already_released_path_does_not_stop_terminals(self):
        task = await self.interrupted(background=False)
        original, mutations = self.service.backend.call, []
        async def recording(method, params, **kwargs):
            if method in ("thread/backgroundTerminals/terminate", "thread/backgroundTerminals/clean"):
                mutations.append(method)
            return await original(method, params, **kwargs)
        report = {"checks": []}
        with patch.object(self.service.backend, "call", recording), contextlib.redirect_stdout(io.StringIO()):
            await self.start_after_interrupt(self.service, task, self.arguments(task), report)
        self.assertEqual(report["background_release_path"], "already_released")
        self.assertEqual(report["checks"], ["workspace already released after interruption",
                                          "second root admitted without background cleanup"])
        self.assertEqual(mutations, [])

    async def test_linux_leftovers_hold_refuse_then_are_stopped_and_admit_the_boundary_task(self):
        task = await self.interrupted()
        result = {"available": True, "reason": "ok", "processes": [{"pid": 1234, "start_time": 99,
            "policy_cwd": task["project"], "command_cwd": task["project"], "project": task["project"]}], "truncated": False, "scanned": 1}
        self.process_scan.side_effect = None
        self.process_scan.return_value = result
        self.service.config = replace(self.service.config, sandbox_process_check=True)
        async def scan(projects):
            return self.process_scan.return_value
        self.service.process_scanner = scan
        clock = ReconciliationClock(self.service)
        def killed(handle, number, info, flags):
            self.assertEqual((handle, number, info, flags), (50, signal.SIGTERM, None, 0))
            self.process_scan.return_value = {**result, "processes": []}
        self.live_namespace["signal"].pidfd_send_signal.side_effect = killed
        async def grace():
            clock.advance()
            await self.reconciliation_idle()
        self.live_namespace["process_grace"] = grace
        report, output = {"checks": []}, io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(self.service, "reconciliation_timer", clock.later):
            following = await self.start_after_interrupt(self.service, task, self.arguments(task), report)
        self.assertNotEqual(following["id"], task["id"])
        self.assertEqual(report["background_release_path"], "sandbox_processes_stopped")
        self.assertEqual(report["sandbox_processes_running"], result)
        self.assertEqual(report["sandbox_processes_after_cleanup"], result)
        self.assertIn("PID 1234", output.getvalue())
        self.assertIn("second root refused while sandbox processes remain", report["checks"])
        self.assertEqual(report["sandbox_process_signals"][0]["result"], "sent")
        self.assertEqual(len(self.store.all("tasks")), 2)

    async def test_goal_refusal_is_not_treated_as_expected_background_work(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.goal(task["id"], "set", objective="Continue", status="active", user_requested=True)
        await self.reconciliation_idle()
        report = {"checks": []}
        with self.assertRaisesRegex(WorkspaceBusy, "codex_goal"):
            await self.start_after_interrupt(self.service, task, self.arguments(task), report)
        self.assertEqual(report["checks"], [])
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_missing_pidfd_support_keeps_processes_and_reports_boundary_skip(self):
        task = await self.interrupted()
        result = {"available": True, "reason": "ok", "processes": [{"pid": 1234, "start_time": 99,
            "policy_cwd": task["project"], "command_cwd": task["project"], "project": task["project"]}], "truncated": False, "scanned": 1}
        self.process_scan.side_effect = None
        self.process_scan.return_value = result
        self.service.config = replace(self.service.config, sandbox_process_check=True)
        async def scan(projects):
            return result
        self.service.process_scanner = scan
        self.live_namespace["os"].pidfd_open = None
        report, output = {"checks": []}, io.StringIO()
        clock = ReconciliationClock(self.service)
        with contextlib.redirect_stdout(output), patch.object(self.service, "reconciliation_timer", clock.later):
            self.assertIsNone(await self.start_after_interrupt(self.service, task, self.arguments(task), report))
        self.assertEqual(report["sandbox_cleanup_pids"], [1234])
        self.assertIn("SKIPPED:", output.getvalue())
        self.assertEqual(report["boundary_check_skipped"], "Codex sandbox processes remain after native terminal cleanup. Stop the listed PIDs and rerun the check.")
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])
        self.live_namespace["signal"].pidfd_send_signal.assert_not_called()
        self.live_namespace["process_grace"].assert_not_awaited()
        self.assertEqual(len(self.store.all("tasks")), 1)

    async def test_cleanup_timeout_reports_remaining_claims_and_does_not_retry_start(self):
        task = await self.interrupted()
        clock = ReconciliationClock(self.service)
        original = self.service.backend.call
        async def not_stopped(method, params, **kwargs):
            if method == "thread/backgroundTerminals/terminate":
                return {"terminated": False}
            if method == "thread/backgroundTerminals/clean":
                return {}
            return await original(method, params, **kwargs)
        report, output = {"checks": []}, io.StringIO()
        with patch.object(self.service, "reconciliation_timer", clock.later), patch.object(
                self.service.backend, "call", not_stopped), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "Workspace remains reserved after stopping background terminals"):
                await self.start_after_interrupt(self.service, task, self.arguments(task), report, release_seconds=0)
        self.assertTrue(report["remaining_workspace_claims"])
        self.assertIn("Remaining workspace claims:", output.getvalue())
        self.assertEqual(len(self.store.all("tasks")), 1)


def supervision_helpers():
    """Compile helpers only; never import the live module or execute run()."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_supervision.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helpers = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "run"]
    namespace = {"asyncio": asyncio, "base64": base64, "hashlib": hashlib, "json": json,
        "contextmanager": contextlib.contextmanager, "offline": offline,
        "os": os, "Path": Path, "time": time, "tomllib": tomllib, "load_config": load_config,
        "project_path": project_path, "read_config_file": read_config_file, "dispatch": dispatch,
        "BridgeError": BridgeError, "TURN_SECONDS": 180, "ARTIFACT_BYTES": 32 * 1024 * 1024,
        "DETAIL_BYTES": 8 * 1024 * 1024, "TERMINAL": frozenset({"completed", "failed", "interrupted"})}
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class LiveSupervisionStaticTest(unittest.TestCase):
    def test_supervision_uses_outcomes_and_never_claims_human_authority_or_stops_a_broker(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "live_supervision.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        for call in calls:
            self.assertFalse(any(k.arg == "human" and ast.literal_eval(k.value) is True for k in call.keywords if k.arg == "human"))
            self.assertNotIn(ast.unparse(call.func), ("asyncio.sleep", "time.sleep", "subprocess.run", "subprocess.Popen"))
        events = [call for call in calls if ast.unparse(call.func) == "dispatch" and len(call.args) > 1
                  and isinstance(call.args[1], ast.Constant) and call.args[1].value == "codex_events"]
        self.assertEqual(len(events), 1)
        helper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "follow_outcome")
        self.assertIn("'view': 'outcome'", ast.unparse(helper))
        self.assertIn("cursors = page['cursors']", ast.unparse(helper))

    def test_missing_and_default_state_are_refused_before_config_or_state_is_opened(self):
        helpers = supervision_helpers()
        load = helpers["load_config"] = Mock(side_effect=AssertionError("Must not load default state"))
        default = Path(__file__).resolve().parent / "synthetic-default-state"
        helpers["default_state_dir"] = lambda: default
        for data in ({}, {"state_dir": str(default)}):
            helpers["read_config_file"] = lambda path: "" if not data else "state_dir = " + json.dumps(str(default))
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, "explicit absolute state_dir|refuses the default"):
                helpers["isolated_inputs"]("unused.toml", "unused-project")
        load.assert_not_called()


class LiveSupervisionHelpersTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.helpers = supervision_helpers()

    async def test_empty_isolated_state_is_required_without_touching_its_contents(self):
        self.helpers["default_state_dir"] = lambda: self.base / "unused-default-state"
        project = self.root / "disposable"
        project.mkdir()
        with self.assertRaisesRegex(RuntimeError, "empty state directory"):
            self.helpers["isolated_inputs"](str(self.config_file), str(project))
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM schema_migrations").fetchone())

    async def test_competing_preflights_cannot_both_open_the_database(self):
        config_path = self.base / "fresh-config.toml"
        state = self.base / "fresh-state"
        project = self.root / "empty"
        project.mkdir()
        write_config(config_path, state, self.cfg.codex)
        self.helpers["default_state_dir"] = lambda: self.base / "unused-default"
        first, _ = self.helpers["isolated_inputs"](str(config_path), str(project))
        second, _ = self.helpers["isolated_inputs"](str(config_path), str(project))
        opened = []
        with self.helpers["isolated_state"](first):
            with self.assertRaisesRegex(RuntimeError, "another process holds its state lock"):
                with self.helpers["isolated_state"](second):
                    opened.append("second")
                    with contextlib.closing(Store(state)):
                        pass
            opened.append("first")
            with contextlib.closing(Store(state)):
                pass
        self.assertEqual(opened, ["first"])
        with self.assertRaisesRegex(RuntimeError, "empty state directory"):
            with self.helpers["isolated_state"](first):
                self.fail("Nonempty state must be refused under the lock")

    async def test_typed_parent_round_trip_is_supervised_only_with_outcome_cursors(self):
        original = self.service.backend.on_message
        async def structured(message):
            if message.get("method") == "item/tool/call":
                message = {**message, "params": {**message["params"], "arguments": {**message["params"]["arguments"],
                    "envelope": {"version": 1, "id": "marker", "options": [{"id": "alpha", "label": "alpha"}], "ttl_seconds": 300}}}}
            await original(message)
        observe = AsyncMock(wraps=self.service.events)
        with patch.object(self.service.backend, "on_message", structured), patch.object(self.service, "events", observe):
            task = await self.start_task("parent")
            report = {"checks": []}
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = await asyncio.wait_for(self.helpers["follow_outcome"](self.service, task["id"], report), 10)
        self.assertEqual(outcome["status"], "completed")
        self.assertTrue(report["typed_decision"])
        self.assertEqual(len(report["parent_receipts"]), 1)
        self.assertTrue(any("decision_mismatch" in c["message"] for c in report["checks"]))
        self.assertTrue(all(call.kwargs["view"] == "outcome" for call in observe.call_args_list))
        self.assertGreater(len(observe.call_args_list), 1)
        self.assertTrue(observe.call_args_list[1].kwargs["cursors"])
        result = await self.helpers["turn_result"](self.service, outcome["result_ref"])
        self.assertTrue(result["agent_claims"])
        self.assertEqual(result["observed_commands"][0]["reported"]["exitCode"], 0)

    async def test_legacy_question_and_approval_denial_do_not_fabricate_a_typed_or_human_receipt(self):
        for prompt in ("parent", "approval"):
            with self.subTest(prompt=prompt):
                task = await self.start_task(prompt, prompt, prompt)
                report = {"checks": []}
                with contextlib.redirect_stdout(io.StringIO()):
                    await asyncio.wait_for(self.helpers["follow_outcome"](self.service, task["id"], report), 10)
                self.assertNotIn("typed_decision", report)
                receipt = self.service.get(task["id"], view="permissions")["decision_receipts"][0]
                self.assertEqual(receipt["decision_source"], "main_agent" if prompt == "parent" else "main_agent_denial")

    async def test_complete_artifact_read_checks_each_page_and_final_size_and_hash(self):
        data = "small multibyte artifact: é漢".encode()
        reference = {"artifact_id": "artifact", "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        chunks = [data[:12], data[12:]]
        async def page(service, name, args):
            offset = args["offset"]
            content = chunks[0] if offset == 0 else chunks[1]
            return {"artifact": {**reference, "offset": offset, "encoding": "base64",
                "data": base64.b64encode(content).decode(), "next_offset": 12 if offset == 0 else None}}
        self.helpers["dispatch"] = page
        self.assertEqual(await self.helpers["artifact_bytes"](None, "task", reference), data)
        chunks[1] = b"x" * len(chunks[1])
        with self.assertRaisesRegex(RuntimeError, "size or SHA256"):
            await self.helpers["artifact_bytes"](None, "task", reference)

    async def test_resting_failure_is_not_reported_as_a_passing_outcome(self):
        self.helpers["dispatch"] = AsyncMock(return_value={"view": "outcome", "events": [], "cursors": {"task": "cursor"},
            "outcomes": [{"id": "task", "status": "failed", "cursor": "cursor"}],
            "pending_requests": [], "pending_requests_total": 0, "pending_requests_next_cursor": None})
        with self.assertRaisesRegex(RuntimeError, "Turn rested with status failed"):
            await self.helpers["follow_outcome"](None, "task", {"checks": []})
