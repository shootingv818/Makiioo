"""
🔗 send_integrate — the thin glue that wires the new send behaviour into the
EXISTING send engine, without rewriting it.
=============================================================================

bot.py's run_send / run_send_remote stay in charge of actually sending. This
module only answers the questions they now need to ask, and performs the
automatic transfer using the project's OWN chain:

  phase_split()        -> order recipients [one-way..., two-way...] + boundary
  phase_jump()         -> "5 consecutive errors in phase 1: skip to two-way"
  should_rotate()      -> "is this stop reason an IP problem (not a session)?"
  auto_rotate()        -> the automatic version of resume_relogin_cb
  live                 -> create/edit ONE pinned send card (no 50-message spam)
  note_auth_error()    -> precise auth code + raw body for curiosity mode

Reused, never re-implemented:
  send_order.classify()'s `mutual`      -> who is one-way vs two-way
  send_phase.PhaseState rules           -> the 5-streak / budget decisions
  send_rotate.auto_transfer()           -> worker_transfer + /session/import
  bot._do_resume()                      -> continue the remaining list
  auth_diag / account_conn.is_auth_error-> auth classification
"""
from __future__ import annotations

import asyncio
import time

import send_phase as sp
import send_live as sl
import send_rotate as srot
import auth_diag

# per-account live-card state: account_id -> {"msg_id", "started", "state"}
_live: dict = {}
# per-account rotation bookkeeping: account_id -> {"switches", "last_switch"}
_rot: dict = {}


# --------------------------------------------------------------------------- #
# 1. Phase ordering (called at PREPARE time, where the records still exist)
# --------------------------------------------------------------------------- #
def phase_split(records: list) -> tuple:
    """(ordered_guids, twoway_from) from send_order's records.

    One-way (no chat) first — keeping send_order's own ordering inside each
    group — then two-way. `twoway_from` is the index where two-way begins, so
    the engine can jump straight there.
    """
    one, two = sp.split_recipients(records)
    return (one + two), len(one)


def phase_of(idx: int, twoway_from: int) -> str:
    if twoway_from <= 0:
        return sp.PHASE_TWOWAY
    return sp.PHASE_ONEWAY if idx < twoway_from else sp.PHASE_TWOWAY


def phase_jump(idx: int, twoway_from: int, total: int):
    """5 consecutive errors happened. In phase 1 -> jump to the start of the
    two-way block (same server). Returns the new index, or None when there is
    nothing to jump to (already two-way, or no two-way contacts)."""
    if twoway_from <= 0:
        return None
    if idx >= twoway_from:
        return None
    if twoway_from >= total:
        return None
    return twoway_from


# --------------------------------------------------------------------------- #
# 2. Rotation decision + execution
# --------------------------------------------------------------------------- #
def budget() -> int:
    return srot.max_switches()


def switches_used(account_id: int) -> int:
    return int(_rot.get(int(account_id), {}).get("switches", 0))


def reset_rotation(account_id: int) -> None:
    _rot.pop(int(account_id), None)
    srot.clear_tried(account_id)


def should_rotate(account_id: int, *, dead: bool, reason: str) -> bool:
    """Rotate only when a DIFFERENT IP could plausibly help.

    * a dead/invalid session -> NEVER (the session is the problem, not the IP;
      learned from the 2026-11-20 run where 4 accounts died in a row)
    * a manual stop          -> never
    * budget exhausted       -> no
    """
    if dead:
        return False
    r = (reason or "").lower()
    if not r:
        return False
    if "manual stop" in r or "session invalid" in r or "re-login" in r:
        return False
    if auth_diag.classify_auth(reason):
        return False                      # any auth flavour: rotation is futile
    return switches_used(account_id) < budget()


async def auto_rotate(bot, account_id: int, phone: str, *, reason: str,
                      resume_at: int = 0) -> bool:
    """Move the account to the next untried server and report it. Returns True
    when the transfer succeeded (the caller then resumes the remaining list).

    Pauses SWITCH_PAUSE_SEC between switches, as the owner asked. A failed
    transfer does NOT consume the chance to try the next server, unless there
    is no server left at all.
    """
    aid = int(account_id)
    st = _rot.setdefault(aid, {"switches": 0, "last_switch": 0.0})
    n = st["switches"] + 1
    total = budget()

    # 3-minute gap between switches (skipped before the very first one)
    if st["last_switch"]:
        wait = sp.SWITCH_PAUSE_SEC - (time.time() - st["last_switch"])
        if wait > 0:
            await asyncio.sleep(wait)

    acc = None
    try:
        acc = bot.db.get_account(aid)
    except Exception:  # noqa: BLE001
        acc = None
    from_tag, from_ip = srot.current_server(acc)

    # seed the tried list exactly like the manual flow does
    try:
        rec = bot.db.get_paused_send(aid)
        payload_tried = (rec or {}).get("payload", {}).get("tried_workers") or []
    except Exception:  # noqa: BLE001
        payload_tried = []
    srot.prime_tried(aid, acc, payload_tried)

    res = await srot.auto_transfer(bot, aid, phone)
    if not res.get("ok"):
        await _safe_log(bot, sl.switch_failed_card(
            n=n, total=total, phone=phone, error=res.get("error", "unknown"),
            clock=bot.now()))
        if res.get("exhausted"):
            st["switches"] = total        # nothing left: stop trying
        return False

    st["switches"] = n
    st["last_switch"] = time.time()
    await _safe_log(bot, sl.switch_card(
        n=n, total=total, phone=phone, reason=reason or "consecutive send errors",
        from_tag=from_tag, from_ip=from_ip, to_tag=res.get("tag", ""),
        to_ip=res.get("ip", ""), resume_at=resume_at, clock=bot.now()))
    _rot[aid]["pending_verdict"] = {"n": n, "total": total,
                                    "tag": res.get("tag", ""),
                                    "ip": res.get("ip", "")}
    return True


