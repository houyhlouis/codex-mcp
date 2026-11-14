"""Authority explanations and one approval gate, independent of UI channels."""

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath

from .security import BridgeError, redact, within
from .supervision import save_receipt

HUMAN_SOURCES = frozenset({"host_elicitation", "local_terminal"})


def validate_settings_reviewer(mode):
    if mode == "inherit":
        raise BridgeError("approval_mode inherit applies only when a task is started; choose ask or approve_for_me", code="invalid_arguments")


def review_markers(request):
    required = request.get("kind") == "file_approval" and bool(request.get("content_redacted"))
    count = request.get("redaction_count", 0)
    return {"person_review_required": required, "review_warning":
        f"Values were hidden in this file-change proposal ({count} redacted fields). A person must review it before acceptance." if required else None}


def decision_source(service, request, result, *, human, review_source):
    if review_source is not None and (not human or not isinstance(review_source, str) or review_source not in HUMAN_SOURCES):
        raise BridgeError("review_source is private to a trusted human review route", code="invalid_arguments")
    denial = service.is_denial(request["kind"], result)
    if (request["kind"] == "file_approval" and result.get("decision") == "accept"
            and request.get("content_redacted") and not human):
        raise BridgeError("A person must review this redacted file-change proposal through the client prompt or local terminal review; approval_relay cannot accept it",
                          code="person_review_required")
    if request["requires_human"] and not human and not service.config.approval_relay and not denial:
        raise BridgeError("Human review required. Use codex_review_request or the local review command; approval_relay is disabled", code="human_review_required")
    return (review_source or "human") if human else "main_agent_denial" if denial else "operator_relay" if request["requires_human"] else "main_agent"


def settings_view(service, task):
    from .extensions import REVIEWERS
    from .managed_worktrees import shared_resources
    project = service.store.get("projects", task["project_id"]) if hasattr(service, "store") and task.get("project_id") else {}
    requested = task.get("requested_settings") or {k: task.get(k) for k in ("access", "approval_mode", "model", "effort", "collaboration_mode", "service_tier")}
    active = task.get("active_settings") or {}
    if active.get("turn_id") != task.get("turn_id"):
        active = {}
    effective = {k: active.get(k, task.get(k)) for k in ("access", "approval_mode", "model", "effort", "collaboration_mode", "service_tier")}
    effective.update(approvals_reviewer=REVIEWERS.get(active.get("approval_mode"), task.get("approvals_reviewer")), permission_profile=task.get("native_permission_profile"),
        approval_policy=task.get("native_approval_policy"), cwd=task.get("native_cwd"),
        verified=not project.get("removed") and task.get("verified_generation") == service.backend.generation and service.backend.alive and task["id"] in service.loaded)
    inherited = {k: effective.get(k) for k, v in requested.items() if v is None or (k == "approval_mode" and v == "inherit")}
    return redact({"format_version": 1, "requested": requested, "current_turn_overrides": active,
        "effective": effective, "inherited": inherited,
        "requested_source": "recorded_input" if task.get("requested_settings") else "stored_projection",
        "inherited_from_task_id": task.get("parent_task_id"),
        "configured_boundary": {"project": task["project"], "read_roots": [task["project"], *map(str, service.config.toolchain_paths)],
            "write_roots": [task["project"]] if effective["access"] == "write" else [], "system_reads": "minimal",
            "network": "requires native approval", "permission_grants": "turn only",
            **({"shared_resources": shared_resources(project)} if project.get("worktree") else {})}})


def path_view(value, cwd, project):
    result = {"requested": value, "normalized": None, "in_project": False}
    if not isinstance(value, str) or not value or "\x00" in value:
        return result
    if PureWindowsPath(value).drive and not PureWindowsPath(value).is_absolute():
        return result
    if os.name != "nt" and (PureWindowsPath(value).drive or "\\" in value):
        return result
    try:
        resolved = (Path(cwd) / value).resolve()
        result.update(normalized=str(resolved), in_project=within(resolved, Path(project).resolve()))
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return result


