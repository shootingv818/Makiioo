"""
🔄 Automatic server rotation — a THIN driver over the project's OWN transfer.
=============================================================================

Nothing here is new logic. The code-free worker transfer already exists and is
proven; it was simply gated behind the manual "🔁 Login to New Worker &
Continue" button (`rlogin_<id>` -> resume_relogin_cb). This module performs the
EXACT same chain automatically, with no confirmation:

    worker_transfer.get_tried / add_tried / set_tried   (never retry a server)
    worker_transfer.pick_worker_for_transfer(exclude_ids=tried)
    db.get_session_blob(account_id)
    bot._session_transfer_to_worker(neww, sess, phone)   (write-only import)
    db.set_account_worker(account_id, neww["id"])

The caller (the send engine) decides WHEN to rotate — that rule lives in
send_phase.py (5 consecutive two-way errors). This module only answers "move
this account to the next server now", and the send continues from its own
cursor, so nobody is messaged twice.

Pinned files are untouched: worker.py / worker_transfer.py / db.py / bot.py's
transfer helper are only CALLED.
"""
from __future__ import annotations

import db
import worker
import worker_transfer
import rubika_client as rb

import send_phase as sp


def tried_for(account_id: int) -> list:
    return worker_transfer.get_tried(account_id)


def prime_tried(account_id: int, acc: dict, payload_tried=None) -> list:
    """Seed the tried list exactly like resume_relogin_cb does: whatever the
    paused payload remembered, PLUS the account's current server (it just
    failed), so the next pick always lands on a DIFFERENT server."""
    tried = list(payload_tried or [])
    worker_transfer.set_tried(account_id, tried)
    cur_w = None
    try:
        cur_w = worker.worker_for_account(acc) if acc else None
    except Exception:  # noqa: BLE001
        cur_w = None
    cur_wid = cur_w["id"] if cur_w else None
    if cur_wid and cur_wid not in tried:
        worker_transfer.add_tried(account_id, cur_wid)
    return worker_transfer.get_tried(account_id)


def clear_tried(account_id: int) -> None:
    worker_transfer.clear_tried(account_id)


async def auto_transfer(bot, account_id: int, phone: str) -> dict:
    """Move this account to the next untried healthy server WITHOUT a code.

    Returns:
      {"ok": True,  "worker": <worker dict>, "tag": str, "ip": str}
      {"ok": False, "error": "<why>", "exhausted": bool}

    `exhausted` True means there is no other server left to try — the caller
    should stop rotating (it is not a transient failure).
    """
    acc = None
    try:
        acc = db.get_account(account_id)
    except Exception:  # noqa: BLE001
        acc = None

    tried = worker_transfer.get_tried(account_id)
    try:
        neww = await worker_transfer.pick_worker_for_transfer(exclude_ids=tried)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"worker pick failed: {repr(e)[:120]}",
                "exhausted": False}
    if not neww:
        return {"ok": False, "exhausted": True,
                "error": "no other worker to transfer to (all tried)"}

    # mark BEFORE transferring, same order as the manual flow
    worker_transfer.add_tried(account_id, neww["id"])

    sess = None
    try:
        sess = db.get_session_blob(account_id)
    except Exception:  # noqa: BLE001
        sess = None
    if not (sess and sess.get("auth")):
        return {"ok": False, "exhausted": False,
                "error": "no stored session — a code-free transfer is impossible"}

    sess["phone"] = rb.normalize_phone(phone)
    try:
        moved = await bot._session_transfer_to_worker(neww, sess, phone)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "exhausted": False,
                "error": f"session import failed: {repr(e)[:120]}"}
    if not moved:
        return {"ok": False, "exhausted": False,
                "error": f"session login on '{neww.get('tag')}' failed"}

    try:
        db.set_account_worker(account_id, neww["id"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "exhausted": False,
                "error": f"could not record the new worker: {repr(e)[:120]}"}

    return {"ok": True, "worker": neww, "tag": neww.get("tag") or "",
            "ip": neww.get("ip") or ""}


def max_switches() -> int:
    """Panel-tunable budget (default 3), stored in the generic app_settings —
    no schema change, same idiom as db.get_setting elsewhere."""
    try:
        return max(0, int(db.get_int_setting("send_max_switches",
                                             sp.DEFAULT_MAX_SWITCHES)))
    except Exception:  # noqa: BLE001
        return sp.DEFAULT_MAX_SWITCHES


def set_max_switches(n: int) -> None:
    try:
        db.set_setting("send_max_switches", max(0, int(n)))
    except Exception:  # noqa: BLE001
        pass


def current_server(acc: dict) -> tuple:
    """(tag, ip) of the account's server — 'local' for the master."""
    try:
        w = worker.worker_for_account(acc) if acc else None
    except Exception:  # noqa: BLE001
        w = None
    if not w:
        return "local", ""
    if worker.is_local(w):
        return w.get("tag") or "local", w.get("ip") or ""
    return w.get("tag") or "", w.get("ip") or ""