async def report_switch_result(bot, account_id: int, phone: str, *,
                               ok_after: int, fail_after: int) -> None:
    """After the resumed run has produced its first results, say whether the
    switch actually helped — the per-IP vs per-account datum."""
    aid = int(account_id)
    pend = _rot.get(aid, {}).pop("pending_verdict", None)
    if not pend:
        return
    await _safe_log(bot, sl.switch_result_card(
        n=pend["n"], total=pend["total"], phone=phone, worker_tag=pend["tag"],
        worker_ip=pend["ip"], ok_after=ok_after, fail_after=fail_after,
        clock=bot.now()))


# --------------------------------------------------------------------------- #
# 3. Live send card (replaces the every-50-recipients spam)
# --------------------------------------------------------------------------- #
def live_begin(account_id: int, phone: str, *, total_oneway: int,
               total_twoway: int, worker_tag: str, worker_ip: str,
               tag: str = "") -> None:
    aid = int(account_id)
    state = sp.PhaseState([0] * max(0, int(total_oneway)),
                          [0] * max(0, int(total_twoway)),
                          max_switches=budget())
    _live[aid] = {"msg_id": None, "started": time.time(), "state": state,
                  "phone": phone, "tag": tag, "worker_tag": worker_tag,
                  "worker_ip": worker_ip, "last_edit": 0.0, "dirty": True}


def live_state(account_id: int):
    rec = _live.get(int(account_id))
    return rec["state"] if rec else None


def live_set_server(account_id: int, worker_tag: str, worker_ip: str) -> None:
    rec = _live.get(int(account_id))
    if rec:
        rec["worker_tag"] = worker_tag
        rec["worker_ip"] = worker_ip
        rec["dirty"] = True


def live_mark(account_id: int) -> None:
    rec = _live.get(int(account_id))
    if rec:
        rec["dirty"] = True


async def live_push(bot, account_id: int, *, force: bool = False,
                    stopped: bool = False, min_gap: float = 5.0) -> None:
    """Create or edit the ONE live card. Throttled so a fast send cannot hit
    Telegram's edit rate limit."""
    aid = int(account_id)
    rec = _live.get(aid)
    if not rec:
        return
    if not force and not rec["dirty"]:
        return
    if not force and (time.time() - rec["last_edit"]) < min_gap:
        return
    body = sl.render_live(
        phone=rec["phone"], tag=rec["tag"], worker_tag=rec["worker_tag"],
        worker_ip=rec["worker_ip"], state=rec["state"],
        elapsed=int(time.time() - rec["started"]), clock=bot.now(),
        stopped=stopped)
    rec["last_edit"] = time.time()
    rec["dirty"] = False
    try:
        if rec["msg_id"] is None:
            m = await bot.bot.send_message(bot.config.LOG_GROUP_ID, body)
            rec["msg_id"] = getattr(m, "id", None)
        else:
            await bot.bot.edit_message(bot.config.LOG_GROUP_ID,
                                       rec["msg_id"], body)
    except Exception:  # noqa: BLE001 - 'not modified' and friends are harmless
        pass


async def live_finish(bot, account_id: int) -> None:
    """Final edit + the end-of-run summary (per-phase and per-server)."""
    aid = int(account_id)
    rec = _live.pop(aid, None)
    if not rec:
        return
    _live[aid] = rec                       # keep for the last edit
    await live_push(bot, aid, force=True, stopped=True)
    rec = _live.pop(aid, None)
    if not rec:
        return
    try:
        await bot.log(sl.final_card(
            phone=rec["phone"], state=rec["state"],
            elapsed=int(time.time() - rec["started"]), clock=bot.now()))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 4. Precise auth reporting + curiosity capture
# --------------------------------------------------------------------------- #
async def note_auth_error(bot, exc, *, stage: str, phone: str,
                          worker_tag: str = "", worker_ip: str = "",
                          action: str = "") -> str:
    """Post the precise auth card and silently store the RAW body. Returns the
    auth code ("" when the error is not an auth failure)."""
    code = auth_diag.classify_auth(exc)
    try:
        import curiosity
        curiosity.record_error(exc, phone=phone, worker=worker_tag,
                               method=stage, note=code or "non-auth")
    except Exception:  # noqa: BLE001
        pass
    if not code:
        return ""
    await _safe_log(bot, auth_diag.diag_card(
        code=code, stage=stage, phone=phone, worker_tag=worker_tag,
        worker_ip=worker_ip, raw=str(exc)[:200], action=action,
        clock=bot.now()))
    return code


async def _safe_log(bot, text) -> None:
    try:
        await bot.log(text)
    except Exception:  # noqa: BLE001
        pass
