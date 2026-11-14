import copy
import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from codex_local_mcp.contract import (NATIVE_METHODS, NATIVE_NOTIFICATIONS, NATIVE_SERVER_REQUESTS,
    REQUIRED, THREAD_SAFETY_FIELDS)
from tests.native_wire import capture_native_wire, normalise_frames
from tests.helpers import normalise_bridge_version, temp_root

try:
    import jsonschema
except ImportError:
    jsonschema = None

FIXTURES = Path(__file__).with_name("fixtures")


class WireInventoryTest(unittest.TestCase):
    def test_recorded_frames_cover_required_methods_and_report_unemitted_events(self):
        fixture = json.loads((FIXTURES / "native-wire.json").read_text(encoding="utf-8"))
        used = {frame["message"].get("method") for frame in fixture["messages"] if frame["direction"] == "outbound"}
        self.assertLessEqual({method for method, level in NATIVE_METHODS.items() if level == REQUIRED}, used)
        incoming = {frame["message"].get("method") for frame in fixture["messages"] if frame["direction"] == "inbound"}
        missing = {method for method, level in NATIVE_NOTIFICATIONS.items() if level == REQUIRED} - (used | incoming)
        self.assertEqual(missing, {"error", "item/fileChange/patchUpdated"})
        self.assertLessEqual({method for method, level in NATIVE_SERVER_REQUESTS.items() if level == REQUIRED}, incoming)
        self.assertEqual(fixture["codex_version"], "codex-cli 0.167.0")
        self.assertEqual(THREAD_SAFETY_FIELDS, ("activePermissionProfile", "cwd", "runtimeWorkspaceRoots", "approvalPolicy"))


class CaptureStdioTest(unittest.IsolatedAsyncioTestCase):
    def test_loaded_identity_lists_are_stable_after_normalisation(self):
        ids = ["10000000-0000-4000-8000-000000000001", "20000000-0000-4000-8000-000000000002"]
        frames = [{"flow": "fixture", "direction": "inbound", "message": {
            "method": "thread/started", "params": {"thread": {"id": identity}}}} for identity in ids]
        frames += [{"flow": "fixture", "direction": "outbound", "message": {
            "id": 5, "method": "thread/loaded/list", "params": {}}},
            {"flow": "fixture", "direction": "inbound", "message": {
                "id": 5, "result": {"data": ids, "nextCursor": None}}}]
        reordered = copy.deepcopy(frames)
        reordered[-1]["message"]["result"]["data"].reverse()
        self.assertEqual(normalise_frames(frames, Path("/unused")), normalise_frames(reordered, Path("/unused")))

    def test_normalisation_preserves_duplicate_mutating_calls(self):
        frames = [{"flow": "fixture", "direction": "outbound", "message": {
            "method": method, "params": {"threadId": "thread"}, "id": identifier}}
            for method in ("thread/read", "turn/interrupt") for identifier in (1, 2)]
        normalised = normalise_frames(frames, Path("/not-a-fixture-path"))
        methods = [frame["message"]["method"] for frame in normalised]
        self.assertEqual(methods.count("thread/read"), 1)
        self.assertEqual(methods.count("turn/interrupt"), 2)
        reordered = copy.deepcopy([*reversed(frames), *frames[:2]])
        for identifier, frame in enumerate(reordered, 1000):
            frame["message"]["id"] = identifier
        self.assertEqual(normalise_frames(reordered, Path("/not-a-fixture-path")), normalised)

    async def test_independent_captures_and_bridge_version_changes_are_byte_stable(self):
        from unittest.mock import patch
        from tests.mcp_wire import capture_mcp_wire
        from codex_local_mcp.contract import MCP_VERSIONS
        first = await capture_native_wire()
        with patch("codex_local_mcp.backend.__version__", "999.0.0"):
            second = await capture_native_wire()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        originals, updates = {}, {}
        for version in MCP_VERSIONS:
            original = await capture_mcp_wire(version)
            with patch("codex_local_mcp.mcp.__version__", "999.0.0"):
                updated = await capture_mcp_wire(version)
            self.assertEqual(original, updated)
            originals[version], updates[version] = original, updated
        if jsonschema is not None:
            from scripts.check_protocol import refresh_fixtures
            subset = json.loads((FIXTURES / "native-schema.json").read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(dir=temp_root()) as directory:
                refresh_fixtures(first, subset, originals, Path(directory), output=lambda _: None)
                output = []
                refresh_fixtures(second, subset, updates, Path(directory), output=output.append)
                self.assertEqual(len(output), len(MCP_VERSIONS) + 6)
                self.assertTrue(all(line.startswith("UNCHANGED ") for line in output), output)

    def test_version_normalisation_preserves_non_bridge_versions(self):
        value = {"clientInfo": {"name": "other-client", "version": "1.2.3"},
                 "data": {"version": "4.5.6"}, "serverInfo": {"name": "codex-local-mcp", "version": "0.2.0"}}
        result = normalise_bridge_version(value)
        self.assertEqual(result["clientInfo"], value["clientInfo"])
        self.assertEqual(result["data"], value["data"])
        self.assertEqual(result["serverInfo"]["version"], "<bridge-version>")

    async def test_capture_with_devnull_and_closed_parent_stdin(self):
        for closed in (False, True):
            program = ("import asyncio,json,os; " + ("os.close(0); " if closed else "")
                       + "from tests.native_wire import capture_native_wire; "
                       + "result=asyncio.run(capture_native_wire()); print(json.dumps({'frames':len(result['messages'])}))")
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", program,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(__file__).resolve().parent.parent))
            try:
                out, err = await asyncio.wait_for(process.communicate(), 30)
                self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
                self.assertGreater(json.loads(out)["frames"], 100)
            finally:
                if process.returncode is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 5)


