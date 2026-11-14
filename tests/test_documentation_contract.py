"""Offline checks for documented tool calls, names, counts and local Markdown links."""

import hashlib
import html
import json
import re
import tempfile
import unicodedata
import unittest
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import BY_NAME, ERROR_RESULT, RESULT_VERSION, TOOLS, arguments, validate
from tests.helpers import temp_root

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {
    "TASK": "10000000-0000-4000-8000-000000000001", "TASK_ID": "10000000-0000-4000-8000-000000000001",
    "PROJECT_ID": "10000000-0000-4000-8000-000000000002", "PENDING_ID": "10000000-0000-4000-8000-000000000003",
    "PENDING_REQUEST_ID": "10000000-0000-4000-8000-000000000003", "TURN": "10000000-0000-4000-8000-000000000004",
    "TURN_ID": "10000000-0000-4000-8000-000000000004", "ARTIFACT": "10000000-0000-4000-8000-000000000005",
    "ARTIFACT_ID": "10000000-0000-4000-8000-000000000005", "OPERATION_ID": "documentation-operation-1",
    "HASH_FROM_THE_REQUEST": "a" * 64, "PROPOSAL_HASH": "a" * 64,
}
# These identifiers are a configuration key and the Python package, not tools.
NON_TOOL_NAMES = {"codex_binary", "codex_local_mcp", "codex_reviewer"}  # setting, module, decision_source value
# (kind, repository-relative file, SHA-256 of the exact example/link/mention) -> reason.
# A marker is an assertion about an existing failure: fixed or removed text fails it.
KNOWN_ISSUES = {}
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)")


def blank(text):
    return re.sub(r"[^\n]", " ", text)


@dataclass(frozen=True)
class Issue:
    kind: str
    path: str
    line: int
    subject: str
    reason: str

    @property
    def key(self):
        return self.kind, self.path, hashlib.sha256(self.subject.encode()).hexdigest()

    def __str__(self):
        return f"{self.path}:{self.line}: {self.kind}: {self.reason}"


def unexpected_issues(issues, known):
    seen, failures = set(), []
    for issue in issues:
        if known.get(issue.key) == issue.reason:
            seen.add(issue.key)
        else:
            failures.append(str(issue))
    failures.extend(f"{key[1]}: stale known issue ({key[0]}, {key[2]}); remove its marker" for key in sorted(set(known) - seen))
    return failures


def markdown(source):
    """Mask comments/fences without moving line numbers; retain JSON fence bodies."""
    visible, blocks, issues = [], [], []
    fence, body, comment = None, [], False
    for line_number, original in enumerate(source.splitlines(keepends=True), 1):
        if fence:
            visible.append(blank(original))
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + r"{" + str(fence[1]) + r",}[ \t]*\n?", original):
                if fence[2] == "json":
                    blocks.append((fence[3], "".join(body)))
                fence, body = None, []
            else:
                body.append(original)
            continue
        line, start = "", 0
        while start < len(original):
            boundary = original.find("-->" if comment else "<!--", start)
            if boundary < 0:
                line += blank(original[start:]) if comment else original[start:]
                break
            end = boundary + (3 if comment else 4)
            line += blank(original[start:end]) if comment else original[start:boundary] + blank(original[boundary:end])
            comment, start = not comment, end
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})([^\n]*)\n?", line)
        if opening:
            info = opening[2].strip().split()
            fence = (opening[1][0], len(opening[1]), info[0].lower() if info else "", line_number)
            visible.append(blank(original))
        else:
            visible.append(line)
    if fence:
        issues.append((fence[3], "Unclosed Markdown code fence"))
    return "".join(visible), blocks, issues


def replace_placeholders(value):
    if isinstance(value, str):
        return PLACEHOLDERS.get(value, value)
    if isinstance(value, list):
        return [replace_placeholders(child) for child in value]
    if isinstance(value, dict):
        return {key: replace_placeholders(child) for key, child in value.items()}
    return value


