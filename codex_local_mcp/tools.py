"""Small, closed MCP surface. There is deliberately no raw RPC or shell tool."""

import hashlib
import json

from .security import BridgeError
from .authority import review_markers, validate_settings_reviewer
from .resources import LIMITS as BUDGET_LIMITS

RESULT_BUDGET = 8 * 1024 * 1024
FRAME_LIMIT = 32 * 1024 * 1024
REQUEST_PAGE_BUDGET = 1024 * 1024
DETAIL_CHARS = 65536
RESULT_VERSION = 1
IDENTITY_FIELDS = ("task_id", "project_id", "thread_id", "turn_id", "expected_turn_id", "queued_id", "before_turn_id", "last_turn_id", "confirm_thread_id")


def result_envelope(value, **identity):
    return {**value, **identity, "result_version": RESULT_VERSION, "ok": True}


def error_envelope(error):
    if not isinstance(error, BridgeError):
        error = BridgeError(str(error), code="native_timeout" if isinstance(error, TimeoutError) else "transport_unavailable" if isinstance(error, OSError) else "internal_error")
    return {"result_version": RESULT_VERSION, "ok": False, "error": error.as_dict()}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def tool_result(value, *, structured=True):
    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
    if structured:
        result["structuredContent"] = value
    return result


def result_size(value):
    # Budget the larger modern form, including the escaped duplicate text.
    return encoded_size({"jsonrpc": "2.0", "id": 0, "result": tool_result(value)}) + 1


