"""What a caller can learn from the published tool catalog alone."""

import hashlib
import json
import unittest

from codex_local_mcp import plans
from codex_local_mcp.contract import MCP_MODERN_VERSION, MCP_VERSIONS
from codex_local_mcp.mcp_modern import META_CAPABILITIES, META_VERSION
from codex_local_mcp.tools import request_detail, tool_catalog
from tests.mcp_wire import RecordingMCP


def catalogs():
    for version in MCP_VERSIONS:
        yield version, {tool["name"]: tool for tool in tool_catalog(version)}


def example_objects(description):
    decoder = json.JSONDecoder()
    while (start := description.find("{")) != -1:
        value, end = decoder.raw_decode(description[start:])
        yield value
        description = description[start + end:]


class CatalogSemanticsTest(unittest.TestCase):
    def test_request_inspection_and_human_review(self):
        for version, catalog in catalogs():
            with self.subTest(version=version):
                tool = catalog["codex_review_request"]
                text = tool["description"].lower()
                props = tool["inputSchema"]["properties"]
                self.assertRegex(text, r"\bwith offset\b[^.]*\b0\b[^.]*read[^.]*(without|no) [^.]*prompt")
                self.assertRegex(text, r"(omit|without) offset[^.]*form")
                self.assertRegex(text, r"relay[^.]*decision")
                self.assertRegex(text, r"without [^.]*form[^.]*pending")
                self.assertIn("server.py review REQUEST_ID", tool["description"])
                self.assertRegex(text, r"accept\w*[^.]*complete content")
                self.assertRegex(text, r"next_offset[^.]*null")
                self.assertNotRegex(text, r"(until|wait for)\s+detail_complete")
                for term in ("client", "human", "assembled", "detail_total_chars", "detail_sha256"):
                    self.assertIn(term, text)
                for source in ("pending", "codex_requests", "codex_events", "codex_status"):
                    self.assertIn(source, props["request_id"]["description"].lower())
                self.assertIn("request_id", props["pending_request_id"]["description"])

    def test_multiple_request_pages_verify_as_one_text(self):
        request = {"id": "pending", "kind": "parent_question", "task_id": "task",
                   "params": {"question": "Which option? 雪 " * 40}, "status": "pending"}
        pages, offset = [], 0
        while offset is not None:
            page = request_detail(request, offset=offset, limit=128)
            self.assertEqual(page["detail_offset"], offset)
            self.assertFalse(page["detail_complete"])
            pages.append(page)
            offset = page["next_offset"]
            if offset is not None:
                self.assertTrue(page["detail"])
                self.assertEqual(offset, page["detail_offset"] + len(page["detail"]))
                self.assertLess(offset, page["detail_total_chars"])
        self.assertGreater(len(pages), 1)
        assembled = "".join(page["detail"] for page in pages)
        digest = hashlib.sha256(assembled.encode("utf-8")).hexdigest()
        for page in pages:
            self.assertEqual(len(assembled), page["detail_total_chars"])
            self.assertEqual(digest, page["detail_sha256"])
        self.assertEqual(json.loads(assembled), request)

    def test_goal_consequences_budgets_and_authority(self):
        for version, catalog in catalogs():
            with self.subTest(version=version):
                text = catalog["codex_goal"]["description"].lower()
                self.assertRegex(text, r"\bget\b[^.]*\bread\w*")
                self.assertRegex(text, r"\bclear\b[^.]*\bremove\w*")
                for field in ("objective", "status", "token_budget"):
                    self.assertRegex(text, rf"\bset\b[^.]*\b{field}\b")
                self.assertRegex(text, r"active goals?[^.]*work[^.]*usage")
                self.assertRegex(text, r"native[^.]*token_budget[^.]*separate[^.]*family budget")
                for term in ("codex_settings", "explicit human", "user_requested=true", "operation key"):
                    self.assertIn(term, text)

    def test_plans_can_be_built_from_the_catalog(self):
        for version, catalog in catalogs():
            with self.subTest(version=version):
                tool = catalog["codex_plans"]
                text = tool["description"].lower()
                self.assertRegex(text, r"submit[^.]*start[^.]*work[^.]*admission")
                self.assertRegex(text, r"cancel[^.]*stop[^.]*new")
                self.assertRegex(text, r"running[^.]*interrupt|interrupt[^.]*running")
                spec = tool["inputSchema"]["properties"]["spec"]["description"]
                for term in ("1-16", "max_parallel", "1-4", "default 2", "absolute workdir"):
                    self.assertIn(term, spec)
                self.assertRegex(spec, r"after[^.]*dependenc")
                self.assertRegex(spec, r"review[^.]*omit prompt")
                self.assertRegex(spec, r"send/review[^.]*start ancestor")

                objects = list(example_objects(spec))
                example = next(obj for obj in objects if "nodes" in obj)
                start, = example["nodes"]
                single = plans.validate(example)
                self.assertEqual(single["nodes"][0]["action"], "start")
                self.assertEqual(single["max_parallel"], 2)

                target = next(obj for obj in objects if "node" in obj)
                review = {"id": "check", "action": "review", "target": target, "after": [start["id"]]}
                dependent = plans.validate({**example, "nodes": [start, review]})["nodes"][1]
                self.assertEqual(dependent["target"], {"node": start["id"]})
                self.assertEqual(dependent["after"], [start["id"]])

    def test_get_selectors_units_and_paging(self):
        for version, catalog in catalogs():
            with self.subTest(version=version):
                props = catalog["codex_get"]["inputSchema"]["properties"]
                view = props["view"]["description"].lower()
                self.assertRegex(view, r"default[^.]*detail[^.]*compact[^.]*summary")
                self.assertRegex(view, r"reconciliation[^.]*recovery report")
                self.assertRegex(view, r"permissions[^.]*receipts[^.]*scope")
                for field in ("requests_cursor", "subagents_offset", "subagents_limit", "claims_offset", "claims_limit"):
                    self.assertIn(field, props)
                    self.assertIn(field, view)
                units = " ".join(props[field].get("description", "") for field in ("view", "offset", "limit")).lower()
                self.assertRegex(units, r"\boffset\b[^.]*\blimit\b")
                self.assertRegex(units, r"artifact[^.]*bytes|bytes[^.]*artifact")
                self.assertRegex(units, r"result[^.]*permissions[^.]*records|records[^.]*result[^.]*permissions")
                turn = props["turn_id"]["description"].lower()
                for term in ("result", "turn", "evidence", "default", "current"):
                    self.assertIn(term, turn)
                self.assertRegex(props["result_id"]["description"], r"pin[^.]*revision[^.]*pages")
                self.assertRegex(props["artifact_id"]["description"].lower(), r"required[^.]*artifact")

    def test_event_fragment_arguments_and_outcome_cursors(self):
        for version, catalog in catalogs():
            with self.subTest(version=version):
                tool = catalog["codex_events"]
                text = tool["description"].lower()
                self.assertRegex(text, r"replay[^.]*\bafter\b")
                self.assertRegex(text, r"keep[^.]*\bafter\b[^.]*\bevent_seq\b")
                self.assertRegex(text, r"\bnext_event_offset\b[^.]*\bevent_offset\b[^.]*complete")
                view = tool["inputSchema"]["properties"]["view"]["description"].lower()
                self.assertRegex(view, r"outcome[^.]*coalesc[^.]*state[^.]*task_ids")
                self.assertRegex(view, r"opaque[^.]*cursors[^.]*unchanged")

    def test_rich_inputs_steering_and_queue_updates(self):
        for version, catalog in catalogs():
            with self.subTest(version=version):
                for name in ("codex_start", "codex_send", "codex_queue"):
                    props = catalog[name]["inputSchema"]["properties"]
                    for field, terms in (
                        ("images", ("project-relative", "png", "jpeg", "webp", "gif", "paths")),
                        ("context_files", ("project-relative", "utf-8", "files", "context")),
                        ("skills", ("enabled", "names", "codex_skills")),
                    ):
                        for term in terms:
                            self.assertIn(term, props[field]["description"].lower(), (name, field))
                    if name == "codex_queue":
                        self.assertNotIn("output_schema", props)
                    else:
                        for term in ("json schema", "final answer"):
                            self.assertIn(term, props["output_schema"]["description"].lower())
                access = catalog["codex_start"]["inputSchema"]["properties"]["access"]
                self.assertIn("project", access["description"].lower())
                self.assertEqual(set(access["enum"]), {"read", "write"})
                self.assertEqual(access["default"], "write")

                send = catalog["codex_send"]["description"].lower()
                self.assertRegex(send, r"follow_up[^.]*starts?[^.]*turn")
                self.assertRegex(send, r"steer[^.]*input[^.]*active turn[^.]*expected_turn_id")
                options = next(sentence for sentence in send.split(".") if "output_schema" in sentence)
                for term in ("model", "effort"):
                    self.assertIn(term, options)
                self.assertRegex(options, r"only.*follow_up|follow_up.*only|steer.*reject|reject.*steer")
                queue = catalog["codex_queue"]["description"].lower()
                self.assertRegex(queue, r"add[^.]*immediately")
                self.assertRegex(queue, r"update[^.]*prompt")
                self.assertRegex(queue, r"update[^.]*queued_id")
                self.assertRegex(queue, r"start[^.]*conversation[^.]*settings")


