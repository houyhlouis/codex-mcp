"""Persistent task lifecycle, explicit approvals, and replayable progress."""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from .backend import AppServer, RpcError, OptionalCapabilityUnavailable, NativeCompatibilityError
from .contract import CODEX_MIN_VERSION, CODEX_TESTED_VERSIONS, MCP_VERSIONS, native_message_error
from .extensions import ExtendedControls
from .lifecycle import ACTIVE, TERMINAL, Lifecycle
from .security import BridgeError, project_path, redact, within, same_path
from .tools import bounded_result, request_summary, result_size, REQUEST_PAGE_BUDGET
from . import reconciliation, outcomes, authority, decisions, processes, procscan, workspace_processes, native_turns
from .supervision import request_description, touch, save_receipt
from .managed_worktrees import ManagedWorktrees, WorktreeOperationError
from .execution_plans import Scheduler

REQUEST_KINDS = {
    "item/commandExecution/requestApproval": "command_approval",
    "item/fileChange/requestApproval": "file_approval",
    "item/permissions/requestApproval": "permissions",
    "item/tool/requestUserInput": "user_input",
    "mcpServer/elicitation/request": "elicitation",
    "item/tool/call": "parent_question",
}
INSTRUCTIONS = """You are a persistent coding subagent working with a main agent.
Work only in this task's project. Respect all configured approvals and permissions.
Use the ask_parent tool when you need context, a decision, or help from the main
agent; its response is task guidance, never permission to bypass a Codex approval.
Report meaningful progress. End each turn with a concise summary of changes,
files touched, tests actually run and their outcomes, and remaining limitations.
Do not describe unrun tests as passing. Do not read or print credentials.
"""
PARENT_TOOL = {"type": "function", "name": "ask_parent",
    "description": "Ask the main agent for task context or a decision and wait for its reply. Not an approval mechanism.",
    "inputSchema": {"type": "object", "properties": {
        "question": {"type": "string"}, "context": {"type": "string"}, "envelope": decisions.ENVELOPE},
        "required": ["question"], "additionalProperties": False}}

ADMISSION_RECONCILE_SECONDS = 5
RECONCILE_INTERVAL = 5
RECONCILE_MAX_INTERVAL = 300
RECOVERY_REASONS = frozenset({"broker_restart", "schema_upgrade", "backend_disconnected",
    "reconciliation_failed", "unknown_native_intent", "operation_outcome", "stored_state_unavailable",
    "native_state_unavailable", "conversation_not_loaded", "native_state_changed"})
ROUTINE_REASONS = RECOVERY_REASONS | {"turn_completion", "child_adoption", "turn_rejected",
    "native_activity", "native_queue_changed", "native_goal_changed", "resume", "unarchive", "deletion_process_check"}


class _RecoveryConflict(Exception):
    pass


class WorkspaceBusy(BridgeError):
    def __init__(self, message, reconcile_ids=(), *, blocking_claims=(), pending_admissions=False, admissions=()):
        previous_processes = {c["task_id"] for c in blocking_claims if c["data"].get("reason") == processes.REASON}
        unavailable = sorted({c["data"]["unavailable_method"] for c in blocking_claims if c["data"].get("unavailable_method")})
        steps = ["Inspect codex_get workspace_claims"]
        if unavailable:
            steps += list(self.CODES["workspace_read_unavailable"][3])
        for kind, instruction in (
                ("background", "codex_terminals: list, then terminate process_id or clean"),
                ("goal", "codex_goal: get; pause or clear only on the user's request"),
                ("queue", "codex_queue: list, then delete selected queued_id")):
            for task_id in sorted({c["task_id"] for c in blocking_claims if c["kind"] == kind
                    and (kind != "background" or c["task_id"] not in previous_processes and not workspace_processes.held(c))})[:8]:
                steps.append(f"{instruction}; task_id={task_id}")
        if blocking_claims:
            steps.append("Use codex_resume with a new operation_id to re-check native state")
        steps += [workspace_processes.message(c) for c in blocking_claims if workspace_processes.held(c)][:8]
        if admissions:
            steps.append("Wait for the listed admission operation_id; inspect codex_list")
        super().__init__(message, code="workspace_read_unavailable" if unavailable else "workspace_busy",
            uncertain=any(c["state"] == "uncertain" for c in blocking_claims),
            next_steps=steps,
            ids={"task_ids": sorted({c["task_id"] for c in blocking_claims})[:32],
                 "family_ids": sorted({a["family_id"] for a in admissions})[:32],
                 "operation_ids": [a["operation_id"] for a in admissions[:32]]},
            details={"blocking_claims": [{k: c.get(k) for k in ("task_id", "kind", "state", "revision")} for c in blocking_claims[:32]],
                     "blocking_claims_total": len(blocking_claims), "unavailable_methods": unavailable,
                     "pending_admissions": bool(pending_admissions), "admissions_total": len(admissions)})
        self.reconcile_ids = tuple(reconcile_ids)
        self.blocking_claims = tuple(blocking_claims)
        self.pending_admissions = pending_admissions


