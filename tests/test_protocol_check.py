import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import jsonschema
except ImportError:
    jsonschema = None


@unittest.skipUnless(jsonschema, "Optional development dependency: jsonschema")
class ProtocolCheckerTest(unittest.TestCase):
    def setUp(self):
        from scripts.check_protocol import pinned_schema, validate_wire, wire_shapes
        fixtures = Path(__file__).with_name("fixtures")
        self.fixture = json.loads((fixtures / "native-wire.json").read_text(encoding="utf-8"))
        self.schema = json.loads((fixtures / "native-schema.json").read_text(encoding="utf-8"))
        self.schema_for = lambda key: pinned_schema(self.schema, key)
        self.validate_wire, self.wire_shapes = validate_wire, wire_shapes

    def test_unsupported_version_is_reported_before_recording_or_generating_schemas(self):
        from scripts.check_protocol import main
        with patch("scripts.check_protocol.subprocess.run", return_value=SimpleNamespace(stdout="codex-cli 0.166.0")) as run, \
                patch("scripts.check_protocol.capture_native_wire", AsyncMock()) as capture, \
                patch("builtins.print") as output:
            self.assertEqual(main("fixture-codex"), 1)
        run.assert_called_once_with(["fixture-codex", "--version"], check=True, capture_output=True, text=True, timeout=10)
        capture.assert_not_awaited()
        self.assertTrue(any("Compatibility: unsupported" in call.args[0] for call in output.call_args_list))

    def test_untested_build_cannot_rewrite_pinned_fixtures(self):
        from scripts.check_protocol import main
        with patch("scripts.check_protocol.subprocess.run", return_value=SimpleNamespace(stdout="codex-cli 0.168.0")), \
                patch("scripts.check_protocol.capture_native_wire", AsyncMock()) as capture, patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "Only a tested Codex version may update pinned fixtures"):
                main("fixture-codex", update_fixtures=True)
        capture.assert_not_awaited()

    def test_generated_schema_is_utf8_even_with_an_ascii_default_encoding(self):
        from scripts.check_protocol import SchemaIndex
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Unicode.json"
            path.write_text(json.dumps({"description": "界"}, ensure_ascii=False), encoding="utf-8")
            with patch("io.text_encoding", side_effect=lambda encoding, *args: encoding or "ascii"):
                self.assertEqual(SchemaIndex(directory).document("Unicode"), {"description": "界"})

    def test_one_refresh_updates_every_fixture_and_reports_changed_files(self):
        from scripts.check_protocol import refresh_fixtures
        from codex_local_mcp.contract import MCP_VERSIONS
        from tests.mcp_wire import CATALOG_FIXTURE
        transcripts = {version: {"protocol_version": version, "messages": [{"direction": "outbound",
            "message": {"id": "tools", "result": {"tools": [{"name": "catalog-is-extracted"}]}}}]}
            for version in MCP_VERSIONS}
        with tempfile.TemporaryDirectory() as directory:
            output = []
            refresh_fixtures(self.fixture, self.schema, transcripts, Path(directory), output.append)
            expected = {"native-wire.json", "native-schema.json", CATALOG_FIXTURE, "result-contract.json", "incident-lost-ack.json", "incident-pending-approval.json",
                        *(f"mcp-{version}.json" for version in MCP_VERSIONS)}
            self.assertEqual({path.name for path in Path(directory).iterdir()}, expected)
            self.assertEqual(len(output), len(expected))
            self.assertTrue(all(line.startswith("UPDATED ") and "sha256 absent ->" in line for line in output))
            for version in MCP_VERSIONS:
                document = json.loads((Path(directory) / f"mcp-{version}.json").read_text())
                reference = {"$fixture": CATALOG_FIXTURE}
                if version < "2025-06-18":
                    reference["omit"] = ["outputSchema"]
                self.assertEqual(document["messages"][0]["message"]["result"]["tools"], reference)
            output.clear()
            refresh_fixtures(self.fixture, self.schema, transcripts, Path(directory), output.append)
            self.assertEqual(len(output), len(expected))
            self.assertTrue(all(line.startswith("UNCHANGED ") for line in output))

    def test_unknown_non_probe_shape_is_reported_and_fails(self):
        fixture = {"messages": [{"direction": "outbound", "message": {
            "id": 1, "method": "future/newMethod", "params": {}}}]}
        output = []
        result = self.validate_wire(fixture, self.schema_for, "test", output=output.append)
        self.assertEqual(len(result["unchecked"]), 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(any("UNCHECKED test outbound:request:future/newMethod" in line for line in output))

    def test_invalid_actual_outbound_message_is_reported_and_fails(self):
        fixture = copy.deepcopy(self.fixture)
        message = next(frame["message"] for frame in fixture["messages"]
                       if frame["message"].get("method") == "turn/start")
        del message["params"]["threadId"]
        result = self.validate_wire(fixture, self.schema_for, "test", output=lambda _: None)
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("outbound:request:turn/start", result["failures"][0])

    def test_missing_schema_has_a_visible_reason_and_is_not_a_pass(self):
        fixture = {"messages": [{"direction": "outbound", "message": {
            "id": 1, "method": "turn/interrupt", "params": {"threadId": "t", "turnId": "u"}}}]}
        def missing(key):
            raise LookupError("Schema was removed by this Codex build")
        output = []
        result = self.validate_wire(fixture, missing, "test", output=output.append)
        self.assertEqual(result["counts"]["outbound"], 0)
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(any("Schema was removed" in line for line in output))

    def test_inbound_native_types_and_fake_consumed_fields_are_checked(self):
        from scripts.check_protocol import check_inbound_contract
        key = "inbound:notification:turn/completed"
        original = next(message for shape, message in self.wire_shapes(self.fixture) if shape == key)
        changed = copy.deepcopy(original)
        changed["params"]["turn"]["status"] = 123
        with self.assertRaisesRegex(ValueError, "incompatible type"):
            check_inbound_contract(key, changed, self.schema_for(key))
        schema = copy.deepcopy(self.schema_for(key))
        schema["definitions"]["Turn"]["properties"]["id"] = {"type": "integer"}
        with self.assertRaisesRegex(ValueError, "incompatible native schema type"):
            check_inbound_contract(key, original, schema)

    def test_boolean_true_schema_and_guarded_fake_extension_are_explicit(self):
        from scripts.check_protocol import check_inbound_contract, field_schema_types
        tool = "inbound:request:item/tool/call"
        schema = self.schema_for(tool)
        self.assertIn("object", field_schema_types(schema, ("params", "arguments")))
        approval = "inbound:request:item/fileChange/requestApproval"
        message = next(message for shape, message in self.wire_shapes(self.fixture) if shape == approval)
        extensions = check_inbound_contract(approval, message, self.schema_for(approval))
        self.assertTrue(any("availableDecisions (array" in value for value in extensions))
        bad = copy.deepcopy(message)
        bad["params"]["availableDecisions"] = 123
        with self.assertRaisesRegex(ValueError, "Guarded optional field"):
            check_inbound_contract(approval, bad, self.schema_for(approval))

    def test_pruned_recursive_schema_preserves_fields_named_title_and_description(self):
        from scripts.check_protocol import pinned_schema, schema_subset
        schema = {"type": "object", "description": "annotation", "additionalProperties": False,
                  "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                                 "child": {"$ref": "#/definitions/Node"},
                                 "definitions": {"type": "string"}, "$ref": {"type": "string"},
                                 "data": {"const": {"description": "data", "$ref": "#/definitions/not-a-schema"}}},
                  "required": ["title", "description"],
                  "definitions": {"Node": {"type": "object", "additionalProperties": False,
                      "properties": {"title": {"type": "string"}, "child": {"$ref": "#/definitions/Node"}},
                      "required": ["title"]}}}
        key = "inbound:notification:fixture"
        subset = schema_subset(SimpleNamespace(wire_schema=lambda _: schema), {key}, "codex-cli 0.167.0")
        pruned = pinned_schema(subset, key)
        self.assertNotIn("description", subset["schemas"][key])
        self.assertIn("description", subset["schemas"][key]["properties"])
        for message in ({"title": "root", "description": "text", "child": {"title": "child"}},
                        {"title": "root", "description": "text", "definitions": "data", "$ref": "text",
                         "data": {"description": "data", "$ref": "#/definitions/not-a-schema"}},
                        {"title": "root", "description": "text", "data": {"description": "wrong"}},
                        {"title": "root", "description": 123},
                        {"title": "root", "description": "text", "child": {}},
                        {"title": "root", "description": "text", "extra": True}):
            self.assertEqual(jsonschema.Draft7Validator(schema).is_valid(message),
                             jsonschema.Draft7Validator(pruned).is_valid(message))

    def test_unrecorded_contract_shapes_are_reported_explicitly(self):
        from scripts.check_protocol import report_unrecorded
        output = []
        report_unrecorded(self.fixture, output=output.append)
        self.assertTrue(any("NOT RECORDED optional notification thread/tokenUsage/updated" in line for line in output))
        self.assertTrue(any("NOT RECORDED required notification error" in line for line in output))
        self.assertTrue(all("no representative offline flow emitted this shape" in line for line in output))