async def served_catalogs():
    for version in MCP_VERSIONS:
        adapter = RecordingMCP()
        params = {}
        if version == MCP_MODERN_VERSION:
            params["_meta"] = {META_VERSION: version, META_CAPABILITIES: {}}
        else:
            adapter.version, adapter.ready = version, True
        await adapter.handle({"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": params})
        tools = adapter.frames[-1]["message"]["result"]["tools"]
        yield version, {tool["name"]: tool for tool in tools}


class ServedCatalogNotesTest(unittest.IsolatedAsyncioTestCase):
    async def test_identifiers_selectors_and_operation_keys_are_described(self):
        fields = {"task_id", "task_ids", "workdir", "project_id", "repo", "request_id", "operation_id"}
        async for version, catalog in served_catalogs():
            for name, tool in catalog.items():
                properties = tool["inputSchema"]["properties"]
                for field in fields.intersection(properties):
                    with self.subTest(version=version, tool=name, field=field):
                        schema = properties[field]
                        if field == "task_ids":
                            schema = schema["items"]
                        description = schema.get("description")
                        self.assertIsInstance(description, str)
                        self.assertTrue(description.strip())

    async def test_action_scope_and_field_meanings_in_tools_list(self):
        notes = {
            ("codex_revert",): ("Trim native history", "Never reverts files", "Local event audit stays"),
            ("codex_revert", "before_turn_id"): ("codex_history's logical_turn_id", "this and all later turns"),
            ("codex_revert", "confirm_history_only"): ("Set true", "history only", "files intact"),
            ("codex_settings",): ("get reads settings", "current (running turn only, expected_turn_id)",
                                  "next (future turns) with a key", "model/effort/service_tier from codex_models",
                                  "next adds access/collaboration_mode/budget", "Pending approvals keep their reviewer"),
            ("codex_settings", "budget"): ("Root-only family lifetime caps apply now", "{} clears"),
            ("codex_projects",): ("list(offset/limit) shows registered projects", "select registers an existing directory",
                                  "create makes a plain one"),
            ("codex_projects", "path"): ("Main agent's absolute select/create directory",),
            ("codex_worktrees",): ("handoff(finished task_id) returns a patch artifact", "cleanup(key) removes the managed folder",
                                   "All but inspect need worktrees=true", "Never merges/pushes"),
            ("codex_worktrees", "project_id"): ("Instead of repo/path", "Needed for cleanup"),
            ("codex_worktrees", "plan_id"): ("Plan result for create", "key required"),
            ("codex_worktrees", "keep_branch"): ("Keep the branch cleanup would delete",),
            ("codex_sessions",): ("For this project, list native sessions including Codex CLI",
                                  "import a root thread_id as a bridge task", "Native scope checked before work"),
            ("codex_sessions", "confirm_stopped"): ("Import needs true", "stopped in all other Codex clients"),
            ("codex_terminals",): ("this task's native background processes", "terminate(process_id,key) stops one",
                                   "clean(key) stops all"),
            ("codex_delegate",): ("Guide a parent", "expected_turn_id if active", "may use native subagents",
                                  "Find children via codex_subagents"),
            ("codex_delegate", "contract"): ("Guidance, no authority", "version=1"),
            ("codex_respond",): ("Answer pending guidance or review requests", "Approvals need human/relay", "Denials need no human"),
            ("codex_respond", "proposal_hash"): ("Accept file_approval with the hash shown at review", "If redacted, human only"),
            ("codex_respond", "response"): ("Match request kind",),
            ("codex_respond", "response", "decision"): ("Approval string or ask_parent object",
                                                        "version,id,context_hash,option_id,rationale", "No form grants authority"),
            ("codex_history",): ("Page owned turns/items saved across outages",),
            ("codex_review",): ("native code review on an idle task", "Poll for findings"),
            ("codex_capabilities",): ("native collaboration, permissions and direct input support",),
            ("codex_skills",): ("List native project skills",),
            ("codex_status",): ("Codex version", "sign-in type", "broker health", "pending request summaries"),
        }
        async for version, catalog in served_catalogs():
            for path, phrases in notes.items():
                name, *fields = path
                with self.subTest(version=version, path=path):
                    node = catalog[name]
                    if fields:
                        node = node["inputSchema"]
                        for field in fields:
                            node = node["properties"][field]
                    for phrase in phrases:
                        self.assertIn(phrase, node["description"])