class Service(ExtendedControls):
    def __init__(self, config, store, backend_factory=AppServer):
        self.config, self.store = config, store
        self.lifecycle = Lifecycle(store)
        self.resources = store.resources
        self.resources.defaults = getattr(config, "budget", None) or {}
        self.budget_tasks = {}
        self.budget_dirty = set()
        self.budget_timers = {}
        self.backend = backend_factory(config, self.on_message, self.on_exit)
        self.backend_lock = asyncio.Lock()
        # Lock order: admission -> SQLite transaction, then release both before
        # awaiting a family lock. Family controls may take backend/adoption locks;
        # they never acquire admission. Events, interrupts and responses take no
        # family lock. Final acknowledgement/admission cleanup follows its release.
        self.admission_lock = asyncio.Lock()
        self.mutation_lock = self.admission_lock
        self.family_locks = {}
        self.project_operations = {}
        self.managed_worktrees = ManagedWorktrees(self)
        self.operation_tasks = {}
        self.response_tasks = {}
        self.intent_inflight = {}
        self.reconciliation_tasks = set()
        self.reconciliation_by_task = {}
        self.reconciliation_dirty = set()
        self.reconciliation_fresh = set()
        self.reconciliation_timers = {}
        self.reconciliation_failures = {}
        self.reconciliation_calls = {}
        self.workspace_scan_calls = {}
        self.os_process_observations = {}
        self.process_activity = {}
        self.process_scanner = self.scan_projects
        self.current_operation = ContextVar("codex_control_operation", default=None)
        self.operation_context = ContextVar("codex_operation_context", default=None)
        self.process_acknowledgement = ContextVar("codex_process_acknowledgement", default=None)
        self.delegation_context = ContextVar("codex_delegation_contract", default=None)
        self.changed = asyncio.Condition()
        self.change_scheduled = False
        self.store.on_change = self.signal_change
        self.store.on_write = self.local_write
        self.store.before_write = self.check_local_write
        self.backend.on_write = self.native_write
        self.native_observations = {}
        self.inline_reviews = {}
        self.terminal_sequence = 0
        self.terminal_observations = {}
        self.deletion_requests = {}
        self.loaded = set()
        self.raw_requests = {}
        self.file_proposals = {}
        self.proposal_revision = 0
        self.adoption_lock = asyncio.Lock()
        self.shutting_down = False
        self.protocol_quarantine = set()
        self.protocol_epochs = {}
        self._recover()
        self.scheduler = Scheduler(self)
        self.scheduler.recover()

    def _recover(self):
        self.resources.disconnect()
        with self.store.transaction():
            for _, task in self.store.records("tasks", light=True, recovery=True):
                if task.get("deleted"):
                    self.record_deletion(task["id"])
                    continue
                processes.capture(self, task)
                if processes.pending(self.store.task_head(task["id"])):
                    processes.hold(self, task["id"])
                elif self.store.claims(task_id=task["id"]) or task["status"] in ACTIVE:
                    self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "broker_restart"})
                if task["status"] in ACTIVE:
                    self.lifecycle.gap(task["id"], "broker_restart", disconnected=True)
                    self.store.event(task["id"], "bridge/recovery", {
                        "message": "Broker restarted. Resume to reconcile Codex history; no prompt was replayed."})
            for task in self.store.unscanned_archives():
                processes.capture(self, task)
                if processes.pending(self.store.task_head(task["id"])):
                    processes.hold(self, task["id"])
            for _, req in self.store.records("requests", statuses=("pending", "answering"), light=True):
                self.store.update("requests", req["id"], {"status": "expired"})
            self.store.db.execute("UPDATE operations SET state='uncertain' WHERE state='running'")
            self.store.db.execute("DELETE FROM admissions")
            self.managed_worktrees.restore()

    async def wake(self):
        async with self.changed:
            self.changed.notify_all()

    def native_write(self):
        context = self.operation_context.get()
        if context and context["key"] == self.current_operation.get():
            if context["phase"] == "rejected":
                raise BridgeError("Operation was rejected before native execution; use a new operation_id", code="state_conflict")
            # The mutable record is shared with tasks spawned by the operation.
            context["native_write"] = True
            if context.pop("pending_execution", False):
                context["execution_write"] = True
                self.scheduler.execution_sent(context["key"])

    def local_write(self):
        context = self.operation_context.get()
        if context and context["key"] == self.current_operation.get() and context["phase"] == "control":
            context["local_write"] = True

    def check_local_write(self):
        context = self.operation_context.get()
        if context and context["key"] == self.current_operation.get() and context["phase"] == "rejected":
            raise BridgeError("Operation was rejected before local execution; use a new operation_id", code="state_conflict")

    def reset_reconciliation(self, task_id):
        self.reconciliation_failures.pop(task_id, None)
        timer = self.reconciliation_timers.pop(task_id, None)
        if timer:
            timer.cancel()

    def terminal_token(self):
        self.terminal_sequence += 1
        return self.terminal_sequence

    def workspace_snapshot(self, family):
        return (self.backend.generation, self.backend.alive,
            tuple((t["id"], t["revision"]) for _, t in self.store.records("tasks", family_id=family, light=True)),
            tuple((c["task_id"], c["kind"], c["revision"]) for c in self.store.claims(family_id=family, include_inactive=True)))

    def workspace_bookkeeping(self, before, after):
        if before[:2] != after[:2]:
            return False
        old, new = dict(before[2]), dict(after[2])
        if old.keys() != new.keys() or not all(self.store.bookkeeping_only("tasks", key, old[key], new[key]) for key in old):
            return False
        old_claims = {(t, k): r for t, k, r in before[3]}
        new_claims = {(t, k): r for t, k, r in after[3]}
        return old_claims.keys() <= new_claims.keys() and all(self.store.bookkeeping_only("claims", key, old_claims.get(key, 0), revision)
            for key, revision in new_claims.items())

    async def scan_projects(self, projects):
        if not self.config.sandbox_process_check or sys.platform != "linux":
            return procscan.scan_for(self.config, projects)
        return await asyncio.to_thread(procscan.scan_for, self.config, projects)

    async def scan_workspace(self, project):
        with self.track_background(self.workspace_scan_calls, project):
            return await self._scan_workspace(project)

    async def _scan_workspace(self, project):
        generation, activity = self.backend.generation, self.process_activity.get(project, 0)
        checked_at = time.time()
        try:
            result = await self.process_scanner([project])
        except (OSError, ValueError):
            result = {"available": False, "reason": "proc_unreadable", "processes": [], "truncated": False, "scanned": 0}
        return {**result, "checked_at": checked_at, "_generation": generation, "_activity": activity}

    def invalidate_process_check(self, project):
        self.process_activity[project] = self.process_activity.get(project, 0) + 1

    @contextmanager
    def track_background(self, calls, key):
        job = asyncio.current_task()
        completion = asyncio.get_running_loop().create_future()
        pending = calls.setdefault(key, {})
        pending[completion] = job
        try:
            yield
        finally:
            pending.pop(completion, None)
            if not completion.done():
                completion.set_result(None)
            if not pending:
                calls.pop(key, None)

    async def settle_family(self, task_id):
        caller = asyncio.current_task()
        async def wait():
            while True:
                # Drain completion callbacks too: they may schedule a fresh
                # pass after an earlier read lost a revision race.
                turn = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(lambda future=turn: not future.done() and future.set_result(None))
                await turn
                task = self.store.task_head(task_id)
                family = task.get("root_task_id", task_id)
                ids = self.store.family_ids(family)
                jobs = {self.reconciliation_by_task[key] for key in ids if key in self.reconciliation_by_task}
                jobs.update(done for key in ids for done, owner in self.reconciliation_calls.get(key, {}).items() if owner is not caller)
                for project, scans in self.workspace_scan_calls.items():
                    if within(Path(project), Path(task["project"])) or within(Path(task["project"]), Path(project)):
                        jobs.update(done for done, owner in scans.items() if owner is not caller)
                jobs = {job for job in jobs if job is not caller and job is not asyncio.current_task() and not job.done()}
                if not jobs:
                    return
                await asyncio.wait(jobs)  # A caller deadline never cancels these reads.
        try:
            await asyncio.wait_for(wait(), ADMISSION_RECONCILE_SECONDS)
        except TimeoutError as exc:
            raise BridgeError("Task-family reconciliation did not settle within 5 seconds; inspect codex_get and retry with a new request_id",
                code="state_conflict", ids={"task_id": task_id}) from exc

    async def os_process_status(self):
        # An empty scope probes availability without reading any cmdlines.
        checked_at = time.time()
        try:
            result = await self.process_scanner([])
        except (OSError, ValueError):
            result = {"available": False, "reason": "proc_unreadable"}
        return {"enabled": self.config.sandbox_process_check, "available": result["available"], "reason": result["reason"],
            "checked_at": checked_at}

    def process_check_view(self, task):
        result = self.os_process_observations.get(task["project"])
        if result is None:
            return workspace_processes.unchecked(reason="disabled" if not self.config.sandbox_process_check else "not_checked")
        if (not workspace_processes.waiver(result) and (result.get("_generation") != self.backend.generation
                or result.get("_activity") != self.process_activity.get(task["project"], 0))):
            return workspace_processes.unchecked(result.get("checked_at"))
        return workspace_processes.summary(result)

    def process_view(self, task):
        # A deletion's bounded audit outcome is immutable. Current process
        # observations remain separately available in the reconciliation view.
        state = workspace_processes.saved_state(task.get("process_state"))
        if task.get("deleted") or (task["project"] not in self.os_process_observations and not state
                and not any(workspace_processes.held(c) for c in self.store.claims(task_id=task["id"]))):
            return state
        return {**(state or {}), "os_process_check": self.process_check_view(task)}

    def family_executed(self, family, project):
        for _, task in self.store.records("tasks", family_id=family, light=True):
            if task.get("workspace_execution_project") == project:
                return True
            proven = self.store.db.execute("""SELECT 1 FROM operation_details WHERE task_id=?
                AND json_extract(data,'$.operation_outcome.acknowledgement')='acknowledged'
                AND json_extract(data,'$.acknowledged_turn.turn_id') IS NOT NULL LIMIT 1""", (task["id"],)).fetchone()
            if proven and task["project"] == project:
                return True
        return False

    async def check_new_family(self, key, family):
        reservation = self.store.db.execute("SELECT project FROM admissions WHERE operation_id=?", (key,)).fetchone()
        if not reservation or self.family_executed(family, reservation[0]):
            return
        project = reservation[0]
        for _ in range(3):
            snapshot = self.workspace_snapshot(family)
            result = await self.scan_workspace(project)
            async with self.admission_lock:
                if snapshot != self.workspace_snapshot(family):
                    continue
                self.capacity(project)
                self.os_process_observations[project] = result
                if not self.family_executed(family, project) and workspace_processes.outcome(result) != "clear":
                    raise self.unowned_processes(result)
                context = self.operation_context.get()
                if context:
                    context["process_admission_project"] = project
                return
        raise BridgeError("Workspace changed during the process check; inspect codex_get and retry with a new request_id", code="state_conflict")

    @staticmethod
    def unowned_processes(result):
        message = (workspace_processes.UNAVAILABLE_MESSAGE if workspace_processes.outcome(result) == "unavailable"
            else procscan.describe(result) + workspace_processes.UNOWNED_ADVICE)
        failure = WorkspaceBusy(message)
        failure.details["os_process_check"] = workspace_processes.summary(result)
        failure.next_steps = [str(failure)]
        return failure

    async def prepare_release_scan(self, task_id, states=None, deleting=(), *, force=False):
        task = self.store.task_head(task_id)
        family = task.get("root_task_id", task_id)
        snapshot = self.workspace_snapshot(family)
        survivors = [t for _, t in self.store.records("tasks", family_id=family, light=True)
            if not t.get("deleted") and t["id"] not in deleting]
        claims = self.store.claims(family_id=family)
        remaining = [c for c in claims if c["task_id"] not in deleting and not workspace_processes.held(c)
            and not (c["task_id"] == task_id and states is not None and states.get(c["kind"]) is False)]
        if not survivors or remaining and not force:
            return None, snapshot
        result = await self.scan_workspace(task["project"])
        if self.workspace_snapshot(family) != snapshot:
            return False, snapshot
        self.os_process_observations[task["project"]] = result
        return result, snapshot

    def apply_release_scan(self, task_id, result, *, deleting=()):
        if not result:
            return
        task = self.store.task_head(task_id)
        family = task.get("root_task_id", task_id)
        family_claims = self.store.claims(family_id=family)
        waived = workspace_processes.waiver(result)
        if waived:
            for claim in family_claims:
                if workspace_processes.held(claim):
                    data = {"source": workspace_processes.SOURCE, "reason": result["reason"], "message": waived}
                    self.store.claim(claim["task_id"], "background", "inactive", data, if_changed=True)
                    self.store.event(claim["task_id"], "bridge/os_process_check_waived", {**data,
                        "previous_claim_revision": claim["revision"], "pids": claim["data"]["pids"]})
            return
        holding = workspace_processes.outcome(result) != "clear"
        if holding and any(c["task_id"] not in deleting and not workspace_processes.held(c) for c in family_claims):
            # While native execution owns the family, matches can be its
            # tracked commands. Refresh the hold after native inactivity is
            # proven; never label those commands as untracked leftovers.
            return
        claims = [c for c in family_claims if workspace_processes.held(c)]
        owners = [c["task_id"] for c in claims if c["task_id"] not in deleting]
        survivors = [t["id"] for _, t in self.store.records("tasks", family_id=family, light=True)
            if not t.get("deleted") and t["id"] not in deleting]
        occupied = {c["task_id"] for c in family_claims if c["kind"] == "background" and not workspace_processes.held(c)}
        eligible = [key for key in survivors if key not in occupied]
        owner = next(iter(owners or ([task_id] if task_id in eligible else eligible)), None)
        for claim in claims:
            if not holding or claim["task_id"] != owner:
                self.store.claim(claim["task_id"], "background", "inactive", {"source": workspace_processes.SOURCE}, if_changed=True)
        if owner and holding:
            state = "active" if workspace_processes.alive(result) else "uncertain"
            self.store.claim(owner, "background", state, workspace_processes.data(result), if_changed=True)

    def remember_process_check(self, task_id):
        claim = self.claim_snapshot(task_id, "background")
        if not claim or claim["state"] == "inactive" or not workspace_processes.held(claim):
            return
        sequence = self.reconciliation_sequence(task_id)
        previous = self.reconciliation_failures.get(task_id, {})
        signature = json.dumps([claim["data"], {k: v for k, v in workspace_processes.summary(self.os_process_observations[claim["project"]]).items() if k != "checked_at"}
            if claim["project"] in self.os_process_observations else None], sort_keys=True)
        same = (previous.get("source") == workspace_processes.SOURCE and previous.get("signature") == signature
            and previous.get("generation") == self.backend.generation and previous.get("sequence") == sequence)
        self.reconciliation_failures[task_id] = {"source": workspace_processes.SOURCE, "signature": signature,
            "generation": self.backend.generation, "sequence": sequence, "observed_sequence": sequence,
            "delay": min(previous["delay"] * 2, RECONCILE_MAX_INTERVAL) if same else RECONCILE_INTERVAL}

    def reconciliation_sequence(self, task_id):
        if self.store.schema_version < 8:
            return self.store.task_head(task_id)["revision"]
        row = self.store.db.execute("SELECT family_seq FROM task_changes WHERE task_id=?", (task_id,)).fetchone()
        return row[0] if row else 0

    def signal_change(self, task_id=None):
        if hasattr(self, "scheduler"):
            self.scheduler.changed()
        failed = self.reconciliation_failures.get(task_id)
        if failed and failed["sequence"] != self.reconciliation_sequence(task_id):
            self.reset_reconciliation(task_id)
            self.schedule_reconciliation(task_id)
        if self.change_scheduled or self.shutting_down:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Synchronous startup has no waiting observers.
        self.change_scheduled = True
        def notify():
            self.change_scheduled = False
            if not self.shutting_down:
                asyncio.create_task(self.wake())
        loop.call_soon(notify)

    async def ensure_backend(self):
        async with self.backend_lock:
            if not self.backend.alive:
                await self.backend.close()
                self.loaded.clear()
                token = self.current_operation.set(None)
                try:
                    await self.backend.start()
                finally:
                    self.current_operation.reset(token)
                self.reconciliation_failures.clear()
                for task_id in self.store.claim_task_ids():
                    self.schedule_reconciliation(task_id)

    def task(self, task_id, allow_deleted=False, *, light=False):
        task = self.store.task_head(task_id) if light else self.store.get("tasks", task_id)
        return self.checked_task(task, allow_deleted=allow_deleted)

    def checked_task(self, task, *, allow_deleted=False, projects=None):
        if task.get("deleted") and not allow_deleted:
            raise BridgeError("This native conversation was deleted; its audit journal remains available", code='state_conflict')
        if projects is not None:
            if task["project_id"] not in projects:
                projects[task["project_id"]] = self.store.get("projects", task["project_id"])
            descriptor = projects[task["project_id"]]
        else:
            descriptor = self.store.get("projects", task["project_id"])
        if descriptor.get("removed") and not allow_deleted:
            raise BridgeError("Managed worktree project was removed; its task history remains available", code="state_conflict")
        current = project_path(self.config, descriptor["path"], allow_missing=allow_deleted and bool(descriptor.get("worktree")))
        if not same_path(current, task["project"]):
            raise BridgeError("Project location changed; task access denied", code='scope_unverified')
        return {**task, "project_removed": True} if descriptor.get("removed") else task

    def task_for_thread(self, thread_id):
        return self.store.task_for_thread(thread_id)

    def public_request(self, req):
        result = {k: v for k, v in req.items() if k not in ("rpc_id", "generation")}
        if req.get("kind") in ("file_approval", "command_approval", "permissions"):
            task = self.task(req["task_id"], allow_deleted=True)
            result["permission_view"] = authority.permission_view(self, req, self.raw_requests.get(req["id"], req.get("params", {})), task)
        result.update(authority.review_markers(req))
        return decisions.public_request(result)

    def requests(self, task_id=None):
        if task_id:
            self.task(task_id, allow_deleted=True, light=True)
        return [self.public_request(r) for _, r in self.store.records("requests", statuses=("pending",), family_id=task_id)]

    def blocking_requests(self, task_id):
        return any(r.get("blocking", True) for _, r in self.store.records("requests", statuses=("pending",), family_id=task_id, light=True))

    def request_page(self, task_id=None, cursor=None, limit=50):
        if task_id:
            self.task(task_id, allow_deleted=True, light=True)
        epoch = self.store.db.execute("SELECT request_epoch FROM retention_state WHERE id=1").fetchone()[0]
        try:
            prefix = f"requests:{task_id or '*'}:"
            if cursor is not None and not cursor.startswith(prefix):
                raise ValueError
            parts = cursor[len(prefix):].split(":") if cursor is not None else ["0"]
            if len(parts) not in (1, 2):
                raise ValueError
            cursor_epoch, after = (0, int(parts[0])) if len(parts) == 1 else (int(parts[0]), int(parts[1]))
            if min(cursor_epoch, after) < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise BridgeError("Invalid pending-request cursor", code='invalid_arguments') from exc
        if cursor is not None and cursor_epoch != epoch:
            raise BridgeError("Pending-request cursor has expired; restart without a cursor", code="cursor_expired",
                              next_steps=["Restart codex_requests or the embedded requests page without its cursor"],
                              details={"resume_cursor": None, "cursor_kind": "requests"})
        if epoch:
            prefix += f"{epoch}:"
        if after > self.store.db.execute("SELECT COALESCE(MAX(rowid),0) FROM requests").fetchone()[0]:
            raise BridgeError("Pending-request cursor is ahead of this request list; restart without a cursor", code='invalid_arguments')
        pending = list(self.store.records("requests", statuses=("pending",), family_id=task_id, light=True, after=after, limit=limit + 1))
        summaries, last = [], after
        for seq, request in pending[:limit]:
            summary = request
            if summaries and result_size({"requests": [*summaries, summary]}) > REQUEST_PAGE_BUDGET:
                break
            summaries.append(summary)
            last = seq
        more = any(seq > last for seq, _ in pending)
        return {"requests": summaries, "next_cursor": prefix + str(last) if more else None,
                "has_more": more, "total": self.store.request_count(task_id)}

    @staticmethod
    def pending_fields(page):
        return {"pending_requests": page["requests"], "pending_requests_next_cursor": page["next_cursor"],
                "pending_requests_has_more": page["has_more"], "pending_requests_total": page["total"]}

    def get(self, task_id, requests_cursor=None, requests_limit=50, subagents_offset=0, subagents_limit=50,
            claims_offset=0, claims_limit=100, view="default", turn_id=None, artifact_id=None, result_id=None, offset=0, limit=None):
        task = self.task(task_id, allow_deleted=True, light=view != "default")
        if view == "reconciliation":
            return {"id": task_id, "status": task["status"], "view": view, "reconciliation": reconciliation.report(self, task_id)}
        if view == "permissions":
            count = 50 if limit is None else limit
            if not 1 <= count <= 200:
                raise BridgeError("Permission receipt limit must be between 1 and 200", code="invalid_arguments")
            receipts = self.store.db.execute("SELECT data FROM decision_receipts WHERE task_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (task_id, count + 1, offset)).fetchall()
            return {"id": task_id, "status": task["status"], "view": view,
                "permission_view": authority.settings_view(self, self.task(task_id, allow_deleted=True)),
                "decision_receipts": [json.loads(r[0]) for r in receipts[:count]],
                "receipts_next_offset": offset + count if len(receipts) > count else None}
        if view == "result":
            result = self.store.evidence.result(task_id, turn_id if result_id else turn_id or task.get("turn_id"), offset, 50 if limit is None else limit, result_id)
            self.schedule_budget_check(task_id)
            return {"id": task_id, "status": task["status"], "view": view,
                    "family_usage": self.resources.usage(task_id), "budget_stop": self.resources.policy(self.resources.family(task_id))[1],
                    "result": result}
        if view == "artifact":
            return {"id": task_id, "status": task["status"], "view": view,
                    "artifact": self.store.evidence.read(task, artifact_id, offset, 65536 if limit is None else limit)}
        if view == "default":
            task = self.lifecycle.view(task)
        else:
            task.pop("process_scan", None)
        total, active = self.store.family_stats(task_id)
        children = [t for _, t in self.store.records("tasks", family_id=task_id, light=True, exclude_id=task_id, offset=subagents_offset, limit=subagents_limit)]
        page = self.request_page(task_id, requests_cursor, requests_limit)
        if task.get("deleted"):
            page = {"requests": [], "next_cursor": None, "has_more": False, "total": 0}
        family = task.get("root_task_id", task_id)
        claims_total = self.store.claim_count(family)
        claims = self.store.claims(family_id=family, offset=claims_offset, limit=claims_limit)
        task["process_state"] = self.process_view(task)
        task["family_usage"] = self.resources.usage(task_id, compact=view == "compact")
        task["budget"], task["budget_stop"] = self.resources.policy(family)
        if view == "compact":
            task.pop("workspace_execution_project", None)
        return {**bounded_result(decisions.task_view(task), 256 * 1024), **self.pending_fields(page), "view": view,
                "family_status": "active" if active else "idle",
                "subagents": [bounded_result({k: t.get(k) for k in ("id", "thread_id", "parent_task_id", "nickname", "status", "can_accept_direct_input")}, 8192)
                              for t in children],
                "subagents_next_offset": subagents_offset + subagents_limit if subagents_offset + subagents_limit < total - 1 else None,
                "workspace_claims": [bounded_result({**c, **({"message": workspace_processes.message(c)} if workspace_processes.held(c) else {})}, 8192) for c in claims],
                "workspace_claims_next_offset": claims_offset + claims_limit if claims_offset + claims_limit < claims_total else None,
                "workspace_claims_total": claims_total, "workspace_reserved": bool(claims_total),
                "event_cursor": self.store.highwater()}

    def list_tasks(self, offset=0, limit=50, view="default"):
        visible, projects = [], {}
        for _, task in self.store.records("tasks", light=True, visible=True, reverse=True):
            try:
                task = self.checked_task(task, allow_deleted=True, projects=projects)
            except (BridgeError, OSError):
                continue
            visible.append(task)
            if len(visible) >= offset + limit + 1:
                break
        records = [bounded_result(decisions.task_view({k: v for k, v in t.items() if k not in ("process_scan", "workspace_execution_project")} if view == "compact" else self.lifecycle.view({**self.store.get("tasks", t["id"]),
            **({"project_removed": True} if t.get("project_removed") else {})})), 128 * 1024) for t in visible[offset:offset + limit]]
        while len(records) > 1 and result_size({"tasks": records}) > 6 * 1024 * 1024:
            records.pop()
        return {"tasks": records, "view": view,
                "next_offset": offset + len(records) if offset + len(records) < len(visible) else None}

    def register_project(self, path):
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-mcp:" + os.path.normcase(str(path))))
        if self.store.db.execute("SELECT id FROM projects WHERE id=?", (key,)).fetchone():
            record = self.store.get("projects", key)
            if record.get("removed"):
                raise BridgeError("Managed worktree project was removed; inspect or create a managed worktree before reusing it", code="state_conflict")
            if record.get("worktree_state") == "incomplete":
                raise BridgeError("Managed worktree is incomplete; use codex_worktrees inspect or cleanup before starting work", code="state_conflict")
            return record
        record = {"id": key, "project_id": key, "path": str(path), "created_at": time.time()}
        return self.store.save("projects", record)

    def select_project(self, workdir=None, project_id=None, create=False, *, register=True):
        if bool(workdir) == bool(project_id):
            raise BridgeError("Supply exactly one existing workdir or selected project_id", code='invalid_arguments')
        descriptor = self.store.get("projects", project_id) if project_id else None
        if descriptor and descriptor.get("removed"):
            raise BridgeError("Managed worktree project was removed; inspect or create a managed worktree before reusing it", code="state_conflict")
        if descriptor and descriptor.get("worktree_state") == "incomplete":
            raise BridgeError("Managed worktree is incomplete; use codex_worktrees inspect or cleanup before starting work", code="state_conflict")
        value = workdir if workdir else descriptor["path"]
        if workdir:
            candidate = project_path(self.config, workdir, allow_missing=create)
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-mcp:" + os.path.normcase(str(candidate))))
            present = self.store.db.execute("SELECT id FROM projects WHERE id=?", (key,)).fetchone()
            if present:
                known = self.store.get("projects", key)
                if known.get("removed"):
                    raise BridgeError("Managed worktree project was removed; inspect or create a managed worktree before reusing it", code="state_conflict")
                if known.get("worktree_state") == "incomplete":
                    raise BridgeError("Managed worktree is incomplete; use codex_worktrees inspect or cleanup before starting work", code="state_conflict")
        path = project_path(self.config, value, create=create)
        if not register:
            return {"path": str(path)}
        with self.store.transaction():
            return self.register_project(path)

    def projects(self, action="list", path=None, offset=0, limit=100):
        if action == "list":
            entries = [p for _, p in self.store.records("projects", offset=offset, limit=limit + 1)]
            return {"projects": entries[:limit],
                    "next_offset": offset + limit if len(entries) > limit else None,
                    "note": "Projects explicitly selected or created by the main agent"}
        if action not in ("select", "create") or not path:
            raise BridgeError("select/create requires an absolute path chosen by the main agent", code='invalid_arguments')
        return self.select_project(workdir=path, create=action == "create")

    async def worktrees(self, **args):
        return await self.managed_worktrees.dispatch(**args)

    async def plans(self, **args):
        return await self.scheduler.call(**args)

    async def models(self, cursor=None, limit=100):
        await self.ensure_backend()
        return await self.backend.call("model/list", {"cursor": cursor, "limit": limit})

    async def choose_model(self, model=None, effort=None, service_tier=None):
        models, cursor = [], None
        for _ in range(100):
            page = await self.models(cursor)
            models.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        else:
            raise BridgeError("Model catalog pagination did not terminate", code='invalid_arguments')
        selected = next((m for m in models if model in (m["id"], m["model"])), None) if model else next((m for m in models if m["isDefault"]), None)
        if not selected:
            raise BridgeError("Model is not in the visible Codex catalog; call codex_models", code='invalid_arguments')
        selected_effort = effort or selected["defaultReasoningEffort"]
        if selected_effort not in [e["reasoningEffort"] for e in selected["supportedReasoningEfforts"]]:
            raise BridgeError("Unsupported reasoning effort for this model; call codex_models", code='invalid_arguments')
        if service_tier is not None and service_tier != "default" and service_tier not in [x["id"] for x in selected.get("serviceTiers", [])]:
            raise BridgeError("Service tier is not offered for this model", code='invalid_arguments')
        return selected["model"], selected_effort

    async def status(self, requests_cursor=None, requests_limit=50):
        await self.ensure_backend()
        account_error = None
        try:
            account = await self.backend.call("account/read", {"refreshToken": False})
        except OptionalCapabilityUnavailable as exc:
            account, account_error = {}, exc.as_dict()
        status = {"codex_version": self.backend.version, "backend": "app-server/stdio",
                "signed_in": bool(account.get("account")) if account_error is None else None,
                "authentication_type": (account.get("account") or {}).get("type"),
                "requires_openai_auth": account.get("requiresOpenaiAuth"),
                "approval_relay": self.config.approval_relay,
                "allow_external_tools": self.config.allow_external_tools,
                "state_schema_version": self.store.schema_version,
                "event_cursor": self.store.highwater(),
                "catalog_is_not_entitlement": True}
        status.update(self.compatibility_fields())
        status["os_process_check"] = await self.os_process_status()
        if account_error:
            status["account_error"] = account_error
        return {**bounded_result(status, 256 * 1024),
                **self.pending_fields(self.request_page(cursor=requests_cursor, limit=requests_limit))}

    def compatibility_fields(self):
        return {"compatibility": dict(self.backend.compatibility),
                "minimum_codex_version": CODEX_MIN_VERSION,
                "tested_codex_versions": list(CODEX_TESTED_VERSIONS),
                "supported_mcp_revisions": list(MCP_VERSIONS),
                "unavailable_optional_methods": sorted(self.backend.unavailable_methods),
                "ingress_backlog": self.backend.backlog}

    async def thread_options(self, task):
        config = {"model_reasoning_effort": task["effort"]}
        if not self.config.allow_external_tools:
            effective = await self.backend.call("config/read", {"cwd": task["project"], "includeLayers": False})
            for name in effective.get("config", {}).get("mcp_servers", {}):
                config[f"mcp_servers.{json.dumps(name)}.enabled"] = False
        result = {"cwd": task.get("native_cwd", task["project"]), "model": task["model"],
                "permissions": self.backend.profile(task["access"]),
                "runtimeWorkspaceRoots": [task["project"]],
                "approvalPolicy": "on-request", "config": config,
                "developerInstructions": INSTRUCTIONS}
        descriptor = self.store.get("projects", task["project_id"])
        if descriptor.get("worktree"):
            result["developerInstructions"] += ("\nThis is a managed worktree. Shared Git metadata is outside your project sandbox. "
                "Edit project files; ask the main agent to use codex_worktrees handoff for the patch. Do not modify shared Git metadata.\n")
        result.update(self.reviewer_params(task.get("approval_mode", "inherit")))
        if task.get("service_tier"):
            result["serviceTier"] = task["service_tier"]
        return result

    def capacity(self, project, exclude=None):
        family = self.store.task_head(exclude).get("root_task_id", exclude) if exclude else None
        own = self.admission()
        if family is None and own:
            family = own["family_id"]
        claims = [c for c in self.store.claims() if c["family_id"] != family]
        admissions = [r for r in self.managed_worktrees.reservations() if r["family_id"] != family]
        counted = [r for r in admissions if r["operation_id"] not in self.project_operations]
        if len({c["family_id"] for c in [*claims, *counted]}) >= self.config.max_tasks:
            admissions = counted
            leading = "Concurrent task limit reached; interrupt or finish another task"
        else:
            overlaps = lambda c: within(Path(c["project"]), Path(project)) or within(Path(project), Path(c["project"]))
            claims, admissions = [c for c in claims if overlaps(c)], [r for r in admissions if overlaps(r)]
            if not claims and not admissions:
                return
            leading = "Another task is active in this project. Use a separate project/worktree or finish it first"
        raise self.workspace_busy(leading, claims, admissions)

    async def check_capacity(self, project, exclude=None):
        try:
            self.capacity(project, exclude=exclude)
        except WorkspaceBusy as blocked:
            if not blocked.reconcile_ids:
                raise
            await self.wait_for_reconciliation(blocked.reconcile_ids)
            self.capacity(project, exclude=exclude)
        own, context = self.admission(), self.operation_context.get()
        family = self.store.task_head(exclude).get("root_task_id", exclude) if exclude else own["family_id"] if own else "unregistered"
        if (not self.config.sandbox_process_check or self.family_executed(family, project)
                or context and context.get("process_admission_project") == project):
            return
        for _ in range(3):
            snapshot = self.workspace_snapshot(family)
            result = await self.scan_workspace(project)
            if snapshot != self.workspace_snapshot(family):
                continue
            self.capacity(project, exclude=exclude)
            self.os_process_observations[project] = result
            if workspace_processes.outcome(result) != "clear" and not self.family_executed(family, project):
                raise self.unowned_processes(result)
            return
        raise BridgeError("Workspace changed during the process check; inspect codex_get and retry with a new request_id", code="state_conflict")

    @staticmethod
    def unavailable_conversation(task):
        message = ("This conversation no longer exists in Codex or cannot be read. "
            f"Use codex_delete with confirm_thread_id={task['thread_id']} to free this project")
        return BridgeError(message, code="state_conflict", ids={"task_id": task["id"], "thread_id": task["thread_id"]},
            next_steps=[f"codex_delete task_id={task['id']} confirm_thread_id={task['thread_id']} with a new operation_id"])

    def workspace_busy(self, leading, claims, admissions):
        previous_processes = {c["task_id"] for c in claims if c["data"].get("reason") == processes.REASON}
        owners = {}
        for claim in claims:
            entry = f"{claim['kind']}={claim['state']}"
            if claim["data"].get("reason"):
                entry += ":" + claim["data"]["reason"]
            if claim["data"].get("error"):
                entry += " (" + claim["data"]["error"] + ")"
            owners.setdefault(claim["task_id"], []).append(entry)
        for row in admissions:
            owners.setdefault(row["family_id"], []).append("admission=running")
        descriptions = [f"{'worktree operation ' + task_id[len('worktree:'):] if task_id.startswith('worktree:') else 'task ' + task_id} [{', '.join(kinds)}]"
            for task_id, kinds in sorted(owners.items())[:8]]
        if len(owners) > 8:
            descriptions.append(f"{len(owners) - 8} more tasks")
        message = leading + ". Blocking workspace claims: " + "; ".join(descriptions) + ". "
        if any(c["state"] == "uncertain" for c in claims):
            message += "Native state is unverified; workspace ownership was retained. "
        unavailable = sorted({c["data"]["unavailable_method"] for c in claims if c["data"].get("unavailable_method")})
        if unavailable:
            message += "Workspace activity could not be re-checked because optional native read API " + ", ".join(unavailable) + " is unavailable. Ownership was retained. "
        message += "Inspect codex_get for workspace_claims. "
        for row in admissions[:8]:
            if row["family_id"].startswith("worktree:"):
                message += f"Use codex_worktrees action=inspect path={row['project']} to inspect worktree operation {row['operation_id']}; no worktree mutation is retried automatically. "
        for task_id in sorted({c["task_id"] for c in claims if c["data"].get("reason") == "stored_state_unavailable"})[:8]:
            owner = self.store.task_head(task_id)
            message += f"For task {task_id}, use codex_delete with confirm_thread_id={owner['thread_id']} to free the project if the conversation is no longer available. "
        if any(c["data"].get("reason") == processes.REASON for c in claims):
            message += processes.MESSAGE + ". "
        for claim in claims[:32]:
            if workspace_processes.held(claim):
                message += workspace_processes.message(claim) + " "
        archived = {c["task_id"] for c in claims if self.store.task_head(c["task_id"]).get("archived")}
        for task_id in sorted(archived)[:8]:
            message += processes.archived_guidance(self.store.task_head(task_id)) + ". "
        for kind, instruction in (
                ("background", "use codex_terminals to list, then terminate each process_id or clean."),
                ("goal", "use codex_goal to pause or clear only on the user's request."),
                ("queue", "use codex_queue to list/delete queued entries.")):
            task_ids = sorted({c["task_id"] for c in claims if c["kind"] == kind
                and (kind != "background" or c["task_id"] not in previous_processes and not workspace_processes.held(c))})
            if task_ids:
                shown = ", ".join(task_ids[:8])
                if len(task_ids) > 8:
                    shown += f" and {len(task_ids) - 8} more"
                label = "task" if len(task_ids) == 1 else "tasks"
                message += f"For {label} {shown}, {instruction} "
        message += "Use codex_resume with a new request_id to re-check native state."
        candidates = set()
        for family_id in {c["family_id"] for c in claims} - {r["family_id"] for r in admissions}:
            group = [c for c in claims if c["family_id"] == family_id]
            tasks = {c["task_id"]: self.store.task_head(c["task_id"]) for c in group}
            candidates.update(c["task_id"] for c in group if workspace_processes.held(c))
            if any(processes.pending(task) for task in tasks.values()):
                continue
            if any(t in self.protocol_quarantine or any(key[0] == t for key in self.intent_inflight) for t in tasks):
                continue
            if any(c["kind"] == "turn" and c["data"].get("start_token")
                   and c["data"]["start_token"] == tasks[c["task_id"]].get("turn_start") for c in group):
                continue
            recovery = any(c["data"].get("reason") in RECOVERY_REASONS for c in group)
            if recovery or all(c["kind"] == "background" or (
                    c["state"] == "uncertain" and c["data"].get("reason") in ROUTINE_REASONS) for c in group):
                candidates.update([family_id, *tasks])
        failure = WorkspaceBusy(message, sorted(candidates), blocking_claims=claims, pending_admissions=bool(admissions), admissions=admissions)
        for task_id in sorted({c["task_id"] for c in claims if c["data"].get("reason") == "stored_state_unavailable"})[:8]:
            owner = self.store.task_head(task_id)
            failure.next_steps.append(f"codex_delete task_id={task_id} confirm_thread_id={owner['thread_id']} with a new operation_id to free the project")
        if any(c["data"].get("reason") == processes.REASON for c in claims):
            failure.next_steps.append(processes.MESSAGE)
        for task_id in sorted(archived)[:8]:
            failure.next_steps.append(processes.archived_guidance(self.store.task_head(task_id)))
        return failure

    def admission(self):
        key = self.current_operation.get()
        return self.store.db.execute("SELECT * FROM admissions WHERE operation_id=?", (key,)).fetchone() if key else None

    async def reconcile_startup(self):
        for task_id in self.store.claim_task_ids():
            try:
                task = self.task(task_id, allow_deleted=True, light=True)
                if not task.get("thread_id"):
                    for claim in self.store.claims(task_id=task_id):
                        self.store.claim(task_id, claim["kind"], "inactive", expected_revision=claim["revision"])
                    continue
                await self.ensure_backend()
                await self.reconcile_claims(task_id, explicit=True)
            except (BridgeError, OSError, TimeoutError, KeyError):
                continue  # Unknown native state keeps ownership; no prompt is replayed.

    def schedule_reconciliation(self, task_id, *, explicit=False):
        if self.shutting_down:
            return
        if explicit:
            self.reset_reconciliation(task_id)
            self.reconciliation_fresh.add(task_id)
        self.reconciliation_dirty.add(task_id)
        timer = self.reconciliation_timers.pop(task_id, None)
        if timer:
            timer.cancel()
        existing = self.reconciliation_by_task.get(task_id)
        if existing and not existing.done():
            return existing
        async def reconcile():
            self.current_operation.set(None)
            self.process_acknowledgement.set(None)
            for _ in range(3):
                self.reconciliation_dirty.discard(task_id)
                revision = self.store.task_head(task_id)["revision"]
                sequence = self.reconciliation_sequence(task_id)
                fresh = task_id in self.reconciliation_fresh
                self.reconciliation_fresh.discard(task_id)
                await self.reconcile_claims(task_id, explicit=fresh)
                failed = self.reconciliation_failures.get(task_id)
                if self.store.task_head(task_id)["revision"] != revision or (failed and failed.get("source") != workspace_processes.SOURCE and failed["observed_sequence"] != sequence):
                    self.reset_reconciliation(task_id)
                    self.reconciliation_dirty.add(task_id)
                if task_id not in self.reconciliation_fresh and (task_id in self.reconciliation_failures or task_id not in self.reconciliation_dirty):
                    break
        pending = asyncio.create_task(reconcile())
        self.reconciliation_tasks.add(pending)
        self.reconciliation_by_task[task_id] = pending
        def finished(task):
            self.reconciliation_tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error is not None and not self.shutting_down:
                    self.reconciliation_failed(task_id, "reconciliation_failed", error=error)
            if self.reconciliation_by_task.get(task_id) is not task:
                return
            self.reconciliation_by_task.pop(task_id, None)
            if self.shutting_down:
                return
            if task_id in self.reconciliation_fresh or (task_id in self.reconciliation_dirty and task_id not in self.reconciliation_failures):
                self.schedule_reconciliation(task_id, explicit=task_id in self.reconciliation_fresh)
            elif self.watch_claims(task_id):
                self.reconciliation_dirty.discard(task_id)
                failed = self.reconciliation_failures.get(task_id)
                self.reconciliation_timers[task_id] = self.reconciliation_timer(task_id, failed["delay"] if failed else RECONCILE_INTERVAL)
        pending.add_done_callback(finished)
        return pending

    def reconciliation_timer(self, task_id, seconds):
        return asyncio.get_running_loop().call_later(seconds, self.periodic_reconciliation, task_id)

    async def reconciliation_idle(self):
        """Drain scheduled checks, excluding future polls and native events."""
        while self.reconciliation_tasks:
            pending = list(self.reconciliation_tasks)
            # wait() lets done callbacks run even when every check completed
            # synchronously. gather() may return immediately on Python 3.14.
            await asyncio.wait(pending)
            for task in pending:
                task.result()

    def reconciliation_methods(self, task):
        methods = {"thread/goal/get", "thread/queue/list"}
        if self.native_observations.get(task["id"], {}).get("thread_status") != "notLoaded":
            methods.add("thread/backgroundTerminals/list")
        if not task.get("parent_task_id"):
            methods.add("thread/list")
        return methods

    def periodic_reconciliation(self, task_id):
        self.reconciliation_timers.pop(task_id, None)
        if self.watch_claims(task_id):
            self.schedule_reconciliation(task_id)

    def watch_claims(self, task_id):
        task = self.store.task_head(task_id)
        claims = self.store.claims(task_id=task_id)
        if not task.get("deleted") and any(workspace_processes.held(c) for c in claims):
            return True  # This observation does not require a native connection.
        if not self.backend.alive or task_id in self.protocol_quarantine:
            return False
        if processes.pending(task):
            return False
        if self.reconciliation_methods(task) & self.backend.unavailable_methods:
            return False
        if task.get("deleted") or task.get("turn_start"):
            return False
        return any(c["kind"] != "turn" or task.get("turn_status") in TERMINAL for c in claims)

    async def reconcile_process_hold(self, task_id, *, remember=True):
        task = self.store.task_head(task_id)
        family = task.get("root_task_id", task_id)
        snapshot = self.workspace_snapshot(family)
        result = await self.scan_workspace(task["project"])
        if self.workspace_snapshot(family) != snapshot or task.get("deleted"):
            self.schedule_reconciliation(task_id)
            return
        self.os_process_observations[task["project"]] = result
        # Clear only scan-based claims. An unavailable scan cannot clear even
        # those; native claims require their own independent observations.
        previous = self.reconciliation_failures.pop(task_id, None)
        with self.store.transaction():
            self.apply_release_scan(task_id, result)
        if previous:
            self.reconciliation_failures[task_id] = previous
        if not any(workspace_processes.held(c) for c in self.store.claims(task_id=task_id)):
            self.reconciliation_failures.pop(task_id, None)
        if remember:
            self.remember_process_check(task_id)

    def reconciliation_failed(self, task_id, reason, *, expected_revision=None, error=None):
        task = self.store.task_head(task_id)
        claims = self.store.claims(task_id=task_id)
        if task.get("deleted") or task_id in self.protocol_quarantine or not claims:
            return
        if all(workspace_processes.held(c) for c in claims):
            # A delayed/failed OS read cannot invalidate already proven native
            # inactivity or create a new native-uncertainty reservation.
            self.os_process_observations[task["project"]] = {"available": False, "reason": "proc_unreadable",
                "processes": [], "truncated": False, "scanned": 0}
            self.remember_process_check(task_id)
            return
        if processes.pending(task) and self.process_acknowledgement.get() != task_id:
            processes.hold(self, task_id)
            return
        data = {"reason": reason}
        if error is not None:
            data["error"] = redact(str(error))[:512]
        if isinstance(error, OptionalCapabilityUnavailable):
            data.update(error_code=error.code, unavailable_method=error.method)
            data["error_data"] = {**error.as_dict(), "ids": {**error.ids, "task_id": task_id}}
        previous = self.reconciliation_failures.pop(task_id, {})
        sequence = self.reconciliation_sequence(task_id)
        signature = json.dumps(data, sort_keys=True)
        same = (previous.get("generation") == self.backend.generation
            and previous.get("sequence") == sequence and previous.get("signature") == signature)
        applied = self.store.claim(task_id, "uncertain", "uncertain", data, expected_revision=expected_revision, if_changed=True)
        if applied:
            self.reconciliation_failures[task_id] = {"generation": self.backend.generation, "sequence": self.reconciliation_sequence(task_id),
                "observed_sequence": sequence, "signature": signature,
                "delay": min(previous["delay"] * 2, RECONCILE_MAX_INTERVAL) if same else RECONCILE_INTERVAL}
        elif previous:
            self.reconciliation_failures[task_id] = previous

    async def wait_for_reconciliation(self, task_ids):
        async def wait():
            scanned = set()
            if not self.backend.alive:
                scanned = {key for key in task_ids if any(workspace_processes.held(c) for c in self.store.claims(task_id=key))}
                checks = [self.schedule_reconciliation(key, explicit=True) for key in scanned]
                await asyncio.gather(*(asyncio.shield(check) for check in checks if check is not None))
            native_needed = any(not workspace_processes.held(c) for task_id in task_ids
                if not processes.pending(self.store.task_head(task_id)) for c in self.store.claims(task_id=task_id))
            if not self.backend.alive and native_needed:
                starter = asyncio.create_task(self.ensure_backend())
                self.reconciliation_tasks.add(starter)
                starter.add_done_callback(lambda task: self._finish_reconciliation_start(task))
                await asyncio.shield(starter)
            pending = [self.schedule_reconciliation(task_id, explicit=True) for task_id in task_ids
                if native_needed or task_id not in scanned and self.store.claims(task_id=task_id)]
            while any(task is not None for task in pending):
                if self.shutting_down:
                    raise BridgeError("Broker is stopping; reconnect before starting another operation", code='state_conflict')
                await asyncio.gather(*(asyncio.shield(task) for task in pending if task is not None))
                pending = []
                for task_id in task_ids:
                    current = self.reconciliation_by_task.get(task_id)
                    if current is not None and not current.done():
                        pending.append(current)
                    elif task_id in self.reconciliation_dirty:
                        pending.append(self.schedule_reconciliation(task_id))
        try:
            await asyncio.wait_for(wait(), ADMISSION_RECONCILE_SECONDS)
        except (BridgeError, OSError, TimeoutError) as exc:
            for task_id in task_ids:
                self.reconciliation_failed(task_id, "reconciliation_failed",
                    error=str(exc) or f"Native check did not finish within {ADMISSION_RECONCILE_SECONDS} seconds")
                self.schedule_reconciliation(task_id)
            # A timeout ends admission's wait, never releases a claim.

    def _finish_reconciliation_start(self, task):
        self.reconciliation_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def native_pages(self, method, thread_id):
        values, cursor = [], None
        for _ in range(100):
            identity = {"threadId": thread_id} if method != "thread/list" else {
                "ancestorThreadId": thread_id, "archived": False,
                "sourceKinds": ["subAgent", "subAgentThreadSpawn", "subAgentReview", "subAgentCompact", "subAgentOther"]}
            page = await self.backend.call(method, {**identity, "cursor": cursor, "limit": 200})
            if not isinstance(page.get("data"), list):
                raise BridgeError("Native intent state is incomplete; workspace claims retained", code='state_conflict')
            values.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return values
        raise BridgeError("Native intent pagination did not terminate; workspace claims retained", code='state_conflict')

    async def latest_turn(self, task_id):
        task = self.task(task_id, allow_deleted=True, light=True)
        generation, epoch = self.backend.generation, self.protocol_epochs.get(task_id, 0)
        def root_evidence(turn_id):
            current = self.store.task_head(task_id)
            if (not self.backend.alive or self.backend.generation != generation
                    or current["revision"] != task["revision"] or task_id in self.protocol_quarantine
                    or self.protocol_epochs.get(task_id, 0) != epoch):
                return None
            return native_turns.terminal_evidence(self.store, current, turn_id)
        scope = native_turns.for_task(self.store, task_id)
        turn = await native_turns.latest(self.backend, task["thread_id"], scope=scope, root_evidence=root_evidence)
        review = self.inline_reviews.get(task_id)
        candidate = (review or {}).get("deferred", {}).get((turn or {}).get("id"))
        if (candidate and review["turn_id"] is None and not scope.owns(candidate)
                and self.process_acknowledgement.get() != task_id):
            raise BridgeError("Inline review turn identity is unresolved; use codex_resume, then codex_history", code="state_conflict", uncertain=True)
        return turn

    async def reconcile_claims(self, task_id, *, explicit=False, check_only=False, recovery=False, _attempt=0):
        with self.track_background(self.reconciliation_calls, task_id):
            return await self._reconcile_claims(task_id, explicit=explicit, check_only=check_only, recovery=recovery, _attempt=_attempt)

    async def _reconcile_claims(self, task_id, *, explicit=False, check_only=False, recovery=False, _attempt=0):
        if explicit and not _attempt:
            self.reset_reconciliation(task_id)
        claims = self.store.claims(task_id=task_id)
        os_hold = any(workspace_processes.held(c) for c in claims)
        if os_hold and not recovery and not check_only:
            await self.reconcile_process_hold(task_id, remember=False)
            remaining = self.store.claims(task_id=task_id)
            if (all(workspace_processes.held(c) for c in remaining) or not remaining
                    or processes.pending(self.store.task_head(task_id)) or task_id in self.protocol_quarantine or not self.backend.alive):
                self.remember_process_check(task_id)
                return self.native_observations.get(task_id)
        self.native_observations.pop(task_id, None)
        if task_id in self.protocol_quarantine:
            return
        task = self.task(task_id, allow_deleted=True, light=True)
        protocol_epoch = self.protocol_epochs.get(task_id, 0)
        if task.get("deleted"):
            for claim in self.store.claims(task_id=task_id):
                self.store.claim(task_id, claim["kind"], "inactive", expected_revision=claim["revision"])
            return
        if processes.pending(task) and self.process_acknowledgement.get() != task_id and not check_only:
            processes.hold(self, task_id)
            return
        if task.get("deleted") or not task.get("thread_id"):
            if not self.store.db.execute("SELECT 1 FROM operations WHERE task_id=? AND state='running'", (task_id,)).fetchone():
                for claim in self.store.claims(task_id=task_id):
                    self.store.claim(task_id, claim["kind"], "inactive", expected_revision=claim["revision"])
            return
        snapshot = {c["kind"]: c["revision"] for c in self.store.claims(task_id=task_id, include_inactive=True)}
        revision = task["revision"]
        generation = self.backend.generation
        reading_thread = True
        try:
            required = self.reconciliation_methods(task)
            if task_id not in self.loaded or task.get("verified_generation") != generation:
                required.discard("thread/backgroundTerminals/list")
            unavailable = required & self.backend.unavailable_methods
            if unavailable:
                raise OptionalCapabilityUnavailable(sorted(unavailable)[0], self.backend.version, {})
            native = (await self.backend.call("thread/read", {"threadId": task["thread_id"], "includeTurns": False}))["thread"]
            reading_thread = False
            not_loaded = native.get("status", {}).get("type") == "notLoaded"
            methods = self.reconciliation_methods(task) - ({"thread/backgroundTerminals/list"} if not_loaded else set())
            unavailable = methods & self.backend.unavailable_methods
            if unavailable:
                raise OptionalCapabilityUnavailable(sorted(unavailable)[0], self.backend.version, {})
            goal = await self.backend.call("thread/goal/get", {"threadId": task["thread_id"]})
            if (not check_only and self.backend.alive and generation == self.backend.generation and isinstance(goal, dict)
                    and isinstance(goal.get("goal"), dict) and goal["goal"].get("status") == "active"):
                if self.observe_goal(task_id, goal, snapshot.get("goal", 0)):
                    snapshot["goal"] = self.claim_snapshot(task_id, "goal")["revision"]
            queue = await self.native_pages("thread/queue/list", task["thread_id"])
            turn = await self.latest_turn(task_id)
            # Inspect processes after the terminal history fact, so a command
            # detached while ending the turn cannot hide behind an older list.
            terminal_token = self.terminal_token()
            terminals = [] if not_loaded else await self.native_pages("thread/backgroundTerminals/list", task["thread_id"])
            if not not_loaded and not check_only:
                if processes.observe_terminals(self, task_id, terminals, generation, terminal_token, complete=True):
                    snapshot["background"] = self.claim_snapshot(task_id, "background")["revision"]
            if not task.get("parent_task_id") and not check_only:
                for thread in await self.native_pages("thread/list", task["thread_id"]):
                    if thread.get("status", {}).get("type") == "notLoaded":
                        child = self.task_for_thread(thread["id"])
                        if child and child.get("root_task_id") == task["id"]:
                            if explicit or child["id"] not in self.reconciliation_failures or child["id"] not in self.reconciliation_timers:
                                await self.reconcile_claims(child["id"], explicit=explicit)
                        elif not await self.stored_descendant_inactive(thread, task["project"]):
                            raise BridgeError("Stored descendant execution is not proven inactive; resume the family and inspect its history", code="state_conflict")
                        continue
                    child = await self.adopt_message_thread({"thread": thread})
                    if child is None or child.get("root_task_id") != task["id"]:
                        raise BridgeError("Native descendant ownership is unknown; workspace claims retained", code='state_conflict')
                    if explicit or child["id"] not in self.reconciliation_failures or child["id"] not in self.reconciliation_timers:
                        await self.reconcile_claims(child["id"], explicit=explicit)
            unavailable = methods & self.backend.unavailable_methods
            if unavailable:
                raise OptionalCapabilityUnavailable(sorted(unavailable)[0], self.backend.version, {})
            final = (await self.backend.call("thread/read", {"threadId": task["thread_id"], "includeTurns": False}))["thread"]
            if (final.get("id") != native.get("id") or final.get("cwd") != native.get("cwd")
                    or final.get("status", {}).get("type") != native.get("status", {}).get("type")):
                self.reconciliation_failed(task_id, "native_state_changed", expected_revision=snapshot.get("uncertain", 0),
                    error="Native thread activity changed during the check; workspace ownership was retained")
                if explicit and not recovery and _attempt < 2:
                    return await self.reconcile_claims(task_id, explicit=True, check_only=check_only, _attempt=_attempt + 1)
                self.schedule_reconciliation(task_id)
                return
        except (BridgeError, OSError, TimeoutError, KeyError) as exc:
            reason = "reconciliation_failed"
            if isinstance(exc, RpcError) and not isinstance(exc, OptionalCapabilityUnavailable):
                reason = "stored_state_unavailable" if reading_thread else "native_state_unavailable"
                exc = "Stored conversation could not be read; workspace ownership was retained" if reading_thread else "Native workspace state could not be read; inspect history and re-check with codex_resume"
            self.reconciliation_failed(task_id, reason, error=exc,
                                       expected_revision=snapshot.get("uncertain", 0))
            return
        if (not self.backend.alive or generation != self.backend.generation
                or task_id in self.protocol_quarantine or self.protocol_epochs.get(task_id, 0) != protocol_epoch
                or self.task(task_id, allow_deleted=True, light=True)["revision"] != revision):
            if self.backend.alive and not self.shutting_down:
                self.reset_reconciliation(task_id)
                if explicit and not recovery and _attempt < 2:
                    return await self.reconcile_claims(task_id, explicit=True, check_only=check_only, _attempt=_attempt + 1)
                self.schedule_reconciliation(task_id)
            return
        if (not isinstance(native, dict) or not isinstance(native.get("status"), dict)
                or not isinstance(goal, dict) or any(not isinstance(t, dict) for t in terminals)):
            self.reconciliation_failed(task_id, "unknown_native_intent", expected_revision=snapshot.get("uncertain", 0))
            return
        native_status = native.get("status", {}).get("type")
        goal_value = goal.get("goal")
        goal_status = goal_value.get("status") if isinstance(goal_value, dict) else None
        if (native_status not in ("active", "idle", "notLoaded") or "goal" not in goal
                or native.get("id") != task["thread_id"]
                or (task.get("turn_id") is not None and not turn)
                or (turn is not None and (not isinstance(turn, dict) or not turn.get("id")
                    or not isinstance(turn.get("status"), str) or turn["status"] not in TERMINAL | {"inProgress"}))
                or not native.get("cwd") or not same_path(native["cwd"], task.get("native_cwd", task["project"]))
                or (goal_value is not None and goal_status not in ("active", "paused", "blocked", "complete"))):
            self.reconciliation_failed(task_id, "unknown_native_intent", expected_revision=snapshot.get("uncertain", 0))
            return
        running = bool(turn and turn.get("status") == "inProgress")
        # The native list has no portable completion-status field.
        background = bool(terminals)
        # A consumed queue entry can be between removal and turn/started. An
        # active thread with no current running turn remains a background claim.
        active_thread = native_status == "active" and not running
        states = {"goal": bool(goal_value and goal_status == "active"), "queue": bool(queue),
                  "turn": running, "background": background or active_thread,
                  "uncertain": not_loaded and (not turn or turn["status"] not in TERMINAL)}
        checked = not not_loaded and not processes.relevant(self.store.task_head(task_id))
        observation = {"generation": generation, "observed_at": time.time(),
            "thread_status": native_status, "latest_turn": {"id": turn["id"], "status": turn["status"],
                **({"source": "journal", "event_seq": turn["_source_event_seq"]} if "_source_event_seq" in turn else {})} if turn else None,
            "goal_status": goal_status, "queue": [{k: q.get(k) for k in ("id", "queuedSubmissionId", "clientUserMessageId")} for q in queue[:200]],
            "queue_total": len(queue), "background_terminals": len(terminals) if checked else None,
            "background_terminals_checked": checked, "current_backend_background_terminals": None if not_loaded else len(terminals)}
        if check_only:
            return observation
        scanned, release_snapshot = await self.prepare_release_scan(task_id, states, force=recovery)
        if scanned is False:
            self.schedule_reconciliation(task_id)
            return
        # No await may separate this check from the field/claim transaction.
        if self.workspace_snapshot(task.get("root_task_id", task_id)) != release_snapshot:
            self.schedule_reconciliation(task_id)
            return
        conflicted = False
        try:
            with self.store.transaction():
                for kind, active in states.items():
                    current = self.claim_snapshot(task_id, kind)
                    if kind == "background" and not active and current and workspace_processes.held(current):
                        continue
                    if kind == "uncertain" and active:
                        continue
                    operation_id = (current or {}).get("data", {}).get("operation_id")
                    if (task_id, kind) in self.intent_inflight:
                        conflicted |= recovery
                        continue
                    if operation_id in self.operation_tasks and operation_id != self.current_operation.get():
                        conflicted |= recovery
                        continue
                    if (kind == "turn" and current and current["data"].get("start_token")
                            and not explicit and not running
                            and current["data"]["start_token"] == task.get("turn_start")
                            and (not turn or turn["id"] == task.get("turn_id"))):
                        continue
                    data = {"source": "native_reconciliation"}
                    if kind == "goal":
                        data["status"] = goal_status
                    if kind == "background" and terminals:
                        commands, truncated = processes.preview([entry.get("command") for entry in terminals])
                        data = {"source": "native_terminal_observation", "commands_preview": commands, "commands_truncated": truncated}
                    if kind == "turn" and turn:
                        data["turn_id"] = turn["id"]
                    if not self.store.claim(task_id, kind, "active" if active else "inactive", data,
                                            expected_revision=snapshot.get(kind, 0), if_changed=True):
                        conflicted = True
                        self.schedule_reconciliation(task_id)
                if recovery:
                    if conflicted or states["uncertain"]:
                        raise _RecoveryConflict()
                    current = self.task(task_id, light=True)
                    self.assert_writable_session(current, allow_recovery=True)
                    newer_terminal = (turn and current.get("turn_id") == turn["id"]
                        and current.get("turn_status") in TERMINAL and turn["status"] == "inProgress")
                    if newer_terminal:
                        self.lifecycle.gap(task_id, "history_mismatch")
                    if current["status"] in ("needs_resume", "uncertain", "starting") or (turn and
                            (current.get("turn_id") != turn["id"] or current.get("turn_status") != turn["status"]) and not newer_terminal):
                        current = self.task(task_id, light=True)
                        recovered_turn = {**turn, "status": current["turn_status"]} if newer_terminal else turn
                        current = self.lifecycle.recover(task_id, recovered_turn, native_status == "active", expected_revision=current["revision"])
                    if (turn and (current.get("turn_id") != turn["id"] or (current.get("turn_status") != turn["status"] and not newer_terminal))) or current["status"] == "needs_resume":
                        raise _RecoveryConflict()
                    if queue and not running and current["status"] != "queued":
                        self.lifecycle.queued(task_id, {"status": "queued"})
                    fields = {"verified_generation": generation}
                    if any(current.get(key) != value for key, value in fields.items()):
                        self.lifecycle.metadata(task_id, fields)
                    processes.acknowledge(self, task_id)
                self.apply_release_scan(task_id, scanned)
                if not conflicted:
                    self.resources.reconcile(task_id, turn, idle=not running and native_status != "active" and not states["goal"], queue=queue, goal_active=states["goal"])
        except _RecoveryConflict:
            return None
        if conflicted and explicit and not recovery and _attempt < 2:
            return await self.reconcile_claims(task_id, explicit=True, _attempt=_attempt + 1)
        if states["uncertain"]:
            self.reconciliation_failed(task_id, "conversation_not_loaded",
                error="Stored latest turn is not terminal; resume and inspect history before more work")
        else:
            if self.reconciliation_failures.get(task_id, {}).get("source") != workspace_processes.SOURCE:
                self.reconciliation_failures.pop(task_id, None)
        self.remember_process_check(task_id)
        if not self.watch_claims(task_id):
            timer = self.reconciliation_timers.pop(task_id, None)
            if timer:
                timer.cancel()

        self.native_observations[task_id] = observation
        if not conflicted:
            self.schedule_budget_check(task_id)
        return self.native_observations[task_id]

    async def stored_descendant_inactive(self, thread, project):
        """Inspect an unadopted stored child without joining or resuming it."""
        native = (await self.backend.call("thread/read", {"threadId": thread["id"], "includeTurns": False}))["thread"]
        if (native.get("id") != thread["id"] or native.get("status", {}).get("type") != "notLoaded"
                or not native.get("cwd") or not within(Path(native["cwd"]).resolve(), Path(project))):
            return False
        owner = self.task_for_thread(thread["id"])
        scope = native_turns.for_task(self.store, owner["id"]) if owner else None
        latest = await native_turns.latest(self.backend, thread["id"], scope=scope)
        goal = await self.backend.call("thread/goal/get", {"threadId": thread["id"]})
        queue = await self.native_pages("thread/queue/list", thread["id"])
        value = goal.get("goal")
        return bool(latest and latest.get("status") in TERMINAL and "goal" in goal
            and (value is None or value.get("status") in ("paused", "blocked", "complete")) and not queue)

    def intent_claim(self, task_id, kind, *, enabling=True):
        if enabling:
            self.capacity(self.task(task_id, light=True)["project"], exclude=task_id)
        previous = self.claim_snapshot(task_id, kind)
        state = "uncertain" if enabling else previous["state"] if previous else "inactive"
        self.store.claim(task_id, kind, state, {"operation_id": self.current_operation.get(), "pending": True})
        revision = self.claim_snapshot(task_id, kind)["revision"]
        self.intent_inflight[(task_id, kind)] = revision
        return revision

    def observe_goal(self, task_id, result, revision):
        if not isinstance(result, dict):
            return
        if task_id in self.protocol_quarantine or self.store.task_head(task_id).get("deleted"):
            return
        value = result.get("goal")
        status = value.get("status") if isinstance(value, dict) else None
        if "goal" not in result or (value is not None and status not in ("active", "paused", "blocked", "complete")):
            return
        if (task_id, "goal") not in self.intent_inflight:
            applied = self.store.claim(task_id, "goal", "active" if status == "active" else "inactive",
                                       {"source": "native_reconciliation", "status": status}, expected_revision=revision, if_changed=True)
            if applied:
                self.resources.goal(task_id, value)
            return applied

    def finish_intent(self, task_id, kind, revision):
        if self.intent_inflight.get((task_id, kind)) == revision:
            self.intent_inflight.pop((task_id, kind))
            self.schedule_reconciliation(task_id)

    def mark_native_intent(self, task_id, kind, reason):
        task = self.store.task_head(task_id)
        if task.get("deleted"):
            return
        current = self.claim_snapshot(task_id, kind)
        if current is None or current["state"] == "inactive":
            self.store.claim(task_id, kind, "uncertain", {"reason": reason})

    def claim_snapshot(self, task_id, kind):
        return next((claim for claim in self.store.claims(task_id=task_id, include_inactive=True)
                     if claim["kind"] == kind), None)

    def restore_rejected_claim(self, task_id, kind, revision, previous):
        # Method-unavailable is a definitive rejection. Restore only our own
        # pending claim; a later native observation must never be overwritten.
        self.store.claim(task_id, kind, previous["state"] if previous else "inactive",
                         previous["data"] if previous else {}, expected_revision=revision)

    def record_deletion(self, task_id, *, requested=False, native_conversation=None, process_check=None, deleting=()):
        with self.store.transaction():
            before = self.store.task_head(task_id)
            family = before.get("root_task_id", task_id)
            removing = set(deleting) | {task_id}
            survivors = [t for _, t in self.store.records("tasks", family_id=family, light=True)
                if not t.get("deleted") and t["id"] not in removing]
            own_claim = self.claim_snapshot(task_id, "background")
            if own_claim and own_claim["state"] != "inactive" and workspace_processes.held(own_claim) and survivors:
                target = survivors[0]["id"]
                # A stronger native claim need not be replaced by this hold.
                other = self.claim_snapshot(target, "background")
                if not other or other["state"] == "inactive" or workspace_processes.held(other):
                    self.store.claim(target, "background", own_claim["state"], own_claim["data"], if_changed=True)
            if process_check is False and survivors:
                if not self.store.claims(family_id=family) or all(c["task_id"] in removing for c in self.store.claims(family_id=family)):
                    self.store.claim(survivors[0]["id"], "background", "uncertain", {"reason": "deletion_process_check"}, if_changed=True)
            elif process_check:
                self.apply_release_scan(task_id, process_check, deleting=removing)
            task = self.lifecycle.archive(task_id, "delete", requested=requested)
            native_conversation = native_conversation or task.get("native_conversation") or "deleted"
            if task.get("native_conversation") != native_conversation:
                task = self.lifecycle.metadata(task_id, {"native_conversation": native_conversation})
            for _, req in self.store.records("requests", task_id=task_id, statuses=("pending", "answering"), light=True):
                self.store.update("requests", req["id"], {"status": "expired"})
                self.raw_requests.pop(req["id"], None)
            self.loaded.discard(task_id)
            for key in list(self.file_proposals):
                if key[1] == task["thread_id"]:
                    self.file_proposals.pop(key, None)
            if task["revision"] != before["revision"]:
                self.store.event(task_id, "bridge/deleted", {"audit_retained": True,
                    "native_conversation": native_conversation, "process_state": self.process_view(task)})
            if requested and hasattr(self, "scheduler"):
                self.scheduler.confirmed_delete(task_id)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass  # Synchronous recovery schedules checks at backend start.
            else:
                for claim in self.store.claims(family_id=family):
                    if claim["task_id"] not in removing and (workspace_processes.held(claim)
                            or claim["data"].get("reason") == "deletion_process_check"):
                        # Defer until the enclosing SQLite transaction commits,
                        # including when an eager task factory is installed.
                        loop.call_soon(self.schedule_reconciliation, claim["task_id"])
            return task

    def _admit(self, key, name, args):
        exclude = args.get("task_id")
        if args.get("task_id"):
            task = self.task(args["task_id"], allow_deleted=name == "codex_delete", light=True)
            family, project = task.get("root_task_id", task["id"]), task["project"]
        elif name in ("codex_start", "codex_sessions"):
            descriptor = self.select_project(args.get("workdir"), args.get("project_id"), register=False)
            family, project = str(uuid.uuid4()), descriptor["path"]
            known = self.task_for_thread(args.get("thread_id")) if name == "codex_sessions" else None
            if known:
                self.task(known["id"], light=True)
                family, exclude = known.get("root_task_id", known["id"]), known["id"]
        else:
            return key
        begins = name in ("codex_start", "codex_resume", "codex_review", "codex_compact", "codex_delegate") or (
            name == "codex_send" and args.get("mode", "follow_up") == "follow_up") or (
            name == "codex_queue" and args.get("action") in ("add", "start")) or (
            name == "codex_sessions" and not args.get("archived")) or (
            name == "codex_archive" and args.get("action") == "unarchive") or (
            name == "codex_goal" and args.get("action") == "set" and args.get("status", "active") == "active")
        if begins:
            # No native I/O or await is permitted between this check and insert.
            self.capacity(project, exclude=exclude)
        self.resources.admit(key, family, name, args, self.config.budget or {})
        if begins:
            self.store.db.execute("INSERT INTO admissions VALUES (?,?,?)", (key, family, project))
        return family

    def schedule_budget_check(self, task_id):
        family = self.resources.family(task_id)
        budget, stopped = self.resources.policy(family)
        self.refresh_budget_timer(family, budget, stopped)
        if not any(k in budget for k in ("max_active_seconds", "max_tokens", "max_output_bytes")) or self.shutting_down:
            return
        current = self.budget_tasks.get(family)
        if current and not current.done():
            self.budget_dirty.add(family)
            return current
        task = asyncio.create_task(self.check_budget(family))
        self.budget_tasks[family] = task
        task.add_done_callback(lambda job: self.finish_budget_check(family, job))
        return task

    def budget_later(self, family, seconds, token):
        return asyncio.get_running_loop().call_later(seconds, self.budget_expired, family, token)

    def cancel_budget_timer(self, family):
        previous = self.budget_timers.pop(family, None)
        if previous:
            previous[2].cancel()

    def cancel_budget_timers(self):
        for family in list(self.budget_timers):
            self.cancel_budget_timer(family)

    def refresh_budget_timer(self, family, budget=None, stopped=None):
        if budget is None:
            budget, stopped = self.resources.policy(family)
        deadline = self.resources.deadline(family, budget["max_active_seconds"]) if (
            not self.shutting_down and "max_active_seconds" in budget and not (stopped and stopped["active"])) else None
        previous = self.budget_timers.get(family)
        if deadline is None or deadline <= self.resources.clock():
            self.cancel_budget_timer(family)
        elif not previous or abs(previous[0] - deadline) > 0.000001:
            self.cancel_budget_timer(family)
            token = object()
            handle = self.budget_later(family, max(0, deadline - self.resources.clock()), token)
            self.budget_timers[family] = (deadline, token, handle)

    def budget_expired(self, family, token):
        current = self.budget_timers.get(family)
        if not current or current[1] is not token:
            return
        self.budget_timers.pop(family, None)
        if not self.shutting_down:
            self.schedule_budget_check(family)

    def finish_budget_check(self, family, job):
        self._finish_task(self.budget_tasks, family, job)
        if family in self.budget_dirty:
            self.budget_dirty.discard(family)
            self.schedule_budget_check(family)

    async def check_budget(self, family):
        operation = self.current_operation.set(None)
        context = self.operation_context.set(None)
        try:
            with self.store.transaction():
                budget, stopped = self.resources.policy(family)
                crossed = self.resources.breach(self.resources.usage(family), budget)
                if not stopped or not stopped["active"]:
                    if not crossed:
                        return
                    stopped = self.resources.stop(family, crossed)
            for _, task in self.store.records("tasks", family_id=family, light=True):
                key = task["id"]
                if task["status"] not in ("running", "waiting", "interrupting") or not task.get("turn_id"):
                    continue
                turn_id = task["turn_id"]
                with self.store.transaction():
                    sent = self.store.db.execute("INSERT OR IGNORE INTO resource_interrupts VALUES (?,?,?,'requested')", (family, key, turn_id))
                if not sent.rowcount:
                    continue
                try:
                    await self.interrupt(key, turn_id)
                except (BridgeError, OSError, TimeoutError):
                    state = "unknown"
                else:
                    state = "acknowledged"
                with self.store.transaction():
                    self.store.db.execute("UPDATE resource_interrupts SET state=? WHERE family_id=? AND task_id=? AND turn_id=?", (state, family, key, turn_id))
            await self.wake()
        finally:
            self.refresh_budget_timer(family)
            self.operation_context.reset(context)
            self.current_operation.reset(operation)

    def _finish_task(self, mapping, key, task):
        if mapping.get(key) is task:
            mapping.pop(key, None)
        if not task.cancelled():
            task.exception()  # A disconnected caller may no longer retrieve it.
        if mapping is self.operation_tasks and not self.shutting_down:
            row = self.store.db.execute("SELECT o.task_id,d.name FROM operations o LEFT JOIN operation_details d ON d.id=o.id WHERE o.id=?", (key,)).fetchone()
            self.scheduler.schedule(retry=not key.startswith("plan:") and (not row or row["name"] != "codex_plans"))
            if row and row[0]:
                self.schedule_reconciliation(row[0])

    def _previous_operation(self, row):
        previous = json.loads(row["result"]) if row["result"] else {}
        data = previous.get("error_data")
        if data and data.get("code") == OptionalCapabilityUnavailable.code:
            return OptionalCapabilityUnavailable(data["method"], data.get("codex_version"), {})
        error = previous.get("error")
        detail = f" (it failed with: {error})" if error else ""
        target = f"task {row['task_id']}" if row["task_id"] else "codex_list"
        message = f"Previous operation with this request_id is {row['state']}{detail}. Inspect {target} and its events, then use a new request_id if more work is needed"
        if data and data.get("code") in BridgeError.CODES:
            return BridgeError.from_dict({**data, "message": message})
        return BridgeError(message, code="operation_uncertain",
                          ids={"operation_id": row["id"], "task_id": row["task_id"]})

    @staticmethod
    def operation_fingerprint(name, args):
        return hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()

    async def operation(self, key, name, args, function, *, legacy_args=None):
        fingerprint = self.operation_fingerprint(name, args)
        spawn = False
        async with self.admission_lock:
            expired = self.store.db.execute("SELECT task_id FROM operation_tombstones WHERE id=?", (key,)).fetchone()
            if expired:
                raise BridgeError("Operation key has expired; its result was pruned and the key cannot be executed again", code="operation_expired",
                                  ids={"operation_id": key, "task_id": expired["task_id"]})
            row = self.store.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
            if row:
                legacy = self.operation_fingerprint(name, legacy_args) if legacy_args is not None else fingerprint
                if row["fingerprint"] not in (fingerprint, legacy):
                    raise BridgeError("request_id was already used with different arguments", code='operation_conflict')
                if row["state"] == "done":
                    result = json.loads(row["result"])
                    if result.get("operation_rejected") is True:
                        data = result["error_data"]
                        if data["code"] == OptionalCapabilityUnavailable.code:
                            raise OptionalCapabilityUnavailable(data["method"], data.get("codex_version"), {})
                        raise BridgeError.from_dict(result["error_data"])
                    return decisions.task_view(result)
                running = self.operation_tasks.get(key)
                if row["state"] != "running" or running is None:
                    raise self._previous_operation(row)
            else:
                if self.shutting_down:
                    raise BridgeError("Broker is stopping; reconnect before starting another operation", code='state_conflict')
                if self.store.db.execute("SELECT 1 FROM admissions WHERE operation_id=?", (key,)).fetchone():
                    raise BridgeError("request_id is reserved by a worktree helper; use a new operation_id", code="operation_conflict")
                failure, family, blocked = None, None, None
                with self.store.transaction():
                    self.store.db.execute("INSERT INTO operations(id,fingerprint,state,task_id) VALUES (?,?,'running',?)",
                                          (key, fingerprint, args.get("task_id")))
                    try:
                        family = self._admit(key, name, args)
                    except WorkspaceBusy as exc:
                        if exc.reconcile_ids:
                            blocked = exc
                        else:
                            failure = exc
                    except Exception as exc:
                        failure = exc
                    self.store.db.execute("INSERT INTO operation_details VALUES (?,?,?,?,?)",
                        (key, args.get("task_id") or (family if name == "codex_start" else None), name, time.time(), json.dumps(redact(request_description(name, args)))))
                    touch(self.store, args.get("task_id"))
                    if failure is not None:
                        data = failure.as_dict() if isinstance(failure, BridgeError) else BridgeError(str(failure), code="internal_error", uncertain=False).as_dict()
                        rejected = {"error": redact(str(failure)), "error_data": data, "operation_rejected": True}
                        reconciliation.operation_outcome(self.store, key, "rejected", data["code"])
                        self.store.db.execute("UPDATE operations SET state='done',result=? WHERE id=?", (json.dumps(rejected), key))
                        self.store.db.execute("DELETE FROM admissions WHERE operation_id=?", (key,))
                        self.resources.finish(key, rejected=True)
                if failure is not None:
                    if isinstance(failure, BridgeError) and failure.code == "budget_exceeded" and args.get("task_id"):
                        self.schedule_budget_check(args["task_id"])
                    raise failure
                spawn = True
        # Creating a task may execute it immediately under an eager task factory.
        # There is no suspension between releasing admission and registering it.
        if spawn:
            running = asyncio.create_task(self._run_operation(key, name, family, function,
                admission=(args, blocked)))
            self.operation_tasks[key] = running
            running.add_done_callback(lambda task: self._finish_task(self.operation_tasks, key, task))
        # Caller cancellation/timeouts detach the waiter, not the accepted work.
        return await asyncio.shield(running)

    async def _run_operation(self, key, name, family, function, admission=None):
        token = self.current_operation.set(key)
        context = {"key": key, "phase": "admission", "native_write": False, "local_write": False, "execution_write": False}
        context_token = self.operation_context.set(context)
        try:
            if admission is not None:
                args, blocked = admission
                if blocked is not None:
                    await self.wait_for_reconciliation(blocked.reconcile_ids)
                    async with self.admission_lock:
                        with self.store.transaction():
                            if self.shutting_down:
                                raise BridgeError("Broker is stopping; reconnect before starting another operation", code='state_conflict')
                            family = self._admit(key, name, args)
                await self.check_new_family(key, family)
            if name == "codex_delete" and admission is not None:
                args = admission[0]
                await self.prepare_deletion(args["task_id"], args["confirm_thread_id"])
                context["delete_preflight"] = args["task_id"]
                family = self.store.task_head(args["task_id"]).get("root_task_id", args["task_id"])
            if name in ("codex_interrupt", "codex_worktrees"):
                context["phase"] = "control"
                result = await function()
            else:
                lock = self.family_locks.setdefault(family, asyncio.Lock())
                async with lock:
                    context["phase"] = "control"
                    result = await function()
        except BaseException as exc:
            phase = context["phase"]
            rejected = (phase == "admission" and isinstance(exc, Exception)) or (
                isinstance(exc, BridgeError) and exc.code == "budget_exceeded" and not context["execution_write"]) or (
                isinstance(exc, decisions.DecisionError) and exc.before_write) or (
                isinstance(exc, WorktreeOperationError) and exc.before_write) or (
                isinstance(exc, BridgeError) and not context["native_write"] and not context["local_write"])
            context["phase"] = "rejected" if rejected else "finished"
            cache_context = self.operation_context.set(None)
            try:
                with self.store.transaction():
                    failure = {"error": redact(str(exc) or type(exc).__name__)}
                    if isinstance(exc, BridgeError):
                        failure["error_data"] = exc.as_dict()
                    else:
                        failure["error_data"] = BridgeError(str(exc) or type(exc).__name__,
                            code="native_timeout" if isinstance(exc, TimeoutError) else "transport_unavailable" if isinstance(exc, OSError) else "internal_error").as_dict()
                    if rejected:
                        failure["operation_rejected"] = True
                        reconciliation.operation_outcome(self.store, key, "rejected", failure["error_data"]["code"])
                    self.resources.finish(key, rejected=rejected or not context["execution_write"], uncertain=not rejected and context["execution_write"])
                    self.store.db.execute("UPDATE operations SET state=?,result=? WHERE id=?",
                        ("done" if rejected else "uncertain", json.dumps(redact(failure)), key))
                    row = self.store.db.execute("SELECT task_id FROM operations WHERE id=?", (key,)).fetchone()
                    if row:
                        touch(self.store, row[0])
                    if row and row[0] and not rejected and not isinstance(exc, RpcError) and name in (
                            "codex_start", "codex_send", "codex_resume", "codex_delegate", "codex_queue", "codex_goal", "codex_review", "codex_compact"):
                        self.store.claim(row[0], "uncertain", "uncertain", {"operation_id": key, "reason": "operation_outcome"})
            finally:
                self.operation_context.reset(cache_context)
            if isinstance(exc, BridgeError) and exc.code == "budget_exceeded" and row and row[0]:
                self.schedule_budget_check(row[0])
            raise
        else:
            context["phase"] = "finished"
            with self.store.transaction():
                reconciliation.operation_outcome(self.store, key, "acknowledged")
                self.resources.finish(key, queued=name == "codex_queue" and admission is not None and admission[0].get("action") == "add")
                self.store.db.execute("UPDATE operations SET state='done',result=? WHERE id=?", (json.dumps(redact(result)), key))
                row = self.store.db.execute("SELECT task_id FROM operations WHERE id=?", (key,)).fetchone()
                if row:
                    touch(self.store, row[0])
            return result
        finally:
            self.operation_context.reset(context_token)
            self.current_operation.reset(token)
            async with self.admission_lock:
                with self.store.transaction():
                    if not self.managed_worktrees.retained(key):
                        self.store.db.execute("DELETE FROM admissions WHERE operation_id=?", (key,))
                        self.project_operations.pop(key, None)

    async def start(self, prompt, request_id, workdir=None, project_id=None, model=None, effort=None, access="write",
                    approval_mode="inherit", collaboration_mode="default", service_tier=None,
                    images=None, context_files=None, skills=None, output_schema=None, name=None):
        if self.current_operation.get() is None:
            values = locals().copy()
            values.pop("self")
            return await self.operation(request_id, "codex_start", values, lambda: self.start(**values))
        descriptor = self.select_project(workdir, project_id)
        project = Path(descriptor["path"])
        await self.check_capacity(str(project))
        requested_settings = {"model": model, "effort": effort, "access": access, "approval_mode": approval_mode,
                              "collaboration_mode": collaboration_mode, "service_tier": service_tier}
        model, effort = await self.choose_model(model, effort, service_tier)
        if service_tier == "default":
            # "default" means no explicit tier; it is not a catalog tier id.
            service_tier = None
        task = {"id": self.admission()["family_id"], "thread_id": None, "project_id": descriptor["id"], "project": str(project),
                "model": model, "effort": effort, "access": access, "status": "starting",
                "turn_id": None, "created_at": time.time(), "updated_at": time.time(),
                "summary": "", "files": [], "commands": [], "plan": [], "usage": None,
                "approval_mode": approval_mode, "collaboration_mode": collaboration_mode,
                "service_tier": service_tier, "name": name, "archived": False, "requested_settings": requested_settings}
        inputs = await self.build_input(task, prompt, images, context_files, skills)
        with self.store.transaction():
            task = self.lifecycle.create(task)
            self.resources.bind(request_id, task["id"])
            self.store.db.execute("UPDATE operations SET task_id=? WHERE id=?", (task["id"], request_id))
            self.scheduler.bind(request_id, task["id"])
            self.store.event(task["id"], "bridge/task_created", {"project": str(project)})
        try:
            generation = self.backend.generation
            options = await self.thread_options(task)
            result = await self.backend.call("thread/start", {**options, "ephemeral": False,
                                             "historyMode": "paginated", "dynamicTools": [PARENT_TOOL]})
            with self.store.transaction():
                task = self.lifecycle.attach(task["id"], result["thread"]["id"])
            # Verify the server actually selected the restrictive profile. No
            # fallback to legacy workspace-write (which allows broad reads).
            self.verify_thread(task, result, generation=generation)
            self.lifecycle.metadata(task["id"], self.effective_fields(result))
            self.loaded.add(task["id"])
            if name:
                claim = self.claim_snapshot(task["id"], "turn")
                try:
                    await self.backend.call("thread/name/set", {"threadId": task["thread_id"], "name": name})
                except OptionalCapabilityUnavailable:
                    with self.store.transaction():
                        restored = self.lifecycle.rollback(task["id"], None, {**task, "status": "idle"}, known_rejected=True)
                        if restored is not None and claim:
                            self.restore_rejected_claim(task["id"], "turn", claim["revision"], None)
                    raise
            return await self.start_turn(task, prompt, inputs, output_schema)
        except Exception:
            latest = self.store.get("tasks", task["id"])
            with self.store.transaction():
                self.lifecycle.start_failed(task["id"])
                self.store.event(task["id"], "bridge/start_failed", {"message": "Start did not finish; inspect status before retrying"})
            await self.wake()
            raise

    def verify_thread(self, task, result, *, generation=None):
        if generation is not None and (generation != self.backend.generation or not self.backend.alive):
            raise BridgeError("Codex connection changed during scope verification; resume before continuing", code='scope_unverified')
        profile = (result.get("activePermissionProfile") or {}).get("id")
        if profile != self.backend.profile(task["access"]):
            raise BridgeError("Codex did not confirm the required permission profile; refusing to start work", code='scope_unverified')
        if not result.get("cwd") or not same_path(result["cwd"], task.get("native_cwd", task["project"])):
            raise BridgeError("Codex returned an unexpected working directory", code='scope_unverified')
        roots = result.get("runtimeWorkspaceRoots")
        if not roots or len(roots) != 1 or not same_path(roots[0], task["project"]):
            raise BridgeError("Codex did not confirm the exact project workspace roots", code='scope_unverified')
        if result.get("approvalPolicy") != "on-request":
            raise BridgeError("Codex did not preserve the requested approval policy", code='scope_unverified')
        expected = self.reviewer_params(task.get("approval_mode", "inherit")).get("approvalsReviewer")
        actual = result.get("approvalsReviewer")
        if actual == "guardian_subagent":
            actual = "auto_review"
        if expected and actual != expected:
            raise BridgeError("Codex did not confirm the requested approval reviewer", code='scope_unverified')

    def begin_turn(self, task_id, turn_id, source="event", *, review_entry=False):
        current = self.store.task_head(task_id)
        if turn_id and current.get("turn_id") != turn_id and native_turns.recorded_root(self.store, current, known=(turn_id,)) == turn_id:
            # A later root event can resolve an otherwise unknown native start.
            self.resources.merge_nested(task_id, current["turn_id"], turn_id)
        review = self.inline_reviews.get(task_id)
        if turn_id and not (review and review["sent"] and review["turn_id"] is None and source == "implicit" and not review_entry):
            self.scheduler.observed_turn(task_id, turn_id)
        reservation = self.store.task_head(task_id).get("queue_reservation") or {}
        task = self.lifecycle.begin(task_id, turn_id, source=source)
        if (turn_id and task.get("turn_id") == turn_id and reservation.get("outcome") == "accepted"
                and turn_id != reservation.get("after_turn_id")):
            self.record_execution(task_id, self.backend.generation)
        for _, req in self.store.records("requests", task_id=task_id, statuses=("pending", "answering")):
            if task.get("turn_id") == turn_id and req["task_id"] == task_id and req["status"] in ("pending", "answering") and req["params"].get("turnId") != turn_id:
                self.store.update("requests", req["id"], {"status": "expired"})
                self.raw_requests.pop(req["id"], None)
        return task

    def reserve_turn(self, task_id, fields=None):
        task = self.task(task_id)
        if task.get("verified_generation") != self.backend.generation or task_id not in self.loaded:
            raise BridgeError("Native thread scope is unverified; resume before starting a turn", code='scope_unverified')
        self.resources.assert_execution(task_id)
        with self.store.transaction():
            key = self.current_operation.get()
            if key is None:
                key = "local:" + str(uuid.uuid4())
            self.resources.require_turn(key, task_id)
            previous, token = self.lifecycle.reserve(task_id, fields)
            self.resources.bind(key, task_id, token=token)
            self.scheduler.bind(key, task_id, token=token)
        self.intent_inflight[(task_id, "turn")] = next(c["revision"] for c in self.store.claims(task_id=task_id) if c["kind"] == "turn")
        return previous, token

    def check_execution(self, task_id, generation, token=None):
        task = self.task(task_id)
        self.assert_writable_session(task)
        if (generation != self.backend.generation or not self.backend.alive
                or task_id not in self.loaded or task.get("verified_generation") != generation):
            raise BridgeError("Native thread scope is unverified; resume before enabling native execution", code='state_conflict')
        if token is not None and task.get("turn_start") != token:
            raise BridgeError("Task became active before turn start; inspect codex_get before sending more work", code='state_conflict')

    def execution_guard(self, task_id, generation, token=None):
        self.check_execution(task_id, generation, token)
        self.resources.assert_execution(task_id)
        context = self.operation_context.get()
        if context:
            context["pending_execution"] = True

    def record_execution(self, task_id, generation):
        # Only acknowledgement/accepted-consumption paths may call this.
        task = self.store.task_head(task_id)
        if (generation == self.backend.generation and self.backend.alive and task_id in self.loaded
                and task.get("verified_generation") == generation and not task.get("deleted")
                and task.get("workspace_execution_project") != task["project"]):
            self.lifecycle.metadata(task_id, {"workspace_execution_project": task["project"]})

    async def start_turn(self, task, prompt, inputs=None, output_schema=None):
        settings = {k: task[k] for k in ("model", "effort")}
        task = self.task(task["id"])
        self.assert_writable_session(task)
        if task["status"] in ACTIVE and not (task["status"] == "starting" and task.get("turn_id") is None and not task.get("turn_start")):
            raise BridgeError("Task became active before turn start; inspect codex_get before sending more work", code='state_conflict')
        if task.get("can_accept_direct_input") is False:
            raise BridgeError("Codex owns this child lifecycle; send instructions through its parent using codex_delegate", code='state_conflict')
        with self.store.transaction():
            previous, token = self.reserve_turn(task["id"], settings)
            claim_revision = self.intent_inflight[(task["id"], "turn")]
            generation = self.backend.generation
            task = self.task(task["id"])
            self.store.event(task["id"], "bridge/input", {"text": prompt})
        try:
            params = {"threadId": task["thread_id"],
                "input": inputs or [{"type": "text", "text": prompt, "text_elements": []}],
                "model": task["model"], "effort": task["effort"],
                "permissions": self.backend.profile(task["access"]),
                "runtimeWorkspaceRoots": [task["project"]],
                "cwd": task.get("native_cwd", task["project"]), "approvalPolicy": "on-request"}
            params.update(self.turn_options(task))
            if output_schema is not None:
                params["outputSchema"] = output_schema
            result = await self.backend.call("turn/start", params,
                before_send=lambda: self.input_guard(task["id"], generation, params["input"], token))
            context = self.delegation_context.get()
            if context is not None:
                context["acknowledged_turn_id"] = native_turns.logical_id(result.get("turn"), native_turns.for_task(self.store, task["id"]))
            return await self.ack_turn(task["id"], result, token=token, generation=generation)
        except Exception as exc:
            latest = self.store.get("tasks", task["id"])
            context = self.delegation_context.get()
            if isinstance(exc, decisions.DecisionError) and context and context["write_attempted"]:
                exc.before_write, exc.uncertain = False, True
            if (isinstance(exc, decisions.DecisionError) and exc.before_write or
                    isinstance(exc, BridgeError) and exc.code == "budget_exceeded"):
                with self.store.transaction():
                    rolled = self.lifecycle.rollback(task["id"], token, previous, known_rejected=True)
                    if rolled is not None:
                        self.store.claim(task["id"], "turn", "inactive", expected_revision=claim_revision)
            elif isinstance(exc, RpcError):
                with self.store.transaction():
                    self.lifecycle.rollback(task["id"], token, previous)
            elif latest.get("turn_start") == token and latest["status"] == "starting":
                with self.store.transaction():
                    self.lifecycle.uncertain(task["id"], token=token)
            raise
        finally:
            self.finish_intent(task["id"], "turn", claim_revision)

    def never_started(self, task):
        # start() commits attach() before any turn-creating control can run.
        # A missing thread therefore proves no execution, unless other durable
        # facts contradict that ordering. Never apply this to an imported child.
        if (task.get("thread_id") is not None or task.get("turn_id") or task.get("turn_start")
                or task.get("native_child") or task.get("workspace_execution_project")
                or task.get("root_task_id", task["id"]) != task["id"]
                or len(self.store.family_ids(task["id"])) != 1):
            return False
        return not self.store.db.execute("""SELECT 1 FROM turns WHERE task_id=?
            UNION ALL SELECT 1 FROM resource_turns WHERE task_id=?
            UNION ALL SELECT 1 FROM resource_reservations WHERE task_id=?
                AND (token IS NOT NULL OR native_key IS NOT NULL OR turn_id IS NOT NULL) LIMIT 1""",
            (task["id"], task["id"], task["id"])).fetchone()

    def start_inflight(self, task):
        if task["status"] == "starting":
            return True
        rows = self.store.db.execute("""SELECT o.id FROM operations o JOIN operation_details d ON d.id=o.id
            WHERE o.task_id=? AND d.name='codex_start'""", (task["id"],))
        for row in rows:
            operation = self.operation_tasks.get(row[0])
            if operation is not None and not operation.done():
                return True
        return False

    def recover_unstarted(self, task_id):
        task = self.task(task_id, light=True)
        if not self.never_started(task) or self.start_inflight(task):
            return None
        message = "Codex never started this task; nothing ran."
        with self.store.transaction():
            if not self.recorded_unstarted(task):
                self.lifecycle._write(task_id, {"status": "failed", "native_conversation": "not_started"})
                self.store.event(task_id, "bridge/start_not_run", {"message": message})
            processes.acknowledge(self, task_id)
            state = self.store.get("tasks", task_id).get("process_state") or {}
            if state.get("previous_backend_unverified"):
                self.lifecycle.metadata(task_id, {"process_state": {**state, "previous_backend_unverified": False}})
            for claim in self.store.claims(task_id=task_id):
                if claim["kind"] == "turn" or (claim["kind"] == "uncertain" and
                        claim["data"].get("reason") in (processes.REASON, "broker_restart", "operation_outcome")):
                    self.store.claim(task_id, claim["kind"], "inactive", expected_revision=claim["revision"])
            for row in self.store.db.execute("SELECT id FROM resource_reservations WHERE task_id=?", (task_id,)).fetchall():
                self.resources.finish(row[0], rejected=True)
        return message

    def recorded_unstarted(self, task):
        return ((task["status"] == "failed" or task.get("deleted")) and self.never_started(task) and
            bool(self.store.db.execute("SELECT 1 FROM tasks WHERE id=? AND json_extract(data,'$.native_conversation')='not_started'",
                (task["id"],)).fetchone()))

    async def resume(self, task_id, *, rebuild=True):
        task = self.task(task_id, light=True)
        self.assert_writable_session(task, allow_recovery=True)
        if not task.get("thread_id") and self.start_inflight(task):
            raise BridgeError("No Codex thread was created for this task", code='state_conflict')
        self.reset_reconciliation(task_id)
        if not rebuild:
            processes.require_acknowledgement(self.task(task_id, light=True))
        acknowledgement = self.process_acknowledgement.set(task_id if rebuild else None)
        try:
            try:
                message = self.recover_unstarted(task_id) if rebuild else None
                if message is None:
                    await self._resume(task_id)
            except Exception as exc:
                if rebuild:
                    try:
                        await reconciliation.reconcile(self, task_id, failure=exc)
                    except Exception:
                        pass  # Preserve the original native/scope refusal.
                raise
            recovery = await reconciliation.reconcile(self, task_id) if rebuild else None
            if self.store.task_head(task_id).get("turn_status") in TERMINAL and task_id not in self.protocol_quarantine:
                self.inline_reviews.pop(task_id, None)
            if rebuild:
                self.scheduler.reconciled(task_id)
            await self.wake()
            return {**self.get(task_id), **({"reconciliation": recovery} if rebuild else {}),
                **({"message": message} if message else {})}
        finally:
            self.process_acknowledgement.reset(acknowledgement)
            if self.store.claims(task_id=task_id):
                self.schedule_reconciliation(task_id)

    async def _resume(self, task_id):
        task = self.task(task_id)
        protocol_epoch = self.protocol_epochs.get(task_id, 0)
        self.assert_writable_session(task, allow_recovery=True)
        if not task.get("thread_id"):
            raise BridgeError("No Codex thread was created for this task", code='state_conflict')
        await self.ensure_backend()
        generation = self.backend.generation
        task = self.task(task_id)
        if (task_id in self.loaded and task.get("verified_generation") == generation
                and task_id not in self.protocol_quarantine
                and task["status"] not in ("uncertain", "starting", "needs_resume")):
            if task["status"] == "queued" or task.get("queue_reservation"):
                await self.sync_queue(task_id, reconcile=True)
            await self.resume_claims(task_id)
            self.check_recovery(task_id, protocol_epoch, generation)
            await self.wake()
            return self.get(task_id)
        await self.check_capacity(task["project"], exclude=task_id)
        options = await self.thread_options(task)
        snapshot = self.task(task_id)
        if not processes.pending(task):
            self.store.claim(task_id, "uncertain", "uncertain", {
                "operation_id": self.current_operation.get(), "reason": "resume"})
        try:
            result = await self.backend.call("thread/resume", {**options,
                "threadId": task["thread_id"], "excludeTurns": True})
        except OptionalCapabilityUnavailable:
            raise
        except RpcError as exc:
            raise self.unavailable_conversation(task) from exc
        task = self.task(task_id)
        self.check_recovery(task_id, protocol_epoch, generation)
        self.verify_thread(task, result, generation=generation)
        fields = self.effective_fields(result)
        if task["revision"] != snapshot["revision"]:
            for key in ("native_status", "approval_mode", "approvals_reviewer"):
                fields.pop(key, None)
        with self.store.transaction():
            self.lifecycle.metadata(task_id, fields)
        self.loaded.add(task_id)
        if self.process_acknowledgement.get() == task_id:
            self.protocol_quarantine.discard(task_id)
            if task.get("queue_reservation") or task["status"] == "queued":
                await self.sync_queue(task_id, reconcile=True)
            await self.resume_claims(task_id)
            self.check_recovery(task_id, protocol_epoch, generation)
            self.protocol_quarantine.discard(task_id)
            with self.store.transaction():
                self.store.event(task_id, "bridge/resumed", {"message": "Conversation loaded; review history before continuing interrupted work"})
            await self.wake()
            return self.get(task_id)
        # Reconcile a lost turn/start acknowledgement or an app-server restart
        # against persisted turns without replaying a prompt.
        snapshot = self.task(task_id)
        turn = await self.latest_turn(task_id)
        self.check_recovery(task_id, protocol_epoch, generation)
        latest = self.store.get("tasks", task_id)
        if latest["status"] in ("needs_resume", "uncertain", "starting"):
            active = result["thread"].get("status", {}).get("type") == "active"
            latest = self.lifecycle.recover(task_id, turn, active, expected_revision=snapshot["revision"])
        elif latest["revision"] == snapshot["revision"]:
            latest = self.lifecycle.gap(task_id, "recovery")
        with self.store.transaction():
            self.store.event(task_id, "bridge/resumed", {"message": "Conversation loaded; review history before continuing interrupted work"})
        if latest.get("queue_reservation") or latest["status"] == "queued":
            await self.sync_queue(task_id, reconcile=True)
        self.check_recovery(task_id, protocol_epoch, generation)
        self.protocol_quarantine.discard(task_id)
        await self.reconcile_claims(task_id, explicit=True)
        self.check_recovery(task_id, protocol_epoch, generation)
        await self.wake()
        return self.get(task_id)

    async def resume_claims(self, task_id):
        if self.process_acknowledgement.get() != task_id:
            return await self.reconcile_claims(task_id, explicit=True)
        epoch, generation = self.protocol_epochs.get(task_id, 0), self.backend.generation
        for _ in range(3):
            observed = await self.reconcile_claims(task_id, explicit=True, recovery=True)
            self.check_recovery(task_id, epoch, generation)
            if observed is not None:
                return observed
            task = self.task(task_id, light=True)
            unavailable = sorted(self.reconciliation_methods(task) & self.backend.unavailable_methods)
            if unavailable:
                raise BridgeError("Resume could not verify native state because optional native read API " + ", ".join(unavailable)
                    + " is unavailable. Workspace ownership was retained", code="workspace_read_unavailable",
                    ids={"task_id": task_id, "thread_id": task.get("thread_id")}, details={"unavailable_methods": unavailable})
        task = self.task(task_id, light=True)
        raise BridgeError("Resume could not commit a consistent native recovery snapshot. Inspect codex_get and retry codex_resume",
            code="state_conflict", retryable=True, uncertain=True,
            ids={"task_id": task_id, "thread_id": task.get("thread_id")},
            next_steps=["Inspect codex_get and codex_history", "Retry codex_resume with a new operation_id"])

    def check_recovery(self, task_id, epoch, generation):
        if self.protocol_epochs.get(task_id, 0) != epoch:
            raise BridgeError("Task received a native protocol error during resume; inspect codex_history and resume again", code='state_conflict')
        if generation != self.backend.generation or not self.backend.alive:
            raise BridgeError("Codex connection changed during scope verification; resume before continuing", code='state_conflict')

    async def send(self, task_id, prompt, mode="follow_up", expected_turn_id=None, model=None, effort=None,
                   images=None, context_files=None, skills=None, output_schema=None):
        task = self.task(task_id)
        self.assert_writable_session(task)
        self.resources.assert_execution(task_id)
        if task.get("can_accept_direct_input") is False:
            raise BridgeError("This native child does not accept direct input; use codex_delegate on its parent", code='state_conflict')
        inputs = await self.build_input(task, prompt, images, context_files, skills)
        if mode == "steer":
            if task["status"] not in ("running", "waiting") or not expected_turn_id or task["turn_id"] != expected_turn_id:
                raise BridgeError("Steering requires the current active expected_turn_id", code='stale_turn')
            if model or effort or output_schema:
                raise BridgeError("Change model/effort on a follow-up turn, not during steering", code='state_conflict')
            if not self.backend.alive or task_id not in self.loaded:
                raise BridgeError("Resume and reconcile the disconnected task before steering", code='state_conflict')
            result = await self.backend.call("turn/steer", {"threadId": task["thread_id"],
                "expectedTurnId": expected_turn_id,
                "input": inputs}, before_send=lambda: self.input_guard(task_id, task["verified_generation"], inputs))
            with self.store.transaction():
                self.store.event(task_id, "bridge/steered", {"text": prompt, "result": result})
            await self.wake()
            return self.get(task_id)
        if task["status"] in ("running", "waiting", "starting", "queued", "interrupting"):
            raise BridgeError("Task is active; use steer, or interrupt and wait for completion", code='state_conflict')
        await self.resume(task_id, rebuild=False)
        task = self.task(task_id)
        if task["status"] in ("running", "waiting", "starting", "queued", "interrupting"):
            raise BridgeError("Resume found an active turn; use steer or interrupt it before sending a follow-up", code='state_conflict')
        await self.check_capacity(task["project"], exclude=task_id)
        task["model"], task["effort"] = await self.choose_model(model or task["model"],
            effort if effort else (None if model and model != task["model"] else task["effort"]))
        return await self.start_turn(task, prompt, inputs, output_schema)

    async def interrupt(self, task_id, expected_turn_id):
        task = self.task(task_id)
        processes.require_acknowledgement(task)
        if task["status"] not in ("running", "waiting", "interrupting") or task["turn_id"] != expected_turn_id:
            raise BridgeError("Interrupt requires the current active expected_turn_id", code='stale_turn')
        if not self.backend.alive:
            raise BridgeError("Codex disconnected; resume to reconcile its history", code='state_conflict')
        identity = self.lifecycle.identity(task_id)
        await self.backend.call("turn/interrupt", {"threadId": task["thread_id"], "turnId": expected_turn_id})
        self.lifecycle.interrupting(identity)
        self.schedule_reconciliation(task_id)
        return self.get(task_id)

    async def history(self, task_id, kind="turns", cursor=None, limit=50, turn_id=None):
        task = self.task(task_id)
        if not task.get("thread_id"):
            raise BridgeError("Task has no Codex thread", code='state_conflict')
        await self.ensure_backend()
        params = {"threadId": task["thread_id"], "cursor": cursor, "limit": limit,
                  "sortDirection": "asc"}
        if kind == "items":
            params["turnId"] = turn_id
        else:
            params["itemsView"] = "summary"
        result = await self.backend.call("thread/" + kind + "/list", params)
        if kind == "turns":
            scope = native_turns.for_task(self.store, task_id)
            scope.known.update(turn["id"] for turn in result.get("data", []))
            result = {**result, "data": [{**turn, "logical_turn_id": native_turns.logical_id(turn, scope)} for turn in result.get("data", [])]}
        result = self.safe_payload(result)
        if len(result.get("data", [])) > limit:
            raise BridgeError("Native history exceeded the requested page size; request a smaller page", code='state_conflict')
        records = [bounded_result(item, 16384) for item in result.pop("data", [])]
        return {**bounded_result(result, 128 * 1024), "data": records}

    @staticmethod
    def safe_payload(value):
        def without_reasoning(item):
            if isinstance(item, dict):
                return {k: without_reasoning(v) for k, v in item.items()
                        if k != "content" or item.get("type") != "reasoning"}
            if isinstance(item, list):
                return [without_reasoning(v) for v in item]
            return item
        # redact already walks the entire tree; repeating it at each ancestor
        # rescans large patches many times without removing any more secrets.
        return redact(without_reasoning(value))

    @classmethod
    def review_content(cls, value):
        safe = cls.safe_payload(value)
        def changes(before, after):
            if isinstance(before, dict) and isinstance(after, dict):
                return sum(changes(v, after[k]) if k in after else 1 for k, v in before.items())
            if isinstance(before, list) and isinstance(after, list):
                return sum(changes(a, b) for a, b in zip(before, after)) + abs(len(before) - len(after))
            return int(before != after)
        count = changes(value, safe)
        return safe, {"content_redacted": bool(count), "redaction_count": count}

    async def events(self, after=0, limit=100, wait_seconds=0, task_id=None, include_descendants=True,
                     requests_cursor=None, requests_limit=50, event_offset=0, event_seq=0, view="default",
                     task_ids=None, cursors=None, tasks_offset=0):
        if view == "outcome":
            return await outcomes.wait(self, task_ids=task_ids, task_id=task_id, cursors=cursors,
                include_descendants=include_descendants, wait_seconds=wait_seconds, tasks_offset=tasks_offset,
                limit=limit, requests_cursor=requests_cursor, requests_limit=requests_limit)
        if task_id:
            self.task(task_id, allow_deleted=True, light=True)
        family_root = task_id if task_id and include_descendants else None
        async with self.changed:
            result = self.store.events(after, limit, task_id, family_root=family_root, event_offset=event_offset, event_seq=event_seq, compact=view == "compact")
            if not result["events"] and wait_seconds and not self.store.request_count(task_id):
                try:
                    await asyncio.wait_for(self.changed.wait(), wait_seconds)
                except TimeoutError:
                    pass
                result = self.store.events(after, limit, task_id, family_root=family_root, event_offset=event_offset, event_seq=event_seq, compact=view == "compact")
        result.update(self.pending_fields(self.request_page(task_id, requests_cursor, requests_limit)))
        result["view"] = view
        if task_id:
            task = self.task(task_id, allow_deleted=True, light=True)
            result.update(task_status=task["status"], turn_id=task.get("turn_id"))
            if view == "compact":
                result.update(counts=task["counts"], evidence_complete=task.get("evidence_complete"),
                              workspace_reserved=bool(self.store.claim_count(task.get("root_task_id", task_id))))
        return result

    async def on_exit(self, reason):
        self.cancel_budget_timers()
        self.resources.disconnect()
        self.loaded.clear()
        self.deletion_requests.clear()
        self.raw_requests.clear()
        self.file_proposals.clear()
        with self.store.transaction():
            for _, task in self.store.records("tasks", light=True, recovery=True):
                processes.capture(self, task)
                if processes.pending(self.store.task_head(task["id"])):
                    processes.hold(self, task["id"])
                elif self.store.claims(task_id=task["id"]) and task["id"] not in self.protocol_quarantine:
                    self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "backend_disconnected"})
                if task["status"] in ACTIVE:
                    self.lifecycle.gap(task["id"], "backend_disconnected", disconnected=True)
                    self.store.event(task["id"], "bridge/disconnected", {"reason": reason,
                        "event_gap_possible": True, "recovery": "Resume, then page through codex_history"})
            for _, req in self.store.records("requests", statuses=("pending", "answering"), light=True):
                if req["status"] in ("pending", "answering"):
                    self.store.update("requests", req["id"], {"status": "expired"})
        await self.wake()

    async def on_message(self, msg):
        task = self.protocol_task(msg)
        error = native_message_error(msg)
        if error:
            await self.protocol_error(msg, task, error)
            return
        if task and task["id"] in self.protocol_quarantine and msg["method"] != "thread/deleted":
            if "id" in msg:
                await self.protocol_error(msg, task, "task requires verified resume after a protocol error", new_error=False)
            else:
                with self.store.transaction():
                    self.store.event(task["id"], msg["method"], self.safe_payload(msg.get("params", {})))
                await self.wake()
            return
        if "id" in msg:
            duplicates = [req for _, req in self.store.records("requests", rpc_id=msg["id"], generation=self.backend.generation, statuses=("pending", "answering"))]
            if duplicates:
                for task_id in {req["task_id"] for req in duplicates} - ({task["id"]} if task else set()):
                    await self.protocol_error({"method": msg["method"]}, self.task(task_id), "duplicate active native request id")
                await self.protocol_error(msg, task, "duplicate active native request id")
                return
        try:
            await self._on_message(msg, task)
        except OptionalCapabilityUnavailable as exc:
            await self.protocol_error(msg, task, str(exc))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            await self.protocol_error(msg, task, f"invalid native fields ({type(exc).__name__})")
        except BridgeError as exc:
            raise NativeCompatibilityError(msg["method"], self.backend.version, str(exc)) from exc

    def protocol_task(self, msg):
        params = msg.get("params") if isinstance(msg, dict) else None
        if not isinstance(params, dict):
            return None
        thread = params.get("thread")
        nested_id = thread.get("id") if isinstance(thread, dict) else None
        thread_id = params.get("threadId")
        if thread_id and nested_id and thread_id != nested_id:
            return None
        identity = thread_id or nested_id
        return self.task_for_thread(identity) if isinstance(identity, str) and identity else None

    async def protocol_error(self, msg, task, reason, *, new_error=True):
        request = isinstance(msg, dict) and "id" in msg
        method = msg.get("method") if isinstance(msg, dict) and isinstance(msg.get("method"), str) else ""
        data = {"code": NativeCompatibilityError.code, "method": method, "codex_version": self.backend.version,
                "reason": reason, "scope": "task" if task else ("request" if request else "backend"),
                "event_gap_possible": True, "recovery": "Resume, then page through codex_history"}
        with self.store.transaction():
            if task and new_error:
                self.protocol_epochs[task["id"]] = self.protocol_epochs.get(task["id"], 0) + 1
                self.lifecycle.gap(task["id"], "native_protocol_error", disconnected=True)
                self.store.claim(task["id"], "uncertain", "uncertain", {"reason": "native_protocol_error"})
                self.protocol_quarantine.add(task["id"])
                self.loaded.discard(task["id"])
                for _, req in self.store.records("requests", task_id=task["id"], statuses=("pending", "answering"), light=True):
                    if req["task_id"] == task["id"] and req["status"] in ("pending", "answering"):
                        self.store.update("requests", req["id"], {"status": "expired"})
                        self.raw_requests.pop(req["id"], None)
                for request_id, raw in list(self.raw_requests.items()):
                    if raw.get("threadId") == task["thread_id"]:
                        self.raw_requests.pop(request_id, None)
                for key in list(self.file_proposals):
                    if key[1] == task["thread_id"]:
                        self.file_proposals.pop(key, None)
            self.store.event(task["id"] if task else None, "bridge/protocol_error", data)
        if request:
            request_id = msg["id"] if type(msg["id"]) in (str, int) else None
            await self.backend.respond(request_id, error={"code": -32602,
                "message": "Malformed native request; no permission granted. " + reason})
        await self.wake()
        if not task and not request:
            raise NativeCompatibilityError(method, self.backend.version, reason)

    async def _on_message(self, msg, task):
        method, params = msg.get("method", ""), msg.get("params") or {}
        if task and task.get("deleted") and method != "thread/deleted":
            if "id" in msg:
                await self.backend.respond(msg["id"], error={"code": -32601, "message": "Unsupported or unowned request; no permission granted"})
                with self.store.transaction():
                    self.store.event(task["id"], "bridge/request_rejected", {"method": method, "reason": "deleted_conversation"})
            elif not method.startswith("codex/event/") and method != "item/reasoning/textDelta":
                with self.store.transaction():
                    self.store.event(task["id"], method, self.safe_payload(params))
            await self.wake()
            return
        if (not task or (task.get("native_child") and task["id"] not in self.loaded)) and (params.get("threadId") or method == "thread/started") and method not in ("thread/deleted", "thread/archived", "thread/unarchived"):
            task = await self.adopt_message_thread(params)
        if task and method == "thread/queue/changed":
            self.mark_native_intent(task["id"], "queue", "native_queue_changed")
            self.schedule_reconciliation(task["id"])
            await self.sync_queue(task["id"])
            task = self.task(task["id"])
        if task:
            # Adoption and queue reconciliation may await native I/O.
            task = self.store.get("tasks", task["id"])
            scope = native_turns.for_task(self.store, task["id"])
            turn_id = native_turns.event_id(params, scope)
            turn = params.get("turn") or {}
            root = turn.get("rootTurnId")
            review = self.inline_reviews.get(task["id"])
            reviewing = bool(review and review["sent"] and review["generation"] == self.backend.generation)
            if method in ("turn/started", "turn/completed") and root and root != turn["id"] and not scope.owns(root):
                foreign = scope.foreign(root)
                if reviewing and foreign and review["turn_id"] != turn["id"]:
                    await self.protocol_error(msg, task,
                        "Inline review turn identity is ambiguous across threads; use codex_resume, then codex_history")
                    return
                pending = bool(task.get("turn_start"))
                if not foreign and ((reviewing and review["turn_id"] != turn["id"]) or pending and not scope.owns(turn["id"])):
                    if reviewing:
                        review["deferred"][turn["id"]] = root
                    with self.store.transaction():
                        self.mark_native_intent(task["id"], "turn", "unresolved_turn_identity")
                        self.store.event(task["id"], method, self.safe_payload(params))
                    self.schedule_reconciliation(task["id"])
                    await self.wake()
                    return
            candidate = (review or {}).get("deferred", {}).get(params.get("turnId"))
            if reviewing and candidate and not scope.owns(candidate) and review["turn_id"] != params.get("turnId"):
                await self.protocol_error(msg, task,
                    "Inline review turn identity is unresolved; use codex_resume, then codex_history")
                return
        if method == "bridge/event_gap" and "id" not in msg:
            with self.store.transaction():
                if task:
                    if params.get("turnId"):
                        self.begin_turn(task["id"], params["turnId"], source="implicit")
                    self.lifecycle.gap(task["id"], "event_ingress_budget")
                self.store.event(task["id"] if task else None, "bridge/event_gap", self.safe_payload(params))
            if task:
                self.schedule_budget_check(task["id"])
            await self.wake()
            return
        if "id" in msg:
            if not task or method not in REQUEST_KINDS:
                await self.backend.respond(msg["id"], error={"code": -32601,
                    "message": "Unsupported or unowned request; no permission granted"})
                with self.store.transaction():
                    self.store.event(task["id"] if task else None, "bridge/request_rejected", {"method": method})
                await self.wake()
                return
            if method == "item/tool/call" and params.get("tool") != "ask_parent":
                await self.backend.respond(msg["id"], {"success": False,
                    "contentItems": [{"type": "inputText", "text": "Unknown bridge tool"}]})
                return
            if method == "item/fileChange/requestApproval":
                proposal = self.file_proposals.get(self.proposal_key(params))
                # Request-supplied patches cannot replace the current native item.
                params = {**params, "proposedChanges": proposal["changes"] if proposal else []}
            content, redaction = self.review_content(params)
            request = {"id": str(uuid.uuid4()), "task_id": task["id"], "method": method,
                       "kind": REQUEST_KINDS[method], "params": content, **redaction,
                       "rpc_id": msg["id"], "generation": self.backend.generation,
                       "status": "pending", "created_at": time.time()}
            request["parent_task_id"] = task.get("parent_task_id")
            request["root_task_id"] = task.get("root_task_id", task["id"])
            request["requires_human"] = request["kind"] != "parent_question"
            request["blocking"] = params.get("isBlocking", True)
            if request["kind"] == "file_approval":
                request.update(proposal_hash=proposal["hash"] if proposal else None,
                               proposal_revision=proposal["revision"] if proposal else None)
            else:
                request.update(proposal_hash=hashlib.sha256(json.dumps([self.backend.generation, method, params],
                    sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(), proposal_revision=1)
            self.raw_requests[request["id"]] = params
            with self.store.transaction():
                if turn_id:
                    task = self.begin_turn(task["id"], turn_id, source="implicit")
                if request["kind"] == "parent_question" and "envelope" in params["arguments"]:
                    try:
                        record = decisions.bind(self, task, params["arguments"]["envelope"], kind="question", request_id=request["id"],
                            question=params["arguments"]["question"], context=params["arguments"].get("context"))
                    except decisions.DecisionError as exc:
                        self.raw_requests.pop(request["id"], None)
                        rejected = exc
                    else:
                        request.update(decision_envelope=record, decision_summary=decisions.summary(record))
                        rejected = None
                else:
                    rejected = None
                if rejected is not None:
                    self.store.event(task["id"], "bridge/decision_rejected", {"code": rejected.code, "turnId": turn_id})
                else:
                    request.update(self.request_authority(request, params, task))
                    request = self.store.save("requests", request)
                    self.store.event(task["id"], "bridge/request", self.public_request(request))
                    self.lifecycle.wait(task["id"], params.get("turnId"), request["blocking"])
            if rejected is not None:
                await self.backend.respond(msg["id"], {"success": False, "contentItems": [{"type": "inputText", "text": json.dumps(rejected.as_dict())}]})
            self.schedule_budget_check(task["id"])
            await self.wake()
            return
        # Raw internal reasoning and legacy duplicate event streams are not public progress.
        if method.startswith("codex/event/") or method == "item/reasoning/textDelta":
            return
        if not task:
            if method in ("warning", "configWarning", "error", "deprecationNotice"):
                with self.store.transaction():
                    self.store.event(None, method, self.safe_payload(params))
                await self.wake()
            return
        deletion_check = None
        deleting = (task["id"],)
        if method == "thread/deleted":
            request = self.deletion_requests.get(task["id"])
            if request and request[0] == self.backend.generation:
                deleting = tuple(key for key, value in self.deletion_requests.items() if value == request)
            for _ in range(3):
                deletion_check, _ = await self.prepare_release_scan(task["id"], deleting=deleting)
                if deletion_check is not False:
                    break
        with self.store.transaction():
            if turn_id and method.startswith(("item/", "turn/")):
                entry = params.get("item", {}).get("type") == "enteredReviewMode" or method == "turn/completed" and not native_turns.nested(params["turn"], scope)
                task = self.begin_turn(task["id"], turn_id, source="event" if method == "turn/started" else "implicit", review_entry=entry)
            if (method in ("turn/started", "turn/completed") or method in ("item/started", "item/completed")
                    and params.get("item", {}).get("type") == "commandExecution" or method == "thread/status/changed"):
                self.invalidate_process_check(task["project"])
            if method == "item/started" and params.get("item", {}).get("type") == "fileChange":
                self.update_proposal(task, params, params["item"].get("id"), params["item"].get("changes"))
            elif method == "item/started" and params.get("item", {}).get("type") == "commandExecution":
                if turn_id == task.get("turn_id") and not any(command.get("id") == params["item"].get("id") for command in task.get("commands", [])):
                    self.mark_native_intent(task["id"], "background", "native_activity")
            elif method == "item/fileChange/patchUpdated":
                self.update_proposal(task, params, params.get("itemId"), params.get("changes"))
            safe = self.safe_payload(params)
            self.store.event(task["id"], method, safe, content_redacted=safe != params)
            if method.startswith(("item/autoApprovalReview/", "autoApprovalReview/")) and method.endswith("/completed"):
                # A late decision still belongs in the audit trail of its turn.
                authority.native_receipt(self, task, params)
            if turn_id and method.startswith(("item/", "turn/")) and turn_id != task.get("turn_id") and method != "turn/completed":
                pass
            elif method == "turn/started":
                pass  # begin_turn also handles duplicate notifications after completion.
            elif method == "thread/archived":
                self.lifecycle.archive(task["id"], "archive")
            elif method == "thread/unarchived":
                self.mark_native_intent(task["id"], "background", "unarchive")
                self.lifecycle.archive(task["id"], "unarchive")
            elif method == "thread/deleted":
                requested = self.deletion_requests.get(task["id"], (None,))[0] == self.backend.generation
                self.record_deletion(task["id"], requested=requested, native_conversation="deleted", process_check=deletion_check, deleting=deleting)
            elif method == "thread/status/changed":
                self.lifecycle.metadata(task["id"], {"native_status": params.get("status")})
                if params.get("status", {}).get("type") == "active":
                    self.mark_native_intent(task["id"], "background", "native_activity")
            elif method in ("thread/goal/updated", "thread/goal/cleared"):
                self.mark_native_intent(task["id"], "goal", "native_goal_changed")
            elif method == "turn/diff/updated":
                self.lifecycle.project(task["id"], turn_id, {"diff": safe.get("diff", ""), "diff_turn_id": turn_id})
            elif method.startswith("item/autoApprovalReview/") or method.startswith("autoApprovalReview/"):
                self.lifecycle.project(task["id"], turn_id or task.get("turn_id"), {"last_automatic_review": {"method": method, "data": safe}})
            elif method == "turn/completed" and not native_turns.nested(params["turn"], scope):
                self.complete_turn(task["id"], turn_id, params["turn"])
            elif method == "serverRequest/resolved":
                for _, req in self.store.records("requests", task_id=task["id"], rpc_id=params.get("requestId"), statuses=("pending", "answering")):
                    if req["task_id"] == task["id"] and req["rpc_id"] == params.get("requestId") and req["status"] in ("pending", "answering"):
                        self.store.update("requests", req["id"], {"status": "resolved"})
                        self.raw_requests.pop(req["id"], None)
                self.lifecycle.answered(task["id"], task.get("turn_id"), self.blocking_requests(task["id"]))
            elif method == "item/completed":
                self.lifecycle.item(task["id"], turn_id or task.get("turn_id"), safe["item"])
            elif method == "turn/plan/updated":
                self.lifecycle.project(task["id"], turn_id, {"plan": safe.get("plan", [])})
            elif method == "thread/tokenUsage/updated":
                self.resources.tokens(task, params)
                self.lifecycle.metadata(task["id"], {"usage": safe.get("tokenUsage"), "usage_scope": "cumulative"})
        self.schedule_budget_check(task["id"])
        if method in ("turn/completed", "item/completed", "thread/queue/changed", "thread/status/changed",
                      "thread/goal/updated", "thread/goal/cleared", "thread/archived", "thread/unarchived", "thread/deleted"):
            self.schedule_reconciliation(task["id"])
            if task.get("root_task_id"):
                self.schedule_reconciliation(task["root_task_id"])
        await self.wake()

    def complete_turn(self, task_id, turn_id, native):
        task = self.task(task_id, light=True)
        turn = self.lifecycle.turn(task_id, turn_id)
        if turn_id == task.get("turn_id") and turn and turn["status"] not in TERMINAL:
            self.mark_native_intent(task_id, "background", "turn_completion")
        self.lifecycle.complete(task_id, turn_id, native["status"], self.safe_payload(native.get("error")), native=native)
        review = self.inline_reviews.get(task_id)
        if review and review["turn_id"] == turn_id:
            self.inline_reviews.pop(task_id, None)
        for _, req in self.store.records("requests", task_id=task_id, statuses=("pending", "answering")):
            if req["params"].get("turnId") in (None, turn_id):
                self.store.update("requests", req["id"], {"status": "expired"})
                self.raw_requests.pop(req["id"], None)
        for key in list(self.file_proposals):
            if key[1:3] == (task["thread_id"], turn_id):
                self.file_proposals.pop(key, None)

    def proposal_key(self, params):
        return (self.backend.generation, params.get("threadId"), params.get("turnId"), params.get("itemId"))

    def update_proposal(self, task, params, item_id, changes):
        if task.get("turn_id") != params.get("turnId") or task.get("turn_status") in TERMINAL:
            return
        params = {**params, "itemId": item_id}
        key = self.proposal_key(params)
        if not all(part is not None for part in key):
            return
        self.proposal_revision += 1
        content = {"generation": key[0], "thread_id": key[1], "turn_id": key[2], "item_id": key[3],
                   "cwd": task.get("native_cwd"), "changes": changes}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        proposal = {"hash": digest, "revision": self.proposal_revision, "changes": changes}
        self.file_proposals[key] = proposal
        with self.store.transaction():
            for _, req in self.store.records("requests", kind="file_approval", generation=key[0], thread_id=key[1], turn_id=key[2], item_id=key[3], statuses=("pending", "answering")):
                if (req["kind"] == "file_approval" and req["status"] in ("pending", "answering")
                        and req["generation"] == key[0] and self.proposal_key(req["params"]) == key):
                    raw = self.raw_requests.get(req["id"])
                    if raw is not None:
                        self.raw_requests[req["id"]] = {**raw, "proposedChanges": changes}
                    content, redaction = self.review_content({**self.raw_requests.get(req["id"], req["params"]), "proposedChanges": changes})
                    fields = {"proposal_hash": digest, **redaction, "proposal_revision": proposal["revision"], "params": content}
                    fields.update(self.request_authority({**req, **fields}, self.raw_requests.get(req["id"], content), task))
                    self.store.update("requests", req["id"], fields)

    def request_authority(self, request, params, task):
        fields = authority.review_markers(request)
        if request["kind"] in ("file_approval", "command_approval", "permissions"):
            view = authority.permission_view(self, request, params, task)
            fields["permission_summary"] = {"scope": view["scope"], "scope_verified": view["scope_verified"],
                "sources": len(view["sources"]), "destinations": len(view["destinations"]),
                "filesystem": len(view["filesystem"]), "network_requested": view["network"]["requested"],
                "session_grant": False}
        return fields

    def review_request(self, request_id):
        req = self.store.get("requests", request_id)
        task = self.task(req["task_id"], allow_deleted=True, light=True)
        if task.get("deleted") or req["status"] != "pending" or req["generation"] != self.backend.generation or not self.backend.alive:
            raise BridgeError("Request is resolved, expired, or disconnected; refresh codex_requests", code='stale_request')
        return req

    def validate_proposal(self, req, proposal_hash):
        current = self.store.get("requests", req["id"])
        params = self.raw_requests.get(req["id"])
        task = self.task(req["task_id"])
        proposal = self.file_proposals.get(self.proposal_key(params)) if params is not None else None
        if (not proposal or not proposal_hash or proposal_hash != proposal["hash"]
                or current.get("proposal_hash") != proposal_hash or current["status"] not in ("pending", "answering")
                or current["generation"] != self.backend.generation or not self.backend.alive
                or task.get("turn_id") != params.get("turnId") or task.get("verified_generation") != self.backend.generation):
            raise BridgeError("File approval proposal is stale or unknown; refresh codex_review_request and review its proposal_hash", code='stale_proposal')
        self.validate_file_paths(req, params)

    async def respond(self, request_id, response, *, human=False, proposal_hash=None, review_source=None):
        if request_id in self.response_tasks:
            raise BridgeError("Request is already being answered; refresh codex_requests", code='stale_request')
        self.review_request(request_id)
        running = asyncio.create_task(self._respond(request_id, response, human=human, proposal_hash=proposal_hash, review_source=review_source))
        self.response_tasks[request_id] = running
        running.add_done_callback(lambda task: self._finish_task(self.response_tasks, request_id, task))
        return await asyncio.shield(running)

    async def _respond(self, request_id, response, *, human=False, proposal_hash=None, review_source=None):
        req = self.review_request(request_id)
        params = self.raw_requests.get(request_id)
        if params is None:
            raise BridgeError("Live request is unavailable; resume and request fresh approval", code='state_conflict')
        positive_file = req["kind"] == "file_approval" and isinstance(response, dict) and response.get("decision") == "accept"
        if positive_file:
            self.validate_proposal(req, proposal_hash)
        result = self.validate_response(req, params, response)
        denial = self.is_denial(req["kind"], result)
        authority.decision_source(self, req, result, human=human, review_source=review_source)
        with self.store.transaction():
            claimed = self.store.update("requests", request_id, {"status": "answering"}, expected={"status": "pending"})
            if claimed is None:
                raise BridgeError("Request is resolved, expired, or disconnected; refresh codex_requests", code='state_conflict')
        stale, decision_receipt = False, None
        def before_send():
            nonlocal stale, decision_receipt
            current = self.store.get("requests", request_id)
            task = self.task(req["task_id"])
            if (current["status"] != "answering" or current["generation"] != self.backend.generation
                    or not self.backend.alive or request_id not in self.raw_requests
                    or (params.get("turnId") is not None and (params["turnId"] != task.get("turn_id")
                        or task.get("turn_status") in TERMINAL))):
                raise BridgeError("Request is resolved, expired, or disconnected; refresh codex_requests", code='state_conflict')
            if positive_file:
                try:
                    self.validate_proposal(req, proposal_hash)
                except BridgeError:
                    stale = True
                    raise
            if not denial:
                if task.get("verified_generation") != self.backend.generation or task["id"] not in self.loaded:
                    stale = True
                    raise BridgeError("Native thread scope is unverified; resume before answering this request", code='state_conflict')
                if req["kind"] == "permissions":
                    try:
                        self.validate_response(req, self.raw_requests[request_id], result)
                    except BridgeError:
                        stale = True
                        raise
                if req["kind"] == "parent_question":
                    try:
                        self.validate_response(current, self.raw_requests[request_id], response)
                    except BridgeError:
                        stale = True
                        raise
            try:
                source = authority.decision_source(self, current, result, human=human, review_source=review_source)
            except BridgeError:
                stale = True
                raise
            decision = "answer" if req["kind"] == "parent_question" else response.get("decision") or response.get("action") or ("deny" if denial else "grant" if req["kind"] == "permissions" else "answer")
            with self.store.transaction():
                decision_receipt = save_receipt(self.store, authority.receipt_fields(current, params, source, decision))
        try:
            await self.backend.respond(req["rpc_id"], result, before_send=before_send)
        except Exception:
            with self.store.transaction():
                self.store.update("requests", request_id, {"status": "pending" if stale else "expired"}, expected={"status": "answering"})
                if not stale:
                    self.raw_requests.pop(request_id, None)
            await self.wake()
            raise
        with self.store.transaction():
            self.store.update("requests", request_id, {"status": "answered"}, expected={"status": "answering"})
            # Answers (especially secrets) are not journaled. Codex may persist
            # them itself as part of its conversation history.
            decision_receipt = {**decision_receipt, "delivery": "sent"}
            self.store.db.execute("UPDATE decision_receipts SET data=? WHERE id=?", (json.dumps(decision_receipt), decision_receipt["receipt_id"]))
            receipt = {"request_id": request_id, "kind": req["kind"], "human_review": human,
                       "receipt_id": decision_receipt["receipt_id"], "decision_source": decision_receipt["decision_source"], "decision": decision_receipt["decision"],
                       "decided_at": decision_receipt["decided_at"]}
            if req["kind"] == "file_approval":
                receipt.update(generation=req["generation"], thread_id=params.get("threadId"),
                    turn_id=params.get("turnId"), item_id=params.get("itemId"),
                    proposal_revision=req.get("proposal_revision"), proposal_hash=req.get("proposal_hash"))
            self.store.event(req["task_id"], "bridge/response_sent", receipt)
            self.lifecycle.answered(req["task_id"], params.get("turnId"), self.blocking_requests(req["task_id"]))
        self.raw_requests.pop(request_id, None)
        await self.wake()
        return {"request_id": request_id, "status": "sent", "receipt": decision_receipt, "note": "Codex remains responsible for enforcing its policies"}

    def validate_response(self, req, params, response):
        if not isinstance(response, dict):
            raise BridgeError("Response must be an object", code='invalid_response')
        kind = req["kind"]
        if kind != "parent_question" and isinstance(response.get("decision"), dict):
            raise BridgeError("Structured decisions are only accepted for ask_parent questions", code="invalid_response")
        if kind in ("command_approval", "file_approval"):
            if set(response) != {"decision"} or response["decision"] not in ("accept", "decline", "cancel"):
                raise BridgeError("Only one-shot accept, decline, or cancel decisions are supported", code='invalid_response')
            offered = params.get("availableDecisions")
            if offered is not None and response["decision"] not in offered:
                raise BridgeError("Decision was not offered by Codex", code='invalid_response')
            if kind == "file_approval" and response["decision"] == "accept":
                self.validate_file_paths(req, params)
            return response
        if kind == "parent_question":
            return decisions.answer(self, req, response)
        if kind == "user_input":
            if any(q.get("isSecret") for q in params["questions"]):
                raise BridgeError("Secret answers are not accepted by the bridge. Use Codex's native authentication or the provider's browser flow", code='invalid_response')
            if set(response) != {"answers"} or not isinstance(response["answers"], dict):
                raise BridgeError("Use answers: {question_id: {answers: [text]}}", code='invalid_response')
            expected = {q["id"] for q in params["questions"]}
            if set(response["answers"]) != expected:
                raise BridgeError("Answer every question id exactly once", code='invalid_response')
            for answer in response["answers"].values():
                if (not isinstance(answer, dict) or set(answer) != {"answers"}
                    or not isinstance(answer["answers"], list) or not answer["answers"]
                    or any(not isinstance(s, str) for s in answer["answers"])):
                    raise BridgeError("Each question requires an answers array of strings", code='invalid_response')
            return response
        if kind == "permissions":
            if set(response) - {"permissions", "scope"} or response.get("scope", "turn") != "turn":
                raise BridgeError("Only turn-scoped permission subsets are supported", code='invalid_response')
            grants = self.normalize_permissions(response.get("permissions", {}))
            if not isinstance(grants, dict) or not self.subset(grants, params["permissions"]):
                raise BridgeError("Grant must be a subset of the requested permissions", code='invalid_response')
            task = self.task(req["task_id"])
            fs = grants.get("fileSystem") or {}
            paths = list(fs.get("read") or []) + list(fs.get("write") or [])
            for entry in fs.get("entries") or []:
                if entry["path"].get("type") != "path":
                    raise BridgeError("Special/glob permission grants are not exposed", code='invalid_response')
                paths.append(entry["path"]["path"])
            if grants:
                project = Path(task["project"]).resolve()
                cwd = Path(task.get("native_cwd", task["project"])).resolve()
                if not within(cwd, project) or (params.get("cwd") is not None and not same_path(params["cwd"], cwd)):
                    raise BridgeError("Permission request has an unverified native working directory", code='invalid_response')
                for path in paths:
                    if PureWindowsPath(path).drive and not PureWindowsPath(path).is_absolute():
                        raise BridgeError("Drive-relative approval paths are not supported", code='invalid_response')
                    if "\x00" in path or (os.name != "nt" and (PureWindowsPath(path).drive or "\\" in path)):
                        raise BridgeError("Filesystem grants must stay within this task's project", code='invalid_response')
                    try:
                        allowed = within((cwd / path).resolve(), project)
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise BridgeError("Permission path cannot be resolved", code='invalid_response') from exc
                    if not allowed:
                        raise BridgeError("Filesystem grants must stay within this task's project", code='invalid_response')
            return {"permissions": grants, "scope": "turn"}
        if kind == "elicitation":
            if set(response) - {"action", "content"} or response.get("action") not in ("accept", "decline", "cancel"):
                raise BridgeError("Elicitation requires accept, decline, or cancel", code='invalid_response')
            if response["action"] != "accept":
                return {"action": response["action"], "content": None}
            if params.get("mode") == "url":
                return {"action": "accept", "content": None}
            from .tools import validate
            validate(response.get("content"), params.get("requestedSchema", {}))
            return response
        raise BridgeError("Unsupported response kind", code='invalid_response')

    def validate_file_paths(self, req, params):
        task = self.task(req["task_id"])
        project = Path(task["project"]).resolve()
        cwd = Path(task.get("native_cwd", task["project"])).resolve()
        if not within(cwd, project) or (params.get("cwd") is not None and (
                not isinstance(params["cwd"], str) or not same_path(params["cwd"], cwd))):
            raise BridgeError("File approval has an unverified native working directory", code='scope_unverified')
        changes = params.get("proposedChanges")
        if not isinstance(changes, list) or not changes:
            raise BridgeError("File approval lacks a known in-project patch", code='scope_unverified')
        paths = []
        for change in changes:
            if not isinstance(change, dict) or set(change) - {"path", "kind", "diff"}:
                raise BridgeError("File approval has an unknown change shape", code='scope_unverified')
            kind = change.get("kind")
            if (not isinstance(kind, dict) or kind.get("type") not in ("add", "delete", "update")
                    or set(kind) - {"type", "move_path"} or not isinstance(change.get("diff"), str)):
                raise BridgeError("File approval has an unknown change kind or shape", code='scope_unverified')
            paths.append(change.get("path"))
            if kind.get("move_path") is not None:
                if kind["type"] != "update":
                    raise BridgeError("File approval has an unknown rename shape", code='scope_unverified')
                paths.append(kind["move_path"])
        if params.get("grantRoot") is not None:
            paths.append(params["grantRoot"])
        for value in paths:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise BridgeError("File approval has an unknown affected path", code='scope_unverified')
            if PureWindowsPath(value).drive and not PureWindowsPath(value).is_absolute():
                raise BridgeError("Drive-relative approval paths are not supported", code='scope_unverified')
            if os.name != "nt" and (PureWindowsPath(value).drive or "\\" in value):
                raise BridgeError("File approval path is outside the native project path syntax", code='scope_unverified')
            try:
                allowed = within((cwd / value).resolve(), project)
            except (OSError, RuntimeError, ValueError) as exc:
                raise BridgeError("File approval affected path cannot be resolved", code='scope_unverified') from exc
            if not allowed:
                raise BridgeError("File approval would grant access outside this project", code='scope_unverified')

    @staticmethod
    def normalize_permissions(grants):
        if grants is None or grants is False or grants == []:
            return {}
        if not isinstance(grants, dict) or set(grants) - {"network", "fileSystem"}:
            raise BridgeError("Unsupported permission keys or shape", code='invalid_response')
        result = {}
        has_grant = False
        network = grants.get("network")
        if network is not None and network is not False and network != [] and network != {}:
            if not isinstance(network, dict) or set(network) - {"enabled"}:
                raise BridgeError("Unsupported network permission shape", code='invalid_response')
            enabled = network.get("enabled")
            if enabled is True:
                result["network"] = {"enabled": True}
                has_grant = True
            elif enabled is not None and enabled is not False:
                raise BridgeError("Network enabled must be a boolean or null", code='invalid_response')
        fs = grants.get("fileSystem")
        if fs is not None and fs is not False and fs != [] and fs != {}:
            if not isinstance(fs, dict) or set(fs) - {"read", "write", "entries"}:
                raise BridgeError("Unsupported filesystem permission shape", code='invalid_response')
            positive = {}
            for key, value in fs.items():
                if value is None or value is False or value == [] or value == {}:
                    continue
                if not isinstance(value, list):
                    raise BridgeError("Filesystem permissions require arrays", code='invalid_response')
                if key in ("read", "write"):
                    if any(not isinstance(p, str) or not p for p in value):
                        raise BridgeError("Filesystem permissions require nonempty paths", code='invalid_response')
                    positive[key] = value
                    has_grant = True
                else:
                    entries = []
                    for entry in value:
                        if (not isinstance(entry, dict) or set(entry) != {"path", "access"}
                                or entry.get("access") not in ("read", "write", "deny")
                                or not isinstance(entry.get("path"), dict)
                                or set(entry["path"]) != {"type", "path"}
                                or entry["path"].get("type") != "path"
                                or not isinstance(entry["path"].get("path"), str) or not entry["path"]["path"]):
                            raise BridgeError("Unsupported filesystem entry shape", code='invalid_response')
                        entries.append(entry)
                        has_grant |= entry["access"] != "deny"
                    if entries:
                        positive[key] = entries
            if positive:
                result["fileSystem"] = positive
        return result if has_grant else {}

    @staticmethod
    def is_denial(kind, result):
        """True for a validated response that grants nothing."""
        if kind in ("command_approval", "file_approval"):
            return result.get("decision") in ("decline", "cancel")
        if kind == "permissions":
            return not Service.normalize_permissions(result.get("permissions", {}))
        if kind == "elicitation":
            return result.get("action") in ("decline", "cancel")
        return False

    @staticmethod
    def subset(granted, requested):
        if isinstance(granted, dict):
            return isinstance(requested, dict) and all(k in requested and Service.subset(v, requested[k]) for k, v in granted.items())
        if isinstance(granted, list):
            return isinstance(requested, list) and all(v in requested for v in granted)
        return granted == requested or (granted is False and requested is True)

    async def close(self):
        self.shutting_down = True
        await self.scheduler.close()
        self.cancel_budget_timers()
        for timer in self.reconciliation_timers.values():
            timer.cancel()
        self.reconciliation_timers.clear()
        running = list(self.operation_tasks.values()) + list(self.response_tasks.values()) + list(self.reconciliation_tasks) + list(self.budget_tasks.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self.managed_worktrees.close()
        self.resources.disconnect()
        for _, task in self.store.records("tasks", light=True, recovery=True):
            processes.capture(self, task)
        await self.backend.close()
