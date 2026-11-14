"""MCP execution handles over existing operations/turns; no parallel executor."""
import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from . import decisions
from .lifecycle import TERMINAL
from .security import BridgeError, redact
from .tools import arguments, dispatch, result_envelope, error_envelope, tool_result, encoded_size, RESULT_BUDGET, request_summary

PREFIX = "mcp_execution:"
EXECUTION_LIMIT, TERMINAL_AGE_SECONDS, POLL_INTERVAL_MS, INPUT_LIMIT = 1024, 86400, 1000, 32
REVIEW_AGE_SECONDS, INPUT_HISTORY_LIMIT, RECORD_BYTES = 300, 128, 65536
FINAL = frozenset({"completed", "cancelled", "failed"})
TURN_TO_TASK = {"completed": "completed", "failed": "completed", "interrupted": "cancelled"}
TASK_TOOLS = frozenset({"codex_start", "codex_send", "codex_review", "codex_compact", "codex_delegate"})
FIELDS = frozenset({"id", "state", "revision", "created_at", "updated_at", "terminal_at", "operation_id", "tool_name",
    "bridge_task_id", "thread_id", "turn_id", "generation", "cancel_requested", "native_status", "inputs", "expires_at"})


def _decode(raw):
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) - FIELDS or not re.fullmatch(r"[a-f0-9]{32}", value.get("id", ""))
                or value.get("state") not in {"working", "input_required", *FINAL}
                or type(value.get("revision")) is not int or value["revision"] < 1
                or not all(type(value.get(key)) in (int, float) and math.isfinite(value[key]) for key in ("created_at", "updated_at"))
                or (value["state"] in FINAL and (type(value.get("terminal_at")) not in (int, float) or not math.isfinite(value["terminal_at"])))
                or not isinstance(value.get("inputs"), dict) or len(value["inputs"]) > INPUT_HISTORY_LIMIT
                or len(raw.encode()) > RECORD_BYTES):
            raise ValueError("Invalid execution")
        return value
    except (ValueError, TypeError) as exc:
        raise BridgeError("Stored MCP execution is invalid", code="state_invalid") from exc


def read_execution(store, execution_id, *, now=None):
    if not isinstance(execution_id, str) or not re.fullmatch(r"[a-f0-9]{32}", execution_id):
        raise BridgeError("MCP task not found or expired", code="not_found")
    row = store.db.execute("SELECT value FROM metadata WHERE key=?", (PREFIX + execution_id,)).fetchone()
    value = _decode(row[0]) if row else None
    if value is not None and value["id"] != execution_id:
        raise BridgeError("Stored MCP execution identity changed", code="state_invalid")
    clock = time.time() if now is None else now
    if value is None or _expired(value, clock):
        raise BridgeError("MCP task not found or expired", code="not_found", ids={"mcp_task_id": execution_id})
    return value


def _expired(value, clock):
    return ((value["state"] in FINAL and clock >= value["terminal_at"] + TERMINAL_AGE_SECONDS)
            or ("expires_at" in value and clock >= value["expires_at"]))


def _expiry_clock(now):
    try:
        if type(now) in (int, float) and math.isfinite(now):
            return now
    except OverflowError:
        pass
    raise ValueError("Execution expiry time must be finite")


