"""Deterministic correctness checks at the scheduler's durable boundaries."""

import asyncio
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp import execution_plans, plans
from codex_local_mcp.security import BridgeError
from codex_local_mcp.incidents import export_incident, fixture_bytes
from codex_local_mcp.retention import backup, Retention
from codex_local_mcp.tools import dispatch, encoded_size, RESULT_BUDGET
from tests import test_execution_plans as cases
from tests.helpers import FaultGate
from tests.test_service import ServiceHarness


class PlanReviewTest(ServiceHarness):
    project = cases.ExecutionPlanTest.project
    node = cases.ExecutionPlanTest.node
    spec = cases.ExecutionPlanTest.spec
    submit = cases.ExecutionPlanTest.submit
    view = cases.ExecutionPlanTest.view
    wait_plan = cases.ExecutionPlanTest.wait_plan
    settled = cases.ExecutionPlanTest.settled
    cancel = cases.ExecutionPlanTest.cancel

    async def test_dispatch_key_collision_is_a_failed_step_without_native_io(self):
        spec = self.spec(self.node("collision"))
        identity = plans.plan_id(plans.validate(spec))
        key = f"plan:{identity}:collision:0"
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Key collision reached Codex")):
            result = await self.submit(spec, key)
            await self.settled()
        final = self.view(result["plan_id"])
        self.assertEqual(final["nodes"][0]["status"], "failed")
        self.assertEqual(final["nodes"][0]["reason"], "operation_conflict")
        self.assertTrue(final["done"])
        self.assertEqual(final["outcome"], "failed")
        self.assertFalse(self.store.db.execute("SELECT 1 FROM tasks").fetchone())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM admissions").fetchone())

    async def test_stop_failure_skips_an_unsent_waiter_without_an_unrelated_event(self):
        owner = await self.start_task("hold", path="owner")
        self.service.config = replace(self.cfg, budget={"max_turns": 1})
        self.service.resources.defaults = {"max_turns": 1}
        gate = self.gates["reject-after-wait"] = FaultGate("reject-after-wait")
        original = execution_plans.dispatch
        async def gated(service, name, args):
            if name == "codex_send":
                await gate.hit()
            return await original(service, name, args)
        with patch.object(execution_plans, "dispatch", gated):
            result = await self.submit(self.spec(self.node("root"),
                self.node("waiting", target={"workdir": owner["project"]}, after=("root",)),
                self.node("capped", action="send", target={"node": "root"}, after=("root",)), max_parallel=2))
            await gate.wait()
            await self.wait_plan(result["plan_id"], lambda p: p["nodes"][1]["status"] == "ready")
            # A proven-unsent failure itself must propagate; no unrelated
            # resource notification is required to finish a stopped plan.
            with patch.object(self.service.scheduler, "changed", return_value=None):
                version = self.service.scheduler.version
                gate.release.set()
                await self.settled()
                self.assertEqual(self.service.scheduler.version, version)
        final = self.view(result["plan_id"])
        self.assertEqual(final["nodes"][2]["reason"], "budget_exceeded")
        self.assertEqual(final["nodes"][1]["status"], "skipped")
        self.assertEqual(final["nodes"][1]["reason"], "plan_stopped")
        self.assertEqual(final["outcome"], "failed")
        self.assertTrue(final["done"])

    async def test_finished_plan_is_readable_without_its_project_folder(self):
        result = await self.submit(self.spec(self.node("gone")))
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        task = self.store.task_head(final["nodes"][0]["task_id"])
        shutil.rmtree(task["project"])
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Plan inspection called Codex")):
            current = await dispatch(self.service, "codex_plans", {"action": "inspect", "plan_id": result["plan_id"]})
            repeated = await self.submit(result["spec"], "repeat-gone")
        self.assertEqual(current["nodes"][0]["result_ref"], final["nodes"][0]["result_ref"])
        self.assertTrue(repeated["done"])
        self.assertEqual(current["outcome"], "succeeded")

    async def test_deleted_child_cannot_erase_its_failed_execution(self):
        result = await self.submit(self.spec(self.node("parent", prompt="native_children"),
            self.node("after", after=("parent",))))
        waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["pending_requests_total"] == 1)
        await self.settled()
        node = waiting["nodes"][0]
        root = self.store.task_head(node["task_id"])
        request = self.store.get("requests", node["pending_requests"][0]["id"])
        child = self.service.task_for_thread(request["params"]["threadId"])
        with patch.object(self.service.scheduler, "schedule", return_value=None):
            for thread, turn, status in ((child["thread_id"], request["params"]["turnId"], "failed"),
                    (root["thread_id"], node["turn_id"], "completed")):
                await self.service.on_message({"method": "turn/completed", "params": {"threadId": thread,
                    "turn": {"id": turn, "status": status, "items": []}}})
            await self.service.on_message({"method": "thread/deleted", "params": {"threadId": child["thread_id"]}})
        self.assertTrue(self.store.task_head(child["id"])["deleted"])
        self.assertEqual(self.store.db.execute("SELECT status FROM turns WHERE task_id=? AND turn_id=?",
            (child["id"], request["params"]["turnId"])).fetchone()[0], "failed")
        self.service.scheduler.schedule()
        await self.settled()
        final = self.view(result["plan_id"])
        self.assertEqual(final["nodes"][0]["status"], "failed")
        self.assertEqual(final["nodes"][0]["reason"], "descendant_failed")
        self.assertEqual(final["nodes"][1]["status"], "skipped")

    async def test_cancel_before_admission_never_dispatches_after_restart(self):
        gate = self.gates["not-admitted"] = FaultGate("not-admitted")
        original = execution_plans.dispatch
        async def gated(service, name, args):
            if name == "codex_start":
                await gate.hit()
            return await original(service, name, args)
        with patch.object(execution_plans, "dispatch", gated):
            result = await self.submit(self.spec(self.node("unsent")))
            await gate.wait()
            self.assertFalse(self.store.db.execute("SELECT 1 FROM operations WHERE id LIKE 'plan:%'").fetchone())
            cancelled = await self.cancel(result["plan_id"])
            await self.service.close()
        self.service = self.new_service()
        native, original_call = [], self.service.backend.call
        async def counted(method, params, **kwargs):
            if method == "turn/start":
                native.append(params)
            return await original_call(method, params, **kwargs)
        with patch.object(self.service.backend, "call", counted):
            await self.settled()
            final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(native, [])
        self.assertTrue(cancelled["done"])
        self.assertEqual(cancelled["nodes"][0]["status"], "cancelled")
        self.assertEqual(final["outcome"], "cancelled")
        self.assertTrue(final["done"])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM tasks").fetchone())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_turns").fetchone())

    async def test_late_owned_turn_event_binds_an_uncertain_step_without_replaying_it(self):
        event = self.gate_callback("late-start-event", "turn/started")
        ack = self.gate_call("lost-ack-before-event", "turn/start", error=TimeoutError("Lost acknowledgement"))
        result = await self.submit(self.spec(self.node("late")))
        await event.wait()
        await ack.wait()
        ack.release.set()
        waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        self.assertIsNone(waiting["nodes"][0]["turn_id"])
        task_id = waiting["nodes"][0]["task_id"]
        event.release.set()
        await self.until(task_id, ("completed",))
        await self.settled()
        bound = self.view(result["plan_id"])["nodes"][0]
        self.assertIsNotNone(bound["turn_id"])
        self.assertEqual(bound["status"], "uncertain")
        await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "resume-late"})
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)

    async def test_active_plan_inspection_contains_a_missing_project_error(self):
        result = await self.submit(self.spec(self.node("missing", prompt="hold")))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        task = self.store.task_head(running["nodes"][0]["task_id"])
        shutil.rmtree(task["project"])
        with self.assertRaises(BridgeError) as blocked:
            self.service.request_page(task["id"])
        current = await dispatch(self.service, "codex_plans", {"action": "inspect", "plan_id": result["plan_id"]})
        self.assertFalse(current["done"])
        self.assertEqual(current["nodes"][0]["status"], "running")
        self.assertEqual(current["nodes"][0]["error"], blocked.exception.as_dict())
        self.assertTrue(current["nodes"][0]["next_steps"])

    async def test_online_backup_includes_the_durable_plan_without_running_it(self):
        spec = self.spec(self.node("backup", prompt="not dispatched yet"))
        with patch.object(self.service.scheduler, "schedule", return_value=None):
            result = await self.submit(spec)
        path = self.base / "backups" / "copy.sqlite3"
        report = backup(self.cfg, path)
        self.assertEqual(report["integrity_check"], "ok")
        with closing(sqlite3.connect(path)) as saved:
            plan = saved.execute("SELECT spec FROM plans WHERE id=?", (result["plan_id"],)).fetchone()
            self.assertEqual(json.loads(plan[0]), result["spec"])
            self.assertEqual(saved.execute("SELECT status FROM plan_nodes WHERE plan_id=?", (result["plan_id"],)).fetchone()[0], "pending")
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_turns").fetchone())

    async def test_incident_export_omits_plan_prompts_and_identity(self):
        prompt = "Private incident example text that must not leave the state folder"
        result = await self.submit(self.spec(self.node("exported", prompt=prompt)))
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        exported = fixture_bytes(export_incident(self.store, [final["nodes"][0]["task_id"]], limit=200)).decode()
        self.assertNotIn(prompt, exported)
        self.assertNotIn(result["plan_id"], exported)
        self.assertNotIn(str(self.root), exported)
        self.assertIn('"content_omitted": true', exported)

    async def test_collision_with_other_arguments_cannot_reuse_the_old_task(self):
        node = self.node("different")
        spec = self.spec(node)
        identity = plans.plan_id(plans.validate(spec))
        key = f"plan:{identity}:different:0"
        previous = await self.start_task("echo", path="other-arguments", key=key)
        await self.until(previous["id"], ("completed",))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Conflicting step made another native call")):
            result = await self.submit(spec, "submit-collision")
            await self.settled()
        failed = self.view(result["plan_id"])
        self.assertTrue(failed["done"])
        self.assertEqual(failed["nodes"][0]["reason"], "operation_conflict")
        self.assertIsNone(failed["nodes"][0]["task_id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)

    async def test_cancelled_running_plan_is_terminal_after_restart(self):
        result = await self.submit(self.spec(self.node("running", prompt="hold")))
        await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        await self.cancel(result["plan_id"])
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        await self.settled()
        self.assertEqual(final["outcome"], "cancelled")
        await self.service.close()
        self.service = self.new_service()
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Finished cancellation was replayed")):
            await self.settled()
        self.assertEqual(self.view(result["plan_id"])["outcome"], "cancelled")
        self.assertTrue(self.view(result["plan_id"])["done"])

    async def test_late_turn_token_cannot_bind_a_different_backend_generation(self):
        event = self.gate_callback("old-event", "turn/started")
        ack = self.gate_call("old-ack", "turn/start", error=TimeoutError("Lost acknowledgement"))
        result = await self.submit(self.spec(self.node("old")))
        await event.wait()
        await ack.wait()
        ack.release.set()
        waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        task_id, generation = waiting["nodes"][0]["task_id"], self.service.backend.generation
        await self.service.backend.close()
        await self.service.ensure_backend()
        self.assertNotEqual(self.service.backend.generation, generation)
        self.service.scheduler.observed_turn(task_id, "unrelated-new-generation-turn")
        self.assertIsNone(self.view(result["plan_id"])["nodes"][0]["turn_id"])
        self.assertEqual(self.view(result["plan_id"])["nodes"][0]["status"], "uncertain")

    async def test_large_pending_request_pages_and_list_results_stay_bounded(self):
        result = await self.submit(self.spec(*(self.node("bounded-" + str(n), prompt="hold") for n in range(16)), max_parallel=4))
        active = await self.wait_plan(result["plan_id"], lambda p: all(n["status"] == "running" for n in p["nodes"][:4]))
        for position, node in enumerate(active["nodes"][:4]):
            task = self.store.task_head(node["task_id"])
            for count in range(21):
                await self.service.on_message({"id": 100000 + position * 100 + count,
                    "method": "item/commandExecution/requestApproval", "params": {
                        "threadId": task["thread_id"], "turnId": node["turn_id"], "itemId": f"item-{position}-{count}",
                        "command": "echo " + "x" * 65536, "cwd": task["project"], "startedAtMs": 0}})
        current = await dispatch(self.service, "codex_plans", {"action": "inspect", "plan_id": result["plan_id"]})
        self.assertLessEqual(encoded_size(current), RESULT_BUDGET)
        for node in current["nodes"][:4]:
            self.assertEqual(node["pending_requests_total"], 21)
            self.assertLessEqual(len(node["pending_requests"]), 20)
            self.assertIsNotNone(node["pending_requests_next_cursor"])
        await self.settled()
        with patch.object(self.service.scheduler, "schedule", return_value=None):
            self.service.scheduler.submit(self.spec(self.node("listed"), name="Another bounded plan"))
        first = await dispatch(self.service, "codex_plans", {"action": "list", "limit": 1})
        second = await dispatch(self.service, "codex_plans", {"action": "list", "limit": 1, "offset": first["next_offset"]})
        self.assertNotEqual(first["plans"][0]["plan_id"], second["plans"][0]["plan_id"])
        self.assertLessEqual(encoded_size(first), RESULT_BUDGET)

    async def test_cancel_then_confirmed_delete_finishes_an_unbound_plan_and_allows_prune(self):
        event = self.gate_callback("unbound-event", "turn/started")
        ack = self.gate_call("unbound-ack", "turn/start", error=TimeoutError("Lost acknowledgement"))
        result = await self.submit(self.spec(self.node("unbound"), self.node("never", after=("unbound",))))
        await event.wait()
        await ack.wait()
        ack.release.set()
        unknown = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        task_id = unknown["nodes"][0]["task_id"]
        await self.service.close()
        self.service = self.new_service()
        await self.settled()
        await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "inspect-unbound-history"})
        await self.settled()
        self.assertIsNone(self.view(result["plan_id"])["nodes"][0]["turn_id"])
        await self.cancel(result["plan_id"])
        await self.settled()
        self.assertFalse(self.view(result["plan_id"])["done"])
        await dispatch(self.service, "codex_archive", {"task_id": task_id, "action": "archive", "request_id": "archive-unbound"})
        await self.settled()
        self.assertFalse(self.view(result["plan_id"])["done"])
        task = self.store.task_head(task_id)
        await dispatch(self.service, "codex_delete", {"task_id": task_id,
            "confirm_thread_id": task["thread_id"], "request_id": "delete-unbound"})
        await self.settled()
        final = self.view(result["plan_id"])
        self.assertTrue(final["done"])
        self.assertEqual(final["outcome"], "cancelled")
        self.assertEqual(final["nodes"][0]["reason"], "task_deleted")
        self.assertEqual(final["nodes"][1]["status"], "cancelled")
        retention = Retention(self.store, clock=lambda: final["updated_at"] + 200)
        proposal = retention.plan(100)
        self.assertEqual(proposal["counts"]["plans"], 1)
        retention.commit(proposal["plan_id"])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM plans WHERE id=?", (result["plan_id"],)).fetchone())

    async def test_cancel_of_detached_metadata_only_start_does_not_require_a_thread_id(self):
        gate = self.gate_call("before-thread", "thread/start", before=True)
        result = await self.submit(self.spec(self.node("metadata")))
        await gate.wait()
        node = self.view(result["plan_id"])["nodes"][0]
        self.assertIsNone(self.store.task_head(node["task_id"])["thread_id"])
        data = json.loads(self.store.db.execute("SELECT data FROM plan_nodes WHERE plan_id=?", (result["plan_id"],)).fetchone()[0])
        self.assertNotIn("start_token", data)
        await self.service.close()
        self.service = self.new_service()
        await self.settled()
        cancelled = await self.cancel(result["plan_id"])
        await self.settled()
        self.assertTrue(self.view(result["plan_id"])["done"])
        self.assertEqual(self.view(result["plan_id"])["outcome"], "cancelled")
        self.assertEqual(self.view(result["plan_id"])["nodes"][0]["reason"], "cancelled_before_execution")
        self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_turns").fetchone())

    async def test_restart_does_not_treat_another_calls_rejection_as_its_own(self):
        owner = await self.start_task("hold", path="foreign-owner")
        gate = self.gates["before-owned-admission"] = FaultGate("before-owned-admission")
        original = execution_plans.dispatch
        async def gated(service, name, args):
            if name == "codex_start":
                await gate.hit()
            return await original(service, name, args)
        with patch.object(execution_plans, "dispatch", gated):
            result = await self.submit(self.spec(self.node("owned")))
            await gate.wait()
            key = self.view(result["plan_id"])["nodes"][0]["operation_id"]
            with self.assertRaises(BridgeError) as rejected:
                await dispatch(self.service, "codex_start", {"workdir": owner["project"], "prompt": "other arguments", "request_id": key})
            self.assertEqual(rejected.exception.code, "workspace_busy")
            await self.service.close()
        self.service = self.new_service()
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Foreign rejection was retried under a new key")):
            await self.settled()
        final = self.view(result["plan_id"])
        self.assertEqual(final["nodes"][0]["status"], "failed")
        self.assertEqual(final["nodes"][0]["reason"], "operation_conflict")
        self.assertEqual(final["nodes"][0]["attempts"], 1)

    async def test_unavailable_review_fails_without_claiming_an_unknown_execution(self):
        terminal = self.gate_callback("before-review", "turn/completed")
        result = await self.submit(self.spec(self.node("source"),
            self.node("review", action="review", target={"node": "source"}, after=("source",))))
        await terminal.wait()
        self.service.backend.unavailable_methods.add("review/start")
        terminal.release.set()
        await self.wait_plan(result["plan_id"], lambda p: p["nodes"][1]["error"] is not None)
        await self.settled()
        final = self.view(result["plan_id"])
        self.assertEqual(final["nodes"][1]["error"]["code"], "optional_native_capability_unavailable")
        self.assertEqual(final["nodes"][1]["status"], "failed")
        self.assertTrue(final["done"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM admissions").fetchone())

    async def test_cached_unavailable_review_is_not_replayed_after_restart(self):
        terminal = self.gate_callback("before-cached-review", "turn/completed")
        returned = self.gates["before-refusal-observed"] = FaultGate("before-refusal-observed")
        original = execution_plans.dispatch
        async def withheld(service, name, args):
            try:
                return await original(service, name, args)
            except BridgeError:
                if name == "codex_review":
                    await returned.hit()
                raise
        with patch.object(execution_plans, "dispatch", withheld):
            result = await self.submit(self.spec(self.node("source"),
                self.node("review", action="review", target={"node": "source"}, after=("source",))))
            await terminal.wait()
            self.service.backend.unavailable_methods.add("review/start")
            terminal.release.set()
            await returned.wait()
            await self.service.close()
        self.service = self.new_service()
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Unavailable review was replayed")):
            await self.settled()
        final = self.view(result["plan_id"])
        self.assertTrue(final["done"])
        self.assertEqual(final["nodes"][1]["status"], "failed")
        self.assertEqual(final["nodes"][1]["error"]["code"], "optional_native_capability_unavailable")
        self.assertEqual(final["nodes"][1]["error"]["method"], "review/start")

    async def test_explicit_resume_releases_a_proven_never_started_task_without_native_io(self):
        gate = self.gate_call("never-started", "thread/start", before=True)
        result = await self.submit(self.spec(self.node("never")))
        await gate.wait()
        task_id = self.view(result["plan_id"])["nodes"][0]["task_id"]
        await self.service.close()
        self.service = self.new_service()
        await self.settled()
        await self.cancel(result["plan_id"])
        await self.settled()
        self.assertTrue(self.store.claims(task_id=task_id))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Recovery sent native work")):
            recovered = await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "recover-never"})
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["message"], "Codex never started this task; nothing ran.")
            self.assertEqual(recovered["native_conversation"], "not_started")
            self.assertEqual(self.store.claims(task_id=task_id), [])
            self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_turns").fetchone())
            with self.assertRaisesRegex(BridgeError, "pass confirm_thread_id equal to the task id"):
                await dispatch(self.service, "codex_delete", {"task_id": task_id,
                    "confirm_thread_id": "wrong", "request_id": "wrong-never-confirmation"})
            deleted = await dispatch(self.service, "codex_delete", {"task_id": task_id,
                "confirm_thread_id": task_id, "request_id": "delete-never"})
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["native_conversation"], "not_started")
        self.assertFalse(deleted["processes_unverified"])
        await self.settled()
        self.assertEqual(self.view(result["plan_id"])["outcome"], "cancelled")
        self.assertTrue(self.view(result["plan_id"])["done"])
        replacement = await self.start_task("echo", path="never", key="after-never-recovery")
        self.assertEqual(replacement["project"], self.store.task_head(task_id)["project"])

    async def test_thread_identity_is_committed_before_turn_start_and_cannot_use_task_confirmation(self):
        gate = self.gate_call("committed-thread", "turn/start", before=True)
        result = await self.submit(self.spec(self.node("durable")))
        await gate.wait()
        task_id = self.view(result["plan_id"])["nodes"][0]["task_id"]
        self.assertFalse(self.store.db.in_transaction)
        with closing(sqlite3.connect(self.state / "state.sqlite3")) as observer:
            thread_id = observer.execute("SELECT thread_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        self.assertIsInstance(thread_id, str)
        self.assertTrue(thread_id)
        gate.release.set()
        await self.wait_plan(result["plan_id"], lambda p: p["done"])
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Wrong confirmation reached Codex")):
            with self.assertRaisesRegex(BridgeError, "exact native thread_id"):
                await dispatch(self.service, "codex_delete", {"task_id": task_id,
                    "confirm_thread_id": task_id, "request_id": "wrong-normal-confirmation"})
        self.assertFalse(self.store.task_head(task_id).get("deleted"))

    async def test_inconsistent_native_work_record_is_not_treated_as_never_started(self):
        gate = self.gate_call("inconsistent-start", "thread/start", before=True)
        result = await self.submit(self.spec(self.node("inconsistent")))
        await gate.wait()
        task_id = self.view(result["plan_id"])["nodes"][0]["task_id"]
        await self.service.close()
        self.service = self.new_service()
        with self.store.transaction():
            self.store.db.execute("""INSERT INTO resource_reservations(id,family_id,task_id,turns,children,state,native_key)
                VALUES ('inconsistent-reservation',?,?,1,0,'uncertain','unconfirmed-native-work')""", (task_id, task_id))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Inconsistent state reached Codex")):
            with self.assertRaises(BridgeError) as refused:
                await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "refuse-inconsistent"})
        self.assertEqual(refused.exception.code, "state_conflict")
        self.assertTrue(self.store.claims(task_id=task_id))
        self.assertNotEqual(self.store.get("tasks", task_id).get("native_conversation"), "not_started")

    async def test_lost_thread_start_reply_recovers_without_replaying_a_turn(self):
        # The native conversation exists, but its acknowledgement has not yet
        # crossed the durable identity boundary. No turn can have been sent.
        gate = self.gate_call("lost-thread-reply", "thread/start")
        result = await self.submit(self.spec(self.node("empty-conversation")))
        await gate.wait()
        task_id = self.view(result["plan_id"])["nodes"][0]["task_id"]
        self.assertIsNone(self.store.task_head(task_id)["thread_id"])
        await self.service.close()
        self.service = self.new_service()
        await self.settled()
        self.assertEqual(self.view(result["plan_id"])["nodes"][0]["status"], "uncertain")
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Lost thread acknowledgement was replayed")):
            recovered = await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "resume-empty-conversation"})
            await self.settled()
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(self.store.claims(task_id=task_id), [])
        final = self.view(result["plan_id"])
        self.assertTrue(final["done"])
        self.assertEqual(final["outcome"], "failed")
        self.assertEqual(final["nodes"][0]["reason"], "not_started")
        self.assertEqual(final["nodes"][0]["attempts"], 1)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_turns").fetchone())

    async def test_another_turn_before_the_dispatch_write_cannot_bind_the_node(self):
        terminal = self.gate_callback("before-follow-up", "turn/completed")
        result = await self.submit(self.spec(self.node("source"),
            self.node("follow-up", action="send", target={"node": "source"}, after=("source",))))
        await terminal.wait()
        before_write = self.gate_call("follow-up-not-sent", "turn/start", before=True)
        terminal.release.set()
        await before_write.wait()
        node = self.view(result["plan_id"])["nodes"][1]
        task = self.store.task_head(node["task_id"])
        self.assertIsNone(node["turn_id"])
        # A native goal or queued turn may start while this local call still
        # waits to write. Its event cannot acknowledge the unsent dispatch.
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"],
            "turn": {"id": "other-native-turn", "status": "inProgress", "items": []}}})
        self.assertIsNone(self.view(result["plan_id"])["nodes"][1]["turn_id"])

    async def test_autonomous_work_prevents_speculative_binding_after_a_write(self):
        terminal = self.gate_callback("before-autonomous-work", "turn/completed")
        result = await self.submit(self.spec(self.node("source"),
            self.node("follow-up", action="send", target={"node": "source"}, prompt="owned follow-up", after=("source",))))
        await terminal.wait()
        started = self.gate_callback("withhold-owned-start", "turn/started")
        ack = self.gates["autonomous-lost-ack"] = FaultGate("autonomous-lost-ack", TimeoutError("Lost acknowledgement"))
        original = self.service.backend.call
        async def withheld(method, params, **kwargs):
            value = await original(method, params, **kwargs)
            if method == "turn/start":
                await ack.hit()
            return value
        source_id = self.view(result["plan_id"])["nodes"][0]["task_id"]
        await asyncio.wait_for(dispatch(self.service, "codex_goal", {"task_id": source_id, "action": "set",
            "objective": "Fixture goal", "user_requested": True, "request_id": "fixture-autonomous-goal"}), 5)
        with patch.object(self.service.backend, "call", withheld):
            terminal.release.set()
            await ack.wait()
            await started.wait()
            node = self.view(result["plan_id"])["nodes"][1]
            task = self.store.task_head(source_id)
            await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"],
                "turn": {"id": "autonomous-native-turn", "status": "inProgress", "items": []}}})
            self.assertIsNone(self.view(result["plan_id"])["nodes"][1]["turn_id"])
            self.store.claim(source_id, "goal", "inactive")
            for kind in ("goal", "queue"):
                for state in ("active", "uncertain"):
                    with self.subTest(kind=kind, state=state):
                        self.store.claim(source_id, kind, state)
                        self.service.scheduler.execution_sent(node["operation_id"])
                        self.assertNotIn(node["operation_id"], self.service.scheduler.live_starts)
                        self.store.claim(source_id, kind, "inactive")
            self.store.claim(source_id, "goal", "active")
            ack.release.set()
            await self.wait_plan(result["plan_id"], lambda p: p["nodes"][1]["status"] == "uncertain")
            started.release.set()
            await self.settled()
        final = self.view(result["plan_id"])
        self.assertEqual(final["nodes"][1]["status"], "uncertain")
        self.assertIsNone(final["nodes"][1]["turn_id"])
        self.assertFalse(final["done"])

    async def delayed_existing_turn(self, action, *, child=False):
        terminal = self.gate_callback("before-delayed-turn", "turn/completed")
        result = await self.submit(self.spec(self.node("source", prompt="native_children" if child else "echo"),
            self.node("follow-up", action=action, target={"node": "source"}, after=("source",)),
            self.node("after", after=("follow-up",))))
        source = None
        if child:
            waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["pending_requests_total"] == 1)
            request = self.store.get("requests", waiting["nodes"][0]["pending_requests"][0]["id"])
            source = self.service.task_for_thread(request["params"]["threadId"])
            await dispatch(self.service, "codex_respond", {"request_id": request["id"], "response": {"decision": "decline"}})
        await terminal.wait()
        parent = self.store.task_head(self.view(result["plan_id"])["nodes"][0]["task_id"])
        source = source or parent
        prior_turn = source["turn_id"]
        self.assertIsNotNone(prior_turn)
        self.assertEqual(self.service.resources.family(source["id"]), parent["id"])
        # Inline reviews emit root item events before the nested turn/started.
        # Hold the first evidence of the new work, not a later announcement.
        started = self.gate_callback("hold-real-follow-up", "item/started" if action == "review" else "turn/started")
        before = self.gates["before-follow-up-write"] = FaultGate("before-follow-up-write")
        ack = self.gates["delayed-turn-lost-ack"] = FaultGate("delayed-turn-lost-ack", TimeoutError("Lost acknowledgement"))
        method, original = "review/start" if action == "review" else "turn/start", self.service.backend.call
        calls = []
        async def withheld(native_method, params, **kwargs):
            if native_method == method:
                await before.hit()
                calls.append(params["threadId"])
            value = await original(native_method, params, **kwargs)
            if native_method == method:
                await ack.hit()
            return value
        with patch.object(self.service.backend, "call", withheld):
            terminal.release.set()
            await before.wait()
            self.assertTrue(self.store.db.execute("SELECT 1 FROM turns WHERE task_id=? AND turn_id=?",
                (source["id"], prior_turn)).fetchone())
            before.release.set()
            await ack.wait()
            await started.wait()
            node = self.view(result["plan_id"])["nodes"][1]
            self.assertIsNone(node["turn_id"])
            # Check proper child routing and a stale identity presented on the
            # parent's stream. Neither may acknowledge this new dispatch.
            threads = [source["thread_id"]]
            if child:
                threads.append(parent["thread_id"])
            for thread_id in threads:
                events = ("turn/started", "item/agentMessage/delta")
                for event in events if action == "send" else reversed(events):
                    with self.subTest(action=action, child=child, thread_id=thread_id, event=event):
                        params = {"threadId": thread_id, "turnId": prior_turn}
                        if event == "turn/started":
                            params["turn"] = {"id": prior_turn, "status": "inProgress", "items": []}
                        else:
                            params.update(itemId="delayed-message", delta="Delayed progress")
                        await self.service.on_message({"method": event, "params": params})
                        self.assertIsNone(self.view(result["plan_id"])["nodes"][1]["turn_id"])
            ack.release.set()
            final = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][1]["status"] == "uncertain")
        self.assertIsNone(final["nodes"][1]["turn_id"])
        self.assertEqual(final["nodes"][1]["attempts"], 1)
        self.assertEqual(final["nodes"][2]["status"], "pending")
        self.assertEqual(final["outcome"], "blocked")
        self.assertFalse(final["done"])
        self.assertEqual(calls, [parent["thread_id"]])

    async def test_delayed_existing_turn_events_cannot_bind_send(self):
        await self.delayed_existing_turn("send")

    async def test_delayed_existing_turn_events_cannot_bind_review(self):
        await self.delayed_existing_turn("review")

    async def test_delayed_family_turn_events_cannot_bind_send(self):
        await self.delayed_existing_turn("send", child=True)

    async def test_delayed_family_turn_events_cannot_bind_review(self):
        await self.delayed_existing_turn("review", child=True)

    async def test_live_thread_start_cannot_be_resolved_as_never_started(self):
        thread = self.gate_call("live-thread-start", "thread/start")
        result = await self.submit(self.spec(self.node("live-start", prompt="hold")))
        await thread.wait()
        node = self.view(result["plan_id"])["nodes"][0]
        task_id = node["task_id"]
        def snapshot():
            return (self.store.get("tasks", task_id), self.store.claims(task_id=task_id, include_inactive=True),
                [dict(row) for row in self.store.db.execute("SELECT * FROM resource_reservations WHERE task_id=? ORDER BY id", (task_id,))],
                self.store.highwater())
        before = snapshot()
        self.assertEqual(before[0]["status"], "starting")
        self.assertIsNone(before[0]["thread_id"])
        self.assertTrue(before[1])
        self.assertTrue(before[2])
        with self.assertRaisesRegex(BridgeError, "^No Codex thread was created for this task$") as refused:
            await asyncio.wait_for(self.service.resume(task_id), 5)
        self.assertEqual(refused.exception.code, "state_conflict")
        self.assertEqual(snapshot(), before)
        self.assertIsNone(self.service.recover_unstarted(task_id))
        self.assertEqual(snapshot(), before)
        # A status change cannot hide a start operation still running here.
        for status in ("failed", "needs_resume"):
            with self.subTest(status=status):
                self.assertTrue(self.service.start_inflight({**before[0], "status": status}))
        family_lock = self.service.family_locks[task_id]
        acquiring, acquire = asyncio.Event(), family_lock.acquire
        async def observed_acquire():
            acquiring.set()
            return await acquire()
        resuming = None
        try:
            with patch.object(family_lock, "acquire", observed_acquire):
                resuming = asyncio.create_task(dispatch(self.service, "codex_resume",
                    {"task_id": task_id, "request_id": "resume-after-live-start"}))
                await asyncio.wait_for(acquiring.wait(), 5)
                self.assertFalse(resuming.done())
                self.assertEqual(snapshot(), before)
                turn = self.gate_call("reserved-live-turn", "turn/start", before=True)
                thread.release.set()
                await turn.wait()
                starting = self.store.task_head(task_id)
                self.assertIsNotNone(starting["thread_id"])
                self.assertTrue(self.store.claims(task_id=task_id))
                reservation = self.store.db.execute("SELECT * FROM resource_reservations WHERE id=?", (node["operation_id"],)).fetchone()
                self.assertIsNotNone(reservation)
                self.assertEqual(reservation["token"], starting["turn_start"])
                self.assertEqual(reservation["turns"], 1)
                with self.assertRaises(BridgeError) as busy:
                    self.service.capacity(starting["project"])
                self.assertEqual(busy.exception.code, "workspace_busy")
                turn.release.set()
                running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
                resumed = await asyncio.wait_for(resuming, 5)
            self.assertEqual(resumed["status"], "running")
            self.assertEqual(resumed["thread_id"], starting["thread_id"])
            self.assertIsNone(resumed.get("native_conversation"))
            self.assertEqual(self.store.db.execute("SELECT server_started FROM resource_turns WHERE task_id=? AND turn_id=?",
                (task_id, running["nodes"][0]["turn_id"])).fetchone()[0], 1)
            self.assertTrue(self.store.claims(task_id=task_id))
            self.assertFalse(self.store.db.execute("SELECT 1 FROM events WHERE task_id=? AND kind='bridge/start_not_run'", (task_id,)).fetchone())
        finally:
            if resuming is not None:
                if not resuming.done():
                    resuming.cancel()
                await asyncio.gather(resuming, return_exceptions=True)

    async def test_start_failed_without_a_thread_remains_explicitly_recoverable(self):
        rejected = self.gate_call("failed-thread-start", "thread/start", before=True, error=TimeoutError("Start failed before its reply"))
        result = await self.submit(self.spec(self.node("failed-start")))
        await rejected.wait()
        task_id = self.view(result["plan_id"])["nodes"][0]["task_id"]
        rejected.release.set()
        await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        await self.settled()
        self.assertEqual(self.store.task_head(task_id)["status"], "failed")
        self.assertIsNone(self.store.task_head(task_id)["thread_id"])
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Failed-start recovery called Codex")):
            recovered = await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "recover-failed-start"})
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["native_conversation"], "not_started")
        self.assertEqual(self.store.claims(task_id=task_id), [])
