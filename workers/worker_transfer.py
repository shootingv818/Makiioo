"""
worker_transfer.py — Isolated worker selection for TRANSFER (not first login).
==============================================================================

When the user presses "انتقال ورکر" repeatedly, this module ensures the bot
picks a worker that has NEVER been tried before for this send — not just the
current one.

Differences from base `worker.pick_worker_for_login`:
  1. `exclude_ids` is a LIST (all previously tried workers), not a single ID.
  2. No load-balancing sort (no "fewest accounts first") — just pick first
     healthy worker by ID order.
  3. Tracks tried workers per account_id (in-memory + persisted in payload).

Does NOT modify: worker.py, rubika_client.py, account_conn.py, db.py, config.py.
Does NOT open any Rubika connection or session.
"""
from __future__ import annotations

import worker
import db


# --------------------------------------------------------------------------- #
# In-memory tracking: account_id -> list of tried worker IDs.
# Also persisted in the paused_send payload for restart safety.
# --------------------------------------------------------------------------- #
_account_tried: dict = {}


def get_tried(account_id: int) -> list:
    """Return the list of tried worker IDs for this account's current send."""
    return list(_account_tried.get(int(account_id), []))


def add_tried(account_id: int, worker_id: int):
    """Mark a worker as tried for this account."""
    aid = int(account_id)
    wid = int(worker_id)
    if aid not in _account_tried:
        _account_tried[aid] = []
    if wid not in _account_tried[aid]:
        _account_tried[aid].append(wid)


def set_tried(account_id: int, ids: list):
    """Restore tried list (e.g. from payload after restart)."""
    _account_tried[int(account_id)] = [int(x) for x in (ids or [])]


def clear_tried(account_id: int):
    """Clear tried history (send cancelled or fully finished)."""
    _account_tried.pop(int(account_id), None)


# --------------------------------------------------------------------------- #
# Worker selection for transfer — excludes ALL previously tried workers.
# --------------------------------------------------------------------------- #
async def pick_worker_for_transfer(exclude_ids: list = None,
                                   verify: bool = True):
    """Find a healthy enabled worker whose ID is NOT in exclude_ids.

    No load-balancing — returns first available by ID order.
    Returns a worker dict or None if no suitable worker exists.
    """
    exclude_set = set(int(x) for x in (exclude_ids or []))

    worker.ensure_master_worker()
    workers = db.list_enabled_workers()
    if not workers:
        return None

    remotes = [w for w in workers if not worker.is_local(w)]
    # Only health-check when there are real remote workers
    if verify and remotes:
        await worker.check_all(workers)
        workers = db.list_enabled_workers()  # reload with fresh health

    # Local master is always usable; remotes must be healthy ("ok")
    pool = [w for w in workers if (worker.is_local(w) or w.get("status") == "ok")]
    # Exclude ALL previously tried workers
    pool = [w for w in pool if w["id"] not in exclude_set]

    if not pool:
        return None

    # No load sort — deterministic by ID
    pool.sort(key=lambda w: w["id"])
    return pool[0]