def example_shape(value):
    """Explicit non-argument shapes; no general JSON-object exemption."""
    if isinstance(value, dict):
        if set(value) == {"mcpServers"} and isinstance(value["mcpServers"], dict) and value["mcpServers"]:
            for name, config in value["mcpServers"].items():
                if (not name or not isinstance(config, dict) or set(config) - {"command", "args", "env"}
                        or not isinstance(config.get("command"), str) or not config["command"]
                        or not isinstance(config.get("args"), list) or not all(isinstance(arg, str) for arg in config["args"])
                        or not isinstance(config.get("env", {}), dict)
                        or not all(isinstance(v, str) for v in config.get("env", {}).values())):
                    return None
            return "MCP client configuration"
        if type(value.get("result_version")) is int and value["result_version"] == RESULT_VERSION and type(value.get("ok")) is bool:
            if value["ok"] is False:
                try:
                    validate(value.get("error"), ERROR_RESULT, "error")
                    if value["error"]["code"] in BridgeError.CODES:
                        return "error envelope"
                except BridgeError:
                    pass
            else:
                for tool in TOOLS:
                    try:
                        validate(value, tool["outputSchema"], "result")
                        return "result envelope"
                    except BridgeError:
                        pass
    if value == "..." or value == {"...": "..."} or value == ["..."]:
        return "placeholder illustration"
    return None


def check_json(body):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON value {value}")

    try:
        value = json.loads(body, parse_constant=reject_constant)
    except ValueError as exc:
        return f"Invalid JSON: {exc}"
    value = replace_placeholders(value)
    if isinstance(value, dict):
        for tool in TOOLS:
            try:
                arguments(tool["name"], value)
                return None
            except BridgeError:
                pass
    if example_shape(value):
        return None
    return "JSON matches no tool input schema/argument rules and no recognised configuration, result, error or placeholder shape"


def unescape(text):
    return html.unescape(re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", text))


def balanced(text, start, opening, closing):
    depth, escaped = 0, False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def destination(text):
    text = text.strip()
    if text.startswith("<"):
        end = text.find(">")
        return unescape(text[1:end]) if end >= 0 else None
    depth = 0
    for index, char in enumerate(text):
        if char == "(" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif char == ")" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
        elif char.isspace() and depth == 0 and (index == 0 or text[index - 1] != "\\"):
            return unescape(text[:index])
    return unescape(text)


def markdown_links(visible):
    """Inline/image and full/collapsed/shortcut reference links, outside code."""
    visible = CODE_SPAN.sub(lambda match: blank(match[0]), visible)
    definitions, lines = {}, visible.splitlines(keepends=True)
    normalize = lambda label: " ".join(unescape(label).split()).casefold()
    for index, line in enumerate(lines):
        match = re.match(r" {0,3}\[([^]\n]+)\]:[ \t]*(.*)", line)
        if match:
            target = destination(match[2] or (lines[index + 1].strip() if index + 1 < len(lines) else ""))
            definitions.setdefault(normalize(match[1]), target)
            lines[index] = blank(line)
            yield index + 1, line.strip(), target
    text = "".join(lines)
    reference_tails = set()
    for index, char in enumerate(text):
        if index in reference_tails or char != "[" or (index and text[index - 1] == "\\"):
            continue
        end = balanced(text, index, "[", "]")
        if end is None:
            continue
        label, tail = text[index + 1:end], end + 1
        line = text.count("\n", 0, index) + 1
        if tail < len(text) and text[tail] == "(":
            close = balanced(text, tail, "(", ")")
            if close is not None:
                yield line, text[index:close + 1], destination(text[tail + 1:close])
        elif tail < len(text) and text[tail] == "[":
            close = balanced(text, tail, "[", "]")
            if close is not None:
                reference_tails.update(range(tail, close + 1))
                key = normalize(text[tail + 1:close] or label)
                yield line, text[index:close + 1], definitions.get(key)
        elif normalize(label) in definitions:
            yield line, text[index:end + 1], definitions[normalize(label)]


def heading_text(text):
    text = re.sub(r"!?\[([^]]*)\]\([^\n]*?\)", r"\1", text)
    text = CODE_SPAN.sub(lambda match: match[2].replace("\n", " "), text)
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>", "", text)
    text = re.sub(r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)(.+?)\1", r"\2", text)
    return unescape(text)


def heading_anchors(visible):
    used, counts, previous = set(), {}, ""
    for line in visible.splitlines():
        match = re.match(r" {0,3}#{1,6}(?:[ \t]+(.*?)|[ \t]*)$", line)
        title = None
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match[1] or "").strip()
        elif previous.strip() and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            title = previous.strip()
        if title is not None:
            text = heading_text(title).lower()
            base = "".join(char for char in text if char in "-_" or char.isspace() or unicodedata.category(char)[0] in "LNM")
            base = re.sub(r"\s", "-", base)
            slug = base
            while slug in used:
                counts[base] = counts.get(base, 0) + 1
                slug = f"{base}-{counts[base]}"
            used.add(slug)
        if (title is not None or not line.strip() or re.match(r"(?: {4}|\t| {0,3}(?:>|[-+*] |\d+[.)] |\[[^]]+\]:))", line)
                or re.fullmatch(r" {0,3}(?:[-*_][ \t]*){3,}", line)):
            previous = ""
        else:
            previous = (previous + " " + line.strip()).strip()
    return used


