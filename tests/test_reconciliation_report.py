"""Recovery explains evidence without treating an ACK as completed work."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from codex_local_mcp.security import BridgeError
from codex_local_mcp import reconciliation
from codex_local_mcp.tools import dispatch
from tests.test_service import ServiceHarness


class ReconciliationReportTest(ServiceHarness):
    async def test_operation_summaries_do_not_decode_large_cached_results(self):
        task = await self.start_task("hold")
        payload = json.dumps({"turn_id": task["turn_id"], "summary": "x" * (2 * 1024 * 1024)})
        with self.store.transaction():
            for index in range(20):
                key = f"large-cache-{index}"
                self.store.db.execute("INSERT INTO operations VALUES (?, 'fp', 'done', ?, ?)", (key, payload, task["id"]))
                self.store.db.execute("INSERT INTO operation_details VALUES (?,?,'codex_send',1,'{}')", (key, task["id"]))
        with patch.object(reconciliation.json, "loads", wraps=json.loads) as decode:
            operations, more = reconciliation.operations(self.service, task["id"])
        self.assertLessEqual(sum(len(call.args[0].encode()) for call in decode.call_args_list), 256 * 1024)
        self.assertEqual(len(operations), 20)
        self.assertTrue(all(op["cached_result_omitted"] for op in operations))
        self.assertTrue(all(op["acknowledgement"] == "unknown" for op in operations))

    async def test_acknowledged_start_and_followup_observe_completed_turns_without_replaying(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await dispatch(self.service, "codex_send", {"task_id": task["id"], "prompt": "echo", "operation_id": "observed-followup"})
        await self.until(task["id"], ("completed",))
        result = await self.service.resume(task["id"])
        for key in ("start", "observed-followup"):
            operation = next(op for op in result["reconciliation"]["operations"] if op["operation_id"] == key)
            self.assertTrue(operation["effect_observed"])
            self.assertEqual(operation["effect_source"], "turn_record")
            self.assertEqual(operation["effect_turn_status"], "completed")
            self.assertFalse(operation["automatic_retry"])
        with self.store.transaction():
            self.store.db.execute("INSERT INTO operations VALUES ('unlinked','fp','done','{}',?)", (task["id"],))
        unknown = next(op for op in reconciliation.report(self.service, task["id"])["operations"] if op["operation_id"] == "unlinked")
        self.assertFalse(unknown["effect_observed"])
        self.assertEqual(unknown["effect_observation"], "not_established")

    async def test_small_ack_metadata_keeps_proven_effect_when_the_cached_result_is_omitted(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        with self.store.transaction():
            self.store.db.execute("UPDATE operations SET result=? WHERE id='start'", (json.dumps({"summary": "x" * (2 * 1024 * 1024)}),))
        with patch.object(reconciliation.json, "loads", wraps=json.loads) as decode:
            report = reconciliation.report(self.service, task["id"])
        operation = next(op for op in report["operations"] if op["operation_id"] == "start")
        self.assertTrue(operation["cached_result_omitted"])
        self.assertEqual(operation["acknowledgement"], "acknowledged")
        self.assertTrue(operation["effect_observed"])
        self.assertEqual(operation["effect_turn_status"], "completed")
        self.assertLessEqual(sum(len(call.args[0].encode()) for call in decode.call_args_list), 256 * 1024)

    async def test_inspection_is_local_and_has_safe_actions_only(self):
        task = await self.start_task("hold")
        before = self.store.db.total_changes
        with patch.object(self.service.backend, "call", AsyncMock(side_effect=AssertionError("Inspection must not call Codex"))):
            result = await dispatch(self.service, "codex_get", {"task_id": task["id"], "view": "reconciliation"})
        report = result["reconciliation"]
        self.assertEqual(self.store.db.total_changes, before)
        self.assertFalse(report["automatic_retry"])
        self.assertFalse(report["replay_permitted"])
        self.assertIn("native_state_not_checked", report["unknown"])
        self.assertEqual({step["tool"] for step in report["next_steps"]}, {"codex_history", "codex_resume"})

    async def test_resume_rebuilds_commands_files_and_diff_with_provenance(self):
        task = await self.start_task()
        done = await self.until(task["id"], ("completed",))
        self.service.lifecycle.project(task["id"], done["turn_id"], {"commands": [], "files": [], "diff": ""})
        recovered = await self.service.resume(task["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["commands"][0]["exitCode"], 0)
        self.assertIn("sample.py", recovered["files"])
        report = recovered["reconciliation"]
        self.assertTrue(report["rebuilt"])
        self.assertTrue(report["projection_sources"]["commands"])
        seq = report["projection_sources"]["commands"][0]["event_seq"]
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM events WHERE seq=?", (seq,)).fetchone())
        self.assertGreater(report["report_revision"], 0)
        self.assertTrue(self.service.get(task["id"], view="reconciliation")["reconciliation"]["checked_at"])

    async def test_lost_start_ack_stays_uncertain_after_observed_completion(self):
        self.service.projects("create", str(self.root / "a"))
        gate = self.gate_call("lost-start", "turn/start", error=TimeoutError("Lost start ACK"))
        starting = asyncio.create_task(dispatch(self.service, "codex_start", {"workdir": str(self.root / "a"), "prompt": "echo", "operation_id": "lost-start"}))
        await gate.wait()
        task = self.store.all("tasks")[0]
        await self.until(task["id"], ("completed",))
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await starting
        recovered = await self.service.resume(task["id"])
        op = next(o for o in recovered["reconciliation"]["operations"] if o["operation_id"] == "lost-start")
        self.assertEqual(op["acknowledgement"], "unknown")
        self.assertEqual(op["requested"]["arguments"]["prompt_preview"], "echo")
        self.assertEqual(recovered["reconciliation"]["native"]["latest_turn"]["status"], "completed")
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='lost-start'").fetchone()[0], "uncertain")
        with self.assertRaises(BridgeError):
            await dispatch(self.service, "codex_start", {"workdir": str(self.root / "a"), "prompt": "echo", "operation_id": "lost-start"})

    async def test_native_items_failure_preserves_journal_recovery_and_unknowns(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.service.lifecycle.project(task["id"], task["turn_id"], {"commands": [], "files": []})
        original = self.service.backend.call
        async def missing(method, params, **kw):
            if method == "thread/items/list":
                raise BridgeError("No item read", code="optional_native_capability_unavailable")
            return await original(method, params, **kw)
        with patch.object(self.service.backend, "call", missing):
            recovered = await self.service.resume(task["id"])
        self.assertTrue(recovered["commands"])
        self.assertIn("native_items_unavailable:optional_native_capability_unavailable", recovered["reconciliation"]["unknown"])
        self.assertFalse(recovered["evidence_complete"])

    async def test_concurrent_progress_is_not_overwritten_during_recovery(self):
        task = await self.start_task("hold")
        gate = self.gate_call("items", "thread/items/list")
        resuming = asyncio.create_task(self.service.resume(task["id"]))
        await gate.wait()
        await self.service.on_message({"method": "turn/plan/updated", "params": {"threadId": task["thread_id"],
            "turnId": task["turn_id"], "plan": [{"step": "New progress", "status": "inProgress"}]}})
        gate.release.set()
        recovered = await resuming
        self.assertEqual(recovered["plan"], [{"step": "New progress", "status": "inProgress"}])
        self.assertFalse(recovered["reconciliation"]["rebuilt"])
        self.assertIn("concurrent_update", recovered["reconciliation"]["unknown"])

    async def test_restart_expires_old_pending_approval_without_answering(self):
        task = await self.start_task("approval")
        waiting = await self.until(task["id"], ("waiting",))
        request = waiting["pending_requests"][0]
        await self.service.close()
        self.service = self.new_service()
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        self.assertGreaterEqual(report["expired_requests"], 1)
        with self.assertRaises(BridgeError):
            await self.service.respond(request["id"], {"decision": "accept"}, human=True)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 0)

    async def test_missing_callback_items_recover_from_native_history_without_fabricating_diff(self):
        original = self.service.backend.on_message
        dropped = []
        async def drop_completed(message):
            if message.get("method") in ("item/completed", "turn/diff/updated"):
                dropped.append(message)
                return
            await original(message)
        with patch.object(self.service.backend, "on_message", drop_completed):
            task = await self.start_task()
            await self.until(task["id"], ("completed",))
        self.assertTrue(dropped)
        self.assertFalse(self.service.get(task["id"])["commands"])
        restored = await self.service.resume(task["id"])
        self.assertEqual(restored["commands"][0]["exitCode"], 0)
        self.assertIn("sample.py", restored["files"])
        self.assertEqual(restored["diff"], "")
        sources = restored["reconciliation"]["projection_sources"]
        self.assertEqual(sources["commands"][0]["source"], "native_history")
        self.assertNotIn("diff", sources)
        self.assertIn("reconciliation_partial", restored["evidence_gaps"])
        count = self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='bridge/recovered_item'").fetchone()[0]
        await self.service.resume(task["id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='bridge/recovered_item'").fetchone()[0], count)
        await self.service.send(task["id"], "hold")
        self.assertEqual(self.service.get(task["id"])["projection_sources"], {})

    async def test_conflicting_native_exit_does_not_overwrite_observed_completion(self):
        task = await self.start_task()
        done = await self.until(task["id"], ("completed",))
        original = self.service.backend.call
        async def conflict(method, params, **kw):
            result = await original(method, params, **kw)
            if method == "thread/items/list":
                for entry in result["data"]:
                    item = entry.get("item", entry)
                    if item.get("type") == "commandExecution":
                        item["exitCode"] = 17
            return result
        with patch.object(self.service.backend, "call", conflict):
            restored = await self.service.resume(task["id"])
        self.assertEqual(restored["commands"], done["commands"])
        self.assertIn("journal_native_disagreement", restored["reconciliation"]["unknown"])
