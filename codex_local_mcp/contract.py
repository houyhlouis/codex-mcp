"""Native and legacy MCP compatibility policy; no version replaces safety checks."""

import re
from dataclasses import dataclass

CODEX_MIN_VERSION = "0.167.0"
CODEX_TESTED_VERSIONS = ("0.167.0",)
MCP_LEGACY_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MCP_MODERN_VERSION = "2026-07-28"
MCP_VERSIONS = (*MCP_LEGACY_VERSIONS, MCP_MODERN_VERSION)
MCP_TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
MCP_MODERN_CATALOG_BUDGET = 54000
REQUIRED, OPTIONAL = "required", "optional"

# Required failures must never trigger a fallback to weaker scope enforcement.
NATIVE_METHODS = dict.fromkeys((
    "initialize", "config/read", "model/list", "thread/start", "thread/resume",
    "thread/read", "thread/turns/list", "thread/items/list", "turn/start",
    "turn/steer", "turn/interrupt",
), REQUIRED)
NATIVE_METHODS.update(dict.fromkeys((
    "account/read", "account/rateLimits/read", "collaborationMode/list",
    "permissionProfile/list", "skills/list", "thread/list", "thread/loaded/list", "thread/fork",
    "thread/name/set", "thread/archive", "thread/unarchive", "thread/delete",
    "thread/settings/update", "turn/settings/update", "thread/compact/start",
    "thread/revert", "review/start", "thread/queue/add", "thread/queue/list",
    "thread/queue/update", "thread/queue/delete", "thread/queue/reorder",
    "thread/queue/start", "thread/backgroundTerminals/list",
    "thread/backgroundTerminals/terminate", "thread/backgroundTerminals/clean",
    "thread/goal/set", "thread/goal/get", "thread/goal/clear",
), OPTIONAL))

NATIVE_NOTIFICATIONS = dict.fromkeys((
    "initialized", "thread/started", "turn/started", "turn/completed",
    "item/started", "item/completed", "item/fileChange/patchUpdated", "serverRequest/resolved", "error",
), REQUIRED)
NATIVE_NOTIFICATIONS.update(dict.fromkeys((
    "thread/status/changed", "thread/archived", "thread/unarchived", "thread/deleted",
    "thread/queue/changed", "thread/tokenUsage/updated", "turn/diff/updated",
    "turn/plan/updated", "item/agentMessage/delta", "item/reasoning/textDelta",
    "item/autoApprovalReview/started", "item/autoApprovalReview/completed",
    "autoApprovalReview/strictReviewRequired", "warning", "configWarning",
    "deprecationNotice",
), OPTIONAL))
NATIVE_NOTIFICATION_PREFIXES = {"codex/event/": OPTIONAL, "item/autoApprovalReview/": OPTIONAL,
                                "autoApprovalReview/": OPTIONAL}

# Unknown notifications and every server request are protected by default.
# Item/proposal updates, diffs, plans and lifecycle events are never replaceable.
REPLACEABLE_PROGRESS = frozenset({
    "item/agentMessage/delta", "item/commandExecution/outputDelta",
    "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
    "thread/tokenUsage/updated",
})

# Unknown server requests still receive an error and never grant permission.
NATIVE_SERVER_REQUESTS = dict.fromkeys((
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "item/tool/requestUserInput", "item/tool/call",
), REQUIRED)
NATIVE_SERVER_REQUESTS["mcpServer/elicitation/request"] = OPTIONAL
SERVER_RESPONSE_SCHEMAS = {
    "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalResponse",
    "item/fileChange/requestApproval": "FileChangeRequestApprovalResponse",
    "item/permissions/requestApproval": "PermissionsRequestApprovalResponse",
    "item/tool/requestUserInput": "ToolRequestUserInputResponse",
    "item/tool/call": "DynamicToolCallResponse",
    "mcpServer/elicitation/request": "McpServerElicitationRequestResponse",
}

