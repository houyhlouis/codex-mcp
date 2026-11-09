"""MCP stdio adapter for handshake-era clients (2024-11-05..2025-11-25).

The supported subset is deliberately small: tools, ping, cancellation, optional
progress and form elicitation. No dependency on client notification delivery.
"""

import asyncio
import json
import sys
import uuid

from . import __version__
from .broker import ensure_broker, rpc
from .security import BridgeError, redact
from .tools import TOOLS, arguments
from .platforms import stdio_lines

VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
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


class MCP:
    def __init__(self, config):
        self.config = config
        self.version = None
        self.ready = False
        self.capabilities = {}
        self.requests = {}
        self.pending_ui = {}
        self.write_lock = asyncio.Lock()

    async def write(self, message):
        async with self.write_lock:
            sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()

    async def result(self, request_id, value):
        await self.write({"jsonrpc": "2.0", "id": request_id, "result": value})

    async def error(self, request_id, code, message):
        await self.write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": redact(message)}})

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
                self.version = requested if requested in VERSIONS else VERSIONS[-1]
                self.capabilities = params.get("capabilities", {})
                await self.result(request_id, {"protocolVersion": self.version,
                    "serverInfo": {"name": "codex-local-mcp", "version": __version__},
                    "capabilities": {"tools": {"listChanged": False}}, "instructions": INSTRUCTIONS})
            elif method == "ping":
                await self.result(request_id, {})
            elif method == "server/discover":
                # Modern SDKs use this method to probe, then fall back to the
                # supported initialize lifecycle on MethodNotFound.
                await self.error(request_id, -32601, "Use initialize; supported through MCP 2025-11-25")
            elif not self.ready:
                await self.error(request_id, -32002, "Initialize and send notifications/initialized first")
            elif method == "tools/list":
                if params.get("cursor"):
                    await self.error(request_id, -32602, "No tools/list cursor is available")
                    return
                await self.result(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                await self.call_tool(request_id, params)
            else:
                await self.error(request_id, -32601, "Method not found")
        except asyncio.CancelledError:
            # Cancelling a poll/tool connection does not kill a Codex task.
            # codex_interrupt is the explicit task cancellation mechanism.
            return
        except Exception as exc:
            await self.error(request_id, -32603, str(exc))

    async def call_tool(self, request_id, params):
        try:
            name = params.get("name")
            if not isinstance(name, str):
                raise BridgeError("Tool name must be a string")
            args = arguments(name, params.get("arguments", {}))
        except BridgeError as exc:
            await self.error(request_id, -32602, str(exc))
            return
        try:
            await ensure_broker(self.config)
            if name == "codex_review_request":
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
            result = redact(result)
            response = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
            if self.version >= "2025-06-18":
                response["structuredContent"] = result
        except (BridgeError, OSError, TimeoutError) as exc:
            response = {"content": [{"type": "text", "text": redact(str(exc))}], "isError": True}
        await self.result(request_id, response)

    async def review(self, request_id):
        req = await rpc(self.config, "tool", name="codex_review_request", args={"request_id": request_id})
        elicitation = self.capabilities.get("elicitation")
        supported = elicitation is not None and (elicitation == {} or "form" in elicitation)
        if not supported or self.version < "2025-06-18":
            return {"status": "pending", "request": req,
                    "next_step": "Client has no form elicitation. Run the local review command, or relay a denial with codex_respond."}
        if any(q.get("isSecret") for q in req["params"].get("questions", [])):
            return {"status": "pending", "request_id": request_id,
                    "next_step": "Secret answers require Codex's native authentication or the provider's browser flow"}
        schema, convert = review_schema(req)
        message = "Codex requests human review. Nothing is approved by default.\n" + json.dumps(req, ensure_ascii=False)
        ui_id = "review:" + str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending_ui[ui_id] = future
        try:
            elicitation_params = {"message": message, "requestedSchema": schema}
            if self.version >= "2025-11-25":
                elicitation_params["mode"] = "form"
            await self.write({"jsonrpc": "2.0", "id": ui_id, "method": "elicitation/create", "params": elicitation_params})
            answer = await asyncio.wait_for(future, 300)
            if "error" in answer or answer.get("result", {}).get("action") != "accept":
                return {"status": "pending", "request_id": request_id,
                        "next_step": "Review was declined, dismissed, or unavailable; no Codex decision was sent"}
            from .tools import validate
            content = answer["result"].get("content")
            validate(content, schema)
            return await rpc(self.config, "review", args={"request_id": request_id, "response": convert(content)})
        except TimeoutError:
            return {"status": "pending", "request_id": request_id, "next_step": "Review timed out; no approval was sent"}
        finally:
            self.pending_ui.pop(ui_id, None)


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
            raise BridgeError("Sensitive form requires direct local review in Codex")
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