@unittest.skipUnless(jsonschema, "Optional development dependency: jsonschema")
class NativeSchemaFixtureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from scripts.check_protocol import pinned_schema, validate_wire, wire_shapes
        self.schema = json.loads((FIXTURES / "native-schema.json").read_text(encoding="utf-8"))
        self.fixture = json.loads((FIXTURES / "native-wire.json").read_text(encoding="utf-8"))
        self.schema_for = lambda key: pinned_schema(self.schema, key)
        self.validate_wire, self.wire_shapes = validate_wire, wire_shapes

    def test_generated_subset_checks_raw_frames_without_padded_metadata(self):
        result = self.validate_wire(self.fixture, self.schema_for, "pinned", output=lambda _: None)
        self.assertFalse(result["failures"])
        self.assertEqual(len(result["unchecked"]), 1)
        self.assertIn("future/unknownApproval", result["unchecked"][0])
        self.assertEqual(result["fake_schema_omissions"], [])
        self.assertTrue(any(key.startswith("inbound:response:") for key, _ in self.wire_shapes(self.fixture)))
        self.assertTrue(any("availableDecisions (array" in value for value in result["fake_extensions"]))
        self.assertEqual(self.schema["codex_version"], "codex-cli 0.167.0")
        self.assertLess((FIXTURES / "native-schema.json").stat().st_size, 150_000)

    async def test_current_bridge_frames_match_and_validate_against_the_pinned_shapes(self):
        checked = []
        def validate_raw(key, message):
            if key == "inbound:request:future/unknownApproval":
                return
            jsonschema.Draft7Validator(self.schema_for(key)).validate(message)
            checked.append(key)
        current = await capture_native_wire(validate_raw)
        result = self.validate_wire(current, self.schema_for, "current", output=lambda _: None)
        self.assertFalse(result["failures"])
        self.assertEqual({key for key, _ in self.wire_shapes(current)},
                         {key for key, _ in self.wire_shapes(self.fixture)})
        self.assertGreater(len(checked), result["counts"]["inbound"])

    def test_missing_required_event_field_is_rejected(self):
        from scripts.check_protocol import check_inbound_contract
        key = "inbound:notification:turn/completed"
        message = copy.deepcopy(next(message for shape, message in self.wire_shapes(self.fixture) if shape == key))
        del message["params"]["threadId"]
        with self.assertRaisesRegex(ValueError, "lacks consumed field params.threadId"):
            check_inbound_contract(key, message, self.schema_for(key))

    def test_unknown_optional_event_field_is_accepted(self):
        from scripts.check_protocol import check_inbound_contract
        key = "inbound:notification:item/agentMessage/delta"
        message = copy.deepcopy(next(message for shape, message in self.wire_shapes(self.fixture) if shape == key))
        message["params"]["futureOptional"] = {"value": True}
        check_inbound_contract(key, message, self.schema_for(key))

    def test_existing_42_samples_are_retained(self):
        from scripts.check_protocol import representative_samples
        self.assertEqual(len(representative_samples()), 42)