def documentation_paths(root):
    paths = {root / "README.md", root / "README.zh-CN.md"}
    paths.update(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in {".md", ".markdown"})
    paths.update(path for folder in ("docs", "examples") for path in (root / folder).rglob("*")
                 if path.is_file() and path.suffix.lower() in {".md", ".markdown"})
    return sorted(paths)


def documentation_issues(root):
    root = root.resolve()
    issues, anchor_cache, count_mentions = [], {}, 0
    for path in documentation_paths(root):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        visible, blocks, fence_errors = markdown(source)
        for line, reason in fence_errors:
            issues.append(Issue("fence", relative, line, source.splitlines()[line - 1], reason))
        for line, body in blocks:
            reason = check_json(body)
            if reason:
                issues.append(Issue("json", relative, line, body, reason))
        for span in CODE_SPAN.finditer(visible):
            for name in re.findall(r"\bcodex_[A-Za-z0-9_]+\b", span[2]):
                if name not in BY_NAME and name not in NON_TOOL_NAMES:
                    line = visible.count("\n", 0, span.start()) + 1
                    issues.append(Issue("tool", relative, line, name, f"Unknown tool `{name}`"))
        prose = CODE_SPAN.sub(lambda match: blank(match[0]), visible)
        for mention in re.finditer(r"(?<!\d)(\d+)\s+(?:MCP\s+)?tools\b|(?<!\d)(\d+)\s*(?:个|個)\s*工具", prose):
            count_mentions += 1
            count = int(mention[1] or mention[2])
            if count != len(TOOLS):
                issues.append(Issue("count", relative, prose.count("\n", 0, mention.start()) + 1,
                    mention[0], f"Documented tool count {count} differs from catalog size {len(TOOLS)}"))
        for line, subject, target in markdown_links(visible):
            reason = None
            if target is None:
                reason = "Undefined Markdown reference or invalid link destination"
            else:
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue  # Never fetch external links (including mailto:).
                local = unquote(parsed.path)
                resolved = (root / local.lstrip("/") if local.startswith("/") else path.parent / local).resolve() if local else path
                if root not in resolved.parents:
                    reason = f"Relative link leaves repository: {target}"
                elif not resolved.is_file():
                    reason = f"Relative link target is not an existing file: {target}"
                elif parsed.fragment:
                    if resolved.suffix.lower() not in {".md", ".markdown"}:
                        reason = f"Heading anchor targets a non-Markdown file: {target}"
                    else:
                        if resolved not in anchor_cache:
                            anchor_cache[resolved] = heading_anchors(markdown(resolved.read_text(encoding="utf-8"))[0])
                        if unquote(parsed.fragment) not in anchor_cache[resolved]:
                            reason = f"No GitHub heading anchor for {target}"
            if reason:
                issues.append(Issue("link", relative, line, subject, reason))
    if not count_mentions:
        issues.append(Issue("count", "README.md", 1, "missing tool count", "No documented tool count was checked"))
    return issues


