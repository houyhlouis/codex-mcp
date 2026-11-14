#!/usr/bin/env python3
"""Opt-in live native subagent, automatic review and CLI-control checks.

Creates a disposable temporary project/config/state and uses the installed
Codex account. Consumes model usage. Only its own fork is deleted; no files,
credentials, global policy or existing conversations are changed.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_local_mcp.config import load_config
from codex_local_mcp.service import Service
from codex_local_mcp.store import Store
from codex_local_mcp.tools import dispatch
from tests.helpers import write_config, temp_root
from codex_local_mcp.security import private_dir
from codex_local_mcp.platforms import IS_WINDOWS


def check(condition, message):
    if not condition:
        raise RuntimeError(str(message))


async def run():
    os.umask(0o077)
    base = Path(tempfile.mkdtemp(prefix="mcp-native-", dir=temp_root())).resolve()
    private_dir(base)
    (base / "project").mkdir()
    (base / "state").mkdir(mode=0o700)
    file = base / "config.toml"
    write_config(file, base / "state")
    cfg = load_config(str(file))
    store = Store(cfg.state_dir)
    service = Service(cfg, store)
    report = {"passed": False, "checks": [], "config": str(file)}
    print("Live test directory:", base, flush=True)

    async def done(task_id, seconds=180):
        cursor = 0
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            page = await service.events(task_id=task_id, after=cursor, wait_seconds=5)
            cursor = page["cursor"]
            for event in page["events"]:
                if event["kind"].startswith("bridge/subagent") or "autoApprovalReview" in event["kind"]:
                    print("Event:", event["kind"], flush=True)
            for req in page["pending_requests"]:
                if req["kind"] == "parent_question":
                    await service.respond(req["id"], {"text": "Follow the explicit controlled test instructions; no external access or file changes."})
                else:
                    raise RuntimeError("Unexpected human request: " + req["kind"])
            result = service.get(task_id)
            if result["status"] in ("completed", "failed", "interrupted", "needs_resume"):
                if result["status"] != "completed":
                    raise RuntimeError(json.dumps(result))
                return result
        raise RuntimeError("Live test turn timed out")

    try:
        report["status"] = await service.status()
        task = await dispatch(service, "codex_start", {"workdir": str(base / "project"), "request_id": "native-start",
            "approval_mode": "approve_for_me", "name": "Native controls integration test",
            "prompt": "Controlled integration test. Spawn exactly one native Codex subagent. Ask it to count ordinary files in the current project, without editing anything, networking, or requesting extra permissions. Wait for its answer and report the count. Actually use your native subagent tool; do not merely describe delegation."})
        report["root_task"] = await done(task["id"])
        children = await service.subagents(task["id"])
        check(children["subagents"], "No verified native subagent was discovered")
        report["children"] = children
        check(report["root_task"]["approvals_reviewer"] == "auto_review", "Native automatic reviewer was not selected")
        report["checks"].extend(["native subagent discovery/subscription", "child results and parent event aggregation", "native auto_review selection"])
        print("Native subagent check passed", flush=True)

        print_command = "Write-Output AUTO_REVIEW_OK" if IS_WINDOWS else "printf AUTO_REVIEW_OK"
        await service.send(task["id"], f"Controlled automatic-review test. Use the command tool with sandbox_permissions=require_escalated to run exactly: {print_command}. This intentionally tests the native reviewer with a harmless print. Do not edit files, use networking or delegate. If the reviewer denies the request, report the denial and stop; do not work around it.")
        reviewed = await done(task["id"])
        report["automatic_review"] = reviewed.get("last_automatic_review")
        check(report["automatic_review"], "No native automatic-review event observed")
        report["checks"].append("native automatic reviewer event/result")

        await service.settings(task["id"], "next", approval_mode="ask", collaboration_mode="plan")
        await service.send(task["id"], "Give a three-step implementation plan for an add(a,b) function and unit tests. Do not run tools, edit files, delegate or ask questions.")
        await done(task["id"])
        report["checks"].append("native plan mode and reviewer changes")
        await service.settings(task["id"], "next", collaboration_mode="default")
        fork = await service.fork(task["id"], "Disposable fork")
        await service.rename(fork["id"], "Renamed disposable fork")
        queued = await service.queue(fork["id"], "add", "Reply QUEUE_OK without using tools or asking questions.")
        queued_id = queued["queuedSubmission"]["id"]
        remaining = (await service.queue(fork["id"]))["data"]
        if any(q["id"] == queued_id for q in remaining):
            try:
                await service.queue(fork["id"], "update", "Reply QUEUE_UPDATED without tools or questions.", queued_id)
                await service.queue(fork["id"], "start", queued_id=queued_id)
            except Exception as exc:
                if "no longer pending" not in str(exc):
                    raise
        await done(fork["id"])
        report["checks"].extend(["native fork and rename", "native queue add/list and consumption"])
        if "--characterize-goal-continuation" in sys.argv:
            print("Opt-in goal timing check uses REAL MODEL QUOTA (2000-token goal budget).", flush=True)
            observations, started = [], time.monotonic()
            cursor = store.highwater()
            try:
                await service.goal(fork["id"], "set", objective=(
                    "Controlled goal timing check. Reply GOAL_TIMING_OK and complete this goal. "
                    "Do not use tools, edit files, delegate, or access the network."),
                    status="active", token_budget=2000, user_requested=True)
                while time.monotonic() - started < 30:
                    page = await service.events(task_id=fork["id"], after=cursor, wait_seconds=1)
                    cursor = page["cursor"]
                    observations.extend({"elapsed": time.monotonic() - started,
                        "kind": e["kind"], "turn_id": e["data"].get("turnId") or e["data"].get("turn", {}).get("id")}
                        for e in page["events"] if e["kind"] in ("turn/started", "turn/completed"))
                    observed_goal = (await service.goal(fork["id"]))["goal"]
                    if observed_goal is None or observed_goal.get("status") != "active":
                        break
            finally:
                await service.goal(fork["id"], "clear", user_requested=True)
                report["goal_timing"] = {"observations": observations,
                    "observation_seconds": time.monotonic() - started,
                    "limit": "An empty observation window does not prove continuation is disabled"}
        await service.goal(fork["id"], "set", objective="Paused integration-test goal", status="paused", token_budget=2000, user_requested=True)
        check((await service.goal(fork["id"]))["goal"], "No paused native goal returned")
        await service.goal(fork["id"], "clear", user_requested=True)
        report["checks"].append("native paused goal set/get/clear")
        report["capabilities"] = await service.capabilities(fork["id"])
        report["skills_count"] = len((await service.skills(fork["id"]))["data"])
        report["terminals"] = await service.terminals(fork["id"])
        report["usage"] = await service.usage()
        report["checks"].append("capabilities, skills, terminals and rate-limit inspection")
        await service.review(fork["id"], "custom", instructions="This project is empty. Review only the current project for code defects; do not modify files. If no source files exist, report that there is nothing to review. Do not delegate.")
        await done(fork["id"])
        report["checks"].append("native code review")
        await service.compact(fork["id"])
        await done(fork["id"])
        report["checks"].append("native compaction")
        history = await service.history(fork["id"], limit=100)
        await service.revert(fork["id"], history["data"][-1]["id"], True)
        report["checks"].append("native history revert")
        await service.archive(fork["id"])
        await service.archive(fork["id"], "unarchive")
        await service.delete(fork["id"], fork["thread_id"])
        report["checks"].append("native archive/restore/delete of test fork")
        await service.close()
        store.close()
        store = Store(cfg.state_dir)
        service = Service(cfg, store)
        await service.resume(task["id"])
        recovered = await service.subagents(task["id"])
        check({t["id"] for t in recovered["subagents"]} == {t["id"] for t in children["subagents"]}, "Recovered child IDs differ")
        report["checks"].append("native child identity and subscriptions restored after restart")
        report["passed"] = True
        print("Extended checks passed:", ", ".join(report["checks"]), flush=True)
    except Exception as exc:
        report["error"] = str(exc)
        print("Live check failed:", str(exc)[:2000], flush=True)
        raise
    finally:
        report["backend_diagnostics"] = list(service.backend.stderr)
        await service.close()
        (cfg.state_dir / "extended-report.json").write_text(json.dumps(service.safe_payload(report), indent=2))
        store.close()


if __name__ == "__main__":
    asyncio.run(run())