NATIVE_RESPONSE_SCHEMAS = {
    "initialize": "InitializeResponse", "config/read": "ConfigReadResponse", "model/list": "ModelListResponse",
    "account/read": "GetAccountResponse", "account/rateLimits/read": "GetAccountRateLimitsResponse",
    "collaborationMode/list": "CollaborationModeListResponse", "permissionProfile/list": "PermissionProfileListResponse",
    "skills/list": "SkillsListResponse", "thread/start": "ThreadStartResponse", "thread/resume": "ThreadResumeResponse",
    "thread/read": "ThreadReadResponse", "thread/list": "ThreadListResponse", "thread/loaded/list": "ThreadLoadedListResponse", "thread/fork": "ThreadForkResponse",
    "thread/name/set": "ThreadSetNameResponse", "thread/archive": "ThreadArchiveResponse", "thread/unarchive": "ThreadUnarchiveResponse",
    "thread/delete": "ThreadDeleteResponse", "thread/settings/update": "ThreadSettingsUpdateResponse",
    "turn/settings/update": "TurnSettingsUpdateResponse", "thread/compact/start": "ThreadCompactStartResponse",
    "thread/revert": "ThreadRevertResponse", "review/start": "ReviewStartResponse",
    "thread/turns/list": "ThreadTurnsListResponse", "thread/items/list": "ThreadItemsListResponse",
    "turn/start": "TurnStartResponse", "turn/steer": "TurnSteerResponse", "turn/interrupt": "TurnInterruptResponse",
    "thread/queue/add": "ThreadQueueAddResponse", "thread/queue/list": "ThreadQueueListResponse",
    "thread/queue/update": "ThreadQueueUpdateResponse", "thread/queue/delete": "ThreadQueueDeleteResponse",
    "thread/queue/reorder": "ThreadQueueReorderResponse", "thread/queue/start": "ThreadQueueStartResponse",
    "thread/backgroundTerminals/list": "ThreadBackgroundTerminalsListResponse",
    "thread/backgroundTerminals/terminate": "ThreadBackgroundTerminalsTerminateResponse",
    "thread/backgroundTerminals/clean": "ThreadBackgroundTerminalsCleanResponse",
    "thread/goal/set": "ThreadGoalSetResponse", "thread/goal/get": "ThreadGoalGetResponse", "thread/goal/clear": "ThreadGoalClearResponse",
}

# Canonical native fields read by the event/request handlers. A '?' prefix
# means the handler accepts an absent value. Generic fallback aliases and
# bridge-derived fields (for example proposedChanges) are not native inputs.
INBOUND_FIELDS = {
    "thread/started": {"thread.id": "string", "thread.cwd": "string", "thread.source": "string|object",
                       "thread.sessionId": "string", "thread.status": "object", "thread.status.type": "string",
                       "?thread.parentThreadId": "string|null", "?thread.canAcceptDirectInput": "boolean|null",
                       "?thread.name": "string|null", "?thread.agentNickname": "string|null", "?thread.agentRole": "string|null"},
    "thread/status/changed": {"threadId": "string", "status": "object"},
    "thread/archived": {"threadId": "string"},
    "thread/unarchived": {"threadId": "string"},
    "thread/deleted": {"threadId": "string"},
    "thread/queue/changed": {"threadId": "string"},
    "thread/tokenUsage/updated": {"threadId": "string", "tokenUsage": "object"},
    "turn/started": {"threadId": "string", "turn.id": "string", "?turn.rootTurnId": "string|null"},
    "turn/completed": {"threadId": "string", "turn.id": "string", "?turn.rootTurnId": "string|null", "turn.status": "string", "?turn.error": "object|null"},
    "turn/plan/updated": {"threadId": "string", "turnId": "string", "plan": "array",
                          "plan.*.step": "string", "plan.*.status": "string"},
    "turn/diff/updated": {"threadId": "string", "turnId": "string", "diff": "string"},
    "item/started": {"threadId": "string", "turnId": "string", "item.id": "string", "item.type": "string"},
    "item/completed": {"threadId": "string", "turnId": "string", "item.id": "string", "item.type": "string"},
    "item/fileChange/patchUpdated": {"threadId": "string", "turnId": "string", "itemId": "string", "changes": "array"},
    "item/agentMessage/delta": {"threadId": "string", "turnId": "string", "itemId": "string", "delta": "string"},
    "item/reasoning/textDelta": {"threadId": "string", "turnId": "string"},
    "serverRequest/resolved": {"threadId": "string", "requestId": "string|integer"},
    "item/commandExecution/requestApproval": {"threadId": "string", "turnId": "string", "itemId": "string",
        "?command": "string|null", "?cwd": "string|null", "?availableDecisions": "array|null"},
    "item/fileChange/requestApproval": {"threadId": "string", "turnId": "string", "itemId": "string", "?grantRoot": "string|null"},
    "item/permissions/requestApproval": {"threadId": "string", "turnId": "string", "permissions": "object"},
    "item/tool/requestUserInput": {"threadId": "string", "turnId": "string", "itemId": "string", "isBlocking": "boolean",
        "questions": "array", "questions.*.id": "string", "questions.*.header": "string",
        "questions.*.question": "string", "?questions.*.options": "array|null", "?questions.*.isSecret": "boolean"},
    "item/tool/call": {"threadId": "string", "turnId": "string", "callId": "string", "tool": "string", "arguments": "object"},
    "mcpServer/elicitation/request": {"threadId": "string", "serverName": "string", "?mode": "string",
        "?requestedSchema": "object", "?message": "string"},
    "error": {"threadId": "string", "turnId": "string", "error": "object"},
}
INBOUND_ITEM_FIELDS = {
    "agentMessage": {"text": "string"},
    "commandExecution": {"command": "string", "cwd": "string", "status": "string",
                         "?exitCode": "integer|null", "?durationMs": "integer|null"},
    "fileChange": {"changes": "array", "changes.*.path": "string", "changes.*.kind": "object",
                   "changes.*.kind.type": "string", "?changes.*.kind.move_path": "string|null", "changes.*.diff": "string"},
}
INBOUND_OPTIONAL_EXTENSIONS = {"item/fileChange/requestApproval": {"availableDecisions": "array|null"}}


