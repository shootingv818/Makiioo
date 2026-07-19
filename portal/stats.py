"""Durable, isolated portal attempt/event statistics in the main SQLite DB."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

import config
import db

TERMINAL = {"success", "expired", "failed"}
_LOCK = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init() -> None:
    with _LOCK, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portal_attempts (
                attempt_id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                owner_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                wrong_code_events INTEGER NOT NULL DEFAULT 0,
                account_id INTEGER,
                last_error TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                event TEXT NOT NULL,
                created_at REAL NOT NULL,
                detail TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_attempt_status_exp ON portal_attempts(status, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_attempt_created ON portal_attempts(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_events_created ON portal_events(created_at, event)")


def create_attempt(attempt_id: str, phone: str, owner_hash: str, created_at: float, expires_at: float) -> None:
    init()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO portal_attempts(attempt_id,phone,owner_hash,status,created_at,expires_at) VALUES(?,?,?,'pending',?,?)",
            (attempt_id, phone, owner_hash, created_at, expires_at),
        )


def mark_started(attempt_id: str, now: float | None = None) -> bool:
    now = now or time.time()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "UPDATE portal_attempts SET started_at=COALESCE(started_at, ?) WHERE attempt_id=? AND status='pending' AND started_at IS NULL",
            (now, attempt_id),
        )
        if cur.rowcount:
            conn.execute("INSERT INTO portal_events(attempt_id,event,created_at) VALUES(?,'started',?)", (attempt_id, now))
        return bool(cur.rowcount)


def wrong_code(attempt_id: str, detail: str = "", now: float | None = None) -> int:
    now = now or time.time()
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE portal_attempts SET wrong_code_events=wrong_code_events+1,last_error=? WHERE attempt_id=? AND status='pending'",
            (detail[:240], attempt_id),
        )
        conn.execute(
            "INSERT INTO portal_events(attempt_id,event,created_at,detail) VALUES(?,'wrong_code',?,?)",
            (attempt_id, now, detail[:240]),
        )
        row = conn.execute("SELECT wrong_code_events FROM portal_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return int(row[0]) if row else 0


def finish(attempt_id: str, status: str, *, account_id: int | None = None, error: str = "", now: float | None = None) -> bool:
    if status not in TERMINAL:
        raise ValueError(f"invalid portal terminal status: {status}")
    now = now or time.time()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "UPDATE portal_attempts SET status=?,finished_at=?,account_id=?,last_error=? WHERE attempt_id=? AND status='pending'",
            (status, now, account_id, error[:240], attempt_id),
        )
        if cur.rowcount:
            conn.execute(
                "INSERT INTO portal_events(attempt_id,event,created_at,detail) VALUES(?,?,?,?)",
                (attempt_id, status, now, error[:240]),
            )
        return bool(cur.rowcount)


def expire_stale(now: float | None = None) -> int:
    """Close persisted pending rows left behind by a previous process."""
    now = now or time.time()
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT attempt_id FROM portal_attempts WHERE status='pending' AND expires_at<=?", (now,)
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE portal_attempts SET status='expired',finished_at=?,last_error='process cleanup' "
                "WHERE attempt_id=? AND status='pending'",
                (now, row[0]),
            )
            conn.execute(
                "INSERT INTO portal_events(attempt_id,event,created_at,detail) VALUES(?,'expired',?,'process cleanup')",
                (row[0], now),
            )
        return len(rows)


def _period_summary(since: float | None) -> dict[str, Any]:
    with _connect() as conn:
        if since is None:
            row = conn.execute(
                "SELECT "
                "SUM(CASE WHEN started_at IS NOT NULL THEN 1 ELSE 0 END) AS started,"
                "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,"
                "SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,"
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
                "COALESCE(SUM(wrong_code_events),0) AS wrong_code_events "
                "FROM portal_attempts"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT "
                "SUM(CASE WHEN started_at>=? THEN 1 ELSE 0 END) AS started,"
                "SUM(CASE WHEN status='success' AND finished_at>=? THEN 1 ELSE 0 END) AS success,"
                "SUM(CASE WHEN status='expired' AND finished_at>=? THEN 1 ELSE 0 END) AS expired,"
                "SUM(CASE WHEN status='failed' AND finished_at>=? THEN 1 ELSE 0 END) AS failed,"
                "(SELECT COUNT(*) FROM portal_events WHERE event='wrong_code' AND created_at>=?) AS wrong_code_events "
                "FROM portal_attempts",
                (since, since, since, since, since),
            ).fetchone()
        # Pending is a current gauge, therefore an attempt crossing midnight is
        # still pending in both the daily card and total card.
        pending = conn.execute(
            "SELECT COUNT(*) FROM portal_attempts WHERE status='pending' AND expires_at>?", (time.time(),)
        ).fetchone()[0]
    out = {key: int(row[key] or 0) for key in ("started", "success", "expired", "failed", "wrong_code_events")}
    out["pending"] = int(pending or 0)
    out["rate"] = round((out["success"] * 100 / out["started"]), 1) if out["started"] else 0.0
    return out


def summary() -> dict[str, dict[str, Any]]:
    init()
    now = config.now_dt()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {"today": _period_summary(start), "total": _period_summary(None)}


def recent(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT attempt_id,phone,status,created_at,expires_at,wrong_code_events,account_id,last_error "
            "FROM portal_attempts ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 100)),)
        ).fetchall()
    return [dict(row) for row in rows]
