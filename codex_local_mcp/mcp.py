"""Dual-era stdio router with an isolated per-request modern adapter.

The supported subset is deliberately small: tools, ping, cancellation, optional
progress and form elicitation. No dependency on client notification delivery.
"""

import asyncio
import json
import hashlib
import sys
import uuid

from . import __version__
from .broker import ensure_broker, rpc
from .security import BridgeError, redact
from .tools import BY_NAME, IDENTITY_FIELDS, arguments, tool_result, result_size, RESULT_BUDGET, FRAME_LIMIT, encoded_size, tool_catalog, result_envelope, error_envelope
from .platforms import stdio_lines
from .contract import MCP_VERSIONS, MCP_LEGACY_VERSIONS
from .backend import OptionalCapabilityUnavailable

VERSIONS = MCP_VERSIONS
INSTRUCTIONS = """Choose/create a project directory with codex_projects, then pass its
project_id or an existing absolute workdir to codex_start. No workspace config is
required. Select supported options using codex_models. Save task ids
and event cursors. Poll codex_events (wait_seconds up to 25) until a turn completes,
respond to ask_parent requests, and relay human approvals through
codex_review_request or the local review command. Never interpret silence as
approval. Reuse request_id only for identical control operations. Use codex_get
for summaries and command evidence. On reconnect, replay from the saved cursor;
after a backend restart, resume and inspect history before sending more work.
Native approve_for_me is selectable with approval_mode; it uses Codex's reviewer,
not approval_relay. Use codex_delegate for native delegation and codex_subagents
to discover children; parent event polling includes their progress and requests.
Check family_status and child statuses before treating delegated work as done.
Respect can_accept_direct_input on children. Use settings, fork, queue, review,
compact, archive and history controls for the corresponding native workflows.
Queued instructions can be consumed immediately: inspect before editing/retrying.
"""


def reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


async def fetch_review_request(config, request_id, exchange=None):
    exchange = exchange or rpc
    page = await exchange(config, "tool", name="codex_review_request", args={"request_id": request_id})
    if "detail" not in page:
        if page.get("details_truncated") or not page.get("detail_complete", True):
            raise BridgeError("Complete request content is unavailable; acceptance is disabled", code='invalid_response')
        return page
    digest, total = page["detail_sha256"], page["detail_total_chars"]
    chunks, offset = [], 0
    while True:
        if (page.get("detail_sha256") != digest or page.get("detail_offset") != offset
                or page.get("detail_total_chars") != total):
            raise BridgeError("Request changed while fetching details; refresh and review again", code='invalid_response')
        chunks.append(page["detail"])
        offset += len(page["detail"])
        if page["next_offset"] is None:
            break
        if page["next_offset"] != offset or not page["detail"]:
            raise BridgeError("Request detail pagination made no progress; acceptance is disabled", code='invalid_response')
        page = await exchange(config, "tool", name="codex_review_request", args={
            "request_id": request_id, "offset": offset, "limit": 262144})
    text = "".join(chunks)
    if len(text) != total or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise BridgeError("Request detail integrity check failed; acceptance is disabled", code='invalid_response')
    return json.loads(text)


