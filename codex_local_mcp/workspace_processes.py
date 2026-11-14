"""Small, bounded projections of the read-only sandbox process check."""

import hashlib
import json

from . import procscan

SOURCE = "os_process_scan"
REASON = "sandbox_processes_alive"
UNAVAILABLE = "process_check_unavailable"
UNAVAILABLE_MESSAGE = ("The process list could not be read; workspace ownership is kept. "
    "Run doctor; set sandbox_process_check = false to turn the check off.")
TRACKED_ADVICE = " Codex no longer tracks them. Stop them with kill PID; the project is then freed automatically."
UNOWNED_ADVICE = " No task accounts for them. Stop them with kill PID or wait until they end, then retry with a new request_id."


def held(claim):
    return claim["kind"] == "background" and claim["data"].get("source") == SOURCE


def alive(result):
    return bool(result["processes"] or result["truncated"])


def outcome(result):
    if alive(result):
        return "present"
    return "clear" if result["available"] or waiver(result) else "unavailable"


def waiver(result):
    if result.get("available") or alive(result):
        return None
    return {"disabled": "The leftover-process check was turned off in the settings file; its process-scan claim was waived.",
        "not_linux": "The leftover-process check does not apply on this platform; its process-scan claim was waived."}.get(result["reason"])


def summary(result):
    return {"available": result["available"], "reason": result["reason"],
        "processes_total": len(result["processes"]) if result["available"] or alive(result) else None,
        "pids": [p["pid"] for p in result["processes"][:10]], "truncated": result["truncated"],
        "checked_at": result.get("checked_at")}


def unchecked(checked_at=None, reason="not_checked"):
    return {"available": False, "reason": reason, "processes_total": None, "pids": [], "truncated": False,
        "checked_at": checked_at}


def saved_state(state):
    if not state or "os_process_check" not in state:
        return state
    snapshot = state["os_process_check"]
    if not isinstance(snapshot, dict) or snapshot.get("checked_at") is None and snapshot.get("reason") not in ("disabled", "not_linux"):
        snapshot = unchecked()
    return {**state, "os_process_check": {"checked_at": None, **snapshot}}


def data(result):
    if outcome(result) == "unavailable":
        return {"source": SOURCE, "reason": UNAVAILABLE, "pids": [], "processes_total": None, "truncated": result["truncated"]}
    identities = sorted((p["pid"], p["start_time"]) for p in result["processes"])
    return {"source": SOURCE, "reason": REASON,
        "processes_hash": hashlib.sha256(json.dumps(identities, separators=(",", ":")).encode()).hexdigest(),
        **({"partial": True} if not result["available"] else {}),
        **{k: v for k, v in summary(result).items()
        if k not in ("available", "reason", "checked_at")}}


def message(claim):
    value = claim["data"]
    if value.get("reason") == UNAVAILABLE:
        return UNAVAILABLE_MESSAGE
    # describe() only reads the first ten ids and the total list length.
    entries = [{"pid": pid} for pid in value["pids"]]
    entries.extend({"pid": 0} for _ in range(max(0, min(value["processes_total"], 64) - len(entries))))
    return procscan.describe({"available": not value.get("partial", False), "processes": entries, "truncated": value["truncated"]}) + TRACKED_ADVICE
