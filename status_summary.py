"""Fast async status snapshot for portal cards and stats endpoints.

The default path reads only SQLite plus the portal's in-memory lifecycle state.
Potentially slow work runs off the event loop behind a hard timeout, and a
short-lived cache (with stale fallback) keeps callers responsive.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import sys
import threading
import time
from typing import Any

import db

_CACHE_TTL = 5.0
_DEFAULT_TIMEOUT = 1.0
_CACHE_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"value": None, "expires": 0.0, "created": 0.0}


def _connect() -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db.DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db.DB_PATH, timeout=0.25)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone())


def _count(conn: sqlite3.Connection, query: str, args: tuple = ()) -> int:
    try:
        row = conn.execute(query, args).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def _empty_portal_stats() -> dict:
    blank = {"started": 0, "success": 0, "pending": 0, "expired": 0,
             "failed": 0, "wrong_code_events": 0, "rate": 0.0}
    return {"today": dict(blank), "total": dict(blank)}


def _collect_sync() -> dict:
    now = time.time()
    accounts = {"total": 0, "active": 0, "inactive": 0, "busy": 0}
    telegram = {"total": 0, "active": 0, "inactive": 0}
    workers = {"total": 0, "enabled": 0, "healthy": 0, "unhealthy": 0,
               "unknown": 0, "items": []}
    jobs = {"active": 0, "paused": 0, "completed": 0, "failed": 0}

    with _connect() as conn:
        if _has_table(conn, "accounts"):
            accounts["total"] = _count(conn, "SELECT COUNT(*) FROM accounts")
            accounts["active"] = _count(
                conn, "SELECT COUNT(*) FROM accounts WHERE status='active'")
            accounts["inactive"] = max(0, accounts["total"] - accounts["active"])
        if _has_table(conn, "tg_accounts"):
            telegram["total"] = _count(conn, "SELECT COUNT(*) FROM tg_accounts")
            telegram["active"] = _count(
                conn, "SELECT COUNT(*) FROM tg_accounts WHERE status='active'")
            telegram["inactive"] = max(0, telegram["total"] - telegram["active"])
        if _has_table(conn, "workers"):
            rows = conn.execute(
                "SELECT id,tag,ip,is_master,enabled,status,ping_ms,file_ok,last_checked "
                "FROM workers ORDER BY id").fetchall()
            workers["total"] = len(rows)
            for row in rows:
                item = dict(row)
                item["enabled"] = bool(item["enabled"])
                item["file_ok"] = bool(item["file_ok"])
                workers["items"].append(item)
                if not item["enabled"]:
                    continue
                workers["enabled"] += 1
                if item.get("status") == "ok":
                    workers["healthy"] += 1
                elif item.get("status") in ("down", "blocked", "failed"):
                    workers["unhealthy"] += 1
                else:
                    workers["unknown"] += 1
        if _has_table(conn, "tg_multi_jobs"):
            jobs["active"] = _count(
                conn, "SELECT COUNT(*) FROM tg_multi_jobs WHERE state IN "
                      "('queued','running','waiting','stop_requested')")
            for state in ("paused", "completed", "failed"):
                jobs[state] = _count(
                    conn, "SELECT COUNT(*) FROM tg_multi_jobs WHERE state=?", (state,))
            accounts["busy"] = _count(
                conn, "SELECT COUNT(DISTINCT current_account) FROM tg_multi_jobs "
                      "WHERE state IN ('running','waiting','stop_requested') "
                      "AND current_account IS NOT NULL")

    try:
        portal_status = sys.modules.get("portal.status")
        if portal_status is None:
            raise RuntimeError("portal status module is not loaded")
        portal_runtime = portal_status.snapshot()
    except Exception as exc:  # isolated modules must never break the card
        portal_runtime = {
            "status": "off", "mode": "quick", "url": "", "server": False,
            "tunnel": False, "detail": f"status unavailable: {type(exc).__name__}",
            "updated_at": now,
        }
    try:
        portal_stats = sys.modules.get("portal.stats")
        if portal_stats is None:
            raise RuntimeError("portal stats module is not loaded")
        portal_numbers = portal_stats.summary()
    except Exception:
        portal_numbers = _empty_portal_stats()

    return {
        "online": True,
        "generated_at": now,
        "stale": False,
        "accounts": accounts,
        "telegram_accounts": telegram,
        "workers": workers,
        "jobs": jobs,
        "portal": {"runtime": portal_runtime, **portal_numbers},
    }


def _minimal(error: str = "") -> dict:
    value = {
        "online": True, "generated_at": time.time(), "stale": True,
        "accounts": {"total": 0, "active": 0, "inactive": 0, "busy": 0},
        "telegram_accounts": {"total": 0, "active": 0, "inactive": 0},
        "workers": {"total": 0, "enabled": 0, "healthy": 0, "unhealthy": 0,
                    "unknown": 0, "items": []},
        "jobs": {"active": 0, "paused": 0, "completed": 0, "failed": 0},
        "portal": {"runtime": {"status": "off", "mode": "quick", "url": ""},
                   **_empty_portal_stats()},
    }
    if error:
        value["error"] = error
    return value


async def _refresh_worker_health(timeout: float) -> None:
    """Optional bounded refresh; normal card rendering uses DB-cached health."""
    if timeout <= 0:
        return
    try:
        import worker
        workers = await asyncio.to_thread(db.list_enabled_workers)
        if workers:
            await asyncio.wait_for(worker.check_all(workers), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        return


async def get_summary(*, force: bool = False, timeout: float = _DEFAULT_TIMEOUT,
                      cache_ttl: float = _CACHE_TTL,
                      refresh_workers: bool = False) -> dict:
    """Return a cached snapshot without blocking the event loop.

    ``refresh_workers=False`` is intentional for latency-sensitive portal and
    ``/start`` calls: persisted health is used immediately. Callers that need a
    live probe may opt in; the same hard timeout still applies.
    """
    timeout = max(0.05, float(timeout))
    cache_ttl = max(0.0, float(cache_ttl))
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE["value"]
        if not force and cached is not None and now_mono < _CACHE["expires"]:
            return copy.deepcopy(cached)

    started = time.monotonic()
    try:
        if refresh_workers:
            await _refresh_worker_health(timeout * 0.65)
        remaining = max(0.05, timeout - (time.monotonic() - started))
        value = await asyncio.wait_for(asyncio.to_thread(_collect_sync), timeout=remaining)
    except asyncio.TimeoutError:
        with _CACHE_LOCK:
            stale = copy.deepcopy(_CACHE["value"])
        if stale is not None:
            stale["stale"] = True
            stale["error"] = "summary timeout; serving cached snapshot"
            return stale
        return _minimal("summary timeout")
    except Exception as exc:
        with _CACHE_LOCK:
            stale = copy.deepcopy(_CACHE["value"])
        if stale is not None:
            stale["stale"] = True
            stale["error"] = f"summary error: {type(exc).__name__}"
            return stale
        return _minimal(f"summary error: {type(exc).__name__}")

    with _CACHE_LOCK:
        _CACHE["value"] = copy.deepcopy(value)
        _CACHE["created"] = time.monotonic()
        _CACHE["expires"] = time.monotonic() + cache_ttl
    return value


def format_card(summary: dict | None = None) -> str:
    """Format a compact Persian status card from ``get_summary`` output."""
    if summary is None:
        with _CACHE_LOCK:
            summary = copy.deepcopy(_CACHE["value"])
        if summary is None:
            summary = _minimal("cache is empty")

    accounts = summary.get("accounts", {})
    tg_accounts = summary.get("telegram_accounts", {})
    workers = summary.get("workers", {})
    jobs = summary.get("jobs", {})
    portal = summary.get("portal", {})
    runtime = portal.get("runtime", {})
    today = portal.get("today", {})
    total = portal.get("total", {})

    portal_state = runtime.get("status", "off")
    portal_label = {
        "running": "فعال", "starting": "در حال شروع", "failed": "خطا", "off": "خاموش",
    }.get(portal_state, str(portal_state))
    stale = " | دادهٔ کش‌شده" if summary.get("stale") else ""
    rate = today.get("rate", 0)
    if isinstance(rate, float) and rate.is_integer():
        rate = int(rate)

    return "\n".join([
        "🤖 وضعیت ربات",
        "━━━━━━━━━━━━",
        f"🟢 ربات آنلاین{stale}",
        f"👤 اکانت‌ها: {accounts.get('total', 0)} | فعال: {accounts.get('active', 0)} "
        f"| مشغول: {accounts.get('busy', 0)}",
        f"✈️ تلگرام: {tg_accounts.get('active', 0)}/{tg_accounts.get('total', 0)} فعال",
        f"🌐 پورتال: {portal_label} | امروز: {today.get('success', 0)}/"
        f"{today.get('started', 0)} ({rate}٪)",
        f"📦 کل ورودی پورتال: {total.get('success', 0)} | منتظر کد: {today.get('pending', 0)}",
        f"🖥 Workerها: {workers.get('healthy', 0)}/{workers.get('enabled', 0)} سالم "
        f"| Job فعال: {jobs.get('active', 0)}",
    ])


def invalidate() -> None:
    """Drop the cached snapshot; the next call performs a bounded refresh."""
    with _CACHE_LOCK:
        _CACHE["value"] = None
        _CACHE["expires"] = 0.0
        _CACHE["created"] = 0.0
