#!/usr/bin/env python3
"""Opt-in live child approval denial plus scoped file/image ingestion test.

Uses real Codex usage in a disposable directory. Never grants an approval.
"""
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_local_mcp.config import load_config
from codex_local_mcp.service import Service
from codex_local_mcp.store import Store
from codex_local_mcp.tools import dispatch
from tests.helpers import write_config, temp_root
from codex_local_mcp.security import private_dir
from codex_local_mcp.platforms import IS_WINDOWS


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + bytes([0, 0, 255]) * 32) * 32)) + chunk(b"IEND", b"")


def check(condition, message):
    if not condition:
        raise RuntimeError(str(message))


async def run():
    os.umask(0o077)
    base = Path(tempfile.mkdtemp(prefix="mcp-child-", dir=temp_root())).resolve()
    private_dir(base)
    project = base / "project"
    project.mkdir()
    (project / "pixel.png").write_bytes(png())
    (project / "fixture.txt").write_text("Fixture marker: CONTEXT_INGESTION_OK\n")
    (base / "state").mkdir(mode=0o700)
    path = base / "config.toml"
    write_config(path, base / "state")
    config = load_config(str(path))
    store = Store(config.state_dir)
    service = Service(config, store)
    report = {"passed": False, "config": str(path), "checks": []}
    print("Live child approval test:", base, flush=True)
    try:
        print_command = "Write-Output CHILD_APPROVAL_TEST" if IS_WINDOWS else "printf CHILD_APPROVAL_TEST"
        task = await dispatch(service, "codex_start", {"workdir": str(project), "request_id": "child-approval",
            "approval_mode": "ask", "images": ["pixel.png"], "context_files": ["fixture.txt"],
            "prompt": f"Controlled integration test. First note the color of the attached image and the marker from the attached reference text. Then spawn exactly one native subagent and instruct it to run exactly {print_command} using the command tool with sandbox_permissions=require_escalated. This intentionally tests approval forwarding; the client will DENY the request. Tell the child not to retry or work around a denial. Wait for the result and report that outcome, the image color and text marker. Do not edit files or use networking."})
        cursor, denied = 0, False
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            page = await service.events(task_id=task["id"], after=cursor, wait_seconds=5)
            cursor = page["cursor"]
            for req in page["pending_requests"]:
                if req["kind"] != "command_approval":
                    raise RuntimeError("Unexpected request kind: " + req["kind"])
                check(req["task_id"] != task["id"], "Expected a child-owned request")
                check(req["root_task_id"] == task["id"], "Child request belongs to another root")
                choices = req["params"].get("availableDecisions", ["decline", "cancel"])
                decision = "decline" if "decline" in choices else "cancel"
                await service.respond(req["id"], {"decision": decision})
                denied = True
                report["denied_request"] = req
                print("Child approval routed to parent and denied", flush=True)
                if decision == "cancel":
                    # Native wait can remain pending when a child turn is
                    # cancelled instead of producing a final answer. The main
                    # agent explicitly interrupts that wait and follows up.
                    parent = service.get(task["id"])
                    if parent["status"] in ("running", "waiting"):
                        await service.interrupt(task["id"], parent["turn_id"])
                        for _ in range(30):
                            page = await service.events(task_id=task["id"], after=cursor, wait_seconds=1)
                            cursor = page["cursor"]
                            if service.get(task["id"])["status"] == "interrupted":
                                break
                        check(service.get(task["id"])["status"] == "interrupted", "Parent interruption did not complete")
                        await service.send(task["id"], "The client intentionally denied the child command and cancelled the parent wait. Do not restart the child or execute the command. Based on the existing conversation, report the denied outcome, image color, and attached text marker without using tools.")
                        report["parent_wait_recovery"] = "explicit interrupt and follow-up"
            current = service.get(task["id"])
            if current["status"] in ("completed", "failed", "interrupted", "needs_resume"):
                report["task"] = current
                check(current["status"] == "completed", current)
                check(denied, "No native child approval request observed")
                check("CONTEXT_INGESTION_OK" in current["summary"], current["summary"])
                check("blue" in current["summary"].lower(), current["summary"])
                report["checks"] = ["native child approval ownership and parent polling", "denial forwarded without approval bypass", "native image snapshot ingestion", "native context-file ingestion"]
                report["passed"] = True
                print("Child approval and rich-input tests passed", flush=True)
                break
        else:
            raise RuntimeError("Live child test timed out")
    finally:
        await service.close()
        (config.state_dir / "child-approval-report.json").write_text(json.dumps(service.safe_payload(report), indent=2))
        store.close()


if __name__ == "__main__":
    asyncio.run(run())
