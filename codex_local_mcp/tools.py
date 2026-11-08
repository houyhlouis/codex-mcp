"""Small, closed MCP surface. There is deliberately no raw RPC or shell tool."""

from .security import BridgeError


def string(description="", **kw):
    return {"type": "string", "description": description, "minLength": 1, "maxLength": 100000, **kw}


def integer(default, minimum=0, maximum=100):
    return {"type": "integer", "default": default, "minimum": minimum, "maximum": maximum}


def tool(name, description, properties, required=(), read_only=False):
    return {"name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                        "idempotentHint": read_only, "openWorldHint": True}}


TASK = string("Task id returned by codex_start")
KEY = string("Unique operation key. Reuse only with identical arguments after a lost response", maxLength=200)
CURSOR = string("Opaque cursor returned by Codex")
APPROVAL_MODE = string("Native Codex reviewer; approve_for_me uses auto_review without disabling sandboxing", enum=["inherit", "ask", "approve_for_me"])
INPUTS = {"images": {"type": "array", "items": string(), "maxItems": 10},
          "context_files": {"type": "array", "items": string(), "maxItems": 20},
          "skills": {"type": "array", "items": string(), "maxItems": 10},
          "output_schema": {"type": "object", "additionalProperties": True}}
PROJECT = {"workdir": string("Absolute existing project directory chosen by the main agent"),
           "project_id": string("Project id returned by codex_projects; use instead of workdir")}
TOOLS = [
    tool("codex_status", "Check installed Codex, sign-in type, pending requests and broker health. No credentials are returned.", {}, read_only=True),
    tool("codex_projects", "The main agent chooses an absolute directory to select or create. Returns a project_id for tasks; no workspace configuration is required. List returns previously selected projects, not the whole filesystem.",
         {"action": string(enum=["list", "select", "create"], default="list"),
          "path": string("Absolute directory to select/create"), "offset": integer(0, maximum=1000000), "limit": integer(100, 1, 200)}),
    tool("codex_models", "Discover model ids, supported reasoning efforts and defaults. Catalog visibility is not an entitlement guarantee.",
         {"cursor": CURSOR, "limit": integer(100, 1, 200)}, read_only=True),
    tool("codex_start", "Start a persistent Codex conversation and return promptly with a task id. Poll codex_events and handle pending_requests until the turn finishes.",
         {**PROJECT, "prompt": string(), "request_id": KEY,
          "model": string(), "effort": string(), "access": string(enum=["read", "write"], default="write"),
          "approval_mode": {**APPROVAL_MODE, "default": "inherit"},
          "collaboration_mode": string(enum=["default", "plan"], default="default"),
          "service_tier": string(), "name": string(maxLength=200), **INPUTS},
         ["prompt", "request_id"]),
    tool("codex_list", "List this broker's persistent tasks, newest first.",
         {"offset": integer(0, maximum=1000000), "limit": integer(50, 1, 200)}, read_only=True),
    tool("codex_get", "Read task state, latest agent summary, changed files, command exit codes, plan, usage and pending questions. Summary is agent-authored; command evidence is separate.",
         {"task_id": TASK}, ["task_id"], read_only=True),
    tool("codex_events", "Replay durable progress after a cursor; optionally wait up to 25 seconds. Save returned cursor only after processing the page. Drain has_more before waiting. Pending requests are always included.",
         {"task_id": TASK, "after": integer(0, maximum=2**53-1), "limit": integer(100, 1, 200),
          "wait_seconds": integer(0, maximum=25), "include_descendants": {"type": "boolean", "default": True}}, read_only=True),
    tool("codex_send", "Send a follow-up turn or steer active work. Steering needs the current expected_turn_id; follow-ups can select a supported model/effort.",
         {"task_id": TASK, "prompt": string(), "mode": string(enum=["follow_up", "steer"], default="follow_up"),
          "expected_turn_id": string(), "model": string(), "effort": string(), "request_id": KEY, **INPUTS},
         ["task_id", "prompt", "request_id"]),
    tool("codex_resume", "Reload a saved conversation after backend/broker restart. Does not replay a prompt or restart interrupted commands. Inspect codex_history, then send a follow-up.",
         {"task_id": TASK, "request_id": KEY}, ["task_id", "request_id"]),
    tool("codex_interrupt", "Ask Codex to interrupt the specified active turn. Poll for interrupted/completed; interrupt acknowledgement alone is not completion.",
         {"task_id": TASK, "expected_turn_id": string(), "request_id": KEY}, ["task_id", "expected_turn_id", "request_id"]),
    tool("codex_history", "Page through Codex's persisted turns or items to recover history, including after a broker outage. Only broker-owned conversations are exposed.",
         {"task_id": TASK, "kind": string(enum=["turns", "items"], default="turns"),
          "cursor": CURSOR, "limit": integer(50, 1, 200), "turn_id": string()}, ["task_id"], True),
    tool("codex_requests", "List pending Codex approvals, user questions and ask_parent requests. Never infer approval from silence or timeout.",
         {"task_id": TASK}, read_only=True),
    tool("codex_respond", "Reply to ask_parent with {text: ...}; deny approvals, or relay human responses when the operator enabled approval_relay. No session-wide grants or rule changes. See tool reference for response shapes.",
         {"request_id": string("Pending request id, not an operation key"),
          "response": {"type": "object", "additionalProperties": True}}, ["request_id", "response"]),
    tool("codex_review_request", "Present one pending request through the client's human elicitation UI. If unavailable, leave it pending and use the local review command. Secrets require native authentication outside this bridge.",
         {"request_id": string()}, ["request_id"]),
    tool("codex_capabilities", "Discover native collaboration/permission options and direct-input capability for a task.",
         {"task_id": TASK}, ["task_id"], True),
    tool("codex_settings", "Read/change task model, effort, native approval reviewer, plan mode, service tier or project access. scope=current affects only a running turn; next affects future turns. Existing pending approvals keep their reviewer. Mutations need request_id.",
         {"task_id": TASK, "scope": string(enum=["get", "next", "current"], default="get"),
          "expected_turn_id": string(), "model": string(), "effort": string(),
          "approval_mode": string(enum=["ask", "approve_for_me"]),
          "collaboration_mode": string(enum=["default", "plan"]), "service_tier": string(),
          "access": string(enum=["read", "write"]), "request_id": KEY}, ["task_id"]),
    tool("codex_subagents", "Discover/subscribe to verified native descendants. Children get task ids usable with get, events, history, send and interrupt where Codex permits direct input. Parent polling includes child approvals.",
         {"task_id": TASK, "cursor": CURSOR, "limit": integer(50, 1, 200),
          "refresh": {"type": "boolean", "default": True}, "archived": {"type": "boolean", "default": False}}, ["task_id"], True),
    tool("codex_delegate", "Ask a Codex parent to spawn/control its native subagents. This sends an explicit delegation instruction; Codex performs the spawning. For a running parent provide expected_turn_id. Discover children with codex_subagents/events.",
         {"task_id": TASK, "prompt": string(), "expected_turn_id": string(), "request_id": KEY}, ["task_id", "prompt", "request_id"]),
    tool("codex_fork", "Fork an idle owned conversation, optionally through a selected turn. Keeps project boundaries; does not start work or automatically continue a goal.",
         {"task_id": TASK, "name": string(maxLength=200), "last_turn_id": string(), "request_id": KEY}, ["task_id", "request_id"]),
    tool("codex_rename", "Rename an owned native conversation.", {"task_id": TASK, "name": string(maxLength=200), "request_id": KEY}, ["task_id", "name", "request_id"]),
    tool("codex_archive", "Archive or restore an idle conversation and its native descendants. Transcripts are retained.",
         {"task_id": TASK, "action": string(enum=["archive", "unarchive"], default="archive"), "request_id": KEY}, ["task_id", "request_id"]),
    tool("codex_delete", "Permanently delete an owned native conversation and descendants. Requires the exact native thread_id in confirm_thread_id and an idle family. Local bridge audit records remain.",
         {"task_id": TASK, "confirm_thread_id": string(), "request_id": KEY}, ["task_id", "confirm_thread_id", "request_id"]),
    tool("codex_compact", "Request native history compaction for an idle conversation; may consume model usage. Poll completion events.",
         {"task_id": TASK, "request_id": KEY}, ["task_id", "request_id"]),
    tool("codex_revert", "Remove a selected turn and all later native conversation history. DOES NOT revert files. Requires explicit acknowledgement; local event audit remains.",
         {"task_id": TASK, "before_turn_id": string(), "confirm_history_only": {"type": "boolean"}, "request_id": KEY},
         ["task_id", "before_turn_id", "confirm_history_only", "request_id"]),
    tool("codex_queue", "List/add/update/delete/reorder/start native queued instructions. Mutation actions require request_id. Queues survive reconnect; starting uses the selected thread's settings.",
         {"task_id": TASK, "action": string(enum=["list", "add", "update", "delete", "reorder", "start"], default="list"),
          "prompt": string(), "queued_id": string(), "ordered_ids": {"type": "array", "items": string(), "maxItems": 100},
          "cursor": CURSOR, "limit": integer(50, 1, 200), "request_id": KEY,
          **{k: v for k, v in INPUTS.items() if k != "output_schema"}}, ["task_id"]),
    tool("codex_review", "Start native code review of uncommitted changes, a base branch, a commit or custom instructions on an idle task. Poll for findings/results.",
         {"task_id": TASK, "target": string(enum=["uncommittedChanges", "baseBranch", "commit", "custom"], default="uncommittedChanges"),
          "branch": string(), "commit": string(), "instructions": string(), "request_id": KEY}, ["task_id", "request_id"]),
    tool("codex_terminals", "List or stop native background terminals scoped to one owned task. No arbitrary shell execution or stdin injection. Mutations require request_id.",
         {"task_id": TASK, "action": string(enum=["list", "terminate", "clean"], default="list"),
          "process_id": string(), "cursor": CURSOR, "limit": integer(50, 1, 200), "request_id": KEY}, ["task_id"]),
    tool("codex_goal", "Read or change a native persistent goal. Set/clear only on explicit human instruction, with user_requested=true and request_id. Active goals may continue work and consume usage; never infer one from a normal task.",
         {"task_id": TASK, "action": string(enum=["get", "set", "clear"], default="get"), "objective": string(),
          "status": string(enum=["active", "paused", "blocked", "complete"]), "token_budget": {"type": "integer", "minimum": 1, "maximum": 10000000},
          "user_requested": {"type": "boolean", "default": False}, "request_id": KEY}, ["task_id"]),
    tool("codex_skills", "List native skills available for this project. Select enabled skills by name in start/send; no global skill configuration changes.",
         {"task_id": TASK, "force_reload": {"type": "boolean", "default": False}}, ["task_id"], True),
    tool("codex_diff", "Retrieve the latest native turn diff and reported file changes. Does not invoke Git hooks or execute a shell.", {"task_id": TASK}, ["task_id"], True),
    tool("codex_usage", "Read native account rate limits/remaining allowance. Does not buy credits or change billing.", {}, read_only=True),
    tool("codex_sessions", "List/import native CLI conversations for the workdir or project_id explicitly selected by the main agent. Import requires thread_id and request_id; permissions are reapplied before work.",
         {**PROJECT, "action": string(enum=["list", "import"], default="list"),
          "thread_id": string(), "cursor": CURSOR, "limit": integer(50, 1, 200),
          "archived": {"type": "boolean", "default": False}, "confirm_stopped": {"type": "boolean", "default": False}, "request_id": KEY}),
]
BY_NAME = {t["name"]: t for t in TOOLS}


def validate(value, schema, path="arguments"):
    """Validate the JSON Schema subset used here and by primitive MCP forms.

    Unknown validation keywords fail closed; this is not a general JSON Schema
    implementation. External forms requiring advanced schemas use native Codex.
    """
    if schema is True:
        return
    if schema is False or not isinstance(schema, dict):
        raise BridgeError(f"{path}: unsupported schema")
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum",
                 "default", "description", "title", "minimum", "maximum", "minLength",
                 "maxLength", "minItems", "maxItems", "$schema", "format"}
    if set(schema) - supported:
        raise BridgeError(f"{path}: unsupported form schema; use native Codex for this request")
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool,
             "number": (int, float), "null": type(None)}
    if expected:
        if not isinstance(expected, str) or expected not in types or not isinstance(value, types[expected]) or (expected in ("integer", "number") and isinstance(value, bool)):
            raise BridgeError(f"{path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise BridgeError(f"{path}: value not in allowed choices")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise BridgeError(f"{path}: missing required fields")
        for key, child in value.items():
            child_schema = properties.get(key, schema.get("additionalProperties", True))
            if child_schema is False:
                raise BridgeError(f"{path}: unknown field {key}")
            if child_schema is not True:
                validate(child, child_schema, f"{path}.{key}")
    if isinstance(value, list):
        for child in value:
            validate(child, schema.get("items", True), path + "[]")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise BridgeError(f"{path}: array length outside bounds")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1000000):
            raise BridgeError(f"{path}: string length outside bounds")
        if schema.get("format") not in (None, "date", "date-time", "email", "uri"):
            raise BridgeError(f"{path}: unsupported form format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise BridgeError(f"{path}: number outside bounds")


def arguments(name, values):
    descriptor = BY_NAME.get(name)
    if not descriptor:
        raise BridgeError("Unknown tool")
    validate(values, descriptor["inputSchema"])
    result = dict(values)
    for key, schema in descriptor["inputSchema"]["properties"].items():
        if key not in result and "default" in schema:
            result[key] = schema["default"]
    return result


async def dispatch(service, name, values, *, human=False):
    args = arguments(name, values)
    if name == "codex_projects":
        return service.projects(**args)
    if name == "codex_get":
        return service.get(**args)
    if name == "codex_list":
        return service.list_tasks(**args)
    if name == "codex_requests":
        return {"requests": service.requests(**args)}
    if name == "codex_respond":
        async with service.mutation_lock:
            return await service.respond(**args, human=human)
    if name == "codex_review_request":
        return service.public_request(service.review_request(args["request_id"]))
    method = getattr(service, name.removeprefix("codex_"))
    mutations = {"codex_start", "codex_send", "codex_resume", "codex_interrupt", "codex_delegate", "codex_fork",
                 "codex_rename", "codex_archive", "codex_delete", "codex_compact", "codex_revert", "codex_review"}
    changing = name in mutations or (name == "codex_settings" and args["scope"] != "get") or (
        name in ("codex_queue", "codex_terminals", "codex_sessions") and args["action"] != "list") or (
        name == "codex_goal" and args["action"] != "get")
    if changing:
        key = args.get("request_id")
        if not key:
            raise BridgeError("This mutation requires a unique request_id")
        call_args = dict(args)
        if name != "codex_start":
            call_args.pop("request_id")
        if name == "codex_queue":
            call_args["operation_id"] = key
        return await service.operation(key, name, args, lambda: method(**call_args))
    args.pop("request_id", None)
    return await method(**args)
