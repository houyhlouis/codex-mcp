import json
import unittest
from pathlib import Path

from codex_local_mcp.contract import MCP_MODERN_VERSION, MCP_MODERN_CATALOG_BUDGET
from codex_local_mcp.tools import encoded_size
from tests.mcp_wire import capture_mcp_wire, revision_fixture, message_shape


class ModernWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_modern_frames_match_real_adapter_and_have_separate_budgets(self):
        directory = Path(__file__).with_name("fixtures")
        recorded = json.loads((directory / f"mcp-{MCP_MODERN_VERSION}.json").read_text())
        current = await capture_mcp_wire(MCP_MODERN_VERSION)
        self.assertEqual(message_shape(revision_fixture(current)), message_shape(recorded))
        results = {frame["message"]["id"]: frame["message"]["result"] for frame in current["messages"]
            if frame["direction"] == "outbound" and "result" in frame["message"]}
        self.assertLessEqual(encoded_size(results["tools"]), MCP_MODERN_CATALOG_BUDGET)
        self.assertEqual(results["create"]["resultType"], "task")
        self.assertEqual(results["pending"]["status"], "input_required")
        self.assertEqual(results["cancel-pending"]["status"], "working")
        self.assertEqual(results["cancelled"]["status"], "cancelled")
        self.assertEqual(results["complete"]["status"], "completed")
        self.assertEqual(results["review"]["resultType"], "input_required")
        self.assertTrue(all(frame["message"].get("method") != "elicitation/create" for frame in current["messages"]))
