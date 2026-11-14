"""Guidance is versioned and scoped, while native approvals stay separate."""

import asyncio
import copy
import json
import time
from unittest.mock import patch

from codex_local_mcp import decisions
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from tests.helpers import read_fake_history
from tests.test_service import ServiceHarness


class DecisionTest(ServiceHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.clock = time.time()
        timer = patch("codex_local_mcp.decisions.time.time", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)

    def envelope(self, **fields):
        return {"version": 1, "id": "choose-target", "goal": "Choose a local target", "constraints": ["Keep the project boundary"],
            "deliverables": ["A focused change"], "verification": ["Run offline checks"],
            "options": [{"id": "a", "label": "First target"}, {"id": "b", "label": "Second target"}], **fields}

    async def pending(self, task_id):
        async def wait():
            async with self.service.changed:
                await self.service.changed.wait_for(lambda: self.store.request_count(task_id) > 0)
                return self.service.requests(task_id)
        return await asyncio.wait_for(wait(), 5)

    async def native_question(self, task, envelope=None, rpc_id=9970):
        arguments = {"question": "Which target?", "context": "Local context"}
        if envelope is not None:
            arguments["envelope"] = envelope
        await self.service.on_message({"id": rpc_id, "method": "item/tool/call", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "tool": "ask_parent", "callId": "native-call", "arguments": arguments}})
        rows = self.service.requests(task["id"])
        return rows[-1] if rows else None

    def response(self, request, **changes):
        record = request["decision_envelope"]
        return {"text": "Use the first target", "decision": {"version": 1, "id": record["id"],
            "context_hash": record["context_hash"], "option_id": "a", **changes}}

    async def test_typed_and_plain_replies_resume_the_exact_native_call(self):
        original = self.service.backend.on_message
        async def structured(message):
            if message.get("method") == "item/tool/call":
                message = copy.deepcopy(message)
                message["params"]["arguments"]["envelope"] = self.envelope(ttl_seconds=60)
            await original(message)
        with patch.object(self.service.backend, "on_message", structured):
            task = await self.start_task("parent")
            req = (await self.pending(task["id"]))[0]
            native_id = self.store.get("requests", req["id"])["rpc_id"]
            with patch.object(self.service.backend.process.stdin, "write", wraps=self.service.backend.process.stdin.write) as sent:
                result = await dispatch(self.service, "codex_respond", {"request_id": req["id"], "response": self.response(req, rationale="private-answer-rationale")})
        done = await self.until(task["id"], ("completed",))
        frames = [json.loads(call.args[0]) for call in sent.call_args_list]
        self.assertEqual([frame["id"] for frame in frames if "result" in frame], [native_id])
        history = read_fake_history(self.state)[task["thread_id"]]["turns"][-1]
        reply = history["fixture_response"]
        self.assertEqual(reply["contentItems"][0]["text"], "Use the first target")
        self.assertEqual(json.loads(reply["contentItems"][1]["text"])["authority"], "guidance_only")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(result["receipt"]["decision"], "answer")
        self.assertNotIn("private-answer-rationale", self.store.db.execute("SELECT data FROM decision_receipts").fetchone()[0])
        self.assertEqual(result["receipt"]["decision_id"], req["decision_envelope"]["id"])
        nxt = await self.service.send(task["id"], "hold")
        plain = await self.native_question(nxt)
        await self.service.respond(plain["id"], {"text": "Legacy text stays unchanged"})

    async def test_expired_typed_decision_refused_but_plain_reply_remains_possible(self):
        task = await self.start_task("hold")
        req = await self.native_question(task, self.envelope(ttl_seconds=60))
        self.assertEqual(req["decision_envelope"]["expires_at"], self.clock + 60)
        self.clock += 60
        summary = self.service.request_page(task["id"])["requests"][0]["decision_summary"]
        self.assertTrue(summary["expired"])
        self.assertFalse(summary["typed_response_allowed"])
        with self.assertRaises(BridgeError) as expired:
            await self.service.respond(req["id"], self.response(req))
        self.assertEqual(expired.exception.code, "decision_expired")
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")
        self.assertTrue(self.service.requests(task["id"])[0]["decision_envelope"]["expired"])
        await self.service.respond(req["id"], {"text": "The decision window passed. Ask a fresh question if needed."})

    async def test_expiry_while_waiting_for_native_write_lock_sends_nothing(self):
        task = await self.start_task("hold")
        req = await self.native_question(task, self.envelope(ttl_seconds=60))
        original = self.service.backend.respond
        reached = asyncio.Event()
        async def waiting(*args, **kw):
            reached.set()
            return await original(*args, **kw)
        await self.service.backend.write_lock.acquire()
        try:
            with patch.object(self.service.backend, "respond", waiting), \
                    patch.object(self.service.backend.process.stdin, "write", wraps=self.service.backend.process.stdin.write) as sent:
                replying = asyncio.create_task(self.service.respond(req["id"], self.response(req)))
                await asyncio.wait_for(reached.wait(), 5)
                self.clock += 61
                self.service.backend.write_lock.release()
                with self.assertRaises(BridgeError) as expired:
                    await asyncio.wait_for(replying, 5)
                self.assertEqual(expired.exception.code, "decision_expired")
                self.assertFalse(any("result" in json.loads(call.args[0]) for call in sent.call_args_list))
        finally:
            if self.service.backend.write_lock.locked():
                self.service.backend.write_lock.release()
        self.assertEqual(self.store.get("requests", req["id"])["status"], "pending")

    async def test_wrong_binding_option_and_cross_request_reuse_are_refused(self):
        task = await self.start_task("hold")
        first = await self.native_question(task, self.envelope(), rpc_id=9970)
        second = await self.native_question(task, self.envelope(), rpc_id=9971)
        self.assertNotEqual(first["decision_envelope"]["context_hash"], second["decision_envelope"]["context_hash"])
        for reply in (self.response(first, id="other"), self.response(first, context_hash="0" * 64),
                      self.response(first, option_id="not-offered"), self.response(second)):
            with self.subTest(reply=reply), self.assertRaises(BridgeError) as mismatch:
                await self.service.respond(first["id"], reply)
            self.assertEqual(mismatch.exception.code, "decision_mismatch")
        await self.service.respond(first["id"], self.response(first))
        with self.assertRaises(BridgeError) as duplicate:
            await self.service.respond(first["id"], self.response(first))
        self.assertEqual(duplicate.exception.code, "stale_request")

    async def test_invalid_native_envelope_returns_a_tool_failure_without_quarantine(self):
        task = await self.start_task("hold")
        for value in (self.envelope(version=2), self.envelope(ttl_seconds=59), self.envelope(ttl_seconds=604801),
                      self.envelope(expires_at=self.clock + 60), self.envelope(scope={"task_id": "other"}),
                      self.envelope(options=[{"id": "a", "label": "one"}, {"id": "a", "label": "two"}])):
            with self.subTest(value=value), patch.object(self.service.backend, "respond", wraps=self.service.backend.respond) as sent:
                req = await self.native_question(task, value)
                self.assertIsNone(req)
                result = sent.call_args.args[1]
                self.assertFalse(result["success"])
                self.assertEqual(json.loads(result["contentItems"][0]["text"])["code"], "invalid_arguments")
        self.assertNotIn(task["id"], self.service.protocol_quarantine)

    async def test_no_ttl_and_large_pending_summary_are_bounded_and_discoverable(self):
        task = await self.start_task("hold")
        req = await self.native_question(task, self.envelope(goal="goal " * 800))
        self.assertIsNone(req["decision_envelope"]["expires_at"])
        summary = self.service.request_page(task["id"])["requests"][0]
        self.assertTrue(summary["details_truncated"])
        self.assertEqual(summary["decision_summary"]["context_hash"], req["decision_envelope"]["context_hash"])
        page = await self.service.events(task_id=task["id"], view="outcome")
        self.assertEqual(page["pending_requests"][0]["decision_summary"]["id"], "choose-target")
        await self.service.respond(req["id"], self.response(req))

    async def test_large_question_context_is_bound_without_a_second_copy(self):
        task = await self.start_task("hold")
        await self.service.on_message({"id": 9970, "method": "item/tool/call", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "tool": "ask_parent", "callId": "large-context",
            "arguments": {"question": "Which target?", "context": "large context " * 4000, "envelope": self.envelope()}}})
        req = self.service.requests(task["id"])[0]
        record = req["decision_envelope"]
        self.assertEqual(len(record["question_sha256"]), 64)
        self.assertEqual(len(record["context_sha256"]), 64)
        self.assertNotIn("large context", json.dumps(record))
        self.assertLess(len(decisions.canonical(record)), 2048)
        await self.service.respond(req["id"], self.response(req))

    async def test_nested_family_question_is_visible_and_cannot_answer_an_approval(self):
        parent = await self.start_task("native_grandchildren")
        approval = (await self.pending(parent["id"]))[0]
        child = self.service.task(approval["task_id"])
        question = await self.native_question(child, self.envelope())
        root_pending = self.service.request_page(parent["id"])["requests"]
        self.assertIn(question["id"], {r["id"] for r in root_pending})
        self.assertEqual(question["decision_envelope"]["scope"]["root_task_id"], parent["id"])
        with self.assertRaises(BridgeError) as wrong_kind:
            await self.service.respond(approval["id"], self.response(question))
        self.assertEqual(wrong_kind.exception.code, "invalid_response")
        await self.service.respond(question["id"], {"text": "Approval-like guidance is still only context"})
        with self.assertRaises(BridgeError) as human:
            await self.service.respond(approval["id"], {"decision": "accept"})
        self.assertEqual(human.exception.code, "human_review_required")
        await self.service.respond(approval["id"], {"decision": "decline"})
        await self.until(parent["id"], ("completed",))

    async def test_restart_expires_the_native_call_instead_of_replaying_a_decision(self):
        task = await self.start_task("hold")
        req = await self.native_question(task, self.envelope())
        await self.service.close()
        self.service = self.new_service()
        with self.assertRaises(BridgeError) as expired:
            await self.service.respond(req["id"], self.response(req))
        self.assertEqual(expired.exception.code, "stale_request")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0], 0)

    async def test_delegation_contract_is_recorded_scoped_and_expires_without_cancelling(self):
        task = await self.start_task("hold")
        args = {"task_id": task["id"], "prompt": "Inspect the local target", "expected_turn_id": task["turn_id"],
            "operation_id": "delegate-contract", "contract": self.envelope(ttl_seconds=60)}
        result = await dispatch(self.service, "codex_delegate", args)
        record = result["delegation_contract"]
        self.assertEqual(record["delivery"]["state"], "acknowledged")
        self.assertEqual(record["scope"]["operation_id"], "delegate-contract")
        self.assertEqual(record["scope"]["turn_id"], task["turn_id"])
        self.assertEqual(record["authority"], "guidance_only")
        self.clock += 61
        current = self.service.get(task["id"])
        self.assertTrue(current["delegation_contract"]["expired"])
        self.assertTrue(self.service.get(task["id"], view="compact")["delegation_contract_summary"]["expired"])
        self.assertEqual(current["status"], "running")
        self.assertTrue((await dispatch(self.service, "codex_delegate", args))["delegation_contract"]["expired"])
        self.assertEqual(self.service.list_tasks(view="compact")["tasks"][0]["delegation_contract_summary"]["expired"], True)
        seq = current["delegation_contracts"][0]["source"]["event_seq"]
        event = (await self.service.events(task_id=task["id"], after=seq - 1, limit=1))["events"][0]
        self.assertEqual(event["data"]["context_hash"], record["context_hash"])

    async def test_expired_delegation_before_write_is_cached_as_rejected_and_releases_its_reservation(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("model", "model/list")
        args = {"task_id": task["id"], "prompt": "Inspect", "operation_id": "expired-contract", "contract": self.envelope(ttl_seconds=60)}
        with patch.object(self.service.backend.process.stdin, "write", wraps=self.service.backend.process.stdin.write) as sent:
            sending = asyncio.create_task(dispatch(self.service, "codex_delegate", args))
            await gate.wait()
            self.clock += 61
            gate.release.set()
            with self.assertRaises(BridgeError) as expired:
                await asyncio.wait_for(sending, 5)
        self.assertEqual(expired.exception.code, "decision_expired")
        self.assertFalse(expired.exception.uncertain)
        self.assertFalse(any(json.loads(call.args[0]).get("method") == "turn/start" for call in sent.call_args_list))
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id='expired-contract'").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertTrue(json.loads(row["result"])["operation_rejected"])
        self.assertFalse(self.service.get(task["id"])["workspace_reserved"])
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Rejected operation must not retry")):
            with self.assertRaises(BridgeError) as replay:
                await dispatch(self.service, "codex_delegate", args)
        self.assertEqual(replay.exception.code, "decision_expired")
        with self.assertRaises(BridgeError) as conflict:
            await dispatch(self.service, "codex_delegate", {**args, "prompt": "Different work"})
        self.assertEqual(conflict.exception.code, "operation_conflict")
        report = self.service.get(task["id"], view="reconciliation")["reconciliation"]
        op = next(item for item in report["operations"] if item["operation_id"] == "expired-contract")
        self.assertEqual(op["acknowledgement"], "rejected")

    async def test_failure_after_write_stays_uncertain_even_if_it_uses_the_guard_error_class(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        events = self.gate_callback("turn-event", "turn/started")
        gate = self.gate_call("after-write", "turn/start", error=decisions.DecisionError("Injected after write", code="decision_expired"))
        sending = asyncio.create_task(dispatch(self.service, "codex_delegate", {"task_id": task["id"], "prompt": "Inspect",
            "operation_id": "sent-contract", "contract": self.envelope(ttl_seconds=60)}))
        await gate.wait()
        await events.wait()
        gate.release.set()
        with self.assertRaises(BridgeError) as uncertain:
            await asyncio.wait_for(sending, 5)
        self.assertTrue(uncertain.exception.uncertain)
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='sent-contract'").fetchone()[0], "uncertain")
        self.assertEqual(self.service.get(task["id"])["delegation_contract"]["delivery"]["state"], "uncertain")
        self.assertEqual(self.service.get(task["id"])["status"], "uncertain")
        events.release.set()
        await self.until(task["id"], ("completed",))

    async def test_lost_delegation_ack_keeps_uncertainty_and_never_replays(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("lost-ack", "turn/start", error=TimeoutError("Injected lost acknowledgement"))
        args = {"task_id": task["id"], "prompt": "Inspect", "operation_id": "lost-contract-ack", "contract": self.envelope()}
        sending = asyncio.create_task(dispatch(self.service, "codex_delegate", args))
        await gate.wait()
        await self.until(task["id"], ("completed",))
        gate.release.set()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(sending, 5)
        self.assertEqual(self.service.get(task["id"])["delegation_contract"]["delivery"]["state"], "uncertain")
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='lost-contract-ack'").fetchone()[0], "uncertain")
        with patch.object(self.service.backend, "call", side_effect=AssertionError("Unknown work cannot replay")), self.assertRaises(BridgeError):
            await dispatch(self.service, "codex_delegate", args)

    async def test_evidence_references_are_scoped_hashed_and_never_materialised_by_decisions(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        self.store.event(task["id"], "item/completed", {"turnId": task["turn_id"], "item": {
            "id": "large-report", "type": "agentMessage", "text": "evidence " * 4000}})
        result = self.store.evidence.result(task["id"], task["turn_id"])
        artifact = result["artifacts"][0]
        reference = {"task_id": task["id"], "turn_id": task["turn_id"], "result_id": result["result_id"],
            "artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"]}
        active = await self.service.send(task["id"], "hold")
        with patch.object(self.store.evidence, "read", side_effect=AssertionError("Metadata validation cannot read artifact bytes")):
            req = await self.native_question(active, self.envelope(evidence=[reference]))
            await self.service.respond(req["id"], self.response(req))
        other = await self.start_task("hold", "b", "other")
        with patch.object(self.service.backend, "respond", wraps=self.service.backend.respond) as refusal:
            self.assertIsNone(await self.native_question(other, self.envelope(evidence=[reference]), rpc_id=9971))
        self.assertEqual(json.loads(refusal.call_args.args[1]["contentItems"][0]["text"])["code"], "decision_mismatch")
        req = await self.native_question(active, self.envelope(evidence=[reference]), rpc_id=9972)
        self.store.db.execute("UPDATE artifacts SET sha256=? WHERE id=?", ("0" * 64, artifact["artifact_id"]))
        with self.assertRaises(BridgeError) as mismatch:
            await self.service.respond(req["id"], self.response(req))
        self.assertEqual(mismatch.exception.code, "decision_mismatch")

    async def test_structured_decision_is_never_a_permission_or_user_answer(self):
        task = await self.start_task("hold")
        for kind in ("command_approval", "file_approval", "permissions", "user_input", "elicitation"):
            with self.subTest(kind=kind), self.assertRaises(BridgeError) as refused:
                self.service.validate_response({"kind": kind, "task_id": task["id"]}, {}, {"text": "approve", "decision": {
                    "version": 1, "id": "permission-like", "context_hash": "0" * 64}})
            self.assertEqual(refused.exception.code, "invalid_response")

    async def test_envelope_limits_are_explicit_and_contract_history_is_bounded(self):
        task = await self.start_task("hold")
        with self.assertRaises(BridgeError) as big:
            decisions.bind(self.service, self.service.task(task["id"]), self.envelope(constraints=["雪" * 1000] * 16), kind="question", request_id="pending")
        self.assertEqual(big.exception.code, "invalid_arguments")
        with patch.object(decisions, "CONTRACT_LIMIT", 2):
            for index in range(3):
                await dispatch(self.service, "codex_delegate", {"task_id": task["id"], "prompt": "Inspect", "expected_turn_id": task["turn_id"],
                    "operation_id": "contract-" + str(index), "contract": self.envelope(id="contract-" + str(index))})
        records = self.service.get(task["id"])["delegation_contracts"]
        self.assertEqual([record["id"] for record in records], ["contract-1", "contract-2"])

    async def test_fork_does_not_copy_a_contract_bound_to_the_source_task(self):
        task = await self.start_task("hold")
        await dispatch(self.service, "codex_delegate", {"task_id": task["id"], "prompt": "Inspect",
            "expected_turn_id": task["turn_id"], "operation_id": "contract-before-fork", "contract": self.envelope()})
        await self.service.interrupt(task["id"], task["turn_id"])
        await self.until(task["id"], ("interrupted",))
        forked = await self.service.fork(task["id"])
        self.assertIsNone(forked["delegation_contract"])
        self.assertEqual(forked["delegation_contracts"], [])
        self.assertTrue(self.service.get(task["id"])["delegation_contract"])

    async def test_newly_adopted_child_does_not_copy_parent_scoped_contracts(self):
        gate = self.gate_callback("adopt-after-contract", "thread/started")
        parent = await self.start_task("native_children")
        await gate.wait()
        await dispatch(self.service, "codex_delegate", {"task_id": parent["id"], "prompt": "Inspect",
            "expected_turn_id": parent["turn_id"], "operation_id": "contract-before-adoption", "contract": self.envelope()})
        gate.release.set()
        requests = await self.pending(parent["id"])
        child = self.service.get(next(r["task_id"] for r in requests if r["task_id"] != parent["id"]))
        self.assertIsNone(child["delegation_contract"])
        self.assertIsNone(child["delegation_contract_summary"])
        self.assertEqual(child["delegation_contracts"], [])
        self.assertEqual(self.service.get(parent["id"])["delegation_contract"]["delivery"]["state"], "acknowledged")

    async def test_artifact_evidence_must_belong_to_the_named_immutable_result_version(self):
        from codex_local_mcp.evidence import artifact
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        old = self.service.get(task["id"], view="result")["result"]
        ref = artifact(self.store.db, task["id"], task["turn_id"], b"later capture")
        reference = {"task_id": task["id"], "turn_id": task["turn_id"], "result_id": old["result_id"],
            "artifact_id": ref["artifact_id"], "sha256": ref["sha256"]}
        with self.assertRaises(BridgeError) as mismatch:
            decisions.bind(self.service, self.service.task(task["id"]), self.envelope(evidence=[reference]), kind="delegation")
        self.assertEqual(mismatch.exception.code, "decision_mismatch")
        fresh = self.service.get(task["id"], view="result")["result"]
        self.assertNotEqual(fresh["result_id"], old["result_id"])
        reference["result_id"] = fresh["result_id"]
        decisions.bind(self.service, self.service.task(task["id"]), self.envelope(evidence=[reference]), kind="delegation")

    async def test_late_ack_contract_delivery_keeps_the_acknowledged_turn_identity(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("delayed-contract-ack", "turn/start")
        sending = asyncio.create_task(dispatch(self.service, "codex_delegate", {"task_id": task["id"], "prompt": "Inspect",
            "operation_id": "delayed-contract", "contract": self.envelope()}))
        await gate.wait()
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: self.service.get(task["id"])["turn_id"] != task["turn_id"]), 5)
        actual = self.service.get(task["id"])["turn_id"]
        await self.service.on_message({"method": "turn/started", "params": {"threadId": task["thread_id"],
            "turnId": "later-native-turn", "turn": {"id": "later-native-turn", "status": "inProgress"}}})
        gate.release.set()
        result = await asyncio.wait_for(sending, 5)
        self.assertEqual(result["turn_id"], "later-native-turn")
        self.assertEqual(result["delegation_contract"]["delivery"]["turn_id"], actual)
