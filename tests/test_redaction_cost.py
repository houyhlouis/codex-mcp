"""Keep the existing redaction policy while avoiding unnecessary large scans."""

import unittest
import re
import sys
from unittest.mock import Mock, patch

from codex_local_mcp import security


def reference(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if security._SECRET_KEY.match(key) else reference(item) for key, item in value.items()}
    if isinstance(value, list):
        return [reference(item) for item in value]
    if isinstance(value, str):
        for pattern in security._SECRETS:
            value = pattern.sub("[REDACTED]", value)
    return value


class RedactionCostTest(unittest.TestCase):
    def test_large_safe_ascii_diffs_need_no_secret_regex_passes(self):
        text = "unchanged context\n+" + "x" * (2 * 1024 * 1024)
        patterns = [Mock(wraps=pattern) for pattern in security._SECRETS]
        with patch.object(security, "_SECRETS", patterns):
            self.assertEqual(security.redact(text), text)
        self.assertFalse(any(pattern.sub.called for pattern in patterns))

    def test_large_safe_unicode_diffs_need_no_secret_regex_passes(self):
        text = "界\\\"\n" * 262144
        patterns = [Mock(wraps=pattern) for pattern in security._SECRETS]
        with patch.object(security, "_SECRETS", patterns):
            self.assertEqual(security.redact(text), text)
        self.assertFalse(any(pattern.sub.called for pattern in patterns))

    def test_prefix_fold_covers_every_unicode_ascii_case_equivalent(self):
        # ASCII regex literals with IGNORECASE also match a few Unicode letters.
        # Check the full character range so Python Unicode updates stay covered.
        letters = re.compile("[a-z]", re.I)
        for number in range(sys.maxunicode + 1):
            value = chr(number)
            if letters.fullmatch(value):
                folded = value.casefold().replace("\u0307", "").replace("\u0131", "i")
                self.assertEqual(len(folded), 1)
                self.assertIn(folded, "abcdefghijklmnopqrstuvwxyz")
                self.assertTrue(re.fullmatch(folded, value, re.I))

    def test_guards_preserve_every_existing_pattern_and_unicode_case(self):
        self.assertEqual(len(security._SECRETS), 4, "Review guards and samples when adding a pattern")
        cases = ["sk-abcdefghijklmnop", "Bearer abc123._-", "eyJabcdefghijklmnop.token.signature",
            "api_key=private", "access_token: private", "refresh_token=private", "password=private",
            "client_secret: private", "PASSWORD=private", "BeArEr\tprivate", "xsk-abcdefghijklmnop",
            "ap\u0131_key=private", "ap\u0130_key=private", "pa\u017f\u017fword=private", "api_\u212aey=private",
            "界 password=private\n+other", "prefix Bearer\u2003private", '"password":"[REDACTED]"',
            "no secret syntax here", "", "[REDACTED]"]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(security.redact(value), reference(value))
                self.assertEqual(security.redact({"payload": [value], "PASSWORD": value}), reference({"payload": [value], "PASSWORD": value}))

    def test_long_safe_content_around_a_secret_still_redacts_it(self):
        prefix, suffix = "x" * 65536, "界" * 65536
        for token in ("sk-abcdefghijklmnop", "Bearer private", "eyJabcdefghijklmnop.a.b", "client_secret=private"):
            value = prefix + " " + token + " " + suffix
            self.assertEqual(security.redact(value), reference(value))
            self.assertIn("[REDACTED]", security.redact(value))
