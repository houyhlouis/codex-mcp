"""Synchronous task transitions. Callers journal events in the same transaction."""

import json
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from .security import BridgeError, redact
from . import native_turns

ACTIVE = frozenset({"starting", "queued", "running", "waiting", "interrupting", "uncertain", "needs_resume"})
BUSY = ACTIVE
TERMINAL = frozenset({"completed", "failed", "interrupted"})
STATUSES = ACTIVE | TERMINAL | {"idle", "archived", "deleted"}
TaskStatus = Literal["starting", "queued", "running", "waiting", "interrupting", "uncertain", "needs_resume",
                     "completed", "failed", "interrupted", "idle", "archived", "deleted"]
TerminalStatus = Literal["completed", "failed", "interrupted"]
TurnSource = Literal["event", "ack", "implicit", "recovery"]


def turn_fields():
    return {"summary": "", "files": [], "commands": [], "plan": [], "diff": "", "error": None,
            "active_settings": {}, "last_automatic_review": None, "recovered_turn_status": None, "projection_sources": {}}


TURN_FIELDS = frozenset(turn_fields())


@dataclass(frozen=True)
class TurnIdentity:
    task_id: str
    thread_id: str | None
    turn_id: str | None


class Lifecycle:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def view(task: dict) -> dict:
        result = dict(task)
        turn_id = task.get("turn_id")
        if not turn_id or task.get("projection_turn_id") != turn_id:
            result.update(turn_fields())
        if not turn_id or task.get("diff_turn_id") != turn_id:
            result["diff"] = ""
        settings = result.get("active_settings")
        if not isinstance(settings, dict) or settings.get("turn_id") != turn_id:
            result["active_settings"] = {}
        return result

    def create(self, task: dict) -> dict:
        if self.store.db.execute("SELECT 1 FROM tasks WHERE id=?", (task["id"],)).fetchone():
            raise BridgeError("Task already exists; use a field transition", code='state_conflict')
        value = {**task, "projection_turn_id": task.get("turn_id"),
                 "turn_status": task.get("turn_status") if task.get("turn_id") else None,
                 "evidence_complete": True, "evidence_gaps": []}
        value.pop("revision", None)
        with self.store.transaction():
            value = self.store.save("tasks", value)
            if self.store.resources:
                self.store.resources.register(value, new=True)
            if value.get("status") in ACTIVE:
                self.store.claim(value["id"], "turn", "uncertain", {"turn_id": value.get("turn_id")})
            return value

    def _write(self, task_id: str, fields: dict, expected: dict | None = None) -> dict | None:
        status: TaskStatus | None = fields.get("status")
        if "status" in fields and status not in STATUSES:
            raise BridgeError("Unknown task lifecycle status", code='state_conflict')
        with self.store.transaction():
            return self.store.update("tasks", task_id, {**fields, "updated_at": time.time()}, expected=expected)

    def metadata(self, task_id: str, fields: dict, *, expected: dict | None = None) -> dict | None:
        if set(fields) & ({"id", "thread_id", "turn_id", "projection_turn_id", "turn_status", "status", "diff_turn_id"} | TURN_FIELDS):
            raise BridgeError("Lifecycle identity and status require a transition", code='state_conflict')
        return self._write(task_id, fields, expected)

    def attach(self, task_id: str, thread_id: str) -> dict:
        task = self.store.get("tasks", task_id)
        if task.get("thread_id") not in (None, thread_id):
            raise BridgeError("Task cannot change its native thread", code='state_conflict')
        with self.store.transaction():
            task = self._write(task_id, {"thread_id": thread_id})
            if self.store.resources:
                self.store.resources.register(task)
            return task

    def identity(self, task_id: str) -> TurnIdentity:
        task = self.store.get("tasks", task_id)
        return TurnIdentity(task_id, task.get("thread_id"), task.get("turn_id"))

    def turn(self, task_id, turn_id):
        return self.store.db.execute("SELECT * FROM turns WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone()

    def reserve(self, task_id: str, fields: dict | None = None) -> tuple[dict, str]:
        previous = self.store.get("tasks", task_id)
        token = str(uuid.uuid4())
        with self.store.transaction():
            self.store.claim(task_id, "turn", "uncertain", {"start_token": token})
            self._write(task_id, {**(fields or {}), "status": "starting", "turn_start": token})
        return previous, token

    def begin(self, task_id: str, turn_id: str | None, *, source: TurnSource = "event", token: str | None = None) -> dict:
        task = self.store.get("tasks", task_id)
        if not turn_id:
            return task
        if not isinstance(turn_id, str) or not task.get("thread_id"):
            raise BridgeError("Turn requires an owned native thread and turn identity", code='state_conflict')
        known = self.turn(task_id, turn_id)
        if known and known["thread_id"] != task["thread_id"]:
            raise BridgeError("Turn belongs to a different native thread", code='state_conflict')
        if known:
            if self.store.resources:
                tracked = self.store.db.execute("SELECT 1 FROM resource_turns WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone()
                with self.store.transaction():
                    self.store.resources.begin(task, turn_id, source=source if tracked else "recovery", token=token)
            return task
        if source == "ack" and (token is None or task.get("turn_start") != token):
            # Remember a superseded ACK so delayed events cannot reopen it.
            with self.store.transaction():
                if self.store.resources:
                    self.store.resources.begin(task, turn_id, source=source, token=token)
                self.store.db.execute("INSERT INTO turns(task_id,thread_id,turn_id,status) VALUES (?,?,?,'inProgress')",
                                      (task_id, task["thread_id"], turn_id))
            return task
        if task.get("deleted") or (task.get("archived") and source != "event"):
            return task
        with self.store.transaction():
            if self.store.resources:
                self.store.resources.begin(task, turn_id, source=source, token=token)
            status = "inProgress"
            if task.get("turn_id") == turn_id:
                status = task.get("turn_status") or (task["status"] if task["status"] in TERMINAL else "inProgress")
            self.store.db.execute("INSERT INTO turns(task_id,thread_id,turn_id,status,error) VALUES (?,?,?,?,?)",
                (task_id, task["thread_id"], turn_id, status, json.dumps(task.get("error") if status in TERMINAL else None)))
            if task.get("turn_id") == turn_id:
                if status not in TERMINAL:
                    self.store.claim(task_id, "turn", "active", {"turn_id": turn_id})
                return self._write(task_id, {"projection_turn_id": turn_id, "turn_status": status})
            self.store.claim(task_id, "turn", "active", {"turn_id": turn_id})
            return self._write(task_id, {**turn_fields(), "turn_id": turn_id, "projection_turn_id": turn_id,
                "turn_status": "inProgress", "status": "running", "diff_turn_id": turn_id,
                "archived": False,
                "turn_start": None, "queue_reservation": None, "queue_uncertain": False,
                "usage_scope": "cumulative", "evidence_complete": source in ("event", "ack"),
                "evidence_gaps": ["recovery"] if source == "recovery" else (["missing_turn_started"] if source == "implicit" else [])})

    def complete(self, task_id: str, turn_id: str, status: TerminalStatus, error=None, *, native=None) -> dict:
        scope = native_turns.for_task(self.store, task_id)
        if native_turns.nested(native, scope):
            return self.store.get("tasks", task_id)
        if native and ("id" in native or "rootTurnId" in native):
            turn_id = native_turns.logical_id(native, scope) or turn_id
        if status not in TERMINAL:
            raise BridgeError("Unknown native terminal status; resume to reconcile history", code='state_conflict')
        error = redact(error)
        with self.store.transaction():
            task = self.begin(task_id, turn_id, source="implicit")
            turn = self.turn(task_id, turn_id)
            if turn is None:
                return task
            if turn["status"] in TERMINAL:
                if self.store.resources and native:
                    self.store.resources.complete(task_id, turn_id, native, recovered=True)
                return task
            if self.store.resources:
                self.store.resources.complete(task_id, turn_id, native)
            self.store.db.execute("UPDATE turns SET status=?,error=?,revision=revision+1 WHERE task_id=? AND turn_id=?",
                                  (status, json.dumps(error), task_id, turn_id))
            if task.get("turn_id") != turn_id:
                return task
            for claim in self.store.claims(task_id=task_id):
                if claim["kind"] == "turn" and claim["data"].get("turn_id") == turn_id:
                    self.store.claim(task_id, "turn", "inactive", expected_revision=claim["revision"])
            fields = {"turn_status": status, "error": error}
            if not task.get("archived") and not task.get("deleted"):
                fields["status"] = "queued" if (task.get("queue_reservation") or {}).get("after_turn_id") == turn_id else status
            task = self._write(task_id, fields)
            self.store.evidence.capture(task_id, turn_id)
            return task

    def project(self, task_id: str, turn_id: str | None, fields: dict) -> dict:
        task = self.store.get("tasks", task_id)
        if task.get("turn_id") != turn_id or task.get("projection_turn_id") != turn_id:
            return task
        return self._write(task_id, fields)

    def item(self, task_id, turn_id, item):
        task = self.store.get("tasks", task_id)
        if task.get("turn_id") != turn_id:
            return task
        kind = item.get("type")
        fields = {}
        if kind == "agentMessage":
            fields["summary"] = item.get("text", "")
        elif kind == "fileChange":
            fields["files"] = sorted(set(task.get("files", []) + [c["path"] for c in item.get("changes", [])]))
        elif kind == "commandExecution":
            command = {k: item.get(k) for k in ("id", "command", "cwd", "status", "exitCode", "durationMs")}
            commands = task.get("commands", [])
            if any(c.get("id") == command["id"] for c in commands):
                return task
            fields["commands"] = (commands + [command])[-100:]
            if len(commands) >= 100:
                fields.update(evidence_complete=False,
                    evidence_gaps=list(dict.fromkeys([*task.get("evidence_gaps", []), "command_limit"])))
        return self.project(task_id, turn_id, fields) if fields else task

    def current_settings(self, task_id, turn_id, values, acknowledgement):
        task = self.store.get("tasks", task_id)
        if task.get("turn_id") != turn_id or task["status"] not in ("running", "waiting") or task.get("turn_status") in TERMINAL:
            return task
        return self.project(task_id, turn_id, {"active_settings": {**task.get("active_settings", {}), **values,
            "turn_id": turn_id, "acknowledgement": acknowledgement}})

    def wait(self, task_id: str, turn_id: str | None, blocking: bool = True) -> dict:
        task = self.store.get("tasks", task_id)
        if blocking and turn_id in (None, task.get("turn_id")) and task.get("turn_status") not in TERMINAL and task["status"] in ("running", "waiting"):
            return self._write(task_id, {"status": "waiting"})
        return task

    def answered(self, task_id: str, turn_id: str | None, blocking: bool) -> dict:
        task = self.store.get("tasks", task_id)
        if not blocking and turn_id in (None, task.get("turn_id")) and task["status"] == "waiting" and task.get("turn_status") not in TERMINAL:
            return self._write(task_id, {"status": "running"})
        return task

    def interrupting(self, identity: TurnIdentity) -> dict:
        task = self.store.get("tasks", identity.task_id)
        if self.identity(identity.task_id) == identity and task["status"] in ("running", "waiting") and task.get("turn_status") not in TERMINAL:
            return self._write(identity.task_id, {"status": "interrupting"})
        return task

    def uncertain(self, task_id, *, token=None):
        task = self.store.get("tasks", task_id)
        if token is not None and task.get("turn_start") != token:
            return task
        if task["status"] in ACTIVE:
            self.store.claim(task_id, "turn", "uncertain", {"turn_id": task.get("turn_id"), "start_token": token})
            return self._write(task_id, {"status": "uncertain"})
        return task

    def gap(self, task_id, reason, *, disconnected=False):
        task = self.store.get("tasks", task_id)
        fields = {"evidence_complete": False, "evidence_gaps": list(dict.fromkeys([*task.get("evidence_gaps", []), reason]))}
        if disconnected and task["status"] in ACTIVE:
            fields["status"] = "needs_resume"
        return self._write(task_id, fields)

    def recover(self, task_id: str, turn: dict | None, active: bool, *, expected_revision: int, adopted: bool = False) -> dict:
        task = self.store.get("tasks", task_id)
        if task["revision"] != expected_revision:
            return task
        scope = native_turns.for_task(self.store, task_id)
        turn = native_turns.root_view(turn, scope)
        with self.store.transaction():
            if turn:
                if task.get("turn_id") != turn["id"] and native_turns.recorded_root(self.store, task, known=(turn["id"],)) == turn["id"]:
                    # Older bridges projected turn.id from a nested start.
                    # Only this task's own journal can prove that alias.
                    if self.store.resources:
                        self.store.resources.merge_nested(task_id, task["turn_id"], turn["id"])
                    known = self.turn(task_id, turn["id"])
                    task = self._write(task_id, {**turn_fields(), "turn_id": turn["id"],
                        "projection_turn_id": turn["id"], "diff_turn_id": turn["id"],
                        "turn_status": known["status"] if known else "inProgress"})
                task = self.begin(task_id, turn["id"], source="recovery")
                if task.get("turn_id") is None and self.turn(task_id, turn["id"]) and not task.get("deleted"):
                    # A history revert clears the task projection but retains
                    # immutable turn facts. Rebind only during verified recovery.
                    task = self._write(task_id, {**turn_fields(), "turn_id": turn["id"],
                        "projection_turn_id": turn["id"], "diff_turn_id": turn["id"],
                        "turn_status": self.turn(task_id, turn["id"])["status"]})
                if task.get("turn_id") != turn["id"]:
                    return self.gap(task_id, "history_mismatch")
                if task.get("turn_status") in TERMINAL and turn.get("status") == "inProgress":
                    return self.gap(task_id, "history_mismatch")
                if turn.get("status") in TERMINAL:
                    task = self.complete(task_id, turn["id"], turn["status"], turn.get("error"), native=turn)
            # History was read after the resume ACK; its turn facts take
            # precedence over that older thread-activity snapshot.
            running = turn.get("status") == "inProgress" if turn else False
            fields = {"status": "running" if running else (turn["status"] if adopted and turn and turn.get("status") in TERMINAL else "idle")}
            if not turn and active:
                fields["status"] = "uncertain"
            elif not running:
                fields["turn_start"] = None
            if turn:
                fields["recovered_turn_status"] = turn["status"]
                fields["turn_status"] = self.turn(task_id, turn["id"])["status"]
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage":
                        fields["summary"] = item.get("text", "")
            task = self._write(task_id, fields)
            return self.gap(task_id, "recovery")

    def queued(self, task_id, fields, *, expected=None):
        if set(fields) - {"status", "queued_after_turn_id", "queue_reservation", "queue_uncertain"}:
            raise BridgeError("Queue state requires separate turn projection updates", code='state_conflict')
        return self._write(task_id, fields, expected)

    def archive(self, task_id, action, *, requested=False):
        task = self.store.get("tasks", task_id)
        if task.get("deleted") and action != "delete":
            return task
        if action == "delete":
            from .processes import deletion_state
            with self.store.transaction():
                state = deletion_state(self.store, task, requested=requested)
                for claim in self.store.claims(task_id=task_id):
                    self.store.claim(task_id, claim["kind"], "inactive", expected_revision=claim["revision"])
                fields = {"deleted": True, "status": "deleted", **({"process_state": state} if state is not None else {})}
                return self._write(task_id, fields) if any(task.get(k) != v for k, v in fields.items()) else task
        if action == "archive":
            return self._write(task_id, {"archived": True, "status": "archived"})
        return self._write(task_id, {"archived": False, **({"status": "idle"} if task["status"] == "archived" else {})})

    def rollback(self, task_id, token, previous, *, known_rejected=False):
        with self.store.transaction():
            result = self._write(task_id, {"status": previous["status"], "turn_start": None},
                               {"status": "starting", "turn_start": token, "turn_id": previous.get("turn_id")})
            if result is not None and known_rejected and self.store.resources:
                rows = self.store.db.execute("SELECT id FROM resource_reservations WHERE task_id=? AND token=? AND turns>0 AND native_key IS NULL", (task_id, token)).fetchall()
                for row in rows:
                    self.store.resources.finish(row[0], rejected=True)
            if result is not None and not known_rejected:
                self.store.claim(task_id, "turn", "uncertain", {"reason": "turn_rejected"})
            return result

    def reverted(self, task_id, revision):
        result = self._write(task_id, {**turn_fields(), "status": "idle", "turn_id": None,
            "projection_turn_id": None, "turn_status": None, "diff_turn_id": None,
            "evidence_complete": False, "evidence_gaps": ["history_reverted"]}, {"revision": revision})
        return result

    def start_failed(self, task_id):
        task = self.store.get("tasks", task_id)
        if task["status"] in ("starting", "uncertain"):
            if not task.get("thread_id"):
                self.store.claim(task_id, "turn", "inactive")
            return self._write(task_id, {"status": "needs_resume" if task.get("thread_id") else "failed"})
        return task
