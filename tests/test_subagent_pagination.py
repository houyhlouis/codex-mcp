import json
import unittest
from unittest.mock import patch

from codex_local_mcp.contract import MCP_VERSIONS
from codex_local_mcp.tools import dispatch, result_size, tool_catalog
from tests.helpers import read_fake_history
from tests.test_service import ServiceHarness


class SubagentCatalogTest(unittest.TestCase):
    def test_catalog_explains_both_continuations(self):
        for version in MCP_VERSIONS:
            with self.subTest(version=version):
                tool = next(t for t in tool_catalog(version) if t["name"] == "codex_subagents")
                discovery, enumeration = tool["description"].split("refresh=false")
                for term in ("refresh=true", "nextCursor", "cursor", "null"):
                    self.assertIn(term, discovery)
                for term in ("offset=0", "next_offset", "null", "can_accept_direct_input", "parent"):
                    self.assertIn(term, enumeration)


class SubagentPaginationTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.parent = self.service.task(task["id"])

    def local_child(self, child_id, **fields):
        return self.service.lifecycle.create({**self.parent, "id": child_id, "thread_id": f"native-{child_id}",
            "parent_task_id": self.parent["id"], "root_task_id": self.parent["id"], "native_child": True,
            "status": "idle", "turn_id": None, **fields})

    async def seed_native_children(self, *thread_ids, archived=False):
        await self.service.backend.close()
        history = read_fake_history(self.state)
        parent_thread = self.parent["thread_id"]
        for thread_id in thread_ids:
            history[thread_id] = {"id": thread_id, "parent_id": parent_thread, "session_id": parent_thread,
                "options": history[parent_thread]["options"], "turns": [], "archived": archived, "direct": False}
        (self.state / "fake-history.json").write_text(json.dumps(history), encoding="utf-8")

    async def subagents(self, **arguments):
        return await dispatch(self.service, "codex_subagents", {"task_id": self.parent["id"], **arguments})

    async def follow(self, page):
        step, = page["next_steps"]
        self.assertEqual(step["tool"], "codex_subagents")
        return await dispatch(self.service, step["tool"], step["arguments"])

    async def test_discovery_snapshots_then_enumerate_known_and_new_children_once(self):
        # This id sorts before any generated UUID, so discovery repeats the local-only child.
        local_only = self.local_child("0-local-only")
        known = self.local_child("known")
        await self.seed_native_children(known["thread_id"], "native-new-a", "native-new-b")

        page = await self.subagents(limit=1)
        for cursor in ("1", "2", None):
            self.assertEqual(page["nextCursor"], cursor)
            self.assertEqual([c["id"] for c in page["subagents"]], [local_only["id"]])
            self.assertIn("local snapshot", page["note"])
            self.assertIn("repeat", page["note"])
            if cursor is not None:
                args = page["next_steps"][0]["arguments"]
                self.assertIs(args["refresh"], True)
                self.assertEqual(args["cursor"], cursor)
                page = await self.follow(page)

        self.assertEqual(page["next_offset"], 1)
        enumerated = []
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Enumeration called native Codex")):
            for offset in range(4):
                args = page["next_steps"][0]["arguments"]
                self.assertIs(args["refresh"], False)
                self.assertEqual(args["offset"], offset)
                page = await self.follow(page)
                self.assertIsNone(page["nextCursor"])
                self.assertEqual(page["next_offset"], offset + 1 if offset < 3 else None)
                child, = page["subagents"]
                enumerated.append((child["thread_id"], child["can_accept_direct_input"]))
        self.assertEqual(page["next_steps"], [])
        self.assertCountEqual(enumerated, [(local_only["thread_id"], True), (known["thread_id"], False),
                                           ("native-new-a", False), ("native-new-b", False)])

        summaries, offset = [], 0
        for expected_offset in (1, 2, 3, None):
            view = await dispatch(self.service, "codex_get", {"task_id": self.parent["id"],
                "subagents_offset": offset, "subagents_limit": 1})
            child, = view["subagents"]
            summaries.append((child["thread_id"], child["can_accept_direct_input"]))
            offset = view["subagents_next_offset"]
            self.assertEqual(offset, expected_offset)
        self.assertCountEqual(summaries, enumerated)

    async def test_archived_discovery_pages_enumerate_only_locally_verified_archives(self):
        first = self.local_child("archived-a", archived=True)
        last = self.local_child("archived-b", archived=True)
        self.local_child("active")
        await self.seed_native_children(first["thread_id"], "unverified-archive", archived=True)

        page = await self.subagents(archived=True, limit=1, offset=1)
        self.assertEqual(page["nextCursor"], "1")
        page = await self.follow(page)
        self.assertEqual([c["id"] for c in page["subagents"]], [last["id"]])
        self.assertIsNone(page["nextCursor"])
        self.assertIsNone(page["next_offset"])
        for expected in (first, last):
            page = await self.follow(page)
            child, = page["subagents"]
            self.assertEqual(child["id"], expected["id"])
            self.assertTrue(child["archived"])
        self.assertEqual(page["next_steps"], [])
        self.assertIsNone(self.service.task_for_thread("unverified-archive"))

    async def test_byte_limited_page_resumes_after_the_last_returned_child(self):
        self.local_child("0-before-offset")
        ids = [f"large-{i:02d}" for i in range(28)]
        summary = "x" * (120 * 1024)
        for child_id in ids:
            self.local_child(child_id, status="completed", turn_id=f"turn-{child_id}", summary=summary)

        first = await self.subagents(refresh=False, offset=1, limit=len(ids))
        returned = [c["id"] for c in first["subagents"]]
        self.assertGreater(len(returned), 0)
        self.assertLess(len(returned), len(ids))
        self.assertLessEqual(result_size({"subagents": first["subagents"]}), 6 * 1024 * 1024)
        self.assertEqual(first["next_offset"], 1 + len(returned))
        self.assertEqual(first["next_steps"][0]["arguments"]["offset"], first["next_offset"])
        last = await self.follow(first)
        self.assertEqual(returned + [c["id"] for c in last["subagents"]], ids)
        self.assertIsNone(last["next_offset"])
        self.assertEqual(last["next_steps"], [])

    async def test_empty_discovery_still_starts_enumeration_at_zero(self):
        page = await self.subagents()
        self.assertIsNone(page["nextCursor"])
        self.assertEqual(page["subagents"], [])
        args = page["next_steps"][0]["arguments"]
        self.assertIs(args["refresh"], False)
        self.assertEqual(args["offset"], 0)
        last = await self.follow(page)
        self.assertEqual(last["subagents"], [])
        self.assertIsNone(last["next_offset"])
        self.assertEqual(last["next_steps"], [])
