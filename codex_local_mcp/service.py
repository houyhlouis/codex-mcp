"""Persistent task lifecycle, explicit approvals, and replayable progress."""

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .backend import AppServer, RpcError
from .extensions import ExtendedControls
from .security import BridgeError, project_path, redact, within, same_path

ACTIVE = {"starting", "queued", "running", "waiting", "interrupting", "uncertain", "needs_resume"}
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
        "question": {"type": "string"}, "context": {"type": "string"}},
        "required": ["question"], "additionalProperties": False}}


class Service(ExtendedControls):
    def __init__(self, config, store, backend_factory=AppServer):
        self.config, self.store = config, store
        self.backend = backend_factory(config, self.on_message, self.on_exit)
        self.backend_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.loaded = set()
        self.raw_requests = {}
        self.adoption_lock = asyncio.Lock()
        self.shutting_down = False
        with store.db:
            # Preserve old conversations, but replace their static root mapping
            # with explicit per-project records. Paths are rechecked before use.
            for task in store.all("tasks"):
                if not task.get("project_id"):
                    record = self.register_project(Path(task["project"]))
                    task["project_id"] = record["id"]
                    task.pop("root", None)
                    task.pop("path", None)
                    store.save("tasks", task)
            store.db.execute("DELETE FROM metadata WHERE key='workspace_scope'")
        self._recover()

    def _recover(self):
        with self.store.db:
            for task in self.store.all("tasks"):
                if task["status"] in ACTIVE:
                    task["status"] = "needs_resume"
                    self.store.save("tasks", task)
                    self.store.event(task["id"], "bridge/recovery", {
                        "message": "Broker restarted. Resume to reconcile Codex history; no prompt was replayed."})
            for req in self.store.all("requests"):
                if req["status"] in ("pending", "answering"):
                    req["status"] = "expired"
                    self.store.save("requests", req)
            self.store.db.execute("UPDATE operations SET state='uncertain' WHERE state='running'")

    async def wake(self):
        async with self.changed:
            self.changed.notify_all()

    async def ensure_backend(self):
        async with self.backend_lock:
            if not self.backend.alive:
                await self.backend.close()
                self.loaded.clear()
                await self.backend.start()

    def task(self, task_id, allow_deleted=False):
        task = self.store.get("tasks", task_id)
        if task.get("deleted") and not allow_deleted:
            raise BridgeError("This native conversation was deleted; its audit journal remains available")
        descriptor = self.store.get("projects", task["project_id"])
        current = project_path(self.config, descriptor["path"])
        if not same_path(current, task["project"]):
            raise BridgeError("Project location changed; task access denied")
        return task

    def task_for_thread(self, thread_id):
        return next((t for t in self.store.all("tasks") if t.get("thread_id") == thread_id), None)

    def public_request(self, req):
        return {k: v for k, v in req.items() if k not in ("rpc_id", "generation")}

    def requests(self, task_id=None):
        if task_id:
            self.task(task_id, allow_deleted=True)
        scope = self.family_ids(task_id) if task_id else None
        return [self.public_request(r) for r in self.store.all("requests")
                if r["status"] == "pending" and (scope is None or r["task_id"] in scope)]

    def get(self, task_id):
        task = self.task(task_id, allow_deleted=True)
        scope = self.family_ids(task_id)
        children = [t for t in self.store.all("tasks") if t["id"] in scope and t["id"] != task_id]
        return {**task, "pending_requests": [] if task.get("deleted") else self.requests(task_id),
                "family_status": "active" if any(t["status"] in ACTIVE for t in [task, *children]) else "idle",
                "subagents": [{k: t.get(k) for k in ("id", "thread_id", "parent_task_id", "nickname", "status", "can_accept_direct_input")} for t in children],
                "event_cursor": self.store.highwater()}

    def list_tasks(self, offset=0, limit=50):
        visible = []
        for task in reversed(self.store.all("tasks")):
            try:
                self.task(task["id"])
            except (BridgeError, OSError):
                continue
            visible.append(task)
        return {"tasks": visible[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(visible) else None}

    def register_project(self, path):
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-mcp:" + os.path.normcase(str(path))))
        try:
            return self.store.get("projects", key)
        except BridgeError:
            record = {"id": key, "project_id": key, "path": str(path), "created_at": time.time()}
            self.store.save("projects", record)
            return record

    def select_project(self, workdir=None, project_id=None, create=False):
        if bool(workdir) == bool(project_id):
            raise BridgeError("Supply exactly one existing workdir or selected project_id")
        value = workdir if workdir else self.store.get("projects", project_id)["path"]
        path = project_path(self.config, value, create=create)
        with self.store.db:
            return self.register_project(path)

    def projects(self, action="list", path=None, offset=0, limit=100):
        if action == "list":
            entries = self.store.all("projects")
            return {"projects": entries[offset:offset + limit],
                    "next_offset": offset + limit if offset + limit < len(entries) else None,
                    "note": "Projects explicitly selected or created by the main agent"}
        if action not in ("select", "create") or not path:
            raise BridgeError("select/create requires an absolute path chosen by the main agent")
        return self.select_project(workdir=path, create=action == "create")

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
            raise BridgeError("Model catalog pagination did not terminate")
        selected = next((m for m in models if model in (m["id"], m["model"])), None) if model else next((m for m in models if m["isDefault"]), None)
        if not selected:
            raise BridgeError("Model is not in the visible Codex catalog; call codex_models")
        selected_effort = effort or selected["defaultReasoningEffort"]
        if selected_effort not in [e["reasoningEffort"] for e in selected["supportedReasoningEfforts"]]:
            raise BridgeError("Unsupported reasoning effort for this model; call codex_models")
        if service_tier is not None and service_tier != "default" and service_tier not in [x["id"] for x in selected.get("serviceTiers", [])]:
            raise BridgeError("Service tier is not offered for this model")
        return selected["model"], selected_effort

    async def status(self):
        await self.ensure_backend()
        account = await self.backend.call("account/read", {"refreshToken": False})
        return {"codex_version": self.backend.version, "backend": "app-server/stdio",
                "signed_in": bool(account.get("account")),
                "authentication_type": (account.get("account") or {}).get("type"),
                "requires_openai_auth": account.get("requiresOpenaiAuth"),
                "approval_relay": self.config.approval_relay,
                "allow_external_tools": self.config.allow_external_tools,
                "event_cursor": self.store.highwater(), "pending_requests": self.requests(),
                "catalog_is_not_entitlement": True}

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
        result.update(self.reviewer_params(task.get("approval_mode", "inherit")))
        if task.get("service_tier"):
            result["serviceTier"] = task["service_tier"]
        return result

    def capacity(self, project, exclude=None):
        family = self.store.get("tasks", exclude).get("root_task_id", exclude) if exclude else None
        active = [t for t in self.store.all("tasks") if t["status"] in ACTIVE and t.get("root_task_id", t["id"]) != family]
        if len({t.get("root_task_id", t["id"]) for t in active}) >= self.config.max_tasks:
            raise BridgeError("Concurrent task limit reached; interrupt or finish another task")
        if any(within(Path(t["project"]), Path(project)) or within(Path(project), Path(t["project"])) for t in active):
            raise BridgeError("Another task is active in this project. Use a separate project/worktree or finish it first")

    async def operation(self, key, name, args, function):
        fingerprint = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
        # Serialize short control operations, never the lifetime of a coding turn.
        async with self.mutation_lock:
            row = self.store.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise BridgeError("request_id was already used with different arguments")
                if row["state"] == "done":
                    return json.loads(row["result"])
                error = (json.loads(row["result"]) if row["result"] else {}).get("error")
                detail = f" (it failed with: {error})" if error else ""
                target = f"task {row['task_id']}" if row["task_id"] else "codex_list"
                raise BridgeError(f"Previous operation with this request_id is {row['state']}{detail}. "
                                  f"Inspect {target} and its events, then use a new request_id if more work is needed")
            with self.store.db:
                self.store.db.execute("INSERT INTO operations(id,fingerprint,state,task_id) VALUES (?,?,'running',?)",
                                      (key, fingerprint, args.get("task_id")))
            try:
                result = await function()
            except Exception as exc:
                with self.store.db:
                    self.store.db.execute("UPDATE operations SET state='uncertain',result=? WHERE id=?",
                                          (json.dumps({"error": redact(str(exc))}), key))
                raise
            with self.store.db:
                self.store.db.execute("UPDATE operations SET state='done',result=? WHERE id=?",
                                      (json.dumps(redact(result)), key))
            return result

    async def start(self, prompt, request_id, workdir=None, project_id=None, model=None, effort=None, access="write",
                    approval_mode="inherit", collaboration_mode="default", service_tier=None,
                    images=None, context_files=None, skills=None, output_schema=None, name=None):
        descriptor = self.select_project(workdir, project_id)
        project = Path(descriptor["path"])
        self.capacity(str(project))
        model, effort = await self.choose_model(model, effort, service_tier)
        if service_tier == "default":
            # "default" means no explicit tier; it is not a catalog tier id.
            service_tier = None
        task = {"id": str(uuid.uuid4()), "thread_id": None, "project_id": descriptor["id"], "project": str(project),
                "model": model, "effort": effort, "access": access, "status": "starting",
                "turn_id": None, "created_at": time.time(), "updated_at": time.time(),
                "summary": "", "files": [], "commands": [], "plan": [], "usage": None,
                "approval_mode": approval_mode, "collaboration_mode": collaboration_mode,
                "service_tier": service_tier, "name": name, "archived": False}
        inputs = await self.build_input(task, prompt, images, context_files, skills)
        with self.store.db:
            self.store.save("tasks", task)
            self.store.db.execute("UPDATE operations SET task_id=? WHERE id=?", (task["id"], request_id))
            self.store.event(task["id"], "bridge/task_created", {"project": str(project)})
        try:
            options = await self.thread_options(task)
            result = await self.backend.call("thread/start", {**options, "ephemeral": False,
                                             "historyMode": "paginated", "dynamicTools": [PARENT_TOOL]})
            task["thread_id"] = result["thread"]["id"]
            # Verify the server actually selected the restrictive profile. No
            # fallback to legacy workspace-write (which allows broad reads).
            with self.store.db:
                self.store.save("tasks", task)
            self.verify_thread(task, result)
            self.record_effective(task, result)
            with self.store.db:
                self.store.save("tasks", task)
            self.loaded.add(task["id"])
            if name:
                await self.backend.call("thread/name/set", {"threadId": task["thread_id"], "name": name})
            return await self.start_turn(task, prompt, inputs, output_schema)
        except Exception:
            latest = self.store.get("tasks", task["id"])
            latest["status"] = "needs_resume" if latest.get("thread_id") else "failed"
            with self.store.db:
                self.store.save("tasks", latest)
                self.store.event(task["id"], "bridge/start_failed", {"message": "Start did not finish; inspect status before retrying"})
            await self.wake()
            raise

    def verify_thread(self, task, result):
        profile = (result.get("activePermissionProfile") or {}).get("id")
        if profile != self.backend.profile(task["access"]):
            raise BridgeError("Codex did not confirm the required permission profile; refusing to start work")
        if not result.get("cwd") or not same_path(result["cwd"], task.get("native_cwd", task["project"])):
            raise BridgeError("Codex returned an unexpected working directory")
        roots = result.get("runtimeWorkspaceRoots")
        if not roots or len(roots) != 1 or not same_path(roots[0], task["project"]):
            raise BridgeError("Codex did not confirm the exact project workspace roots")
        if result.get("approvalPolicy") != "on-request":
            raise BridgeError("Codex did not preserve the requested approval policy")
        expected = self.reviewer_params(task.get("approval_mode", "inherit")).get("approvalsReviewer")
        actual = result.get("approvalsReviewer")
        if actual == "guardian_subagent":
            actual = "auto_review"
        if expected and actual != expected:
            raise BridgeError("Codex did not confirm the requested approval reviewer")

    async def start_turn(self, task, prompt, inputs=None, output_schema=None):
        self.assert_writable_session(task)
        if task.get("can_accept_direct_input") is False:
            raise BridgeError("Codex owns this child lifecycle; send instructions through its parent using codex_delegate")
        with self.store.db:
            task.update(status="starting", summary="", files=[], commands=[], plan=[], turn_id=None, error=None)
            self.store.save("tasks", task)
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
            result = await self.backend.call("turn/start", params)
            latest = self.store.get("tasks", task["id"])
            # Events may arrive before the response. Never overwrite a completed
            # turn or a waiting request with an earlier running snapshot.
            if latest["status"] == "starting":
                latest["status"] = "running"
            latest["turn_id"] = result["turn"]["id"]
            latest["updated_at"] = time.time()
            with self.store.db:
                self.store.save("tasks", latest)
            await self.wake()
            return self.get(task["id"])
        except Exception:
            latest = self.store.get("tasks", task["id"])
            if latest["status"] in ("starting", "running"):
                with self.store.db:
                    latest["status"] = "uncertain"
                    self.store.save("tasks", latest)
            raise

    async def resume(self, task_id):
        task = self.task(task_id)
        self.assert_writable_session(task)
        if not task.get("thread_id"):
            raise BridgeError("No Codex thread was created for this task")
        await self.ensure_backend()
        if task_id in self.loaded and task["status"] != "uncertain":
            return self.get(task_id)
        self.capacity(task["project"], exclude=task_id)
        options = await self.thread_options(task)
        result = await self.backend.call("thread/resume", {**options,
            "threadId": task["thread_id"], "excludeTurns": True})
        self.verify_thread(task, result)
        task = self.task(task_id)
        self.record_effective(task, result)
        with self.store.db:
            self.store.save("tasks", task)
        self.loaded.add(task_id)
        # Reconcile a lost turn/start acknowledgement or an app-server restart
        # against persisted turns without replaying a prompt.
        history = await self.backend.call("thread/turns/list", {"threadId": task["thread_id"],
            "limit": 1, "sortDirection": "desc", "itemsView": "summary"})
        latest = self.store.get("tasks", task_id)
        if latest["status"] in ("needs_resume", "uncertain", "starting"):
            turn = next(iter(history.get("data", [])), None)
            active = result["thread"].get("status", {}).get("type") == "active"
            latest["turn_id"] = turn["id"] if turn else None
            latest["status"] = "running" if active else "idle"
            if turn:
                latest["recovered_turn_status"] = turn["status"]
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage":
                        latest["summary"] = item.get("text", "")
        with self.store.db:
            self.store.save("tasks", latest)
            self.store.event(task_id, "bridge/resumed", {"message": "Conversation loaded; review history before continuing interrupted work"})
        await self.wake()
        return self.get(task_id)

    async def send(self, task_id, prompt, mode="follow_up", expected_turn_id=None, model=None, effort=None,
                   images=None, context_files=None, skills=None, output_schema=None):
        task = self.task(task_id)
        self.assert_writable_session(task)
        if task.get("can_accept_direct_input") is False:
            raise BridgeError("This native child does not accept direct input; use codex_delegate on its parent")
        inputs = await self.build_input(task, prompt, images, context_files, skills)
        if mode == "steer":
            if task["status"] not in ("running", "waiting") or not expected_turn_id or task["turn_id"] != expected_turn_id:
                raise BridgeError("Steering requires the current active expected_turn_id")
            if model or effort or output_schema:
                raise BridgeError("Change model/effort on a follow-up turn, not during steering")
            if not self.backend.alive or task_id not in self.loaded:
                raise BridgeError("Resume and reconcile the disconnected task before steering")
            result = await self.backend.call("turn/steer", {"threadId": task["thread_id"],
                "expectedTurnId": expected_turn_id,
                "input": inputs})
            with self.store.db:
                self.store.event(task_id, "bridge/steered", {"text": prompt, "result": result})
            await self.wake()
            return self.get(task_id)
        if task["status"] in ("running", "waiting", "starting", "queued", "interrupting"):
            raise BridgeError("Task is active; use steer, or interrupt and wait for completion")
        await self.resume(task_id)
        task = self.task(task_id)
        if task["status"] in ("running", "waiting", "starting", "queued", "interrupting"):
            raise BridgeError("Resume found an active turn; use steer or interrupt it before sending a follow-up")
        self.capacity(task["project"], exclude=task_id)
        task["model"], task["effort"] = await self.choose_model(model or task["model"],
            effort if effort else (None if model and model != task["model"] else task["effort"]))
        return await self.start_turn(task, prompt, inputs, output_schema)

    async def interrupt(self, task_id, expected_turn_id):
        task = self.task(task_id)
        if task["status"] not in ("running", "waiting", "interrupting") or task["turn_id"] != expected_turn_id:
            raise BridgeError("Interrupt requires the current active expected_turn_id")
        if not self.backend.alive:
            raise BridgeError("Codex disconnected; resume to reconcile its history")
        await self.backend.call("turn/interrupt", {"threadId": task["thread_id"], "turnId": expected_turn_id})
        task = self.task(task_id)
        if task["status"] in ("running", "waiting"):
            with self.store.db:
                task["status"] = "interrupting"
                self.store.save("tasks", task)
        return self.get(task_id)

    async def history(self, task_id, kind="turns", cursor=None, limit=50, turn_id=None):
        task = self.task(task_id)
        if not task.get("thread_id"):
            raise BridgeError("Task has no Codex thread")
        await self.ensure_backend()
        params = {"threadId": task["thread_id"], "cursor": cursor, "limit": limit,
                  "sortDirection": "asc"}
        if kind == "items":
            params["turnId"] = turn_id
        else:
            params["itemsView"] = "summary"
        result = await self.backend.call("thread/" + kind + "/list", params)
        return self.safe_payload(result)

    @staticmethod
    def safe_payload(value):
        if isinstance(value, dict):
            if value.get("type") == "reasoning":
                value = {k: v for k, v in value.items() if k != "content"}
            return redact({k: Service.safe_payload(v) for k, v in value.items()})
        if isinstance(value, list):
            return [Service.safe_payload(v) for v in value]
        return redact(value)

    async def events(self, after=0, limit=100, wait_seconds=0, task_id=None, include_descendants=True):
        if task_id:
            self.task(task_id, allow_deleted=True)
        scope = self.family_ids(task_id) if task_id and include_descendants else None
        async with self.changed:
            result = self.store.events(after, limit, task_id, scope)
            if not result["events"] and wait_seconds and not self.requests(task_id):
                try:
                    await asyncio.wait_for(self.changed.wait(), wait_seconds)
                except TimeoutError:
                    pass
                scope = self.family_ids(task_id) if task_id and include_descendants else None
                result = self.store.events(after, limit, task_id, scope)
        result["pending_requests"] = self.requests(task_id)
        if task_id:
            result["task_status"] = self.task(task_id, allow_deleted=True)["status"]
        return result

    async def on_exit(self, reason):
        self.loaded.clear()
        self.raw_requests.clear()
        with self.store.db:
            for task in self.store.all("tasks"):
                if task["status"] in ACTIVE:
                    task["status"] = "needs_resume"
                    self.store.save("tasks", task)
                    self.store.event(task["id"], "bridge/disconnected", {"reason": reason,
                        "event_gap_possible": True, "recovery": "Resume, then page through codex_history"})
            for req in self.store.all("requests"):
                if req["status"] in ("pending", "answering"):
                    req["status"] = "expired"
                    self.store.save("requests", req)
        await self.wake()

    async def on_message(self, msg):
        method, params = msg.get("method", ""), msg.get("params") or {}
        task = self.task_for_thread(params.get("threadId") or (params.get("thread") or {}).get("id"))
        if (not task or (task.get("native_child") and task["id"] not in self.loaded)) and (params.get("threadId") or method == "thread/started"):
            task = await self.adopt_message_thread(params)
        if task and method == "thread/queue/changed" and task["status"] == "queued":
            await self.sync_queue(task["id"])
            task = self.task(task["id"])
        if "id" in msg:
            if not task or method not in REQUEST_KINDS:
                await self.backend.respond(msg["id"], error={"code": -32601,
                    "message": "Unsupported or unowned request; no permission granted"})
                with self.store.db:
                    self.store.event(task["id"] if task else None, "bridge/request_rejected", {"method": method})
                await self.wake()
                return
            if method == "item/tool/call" and params.get("tool") != "ask_parent":
                await self.backend.respond(msg["id"], {"success": False,
                    "contentItems": [{"type": "inputText", "text": "Unknown bridge tool"}]})
                return
            if method == "item/fileChange/requestApproval":
                # Codex sends the patch in item/started, before the approval.
                # Include it in the human review so the decision is concrete.
                rows = self.store.db.execute("SELECT data FROM events WHERE task_id=? AND kind='item/started' ORDER BY seq DESC LIMIT 200", (task["id"],))
                for row in rows:
                    item = json.loads(row[0]).get("item", {})
                    if item.get("id") == params.get("itemId"):
                        params = {**params, "proposedChanges": item.get("changes", [])}
                        break
            request = {"id": str(uuid.uuid4()), "task_id": task["id"], "method": method,
                       "kind": REQUEST_KINDS[method], "params": self.safe_payload(params),
                       "rpc_id": msg["id"], "generation": self.backend.generation,
                       "status": "pending", "created_at": time.time()}
            request["parent_task_id"] = task.get("parent_task_id")
            request["root_task_id"] = task.get("root_task_id", task["id"])
            request["requires_human"] = request["kind"] != "parent_question"
            request["blocking"] = params.get("isBlocking", True)
            self.raw_requests[request["id"]] = params
            with self.store.db:
                self.store.save("requests", request)
                self.store.event(task["id"], "bridge/request", self.public_request(request))
                if request["blocking"]:
                    task["status"] = "waiting"
                self.store.save("tasks", task)
            await self.wake()
            return
        # Raw internal reasoning and legacy duplicate event streams are not public progress.
        if method.startswith("codex/event/") or method == "item/reasoning/textDelta":
            return
        if not task:
            if method in ("warning", "configWarning", "error", "deprecationNotice"):
                with self.store.db:
                    self.store.event(None, method, self.safe_payload(params))
                await self.wake()
            return
        with self.store.db:
            safe = self.safe_payload(params)
            self.store.event(task["id"], method, safe)
            if method == "turn/started":
                task.update(status="running", turn_id=params["turn"]["id"])
            elif method == "thread/archived":
                task.update(archived=True, status="archived")
            elif method == "thread/unarchived":
                task.update(archived=False, status="idle")
            elif method == "thread/deleted":
                task.update(deleted=True, status="deleted")
            elif method == "thread/status/changed":
                task["native_status"] = params.get("status")
            elif method == "turn/diff/updated":
                task["diff"] = safe.get("diff", "")
            elif method.startswith("item/autoApprovalReview/") or method.startswith("autoApprovalReview/"):
                task["last_automatic_review"] = {"method": method, "data": safe}
            elif method == "turn/completed":
                task["status"] = params["turn"]["status"]
                task["turn_id"] = params["turn"]["id"]
                task["error"] = self.safe_payload(params["turn"].get("error"))
                for req in self.store.all("requests"):
                    if req["task_id"] == task["id"] and req["status"] in ("pending", "answering"):
                        req["status"] = "expired"
                        self.store.save("requests", req)
                        self.raw_requests.pop(req["id"], None)
            elif method == "serverRequest/resolved":
                for req in self.store.all("requests"):
                    if req["task_id"] == task["id"] and req["rpc_id"] == params.get("requestId") and req["status"] in ("pending", "answering"):
                        req["status"] = "resolved"
                        self.store.save("requests", req)
                        self.raw_requests.pop(req["id"], None)
                if task["status"] == "waiting" and not any(r["blocking"] for r in self.requests(task["id"])):
                    task["status"] = "running"
            elif method == "item/completed":
                item = safe["item"]
                if item["type"] == "agentMessage":
                    task["summary"] = item.get("text", "")
                elif item["type"] == "fileChange":
                    task["files"] = sorted(set(task["files"] + [c["path"] for c in item.get("changes", [])]))
                elif item["type"] == "commandExecution":
                    task["commands"] = (task["commands"] + [{k: item.get(k) for k in
                        ("id", "command", "cwd", "status", "exitCode", "durationMs")}])[-100:]
            elif method == "turn/plan/updated":
                task["plan"] = safe.get("plan", [])
            elif method == "thread/tokenUsage/updated":
                task["usage"] = safe.get("tokenUsage")
            task["updated_at"] = time.time()
            self.store.save("tasks", task)
        await self.wake()

    def review_request(self, request_id):
        req = self.store.get("requests", request_id)
        self.task(req["task_id"])
        if req["status"] != "pending" or req["generation"] != self.backend.generation or not self.backend.alive:
            raise BridgeError("Request is resolved, expired, or disconnected; refresh codex_requests")
        return req

    async def respond(self, request_id, response, *, human=False):
        req = self.review_request(request_id)
        params = self.raw_requests.get(request_id)
        if params is None:
            raise BridgeError("Live request is unavailable; resume and request fresh approval")
        result = self.validate_response(req, params, response)
        if req["requires_human"] and not human and not self.config.approval_relay and not self.is_denial(req["kind"], result):
            # Denials are always safe. Positive answers require a user-facing UI
            # or an operator-configured trusted relay.
            raise BridgeError("Human review required. Use codex_review_request or the local review command; approval_relay is disabled")
        with self.store.db:
            req["status"] = "answering"
            self.store.save("requests", req)
        try:
            await self.backend.respond(req["rpc_id"], result)
        except Exception:
            with self.store.db:
                req["status"] = "expired"
                self.store.save("requests", req)
            raise
        with self.store.db:
            req["status"] = "answered"
            self.store.save("requests", req)
            # Answers (especially secrets) are not journaled. Codex may persist
            # them itself as part of its conversation history.
            self.store.event(req["task_id"], "bridge/response_sent", {"request_id": request_id,
                "kind": req["kind"], "human_review": human})
            task = self.task(req["task_id"])
            if task["status"] == "waiting" and not any(r["blocking"] for r in self.requests(task["id"])):
                task["status"] = "running"
                self.store.save("tasks", task)
        self.raw_requests.pop(request_id, None)
        await self.wake()
        return {"request_id": request_id, "status": "sent", "note": "Codex remains responsible for enforcing its policies"}

    def validate_response(self, req, params, response):
        if not isinstance(response, dict):
            raise BridgeError("Response must be an object")
        kind = req["kind"]
        if kind in ("command_approval", "file_approval"):
            if set(response) != {"decision"} or response["decision"] not in ("accept", "decline", "cancel"):
                raise BridgeError("Only one-shot accept, decline, or cancel decisions are supported")
            decisions = params.get("availableDecisions")
            if decisions is not None and response["decision"] not in decisions:
                raise BridgeError("Decision was not offered by Codex")
            if kind == "file_approval" and response["decision"] == "accept":
                project = Path(self.task(req["task_id"])["project"])
                paths = [c["path"] for c in params.get("proposedChanges", [])]
                if params.get("grantRoot"):
                    paths.append(params["grantRoot"])
                if not paths or any(not within((project / p).resolve(), project) for p in paths):
                    raise BridgeError("File approval lacks an in-project patch or would grant access outside this project")
            return response
        if kind == "parent_question":
            if set(response) != {"text"} or not isinstance(response["text"], str) or not response["text"].strip():
                raise BridgeError("Parent response requires nonempty text")
            return {"success": True, "contentItems": [{"type": "inputText", "text": response["text"]}]}
        if kind == "user_input":
            if any(q.get("isSecret") for q in params["questions"]):
                raise BridgeError("Secret answers are not accepted by the bridge. Use Codex's native authentication or the provider's browser flow")
            if set(response) != {"answers"} or not isinstance(response["answers"], dict):
                raise BridgeError("Use answers: {question_id: {answers: [text]}}")
            expected = {q["id"] for q in params["questions"]}
            if set(response["answers"]) != expected:
                raise BridgeError("Answer every question id exactly once")
            for answer in response["answers"].values():
                if (not isinstance(answer, dict) or set(answer) != {"answers"}
                    or not isinstance(answer["answers"], list) or not answer["answers"]
                    or any(not isinstance(s, str) for s in answer["answers"])):
                    raise BridgeError("Each question requires an answers array of strings")
            return response
        if kind == "permissions":
            if set(response) - {"permissions", "scope"} or response.get("scope", "turn") != "turn":
                raise BridgeError("Only turn-scoped permission subsets are supported")
            grants = response.get("permissions")
            if not isinstance(grants, dict) or not self.subset(grants, params["permissions"]):
                raise BridgeError("Grant must be a subset of the requested permissions")
            task = self.task(req["task_id"])
            fs = grants.get("fileSystem") or {}
            paths = list(fs.get("read") or []) + list(fs.get("write") or [])
            for entry in fs.get("entries") or []:
                if entry["path"].get("type") != "path":
                    raise BridgeError("Special/glob permission grants are not exposed")
                paths.append(entry["path"]["path"])
            project = Path(task["project"])
            if any(not within((project / p).resolve(), project) for p in paths):
                raise BridgeError("Filesystem grants must stay within this task's project")
            return {"permissions": grants, "scope": "turn"}
        if kind == "elicitation":
            if set(response) - {"action", "content"} or response.get("action") not in ("accept", "decline", "cancel"):
                raise BridgeError("Elicitation requires accept, decline, or cancel")
            if response["action"] != "accept":
                return {"action": response["action"], "content": None}
            if params.get("mode") == "url":
                return {"action": "accept", "content": None}
            from .tools import validate
            validate(response.get("content"), params.get("requestedSchema", {}))
            return response
        raise BridgeError("Unsupported response kind")

    @staticmethod
    def is_denial(kind, result):
        """True for a validated response that grants nothing."""
        if kind in ("command_approval", "file_approval"):
            return result.get("decision") in ("decline", "cancel")
        if kind == "permissions":
            return all(value is None for value in result.get("permissions", {}).values())
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
        await self.backend.close()
