import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp import plans
from codex_local_mcp.retention import Retention
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import arguments, dispatch, encoded_size, tool_catalog, validate, BY_NAME
from tests.helpers import thread_wakeups
from tests.test_service import ServiceHarness


class ExecutionPlanTest(ServiceHarness):
    def project(self, name):
        return self.service.projects("create", str(self.root / name))

    def node(self, identity, *, after=(), action="start", target=None, prompt="echo", **fields):
        return {"id": identity, "action": action, "target": target or {"project_id": self.project(identity)["id"]},
            "prompt": prompt, "after": list(after), **fields}

    def spec(self, *nodes, **fields):
        return {"version": 1, "name": "Offline plan", "nodes": list(nodes), **fields}

    async def submit(self, spec, key="submit"):
        return await dispatch(self.service, "codex_plans", {"action": "submit", "spec": spec, "request_id": key})

    def view(self, identity):
        return self.service.scheduler.inspect(identity)

    async def wait_plan(self, identity, predicate):
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: predicate(self.view(identity))), 10)
        return self.view(identity)

    async def settled(self):
        await asyncio.wait_for(self.service.scheduler.idle(), 10)

    async def cancel(self, identity, key="cancel"):
        return await dispatch(self.service, "codex_plans", {"action": "cancel", "plan_id": identity, "request_id": key})

    def count_starts(self, calls):
        original = self.service.backend.call
        async def record(method, params, **kwargs):
            if method == "turn/start":
                calls.append(params["threadId"])
            return await original(method, params, **kwargs)
        self.service.backend.call = record

    async def test_diamond_success_waits_for_native_results_and_passes_only_references(self):
        sent, original = [], self.service.backend.call
        async def record(method, params, **kwargs):
            if method == "turn/start":
                sent.append(params["input"][0]["text"])
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", record):
            result = await self.submit(self.spec(self.node("root"), self.node("left", after=("root",)),
                self.node("right", after=("root",)), self.node("join", after=("left", "right"))))
            final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual([n["status"] for n in final["nodes"]], ["succeeded"] * 4)
        self.assertEqual(len(sent), 4)
        self.assertNotIn("Dependency references", sent[0])
        for node, prompt in zip(final["nodes"][1:], sent[1:]):
            self.assertIn("Dependency references only", prompt)
            self.assertNotIn("Fixture summary", prompt)
        for node in final["nodes"]:
            ref = node["result_ref"]
            native = self.store.db.execute("SELECT status FROM turns WHERE task_id=? AND turn_id=?", (node["task_id"], node["turn_id"])).fetchone()
            self.assertEqual(native[0], "completed")
            self.assertIsNotNone(ref["result_id"])
            task = self.service.get(node["task_id"])
            self.assertEqual((task["plan_id"], task["plan_node_id"]), (result["plan_id"], node["id"]))
        self.assertIn(final["nodes"][0]["result_ref"]["result_id"], sent[1])

    async def test_acknowledgement_done_is_not_a_completed_node(self):
        result = await self.submit(self.spec(self.node("root", prompt="hold"), self.node("child", after=("root",))))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        node = running["nodes"][0]
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id=?", (node["operation_id"],)).fetchone()[0], "done")
        self.assertFalse(running["done"])
        self.assertEqual(running["nodes"][1]["status"], "pending")
        self.assertIsNone(node["result_ref"])

    async def test_another_turns_completion_cannot_complete_the_bound_node(self):
        result = await self.submit(self.spec(self.node("bound", prompt="hold"), self.node("after", after=("bound",))))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        node = running["nodes"][0]
        task = self.service.get(node["task_id"])
        for method, status in (("turn/started", "inProgress"), ("turn/completed", "completed")):
            await self.service.on_message({"method": method, "params": {"threadId": task["thread_id"],
                "turn": {"id": "other-turn", "status": status, "items": []}}})
        unknown = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        self.assertEqual(unknown["nodes"][0]["turn_id"], node["turn_id"])
        self.assertIsNone(unknown["nodes"][0]["result_ref"])
        self.assertEqual(unknown["nodes"][1]["status"], "pending")

    async def test_max_parallel_holds_dispatching_slots(self):
        result = await self.submit(self.spec(self.node("a", prompt="hold"), self.node("b", prompt="hold"), max_parallel=1))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        self.assertEqual(running["nodes"][1]["status"], "pending")
        first = running["nodes"][0]
        await self.service.interrupt(first["task_id"], first["turn_id"])
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual([n["status"] for n in final["nodes"]], ["failed", "skipped"])

    async def test_global_capacity_wait_is_retried_after_the_owner_finishes(self):
        self.service.config = replace(self.cfg, max_tasks=1)
        gate = self.gate_callback("owner-initial-events", "item/reasoning/textDelta")
        owner = await self.start_task("hold", path="owner")
        await gate.wait()
        gate.release.set()
        result = await self.submit(self.spec(self.node("waiting")))
        await self.settled()
        waiting = self.view(result["plan_id"])
        self.assertEqual((waiting["nodes"][0]["status"], waiting["nodes"][0]["reason"]), ("ready", "workspace_busy"))
        self.assertEqual(waiting["nodes"][0]["attempts"], 1)
        self.assertEqual(waiting["outcome"], "blocked")
        for _ in range(3):
            self.service.scheduler.schedule(retry=False)
            await self.settled()
        self.assertEqual(self.view(result["plan_id"])["nodes"][0]["attempts"], 1)
        await self.service.interrupt(owner["id"], owner["turn_id"])
        await self.until(owner["id"], ("interrupted",))
        await self.service.reconcile_claims(owner["id"], explicit=True)
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertGreaterEqual(final["nodes"][0]["attempts"], 2)

    async def test_busy_node_does_not_starve_an_independent_workspace(self):
        owner = await self.start_task("hold", path="busy-owner")
        result = await self.submit(self.spec(self.node("busy", target={"workdir": owner["project"]}), self.node("free"), max_parallel=1))
        done = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][1]["status"] == "succeeded")
        await self.settled()
        self.assertEqual(self.view(result["plan_id"])["nodes"][0]["status"], "ready")
        self.assertFalse(done["done"])

    async def test_project_alias_overlap_is_refused_without_any_plan_or_native_io(self):
        project = self.project("same")
        value = self.spec(self.node("a", target={"project_id": project["id"]}), self.node("b", target={"workdir": project["path"]}))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Overlap reached Codex")), self.assertRaises(BridgeError) as error:
            await self.submit(value)
        self.assertEqual(error.exception.code, "invalid_arguments")
        self.assertEqual(error.exception.details["plan_error"], "same_workspace_parallel")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)

    async def test_resolved_validation_failure_is_a_finished_rejection_without_registration(self):
        from codex_local_mcp import execution_plans
        path = self.root / "unregistered"
        path.mkdir()
        value = self.spec(self.node("large", target={"workdir": str(path)}))
        original, calls = execution_plans.plans.validate, []
        def validating(spec):
            calls.append(spec)
            if len(calls) == 3:  # Input validation, identity lookup, then resolved scope.
                raise plans.PlanError("too_large", "Plan exceeds 65536 UTF-8 bytes.")
            return original(spec)
        with patch.object(execution_plans.plans, "validate", validating), self.assertRaises(BridgeError) as error:
            await self.submit(value, "resolved-invalid")
        self.assertEqual(error.exception.code, "invalid_arguments")
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id='resolved-invalid'").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM plans").fetchone())

    async def test_unsent_admission_retries_keep_the_original_ready_age(self):
        self.service.config = replace(self.cfg, max_tasks=1)
        gate = self.gate_callback("age-owner-ready", "item/reasoning/textDelta")
        await self.start_task("hold", path="age-owner")
        await gate.wait()
        gate.release.set()
        now = [100]
        self.service.scheduler.clock = lambda: now[0]
        result = await self.submit(self.spec(self.node("aging")))
        await self.settled()
        first = self.view(result["plan_id"])["nodes"][0]
        self.assertEqual((first["status"], first["since"]), ("ready", 100))
        now[0] = 220
        self.service.scheduler.schedule()
        await self.settled()
        second = self.view(result["plan_id"])["nodes"][0]
        self.assertEqual((second["status"], second["since"]), ("ready", 100))
        self.assertEqual(second["attempts"], first["attempts"] + 1)

    async def test_uncertain_operation_cannot_advance_an_attempt_using_a_stale_rejection_flag(self):
        self.service.config = replace(self.cfg, max_tasks=1)
        gate = self.gate_callback("uncertain-owner-ready", "item/reasoning/textDelta")
        await self.start_task("hold", path="uncertain-owner")
        await gate.wait()
        gate.release.set()
        with patch.object(self.service.scheduler, "refresh", return_value=None):
            result = await self.submit(self.spec(self.node("uncertain-admission")))
            await self.settled()
            node = self.view(result["plan_id"])["nodes"][0]
            row = self.store.db.execute("SELECT state,result FROM operations WHERE id=?", (node["operation_id"],)).fetchone()
            self.assertEqual(row["state"], "done")
            self.assertTrue(json.loads(row["result"])["operation_rejected"])
            with self.store.transaction():
                self.store.db.execute("UPDATE operations SET state='uncertain' WHERE id=?", (node["operation_id"],))
        for _ in range(3):
            self.service.scheduler.schedule()
            await self.settled()
        current = self.view(result["plan_id"])["nodes"][0]
        self.assertEqual(current["status"], "uncertain")
        self.assertEqual(current["attempts"], 1)
        self.assertEqual(current["operation_id"], node["operation_id"])

    async def test_bare_send_review_unknown_scope_and_sensitive_prompts_fail_closed(self):
        for action in ("send", "review"):
            with self.subTest(action=action), self.assertRaisesRegex(BridgeError, "send and review nodes must name an earlier start node"):
                await self.submit(self.spec(self.node(action, action=action)), "bad-" + action)
        with self.assertRaises(BridgeError) as error:
            await self.submit(self.spec(self.node("unknown", target={"project_id": "missing"})), "missing-scope")
        self.assertEqual(error.exception.code, "invalid_arguments")
        secret = "sk-" + "x" * 48
        for field in ("prompt", "name"):
            value = self.spec(self.node("sensitive"))
            if field == "name":
                value[field] = secret
            else:
                value["nodes"][0][field] = secret
            with self.subTest(field=field), self.assertRaisesRegex(BridgeError, "A plan stores its prompts"):
                await self.submit(value, "secret-" + field)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM plans").fetchone())
        for table in ("operations", "operation_details"):
            self.assertNotIn(secret, str([tuple(r) for r in self.store.db.execute("SELECT * FROM " + table)]))

    async def test_changed_stored_spec_is_refused_before_dispatch(self):
        value = self.service.scheduler.submit(self.spec(self.node("immutable")))
        with self.store.transaction():
            spec = value["spec"]
            spec["nodes"][0]["prompt"] = "Changed after acceptance"
            self.store.db.execute("UPDATE plans SET spec=? WHERE id=?", (json.dumps(spec), value["plan_id"]))
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Changed spec reached Codex")):
            with self.assertRaises(BridgeError) as error:
                await self.settled()
        self.assertEqual(error.exception.code, "state_invalid")
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_plans", {"action": "inspect", "plan_id": value["plan_id"]})
        self.assertEqual(error.exception.code, "state_invalid")

    async def test_duplicate_canonical_submissions_reuse_plan_and_execution(self):
        value = self.spec(self.node("once", prompt="hold"))
        first = await self.submit(value)
        second = await self.submit(value, "other-submit")
        same_key = await self.submit(value)
        self.assertEqual({first["plan_id"], second["plan_id"], same_key["plan_id"]}, {first["plan_id"]})
        await self.settled()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)
        resolved = self.view(first["plan_id"])["spec"]
        resolved["nodes"][0]["target"] = {"workdir": self.project("once")["path"]}
        alias = await self.submit(resolved, "alias-submit")
        self.assertEqual(alias["plan_id"], first["plan_id"])

    async def test_duplicate_canonical_plan_remains_readable_after_its_project_is_removed(self):
        result = await self.submit(self.spec(self.node("old-project")))
        done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        project_id = done["spec"]["nodes"][0]["target"]["project_id"]
        project = self.store.get("projects", project_id)
        self.store.save("projects", {**project, "removed": True})
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Duplicate started native work")):
            repeated = await self.submit(done["spec"], "removed-project-submit")
            await self.settled()
        self.assertEqual(repeated["plan_id"], result["plan_id"])
        self.assertTrue(repeated["done"])

    async def test_send_and_review_reuse_the_start_task_and_native_identity(self):
        result = await self.submit(self.spec(self.node("root"), self.node("send", action="send", target={"node": "root"}, after=("root",)),
            self.node("review", action="review", target={"node": "root"}, after=("send",), prompt="Review carefully")))
        done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(done["outcome"], "succeeded")
        self.assertEqual(len({n["task_id"] for n in done["nodes"]}), 1)
        self.assertEqual(len({n["turn_id"] for n in done["nodes"]}), 3)

    async def test_budget_rejection_fails_the_node_and_obeys_stop_or_continue(self):
        self.service.config = replace(self.cfg, budget={"max_turns": 1})
        self.service.resources.defaults = {"max_turns": 1}
        for policy in ("stop", "continue"):
            result = await self.submit(self.spec(self.node("root-" + policy),
                self.node("capped-" + policy, action="send", target={"node": "root-" + policy}, after=("root-" + policy,), on_failure=policy),
                self.node("independent-" + policy, after=("root-" + policy,)), max_parallel=1), "budget-" + policy)
            done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
            self.assertEqual(done["nodes"][1]["status"], "failed")
            self.assertEqual(done["nodes"][1]["reason"], "budget_exceeded")
            self.assertEqual(done["nodes"][2]["status"], "skipped" if policy == "stop" else "succeeded")
            self.assertEqual(done["outcome"], "failed")

    async def test_terminal_budget_checkpoint_cannot_race_past_node_success(self):
        from tests.helpers import FaultGate
        terminal = self.gate_callback("terminal-budget-event", "turn/completed")
        result = await self.submit(self.spec(self.node("soft")))
        await terminal.wait()
        node = self.view(result["plan_id"])["nodes"][0]
        await self.settled()
        checkpoint = self.gates["terminal-budget-check"] = FaultGate("terminal-budget-check")
        original = self.service.check_budget
        async def postponed(family):
            await checkpoint.hit()
            await original(family)
        limit = self.service.get(node["task_id"])["family_usage"]["output_bytes"]["total"] + 200
        with patch.object(self.service, "check_budget", postponed):
            self.service.resources.set_budget(node["task_id"], {"max_output_bytes": limit})
            terminal.release.set()
            try:
                await checkpoint.wait()
                done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
                self.assertEqual(done["nodes"][0]["status"], "failed")
                self.assertEqual(done["nodes"][0]["reason"], "budget_exceeded")
                self.assertTrue(self.service.get(node["task_id"])["budget_stop"]["may_overshoot"])
            finally:
                checkpoint.release.set()
                if self.service.budget_tasks:
                    await asyncio.wait_for(asyncio.gather(*list(self.service.budget_tasks.values())), 5)

    async def test_failed_upstream_skips_descendants_and_continues_an_independent_branch(self):
        result = await self.submit(self.spec(self.node("bad", prompt="fail", on_failure="continue"), self.node("descendant", after=("bad",)), self.node("independent")))
        done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual([n["status"] for n in done["nodes"]], ["failed", "skipped", "succeeded"])
        self.assertEqual(done["nodes"][1]["reason"], "upstream_failed")

    async def test_pending_approval_is_visible_and_never_answered_by_the_plan(self):
        result = await self.submit(self.spec(self.node("approval", prompt="approval"), self.node("after", after=("approval",))))
        waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["pending_requests_total"] == 1)
        self.assertEqual(waiting["nodes"][1]["status"], "pending")
        self.assertFalse(waiting["done"])
        request = waiting["nodes"][0]["pending_requests"][0]
        self.assertNotIn("params", request)
        self.assertTrue(request["details_truncated"])
        with self.assertRaises(BridgeError) as error:
            await dispatch(self.service, "codex_respond", {"request_id": request["id"], "response": {"decision": "accept"}})
        self.assertEqual(error.exception.code, "human_review_required")
        await dispatch(self.service, "codex_respond", {"request_id": request["id"], "response": {"decision": "decline"}})
        self.assertEqual((await self.wait_plan(result["plan_id"], lambda p: p["done"]))["outcome"], "succeeded")

    async def test_required_child_outcome_survives_a_parent_terminal_event(self):
        result = await self.submit(self.spec(self.node("parent", prompt="native_children"), self.node("after", after=("parent",))))
        waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["pending_requests_total"] == 1)
        parent = waiting["nodes"][0]
        task = self.service.get(parent["task_id"])
        await self.service.on_message({"method": "turn/completed", "params": {"threadId": task["thread_id"],
            "turn": {"id": parent["turn_id"], "status": "completed", "items": []}}})
        await self.settled()
        self.assertEqual(self.view(result["plan_id"])["nodes"][0]["status"], "running")
        self.assertEqual(self.view(result["plan_id"])["nodes"][1]["status"], "pending")
        await dispatch(self.service, "codex_respond", {"request_id": waiting["nodes"][0]["pending_requests"][0]["id"], "response": {"decision": "decline"}})
        self.assertEqual((await self.wait_plan(result["plan_id"], lambda p: p["done"]))["outcome"], "succeeded")

    async def test_failed_required_child_is_a_named_failure_even_when_parent_completed(self):
        result = await self.submit(self.spec(self.node("parent", prompt="native_children"), self.node("after", after=("parent",))))
        waiting = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["pending_requests_total"] == 1)
        node = waiting["nodes"][0]
        parent = self.service.get(node["task_id"])
        request = self.store.get("requests", node["pending_requests"][0]["id"])
        for thread_id, turn_id, status in ((parent["thread_id"], node["turn_id"], "completed"),
                (request["params"]["threadId"], request["params"]["turnId"], "failed")):
            await self.service.on_message({"method": "turn/completed", "params": {"threadId": thread_id,
                "turn": {"id": turn_id, "status": status, "items": []}}})
        failed = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(failed["nodes"][0]["status"], "failed")
        self.assertEqual(failed["nodes"][0]["reason"], "descendant_failed")
        self.assertEqual(failed["nodes"][1]["reason"], "plan_stopped")

    async def test_lost_ack_is_never_replayed_and_resume_unlocks_dependents(self):
        gate = self.gate_call("lost-plan-ack", "turn/start", error=TimeoutError("Applied start lost acknowledgement"))
        result = await self.submit(self.spec(self.node("uncertain"), self.node("after", after=("uncertain",))))
        try:
            await gate.wait()
        finally:
            gate.release.set()
        uncertain = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        await self.until(uncertain["nodes"][0]["task_id"], ("completed",))
        self.assertEqual(uncertain["nodes"][1]["status"], "pending")
        for _ in range(3):
            self.service.scheduler.schedule()
            await self.settled()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)
        await dispatch(self.service, "codex_resume", {"task_id": uncertain["nodes"][0]["task_id"], "request_id": "reconcile-plan"})
        done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(done["outcome"], "succeeded")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 2)

    async def test_cancel_interrupts_once_and_cancels_unstarted_nodes(self):
        result = await self.submit(self.spec(self.node("run", prompt="hold"), self.node("after", after=("run",))))
        await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        calls, original = [], self.service.backend.call
        async def record(method, params, **kwargs):
            if method == "turn/interrupt":
                calls.append(params["turnId"])
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", record):
            await self.cancel(result["plan_id"])
            await self.cancel(result["plan_id"], "cancel-again")
            done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(done["outcome"], "cancelled")
        self.assertEqual(done["nodes"][1]["status"], "cancelled")
        self.assertEqual(len(calls), 1)

    async def test_uncertain_running_work_keeps_its_parallel_slot(self):
        gate = self.gate_call("running-lost-ack", "turn/start", error=TimeoutError("Unknown start acknowledgement"))
        result = await self.submit(self.spec(self.node("unknown", prompt="hold", on_failure="continue"), self.node("independent"), max_parallel=1))
        await gate.wait()
        gate.release.set()
        blocked = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain" and p["outcome"] == "blocked")
        self.assertEqual(blocked["nodes"][1]["status"], "pending")
        self.assertEqual(blocked["nodes"][1]["reason"], "capacity")
        self.assertEqual(blocked["nodes"][1]["blocked_by"], ["unknown"])
        task_id = blocked["nodes"][0]["task_id"]
        await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "running-reconcile"})
        await self.settled()
        self.assertEqual(self.view(result["plan_id"])["nodes"][1]["status"], "pending")
        current = self.service.get(task_id)
        await self.service.interrupt(task_id, current["turn_id"])
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual([n["status"] for n in final["nodes"]], ["failed", "succeeded"])

    async def test_cancel_waits_for_personal_reconciliation_of_uncertain_running_work(self):
        gate = self.gate_call("cancel-running-unknown", "turn/start", error=TimeoutError("Unknown start acknowledgement"))
        result = await self.submit(self.spec(self.node("running", prompt="hold")))
        await gate.wait()
        gate.release.set()
        unknown = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        calls, original = [], self.service.backend.call
        async def record(method, params, **kwargs):
            if method == "turn/interrupt":
                calls.append(params["turnId"])
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", record):
            await self.cancel(result["plan_id"])
            await self.settled()
            self.assertEqual(calls, [])
            await dispatch(self.service, "codex_resume", {"task_id": unknown["nodes"][0]["task_id"], "request_id": "reconcile-then-cancel"})
            final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(final["outcome"], "cancelled")
        self.assertEqual(calls, [unknown["nodes"][0]["turn_id"]])

    async def test_manual_fork_is_not_mislabelled_as_its_sources_plan_node(self):
        result = await self.submit(self.spec(self.node("source")))
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        fork = await dispatch(self.service, "codex_fork", {"task_id": final["nodes"][0]["task_id"], "request_id": "manual-fork"})
        self.assertIsNone(fork.get("plan_id"))
        self.assertIsNone(fork.get("plan_node_id"))

    async def test_restart_with_a_running_node_requires_resume_and_never_replays_it(self):
        result = await self.submit(self.spec(self.node("running", prompt="hold"), self.node("after", after=("running",))))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        before = self.service.scheduler.record(result["plan_id"])["spec"]
        await self.service.close()
        self.service = self.new_service()
        await self.settled()
        view = self.view(result["plan_id"])
        self.assertEqual(self.service.scheduler.record(result["plan_id"])["spec"], before)
        self.assertEqual(view["nodes"][0]["status"], "uncertain")
        self.assertEqual(view["nodes"][1]["status"], "pending")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)
        await dispatch(self.service, "codex_resume", {"task_id": running["nodes"][0]["task_id"], "request_id": "restart-resume"})
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual([n["status"] for n in final["nodes"]], ["failed", "skipped"])

    async def test_crash_before_dispatch_reuses_the_saved_operation_key(self):
        from codex_local_mcp import execution_plans
        from tests.helpers import FaultGate
        gate = self.gates["pre-dispatch"] = FaultGate("pre-dispatch")
        original, sent = execution_plans.dispatch, []
        native_calls = []
        self.count_starts(native_calls)
        async def gated(service, name, args):
            if name == "codex_start":
                if not gate.used:
                    await gate.hit()
                sent.append(args["request_id"])
            return await original(service, name, args)
        with patch.object(execution_plans, "dispatch", gated):
            result = await self.submit(self.spec(self.node("before")))
            await gate.wait()
            self.assertFalse(self.store.db.execute("SELECT 1 FROM operations WHERE id LIKE 'plan:%'").fetchone())
            expected = self.view(result["plan_id"])["nodes"][0]["operation_id"]
            await self.service.close()
            self.service = self.new_service()
            self.count_starts(native_calls)
            final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(sent, [expected])
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)

    async def test_crash_after_dispatch_ack_does_not_start_the_finished_turn_twice(self):
        native_calls = []
        self.count_starts(native_calls)
        gate = self.gate_callback("finished-before-observation", "turn/completed")
        result = await self.submit(self.spec(self.node("finished"), self.node("after", after=("finished",))))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        await gate.wait()
        self.service.scheduler.enabled = False
        node = running["nodes"][0]
        gate.release.set()
        await self.until(node["task_id"], ("completed",))
        await self.service.close()
        self.service = self.new_service()
        self.count_starts(native_calls)
        final = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(final["nodes"][0]["operation_id"], node["operation_id"])
        self.assertEqual(len(native_calls), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns WHERE task_id=?", (node["task_id"],)).fetchone()[0], 1)

    async def test_cancel_of_uncertain_work_stays_blocked_until_explicit_reconciliation(self):
        gate = self.gate_call("uncertain-cancel", "turn/start", error=TimeoutError("Unknown start acknowledgement"))
        result = await self.submit(self.spec(self.node("unknown"), self.node("after", after=("unknown",))))
        await gate.wait()
        gate.release.set()
        unknown = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "uncertain")
        await self.until(unknown["nodes"][0]["task_id"], ("completed",))
        await self.cancel(result["plan_id"])
        await self.settled()
        waiting = self.view(result["plan_id"])
        self.assertEqual(waiting["outcome"], "blocked")
        self.assertFalse(waiting["done"])
        self.assertEqual(waiting["nodes"][1]["status"], "cancelled")
        await dispatch(self.service, "codex_resume", {"task_id": unknown["nodes"][0]["task_id"], "request_id": "cancel-reconcile"})
        self.assertEqual((await self.wait_plan(result["plan_id"], lambda p: p["done"]))["outcome"], "cancelled")

    async def test_unknown_interrupt_ack_blocks_cancel_and_is_not_retried(self):
        result = await self.submit(self.spec(self.node("interrupt-unknown", prompt="hold")))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        gate = self.gate_call("unknown-interrupt", "turn/interrupt", before=True, error=TimeoutError("Interrupt outcome unknown"))
        await self.cancel(result["plan_id"])
        await gate.wait()
        job = self.service.scheduler.jobs[("cancel", result["plan_id"], "interrupt-unknown")]
        gate.release.set()
        await asyncio.gather(job, return_exceptions=True)
        await self.settled()
        blocked = self.view(result["plan_id"])
        self.assertEqual(blocked["nodes"][0]["status"], "uncertain")
        self.assertEqual(blocked["outcome"], "blocked")
        await self.cancel(result["plan_id"], "unknown-cancel-again")
        await self.settled()
        task_id, turn_id = running["nodes"][0]["task_id"], running["nodes"][0]["turn_id"]
        await dispatch(self.service, "codex_resume", {"task_id": task_id, "request_id": "unknown-cancel-resume"})
        await self.settled()
        self.assertFalse(self.view(result["plan_id"])["done"])
        await dispatch(self.service, "codex_interrupt", {"task_id": task_id, "expected_turn_id": turn_id, "request_id": "explicit-new-interrupt"})
        self.assertEqual((await self.wait_plan(result["plan_id"], lambda p: p["done"]))["outcome"], "cancelled")

    async def test_finished_plan_references_are_retained_until_the_plan_expires(self):
        result = await self.submit(self.spec(self.node("retained")))
        done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        await self.service.reconcile_claims(done["nodes"][0]["task_id"], explicit=True)
        clock = done["updated_at"] + 200
        retention = Retention(self.store, clock=lambda: clock)
        protected = retention.plan(300)
        self.assertEqual(protected["counts"]["plans"], 0)
        self.assertGreater(protected["protected"]["dependency_plans"]["items"], 0)
        expired = retention.plan(100)
        self.assertEqual(expired["counts"]["plans"], 1)
        retention.commit(expired["plan_id"])
        self.assertFalse(self.store.db.execute("SELECT 1 FROM plans").fetchone())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM plan_nodes").fetchone())
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM operation_tombstones WHERE id=?", (done["nodes"][0]["operation_id"],)).fetchone())
        native_calls, original = [], self.service.backend.call
        async def record(method, params, **kwargs):
            if method == "turn/start":
                native_calls.append(method)
            return await original(method, params, **kwargs)
        with patch.object(self.service.backend, "call", record):
            with self.assertRaises(BridgeError) as error:
                await self.submit(result["spec"], "expired-plan")
        self.assertEqual(error.exception.code, "operation_expired")
        self.assertEqual(native_calls, [])

    async def test_pruned_cancelled_plan_cannot_be_resurrected_but_a_renamed_copy_runs(self):
        project = self.project("cancelled-before-work")
        value = self.spec(self.node("unrun", target={"workdir": project["path"]}))
        with patch.object(self.service.scheduler, "schedule", return_value=None):
            result = await self.submit(value)
            cancelled = await self.cancel(result["plan_id"])
        self.assertTrue(cancelled["done"])
        self.assertEqual(cancelled["nodes"][0]["status"], "cancelled")
        self.assertFalse(self.store.db.execute("SELECT 1 FROM resource_turns").fetchone())
        retention = Retention(self.store, clock=lambda: cancelled["updated_at"] + 200)
        proposed = retention.plan(100)
        self.assertEqual(proposed["plan_tombstones_added"], 1)
        retention.commit(proposed["plan_id"])
        tombstone = self.store.db.execute("SELECT value FROM metadata WHERE key=?", ("dependency_plan_expired:" + result["plan_id"],)).fetchone()
        self.assertEqual(set(json.loads(tombstone[0])), {"plan_id", "outcome", "pruned_at"})
        with self.assertRaisesRegex(BridgeError, "removed by cleanup") as error:
            self.view(result["plan_id"])
        self.assertEqual(error.exception.code, "not_found")
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Expired plan ran")):
            for index, spec in enumerate((value, cancelled["spec"])):
                with self.assertRaises(BridgeError) as error:
                    await self.submit(spec, "expired-" + str(index))
                self.assertEqual(error.exception.code, "operation_expired")
        fresh = await self.submit({**value, "name": "New explicit work"}, "renamed-plan")
        final = await self.wait_plan(fresh["plan_id"], lambda p: p["done"])
        self.assertNotEqual(final["plan_id"], result["plan_id"])
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resource_turns").fetchone()[0], 1)

    async def test_undone_plan_and_its_operation_are_protected_from_prune(self):
        result = await self.submit(self.spec(self.node("live", prompt="hold")))
        running = await self.wait_plan(result["plan_id"], lambda p: p["nodes"][0]["status"] == "running")
        retention = Retention(self.store, clock=lambda: running["updated_at"] + 1000)
        planned = retention.plan(100)
        saved = json.loads(self.store.db.execute("SELECT data FROM retention_plans WHERE id=?", (planned["plan_id"],)).fetchone()[0])
        self.assertEqual(planned["counts"]["plans"], 0)
        self.assertNotIn(running["nodes"][0]["operation_id"], saved["selected"]["operations"])

    async def test_plan_change_after_prune_planning_refuses_the_commit(self):
        result = await self.submit(self.spec(self.node("prunable")))
        done = await self.wait_plan(result["plan_id"], lambda p: p["done"])
        await self.service.reconcile_claims(done["nodes"][0]["task_id"], explicit=True)
        retention = Retention(self.store, clock=lambda: done["updated_at"] + 200)
        proposal = retention.plan(100)
        self.assertEqual(proposal["counts"]["plans"], 1)
        with self.store.transaction():
            self.store.db.execute("UPDATE plans SET revision=revision+1 WHERE id=?", (result["plan_id"],))
        with self.assertRaises(BridgeError) as error:
            retention.commit(proposal["plan_id"])
        self.assertEqual(error.exception.code, "prune_plan_invalid")
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM plans WHERE id=?", (result["plan_id"],)).fetchone())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM plan_nodes WHERE plan_id=?", (result["plan_id"],)).fetchone()[0], 1)

    async def test_list_pages_and_all_actions_validate_against_output_schema(self):
        result = await self.submit(self.spec(self.node("forms", prompt="hold")))
        for action, fields in (("inspect", {"plan_id": result["plan_id"]}), ("list", {"limit": 1}),
                ("cancel", {"plan_id": result["plan_id"], "operation_id": "forms-cancel"})):
            value = await dispatch(self.service, "codex_plans", {"action": action, **fields})
            validate(value, BY_NAME["codex_plans"]["outputSchema"], "result")
        with self.assertRaisesRegex(BridgeError, "does not apply"):
            arguments("codex_plans", {"action": "list", "spec": {}})
        self.assertEqual(arguments("codex_plans", {"action": "inspect", "plan_id": result["plan_id"], "offset": 0, "limit": 20}),
            {"action": "inspect", "plan_id": result["plan_id"]})

    async def test_catalog_stays_inside_the_existing_limits(self):
        self.assertLessEqual(encoded_size({"tools": tool_catalog("2024-11-05")}), 40000)
        self.assertLessEqual(encoded_size({"tools": tool_catalog("2025-06-18")}), 54000)


