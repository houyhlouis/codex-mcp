#!/usr/bin/env python3
"""Deterministic app-server double. Never performs inference or shell execution."""

import asyncio
import json
import os
import sys
import uuid
import copy
import threading
import time
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

if "--version" in sys.argv:
    print("codex-cli 0.167.0")
    raise SystemExit(0)

DATA = Path.cwd() / "fake-history.json"
threads = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
loaded = set()
for thread in threads.values():
    thread["background"] = []  # A new app-server cannot list the old process's terminals.
    if not thread.pop("fixture_stale_in_progress", False):
        for turn in thread["turns"]:
            if turn["status"] == "inProgress":
                turn["status"] = "interrupted"
pending = {}
jobs = {}
counter = 1000


def persist():
    # Replace atomically: tests read this file while the fixture is running,
    # and a truncate-then-write could expose an empty file (seen on Windows).
    temporary = DATA.with_name(DATA.name + ".tmp")
    temporary.write_text(json.dumps(threads), encoding="utf-8")
    for _ in range(100):
        try:
            os.replace(temporary, DATA)
            return
        except PermissionError:
            # Windows refuses to replace a file another process is reading.
            time.sleep(0.01)
    os.replace(temporary, DATA)


def send(value):
    print(json.dumps(value), flush=True)


def event(method, params):
    if method in ("item/started", "item/completed"):
        params = {**params, "startedAtMs" if method == "item/started" else "completedAtMs": 0}
    send({"method": method, "params": params})


def view(thread):
    active = thread["id"] in jobs and not jobs[thread["id"]].done() and bool(thread["turns"] and thread["turns"][-1]["status"] == "inProgress")
    return {"id": thread["id"], "cwd": thread["options"]["cwd"], "parentThreadId": thread.get("parent_id"),
            "sessionId": thread.get("session_id", thread["id"]), "canAcceptDirectInput": thread.get("direct", True),
            "source": {"subAgent": {"thread_spawn": {"parent_thread_id": thread["parent_id"], "depth": 1}}} if thread.get("parent_id") else "appServer",
            "agentNickname": "Fixture child" if thread.get("parent_id") else None,
            "status": {"type": "notLoaded"} if thread["id"] not in loaded else ({"type": "active", "activeFlags": []} if active else {"type": "idle"}), "name": thread.get("name"),
            "cliVersion": "0.167.0", "createdAt": 0, "updatedAt": 0, "ephemeral": False,
            "modelProvider": "test", "preview": "", "projectId": None, "turns": [],
            "model": thread["options"].get("model", "test-model"), "reasoningEffort": "medium"}


def configuration(thread):
    p = thread["options"]
    return {"thread": view(thread),
            "cwd": p["cwd"], "runtimeWorkspaceRoots": p["runtimeWorkspaceRoots"],
            "activePermissionProfile": {"id": p["permissions"]},
            "approvalPolicy": p["approvalPolicy"], "approvalsReviewer": p.get("approvalsReviewer", "user"),
            "model": p.get("model", "test-model"), "modelProvider": "test", "sandbox": {"type": "readOnly"}}


async def ask(method, params):
    global counter
    counter += 1
    request_id = counter
    future = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"):
        params = {**params, "startedAtMs": 0}
    send({"id": request_id, "method": method, "params": params})
    try:
        response = await future
        event("serverRequest/resolved", {"threadId": params["threadId"], "requestId": request_id})
        return response
    finally:
        pending.pop(request_id, None)


