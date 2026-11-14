import asyncio
import copy
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

from codex_local_mcp.incidents import export_incident, preview, readonly_store, validate_incident, write_export
from codex_local_mcp.security import BridgeError
from tests.incident_replay import IncidentReplay, ReplayGate
from tests.test_service import ServiceHarness


class IncidentTest(ServiceHarness):
    def fixture(self, task):
        return export_incident(self.store, [task["id"]])

    async def test_structural_export_omits_values_keys_binary_paths_and_environment(self):
        task = await self.start_task("hold")
        secret = "SEED_SECRET_NONREGEX_49917"
        with self.store.transaction():
            self.store.update("tasks", task["id"], {"name": secret, "summary": secret, "commands": [{"command": secret, "output": secret}], "private": {secret: secret}})
            self.store.event(task["id"], "turn/diff/updated", {"threadId": task["thread_id"], "turnId": task["turn_id"],
                "diff": secret, "images": [{"data": secret, "path": "/private/" + secret}], "unknown": {secret: secret}})
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret, "GITHUB_TOKEN": secret}):
            fixture = self.fixture(task)
        text = json.dumps(preview(fixture))
        for value in (secret, task["id"], task["thread_id"], str(self.base), "OPENAI_API_KEY", "GITHUB_TOKEN"):
            self.assertNotIn(value, text)
        self.assertIn('"$omitted"', text)
        self.assertIn('"length"', text)
        validate_incident(fixture)

    async def test_preview_is_required_matches_structure_and_never_overwrites(self):
        task = await self.start_task("hold")
        fixture = self.fixture(task)
        report = preview(fixture)
        destination = self.base / "incident.json"
        for confirmation in (None, "wrong"):
            with self.assertRaises(BridgeError) as caught:
                write_export(fixture, destination, confirmation)
            self.assertEqual(caught.exception.code, "incident_preview_mismatch")
            self.assertFalse(destination.exists())
        changed = copy.deepcopy(fixture)
        changed["partial"] = True
        with self.assertRaises(BridgeError):
            write_export(changed, destination, report["preview_id"])
        self.assertTrue(write_export(fixture, destination, report["preview_id"])["written"])
        self.assertEqual(json.loads(destination.read_text()), fixture)
        with self.assertRaises(FileExistsError):
            write_export(fixture, destination, report["preview_id"])

    async def test_cli_preview_is_readonly_and_does_not_start_a_broker(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        await self.service.close()
        self.store.close()
        files = {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()}
        command = [sys.executable, "server.py", "--config", str(self.config_file), "incident-export", "--task-id", task["id"], "--json"]
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, err.decode())
            validate_incident(json.loads(out)["fixture"])
            self.assertEqual(files, {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()})
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
            from codex_local_mcp.store import Store
            self.store = Store(self.state)
            self.service = self.new_service()

    async def test_readonly_store_never_migrates_or_writes(self):
        task = await self.start_task("hold")
        with readonly_store(self.cfg) as store:
            validate_incident(export_incident(store, [task["id"]]))
            with self.assertRaises(sqlite3.OperationalError) as caught:
                store.db.execute("DELETE FROM tasks")
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)

    async def test_version_malformed_content_and_injections_are_refused(self):
        task = await self.start_task("hold")
        fixture = self.fixture(task)
        for change in ({"format": 99}, {"content_omitted": False}, {"tasks": ["bad"]},
                       {"replay": {"consumer": "shell", "injections": []}}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                validate_incident({**fixture, **change})
        fixture["tasks"][0]["summary"] = "unomitted content"
        with self.assertRaises(BridgeError):
            validate_incident(fixture)

    async def test_exported_lost_ack_replays_terminal_order_without_reopening(self):
        gate = self.gate_call("lost-ack", "turn/start", error=TimeoutError("Injected lost acknowledgement"))
        starting = asyncio.create_task(self.start_task())
        try:
            await gate.wait()
            task = self.store.all("tasks")[0]
            await self.until(task["id"], ("completed",))
            gate.release.set()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(starting, 5)
            fixture = self.fixture(task)
            fixture["replay"]["injections"] = [{"action": "drop_ack", "at": 0}]
            first = await IncidentReplay(fixture).run()
            second = await IncidentReplay(fixture).run()
            self.assertEqual(first, second)
            self.assertFalse(first["acknowledged"])
            self.assertEqual(first["tasks"][0]["status"], "completed")
            self.assertLess(first["trace"].index("turn/started"), first["trace"].index("turn/completed"))
        finally:
            gate.release.set()
            await asyncio.wait_for(asyncio.gather(starting, return_exceptions=True), 5)

    async def test_exported_pending_approval_restart_expires_it_and_grants_nothing(self):
        task = await self.start_task("approval")
        await self.until(task["id"], ("waiting",))
        fixture = self.fixture(task)
        fault = {"action": "crash", "at": len(fixture["events"])}
        report = await IncidentReplay(fixture, injections=[fault]).run()
        self.assertTrue(report["restarted"])
        self.assertEqual(report["tasks"][0]["status"], "needs_resume")
        self.assertEqual(report["tasks"][0]["requests"], ["expired"])
        self.assertFalse(report["tasks"][0]["evidence_complete"])

    async def test_explicit_delay_gate_and_dropped_event_mark_missing_evidence(self):
        task = await self.start_task()
        await self.until(task["id"], ("completed",))
        fixture, gate = self.fixture(task), ReplayGate()
        progress = next(index for index, event in enumerate(fixture["events"])
            if event["kind"] == "item/agentMessage/delta")
        faults = [{"action": "delay", "at": 0, "gate": "gate-1"}, {"action": "drop", "at": progress}]
        running = asyncio.create_task(IncidentReplay(fixture, injections=faults, gates={"gate-1": gate}).run())
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            self.assertFalse(running.done())
            gate.release.set()
            report = await asyncio.wait_for(running, 10)
            self.assertIn("drop", report["trace"])
            self.assertFalse(report["tasks"][0]["evidence_complete"])
        finally:
            gate.release.set()
            await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 10)

    async def test_pinned_examples_are_real_exporter_outputs_and_replay(self):
        from tests.incident_samples import sample_incidents
        directory = Path(__file__).with_name("fixtures")
        for name, fixture in sample_incidents().items():
            with self.subTest(name=name):
                self.assertEqual(json.loads((directory / name).read_text()), fixture)
                report = await IncidentReplay(fixture).run()
                self.assertEqual(report["tasks"][0]["status"], "needs_resume" if "pending" in name else "completed")

    async def test_large_collections_are_typed_omissions_and_numeric_rpc_identity_stays_typed(self):
        task = await self.start_task("approval")
        await self.until(task["id"], ("waiting",))
        with self.store.transaction():
            self.store.update("tasks", task["id"], {"files": ["private/file"] * 1000})
        fixture = self.fixture(task)
        self.assertTrue(fixture["partial"])
        self.assertEqual(fixture["tasks"][0]["files"], {"$omitted": {"type": "array", "length": 1000}})
        self.assertEqual(fixture["requests"][0]["rpc_id"]["type"], "integer")