def _expired_metadata(key, raw, now):
    # A future or damaged layout is not ours to delete during offline cleanup.
    if not isinstance(key, str) or not re.fullmatch(PREFIX + r"[a-f0-9]{32}", key) or not isinstance(raw, str):
        return False
    try:
        if len(raw.encode()) > RECORD_BYTES:
            return False
        value = _decode(raw)
        if key != PREFIX + value["id"] or value.get("tool_name") not in TASK_TOOLS | {"codex_review_request"}:
            return False
        if (not all(isinstance(value.get(field), str) and value[field] for field in ("operation_id", "bridge_task_id", "thread_id"))
                or type(value.get("generation")) not in (str, int) or value["generation"] == ""
                or type(value.get("cancel_requested")) is not bool
                or not all(isinstance(binding, dict) for binding in value["inputs"].values())):
            return False
        if not (isinstance(value.get("turn_id"), str) and value["turn_id"]):
            if value["tool_name"] != "codex_review_request" or "turn_id" not in value or value["turn_id"] is not None:
                return False
        if "expires_at" in value:
            if (value["tool_name"] != "codex_review_request" or type(value["expires_at"]) not in (int, float)
                    or not math.isfinite(value["expires_at"])):
                return False
        elif value["tool_name"] == "codex_review_request":
            return False
        return _expired(value, now)
    except (BridgeError, ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return False


def expired_execution_keys(store, now: float) -> dict[str, str]:
    """Read-only key -> SHA-256 snapshot of expired execution metadata, in key order.

    Persist the returned mapping with a prune plan. Unknown or malformed records
    are retained; expired forms within a live task do not expire that task.
    """
    clock = _expiry_clock(now)
    return {row["key"]: hashlib.sha256(row["value"].encode()).hexdigest()
            for row in store.db.execute("SELECT key,value FROM metadata WHERE key GLOB 'mcp_execution:*' ORDER BY key")
            if _expired_metadata(row["key"], row["value"], clock)}


def delete_expired_execution_keys(store, keys: Sequence[str], now: float, *, expected: Mapping[str, str]) -> list[str]:
    """Delete only selected, unchanged, still-expired rows in the caller's transaction.

    `expected` is the persisted snapshot from expired_execution_keys. This function
    neither begins nor commits a transaction; rollback remains the caller's choice.
    """
    clock = _expiry_clock(now)
    if not store.db.in_transaction:
        raise ValueError("Execution expiry deletion requires the caller's transaction")
    deleted = []
    for key in dict.fromkeys(keys):
        row = store.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if (row and _expired_metadata(key, row[0], clock)
                and expected.get(key) == hashlib.sha256(row[0].encode()).hexdigest()):
            if store.db.execute("DELETE FROM metadata WHERE key=? AND value=?", (key, row[0])).rowcount:
                deleted.append(key)
    return deleted


def _maintain_executions(store, clock, skip):
    # Observe already-persisted completion facts at write time. A client that
    # lost its handle must not leave a finished execution occupying quota forever.
    with store.transaction():
        rows = store.db.execute("SELECT key,value FROM metadata WHERE key GLOB 'mcp_execution:*' LIMIT ?", (EXECUTION_LIMIT + 1,)).fetchall()
        for row in rows:
            value = _decode(row["value"])
            if row["key"] != PREFIX + value["id"]:
                raise BridgeError("Stored MCP execution identity changed", code="state_invalid")
            if value["id"] == skip:
                continue
            if _expired(value, clock):
                store.db.execute("DELETE FROM metadata WHERE key=?", (row["key"],))
            elif value["state"] not in FINAL and value.get("tool_name") in TASK_TOOLS:
                turn = store.db.execute("SELECT thread_id,status FROM turns WHERE task_id=? AND turn_id=?",
                    (value.get("bridge_task_id"), value.get("turn_id"))).fetchone()
                if turn and turn["thread_id"] == value.get("thread_id") and turn["status"] in TERMINAL:
                    value.update(state=TURN_TO_TASK[turn["status"]], native_status=turn["status"],
                        terminal_at=clock, updated_at=clock, revision=value["revision"] + 1)
                    store.db.execute("UPDATE metadata SET value=? WHERE key=?", (json.dumps(value), row["key"]))


def update_execution(store, execution_id, changes, *, expected=None, now=None):
    """One broker transaction; bounded namespace, CAS and immutable terminals."""
    clock = time.time() if now is None else now
    _maintain_executions(store, clock, execution_id)
    with store.transaction():
        rows = store.db.execute("SELECT key,value FROM metadata WHERE key GLOB 'mcp_execution:*' LIMIT ?", (EXECUTION_LIMIT + 1,)).fetchall()
        for row in rows:
            value = _decode(row["value"])
            if row["key"] != PREFIX + value["id"]:
                raise BridgeError("Stored MCP execution identity changed", code="state_invalid")
            if _expired(value, clock):
                store.db.execute("DELETE FROM metadata WHERE key=?", (row["key"],))
        row = store.db.execute("SELECT value FROM metadata WHERE key=?", (PREFIX + execution_id,)).fetchone()
        previous = _decode(row[0]) if row else None
        if previous is None and expected is not None:
            raise BridgeError("MCP task not found or expired", code="not_found", ids={"mcp_task_id": execution_id})
        if (previous["revision"] if previous else None) != expected:
            raise BridgeError("MCP task revision changed; poll again", code="state_conflict", ids={"mcp_task_id": execution_id})
        identities = {"id", "operation_id", "tool_name", "bridge_task_id", "thread_id", "turn_id", "generation", "created_at"}
        if ("id" in changes and changes["id"] != execution_id) or (previous and any(
                key in changes and previous.get(key) != changes[key] for key in identities)):
            raise BridgeError("MCP execution identity cannot change", code="state_conflict", ids={"mcp_task_id": execution_id})
        if previous and previous["state"] in FINAL:
            if any(previous.get(key) != value for key, value in changes.items()):
                raise BridgeError("Terminal MCP tasks cannot change", code="state_conflict", ids={"mcp_task_id": execution_id})
            return previous
        if previous is None:
            count = store.db.execute("SELECT COUNT(*) FROM metadata WHERE key GLOB 'mcp_execution:*'").fetchone()[0]
            if count >= EXECUTION_LIMIT:
                raise BridgeError("MCP execution record budget exhausted; wait for terminal retention expiry", code="result_too_large")
            previous = {"id": execution_id, "created_at": clock, "revision": 0}
        value = {**previous, **changes, "updated_at": clock, "revision": previous["revision"] + 1}
        if value["state"] in FINAL:
            value["terminal_at"] = clock
        if encoded_size(value) > RECORD_BYTES:
            raise BridgeError("MCP execution metadata exceeds 64 KiB", code="result_too_large")
        _decode(json.dumps(value))
        store.db.execute("INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (PREFIX + execution_id, json.dumps(value)))
    return value


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ProtocolTasks:
    def __init__(self, service):
        self.service, self.store = service, service.store

    async def handle(self, action, params):
        shapes = {"create": {"name", "arguments"}, "get": {"taskId"}, "update": {"taskId", "inputResponses"},
            "cancel": {"taskId"}, "review_begin": {"request_id"}, "review_answer": {"request_id", "requestState", "inputResponses"}}
        if action not in shapes:
            raise BridgeError("Unsupported MCP task operation", code="unsupported_task_operation")
        if (not isinstance(params, dict) or set(params) - (shapes[action] | {"form"}) or not shapes[action] <= params.keys()
                or type(params.get("form", False)) is not bool):
            raise BridgeError("Invalid MCP task operation parameters", code="invalid_arguments")
        form = params.get("form", False)
        if action == "create":
            return await self.create(params["name"], params["arguments"])
        if action == "get":
            return await self.get(params["taskId"], form=form)
        if action == "update":
            return await self.update(params["taskId"], params["inputResponses"], form=form)
        if action == "cancel":
            return await self.cancel(params["taskId"])
        if action == "review_begin":
            return await self.review_begin(params["request_id"], form=form)
        return await self.review_answer(params["requestState"], params["request_id"], params["inputResponses"], form=form)

    @staticmethod
    def eligible(name, args):
        return (name in TASK_TOOLS and not (name == "codex_send" and args.get("mode", "follow_up") != "follow_up")
                and not (name == "codex_delegate" and args.get("expected_turn_id")))

    def seed(self, record):
        return {"taskId": record["id"], "status": record["state"], "createdAt": timestamp(record["created_at"]),
            "lastUpdatedAt": timestamp(record["updated_at"]), "ttlMs":
                int((record["terminal_at"] - record["created_at"] + TERMINAL_AGE_SECONDS) * 1000) if record["state"] in FINAL else None,
            "pollIntervalMs": POLL_INTERVAL_MS}

    async def create(self, name, values):
        args = arguments(name, values)
        if not self.eligible(name, args):
            raise BridgeError("Tool does not create an MCP execution", code="unsupported_task_operation")
        # Invalid augmentation state must be rejected before accepting native
        # work, rather than reporting a definite rejection after starting it.
        for row in self.store.db.execute("SELECT key,value FROM metadata WHERE key GLOB 'mcp_execution:*' LIMIT ?", (EXECUTION_LIMIT + 1,)):
            record = _decode(row["value"])
            if row["key"] != PREFIX + record["id"]:
                raise BridgeError("Stored MCP execution identity changed", code="state_invalid")
        # This is a comparison snapshot, not authorization. Dispatch retains
        # every execution check, while cached receipts stay readable after deletion.
        before = self.store.task_head(args["task_id"]).get("turn_id") if args.get("task_id") else None
        # The native acknowledgement snapshot binds the exact turn even if a
        # different frontend starts a follow-up before this coroutine resumes.
        result = await dispatch(self.service, name, values)
        operation_id = args["request_id"]
        for row in self.store.db.execute("SELECT value FROM metadata WHERE key GLOB 'mcp_execution:*' LIMIT ?", (EXECUTION_LIMIT + 1,)):
            record = _decode(row[0])
            if (record.get("operation_id") == operation_id and record.get("turn_id") == result.get("turn_id")
                    and record.get("bridge_task_id") == result.get("id") and record.get("thread_id") == result.get("thread_id")):
                try:
                    record = read_execution(self.store, record["id"])
                except BridgeError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    return {"resultType": "task", **self.seed(record)}
        if (not isinstance(result.get("turn_id"), str) or result["turn_id"] == before
                or not result.get("thread_id") or not result.get("id")):
            return {"resultType": "complete", **tool_result(result, structured=True)}
        try:
            record = update_execution(self.store, uuid.uuid4().hex, {"state": "working", "operation_id": operation_id,
                "tool_name": name, "bridge_task_id": result["id"], "thread_id": result["thread_id"], "turn_id": result["turn_id"],
                "generation": result.get("verified_generation", self.service.backend.generation), "cancel_requested": False, "inputs": {} })
        except BridgeError as exc:
            if exc.code != "result_too_large":
                raise
            # Task augmentation is optional. Accepted work still returns its
            # ordinary bounded acknowledgement when the handle budget is full.
            return {"resultType": "complete", **tool_result(result, structured=True)}
        return {"resultType": "task", **self.seed(record)}

    def pending(self, record):
        result = []
        current = self.service.task(record["bridge_task_id"], allow_deleted=True, light=True)
        active = current.get("turn_id") == record["turn_id"] and current.get("turn_status") not in TERMINAL
        for _, request in self.store.records("requests", task_id=record["bridge_task_id"], generation=self.service.backend.generation,
                statuses=("pending", "answering"), limit=INPUT_LIMIT + 1):
            if request["params"].get("turnId") == record["turn_id"] or (
                    active and request["kind"] == "elicitation" and request["params"].get("turnId") is None):
                result.append(request)
        if len(result) > INPUT_LIMIT:
            raise BridgeError("MCP task has more than 32 pending inputs; use codex_requests pages", code="result_too_large")
        return result

    @staticmethod
    def input_binding(request):
        binding = {"request_id": request["id"], "generation": request["generation"], "proposal_hash": request.get("proposal_hash"),
                   "turn_id": request["params"].get("turnId"), "item_id": request["params"].get("itemId"), "request_revision": request["revision"]}
        return binding

    def refresh(self, record):
        if record["state"] in FINAL:
            return record
        turn = self.service.lifecycle.turn(record["bridge_task_id"], record["turn_id"])
        if turn and turn["thread_id"] != record["thread_id"]:
            raise BridgeError("MCP execution native identity changed", code="scope_unverified")
        if turn and turn["status"] in TERMINAL:
            fields = {"state": TURN_TO_TASK[turn["status"]], "native_status": turn["status"]}
        else:
            requests = self.pending(record)
            fields = {"state": "input_required" if requests else "working"}
        if all(record.get(key) == value for key, value in fields.items()):
            return record
        return update_execution(self.store, record["id"], fields, expected=record["revision"])

    def final_result(self, record):
        row = self.store.db.execute("SELECT state,result FROM operations WHERE id=?", (record["operation_id"],)).fetchone()
        if row is None or row["state"] != "done" or not row["result"]:
            raise BridgeError("MCP task result is no longer retained; inspect the bound native history", code="not_found", ids={"mcp_task_id": record["id"]})
        result = decisions.task_view(json.loads(row["result"]))
        result["native_execution"] = {"task_id": record["bridge_task_id"], "thread_id": record["thread_id"],
            "turn_id": record["turn_id"], "status": record["native_status"]}
        failure = record["native_status"] == "failed"
        payload = result_envelope(redact(result), operation_id=record["operation_id"], request_id_kind="operation")
        if failure:
            payload.update(error_envelope(BridgeError("Native turn failed; inspect codex_history for the bound turn", code="native_rejected",
                ids={"task_id": record["bridge_task_id"], "turn_id": record["turn_id"], "operation_id": record["operation_id"]})))
        response = tool_result(payload, structured=True)
        response["resultType"] = "complete"
        response["isError"] = failure
        return response

    def task_record(self, execution_id):
        record = read_execution(self.store, execution_id)
        if record.get("tool_name") not in TASK_TOOLS:
            raise BridgeError("MCP task not found or expired", code="not_found", ids={"mcp_task_id": execution_id})
        return record

    async def get(self, execution_id, *, form=False):
        record = self.refresh(self.task_record(execution_id))
        result = {"resultType": "complete", **self.seed(record)}
        if record["state"] == "completed":
            result["result"] = self.final_result(record)
        elif record["state"] == "input_required":
            requests = self.pending(record)
            issued, bindings, details = self.prepare_inputs(record, requests, form)
            result["inputRequests"] = issued
            result["_meta"] = {"codex-local-mcp/pendingRequests": details}
            result["statusMessage"] = "Review the issued human form, or use codex_review_request/local review; parent questions use codex_respond"
            if encoded_size(result) > RESULT_BUDGET - 2048:
                issued, bindings, details = self.prepare_inputs(record, requests, form, deny_only=True)
                result["inputRequests"] = issued
                result["_meta"]["codex-local-mcp/pendingRequests"] = details
            if bindings != record["inputs"]:
                record = update_execution(self.store, record["id"], {"inputs": bindings}, expected=record["revision"])
                result.update(self.seed(record))
        elif record["state"] == "working":
            task = self.service.task(record["bridge_task_id"], allow_deleted=True, light=True)
            if task["status"] in ("needs_resume", "uncertain") or not self.service.backend.alive:
                result["statusMessage"] = "Native execution needs recovery; use codex_resume and inspect the bound history"
            elif record["cancel_requested"]:
                result["statusMessage"] = "Interrupt acknowledged; native completion is not yet confirmed"
        elif record["state"] == "failed":
            raise BridgeError("MCP task has no retained protocol failure", code="state_invalid")
        if encoded_size(result) > RESULT_BUDGET:
            raise BridgeError("MCP task result exceeds the response budget; use ordinary paged tools", code="result_too_large")
        return result

    @staticmethod
    def denial_form(reason, request):
        from .mcp import review_notice
        return {"mode": "form", "message": review_notice(request) + "\n" + reason + " No acceptance is available; review in the terminal.",
            "requestedSchema": {"type": "object", "properties": {"decision": {"type": "string", "enum": ["decline", "cancel"]}}, "required": ["decision"]}}

    def plan(self, request, *, deny_only=False):
        from .mcp import review_form
        if request["kind"] == "parent_question":
            return None, None, "Parent questions are answered through codex_respond"
        form, convert, reason = review_form(self.service.public_request(request))
        denied = deny_only or form is None
        if denied:
            form = self.denial_form(reason or "Combined proposals exceed the human UI budget", request) if request["kind"] in (
                "command_approval", "file_approval", "permissions", "elicitation") else None
            convert = None
        return form, convert, denied

    def prepare_inputs(self, record, requests, form_capability, *, deny_only=False):
        bindings, issued, details = dict(record["inputs"]), {}, []
        for request in requests:
            identity = self.input_binding(request)
            match = next(((key, value) for key, value in bindings.items()
                if all(value.get(k) == v for k, v in identity.items()) and not value.get("consumed") and not value.get("superseded")), None)
            key, binding = match if match else (uuid.uuid4().hex, {**identity, "consumed": False, "issued": False})
            # Native MCP elicitation may have no turn correlation. Show it as
            # pending, but keep positive responses on ordinary human review.
            scoped = record.get("tool_name") in TASK_TOOLS and request["params"].get("turnId") is None
            form, _, denied = self.plan(request, deny_only=deny_only) if form_capability and not scoped else (None, None, True)
            if form is not None and request["status"] == "pending":
                form_hash = hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()
                if binding.get("issued") and (binding.get("form_hash") != form_hash or time.time() >= binding.get("expires_at", 0)):
                    bindings[key] = {**binding, "superseded": True}
                    key, binding = uuid.uuid4().hex, {**identity, "consumed": False, "issued": False}
                binding = {**binding, "issued": True, "deny_only": denied,
                    "form_hash": form_hash, "expires_at": binding.get("expires_at", time.time() + REVIEW_AGE_SECONDS)}
                issued[key] = {"method": "elicitation/create", "params": form}
            bindings[key] = binding
            details.append({**request_summary(decisions.public_request(request)), "input_key": key})
        if len(bindings) > INPUT_HISTORY_LIMIT:
            raise BridgeError("MCP task input history exceeds 128 forms; use local review", code="result_too_large")
        return issued, bindings, details

    def input_plan(self, record, key, answer, *, form=False):
        binding = record.get("inputs", {}).get(key)
        if binding is None:
            if isinstance(answer, dict) and answer.get("action") == "accept":
                raise BridgeError("No issued elicitation matches this response", code="stale_request")
            return None
        if binding.get("consumed") or binding.get("superseded") or record["state"] in FINAL:
            raise BridgeError("MCP elicitation was already consumed", code="stale_request")
        request = self.store.get("requests", binding["request_id"])
        if request["status"] != "pending":
            raise BridgeError("MCP elicitation is no longer pending", code="stale_request")
        if request["generation"] != binding["generation"] or request["generation"] != self.service.backend.generation:
            raise BridgeError("MCP input belongs to an expired native generation", code="stale_request")
        if request.get("proposal_hash") != binding["proposal_hash"]:
            raise BridgeError("MCP input proposal changed; poll again", code="stale_proposal")
        if request["revision"] != binding["request_revision"]:
            raise BridgeError("MCP input request revision changed; review again", code="stale_request")
        params = request["params"]
        scoped = record.get("tool_name") in TASK_TOOLS and request["kind"] == "elicitation" and params.get("turnId") is None
        if (request["task_id"] != record["bridge_task_id"] or params.get("threadId") != record["thread_id"]
                or params.get("itemId") != binding["item_id"]):
            raise BridgeError("MCP input belongs to a different native request", code="stale_request")
        if params.get("turnId") != record["turn_id"] and not scoped:
            raise BridgeError("MCP input belongs to a different turn", code="stale_request")
        if not isinstance(answer, dict) or answer.get("action") not in ("accept", "decline", "cancel"):
            raise BridgeError("Malformed MCP input response", code="invalid_arguments")
        if answer["action"] != "accept":
            response, human = None, False  # Dismissal never answers Codex.
        else:
            content, kind = answer.get("content"), request["kind"]
            if kind == "parent_question":
                raise BridgeError("Parent questions are answered through codex_respond", code="invalid_response")
            denial = isinstance(content, dict) and set(content) == {"decision"} and content["decision"] in ("decline", "cancel")
            if denial:
                decision = content["decision"]
                response = ({"permissions": {}, "scope": "turn"} if kind == "permissions" else
                    {"action": decision, "content": None} if kind == "elicitation" else {"decision": decision})
                human = False
                if form and binding.get("issued") and time.time() < binding.get("expires_at", 0):
                    plan, _, _ = self.plan(request, deny_only=binding.get("deny_only", False))
                    human = plan is not None and hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() == binding.get("form_hash")
            else:
                if scoped:
                    raise BridgeError("Thread-scoped elicitation needs ordinary human review; use codex_review_request", code="human_review_required")
                if not binding.get("issued"):
                    if not form:
                        raise BridgeError("Positive MCP input requires this request's issued human form", code="human_review_required")
                    raise BridgeError("No issued elicitation matches this response", code="stale_request")
                if not form or binding.get("deny_only"):
                    raise BridgeError("Positive MCP input requires this request's issued human form", code="human_review_required")
                if time.time() >= binding.get("expires_at", 0):
                    raise BridgeError("MCP elicitation expired; request a fresh human form", code="stale_request")
                plan, convert, denied = self.plan(request)
                if plan is None or denied or hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() != binding.get("form_hash"):
                    raise BridgeError("MCP elicitation content changed; review again", code="stale_request")
                from .tools import validate
                validate(content, plan["requestedSchema"])
                response, human = convert(content), True
            if kind == "file_approval" and not self.service.is_denial(kind, response):
                self.service.validate_proposal(request, binding["proposal_hash"])
        return request, response, human

    async def apply_inputs(self, record, responses, *, form=False):
        if not isinstance(responses, dict) or len(responses) > INPUT_LIMIT:
            raise BridgeError("tasks/update requires at most 32 inputResponses", code="invalid_arguments")
        # Reject an invalid batch before consuming a form or sending any answer.
        for key, answer in responses.items():
            self.input_plan(record, key, answer, form=form)
        results, attempted = [], False
        try:
            for key, answer in responses.items():
                latest = read_execution(self.store, record["id"])
                plan = self.input_plan(latest, key, answer, form=form)
                if plan is None:
                    continue
                request, response, human = plan
                ledger = dict(latest["inputs"])
                ledger[key] = {**ledger[key], "consumed": True}
                record = update_execution(self.store, record["id"], {"inputs": ledger}, expected=latest["revision"])
                if response is not None:
                    attempted = True
                    results.append(await dispatch(self.service, "codex_respond", {"pending_request_id": request["id"], "response": response,
                        **({"review_source": "host_elicitation"} if human else {}),
                        **({"proposal_hash": request.get("proposal_hash")} if request["kind"] == "file_approval" else {})}, human=human))
        except Exception as exc:
            if not isinstance(exc, BridgeError):
                exc = BridgeError.from_dict(error_envelope(exc)["error"])
            if attempted:
                exc.uncertain, exc.retryable = True, False
                exc.ids.setdefault("task_id", record["bridge_task_id"])
                exc.next_steps = ["Inspect codex_requests and codex_events before retrying; earlier input responses may already have been sent", *exc.next_steps]
            raise exc
        return record, results

    async def update(self, execution_id, responses, *, form=False):
        await self.apply_inputs(self.task_record(execution_id), responses, form=form)
        return {"resultType": "complete"}

    async def cancel(self, execution_id):
        record = self.refresh(self.task_record(execution_id))
        if record["state"] not in FINAL:
            await dispatch(self.service, "codex_interrupt", {"task_id": record["bridge_task_id"], "expected_turn_id": record["turn_id"],
                "operation_id": "mcp-cancel-" + execution_id})
            latest = read_execution(self.store, execution_id)
            if latest["state"] not in FINAL:
                update_execution(self.store, execution_id, {"cancel_requested": True}, expected=latest["revision"])
        return {"resultType": "complete"}

    async def review_begin(self, request_id, *, form=False):
        request = self.service.review_request(request_id)
        pending = {"status": "pending", "request": request_summary(decisions.public_request(request)),
            "next_step": "Client has no form elicitation. Run the local review command, or relay a denial with codex_respond."}
        plan, _, denied = self.plan(request)
        if not form or plan is None:
            if form:
                pending["next_step"] = "Use codex_respond for parent questions; otherwise use the local review command"
            return {"resultType": "complete", **tool_result(result_envelope(pending, pending_request_id=request_id, request_id_kind="pending_request"), structured=True)}
        record = update_execution(self.store, uuid.uuid4().hex, {"state": "input_required", "tool_name": "codex_review_request",
            "operation_id": uuid.uuid4().hex, "bridge_task_id": request["task_id"], "thread_id": request["params"].get("threadId"),
            "turn_id": request["params"].get("turnId"), "generation": request["generation"], "cancel_requested": False,
            "expires_at": time.time() + REVIEW_AGE_SECONDS, "inputs": {}})
        issued, bindings, _ = self.prepare_inputs(record, [request], True)
        if encoded_size({"resultType": "input_required", "requestState": record["id"], "inputRequests": issued}) > RESULT_BUDGET - 2048:
            issued, bindings, _ = self.prepare_inputs(record, [request], True, deny_only=True)
        record = update_execution(self.store, record["id"], {"inputs": bindings}, expected=record["revision"])
        return {"resultType": "input_required", "requestState": record["id"], "inputRequests": issued}

    async def review_answer(self, request_state, request_id, responses, *, form=False):
        try:
            record = read_execution(self.store, request_state)
        except BridgeError as exc:
            if exc.code == "not_found":
                raise BridgeError("Review state expired or unknown; fetch codex_review_request again", code="stale_request") from exc
            raise
        if record.get("tool_name") != "codex_review_request" or any(binding["request_id"] != request_id for binding in record["inputs"].values()):
            raise BridgeError("Review state belongs to a different request", code="stale_request")
        if not isinstance(responses, dict) or set(responses) != set(record["inputs"]):
            raise BridgeError("Review response does not match the issued elicitation", code="stale_request")
        record, results = await self.apply_inputs(record, responses, form=form)
        update_execution(self.store, record["id"], {"state": "completed"}, expected=record["revision"])
        result = results[0] if results else {"status": "pending", "request_id": request_id,
            "next_step": "Review was declined, dismissed, or unavailable; no Codex decision was sent"}
        return {"resultType": "complete", **tool_result(result_envelope(result, pending_request_id=request_id, request_id_kind="pending_request"), structured=True)}
