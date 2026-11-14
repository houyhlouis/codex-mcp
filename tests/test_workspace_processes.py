"""Injected process observations; these tests never inspect the host's /proc."""

import asyncio
import copy
import hashlib
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch, PropertyMock

from codex_local_mcp import procscan, workspace_processes, processes
from codex_local_mcp.security import BridgeError
from codex_local_mcp.service import WorkspaceBusy
from codex_local_mcp.tools import dispatch
from tests.helpers import FaultGate, ReconciliationClock, thread_wakeups
from tests.test_service import ServiceHarness


def observation(*pids, available=True, reason="ok", truncated=False):
    return {"available": available, "reason": reason, "processes": [{"pid": pid, "start_time": pid * 100,
        "policy_cwd": None, "command_cwd": None, "project": "injected"} for pid in pids],
        "truncated": truncated, "scanned": len(pids)}


class HelpersProcessSafetyTest(ServiceHarness):
    async def test_helpers_cannot_scan_host_proc(self):
        self.assertFalse(self.cfg.sandbox_process_check)
        with patch.object(procscan, "scan", side_effect=AssertionError("A test read the host /proc")):
            task = await self.start_task()
            await self.until(task["id"], ("completed",))
            await self.service.reconcile_claims(task["id"], explicit=True)
            self.assertEqual((await self.service.status())["os_process_check"]["reason"], "disabled")


