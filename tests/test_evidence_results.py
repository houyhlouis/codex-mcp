"""Turn records distinguish native observations, claims and scoped byte versions."""

import base64
import hashlib
import json
from pathlib import Path

from codex_local_mcp.evidence import ARTIFACT_BYTES, artifact
from codex_local_mcp.security import BridgeError
from codex_local_mcp.tools import BY_NAME, arguments, dispatch, result_size, validate
from tests.helpers import link_or_skip
from tests.test_service import ServiceHarness


class EvidenceResultsTest(ServiceHarness):
    async def notify(self, task, method, **params):
        await self.service.on_message({"method": method, "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], **params}})

    async def finish(self, task):
        await self.notify(task, "turn/completed", turn={"id": task["turn_id"], "status": "completed"})

    async def result(self, task, **fields):
        value = await dispatch(self.service, "codex_get", {"task_id": task["id"], "view": "result", **fields})
        validate(value, BY_NAME["codex_get"]["outputSchema"])
        return value["result"]

    async def test_native_evidence_is_source_linked_and_separate_from_claims(self):
        task = await self.start_task("hold")
        await self.notify(task, "item/completed", item={"id": "cmd", "type": "commandExecution", "command": "true", "cwd": task["project"], "status": "completed", "exitCode": 0})
        await self.notify(task, "item/completed", item={"id": "claim", "type": "agentMessage", "text": "Everything is correct"})
        await self.finish(task)
        value = await self.result(task)
        self.assertEqual(value["format_version"], 1)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["observed_commands"][0]["reported"]["exitCode"], 0)
        self.assertEqual(value["agent_claims"][0]["reported"]["text"], "Everything is correct")
        self.assertIn("not verification", value["limitations"][0]["reason"])
        for category in ("agent_claims", "observed_commands"):
            source = value[category][0]["source"]
            event = self.store.db.execute("SELECT * FROM events WHERE seq=?", (source["event_seq"],)).fetchone()
            self.assertEqual(event["task_id"], task["id"])
            self.assertEqual(source["thread_id"], task["thread_id"])
            self.assertEqual(source["turn_id"], task["turn_id"])
        self.assertEqual((await self.result(task))["result_id"], value["result_id"])

    async def test_immutable_results_survive_followups_late_events_and_paging(self):
        task = await self.start_task("hold")
        for i in range(6):
            await self.notify(task, "item/completed", item={"id": "claim" + str(i), "type": "agentMessage", "text": str(i)})
        await self.finish(task)
        first = await self.result(task, limit=2)
        pinned, offset = [], 0
        while True:
            page = await self.result(task, result_id=first["result_id"], offset=offset, limit=2)
            pinned.extend(page["agent_claims"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(len(pinned), 6)
        await self.notify(task, "item/completed", item={"id": "late", "type": "agentMessage", "text": "late"})
        newer = await self.result(task)
        self.assertGreater(newer["revision"], first["revision"])
        self.assertEqual((await self.result(task, result_id=first["result_id"]))["counts"]["agent_claims"], 6)
        next_task = {**task, "turn_id": "next-turn"}
        await self.notify(next_task, "turn/started", turn={"id": "next-turn", "status": "inProgress"})
        current = await self.result(next_task)
        self.assertEqual(current["counts"]["agent_claims"], 0)
        old = await self.result(next_task, turn_id=task["turn_id"])
        self.assertEqual(old["turn_id"], task["turn_id"])
        self.assertEqual(old["counts"]["agent_claims"], 7)
        self.assertEqual((await self.result(next_task, result_id=first["result_id"]))["counts"]["agent_claims"], 6)

    async def test_large_output_is_bounded_hashed_and_fetched_by_task(self):
        task = await self.start_task("hold")
        output = 'é\\"\n' * 100000
        await self.notify(task, "item/completed", item={"id": "cmd", "type": "commandExecution", "command": "offline", "cwd": task["project"], "status": "completed", "exitCode": 0, "aggregatedOutput": output})
        await self.finish(task)
        value = await self.result(task)
        ref = value["observed_commands"][0]["reported"]
        self.assertIn("artifact_id", ref)
        self.assertEqual(ref["encoding"], "json")
        stored = self.store.db.execute("SELECT content,source_locator FROM artifacts WHERE id=?", (ref["artifact_id"],)).fetchone()
        self.assertIsNone(stored["content"])
        self.assertEqual(json.loads(stored["source_locator"]), {"format": 1, "method": "item/completed", "path": [], "encoding": "json"})
        data, offset = bytearray(), 0
        while True:
            response = await dispatch(self.service, "codex_get", {"task_id": task["id"], "view": "artifact", "artifact_id": ref["artifact_id"], "offset": offset, "limit": 65536})
            self.assertLess(result_size(response), 1024 * 1024)
            part = response["artifact"]
            data.extend(base64.b64decode(part["data"]))
            if part["next_offset"] is None:
                break
            offset = part["next_offset"]
        self.assertEqual(hashlib.sha256(data).hexdigest(), ref["sha256"])
        self.assertEqual(json.loads(data)["aggregatedOutput"], output)
        other = await self.start_task("hold", "other", "other")
        with self.assertRaises(BridgeError) as denied:
            self.service.get(other["id"], view="artifact", artifact_id=ref["artifact_id"])
        self.assertEqual(denied.exception.code, "artifact_not_found")

    async def test_new_large_command_output_is_stored_once(self):
        task = await self.start_task("hold")
        allocated = lambda: self.store.db.execute("PRAGMA page_count").fetchone()[0] * self.store.db.execute("PRAGMA page_size").fetchone()[0]
        before = allocated()
        output = "unique-command-output:" + "x" * (2 * 1024 * 1024)
        await self.notify(task, "item/completed", item={"id": "one-copy", "type": "commandExecution", "command": "offline", "cwd": task["project"], "status": "completed", "exitCode": 0, "aggregatedOutput": output})
        await self.finish(task)
        result = await self.result(task)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE instr(data,'unique-command-output:')>0").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM artifacts WHERE content IS NOT NULL").fetchone()[0], 0)
        for table in ("tasks", "evidence_entries", "turn_results"):
            self.assertEqual(self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE instr(data,'unique-command-output:')>0").fetchone()[0], 0, table)
        self.assertLess(allocated() - before, len(output) + 256 * 1024)
        self.assertEqual(result["counts"]["observed_commands"], 1)

    async def test_changed_or_missing_journal_source_is_an_explicit_error(self):
        task = await self.start_task("hold")
        await self.notify(task, "item/completed", item={"id": "one", "type": "agentMessage", "text": "x" * 20000})
        ref = (await self.result(task))["agent_claims"][0]["reported"]
        seq = self.store.db.execute("SELECT source_seq FROM artifacts WHERE id=?", (ref["artifact_id"],)).fetchone()[0]
        original = self.store.db.execute("SELECT data FROM events WHERE seq=?", (seq,)).fetchone()[0]
        with self.store.transaction():
            changed = json.loads(original)
            changed["item"]["text"] = "different"
            self.store.db.execute("UPDATE events SET data=? WHERE seq=?", (json.dumps(changed), seq))
        with self.assertRaises(BridgeError) as mismatch:
            self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])
        self.assertEqual(mismatch.exception.code, "artifact_version_mismatch")
        self.assertIn("no longer matches", str(mismatch.exception))
        with self.store.transaction():
            self.store.db.execute("UPDATE events SET data=?,task_id='another-task' WHERE seq=?", (original, seq))
        with self.assertRaises(BridgeError) as wrong_owner:
            self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])
        self.assertEqual(wrong_owner.exception.code, "artifact_version_mismatch")
        with self.store.transaction():
            self.store.db.execute("DELETE FROM events WHERE seq=?", (seq,))
        with self.assertRaises(BridgeError) as missing:
            self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])
        self.assertEqual(missing.exception.code, "artifact_version_mismatch")
        self.assertIn("no longer available", str(missing.exception))

    async def test_diff_and_patch_artifacts_reconstruct_the_reported_shape(self):
        task = await self.start_task("hold")
        diff = "a change\n" * 3000
        changes = [{"path": "example.py", "kind": {"type": "update"}, "diff": diff}]
        await self.notify(task, "turn/diff/updated", diff=diff)
        await self.notify(task, "item/fileChange/patchUpdated", itemId="patch", changes=changes)
        reports = (await self.result(task))["reported_changes"]
        observed = []
        for entry in reports:
            ref = entry["reported"]
            response = self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])["artifact"]
            observed.append(json.loads(base64.b64decode(response["data"])))
        self.assertEqual(observed, [{"type": "native_diff", "diff": diff}, {"type": "native_patch", "changes": changes}])

    async def test_file_artifacts_are_immutable_and_live_references_fail_on_change(self):
        task = await self.start_task("hold")
        path = Path(task["project"]) / "proof.txt"
        path.write_bytes(b"captured evidence")
        await self.notify(task, "item/completed", item={"id": "file", "type": "fileChange", "changes": [{"path": "proof.txt", "kind": {"type": "add"}, "diff": "+captured evidence"}]})
        await self.finish(task)
        value = await self.result(task)
        refs = {r["kind"]: r for r in value["artifacts"]}
        self.assertEqual(set(refs), {"captured", "live_file"})
        def read(kind):
            return self.service.get(task["id"], view="artifact", artifact_id=refs[kind]["artifact_id"])["artifact"]
        self.assertEqual(base64.b64decode(read("live_file")["data"]), b"captured evidence")
        path.write_bytes(b"changed evidence")
        self.assertEqual(base64.b64decode(read("captured")["data"]), b"captured evidence")
        with self.assertRaises(BridgeError) as stale:
            read("live_file")
        self.assertEqual(stale.exception.code, "artifact_version_mismatch")
        path.unlink()
        link_or_skip(self, path, self.config_file)
        with self.assertRaises(BridgeError):
            read("live_file")

    async def test_reported_paths_cannot_escape_or_follow_links(self):
        task = await self.start_task("hold")
        project = Path(task["project"])
        link_or_skip(self, project / "escape", self.base)
        for i, path in enumerate(("../config.toml", str(self.config_file), "escape/config.toml")):
            await self.notify(task, "item/completed", item={"id": str(i), "type": "fileChange", "changes": [{"path": path, "kind": {"type": "update"}, "diff": ""}]})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        value = await self.result(task)
        self.assertEqual(value["counts"]["reported_changes"], 3)
        self.assertEqual(value["counts"]["limitations"], 5)
        self.assertFalse(value["evidence_completeness"]["artifacts_complete"])
        self.assertIn("file_bytes_unavailable", value["evidence_completeness"]["gaps"])

    async def test_native_cwd_is_used_and_outside_cwd_is_refused(self):
        task = await self.start_task("hold")
        project = Path(task["project"])
        (project / "sub").mkdir()
        (project / "sub" / "proof.txt").write_bytes(b"subdir")
        self.service.lifecycle.metadata(task["id"], {"native_cwd": str(project / "sub")})
        await self.notify(task, "item/completed", item={"id": "sub", "type": "fileChange", "changes": [{"path": "proof.txt", "kind": {"type": "add"}, "diff": "+subdir"}]})
        rows = self.store.db.execute("SELECT path FROM artifacts").fetchall()
        self.assertEqual({r[0] for r in rows}, {str(Path("sub") / "proof.txt")})
        self.service.lifecycle.metadata(task["id"], {"native_cwd": str(self.base)})
        await self.notify(task, "item/completed", item={"id": "outside", "type": "fileChange", "changes": [{"path": "config.toml", "kind": {"type": "update"}, "diff": ""}]})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 2)

    async def test_redaction_precedes_encoding_and_private_version_hash_never_leaks(self):
        task = await self.start_task("hold")
        path = Path(task["project"]) / "evidence.txt"
        raw = b"password=short-secret\n"
        path.write_bytes(raw)
        async def changed():
            await self.notify(task, "item/completed", item={"id": "redacted-file", "type": "fileChange", "changes": [{"path": "evidence.txt", "kind": {"type": "update"}, "diff": "password=short-secret"}]})
        await changed()
        value = await self.result(task)
        self.assertNotIn(hashlib.sha256(raw).hexdigest(), json.dumps(value))
        refs = {r["kind"]: r for r in value["artifacts"]}
        for ref in refs.values():
            self.assertTrue(ref["content_redacted"])
            self.assertEqual(ref["redaction_count"], 1)
            response = self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])
            data = base64.b64decode(response["artifact"]["data"])
            self.assertNotIn(b"short-secret", data)
            self.assertIn(b"[REDACTED]", data)
            self.assertEqual(hashlib.sha256(data).hexdigest(), ref["sha256"])
            self.assertNotIn(hashlib.sha256(raw).hexdigest(), json.dumps(response))
        path.write_bytes(b"password=other-secret\n")
        with self.assertRaises(BridgeError) as stale:
            self.service.get(task["id"], view="artifact", artifact_id=refs["live_file"]["artifact_id"])
        self.assertEqual(stale.exception.code, "artifact_version_mismatch")
        await changed()
        newer = await self.result(task)
        live_ids = {r["artifact_id"] for r in newer["artifacts"] if r["kind"] == "live_file"}
        self.assertEqual(len(live_ids), 2)
        new_id = (live_ids - {refs["live_file"]["artifact_id"]}).pop()
        self.assertTrue(self.service.get(task["id"], view="artifact", artifact_id=new_id)["artifact"]["content_redacted"])
        (Path(task["project"]) / "secrets.json").write_text('{"password": "json-secret"}', encoding="utf-8-sig")
        await self.notify(task, "item/completed", item={"id": "json", "type": "fileChange", "changes": [{"path": "secrets.json", "kind": {"type": "add"}, "diff": ""}]})
        json_refs = [r for r in (await self.result(task))["artifacts"] if r.get("path") == "secrets.json"]
        self.assertEqual(len(json_refs), 2)
        for ref in json_refs:
            response = self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])["artifact"]
            self.assertNotIn(b"json-secret", base64.b64decode(response["data"]))

    async def test_binary_artifacts_preserve_the_observed_bytes(self):
        task = await self.start_task("hold")
        raw = b"\xff\x00\x80binary"
        (Path(task["project"]) / "binary.dat").write_bytes(raw)
        await self.notify(task, "item/completed", item={"id": "binary", "type": "fileChange", "changes": [{"path": "binary.dat", "kind": {"type": "add"}, "diff": ""}]})
        value = await self.result(task)
        for ref in value["artifacts"]:
            data = self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])["artifact"]
            self.assertFalse(data["content_redacted"])
            self.assertEqual(base64.b64decode(data["data"]), raw)

    async def test_capture_limit_integrity_and_late_started_do_not_erase_completion(self):
        task = await self.start_task("hold")
        with self.assertRaises(BridgeError) as big:
            artifact(self.store.db, task["id"], task["turn_id"], b"x" * (ARTIFACT_BYTES + 1))
        self.assertEqual(big.exception.code, "artifact_too_large")
        command = {"id": "cmd", "type": "commandExecution", "command": "true", "cwd": task["project"], "status": "completed", "exitCode": 0}
        await self.notify(task, "item/completed", item=command)
        await self.notify(task, "item/started", item={**command, "status": "inProgress", "exitCode": None})
        with self.store.transaction():
            self.store.evidence.capture(task["id"], task["turn_id"])
        value = await self.result(task)
        self.assertEqual(value["observed_commands"][0]["reported"]["exitCode"], 0)
        with self.store.transaction():
            ref = artifact(self.store.db, task["id"], task["turn_id"], b"original")
            self.store.db.execute("UPDATE artifacts SET content=? WHERE id=?", (b"corrupted", ref["artifact_id"]))
        with self.assertRaises(BridgeError) as changed:
            self.service.get(task["id"], view="artifact", artifact_id=ref["artifact_id"])
        self.assertEqual(changed.exception.code, "artifact_version_mismatch")

    async def test_missing_events_are_explicit_and_current_fields_keep_working(self):
        task = await self.start_task()
        done = await self.until(task["id"], ("completed",))
        self.service.lifecycle.gap(task["id"], "offline_gap")
        value = await self.result(task)
        self.assertFalse(value["evidence_completeness"]["complete"])
        self.assertIn("offline_gap", value["evidence_completeness"]["gaps"])
        default = self.service.get(task["id"])
        self.assertEqual(default["summary"], done["summary"])
        self.assertEqual(default["commands"], done["commands"])
        self.assertNotIn("summary", self.service.get(task["id"], view="compact"))
        for args in ({"view": "default", "artifact_id": "x"}, {"view": "compact", "turn_id": "t"}, {"view": "artifact"}):
            with self.assertRaises(BridgeError):
                arguments("codex_get", {"task_id": task["id"], **args})
