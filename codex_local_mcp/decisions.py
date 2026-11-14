"""Bounded task guidance. Envelopes never grant native permissions."""

import hashlib
import json
import re
import time

from .security import BridgeError, redact, same_path

ENVELOPE_BYTES = 32768
CONTRACT_LIMIT = 32
TTL_MIN, TTL_MAX = 60, 7 * 86400
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 128}
HASH = {"type": "string", "minLength": 64, "maxLength": 64}
WORDS = {"type": "string", "minLength": 1, "maxLength": 4096}
LINES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1024}, "maxItems": 16}
REFERENCE = {"type": "object", "properties": {"task_id": IDENTIFIER, "turn_id": IDENTIFIER,
    "result_id": IDENTIFIER, "artifact_id": IDENTIFIER, "sha256": HASH},
    "required": ["task_id", "turn_id", "result_id", "sha256"], "additionalProperties": False}
ENVELOPE = {"type": "object", "properties": {"version": {"type": "integer", "const": 1}, "id": IDENTIFIER,
    "goal": WORDS, "constraints": LINES, "deliverables": LINES, "verification": LINES,
    "options": {"type": "array", "items": {"type": "object", "properties": {"id": IDENTIFIER,
        "label": {"type": "string", "minLength": 1, "maxLength": 1024}}, "required": ["id", "label"], "additionalProperties": False}, "maxItems": 16},
    "evidence": {"type": "array", "items": REFERENCE, "maxItems": 16},
    "ttl_seconds": {"type": "integer", "minimum": TTL_MIN, "maximum": TTL_MAX}},
    "required": ["version", "id"], "additionalProperties": False}
ANSWER = {"type": "object", "properties": {"version": {"type": "integer", "const": 1}, "id": IDENTIFIER,
    "context_hash": HASH, "option_id": IDENTIFIER, "rationale": WORDS},
    "required": ["version", "id", "context_hash"], "additionalProperties": False}


class DecisionError(BridgeError):
    """A guidance validation refusal before a native write."""
    before_write = True


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def validate(value, schema, path="envelope"):
    from .tools import validate as check
    try:
        check(value, schema, path)
        if len(canonical(value)) > ENVELOPE_BYTES:
            raise BridgeError("Decision envelope exceeds the 32 KiB limit", code="invalid_arguments")
    except BridgeError as exc:
        raise DecisionError(str(exc), code=exc.code) from exc


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) or redact(value) != value:
        raise DecisionError("Decision ids must be nonsecret identifiers using letters, digits, '.', ':', '_' or '-'", code="invalid_arguments")


def evidence(service, task, references):
    family = task.get("root_task_id") or task["id"]
    manifests = {}
    for ref in references:
        try:
            owner = service.task(ref["task_id"], allow_deleted=True, light=True)
            result = service.store.db.execute("SELECT task_id,turn_id,sha256 FROM turn_results WHERE id=?", (ref["result_id"],)).fetchone()
            expired = service.store.db.execute("SELECT 1 FROM result_expirations WHERE task_id=? AND turn_id=?", (ref["task_id"], ref["turn_id"])).fetchone()
            valid = (result is not None and not expired and result["task_id"] == owner["id"] and result["turn_id"] == ref["turn_id"]
                and (owner.get("root_task_id") or owner["id"]) == family and same_path(owner["project"], task["project"]))
            digest = result["sha256"] if result else None
            if ref.get("artifact_id"):
                artifact = service.store.db.execute("SELECT task_id,turn_id,sha256 FROM artifacts WHERE id=?", (ref["artifact_id"],)).fetchone()
                valid = valid and artifact is not None and artifact["task_id"] == owner["id"] and artifact["turn_id"] == ref["turn_id"]
                digest = artifact["sha256"] if artifact else None
                if valid:
                    if ref["result_id"] not in manifests:
                        row = service.store.db.execute("SELECT data FROM turn_results WHERE id=?", (ref["result_id"],)).fetchone()
                        manifests[ref["result_id"]] = {(entry.get("artifact_id"), entry.get("sha256"))
                            for entry in json.loads(row[0]).get("artifacts", []) if isinstance(entry, dict)}
                    valid = (ref["artifact_id"], digest) in manifests[ref["result_id"]]
            if not valid or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"]) or digest != ref["sha256"]:
                raise ValueError
        except (BridgeError, ValueError):
            raise DecisionError("Decision evidence is missing, expired, has a different hash or belongs to another task family/project",
                code="decision_mismatch", ids={"task_id": task["id"]}) from None


def bind(service, task, value, *, kind, request_id=None, question=None, context=None, expected_turn_id=None):
    validate(value, ENVELOPE, "contract" if kind == "delegation" else "envelope")
    identifier(value["id"])
    options = value.get("options", [])
    for option in options:
        identifier(option["id"])
    if len({o["id"] for o in options}) != len(options):
        raise DecisionError("Decision option ids must be unique", code="invalid_arguments")
    safe = redact(value)
    evidence(service, task, safe.get("evidence", []))
    now = time.time()
    scope = {"task_id": task["id"], "root_task_id": task.get("root_task_id") or task["id"],
        "thread_id": task["thread_id"], "turn_id": task.get("turn_id") if kind == "question" else expected_turn_id,
        "generation": service.backend.generation}
    scope["request_id" if kind == "question" else "operation_id"] = request_id if kind == "question" else service.current_operation.get()
    record = {**safe, "kind": kind, "scope": scope, "created_at": now,
        "expires_at": now + safe["ttl_seconds"] if "ttl_seconds" in safe else None, "authority": "guidance_only"}
    if kind == "question":
        record.update(question_sha256=hashlib.sha256(canonical(redact(question))).hexdigest(),
                      context_sha256=hashlib.sha256(canonical(redact(context))).hexdigest())
    record["context_hash"] = hashlib.sha256(canonical(record)).hexdigest()
    return record


