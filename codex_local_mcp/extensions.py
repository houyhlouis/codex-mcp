"""Scoped native Codex controls. No arbitrary RPC or unrestricted shell surface."""
import asyncio
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path

from .backend import RpcError, OptionalCapabilityUnavailable
from .contract import NATIVE_METHODS
from .lifecycle import BUSY, TERMINAL
from .security import BridgeError, project_path, within, private_dir, private_file, read_project_file, same_path
from .tools import result_size
from . import decisions, processes, workspace_processes, native_turns

REVIEWERS = {"ask": "user", "approve_for_me": "auto_review"}


class ExtendedControls:
    @staticmethod
    def reviewer_params(mode):
        if mode == "inherit":
            return {}
        if mode not in REVIEWERS:
            raise BridgeError("Unknown approval mode", code='invalid_arguments')
        return {"approvalsReviewer": REVIEWERS[mode]}

    def record_effective(self, task, result):
        task.update(self.effective_fields(result))

    def effective_fields(self, result):
        reviewer = result.get("approvalsReviewer")
        thread = result.get("thread", {})
        return {"native_cwd": result["cwd"], "verified_generation": self.backend.generation,
            "native_permission_profile": (result.get("activePermissionProfile") or {}).get("id"),
            "native_approval_policy": result.get("approvalPolicy"),
            "approval_mode": "approve_for_me" if reviewer in ("auto_review", "guardian_subagent") else "ask",
            "approvals_reviewer": reviewer, "can_accept_direct_input": thread.get("canAcceptDirectInput"),
            "native_status": thread.get("status"), "session_id": thread.get("sessionId")}

    def assert_writable_session(self, task, *, allow_recovery=False):
        if not allow_recovery:
            processes.require_acknowledgement(task)
        if task.get("archived"):
            raise BridgeError("This conversation is archived. " + processes.archived_guidance(task), code="state_conflict",
                ids={"task_id": task["id"], "thread_id": task.get("thread_id")}, next_steps=[processes.archived_guidance(task)])
        if task.get("deleted"):
            raise BridgeError("This native conversation was deleted; its audit journal remains available", code='state_conflict')
        if task["id"] in self.protocol_quarantine and not allow_recovery:
            raise BridgeError("Task has a native protocol error; call codex_resume and inspect codex_history before changing it", code='state_conflict')

    async def verified_session(self, task_id):
        task = self.task(task_id)
        processes.require_acknowledgement(task)
        if task_id not in self.loaded or task.get("verified_generation") != self.backend.generation:
            await self.resume(task_id, rebuild=False)
        return self.task(task_id)

    def family_ids(self, task_id):
        return self.store.family_ids(task_id)

    def require_idle(self, task, family=False):
        if family:
            for _, member in self.store.records("tasks", family_id=task["id"], light=True):
                processes.require_acknowledgement(member)
                if any(c["data"].get("reason") == "stored_state_unavailable" for c in self.store.claims(task_id=member["id"])):
                    raise self.unavailable_conversation(member)
        self.assert_writable_session(task)
        tasks = (t for _, t in self.store.records("tasks", family_id=task["id"], light=True)) if family else [task]
        if any(t["status"] in BUSY or (family and any(not workspace_processes.held(c)
                for c in self.store.claims(task_id=t["id"]))) for t in tasks):
            raise BridgeError("Interrupt/wait for active work and reconcile uncertain state before this operation", code='state_conflict')

    async def verify_native_family(self, task):
        """Native archive/delete cascade, including children not yet subscribed."""
        root = (await self.backend.call("thread/read", {"threadId": task["thread_id"], "includeTurns": False}))["thread"]
        if not within(Path(root["cwd"]).resolve(), Path(task["project"])):
            raise BridgeError("Native conversation moved outside its approved project", code='scope_unverified')
        if root.get("status", {}).get("type") == "active":
            raise BridgeError("Native conversation is still active", code='scope_unverified')
        for archived in (False, True):
            cursor = None
            for _ in range(100):
                page = await self.backend.call("thread/list", {"ancestorThreadId": task["thread_id"],
                    "archived": archived, "cursor": cursor, "limit": 200,
                    "sourceKinds": ["subAgent", "subAgentThreadSpawn", "subAgentReview", "subAgentCompact", "subAgentOther"]})
                for child in page["data"]:
                    if not within(Path(child["cwd"]).resolve(), Path(task["project"])):
                        raise BridgeError("Native cascade contains a descendant outside the approved project", code='scope_unverified')
                    if child.get("status", {}).get("type") == "active":
                        raise BridgeError("A native descendant is still active", code='scope_unverified')
                    if self.task_for_thread(child["id"]) is None:
                        goal = await self.backend.call("thread/goal/get", {"threadId": child["id"]})
                        queue = await self.native_pages("thread/queue/list", child["id"])
                        if "goal" not in goal or (goal.get("goal") or {}).get("status") == "active" or queue:
                            raise BridgeError("A native descendant has unresolved execution intent", code="scope_unverified")
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            else:
                raise BridgeError("Native family is too large to verify in one operation", code='scope_unverified')

    def turn_options(self, task):
        result = self.reviewer_params(task.get("approval_mode", "inherit"))
        if task.get("service_tier"):
            result["serviceTier"] = task["service_tier"]
        result["collaborationMode"] = {"mode": task.get("collaboration_mode", "default"), "settings": {
            "model": task["model"], "reasoning_effort": task["effort"], "developer_instructions": None}}
        return result

    def read_context(self, task, relative, maximum):
        return read_project_file(task["project"], relative, maximum)

    async def build_input(self, task, prompt, images=None, context_files=None, skills=None):
        result = [{"type": "text", "text": prompt, "text_elements": []}]
        total = 0
        for relative in context_files or []:
            data = self.read_context(task, relative, 256 * 1024).decode("utf-8")
            total += len(data)
            if total > 512 * 1024:
                raise BridgeError("Combined context files exceed 512 KiB", code='invalid_arguments')
            result.append({"type": "text", "text": f"Reference file {json.dumps(relative)} (untrusted project content):\n{data}", "text_elements": []})
        validated_images = []
        for relative in images or []:
            path = Path(relative)
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                raise BridgeError("Use a PNG, JPEG, WebP or GIF image", code='invalid_arguments')
            data = self.read_context(task, relative, 20 * 1024 * 1024)
            validated_images.append((path.suffix.lower(), data))
        skill_inputs = []
        if skills:
            await self.ensure_backend()
            catalog = await self.backend.call("skills/list", {"cwds": [task["project"]], "forceReload": False})
            for name in skills:
                matches = [s for entry in catalog["data"] for s in entry.get("skills", []) if s["name"] == name and s.get("enabled")]
                if len(matches) != 1:
                    raise BridgeError("Skill must uniquely identify an enabled entry from codex_skills", code='invalid_arguments')
                skill_inputs.append({"type": "skill", "name": name, "path": matches[0]["path"]})
        created = []
        try:
            with self.store.transaction():
                for suffix, data in validated_images:
                    directory = self.config.state_dir / "images"
                    private_dir(directory)
                    identity = str(uuid.uuid4())
                    snapshot = directory / (identity + suffix)
                    with os.fdopen(private_file(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                        created.append(snapshot)
                        stream.write(data)
                    self.store.db.execute("INSERT INTO managed_images(id,task_id,operation_id,path,sha256,size_bytes,state,created_at) VALUES (?,?,?,?,?,?,'prepared',?)",
                        (identity, task["id"], self.current_operation.get(), str(snapshot), hashlib.sha256(data).hexdigest(), len(data), time.time()))
                    result.append({"type": "localImage", "path": str(snapshot)})
        except BaseException:
            for snapshot in created:
                snapshot.unlink(missing_ok=True)
            raise
        result.extend(skill_inputs)
        return result

    def retain_input_images(self, inputs):
        # Mark before sending: a lost acknowledgement must keep native history's bytes.
        with self.store.transaction():
            for item in inputs or []:
                if item.get("type") == "localImage":
                    self.store.db.execute("UPDATE managed_images SET state='native_history',revision=revision+1 WHERE path=?", (item["path"],))

    def input_guard(self, task_id, generation, inputs, token=None, *, execution=True):
        self.check_execution(task_id, generation, token)
        if execution:
            self.resources.assert_execution(task_id)
        decisions.contract_guard(self, task_id)
        self.retain_input_images(inputs)
        context = self.delegation_context.get()
        if context is not None:
            context["write_attempted"] = True
        operation = self.operation_context.get()
        if execution and operation:
            operation["pending_execution"] = True

    @staticmethod
    def native_parent(thread):
        source = thread.get("source")
        if thread.get("parentThreadId"):
            return thread["parentThreadId"]
        if isinstance(source, dict) and isinstance(source.get("subAgent"), dict):
            return source["subAgent"].get("thread_spawn", {}).get("parent_thread_id")
        return None

    async def adopt_message_thread(self, params):
        if not self.backend.alive:
            return None
        thread_id = params.get("threadId") or params.get("thread", {}).get("id")
        async with self.adoption_lock:
            try:
                thread = params.get("thread") or (await self.backend.call("thread/read", {"threadId": thread_id, "includeTurns": False}))["thread"]
                return await self.adopt_thread(thread, set())
            except (BridgeError, OSError, KeyError) as exc:
                with self.store.transaction():
                    self.store.event(None, "bridge/child_not_adopted", {"thread_id": thread_id, "reason": str(exc)})
                return None

    async def adopt_thread(self, thread, visited):
        existing = self.task_for_thread(thread["id"])
        protocol_epoch = self.protocol_epochs.get(existing["id"], 0) if existing else 0
        if existing and existing.get("deleted"):
            return existing
        if existing and existing["id"] in self.protocol_quarantine:
            return existing
        if existing and (not existing.get("native_child") or existing["id"] in self.loaded):
            return existing
        if thread["id"] in visited or len(visited) >= 16:
            raise BridgeError("Invalid native parent chain", code='scope_unverified')
        visited.add(thread["id"])
        parent_id = self.native_parent(thread)
        if not parent_id:
            return None
        parent = self.task_for_thread(parent_id)
        if not parent:
            ancestor = await self.backend.call("thread/read", {"threadId": parent_id, "includeTurns": False})
            parent = await self.adopt_thread(ancestor["thread"], visited)
        if not parent or parent.get("deleted") or parent.get("archived"):
            return None
        if parent["id"] in self.protocol_quarantine:
            return None
        if existing and existing.get("parent_task_id") != parent["id"]:
            raise BridgeError("Native child's parent differs from its recorded ownership", code='scope_unverified')
        snapshot = existing["revision"] if existing else None
        self.task(parent["id"])
        cwd = project_path(self.config, thread["cwd"])
        if not within(cwd, Path(parent["project"])):
            raise BridgeError("Native child working directory is outside its parent's project", code='scope_unverified')
        root_id = parent.get("root_task_id", parent["id"])
        # Joining may load autonomous intent. Fence family observations before I/O.
        with self.store.transaction():
            self.store.claim(root_id, "background", "uncertain", {"reason": "child_adoption"})
            self.lifecycle.metadata(root_id, {})
        self.schedule_reconciliation(root_id)
        # No overrides: join the native child without restarting its work or
        # replacing inherited settings. Verify the resulting permission scope.
        params = {"threadId": thread["id"], "excludeTurns": True}
        if existing:
            # A new backend generation must reapply the same bridge boundary;
            # versioned profile IDs need not exist in old native snapshots.
            params.update(permissions=self.backend.profile(existing["access"]), cwd=str(cwd),
                          runtimeWorkspaceRoots=[parent["project"]], approvalPolicy="on-request")
            params.update(self.reviewer_params(existing.get("approval_mode", "inherit")))
        generation = self.backend.generation
        result = await self.backend.call("thread/resume", params)
        parent = self.task(parent["id"])
        existing = self.task_for_thread(thread["id"])
        if existing:
            self.check_recovery(existing["id"], protocol_epoch, generation)
        child = {**(existing or parent), "id": existing["id"] if existing else str(uuid.uuid4()), "thread_id": thread["id"],
            "native_cwd": str(cwd), "parent_task_id": parent["id"],
            "root_task_id": parent.get("root_task_id", parent["id"]), "native_child": True,
            "nickname": thread.get("agentNickname"), "agent_role": thread.get("agentRole"),
            "name": thread.get("name") or thread.get("agentNickname"),
            "model": result.get("model", parent["model"]), "effort": result.get("reasoningEffort") or parent["effort"],
            "created_at": existing["created_at"] if existing else time.time()}
        if not existing:
            child.update(summary="", commands=[], files=[], plan=[], diff="", diff_turn_id=None, usage=None,
                         active_settings={}, error=None, last_automatic_review=None, recovered_turn_status=None,
                         delegation_contract=None, delegation_contract_summary=None, delegation_contracts=[],
                         process_state=None, background_observation=None, process_scan=None, workspace_execution_project=None,
                         turn_id=None, turn_start=None, queue_reservation=None, queue_uncertain=False,
                         usage_scope="cumulative", updated_at=time.time(), status="idle")
        source = thread.get("source") or {}
        child["native_agent_path"] = source.get("subAgent", {}).get("thread_spawn", {}).get("agent_path") if isinstance(source, dict) and isinstance(source.get("subAgent"), dict) else None
        # Children inherit whichever native reviewer was active when spawned.
        child["approval_mode"] = existing.get("approval_mode", "inherit") if existing else "inherit"
        self.verify_thread(child, result, generation=generation)
        self.record_effective(child, result)
        # Publish ownership before reading history, so concurrent callbacks can
        # update this child without a later history ACK replacing their state.
        with self.store.transaction():
            if existing:
                owned = {k: child.get(k) for k in ("nickname", "agent_role", "native_agent_path")}
                effective = self.effective_fields(result)
                if existing["revision"] == snapshot:
                    owned.update({k: child.get(k) for k in ("name", "model", "effort")})
                else:
                    for key in ("native_status", "approval_mode", "approvals_reviewer"):
                        effective.pop(key, None)
                child = self.lifecycle.metadata(child["id"], {**owned, **effective})
            else:
                child = self.lifecycle.create(child)
        self.loaded.add(child["id"])
        snapshot = child["revision"] if snapshot is None or (existing and existing["revision"] == snapshot) else -1
        turn = await self.latest_turn(child["id"])
        self.check_recovery(child["id"], protocol_epoch, generation)
        with self.store.transaction():
            child = self.task(child["id"])
            child = self.lifecycle.recover(child["id"], turn,
                (result["thread"].get("status") or {}).get("type") == "active", expected_revision=snapshot, adopted=True)
            self.store.event(parent["id"], "bridge/subagent_discovered", {"task_id": child["id"], "thread_id": child["thread_id"], "nickname": child.get("nickname")})
        self.loaded.add(child["id"])
        self.schedule_reconciliation(root_id)
        await self.wake()
        return child

    async def subagents(self, task_id, cursor=None, limit=50, refresh=True, archived=False, offset=0):
        parent = self.task(task_id)
        result = {"data": [], "nextCursor": None}
        if refresh:
            await self.ensure_backend()
            result = await self.backend.call("thread/list", {"ancestorThreadId": parent["thread_id"], "cursor": cursor,
                "limit": limit, "archived": archived, "sourceKinds": ["subAgent", "subAgentThreadSpawn", "subAgentReview", "subAgentCompact", "subAgentOther"]})
            for thread in result["data"]:
                known = self.task_for_thread(thread["id"])
                if not archived and (not known or known["id"] not in self.loaded):
                    await self.adopt_message_thread({"thread": thread})
        children = [t for _, t in self.store.records("tasks", family_id=task_id, exclude_id=task_id, archived=archived,
                    sort_ids=True, offset=offset, limit=limit + 1, light=True)]
        records = [self.get(t["id"]) for t in children[:limit]]
        while len(records) > 1 and result_size({"subagents": records}) > 6 * 1024 * 1024:
            records.pop()
        return {"subagents": records, "next_offset": offset + len(records) if len(records) < len(children) else None,
                "nextCursor": result.get("nextCursor"), "note": "Native children are verified descendants, not unrelated sessions"}

    async def delegate(self, task_id, prompt, expected_turn_id=None, contract=None):
        task = self.task(task_id)
        def validate_turn(current):
            if expected_turn_id is not None and (current["status"] not in ("running", "waiting") or current.get("turn_id") != expected_turn_id):
                raise BridgeError("expected_turn_id requires an active matching turn; omit it for an idle delegation", code="stale_turn")
        validate_turn(task)
        message = "Use your native Codex subagent tools to carry out this delegation/control request. Preserve the current project and permission boundaries. Report the child agents and results.\n\n" + prompt
        if contract is not None:
            decisions.validate(contract, decisions.ENVELOPE, "contract")
            if task_id not in self.loaded or task.get("verified_generation") != self.backend.generation:
                await self.resume(task_id, rebuild=False)
                task = self.task(task_id)
                validate_turn(task)
            active = task["status"] in ("running", "waiting")
            if active and not expected_turn_id:
                raise BridgeError("Delegating during a turn requires expected_turn_id", code="stale_turn")
            record = decisions.bind(self, task, contract, kind="delegation", expected_turn_id=expected_turn_id if active else None)
            decisions.record_contract(self, task_id, record, {"state": "prepared"})
            message += "\n\nGuidance contract; this grants no permissions:\n" + json.dumps(record, ensure_ascii=False)
            context = {"record": record, "write_attempted": False}
            token = self.delegation_context.set(context)
            try:
                result = await self.send(task_id, message, "steer", expected_turn_id) if active else await self.send(task_id, message)
            except Exception as exc:
                if isinstance(exc, decisions.DecisionError) and context["write_attempted"]:
                    exc.before_write, exc.uncertain = False, True
                delivery = "not_sent" if isinstance(exc, decisions.DecisionError) else "rejected" if isinstance(exc, RpcError) else "uncertain"
                if context["write_attempted"] and delivery == "not_sent":
                    delivery = "uncertain"
                decisions.record_contract(self, task_id, record, {"state": delivery, "error_code": getattr(exc, "code", type(exc).__name__)})
                raise
            else:
                decisions.record_contract(self, task_id, record, {"state": "acknowledged",
                    "turn_id": record["scope"].get("turn_id") or context.get("acknowledged_turn_id")})
                return self.get(task_id)
            finally:
                self.delegation_context.reset(token)
        if task["status"] in ("running", "waiting"):
            if not expected_turn_id:
                raise BridgeError("Delegating during a turn requires expected_turn_id", code='stale_turn')
            return await self.send(task_id, message, "steer", expected_turn_id)
        return await self.send(task_id, message)

    async def capabilities(self, task_id):
        task = self.task(task_id)
        await self.ensure_backend()
        result = {"approval_modes": ["ask", "approve_for_me"], "access": ["read", "write"],
                  "can_accept_direct_input": task.get("can_accept_direct_input"), "codex_version": self.backend.version}
        for key, method, params in [("collaboration_modes", "collaborationMode/list", {}),
                                    ("permission_profiles", "permissionProfile/list", {"cwd": task["project"]})]:
            try:
                result[key] = await self.backend.call(method, params)
            except OptionalCapabilityUnavailable as exc:
                result[key] = {"error": exc.as_dict()}
        result.update(self.compatibility_fields(), native_methods=dict(NATIVE_METHODS))
        result["os_process_check"] = await self.os_process_status()
        return self.safe_payload(result)

    async def settings(self, task_id, scope="get", expected_turn_id=None, model=None, effort=None,
                       approval_mode=None, collaboration_mode=None, service_tier=None, access=None, budget=None):
        from .authority import validate_settings_reviewer
        validate_settings_reviewer(approval_mode)
        task = self.task(task_id)
        if scope == "get":
            task = self.lifecycle.view(task)
            from .authority import settings_view
            return {**{k: task.get(k) for k in ("id", "model", "effort", "approval_mode", "approvals_reviewer", "collaboration_mode", "service_tier", "access", "active_settings")},
                    "permission_view": settings_view(self, task), "budget": self.resources.policy(self.resources.family(task_id))[0]}
        if budget is not None:
            from .resources import validate_budget
            validate_budget(budget)
            if self.resources.family(task_id) != task_id:
                raise BridgeError("Family budgets can be changed only on the root task", code="invalid_arguments")
            if scope != "next":
                raise BridgeError("Family budgets require scope=next", code="invalid_arguments")
            if all(value is None for value in (model, effort, approval_mode, collaboration_mode, service_tier, access)):
                self.resources.set_budget(task_id, budget)
                self.schedule_budget_check(task_id)
                return self.get(task_id)
        await self.resume(task_id, rebuild=False)
        task = self.task(task_id)
        values = {k: v for k, v in {"model": model, "effort": effort, "approval_mode": approval_mode,
                  "collaboration_mode": collaboration_mode, "service_tier": service_tier, "access": access}.items() if v is not None}
        if not values:
            raise BridgeError("Provide at least one setting to change", code='invalid_arguments')
        candidate = {**task, **values}
        candidate["model"], candidate["effort"] = await self.choose_model(model or task["model"],
            effort or (None if model and model != task["model"] else task["effort"]), service_tier or task.get("service_tier"))
        task = self.task(task_id)
        params = {"threadId": task["thread_id"]}
        if model or effort:
            params.update(model=candidate["model"], effort=candidate["effort"])
        if approval_mode:
            params.update(self.reviewer_params(approval_mode))
        if service_tier:
            params["serviceTier"] = None if service_tier == "default" else service_tier
        if scope == "current":
            if collaboration_mode or access:
                raise BridgeError("Access/collaboration mode changes apply only to subsequent turns", code='invalid_arguments')
            if not expected_turn_id or expected_turn_id != task["turn_id"] or task["status"] not in ("running", "waiting"):
                raise BridgeError("Current-turn changes require the active expected_turn_id", code='stale_turn')
            params["turnId"] = expected_turn_id
            result = await self.backend.call("turn/settings/update", params)
            if result.get("status") != "applied":
                raise BridgeError("Native turn settings were not applied: " + str(result.get("status")), code='invalid_arguments')
            self.lifecycle.current_settings(task_id, expected_turn_id, values, result)
        else:
            if access:
                if task.get("native_child") and access == "write" and self.task(task["parent_task_id"])["access"] == "read":
                    raise BridgeError("A native child cannot broaden its read-only parent's project access", code='invalid_arguments')
                self.require_idle(task, family=True)
                params["permissions"] = self.backend.profile(access)
            if collaboration_mode or model or effort:
                params["collaborationMode"] = self.turn_options(candidate)["collaborationMode"]
            await self.backend.call("thread/settings/update", params)
            fields = dict(values)
            if fields.get("service_tier") == "default":
                fields["service_tier"] = None
            if access:
                fields["native_permission_profile"] = self.backend.profile(access)
            if model or effort:
                fields.update(model=candidate["model"], effort=candidate["effort"])
            if approval_mode:
                fields["approvals_reviewer"] = REVIEWERS[approval_mode]
            self.lifecycle.metadata(task_id, fields)
            latest = self.task(task_id)
            self.lifecycle.metadata(task_id, {"requested_settings": {**latest.get("requested_settings", {}), **values}})
        with self.store.transaction():
            if budget is not None:
                self.resources.set_budget(task_id, budget)
            self.store.event(task_id, "bridge/settings_changed", {"scope": scope, "settings": values})
        if budget is not None:
            self.schedule_budget_check(task_id)
        return self.get(task_id)

    async def fork(self, task_id, name=None, last_turn_id=None):
        source = self.task(task_id)
        await self.reconcile_claims(task_id, explicit=True)
        source = self.task(task_id)
        self.require_idle(source, family=True)
        await self.ensure_backend()
        generation = self.backend.generation
        options = await self.thread_options(source)
        fork_turn = source.get("turn_id")
        params = {**options, "threadId": source["thread_id"], "excludeTurns": True, "ephemeral": False, "deferGoalContinuation": True}
        if last_turn_id:
            params["lastTurnId"] = last_turn_id
        result = await self.backend.call("thread/fork", params)
        source = self.task(task_id)
        baseline = self.resources.fork_baseline(source, last_turn_id) if last_turn_id or source.get("turn_id") == fork_turn else None
        child = {**source, "id": str(uuid.uuid4()), "thread_id": result["thread"]["id"], "status": "idle",
                 "forked_from_task_id": task_id, "turn_id": None, "created_at": time.time(), "name": name,
                 "archived": False, "deleted": False, "native_child": False}
        child.update(summary="", files=[], commands=[], plan=[], diff="", diff_turn_id=None,
                     active_settings={}, error=None, last_automatic_review=None,
                     recovered_turn_status=None, turn_start=None, queue_reservation=None, queue_uncertain=False,
                     delegation_contract=None, delegation_contract_summary=None, delegation_contracts=[])
        child["process_state"] = None
        child["background_observation"] = None
        child["process_scan"] = None
        child["workspace_execution_project"] = None
        child["plan_id"] = None
        child["plan_node_id"] = None
        for key in ("parent_task_id", "root_task_id"):
            child.pop(key, None)
        self.verify_thread(child, result, generation=generation)
        self.record_effective(child, result)
        with self.store.transaction():
            child = self.lifecycle.create(child)
            if baseline is not None:
                self.resources.set_baseline(child["id"], baseline, "parent_at_fork")
            self.store.event(child["id"], "bridge/forked", {"from_task_id": task_id})
        self.loaded.add(child["id"])
        if name:
            await self.rename(child["id"], name)
        return self.get(child["id"])

    async def rename(self, task_id, name):
        task = self.task(task_id)
        await self.ensure_backend()
        await self.backend.call("thread/name/set", {"threadId": task["thread_id"], "name": name})
        with self.store.transaction():
            self.lifecycle.metadata(task_id, {"name": name})
            self.store.event(task_id, "bridge/renamed", {"name": name})
        return self.get(task_id)

    async def archive(self, task_id, action="archive"):
        task = self.task(task_id)
        if action == "archive":
            for key in self.family_ids(task_id):
                await self.reconcile_claims(key, explicit=True)
            task = self.task(task_id)
            self.require_idle(task, family=True)
        await self.ensure_backend()
        try:
            await self.verify_native_family(task)
        except OptionalCapabilityUnavailable:
            raise
        except RpcError as exc:
            raise self.unavailable_conversation(task) from exc
        if action == "unarchive":
            await self.check_capacity(task["project"], exclude=task_id)
            previous_claim = self.claim_snapshot(task_id, "uncertain")
            self.store.claim(task_id, "uncertain", "uncertain", {
                "operation_id": self.current_operation.get(), "reason": "unarchive"})
            claim_revision = self.claim_snapshot(task_id, "uncertain")["revision"]
        try:
            await self.backend.call("thread/" + action, {"threadId": task["thread_id"]})
        except OptionalCapabilityUnavailable:
            if action == "unarchive":
                self.restore_rejected_claim(task_id, "uncertain", claim_revision, previous_claim)
            raise
        except RpcError:
            if action == "unarchive":
                await self.reconcile_claims(task_id)
            raise
        finally:
            if action == "unarchive":
                self.schedule_reconciliation(task_id)
        with self.store.transaction():
            for key in self.family_ids(task_id):
                self.lifecycle.archive(key, action)
                self.loaded.discard(key)
            self.store.event(task_id, "bridge/" + action, {})
        if action == "unarchive":
            for key in self.family_ids(task_id):
                await self.reconcile_claims(key)
        return self.get(task_id)

    def deletion_confirmation(self, task, confirm_thread_id):
        unstarted = self.recorded_unstarted(task)
        if unstarted and confirm_thread_id != task["id"]:
            raise BridgeError("This task never reached Codex; pass confirm_thread_id equal to the task id to delete it", code='invalid_arguments')
        if not unstarted and confirm_thread_id != task["thread_id"]:
            raise BridgeError("Permanent deletion requires the exact native thread_id as confirmation", code='invalid_arguments')
        return unstarted

    async def prepare_deletion(self, task_id, confirm_thread_id):
        task = self.task(task_id, allow_deleted=True, light=True)
        unstarted = self.deletion_confirmation(task, confirm_thread_id)
        self.deletion_ready(task_id, probe=True)
        if not task.get("deleted") and not unstarted:
            await self.ensure_backend()
            await self.settle_family(task_id)

    async def delete(self, task_id, confirm_thread_id):
        if (self.operation_context.get() or {}).get("delete_preflight") != task_id:
            await self.prepare_deletion(task_id, confirm_thread_id)
        family = self.store.task_head(task_id).get("root_task_id", task_id)
        with self.store.bookkeeping_scope(self.store.family_ids(family)):
            return await self._delete_settled(task_id, confirm_thread_id)

    async def _delete_settled(self, task_id, confirm_thread_id):
        task = self.task(task_id, allow_deleted=True, light=True)
        unstarted = self.deletion_confirmation(task, confirm_thread_id)
        self.deletion_ready(task_id, probe=True)
        if unstarted:
            with self.store.transaction():
                self.deletion_ready(task_id)
                self.record_deletion(task_id, requested=True, native_conversation="not_started")
            return {**self.deletion_result(task_id, [task_id], set()), "native_conversation": "not_started"}
        if task.get("deleted"):
            with self.store.transaction():
                self.deletion_ready(task_id)
                self.record_deletion(task_id, requested=True)
            return self.deletion_result(task_id, [task_id], {task_id} if task.get("native_conversation") == "unavailable" else set())
        await self.ensure_backend()
        generation = self.backend.generation
        members = {key: self.task(key, allow_deleted=True, light=True) for key in self.family_ids(task_id)}
        snapshot = self.deletion_snapshot(task_id)
        rebases = 0
        def refresh(before, after, key=None):
            nonlocal rebases
            if before == after:
                return False
            if rebases >= 2 or not self.deletion_bookkeeping(before, after, key):
                raise BridgeError("Task family changed during deletion; retry after inspecting codex_get", code="state_conflict")
            rebases += 1
            return True
        def validate():
            nonlocal snapshot
            if generation != self.backend.generation or not self.backend.alive:
                raise BridgeError("Task family changed during deletion; retry after inspecting codex_get", code="state_conflict")
            latest = self.deletion_snapshot(task_id)
            changed = refresh(snapshot, latest)
            snapshot = latest
            return changed
        loaded = await self.loaded_conversations()
        validate()
        absent, absence_proofs = set(), {}
        for key, member in members.items():
            if member.get("deleted"):
                continue
            self.reset_reconciliation(key)
            try:
                await self.backend.call("thread/read", {"threadId": member["thread_id"], "includeTurns": False})
            except RpcError as exc:
                validate()
                code = exc.error.get("code") if isinstance(exc.error, dict) else None
                if (loaded is None or member["thread_id"] in loaded or isinstance(exc, OptionalCapabilityUnavailable)
                        or code in (-32601, -32602) or type(code) is not int):
                    raise self.unavailable_conversation(member) from exc
                current = self.deletion_member_snapshot(key)
                absent.add(key)
                absence_proofs[key] = current
            else:
                validate()
            if key not in absent:
                if member["status"] in BUSY:
                    processes.require_acknowledgement(member)
                    raise BridgeError("Stop and reconcile the entire task family before deleting it", code="state_conflict")
                if key == task_id:
                    await self.verify_native_family(task)
                    validate()
                # Keep the preflight read-only. Its proof is applied atomically
                # with deletion, so it cannot hide a concurrent event in a new
                # snapshot or release ownership before the delete acknowledgement.
                observed = await self.reconcile_claims(key, explicit=True, check_only=True)
                validate()
                inactive = (observed and observed["thread_status"] != "active" and observed["goal_status"] != "active"
                    and not observed["queue_total"] and not observed["current_backend_background_terminals"]
                    and (observed["latest_turn"]["status"] in TERMINAL if observed["latest_turn"] else not processes.pending(member)))
                if not inactive:
                    raise BridgeError("Stop and reconcile the entire task family before deleting it", code="state_conflict")
        if absent:
            self.deletion_loaded_safe(loaded, members, contact_lost=any(members[key]["status"] in BUSY for key in absent))
        present = {key for key in members if not members[key].get("deleted")} - absent
        roots = []
        for key in present:
            parent, seen = members[key].get("parent_task_id"), set()
            while parent in members and parent not in seen and parent not in present:
                seen.add(parent)
                parent = members[parent].get("parent_task_id")
            if parent not in present:
                roots.append(key)
        for key in sorted(roots):
            if key != task_id:
                await self.verify_native_family(members[key])
                validate()
        def guard():
            nonlocal snapshot
            self.deletion_ready(task_id, absent=absent)
            changed = validate()
            for key, proof in absence_proofs.items():
                current = self.deletion_member_snapshot(key)
                # A family refresh already accounted for this same write.
                if proof != current and not self.deletion_bookkeeping(proof, current, key):
                    raise BridgeError("Task family changed during deletion; retry after inspecting codex_get", code="state_conflict")
                absence_proofs[key] = current
            return changed
        token = object()
        def clear_requests():
            for key in members:
                if self.deletion_requests.get(key) == (generation, token):
                    self.deletion_requests.pop(key, None)
        try:
            guard()
            for key in sorted(roots):
                covered = self.family_ids(key)
                def sending():
                    guard()
                    for identity in members:
                        self.deletion_requests[identity] = (generation, token)
                await self.backend.call("thread/delete", {"threadId": members[key]["thread_id"]}, before_send=sending)
                latest = self.deletion_snapshot(task_id)
                previous = {entry[0]: entry[1:] for entry in snapshot}
                if {entry[0] for entry in latest} != set(previous):
                    raise BridgeError("Task family changed during deletion; retry after inspecting codex_get", code="state_conflict")
                for identity, *state in latest:
                    if tuple(state) != tuple(previous.get(identity, ())):
                        member = self.store.task_head(identity)
                        if identity not in covered or not member.get("deleted") or self.store.claims(task_id=identity):
                            refresh(previous[identity], tuple(state), identity)
                snapshot = latest
            for _ in range(3):
                guard()
                if absent:
                    latest_loaded = await self.loaded_conversations()
                    if latest_loaded is None or any(members[key]["thread_id"] in latest_loaded for key in absent):
                        raise BridgeError("Conversation availability changed during deletion; retry after inspecting codex_get", code="state_conflict")
                    self.deletion_loaded_safe(latest_loaded, members, contact_lost=any(members[key]["status"] in BUSY for key in absent))
                    for key in sorted(absent):
                        try:
                            await self.backend.call("thread/read", {"threadId": members[key]["thread_id"], "includeTurns": False})
                        except RpcError as exc:
                            code = exc.error.get("code") if isinstance(exc.error, dict) else None
                            if isinstance(exc, OptionalCapabilityUnavailable) or code in (-32601, -32602) or type(code) is not int:
                                raise self.unavailable_conversation(members[key]) from exc
                        except (BridgeError, OSError, TimeoutError) as exc:
                            raise BridgeError("Conversation availability changed during deletion; retry after inspecting codex_get", code="state_conflict") from exc
                        else:
                            raise BridgeError("Conversation availability changed during deletion; retry after inspecting codex_get", code="state_conflict")
                process_check, release_snapshot = await self.prepare_release_scan(task_id, deleting=members)
                if guard():
                    continue  # Re-do the read-only proof, never a native delete.
                if process_check is False or self.workspace_snapshot(task.get("root_task_id", task_id)) != release_snapshot:
                    if rebases < 2 and self.workspace_bookkeeping(release_snapshot, self.workspace_snapshot(task.get("root_task_id", task_id))):
                        rebases += 1
                        continue
                    raise BridgeError("Task family changed during deletion; retry after inspecting codex_get", code="state_conflict")
                with self.store.transaction():
                    for key in members:
                        self.record_deletion(key, requested=True, native_conversation="unavailable" if key in absent else None if members[key].get("deleted") else "deleted",
                            process_check=process_check, deleting=members)
                break
            else:
                raise BridgeError("Task family changed during deletion; retry after inspecting codex_get", code="state_conflict")
        except RpcError:
            clear_requests()
            raise
        else:
            clear_requests()
        return self.deletion_result(task_id, members, absent)

    def deletion_result(self, task_id, members, absent):
        states, states_total, state_bytes = [], 0, 0
        for key in members:
            state = self.process_view(self.store.get("tasks", key))
            if state and state.get("previous_backend_unverified"):
                states_total += 1
                entry = {"task_id": key, **state}
                size = result_size(entry)
                if len(states) < 100 and state_bytes + size <= 128 * 1024:
                    states.append(entry)
                    state_bytes += size
        return {"task_id": task_id, "deleted": True, "audit_retained": True,
            "native_conversation": "unavailable" if task_id in absent else "deleted", "native_conversations_unavailable": len(absent),
            "processes_unverified": bool(states_total), "process_state": self.process_view(self.store.get("tasks", task_id)),
            "process_states": states, "process_states_total": states_total, "process_states_truncated": len(states) < states_total}

    def deletion_ready(self, task_id, *, probe=False, absent=()):
        ids = self.family_ids(task_id)
        for key in ids:
            member = self.store.task_head(key)
            previous = (processes.pending(member) and member["status"] in ("needs_resume", "uncertain")
                and key not in self.loaded)
            if member["status"] in BUSY and not (previous and (probe or key in absent)):
                raise BridgeError("Stop and reconcile the entire task family before deleting it", code="invalid_arguments")
        rows = self.store.db.execute("""SELECT o.id,o.task_id,d.task_id described_task FROM operations o
            LEFT JOIN operation_details d ON d.id=o.id WHERE o.state='running'""")
        if any(row["id"] != self.current_operation.get() and (row["task_id"] in ids or row["described_task"] in ids) for row in rows):
            raise BridgeError("A task-family operation is still running; wait for it before deleting", code="state_conflict")

    def deletion_snapshot(self, task_id):
        return [(key, *self.deletion_member_snapshot(key))
            for key in sorted(self.family_ids(task_id))]

    def deletion_member_snapshot(self, task_id):
        return (self.store.task_head(task_id)["revision"],
            [(c["kind"], c["revision"]) for c in self.store.claims(task_id=task_id, include_inactive=True)],
            self.store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=?", (task_id,)).fetchone()[0])

    def deletion_bookkeeping(self, before, after, task_id=None):
        if task_id is None:
            previous, current = {r[0]: r[1:] for r in before}, {r[0]: r[1:] for r in after}
            return previous.keys() == current.keys() and all(self.deletion_bookkeeping(previous[key], current[key], key) for key in previous)
        if before[2] != after[2] or not self.store.bookkeeping_only("tasks", task_id, before[0], after[0]):
            return False
        old, new = dict(before[1]), dict(after[1])
        return old.keys() <= new.keys() and all(self.store.bookkeeping_only("claims", (task_id, kind), old.get(kind, 0), revision)
            for kind, revision in new.items())

    async def loaded_conversations(self):
        result, cursor = set(), None
        try:
            for _ in range(100):
                page = await self.backend.call("thread/loaded/list", {"cursor": cursor, "limit": 200})
                if not isinstance(page.get("data"), list) or any(not isinstance(value, str) for value in page["data"]):
                    return None
                if "nextCursor" not in page or (page["nextCursor"] is not None and not isinstance(page["nextCursor"], str)):
                    return None
                result.update(page["data"])
                cursor = page["nextCursor"]
                if cursor is None:
                    return result
        except (BridgeError, OSError, TimeoutError):
            return None
        return None

    def deletion_loaded_safe(self, loaded, members, *, contact_lost=False):
        for identity in loaded:
            owner = self.task_for_thread(identity)
            if owner is None:
                raise BridgeError("An unadopted conversation is loaded; retry deletion later after inspecting codex_subagents. Nothing was deleted", code="state_conflict")
            family = next(iter(members.values())).get("root_task_id")
            if owner.get("root_task_id") == family and (contact_lost or owner["id"] not in members):
                raise BridgeError("Another conversation of this family is loaded; reconcile the entire family before deleting", code="state_conflict")

    async def compact(self, task_id):
        await self.resume(task_id, rebuild=False)
        task = self.task(task_id)
        self.require_idle(task)
        await self.check_capacity(task["project"], exclude=task_id)
        await self.turn_control(task, "thread/compact/start", {"threadId": task["thread_id"]})
        return self.get(task_id)

    async def turn_control(self, task, method, params):
        task = self.task(task["id"])
        self.require_idle({**task, "status": "idle"} if method == "thread/queue/start" and task["status"] == "queued" else task)
        previous_claim = self.claim_snapshot(task["id"], "turn")
        previous, token = self.reserve_turn(task["id"])
        claim_revision = self.intent_inflight[(task["id"], "turn")]
        generation = self.backend.generation
        review = None
        if method == "review/start":
            review = {"generation": generation, "sent": False, "turn_id": None, "deferred": {}}
            self.inline_reviews[task["id"]] = review
        def guard():
            self.execution_guard(task["id"], generation, token)
            if review is not None:
                review["sent"] = True
        try:
            result = await self.backend.call(method, params,
                before_send=guard)
        except Exception as exc:
            with self.store.transaction():
                if isinstance(exc, RpcError):
                    self.inline_reviews.pop(task["id"], None)
                    self.lifecycle.rollback(task["id"], token, previous,
                                            known_rejected=isinstance(exc, OptionalCapabilityUnavailable))
                    if isinstance(exc, OptionalCapabilityUnavailable):
                        self.restore_rejected_claim(task["id"], "turn", claim_revision, previous_claim)
                        self.resources.finish(self.current_operation.get(), rejected=True)
                elif isinstance(exc, BridgeError) and exc.code == "budget_exceeded":
                    self.inline_reviews.pop(task["id"], None)
                    self.lifecycle.rollback(task["id"], token, previous, known_rejected=True)
                    self.restore_rejected_claim(task["id"], "turn", claim_revision, previous_claim)
                else:
                    self.lifecycle.uncertain(task["id"], token=token)
            if isinstance(exc, RpcError):
                self.finish_intent(task["id"], "turn", claim_revision)
                if not isinstance(exc, OptionalCapabilityUnavailable):
                    await self.reconcile_claims(task["id"])
            raise
        finally:
            self.finish_intent(task["id"], "turn", claim_revision)
        if result.get("turn"):
            await self.ack_turn(task["id"], result, token=token, generation=generation)
        else:
            # Compaction acknowledges the start with an empty result.
            with self.store.transaction():
                self.record_execution(task["id"], generation)
        return result

    async def revert(self, task_id, before_turn_id, confirm_history_only):
        if not confirm_history_only:
            raise BridgeError("Confirm that this removes conversation history, not filesystem changes", code='invalid_arguments')
        task = self.task(task_id)
        await self.reconcile_claims(task_id, explicit=True)
        task = self.task(task_id)
        self.require_idle(task, family=True)
        await self.ensure_backend()
        task = self.task(task_id)
        with self.store.bookkeeping_scope(self.family_ids(task_id)):
            snapshot = self.deletion_snapshot(task_id)
            if native_turns.recorded_root(self.store, {**task, "turn_id": before_turn_id}):
                raise BridgeError(native_turns.NESTED_REVERT, code="invalid_arguments")
            if not self.lifecycle.turn(task_id, before_turn_id):
                await native_turns.require_root(self.backend, task["thread_id"], before_turn_id,
                    scope=native_turns.for_task(self.store, task_id))
            result = await self.backend.call("thread/revert", {"threadId": task["thread_id"], "beforeTurnId": before_turn_id})
            with self.store.transaction():
                current = self.deletion_snapshot(task_id)
                revision = self.store.task_head(task_id)["revision"]
                if self.deletion_bookkeeping(snapshot, current):
                    self.lifecycle.reverted(task_id, revision)
                self.store.event(task_id, "bridge/reverted", {"before_turn_id": before_turn_id, "files_changed": False})
        return {"task_id": task_id, "history": self.safe_payload(result), "files_changed": False}

    async def queue(self, task_id, action="list", prompt=None, queued_id=None, ordered_ids=None, cursor=None, limit=50,
                    images=None, context_files=None, skills=None, operation_id=None):
        task = self.task(task_id)
        self.assert_writable_session(task)
        await self.ensure_backend()
        task = self.task(task_id)
        if action in ("add", "update", "reorder"):
            task = await self.verified_session(task_id)
        generation = self.backend.generation
        reservation = None
        previous_queue = {key: task.get(key) for key in
                          ("status", "queued_after_turn_id", "queue_reservation", "queue_uncertain")}
        params = {"threadId": task["thread_id"]}
        if action in ("add", "update"):
            if not prompt:
                raise BridgeError("Queue add/update requires a prompt", code='invalid_arguments')
            params["input"] = await self.build_input(task, prompt, images, context_files, skills)
        if action in ("update", "delete"):
            if not queued_id:
                raise BridgeError("This operation requires queued_id", code='invalid_arguments')
            params["queuedSubmissionId"] = queued_id
        if action == "add":
            await self.check_capacity(task["project"], exclude=task_id)
            task = self.task(task_id)
            previous_queue = {key: task.get(key) for key in previous_queue}
            params["clientUserMessageId"] = operation_id or str(uuid.uuid4())
            previous_claim = next((c for c in self.store.claims(task_id=task_id, include_inactive=True) if c["kind"] == "queue"), None)
            claim_revision = self.intent_claim(task_id, "queue")
            if not task.get("queue_reservation"):
                task = self.task(task_id)
                snapshot = {"revision": task["revision"]}
                try:
                    previous = await self.latest_turn(task_id)
                except BaseException:
                    self.finish_intent(task_id, "queue", claim_revision)
                    self.store.claim(task_id, "queue", previous_claim["state"] if previous_claim else "inactive",
                        previous_claim["data"] if previous_claim else {}, expected_revision=claim_revision)
                    raise
                previous = previous or {}
                reservation = {"id": params["clientUserMessageId"], "after_turn_id": previous.get("id"),
                               "previous_status": task["status"], "outcome": "pending"}
                with self.store.transaction():
                    updated = self.lifecycle.queued(task_id, {"status": task["status"] if task["status"] in BUSY else "queued",
                        "queued_after_turn_id": previous.get("id"), "queue_reservation": reservation,
                        "queue_uncertain": False}, expected=snapshot)
                if updated is None:
                    reservation = None
                task = self.task(task_id)
            self.resources.bind(self.current_operation.get(), task_id, native_key=params["clientUserMessageId"],
                                after_turn_id=(reservation or task.get("queue_reservation") or {}).get("after_turn_id"))
        elif action == "reorder":
            if not ordered_ids or len(set(ordered_ids)) != len(ordered_ids):
                raise BridgeError("Provide a nonempty list of unique ordered_ids", code='invalid_arguments')
            params["queuedSubmissionIds"] = ordered_ids
        elif action == "start":
            await self.resume(task_id, rebuild=False)
            task = self.task(task_id)
            self.require_idle({**task, "status": "idle"} if task["status"] == "queued" else task)
            await self.check_capacity(task["project"], exclude=task_id)
            if queued_id:
                params["queuedSubmissionId"] = queued_id
        elif action == "list":
            params.update(cursor=cursor, limit=limit)
        try:
            guard = (lambda: self.input_guard(task_id, generation, params.get("input"), execution=action == "add")) if action in ("add", "update", "reorder") else None
            result = await self.turn_control(task, "thread/queue/start", params) if action == "start" else await self.backend.call("thread/queue/" + action, params, before_send=guard)
        except OptionalCapabilityUnavailable:
            if action == "add":
                self.finish_intent(task_id, "queue", claim_revision)
                with self.store.transaction():
                    if reservation:
                        self.lifecycle.queued(task_id, previous_queue, expected={"queue_reservation": reservation})
                    self.restore_rejected_claim(task_id, "queue", claim_revision, previous_claim)
                    self.resources.finish(self.current_operation.get(), rejected=True)
                await self.wake()
            raise
        except RpcError as exc:
            if action == "add":
                self.finish_intent(task_id, "queue", claim_revision)
            if action == "add" and reservation:
                # A rejection alone is insufficient: inspect for an accepted or
                # already consumed submission before releasing the reservation.
                try:
                    await self.sync_queue(task_id, rejected_id=reservation["id"])
                    current = self.task(task_id).get("queue_reservation") or {}
                    if current.get("id") == reservation["id"] and current.get("outcome") != "accepted":
                        self.mark_queue_uncertain(task_id, current)
                except (BridgeError, OSError, TimeoutError):
                    self.mark_queue_uncertain(task_id, reservation)
                await self.wake()
            if action == "add":
                await self.reconcile_claims(task_id)
                current = self.task(task_id)
                if not current.get("queue_reservation") and not current.get("queue_uncertain"):
                    self.resources.finish(self.current_operation.get(), rejected=True)
            if "queued submission not found" in str(exc):
                await self.sync_queue(task_id)
                raise BridgeError("Queued instruction is no longer pending and may already be running. Inspect codex_get/history; do not re-add it blindly", code='invalid_arguments') from exc
            raise
        except Exception as exc:
            if action == "add":
                self.finish_intent(task_id, "queue", claim_revision)
                if isinstance(exc, BridgeError) and exc.code == "budget_exceeded":
                    with self.store.transaction():
                        if reservation:
                            self.lifecycle.queued(task_id, previous_queue, expected={"queue_reservation": reservation})
                        self.restore_rejected_claim(task_id, "queue", claim_revision, previous_claim)
                        self.resources.finish(self.current_operation.get(), rejected=True)
                else:
                    self.mark_queue_uncertain(task_id, reservation)
                await self.wake()
            raise
        except BaseException:
            if action == "add":
                self.finish_intent(task_id, "queue", claim_revision)
                self.mark_queue_uncertain(task_id, reservation)
            raise
        if action == "add":
            self.finish_intent(task_id, "queue", claim_revision)
            with self.store.transaction():
                self.resources.queue_accepted(self.current_operation.get(), (result.get("queuedSubmission") or {}).get("id"))
                self.store.claim(task_id, "queue", "active", {"operation_id": self.current_operation.get()},
                                 expected_revision=claim_revision)
                current = self.store.task_head(task_id)
                pending = current.get("queue_reservation") or {}
                if reservation and pending.get("id") == reservation["id"] and pending.get("outcome") != "accepted":
                    self.lifecycle.queued(task_id, {"queue_reservation": {**pending, "outcome": "accepted"}, "queue_uncertain": False})
                if current.get("turn_id") and current["turn_id"] != task.get("turn_id"):
                    self.record_execution(task_id, generation)
        if action == "start":
            await self.reconcile_claims(task_id)
            return self.get(task_id)
        if action == "delete":
            self.resources.queue_removed(task_id, queued_id)
        if action != "list":
            await self.sync_queue(task_id, allow_idle=action == "delete")
            await self.reconcile_claims(task_id)
        elif not result.get("nextCursor") and not cursor:
            await self.reconcile_claims(task_id)
        return self.safe_payload(result)

    def mark_queue_uncertain(self, task_id, reservation):
        task = self.task(task_id)
        with self.store.transaction():
            fields = {"queue_uncertain": True}
            if reservation and task.get("queue_reservation") == reservation:
                fields["queue_reservation"] = {**reservation, "outcome": "uncertain"}
            if task["status"] in BUSY:
                self.lifecycle.queued(task_id, fields)

    async def sync_queue(self, task_id, allow_idle=False, reconcile=False, rejected_id=None):
        task = self.task(task_id)
        if task["status"] != "queued" and not task.get("queue_reservation"):
            return
        expected = {"revision": task["revision"]}
        queued, cursor = [], None
        for _ in range(100):
            page = await self.backend.call("thread/queue/list", {"threadId": task["thread_id"], "limit": 200, "cursor": cursor})
            queued.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        else:
            raise BridgeError("Native queue pagination did not terminate; reservation retained", code='invalid_arguments')
        native = (await self.backend.call("thread/read", {"threadId": task["thread_id"], "includeTurns": False}))["thread"]
        turn = await self.latest_turn(task_id)
        latest = self.task(task_id)
        if any(latest.get(k) != v for k, v in expected.items()):
            return
        native_status = native.get("status", {}).get("type")
        if native_status not in ("idle", "active", "notLoaded", "systemError"):
            raise BridgeError("Native activity is unknown; queue reservation retained", code='invalid_arguments')
        active = native_status == "active"
        reservation = task.get("queue_reservation") or {}
        if queued:
            matched = any(q.get("clientUserMessageId") == reservation.get("id") for q in queued) if reservation else False
            fields = {"status": task["status"] if task["status"] in ("running", "waiting", "starting", "interrupting") else "queued"}
            if matched:
                fields.update(queue_uncertain=False, queue_reservation={**reservation, "outcome": "accepted"})
            with self.store.transaction():
                self.lifecycle.queued(task_id, fields, expected=expected)
            await self.wake()
            return
        if task["status"] in ("queued", "needs_resume", "uncertain", "idle"):
            # Queue consumption can precede turn/started. Keep the project
            # reserved until native turn history or events establish its state.
            after = reservation.get("after_turn_id", task.get("queued_after_turn_id"))
            if turn and (turn.get("id") != after or turn.get("status") == "inProgress"):
                with self.store.transaction():
                    task = self.lifecycle.begin(task_id, turn["id"], source="recovery")
                    if task.get("turn_id") != turn["id"]:
                        return
                    if turn.get("status") in TERMINAL:
                        self.lifecycle.complete(task_id, turn["id"], turn["status"], turn.get("error"))
                    self.lifecycle.gap(task_id, "queue_reconciliation")
                    if reservation.get("outcome") == "accepted":
                        self.record_execution(task_id, self.backend.generation)
                fields = {"status": "running" if active or turn.get("status") == "inProgress" else turn["status"],
                          "queue_reservation": None, "queue_uncertain": False}
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage":
                        fields["summary"] = item.get("text", "")
            elif not active and (turn.get("id") if turn else None) == after and (
                    allow_idle or reconcile or (rejected_id and rejected_id == reservation.get("id"))):
                previous_status = reservation.get("previous_status", "idle")
                if previous_status in BUSY:
                    previous_status = turn["status"] if turn and turn.get("status") != "inProgress" else "idle"
                fields = {"status": previous_status,
                          "queue_reservation": None, "queue_uncertain": False}
            else:
                return
            with self.store.transaction():
                if "summary" in fields:
                    self.lifecycle.project(task_id, task["turn_id"], {"summary": fields.pop("summary")})
                self.lifecycle.queued(task_id, fields)
            await self.wake()

    async def ack_turn(self, task_id, result, token=None, generation=None):
        native = result.get("turn") or {}
        turn_id = native_turns.logical_id(native, native_turns.for_task(self.store, task_id))
        review = self.inline_reviews.get(task_id)
        if review and review["generation"] == self.backend.generation:
            review["turn_id"] = turn_id
        operation_id = self.current_operation.get()
        row = self.store.db.execute("SELECT data FROM operation_details WHERE id=?", (operation_id,)).fetchone()
        with self.store.transaction():
            if row and turn_id:
                details = {**json.loads(row[0]), "acknowledged_turn": {"task_id": task_id,
                    "thread_id": self.task(task_id, light=True)["thread_id"], "turn_id": turn_id, "native_turn_id": native["id"]}}
                self.store.db.execute("UPDATE operation_details SET data=? WHERE id=?", (json.dumps(details), operation_id))
            if turn_id:
                self.scheduler.bind(operation_id, task_id, turn_id=turn_id)
            if task_id not in self.protocol_quarantine:
                self.lifecycle.begin(task_id, turn_id, source="ack", token=token)
                if turn_id:
                    self.record_execution(task_id, self.backend.generation if generation is None else generation)
                    completed = native_turns.terminal_evidence(self.store, self.task(task_id, light=True), turn_id)
                    if completed:
                        self.complete_turn(task_id, turn_id, completed)
            known = self.lifecycle.turn(task_id, turn_id)
            if review and known and known["status"] in TERMINAL:
                self.inline_reviews.pop(task_id, None)
        self.schedule_budget_check(task_id)
        await self.wake()
        return self.get(task_id)

    async def review(self, task_id, target="uncommittedChanges", branch=None, commit=None, instructions=None):
        await self.resume(task_id, rebuild=False)
        task = self.task(task_id)
        self.require_idle(task)
        await self.check_capacity(task["project"], exclude=task_id)
        review_target = {"type": target}
        if target == "baseBranch":
            if not branch:
                raise BridgeError("Base branch is required", code='invalid_arguments')
            review_target["branch"] = branch
        elif target == "commit":
            if not commit:
                raise BridgeError("Commit is required", code='invalid_arguments')
            review_target["sha"] = commit
        elif target == "custom":
            if not instructions:
                raise BridgeError("Review instructions are required", code='invalid_arguments')
            review_target["instructions"] = instructions
        await self.turn_control(task, "review/start", {"threadId": task["thread_id"], "target": review_target, "delivery": "inline"})
        return self.get(task_id)

    async def terminals(self, task_id, action="list", process_id=None, cursor=None, limit=50):
        task = self.task(task_id)
        await self.ensure_backend()
        generation = self.backend.generation
        terminal_token = self.terminal_token()
        params = {"threadId": task["thread_id"]}
        if action == "list":
            params.update(cursor=cursor, limit=limit)
        elif action == "terminate":
            if not process_id:
                raise BridgeError("process_id is required", code='invalid_arguments')
            params["processId"] = process_id
        try:
            result = await self.backend.call("thread/backgroundTerminals/" + action, params)
            if action == "list" and isinstance(result.get("data"), list):
                processes.observe_terminals(self, task_id, result["data"], generation, terminal_token,
                    complete=cursor is None and not result.get("nextCursor"))
            await self.reconcile_claims(task_id, explicit=action != "list")
        finally:
            if action != "list" and self.store.claims(task_id=task_id):
                self.schedule_reconciliation(task_id)
            await self.wake()
        return self.safe_payload(result)

    async def goal(self, task_id, action="get", objective=None, status=None, token_budget=None, user_requested=False):
        if action != "get" and self.current_operation.get() is None:
            values = {"task_id": task_id, "action": action, "user_requested": user_requested,
                **{k: v for k, v in (("objective", objective), ("status", status), ("token_budget", token_budget)) if v is not None}}
            return await self.operation("local:" + str(uuid.uuid4()), "codex_goal", values, lambda: self.goal(**values))
        task = self.task(task_id)
        await self.ensure_backend()
        params = {"threadId": task["thread_id"]}
        if action != "get":
            if not user_requested:
                raise BridgeError("Goal mutations require an explicit user request; do not infer one from a coding task", code="user_request_required")
            params["origin"] = "user"
            if action == "set":
                self.assert_writable_session(task)
                task = await self.verified_session(task_id)
                if status == "active" or (objective is not None and status is None):
                    await self.check_capacity(task["project"], exclude=task_id)
                if objective is not None:
                    params["objective"] = objective
                if status is not None:
                    params["status"] = status
                if token_budget is not None:
                    params["tokenBudget"] = token_budget
                if len(params) == 2:
                    raise BridgeError("Provide an objective, status or budget", code='invalid_arguments')
        enabling = action == "set" and status not in ("paused", "blocked", "complete")
        previous_claim = self.claim_snapshot(task_id, "goal")
        snapshot = next((c["revision"] for c in self.store.claims(task_id=task_id, include_inactive=True) if c["kind"] == "goal"), 0)
        mutating = action != "get"
        if mutating:
            snapshot = self.intent_claim(task_id, "goal", enabling=enabling)
        generation = self.backend.generation
        def enabling_guard():
            self.check_execution(task_id, generation)
            self.resources.arm_goal(self.current_operation.get(), task_id)
            self.execution_guard(task_id, generation)
        try:
            guard = enabling_guard if enabling else None
            result = await self.backend.call("thread/goal/" + action, params, before_send=guard)
        except OptionalCapabilityUnavailable:
            if mutating:
                self.finish_intent(task_id, "goal", snapshot)
                self.restore_rejected_claim(task_id, "goal", snapshot, previous_claim)
            raise
        except RpcError:
            if mutating:
                self.finish_intent(task_id, "goal", snapshot)
            observed = await self.reconcile_claims(task_id)
            reservation = self.store.db.execute("SELECT after_turn_id FROM resource_reservations WHERE id=?", (self.current_operation.get(),)).fetchone()
            latest = (observed or {}).get("latest_turn")
            current = self.store.task_head(task_id)
            if (reservation and observed and observed["generation"] == self.backend.generation and self.backend.alive
                    and observed["thread_status"] == "idle" and observed["goal_status"] in (None, "paused", "blocked", "complete")
                    and (latest or {}).get("id") == reservation[0] == current.get("turn_id")
                    and (not latest or latest["status"] in TERMINAL and latest["status"] == current.get("turn_status"))):
                self.resources.finish(self.current_operation.get(), rejected=True)
            raise
        except BridgeError as error:
            if mutating and error.code == "budget_exceeded":
                self.restore_rejected_claim(task_id, "goal", snapshot, previous_claim)
            raise
        finally:
            if mutating:
                self.finish_intent(task_id, "goal", snapshot)
        value = result.get("goal")
        observed = value.get("status") if isinstance(value, dict) else None
        if "goal" in result and (value is None or observed in ("active", "paused", "blocked", "complete")):
            if observed != "active" and previous_claim and previous_claim["state"] != "inactive":
                self.mark_native_intent(task_id, "background", "native_goal_changed")
                self.schedule_reconciliation(task_id)
            with self.store.transaction():
                self.observe_goal(task_id, result, snapshot)
                if enabling and observed == "active":
                    self.record_execution(task_id, generation)
        elif enabling:
            # An acknowledgement without native goal state is not release evidence.
            await self.reconcile_claims(task_id)
        if action != "get":
            await self.reconcile_claims(task_id, explicit=True)
        return self.safe_payload(result)

    async def skills(self, task_id, force_reload=False):
        task = self.task(task_id)
        await self.ensure_backend()
        return self.safe_payload(await self.backend.call("skills/list", {"cwds": [task["project"]], "forceReload": force_reload}))

    async def usage(self):
        await self.ensure_backend()
        return self.safe_payload(await self.backend.call("account/rateLimits/read", {}))

    async def diff(self, task_id):
        task = self.lifecycle.view(self.task(task_id))
        return {"task_id": task_id, "turn_id": task.get("turn_id"), "diff_turn_id": task.get("diff_turn_id"),
                "diff": task.get("diff", "") if task.get("diff_turn_id") == task.get("turn_id") else "",
                "files": task.get("files", []), "source": "Codex turn events; not an independent Git working-tree audit"}

    async def sessions(self, workdir=None, project_id=None, action="list", thread_id=None, cursor=None, limit=50, archived=False, confirm_stopped=False):
        descriptor = self.select_project(workdir, project_id)
        project = Path(descriptor["path"])
        await self.ensure_backend()
        if action == "list":
            result = await self.backend.call("thread/list", {"cwd": str(project), "archived": archived,
                "cursor": cursor, "limit": limit, "sourceKinds": ["cli", "vscode", "exec", "appServer"]})
            return {"sessions": [{k: t.get(k) for k in ("id", "name", "cwd", "updatedAt", "source", "status")} for t in result["data"] if same_path(t["cwd"], project)],
                    "nextCursor": result.get("nextCursor")}
        if not thread_id:
            raise BridgeError("Import requires an explicit native thread_id", code='invalid_arguments')
        if not confirm_stopped:
            raise BridgeError("Confirm that this conversation is stopped in other Codex clients before importing it", code='invalid_arguments')
        existing = self.task_for_thread(thread_id)
        if existing:
            if not same_path(existing["project"], project):
                raise BridgeError("Thread does not belong to this project", code='invalid_arguments')
            return self.get(existing["id"])
        thread = (await self.backend.call("thread/read", {"threadId": thread_id, "includeTurns": False}))["thread"]
        if not same_path(thread["cwd"], project) or self.native_parent(thread):
            raise BridgeError("Import only a root conversation whose working directory exactly matches the selected project", code='invalid_arguments')
        if thread.get("status", {}).get("type") == "active":
            raise BridgeError("Stop the native active conversation before importing it", code='invalid_arguments')
        model, effort = await self.choose_model(thread.get("model"), thread.get("reasoningEffort"))
        admission = self.admission()
        task = {"id": admission["family_id"] if admission else str(uuid.uuid4()), "thread_id": thread_id, "project_id": descriptor["id"],
                "project": str(project), "model": model, "effort": effort, "access": "write", "approval_mode": "inherit",
                "collaboration_mode": "default", "status": "archived" if archived else "needs_resume", "archived": archived, "turn_id": None,
                "summary": "", "commands": [], "files": [], "plan": [], "usage": None,
                "created_at": time.time(), "name": thread.get("name")}
        if not archived:
            await self.check_capacity(str(project))
        with self.store.transaction():
            task = self.lifecycle.create(task)
            self.resources.imported(task["id"])
            if self.current_operation.get():
                self.store.db.execute("UPDATE operations SET task_id=? WHERE id=?", (task["id"], self.current_operation.get()))
            self.store.event(task["id"], "bridge/imported", {"thread_id": thread_id})
        return self.get(task["id"]) if archived else await self.resume(task["id"], rebuild=False)
