"""Stateless MCP 2026-07-28 stdio requests and the Tasks extension."""
from . import __version__
from .broker import ensure_broker, rpc
from .contract import MCP_MODERN_VERSION, MCP_VERSIONS, MCP_TASKS_EXTENSION, MCP_MODERN_CATALOG_BUDGET
from .security import BridgeError, redact
from .tools import (BY_NAME, arguments, error_envelope, result_envelope, tool_result, tool_catalog,
                    encoded_size, RESULT_BUDGET)
from .protocol_tasks import ProtocolTasks

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"


def has_form(capabilities):
    value = capabilities.get("elicitation")
    return isinstance(value, dict) and (value == {} or isinstance(value.get("form"), dict))


def has_tasks(capabilities):
    extensions = capabilities.get("extensions")
    return isinstance(extensions, dict) and isinstance(extensions.get(MCP_TASKS_EXTENSION), dict)


class ModernMCP:
    def __init__(self, host):
        self.host, self.config = host, host.config

    async def result(self, request_id, value, *, catalog=False):
        value = {**value, "_meta": {**value.get("_meta", {}), META_SERVER: {"name": "codex-local-mcp", "version": __version__}}}
        if catalog and encoded_size(value) > MCP_MODERN_CATALOG_BUDGET:
            raise BridgeError(f"Modern MCP tool catalog exceeds {MCP_MODERN_CATALOG_BUDGET} bytes", code="result_too_large")
        if encoded_size(value) > RESULT_BUDGET:
            raise BridgeError("MCP result exceeds the response budget; use ordinary paged tools", code="result_too_large")
        await self.host.result(request_id, value)

    async def error(self, request_id, code, error, **data):
        await self.host.error(request_id, code, str(error), redact({**error_envelope(error), **data}))

    async def handle(self, message):
        request_id, method, params = message["id"], message["method"], message.get("params", {})
        try:
            meta = params.get("_meta") if isinstance(params, dict) else None
            if (not isinstance(meta, dict) or not isinstance(meta.get(META_VERSION), str)
                    or not isinstance(meta.get(META_CAPABILITIES), dict)):
                await self.error(request_id, -32602, BridgeError("Modern MCP requests require protocolVersion and clientCapabilities in _meta", code="invalid_arguments"))
                return
            version, capabilities = meta[META_VERSION], meta[META_CAPABILITIES]
            client = meta.get("io.modelcontextprotocol/clientInfo")
            extensions, elicitation = capabilities.get("extensions"), capabilities.get("elicitation")
            if ((client is not None and (not isinstance(client, dict) or not isinstance(client.get("name"), str) or not isinstance(client.get("version"), str)))
                    or (extensions is not None and (not isinstance(extensions, dict) or any(not isinstance(value, dict) for value in extensions.values())))
                    or (elicitation is not None and (not isinstance(elicitation, dict) or any(key in elicitation and not isinstance(elicitation[key], dict) for key in ("form", "url"))))):
                raise BridgeError("Invalid modern MCP client metadata", code="invalid_arguments")
            if version != MCP_MODERN_VERSION:
                await self.error(request_id, -32022, BridgeError("Unsupported protocol version", code="mcp_version_unsupported"),
                    requested=version, supported=list(MCP_VERSIONS))
                return
            if method.startswith("tasks/"):
                action = method.removeprefix("tasks/")
                if action not in ("get", "update", "cancel"):
                    await self.error(request_id, -32601, BridgeError("Unsupported MCP task operation", code="unsupported_task_operation"))
                    return
                if not has_tasks(capabilities):
                    await self.error(request_id, -32021, BridgeError("Missing required client capability", code="mcp_capability_required"),
                        requiredCapabilities={"extensions": {MCP_TASKS_EXTENSION: {}}})
                    return
                if not isinstance(params.get("taskId"), str):
                    raise BridgeError("tasks requests require taskId", code="invalid_arguments")
                values = {"taskId": params["taskId"], "form": has_form(capabilities)}
                if action == "update":
                    if not isinstance(params.get("inputResponses"), dict):
                        raise BridgeError("tasks/update requires inputResponses", code="invalid_arguments")
                    values["inputResponses"] = params["inputResponses"]
                await ensure_broker(self.config)
                value = await rpc(self.config, "mcp_tasks", name=action, args=values)
            elif method == "server/discover":
                from .mcp import INSTRUCTIONS
                value = {"resultType": "complete", "supportedVersions": list(MCP_VERSIONS), "capabilities": {
                    "tools": {}, "extensions": {MCP_TASKS_EXTENSION: {}}}, "instructions": INSTRUCTIONS,
                    "ttlMs": 0, "cacheScope": "private"}
            elif method == "tools/list":
                if params.get("cursor"):
                    raise BridgeError("No tools/list cursor is available", code="invalid_arguments")
                value = {"resultType": "complete", "tools": tool_catalog(MCP_MODERN_VERSION), "ttlMs": 0, "cacheScope": "private"}
            elif method == "ping":
                value = {"resultType": "complete"}
            elif method == "tools/call":
                value = await self.call_tool(params, capabilities)
            else:
                await self.error(request_id, -32601, BridgeError("Method not found", code="invalid_arguments"))
                return
            await self.result(request_id, value, catalog=method == "tools/list")
        except Exception as exc:
            error = exc if isinstance(exc, BridgeError) else BridgeError("Modern MCP request failed", code="internal_error")
            if isinstance(params, dict) and isinstance(params.get("taskId"), str):
                error.ids.setdefault("mcp_task_id", params["taskId"])
            code = -32602 if error.code in ("invalid_arguments", "invalid_response", "not_found", "stale_request", "stale_proposal", "stale_turn", "human_review_required", "person_review_required", "state_conflict") else -32603
            await self.error(request_id, code, error)

    async def call_tool(self, params, capabilities):
        name = params.get("name")
        if not isinstance(name, str) or name not in BY_NAME:
            raise BridgeError("Unknown tool", code="invalid_arguments")
        args = arguments(name, params.get("arguments", {}))
        responding = "inputResponses" in params or "requestState" in params
        if responding and name == "codex_review_request" and not isinstance(params.get("requestState"), str):
            raise BridgeError("Review responses require server-issued requestState", code="stale_request")
        if responding and (name != "codex_review_request" or not isinstance(params.get("inputResponses"), dict)):
            raise BridgeError("Review responses require the original tool and server-issued requestState", code="invalid_arguments")
        await ensure_broker(self.config)
        try:
            if name == "codex_review_request" and (responding or "offset" not in args):
                action = "review_answer" if responding else "review_begin"
                values = {"request_id": args["request_id"], "form": has_form(capabilities)}
                if responding:
                    values.update(requestState=params["requestState"], inputResponses=params["inputResponses"])
                return await rpc(self.config, "mcp_tasks", name=action, args=values)
            if has_tasks(capabilities) and ProtocolTasks.eligible(name, args):
                return await rpc(self.config, "mcp_tasks", name="create", args={"name": name, "arguments": args})
            result = await rpc(self.config, "tool", name=name, args=args)
            return {"resultType": "complete", **tool_result(redact(result_envelope(result)), structured=True)}
        except (BridgeError, OSError, TimeoutError) as exc:
            # Invalid/stale MRTR responses remain protocol errors so they cannot
            # be mistaken for a successful human decision.
            if responding or (isinstance(exc, BridgeError) and exc.code in ("invalid_arguments", "stale_request", "stale_proposal")):
                raise
            from .mcp import tool_failure
            return {"resultType": "complete", **tool_result(tool_failure(exc, name, args), structured=True), "isError": True}