class DocumentationContractTest(unittest.TestCase):
    def test_all_documentation_examples_names_counts_and_links(self):
        failures = unexpected_issues(documentation_issues(ROOT), KNOWN_ISSUES)
        self.assertFalse(failures, "\n".join(failures))

    def test_argument_examples_use_server_validation_and_specific_placeholders(self):
        for value in (
            {"task_id": "TASK", "view": "artifact", "artifact_id": "ARTIFACT", "offset": 0},
            {"task_id": "TASK_ID", "prompt": "Continue", "operation_id": "OPERATION_ID"},
            {"pending_request_id": "PENDING_ID", "response": {"decision": "decline"}},
        ):
            with self.subTest(value=value):
                self.assertIsNone(check_json(json.dumps(value)))
        for value in (
            {"task_id": "TASK", "unknown_argument": True},
            {"task_id": "TASK", "view": "artifact"},  # Schema permits it; arguments() requires the artifact id.
            {"task_id": "TASK", "mode": "NOT_A_REAL_ENUM", "prompt": "Continue", "request_id": "op"},
            {"action": "create"}, {"task_id": "TASK", "wait_seconds": -1},
        ):
            with self.subTest(value=value):
                self.assertIsNotNone(check_json(json.dumps(value)))
        self.assertIsNotNone(check_json('{"wait_seconds": NaN}'))
        self.assertEqual(replace_placeholders("NOT_A_REAL_ENUM"), "NOT_A_REAL_ENUM")

    def test_non_argument_shapes_are_explicit_and_checked(self):
        valid = (
            {"mcpServers": {"codex": {"command": "py", "args": ["-3", "server.py", "serve"]}}},
            {"result_version": 1, "ok": True, "projects": []},
            {"result_version": 1, "ok": False, "error": {"code": "stale_turn", "message": "Refresh", "retryable": False,
                "uncertain": False, "next_steps": ["Refresh"], "ids": {"task_id": "TASK"}}},
            "...", {"...": "..."}, ["..."],
        )
        for value in valid:
            with self.subTest(value=value):
                self.assertIsNone(check_json(json.dumps(value)))
        for value in ({"something_else": {}}, {"mcpServers": {}}, {"mcpServers": {"codex": {"command": "py", "args": "serve"}}},
                {"result_version": 1, "ok": False, "error": {}}, {"result_version": 99, "ok": True}, {"example": "..."}):
            with self.subTest(value=value):
                self.assertIsNotNone(check_json(json.dumps(value)))

    def test_fences_comments_and_inline_code_do_not_create_links(self):
        source = '<!-- [bad](absent) -->\n```text\n[bad](absent)\n```\n   ```json\n{"task_id":"TASK"}\n   ```\n`[bad](absent)`\n[good](README.md)\n'
        visible, blocks, errors = markdown(source)
        self.assertEqual(errors, [])
        self.assertEqual(blocks, [(5, '{"task_id":"TASK"}\n')])
        self.assertEqual(list(markdown_links(visible)), [(9, "[good](README.md)", "README.md")])
        self.assertEqual(markdown("```json\n{}")[2], [(1, "Unclosed Markdown code fence")])

    def test_inline_reference_image_and_escaped_destinations(self):
        source = ('[inline](docs/a(b).md "title") ![image](<images/a b.png>)\n'
            '[full][Target] [target][] [Target] [undefined][missing]\n'
            '[target]: docs/example.md#heading "title"\n'
            r'[escaped](docs/a\(b\).md)' + '\n')
        links = list(markdown_links(source))
        self.assertEqual([target for _, _, target in links], ["docs/example.md#heading", "docs/a(b).md", "images/a b.png", "docs/example.md#heading",
            "docs/example.md#heading", "docs/example.md#heading", None, "docs/a(b).md"])

    def test_github_heading_rules_chinese_markup_and_duplicate_collisions(self):
        source = ('# Hello, **World**!\n# Hello, World!\n# Hello World-1\n'
            '# 以 Python 包安装（可选）\n## `request_id` and *forms*\n'
            'Setext &amp; <em>HTML</em>\n---\n# [Linked title](other.md)\n'
            '# Repeated\n# Repeated\n# Repeated-1\n# Repeated\n'
            '```md\n# hidden\n```\n')
        anchors = heading_anchors(markdown(source)[0])
        self.assertEqual(anchors, {"hello-world", "hello-world-1", "hello-world-1-1", "以-python-包安装可选",
            "request_id-and-forms", "setext--html", "linked-title", "repeated", "repeated-1", "repeated-1-1", "repeated-2"})
        self.assertEqual(heading_anchors("## Heading\n---\n## Heading\n\nMulti\nline\n===\n"),
            {"heading", "heading-1", "multi-line"})

    def test_known_issues_must_match_actual_failures_and_fail_when_fixed(self):
        problem = Issue("json", "docs/example.md", 12, '{"bad":true}', "Invalid example")
        marker = {problem.key: problem.reason}
        self.assertEqual(unexpected_issues([problem], marker), [])
        self.assertIn("stale known issue", unexpected_issues([], marker)[0])
        moved = Issue(problem.kind, problem.path, 99, problem.subject, problem.reason)
        self.assertEqual(unexpected_issues([moved], marker), [])
        different = Issue("json", problem.path, 12, '{"different":true}', problem.reason)
        self.assertEqual(len(unexpected_issues([different], marker)), 2)

    def test_dynamic_documents_targets_and_anchor_failures(self):
        with tempfile.TemporaryDirectory(dir=temp_root()) as temp:
            root = Path(temp)
            (root / "docs").mkdir()
            (root / "examples").mkdir()
            (root / "README.md").write_text(f'# Root\n{len(TOOLS)} tools\n[ok](docs/new.md#中文标题)\n[bad](#missing)\n[absent](none.md)\n', encoding="utf-8")
            (root / "README.zh-CN.md").write_text("# 中文\n[ok](README.md#root)\n", encoding="utf-8")
            (root / "docs" / "new.md").write_text('# 中文标题\n## Repeat\n## Repeat\n[ok](#repeat-1)\n[external](https://invalid.example/#absent)\n'
                '[encoded](#%E4%B8%AD%E6%96%87%E6%A0%87%E9%A2%98)\n[unused]: missing.md\n', encoding="utf-8")
            (root / "examples" / "new.markdown").write_text('```json\n{"task_id":"TASK","view":"result"}\n```\n`codex_typo`\n'
                '```json\n{"not_an_argument":true}\n```\n999 tools\n', encoding="utf-8")
            problems = documentation_issues(root)
            self.assertEqual([(issue.kind, issue.path, issue.line) for issue in problems], [
                ("link", "README.md", 4), ("link", "README.md", 5), ("link", "docs/new.md", 7),
                ("json", "examples/new.markdown", 5), ("tool", "examples/new.markdown", 4), ("count", "examples/new.markdown", 8)])

    def test_root_markdown_checks_examples_tools_links_and_anchors(self):
        with tempfile.TemporaryDirectory(dir=temp_root()) as temp:
            root = Path(temp)
            (root / "README.md").write_text(f"# Root\n{len(TOOLS)} tools\n", encoding="utf-8")
            (root / "README.zh-CN.md").write_text("# 中文\n", encoding="utf-8")
            (root / "CONTRIBUTING.md").write_text('```json\n{"not_an_argument":true}\n```\n`codex_not_a_tool`\n', encoding="utf-8")
            (root / "SECURITY.md").write_text("# Security\n[missing](absent.md)\n", encoding="utf-8")
            (root / "CHANGELOG.md").write_text("# Releases\n[missing](SECURITY.md#not-a-heading)\n[ok](README.zh-CN.md#中文)\n", encoding="utf-8")
            issues = documentation_issues(root)
            self.assertEqual([(issue.kind, issue.path, issue.line) for issue in issues], [
                ("link", "CHANGELOG.md", 2), ("json", "CONTRIBUTING.md", 1),
                ("tool", "CONTRIBUTING.md", 4), ("link", "SECURITY.md", 2)])


if __name__ == "__main__":
    unittest.main()
