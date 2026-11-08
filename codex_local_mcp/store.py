"""A single broker owns the SQLite connection. Events and state commit together."""

import json
import os
import sqlite3
import time

from .security import BridgeError, private_file, private_dir, redact


class Store:
    def __init__(self, directory):
        private_dir(directory)
        path = directory / "state.sqlite3"
        os.close(private_file(path, os.O_CREAT | os.O_RDWR))
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT NOT NULL,
                at REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id,seq);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, task_id TEXT);
        """)
        self.db.commit()

    def save(self, table, value):
        assert table in ("tasks", "requests", "projects")
        self.db.execute(f"INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (value["id"], json.dumps(redact(value))))

    def get(self, table, key):
        assert table in ("tasks", "requests", "projects")
        row = self.db.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise BridgeError(f"Unknown {table[:-1]} id")
        return json.loads(row[0])

    def all(self, table):
        assert table in ("tasks", "requests", "projects")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def event(self, task_id, kind, data):
        cursor = self.db.execute("INSERT INTO events(task_id,kind,at,data) VALUES (?,?,?,?)",
                                 (task_id, kind, time.time(), json.dumps(redact(data))))
        return cursor.lastrowid

    def highwater(self):
        return self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]

    def events(self, after, limit, task_id=None, task_ids=None):
        if after > self.highwater():
            raise BridgeError("Cursor is ahead of this journal; check the state directory")
        query = "SELECT * FROM events WHERE seq>?"
        args = [after]
        if task_ids:
            query += " AND task_id IN (" + ",".join("?" for _ in task_ids) + ")"
            args.extend(task_ids)
        elif task_id:
            query += " AND task_id=?"
            args.append(task_id)
        rows = self.db.execute(query + " ORDER BY seq LIMIT ?", (*args, limit + 1))
        events, size, more = [], 0, False
        for row in rows:
            if len(events) == limit or (events and size + len(row["data"]) > 1024 * 1024):
                more = True
                break
            size += len(row["data"])
            events.append({**dict(row), "data": json.loads(row["data"])})
        # With a filter, consuming through highwater skips only irrelevant events.
        cursor = events[-1]["seq"] if more else self.highwater()
        return {"events": events, "cursor": cursor, "has_more": more}

    def close(self):
        self.db.close()
