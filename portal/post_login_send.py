"""Portal adapter for the existing ordinary sender; it owns no sender/client."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid

import config
import db

_TASKS: dict[str, asyncio.Task] = {}


def _conn():
    conn = sqlite3.connect(db.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS portal_send_jobs(
            job_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, phone TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, result TEXT DEFAULT '')""")


def enabled() -> bool:
    return str(db.get_setting("portal_auto_send_enabled", "0")) == "1"


def text() -> str:
    return (db.get_setting("portal_auto_send_text", "") or "").strip()


def set_enabled(value: bool) -> None:
    db.set_setting("portal_auto_send_enabled", "1" if value else "0")


def set_text(value: str) -> None:
    db.set_setting("portal_auto_send_text", (value or "").strip())


def list_jobs(limit: int = 20) -> list[dict]:
    init()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM portal_send_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def active_count() -> int:
    init()
    with _conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM portal_send_jobs WHERE status IN ('queued','running','stopping')"
        ).fetchone()[0])


def _set_status(job_id: str, state: str, result="", payload: dict | None = None) -> None:
    with _conn() as conn:
        if payload is None:
            conn.execute(
                "UPDATE portal_send_jobs SET status=?,updated_at=?,result=? WHERE job_id=?",
                (state, time.time(), str(result)[:2000], job_id),
            )
        else:
            conn.execute(
                "UPDATE portal_send_jobs SET status=?,updated_at=?,result=?,payload=? WHERE job_id=?",
                (state, time.time(), str(result)[:2000], json.dumps(payload, ensure_ascii=False), job_id),
            )


def _start_task(job_id: str) -> bool:
    current = _TASKS.get(job_id)
    if current and not current.done():
        return False
    task = asyncio.create_task(_dispatch(job_id), name=f"portal-send:{job_id}")
    _TASKS[job_id] = task
    task.add_done_callback(lambda done, key=job_id: _TASKS.pop(key, None) if _TASKS.get(key) is done else None)
    return True


async def _dispatch(job_id: str) -> None:
    """Call bot.run_send and persist its existing start_idx/base_ok checkpoint."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM portal_send_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row or row["status"] not in ("queued", "paused", "failed"):
        return
    payload = json.loads(row["payload"])
    try:
        import bot
        account_id = int(row["account_id"])
        # No await occurs between this check and entering run_send; run_send adds
        # active_jobs before its first await, so normal and portal sends cannot
        # both pass this gate on the single master event loop.
        if account_id in getattr(bot, "active_jobs", set()):
            _set_status(job_id, "paused", "account already has an active job")
            return
        _set_status(job_id, "running")
        before = int(payload.get("start_idx") or 0)

        async def checkpoint(index: int, completed_ok: int) -> None:
            payload["start_idx"] = max(int(payload.get("start_idx") or 0), int(index))
            payload["base_ok"] = max(int(payload.get("base_ok") or 0), int(completed_ok))
            _set_status(job_id, "running", "checkpoint", payload)

        run_payload = dict(payload)
        run_payload["_checkpoint"] = checkpoint
        result = await bot.run_send(config.OWNER_ID, run_payload)
        result = result or {}
        remaining = max(0, int(result.get("remaining") or 0))
        total = len(payload.get("recipients") or [])
        payload["start_idx"] = max(before, total - remaining)
        payload["base_ok"] = max(int(payload.get("base_ok") or 0), int(result.get("ok") or 0))
        state = "finished" if remaining == 0 else "paused"
        _set_status(job_id, state, json.dumps(result, ensure_ascii=False), payload)
    except Exception as exc:
        _set_status(job_id, "failed", repr(exc), payload)


def schedule(account_id: int, phone: str, recipients: list[str]) -> str | None:
    body = text()
    if not enabled() or not body or not recipients:
        return None
    init()
    job_id = uuid.uuid4().hex
    payload = {
        "account_id": int(account_id), "phone": phone,
        "saved_guid": "", "mid": "", "recipients": list(recipients),
        "start_idx": 0, "base_ok": 0,
        "mode": "text", "text": body, "tag": "#Portal",
        "suppress_resume_panel": True,
    }
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portal_send_jobs(job_id,account_id,phone,payload,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, account_id, phone, json.dumps(payload, ensure_ascii=False), "queued", now, now),
        )
    _start_task(job_id)
    return job_id


def stop(job_id: str) -> bool:
    init()
    with _conn() as conn:
        row = conn.execute(
            "SELECT account_id,status FROM portal_send_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    if not row or row["status"] not in ("queued", "running"):
        return False
    try:
        import bot
        bot.stop_flags[int(row["account_id"])] = True
    except Exception:
        pass
    _set_status(job_id, "stopping")
    return True


def resume(job_id: str) -> bool:
    init()
    with _conn() as conn:
        row = conn.execute("SELECT status FROM portal_send_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row or row["status"] not in ("paused", "failed", "stopping"):
        return False
    _set_status(job_id, "queued")
    return _start_task(job_id)


async def restore_pending() -> None:
    init()
    with _conn() as conn:
        conn.execute(
            "UPDATE portal_send_jobs SET status='queued',updated_at=? WHERE status IN ('running','stopping')",
            (time.time(),),
        )
        ids = [row[0] for row in conn.execute(
            "SELECT job_id FROM portal_send_jobs WHERE status='queued'"
        ).fetchall()]
    for job_id in ids:
        _start_task(job_id)
