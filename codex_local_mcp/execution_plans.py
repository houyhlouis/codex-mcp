"""Durable small plans. Native turn results, never RPC ACKs, finish nodes."""

import asyncio
import json
import time
from pathlib import Path

from . import plans, processes
from .lifecycle import TERMINAL
from .security import BridgeError, redact, within
from .tools import dispatch, bounded_result, error_envelope, arguments, BY_NAME

TABLES = ("plans", "plan_nodes")
EXPIRED_PREFIX = "dependency_plan_expired:"
FINAL = plans.TERMINAL - {"uncertain"}
RETRY = frozenset(("workspace_busy", "max_tasks"))
REQUEST_FIELDS = ("id", "pending_request_id", "request_id_kind", "task_id", "kind", "status", "blocking", "requires_human",
    "proposal_revision", "proposal_hash", "content_redacted", "redaction_count", "person_review_required", "review_warning",
    "permission_summary", "content_bytes", "proposal_bytes", "preview", "preview_truncated", "detail_tool")


def migrate(db):
    for statement in (
        "CREATE TABLE plans (id TEXT PRIMARY KEY, spec TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, revision INTEGER NOT NULL, done INTEGER NOT NULL, outcome TEXT NOT NULL, cancel_requested INTEGER NOT NULL, decision TEXT NOT NULL)",
        "CREATE INDEX plans_open ON plans(done,created_at)",
        "CREATE INDEX plans_created ON plans(created_at DESC,id)",
        "CREATE TABLE plan_nodes (plan_id TEXT NOT NULL, node_id TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL, since REAL NOT NULL, task_id TEXT, turn_id TEXT, operation_id TEXT, attempt INTEGER NOT NULL, result_ref TEXT, data TEXT NOT NULL, PRIMARY KEY(plan_id,node_id))",
        "CREATE INDEX plan_nodes_task ON plan_nodes(task_id,status)",
        "CREATE UNIQUE INDEX plan_nodes_operation ON plan_nodes(operation_id) WHERE operation_id IS NOT NULL",
    ):
        db.execute(statement)


def invalid(error):
    return BridgeError(str(error), code="invalid_arguments", details={"plan_error": error.code})


