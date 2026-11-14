"""Record modern adapter frames over the ordinary private fake-backed service."""
import asyncio
import json
import re

from codex_local_mcp.contract import MCP_MODERN_VERSION, MCP_TASKS_EXTENSION
from codex_local_mcp.mcp_modern import META_VERSION, META_CAPABILITIES, has_tasks, has_form
from codex_local_mcp.tools import dispatch
from tests.helpers import normalise_bridge_version


def normalise_modern(value, base):
    identifiers = {}
    def replace(item, key=""):
        if isinstance(item, dict):
            result = {replace(name) if re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{64}", name) else name: replace(child, name)
                for name, child in item.items()}
            if key == "family_usage":
                for field in ("active_seconds", "known_active_seconds"):
                    if type(result.get(field)) in (int, float):
                        result[field] = 1
                if isinstance(result.get("output_bytes"), dict):
                    stored = result["output_bytes"]
                    for field in ("journal", "artifacts"):
                        stored[field] = int(bool(stored.get(field)))
                    stored["total"] = stored["journal"] + stored["artifacts"]
            if "content_bytes" in result and isinstance(result.get("params"), dict):
                # Request sizes include timestamps and the temporary directory.
                # Recompute from canonical content rather than pinning its raw size.
                derived = {"content_bytes", "proposal_bytes", "preview", "preview_truncated",
                    "details_truncated", "detail_tool", "pending_request_id", "request_id_kind"}
                source = {name: child for name, child in result.items() if name not in derived}
                result["content_bytes"] = len(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            return result
        if isinstance(item, list):
            return [replace(child, key) for child in item]
        if (key in ("at", "event_cursor", "revision", "proposal_revision", "report_revision") or key.endswith("_at")) and type(item) in (int, float):
            return 1
        if key in ("createdAt", "lastUpdatedAt") and isinstance(item, str):
            return "2000-01-01T00:00:00.000Z"
        if key == "ttlMs" and item is not None and item > 0:
            return 86400000
        if isinstance(item, str):
            if item.startswith("Codex requests human review. Nothing is approved by default.\n"):
                prefix, data = item.rsplit("\n", 1)
                if data.startswith("{"):
                    return prefix + "\n" + json.dumps(replace(json.loads(data)), ensure_ascii=False)
            if item.startswith("{"):
                try:
                    return json.dumps(replace(json.loads(item)), ensure_ascii=False)
                except ValueError:
                    pass
            item = item.replace(str(base), "/fixture")
            item = re.sub(r"local_mcp_(read|write)_[a-f0-9]{12}", r"local_mcp_\1_fixture", item)
            def identity(match):
                original = match[0]
                if original not in identifiers:
                    count = len(identifiers) + 1
                    identifiers[original] = (f"00000000-0000-4000-8000-{count:012d}" if "-" in original else f"{count:0{len(original)}x}")
                return identifiers[original]
            return re.sub(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|\b[a-f0-9]{64}\b|\b[a-f0-9]{32}\b", identity, item)
        return item
    return normalise_bridge_version(replace(value))


async def capture_modern_wire(capabilities=None):
    from tests.test_mcp_modern import ModernMCPTest, FORM_CAPS
    case = ModernMCPTest()
    await case.asyncSetUp()
    capabilities = FORM_CAPS if capabilities is None else capabilities
    async def request(identifier, method, params=None, caps=None):
        frame = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": {**(params or {}),
            "_meta": {META_VERSION: MCP_MODERN_VERSION, META_CAPABILITIES: capabilities if caps is None else caps}}}
        case.adapter.frames.append({"direction": "inbound", "message": frame})
        await asyncio.wait_for(case.adapter.handle(frame), 10)
        return case.adapter.frames[-1]["message"]
    try:
        await request("discover", "server/discover")
        await request("tools", "tools/list")
        await request("call", "tools/call", {"name": "codex_projects", "arguments": {"action": "list"}})
        await request("unsupported", "tasks/list", {})
        project = case.root / "wire"
        case.service.projects("create", str(project))
        created = await request("create", "tools/call", {"name": "codex_start", "arguments": {
            "workdir": str(project), "prompt": "approval", "operation_id": "wire-modern-start"}})
        task = case.store.all("tasks")[0]
        await case.until(task["id"], ("waiting",))
        pending_id = case.service.requests(task["id"])[0]["id"]
        await request("review-no-form", "tools/call", {"name": "codex_review_request", "arguments": {"pending_request_id": pending_id}}, {})
        if has_tasks(capabilities):
            task_id = created["result"]["taskId"]
            await request("missing-capability", "tasks/get", {"taskId": task_id}, {})
            pending = await request("pending", "tasks/get", {"taskId": task_id})
            key = next(iter(pending["result"]["inputRequests"])) if has_form(capabilities) else pending["result"]["_meta"]["codex-local-mcp/pendingRequests"][0]["input_key"]
            await request("input", "tasks/update", {"taskId": task_id, "inputResponses": {key: {"action": "accept", "content": {"decision": "decline"}}}})
            await case.until(task["id"], ("completed",))
            await request("complete", "tasks/get", {"taskId": task_id})
            follow_up = await request("follow-up", "tools/call", {"name": "codex_send", "arguments": {
                "task_id": task["id"], "prompt": "hold", "operation_id": "wire-modern-follow-up"}})
            gate = case.gate_callback("wire-terminal", "turn/completed")
            await request("cancel", "tasks/cancel", {"taskId": follow_up["result"]["taskId"]})
            await gate.wait()
            await request("cancel-pending", "tasks/get", {"taskId": follow_up["result"]["taskId"]})
            gate.release.set()
            await case.until(task["id"], ("interrupted",))
            await request("cancelled", "tasks/get", {"taskId": follow_up["result"]["taskId"]})
        else:
            await dispatch(case.service, "codex_respond", {"pending_request_id": pending_id, "response": {"decision": "decline"}})
            await case.until(task["id"], ("completed",))
        # The core MRTR path uses the same form policy and one-shot binding.
        await dispatch(case.service, "codex_send", {"task_id": task["id"], "prompt": "approval", "operation_id": "wire-modern-review"})
        await case.until(task["id"], ("waiting",))
        pending_id = case.service.requests(task["id"])[0]["id"]
        params = {"name": "codex_review_request", "arguments": {"pending_request_id": pending_id}}
        review = await request("review", "tools/call", params)
        if has_form(capabilities):
            key = next(iter(review["result"]["inputRequests"]))
            await request("review-response", "tools/call", {**params, "requestState": review["result"]["requestState"],
                "inputResponses": {key: {"action": "accept", "content": {"decision": "decline"}}}})
            await case.until(task["id"], ("completed",))
        return normalise_modern({"format": 1, "protocol_version": MCP_MODERN_VERSION, "client_capabilities": capabilities,
            "source": "Modern MCP adapter frames over ordinary private fake Codex subprocess flows",
            "normalisation": "Temporary paths, identifiers, timestamps, revisions and derived request byte counts use canonical values",
            "messages": case.adapter.frames}, case.base)
    finally:
        await case.asyncTearDown()
