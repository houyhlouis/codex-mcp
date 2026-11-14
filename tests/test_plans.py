import copy
import json
import random
import unittest
from unittest.mock import patch

from codex_local_mcp import plans


def node(identity, *, after=(), target=None, action="start", **fields):
    return {"id": identity, "action": action, "target": target or {"project_id": "project-" + identity},
        "prompt": "Work on this node", "after": list(after), **fields}


def spec(*nodes, **fields):
    return {"version": 1, "name": "Example", "nodes": list(nodes), **fields}


def snapshot(**statuses):
    return {"nodes": {identity: {"status": status, "since": 0, "task_id": None, "result_ref": None}
        for identity, status in statuses.items()}}


class PlanValidationTest(unittest.TestCase):
    def error(self, code, value):
        with self.assertRaises(plans.PlanError) as caught:
            plans.validate(value)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def test_defaults_canonical_identity_and_no_input_mutation(self):
        value = spec(node("a"), node("b"), node("c", after=("b", "a", "a")))
        before = copy.deepcopy(value)
        canonical = plans.validate(value)
        self.assertEqual(value, before)
        self.assertEqual(canonical["max_parallel"], 2)
        self.assertEqual(canonical["nodes"][0]["priority"], 0)
        self.assertEqual(canonical["nodes"][0]["on_failure"], "stop")
        self.assertTrue(canonical["nodes"][0]["handoff"])
        self.assertEqual(canonical["nodes"][2]["after"], ["a", "b"])
        self.assertEqual(plans.validate(canonical), canonical)
        reordered_keys = json.loads(json.dumps(value, sort_keys=True))
        self.assertEqual(plans.plan_id(canonical), plans.plan_id(reordered_keys))
        self.assertEqual(plans.plan_id(value), plans.plan_id(plans.validate(value)))
        self.assertEqual(len(plans.plan_id(value)), 64)
        canonical["nodes"][0]["prompt"] = "Different work"
        self.assertNotEqual(plans.plan_id(value), plans.plan_id(canonical))
        self.assertEqual(value, before)

    def test_registry_resolution_pairs_are_derived_and_cannot_be_forged(self):
        value = spec(node("a"), node("b", target={"workdir": "/project/b"}), node("c", after=("a",)))
        canonical = plans.validate(value)
        self.assertEqual(canonical["needs_resolution"], [["a", "b"], ["b", "c"]])
        self.assertEqual(plans.validate(canonical), canonical)
        canonical["needs_resolution"] = []
        self.error("invalid_plan", canonical)
        paths = plans.validate(spec(node("a", target={"workdir": "/first"}), node("b", target={"workdir": "/second"})))
        self.assertEqual(paths["needs_resolution"], [["a", "b"]])

    def test_duplicate_unknown_dependency_and_cycle_have_specific_codes(self):
        self.assertEqual(self.error("duplicate_id", spec(node("a"), node("a"))), "Duplicate node id: a.")
        self.assertEqual(self.error("unknown_dependency", spec(node("a", after=("absent",)))),
            "Node a depends on unknown node absent.")
        self.assertEqual(self.error("cycle", spec(node("a", after=("c",)), node("b", after=("a",)), node("c", after=("b",)))),
            "Dependency cycle: a -> c -> b -> a.")
        self.assertIn("a -> a", self.error("cycle", spec(node("a", after=("a",)))))

    def test_reuse_requires_a_start_node_ancestor_not_array_position(self):
        value = spec(node("send", action="send", after=("middle",), target={"node": "start"}),
            node("middle", after=("start",)), node("start"))
        self.assertEqual(plans.validate(value)["nodes"][0]["target"], {"node": "start"})
        for value in (spec(node("a", target={"node": "a"})),
                spec(node("a"), node("b", action="send", target={"node": "a"})),
                spec(node("a"), node("b", action="review", after=("a",)),
                    node("c", action="send", after=("b",), target={"node": "b"})),
                spec(node("a", action="review", target={"node": "absent"}))):
            with self.subTest(value=value):
                self.error("invalid_target", value)

    def test_same_project_refused_even_with_max_parallel_one(self):
        left, right = node("a"), node("b", target={"project_id": "project-a"})
        self.assertEqual(self.error("same_workspace_parallel", spec(left, right, max_parallel=1)),
            "Nodes a and b may run in the same workspace; add a dependency.")
        right["after"] = ["a"]
        plans.validate(spec(left, right))

    def test_workdir_overlap_and_siblings_use_path_components(self):
        for other in ("/workspace/project", "/workspace/project/child", "/workspace"):
            self.error("same_workspace_parallel", spec(node("a", target={"workdir": "/workspace/project"}),
                node("b", target={"workdir": other})))
        plans.validate(spec(node("a", target={"workdir": "/workspace/project"}),
            node("b", target={"workdir": "/workspace/project-sibling"})))
        canonical = plans.validate(spec(node("a", target={"workdir": "//workspace///project/./"})))
        self.assertEqual(canonical["nodes"][0]["target"], {"workdir": "/workspace/project"})
        plans.validate(spec(node("a", target={"workdir": "/workspace/Project"}),
            node("b", target={"workdir": "/workspace/project"})))

    def test_windows_paths_are_checked_purely_on_every_platform(self):
        self.error("same_workspace_parallel", spec(node("a", target={"workdir": r"C:\Workspace\Project"}),
            node("b", target={"workdir": r"c:/workspace/project/child"})))
        plans.validate(spec(node("a", target={"workdir": r"C:\workspace\project"}),
            node("b", target={"workdir": r"D:\workspace\project"})))
        for value in ("relative", "~/project", "/workspace/../other", "C:relative", r"\rooted", r"\\server\share", "/project\nsecret", r"C:\con\project"):
            with self.subTest(path=value):
                self.error("invalid_target", spec(node("a", target={"workdir": value})))

    def test_parallel_followups_of_one_task_are_refused(self):
        self.error("same_workspace_parallel", spec(node("a"),
            node("b", action="send", after=("a",), target={"node": "a"}),
            node("c", action="review", after=("a",), target={"node": "a"})))
        plans.validate(spec(node("a"), node("b", action="send", after=("a",), target={"node": "a"}),
            node("c", action="review", after=("b",), target={"node": "a"})))

    def test_shape_unknown_keys_and_boolean_integers_are_refused(self):
        values = [None, [], {}, spec(), spec(node("a"), version=True), spec(node("a"), version=2),
            spec(node("a"), name=3), spec(node("a"), name="n" * 65), spec(node("a"), extra="ignored"),
            spec(node("a", extra="ignored")), spec(node("a", action="raw_rpc")), spec(node("a", prompt=None)),
            spec(node("a", priority=True)), spec(node("a", priority=-11)), spec(node("a", priority=11)),
            spec(node("a", on_failure="retry")), spec(node("a", handoff=1)), spec({**node("a"), "after": "b"}),
            spec(node("a", after=[None])), spec(node("a"), max_parallel=True), spec(node("a"), max_parallel=0),
            spec(node("a"), max_parallel=5), spec(node("a", id="")), spec(node("a", id="a" * 33)),
            spec(node("a", id="a b")), spec(node("a", id="界"))]
        for value in values:
            with self.subTest(value=value):
                self.error("invalid_plan", value)
        for target in ({}, {"project_id": "a", "workdir": "/a"}, {"unknown": "a"}, {"project_id": ""}, {"project_id": None}, []):
            with self.subTest(target=target):
                value = node("a")
                value["target"] = target
                self.error("invalid_target", spec(value))

    def test_node_limit_id_alphabet_and_review_optional_prompt(self):
        plans.validate(spec(*(node(str(index)) for index in range(16))))
        self.error("invalid_plan", spec(*(node(str(index)) for index in range(17))))
        for identity in (".", "..", "-", "_", "a.b-c_9", "a" * 32):
            self.assertEqual(plans.validate(spec(node(identity)))["nodes"][0]["id"], identity)
        value = node("review", action="review")
        del value["prompt"]
        self.assertEqual(plans.validate(spec(value))["nodes"][0]["prompt"], "")
        for action in ("start", "send"):
            value = node(action, action=action)
            del value["prompt"]
            self.error("invalid_plan", spec(value))

    def test_utf8_prompt_and_total_byte_limits_and_bad_json(self):
        plans.validate(spec(node("a", prompt="😀" * 4096)))
        self.error("too_large", spec(node("a", prompt="😀" * 4096 + "x")))
        self.error("too_large", spec(*(node(str(index), prompt="x" * 16384) for index in range(4))))
        value = spec(node("a"))
        value["nodes"][0]["after"] = ["a"] * 20000
        self.error("too_large", value)
        for extra in (float("nan"), float("inf"), b"bytes", "\ud800"):
            self.error("invalid_plan", spec(node("a"), extra=extra))
        value = spec(node("a"))
        value["extra"] = value
        self.error("invalid_plan", value)

    def test_validation_and_handoff_do_not_read_files_or_start_processes(self):
        value = spec(node("a", target={"workdir": "/not-present/project"}), node("b", after=("a",)))
        with patch("builtins.open", side_effect=AssertionError("File read")), patch("os.stat", side_effect=AssertionError("Filesystem lookup")), patch("subprocess.Popen", side_effect=AssertionError("Process start")):
            canonical = plans.validate(value)
            self.assertEqual(len(plans.plan_id(canonical)), 64)
            self.assertIn("a |", plans.handoff_text(canonical, "b", {}))
            self.assertEqual(plans.step(canonical, {"nodes": {}}, 0)["dispatch"], ["a"])