def bounded_result(value, budget=RESULT_BUDGET):
    if result_size(value) <= budget:
        return value
    original = encoded_size(value)
    for maximum in (16384, 4096, 1024, 256, 64):
        omitted = []
        def shorten(item, path=""):
            if isinstance(item, str) and len(item) > maximum:
                omitted.append(path)
                return item[:maximum]
            if isinstance(item, list):
                count = max(1, maximum // 128)
                if len(item) > count:
                    omitted.append(path)
                return [shorten(v, f"{path}/{i}") for i, v in enumerate(item[:count])]
            if isinstance(item, dict):
                pairs = list(item.items())
                if len(pairs) > 200:
                    omitted.append(path)
                return {k: shorten(v, path + "/" + k) for k, v in pairs[:200]}
            return item
        result = shorten(value)
        if not isinstance(result, dict):
            result = {"data": result}
        result.update(truncated=True, truncated_fields=omitted[:100],
                      truncated_fields_more=len(omitted) > 100, original_bytes=original)
        if result_size(result) <= budget:
            return result
    raise BridgeError("Result exceeds the response budget; request a smaller page", code='result_too_large')


def request_summary(request):
    request = {**request, **review_markers(request)}
    text = json_text(request)
    small = len(text.encode("utf-8")) <= 4096
    keys = ("id", "kind", "task_id", "parent_task_id", "root_task_id", "blocking", "requires_human",
            "proposal_revision", "proposal_hash", "status", "created_at", "revision", "content_redacted", "redaction_count",
            "person_review_required", "review_warning", "permission_summary", "decision_summary")
    summary = dict(request) if small else {k: request.get(k) for k in keys}
    params = json_text(request.get("params", {}))
    summary.update(content_bytes=len(text.encode("utf-8")),
        proposal_bytes=len(json_text(request.get("params", {}).get("proposedChanges", [])).encode("utf-8")),
        preview=params[:512], preview_truncated=len(params) > 512,
        details_truncated=not small, detail_tool="codex_review_request")
    if request.get("id"):
        summary.update(pending_request_id=request["id"], request_id_kind="pending_request")
    return summary


def request_detail(request, offset=None, limit=DETAIL_CHARS):
    if offset is None and result_size(request) <= RESULT_BUDGET:
        return {**request, "detail_complete": True}
    text = json_text(request)
    offset = offset or 0
    if offset > len(text):
        raise BridgeError("Request detail offset is beyond this request; restart at offset 0", code='invalid_arguments')
    end = min(len(text), offset + limit)
    return {**request_summary(request), "detail": text[offset:end], "detail_offset": offset,
        "next_offset": end if end < len(text) else None, "detail_total_chars": len(text),
        "detail_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "detail_complete": offset == 0 and end == len(text)}


def string(description="", **kw):
    return {"type": "string", **({"description": description} if description else {}), "minLength": 1, "maxLength": 100000, **kw}


def integer(default, minimum=0, maximum=100):
    return {"type": "integer", "default": default, "minimum": minimum, "maximum": maximum}


def tool(name, description, properties, required=(), read_only=False):
    return {"name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                        "idempotentHint": read_only, "openWorldHint": True}}


TASK = string("Task id")
KEY = string("Key for identical retries", maxLength=200)
CURSOR = string("Native cursor")
BUDGET = {"type": "object", "properties": {name: {"type": "integer", "minimum": 1, "maximum": maximum}
    for name, maximum in BUDGET_LIMITS.items()}, "additionalProperties": False,
    "description": "Family lifetime limits: server turns/child admissions hard; active seconds, tokens, stored bytes soft (may overshoot). Native continuations/children may exceed admission caps. {} clears."}
APPROVAL_MODE = string("Native Codex reviewer; approve_for_me uses auto_review without disabling sandboxing", enum=["inherit", "ask", "approve_for_me"])
INPUTS = {"images": {"type": "array", "items": string(), "maxItems": 10},
          "context_files": {"type": "array", "items": string(), "maxItems": 20},
          "skills": {"type": "array", "items": string(), "maxItems": 10},
          "output_schema": {"type": "object", "additionalProperties": True}}
PROJECT = {"workdir": string("Absolute project directory chosen by the main agent"),
           "project_id": string("Selected project; use instead of workdir")}
REQUEST_PAGING = {"requests_cursor": string("Pending-request cursor"),
                  "requests_limit": integer(50, 1, 200)}
TOOLS = [
    tool("codex_plans", "submit: spec/key; inspect: plan_id; list: offset/limit; cancel: plan_id/key. Runs bounded dependency plans through ordinary task admission. Unknown outcomes wait for explicit resume; no side effect is retried.",
         {"action": string(enum=["submit", "inspect", "list", "cancel"]),
          "spec": {"type": "object", "description": "Version 1 dependency plan; see the tools documentation."},
          "plan_id": string(minLength=64, maxLength=64), "request_id": KEY,
          "offset": integer(0, maximum=1000000), "limit": integer(20, 1, 100)}, ["action"]),
    tool("codex_status", "Check installed Codex, sign-in type, pending request summaries and broker health.", REQUEST_PAGING, read_only=True),
    tool("codex_projects", "The main agent chooses an absolute directory to select or create. Returns a project_id for tasks; no workspace configuration is required. List returns previously selected projects, not the whole filesystem.",
         {"action": string(enum=["list", "select", "create"], default="list"),
          "path": string("Absolute directory to select/create"), "offset": integer(0, maximum=1000000), "limit": integer(100, 1, 200)}),
    tool("codex_worktrees", "plan/create: repo or project_id, name, optional base; create also plan_id/key. inspect: project_id or path, offset/limit. handoff: finished task_id. cleanup: project_id, optional keep_branch, key. Except inspect, needs worktrees=true. Never merges/pushes.",
         {"action": string(enum=["plan", "create", "inspect", "handoff", "cleanup"]),
          "repo": string("Absolute repository"), "project_id": string(), "name": string(maxLength=64),
          "base": string(default="HEAD", maxLength=1024), "plan_id": string(minLength=64, maxLength=64),
          "path": string(), "task_id": TASK, "request_id": KEY,
          "keep_branch": {"type": "boolean", "default": False}, "offset": integer(0, maximum=1000000), "limit": integer(50, 1, 200)}, ["action"]),
    tool("codex_models", "Discover model ids, supported reasoning efforts and defaults. Catalog visibility is not an entitlement guarantee.",
         {"cursor": CURSOR, "limit": integer(100, 1, 200)}, read_only=True),
    tool("codex_start", "Start a persistent Codex conversation and return promptly with a task id. Poll codex_events and handle pending_requests until the turn finishes.",
         {**PROJECT, "prompt": string(), "request_id": KEY,
          "model": string(), "effort": string(), "access": string(enum=["read", "write"], default="write"),
          "approval_mode": {**APPROVAL_MODE, "default": "inherit"},
          "collaboration_mode": string(enum=["default", "plan"], default="default"),
          "service_tier": string(), "name": string(maxLength=200), **INPUTS},
         ["prompt", "request_id"]),
    tool("codex_list", "Page persistent tasks, newest first. Large projections identify truncated_fields.",
         {"offset": integer(0, maximum=1000000), "limit": integer(50, 1, 200)}, read_only=True),
    tool("codex_get", "Read task state and cumulative usage. workspace_claims explain ownership; page requests, children and claims. Large fields identify truncated_fields.",
         {"task_id": TASK, **REQUEST_PAGING, "subagents_offset": integer(0, maximum=1000000),
          "subagents_limit": integer(50, 1, 200), "claims_offset": integer(0, maximum=1000000),
          "claims_limit": integer(100, 1, 200)}, ["task_id"], read_only=True),
    tool("codex_events", "Replay progress or wait up to 25s. Drain has_more. For fragments, keep after and use event_seq/next_event_offset until complete. Page requests with requests_cursor.",
         {"task_id": TASK, "after": integer(0, maximum=2**53-1), "limit": integer(100, 1, 200),
          "wait_seconds": integer(0, maximum=25), "include_descendants": {"type": "boolean", "default": True},
          **REQUEST_PAGING, "event_offset": integer(0, maximum=2**53-1),
          "event_seq": integer(0, maximum=2**53-1)}, read_only=True),
    tool("codex_send", "Send a follow-up turn or steer active work. Steering needs the current expected_turn_id; follow-ups can select a supported model/effort.",
         {"task_id": TASK, "prompt": string(), "mode": string(enum=["follow_up", "steer"], default="follow_up"),
          "expected_turn_id": string(), "model": string(), "effort": string(), "request_id": KEY, **INPUTS},
         ["task_id", "prompt", "request_id"]),
    tool("codex_resume", "Reconcile native history and execution intent after reconnect. No prompt or command replay. Inspect codex_history before a follow-up.",
         {"task_id": TASK, "request_id": KEY}, ["task_id", "request_id"]),
    tool("codex_interrupt", "Ask Codex to interrupt the specified active turn. Poll for interrupted/completed; interrupt acknowledgement alone is not completion.",
         {"task_id": TASK, "expected_turn_id": string(), "request_id": KEY}, ["task_id", "expected_turn_id", "request_id"]),
    tool("codex_history", "Page persisted turns or items for owned conversations, including after an outage. Large items identify truncated_fields; pending approval details come from codex_review_request.",
         {"task_id": TASK, "kind": string(enum=["turns", "items"], default="turns"),
          "cursor": CURSOR, "limit": integer(50, 1, 200), "turn_id": string()}, ["task_id"], True),
    tool("codex_requests", "Page bounded pending approval/question summaries using cursor. Fetch complete content by id with codex_review_request before approval. Never infer approval from silence or timeout.",
         {"task_id": TASK, "cursor": string("Pending-request continuation"), "limit": integer(50, 1, 200)}, read_only=True),
    tool("codex_respond", "Answer guidance or review requests. Typed decisions apply only to ask_parent. Approvals need human/relay; file accepts need proposal_hash. Denials need no human.",
         {"request_id": string("Pending request id, not an operation key"),
          "proposal_hash": string("Required for file_approval accept: the hash displayed with the reviewed proposal", maxLength=64),
          "response": {"type": "object", "additionalProperties": True, "properties": {"decision": {"anyOf": [{"type": "string"}, {"type": "object"}],
              "description": "Approval string or ask_parent object: version,id,context_hash,option_id,rationale. Guidance grants no authority."}}}}, ["request_id", "response"]),
    tool("codex_review_request", "Human review, or read details with offset=0. Follow next_offset and verify detail_sha256. Acceptance requires complete content.",
         {"request_id": string(), "offset": {"type": "integer", "minimum": 0, "maximum": 2**53-1},
          "limit": integer(DETAIL_CHARS, 1, 262144)}, ["request_id"]),
    tool("codex_capabilities", "Discover native collaboration/permission options and direct-input capability for a task.",
         {"task_id": TASK}, ["task_id"], True),
    tool("codex_settings", "Read/change task model, effort, native approval reviewer, plan mode, service tier or project access. scope=current affects only a running turn; next affects future turns. Existing pending approvals keep their reviewer. Mutations need request_id.",
         {"task_id": TASK, "scope": string(enum=["get", "next", "current"], default="get"),
          "expected_turn_id": string(), "model": string(), "effort": string(),
          "approval_mode": string(enum=["ask", "approve_for_me"]),
          "collaboration_mode": string(enum=["default", "plan"]), "service_tier": string(),
          "access": string(enum=["read", "write"]), "budget": BUDGET, "request_id": KEY}, ["task_id"]),
    tool("codex_subagents", "Page verified descendants; parent events include child requests. refresh=false uses offset; refresh=true uses cursor. Respect can_accept_direct_input.",
         {"task_id": TASK, "cursor": CURSOR, "limit": integer(50, 1, 200),
          "refresh": {"type": "boolean", "default": True}, "archived": {"type": "boolean", "default": False},
          "offset": integer(0, maximum=1000000)}, ["task_id"], True),
    tool("codex_delegate", "Ask a parent to manage native subagents; discover them with codex_subagents. Active parents require expected_turn_id.",
         {"task_id": TASK, "prompt": string(), "expected_turn_id": string(), "request_id": KEY,
          "contract": {"type": "object", "description": "Version 1 guidance: version,id,goal,constraints,deliverables,verification,options,evidence,ttl_seconds (60..604800). No authority."}}, ["task_id", "prompt", "request_id"]),
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
    tool("codex_diff", "Retrieve the current native turn's diff and reported file changes; old diffs are not carried into new turns. Does not invoke Git hooks or execute a shell.", {"task_id": TASK}, ["task_id"], True),
    tool("codex_usage", "Read native account rate limits/remaining allowance. Does not buy credits or change billing.", {}, read_only=True),
    tool("codex_sessions", "List/import native CLI conversations for the workdir or project_id explicitly selected by the main agent. Import requires thread_id and request_id; permissions are reapplied before work.",
         {**PROJECT, "action": string(enum=["list", "import"], default="list"),
          "thread_id": string(), "cursor": CURSOR, "limit": integer(50, 1, 200),
          "archived": {"type": "boolean", "default": False}, "confirm_stopped": {"type": "boolean", "default": False}, "request_id": KEY}),
]
ACTION_FIELDS = {
    "codex_plans": ("action", None, set(), {"submit": {"spec", "request_id"}, "inspect": {"plan_id"}, "list": {"offset", "limit"}, "cancel": {"plan_id", "request_id"}}),
    "codex_worktrees": ("action", "inspect", set(), {"plan": {"repo", "project_id", "name", "base"},
        "create": {"repo", "project_id", "name", "base", "plan_id", "request_id"},
        "inspect": {"project_id", "path", "offset", "limit"}, "handoff": {"task_id"},
        "cleanup": {"project_id", "keep_branch", "request_id"}}),
    "codex_projects": ("action", "list", set(), {"list": {"offset", "limit"}, "select": {"path"}, "create": {"path"}}),
    "codex_settings": ("scope", "get", {"task_id"}, {"get": set(),
        "next": {"model", "effort", "approval_mode", "collaboration_mode", "service_tier", "access", "budget", "request_id"},
        "current": {"model", "effort", "approval_mode", "service_tier", "expected_turn_id", "request_id"}}),
    "codex_queue": ("action", "list", {"task_id"}, {"list": {"cursor", "limit"},
        "add": {"prompt", "images", "context_files", "skills", "request_id"},
        "update": {"queued_id", "prompt", "images", "context_files", "skills", "request_id"},
        "delete": {"queued_id", "request_id"}, "reorder": {"ordered_ids", "request_id"}, "start": {"queued_id", "request_id"}}),
    "codex_terminals": ("action", "list", {"task_id"}, {"list": {"cursor", "limit"}, "terminate": {"process_id", "request_id"}, "clean": {"request_id"}}),
    "codex_goal": ("action", "get", {"task_id"}, {"get": set(), "set": {"objective", "status", "token_budget", "user_requested", "request_id"}, "clear": {"user_requested", "request_id"}}),
    "codex_sessions": ("action", "list", {"workdir", "project_id"}, {"list": {"cursor", "limit", "archived"}, "import": {"thread_id", "archived", "confirm_stopped", "request_id"}}),
}
ACTION_DESCRIPTIONS = {
    "codex_projects": "List selected projects with offset/limit; select/create takes path. The main agent chooses an absolute project directory.",
    "codex_settings": "get reads settings. next takes model/effort/approval_mode/collaboration_mode/service_tier/access and root-only family budget (applies now; {} clears). current takes model/effort/approval_mode/service_tier and expected_turn_id. Changes need an operation key.",
    "codex_queue": "list takes cursor/limit; add takes prompt and optional images/context_files/skills; update also needs queued_id; delete needs queued_id; reorder needs ordered_ids; start takes optional queued_id. Mutations need an operation key.",
    "codex_terminals": "list takes cursor/limit; terminate takes process_id; clean stops all background terminals. Mutations need an operation key. No arbitrary shell or stdin.",
    "codex_goal": "get reads a goal; set takes objective/status/token_budget; clear removes it. Mutations need user_requested=true and an operation key. Only change goals on explicit human instruction.",
    "codex_sessions": "Choose workdir or project_id. list takes cursor/limit/archived; import takes thread_id, confirm_stopped=true, optional archived and an operation key. Native scope is verified before work.",
}
ACTION_REQUIRED = {
    ("codex_plans", "submit"): ("spec", "request_id"), ("codex_plans", "inspect"): ("plan_id",),
    ("codex_plans", "cancel"): ("plan_id", "request_id"),
    ("codex_worktrees", "plan"): ("name",), ("codex_worktrees", "create"): ("name", "plan_id", "request_id"),
    ("codex_worktrees", "cleanup"): ("project_id", "request_id"), ("codex_worktrees", "handoff"): ("task_id",),
    ("codex_projects", "select"): ("path",), ("codex_projects", "create"): ("path",),
    ("codex_settings", "current"): ("expected_turn_id",),
    ("codex_queue", "add"): ("prompt",), ("codex_queue", "update"): ("prompt", "queued_id"),
    ("codex_queue", "delete"): ("queued_id",), ("codex_queue", "reorder"): ("ordered_ids",),
    ("codex_terminals", "terminate"): ("process_id",), ("codex_sessions", "import"): ("thread_id", "confirm_stopped"),
}
for descriptor in TOOLS:
    name, schema = descriptor["name"], descriptor["inputSchema"]
    if name in ACTION_DESCRIPTIONS:
        descriptor["description"] = ACTION_DESCRIPTIONS[name]
    if "request_id" in schema["properties"]:
        alias = "pending_request_id" if name in ("codex_respond", "codex_review_request") else "operation_id"
        schema["properties"][alias] = {**schema["properties"]["request_id"], "description": "Alias of request_id"}
        if "request_id" in schema["required"]:
            schema["required"].remove("request_id")
            schema["anyOf"] = [{"required": ["request_id"]}, {"required": [alias]}]
    if name in ("codex_get", "codex_list", "codex_events"):
        schema["properties"]["view"] = string("Detail or compact supervision view", enum=["default", "compact"], default="default")
    if name == "codex_get":
        schema["properties"]["view"]["enum"] += ["result", "artifact", "reconciliation", "permissions"]
        schema["properties"].update({"turn_id": string(), "artifact_id": string(), "result_id": string(),
            "offset": integer(0, maximum=2**53-1), "limit": {"type": "integer", "minimum": 1, "maximum": 262144}})
        descriptor["description"] += " result pages turn evidence (pin result_id); artifact reads scoped bytes by artifact_id. Use offset/limit. reconciliation explains recovery; permissions pages receipts and scope."
    if name == "codex_events":
        schema["properties"]["view"]["enum"].append("outcome")
        schema["properties"].update({"task_ids": {"type": "array", "items": TASK, "minItems": 1, "maxItems": 50},
            "cursors": {"type": "object", "additionalProperties": string(maxLength=2048)},
            "tasks_offset": integer(0, maximum=1000000)})
        descriptor["description"] += " outcome coalesces state for task_ids (max 50); return cursors unchanged on the next poll. Detailed replay uses after separately."

BY_NAME = {t["name"]: t for t in TOOLS}


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


TEXT, FLAG, NUMBER, OBJECT, ARRAY = ({"type": t} for t in ("string", "boolean", "integer", "object", "array"))
NULLABLE_TEXT = {"anyOf": [TEXT, {"type": "null"}]}
NULLABLE_NUMBER = {"anyOf": [NUMBER, {"type": "null"}]}
TASK_RESULT = object_schema({"id": TEXT, "status": TEXT}, ("id", "status"))
ERROR_RESULT = object_schema({"code": TEXT, "message": TEXT, "retryable": FLAG, "uncertain": FLAG,
    "next_steps": {"type": "array", "items": TEXT}, "ids": OBJECT}, ("code", "message", "retryable", "uncertain", "next_steps", "ids"))
# Output schemas describe stable top-level fields. Native and paged content
# stays open, so compatible fields do not expand every host's tool catalog.
SUCCESS_SCHEMAS = {
    "codex_plans": object_schema({"action": TEXT}, ("action",)),
    "codex_worktrees": object_schema({"action": TEXT}, ("action",)),
    "codex_status": object_schema({"codex_version": TEXT, "state_schema_version": NUMBER, "pending_requests": ARRAY, "os_process_check": OBJECT}, ("codex_version", "state_schema_version", "pending_requests", "os_process_check")),
    "codex_projects": {"anyOf": [object_schema({"projects": ARRAY}, ("projects",)), object_schema({"id": TEXT, "path": TEXT}, ("id", "path"))]},
    "codex_list": object_schema({"tasks": ARRAY, "next_offset": NULLABLE_NUMBER}, ("tasks", "next_offset")),
    "codex_events": object_schema({"events": ARRAY, "cursor": NUMBER, "has_more": FLAG, "pending_requests": ARRAY}, ("events", "cursor", "has_more", "pending_requests")),
    "codex_requests": object_schema({"requests": ARRAY, "has_more": FLAG, "total": NUMBER, "next_cursor": NULLABLE_TEXT}, ("requests", "next_cursor", "has_more", "total")),
    "codex_respond": object_schema({"request_id": TEXT, "status": TEXT}, ("request_id", "status")),
    "codex_review_request": {"anyOf": [object_schema({"id": TEXT, "kind": TEXT, "task_id": TEXT}, ("id", "kind", "task_id")),
        object_schema({"status": TEXT, "request": OBJECT}, ("status", "request")),
        object_schema({"request_id": TEXT, "status": TEXT}, ("request_id", "status"))]},
    "codex_settings": object_schema({"id": TEXT}, ("id",)),
    "codex_subagents": object_schema({"subagents": ARRAY, "nextCursor": NULLABLE_TEXT, "next_offset": NULLABLE_NUMBER}, ("subagents", "nextCursor", "next_offset")),
    "codex_delete": object_schema({"task_id": TEXT, "deleted": FLAG, "audit_retained": FLAG}, ("task_id", "deleted", "audit_retained")),
    "codex_revert": object_schema({"task_id": TEXT, "history": OBJECT, "files_changed": FLAG}, ("task_id", "history", "files_changed")),
    "codex_diff": object_schema({"task_id": TEXT, "turn_id": NULLABLE_TEXT, "diff": TEXT, "files": ARRAY}, ("task_id", "turn_id", "diff", "files")),
    "codex_capabilities": object_schema({"native_methods": OBJECT, "compatibility": OBJECT, "os_process_check": OBJECT}, ("native_methods", "compatibility", "os_process_check")),
    "codex_sessions": {"anyOf": [TASK_RESULT, object_schema({"sessions": ARRAY, "nextCursor": NULLABLE_TEXT}, ("sessions", "nextCursor"))]},
}
for name in ("codex_start", "codex_get", "codex_send", "codex_resume", "codex_interrupt", "codex_delegate", "codex_fork", "codex_rename", "codex_archive", "codex_review", "codex_compact"):
    SUCCESS_SCHEMAS[name] = TASK_RESULT
SUCCESS_SCHEMAS["codex_get"] = object_schema({"id": TEXT, "status": TEXT, "family_usage": OBJECT, "budget": OBJECT,
    "budget_stop": {"anyOf": [OBJECT, {"type": "null"}]}}, ("id", "status"))
for name in ("codex_models", "codex_history", "codex_skills"):
    SUCCESS_SCHEMAS[name] = object_schema({"data": ARRAY}, ("data",))
for name in ("codex_queue", "codex_terminals", "codex_goal", "codex_usage"):
    SUCCESS_SCHEMAS[name] = OBJECT
for descriptor in TOOLS:
    success = SUCCESS_SCHEMAS[descriptor["name"]]
    descriptor["outputSchema"] = object_schema({"result_version": {"type": "integer", "const": RESULT_VERSION}, "ok": FLAG}, ("result_version", "ok"))
    descriptor["outputSchema"]["anyOf"] = [
        {**success, "properties": {**success.get("properties", {}), "ok": {"const": True}}},
        object_schema({"ok": {"const": False}, "error": OBJECT}, ("error",))]


def tool_catalog(version):
    return TOOLS if version >= "2025-06-18" else [{k: v for k, v in t.items() if k != "outputSchema"} for t in TOOLS]


def validate(value, schema, path="arguments"):
    """Validate the JSON Schema subset used here and by primitive MCP forms.

    Unknown validation keywords fail closed; this is not a general JSON Schema
    implementation. External forms requiring advanced schemas use native Codex.
    """
    if schema is True:
        return
    if schema is False or not isinstance(schema, dict):
        raise BridgeError(f"{path}: unsupported schema", code='invalid_arguments')
    supported = {"type", "properties", "required", "additionalProperties", "items", "enum",
                 "default", "description", "title", "minimum", "maximum", "minLength",
                 "maxLength", "minItems", "maxItems", "$schema", "format", "anyOf", "const"}
    if set(schema) - supported:
        raise BridgeError(f"{path}: unsupported form schema; use native Codex for this request", code='invalid_arguments')
    for keyword in ("anyOf",):
        if keyword in schema:
            matches = 0
            for candidate in schema[keyword]:
                try:
                    validate(value, candidate, path)
                except BridgeError:
                    continue
                matches += 1
            if not matches:
                raise BridgeError(f"{path}: no supported {keyword} shape", code="invalid_arguments")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise BridgeError(f"{path}: unexpected constant", code="invalid_arguments")
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool,
             "number": (int, float), "null": type(None)}
    if expected:
        if not isinstance(expected, str) or expected not in types or not isinstance(value, types[expected]) or (expected in ("integer", "number") and isinstance(value, bool)):
            raise BridgeError(f"{path}: expected {expected}", code='invalid_arguments')
    if "enum" in schema and value not in schema["enum"]:
        raise BridgeError(f"{path}: value not in allowed choices", code='invalid_arguments')
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise BridgeError(f"{path}: missing required fields", code='invalid_arguments')
        for key, child in value.items():
            child_schema = properties.get(key, schema.get("additionalProperties", True))
            if child_schema is False:
                raise BridgeError(f"{path}: unknown field {key}", code='invalid_arguments')
            if child_schema is not True:
                validate(child, child_schema, f"{path}.{key}")
    if isinstance(value, list):
        for child in value:
            validate(child, schema.get("items", True), path + "[]")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise BridgeError(f"{path}: array length outside bounds", code='invalid_arguments')
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1000000):
            raise BridgeError(f"{path}: string length outside bounds", code='invalid_arguments')
        if schema.get("format") not in (None, "date", "date-time", "email", "uri"):
            raise BridgeError(f"{path}: unsupported form format", code='invalid_arguments')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise BridgeError(f"{path}: number outside bounds", code='invalid_arguments')


def arguments(name, values):
    descriptor = BY_NAME.get(name)
    if not descriptor:
        raise BridgeError("Unknown tool", code='invalid_arguments')
    if name == "codex_settings" and isinstance(values, dict):
        validate_settings_reviewer(values.get("approval_mode"))
    validate(values, descriptor["inputSchema"])
    result = dict(values)
    for alias in ("operation_id", "pending_request_id"):
        if alias in result:
            if "request_id" in result and result["request_id"] != result[alias]:
                raise BridgeError(f"{alias} and request_id must match when both are supplied", code="invalid_arguments")
            result["request_id"] = result.pop(alias)
    allowed = None
    if name in ACTION_FIELDS:
        selector, default, common, actions = ACTION_FIELDS[name]
        action = result.get(selector, default)
        allowed = common | actions[action] | {selector}
        for field in sorted(set(result) - allowed):
            prop = descriptor["inputSchema"]["properties"][field]
            if "default" not in prop or result[field] != prop["default"]:
                raise BridgeError(f"{name} {selector}={action}: argument {field} does not apply", code="invalid_arguments")
            del result[field]
        for required in ACTION_REQUIRED.get((name, action), ()):
            if required not in result:
                raise BridgeError(f"{name} {selector}={action}: argument {required} is required", code="invalid_arguments")
        if name == "codex_goal" and action != "get" and result.get("user_requested") is not True:
            raise BridgeError("Goal mutations require an explicit user request; do not infer one from a coding task", code="user_request_required")
        settings = actions[action] - {"request_id", "expected_turn_id"}
        if name == "codex_settings" and action != "get" and not (settings & result.keys()):
            raise BridgeError("Provide at least one setting to change", code="invalid_arguments")
        if name == "codex_goal" and action == "set" and not ({"objective", "status", "token_budget"} & result.keys()):
            raise BridgeError("Provide an objective, status or budget", code="invalid_arguments")
        if name == "codex_worktrees" and action in ("plan", "create", "inspect"):
            choices = ("project_id", "path") if action == "inspect" else ("repo", "project_id")
            if sum(field in result for field in choices) != 1:
                raise BridgeError(f"codex_worktrees action={action}: supply exactly one {choices[0]} or {choices[1]}", code="invalid_arguments")
    if name == "codex_events" and result.get("view") == "compact" and (result.get("event_offset") or result.get("event_seq")):
        raise BridgeError("codex_events view=compact does not accept event_offset or event_seq", code="invalid_arguments")
    if name == "codex_events":
        outcome = result.get("view") == "outcome"
        for field in ("task_ids", "cursors", "tasks_offset"):
            if field in result and not outcome:
                if field == "tasks_offset" and result[field] == 0:
                    result.pop(field)
                else:
                    raise BridgeError(f"codex_events view={result.get('view', 'default')}: argument {field} does not apply", code="invalid_arguments")
        if outcome and any(result.get(k) for k in ("after", "event_offset", "event_seq")):
            raise BridgeError("codex_events view=outcome uses cursors; after, event_offset and event_seq must be zero", code="invalid_arguments")
    if name == "codex_get":
        view = result.get("view", "default")
        for field in ("turn_id", "artifact_id", "result_id", "offset", "limit"):
            applies = view == "artifact" if field == "artifact_id" else view == "result" if field in ("turn_id", "result_id") else view in ("result", "artifact", "permissions")
            if field in result and not applies:
                if field == "offset" and result[field] == 0:
                    result.pop(field)
                else:
                    raise BridgeError(f"codex_get view={view}: argument {field} does not apply", code="invalid_arguments")
        if view == "artifact" and "artifact_id" not in result:
            raise BridgeError("codex_get view=artifact: argument artifact_id is required", code="invalid_arguments")
    for key, schema in descriptor["inputSchema"]["properties"].items():
        if name == "codex_get" and key == "offset" and result.get("view", "default") not in ("result", "artifact", "permissions"):
            continue
        if name == "codex_events" and key == "tasks_offset" and result.get("view", "default") != "outcome":
            continue
        if key not in result and "default" in schema and (allowed is None or key in allowed):
            result[key] = schema["default"]
    return result


async def dispatch(service, name, values, *, human=False):
    review_source = None
    if human and name == "codex_respond" and "review_source" in values:
        values = dict(values)
        review_source = values.pop("review_source")
    args = arguments(name, values)
    try:
        result = await _dispatch(service, name, args, human=human, review_source=review_source)
    except BridgeError as exc:
        for key in IDENTITY_FIELDS:
            if args.get(key):
                exc.ids.setdefault(key, args[key])
        if args.get("request_id"):
            exc.ids.setdefault("pending_request_id" if name in ("codex_respond", "codex_review_request") else "operation_id", args["request_id"])
        if BY_NAME[name]["annotations"]["readOnlyHint"] and exc.code in ("transport_unavailable", "native_timeout"):
            exc.retryable = True
        raise
    identity = {}
    if args.get("request_id"):
        pending = name in ("codex_respond", "codex_review_request")
        identity = {"pending_request_id" if pending else "operation_id": args["request_id"], "request_id_kind": "pending_request" if pending else "operation"}
    result = result_envelope(result, **identity)
    if name in ("codex_events", "codex_requests", "codex_review_request"):
        if result_size(result) > RESULT_BUDGET:
            raise BridgeError("Result exceeds the response budget; request a smaller page", code='result_too_large')
        return result
    return bounded_result(result)


async def _dispatch(service, name, values, *, human=False, review_source=None):
    args = arguments(name, values)
    if name == "codex_projects":
        return service.projects(**args)
    if name == "codex_get":
        return service.get(**args)
    if name == "codex_list":
        return service.list_tasks(**args)
    if name == "codex_requests":
        return service.request_page(**args)
    if name == "codex_respond":
        return await service.respond(**args, human=human, review_source=review_source)
    if name == "codex_review_request":
        return request_detail(service.public_request(service.review_request(args["request_id"])),
                              args.get("offset"), args["limit"])
    method = getattr(service, name.removeprefix("codex_"))
    mutations = {"codex_start", "codex_send", "codex_resume", "codex_interrupt", "codex_delegate", "codex_fork",
                 "codex_rename", "codex_archive", "codex_delete", "codex_compact", "codex_revert", "codex_review"}
    changing = name in mutations or (name == "codex_settings" and args["scope"] != "get") or (
        name in ("codex_queue", "codex_terminals", "codex_sessions") and args["action"] != "list") or (
        name == "codex_goal" and args["action"] != "get") or (
        name == "codex_worktrees" and args["action"] in ("create", "cleanup")) or (
        name == "codex_plans" and args["action"] in ("submit", "cancel"))
    if changing:
        key = args.get("request_id")
        if not key:
            raise BridgeError("This mutation requires a unique request_id", code='invalid_arguments')
        call_args = dict(args)
        if name != "codex_start":
            call_args.pop("request_id")
        if name == "codex_queue":
            call_args["operation_id"] = key
        # Only existing keys may use the old fingerprint with unused defaults.
        legacy_args = dict(args)
        for field, schema in BY_NAME[name]["inputSchema"]["properties"].items():
            if "default" in schema:
                legacy_args.setdefault(field, schema["default"])
        return await service.operation(key, name, args, lambda: method(**call_args), legacy_args=legacy_args)
    args.pop("request_id", None)
    return await method(**args)
