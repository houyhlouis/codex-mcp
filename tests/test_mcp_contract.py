import copy
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_local_mcp.contract import MCP_LEGACY_VERSIONS as MCP_VERSIONS
from codex_local_mcp.mcp import MCP
from codex_local_mcp.tools import TOOLS, encoded_size, result_envelope, tool_catalog
from tests.mcp_wire import CATALOG_FIXTURE, capture_mcp_wire, message_shape, revision_fixture

FIXTURES = Path(__file__).with_name("fixtures")


class LegacyMCPContractTest(unittest.IsolatedAsyncioTestCase):
    def test_catalog_stays_within_revision_budgets(self):
        for version in MCP_VERSIONS:
            budget = 54000 if version >= "2025-06-18" else 40000
            with self.subTest(version=version):
                self.assertLessEqual(encoded_size({"tools": tool_catalog(version)}), budget)

    async def test_standard_handshake_and_catalog_frames_fit_64_kib(self):
        for version in MCP_VERSIONS:
            adapter, output = MCP(None), io.BytesIO()
            with patch("codex_local_mcp.mcp.sys.stdout", SimpleNamespace(buffer=output)):
                await adapter.handle({"jsonrpc": "2.0", "id": "initialize", "method": "initialize", "params": {
                    "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "frame-test", "version": "1"}}})
                adapter.ready = True
                await adapter.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}})
            frames = output.getvalue().splitlines(keepends=True)
            self.assertEqual(len(frames), 2)
            for frame in frames:
                self.assertLessEqual(len(frame), 64 * 1024, (version, len(frame)))
            self.assertEqual(json.loads(frames[1])["result"], {"tools": tool_catalog(version)})

    def check_transcript(self, fixture):
        version = fixture["protocol_version"]
        responses = {frame["message"]["id"]: frame["message"] for frame in fixture["messages"]
                     if frame["direction"] == "outbound" and "result" in frame["message"]}
        self.assertEqual(responses["initialize"]["result"]["protocolVersion"], version)
        tools = responses["tools"]["result"]["tools"]
        if isinstance(tools, dict):
            expected = {"$fixture": CATALOG_FIXTURE}
            if version < "2025-06-18":
                expected["omit"] = ["outputSchema"]
            self.assertEqual(tools, expected)
            tools = json.loads((FIXTURES / CATALOG_FIXTURE).read_text(encoding="utf-8"))["tools"]
            if version < "2025-06-18":
                tools = [{k: v for k, v in t.items() if k != "outputSchema"} for t in tools]
        self.assertEqual(tools, tool_catalog(version))
        self.assertTrue(tools)
        self.assertEqual(len({tool["name"] for tool in tools}), len(tools))
        self.assertTrue(all(tool["inputSchema"]["type"] == "object" for tool in tools))
        called = responses["call"]["result"]
        self.assertFalse(called["isError"])
        self.assertEqual(json.loads(called["content"][0]["text"]), result_envelope({"projects": [], "next_offset": None}))
        self.assertEqual("structuredContent" in called, version >= "2025-06-18")
        unsupported = responses["unsupported"]["result"]
        self.assertTrue(unsupported["isError"])
        error = json.loads(unsupported["content"][0]["text"])["error"]
        self.assertEqual(error["code"], "optional_native_capability_unavailable")
        self.assertEqual(error["method"], "account/rateLimits/read")
        self.assertEqual("structuredContent" in unsupported, version >= "2025-06-18")
        elicitation = fixture["client_capabilities"].get("elicitation")
        form = elicitation is not None and (elicitation == {} or "form" in elicitation)
        expected_form = version >= "2025-06-18" and form
        prompts = [frame["message"] for frame in fixture["messages"]
                   if frame["message"].get("method") == "elicitation/create"]
        self.assertEqual(len(prompts), int(expected_form))
        reviews = [call for call in fixture["broker_calls"] if call["method"] == "review"]
        if expected_form:
            self.assertEqual("mode" in prompts[0]["params"], version >= "2025-11-25")
            self.assertEqual(prompts[0]["params"].get("mode", "form"), "form")
            self.assertEqual(reviews[0]["args"]["response"], {"decision": "decline"})
        else:
            self.assertFalse(reviews)
            self.assertEqual(json.loads(responses["review"]["result"]["content"][0]["text"])["status"], "pending")

    async def test_all_supported_revisions_match_recorded_real_adapter_shapes(self):
        for version in MCP_VERSIONS:
            with self.subTest(version=version):
                stored = json.loads((FIXTURES / f"mcp-{version}.json").read_text(encoding="utf-8"))
                current = await capture_mcp_wire(version)
                self.check_transcript(stored)
                self.check_transcript(current)
                self.assertEqual(message_shape(revision_fixture(current)), message_shape(stored))

    async def test_tool_catalog_matches_one_shared_fixture(self):
        stored = json.loads((FIXTURES / CATALOG_FIXTURE).read_text(encoding="utf-8"))
        self.assertEqual(stored["tools"], TOOLS,
                         "Regenerate all fixtures with scripts/check_protocol.py --update-fixtures")
        current = await capture_mcp_wire(MCP_VERSIONS[-1])
        result = next(frame["message"]["result"] for frame in current["messages"]
                      if frame["direction"] == "outbound" and frame["message"].get("id") == "tools")
        self.assertEqual(result, {"tools": stored["tools"]})

    async def test_catalog_changes_do_not_duplicate_revision_fixture_failures(self):
        changed = copy.deepcopy(TOOLS)
        changed[0]["inputSchema"]["properties"]["future_optional_page"] = {"type": "string"}
        stored = json.loads((FIXTURES / CATALOG_FIXTURE).read_text(encoding="utf-8"))
        self.assertNotEqual(stored["tools"], changed)
        for version in MCP_VERSIONS:
            with self.subTest(version=version):
                original = await capture_mcp_wire(version)
                with patch("codex_local_mcp.tools.TOOLS", changed):
                    updated = await capture_mcp_wire(version)
                self.assertEqual(revision_fixture(original), revision_fixture(updated))

    async def test_form_capability_and_revision_both_control_elicitation(self):
        for version in MCP_VERSIONS:
            for capabilities in ({}, {"elicitation": {}}, {"elicitation": {"form": {}}},
                                 {"elicitation": {"url": {}}}, {"elicitation": {"form": {}, "url": {}}}):
                with self.subTest(version=version, capabilities=capabilities):
                    self.check_transcript(await capture_mcp_wire(version, capabilities))
