"""Durable, single-use approvals shared by all API workers on one host."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


class ActionStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("TORRE_ACTION_DB", str(Path(__file__).parent / ".assistant-state" / "actions.sqlite3")))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, owner TEXT, project TEXT, cluster TEXT, payload TEXT, state TEXT, created REAL, expires REAL, result TEXT)")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def owner(session):
        return hashlib.sha256(session.encode()).hexdigest()

    @staticmethod
    def public(row):
        payload = json.loads(row["payload"])
        state = row["state"]
        if state == "pending" and row["expires"] < time.time():
            state = "expired"
        return {"id": row["id"], "state": state, "expires_at": row["expires"],
                "operation": payload["operation"], "arguments": payload["arguments"],
                "details": payload["details"], "destructive": payload["destructive"],
                "result": json.loads(row["result"]) if row["result"] else None}

    def create(self, session, project, cluster, payload):
        now, action_id = time.time(), secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM actions WHERE created < ? AND state != 'executing'", (now - 7 * 86400,))
            pending = conn.execute("SELECT count(*) FROM actions WHERE owner=? AND state='pending' AND expires>?", (self.owner(session), now)).fetchone()[0]
            if pending >= 20:
                raise ValueError("Há 20 ações pendentes; aprove ou cancele antes de preparar outras.")
            conn.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?)", (action_id, self.owner(session), project, cluster, json.dumps(payload), "pending", now, now + 900, None))
            row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return self.public(row)

    def get(self, action_id, session, project, cluster):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id=? AND owner=? AND project=? AND cluster=?", (action_id, self.owner(session), project, cluster)).fetchone()
        if row is None:
            raise ValueError("Ação não encontrada para esta sessão e cluster.")
        return row

    def claim(self, action_id, session, project, cluster, cancel=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM actions WHERE id=? AND owner=? AND project=? AND cluster=?", (action_id, self.owner(session), project, cluster)).fetchone()
            if row is None:
                raise ValueError("Ação não encontrada para esta sessão e cluster.")
            if row["state"] != "pending":
                return False, row
            state = "expired" if row["expires"] <= time.time() else "cancelled" if cancel else "executing"
            conn.execute("UPDATE actions SET state=? WHERE id=?", (state, action_id))
            row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        return state == "executing", row

    def finish(self, action_id, state, result):
        with self.connect() as conn:
            conn.execute("UPDATE actions SET state=?,result=? WHERE id=? AND state='executing'", (state, json.dumps(result), action_id))

    def recent(self, session, project, cluster):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM actions WHERE owner=? AND project=? AND cluster=? ORDER BY created DESC LIMIT 20", (self.owner(session), project, cluster)).fetchall()
        return [self.public(row) for row in rows]


store = ActionStore()