async def drive(thread, turn, prompt, *, started_turn=None, started=None, start_first=False):
    tid, turn_id = thread["id"], turn["id"]
    common = {"threadId": tid, "turnId": turn_id}
    try:
        if started_turn and not start_first:
            item = {"id": "review-entry", "type": "enteredReviewMode", "review": "fixture review"}
            event("item/started", {**common, "item": item})
            event("item/completed", {**common, "item": item})
        # Deliberately emit events before the turn/start response.
        event("turn/started", {"threadId": tid, "turn": started_turn} if started_turn else {**common, "turn": turn})
        if started is not None:
            started.set()
        if started_turn and start_first:
            item = {"id": "review-entry", "type": "enteredReviewMode", "review": "fixture review"}
            event("item/started", {**common, "item": item})
            event("item/completed", {**common, "item": item})
        event("turn/plan/updated", {**common, "plan": [{"step": "Run fixture", "status": "inProgress"}]})
        event("item/agentMessage/delta", {**common, "itemId": "message", "delta": "Working"})
        event("item/reasoning/textDelta", {**common, "itemId": "reasoning", "contentIndex": 0, "delta": "private reasoning should not be exposed"})
        if prompt == "disconnect":
            os._exit(17)
        elif prompt in ("native_children", "native_grandchildren", "native_outside"):
            child_id = str(uuid.uuid4())
            child = {"id": child_id, "parent_id": tid, "session_id": thread.get("session_id", tid),
                     "options": copy.deepcopy(thread["options"]), "turns": []}
            if prompt == "native_outside":
                child["options"]["cwd"] = str(Path.cwd())
            child_turn = {"id": str(uuid.uuid4()), "rootTurnId": turn.get("rootTurnId") or turn_id,
                          "status": "inProgress", "items": []}
            child["turns"].append(child_turn)
            threads[child_id] = child
            loaded.add(child_id)
            persist()
            jobs[child_id] = asyncio.create_task(drive(child, child_turn, "native_children" if prompt == "native_grandchildren" else "approval"))
            event("thread/started", {"thread": view(child)})
            await jobs[child_id]
        elif prompt in ("hold", "hold_command", "hold_command_background"):
            if prompt.startswith("hold_command"):
                item = {"id": "running-command", "type": "commandExecution", "command": "fixture sleep",
                        "cwd": thread["options"]["cwd"], "status": "inProgress", "commandActions": []}
                event("item/started", {**common, "item": item})
                if prompt.endswith("_background"):
                    thread["background"] = [{"processId": "background", "itemId": "running-command",
                        "command": "fixture sleep", "cwd": thread["options"]["cwd"]}]
                persist()
            await asyncio.Future()
        elif prompt == "supervision":
            await ask("item/tool/call", {**common, "tool": "ask_parent", "callId": "context",
                "arguments": {"question": "Which checks should I run?"}})
            await ask("item/commandExecution/requestApproval", {**common, "itemId": "check",
                "command": "python -m unittest", "cwd": thread["options"]["cwd"],
                "availableDecisions": ["accept", "decline", "cancel"]})
        elif prompt in ("parent", "approval", "file_approval", "question", "permission", "unknown"):
            methods = {"parent": "item/tool/call", "approval": "item/commandExecution/requestApproval",
                       "file_approval": "item/fileChange/requestApproval",
                       "question": "item/tool/requestUserInput", "permission": "item/permissions/requestApproval",
                       "unknown": "future/unknownApproval"}
            extras = {
                "parent": {"tool": "ask_parent", "callId": "c1", "arguments": {"question": "Which test?"}},
                "approval": {"itemId": "c1", "command": "printf fixture", "cwd": thread["options"]["cwd"],
                             "availableDecisions": ["accept", "decline", "cancel"]},
                "file_approval": {"itemId": "patch", "availableDecisions": ["accept", "decline", "cancel"]},
                "question": {"itemId": "q1", "isBlocking": True, "questions": [
                    {"id": "choice", "header": "Choose", "question": "Which option?", "options": None}]},
                "permission": {"itemId": "p1", "cwd": thread["options"]["cwd"],
                               "permissions": {"network": {"enabled": True}}}, "unknown": {}}
            if prompt == "file_approval":
                event("item/started", {**common, "item": {"id": "patch", "type": "fileChange", "status": "inProgress", "changes": [
                    {"path": "source.txt", "kind": {"type": "update", "move_path": "destination.txt"}, "diff": "+ fixture"}]}})
            turn["fixture_response"] = await ask(methods[prompt], {**common, **extras[prompt]})
        await asyncio.sleep(0.025)
        item = {"id": str(uuid.uuid4()), "type": "commandExecution", "command": "python -m unittest",
                "cwd": thread["options"]["cwd"], "status": "completed", "exitCode": 0, "durationMs": 1, "commandActions": []}
        event("item/completed", {**common, "item": item})
        turn["items"].append(item)
        file_item = {"id": "file1", "type": "fileChange", "status": "completed",
                     "changes": [{"path": "sample.py", "kind": {"type": "update"}, "diff": "+ example"}]}
        event("item/completed", {**common, "item": file_item})
        event("turn/diff/updated", {**common, "diff": "+ example"})
        turn["items"].append(file_item)
        item = {"id": "message", "type": "agentMessage", "phase": "final_answer", "text": "Fixture summary: " + prompt}
        event("item/completed", {**common, "item": item})
        turn["items"].append(item)
        turn["status"] = "failed" if prompt == "fail" else "completed"
        if prompt == "fail":
            turn["error"] = {"message": "Fixture failure"}
        persist()
        event("turn/completed", {**common, "turn": turn})
    except asyncio.CancelledError:
        if prompt.startswith("hold_command"):
            item = {"id": "running-command", "type": "commandExecution", "command": "fixture sleep",
                    "cwd": thread["options"]["cwd"], "status": "failed", "exitCode": 130, "durationMs": 1, "commandActions": []}
            turn["items"].append(item)
            event("item/completed", {**common, "item": item})
        turn["status"] = "interrupted"
        persist()
        event("turn/completed", {**common, "turn": turn})
        if prompt.startswith("hold_command"):
            event("thread/status/changed", {"threadId": tid, "status": {"type": "idle"}})


