"""Observed family resources and admission reservations, never billing estimates."""

import json
import time
import uuid
from functools import wraps

from .lifecycle import TERMINAL
from .security import BridgeError
from .supervision import touch, STREAM_ONLY
from . import native_turns

LIMITS = {"max_turns": 1000000, "max_active_seconds": 31536000, "max_tokens": 10**12,
          "max_output_bytes": 10**12, "max_children": 10000}
SOFT = ("max_active_seconds", "max_tokens", "max_output_bytes")
TABLES = ("resource_families", "resource_threads", "resource_turns", "resource_reservations", "resource_storage", "resource_interrupts", "resource_totals")


def atomic(function):
    @wraps(function)
    def write(self, *args, **kwargs):
        with self.store.transaction():
            return function(self, *args, **kwargs)
    return write


def validate_budget(value):
    if not isinstance(value, dict) or set(value) - LIMITS.keys():
        raise BridgeError("budget must be an object containing only max_turns, max_active_seconds, max_tokens, max_output_bytes and max_children", code="invalid_arguments")
    for name, maximum in LIMITS.items():
        if name in value and (type(value[name]) is not int or not 1 <= value[name] <= maximum):
            raise BridgeError(f"budget.{name} must be an integer between 1 and {maximum}", code="invalid_arguments")
    return dict(value)


def migrate(db):
    for statement in (
        "CREATE TABLE resource_families (family_id TEXT PRIMARY KEY, budget TEXT NOT NULL, stop TEXT, revision INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE resource_threads (task_id TEXT PRIMARY KEY, thread_id TEXT, tokens TEXT, token_turn INTEGER NOT NULL DEFAULT 0, gaps TEXT NOT NULL, historical INTEGER NOT NULL, goal TEXT)",
        "CREATE TABLE resource_turns (task_id TEXT NOT NULL, turn_id TEXT NOT NULL, started_at REAL, ended_at REAL, seconds REAL, seconds_source TEXT NOT NULL DEFAULT 'unknown', connection TEXT, server_started INTEGER NOT NULL DEFAULT 0, unknown INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id,turn_id))",
        "CREATE INDEX resource_open_turns ON resource_turns(connection) WHERE ended_at IS NULL",
        "CREATE TABLE resource_reservations (id TEXT PRIMARY KEY, family_id TEXT NOT NULL, task_id TEXT, turns INTEGER NOT NULL, children INTEGER NOT NULL, state TEXT NOT NULL, token TEXT, native_key TEXT, turn_id TEXT, after_turn_id TEXT, budget TEXT, queue_id TEXT, queue_owner TEXT)",
        "CREATE INDEX resource_reserved_family ON resource_reservations(family_id,state)",
        "CREATE INDEX resource_reserved_task ON resource_reservations(task_id,token,native_key)",
        "CREATE TABLE resource_storage (task_id TEXT PRIMARY KEY, journal_bytes INTEGER NOT NULL DEFAULT 0, artifact_bytes INTEGER NOT NULL DEFAULT 0, initialized INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE resource_interrupts (family_id TEXT NOT NULL, task_id TEXT NOT NULL, turn_id TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(family_id,task_id,turn_id))",
    ):
        db.execute(statement)
    # No scan/backfill: old payloads are counted once, only on a usage read.
    for table, expression, column in (("events", "LENGTH(CAST({}.data AS BLOB))", "journal_bytes"),
                                      ("artifacts", "COALESCE(LENGTH({}.content),0)", "artifact_bytes")):
        for operation in ("INSERT", "DELETE", "UPDATE"):
            statements = []
            if operation != "INSERT":
                statements.append(f"UPDATE resource_storage SET {column}=MAX(0,{column}-{expression.format('OLD')}) WHERE task_id=OLD.task_id;")
            if operation != "DELETE":
                statements.append(f"INSERT INTO resource_storage(task_id,{column}) SELECT NEW.task_id,{expression.format('NEW')} WHERE NEW.task_id IS NOT NULL ON CONFLICT(task_id) DO UPDATE SET {column}={column}+excluded.{column};")
            owner = "OLD" if operation == "DELETE" else "NEW"
            coalesced = " AND " + owner + ".kind NOT IN (" + ",".join("'" + kind + "'" for kind in sorted(STREAM_ONLY)) + ")" if table == "events" else ""
            statements.append(f"UPDATE resource_families SET revision=revision+1 WHERE family_id=(SELECT root_task_id FROM tasks WHERE id={owner}.task_id)" + coalesced + ";")
            db.execute(f"CREATE TRIGGER resource_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN " + " ".join(statements) + " END")