class ManagedPlanTest(ServiceHarness):
    def _tearDownAsyncioRunner(self):
        loop = getattr(self, "worktree_loop", None)
        if loop is not None and not loop.is_closed():
            with thread_wakeups(loop):
                return super()._tearDownAsyncioRunner()
        return super()._tearDownAsyncioRunner()

    async def test_plan_uses_two_managed_worktrees_as_independent_projects(self):
        from codex_local_mcp import worktrees as git
        from tests.test_managed_worktrees import isolated_git
        self.worktree_loop = asyncio.get_running_loop()
        self.enterContext(thread_wakeups())
        self.enterContext(isolated_git(self.base))
        self.service.config = replace(self.cfg, worktrees=True)
        repo = self.root / "repo"
        repo.mkdir()
        for args in (["init", "--initial-branch=main"], ["config", "user.name", "Offline"],
                ["config", "user.email", "offline@example.invalid"], ["commit", "--allow-empty", "-m", "fixture"]):
            await asyncio.to_thread(git._git, args, repo, write=True)
        nodes, selected = [], []
        for name in ("left", "right"):
            prepared = await dispatch(self.service, "codex_worktrees", {"action": "plan", "repo": str(repo), "name": name})
            created = await dispatch(self.service, "codex_worktrees", {"action": "create", "repo": str(repo), "name": name,
                "plan_id": prepared["plan_id"], "request_id": "create-" + name})
            selected.append(created["project_id"])
            nodes.append({"id": name, "action": "start", "target": {"project_id": created["project_id"]}, "prompt": "hold"})
        result = await dispatch(self.service, "codex_plans", {"action": "submit", "spec": {"version": 1, "name": "Managed projects", "nodes": nodes}, "request_id": "managed-plan"})
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: all(n["status"] == "running" for n in self.service.scheduler.inspect(result["plan_id"])["nodes"])), 10)
        active = self.service.scheduler.inspect(result["plan_id"])
        self.assertEqual({self.service.get(n["task_id"])["project_id"] for n in active["nodes"]}, set(selected))
        self.assertEqual(len({n["task_id"] for n in active["nodes"]}), 2)
        await dispatch(self.service, "codex_plans", {"action": "cancel", "plan_id": result["plan_id"], "request_id": "cancel-managed"})
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: self.service.scheduler.inspect(result["plan_id"])["done"]), 10)
