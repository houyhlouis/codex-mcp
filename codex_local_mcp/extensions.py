"""Scoped native Codex controls. No arbitrary RPC or unrestricted shell surface."""
import asyncio
import json
import os
import stat
import time
import uuid
from pathlib import Path

from .backend import RpcError
from .security import BridgeError, project_path, within, private_dir, private_file, read_project_file, same_path

BUSY = {"running", "waiting", "starting", "queued", "interrupting", "uncertain", "needs_resume"}
REVIEWERS = {"ask": "user", "approve_for_me": "auto_review"}


class ExtendedControls:
    @staticmethod
    def reviewer_params(mode):
        if mode == "inherit":
            return {}
        if mode not in REVIEWERS:
            raise BridgeError("Unknown approval mode")
        return {"approvalsReviewer": REVIEWERS[mode]}

    def record_effective(self, task, result):
        reviewer = result.get("approvalsReviewer")
        task["approval_mode"] = "approve_for_me" if reviewer in ("auto_review", "guardian_subagent") else "ask"
        task["approvals_reviewer"] = reviewer
        thread = result.get("thread", {})
        task["can_accept_direct_input"] = thread.get("canAcceptDirectInput")
        task["native_status"] = thread.get("status")
        task["session_id"] = thread.get("sessionId")

    @staticmethod
    def assert_writable_session(task):
        if task.get("archived") or task.get("deleted"):
            raise BridgeError("Restore the archived conversation before continuing; deleted conversations cannot be resumed")

    def family_ids(self, task_id):
        ids = {task_id}
        for _ in range(32):
            more = {t["id"] for t in self.store.all("tasks") if t.get("parent_task_id") in ids}
            if more <= ids:
                break
            ids |= more
        return ids

    def require_idle(self, task, family=False):
        self.assert_writable_session(task)
        tasks = [self.store.get("tasks", i) for i in self.family_ids(task["id"])] if family else [task]
        if any(t["status"] in BUSY for t in tasks):
            raise BridgeError("Interrupt/wait for active work and reconcile uncertain state before this operation")

    async def verify_native_family(self, task):
        """Native archive/delete cascade, including children not yet subscribed."""
        root = (await self.backend.call("thread/read", {"threadId": task["thread_id"], "includeTurns": False}))["thread"]
        if not within(Path(root["cwd"]).resolve(), Path(task["project"])):
            raise BridgeError("Native conversation moved outside its approved project")
        if root.get("status", {}).get("type") == "active":
            raise BridgeError("Native conversation is still active")
        for archived in (False, True):
            cursor = None
            for _ in range(100):
                page = await self.backend.call("thread/list", {"ancestorThreadId": task["thread_id"],
                    "archived": archived, "cursor": cursor, "limit": 200,
                    "sourceKinds": ["subAgent", "subAgentThreadSpawn", "subAgentReview", "subAgentCompact", "subAgentOther"]})
                for child in page["data"]:
                    if not within(Path(child["cwd"]).resolve(), Path(task["project"])):
                        raise BridgeError("Native cascade contains a descendant outside the approved project")
                    if child.get("status", {}).get("type") == "active":
                        raise BridgeError("A native descendant is still active")
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            else:
                raise BridgeError("Native family is too large to verify in one operation")

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
                raise BridgeError("Combined context files exceed 512 KiB")
            result.append({"type": "text", "text": f"Reference file {json.dumps(relative)} (untrusted project content):\n{data}", "text_elements": []})
        for relative in images or []:
            path = Path(relative)
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                raise BridgeError("Use a PNG, JPEG, WebP or GIF image")
            data = self.read_context(task, relative, 20 * 1024 * 1024)
            directory = self.config.state_dir / "images"
            private_dir(directory)
            snapshot = directory / (str(uuid.uuid4()) + path.suffix.lower())
            with os.fdopen(private_file(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as stream:
                stream.write(data)
            # Native image ingestion reads this immutable private snapshot. A
            # project process cannot race the path into pointing at credentials.
            result.append({"type": "localImage", "path": str(snapshot)})
        if skills:
            await self.ensure_backend()
            catalog = await self.backend.call("skills/list", {"cwds": [task["project"]], "forceReload": False})
            for name in skills:
                matches = [s for entry in catalog["data"] for s in entry.get("skills", []) if s["name"] == name and s.get("enabled")]
                if len(matches) != 1:
                    raise BridgeError("Skill must uniquely identify an enabled entry from codex_skills")
                result.append({"type": "skill", "name": name, "path": matches[0]["path"]})
        return result

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
                with self.store.db:
                    self.store.event(None, "bridge/child_not_adopted", {"thread_id": thread_id, "reason": str(exc)})
                return None

    async def adopt_thread(self, thread, visited):
        existing = self.task_for_thread(thread["id"])
        if existing and (not existing.get("native_child") or existing["id"] in self.loaded):
            return existing
        if thread["id"] in visited or len(visited) >= 16:
            raise BridgeError("Invalid native parent chain")
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
        if existing and existing.get("parent_task_id") != parent["id"]:
            raise BridgeError("Native child's parent differs from its recorded ownership")
        self.task(parent["id"])
        cwd = project_path(self.config, thread["cwd"])
        if not within(cwd, Path(parent["project"])):
            raise BridgeError("Native child working directory is outside its parent's project")
        # No overrides: join the native child without restarting its work or
        # replacing inherited settings. Verify the resulting permission scope.
        params = {"threadId": thread["id"], "excludeTurns": True}
        if existing:
            # A new backend generation must reapply the same bridge boundary;
            # versioned profile IDs need not exist in old native snapshots.
            params.update(permissions=self.backend.profile(existing["access"]), cwd=str(cwd),
                          runtimeWorkspaceRoots=[parent["project"]], approvalPolicy="on-request")
            params.update(self.reviewer_params(existing.get("approval_mode", "inherit")))
        result = await self.backend.call("thread/resume", params)
        child = {**parent, **(existing or {}), "id": existing["id"] if existing else str(uuid.uuid4()), "thread_id": thread["id"],
            "native_cwd": str(cwd), "parent_task_id": parent["id"],
            "root_task_id": parent.get("root_task_id", parent["id"]), "native_child": True,
            "nickname": thread.get("agentNickname"), "agent_role": thread.get("agentRole"),
            "name": thread.get("name") or thread.get("agentNickname"),
            "model": result.get("model", parent["model"]), "effort": result.get("reasoningEffort") or parent["effort"],
            "summary": "", "commands": [], "files": [], "plan": [], "diff": "", "usage": None,
            "turn_id": None, "created_at": existing["created_at"] if existing else time.time(), "updated_at": time.time()}
        source = thread.get("source") or {}
        child["native_agent_path"] = source.get("subAgent", {}).get("thread_spawn", {}).get("agent_path") if isinstance(source, dict) and isinstance(source.get("subAgent"), dict) else None
        # Children inherit whichever native reviewer was active when spawned.
        child["approval_mode"] = "inherit"
        self.verify_thread(child, result)
        self.record_effective(child, result)
        child["status"] = "running" if (result["thread"].get("status") or {}).get("type") == "active" else "idle"
        history = await self.backend.call("thread/turns/list", {"threadId": thread["id"], "limit": 1, "sortDirection": "desc", "itemsView": "summary"})
        if history.get("data"):
            turn = history["data"][0]
            child["turn_id"] = turn["id"]
            if turn["status"] != "inProgress":
                child["status"] = turn["status"]
            for item in turn.get("items", []):
                if item.get("type") == "agentMessage":
                    child["summary"] = item.get("text", "")
        with self.store.db:
            self.store.save("tasks", child)
            self.store.event(parent["id"], "bridge/subagent_discovered", {"task_id": child["id"], "thread_id": child["thread_id"], "nickname": child.get("nickname")})
        self.loaded.add(child["id"])
        await self.wake()
        return child

    async def subagents(self, task_id, cursor=None, limit=50, refresh=True, archived=False):
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
        ids = self.family_ids(task_id) - {task_id}
        return {"subagents": [self.get(i) for i in ids if bool(self.task(i).get("archived")) == archived],
                "nextCursor": result.get("nextCursor"), "note": "Native children are verified descendants, not unrelated sessions"}

    async def delegate(self, task_id, prompt, expected_turn_id=None):
        task = self.task(task_id)
        message = "Use your native Codex subagent tools to carry out this delegation/control request. Preserve the current project and permission boundaries. Report the child agents and results.\n\n" + prompt
        if task["status"] in ("running", "waiting"):
            if not expected_turn_id:
                raise BridgeError("Delegating during a turn requires expected_turn_id")
            return await self.send(task_id, message, "steer", expected_turn_id)
        return await self.send(task_id, message)

    async def capabilities(self, task_id):
        task = self.task(task_id)
        await self.ensure_backend()
        result = {"approval_modes": ["ask", "approve_for_me"], "access": ["read", "write"],
                  "can_accept_direct_input": task.get("can_accept_direct_input"), "codex_version": self.backend.version}
        for key, method, params in [("collaboration_modes", "collaborationMode/list", {}),
                                    ("permission_profiles", "permissionProfile/list", {"cwd": task["project"]})]:
            result[key] = await self.backend.call(method, params)
        return self.safe_payload(result)

    async def settings(self, task_id, scope="get", expected_turn_id=None, model=None, effort=None,
                       approval_mode=None, collaboration_mode=None, service_tier=None, access=None):
        task = self.task(task_id)
        if scope == "get":
            return {k: task.get(k) for k in ("id", "model", "effort", "approval_mode", "approvals_reviewer", "collaboration_mode", "service_tier", "access", "active_settings")}
        await self.resume(task_id)
        task = self.task(task_id)
        values = {k: v for k, v in {"model": model, "effort": effort, "approval_mode": approval_mode,
                  "collaboration_mode": collaboration_mode, "service_tier": service_tier, "access": access}.items() if v is not None}
        if not values:
            raise BridgeError("Provide at least one setting to change")
        candidate = {**task, **values}
        candidate["model"], candidate["effort"] = await self.choose_model(model or task["model"],
            effort or (None if model and model != task["model"] else task["effort"]), service_tier or task.get("service_tier"))
        params = {"threadId": task["thread_id"]}
        if model or effort:
            params.update(model=candidate["model"], effort=candidate["effort"])
        if approval_mode:
            params.update(self.reviewer_params(approval_mode))
        if service_tier:
            params["serviceTier"] = None if service_tier == "default" else service_tier
        if scope == "current":
            if collaboration_mode or access:
                raise BridgeError("Access/collaboration mode changes apply only to subsequent turns")
            if not expected_turn_id or expected_turn_id != task["turn_id"] or task["status"] not in ("running", "waiting"):
                raise BridgeError("Current-turn changes require the active expected_turn_id")
            params["turnId"] = expected_turn_id
            result = await self.backend.call("turn/settings/update", params)
            if result.get("status") != "applied":
                raise BridgeError("Native turn settings were not applied: " + str(result.get("status")))
            task = self.task(task_id)
            task["active_settings"] = {**task.get("active_settings", {}), **values, "acknowledgement": result}
        else:
            if access:
                if task.get("native_child") and access == "write" and self.task(task["parent_task_id"])["access"] == "read":
                    raise BridgeError("A native child cannot broaden its read-only parent's project access")
                self.require_idle(task, family=True)
                params["permissions"] = self.backend.profile(access)
            if collaboration_mode or model or effort:
                params["collaborationMode"] = self.turn_options(candidate)["collaborationMode"]
            await self.backend.call("thread/settings/update", params)
            task = self.task(task_id)
            task.update(values)
            if task.get("service_tier") == "default":
                task["service_tier"] = None
            if model or effort:
                task.update(model=candidate["model"], effort=candidate["effort"])
            if approval_mode:
                task["approvals_reviewer"] = REVIEWERS[approval_mode]
        with self.store.db:
            self.store.save("tasks", task)
            self.store.event(task_id, "bridge/settings_changed", {"scope": scope, "settings": values})
        return self.get(task_id)

    async def fork(self, task_id, name=None, last_turn_id=None):
        source = self.task(task_id)
        self.require_idle(source, family=True)
        await self.ensure_backend()
        options = await self.thread_options(source)
        params = {**options, "threadId": source["thread_id"], "excludeTurns": True, "ephemeral": False, "deferGoalContinuation": True}
        if last_turn_id:
            params["lastTurnId"] = last_turn_id
        result = await self.backend.call("thread/fork", params)
        child = {**source, "id": str(uuid.uuid4()), "thread_id": result["thread"]["id"], "status": "idle",
                 "forked_from_task_id": task_id, "turn_id": None, "created_at": time.time(), "name": name,
                 "archived": False, "deleted": False, "native_child": False}
        for key in ("parent_task_id", "root_task_id"):
            child.pop(key, None)
        self.verify_thread(child, result)
        self.record_effective(child, result)
        with self.store.db:
            self.store.save("tasks", child)
            self.store.event(child["id"], "bridge/forked", {"from_task_id": task_id})
        self.loaded.add(child["id"])
        if name:
            await self.rename(child["id"], name)
        return self.get(child["id"])

    async def rename(self, task_id, name):
        task = self.task(task_id)
        await self.ensure_backend()
        await self.backend.call("thread/name/set", {"threadId": task["thread_id"], "name": name})
        task["name"] = name
        with self.store.db:
            self.store.save("tasks", task)
            self.store.event(task_id, "bridge/renamed", {"name": name})
        return self.get(task_id)

    async def archive(self, task_id, action="archive"):
        task = self.task(task_id)
        if action == "archive":
            self.require_idle(task, family=True)
        await self.ensure_backend()
        await self.verify_native_family(task)
        await self.backend.call("thread/" + action, {"threadId": task["thread_id"]})
        with self.store.db:
            for key in self.family_ids(task_id):
                member = self.store.get("tasks", key)
                member.update(archived=action == "archive", status="archived" if action == "archive" else "idle")
                self.store.save("tasks", member)
                self.loaded.discard(key)
            self.store.event(task_id, "bridge/" + action, {})
        return self.get(task_id)

    async def delete(self, task_id, confirm_thread_id):
        task = self.task(task_id)
        if confirm_thread_id != task["thread_id"]:
            raise BridgeError("Permanent deletion requires the exact native thread_id as confirmation")
        if any(self.store.get("tasks", i)["status"] in BUSY for i in self.family_ids(task_id)):
            raise BridgeError("Stop and reconcile the entire task family before deleting it")
        await self.ensure_backend()
        await self.verify_native_family(task)
        await self.backend.call("thread/delete", {"threadId": task["thread_id"]})
        with self.store.db:
            for key in self.family_ids(task_id):
                member = self.store.get("tasks", key)
                member.update(deleted=True, status="deleted")
                self.store.save("tasks", member)
                self.loaded.discard(key)
            self.store.event(task_id, "bridge/deleted", {"audit_retained": True})
        return {"task_id": task_id, "deleted": True, "audit_retained": True}

    async def compact(self, task_id):
        await self.resume(task_id)
        task = self.task(task_id)
        self.require_idle(task)
        self.capacity(task["project"], exclude=task_id)
        await self.turn_control(task, "thread/compact/start", {"threadId": task["thread_id"]})
        return self.get(task_id)

    async def turn_control(self, task, method, params):
        previous = dict(task)
        with self.store.db:
            task.update(status="starting", turn_id=None, summary="", files=[], commands=[], plan=[], diff="", error=None)
            self.store.save("tasks", task)
        try:
            result = await self.backend.call(method, params)
        except Exception as exc:
            latest = self.task(task["id"])
            if latest["status"] == "starting":
                with self.store.db:
                    self.store.save("tasks", previous if isinstance(exc, RpcError) else {**latest, "status": "uncertain"})
            raise
        if result.get("turn"):
            await self.ack_turn(task["id"], result)
        return result

    async def revert(self, task_id, before_turn_id, confirm_history_only):
        if not confirm_history_only:
            raise BridgeError("Confirm that this removes conversation history, not filesystem changes")
        task = self.task(task_id)
        self.require_idle(task, family=True)
        await self.ensure_backend()
        result = await self.backend.call("thread/revert", {"threadId": task["thread_id"], "beforeTurnId": before_turn_id})
        with self.store.db:
            task.update(status="idle", summary="", turn_id=None, files=[], commands=[], diff="")
            self.store.save("tasks", task)
            self.store.event(task_id, "bridge/reverted", {"before_turn_id": before_turn_id, "files_changed": False})
        return {"task_id": task_id, "history": self.safe_payload(result), "files_changed": False}

    async def queue(self, task_id, action="list", prompt=None, queued_id=None, ordered_ids=None, cursor=None, limit=50,
                    images=None, context_files=None, skills=None, operation_id=None):
        task = self.task(task_id)
        self.assert_writable_session(task)
        await self.ensure_backend()
        params = {"threadId": task["thread_id"]}
        if action in ("add", "update"):
            if not prompt:
                raise BridgeError("Queue add/update requires a prompt")
            params["input"] = await self.build_input(task, prompt, images, context_files, skills)
        if action in ("update", "delete"):
            if not queued_id:
                raise BridgeError("This operation requires queued_id")
            params["queuedSubmissionId"] = queued_id
        if action == "add":
            self.capacity(task["project"], exclude=task_id)
            params["clientUserMessageId"] = operation_id or str(uuid.uuid4())
            if task["status"] not in BUSY:
                history = await self.backend.call("thread/turns/list", {"threadId": task["thread_id"], "limit": 1, "sortDirection": "desc", "itemsView": "summary"})
                previous = next(iter(history.get("data", [])), {})
                task["queued_after_turn_id"] = previous.get("id")
                task.update(status="queued", turn_id=None, summary="")
                with self.store.db:
                    self.store.save("tasks", task)
        elif action == "reorder":
            if not ordered_ids or len(set(ordered_ids)) != len(ordered_ids):
                raise BridgeError("Provide a nonempty list of unique ordered_ids")
            params["queuedSubmissionIds"] = ordered_ids
        elif action == "start":
            await self.resume(task_id)
            task = self.task(task_id)
            self.require_idle({**task, "status": "idle"} if task["status"] == "queued" else task)
            self.capacity(task["project"], exclude=task_id)
            if queued_id:
                params["queuedSubmissionId"] = queued_id
        elif action == "list":
            params.update(cursor=cursor, limit=limit)
        try:
            result = await self.turn_control(task, "thread/queue/start", params) if action == "start" else await self.backend.call("thread/queue/" + action, params)
        except RpcError as exc:
            if "queued submission not found" in str(exc):
                await self.sync_queue(task_id)
                raise BridgeError("Queued instruction is no longer pending and may already be running. Inspect codex_get/history; do not re-add it blindly") from exc
            raise
        if action == "start":
            return await self.ack_turn(task_id, result)
        if action != "list":
            await self.sync_queue(task_id, allow_idle=action == "delete")
        return self.safe_payload(result)

    async def sync_queue(self, task_id, allow_idle=False):
        task = self.task(task_id)
        if task["status"] != "queued":
            return
        page = await self.backend.call("thread/queue/list", {"threadId": task["thread_id"], "limit": 1})
        native = (await self.backend.call("thread/read", {"threadId": task["thread_id"], "includeTurns": False}))["thread"]
        task = self.task(task_id)
        if task["status"] == "queued" and not page.get("data"):
            active = native.get("status", {}).get("type") == "active"
            # Queue consumption can precede turn/started. Keep the project
            # reserved until native turn history or events establish its state.
            history = await self.backend.call("thread/turns/list", {"threadId": task["thread_id"], "limit": 1, "sortDirection": "desc", "itemsView": "summary"})
            task = self.task(task_id)
            if task["status"] != "queued":
                return
            turn = next(iter(history.get("data", [])), None)
            if turn and (active or turn.get("status") == "inProgress"):
                task.update(status="running", turn_id=turn["id"])
            elif turn and turn.get("id") == task.get("queued_after_turn_id"):
                task["status"] = "idle" if allow_idle else "queued"
            elif turn:
                task.update(status=turn["status"], turn_id=turn["id"])
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage":
                        task["summary"] = item.get("text", "")
            elif not active and allow_idle:
                task["status"] = "idle"
            with self.store.db:
                self.store.save("tasks", task)

    async def ack_turn(self, task_id, result):
        task = self.task(task_id)
        turn = result.get("turn") or {}
        # Never overwrite a terminal event already received for this turn.
        if task.get("turn_id") != turn.get("id") or task["status"] not in ("completed", "failed", "interrupted", "waiting"):
            task.update(turn_id=turn.get("id"), status="running")
        with self.store.db:
            self.store.save("tasks", task)
        await self.wake()
        return self.get(task_id)

    async def review(self, task_id, target="uncommittedChanges", branch=None, commit=None, instructions=None):
        await self.resume(task_id)
        task = self.task(task_id)
        self.require_idle(task)
        self.capacity(task["project"], exclude=task_id)
        review_target = {"type": target}
        if target == "baseBranch":
            if not branch:
                raise BridgeError("Base branch is required")
            review_target["branch"] = branch
        elif target == "commit":
            if not commit:
                raise BridgeError("Commit is required")
            review_target["sha"] = commit
        elif target == "custom":
            if not instructions:
                raise BridgeError("Review instructions are required")
            review_target["instructions"] = instructions
        result = await self.turn_control(task, "review/start", {"threadId": task["thread_id"], "target": review_target, "delivery": "inline"})
        return await self.ack_turn(task_id, result)

    async def terminals(self, task_id, action="list", process_id=None, cursor=None, limit=50):
        task = self.task(task_id)
        await self.ensure_backend()
        params = {"threadId": task["thread_id"]}
        if action == "list":
            params.update(cursor=cursor, limit=limit)
        elif action == "terminate":
            if not process_id:
                raise BridgeError("process_id is required")
            params["processId"] = process_id
        return self.safe_payload(await self.backend.call("thread/backgroundTerminals/" + action, params))

    async def goal(self, task_id, action="get", objective=None, status=None, token_budget=None, user_requested=False):
        task = self.task(task_id)
        await self.ensure_backend()
        params = {"threadId": task["thread_id"]}
        if action != "get":
            if not user_requested:
                raise BridgeError("Goal mutations require an explicit user request; do not infer one from a coding task")
            params["origin"] = "user"
            if action == "set":
                self.assert_writable_session(task)
                if status == "active" or (objective is not None and status is None):
                    self.capacity(task["project"], exclude=task_id)
                if objective is not None:
                    params["objective"] = objective
                if status is not None:
                    params["status"] = status
                if token_budget is not None:
                    params["tokenBudget"] = token_budget
                if len(params) == 2:
                    raise BridgeError("Provide an objective, status or budget")
        return self.safe_payload(await self.backend.call("thread/goal/" + action, params))

    async def skills(self, task_id, force_reload=False):
        task = self.task(task_id)
        await self.ensure_backend()
        return self.safe_payload(await self.backend.call("skills/list", {"cwds": [task["project"]], "forceReload": force_reload}))

    async def usage(self):
        await self.ensure_backend()
        return self.safe_payload(await self.backend.call("account/rateLimits/read", {}))

    async def diff(self, task_id):
        task = self.task(task_id)
        return {"task_id": task_id, "turn_id": task.get("turn_id"), "diff": task.get("diff", ""),
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
            raise BridgeError("Import requires an explicit native thread_id")
        if not confirm_stopped:
            raise BridgeError("Confirm that this conversation is stopped in other Codex clients before importing it")
        existing = self.task_for_thread(thread_id)
        if existing:
            if not same_path(existing["project"], project):
                raise BridgeError("Thread does not belong to this project")
            return self.get(existing["id"])
        thread = (await self.backend.call("thread/read", {"threadId": thread_id, "includeTurns": False}))["thread"]
        if not same_path(thread["cwd"], project) or self.native_parent(thread):
            raise BridgeError("Import only a root conversation whose working directory exactly matches the selected project")
        if thread.get("status", {}).get("type") == "active":
            raise BridgeError("Stop the native active conversation before importing it")
        model, effort = await self.choose_model(thread.get("model"), thread.get("reasoningEffort"))
        task = {"id": str(uuid.uuid4()), "thread_id": thread_id, "project_id": descriptor["id"],
                "project": str(project), "model": model, "effort": effort, "access": "write", "approval_mode": "inherit",
                "collaboration_mode": "default", "status": "archived" if archived else "needs_resume", "archived": archived, "turn_id": None,
                "summary": "", "commands": [], "files": [], "plan": [], "usage": None,
                "created_at": time.time(), "name": thread.get("name")}
        if not archived:
            self.capacity(str(project))
        with self.store.db:
            self.store.save("tasks", task)
            self.store.event(task["id"], "bridge/imported", {"thread_id": thread_id})
        return self.get(task["id"]) if archived else await self.resume(task["id"])
