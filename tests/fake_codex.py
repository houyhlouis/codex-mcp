#!/usr/bin/env python3
"""Deterministic app-server double. Never performs inference or shell execution."""

import asyncio
import json
import os
import sys
import uuid
import copy
import threading
from pathlib import Path

for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

if "--version" in sys.argv:
    print("codex-cli 0.167.0")
    raise SystemExit(0)

DATA = Path.cwd() / "fake-history.json"
threads = json.loads(DATA.read_text()) if DATA.exists() else {}
pending = {}
jobs = {}
counter = 1000


def persist():
    DATA.write_text(json.dumps(threads))


def send(value):
    print(json.dumps(value), flush=True)


def event(method, params):
    send({"method": method, "params": params})


def view(thread):
    active = thread["id"] in jobs and not jobs[thread["id"]].done()
    return {"id": thread["id"], "cwd": thread["options"]["cwd"], "parentThreadId": thread.get("parent_id"),
            "sessionId": thread.get("session_id", thread["id"]), "canAcceptDirectInput": thread.get("direct", True),
            "source": {"subAgent": {"thread_spawn": {"parent_thread_id": thread["parent_id"], "depth": 1}}} if thread.get("parent_id") else "appServer",
            "agentNickname": "Fixture child" if thread.get("parent_id") else None,
            "status": {"type": "active" if active else "idle"}, "name": thread.get("name"),
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
    future = asyncio.get_running_loop().create_future()
    pending[counter] = future
    send({"id": counter, "method": method, "params": params})
    try:
        response = await future
        event("serverRequest/resolved", {"threadId": params["threadId"], "requestId": counter})
        return response
    finally:
        pending.pop(counter, None)


async def drive(thread, turn, prompt):
    tid, turn_id = thread["id"], turn["id"]
    common = {"threadId": tid, "turnId": turn_id}
    try:
        # Deliberately emit events before the turn/start response.
        event("turn/started", {**common, "turn": turn})
        event("turn/plan/updated", {**common, "plan": [{"step": "Run fixture", "status": "inProgress"}]})
        event("item/agentMessage/delta", {**common, "itemId": "message", "delta": "Working"})
        event("item/reasoning/textDelta", {**common, "delta": "private reasoning should not be exposed"})
        if prompt == "disconnect":
            os._exit(17)
        elif prompt in ("native_children", "native_grandchildren", "native_outside"):
            child_id = str(uuid.uuid4())
            child = {"id": child_id, "parent_id": tid, "session_id": thread.get("session_id", tid),
                     "options": copy.deepcopy(thread["options"]), "turns": []}
            if prompt == "native_outside":
                child["options"]["cwd"] = str(Path.cwd())
            child_turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": []}
            child["turns"].append(child_turn)
            threads[child_id] = child
            persist()
            jobs[child_id] = asyncio.create_task(drive(child, child_turn, "native_children" if prompt == "native_grandchildren" else "approval"))
            event("thread/started", {"thread": view(child)})
            await jobs[child_id]
        elif prompt == "hold":
            await asyncio.sleep(120)
        elif prompt in ("parent", "approval", "question", "permission", "unknown"):
            methods = {"parent": "item/tool/call", "approval": "item/commandExecution/requestApproval",
                       "question": "item/tool/requestUserInput", "permission": "item/permissions/requestApproval",
                       "unknown": "future/unknownApproval"}
            extras = {
                "parent": {"tool": "ask_parent", "callId": "c1", "arguments": {"question": "Which test?"}},
                "approval": {"itemId": "c1", "command": "printf fixture", "cwd": thread["options"]["cwd"],
                             "availableDecisions": ["accept", "decline", "cancel"]},
                "question": {"itemId": "q1", "isBlocking": True, "questions": [
                    {"id": "choice", "header": "Choose", "question": "Which option?", "options": None}]},
                "permission": {"itemId": "p1", "cwd": thread["options"]["cwd"],
                               "permissions": {"network": {"enabled": True}}}, "unknown": {}}
            turn["fixture_response"] = await ask(methods[prompt], {**common, **extras[prompt]})
        await asyncio.sleep(0.025)
        item = {"id": str(uuid.uuid4()), "type": "commandExecution", "command": "python -m unittest",
                "cwd": thread["options"]["cwd"], "status": "completed", "exitCode": 0, "durationMs": 1}
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
        turn["status"] = "interrupted"
        persist()
        event("turn/completed", {**common, "turn": turn})


async def main():
    loop = asyncio.get_running_loop()
    incoming = asyncio.Queue()
    def pump():
        while True:
            line = sys.stdin.buffer.readline()
            loop.call_soon_threadsafe(incoming.put_nowait, line)
            if not line:
                return
    threading.Thread(target=pump, daemon=True).start()
    try:
        while line := await incoming.get():
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
                result = {"userAgent": "fixture/0.167.0"}
            elif method == "model/list":
                catalog = [{"id": "test-model", "model": "test-model", "isDefault": True,
                            "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [
                                {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}],
                            "serviceTiers": [{"id": "priority", "name": "Fast", "description": "Fixture tier"}]},
                           {"id": "small", "model": "small", "isDefault": False,
                            "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]
                offset, limit = int(p.get("cursor") or 0), p.get("limit", 100)
                result = {"data": catalog[offset:offset + limit], "nextCursor": str(offset + limit) if offset + limit < 2 else None}
            elif method == "account/read":
                result = {"account": {"type": "chatgpt", "email": "not-exposed@example.invalid",
                                      "access_token": "not-exposed"}, "requiresOpenaiAuth": True}
            elif method == "config/read":
                result = {"config": {"mcp_servers": {"unrelated": {"enabled": True}}}}
            elif method == "account/rateLimits/read":
                result = {"rateLimits": {"primary": {"usedPercent": 10}}}
            elif method == "collaborationMode/list":
                result = {"data": [{"mode": "default"}, {"mode": "plan"}]}
            elif method == "permissionProfile/list":
                result = {"data": []}
            elif method == "skills/list":
                result = {"data": [{"cwd": p["cwds"][0], "skills": [{"name": "fixture-skill", "path": "/tmp/SKILL.md", "enabled": True}], "errors": []}]}
            elif method == "thread/start":
                assert p["config"]['mcp_servers."unrelated".enabled'] is False
                tid = str(uuid.uuid4())
                threads[tid] = {"id": tid, "options": p, "turns": []}
                persist()
                result = configuration(threads[tid])
            elif method == "thread/resume":
                thread = threads[p["threadId"]]
                thread["options"].update(p)
                for turn in thread["turns"]:
                    if turn["status"] == "inProgress" and (thread["id"] not in jobs or jobs[thread["id"]].done()):
                        turn["status"] = "interrupted"
                result = configuration(thread)
                persist()
            elif method == "thread/read":
                result = {"thread": view(threads[p["threadId"]])}
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
                    thread["goal"] = {**thread.get("goal", {}), **p}
                elif method.endswith("/clear"):
                    thread.pop("goal", None)
                persist()
                result = {"goal": thread.get("goal")}
            elif method.startswith("thread/backgroundTerminals/"):
                result = {"data": [], "nextCursor": None} if method.endswith("/list") else {}
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
                    else:
                        queue.remove(record)
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
                jobs[thread["id"]] = asyncio.create_task(drive(thread, turn, "review" if method == "review/start" else "compact"))
                result = {"turn": turn} if method == "review/start" else {}
                persist()
            elif method == "turn/start":
                thread = threads[p["threadId"]]
                turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": []}
                thread["turns"].append(turn)
                persist()
                job = asyncio.create_task(drive(thread, turn, p["input"][0]["text"]))
                jobs[p["threadId"]] = job
                await asyncio.sleep(0.005)
                result = {"turn": {"id": turn["id"], "status": "inProgress"}}
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
        for job in jobs.values():
            job.cancel()


asyncio.run(main())
