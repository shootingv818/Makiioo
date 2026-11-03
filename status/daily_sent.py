"""
📤 Daily successful-send counter — ISOLATED, ADDITIVE helper.
============================================================

Purpose (exactly what the owner asked for, nothing more):
  Show, on the `/start` control-panel card, how many messages were
  *successfully* sent TODAY across ALL send paths (normal forward, plain
  text, worker/remote, resume/transfer continuation, brain, pool).

Why a separate module:
  * It touches NOTHING in the send engine's logic. `bot.run_send` and
    `bot.run_send_remote` only gain a single GUARDED `record(ok)` call at the
    point where each run's confirmed-success count is already known. That call
    can never change send behaviour (wrapped in try/except, returns on any
    error).
  * No new DB table and no schema change: the daily tallies live in the
    existing generic `app_settings` key/value store under
    `daily_sent:YYYY-MM-DD`, so old databases keep working untouched and the
    old counts naturally age out (we only ever read today's key).

Counting rule (avoids double counting on resume/worker-transfer):
  callers pass the successes achieved IN THAT RUN (`ok`), not the cumulative
  `base_ok + ok`. A send that stops and is resumed/transferred on another
  worker records only its own new successes in each leg, so the daily total is
  the real number of recipients messaged today.
"""
from __future__ import annotations

from datetime import datetime

import db

_PREFIX = "daily_sent:"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _key(day: str | None = None) -> str:
    return _PREFIX + (day or _today())


def record(n) -> None:
    """Add ``n`` confirmed-successful sends to today's tally. GUARDED: any
    error (bad value, DB locked) is swallowed so this can never disturb a send.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    try:
        key = _key()
        cur = db.get_int_setting(key, 0)
        db.set_setting(key, cur + n)
    except Exception:  # noqa: BLE001 — instrumentation must never raise
        pass


def today_count() -> int:
    """Return today's confirmed-successful send count (0 on any error)."""
    try:
        return db.get_int_setting(_key(), 0)
    except Exception:  # noqa: BLE001
        return 0


def count_for(day: str) -> int:
    """Return the tally for a specific ``YYYY-MM-DD`` (0 on any error)."""
    try:
        return db.get_int_setting(_key(day), 0)
    except Exception:  # noqa: BLE001
        return 0