def state(record):
    return {**record, "expired": record.get("expires_at") is not None and time.time() >= record["expires_at"]}


def summary(record):
    result = {k: record.get(k) for k in ("version", "id", "kind", "scope", "context_hash", "created_at", "expires_at", "authority", "delivery", "source")}
    result.update(goal_preview=record.get("goal", "")[:256], counts={k: len(record.get(k, [])) for k in ("constraints", "deliverables", "verification", "options", "evidence")})
    expired = state(record)["expired"]
    result.update(expired=expired)
    if record["kind"] == "question":
        result["typed_response_allowed"] = not expired
    return result


def public_request(request):
    record = request.get("decision_envelope")
    if not record:
        return request
    return {**request, "decision_envelope": state(record), "decision_summary": summary(record)}


def public_summary(request):
    info = request.get("decision_summary")
    if info:
        info = state(info)
        request = {**request, "decision_summary": {**info, "typed_response_allowed": not info["expired"]}}
    return request


def check_scope(service, task, record, request_id=None):
    scope = record["scope"]
    if (scope["task_id"] != task["id"] or scope["root_task_id"] != (task.get("root_task_id") or task["id"])
            or scope["thread_id"] != task.get("thread_id") or scope["generation"] != service.backend.generation
            or (scope.get("turn_id") is not None and scope["turn_id"] != task.get("turn_id"))
            or (request_id is not None and scope.get("request_id") != request_id)):
        raise DecisionError("Decision envelope no longer matches this request, task, turn or backend generation", code="decision_mismatch")


def answer(service, request, response):
    if set(response) - {"text", "decision"} or not isinstance(response.get("text"), str) or not response["text"].strip():
        raise BridgeError("Parent response requires nonempty text", code="invalid_response")
    if "decision" not in response:
        return {"success": True, "contentItems": [{"type": "inputText", "text": response["text"]}]}
    record = request.get("decision_envelope")
    if not record:
        raise DecisionError("This parent question has no decision envelope; answer with plain text", code="decision_mismatch")
    decision = response["decision"]
    validate(decision, ANSWER, "decision")
    task = service.task(request["task_id"], light=True)
    check_scope(service, task, record, request["id"])
    if decision["id"] != record["id"] or decision["context_hash"] != record["context_hash"]:
        raise DecisionError("Decision id or context_hash does not match this parent question", code="decision_mismatch")
    if state(record)["expired"]:
        raise DecisionError("Decision envelope expired; send a plain-text reply explaining that the decision window passed", code="decision_expired")
    if "option_id" in decision and decision["option_id"] not in {option["id"] for option in record.get("options", [])}:
        raise DecisionError("Decision option_id was not offered by this parent question", code="decision_mismatch")
    evidence(service, task, record.get("evidence", []))
    if state(record)["expired"]:
        raise DecisionError("Decision envelope expired; send a plain-text reply explaining that the decision window passed", code="decision_expired")
    return {"success": True, "contentItems": [{"type": "inputText", "text": response["text"]},
        {"type": "inputText", "text": json.dumps({"decision": decision, "authority": "guidance_only"}, ensure_ascii=False)}]}


def contract_guard(service, task_id):
    context = service.delegation_context.get()
    if not context:
        return
    record = context["record"]
    task = service.task(task_id, light=True)
    check_scope(service, task, record)
    if state(record)["expired"]:
        raise DecisionError("Delegation contract expired before sending; inspect the task and use a new operation_id for new work", code="decision_expired")
    evidence(service, task, record.get("evidence", []))
    if state(record)["expired"]:
        raise DecisionError("Delegation contract expired before sending; inspect the task and use a new operation_id for new work", code="decision_expired")


def record_contract(service, task_id, record, delivery):
    with service.store.transaction():
        task = service.task(task_id)
        stored = {**record, "delivery": delivery}
        seq = service.store.event(task_id, "bridge/delegation_contract", stored)
        stored["source"] = {"event_seq": seq}
        identity = record["scope"].get("operation_id") or record["context_hash"]
        retained = [item for item in task.get("delegation_contracts", []) if (item["scope"].get("operation_id") or item["context_hash"]) != identity]
        retained.append({**summary(stored), "evidence": stored.get("evidence", [])})
        retained = retained[-CONTRACT_LIMIT:]
        while len(retained) > 1 and len(canonical(retained)) > ENVELOPE_BYTES:
            retained.pop(0)
        service.lifecycle.metadata(task_id, {"delegation_contract": stored, "delegation_contracts": retained[-CONTRACT_LIMIT:],
            "delegation_contract_summary": summary(stored)})
        operation_id = record["scope"].get("operation_id")
        row = service.store.db.execute("SELECT data FROM operation_details WHERE id=?", (operation_id,)).fetchone()
        if row:
            description = {**json.loads(row[0]), "contract": summary(stored)}
            service.store.db.execute("UPDATE operation_details SET data=? WHERE id=?", (json.dumps(description), operation_id))
    return stored


def task_view(task):
    fields = {}
    if task.get("delegation_contract"):
        fields["delegation_contract"] = state(task["delegation_contract"])
    if task.get("delegation_contracts"):
        fields["delegation_contracts"] = [state(record) for record in task["delegation_contracts"]]
    if task.get("delegation_contract_summary"):
        fields["delegation_contract_summary"] = state(task["delegation_contract_summary"])
    return {**task, **fields}
