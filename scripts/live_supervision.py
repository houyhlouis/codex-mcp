#!/usr/bin/env python3
"""Opt-in Phase 2 supervision check; consumes real signed-in Codex usage.

Use --config with an explicit, empty, disposable state_dir and --workdir with
an empty disposable project. No approval is granted by this script.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_local_mcp.config import load_config
from codex_local_mcp.platforms import default_state_dir
from codex_local_mcp.security import BridgeError, private_file, project_path, read_config_file, redact
from codex_local_mcp.service import Service
from codex_local_mcp.store import Store
from codex_local_mcp.tools import dispatch
from codex_local_mcp.retention import offline

TURN_SECONDS = 180
ARTIFACT_BYTES = 32 * 1024 * 1024
DETAIL_BYTES = 8 * 1024 * 1024
TERMINAL = frozenset({"completed", "failed", "interrupted"})


def check(condition, message):
    if not condition:
        raise RuntimeError(str(message))


def record(report, status, message):
    report["checks"].append({"status": status, "message": message})
    print(f"{status}: {message}", flush=True)


def isolated_inputs(config_path, workdir):
    data = tomllib.loads(read_config_file(Path(config_path).expanduser().absolute()))
    state = data.get("state_dir")
    check(isinstance(state, str) and Path(state).expanduser().is_absolute(),
          "Live supervision requires an explicit absolute state_dir in --config")
    # Compare before load_config can inspect the default state's parents.
    normalize = lambda path: os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
    check(normalize(state) != normalize(default_state_dir()), "Live supervision refuses the default state directory")
    cfg = load_config(config_path)
    check(normalize(cfg.state_dir) != normalize(default_state_dir()), "Live supervision refuses the default state directory")
    check(not cfg.state_dir.exists() or not any(cfg.state_dir.iterdir()), "Live supervision requires an empty state directory")
    project = project_path(cfg, workdir)
    check(not any(project.iterdir()), "Live supervision requires an empty workspace root")
    return cfg, project


@contextmanager
def isolated_state(cfg):
    try:
        with offline(cfg):
            check(all(path.name in ("startup.lock", "broker.lock") for path in cfg.state_dir.iterdir()),
                  "Live supervision requires an empty state directory")
            yield
    except BridgeError as exc:
        if exc.code == "maintenance_busy":
            raise RuntimeError("Live supervision requires an unused state directory; another process holds its state lock") from exc
        raise


async def request_content(service, request_id):
    text, digest, offset = [], None, 0
    for _ in range(1024):
        page = await dispatch(service, "codex_review_request", {"pending_request_id": request_id, "offset": offset})
        check(page["detail_offset"] == offset, "Request detail offset changed")
        digest = digest or page["detail_sha256"]
        check(page["detail_sha256"] == digest, "Request changed while fetching complete content")
        text.append(page["detail"])
        check(sum(len(part.encode("utf-8")) for part in text) <= DETAIL_BYTES, "Parent question exceeds the live-check detail limit")
        following = page["next_offset"]
        if following is None:
            value = "".join(text)
            check(hashlib.sha256(value.encode("utf-8")).hexdigest() == digest, "Request detail hash mismatch")
            return json.loads(value)
        check(following > offset, "Request detail pagination did not advance")
        offset = following
    raise RuntimeError("Request detail pagination did not finish")


async def answer_parent(service, request, report):
    envelope = request.get("decision_envelope")
    response = {"text": "Choose alpha. Create supervision.txt containing alpha, then run the one short local verification command. This is guidance, not a permission grant."}
    if envelope and envelope.get("options") and not envelope.get("expired"):
        selected = next((option for option in envelope["options"] if option["id"] == "alpha"), envelope["options"][0])
        response["text"] = f"Choose {selected['id']}. Create supervision.txt containing that marker, then run the one short local verification command. This is guidance, not a permission grant."
        decision = {"version": 1, "id": envelope["id"], "context_hash": envelope["context_hash"],
                    "option_id": selected["id"]}
        wrong = "0" * 64 if decision["context_hash"] != "0" * 64 else "1" * 64
        try:
            await dispatch(service, "codex_respond", {"pending_request_id": request["id"],
                "response": {**response, "decision": {**decision, "context_hash": wrong}}})
        except BridgeError as exc:
            check(exc.code == "decision_mismatch", f"Wrong decision hash returned {exc.code}")
        else:
            raise RuntimeError("Wrong decision context_hash was accepted")
        record(report, "PASS", "wrong structured-decision hash refused with decision_mismatch")
        response["decision"] = decision
    result = await dispatch(service, "codex_respond", {"pending_request_id": request["id"], "response": response})
    receipt = result["receipt"]
    check(receipt["decision_source"] == "main_agent" and receipt["decision"] == "answer" and receipt["delivery"] == "sent",
          "Parent answer receipt does not identify the main agent and successful delivery")
    if "decision" in response:
        check(receipt["decision_id"] == envelope["id"] and receipt["decision_context_hash"] == envelope["context_hash"],
              "Typed decision receipt is not bound to the displayed envelope")
        report["typed_decision"] = True
        record(report, "PASS", "typed parent decision delivered with bound receipt fields")
    report.setdefault("parent_receipts", []).append(receipt["receipt_id"])
    record(report, "PASS", "parent question answered with a main_agent receipt")


async def follow_outcome(service, task_id, report, *, seconds=TURN_SECONDS):
    deadline, cursors, requests_cursor, latest = time.monotonic() + seconds, {}, None, None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        args = {"task_id": task_id, "view": "outcome", "cursors": cursors,
                "wait_seconds": min(10, max(1, int(remaining)))}
        if requests_cursor:
            args["requests_cursor"] = requests_cursor
        try:
            page = await asyncio.wait_for(dispatch(service, "codex_events", args), remaining)
        except TimeoutError:
            raise RuntimeError("Outcome supervision timed out") from None
        check(page["view"] == "outcome" and page["events"] == [], "Outcome polling returned detailed replay")
        cursors = page["cursors"]
        for outcome in page["outcomes"]:
            if outcome["id"] == task_id:
                latest = outcome
                check(outcome["cursor"] == cursors[task_id], "Outcome cursor was not returned for reuse")
        for summary in page["pending_requests"]:
            request = await request_content(service, summary["id"])
            if request["kind"] == "parent_question":
                await answer_parent(service, request, report)
            else:
                # Denials never claim a person reviewed the native request.
                if request["kind"] in ("command_approval", "file_approval"):
                    offered = request["params"].get("availableDecisions", ["decline", "cancel"])
                    response = {"decision": "decline" if "decline" in offered else "cancel"}
                elif request["kind"] == "permissions":
                    response = {"permissions": {}}
                elif request["kind"] == "elicitation":
                    response = {"action": "decline"}
                else:
                    raise RuntimeError("Human input required; live supervision will not supply it")
                await dispatch(service, "codex_respond", {"pending_request_id": request["id"], "response": response})
                record(report, "PASS", "native approval request declined without human authority")
        requests_cursor = page["pending_requests_next_cursor"]
        if latest and latest["status"] in TERMINAL and page["pending_requests_total"] == 0:
            check(latest["status"] == "completed", f"Turn rested with status {latest['status']}")
            ref = latest.get("result_ref")
            check(ref and ref["available"] and ref["tool"] == "codex_get" and ref["arguments"]["view"] == "result"
                  and ref["arguments"]["turn_id"] == latest["turn_id"], "Terminal outcome has no scoped result_ref")
            return latest
        check(not latest or latest["status"] not in ("needs_resume", "uncertain"), "Turn requires explicit recovery")
    raise RuntimeError("Outcome supervision timed out")


async def turn_result(service, reference):
    args, combined, identity = dict(reference["arguments"]), {}, None
    for _ in range(100):
        page = (await dispatch(service, reference["tool"], args))["result"]
        identity = identity or page["result_id"]
        check(page["result_id"] == identity, "Result version changed while paging")
        for key in ("agent_claims", "observed_commands", "reported_changes", "artifacts", "limitations"):
            combined.setdefault(key, []).extend(page[key])
        if page["next_offset"] is None:
            return {**page, **combined}
        check(page["next_offset"] > args.get("offset", 0), "Result pagination did not advance")
        args.update(result_id=identity, offset=page["next_offset"])
    raise RuntimeError("Result pagination did not finish")


async def artifact_bytes(service, task_id, reference):
    data, offset = bytearray(), 0
    check(0 <= reference["size_bytes"] <= ARTIFACT_BYTES, "Artifact exceeds the live-check capture limit")
    for _ in range(ARTIFACT_BYTES // 65536 + 1):
        value = (await dispatch(service, "codex_get", {"task_id": task_id, "view": "artifact",
            "artifact_id": reference["artifact_id"], "offset": offset, "limit": 65536}))["artifact"]
        check(value["offset"] == offset and value["sha256"] == reference["sha256"]
              and value["size_bytes"] == reference["size_bytes"] and value["encoding"] == "base64", "Artifact identity changed while paging")
        data.extend(base64.b64decode(value["data"], validate=True))
        check(len(data) <= reference["size_bytes"], "Artifact returned extra bytes")
        if value["next_offset"] is None:
            check(len(data) == reference["size_bytes"] and hashlib.sha256(data).hexdigest() == reference["sha256"],
                  "Complete artifact size or SHA256 does not match its reference")
            return bytes(data)
        check(value["next_offset"] == len(data) and value["next_offset"] > offset, "Artifact pagination did not advance")
        offset = value["next_offset"]
    raise RuntimeError("Artifact pagination did not finish")


def operator_commands(config_path):
    base = [sys.executable, str(Path(__file__).resolve().parents[1] / "server.py"), "--config", str(Path(config_path).expanduser().absolute())]
    quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
    print("Operator commands (not executed; stop any broker before offline pruning):", flush=True)
    for args in (["stop"], ["backup"], ["prune", "--older-than", "30d"], ["doctor"]):
        print(" ", quote([*base, *args]), flush=True)


async def run(config_path, workdir):
    os.umask(0o077)
    report = {"passed": False, "checks": [], "started_at": time.time()}
    service, store, cfg = None, None, None
    locks = ExitStack()
    try:
        cfg, project = isolated_inputs(config_path, workdir)
        locks.enter_context(isolated_state(cfg))
        store = Store(cfg.state_dir)
        service = Service(cfg, store)
        prefix = "supervision-" + uuid.uuid4().hex
        task = await dispatch(service, "codex_start", {"workdir": str(project), "operation_id": prefix + "-start",
            "prompt": 'Small integration check in a disposable project. Do not delegate or access the network. First call ask_parent once to choose alpha or beta. Use question="Which marker?" and envelope={"version":1,"id":"marker","goal":"Choose a marker","options":[{"id":"alpha","label":"alpha"},{"id":"beta","label":"beta"}],"ttl_seconds":300}. If structured envelopes are unsupported, ask in plain text. Wait for the answer. Use the file-change/apply_patch tool to create only supervision.txt containing the selected marker. Run one short local Python command that reads that file and prints it. No installs, other edits, or long-running commands. Report what happened briefly.'})
        report["task_id"] = task["id"]
        outcome = await follow_outcome(service, task["id"], report)
        report["outcome"] = outcome
        record(report, "PASS", "outcome-only polling returned completion, reusable cursors and a result_ref")
        result = await turn_result(service, outcome["result_ref"])
        check(result["agent_claims"], "No source-linked agent claims in the result")
        check(any(c["reported"].get("exitCode") == 0 and c["source"].get("event_seq") for c in result["observed_commands"]),
              "No observed successful command with source and exit code")
        check(result["reported_changes"] and all(c.get("source", {}).get("event_seq") for c in result["reported_changes"]),
              "No source-linked reported changes")
        check(result["artifacts"], "No scoped artifacts in the result")
        record(report, "PASS", "result includes claims, successful command evidence, reported changes and artifacts")
        reference = next((a for a in result["artifacts"] if a["kind"] == "captured"), result["artifacts"][0])
        await artifact_bytes(service, task["id"], reference)
        record(report, "PASS", "complete artifact bytes match the reference size and SHA256")
        compact = await dispatch(service, "codex_get", {"task_id": task["id"], "view": "compact"})
        check(compact["view"] == "compact" and compact["turn_id"] == outcome["turn_id"]
              and "counts" in compact and "summary" not in compact and "diff" not in compact, "Compact task view is inconsistent")
        record(report, "PASS", "compact task view exposes turn/status/counts without large projections")
        permissions = await dispatch(service, "codex_get", {"task_id": task["id"], "view": "permissions", "limit": 200})
        settings = await dispatch(service, "codex_settings", {"task_id": task["id"]})
        for view in (permissions["permission_view"], settings["permission_view"]):
            check(all(key in view for key in ("requested", "effective", "inherited")) and view["effective"]["verified"],
                  "Settings explanation is absent or its native scope is unverified")
        receipts = permissions["decision_receipts"]
        check(report.get("parent_receipts"), "Codex did not exercise ask_parent")
        check(set(report["parent_receipts"]) <= {r["receipt_id"] for r in receipts if r["decision_source"] == "main_agent"},
              "Parent answer receipts are not retained")
        automatic = [r for r in receipts if r["kind"] == "native_review"]
        check(all(r["decision_source"] == "codex_reviewer" for r in automatic), "Automatic native review receipt has the wrong source")
        current = await dispatch(service, "codex_get", {"task_id": task["id"]})
        observed_review = current.get("last_automatic_review") or {}
        if observed_review.get("method", "").endswith("/completed"):
            check(any(r.get("native_review_id") == observed_review["data"].get("reviewId") for r in automatic),
                  "Observed native automatic review has no retained receipt")
        record(report, "PASS", "permission/settings explanations and retained parent receipt sources agree")
        record(report, "PASS" if automatic else "SKIPPED", "native automatic-review receipt source (only when Codex reviews)")
        if not report.get("typed_decision"):
            record(report, "SKIPPED", "typed decision: Codex did not send an unexpired envelope with options")
        delegated = await dispatch(service, "codex_delegate", {"task_id": task["id"], "operation_id": prefix + "-delegate",
            "prompt": "Trivial delegation: ask one native child to read supervision.txt and report its marker in one sentence. No commands, edits, network, or further delegation. Wait for it and finish.",
            "contract": {"version": 1, "id": "marker-read", "goal": "Read the marker", "constraints": ["No edits, commands or network"],
                         "deliverables": ["One sentence"], "ttl_seconds": 300}})
        contract = delegated["delegation_contract"]
        check(contract["delivery"]["state"] == "acknowledged" and contract["delivery"].get("turn_id")
              and contract["scope"]["operation_id"] == prefix + "-delegate"
              and contract["expires_at"] == contract["created_at"] + 300 and contract["authority"] == "guidance_only",
              "Delegation contract delivery, scope or expiry stamp is missing")
        record(report, "PASS", "idle-task delegation records an acknowledged guidance contract and expiry stamp")
        await follow_outcome(service, task["id"], report)
        history = await dispatch(service, "codex_history", {"task_id": task["id"], "limit": 50})
        check(not history.get("nextCursor"), "Unexpectedly large live-check history")
        before_turns = [turn["id"] for turn in history["data"]]
        await service.close()
        store.close()
        store = Store(cfg.state_dir)
        service = Service(cfg, store)
        resumed = await dispatch(service, "codex_resume", {"task_id": task["id"], "operation_id": prefix + "-resume"})
        recovery = resumed["reconciliation"]
        for operation_id in (prefix + "-start", prefix + "-delegate"):
            observed = next((o for o in recovery["operations"] if o["operation_id"] == operation_id), None)
            check(observed and observed["acknowledgement"] == "acknowledged" and observed["automatic_retry"] is False,
                  "Reconciliation lost an acknowledged operation or allowed automatic retry")
        check(recovery["automatic_retry"] is False and recovery["replay_permitted"] is False, "Recovery permits automatic replay")
        local = (await dispatch(service, "codex_get", {"task_id": task["id"], "view": "reconciliation"}))["reconciliation"]
        check(local["report_revision"] == recovery["report_revision"] and local["automatic_retry"] is False
              and local["replay_permitted"] is False, "Local reconciliation view differs from the resume report")
        check(resumed["turn_id"] == contract["delivery"]["turn_id"], "Restart changed the acknowledged delegated turn")
        history = await dispatch(service, "codex_history", {"task_id": task["id"], "limit": 50})
        check(not history.get("nextCursor") and [turn["id"] for turn in history["data"]] == before_turns,
              "Restart/resume added or replayed a native turn")
        record(report, "PASS", "backend restart/resume preserves acknowledgements and reports no automatic replay")
        report["passed"] = True
    except Exception as exc:
        report["error"] = redact(str(exc))
        record(report, "FAIL", report["error"])
        raise
    finally:
        try:
            try:
                if service is not None:
                    await service.close()
            finally:
                if store is not None:
                    store.close()
            report["finished_at"] = time.time()
            if cfg is not None and service is not None:
                target = cfg.state_dir / "live-supervision-report.json"
                with os.fdopen(private_file(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", encoding="utf-8") as stream:
                    json.dump(redact(report), stream, indent=2)
            operator_commands(config_path)
        finally:
            locks.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.config, args.workdir))
