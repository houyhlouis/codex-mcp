"""Black-box wire tests: MCP process -> private broker -> fake Codex process."""

import asyncio
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from codex_local_mcp.broker import rpc
from codex_local_mcp.config import load_config
from tests.helpers import install_fake_codex, write_config, stop_test_broker, temp_root
from codex_local_mcp.platforms import IS_WINDOWS, process_running

REPO = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self, config):
        self.config = config
        self.counter = 0
        self.futures = {}
        self.server_requests = asyncio.Queue()
        self.notifications = []

    async def start(self, capabilities=None, version="2025-11-25"):
        self.process = await asyncio.create_subprocess_exec(sys.executable, str(REPO / "server.py"),
            "--config", str(self.config), "serve", cwd=tempfile.gettempdir(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.reader = asyncio.create_task(self.read())
        result = await self.request("initialize", {"protocolVersion": version,
            "clientInfo": {"name": "wire-test", "version": "1"}, "capabilities": capabilities or {}})
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if "method" in message:
                if "id" in message:
                    await self.server_requests.put(message)
                else:
                    self.notifications.append(message)
            else:
                future = self.futures.get(message.get("id"))
                if future and not future.done():
                    future.set_result(message)

    async def request(self, method, params=None):
        self.counter += 1
        key = str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.futures[key] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": key, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, 20)
        finally:
            self.futures.pop(key, None)

    async def tool(self, tool_name, **args):
        reply = await self.request("tools/call", {"name": tool_name, "arguments": args})
        if "error" in reply:
            raise AssertionError(reply)
        result = reply["result"]
        if result.get("isError"):
            raise AssertionError(result)
        return json.loads(result["content"][0]["text"])

    async def close(self, kill=False):
        if self.process.returncode is None:
            if kill:
                self.process.kill()
            else:
                self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), 5)
        await self.reader
        return (await self.process.stderr.read()).decode()


class MCPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-wire-", dir=temp_root())
        self.base = Path(self.temp.name).resolve()
        (self.base / "projects").mkdir()
        fixture = install_fake_codex(self.base)
        self.config_file = self.base / "config.toml"
        write_config(self.config_file, self.base / "state", fixture)
        self.config = load_config(str(self.config_file))
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            if client.process.returncode is None:
                await client.close()
        await stop_test_broker(self.config)
        self.temp.cleanup()

    async def client(self, **kwargs):
        client = Client(self.config_file)
        self.clients.append(client)
        await client.start(**kwargs)
        return client

    async def task(self, client, prompt="echo", project="a", request_id="start"):
        directory = str(self.base / "projects" / project)
        descriptor = await client.tool("codex_projects", action="create", path=directory)
        return await client.tool("codex_start", project_id=descriptor["project_id"], prompt=prompt, request_id=request_id)

    async def until(self, client, task_id, statuses=("completed", "waiting", "interrupted")):
        for _ in range(100):
            task = await client.tool("codex_get", task_id=task_id)
            if task["status"] in statuses:
                return task
            await asyncio.sleep(0.025)
        self.fail("No expected task status")

    async def test_handshake_tools_and_no_raw_execution(self):
        client = await self.client()
        reply = await client.request("tools/list")
        names = [t["name"] for t in reply["result"]["tools"]]
        self.assertEqual(len(names), 32)
        self.assertNotIn("command/exec", names)
        self.assertEqual((await client.request("command/exec"))["error"]["code"], -32601)
        self.assertEqual((await client.request("ping"))["result"], {})

    async def test_normal_mcp_flow_and_replay_without_notifications(self):
        client = await self.client()
        task = await self.task(client)
        done = await self.until(client, task["id"])
        self.assertEqual(done["status"], "completed")
        page = await client.tool("codex_events", task_id=task["id"], after=0, limit=2)
        self.assertTrue(page["has_more"])
        self.assertEqual(client.notifications, [])
        if IS_WINDOWS:
            self.assertTrue(self.config.endpoint.startswith("\\\\.\\pipe\\codex-mcp-"))
        else:
            self.assertEqual(stat.S_IMODE(self.config.socket.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.config.state_dir.stat().st_mode), 0o700)

    async def test_killed_mcp_client_does_not_kill_task(self):
        first = await self.client()
        task = await self.task(first, "hold")
        await first.close(kill=True)
        second = await self.client()
        current = await second.tool("codex_get", task_id=task["id"])
        self.assertEqual(current["status"], "running")
        events = await second.tool("codex_events", task_id=task["id"], after=0)
        self.assertTrue(events["events"])
        await second.tool("codex_interrupt", task_id=task["id"], expected_turn_id=current["turn_id"], request_id="stop")
        self.assertEqual((await self.until(second, task["id"]))["status"], "interrupted")

    async def test_two_clients_share_same_broker(self):
        first, second = await asyncio.gather(self.client(), self.client())
        await asyncio.gather(first.tool("codex_projects"), second.tool("codex_projects"))
        pid = (await rpc(self.config, "ping"))["pid"]
        task = await self.task(first, "hold")
        current = await second.tool("codex_get", task_id=task["id"])
        self.assertEqual(current["thread_id"], task["thread_id"])
        self.assertEqual((await rpc(self.config, "ping"))["pid"], pid)

    async def test_broker_crash_recovery_does_not_replay_turn_or_approval(self):
        client = await self.client()
        task = await self.task(client, "approval")
        request = (await self.until(client, task["id"]))["pending_requests"][0]
        pid = (await rpc(self.config, "ping"))["pid"]
        os.kill(pid, signal.SIGTERM if IS_WINDOWS else signal.SIGKILL)
        await asyncio.sleep(0.1)
        current = await client.tool("codex_get", task_id=task["id"])
        self.assertEqual(current["status"], "needs_resume")
        self.assertEqual(current["pending_requests"], [])
        reply = await client.request("tools/call", {"name": "codex_respond", "arguments": {
            "request_id": request["id"], "response": {"decision": "accept"}}})
        self.assertTrue(reply["result"]["isError"])
        result = await client.tool("codex_resume", task_id=task["id"], request_id="resume")
        self.assertEqual(result["status"], "idle")
        history = await client.tool("codex_history", task_id=task["id"])
        self.assertEqual(len(history["data"]), 1)

    async def test_client_without_elicitation_gets_pending_fallback(self):
        client = await self.client()
        task = await self.task(client, "approval")
        req = (await self.until(client, task["id"]))["pending_requests"][0]
        result = await client.tool("codex_review_request", request_id=req["id"])
        self.assertEqual(result["status"], "pending")
        await client.tool("codex_respond", request_id=req["id"], response={"decision": "decline"})

    async def test_elicitation_accept_goes_through_human_channel(self):
        client = await self.client(capabilities={"elicitation": {"form": {}}})
        task = await self.task(client, "approval")
        req = (await self.until(client, task["id"]))["pending_requests"][0]
        review = asyncio.create_task(client.tool("codex_review_request", request_id=req["id"]))
        prompt = await asyncio.wait_for(client.server_requests.get(), 5)
        self.assertEqual(prompt["method"], "elicitation/create")
        self.assertEqual(prompt["params"]["mode"], "form")
        self.assertNotIn("default", prompt["params"]["requestedSchema"]["properties"]["decision"])
        await client.send({"jsonrpc": "2.0", "id": prompt["id"], "result": {"action": "accept", "content": {"decision": "accept"}}})
        self.assertEqual((await review)["status"], "sent")
        self.assertEqual((await self.until(client, task["id"]))["status"], "completed")

    async def test_dismissed_elicitation_does_not_approve(self):
        client = await self.client(capabilities={"elicitation": {"form": {}}})
        task = await self.task(client, "approval")
        req = (await self.until(client, task["id"]))["pending_requests"][0]
        review = asyncio.create_task(client.tool("codex_review_request", request_id=req["id"]))
        prompt = await asyncio.wait_for(client.server_requests.get(), 5)
        await client.send({"jsonrpc": "2.0", "id": prompt["id"], "result": {"action": "cancel"}})
        self.assertEqual((await review)["status"], "pending")
        self.assertEqual((await client.tool("codex_get", task_id=task["id"]))["status"], "waiting")

    async def test_invalid_arguments_return_protocol_error(self):
        client = await self.client()
        result = await client.request("tools/call", {"name": "codex_start", "arguments": {
            "root": "work", "prompt": "hello", "request_id": "x", "sandbox": "danger-full-access"}})
        self.assertEqual(result["error"]["code"], -32602)

    async def test_extended_native_controls_through_mcp(self):
        client = await self.client()
        task = await self.task(client)
        await self.until(client, task["id"])
        changed = await client.tool("codex_settings", task_id=task["id"], scope="next", approval_mode="approve_for_me", request_id="settings")
        self.assertEqual(changed["approval_mode"], "approve_for_me")
        fork = await client.tool("codex_fork", task_id=task["id"], name="MCP fork", request_id="fork")
        self.assertEqual(fork["name"], "MCP fork")
        await client.tool("codex_archive", task_id=fork["id"], request_id="archive")
        await client.tool("codex_archive", task_id=fork["id"], action="unarchive", request_id="restore")
        self.assertIn("rateLimits", await client.tool("codex_usage"))

    async def test_late_ui_reply_and_malformed_notifications_do_not_break_session(self):
        client = await self.client()
        await client.send({"jsonrpc": "2.0", "id": "review:expired", "result": {"action": "accept", "content": {"decision": "accept"}}})
        await client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": ["invalid"]})
        self.assertEqual((await client.request("ping"))["result"], {})

    async def test_old_client_and_future_handshake_negotiation(self):
        client = await self.client(version="2024-11-05")
        result = await client.request("tools/call", {"name": "codex_projects", "arguments": {}})
        self.assertNotIn("structuredContent", result["result"])
        other = Client(self.config_file)
        self.clients.append(other)
        reply = await other.start(version="2026-07-28")
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")

    async def test_stop_waits_for_the_broker_process_to_exit(self):
        client = await self.client()
        await client.tool("codex_projects")
        await client.close()
        pid = (await rpc(self.config, "ping"))["pid"]
        result = await stop_test_broker(self.config)
        self.assertEqual(result, {"stopped": True, "pid": pid})
        if IS_WINDOWS:
            self.assertFalse(process_running(pid))
        # The state directory can be cleaned up immediately (Windows locks open files).
        (self.config.state_dir / "broker.log").unlink()
        self.assertEqual((await stop_test_broker(self.config))["stopping"], False)

    async def test_progress_is_optional_and_poll_cancel_keeps_task(self):
        client = await self.client()
        task = await self.task(client, "hold")
        first = await client.tool("codex_events", task_id=task["id"])
        poll = asyncio.create_task(client.request("tools/call", {"name": "codex_events",
            "arguments": {"task_id": task["id"], "after": first["cursor"], "wait_seconds": 10},
            "_meta": {"progressToken": "progress"}}))
        for _ in range(100):
            if client.notifications:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(client.notifications[-1]["method"], "notifications/progress")
        await client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": str(client.counter)}})
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
        self.assertEqual((await client.tool("codex_get", task_id=task["id"]))["status"], "running")


if __name__ == "__main__":
    unittest.main()