def migrate_projections(db):
    db.execute("ALTER TABLE resource_threads ADD COLUMN token_baseline TEXT")
    db.execute("""CREATE TABLE resource_totals(task_id TEXT PRIMARY KEY, server_turns INTEGER NOT NULL DEFAULT 0,
        native_turns INTEGER NOT NULL DEFAULT 0, closed_seconds REAL NOT NULL DEFAULT 0,
        closed_unknown INTEGER NOT NULL DEFAULT 0, observed_turns INTEGER NOT NULL DEFAULT 0)""")
    db.execute("CREATE INDEX resource_live_task ON resource_turns(task_id) WHERE ended_at IS NULL")
    expressions = {"server_turns": "server_started", "native_turns": "1-server_started",
        "closed_seconds": "CASE WHEN ended_at IS NOT NULL THEN COALESCE(seconds,0) ELSE 0 END",
        "closed_unknown": "CASE WHEN ended_at IS NOT NULL AND seconds IS NULL THEN 1 ELSE 0 END",
        "observed_turns": "CASE WHEN started_at IS NOT NULL THEN 1 ELSE 0 END"}
    names = list(expressions)
    db.execute("INSERT INTO resource_totals SELECT task_id," + ",".join("SUM(" + value + ")" for value in expressions.values()) + " FROM resource_turns GROUP BY task_id")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        statements = []
        for owner, sign in (("OLD", "-"), ("NEW", "+")):
            if operation == "INSERT" and owner == "OLD" or operation == "DELETE" and owner == "NEW":
                continue
            if owner == "NEW":
                statements.append("INSERT OR IGNORE INTO resource_totals(task_id) VALUES (NEW.task_id);")
            qualified = {name: expression for name, expression in expressions.items()}
            for name, expression in qualified.items():
                for column in ("server_started", "ended_at", "seconds", "started_at"):
                    expression = expression.replace(column, owner + "." + column)
                qualified[name] = expression
            statements.append("UPDATE resource_totals SET " + ",".join(name + "=" + name + sign + "(" + qualified[name] + ")" for name in names) + " WHERE task_id=" + owner + ".task_id;")
        db.execute("CREATE TRIGGER resource_turns_" + operation.lower() + " AFTER " + operation + " ON resource_turns BEGIN " + " ".join(statements) + " END")
    # The old import path lacked an imported flag; its operation record is proof.
    db.execute("""UPDATE resource_threads SET historical=historical | 2 WHERE task_id IN (
        SELECT COALESCE(o.task_id,d.task_id) FROM operation_details d LEFT JOIN operations o ON o.id=d.id
        WHERE d.name='codex_sessions' AND json_extract(d.data,'$.arguments.action')='import')""")
    db.execute("UPDATE resource_threads SET historical=historical & ~2 WHERE task_id IN (SELECT id FROM tasks WHERE parent_task_id IS NOT NULL)")
    db.execute("""UPDATE resource_threads SET token_baseline=json_object('tokens',NULL,'source','unknown_before_upgrade','frozen',json('true'))
        WHERE (historical & 2)!=0 AND (EXISTS(SELECT 1 FROM resource_turns r WHERE r.task_id=resource_threads.task_id)
            OR EXISTS(SELECT 1 FROM resource_reservations r WHERE r.task_id=resource_threads.task_id AND (r.token IS NOT NULL OR r.native_key IS NOT NULL)))""")