class PlanStepTest(unittest.TestCase):
    def test_diamond_dependencies_dispatch_only_after_verified_success(self):
        canonical = plans.validate(spec(node("root"), node("left", after=("root",)),
            node("right", after=("root",)), node("join", after=("left", "right"))))
        state = snapshot(root="pending", left="pending", right="pending", join="pending")
        self.assertEqual(plans.step(canonical, state, 0)["dispatch"], ["root"])
        for active in ("dispatching", "running"):
            state["nodes"]["root"]["status"] = active
            self.assertEqual(plans.step(canonical, state, 1)["dispatch"], [])
        state["nodes"]["root"].update(status="succeeded", since=10)
        self.assertEqual(plans.step(canonical, state, 10)["dispatch"], ["left", "right"])
        state["nodes"]["left"]["status"] = "succeeded"
        state["nodes"]["right"]["status"] = "running"
        self.assertEqual(plans.step(canonical, state, 20)["dispatch"], [])
        state["nodes"]["right"].update(status="succeeded", since=30)
        self.assertEqual(plans.step(canonical, state, 30)["dispatch"], ["join"])
        state["nodes"]["join"].update(status="succeeded", since=40)
        decision = plans.step(canonical, state, 40)
        self.assertTrue(decision["done"])
        self.assertEqual(decision["outcome"], "succeeded")

    def test_bootstrap_and_reserved_node_names_use_one_snapshot_shape(self):
        canonical = plans.validate(spec(node("nodes"), node("cancel_requested")))
        self.assertEqual(plans.step(canonical, {"nodes": {}}, 0)["dispatch"], ["nodes", "cancel_requested"])
        for state in ({}, {"nodes": None}, {"a": {"status": "pending"}}, {"nodes": {}, "cancel_requested": 1}):
            with self.subTest(state=state), self.assertRaises(plans.PlanError):
                plans.step(canonical, state, 0)

    def test_parallel_limit_counts_dispatching_and_running(self):
        canonical = plans.validate(spec(*(node(str(index)) for index in range(6)), max_parallel=3))
        state = snapshot(**{"0": "dispatching", "1": "running", "2": "ready", "3": "ready", "4": "ready", "5": "pending"})
        decision = plans.step(canonical, state, 1)
        self.assertEqual(decision["dispatch"], ["2"])
        self.assertEqual([record["id"] for record in decision["blocked"] if record["reason"] == "capacity"], ["3", "4", "5"])
        state["nodes"]["0"]["status"] = "running"
        state["nodes"]["2"]["status"] = "dispatching"
        self.assertEqual(plans.step(canonical, state, 2)["dispatch"], [])

    def test_priority_fifo_tie_breaker_and_aging_prevent_starvation(self):
        canonical = plans.validate(spec(node("low", priority=-10), node("high", priority=10), max_parallel=1))
        state = snapshot(low="ready", high="ready")
        self.assertEqual(plans.step(canonical, state, 0)["dispatch"], ["high"])
        state["nodes"]["high"]["since"] = 1200
        self.assertEqual(plans.step(canonical, state, 1199)["dispatch"], ["high"])
        self.assertEqual(plans.step(canonical, state, 1200)["dispatch"], ["low"])
        canonical = plans.validate(spec(node("z"), node("a"), max_parallel=1))
        state = snapshot(z="ready", a="ready")
        self.assertEqual(plans.step(canonical, state, 0)["dispatch"], ["z"])
        state["nodes"]["a"]["since"] = -1
        self.assertEqual(plans.step(canonical, state, 0)["dispatch"], ["a"])

    def test_pending_age_begins_at_dependency_success_not_plan_creation(self):
        canonical = plans.validate(spec(node("parent"), node("child", after=("parent",)), node("old"), max_parallel=1))
        state = snapshot(parent="succeeded", child="pending", old="ready")
        state["nodes"]["parent"]["since"] = 1200
        state["nodes"]["old"]["since"] = 1190
        self.assertEqual(plans.step(canonical, state, 1200)["dispatch"], ["old"])

    def test_failed_stop_skips_all_unstarted_nodes_without_interrupting_work(self):
        canonical = plans.validate(spec(node("failed"), node("child", after=("failed",)),
            node("grandchild", after=("child",)), node("independent"), node("working")))
        state = snapshot(failed="failed", child="pending", grandchild="ready", independent="pending", working="running")
        decision = plans.step(canonical, state, 0)
        self.assertEqual(decision["dispatch"], [])
        self.assertEqual(decision["interrupt"], [])
        self.assertEqual(decision["skip"], [{"id": identity, "status": "skipped", "reason": "plan_stopped"}
            for identity in ("child", "grandchild", "independent")])
        self.assertFalse(decision["done"])
        self.assertEqual(decision["outcome"], "running")
        state["nodes"]["working"]["status"] = "succeeded"
        decision = plans.step(canonical, state, 1)
        self.assertTrue(decision["done"])
        self.assertEqual(decision["outcome"], "failed")

    def test_failed_continue_skips_descendants_and_runs_independent_branch(self):
        canonical = plans.validate(spec(node("failed", on_failure="continue"), node("child", after=("failed",)),
            node("grandchild", after=("child",)), node("independent")))
        state = snapshot(failed="failed", child="pending", grandchild="pending", independent="pending")
        decision = plans.step(canonical, state, 0)
        self.assertEqual(decision["dispatch"], ["independent"])
        self.assertEqual(decision["skip"], [{"id": identity, "status": "skipped", "reason": "upstream_failed"}
            for identity in ("child", "grandchild")])
        # A skipped descendant's default stop policy must not stop the other branch.
        state["nodes"]["child"]["status"] = "skipped"
        self.assertEqual(plans.step(canonical, state, 1)["dispatch"], ["independent"])
        state["nodes"]["independent"]["status"] = "succeeded"
        decision = plans.step(canonical, state, 2)
        self.assertTrue(decision["done"])
        self.assertEqual(decision["outcome"], "failed")

    def test_uncertainty_blocks_without_retry_and_explicit_reconciliation_resumes(self):
        canonical = plans.validate(spec(node("uncertain"), node("child", after=("uncertain",)), node("independent")))
        state = snapshot(uncertain="uncertain", child="pending", independent="pending")
        decision = plans.step(canonical, state, 0)
        self.assertEqual(decision["dispatch"], ["independent"])
        self.assertEqual(decision["skip"], [])
        self.assertIn({"id": "uncertain", "reason": "uncertain_node"}, decision["blocked"])
        self.assertIn({"id": "child", "reason": "upstream_uncertain"}, decision["blocked"])
        state["nodes"]["independent"]["status"] = "succeeded"
        self.assertEqual(plans.step(canonical, state, 1)["outcome"], "blocked")
        after = copy.deepcopy(state)
        after["nodes"]["uncertain"].update(status="succeeded", since=2)
        plans.check_transition(state, after, reconciled=("uncertain",))
        self.assertEqual(plans.step(canonical, after, 2)["dispatch"], ["child"])

    def test_cancel_preserves_running_and_dispatching_until_terminal_confirmation(self):
        canonical = plans.validate(spec(node("running"), node("dispatching"), node("pending"), node("ready")))
        state = snapshot(running="running", dispatching="dispatching", pending="pending", ready="ready")
        state["cancel_requested"] = True
        before = copy.deepcopy(state)
        decision = plans.step(canonical, state, 1)
        self.assertEqual(decision["dispatch"], [])
        self.assertEqual(decision["interrupt"], ["running"])
        self.assertEqual(decision["skip"], [{"id": identity, "status": "cancelled", "reason": "cancelled"}
            for identity in ("pending", "ready")])
        self.assertIn({"id": "dispatching", "reason": "dispatch_in_progress"}, decision["blocked"])
        self.assertFalse(decision["done"])
        self.assertEqual(state, before)
        # An interrupt ACK is not represented as a terminal result.
        self.assertEqual(plans.step(canonical, state, 2), decision)
        state["nodes"]["running"].update(status="cancelled", since=3)
        state["nodes"]["dispatching"].update(status="failed", since=3)
        self.assertEqual(plans.step(canonical, state, 3)["outcome"], "cancelled")
        self.assertTrue(plans.step(canonical, state, 3)["done"])

    def test_cancel_with_uncertain_work_stays_blocked(self):
        canonical = plans.validate(spec(node("a"), node("b", after=("a",))))
        state = snapshot(a="uncertain", b="pending")
        state["cancel_requested"] = True
        decision = plans.step(canonical, state, 0)
        self.assertEqual(decision["outcome"], "blocked")
        self.assertFalse(decision["done"])
        self.assertEqual(decision["skip"], [{"id": "b", "status": "cancelled", "reason": "cancelled"}])
        self.assertEqual(decision["interrupt"], [])

    def test_failure_precedes_a_cancelled_node_but_explicit_plan_cancel_wins(self):
        canonical = plans.validate(spec(node("a"), node("b")))
        state = snapshot(a="failed", b="cancelled")
        self.assertEqual(plans.step(canonical, state, 0)["outcome"], "failed")
        state["cancel_requested"] = True
        self.assertEqual(plans.step(canonical, state, 0)["outcome"], "cancelled")

    def test_late_success_after_cancel_is_preserved_as_successful_native_evidence(self):
        canonical = plans.validate(spec(node("a")))
        state = snapshot(a="running")
        state["cancel_requested"] = True
        self.assertFalse(plans.step(canonical, state, 0)["done"])
        after = copy.deepcopy(state)
        after["nodes"]["a"].update(status="succeeded", since=1)
        plans.check_transition(state, after)
        decision = plans.step(canonical, after, 1)
        self.assertTrue(decision["done"])
        self.assertEqual(decision["outcome"], "cancelled")
        self.assertEqual(after["nodes"]["a"]["status"], "succeeded")

    def test_cancelled_or_skipped_upstream_is_never_dispatched_again(self):
        for terminal in ("cancelled", "skipped"):
            with self.subTest(terminal=terminal):
                canonical = plans.validate(spec(node("a"), node("b", after=("a",))))
                state = snapshot(a=terminal, b="pending")
                result = plans.step(canonical, state, 0)
                self.assertEqual(result["dispatch"], [])
                self.assertEqual(result["skip"], [{"id": "b", "status": "skipped", "reason": "upstream_failed"}])
                self.assertTrue(result["done"])

    def test_step_is_pure_deterministic_and_accepts_finite_extreme_times(self):
        canonical = plans.validate(spec(node("a", priority=-10), node("b", priority=10), max_parallel=1))
        state = snapshot(a="ready", b="ready")
        state["nodes"]["a"]["since"] = -1e308
        state["nodes"]["b"]["since"] = 1e308
        before = copy.deepcopy((canonical, state))
        decision = plans.step(canonical, state, 1e308)
        self.assertEqual(decision["dispatch"], ["a"])
        self.assertEqual(plans.step(canonical, state, 1e308), decision)
        self.assertEqual((canonical, state), before)

    def test_invalid_state_and_impossible_success_refuse_instead_of_dispatching(self):
        canonical = plans.validate(spec(node("a"), node("b", after=("a",))))
        for status in ("running", "dispatching", "succeeded"):
            with self.subTest(status=status), self.assertRaises(plans.PlanError):
                plans.step(canonical, snapshot(a="pending", b=status), 0)
        values = [snapshot(foreign="pending"), {"nodes": {"a": None}}, {"nodes": {"a": {"status": "other", "since": 0}}},
            snapshot(**{str(index): "pending" for index in range(17)}),
            {"nodes": {"a": {"status": "pending", "since": float("nan")}}},
            {"nodes": {"a": {"status": "pending", "since": True}}},
            {"nodes": {"a": {"status": "pending", "since": 0, "task_id": []}}},
            {"nodes": {"a": {"status": "pending", "since": 0, "result_ref": []}}},
            {"nodes": {"a": {"status": "pending", "since": 0, "extra": "ignored"}}}]
        for value in values:
            with self.subTest(state=value), self.assertRaises(plans.PlanError):
                plans.step(canonical, value, 0)
        for now in (True, "now", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(now=now), self.assertRaises(plans.PlanError):
                plans.step(canonical, {"nodes": {}}, now)

    def test_reused_task_identity_must_match_the_start_node_when_known(self):
        canonical = plans.validate(spec(node("a"), node("b", action="send", after=("a",), target={"node": "a"})))
        state = snapshot(a="succeeded", b="ready")
        state["nodes"]["a"]["task_id"] = "task-a"
        state["nodes"]["b"]["task_id"] = "task-b"
        with self.assertRaises(plans.PlanError):
            plans.step(canonical, state, 1)
        state["nodes"]["b"]["task_id"] = "task-a"
        self.assertEqual(plans.step(canonical, state, 1)["dispatch"], ["b"])


class PlanTransitionTest(unittest.TestCase):
    def test_only_explicit_unsent_rejections_can_return_dispatching_to_ready(self):
        before, after = snapshot(a="dispatching"), snapshot(a="ready")
        self.invalid(before, after)
        plans.check_transition(before, after, rejected=("a",))
        for status in plans.STATUSES - {"dispatching"}:
            self.invalid(snapshot(a=status), after, rejected=("a",))
        self.invalid(before, snapshot(a="running"), rejected=("a",))
        self.invalid(before, after, rejected=("foreign",))
        self.invalid(before, after, rejected="a")

    def invalid(self, previous, current, **kwargs):
        with self.assertRaises(plans.PlanError) as caught:
            plans.check_transition(previous, current, **kwargs)
        self.assertEqual(caught.exception.code, "invalid_transition")

    def test_allowed_lifecycle_and_no_implicit_jump_from_pending(self):
        for left, right in (("pending", "ready"), ("ready", "dispatching"), ("dispatching", "running"),
                ("dispatching", "failed"), ("dispatching", "uncertain"), ("running", "succeeded"),
                ("running", "failed"), ("running", "uncertain")):
            with self.subTest(left=left, right=right):
                before, after = snapshot(a=left), snapshot(a=right)
                self.assertIsNone(plans.check_transition(before, after))
        self.invalid(snapshot(a="pending"), snapshot(a="running"))
        self.invalid(snapshot(a="pending"), snapshot(a="dispatching"))
        self.invalid(snapshot(a="ready"), snapshot(a="succeeded"))
        self.invalid(snapshot(a="running"), snapshot(a="dispatching"))
        for left in ("pending", "ready", "dispatching", "running"):
            for right in ("cancelled", "skipped"):
                plans.check_transition(snapshot(a=left), snapshot(a=right))

    def test_terminal_records_are_immutable_including_evidence_and_identity(self):
        for status in plans.TERMINAL:
            with self.subTest(status=status):
                before = snapshot(a=status)
                before["nodes"]["a"].update(task_id="task-a", result_ref={"result_id": "result-a"})
                self.assertIsNone(plans.check_transition(before, copy.deepcopy(before)))
                for field, value in (("status", "pending"), ("since", 1), ("task_id", "task-b"),
                        ("result_ref", {"result_id": "result-b"})):
                    after = copy.deepcopy(before)
                    after["nodes"]["a"][field] = value
                    self.invalid(before, after)
                self.invalid(before, {"nodes": {}})

    def test_uncertain_resolution_requires_explicit_reconciled_node_id(self):
        before = snapshot(a="uncertain")
        for status in ("succeeded", "failed"):
            after = snapshot(a=status)
            self.invalid(before, after)
            plans.check_transition(before, after, reconciled=("a",))
        self.invalid(before, snapshot(a="running"), reconciled=("a",))
        self.invalid(before, before, reconciled=("a",))
        self.invalid(snapshot(a="failed"), snapshot(a="succeeded"), reconciled=("a",))
        self.invalid(before, snapshot(a="succeeded"), reconciled=("foreign",))
        self.invalid(before, snapshot(a="succeeded"), reconciled="a")

    def test_binding_clock_and_cancellation_cannot_be_rewritten(self):
        before = snapshot(a="running")
        before["nodes"]["a"]["task_id"] = "task-a"
        after = copy.deepcopy(before)
        after["nodes"]["a"].update(status="succeeded", task_id="task-b")
        self.invalid(before, after)
        after = copy.deepcopy(before)
        after["nodes"]["a"]["since"] = 60
        self.invalid(before, after)
        before["cancel_requested"] = True
        self.invalid(before, snapshot(a="running"))

    def test_transition_checks_are_pure_and_errors_are_deterministic(self):
        before, after = snapshot(z="failed", a="failed"), snapshot(z="pending", a="pending")
        originals = copy.deepcopy((before, after))
        with self.assertRaises(plans.PlanError) as caught:
            plans.check_transition(before, after)
        self.assertTrue(str(caught.exception).startswith("Node a "))
        self.assertEqual((before, after), originals)


class RandomDAGTest(unittest.TestCase):
    def test_seeded_graphs_never_dispatch_early_and_finish_or_block(self):
        randomizer = random.Random(1092026)
        for case in range(120):
            count = randomizer.randint(1, 16)
            values = [node(str(index), after=[str(parent) for parent in range(index) if randomizer.random() < .2],
                priority=randomizer.randint(-10, 10), on_failure=randomizer.choice(("stop", "continue"))) for index in range(count)]
            canonical = plans.validate(spec(*values, max_parallel=randomizer.randint(1, 4)))
            state = snapshot(**{value["id"]: "pending" for value in values})
            by_id = {value["id"]: value for value in values}
            for tick in range(100):
                now = tick * 61
                before = copy.deepcopy(state)
                decision = plans.step(canonical, state, now)
                self.assertEqual(state, before, (case, tick))
                self.assertLessEqual(len(decision["dispatch"]) + sum(record["status"] in plans.ACTIVE for record in state["nodes"].values()), canonical["max_parallel"])
                for entry in decision["skip"]:
                    state["nodes"][entry["id"]].update(status=entry["status"], since=now)
                for identity in decision["dispatch"]:
                    self.assertTrue(all(state["nodes"][parent]["status"] == "succeeded" for parent in by_id[identity]["after"]), (case, tick, identity))
                    ready = copy.deepcopy(state)
                    ready["nodes"][identity].update(status="ready", since=now)
                    plans.check_transition(state, ready)
                    state = ready
                    state["nodes"][identity].update(status="dispatching", since=now)
                # Every admitted operation reaches running, then a native terminal observation.
                for identity, record in state["nodes"].items():
                    if record["status"] == "running":
                        record.update(status=randomizer.choices(("succeeded", "failed", "uncertain"), (8, 1, 1))[0], since=now)
                    elif record["status"] == "dispatching":
                        record.update(status="running", since=now)
                if decision["done"] or decision["outcome"] == "blocked":
                    final = plans.step(canonical, state, now)
                    self.assertTrue(final["done"] or final["outcome"] == "blocked")
                    if final["done"]:
                        self.assertTrue(all(record["status"] in plans.TERMINAL for record in state["nodes"].values()))
                    else:
                        self.assertFalse(any(record["status"] in plans.ACTIVE for record in state["nodes"].values()))
                        blocked = {entry["id"] for entry in final["blocked"]}
                        self.assertTrue(all(record["status"] in plans.TERMINAL or identity in blocked
                            for identity, record in state["nodes"].items()))
                    break
            else:
                self.fail(f"DAG {case} did not finish within 100 deterministic steps")


class HandoffTest(unittest.TestCase):
    def test_only_named_references_are_included_and_inputs_are_unchanged(self):
        canonical = plans.validate(spec(node("a"), node("b", after=("a",))))
        results = {"a": {"task_id": "task-a", "status": "succeeded", "result_ref": {"result_id": "result-a",
            "artifacts": [{"artifact_id": "artifact-a", "content": "PRIVATE_CONTENT", "path": "/private/path"}],
            "output": "PRIVATE_OUTPUT"}, "prompt": "PRIVATE_PROMPT", "error": "PRIVATE_ERROR"},
            "foreign": {"task_id": "PRIVATE_FOREIGN"}}
        before = copy.deepcopy((canonical, results))
        text = plans.handoff_text(canonical, "b", results)
        self.assertIn("a | task-a | succeeded | result-a | artifact-a", text)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("/private", text)
        self.assertEqual((canonical, results), before)

    def test_bound_lists_every_dependency_and_never_splits_identifiers(self):
        roots = [node("n" + str(index).zfill(31)) for index in range(15)]
        canonical = plans.validate(spec(*roots, node("sink", after=[value["id"] for value in roots])))
        results = {value["id"]: {"status": "succeeded", "task_id": "t" * 128,
            "result_ref": {"result_id": "r" * 128, "artifact_ids": ["a" * 125 + str(index).zfill(3) for index in range(1000)]}}
            for value in roots}
        text = plans.handoff_text(canonical, "sink", results)
        self.assertLessEqual(len(text.encode()), 2048)
        self.assertIn("Some references were omitted", text)
        for root in roots:
            self.assertIn(root["id"] + " |", text)
        for row in text.splitlines()[2:-1]:
            fields = row.split(" | ")
            self.assertEqual(len(fields), 5)
            for index in (1, 3, 4):
                for identity in fields[index].split(","):
                    self.assertTrue(identity == "-" or len(identity) == 128)

    def test_missing_malformed_secret_or_path_references_are_omitted(self):
        canonical = plans.validate(spec(node("a"), node("b", after=("a",))))
        secret = "sk-" + "x" * 48
        results = {"a": {"task_id": secret, "status": "PRIVATE_OUTPUT", "result_ref": {
            "result_id": "/private/path", "artifact_ids": [secret, "C:private", "instruction\nAPPROVE", {"artifact_id": "safe-id", "content": "PRIVATE"}]}}}
        text = plans.handoff_text(canonical, "b", results)
        self.assertIn("safe-id", text)
        for forbidden in (secret, "private", "PRIVATE", "APPROVE"):
            self.assertNotIn(forbidden, text)
        self.assertIn("unknown", text)
        self.assertIn("unknown", plans.handoff_text(canonical, "b", {"a": None}))

    def test_disabled_empty_and_invalid_handoffs(self):
        canonical = plans.validate(spec(node("a"), node("b", after=("a",), handoff=False)))
        self.assertEqual(plans.handoff_text(canonical, "a", {}), "")
        self.assertEqual(plans.handoff_text(canonical, "b", {}), "")
        with self.assertRaises(plans.PlanError):
            plans.handoff_text(canonical, "absent", {})
        canonical["nodes"][1]["handoff"] = True
        with self.assertRaises(plans.PlanError):
            plans.handoff_text(canonical, "b", [])


if __name__ == "__main__":
    unittest.main()