class WorkspaceProcessesTest(ServiceHarness):
    automatic_reconciliation = True

    def _tearDownAsyncioRunner(self):
        # Runner.close also shuts down the executor via a worker thread.
        # Its self-pipe needs the same narrow sandbox wake-up as the scan.
        loop = getattr(self, "scanner_loop", None)
        if loop is not None and not loop.is_closed():
            with thread_wakeups(loop):
                return super()._tearDownAsyncioRunner()
        return super()._tearDownAsyncioRunner()

    def new_service(self):
        service = super().new_service()
        service.process_scanner = self.injected_scan
        return service

    async def injected_scan(self, projects):
        self.calls.append((list(projects), threading.get_ident()))
        return copy.deepcopy(self.result)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cfg = replace(self.cfg, sandbox_process_check=True)
        self.service.config = self.cfg
        self.result = observation()
        self.calls = []
        self.clock = ReconciliationClock(self.service)
        timer = patch.object(self.service, "reconciliation_timer", self.clock.later)
        timer.start()
        self.addCleanup(timer.stop)

    async def held(self, prompt="hold_command"):
        task = await self.start_task(prompt)
        self.result = observation(1234)
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.reconciliation_idle()
        return task

    def os_claim(self, task):
        return next((c for c in self.store.claims(task_id=task["id"]) if c["data"].get("source") == "os_process_scan"), None)

    async def test_release_holds_until_processes_end_without_resume(self):
        task = await self.held()
        claim = self.os_claim(task)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["data"], {"source": "os_process_scan", "reason": "sandbox_processes_alive", "pids": [1234],
            "processes_total": 1, "truncated": False,
            "processes_hash": hashlib.sha256(b"[[1234,123400]]").hexdigest()})
        shown = self.service.get(task["id"])
        self.assertEqual(shown["workspace_claims"][0]["message"], "1 Codex sandbox process is still running in this project (PID 1234). "
            "Codex no longer tracks them. Stop them with kill PID; the project is then freed automatically.")
        self.result = observation()
        self.clock.advance()
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_new_unowned_family_is_rejected_before_native_io_and_cached(self):
        self.result = observation(1234)
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Rejected work reached Codex")):
            for _ in range(2):
                with self.assertRaisesRegex(BridgeError, "No task accounts for them.*kill PID") as caught:
                    await self.start_task(key="unowned")
                self.assertEqual(caught.exception.code, "workspace_busy")
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id='unowned'").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])
        self.assertFalse(self.store.all("tasks"))

    async def test_owned_refusal_has_process_advice_without_terminal_advice(self):
        task = await self.held()
        with self.assertRaises(WorkspaceBusy) as caught:
            await self.start_task(key="owned")
        self.assertIn("Codex no longer tracks them. Stop them with kill PID", str(caught.exception))
        self.assertNotIn("codex_terminals", str(caught.exception))
        self.assertEqual(caught.exception.code, "workspace_busy")
        self.assertTrue(self.os_claim(task))

    async def test_unchanged_scan_writes_nothing_and_backoff_is_bounded(self):
        task = await self.held()
        snapshot = (self.store.db.total_changes, self.store.highwater(), self.service.reconciliation_sequence(task["id"]))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("OS-only hold made native calls")):
            for seconds in (5, 10, 20, 40, 80, 160, 300, 300):
                self.assertEqual(self.clock.pending[-1][1], seconds)
                self.clock.advance()
                await self.reconciliation_idle()
                self.assertEqual(snapshot, (self.store.db.total_changes, self.store.highwater(), self.service.reconciliation_sequence(task["id"])))

    async def test_pid_change_and_pid_reuse_advance_once_and_reset_backoff(self):
        task = await self.held()
        self.result = observation(*range(1000, 1012))
        self.clock.advance()
        await self.reconciliation_idle()
        first = self.os_claim(task)
        self.assertEqual(len(first["data"]["pids"]), 10)
        self.assertEqual(first["data"]["processes_total"], 12)
        self.result["processes"][-1]["start_time"] += 1
        self.clock.advance()
        await self.reconciliation_idle()
        second = self.os_claim(task)
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertNotEqual(first["data"]["processes_hash"], second["data"]["processes_hash"])
        self.assertEqual(second["data"]["pids"], first["data"]["pids"])
        self.assertEqual(self.clock.pending[-1][1], 5)

    async def test_admission_scans_fresh_without_waiting_for_backoff(self):
        first = await self.held()
        for _ in range(7):
            self.clock.advance()
            await self.reconciliation_idle()
        self.result = observation()
        following = await self.start_task(key="immediate-admission")
        self.assertNotEqual(first["id"], following["id"])
        self.assertFalse(self.service.get(first["id"])["workspace_reserved"])

    async def test_admission_timeout_keeps_only_the_scan_hold_and_does_not_cancel_its_check(self):
        task = await self.held()
        gate = self.gates["slow-admission-scan"] = FaultGate("slow-admission-scan")
        async def scan(projects):
            await gate.hit()
            return observation()
        self.service.process_scanner = scan
        native_wait_for = asyncio.wait_for
        async def deadline(coroutine, timeout):
            if getattr(coroutine, "cr_code", None) and coroutine.cr_code.co_qualname.endswith("wait_for_reconciliation.<locals>.wait"):
                waiting = asyncio.create_task(coroutine)
                await native_wait_for(gate.entered.wait(), 5)
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                raise TimeoutError("Injected admission deadline")
            return await native_wait_for(coroutine, timeout)
        before = self.os_claim(task)
        with patch("codex_local_mcp.service.asyncio.wait_for", deadline):
            with self.assertRaises(WorkspaceBusy):
                await self.start_task(key="scan-deadline")
        self.assertEqual(self.store.claims(task_id=task["id"]), [before])
        self.assertTrue(self.service.reconciliation_tasks)
        gate.release.set()
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_explicit_resume_scans_fresh_and_reports_zero(self):
        task = await self.held()
        self.result = observation()
        result = await self.service.resume(task["id"])
        self.assertFalse(result["workspace_reserved"])
        self.assertEqual(result["reconciliation"]["os_process_check"]["processes_total"], 0)

    async def test_admission_refreshes_os_hold_without_acknowledging_restart_uncertainty(self):
        task = await self.held()
        processes.hold(self.service, task["id"], {"previous_backend_unverified": True, "acknowledged": False})
        self.result = observation()
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Admission tried to acknowledge native process uncertainty")):
            with self.assertRaisesRegex(WorkspaceBusy, "Commands started before the restart"):
                await self.start_task(key="unknown-plus-os")
        self.assertIsNone(self.os_claim(task))
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertTrue(self.service.get(task["id"])["workspace_reserved"])

    async def test_follow_up_in_own_family_is_not_blocked_by_the_scan(self):
        task = await self.held()
        result = await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "hold", "request_id": "same-family"})
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.store.task_head(task["id"])["workspace_execution_project"], task["project"])
        self.assertNotIn("workspace_execution_project", self.service.get(task["id"], view="compact"))

    async def test_settings_and_metadata_fork_do_not_treat_an_os_hold_as_native_work(self):
        task = await self.held()
        settings = await self.service.settings(task["id"], scope="next", access="read")
        self.assertEqual(settings["access"], "read")
        fork = await self.service.fork(task["id"])
        self.assertIsNone(fork["workspace_execution_project"])
        self.assertTrue(self.os_claim(task))

    async def test_fork_does_not_inherit_own_execution_and_first_work_is_checked(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        fork = await self.service.fork(task["id"])
        self.assertIsNone(fork["workspace_execution_project"])
        self.result = observation(1234)
        with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
            await dispatch(self.service, "codex_send", {"task_id": fork["id"], "prompt": "hold", "request_id": "fork-first-work"})

    async def test_unavailable_and_scan_errors_cannot_clear_a_scan_hold(self):
        task = await self.held()
        for reason in ("proc_unreadable",):
            self.result = observation(available=False, reason=reason)
            await self.service.reconcile_claims(task["id"], explicit=True)
            self.assertEqual(self.os_claim(task)["state"], "uncertain")
            self.assertEqual(self.os_claim(task)["data"]["reason"], "process_check_unavailable")
            self.assertIsNone(self.service.get(task["id"])["process_state"]["os_process_check"]["processes_total"])
        revision = self.os_claim(task)["revision"]
        async def failed(projects):
            raise OSError("Injected proc failure")
        self.service.process_scanner = failed
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertEqual(self.os_claim(task)["revision"], revision)

    async def test_disabled_and_nonlinux_waive_only_os_claims_once_with_an_audit_reason(self):
        task = await self.held()
        with patch.object(self.service, "schedule_reconciliation"), patch.object(
                self.service.backend, "call", side_effect=AssertionError("Policy waiver made a native request")):
            processes.hold(self.service, task["id"], {"previous_backend_unverified": True, "acknowledged": False})
            original = self.service.claim_snapshot(task["id"], "uncertain")
            for reason in ("disabled", "not_linux"):
                self.store.claim(task["id"], "background", "active", workspace_processes.data(observation(1234)))
                self.result = observation(available=False, reason=reason)
                await self.service.reconcile_process_hold(task["id"])
                self.assertIsNone(self.os_claim(task))
                self.assertEqual(self.service.claim_snapshot(task["id"], "uncertain"), original)
                self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
                row = self.service.claim_snapshot(task["id"], "background")
                self.assertEqual(row["data"]["reason"], reason)
                self.assertIn("claim was waived", row["data"]["message"])
                changes = self.store.db.total_changes
                await self.service.reconcile_process_hold(task["id"])
                self.assertEqual(self.store.db.total_changes, changes)
            events = [e for e in self.store.events(0, 200, task_id=task["id"])["events"] if e["kind"] == "bridge/os_process_check_waived"]
            self.assertEqual([e["data"]["reason"] for e in events], ["disabled", "not_linux"])

    async def test_disabling_the_check_releases_an_old_os_hold_at_admission(self):
        first = await self.held()
        self.service.config = replace(self.service.config, sandbox_process_check=False)
        self.service.process_scanner = self.service.scan_projects
        with patch.object(procscan, "scan", side_effect=AssertionError("Disabled policy read /proc")):
            following = await self.start_task(key="disabled-admission")
        self.assertNotEqual(first["id"], following["id"])
        self.assertFalse(self.service.get(first["id"])["workspace_reserved"])

    async def test_unavailable_new_scan_is_a_noop_and_native_claim_is_independent(self):
        self.result = observation(available=False, reason="not_linux")
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        await self.service.goal(task["id"], "set", objective="Keep working", status="active", user_requested=True)
        self.result = observation()
        await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertTrue(any(c["kind"] == "goal" for c in self.store.claims(task_id=task["id"])))

    async def test_restart_warning_acknowledgement_keeps_the_os_hold(self):
        task = await self.start_task("hold_command_background")
        await self.service.close()
        self.service = self.new_service()
        self.result = observation(1234)
        result = await self.service.resume(task["id"])
        self.assertTrue(result["process_state"]["acknowledged"])
        self.assertEqual(result["process_state"]["os_process_check"]["processes_total"], 1)
        self.assertTrue(self.os_claim(task))
        self.assertIn("processes_unverified", result["reconciliation"]["unknown"])
        self.assertEqual(result["reconciliation"]["os_process_check"]["pids"], [1234])

    async def test_os_hold_at_restart_requires_acknowledgement_once_and_not_again_without_new_work(self):
        task = await self.held()
        await self.service.close()
        self.service = self.new_service()
        self.assertFalse(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertTrue(any(c["data"].get("reason") == "processes_unverified" for c in self.store.claims(task_id=task["id"])))
        resumed = await self.service.resume(task["id"])
        self.assertTrue(resumed["process_state"]["acknowledged"])
        self.assertTrue(self.os_claim(task))
        await self.service.close()
        self.service = self.new_service()
        await self.service.reconcile_startup()
        await self.reconciliation_idle()
        self.assertTrue(self.service.get(task["id"])["process_state"]["acknowledged"])
        self.assertTrue(self.os_claim(task))
        self.assertFalse(any(c["data"].get("reason") == "processes_unverified" for c in self.store.claims(task_id=task["id"])))

    async def test_delete_last_member_leaves_no_deleted_claim_but_new_work_is_refused(self):
        task = await self.held()
        await self.service.delete(task["id"], task["thread_id"])
        self.assertFalse(self.store.claims(task_id=task["id"]))
        with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
            await self.start_task(key="after-deletion")

    async def test_stale_scan_cannot_release_after_task_revision_changes(self):
        task = await self.held()
        gate = self.gates["scan"] = FaultGate("scan")
        async def scan(projects):
            await gate.hit()
            return observation()
        with patch.object(self.service, "schedule_reconciliation"):
            self.service.process_scanner = scan
            pending = asyncio.create_task(self.service.reconcile_claims(task["id"], explicit=True))
            await gate.wait()
            self.service.lifecycle.metadata(task["id"], {"name": "changed-during-scan"})
            revision = self.os_claim(task)["revision"]
            gate.release.set()
            await pending
            self.assertEqual(self.os_claim(task)["revision"], revision)

    async def test_backend_generation_change_during_scan_cannot_release(self):
        task = await self.held()
        gate = self.gates["generation-scan"] = FaultGate("generation-scan")
        async def scan(projects):
            await gate.hit()
            return observation()
        self.service.process_scanner = scan
        with patch.object(self.service, "schedule_reconciliation"):
            pending = asyncio.create_task(self.service.reconcile_claims(task["id"], explicit=True))
            await gate.wait()
            self.service.backend.generation += 1
            gate.release.set()
            await pending
        self.assertTrue(self.os_claim(task))

    async def test_status_and_capabilities_report_availability(self):
        task = await self.start_task()
        expected = {"enabled": True, "available": True, "reason": "ok", "checked_at": 1234}
        with patch("codex_local_mcp.service.time.time", return_value=1234):
            self.assertEqual((await self.service.status())["os_process_check"], expected)
            self.assertEqual((await self.service.capabilities(task["id"]))["os_process_check"], expected)

    async def test_running_native_commands_are_not_labelled_as_untracked_leftovers(self):
        task = await self.start_task("hold_command")
        self.result = observation(1234)
        result = await self.service.resume(task["id"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["process_state"]["os_process_check"]["processes_total"], 1)
        self.assertIsNone(self.os_claim(task))
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        await self.reconciliation_idle()
        self.assertTrue(self.os_claim(task))

    async def test_completed_turn_is_held_before_it_exposes_an_unreserved_project(self):
        gate = self.gate_callback("completion", "turn/completed")
        task = await self.start_task()
        await gate.wait()
        self.result = observation(1234)
        gate.release.set()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        self.assertTrue(self.os_claim(task))

    async def test_direct_first_work_in_a_fork_also_checks_the_project(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.reconciliation_idle()
        fork = await self.service.fork(task["id"])
        self.result = observation(1234)
        with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
            await self.service.send(fork["id"], "hold")

    async def family_member(self):
        parent = await self.start_task()
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        child = await self.service.fork(parent["id"])
        child = self.service.lifecycle.metadata(child["id"], {"parent_task_id": parent["id"], "root_task_id": parent["id"]})
        self.result = observation(1234)
        self.store.claim(child["id"], "background", "active", workspace_processes.data(self.result))
        return parent, child

    async def test_member_delete_transfers_the_hold_atomically_to_a_survivor(self):
        parent, child = await self.family_member()
        scheduled = asyncio.Event()
        original = self.service.schedule_reconciliation
        def scheduling(task_id, **kwargs):
            if task_id == parent["id"]:
                scheduled.set()
            return original(task_id, **kwargs)
        with patch.object(self.service, "schedule_reconciliation", scheduling):
            await self.service.delete(child["id"], child["thread_id"])
            await asyncio.wait_for(scheduled.wait(), 5)
            await self.reconciliation_idle()
        self.assertTrue(self.store.task_head(child["id"])["deleted"])
        self.assertFalse(self.store.claims(task_id=child["id"]))
        self.assertTrue(self.os_claim(parent))
        with self.assertRaisesRegex(BridgeError, "Codex no longer tracks them"):
            await self.start_task(key="surviving-owner")
        self.result = observation()
        self.clock.advance()
        await self.reconciliation_idle()
        self.assertFalse(self.service.get(parent["id"])["workspace_reserved"])

    async def test_family_delete_notification_before_ack_does_not_transfer_to_a_doomed_child(self):
        parent = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        request = self.service.requests(parent["id"])[0]
        await self.service.respond(request["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))
        await self.reconciliation_idle()
        self.result = observation(1234)
        self.store.claim(parent["id"], "background", "active", workspace_processes.data(self.result))
        original = self.service.backend.call
        gate = self.gates["family-delete-ack"] = FaultGate("family-delete-ack")
        async def notified(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "thread/delete":
                await self.service.on_message({"method": "thread/deleted", "params": {"threadId": params["threadId"]}})
                await gate.hit()
            return result
        self.service.backend.call = notified
        deleting = asyncio.create_task(self.service.delete(parent["id"], parent["thread_id"]))
        await gate.wait()
        await self.reconciliation_idle()
        gate.release.set()
        await deleting
        self.assertTrue(all(t.get("deleted") for _, t in self.store.records("tasks", family_id=parent["id"], light=True)))
        self.assertFalse(self.store.claims(family_id=parent["id"]))

    async def test_native_member_deletion_checks_before_releasing_its_project(self):
        parent, child = await self.family_member()
        self.store.claim(child["id"], "background", "uncertain", {"reason": "turn_completion"})
        gate = self.gates["delete-scan"] = FaultGate("delete-scan")
        async def scan(projects):
            await gate.hit()
            return observation(1234)
        self.service.process_scanner = scan
        notifying = asyncio.create_task(self.service.on_message({"method": "thread/deleted", "params": {"threadId": child["thread_id"]}}))
        await gate.wait()
        self.assertFalse(self.store.task_head(child["id"])["deleted"])
        gate.release.set()
        await notifying
        self.assertFalse(self.store.claims(task_id=child["id"]))
        self.assertTrue(self.os_claim(parent))

    async def test_scan_hold_survives_unavailable_native_backend_and_releases_without_rpc(self):
        task = await self.held()
        self.result = observation()
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Scan-only release called native RPC")), patch.object(
                type(self.service.backend), "alive", new_callable=PropertyMock, return_value=False):
            await self.service.reconcile_claims(task["id"], explicit=True)
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])

    async def test_new_family_scan_is_repeated_if_its_backend_generation_changes(self):
        original, scans = self.service.process_scanner, []
        async def scan(projects):
            scans.append(projects)
            if len(scans) == 1:
                self.service.backend.generation += 1
            return await original(projects)
        self.service.process_scanner = scan
        self.result = observation(1234)
        with self.assertRaisesRegex(BridgeError, "No task accounts for them"):
            await self.start_task(key="generation-admission")
        self.assertEqual(len(scans), 2)

    async def test_scan_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        self.scanner_loop = loop
        ready = asyncio.Event()
        def scan(config, projects):
            loop.call_soon_threadsafe(ready.set)
            entered.set()
            if not release.wait(5):
                raise AssertionError("The event loop could not release the scanner")
            return observation()
        self.service.process_scanner = self.service.scan_projects
        with patch.object(procscan, "scan_for", side_effect=scan), patch(
                "codex_local_mcp.service.sys", SimpleNamespace(platform="linux")), thread_wakeups():
            pending = asyncio.create_task(self.start_task())
            try:
                await asyncio.wait_for(ready.wait(), 5)
                self.assertTrue(entered.is_set())
                self.assertFalse(self.service.admission_lock.locked())
                self.assertFalse(self.store.db.in_transaction)
            finally:
                release.set()
            await asyncio.wait_for(pending, 5)
            await self.reconciliation_idle()
            await self.service.close()