class MCP:
    def __init__(self, config):
        self.config = config
        self.version = None
        self.ready = False
        self.capabilities = {}
        self.requests = {}
        self.pending_ui = {}
        self.write_lock = asyncio.Lock()
        self.modern = None

    async def write(self, message):
        if encoded_size(message) + 1 > FRAME_LIMIT:
            raise BridgeError("MCP response exceeds the transport frame limit", code='result_too_large')
        async with self.write_lock:
            sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()

    async def result(self, request_id, value):
        await self.write({"jsonrpc": "2.0", "id": request_id, "result": value})

    async def error(self, request_id, code, message, data=None):
        error = {"code": code, "message": redact(str(message))[:16384]}
        error["data"] = data if data is not None else error_envelope(BridgeError(str(message),
            code="invalid_arguments" if code in (-32700, -32600, -32601, -32602, -32002) else "bridge_error"))
        await self.write({"jsonrpc": "2.0", "id": request_id, "error": error})

    async def run(self):
        # Handshake and tools/list work without starting Codex or making network
        # requests. Broker startup is lazy on the first actual tool call.
        lines = stdio_lines(sys.stdin.buffer)
        try:
            while True:
                try:
                    line = await anext(lines)
                except StopAsyncIteration:
                    break
                except ValueError:
                    await self.error(None, -32700, "MCP frame exceeds the 1 MiB input limit")
                    break
                if not line:
                    break
                try:
                    message = json.loads(line, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    await self.error(None, -32700, "Invalid JSON")
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    await self.error(None, -32600, "Expected a JSON-RPC 2.0 object")
                    continue
                if "id" in message and type(message["id"]) not in (str, int):
                    await self.error(None, -32600, "Request id must be a string or integer")
                    continue
                if "method" not in message and ("result" in message or "error" in message):
                    future = self.pending_ui.get(message.get("id"))
                    if future and not future.done():
                        future.set_result(message)
                    # Late responses to expired human prompts are ignored.
                    continue
                request_id = message.get("id")
                method = message.get("method")
                if not isinstance(method, str):
                    await self.error(request_id, -32600, "Missing method")
                    continue
                if "id" not in message:
                    if method == "notifications/initialized" and self.version:
                        self.ready = True
                    elif method == "notifications/cancelled":
                        cancel_params = message.get("params") or {}
                        cancel_id = cancel_params.get("requestId") if isinstance(cancel_params, dict) else None
                        target = self.requests.get(cancel_id) if type(cancel_id) in (str, int) else None
                        if target:
                            target.cancel()
                    continue
                if type(request_id) not in (str, int):
                    await self.error(None, -32600, "Request id must be a string or integer")
                    continue
                if request_id in self.requests:
                    await self.error(request_id, -32600, "Duplicate active request id")
                    continue
                task = asyncio.create_task(self.handle(message))
                self.requests[request_id] = task
                task.add_done_callback(lambda _, key=request_id: self.requests.pop(key, None))
        finally:
            await lines.aclose()
            active = list(self.requests.values())
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    async def handle(self, message):
        request_id, method, params = message["id"], message["method"], message.get("params", {})
        if (method == "server/discover" or (isinstance(method, str) and method.startswith("tasks/") and self.version is None) or
                (isinstance(params, dict) and isinstance(params.get("_meta"), dict) and any(
                    key in params["_meta"] for key in ("io.modelcontextprotocol/protocolVersion", "io.modelcontextprotocol/clientCapabilities")))):
            from .mcp_modern import ModernMCP
            if self.modern is None:
                self.modern = ModernMCP(self)
            await self.modern.handle(message)
            return
        if not isinstance(params, dict):
            await self.error(request_id, -32602, "params must be an object")
            return
        try:
            if method == "initialize":
                if self.version:
                    await self.error(request_id, -32600, "Already initialized")
                    return
                requested = params.get("protocolVersion")
                client = params.get("clientInfo")
                if (not isinstance(requested, str) or not isinstance(params.get("capabilities"), dict)
                        or not isinstance(client, dict) or not isinstance(client.get("name"), str)
                        or not isinstance(client.get("version"), str)):
                    await self.error(request_id, -32602, "Invalid initialize parameters")
                    return
                self.version = requested if requested in MCP_LEGACY_VERSIONS else MCP_LEGACY_VERSIONS[-1]
                self.capabilities = params.get("capabilities", {})
                await self.result(request_id, {"protocolVersion": self.version,
                    "serverInfo": {"name": "codex-local-mcp", "version": __version__},
                    "capabilities": {"tools": {"listChanged": False}}, "instructions": INSTRUCTIONS})
            elif method == "ping":
                await self.result(request_id, {})
            elif not self.ready:
                await self.error(request_id, -32002, "Initialize and send notifications/initialized first")
            elif method == "tools/list":
                if params.get("cursor"):
                    await self.error(request_id, -32602, "No tools/list cursor is available")
                    return
                await self.result(request_id, {"tools": tool_catalog(self.version)})
            elif method == "tools/call":
                await self.call_tool(request_id, params)
            else:
                await self.error(request_id, -32601, "Method not found")
        except asyncio.CancelledError:
            # Cancelling a poll/tool connection does not kill a Codex task.
            # codex_interrupt is the explicit task cancellation mechanism.
            return
        except Exception as exc:
            await self.error(request_id, -32603, str(exc), error_envelope(exc))

    async def call_tool(self, request_id, params):
        try:
            name = params.get("name")
            if not isinstance(name, str):
                raise BridgeError("Tool name must be a string", code='invalid_response')
            args = arguments(name, params.get("arguments", {}))
        except BridgeError as exc:
            await self.error(request_id, -32602, str(exc), error_envelope(exc))
            return
        try:
            await ensure_broker(self.config)
            if name == "codex_review_request" and "offset" not in args:
                result = await self.review(args["request_id"])
            else:
                token = (params.get("_meta") or {}).get("progressToken")
                if name == "codex_events" and type(token) in (str, int, float):
                    await self.write({"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": 0, "total": 1,
                                   "message": "Waiting for Codex events"}})
                result = await rpc(self.config, "tool", name=name, args=args)
                if name == "codex_events" and type(token) in (str, int, float):
                    await self.write({"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": 1, "total": 1}})
            identity = {"pending_request_id": args["request_id"], "request_id_kind": "pending_request"} if name == "codex_review_request" else {}
            result = redact(result_envelope(result, **identity))
            if result_size(result) > RESULT_BUDGET:
                raise BridgeError("Result exceeds the response budget; request a smaller page", code='result_too_large')
            response = tool_result(result, structured=self.version >= "2025-06-18")
        except (BridgeError, OSError, TimeoutError) as exc:
            failure = tool_failure(exc, name, args)
            response = tool_result(failure, structured=self.version >= "2025-06-18")
            response["isError"] = True
        await self.result(request_id, response)

    async def review(self, request_id):
        req = await fetch_review_request(self.config, request_id)
        elicitation = self.capabilities.get("elicitation")
        supported = elicitation is not None and (elicitation == {} or "form" in elicitation)
        if not supported or self.version < "2025-06-18":
            from .tools import request_summary
            return {"status": "pending", "request": request_summary(req),
                    "next_step": "Client has no form elicitation. Run the local review command, or relay a denial with codex_respond."}
        form, convert, fallback = review_form(req)
        if form is None:
            return {"status": "pending", "request_id": request_id, "next_step": fallback}
        ui_id = "review:" + str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending_ui[ui_id] = future
        try:
            elicitation_params = dict(form)
            if self.version < "2025-11-25":
                elicitation_params.pop("mode")
            await self.write({"jsonrpc": "2.0", "id": ui_id, "method": "elicitation/create", "params": elicitation_params})
            answer = await asyncio.wait_for(future, 300)
            if "error" in answer or answer.get("result", {}).get("action") != "accept":
                return {"status": "pending", "request_id": request_id,
                        "next_step": "Review was declined, dismissed, or unavailable; no Codex decision was sent"}
            from .tools import validate
            content = answer["result"].get("content")
            validate(content, form["requestedSchema"])
            response = convert(content)
            args = {"request_id": request_id, "response": response, "review_source": "host_elicitation"}
            if req["kind"] == "file_approval" and response.get("decision") == "accept":
                args["proposal_hash"] = req.get("proposal_hash")
            return await rpc(self.config, "review", args=args)
        except TimeoutError:
            return {"status": "pending", "request_id": request_id, "next_step": "Review timed out; no approval was sent"}
        finally:
            self.pending_ui.pop(ui_id, None)


