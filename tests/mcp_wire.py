"""Exercise MCP request handlers with explicit frames, without process stdio."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

from codex_local_mcp.mcp import MCP
from codex_local_mcp.backend import OptionalCapabilityUnavailable
from codex_local_mcp.contract import MCP_MODERN_VERSION
from tests.helpers import normalise_bridge_version

CATALOG_FIXTURE = "mcp-tools.json"


def revision_fixture(transcript):
    """Keep revision-specific frames; the shared catalog is pinned once."""
    value = copy.deepcopy(transcript)
    value["catalog_fixture"] = CATALOG_FIXTURE
    for frame in value["messages"]:
        message = frame["message"]
        if frame["direction"] == "outbound" and message.get("id") == "tools":
            message["result"]["tools"] = {"$fixture": CATALOG_FIXTURE}
            if transcript["protocol_version"] < "2025-06-18":
                message["result"]["tools"]["omit"] = ["outputSchema"]
    return value


class RecordingMCP(MCP):
    def __init__(self):
        super().__init__(None)
        self.frames = []
        self.elicited = asyncio.Event()
        self.ui_id = None

    async def write(self, message):
        message = json.loads(json.dumps(message, ensure_ascii=False))
        self.frames.append({"direction": "outbound", "message": message})
        if message.get("method") == "elicitation/create":
            self.ui_id = message["id"]
            self.elicited.set()


async def capture_mcp_wire(version, capabilities=None):
    if version == MCP_MODERN_VERSION:
        from tests.modern_wire import capture_modern_wire
        return await asyncio.wait_for(capture_modern_wire(capabilities), 30)
    return await asyncio.wait_for(_capture_mcp_wire(version, capabilities), 15)


async def _capture_mcp_wire(version, capabilities=None):
    capabilities = {"elicitation": {"form": {}}} if capabilities is None else capabilities
    adapter = RecordingMCP()
    broker_calls = []
    review = {"id": "fixture-request", "task_id": "fixture-task", "kind": "command_approval",
              "params": {"command": "printf fixture", "cwd": "/fixture/project",
                         "availableDecisions": ["decline", "cancel", "accept"]}}

    async def broker(config, method, *, name=None, args=None, **kwargs):
        broker_calls.append({"method": method, "name": name, "args": args})
        if method == "review":
            return {"status": "sent", "request_id": args["request_id"]}
        if name == "codex_review_request":
            return review
        if name == "codex_projects":
            return {"projects": [], "next_offset": None}
        if name == "codex_usage":
            raise OptionalCapabilityUnavailable("account/rateLimits/read", "codex-cli 0.167.0", {})
        raise AssertionError((method, name))

    def incoming(message):
        adapter.frames.append({"direction": "inbound", "message": message})
        return message

    with patch("codex_local_mcp.mcp.ensure_broker", AsyncMock()), patch("codex_local_mcp.mcp.rpc", broker):
        await asyncio.wait_for(adapter.handle(incoming({"jsonrpc": "2.0", "id": "initialize", "method": "initialize", "params": {
            "protocolVersion": version, "clientInfo": {"name": "contract-test", "version": "1"},
            "capabilities": capabilities}})), 5)
        incoming({"jsonrpc": "2.0", "method": "notifications/initialized"})
        adapter.ready = True
        await asyncio.wait_for(adapter.handle(incoming({"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}})), 5)
        await asyncio.wait_for(adapter.handle(incoming({"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {
            "name": "codex_projects", "arguments": {"action": "list"}}})), 5)
        await asyncio.wait_for(adapter.handle(incoming({"jsonrpc": "2.0", "id": "unsupported", "method": "tools/call", "params": {
            "name": "codex_usage", "arguments": {}}})), 5)
        request = incoming({"jsonrpc": "2.0", "id": "review", "method": "tools/call", "params": {
            "name": "codex_review_request", "arguments": {"request_id": "fixture-request"}}})
        reviewing = asyncio.create_task(adapter.handle(request))
        elicitation = capabilities.get("elicitation")
        form = elicitation is not None and (elicitation == {} or "form" in elicitation)
        try:
            if version >= "2025-06-18" and form:
                await asyncio.wait_for(adapter.elicited.wait(), 5)
                response = incoming({"jsonrpc": "2.0", "id": adapter.ui_id,
                                     "result": {"action": "accept", "content": {"decision": "decline"}}})
                adapter.pending_ui[adapter.ui_id].set_result(response)
            await asyncio.wait_for(reviewing, 5)
        finally:
            if not reviewing.done():
                reviewing.cancel()
            await asyncio.gather(reviewing, return_exceptions=True)
    for frame in adapter.frames:
        message = frame["message"]
        if isinstance(message.get("id"), str) and message["id"].startswith("review:"):
            message["id"] = "review:fixture"
    return normalise_bridge_version({"format": 1, "protocol_version": version, "client_capabilities": capabilities,
            "source": "MCP request handlers with explicit frames and a deterministic broker stub",
            "messages": adapter.frames, "broker_calls": broker_calls})


def message_shape(value):
    """Compare wire structure without freezing descriptions or server versions."""
    if isinstance(value, dict):
        return {key: item if key in ("method", "protocolVersion", "mode", "name", "type") else message_shape(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [message_shape(item) for item in value]
    return type(value).__name__
