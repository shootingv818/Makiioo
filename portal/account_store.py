"""Atomic account/session/worker persistence for a completed portal login."""
from __future__ import annotations

import json
import sqlite3

import db


def _session_blob(values: dict) -> str:
    try:
        import crypto_util
        return crypto_util.encrypt(json.dumps(values or {}))
    except Exception:
        return db.session_pack(values or {})


def save_atomic(
    *, phone: str, name: str, user_id: str, session_path: str,
    session_values: dict, worker_id: int, contacts: int = 0, groups: int = 0,
) -> int:
    if not session_values or not session_values.get("auth"):
        raise RuntimeError("portable session is incomplete")
    if not worker_id:
        raise RuntimeError("worker assignment is missing")
    conn = sqlite3.connect(db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS portal_account_meta(
            account_id INTEGER PRIMARY KEY,
            contacts INTEGER NOT NULL DEFAULT 0,
            groups INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )""")
        conn.execute(
            """INSERT INTO accounts(phone,name,user_id,session,added_at,status,worker_id,session_blob)
               VALUES(?,?,?,?,datetime('now','localtime'),'active',?,?)""",
            (phone, name, user_id, session_path, int(worker_id), _session_blob(session_values)),
        )
        row = conn.execute("SELECT id,worker_id,session_blob FROM accounts WHERE phone=?", (phone,)).fetchone()
        if not row or int(row["worker_id"] or 0) != int(worker_id) or not row["session_blob"]:
            raise RuntimeError("account persistence verification failed")
        conn.execute(
            "INSERT OR REPLACE INTO portal_account_meta(account_id,contacts,groups,updated_at) "
            "VALUES(?,?,?,datetime('now','localtime'))",
            (int(row["id"]), max(0, int(contacts)), max(0, int(groups))),
        )
        conn.commit()
        return int(row["id"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