def permission_view(service, request, params, task):
    cwd, project = task.get("native_cwd") or task["project"], task["project"]
    requested_cwd = params.get("cwd", cwd)
    sources, destinations, filesystem, unknown = [], [], [], []
    if request["kind"] == "command_approval":
        resolved_cwd = path_view(requested_cwd, cwd, project)
        cwd = resolved_cwd["normalized"]
        if cwd is None:
            unknown.append("command_cwd_unresolved")
        elif not resolved_cwd["in_project"]:
            unknown.append("command_cwd_outside_project")
    network = {"requested": False, "host": None, "requested_host": None, "protocol": None, "scope": "none"}
    if request["kind"] == "file_approval":
        for change in params.get("proposedChanges") or []:
            if not isinstance(change, dict):
                unknown.append("unknown_change_shape")
                continue
            kind = change.get("kind") or {}
            if not isinstance(kind, dict) or kind.get("type") not in ("add", "delete", "update"):
                unknown.append("unknown_change_kind")
                kind = {} if not isinstance(kind, dict) else kind
            sources.append({**path_view(change.get("path"), cwd, project), "action": kind.get("type")})
            if kind.get("move_path") is not None:
                destinations.append(path_view(kind["move_path"], cwd, project))
    elif request["kind"] == "permissions":
        try:
            permissions = service.normalize_permissions(params.get("permissions"))
            network["requested"] = bool(permissions.get("network", {}).get("enabled"))
            if network["requested"]:
                network["scope"] = "all_hosts"
            fs = permissions.get("fileSystem") or {}
            for key in ("read", "write"):
                filesystem += [{**path_view(path, cwd, project), "access": key} for path in fs.get(key, [])]
            for entry in fs.get("entries", []):
                filesystem.append({**path_view(entry["path"]["path"], cwd, project), "access": entry["access"]})
        except BridgeError:
            unknown.append("unsupported_permission_scope")
    context = params.get("networkApprovalContext")
    if isinstance(context, dict):
        host = context.get("host")
        network = {"requested": True, "host": host.strip().lower().rstrip(".") if isinstance(host, str) else None,
                   "requested_host": host, "protocol": context.get("protocol"), "scope": "specified_host"}
    settings = settings_view(service, task)
    if not settings["effective"]["verified"]:
        unknown.append("native_thread_unverified")
    paths = [*sources, *destinations, *filesystem]
    if any(not p["in_project"] for p in paths):
        unknown.append("affected_path_outside_project_or_unresolved")
    scope_valid = settings["effective"]["verified"] and not unknown
    if request["kind"] == "file_approval":
        try:
            service.validate_file_paths(request, params)
        except (BridgeError, TypeError, ValueError):
            scope_valid = False
    view = {"format_version": 1, "kind": request["kind"], "cwd": cwd, "requested_cwd": requested_cwd, "project": project,
        "sources": sources, "destinations": destinations, "filesystem": filesystem,
        "grant_root": path_view(params["grantRoot"], cwd, project) if params.get("grantRoot") is not None else None,
        "network": network, "scope": "turn" if request["kind"] == "permissions" else "request", "session_grant": False,
        "scope_verified": bool(scope_valid), "unknown": unknown, "settings": settings}
    return redact(view)


def receipt_fields(request, params, source, decision):
    receipt = {"request_id": request["id"], "pending_request_id": request["id"], "request_id_kind": "pending_request",
        "task_id": request["task_id"], "kind": request["kind"],
        "generation": request.get("generation"), "thread_id": params.get("threadId"), "turn_id": params.get("turnId"),
        "item_id": params.get("itemId"), "proposal_revision": request.get("proposal_revision"),
        "proposal_hash": request.get("proposal_hash"), "proposal_known": bool(request.get("proposal_hash")),
        "decision": decision, "decision_source": source,
        "delivery": "uncertain", "content_redacted": bool(request.get("content_redacted")),
        "redaction_count": request.get("redaction_count", 0), "authorization_scope": "turn" if request["kind"] == "permissions" else "request"}
    if request.get("decision_envelope"):
        receipt.update(decision_id=request["decision_envelope"]["id"], decision_context_hash=request["decision_envelope"]["context_hash"])
    return receipt


def native_receipt(service, task, params):
    review_id = params.get("reviewId")
    if not isinstance(review_id, str) or not review_id:
        return
    identity = [service.backend.generation, task["thread_id"], params.get("turnId"), review_id]
    request_id = "native_review:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    if service.store.db.execute("SELECT 1 FROM decision_receipts WHERE request_id=?", (request_id,)).fetchone():
        return
    item_id = params.get("targetItemId")
    proposal = service.file_proposals.get((service.backend.generation, task["thread_id"], params.get("turnId"), item_id))
    _, redaction = service.review_content({"proposedChanges": proposal["changes"]}) if proposal else ({}, {"content_redacted": False, "redaction_count": 0})
    receipt = save_receipt(service.store, {"request_id": request_id, "request_id_kind": "native_review", "task_id": task["id"], "kind": "native_review",
        "generation": service.backend.generation, "thread_id": task["thread_id"], "turn_id": params.get("turnId"),
        "item_id": item_id, "native_review_id": review_id, "native_completed_at_ms": params.get("completedAtMs"),
        "decision": (params.get("review") or {}).get("status"),
        "decision_source": "codex_reviewer", "delivery": "observed", "authorization_scope": "request",
        "proposal_revision": proposal["revision"] if proposal else None, "proposal_hash": proposal["hash"] if proposal else None,
        "proposal_known": bool(proposal), **redaction})
    service.store.event(task["id"], "bridge/decision_receipt", receipt)
