#!/usr/bin/env python3
"""Optional black-box interoperability check using official mcp==1.26.0.

Uses the deterministic fake Codex process, not a real account. Install the MCP
SDK in a separate test environment; it is not a server runtime dependency.
"""
import asyncio
import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from codex_local_mcp.config import load_config
from codex_local_mcp.tools import TOOLS
from tests.helpers import install_fake_codex, write_config, stop_test_broker, temp_root


async def main():
    with tempfile.TemporaryDirectory(prefix="mcp-sdk-", dir=temp_root()) as directory:
        base = Path(directory).resolve()
        (base / "projects").mkdir()
        fixture = install_fake_codex(base)
        config_path = base / "config.toml"
        write_config(config_path, base / "state", fixture)
        config = load_config(str(config_path))
        reviewed = []

        async def elicit(context, params):
            reviewed.append(params.message)
            return ElicitResult(action="accept", content={"decision": "accept"})

        params = StdioServerParameters(command=sys.executable, args=[str(REPO / "server.py"),
                                       "--config", str(config_path), "serve"])
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, elicitation_callback=elicit) as session:
                    init = await session.initialize()
                    assert init.protocolVersion == "2025-11-25"
                    tools = await session.list_tools()
                    assert len(tools.tools) == len(TOOLS)
                    await session.call_tool("codex_projects", {"action": "create", "path": str(base / "projects" / "test")})
                    started = await session.call_tool("codex_start", {"workdir": str(base / "projects" / "test"),
                        "prompt": "approval", "request_id": "sdk-start"})
                    assert not started.isError, started
                    task = started.structuredContent
                    for _ in range(50):
                        current = (await session.call_tool("codex_get", {"task_id": task["id"]})).structuredContent
                        if current["pending_requests"]:
                            break
                        await asyncio.sleep(0.02)
                    req = current["pending_requests"][0]
                    response = await session.call_tool("codex_review_request", {"request_id": req["id"]})
                    assert not response.isError and response.structuredContent["status"] == "sent", response
                    assert reviewed
                    for _ in range(50):
                        current = (await session.call_tool("codex_get", {"task_id": task["id"]})).structuredContent
                        if current["status"] == "completed":
                            break
                        await asyncio.sleep(0.02)
                    assert current["status"] == "completed"
                    print(f"Official MCP SDK {version('mcp')}, negotiated MCP {init.protocolVersion}: "
                          f"initialize, {len(TOOLS)} tool schemas, structured results, coding lifecycle and form elicitation passed")
        finally:
            await stop_test_broker(config)


asyncio.run(main())