def tool_failure(exc, name, args):
    failure = error_envelope(exc)
    if BY_NAME[name]["annotations"]["readOnlyHint"] and failure["error"]["code"] in ("transport_unavailable", "native_timeout"):
        failure["error"]["retryable"] = True
    ids = failure["error"]["ids"]
    for key in IDENTITY_FIELDS:
        if args.get(key):
            ids.setdefault(key, args[key])
    if args.get("request_id"):
        ids.setdefault("pending_request_id" if name in ("codex_respond", "codex_review_request") else "operation_id", args["request_id"])
    return failure


def review_notice(req):
    lines = ["Codex requests human review. Nothing is approved by default.",
        "content_redacted: " + json.dumps(bool(req.get("content_redacted"))),
        "redaction_count: " + str(req.get("redaction_count", 0))]
    if req.get("review_warning"):
        lines.append(req["review_warning"])
    return "\n".join(lines)


def review_form(req):
    """One form policy for legacy UI, modern MRTR and task input."""
    if any(q.get("isSecret") for q in req["params"].get("questions", [])):
        return None, None, "Secret answers require Codex's native authentication or the provider's browser flow"
    schema, convert = review_schema(req)
    message = review_notice(req) + "\n" + json.dumps(req, ensure_ascii=False)
    if encoded_size({"message": message, "requestedSchema": schema}) > RESULT_BUDGET:
        return None, None, "Complete proposal exceeds the human UI budget; acceptance is disabled. Use the local review command."
    return {"mode": "form", "message": message, "requestedSchema": schema}, convert, None


def review_schema(req):
    kind, params = req["kind"], req["params"]
    if kind == "user_input":
        properties = {q["id"]: {"type": "string", "title": q.get("header") or q["id"],
                      "description": q.get("question", "") + "\n" + json.dumps(q.get("options") or [])}
                      for q in params["questions"]}
        return {"type": "object", "properties": properties, "required": list(properties)}, lambda c: {
            "answers": {key: {"answers": [value]} for key, value in c.items()}}
    if kind == "parent_question":
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, lambda c: c
    if kind == "elicitation" and params.get("mode") in ("form", "openai/form"):
        schema = params["requestedSchema"]
        if any(any(word in key.lower() for word in ("password", "secret", "token", "api_key")) for key in schema.get("properties", {})):
            raise BridgeError("Sensitive form requires direct local review in Codex", code='invalid_response')
        return schema, lambda c: {"action": "accept", "content": c}
    choices = ["decline", "cancel", "accept"]
    if kind in ("command_approval", "file_approval") and params.get("availableDecisions") is not None:
        choices = [x for x in choices if x in params["availableDecisions"]]
    schema = {"type": "object", "properties": {"decision": {"type": "string", "enum": choices,
        "description": "accept grants this request once; decline denies it; cancel cancels it"}}, "required": ["decision"]}

    def convert(content):
        decision = content["decision"]
        if kind == "permissions":
            return {"permissions": params["permissions"] if decision == "accept" else {}, "scope": "turn"}
        if kind == "elicitation":
            return {"action": decision, "content": None}
        return {"decision": decision}
    return schema, convert