class Scheduler:
    def __init__(self, service):
        self.service, self.store, self.db = service, service.store, service.store.db
        self.worker, self.jobs, self.wait_versions, self.attempt_versions = None, {}, {}, {}
        self.live_starts = {}
        self.dirty, self.version = False, 0
        self.clock = time.time
        self.enabled = bool(self.db.execute("SELECT 1 FROM plans WHERE done=0 LIMIT 1").fetchone())

    def record(self, identity):
        row = self.db.execute("SELECT * FROM plans WHERE id=?", (identity,)).fetchone()
        if not row:
            if self.expired(identity):
                raise BridgeError("Dependency plan was removed by cleanup", code="not_found", ids={"plan_id": identity})
            raise BridgeError("Dependency plan was not found or was pruned", code="not_found", ids={"plan_id": identity})
        return dict(row)

    def expired(self, identity):
        return bool(self.db.execute("SELECT 1 FROM metadata WHERE key=?", (EXPIRED_PREFIX + identity,)).fetchone())

    def check_expired(self, identity):
        if self.expired(identity):
            raise BridgeError("This plan was removed by cleanup and cannot run again under the same identity; change the plan's name to submit it as a new plan",
                code="operation_expired", ids={"plan_id": identity}, next_steps=["Change the plan's name to submit it as a new plan"])

    @staticmethod
    def canonical(plan):
        try:
            value = json.loads(plan["spec"])
            if plans.plan_id(value) == plan["id"]:
                return value
        except (ValueError, TypeError, plans.PlanError):
            pass
        raise BridgeError("Stored dependency plan identity is inconsistent; inspect state before running more work", code="state_invalid", ids={"plan_id": plan["id"]})

    def nodes(self, identity):
        return [dict(row) for row in self.db.execute("SELECT * FROM plan_nodes WHERE plan_id=? ORDER BY position", (identity,))]

    def state(self, identity):
        return {"cancel_requested": bool(self.record(identity)["cancel_requested"]), "nodes": {
            n["node_id"]: {"status": n["status"], "since": n["since"], "task_id": n["task_id"],
                "result_ref": json.loads(n["result_ref"]) if n["result_ref"] else None} for n in self.nodes(identity)}}

    def update(self, identity, node_id, fields=None, *, data=None, reconciled=False, rejected=False):
        fields = dict(fields or {})
        with self.store.transaction():
            row = self.db.execute("SELECT * FROM plan_nodes WHERE plan_id=? AND node_id=?", (identity, node_id)).fetchone()
            if "status" in fields and fields["status"] != row["status"]:
                fields.setdefault("since", self.clock())
            before = self.state(identity)
            after = {**before, "nodes": {**before["nodes"], node_id: {**before["nodes"][node_id],
                **{k: v for k, v in fields.items() if k in ("status", "since", "task_id", "result_ref")}}}}
            plans.check_transition(before, after, reconciled=(node_id,) if reconciled else (), rejected=(node_id,) if rejected else ())
            if fields.get("turn_id") and row["turn_id"] and fields["turn_id"] != row["turn_id"]:
                raise BridgeError("Plan node is already bound to another native turn", code="state_conflict")
            if "result_ref" in fields:
                fields["result_ref"] = json.dumps(fields["result_ref"]) if fields["result_ref"] else None
            if data is not None:
                fields["data"] = json.dumps({**json.loads(row["data"]), **data})
            fields = {k: v for k, v in fields.items() if row[k] != v}
            if fields:
                self.db.execute("UPDATE plan_nodes SET " + ",".join(k + "=?" for k in fields) + " WHERE plan_id=? AND node_id=?",
                    (*fields.values(), identity, node_id))
                self.db.execute("UPDATE plans SET revision=revision+1,updated_at=? WHERE id=?", (self.clock(), identity))

    def submit(self, spec):
        from .store import project_record
        try:
            canonical = plans.validate(spec)
            for node in canonical["nodes"]:
                if node["action"] != "start" and "node" not in node["target"]:
                    raise plans.PlanError("invalid_target", "send and review nodes must name an earlier start node as target")
                if redact(node["prompt"]) != node["prompt"] or redact(canonical["name"]) != canonical["name"]:
                    raise plans.PlanError("invalid_plan", "A plan stores its prompts until they run; remove the value that looks like a password, token or key from node " + node["id"] + " and submit again")
            existing = plans.plan_id(canonical)
            self.check_expired(existing)
            if self.db.execute("SELECT 1 FROM plans WHERE id=?", (existing,)).fetchone():
                self.enabled = True
                self.schedule()
                return self.inspect(existing)
            resolved = {}
            for node in canonical["nodes"]:
                target = node["target"]
                if "node" in target:
                    continue
                try:
                    descriptor = self.service.select_project(**target, register=False)
                except BridgeError as exc:
                    raise plans.PlanError("invalid_target", f"Node {node['id']} target is unavailable: {exc}") from exc
                resolved[node["id"]] = descriptor["path"]
            by_id = {n["id"]: n for n in canonical["nodes"]}
            for left, right in canonical["needs_resolution"]:
                paths = [Path(resolved[by_id[n]["target"].get("node", n)]) for n in (left, right)]
                if within(paths[0], paths[1]) or within(paths[1], paths[0]):
                    raise plans.PlanError("same_workspace_parallel", f"Nodes {left} and {right} may run in the same workspace; add a dependency.")
            for node in canonical["nodes"]:
                if node["id"] in resolved:
                    node["target"] = {"project_id": project_record(resolved[node["id"]])["id"]}
            canonical = plans.validate(canonical)
            identity = plans.plan_id(canonical)
            self.check_expired(identity)
            with self.store.transaction():
                for path in resolved.values():
                    self.service.register_project(Path(path))
                if not self.db.execute("SELECT 1 FROM plans WHERE id=?", (identity,)).fetchone():
                    now = self.clock()
                    state = {"nodes": {n["id"]: {"status": "pending", "since": now, "task_id": None, "result_ref": None} for n in canonical["nodes"]}}
                    first = plans.step(canonical, state, now)
                    self.db.execute("INSERT INTO plans VALUES (?,?,?,?,1,0,?,0,?)", (identity, json.dumps(canonical), now, now, first["outcome"], json.dumps(first)))
                    self.db.executemany("INSERT INTO plan_nodes VALUES (?,?,?,'pending',?,NULL,NULL,NULL,0,NULL,'{}')",
                        ((identity, node["id"], pos, now) for pos, node in enumerate(canonical["nodes"])))
        except plans.PlanError as exc:
            raise invalid(exc) from exc
        self.enabled = True
        self.schedule()
        return self.inspect(identity)

    def inspect(self, identity):
        plan, nodes = self.record(identity), self.nodes(identity)
        canonical = self.canonical(plan)
        blocked = {item["id"]: item for item in json.loads(plan["decision"])["blocked"]}
        pending, summaries = [], []
        for node in nodes:
            extra = json.loads(node["data"])
            requests, request_error = {"requests": [], "next_cursor": None, "total": 0}, None
            if node["task_id"] and node["status"] not in FINAL:
                try:
                    requests = self.service.request_page(node["task_id"], limit=20)
                except BridgeError as exc:
                    request_error = bounded_result(exc.as_dict(), 8192)
                    requests["total"] = self.store.request_count(node["task_id"])
            actions = []
            if request_error:
                actions.append({"tool": "codex_requests", "arguments": {"task_id": node["task_id"]},
                    "reason": "Restore access to the task's project, then inspect its pending requests"})
            elif requests["total"]:
                actions.append({"tool": "codex_requests", "arguments": {"task_id": node["task_id"]}, "reason": "Review and answer pending requests through their normal human or guidance route"})
            if node["status"] == "uncertain":
                if node["task_id"]:
                    task = self.store.task_head(node["task_id"])
                    if task.get("deleted"):
                        actions.append({"tool": "codex_get", "arguments": {"task_id": node["task_id"], "view": "reconciliation"},
                            "reason": "Inspect the deleted conversation's retained evidence; the plan cannot infer a terminal execution result"})
                    elif not task.get("thread_id"):
                        actions.append({"tool": "codex_resume", "arguments": {"task_id": node["task_id"]}, "requires_new_operation_id": True,
                            "reason": "Reconcile this task; no owned thread was established and no plan side effect is replayed"})
                    elif extra.get("reason") == "bound_turn_unknown":
                        actions.append({"tool": "codex_history", "arguments": {"task_id": node["task_id"], "kind": "turns"},
                            "reason": "This step has no confirmed turn identity; inspect history. The plan will not assume that the latest turn belongs to it"})
                    else:
                        actions.append({"tool": "codex_resume", "arguments": {"task_id": node["task_id"]}, "requires_new_operation_id": True,
                            "reason": "Reconcile this task; no plan side effect is replayed"})
                        if plan["cancel_requested"] and extra.get("reconciled") and extra.get("cancel_key") and task.get("turn_id") == node["turn_id"] and task["status"] in ("running", "waiting", "interrupting"):
                            actions.append({"tool": "codex_interrupt", "arguments": {"task_id": node["task_id"], "expected_turn_id": node["turn_id"]},
                                "requires_new_operation_id": True, "reason": "The previous interrupt outcome is unknown. Inspect history, then explicitly interrupt this turn if still intended"})
                    if plan["cancel_requested"] and not task.get("deleted") and task.get("thread_id"):
                        actions.append({"tool": "codex_delete", "arguments": {"task_id": node["task_id"], "confirm_thread_id": task["thread_id"]},
                            "requires_new_operation_id": True,
                            "reason": "After reviewing history and stopping the task family, confirmed deletion lets this cancelled plan finish. It does not mark execution successful"})
                else:
                    actions.append({"tool": "codex_status", "arguments": {}, "reason": "Inspect the uncertain operation before any new work; no task or bound turn was established"})
            elif node["status"] == "ready" and extra.get("reason") in RETRY:
                actions.append({"tool": "codex_list", "arguments": {"view": "compact"}, "reason": "Waiting for workspace or capacity; retried only after a scheduling event"})
                actions.append({"tool": "codex_plans", "arguments": {"action": "submit"}, "use_same_spec": True,
                    "requires_new_operation_id": True, "reason": "After clearing contention, submit the same spec to request a fresh admission check"})
            pending.extend(actions)
            summaries.append({"id": node["node_id"], "status": node["status"], "since": node["since"],
                "task_id": node["task_id"], "turn_id": node["turn_id"], "result_ref": json.loads(node["result_ref"]) if node["result_ref"] else None,
                "operation_id": node["operation_id"] or extra.get("last_operation_id"),
                "attempts": node["attempt"] + int(node["operation_id"] is not None),
                "reason": extra.get("reason") or blocked.get(node["node_id"], {}).get("reason"),
                "blocked_by": blocked.get(node["node_id"], {}).get("uncertain_nodes", []), "error": extra.get("error") or request_error,
                "pending_requests": [{**{k: r[k] for k in REQUEST_FIELDS if k in r}, "details_truncated": True} for r in requests["requests"]],
                "pending_requests_total": requests["total"],
                "pending_requests_next_cursor": requests["next_cursor"], "next_steps": actions})
        return {"plan_id": identity, "spec": canonical, "revision": plan["revision"], "created_at": plan["created_at"],
            "updated_at": plan["updated_at"], "cancel_requested": bool(plan["cancel_requested"]), "done": bool(plan["done"]),
            "outcome": plan["outcome"], "nodes": summaries, "decision": json.loads(plan["decision"]), "next_steps": pending[:32]}

    async def call(self, action, spec=None, plan_id=None, offset=0, limit=20):
        if action == "submit":
            result = self.submit(spec)
        elif action == "inspect":
            result = self.inspect(plan_id)
        elif action == "list":
            rows = self.db.execute("SELECT id,created_at,updated_at,done,outcome,cancel_requested FROM plans ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            result = {"plans": [{"plan_id": r["id"], **{k: r[k] for k in ("created_at", "updated_at", "outcome")},
                "done": bool(r["done"]), "cancel_requested": bool(r["cancel_requested"])} for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}
        else:
            self.record(plan_id)
            cancel_jobs = []
            with self.store.transaction():
                self.db.execute("UPDATE plans SET cancel_requested=1,revision=revision+1,updated_at=? WHERE id=? AND cancel_requested=0 AND done=0", (self.clock(), plan_id))
                for node in self.nodes(plan_id):
                    if node["status"] == "dispatching" and not self.db.execute("SELECT 1 FROM operations WHERE id=?", (node["operation_id"],)).fetchone():
                        # Operation admission is durable before any native I/O.
                        # No row means this dispatch has not been accepted.
                        self.update(plan_id, node["node_id"], {"status": "cancelled"}, data={"reason": "cancelled_before_dispatch"})
                        job = self.jobs.get(("dispatch", plan_id, node["node_id"]))
                        if job:
                            cancel_jobs.append(job)
                    elif node["status"] == "uncertain":
                        self.refresh(plan_id, node)
                canonical = self.canonical(self.record(plan_id))
                decision = plans.step(canonical, self.state(plan_id), self.clock())
                for skipped in decision["skip"]:
                    self.update(plan_id, skipped["id"], {"status": skipped["status"]}, data={"reason": skipped["reason"]})
                self.decision(plan_id, canonical)
            for job in cancel_jobs:
                job.cancel()
            self.schedule()
            result = self.inspect(plan_id)
        return {"action": action, **result}

    def bind(self, operation_id, task_id, *, token=None, turn_id=None):
        node = self.db.execute("SELECT * FROM plan_nodes WHERE operation_id=?", (operation_id,)).fetchone()
        if not node or node["status"] in FINAL:
            return
        task = self.store.task_head(task_id)
        fields = {"task_id": task_id}
        if turn_id:
            fields["turn_id"] = turn_id
        data = {"thread_id": task.get("thread_id")}
        if token:
            data.update(start_token=token, before_turn_id=task.get("turn_id"))
        self.update(node["plan_id"], node["node_id"], fields, data=data)
        if turn_id:
            self.live_starts.pop(operation_id, None)
        if task.get("plan_id") != node["plan_id"] or task.get("plan_node_id") != node["node_id"]:
            self.service.lifecycle.metadata(task_id, {"plan_id": node["plan_id"], "plan_node_id": node["node_id"]})

    def execution_sent(self, operation_id):
        node = self.db.execute("SELECT task_id,data FROM plan_nodes WHERE operation_id=?", (operation_id,)).fetchone()
        if node:
            token = json.loads(node["data"]).get("start_token")
            task = self.store.task_head(node["task_id"])
            ambiguous = (task.get("queue_reservation") or task.get("queue_uncertain") or
                any(claim["kind"] in ("goal", "queue") for claim in self.store.claims(task_id=node["task_id"])))
            self.live_starts.pop(operation_id, None)
            if token and not ambiguous:
                # Immutable AUTOINCREMENT ordinals bound the known turns without
                # copying a conversation's history at every dispatch.
                boundary = self.db.execute("SELECT COALESCE(MAX(ordinal),0) FROM turns").fetchone()[0]
                self.live_starts[operation_id] = (node["task_id"], token, self.service.backend.generation, boundary)

    def observed_turn(self, task_id, turn_id):
        task = self.store.task_head(task_id)
        token = task.get("turn_start")
        if token:
            for row in self.db.execute("SELECT operation_id,data FROM plan_nodes WHERE task_id=? AND turn_id IS NULL AND status IN ('dispatching','uncertain')", (task_id,)).fetchall():
                witness = self.live_starts.get(row["operation_id"])
                if (witness and witness[:3] == (task_id, token, self.service.backend.generation)
                        and json.loads(row["data"]).get("start_token") == token):
                    family = task.get("root_task_id") or task_id
                    known = self.db.execute("""SELECT 1 FROM turns WHERE turn_id=? AND ordinal<=?
                        AND task_id IN (SELECT id FROM tasks WHERE root_task_id=? UNION SELECT ?) LIMIT 1""",
                        (turn_id, witness[3], family, family)).fetchone()
                    if not known:
                        self.bind(row["operation_id"], task_id, turn_id=turn_id)

    def reconciled(self, task_id):
        family = self.service.resources.family(task_id)
        for row in self.db.execute("SELECT n.plan_id,n.node_id FROM plan_nodes n JOIN tasks t ON t.id=n.task_id WHERE t.root_task_id=? AND n.status='uncertain'", (family,)).fetchall():
            self.update(row["plan_id"], row["node_id"], data={"reconciled": True})
        self.schedule()

    def confirmed_delete(self, task_id):
        for node in self.db.execute("""SELECT n.* FROM plan_nodes n LEFT JOIN operations o ON o.id=n.operation_id
                WHERE n.task_id=? OR o.task_id=?""", (task_id, task_id)).fetchall():
            if node["status"] in FINAL:
                continue
            self.update(node["plan_id"], node["node_id"], data={"confirmed_task_delete": True})
        self.schedule()

    def matches_operation(self, node, operation):
        data = json.loads(node["data"])
        tool = data["tool"]
        values = arguments(tool, data["dispatch_arguments"])
        legacy = dict(values)
        for field, schema in BY_NAME[tool]["inputSchema"]["properties"].items():
            if "default" in schema:
                legacy.setdefault(field, schema["default"])
        return operation["fingerprint"] in {self.service.operation_fingerprint(tool, value) for value in (values, legacy)}

    def identity_conflict(self, identity, node):
        if json.loads(node["data"]).get("start_token") or node["turn_id"]:
            raise BridgeError("Stored dependency plan operation identity is inconsistent; inspect state before running more work",
                code="state_invalid", ids={"plan_id": identity, "operation_id": node["operation_id"]})
        error = BridgeError("request_id was already used with different arguments", code="operation_conflict")
        self.update(identity, node["node_id"], {"status": "failed"},
            data={"reason": error.code, "error": error.as_dict()}, reconciled=node["status"] == "uncertain")

    def unavailable(self, identity, node, operation):
        if not operation or operation["code"] != "optional_native_capability_unavailable":
            return False
        error = json.loads(operation["result"])["error_data"]
        self.live_starts.pop(node["operation_id"], None)
        self.update(identity, node["node_id"], {"status": "failed"}, reconciled=node["status"] == "uncertain",
            data={"reason": error["code"], "error": bounded_result(error, 8192)})
        return True

    def recover(self):
        for node in self.db.execute("SELECT n.* FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE p.done=0 AND n.status IN ('running','dispatching')").fetchall():
            operation = self.db.execute("SELECT state,fingerprint,result,json_extract(result,'$.operation_rejected') rejected,json_extract(result,'$.error_data.code') code FROM operations WHERE id=?", (node["operation_id"],)).fetchone()
            if operation and not self.matches_operation(node, operation):
                self.identity_conflict(node["plan_id"], node)
                continue
            if self.unavailable(node["plan_id"], node, operation):
                continue
            if operation and operation["state"] == "done" and operation["rejected"] and node["status"] == "dispatching":
                self.rejected(node["plan_id"], dict(node), operation["code"])
                continue
            task = self.store.task_head(node["task_id"]) if node["task_id"] else None
            if operation and operation[0] != "done" or task and (task["status"] in ("needs_resume", "uncertain", "recovering") or processes.pending(task)):
                self.update(node["plan_id"], node["node_id"], {"status": "uncertain"}, data={"reason": "broker_restart", "reconciled": False})
        if self.enabled:
            self.schedule()

    def own_job(self):
        try:
            current = asyncio.current_task()
        except RuntimeError:
            return False
        key = self.service.current_operation.get()
        return current is self.worker or current in self.jobs.values() or bool(key and key.startswith("plan:"))

    def schedule(self, *, retry=True):
        if not self.enabled or self.service.shutting_down:
            return
        if retry:
            self.version += 1
        self.dirty = True
        if self.worker is None or self.worker.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self.worker = loop.create_task(self.run())
            self.worker.add_done_callback(lambda job: job.exception() if not job.cancelled() else None)

    def changed(self):
        if self.enabled and not self.own_job():
            self.schedule()

    async def idle(self):
        while self.worker and not self.worker.done() or self.jobs:
            waiting = [*self.jobs.values(), *([self.worker] if self.worker else [])]
            await asyncio.wait(waiting)
            for job in waiting:
                job.result()
        if self.worker:
            self.worker.result()

    async def close(self):
        pending = [*self.jobs.values(), *([self.worker] if self.worker else [])]
        for job in pending:
            job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def run(self):
        self.service.current_operation.set(None)
        self.service.operation_context.set(None)
        while self.dirty and not self.service.shutting_down:
            self.dirty = False
            for row in self.db.execute("SELECT id,spec,cancel_requested FROM plans WHERE done=0 ORDER BY created_at,id").fetchall():
                identity, canonical = row["id"], self.canonical(row)
                for node in self.nodes(identity):
                    self.refresh(identity, node)
                decision = self.decide(identity, canonical)
                for skipped in decision["skip"]:
                    self.update(identity, skipped["id"], {"status": skipped["status"]}, data={"reason": skipped["reason"]})
                for node_id in decision["dispatch"]:
                    node = next(n for n in self.nodes(identity) if n["node_id"] == node_id)
                    if node["status"] == "pending":
                        spec = next(n for n in canonical["nodes"] if n["id"] == node_id)
                        state = self.state(identity)["nodes"]
                        since = max([node["since"], *(state[parent]["since"] for parent in spec["after"])])
                        self.update(identity, node_id, {"status": "ready", "since": since})
                    if self.wait_versions.get((identity, node_id), -1) < self.version:
                        self.launch(identity, canonical, node_id)
                for node_id in decision["interrupt"]:
                    self.launch_cancel(identity, node_id)
                if row["cancel_requested"]:
                    for node in self.nodes(identity):
                        if node["status"] == "uncertain" and json.loads(node["data"]).get("reconciled") and node["task_id"]:
                            task = self.store.task_head(node["task_id"])
                            if task["status"] in ("running", "waiting", "interrupting") and task.get("turn_id") == node["turn_id"]:
                                self.launch_cancel(identity, node["node_id"])
                self.decision(identity, canonical)
            self.enabled = bool(self.db.execute("SELECT 1 FROM plans WHERE done=0 LIMIT 1").fetchone())
            await self.service.wake()

    def decision(self, identity, canonical):
        value = self.decide(identity, canonical)
        plan = self.record(identity)
        if plan["decision"] != json.dumps(value) or bool(plan["done"]) != value["done"] or plan["outcome"] != value["outcome"]:
            with self.store.transaction():
                self.db.execute("UPDATE plans SET decision=?,done=?,outcome=?,revision=revision+1,updated_at=? WHERE id=?",
                    (json.dumps(value), int(value["done"]), value["outcome"], self.clock(), identity))

    def decide(self, identity, canonical):
        state, held = self.state(identity), {}
        failures = {node["id"] for node in canonical["nodes"] if node["on_failure"] == "stop"}
        stopping = state["cancel_requested"] or any(node_id in failures and record["status"] == "failed"
            for node_id, record in state["nodes"].items())
        unknown = [node_id for node_id, record in state["nodes"].items() if record["status"] == "uncertain"]
        active = sum(record["status"] in plans.ACTIVE for record in state["nodes"].values())
        # An unsent admission waits for another resource event and occupies no
        # parallel slot. Exclude it from this pure decision, without changing
        # its durable ready state or making it look uncertain to the caller.
        for node_id, record in state["nodes"].items():
            if not stopping and record["status"] == "ready" and self.wait_versions.get((identity, node_id), -1) >= self.version:
                held[node_id] = next(n for n in self.nodes(identity) if n["node_id"] == node_id)
                state["nodes"][node_id] = {**record, "status": "uncertain"}
        value = plans.step(canonical, state, self.clock())
        free = max(0, canonical["max_parallel"] - active - len(unknown))
        for node_id in value["dispatch"][free:]:
            value["blocked"].append({"id": node_id, "reason": "capacity", "uncertain_nodes": unknown})
        value["dispatch"] = value["dispatch"][:free]
        if not active and not value["dispatch"] and (unknown or held):
            value["outcome"], value["done"] = "blocked", False
        for blocked in value["blocked"]:
            if blocked["id"] in held:
                blocked["reason"] = json.loads(held[blocked["id"]]["data"]).get("reason") or "workspace_busy"
            elif held and blocked["reason"] == "upstream_uncertain" and not any(n["status"] == "uncertain" for n in self.nodes(identity)):
                blocked["reason"] = "dependencies_pending"
        return value

    def refresh(self, identity, node):
        if node["status"] in FINAL or node["status"] in ("pending", "ready"):
            return
        data = json.loads(node["data"])
        operation = self.db.execute("SELECT state,task_id,fingerprint,result,json_extract(result,'$.operation_rejected') rejected,json_extract(result,'$.error_data.code') code FROM operations WHERE id=?", (node["operation_id"],)).fetchone()
        if node["status"] == "dispatching" and ("dispatch", identity, node["node_id"]) in self.jobs:
            return
        if operation and not self.matches_operation(node, operation):
            self.identity_conflict(identity, node)
            return
        if self.unavailable(identity, node, operation):
            return
        cancelled = self.record(identity)["cancel_requested"]
        if cancelled and data.get("confirmed_task_delete"):
            self.update(identity, node["node_id"], {"status": "failed"}, reconciled=node["status"] == "uncertain",
                data={"reason": "task_deleted", "reconciled": False})
            return
        if cancelled and node["status"] == "uncertain":
            job = self.service.operation_tasks.get(node["operation_id"])
            reservation = self.db.execute("""SELECT 1 FROM resource_reservations WHERE id=?
                AND (token IS NOT NULL OR native_key IS NOT NULL OR turn_id IS NOT NULL)""", (node["operation_id"],)).fetchone()
            expired = self.db.execute("SELECT 1 FROM operation_tombstones WHERE id=?", (node["operation_id"],)).fetchone()
            unsent = (not data.get("start_token") and not node["turn_id"] and not reservation and not expired
                and (not operation or operation["state"] != "done") and (not job or job.done()))
            if unsent:
                self.update(identity, node["node_id"], {"status": "failed"}, reconciled=True,
                    data={"reason": "cancelled_before_execution", "reconciled": False})
                return
        if operation is None:
            if node["status"] == "dispatching":
                if self.record(identity)["cancel_requested"]:
                    self.update(identity, node["node_id"], {"status": "cancelled"}, data={"reason": "cancelled_before_dispatch"})
                else:
                    self.launch_existing(identity, node)
            return
        if node["status"] == "uncertain" and not data.get("reconciled"):
            return
        if operation["state"] == "done" and operation["rejected"]:
            self.rejected(identity, node, operation["code"])
            return
        task = self.store.task_head(node["task_id"]) if node["task_id"] else None
        if task and self.service.recorded_unstarted(task):
            self.update(identity, node["node_id"], {"status": "failed"}, reconciled=node["status"] == "uncertain",
                data={"reason": "not_started", "reconciled": False})
            return
        unsafe = not task or task.get("deleted") or task["status"] in ("needs_resume", "uncertain", "recovering") or processes.pending(task)
        unknown = operation["state"] != "done" and not data.get("reconciled")
        if unsafe or unknown:
            if node["status"] != "uncertain":
                self.update(identity, node["node_id"], {"status": "uncertain"}, data={"reason": "execution_uncertain", "reconciled": False})
            return
        if node["status"] == "uncertain" and not data.get("reconciled"):
            return
        if node["status"] == "dispatching":
            self.update(identity, node["node_id"], {"status": "running"})
            node = dict(self.db.execute("SELECT * FROM plan_nodes WHERE plan_id=? AND node_id=?", (identity, node["node_id"])).fetchone())
        if not node["turn_id"]:
            self.update(identity, node["node_id"], {"status": "uncertain"}, data={"reason": "bound_turn_unknown", "reconciled": False})
            return
        turn = self.db.execute("SELECT status FROM turns WHERE task_id=? AND thread_id=? AND turn_id=?", (node["task_id"], data.get("thread_id"), node["turn_id"])).fetchone()
        if (not turn or turn[0] not in TERMINAL) and task.get("turn_id") != node["turn_id"]:
            if node["status"] != "uncertain":
                self.update(identity, node["node_id"], {"status": "uncertain"}, data={"reason": "bound_turn_changed", "reconciled": False})
            return
        family = self.service.resources.family(node["task_id"])
        children = self.db.execute("""WITH children AS (
            SELECT status,deleted,archived,json_extract(projection,'$.turn_status') turn_status FROM tasks
            WHERE root_task_id=? AND id!=?)
            SELECT COALESCE(MAX(status IN ('needs_resume','uncertain','recovering') OR
                ((deleted OR archived) AND COALESCE(turn_status,'') NOT IN ('completed','failed','interrupted'))),0) unknown,
            COALESCE(MAX(status IN ('starting','queued','running','waiting','interrupting')),0) active,
            COALESCE(MAX(status IN ('failed','interrupted') OR turn_status IN ('failed','interrupted')),0) failed
            FROM children""", (family, node["task_id"])).fetchone()
        child_unknown = children["unknown"] or self.db.execute("""SELECT 1 FROM workspace_claims c JOIN tasks t ON t.id=c.task_id
            WHERE t.root_task_id=? AND t.deleted=0 AND c.state!='inactive' AND json_extract(c.data,'$.reason')=? LIMIT 1""", (family, processes.REASON)).fetchone()
        if child_unknown:
            if node["status"] != "uncertain":
                self.update(identity, node["node_id"], {"status": "uncertain"}, data={"reason": "descendant_uncertain", "reconciled": False})
            return
        pending = self.db.execute(self.store.family_query() + "SELECT 1 FROM requests WHERE task_id IN (SELECT id FROM family) AND status IN ('pending','answering') LIMIT 1", (node["task_id"],)).fetchone()
        if not turn or turn[0] not in TERMINAL or children["active"] or pending:
            return
        budget, budget_stop = self.service.resources.policy(family)
        if not budget_stop or not budget_stop["active"]:
            crossed = self.service.resources.breach(self.service.resources.usage(node["task_id"]), budget) if budget else None
            if crossed:
                budget_stop = self.service.resources.stop(family, crossed)
                self.service.schedule_budget_check(node["task_id"])
        failed = turn[0] != "completed" or children["failed"] or budget_stop and budget_stop["active"]
        result = self.db.execute("SELECT id FROM turn_results WHERE task_id=? AND turn_id=? ORDER BY revision DESC LIMIT 1", (node["task_id"], node["turn_id"])).fetchone()
        if not result:
            self.store.evidence.result(node["task_id"], node["turn_id"], limit=1)
            result = self.db.execute("SELECT id FROM turn_results WHERE task_id=? AND turn_id=? ORDER BY revision DESC LIMIT 1", (node["task_id"], node["turn_id"])).fetchone()
        artifacts = [r[0] for r in self.db.execute("SELECT json_extract(j.value,'$.artifact_id') FROM turn_results r,json_each(r.data,'$.artifacts') j WHERE r.id=? LIMIT 65", (result[0],))]
        reference = {"tool": "codex_get", "task_id": node["task_id"], "turn_id": node["turn_id"], "result_id": result[0],
            "arguments": {"task_id": node["task_id"], "turn_id": node["turn_id"], "result_id": result[0], "view": "result"},
            "artifact_ids": artifacts[:64], "artifacts_truncated": len(artifacts) > 64}
        reason = "budget_exceeded" if budget_stop and budget_stop["active"] else turn[0] if turn[0] != "completed" else "descendant_failed" if children["failed"] else "completed"
        self.update(identity, node["node_id"], {"status": "failed" if failed else "succeeded", "result_ref": reference},
            data={"reason": reason, "reconciled": False}, reconciled=node["status"] == "uncertain")

    def rejected(self, identity, node, code):
        self.live_starts.pop(node["operation_id"], None)
        if code in RETRY and node["status"] == "dispatching":
            self.wait_versions[(identity, node["node_id"])] = self.attempt_versions.get((identity, node["node_id"]), self.version)
            ready_since = json.loads(node["data"]).get("ready_since", node["since"])
            self.update(identity, node["node_id"], {"status": "ready", "since": ready_since, "operation_id": None, "attempt": node["attempt"] + 1},
                data={"reason": code, "last_operation_id": node["operation_id"], "dispatch_arguments": None}, rejected=True)
        else:
            self.update(identity, node["node_id"], {"status": "failed"}, data={"reason": code}, reconciled=node["status"] == "uncertain")

    def launch(self, identity, canonical, node_id):
        key = ("dispatch", identity, node_id)
        if key in self.jobs:
            return
        node = next(n for n in self.nodes(identity) if n["node_id"] == node_id)
        spec = next(n for n in canonical["nodes"] if n["id"] == node_id)
        operation = f"plan:{identity}:{node_id}:{node['attempt']}"
        self.attempt_versions[(identity, node_id)] = self.version
        prompt = spec["prompt"]
        handoff = plans.handoff_text(canonical, node_id, self.state(identity)["nodes"])
        if handoff:
            prompt += "\n\n" + handoff
        args = {"request_id": operation}
        if spec["action"] == "start":
            args.update(spec["target"], prompt=prompt)
        else:
            parent = next(n for n in self.nodes(identity) if n["node_id"] == spec["target"]["node"])
            args["task_id"] = parent["task_id"]
            if spec["action"] == "send":
                args["prompt"] = prompt
            elif prompt:
                args.update(target="custom", instructions=("Review uncommitted changes in this project.\n\n" if not spec["prompt"] else "") + prompt)
        self.update(identity, node_id, {"status": "dispatching", "operation_id": operation}, data={"dispatch_arguments": args, "tool": "codex_" + spec["action"], "reason": None, "ready_since": node["since"]})
        if args.get("task_id"):
            self.bind(operation, args["task_id"])
        self.start_job(key, self.execute(identity, node_id, "codex_" + spec["action"], args))

    def launch_existing(self, identity, node):
        data = json.loads(node["data"])
        if data.get("dispatch_arguments") and data.get("tool"):
            self.start_job(("dispatch", identity, node["node_id"]), self.execute(identity, node["node_id"], data["tool"], data["dispatch_arguments"]))

    def start_job(self, key, work):
        if key in self.jobs:
            work.close()
            return
        job = asyncio.create_task(work)
        self.jobs[key] = job
        def done(current):
            self.jobs.pop(key, None)
            if not current.cancelled():
                current.exception()
            self.schedule(retry=False)
        job.add_done_callback(done)

    async def execute(self, identity, node_id, tool, args):
        self.service.current_operation.set(None)
        self.service.operation_context.set(None)
        try:
            await dispatch(self.service, tool, args)
        except Exception as exc:
            error = error_envelope(exc)["error"]
            self.update(identity, node_id, data={"error": bounded_result(error, 8192)})
            known = ("invalid_arguments", "operation_expired", "operation_conflict", "optional_native_capability_unavailable")
            if error["code"] in known[2:] or not self.db.execute("SELECT 1 FROM operations WHERE id=?", (args["request_id"],)).fetchone():
                self.update(identity, node_id, {"status": "failed" if error["code"] in known else "uncertain"}, data={"reason": error["code"]})
        else:
            row = self.db.execute("SELECT json_extract(d.data,'$.acknowledged_turn.task_id') task_id,json_extract(d.data,'$.acknowledged_turn.turn_id') turn_id FROM operation_details d WHERE d.id=?", (args["request_id"],)).fetchone()
            if row and row["task_id"] and row["turn_id"]:
                self.bind(args["request_id"], row["task_id"], turn_id=row["turn_id"])

    def launch_cancel(self, identity, node_id):
        node = next(n for n in self.nodes(identity) if n["node_id"] == node_id)
        data = json.loads(node["data"])
        if not node["task_id"] or not node["turn_id"]:
            return
        operation = data.get("cancel_key") or f"plan:{identity}:{node_id}:cancel"
        if data.get("cancel_key") and (self.db.execute("SELECT 1 FROM operations WHERE id=?", (operation,)).fetchone()
                or self.db.execute("SELECT 1 FROM operation_tombstones WHERE id=?", (operation,)).fetchone()):
            return
        self.update(identity, node_id, data={"cancel_key": operation})
        async def cancel():
            try:
                await dispatch(self.service, "codex_interrupt", {"task_id": node["task_id"], "expected_turn_id": node["turn_id"], "request_id": operation})
            except Exception as exc:
                row = self.db.execute("SELECT state FROM operations WHERE id=?", (operation,)).fetchone()
                if row and row[0] != "done":
                    current = next(n for n in self.nodes(identity) if n["node_id"] == node_id)
                    if current["status"] in ("running", "uncertain"):
                        self.update(identity, node_id, {"status": "uncertain"}, data={"reason": "cancel_uncertain", "reconciled": False,
                            "error": bounded_result(error_envelope(exc)["error"], 8192)})
        self.start_job(("cancel", identity, node_id), cancel())