def _field_values(value, path):
    if not path:
        yield value
    elif path[0] == "*" and isinstance(value, list):
        for item in value:
            yield from _field_values(item, path[1:])
    elif isinstance(value, dict) and path[0] in value:
        yield from _field_values(value[path[0]], path[1:])
    else:
        yield _MISSING


_MISSING = object()
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean",
               dict: "object", list: "array", type(None): "null"}


def _fields_error(value, fields, prefix="params"):
    for field, expected in fields.items():
        optional, field = field.startswith("?"), field.lstrip("?")
        for item in _field_values(value, field.split(".")):
            if item is _MISSING and optional:
                continue
            if item is _MISSING or _JSON_TYPES.get(type(item)) not in expected.split("|"):
                return f"{prefix}.{field} must be {expected}"
    return None


def native_message_error(message):
    """Check fields before handlers mutate state, without a schema dependency."""
    if not isinstance(message, dict) or not isinstance(message.get("method"), str) or not message["method"]:
        return "frame must contain a method string"
    method, params = message["method"], message.get("params", {})
    if not isinstance(params, dict):
        return "params must be an object"
    if "id" in message and type(message["id"]) not in (str, int):
        return "request id must be a string or integer"
    for field in ("threadId", "turnId"):
        if field in params and params[field] is not None and (not isinstance(params[field], str) or not params[field]):
            return f"params.{field} must be a nonempty string"
    for field in ("thread", "turn"):
        if field in params and not isinstance(params[field], dict):
            return f"params.{field} must be an object"
        if field in params and "id" in params[field] and (not isinstance(params[field]["id"], str) or not params[field]["id"]):
            return f"params.{field}.id must be a nonempty string"
    thread_id = params.get("threadId")
    nested_id = params.get("thread", {}).get("id")
    if thread_id and nested_id and thread_id != nested_id:
        return "thread identities disagree"
    turn = params.get("turn", {})
    if "rootTurnId" in turn and turn["rootTurnId"] is not None and (not isinstance(turn["rootTurnId"], str) or not turn["rootTurnId"]):
        return "params.turn.rootTurnId must be a nonempty string or null"
    if params.get("turnId") and turn.get("id") and params["turnId"] not in (turn["id"], turn.get("rootTurnId")):
        return "turn identities disagree"
    fields = INBOUND_FIELDS.get(method, {})
    # An owned thread/started notification needs only identity; adoption verifies
    # full scope through thread/resume. No fake-only metadata is invented here.
    if method == "thread/started":
        fields = {"thread.id": "string"}
    elif method == "mcpServer/elicitation/request":
        # serverName is display metadata; legacy relays may omit it.
        fields = {key: value for key, value in fields.items() if key != "serverName"}
    error = _fields_error(params, fields)
    if error:
        return error
    if "id" in message and method in NATIVE_SERVER_REQUESTS:
        error = _fields_error(params, {"?isBlocking": "boolean", **{
            "?" + field: expected for field, expected in INBOUND_OPTIONAL_EXTENSIONS.get(method, {}).items()}})
        if error:
            return error
        if method == "item/permissions/requestApproval":
            error = _fields_error(params, {
                "?permissions.network": "object|null", "?permissions.network.enabled": "boolean|null",
                "?permissions.fileSystem": "object|null", "?permissions.fileSystem.read": "array|null",
                "?permissions.fileSystem.write": "array|null", "?permissions.fileSystem.entries": "array|null",
                "?permissions.fileSystem.read.*": "string", "?permissions.fileSystem.write.*": "string",
                "?permissions.fileSystem.entries.*": "object"})
            if error:
                return error
        if method == "mcpServer/elicitation/request" and params.get("mode") in ("form", "openai/form"):
            error = _fields_error(params, {"requestedSchema": "object"})
            if error:
                return error
    if method in ("turn/started", "turn/completed") and not params["turn"]["id"]:
        return "params.turn.id must be a nonempty string"
    if method == "turn/completed" and params["turn"]["status"] not in ("completed", "failed", "interrupted"):
        return "params.turn.status must be completed, failed or interrupted"
    if method in ("item/started", "item/completed"):
        item = params["item"]
        item_fields = INBOUND_ITEM_FIELDS.get(item["type"], {})
        if method == "item/completed" or item["type"] == "fileChange":
            error = _fields_error(item, item_fields, "params.item")
            if error:
                return error
    if method == "item/fileChange/patchUpdated":
        return _fields_error(params, {"changes.*.path": "string", "changes.*.kind": "object", "changes.*.kind.type": "string",
                                     "changes.*.diff": "string"})
    if "id" in message and method == "item/tool/call" and params.get("tool") == "ask_parent":
        return _fields_error(params["arguments"], {"question": "string"}, "params.arguments")
    return None

