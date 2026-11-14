import unittest

from codex_local_mcp.contract import (CODEX_MIN_VERSION, CODEX_TESTED_VERSIONS, MCP_VERSIONS, MCP_LEGACY_VERSIONS, MCP_MODERN_VERSION,
    NATIVE_METHODS, NATIVE_NOTIFICATIONS, NATIVE_SERVER_REQUESTS, OPTIONAL, REQUIRED,
    codex_compatibility, method_unavailable, parse_codex_version)
from codex_local_mcp.mcp import VERSIONS
from codex_local_mcp.service import REQUEST_KINDS


class VersionContractTest(unittest.TestCase):
    def test_minimum_and_tested_versions(self):
        self.assertEqual(CODEX_MIN_VERSION, "0.167.0")
        self.assertEqual(CODEX_TESTED_VERSIONS, ("0.167.0",))
        for version in CODEX_TESTED_VERSIONS:
            self.assertEqual(codex_compatibility("codex-cli " + version)["status"], "tested")

    def test_prerelease_and_build_metadata_are_preserved(self):
        value = "codex-cli 0.167.0-alpha.4+build.007"
        parsed = parse_codex_version(value)
        self.assertEqual(parsed.release, (0, 167, 0))
        self.assertEqual(parsed.prerelease, ("alpha", "4"))
        self.assertEqual(parsed.build, ("build", "007"))
        record = codex_compatibility(value)
        self.assertEqual(record["version"], value)
        self.assertEqual(record["status"], "untested")
        self.assertIn("Prerelease", record["reason"])

    def test_minimum_prerelease_and_newer_versions_are_untested(self):
        for value in ("0.167.0-alpha.4", "0.167.1", "0.168.0", "1.0.0", "0.167.0+build"):
            with self.subTest(version=value):
                self.assertEqual(codex_compatibility(value)["status"], "untested")

    def test_lower_release_families_are_refused_including_prereleases(self):
        for value in ("0.166.999", "0.166.999-alpha.4", "0.1.0", "0.0.0"):
            with self.subTest(version=value):
                self.assertEqual(codex_compatibility(value)["status"], "unsupported")

    def test_garbage_and_invalid_semver_are_refused(self):
        for value in ("", None, 167, "garbage 0.167.0", "codex-cli 0.167.0 extra", "0.167", "00.167.0",
                      "0.167.00", "0.167.0-alpha.01", "0.167.0-", "0.167.0+", "0.167.0-a..b"):
            with self.subTest(version=value):
                with self.assertRaises(ValueError):
                    parse_codex_version(value)
                self.assertEqual(codex_compatibility(value)["status"], "unsupported")

    def test_inventory_and_legacy_mcp_revisions_match_the_adapter(self):
        self.assertIs(MCP_VERSIONS, VERSIONS)
        self.assertEqual(MCP_VERSIONS, (*MCP_LEGACY_VERSIONS, MCP_MODERN_VERSION))
        self.assertEqual(set(NATIVE_SERVER_REQUESTS), set(REQUEST_KINDS))
        for inventory in (NATIVE_METHODS, NATIVE_NOTIFICATIONS, NATIVE_SERVER_REQUESTS):
            self.assertTrue(inventory)
            self.assertLessEqual(set(inventory.values()), {REQUIRED, OPTIONAL})
        for method in ("thread/start", "thread/resume", "turn/start", "turn/interrupt", "config/read"):
            self.assertEqual(NATIVE_METHODS[method], REQUIRED)
        self.assertEqual(NATIVE_METHODS["permissionProfile/list"], OPTIONAL)

    def test_unavailability_detection_does_not_hide_bad_arguments(self):
        for error in ({"code": -32601, "message": "missing"},
                      {"code": -32000, "message": "Unsupported method thread/goal/get"},
                      {"code": -32000, "message": "This method is not supported by this build"},
                      {"code": -32602, "message": "Unsupported request"}):
            self.assertTrue(method_unavailable(error), error)
        for error in ({"code": -32602, "message": "Invalid params"},
                      {"code": -32000, "message": "Unsupported model"},
                      {"code": -32000, "message": "Request contains an unsupported effort"},
                      {"code": -32602, "message": "Unsupported request parameter effort"}, None, "invalid error"):
            self.assertFalse(method_unavailable(error), error)
