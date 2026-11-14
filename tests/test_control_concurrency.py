import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

from codex_local_mcp.backend import RpcError
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import dispatch
from codex_local_mcp import broker
from codex_local_mcp.store import SCHEMA_VERSION
from tests.helpers import FaultGate, read_fake_history
from tests.test_service import ServiceHarness


class ObservedLock(asyncio.Lock):
    def __init__(self, admission):
        super().__init__()
        self.admission = admission
        self.waiting = asyncio.Event()

    async def acquire(self):
        if self.admission.locked():
            raise AssertionError("Family lock acquired under admission lock")
        if self.locked():
            self.waiting.set()
        return await super().acquire()


class ControlConcurrencyTest(ServiceHarness):
    automatic_reconciliation = True

    async def interrupt(self, task, key="interrupt"):
        return await dispatch(self.service, "codex_interrupt", {
            "task_id": task["id"], "expected_turn_id": task["turn_id"], "request_id": key})

    async def test_interrupt_reaches_backend_while_other_project_model_lookup_is_stalled(self):
        task = await self.start_task("hold")
        gate = self.gate_call("other_project_models", "model/list", before=True)
        starting = asyncio.create_task(self.start_task("hold", path="b", key="other"))
        await gate.wait()
        await asyncio.wait_for(self.interrupt(task), 2)
        self.assertFalse(gate.release.is_set())
        await self.until(task["id"], ("interrupted",))
        gate.release.set()
        await starting

    async def test_denial_reaches_backend_while_other_project_is_stalled(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        gate = self.gate_call("other_project_models", "model/list", before=True)
        starting = asyncio.create_task(self.start_task("hold", path="b", key="other"))
        await gate.wait()
        sent = await asyncio.wait_for(dispatch(self.service, "codex_respond", {
            "request_id": req["id"], "response": {"decision": "decline"}}), 2)
        self.assertEqual(sent["status"], "sent")
        await self.until(task["id"], ("completed",))
        self.assertFalse(gate.release.is_set())
        gate.release.set()
        await starting

    async def test_parent_answer_reaches_backend_while_other_project_is_stalled(self):
        task = await self.start_task("parent")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        gate = self.gate_call("other_project_models", "model/list", before=True)
        starting = asyncio.create_task(self.start_task("hold", path="b", key="other"))
        await gate.wait()
        await asyncio.wait_for(dispatch(self.service, "codex_respond", {
            "request_id": req["id"], "response": {"text": "Use the selected project"}}), 2)
        self.assertFalse(gate.release.is_set())
        gate.release.set()
        await starting

    async def test_same_family_controls_serialize_but_interrupt_bypasses_them(self):
        task = await self.start_task("hold")
        lock = ObservedLock(self.service.admission_lock)
        self.service.family_locks[task["id"]] = lock
        gate = self.gate_call("rename_ack", "thread/name/set")
        renaming = asyncio.create_task(dispatch(self.service, "codex_rename", {
            "task_id": task["id"], "name": "new", "request_id": "rename"}))
        await gate.wait()
        settings = asyncio.create_task(dispatch(self.service, "codex_settings", {
            "task_id": task["id"], "scope": "next", "effort": "high", "request_id": "settings"}))
        await asyncio.wait_for(lock.waiting.wait(), 2)
        self.assertFalse(settings.done())
        await asyncio.wait_for(self.interrupt(task), 2)
        self.assertFalse(gate.release.is_set())
        gate.release.set()
        await asyncio.gather(renaming, settings)

    async def test_send_start_ack_does_not_block_current_turn_interrupt(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        gate = self.gate_call("followup_ack", "turn/start")
        sending = asyncio.create_task(dispatch(self.service, "codex_send", {
            "task_id": task["id"], "prompt": "hold", "request_id": "followup"}))
        await gate.wait()
        active = self.service.get(task["id"])
        self.assertNotEqual(active["turn_id"], task["turn_id"])
        await asyncio.wait_for(self.interrupt(active), 2)
        await self.until(task["id"], ("interrupted",))
        gate.release.set()
        self.assertEqual((await sending)["status"], "interrupted")
        with self.assertRaisesRegex(BridgeError, "current active"):
            await self.interrupt(task, "old-interrupt")

    async def test_concurrent_starts_reserve_capacity_before_model_lookup(self):
        self.service.config = replace(self.cfg, max_tasks=1)
        gate = self.gate_call("first_models", "model/list", before=True)
        first = asyncio.create_task(self.start_task("hold"))
        await gate.wait()
        self.assertEqual(self.store.all("tasks"), [])
        with self.assertRaisesRegex(BridgeError, "Concurrent task limit"):
            await self.start_task("hold", path="b", key="second")
        gate.release.set()
        await first
        self.assertEqual(len(self.store.all("tasks")), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)

    async def test_concurrent_starts_cannot_admit_overlapping_directories(self):
        gate = self.gate_call("first_models", "model/list", before=True)
        first = asyncio.create_task(self.start_task("hold", path="outer"))
        await gate.wait()
        with self.assertRaisesRegex(BridgeError, "active in this project"):
            await self.start_task("hold", path="outer/inner", key="overlap")
        gate.release.set()
        await first
        self.assertEqual(len(self.store.all("tasks")), 1)

    async def test_concurrent_duplicate_keys_share_one_operation(self):
        gate = self.gate_call("models", "model/list", before=True)
        first = asyncio.create_task(self.start_task("hold"))
        await gate.wait()
        duplicate = asyncio.create_task(self.start_task("hold"))
        gate.release.set()
        one, two = await asyncio.gather(first, duplicate)
        self.assertEqual(one, two)
        self.assertEqual(len(self.store.all("tasks")), 1)
        self.assertEqual(len(read_fake_history(self.state)), 1)

    async def test_caller_cancellation_keeps_operation_and_admission_until_ack(self):
        gate = self.gate_call("models", "model/list", before=True)
        caller = asyncio.create_task(self.start_task("hold"))
        await gate.wait()
        accepted = self.service.operation_tasks["start"]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(accepted.cancelled())
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='start'").fetchone()[0], "running")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 1)
        gate.release.set()
        result = await self.start_task("hold")
        self.assertEqual(result["id"], (await accepted)["id"])
        self.assertEqual(len(self.store.all("tasks")), 1)
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='start'").fetchone()[0], "done")

    async def test_caller_timeout_does_not_cancel_operation(self):
        gate = self.gate_call("models", "model/list", before=True)
        caller = asyncio.create_task(self.start_task("hold"))
        await gate.wait()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(caller, 0.01)
        accepted = self.service.operation_tasks["start"]
        self.assertFalse(accepted.done())
        gate.release.set()
        result = await self.start_task("hold")
        self.assertEqual((await accepted)["id"], result["id"])

    async def test_failed_admission_work_releases_capacity(self):
        gate = self.gate_call("model_rejection", "model/list", before=True,
            error=RpcError({"code": -32603, "message": "Rejected catalog lookup"}))
        first = asyncio.create_task(self.start_task("hold"))
        await gate.wait()
        gate.release.set()
        with self.assertRaises(RpcError):
            await first
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)
        self.assertEqual(self.store.all("tasks"), [])
        self.assertEqual((await self.start_task("hold", key="retry"))["status"], "running")

    async def test_native_calls_and_family_waits_never_hold_admission_lock(self):
        original = self.service.backend.call
        async def checked(method, params, **kwargs):
            self.assertFalse(self.service.admission_lock.locked(), method)
            return await original(method, params, **kwargs)
        self.service.backend.call = checked
        task = await self.start_task("hold")
        self.service.family_locks[task["id"]] = ObservedLock(self.service.admission_lock)
        await dispatch(self.service, "codex_rename", {"task_id": task["id"], "name": "new", "request_id": "rename"})
        await self.interrupt(task)

    async def test_response_cancellation_keeps_send_and_rejects_duplicate_answer(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        original = self.service.backend.respond
        gate = self.gates["response"] = FaultGate("response")
        async def delayed(*args, **kwargs):
            await gate.hit()
            await original(*args, **kwargs)
        self.service.backend.respond = delayed
        caller = asyncio.create_task(self.service.respond(req["id"], {"decision": "decline"}))
        await gate.wait()
        accepted = self.service.response_tasks[req["id"]]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        with self.assertRaisesRegex(BridgeError, "already being answered"):
            await self.service.respond(req["id"], {"decision": "decline"})
        gate.release.set()
        self.assertEqual((await accepted)["status"], "sent")
        await self.until(task["id"], ("completed",))
        with self.assertRaises(BridgeError):
            await self.service.respond(req["id"], {"decision": "decline"})

    async def test_resolved_approval_waiting_for_write_lock_is_never_sent(self):
        task = await self.start_task("approval")
        req = (await self.until(task["id"], ("waiting",)))["pending_requests"][0]
        backend = self.service.backend
        entered = asyncio.Event()
        original = backend.respond
        async def entering(*args, **kwargs):
            entered.set()
            await original(*args, **kwargs)
        backend.respond = entering
        with patch.object(backend.process.stdin, "write", wraps=backend.process.stdin.write) as written:
            async with backend.write_lock:
                answering = asyncio.create_task(self.service.respond(req["id"], {"decision": "accept"}, human=True))
                await asyncio.wait_for(entered.wait(), 2)
                await self.service.on_message({"method": "turn/completed", "params": {
                    "threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}}})
            with self.assertRaisesRegex(BridgeError, "resolved, expired"):
                await answering
            # Completion may reconcile native intent, but no approval response
            # may pass the guarded write boundary after the request expired.
            self.assertTrue(all("method" in json.loads(call.args[0]) for call in written.call_args_list))
        self.assertEqual(self.store.get("requests", req["id"])["status"], "expired")

    async def test_child_controls_share_parent_family_serialization(self):
        parent = await self.start_task("native_children")
        async with self.service.changed:
            await asyncio.wait_for(self.service.changed.wait_for(lambda: bool(self.service.requests(parent["id"]))), 5)
        child = self.service.get(self.service.requests(parent["id"])[0]["task_id"])
        lock = ObservedLock(self.service.admission_lock)
        self.service.family_locks[parent["id"]] = lock
        gate = self.gate_call("parent_rename", "thread/name/set")
        renaming = asyncio.create_task(dispatch(self.service, "codex_rename", {
            "task_id": parent["id"], "name": "new", "request_id": "parent-name"}))
        await gate.wait()
        setting = asyncio.create_task(dispatch(self.service, "codex_settings", {
            "task_id": child["id"], "scope": "next", "effort": "high", "request_id": "child-settings"}))
        await asyncio.wait_for(lock.waiting.wait(), 2)
        await asyncio.wait_for(self.interrupt(child), 2)
        self.assertFalse(setting.done())
        gate.release.set()
        await asyncio.gather(renaming, setting)

    async def test_broker_migration_refusal_releases_lock_without_constructing_service(self):
        with self.store.transaction():
            self.store.db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with patch.object(broker, "Service") as construct, patch.object(broker, "release_lock", wraps=broker.release_lock) as release:
            with self.assertRaisesRegex(BridgeError, "newer than supported"):
                await broker.run_broker(self.cfg)
        construct.assert_not_called()
        release.assert_called_once()

    async def test_restart_marks_operations_uncertain_and_drops_only_admission_plans(self):
        with self.store.transaction():
            self.store.db.execute("INSERT INTO operations VALUES ('old','fingerprint','running',NULL,NULL)")
            self.store.db.execute("INSERT INTO admissions VALUES ('old','family',?)", (str(self.root / "a"),))
        self.service._recover()
        self.assertEqual(self.store.db.execute("SELECT state FROM operations WHERE id='old'").fetchone()[0], "uncertain")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)

    async def test_session_import_admission_uses_the_owned_family(self):
        task = await self.start_task("hold")
        result = await dispatch(self.service, "codex_sessions", {"workdir": task["project"], "action": "import",
            "thread_id": task["thread_id"], "confirm_stopped": True, "request_id": "owned-import"})
        self.assertEqual(result["id"], task["id"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)

    async def test_operation_task_factory_runs_after_admission_is_released(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        held = []
        def factory(event_loop, coroutine, **kwargs):
            if coroutine.__qualname__.endswith("._run_operation"):
                held.append(self.service.admission_lock.locked())
            return asyncio.Task(coroutine, loop=event_loop, **kwargs)
        loop.set_task_factory(factory)
        try:
            await self.start_task("hold")
        finally:
            loop.set_task_factory(previous)
        self.assertEqual(held, [False])