# These acknowledgements are stricter than the generated schema's required list.
THREAD_SAFETY_FIELDS = ("activePermissionProfile", "cwd", "runtimeWorkspaceRoots", "approvalPolicy")
VERSION_REQUIRED = "Codex 0.167.0 or newer is required for the verified permission-profile protocol"
_VERSION = re.compile(
    r"(?:codex-cli[ \t]+)?(?P<version>"
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?)"
)


@dataclass(frozen=True)
class CodexVersion:
    version: str
    release: tuple[int, int, int]
    prerelease: tuple[str, ...]
    build: tuple[str, ...]


def parse_codex_version(value):
    match = _VERSION.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"Unrecognised Codex version: {value!r}")
    prerelease = tuple(match["prerelease"].split(".")) if match["prerelease"] else ()
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
        raise ValueError(f"Unrecognised Codex version: {value!r}")
    build = tuple(match["build"].split(".")) if match["build"] else ()
    return CodexVersion(match["version"], tuple(int(match[key]) for key in ("major", "minor", "patch")),
                        prerelease, build)


def codex_compatibility(value):
    """Classify release families; prereleases of the minimum remain untested."""
    try:
        version = parse_codex_version(value)
    except ValueError as exc:
        return {"version": value, "status": "unsupported", "reason": f"{exc}. {VERSION_REQUIRED}"}
    if version.release < parse_codex_version(CODEX_MIN_VERSION).release:
        status, reason = "unsupported", VERSION_REQUIRED
    elif version.version in CODEX_TESTED_VERSIONS:
        status, reason = "tested", "Version is in the tested Codex set"
    elif version.prerelease:
        status, reason = "untested", "Prerelease; required thread safety acknowledgements must still pass"
    else:
        status, reason = "untested", "Version is outside the tested Codex set; required thread safety acknowledgements must still pass"
    return {"version": value, "status": status, "reason": reason}


def method_unavailable(error):
    """Recognise method availability errors, never ordinary argument failures."""
    if not isinstance(error, dict):
        return False
    if error.get("code") == -32601:
        return True
    message = str(error.get("message", "")).strip().lower()
    if re.search(r"\b(?:method|request) (?:parameter|argument|field|id)\b", message):
        return False
    if message in ("unsupported", "not supported", "method not found"):
        return True
    return bool(re.search(r"\b(?:unsupported|unknown|unrecognised|unrecognized) (?:method|request)\b|"
                          r"\b(?:method|request|capability)(?: is)? (?:unsupported|not supported|unavailable|not available)\b",
                          message))
