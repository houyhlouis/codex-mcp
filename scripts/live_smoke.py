#!/usr/bin/env python3
"""Opt-in live test: uses the signed-in Codex account and consumes real usage.

Pass an empty, disposable project directory with --workdir. The test creates
calculator.py and its unit test there. Never run it against a real project.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_local_mcp.config import load_config
from codex_local_mcp.security import private_dir
from codex_local_mcp.service import Service
from codex_local_mcp.store import Store
from codex_local_mcp.tools import dispatch


async def run(config_path, workdir):
    os.umask(0o077)
    cfg = load_config(config_path)
    project = Path(workdir).expanduser().resolve()
    if any(project.iterdir()):
        raise RuntimeError("Live smoke requires an empty workspace root")
    private_dir(cfg.state_dir)
    store = Store(cfg.state_dir)
    service = Service(cfg, store)
    report = {"checks": [], "started_at": time.time()}
    try:
        report["status"] = await service.status()
        print("Codex connected:", report["status"]["codex_version"], flush=True)
        task = await dispatch(service, "codex_start", {"workdir": str(project), "request_id": "live-start",
            "prompt": "This is a small integration test in a disposable project. First call ask_parent to ask which function and test to implement. Wait for its answer. Then implement only the requested files, run the tests, and report the result. Do not delegate to any additional agents."})
        report["task_id"] = task["id"]
        report["thread_id"] = task["thread_id"]
        deadline = time.monotonic() + 240
        answered = False
        cursor = 0
        while time.monotonic() < deadline:
            page = await service.events(after=cursor, task_id=task["id"], wait_seconds=10)
            cursor = page["cursor"]
            for req in page["pending_requests"]:
                print("Pending request:", req["kind"], flush=True)
                if req["kind"] == "parent_question":
                    await service.respond(req["id"], {"text": "Create calculator.py containing add(a, b) that returns a + b. Create test_calculator.py using unittest with positive, zero and negative cases. Run python3 -m unittest -v. Do not install dependencies or access other projects."})
                    answered = True
                else:
                    report.setdefault("approval_requests", []).append(req)
                    # Smoke never escalates permissions or fabricates human approval.
                    if req["kind"] in ("command_approval", "file_approval"):
                        choices = req["params"].get("availableDecisions", ["decline", "cancel"])
                        await service.respond(req["id"], {"decision": "decline" if "decline" in choices else "cancel"})
                    elif req["kind"] == "permissions":
                        await service.respond(req["id"], {"permissions": {}})
                    else:
                        raise RuntimeError("Human input required; live smoke will not answer it automatically")
            current = service.get(task["id"])
            if current["status"] in ("completed", "failed", "interrupted", "needs_resume"):
                report["first_turn"] = current
                break
        else:
            raise RuntimeError("Live turn timed out")
        assert current["status"] == "completed", current
        assert answered, "Codex did not exercise ask_parent"
        assert (project / "calculator.py").exists() and (project / "test_calculator.py").exists()
        assert any(c["exitCode"] == 0 and "unittest" in (c["command"] or "") for c in current["commands"]), current["commands"]
        report["checks"].extend(["model discovery", "named project permissions", "live parent question/reply", "coding turn", "unit test command evidence"])
        follow = await dispatch(service, "codex_send", {"task_id": task["id"], "request_id": "live-followup",
            "prompt": "Without running commands or editing files, state the function and the test cases you just implemented, based on this conversation."})
        assert follow["thread_id"] == task["thread_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            page = await service.events(after=cursor, task_id=task["id"], wait_seconds=10)
            cursor = page["cursor"]
            current = service.get(task["id"])
            if current["status"] in ("completed", "failed", "interrupted"):
                break
        assert current["status"] == "completed", current
        report["followup"] = current
        report["checks"].append("persistent follow-up")
        await service.close()
        store.close()
        store = Store(cfg.state_dir)
        service = Service(cfg, store)
        await service.resume(task["id"])
        history = await service.history(task["id"], limit=10)
        assert len(history["data"]) >= 2
        report["checks"].extend(["backend restart and resume", "persisted history retrieval", "durable event replay"])
        report["history_turns"] = len(history["data"])
        report["passed"] = True
        print("Live checks passed:", ", ".join(report["checks"]), flush=True)
    except Exception as exc:
        report["error"] = str(exc)
        print("Live check failed:", str(exc)[:2000], flush=True)
        raise
    finally:
        await service.close()
        report["finished_at"] = time.time()
        (cfg.state_dir / "live-report.json").write_text(json.dumps(report, indent=2))
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.config, args.workdir))
