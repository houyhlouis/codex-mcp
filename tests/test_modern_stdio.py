"""Real stdio discovery/catalog frames need neither broker nor Codex startup."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from codex_local_mcp.contract import MCP_MODERN_CATALOG_BUDGET, MCP_MODERN_VERSION
from codex_local_mcp.mcp_modern import META_VERSION, META_CAPABILITIES
from tests.helpers import install_fake_codex, temp_root, write_config


class ModernStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_per_request_discovery_and_catalog_without_handshake(self):
        with tempfile.TemporaryDirectory(prefix="mstdio-", dir=temp_root()) as directory:
            base = Path(directory).resolve()
            config = base / "config.toml"
            write_config(config, base / "state", install_fake_codex(base))
            process = await asyncio.create_subprocess_exec(sys.executable, "server.py", "--config", str(config), "serve",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                for index, method in enumerate(("server/discover", "tools/list", "ping")):
                    message = {"jsonrpc": "2.0", "id": index, "method": method, "params": {"_meta": {
                        META_VERSION: MCP_MODERN_VERSION, META_CAPABILITIES: {}}}}
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await asyncio.wait_for(process.stdin.drain(), 5)
                    try:
                        frame = await asyncio.wait_for(process.stdout.readline(), 5)
                    except TimeoutError:
                        process.kill()
                        await asyncio.wait_for(process.wait(), 5)
                        error = await asyncio.wait_for(process.stderr.read(), 5)
                        self.fail("MCP stdio did not answer: " + error.decode(errors="replace"))
                    response = json.loads(frame)
                    self.assertEqual(response["result"]["resultType"], "complete")
                    if method == "tools/list":
                        self.assertLessEqual(len(json.dumps(response["result"], ensure_ascii=False).encode()), MCP_MODERN_CATALOG_BUDGET)
                self.assertFalse((base / "state").exists())
                process.stdin.close()
                self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
                self.assertNotIn(b"Traceback", await asyncio.wait_for(process.stderr.read(), 5))
            finally:
                if process.returncode is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 5)