async def main():
    loop = asyncio.get_running_loop()
    incoming = asyncio.Queue()
    transport = None
    def pump():
        while True:
            line = sys.stdin.buffer.readline()
            loop.call_soon_threadsafe(incoming.put_nowait, line)
            if not line:
                return
    if sys.platform == "win32":
        threading.Thread(target=pump, daemon=True).start()
        read_line = incoming.get
    else:
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        read_line = reader.readline
    try:
        while line := await read_line():
            msg = json.loads(line)
            if "method" not in msg:
                future = pending.get(msg.get("id"))
                if future and not future.done():
                    future.set_result(msg.get("result", msg.get("error")))
                continue
            method, p = msg["method"], msg.get("params", {})
            if method == "initialized":
                continue
            result = {}
            if method == "initialize":
                persist()
                result = {"userAgent": "fixture/0.167.0", "codexHome": str(Path.cwd()), "platformFamily": "fixture", "platformOs": "fixture"}
            elif method == "model/list":
                catalog = [{"id": "test-model", "model": "test-model", "isDefault": True,
                            "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [
                                {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}],
                            "serviceTiers": [{"id": "priority", "name": "Fast", "description": "Fixture tier"}]},
                           {"id": "small", "model": "small", "isDefault": False,
                            "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]
                for entry in catalog:
                    entry.update(displayName=entry["id"], description="Offline fixture model", hidden=False)
                    for effort in entry["supportedReasoningEfforts"]:
                        effort["description"] = "Offline fixture effort"
                offset, limit = int(p.get("cursor") or 0), p.get("limit", 100)
                result = {"data": catalog[offset:offset + limit], "nextCursor": str(offset + limit) if offset + limit < 2 else None}
            elif method == "account/read":
                result = {"account": {"type": "chatgpt", "email": "not-exposed@example.invalid",
                                      "planType": "pro", "access_token": "not-exposed"}, "requiresOpenaiAuth": True}
            elif method == "config/read":
                result = {"config": {"mcp_servers": {"unrelated": {"enabled": True}}}, "origins": {}}
            elif method == "account/rateLimits/read":
                result = {"rateLimits": {"primary": {"usedPercent": 10}}}
            elif method == "collaborationMode/list":
                result = {"data": [{"mode": "default", "name": "Default"}, {"mode": "plan", "name": "Plan"}]}
            elif method == "permissionProfile/list":
                result = {"data": []}
            elif method == "skills/list":
                result = {"data": [{"cwd": p["cwds"][0], "skills": [{"name": "fixture-skill", "path": str(Path.cwd() / "SKILL.md"),
                    "enabled": True, "scope": "user", "description": "Fixture skill"}], "errors": []}]}
            elif method == "thread/start":
                assert p["config"]['mcp_servers."unrelated".enabled'] is False
                tid = str(uuid.uuid4())
                threads[tid] = {"id": tid, "options": p, "turns": []}
                loaded.add(tid)
                persist()
                result = configuration(threads[tid])
            elif method == "thread/resume":
                if p["threadId"] not in threads or threads[p["threadId"]].get("deleted"):
                    send({"id": msg["id"], "error": {"code": -32600, "message": "no rollout found for thread id " + p["threadId"]}})
                    continue
                thread = threads[p["threadId"]]
                loaded.add(thread["id"])
                thread["options"].update(p)
                for turn in thread["turns"]:
                    if turn["status"] == "inProgress" and (thread["id"] not in jobs or jobs[thread["id"]].done()):
                        turn["status"] = "interrupted"
                result = configuration(thread)
                persist()
            elif method == "thread/read":
                if p["threadId"] not in threads or threads[p["threadId"]].get("deleted"):
                    send({"id": msg["id"], "error": {"code": -32600, "message": "thread not loaded: " + p["threadId"]}})
                    continue
                result = {"thread": view(threads[p["threadId"]])}
            elif method == "thread/loaded/list":
                data = sorted(loaded)
                offset, limit = int(p.get("cursor") or 0), p.get("limit", 50)
                result = {"data": data[offset:offset + limit], "nextCursor": str(offset + limit) if offset + limit < len(data) else None}
            elif method == "thread/list":
                data = list(threads.values())
                if p.get("ancestorThreadId"):
                    def below(t):
                        while t.get("parent_id"):
                            if t["parent_id"] == p["ancestorThreadId"]:
                                return True
                            t = threads[t["parent_id"]]
                        return False
                    data = [t for t in data if below(t)]
                if p.get("cwd"):
                    data = [t for t in data if t["options"]["cwd"] == p["cwd"]]
                data = [t for t in data if bool(t.get("archived")) == bool(p.get("archived")) and not t.get("deleted")]
                offset, limit = int(p.get("cursor") or 0), p.get("limit", 50)
                result = {"data": [view(t) for t in data[offset:offset + limit]], "nextCursor": str(offset + limit) if offset + limit < len(data) else None}
            elif method == "thread/fork":
                source = threads[p["threadId"]]
                target = copy.deepcopy(source)
                target["id"] = str(uuid.uuid4())
                target.pop("parent_id", None)
                target["options"].update(p)
                if p.get("lastTurnId"):
                    index = next(i for i, t in enumerate(target["turns"]) if t["id"] == p["lastTurnId"])
                    target["turns"] = target["turns"][:index + 1]
                threads[target["id"]] = target
                loaded.add(target["id"])
                persist()
                result = configuration(target)
            elif method == "thread/name/set":
                threads[p["threadId"]]["name"] = p["name"]
                persist()
            elif method in ("thread/settings/update", "turn/settings/update"):
                threads[p["threadId"]]["options"].update(p)
                persist()
                result = {"status": "applied"} if method.startswith("turn/") else {}
            elif method in ("thread/archive", "thread/unarchive", "thread/delete"):
                thread = threads[p["threadId"]]
                if method == "thread/delete":
                    thread["deleted"] = True
                    loaded.discard(thread["id"])
                else:
                    thread["archived"] = method == "thread/archive"
                persist()
                result = {"thread": view(thread)} if method == "thread/unarchive" else {}
            elif method == "thread/revert":
                thread = threads[p["threadId"]]
                index = next(i for i, t in enumerate(thread["turns"]) if t["id"] == p["beforeTurnId"])
                thread["turns"] = thread["turns"][:index]
                persist()
                result = {"thread": view(thread)}
            elif method.startswith("thread/goal/"):
                thread = threads[p["threadId"]]
                if method.endswith("/set"):
                    thread["goal"] = {"status": "active", "createdAt": 0, "updatedAt": 0, "tokensUsed": 0, "timeUsedSeconds": 0,
                                      **thread.get("goal", {}), **p}
                elif method.endswith("/clear"):
                    thread.pop("goal", None)
                persist()
                result = {"cleared": True} if method.endswith("/clear") else {"goal": thread.get("goal")}
            elif method.startswith("thread/backgroundTerminals/"):
                if method.endswith("/list") and p["threadId"] not in loaded:
                    send({"id": msg["id"], "error": {"code": -32600, "message": "thread not found: " + p["threadId"]}})
                    continue
                thread = threads[p["threadId"]]
                if method.endswith("/list"):
                    result = {"data": thread.get("background", []), "nextCursor": None}
                elif method.endswith("/terminate"):
                    result = {"terminated": any(t["processId"] == p["processId"] for t in thread.get("background", []))}
                    thread["background"] = [t for t in thread.get("background", []) if t["processId"] != p.get("processId")]
                    persist()
                else:
                    thread["background"] = []
                    persist()
            elif method.startswith("thread/queue/"):
                thread = threads[p["threadId"]]
                queue = thread.setdefault("queue", [])
                action = method.rsplit("/", 1)[-1]
                if action == "list":
                    result = {"data": queue, "nextCursor": None}
                elif action == "add":
                    record = {"id": str(uuid.uuid4()), "input": p["input"], "clientUserMessageId": p["clientUserMessageId"]}
                    queue.append(record)
                    result = {"queuedSubmission": record}
                elif action in ("update", "delete", "start"):
                    record = next((q for q in queue if not p.get("queuedSubmissionId") or q["id"] == p["queuedSubmissionId"]), None)
                    if action == "update":
                        record["input"] = p["input"]
                        result = {"queuedSubmission": record}
                    else:
                        queue.remove(record)
                        result = {"deleted": True}
                    if action == "start":
                        turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": []}
                        thread["turns"].append(turn)
                        jobs[thread["id"]] = asyncio.create_task(drive(thread, turn, record["input"][0]["text"]))
                        result = {"turn": turn}
                elif action == "reorder":
                    thread["queue"] = [next(q for q in queue if q["id"] == key) for key in p["queuedSubmissionIds"]]
                persist()
            elif method in ("review/start", "thread/compact/start"):
                thread = threads[p["threadId"]]
                turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": []}
                thread["turns"].append(turn)
                started_turn, started = None, None
                prompt = "compact"
                if method == "review/start":
                    # Inline reviews announce a nested Turn; items and completion
                    # belong to its root. The response can precede either event.
                    turn["rootTurnId"] = turn["id"]
                    started_turn = {"id": str(uuid.uuid4()), "rootTurnId": turn["id"], "status": "inProgress", "items": []}
                    instructions = p.get("target", {}).get("instructions")
                    if instructions == "review_parent_root":
                        turn["rootTurnId"] = thread["turns"][-2]["rootTurnId"]
                        started_turn["rootTurnId"] = turn["rootTurnId"]
                    prompt = "hold" if instructions == "hold" else "review"
                    if instructions in ("review_before_ack", "review_early_start"):
                        started = asyncio.Event()
                jobs[thread["id"]] = asyncio.create_task(drive(thread, turn, prompt, started_turn=started_turn, started=started,
                    start_first=method == "review/start" and instructions == "review_early_start"))
                if started is not None:
                    await started.wait()
                result = {"turn": turn, "reviewThreadId": thread["id"]} if method == "review/start" else {}
                persist()
            elif method == "turn/start":
                thread = threads[p["threadId"]]
                turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": []}
                thread["turns"].append(turn)
                persist()
                job = asyncio.create_task(drive(thread, turn, p["input"][0]["text"]))
                jobs[p["threadId"]] = job
                await asyncio.sleep(0.005)
                result = {"turn": {"id": turn["id"], "status": "inProgress", "items": []}}
            elif method == "turn/interrupt":
                jobs[p["threadId"]].cancel()
                await asyncio.sleep(0)
            elif method == "turn/steer":
                turn = threads[p["threadId"]]["turns"][-1]
                assert p["expectedTurnId"] == turn["id"]
                result = {"turnId": turn["id"]}
            elif method.startswith("thread/") and method.endswith("/list"):
                turns = threads[p["threadId"]]["turns"]
                data = turns if method == "thread/turns/list" else [
                    {"turnId": t["id"], "item": i} for t in turns for i in t["items"] if not p.get("turnId") or p["turnId"] == t["id"]]
                if p.get("sortDirection") == "desc":
                    data = list(reversed(data))
                offset, limit = int(p.get("cursor") or 0), p.get("limit", 50)
                result = {"data": data[offset:offset + limit], "nextCursor": str(offset + limit) if offset + limit < len(data) else None}
            else:
                send({"id": msg["id"], "error": {"code": -32601, "message": "Unknown fixture method"}})
                continue
            send({"id": msg["id"], "result": result})
    finally:
        if transport:
            transport.close()
        for job in jobs.values():
            job.cancel()


asyncio.run(main())
