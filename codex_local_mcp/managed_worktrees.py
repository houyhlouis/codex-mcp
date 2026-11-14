"""Broker integration for the trusted, opt-in Git helper."""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from . import worktrees as git, workspace_processes, procscan
from .config import checked_worktree_root
from .evidence import artifact, file_bytes, put
from .lifecycle import ACTIVE, TERMINAL
from .security import BridgeError, project_path, within


class WorktreeOperationError(BridgeError):
    def __init__(self, error, *, before_write, path=None):
        super().__init__(str(error), code="worktree_" + error.code, uncertain=not before_write,
            details={**error.details, **({"path": str(path)} if path else {})})
        self.before_write = before_write


def shared_resources(project):
    worktree = project.get("worktree") or {}
    return [{"kind": "git_common_dir", "path": worktree["common_dir"],
        "task_access": "not_granted", "per_thread_read_only_supported": False}] if worktree else []


class ManagedWorktrees:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.jobs, self.calls = set(), set()

    def enabled(self, action):
        if action != "inspect" and not self.service.config.worktrees:
            raise BridgeError("Managed worktree actions require worktrees = true in the settings file; inspect remains available",
                code="worktrees_disabled")
        if self.service.shutting_down:
            raise BridgeError("Broker is stopping; reconnect before another worktree action", code="state_conflict")

    async def run(self, function, *args, mutating=False, path=None, **kwargs):
        job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.jobs.add(job)
        try:
            result = await asyncio.shield(job)
        except asyncio.CancelledError:
            # Git must settle before releasing admission or closing SQLite.
            await asyncio.gather(job, return_exceptions=True)
            raise BridgeError("Worktree operation was interrupted; inspect the path before any retry",
                code="worktree_interrupted", details={"path": str(path)} if path else {}) from None
        except git.WorktreeError as error:
            before = not mutating
            if mutating and not error.details:
                before = error.code not in ("git_failed",)
            elif mutating and function is git.create and error.code == "locked":
                before = all(error.details.get(k) is False for k in ("directory_exists", "registered", "branch_exists"))
            elif mutating and function is git.cleanup:
                before = error.code in ("dirty", "unmerged_commits", "filters_configured", "not_managed") or (
                    error.code == "locked" and error.details.get("registered") is True and error.details.get("directory_exists") is True)
            raise WorktreeOperationError(error, before_write=before, path=path) from error
        finally:
            self.jobs.discard(job)
        if self.service.shutting_down:
            raise BridgeError("Worktree operation was interrupted; inspect the path before any retry",
                code="worktree_interrupted", details={"path": str(path)} if path else {})
        return result

    async def close(self):
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)
        calls = [task for task in self.calls if task is not asyncio.current_task()]
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)

    def boundaries(self, report, *, source_exists=False):
        config = self.service.config
        source = project_path(config, report["repo"], allow_missing=not source_exists)
        target = project_path(config, report["path"], allow_missing=True)
        common = project_path(config, report["common_dir"])
        if within(target, source) or within(source, target) or within(target, common) or within(common, target):
            raise BridgeError("Worktree target must not overlap its source repository or common Git directory", code="worktree_unsafe_config")
        return target

    def project(self, project_id):
        project = self.store.get("projects", project_id)
        if not project.get("worktree"):
            raise BridgeError("Project is not a registered managed worktree; inspect its helper-created path first", code="worktree_not_managed")
        return project

    async def proposed(self, repo, project_id, name, base):
        selected = self.service.select_project(repo, project_id, register=False)
        root = checked_worktree_root(self.service.config.worktree_root and str(self.service.config.worktree_root))
        if root:
            project_path(self.service.config, root, allow_missing=True)
        result = await self.run(git.plan, selected["path"], name, base, root=root)
        self.boundaries(result, source_exists=True)
        if len(json.dumps(result).encode()) > 65536:
            raise BridgeError("The worktree plan exceeds the 65536-byte receipt limit; prepare it manually", code="worktree_unsafe_config")
        return result

    def registered(self, report):
        path = self.boundaries(report)
        # Reuse the same project id as normal directory selection.
        from .store import project_record
        descriptor = project_record(path)
        present = self.store.db.execute("SELECT id FROM projects WHERE id=?", (descriptor["id"],)).fetchone()
        info = {k: report[k] for k in ("repo", "common_dir", "branch", "path")}
        info["base"] = report["recorded_base"]
        with self.store.transaction():
            if present:
                current = self.store.get("projects", descriptor["id"])
                fields = {"worktree": info, "removed": False, "worktree_state": "ready" if report.get("managed") and report.get("registration") == "ok" else "incomplete"}
                if report.get("registration") == "unchecked" and report.get("managed") and report.get("directory_exists") and current.get("worktree") == info:
                    fields["worktree_state"] = current.get("worktree_state", "incomplete")
                return self.store.update("projects", current["id"], fields) if any(current.get(k) != v for k, v in fields.items()) else current
            return self.store.save("projects", {**descriptor, "worktree": info, "removed": False,
                "worktree_state": "ready" if report.get("managed") and report.get("registration") == "ok" else "incomplete"})

    @staticmethod
    def overlaps(left, right):
        return within(Path(left), Path(right)) or within(Path(right), Path(left))

    def owners(self, project, key=None, inspections=()):
        claims = [c for c in self.store.claims() if self.overlaps(c["project"], project)]
        key = key or self.service.current_operation.get()
        ignored = {key}
        for record in inspections:
            job = self.service.operation_tasks.get(record["id"])
            row = self.store.db.execute("""SELECT d.data FROM operations o JOIN operation_details d ON d.id=o.id
                WHERE o.id=? AND o.state='uncertain'""", (record["id"],)).fetchone()
            if row and row["data"] == record["data"] and (not job or job.done()):
                ignored.add(record["id"])
        admissions = [r for r in self.reservations() if r["operation_id"] not in ignored and self.overlaps(r["project"], project)]
        running = self.store.db.execute("""SELECT DISTINCT t.id,t.project FROM tasks t JOIN operations o
            ON o.task_id=t.id WHERE o.state='running' AND o.id!=?""", (key or "",)).fetchall()
        return claims, admissions, [r["id"] for r in running if self.overlaps(r["project"], project)]

    def free(self, project, key=None, inspections=()):
        claims, admissions, running = self.owners(project, key, inspections)
        if claims or admissions:
            raise self.service.workspace_busy("Worktree is reserved; finish its work before changing its directory", claims, admissions)
        if running:
            raise BridgeError("Worktree has a running control operation; wait for it to finish before cleanup", code="workspace_busy", ids={"task_ids": running[:50]})

    def reservations(self):
        for row in self.store.db.execute("SELECT * FROM admissions"):
            record = dict(row)
            for path in self.service.project_operations.get(row["operation_id"], [row["project"]]):
                yield {**record, "project": path}

    async def reserve(self, path, *, key=None, inspections=()):
        key = key or self.service.current_operation.get()
        if not key:
            raise BridgeError("Worktree changes require an operation key", code="invalid_arguments")
        async with self.service.admission_lock:
            self.free(path, key, inspections)
            with self.store.transaction():
                self.store.db.execute("INSERT INTO admissions VALUES (?,?,?)", (key, "worktree:" + key, str(path)))
                self.service.project_operations[key] = [str(path)]
        return key

    async def release(self, key=None):
        key = key or self.service.current_operation.get()
        async with self.service.admission_lock:
            with self.store.transaction():
                self.store.db.execute("DELETE FROM admissions WHERE operation_id=?", (key,))
                self.service.project_operations.pop(key, None)

    @asynccontextmanager
    async def common_gate(self, common):
        common = str(project_path(self.service.config, common))
        key = self.service.current_operation.get() or "worktree-check:" + str(uuid.uuid4())
        def check():
            claims, admissions, running = self.owners(common, key)
            if claims or admissions or running:
                task_ids = sorted({c["task_id"] for c in claims} | set(running) | {
                    a["family_id"] for a in admissions if not a["family_id"].startswith("worktree:")})
                suffix = " Blocking tasks: " + ", ".join(task_ids[:20]) + "." if task_ids else " Another worktree helper is using the shared Git directory."
                raise BridgeError("Worktree changes need the main checkout to be idle." + suffix,
                    code="workspace_busy", ids={"task_ids": task_ids[:20]}, details={"common_dir": common})
        async with self.service.admission_lock:
            check()
            owned = key in self.service.project_operations
            if owned:
                self.service.project_operations[key].append(common)
            else:
                with self.store.transaction():
                    self.store.db.execute("INSERT INTO admissions VALUES (?,?,?)", (key, "worktree:" + key, common))
                    self.service.project_operations[key] = [common]
        try:
            scanned = await self.service.scan_workspace(common)
            async with self.service.admission_lock:
                check()
                if workspace_processes.outcome(scanned) != "clear":
                    reason = workspace_processes.UNAVAILABLE_MESSAGE if workspace_processes.outcome(scanned) == "unavailable" else procscan.describe(scanned)
                    raise BridgeError("Worktree changes need the main checkout to be idle. " + reason,
                        code="workspace_busy", details={"common_dir": common, "os_process_check": workspace_processes.summary(scanned)})
            yield
        finally:
            async with self.service.admission_lock:
                if owned:
                    self.service.project_operations[key].remove(common)
                else:
                    with self.store.transaction():
                        self.store.db.execute("DELETE FROM admissions WHERE operation_id=?", (key,))
                        self.service.project_operations.pop(key, None)

    def intent(self, report, action):
        key = self.service.current_operation.get()
        with self.store.transaction():
            row = self.store.db.execute("SELECT data FROM operation_details WHERE id=?", (key,)).fetchone()
            if row:
                self.store.db.execute("UPDATE operation_details SET data=? WHERE id=?", (json.dumps({**json.loads(row[0]),
                    "worktree": {**{k: report[k] for k in ("path", "repo", "common_dir", "branch", "base")}, "action": action}}), key))
        self.service.local_write()

    def retained(self, key):
        row = self.store.db.execute("""SELECT o.state,d.data FROM operations o JOIN operation_details d ON d.id=o.id
            WHERE o.id=? AND d.name='codex_worktrees'""", (key,)).fetchone()
        if not row or row["state"] not in ("uncertain", "running"):
            return False
        intent = json.loads(row["data"]).get("worktree")
        return bool(intent and not intent.get("reservation_released"))

    def restore(self):
        for row in self.store.db.execute("""SELECT o.id,d.data FROM operations o JOIN operation_details d ON d.id=o.id
                WHERE o.state='uncertain' AND d.name='codex_worktrees'"""):
            intent = json.loads(row["data"]).get("worktree")
            if not intent or intent.get("reservation_released"):
                continue
            path = str(project_path(self.service.config, intent["path"], allow_missing=True))
            self.store.db.execute("INSERT INTO admissions VALUES (?,?,?)", (row["id"], "worktree:" + row["id"], path))
            self.service.project_operations[row["id"]] = [path]

    def pending(self, path):
        rows = self.store.db.execute("""SELECT o.id,d.data FROM operations o JOIN operation_details d ON d.id=o.id
            WHERE o.state IN ('uncertain','running') AND d.name='codex_worktrees'
            AND json_extract(d.data,'$.worktree.path')=?
            AND COALESCE(json_extract(d.data,'$.worktree.reservation_released'),0)=0""", (str(path),)).fetchall()
        return rows

    async def release_inspected(self, path, report, pending):
        complete = report.get("managed") and report.get("registration") == "ok" and report.get("directory_exists")
        inactive = report.get("directory_exists") is False and report.get("registered") is False
        released = []
        if complete or inactive:
            async with self.service.admission_lock:
                with self.store.transaction():
                    for row in pending:
                        job = self.service.operation_tasks.get(row["id"])
                        if job and not job.done():
                            continue
                        latest = self.store.db.execute("SELECT data FROM operation_details WHERE id=?", (row["id"],)).fetchone()
                        if not latest or latest["data"] != row["data"] or not self.retained(row["id"]):
                            continue
                        data = json.loads(row["data"])
                        data["worktree"]["reservation_released"] = True
                        self.store.db.execute("UPDATE operation_details SET data=? WHERE id=?", (json.dumps(data), row["id"]))
                        self.store.db.execute("DELETE FROM admissions WHERE operation_id=?", (row["id"],))
                        self.service.project_operations.pop(row["id"], None)
                        released.append(row["id"])
        return released

    async def create(self, proposed, plan_id):
        if proposed["plan_id"] != plan_id:
            raise BridgeError("Worktree plan changed; run plan again and review the new plan_id", code="worktree_plan_mismatch")
        target = self.boundaries(proposed, source_exists=True)
        await self.reserve(target)
        async with self.common_gate(proposed["common_dir"]):
            self.intent(proposed, "create")
            report = await self.run(git.create, proposed, lock_dir=Path(proposed["common_dir"]) / git.METADATA / "locks", mutating=True, path=target)
        descriptor = self.registered(report)
        return {**report, "project_id": descriptor["id"], "worktree": descriptor["worktree"],
            "shared_resources": shared_resources(descriptor)}

    async def inspect(self, project_id, path, offset, limit):
        if bool(project_id) == bool(path):
            raise BridgeError("Inspect requires exactly one project_id or absolute path", code="invalid_arguments")
        prior = self.project(project_id) if project_id else None
        target = project_path(self.service.config, prior["path"] if prior else path, allow_missing=True)
        pending = self.pending(target)
        if prior and prior.get("removed") and not os.path.lexists(target) and not pending:
            report = {**prior["worktree"], "removed": True, "managed": False, "registration": "removed"}
            descriptor = prior
        else:
            fallback = prior["worktree"] if prior else (json.loads(pending[0]["data"])["worktree"] if pending else None)
            key, reserved = "worktree-inspect:" + str(uuid.uuid4()), False
            try:
                await self.reserve(target, key=key, inspections=pending)
                reserved = True
                scanned = await self.service.scan_workspace(str(target))
                async with self.service.admission_lock:
                    self.free(target, key, pending)
                    if workspace_processes.outcome(scanned) != "clear":
                        reason = workspace_processes.UNAVAILABLE_MESSAGE if workspace_processes.outcome(scanned) == "unavailable" else procscan.describe(scanned)
                        raise BridgeError(reason, code="workspace_busy")
                common = (fallback or (await self.run(lambda: git._load(target)[1])))["common_dir"]
                async with self.common_gate(common):
                    report = await self.run(self.inspect_remains, target, fallback)
                    report["working_tree_checked"] = bool(report.get("directory_exists") and report.get("registration") != "incomplete")
                    report["working_tree_reason"] = None if report["working_tree_checked"] else "No available managed worktree directory was checked."
            except BridgeError as error:
                if error.code != "workspace_busy":
                    raise
                report = await self.run(self.inspect_metadata, target, fallback, str(error))
                report["working_tree_error"] = error.as_dict()
            finally:
                if reserved:
                    await self.release(key)
            descriptor = self.registered(report)
            if report.get("removed"):
                descriptor = self.store.update("projects", descriptor["id"], {"removed": True})
        released = await self.release_inspected(target, report, pending)
        rows = self.store.db.execute("SELECT id,revision,projection FROM tasks WHERE project=? ORDER BY rowid LIMIT ? OFFSET ?",
            (str(target), limit + 1, offset)).fetchall()
        tasks = [self.store._projection("tasks", r) for r in rows[:limit]]
        tasks = [{k: t.get(k) for k in ("id", "root_task_id", "parent_task_id", "status", "turn_id", "archived", "deleted")} for t in tasks]
        claims = [c for c in self.store.claims() if self.overlaps(c["project"], target)]
        return {**report, "project_id": descriptor["id"], "shared_resources": shared_resources(descriptor),
            "tasks": tasks, "tasks_total": self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE project=?", (str(target),)).fetchone()[0],
            "workspace_claims": claims[offset:offset + limit], "workspace_claims_total": len(claims),
            "pending_operation_ids": [row["id"] for row in pending][:200], "released_operation_ids": released[:200],
            "next_offset": offset + limit if len(rows) > limit or len(claims) > offset + limit else None}

    @staticmethod
    def inspect_metadata(target, fallback, reason):
        if os.path.lexists(git._locator(target)):
            _, record = git._load(target)
        elif fallback:
            record = fallback
        else:
            raise git.WorktreeError("not_managed", "No intact private helper receipt was found for this worktree.")
        state = git._exists(record)
        entry = state.pop("entry", None)
        return {**{k: record[k] for k in ("repo", "common_dir", "path", "branch")}, **state,
            "recorded_base": record["base"], "managed": bool(entry and entry.get("branch") == "refs/heads/" + record["branch"] and state["branch_exists"]),
            "registration": "locked" if entry and "locked" in entry else "unchecked", "dirty": None, "untracked": None, "untracked_count": None,
            "working_tree_checked": False, "working_tree_reason": reason}

    @staticmethod
    def inspect_remains(target, fallback=None):
        # A verified receipt also identifies a failed creation before checkout.
        # Registration permits explicit cleanup, not task admission.
        try:
            return git.inspect(target)
        except git.WorktreeError as error:
            if error.code != "not_managed":
                raise
            if not error.details and fallback and not os.path.lexists(target) and not os.path.lexists(git._locator(target)):
                common = Path(fallback["common_dir"])
                registered = any(os.path.normcase(entry.get("worktree", "")) == os.path.normcase(str(target)) for entry in git._entries(common))
                if not registered:
                    return {**fallback, "recorded_base": fallback["base"], "managed": False, "removed": True,
                        "directory_exists": False, "registered": False, "branch_exists": bool(git._branch(common, fallback["branch"])),
                        "registration": "removed"}
            if not error.details:
                raise
            _, record = git._load(target)
            return {**{k: record[k] for k in ("repo", "common_dir", "name", "path", "branch", "plan_id")},
                **error.details, "recorded_base": record["base"], "managed": False, "registration": "incomplete"}

    async def handoff(self, task_id):
        task = self.service.task(task_id, light=True)
        project = self.project(task["project_id"])
        key = "worktree-handoff:" + str(uuid.uuid4())
        await self.reserve(project["path"], key=key)
        try:
            task = self.service.task(task_id, light=True)
            project = self.project(task["project_id"])
            turn = self.service.lifecycle.turn(task_id, task.get("turn_id"))
            if task["status"] in ACTIVE or not turn or turn["status"] not in TERMINAL:
                raise BridgeError("Wait for the task's turn to finish before requesting a worktree handoff", code="state_conflict")
            before = (task["thread_id"], task["turn_id"], project["revision"])
            async with self.common_gate(project["worktree"]["common_dir"]):
                report = await self.run(self.locked_handoff, project, path=project["path"])
            current = self.service.task(task_id, light=True)
            latest = self.project(project["id"])
            if current["status"] in ACTIVE or before != (current["thread_id"], current["turn_id"], latest["revision"]):
                raise BridgeError("Task or worktree changed during handoff; wait for it to rest and try again", code="state_conflict")
            data, redactions = file_bytes(report["patch"])
            with self.store.transaction():
                ref = artifact(self.store.db, task_id, task["turn_id"], data, redaction_count=redactions)
                details = {"turnId": task["turn_id"], "project_id": project["id"], "artifact": ref,
                    "base": report["base"], "head": report["head"], "files_count": len(report["files"]), "binary": report["binary"]}
                seq = self.store.event(task_id, "bridge/worktree_handoff", details)
                put(self.store.db, task_id, task["turn_id"], "artifacts", ref["artifact_id"], seq,
                    {**ref, "source": {"event_seq": seq, "method": "bridge/worktree_handoff", "task_id": task_id,
                        "thread_id": task["thread_id"], "turn_id": task["turn_id"]}})
                result = self.store.evidence.capture(task_id, task["turn_id"])
            self.service.schedule_budget_check(task_id)
            await self.service.wake()
            return {**details, **ref, "turn_id": task["turn_id"], "result_id": result["result_id"], "truncated": False,
                "artifact_ref": {"tool": "codex_get", "arguments": {"task_id": task_id, "view": "artifact", "artifact_id": ref["artifact_id"]}}}
        finally:
            await self.release(key)

    @staticmethod
    def locked_handoff(project):
        common = Path(project["worktree"]["common_dir"])
        with git._lock(common, common / git.METADATA / "locks"):
            return git.handoff(project["path"], max_bytes=git.HANDOFF_BYTES)

    async def cleanup(self, project_id, keep_branch):
        project = self.project(project_id)
        if project.get("removed"):
            raise BridgeError("Project was already removed; inspect it before another cleanup", code="worktree_not_managed")
        target = project_path(self.service.config, project["path"], allow_missing=True)
        await self.reserve(target)
        snapshot = project["revision"]
        scanned = await self.service.scan_workspace(str(target))
        async with self.service.admission_lock:
            self.free(target)
            if self.project(project_id)["revision"] != snapshot:
                raise BridgeError("Worktree registration changed during cleanup; inspect it before retrying", code="state_conflict")
            if workspace_processes.outcome(scanned) != "clear":
                message = workspace_processes.UNAVAILABLE_MESSAGE if workspace_processes.outcome(scanned) == "unavailable" else (
                    procscan.describe(scanned) + " Stop them with kill PID or wait until they end before cleanup.")
                raise BridgeError(message, code="workspace_busy", details={"os_process_check": workspace_processes.summary(scanned)})
        async with self.common_gate(project["worktree"]["common_dir"]):
            self.intent(project["worktree"], "cleanup")
            report = await self.run(git.cleanup, target, lock_dir=Path(project["worktree"]["common_dir"]) / git.METADATA / "locks",
                keep_branch=keep_branch, mutating=True, path=target)
        with self.store.transaction():
            current = self.project(project_id)
            self.store.update("projects", project_id, {"removed": True}, expected={"revision": current["revision"]})
        return {**report, "project_id": project_id}

    async def dispatch(self, action, repo=None, project_id=None, name=None, base="HEAD", plan_id=None,
                       path=None, task_id=None, keep_branch=False, offset=0, limit=50):
        self.enabled(action)
        caller = asyncio.current_task()
        self.calls.add(caller)
        try:
            if action in ("plan", "create"):
                proposed = await self.proposed(repo, project_id, name, base)
                result = ({**proposed, "shared_resources": shared_resources({"worktree": proposed})} if action == "plan"
                    else await self.create(proposed, plan_id))
            elif action == "inspect":
                result = await self.inspect(project_id, path, offset, limit)
            elif action == "handoff":
                result = await self.handoff(task_id)
            else:
                result = await self.cleanup(project_id, keep_branch)
            return {"action": action, **result}
        finally:
            self.calls.discard(caller)
