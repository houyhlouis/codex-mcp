"""Real frontend/broker/fake-backend Task handles; bounded, no model calls."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from codex_local_mcp.config import load_config
from codex_local_mcp.mcp_modern import META_VERSION, META_CAPABILITIES
from codex_local_mcp.contract import MCP_MODERN_VERSION, MCP_TASKS_EXTENSION
from tests.helpers import install_fake_codex, stop_test_broker, temp_root, write_config
from tests.test_mcp import Client


class ModernIPCTest(unittest.IsolatedAsyncioTestCase):
    async def test_execution_handle_and_input_survive_frontend_reconnect(self):
        with tempfile.TemporaryDirectory(prefix="mipc-", dir=temp_root()) as directory:
            base = Path(directory).resolve()
            config_path = base / "config.toml"
            write_config(config_path, base / "state", install_fake_codex(base))
            config = load_config(str(config_path))
            clients = []
            async def call(client, method, params=None):
                return await asyncio.wait_for(client.request(method, {**(params or {}), "_meta": {
                    META_VERSION: MCP_MODERN_VERSION, META_CAPABILITIES: {
                        "extensions": {MCP_TASKS_EXTENSION: {}}, "elicitation": {"form": {}}}}}), 25)
            async def client():
                value = Client(config_path)
                clients.append(value)
                await asyncio.wait_for(value.start(), 25)
                return value
            async def wait_native(value, task_id, status):
                for _ in range(20):
                    current = await value.tool("codex_get", task_id=task_id)
                    if current["status"] == status:
                        return current
                    await value.tool("codex_events", task_id=task_id, after=current["event_cursor"], wait_seconds=1)
                self.fail("Native state did not reach " + status)
            try:
                first = await client()
                project = await first.tool("codex_projects", action="create", path=str(base / "project"))
                created = (await call(first, "tools/call", {"name": "codex_start", "arguments": {
                    "project_id": project["project_id"], "prompt": "approval", "operation_id": "ipc-task"}}))["result"]
                domain = (await first.tool("codex_list"))["tasks"][0]
                await asyncio.wait_for(wait_native(first, domain["id"], "waiting"), 30)
                await asyncio.wait_for(first.close(), 8)
                second = await client()
                pending = (await call(second, "tasks/get", {"taskId": created["taskId"]}))["result"]
                self.assertEqual(pending["status"], "input_required")
                key = next(iter(pending["inputRequests"]))
                response = await call(second, "tasks/update", {"taskId": created["taskId"], "inputResponses": {
                    key: {"action": "accept", "content": {"decision": "decline"}}}})
                self.assertEqual(response["result"]["resultType"], "complete")
                await asyncio.wait_for(wait_native(second, domain["id"], "completed"), 30)
                completed = (await call(second, "tasks/get", {"taskId": created["taskId"]}))["result"]
                self.assertEqual(completed["status"], "completed")
            finally:
                for value in clients:
                    if hasattr(value, "process") and value.process.returncode is None:
                        try:
                            await asyncio.wait_for(value.close(), 8)
                        except (OSError, TimeoutError):
                            value.process.kill()
                            await asyncio.wait_for(value.process.wait(), 5)
                            value.reader.cancel()
                            await asyncio.wait_for(asyncio.gather(value.reader, return_exceptions=True), 5)
                await asyncio.wait_for(stop_test_broker(config), 25)