class Resources:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.clock = time.monotonic
        self.connection = str(uuid.uuid4())
        self.defaults = {}

    def family(self, task_id):
        row = self.db.execute("SELECT root_task_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        return (row[0] or task_id) if row else task_id

    def register(self, task, *, new=False):
        family = task.get("root_task_id") or task["id"]
        present = self.db.execute("SELECT thread_id FROM resource_threads WHERE task_id=?", (task["id"],)).fetchone()
        if present:
            if task.get("thread_id") and present["thread_id"] != task["thread_id"]:
                with self.store.transaction():
                    self.db.execute("UPDATE resource_threads SET thread_id=? WHERE task_id=?", (task["thread_id"], task["id"]))
            return
        reservation = self.db.execute("SELECT budget FROM resource_reservations WHERE family_id=? AND budget IS NOT NULL LIMIT 1", (family,)).fetchone()
        policy = json.loads(reservation[0]) if reservation else self.defaults if new else {}
        origin = task
        if not new:
            row = self.db.execute("SELECT parent_task_id,json_extract(data,'$.forked_from_task_id') forked_from_task_id,json_extract(data,'$.imported') imported FROM tasks WHERE id=?", (task["id"],)).fetchone()
            origin = dict(row) if row else task
        inherited = not origin.get("parent_task_id") and (origin.get("forked_from_task_id") or origin.get("imported"))
        historical = (2 if inherited else 0) | (1 if not new else 0)
        baseline = json.dumps({"tokens": None, "source": "unknown_before_upgrade", "frozen": True}) if inherited and not new else None
        with self.store.transaction():
            self.db.execute("INSERT OR IGNORE INTO resource_families(family_id,budget) VALUES (?,?)", (family, json.dumps(policy)))
            self.db.execute("INSERT INTO resource_threads(task_id,thread_id,gaps,historical,token_baseline) VALUES (?,?,?,?,?)",
                (task["id"], task.get("thread_id"), "[]", historical, baseline))
            self.db.execute("INSERT OR IGNORE INTO resource_storage(task_id,initialized) VALUES (?,?)", (task["id"], int(new)))
            if new and task.get("parent_task_id"):
                row = self.db.execute("SELECT id,children FROM resource_reservations WHERE family_id=? AND children>0 ORDER BY rowid LIMIT 1", (family,)).fetchone()
                if row:
                    self.db.execute("UPDATE resource_reservations SET children=children-1 WHERE id=?", (row["id"],))
                budget, _ = self.policy(family)
                children = self.db.execute("SELECT COUNT(*)-1 FROM tasks WHERE root_task_id=?", (family,)).fetchone()[0]
                if "max_children" in budget and children > budget["max_children"]:
                    self.store.event(family, "bridge/budget_limit_observed", {"what": "max_children", "observed": children,
                        "limit": budget["max_children"], "source": "native_adoption", "not_preventable": True})

    def policy(self, family):
        row = self.db.execute("SELECT * FROM resource_families WHERE family_id=?", (family,)).fetchone()
        return (json.loads(row["budget"]), json.loads(row["stop"]) if row["stop"] else None) if row else ({}, None)

    @atomic
    def changed(self, task_id):
        family = self.family(task_id)
        self.db.execute("UPDATE resource_families SET revision=revision+1 WHERE family_id=?", (family,))
        touch(self.store, task_id)

    def storage(self, task_id):
        row = self.db.execute("SELECT * FROM resource_storage WHERE task_id=?", (task_id,)).fetchone()
        if not row or not row["initialized"]:
            journal = self.db.execute("SELECT COALESCE(SUM(LENGTH(CAST(data AS BLOB))),0) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
            artifacts = self.db.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM artifacts WHERE task_id=?", (task_id,)).fetchone()[0]
            with self.store.transaction():
                self.db.execute("INSERT INTO resource_storage VALUES (?,?,?,1) ON CONFLICT(task_id) DO UPDATE SET journal_bytes=excluded.journal_bytes,artifact_bytes=excluded.artifact_bytes,initialized=1", (task_id, journal, artifacts))
        return self.db.execute("SELECT journal_bytes,artifact_bytes FROM resource_storage WHERE task_id=?", (task_id,)).fetchone()

    def usage(self, task_id, *, compact=False):
        family, now = self.family(task_id), self.clock()
        heads = self.db.execute("SELECT id,thread_id,json_extract(projection,'$.evidence_complete') evidence_complete FROM tasks WHERE root_task_id=?", (family,)).fetchall()
        gaps, totals, journals, artifacts, historical = set(), [], 0, 0, False
        for head in heads:
            if head["evidence_complete"] == 0:
                gaps.add("native_evidence_gap")
            self.register({"id": head["id"], "thread_id": head["thread_id"], "root_task_id": family})
            row = self.db.execute("SELECT * FROM resource_threads WHERE task_id=?", (head["id"],)).fetchone()
            if row["historical"] & 1:
                historical = True
                gaps.add("not_counted_before_upgrade")
            if row["historical"] & 2:
                gaps.add("inherited_history_not_counted")
            gaps.update(json.loads(row["gaps"]))
            baseline = json.loads(row["token_baseline"]) if row["token_baseline"] else {}
            base = baseline.get("tokens")
            if row["historical"] & 2 and base is None:
                gaps.add("inherited_token_baseline_unknown")
            if row["tokens"]:
                values = json.loads(row["tokens"])
                if row["historical"] & 2 and base is not None:
                    if any(values.get(k) is not None and base.get(k) is not None and values[k] < base[k] for k in values):
                        gaps.add("usage_below_baseline")
                    values = {k: max(0, v - base[k]) if v is not None and base.get(k) is not None else None for k, v in values.items()}
                totals.append(values)
            else:
                gaps.add("tokens_not_reported")
            stored = self.storage(head["id"])
            journals += stored[0]
            artifacts += stored[1]
        measured = self.timing(family, now)
        active, unknown, server, native = measured["active"], bool(measured["unknown"] or historical), measured["server"], measured["native"]
        if unknown:
            gaps.add("active_time_unknown")
        if self.db.execute("""SELECT 1 FROM resource_reservations r JOIN tasks t ON t.id=r.task_id
            JOIN resource_turns u ON u.task_id=t.id AND u.turn_id=json_extract(t.projection,'$.turn_id')
            WHERE t.root_task_id=? AND r.turns>0 AND r.native_key IS NOT NULL
            AND (r.after_turn_id IS NULL OR r.after_turn_id!=u.turn_id) LIMIT 1""", (family,)).fetchone():
            gaps.add("queued_turn_unconfirmed")
        tokens = None
        if len(totals) == len(heads) and totals and all(t.get("total") is not None for t in totals):
            tokens = {key: sum(t[key] for t in totals) if all(t.get(key) is not None for t in totals) else None for key in ("input", "output", "total")}
            if any(value is None for value in tokens.values()):
                gaps.add("token_fields_missing")
        reserved = self.db.execute("SELECT COALESCE(SUM(turns),0),COALESCE(SUM(children),0) FROM resource_reservations WHERE family_id=?", (family,)).fetchone()
        value = {"active_seconds": None if unknown else round(active, 3), "turns": server, "native_turns": native,
            "tokens": tokens, "output_bytes": {"journal": journals, "artifacts": artifacts, "total": journals + artifacts},
            "complete": not gaps, "gaps": sorted(gaps), "reserved_turns": reserved[0], "children": max(0, len(heads) - 1), "reserved_children": reserved[1]}
        budget, _ = self.policy(family)
        value["native_limit_violations"] = ([{"what": "max_children", "observed": value["children"], "limit": budget["max_children"], "not_preventable": True}]
            if "max_children" in budget and value["children"] > budget["max_children"] else [])
        if unknown:
            value["known_active_seconds"] = round(active, 3)
        if not compact:
            goals = self.db.execute("SELECT r.task_id,r.goal FROM resource_threads r JOIN tasks t ON t.id=r.task_id WHERE t.root_task_id=? AND r.goal IS NOT NULL LIMIT 51", (family,)).fetchall()
            value["native_goal_budgets"] = [{"task_id": r[0], **json.loads(r[1])} for r in goals[:50]]
            value["native_goal_budgets_truncated"] = len(goals) > 50
            value["token_budget_preference"] = "native_goal" if goals else "family_soft_limit"
        return value

    def timing(self, family, now=None):
        now = self.clock() if now is None else now
        closed = self.db.execute("""SELECT COALESCE(SUM(r.server_turns),0) server,COALESCE(SUM(r.native_turns),0) native,
            COALESCE(SUM(r.closed_seconds),0) active,COALESCE(SUM(r.closed_unknown),0) unknown
            FROM tasks t JOIN resource_totals r ON r.task_id=t.id WHERE t.root_task_id=?""", (family,)).fetchone()
        live_condition = """r.unknown=0 AND r.connection IS ? AND r.started_at IS NOT NULL
            AND COALESCE(t.status IN ('running','waiting','interrupting','starting'),0) AND r.turn_id IS json_extract(t.projection,'$.turn_id')"""
        live = self.db.execute("""SELECT
            COALESCE(SUM(CASE WHEN seconds IS NOT NULL THEN seconds
                WHEN """ + live_condition + """ THEN MAX(0,?-started_at) ELSE 0 END),0) active,
            COALESCE(SUM(CASE WHEN """ + live_condition + """ THEN 1 ELSE 0 END),0) running,
            COALESCE(SUM(CASE WHEN seconds IS NULL AND NOT(""" + live_condition + """) THEN 1 ELSE 0 END),0) unknown
            FROM tasks t JOIN resource_turns r INDEXED BY resource_live_task ON r.task_id=t.id
            WHERE t.root_task_id=? AND r.ended_at IS NULL""", (self.connection, now, self.connection, self.connection, family)).fetchone()
        return {"server": closed["server"], "native": closed["native"], "active": closed["active"] + live["active"],
            "unknown": closed["unknown"] + live["unknown"], "running": live["running"]}

    def turn_count(self, family):
        return self.db.execute("SELECT COALESCE(SUM(r.server_turns),0) FROM resource_totals r JOIN tasks t ON t.id=r.task_id WHERE t.root_task_id=?", (family,)).fetchone()[0]

    def deadline(self, family, limit):
        now = self.clock()
        measured = self.timing(family, now)
        historical = self.db.execute("SELECT 1 FROM resource_threads r JOIN tasks t ON t.id=r.task_id WHERE t.root_task_id=? AND (r.historical & 1)!=0 LIMIT 1", (family,)).fetchone()
        if measured["unknown"] or historical or not measured["running"]:
            return None
        return now + max(0, (limit - measured["active"]) / measured["running"])

    def error(self, family, what, observed, limit):
        return BridgeError(f"Family budget {what} reached: observed {observed}, limit {limit}. Use codex_settings on the root task to raise the limit or set budget={{}} to clear it", code="budget_exceeded",
            ids={"task_id": family}, details={"what": what, "observed": observed, "limit": limit})

    def breach(self, usage, budget):
        values = {"max_active_seconds": usage["active_seconds"], "max_tokens": (usage["tokens"] or {}).get("total"), "max_output_bytes": usage["output_bytes"]["total"]}
        for key in SOFT:
            observed = values[key]
            if key == "max_tokens" and any(gap.startswith(("usage_", "tokens_", "token_", "inherited_token_baseline_unknown")) for gap in usage["gaps"]):
                continue
            if key in budget and observed is not None and (observed >= budget[key] if key == "max_active_seconds" else observed > budget[key]):
                return key, observed, budget[key]
        return None

    def assert_execution(self, task_id):
        family = self.family(task_id)
        budget, stop = self.policy(family)
        if stop and stop["active"]:
            raise self.error(family, stop["what"], stop["observed"], stop["limit"])
        crossed = self.breach(self.usage(task_id), budget) if any(key in budget for key in SOFT) else None
        if crossed:
            self.stop(family, crossed)
            raise self.error(family, *crossed)

    @atomic
    def admit(self, key, family, name, args, defaults):
        execution = name in ("codex_start", "codex_send", "codex_review", "codex_compact", "codex_delegate") or name == "codex_queue" and args.get("action") in ("add", "start") or name == "codex_goal" and args.get("action") == "set" and args.get("status", "active") == "active"
        if not execution:
            return
        task_id = args.get("task_id")
        if task_id:
            self.assert_execution(task_id)
            budget, _ = self.policy(family)
            counts = self.turn_count(family)
            reserved = self.db.execute("SELECT COALESCE(SUM(turns),0),COALESCE(SUM(children),0) FROM resource_reservations WHERE family_id=?", (family,)).fetchone()
            usage = {"turns": counts, "reserved_turns": reserved[0], "reserved_children": reserved[1],
                "children": self.db.execute("SELECT MAX(COUNT(*)-1,0) FROM tasks WHERE root_task_id=?", (family,)).fetchone()[0]}
        else:
            budget = validate_budget(defaults)
            usage = {"turns": 0, "reserved_turns": 0, "children": 0, "reserved_children": 0}
        goal = name == "codex_goal"
        idle = task_id and self.store.task_head(task_id)["status"] not in ("running", "waiting", "starting", "interrupting")
        turns = int(name in ("codex_start", "codex_review", "codex_compact") or name == "codex_send" and args.get("mode", "follow_up") != "steer" or name == "codex_delegate" and not args.get("expected_turn_id") or name == "codex_queue" and args.get("action") in ("add", "start") or goal and idle)
        if name == "codex_queue" and args.get("action") == "start":
            queued = self.db.execute("SELECT id FROM resource_reservations WHERE task_id=? AND state='queued' AND turns>0 AND queue_owner IS NULL AND (? IS NULL OR queue_id=?) ORDER BY rowid LIMIT 1", (task_id, args.get("queued_id"), args.get("queued_id"))).fetchone()
            if queued:
                turns = 0
        children = int(name == "codex_delegate")
        for field, used, amount in (("max_turns", usage["turns"] + usage["reserved_turns"], turns),
                                    ("max_children", usage["children"] + usage["reserved_children"], children)):
            if amount and field in budget and used + amount > budget[field]:
                raise self.error(family, field, used, budget[field])
        if name == "codex_queue" and args.get("action") == "start" and queued:
            self.db.execute("UPDATE resource_reservations SET queue_owner=? WHERE id=?", (key, queued["id"]))
        self.db.execute("INSERT INTO resource_reservations(id,family_id,task_id,turns,children,state,budget,turn_id,after_turn_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, family, task_id, turns, children, "goal_admitted" if goal else "admitted",
             json.dumps(budget) if name == "codex_start" else None, args.get("expected_turn_id"), self.store.task_head(task_id).get("turn_id") if goal else None))

    @atomic
    def bind(self, key, task_id, token=None, native_key=None, after_turn_id=None):
        before = after_turn_id if native_key is not None else self.store.task_head(task_id).get("turn_id")
        self.db.execute("UPDATE resource_reservations SET task_id=?,token=COALESCE(?,token),native_key=COALESCE(?,native_key),after_turn_id=? WHERE id=?", (task_id, token, native_key, before, key))
        if token:
            self.db.execute("UPDATE resource_reservations SET token=? WHERE queue_owner=?", (token, key))
        if token or native_key:
            self.freeze_baseline(task_id)

    @atomic
    def require_turn(self, key, task_id):
        row = self.db.execute("SELECT turns FROM resource_reservations WHERE id=?", (key,)).fetchone()
        if row is None:
            return self.admit(key, self.family(task_id), "codex_send", {"task_id": task_id}, {})
        if row["turns"] or self.db.execute("SELECT 1 FROM resource_reservations WHERE queue_owner=? AND turns>0", (key,)).fetchone():
            return
        self.assert_execution(task_id)
        family = self.family(task_id)
        budget, _ = self.policy(family)
        count = self.turn_count(family)
        reserved = self.db.execute("SELECT COALESCE(SUM(turns),0) FROM resource_reservations WHERE family_id=?", (family,)).fetchone()[0]
        if "max_turns" in budget and count + reserved + 1 > budget["max_turns"]:
            raise self.error(family, "max_turns", count + reserved, budget["max_turns"])
        self.db.execute("UPDATE resource_reservations SET turns=1 WHERE id=?", (key,))

    @atomic
    def arm_goal(self, key, task_id):
        task = self.store.task_head(task_id)
        if task["status"] not in ("running", "waiting", "starting", "interrupting"):
            self.require_turn(key, task_id)
        self.db.execute("UPDATE resource_reservations SET state='goal_enabling',after_turn_id=? WHERE id=?", (task.get("turn_id"), key))
        self.freeze_baseline(task_id)

    @atomic
    def queue_accepted(self, key, queue_id):
        self.db.execute("UPDATE resource_reservations SET queue_id=?,state='queued' WHERE id=?", (queue_id, key))

    @atomic
    def queue_removed(self, task_id, queue_id):
        self.db.execute("DELETE FROM resource_reservations WHERE task_id=? AND queue_id=? AND turns>0", (task_id, queue_id))

    @atomic
    def finish(self, key, *, rejected=False, queued=False, uncertain=False):
        if not uncertain:
            self.db.execute("UPDATE resource_reservations SET queue_owner=NULL WHERE queue_owner=?", (key,))
        row = self.db.execute("SELECT * FROM resource_reservations WHERE id=?", (key,)).fetchone()
        if not row:
            return
        if rejected or not uncertain and not queued and not row["children"] and not row["turns"]:
            self.db.execute("DELETE FROM resource_reservations WHERE id=?", (key,))
            if rejected and row["task_id"]:
                self.reopen_baseline(row["task_id"])
        else:
            state = ("goal_uncertain" if uncertain else "goal_active") if row["state"].startswith("goal_") else "uncertain" if uncertain else "queued" if queued else "started"
            self.db.execute("UPDATE resource_reservations SET state=? WHERE id=?", (state, key))

    @atomic
    def begin(self, task, turn_id, *, source, token=None, reservation_id=None):
        self.register(task)
        row = self.db.execute("SELECT server_started FROM resource_turns WHERE task_id=? AND turn_id=?", (task["id"], turn_id)).fetchone()
        reservation = self.db.execute("SELECT * FROM resource_reservations WHERE task_id=? AND turns>0 AND (token=? OR id=?) AND (after_turn_id IS NULL OR after_turn_id!=?) ORDER BY (token=?) DESC,rowid LIMIT 1", (task["id"], token or task.get("turn_start"), reservation_id, turn_id, token or task.get("turn_start"))).fetchone()
        queued = self.db.execute("SELECT 1 FROM resource_reservations WHERE task_id=? AND turns>0 AND native_key IS NOT NULL LIMIT 1", (task["id"],)).fetchone() if reservation is None and row is None else None
        if reservation is None and row is None and not queued:
            reservation = self.db.execute("""SELECT * FROM resource_reservations WHERE task_id=? AND turns>0
                AND state IN ('goal_enabling','goal_active','goal_uncertain') AND (after_turn_id IS NULL OR after_turn_id!=?)
                ORDER BY rowid LIMIT 1""", (task["id"], turn_id)).fetchone()
        server = bool(reservation or source == "ack" and token)
        if not row:
            if source in ("event", "ack"):
                self.freeze_baseline(task["id"])
            started = self.clock() if source in ("event", "ack") else None
            self.db.execute("INSERT INTO resource_turns(task_id,turn_id,started_at,connection,server_started,unknown) VALUES (?,?,?,?,?,?)", (task["id"], turn_id, started, self.connection, int(server), int(started is None)))
            self.changed(task["id"])
        elif server and not row["server_started"]:
            self.db.execute("UPDATE resource_turns SET server_started=1 WHERE task_id=? AND turn_id=?", (task["id"], turn_id))
            self.changed(task["id"])
        if reservation:
            self.db.execute("UPDATE resource_reservations SET turns=0,turn_id=? WHERE id=?", (turn_id, reservation["id"]))

    @atomic
    def merge_nested(self, task_id, nested_id, root_id):
        if nested_id == root_id:
            return
        alias = self.db.execute("SELECT * FROM resource_turns WHERE task_id=? AND turn_id=?", (task_id, nested_id)).fetchone()
        if not alias:
            return
        root = self.db.execute("SELECT * FROM resource_turns WHERE task_id=? AND turn_id=?", (task_id, root_id)).fetchone()
        if root:
            if alias["server_started"] and not root["server_started"]:
                self.db.execute("UPDATE resource_turns SET server_started=1 WHERE task_id=? AND turn_id=?", (task_id, root_id))
            self.db.execute("DELETE FROM resource_turns WHERE task_id=? AND turn_id=?", (task_id, nested_id))
        else:
            # Nested timing is not evidence of the whole root's duration.
            self.db.execute("UPDATE resource_turns SET turn_id=?,started_at=NULL,ended_at=NULL,seconds=NULL,seconds_source='unknown',unknown=1 WHERE task_id=? AND turn_id=?",
                (root_id, task_id, nested_id))
        self.changed(task_id)

    @atomic
    def reconcile(self, task_id, turn, *, idle=False, queue=(), goal_active=None):
        scope = native_turns.for_task(self.store, task_id)
        if turn and turn.get("rootTurnId") and turn["rootTurnId"] != turn["id"] and scope.foreign(turn["rootTurnId"]):
            self.merge_nested(task_id, turn["rootTurnId"], turn["id"])
        turn = native_turns.root_view(turn, scope)
        if queue or goal_active:
            self.freeze_baseline(task_id)
        queued = {q["clientUserMessageId"]: q.get("id") for q in queue if isinstance(q.get("clientUserMessageId"), str)}
        for row in self.db.execute("""SELECT r.*,o.state operation_state FROM resource_reservations r LEFT JOIN operations o ON o.id=r.id
            WHERE r.task_id=? ORDER BY (r.state LIKE 'goal_%'),r.rowid""", (task_id,)).fetchall():
            if row["operation_state"] == "running":
                continue
            goal = row["state"].startswith("goal_")
            if row["state"] == "goal_admitted":
                continue
            if row["native_key"] in queued:
                self.db.execute("UPDATE resource_reservations SET queue_id=? WHERE id=?", (queued[row["native_key"]], row["id"]))
                if idle and (turn or {}).get("id") == row["after_turn_id"] and row["queue_owner"]:
                    rejected = self.db.execute("""SELECT 1 FROM operations WHERE id=? AND state!='running'
                        AND json_extract(result,'$.error_data.code') IN ('native_rejected','optional_native_capability_unavailable')
                        AND json_extract(result,'$.error_data.uncertain')=0""", (row["queue_owner"],)).fetchone()
                    if rejected:
                        self.db.execute("UPDATE resource_reservations SET queue_owner=NULL WHERE id=?", (row["id"],))
                continue
            if turn and turn["id"] != row["after_turn_id"] and row["turns"]:
                observed = self.db.execute("SELECT server_started FROM resource_turns WHERE task_id=? AND turn_id=?", (task_id, turn["id"])).fetchone()
                if (row["native_key"] or goal) and observed and observed[0]:
                    if goal and goal_active is False:
                        self.db.execute("DELETE FROM resource_reservations WHERE id=?", (row["id"],))
                    continue  # One observed turn cannot prove two submissions ran.
                self.begin(self.store.task_head(task_id), turn["id"], source="ack", token=row["token"], reservation_id=row["id"])
            if goal and goal_active is False or idle and (not turn or turn["status"] in TERMINAL):
                self.db.execute("DELETE FROM resource_reservations WHERE id=?", (row["id"],))
        if turn and turn["status"] in TERMINAL:
            self.complete(task_id, turn["id"], turn, recovered=True)
        elif turn and turn["status"] == "inProgress":
            started, wall = turn.get("startedAt"), time.time()
            if type(started) is int and 0 <= started <= wall:
                changed = self.db.execute("UPDATE resource_turns SET started_at=?,connection=?,unknown=0 WHERE task_id=? AND turn_id=? AND ended_at IS NULL AND unknown=1",
                    (self.clock() - (wall - started), self.connection, task_id, turn["id"]))
                if changed.rowcount:
                    self.changed(task_id)

    @atomic
    def complete(self, task_id, turn_id, native=None, *, recovered=False):
        scope = native_turns.for_task(self.store, task_id)
        if native_turns.nested(native, scope):
            return
        if native and ("id" in native or "rootTurnId" in native):
            turn_id = native_turns.logical_id(native, scope) or turn_id
        row = self.db.execute("SELECT * FROM resource_turns WHERE task_id=? AND turn_id=?", (task_id, turn_id)).fetchone()
        if row is None:
            return
        duration = (native or {}).get("durationMs")
        start, end = (native or {}).get("startedAt"), (native or {}).get("completedAt")
        seconds = duration / 1000 if type(duration) is int and 0 <= duration < 2**63 else (
            max(0, end - start) if type(start) is int and type(end) is int and 0 <= start <= end < 2**63 else None)
        source = "native" if seconds is not None else "unknown"
        if seconds is None and not recovered and not row["unknown"] and row["connection"] == self.connection and row["started_at"] is not None:
            seconds = max(0, self.clock() - row["started_at"])
            source = "events"
        if row["ended_at"] is None or row["seconds"] is None and seconds is not None or source == "native" and row["seconds_source"] != "native":
            self.db.execute("UPDATE resource_turns SET ended_at=?,seconds=?,seconds_source=?,unknown=? WHERE task_id=? AND turn_id=?", (self.clock(), seconds, source, int(seconds is None), task_id, turn_id))
            self.changed(task_id)
        self.db.execute("DELETE FROM resource_reservations WHERE task_id=? AND turn_id=? AND turns=0", (task_id, turn_id))

    @atomic
    def tokens(self, task, params):
        self.register(task)
        raw = (params.get("tokenUsage") or {}).get("total") or {}
        values = {key: raw.get(native) if type(raw.get(native)) is int and raw[native] >= 0 else None for key, native in (("input", "inputTokens"), ("output", "outputTokens"), ("total", "totalTokens"))}
        valid_total, partial = values["total"] is not None, any(v is None for v in values.values())
        row = self.db.execute("SELECT * FROM resource_threads WHERE task_id=?", (task["id"],)).fetchone()
        gaps, old = set(json.loads(row["gaps"])), json.loads(row["tokens"]) if row["tokens"] else {}
        ordinal = self.db.execute("SELECT ordinal FROM turns WHERE task_id=? AND turn_id=?", (task["id"], params.get("turnId"))).fetchone()
        ordinal = ordinal[0] if ordinal else 0
        if ordinal:
            gaps.discard("usage_turn_unknown")
        else:
            gaps.add("usage_turn_unknown")
        current = self.db.execute("SELECT ordinal FROM turns WHERE task_id=? AND turn_id=?", (task["id"], task.get("turn_id"))).fetchone()
        if ordinal and current and ordinal < current[0]:
            gaps.add("usage_delayed")
        elif ordinal and current and ordinal == current[0]:
            gaps.discard("usage_delayed")
        late = (ordinal and ordinal < row["token_turn"]) or any(old.get(k) is not None and values[k] is not None and values[k] < old[k] for k in values)
        if late:
            gaps.add("usage_out_of_order")
            values = {k: max(old.get(k) or 0, value) if value is not None else old.get(k) for k, value in values.items()}
        elif valid_total and not partial:
            gaps.discard("usage_restart_gap")
        values = {key: value if value is not None else old.get(key) for key, value in values.items()}
        if partial:
            gaps.add("usage_partial")
        else:
            gaps.discard("usage_partial")
        if not valid_total:
            gaps.add("usage_unavailable")
        else:
            gaps.discard("usage_unavailable")
        baseline = json.loads(row["token_baseline"]) if row["token_baseline"] else {}
        if row["historical"] & 2 and not baseline.get("frozen"):
            counts = self.db.execute("SELECT observed_turns,server_turns FROM resource_totals WHERE task_id=?", (task["id"],)).fetchone()
            if task["status"] in ("running", "waiting", "starting", "interrupting", "queued") or counts and (counts[0] or counts[1]):
                self.freeze_baseline(task["id"])
            elif valid_total and not partial and not late:
                self.set_baseline(task["id"], values, "thread_before_first_turn")
        if values != old or json.loads(row["gaps"]) != sorted(gaps) or ordinal > row["token_turn"]:
            self.db.execute("UPDATE resource_threads SET tokens=?,token_turn=MAX(token_turn,?),gaps=? WHERE task_id=?", (json.dumps(values), ordinal, json.dumps(sorted(gaps)), task["id"]))

    def fork_baseline(self, source, last_turn_id=None):
        if last_turn_id is not None and last_turn_id != source.get("turn_id"):
            return None
        row = self.db.execute("SELECT * FROM resource_threads WHERE task_id=?", (source["id"],)).fetchone()
        if not row or not row["tokens"] or json.loads(row["gaps"]):
            return None
        current = self.db.execute("SELECT ordinal FROM turns WHERE task_id=? AND turn_id=?", (source["id"], source.get("turn_id"))).fetchone()
        values = json.loads(row["tokens"])
        return values if current and current[0] == row["token_turn"] and all(type(values.get(k)) is int for k in ("input", "output", "total")) else None

    @atomic
    def imported(self, task_id):
        self.db.execute("UPDATE resource_threads SET historical=historical | 2 WHERE task_id=?", (task_id,))
        self.changed(task_id)

    @atomic
    def set_baseline(self, task_id, values, source):
        encoded = json.dumps({"tokens": values, "source": source, "frozen": False})
        old = self.db.execute("SELECT token_baseline FROM resource_threads WHERE task_id=?", (task_id,)).fetchone()
        if old and old[0] != encoded:
            self.db.execute("UPDATE resource_threads SET token_baseline=? WHERE task_id=?", (encoded, task_id))
            self.changed(task_id)

    @atomic
    def freeze_baseline(self, task_id):
        row = self.db.execute("SELECT historical,token_baseline FROM resource_threads WHERE task_id=?", (task_id,)).fetchone()
        if row and row["historical"] & 2:
            baseline = json.loads(row["token_baseline"]) if row["token_baseline"] else {"tokens": None, "source": "unknown_after_execution"}
            if not baseline.get("frozen"):
                self.db.execute("UPDATE resource_threads SET token_baseline=? WHERE task_id=?", (json.dumps({**baseline, "frozen": True}), task_id))
                self.changed(task_id)

    @atomic
    def reopen_baseline(self, task_id):
        row = self.db.execute("SELECT token_baseline FROM resource_threads WHERE task_id=? AND (historical & 2)!=0", (task_id,)).fetchone()
        if not row or not row[0]:
            return
        baseline = json.loads(row[0])
        if not baseline.get("frozen") or baseline.get("source") == "unknown_before_upgrade":
            return
        counts = self.db.execute("SELECT observed_turns,server_turns FROM resource_totals WHERE task_id=?", (task_id,)).fetchone()
        pending = self.db.execute("SELECT 1 FROM resource_reservations WHERE task_id=? AND (token IS NOT NULL OR native_key IS NOT NULL OR state IN ('goal_enabling','goal_active','goal_uncertain')) LIMIT 1", (task_id,)).fetchone()
        if not pending and (not counts or not counts[0] and not counts[1]):
            self.db.execute("UPDATE resource_threads SET token_baseline=? WHERE task_id=?", (json.dumps({**baseline, "frozen": False}), task_id))
            self.changed(task_id)

    @atomic
    def goal(self, task_id, value):
        task = self.store.task_head(task_id)
        self.register(task)
        if isinstance(value, dict) and value.get("status") == "active":
            self.freeze_baseline(task_id)
        goal = {"source": "native_goal", "token_budget": value.get("tokenBudget"), "tokens_used": value.get("tokensUsed"), "status": value.get("status")} if isinstance(value, dict) and value.get("tokenBudget") is not None else None
        encoded = json.dumps(goal) if goal else None
        old = self.db.execute("SELECT goal FROM resource_threads WHERE task_id=?", (task_id,)).fetchone()[0]
        if old != encoded:
            self.db.execute("UPDATE resource_threads SET goal=? WHERE task_id=?", (encoded, task_id))

    def disconnect(self):
        with self.store.transaction():
            self.db.execute("UPDATE resource_interrupts SET state='unknown' WHERE state='requested'")
            for row in self.db.execute("SELECT task_id,gaps FROM resource_threads WHERE tokens IS NOT NULL").fetchall():
                gaps = set(json.loads(row["gaps"]))
                if "usage_restart_gap" not in gaps:
                    self.db.execute("UPDATE resource_threads SET gaps=? WHERE task_id=?", (json.dumps(sorted(gaps | {"usage_restart_gap"})), row["task_id"]))
                    touch(self.store, row["task_id"])
            rows = self.db.execute("SELECT DISTINCT task_id FROM resource_turns WHERE ended_at IS NULL AND unknown=0").fetchall()
            self.db.execute("UPDATE resource_turns SET unknown=1 WHERE ended_at IS NULL AND unknown=0")
            for row in rows:
                touch(self.store, row[0])
        self.connection = str(uuid.uuid4())

    def set_budget(self, task_id, value):
        budget = validate_budget(value)
        if self.family(task_id) != task_id:
            raise BridgeError("Family budgets can be changed only on the root task", code="invalid_arguments")
        task = self.store.task_head(task_id)
        self.register(task)
        with self.store.transaction():
            _, stop = self.policy(task_id)
            relaxed = stop and (stop["what"] not in budget or (
                budget[stop["what"]] > stop["limit"] and budget[stop["what"]] >= stop["observed"]))
            if relaxed and not self.breach(self.usage(task_id), budget):
                stop = {**stop, "active": False, "cleared_at": time.time()}
            self.db.execute("UPDATE resource_families SET budget=?,stop=?,revision=revision+1 WHERE family_id=?", (json.dumps(budget), json.dumps(stop) if stop else None, task_id))
            self.store.event(task_id, "bridge/budget_changed", {"budget": budget})

    @atomic
    def stop(self, family, crossed):
        _, prior = self.policy(family)
        if prior and prior["active"]:
            return prior
        what, observed, limit = crossed
        stopped = {"reason": "budget", "what": what, "observed": observed, "limit": limit, "may_overshoot": True,
            "message": "Cooperative budget stop; may overshoot. Commands already executed are not rolled back", "at": time.time(), "active": True,
            "evidence_ref": {"tool": "codex_get", "arguments": {"task_id": family, "view": "result"}}}
        self.db.execute("UPDATE resource_families SET stop=?,revision=revision+1 WHERE family_id=?", (json.dumps(stopped), family))
        self.store.event(family, "bridge/budget_stop", stopped)
        return stopped
