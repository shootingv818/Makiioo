"""
🔁 Automation Engine — master ORCHESTRATOR (the brain).
========================================================

One multi-account engine that replaces the scattered old pieces, bringing them
under one roof (the owner's "زیر یک چتر"):

  * SCANNERS extract group links from linkdooni sources, check each link
    WITHOUT joining, and bank the healthy ones.
  * SENDERS get healthy groups the moment they are found ("tنوری داغ"), join
    them (10-20s gap, skip approval-required), then send a fixed or rotating
    ad text on a per-account interval.
  * group management: a muted/banned group is dropped and replaced from the
    reserve; a group that bans several accounts is excluded for everyone;
    dead/muted groups are left.
  * account management: a dead/quarantined account's work is handed off; a
    worker that is down does NOT mean the account is dead.
  * PV auto-reply once per person (durable ledger on the master).
  * a pinned live card edited every 30s; errors as separate coded messages.

Testability: every network action goes through an injected `executor` (the
local/remote dispatcher). The selftest injects a fake executor + fake bot +
scratch DB, so the whole flow runs offline. bot.py gets only thin UI hooks.

Nothing pinned is modified: this module only CALLS account_conn / rubika_client
(indirectly, via autoeng_worker) and db/worker (via the injected bot).
"""
from __future__ import annotations

import asyncio
import random
import time

import autoeng_core as core
import autoeng_db as adb
import autoeng_log as live

# Tunables kept local (config.py is pinned; these are the engine's own knobs
# and are also overridable from the Settings menu via adb.set(...)).
JOIN_GAP_MIN = 10
JOIN_GAP_MAX = 20
SCAN_GAP_MIN = 3
SCAN_GAP_MAX = 7
SEND_INTERVAL_DEFAULT = 60       # per-account send interval (seconds)
SEND_GAP_MIN = 1.0               # gap between two groups in one send pass
SEND_GAP_MAX = 2.5
PV_POLL_INTERVAL = 45
PV_REPLY_GAP_MIN = 2.0           # human-like delay before answering a PV
PV_REPLY_GAP_MAX = 5.0
LIVE_REFRESH = 30
RESERVE_TARGET = 30
SCAN_BATCH_MULTIPLIER = 3        # extract up to want*mult raw links to net `want` new
ACC_LIMIT_COOLDOWN = 180         # pause an account this long after a rate-limit
GROUP_FAULT_STRIKES = 3          # consecutive send failures -> drop the group


# --------------------------------------------------------------------------- #
# Engine control + snapshot (module-level singleton, one engine per panel)
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self):
        self.running = False
        self.stop = False
        self.started_at = 0.0
        self.task = None
        self.tasks = []           # per-account loops
        self.roles = {}           # account_id -> role
        self.accounts = {}        # account_id -> account dict
        self.scan = {"extracted": 0, "checked": 0, "healthy": 0, "dead": 0,
                     "approval": 0, "error": 0}
        self.acc_state = {}       # account_id -> {icon, note, ...} for live card
        self.dead_scanners = set()
        self.quarantined = set()
        self.lost_groups = 0

    def reset_counters(self):
        self.scan = {"extracted": 0, "checked": 0, "healthy": 0, "dead": 0,
                     "approval": 0, "error": 0}
        self.acc_state = {}
        self.dead_scanners = set()
        self.quarantined = set()
        self.lost_groups = 0


engine = Engine()


# --------------------------------------------------------------------------- #
# Snapshot -> live-card state
# --------------------------------------------------------------------------- #
def _tag_for(aid) -> str:
    acc = engine.accounts.get(aid, {})
    return acc.get("_tag") or f"A{aid}"


def _worker_label(aid) -> str:
    return engine.acc_state.get(aid, {}).get("worker", "local")


def build_live_state() -> dict:
    scanners, senders = core.split_roles(engine.roles)
    saved = adb.count_unused_healthy()
    totals = {"sent": 0, "fail": 0, "pv_in": 0, "pv_out": 0}
    for aid in engine.roles:
        st = adb.get_stats(aid)
        pv = adb.get_pv(aid)
        totals["sent"] += st["sent"]
        totals["fail"] += st["fail"]
        totals["pv_in"] += pv["received"]
        totals["pv_out"] += pv["replied"]

    def _acc_row(aid, is_scanner):
        s = engine.acc_state.get(aid, {})
        row = {"tag": _tag_for(aid), "worker": s.get("worker", "local"),
               "icon": s.get("icon"), "note": s.get("note")}
        if is_scanner:
            row.update(checked=s.get("checked", 0), assigned=s.get("assigned", 0),
                       ok=s.get("ok", 0), resting=s.get("resting", False))
        else:
            st = adb.get_stats(aid)
            pv = adb.get_pv(aid)
            joined = len(adb.groups_of(aid, adb.G_JOINED))
            target = adb.get_int("groups_per", 3)
            row.update(groups=joined, groups_target=target, sent=st["sent"],
                       fail=st["fail"], pv_in=pv["received"], pv_out=pv["replied"],
                       pv_queue=s.get("pv_queue", 0))
        return row

    return {
        "running": engine.running,
        "elapsed": int(time.time() - engine.started_at) if engine.started_at else 0,
        "clock": time.strftime("%H:%M:%S"),
        "scan": dict(engine.scan),
        "saved": saved, "saved_max": RESERVE_TARGET,
        "totals": totals,
        "scanners": [_acc_row(a, True) for a in scanners],
        "senders": [_acc_row(a, False) for a in senders],
    }


# --------------------------------------------------------------------------- #
# Default executor: dispatches an action to the account's home (local or remote
# worker). Injected so the selftest can replace it. `bot` provides worker + the
# autoeng_worker local calls.
# --------------------------------------------------------------------------- #
class Executor:
    """Routes one action to where the account's session lives."""

    def __init__(self, bot):
        self.bot = bot
        self._w = getattr(bot, "worker", None)
        import autoeng_worker as _aw
        self._local = _aw

    def _home(self, acc):
        try:
            return self._w.worker_for_account(acc)
        except Exception:  # noqa: BLE001
            return None

    def _is_remote(self, w):
        return bool(w) and not self._w.is_local(w)

    async def _remote(self, acc, path, payload):
        w = self._home(acc)
        if not self._is_remote(w):
            return None, False
        try:
            res = await self._w.api_call(w, "POST", path, payload, timeout=180)
            return res, True
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": core.WRK_DOWN, "error": repr(e)[:160]}, True

    async def preview(self, acc, link):
        res, remote = await self._remote(acc, "/autoeng/preview",
                                         {"phone": acc["phone"], "link": link})
        if remote:
            return res or {"ok": False, "code": core.WRK_DOWN, "error": "no worker"}
        return await self._local.preview_link(acc["phone"], link)

    async def join(self, acc, link):
        res, remote = await self._remote(acc, "/autoeng/join",
                                         {"phone": acc["phone"], "link": link})
        if remote:
            return res or {"ok": False, "code": core.WRK_DOWN, "error": "no worker"}
        return await self._local.join_link(acc["phone"], link)

    async def send(self, acc, guid, text):
        res, remote = await self._remote(acc, "/autoeng/send",
                                         {"phone": acc["phone"], "guid": guid,
                                          "text": text})
        if remote:
            return res or {"ok": False, "code": core.WRK_DOWN, "error": "no worker"}
        return await self._local.send_to(acc["phone"], guid, text)

    async def leave(self, acc, guid):
        res, remote = await self._remote(acc, "/autoeng/leave",
                                         {"phone": acc["phone"], "guid": guid})
        if remote:
            return res or {"ok": False}
        return await self._local.leave(acc["phone"], guid)

    async def send_file(self, acc, guid):
        """Send the ad FILE + caption to a group. The bytes live on the master
        (caption_file store); for a remote account we ship them base64 like the
        existing /capfile/upload does."""
        import base64
        import caption_file
        data = caption_file.read_file_bytes()
        if not data:
            return {"ok": False, "code": core.SYS_ERR, "error": "no file set"}
        cap = caption_file.get_caption()
        name = caption_file.get_name() or "file"
        b64 = base64.b64encode(data).decode()
        res, remote = await self._remote(acc, "/autoeng/sendfile",
                                         {"phone": acc["phone"], "guid": guid,
                                          "file_b64": b64, "caption": cap,
                                          "filename": name})
        if remote:
            return res or {"ok": False, "code": core.WRK_DOWN, "error": "no worker"}
        return await self._local.send_file_to(acc["phone"], guid, b64, cap, name)

    async def pv(self, acc, state):
        res, remote = await self._remote(acc, "/autoeng/pv",
                                         {"phone": acc["phone"], "state": state})
        if remote:
            return res or {"ok": False, "new": [], "state": state}
        return await self._local.fetch_pv(acc["phone"], state)

    async def extract(self, acc, channels, want):
        """Link extraction via the reader account. Reuses the panel's PROVEN
        _ld_extract_links, which already routes local vs the worker's
        /linkdooni/extract and harvests invite links. `channels` is a list of
        source refs (strings); that helper expects dicts with a 'ref' key."""
        fn = getattr(self.bot, "_ld_extract_links", None)
        if fn is None:
            return []
        chans = [{"ref": c} for c in channels]
        try:
            return await fn(acc, chans, want) or []
        except Exception:  # noqa: BLE001
            return []


# --------------------------------------------------------------------------- #
# Logging helpers (thin; go through the injected bot)
# --------------------------------------------------------------------------- #
async def _safe_log(bot, text):
    try:
        await bot.log(text)
    except Exception:  # noqa: BLE001
        pass


async def _err_card(bot, *, code, stage, acc, reason, action):
    await _safe_log(bot, live.error_card(
        code=code, stage=stage, account=str(acc.get("phone", "?")),
        worker=_worker_label(acc.get("id")), reason=reason, action=action,
        clock=time.strftime("%H:%M:%S")))


def _set_acc(aid, **kw):
    engine.acc_state.setdefault(aid, {}).update(kw)


# --------------------------------------------------------------------------- #
# Group hand-off: give a freshly-healthy group to the best sender.
# --------------------------------------------------------------------------- #
def _hand_off_healthy(guid, link, name, members):
    _scanners, senders = core.split_roles(engine.roles)
    senders = [s for s in senders if s not in engine.quarantined]
    groups_per = adb.get_int("groups_per", 3)
    counts = adb.group_counts_by_owner(senders)
    sid = core.assign_group(counts, senders, groups_per)
    adb.upsert_group(guid, link, name, adb.G_HEALTHY, members=members or 0)
    if sid is None:
        return None            # reserve; senders are full
    adb.set_group_state(guid, adb.G_ASSIGNED, owner_id=sid)
    return sid


# --------------------------------------------------------------------------- #
# SCANNER loop — check assigned links one-by-one, bank healthy, rest on reserve.
# --------------------------------------------------------------------------- #
async def _scanner_loop(bot, ex, aid):
    acc = engine.accounts[aid]
    _set_acc(aid, icon=live.IC_SCAN, worker=_worker_label(aid))
    while not engine.stop:
        if core.reserve_is_full(adb.count_unused_healthy(), RESERVE_TARGET):
            _set_acc(aid, resting=True)
            await _interruptible_sleep(5)
            continue
        _set_acc(aid, resting=False)
        link = adb.claim_link(aid)
        if link is None:
            await _interruptible_sleep(3)
            continue
        info = await ex.preview(acc, link)
        engine.scan["checked"] += 1
        _set_acc(aid, checked=engine.acc_state.get(aid, {}).get("checked", 0) + 1)
        if info.get("ok"):
            if info.get("approval"):
                engine.scan["approval"] += 1
                adb.set_link_result(link, adb.L_DONE, core.GRP_APPROVAL)
            else:
                engine.scan["healthy"] += 1
                _set_acc(aid, ok=engine.acc_state.get(aid, {}).get("ok", 0) + 1)
                adb.set_link_result(link, adb.L_DONE, "healthy")
                _hand_off_healthy(info["guid"], link, info.get("name", "-"),
                                  info.get("members"))
        else:
            code = info.get("code", core.SYS_ERR)
            if core.is_fatal_account(code):
                await _quarantine(bot, acc, "scan", info.get("error", ""))
                engine.dead_scanners.add(aid)
                adb.requeue_link(link)        # hand its link back to the pool
                return
            if core.should_retry_link(code):
                adb.requeue_link(link)        # not the link's fault -> retry later
                if code == core.ACC_LIMIT:
                    _set_acc(aid, icon=live.IC_LIMIT, note="rate-limited")
                    await _interruptible_sleep(ACC_LIMIT_COOLDOWN)
                    _set_acc(aid, icon=live.IC_SCAN, note=None)
            else:
                engine.scan["dead"] += 1
                adb.set_link_result(link, adb.L_DONE, code)
        await _interruptible_sleep(random.uniform(SCAN_GAP_MIN, SCAN_GAP_MAX))


# --------------------------------------------------------------------------- #
# SENDER loop — join assigned groups (gap, skip approval), then send on interval
# with rotating text, dropping+replacing muted/banned groups.
# --------------------------------------------------------------------------- #
async def _sender_loop(bot, ex, aid):
    acc = engine.accounts[aid]
    _set_acc(aid, icon=live.IC_WAIT, worker=_worker_label(aid))
    last_idx = {}
    strikes = {}
    while not engine.stop:
        # 1) join any assigned-but-not-joined groups, with a human gap.
        for g in adb.groups_of(aid, adb.G_ASSIGNED):
            if engine.stop:
                break
            _set_acc(aid, icon=live.IC_JOIN)
            res = await ex.join(acc, g["link"])
            if res.get("ok"):
                adb.mark_group_joined(res["guid"] or g["guid"], aid)
            else:
                code = res.get("code", core.SYS_ERR)
                if core.is_fatal_account(code):
                    await _quarantine(bot, acc, "join", res.get("error", ""))
                    return
                if code == core.ACC_LIMIT:
                    # rate-limited: keep the group ASSIGNED, cool down, retry.
                    _set_acc(aid, icon=live.IC_LIMIT, note="rate-limited")
                    await _err_card(bot, code=code, stage="join", acc=acc,
                                    reason=res.get("error", ""),
                                    action="cooling down (group kept)")
                    await _interruptible_sleep(ACC_LIMIT_COOLDOWN)
                    _set_acc(aid, icon=live.IC_JOIN, note=None)
                    break        # restart the join phase next outer pass
                if core.should_retry_link(code):
                    # transient (NET/worker): leave ASSIGNED, retry next pass.
                    await _interruptible_sleep(random.uniform(JOIN_GAP_MIN,
                                                              JOIN_GAP_MAX))
                    continue
                # a real group fault (approval-required / bad link): drop it and
                # pull a replacement from the reserve.
                engine.lost_groups += 1
                adb.set_group_state(g["guid"], adb.G_DEAD)
                await _err_card(bot, code=code, stage="join", acc=acc,
                                reason=res.get("error", ""), action="dropped group")
                _replace_group(aid)
            await _interruptible_sleep(random.uniform(JOIN_GAP_MIN, JOIN_GAP_MAX))

        # 2) one send pass over joined groups. The ad content is either a
        # FILE + caption (file mode) or one/several rotating plain texts.
        texts = adb.list_texts()
        joined = adb.groups_of(aid, adb.G_JOINED)
        file_mode = adb.get_bool("text_is_file", False)
        if (texts or file_mode) and joined:
            _set_acc(aid, icon=live.IC_SEND)
            for g in joined:
                if engine.stop:
                    break
                if file_mode:
                    res = await ex.send_file(acc, g["guid"])
                else:
                    txt = _pick_text(texts, last_idx.get(g["guid"]))
                    last_idx[g["guid"]] = texts.index(txt)
                    res = await ex.send(acc, g["guid"], txt)
                if res.get("ok"):
                    adb.incr_stats(aid, sent=1)
                    strikes[g["guid"]] = 0
                else:
                    adb.incr_stats(aid, fail=1)
                    code = res.get("code", core.SYS_ERR)
                    await _handle_send_fault(bot, ex, acc, aid, g, code,
                                             res.get("error", ""), strikes)
                    if core.is_fatal_account(code):
                        return
                await _interruptible_sleep(random.uniform(SEND_GAP_MIN, SEND_GAP_MAX))
        elif not joined:
            _set_acc(aid, icon=live.IC_WAIT)

        interval = adb.get_int("send_interval", SEND_INTERVAL_DEFAULT)
        _set_acc(aid, icon=live.IC_WAIT)
        await _interruptible_sleep(interval)


async def _handle_send_fault(bot, ex, acc, aid, g, code, reason, strikes):
    if code == core.ACC_LIMIT:
        _set_acc(aid, icon=live.IC_LIMIT, note="rate-limited")
        await _err_card(bot, code=code, stage="send", acc=acc, reason=reason,
                        action="cooling down (group kept)")
        await _interruptible_sleep(ACC_LIMIT_COOLDOWN)
        _set_acc(aid, note=None)
        return
    if core.is_fatal_account(code):
        await _quarantine(bot, acc, "send", reason)
        return
    if core.is_group_fault(code):
        strikes[g["guid"]] = strikes.get(g["guid"], 0) + 1
        if strikes[g["guid"]] >= GROUP_FAULT_STRIKES or code in (core.GRP_GONE,):
            # cross-account exclusion: if several accounts were banned here,
            # block the group for everyone.
            n = adb.add_ban_hit(g["guid"], aid)
            engine.lost_groups += 1
            if n >= 2:
                adb.set_group_state(g["guid"], adb.G_EXCLUDED)
                action = "excluded for all accounts"
            else:
                adb.set_group_state(g["guid"], adb.G_MUTED)
                action = "dropped + replaced"
            await ex.leave(acc, g["guid"])      # free the membership slot
            await _err_card(bot, code=code, stage="send", acc=acc,
                            reason=reason, action=action)
            _replace_group(aid)


def _replace_group(aid):
    """Pull one reserve group for `aid` to keep it at target."""
    g = adb.take_reserve_group()
    if g is None:
        return False
    adb.set_group_state(g["guid"], adb.G_ASSIGNED, owner_id=aid)
    return True


def _pick_text(texts, last_idx):
    """Rotate (avoid repeating the immediately previous text for this group)."""
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]
    idx = random.randrange(len(texts))
    if last_idx is not None and idx == last_idx:
        idx = (idx + 1) % len(texts)
    return texts[idx]


# --------------------------------------------------------------------------- #
# PV loop — once-per-person auto-reply (durable ledger on master).
# --------------------------------------------------------------------------- #
async def _pv_loop(bot, ex, aid):
    if not adb.get_bool("pv_enabled", False):
        return
    acc = engine.accounts[aid]
    state = None
    first = True
    while not engine.stop:
        res = await ex.pv(acc, state)
        state = res.get("state", state)
        if res.get("ok") and not first:
            # Count only DISTINCT, not-yet-answered senders as "received", so a
            # worker that returns a non-advancing cursor can't inflate the
            # metric by re-reporting the same person every poll.
            fresh, _seen = [], set()
            for u in res.get("new", []):
                g = u.get("guid")
                if not g or g in _seen or adb.already_replied(aid, g):
                    continue
                _seen.add(g)
                fresh.append(u)
            if fresh:
                adb.incr_pv(aid, received=len(fresh))
                _set_acc(aid, pv_queue=len(fresh))
            reply = adb.get("pv_text", "") or ""
            for u in fresh:
                if engine.stop:
                    break
                # re-check inside the loop: a poll can list the same guid twice,
                # and we mark-replied only after the first send.
                if adb.already_replied(aid, u["guid"]):
                    continue
                if reply:
                    r = await ex.send(acc, u["guid"], reply)
                    if r.get("ok"):
                        adb.mark_replied(aid, u["guid"])
                        adb.incr_pv(aid, replied=1)
                    await _interruptible_sleep(random.uniform(PV_REPLY_GAP_MIN,
                                                              PV_REPLY_GAP_MAX))
                _set_acc(aid, pv_queue=max(0, engine.acc_state.get(aid, {})
                                           .get("pv_queue", 1) - 1))
        elif not res.get("ok") and core.is_fatal_account(res.get("code", "")):
            return
        first = False
        await _interruptible_sleep(PV_POLL_INTERVAL)


# --------------------------------------------------------------------------- #
# Quarantine (reuses the panel's own stop/quarantine; we only flag + free work)
# --------------------------------------------------------------------------- #
async def _quarantine(bot, acc, stage, reason):
    aid = int(acc["id"])
    if aid in engine.quarantined:
        return
    engine.quarantined.add(aid)
    _set_acc(aid, icon=live.IC_DEAD, note="QUARANTINED · " + core.ACC_DEAD)
    # free this account's groups back to the reserve so others can take them.
    for g in adb.groups_of(aid, adb.G_JOINED) + adb.groups_of(aid, adb.G_ASSIGNED):
        adb.set_group_state(g["guid"], adb.G_HEALTHY, owner_id=None)
    await _err_card(bot, code=core.ACC_DEAD, stage=stage, acc=acc,
                    reason=reason, action="account quarantined; work reassigned")
    # ask the panel to do its real quarantine (pinned logic), best-effort.
    try:
        fn = getattr(bot, "_quarantine_dead_account", None)
        if fn is not None:
            await fn(acc)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Interruptible sleep (so stop takes effect quickly)
# --------------------------------------------------------------------------- #
async def _interruptible_sleep(seconds):
    # ALWAYS yield at least once, even for a zero/negative duration, so a loop
    # that calls this every pass can never starve the event loop.
    await asyncio.sleep(0)
    end = time.time() + float(seconds)
    while not engine.stop and time.time() < end:
        await asyncio.sleep(min(1.0, max(0.01, end - time.time())))


# --------------------------------------------------------------------------- #
# Extraction driver (uses one scanner as the reader each round)
# --------------------------------------------------------------------------- #
async def _extract_round(bot, ex):
    scanners, _ = core.split_roles(engine.roles)
    scanners = [s for s in scanners if s not in engine.dead_scanners]
    if not scanners:
        return
    reader = engine.accounts[scanners[0]]
    channels = [c.strip() for c in (adb.get("channels", "") or "").split(",")
                if c.strip()]
    if not channels:
        return
    want = adb.get_int("scan_count", 50)
    raw = await ex.extract(reader, channels, want * SCAN_BATCH_MULTIPLIER)
    new = 0
    for lk in raw:
        if new >= want:
            break
        if adb.link_seen(lk):     # only NEW links enter the queue
            new += 1
    engine.scan["extracted"] += new


# --------------------------------------------------------------------------- #
# Live-card loop (edits the pinned message(s) every 30s via the injected bot)
# --------------------------------------------------------------------------- #
async def _live_loop(bot):
    while not engine.stop:
        try:
            await bot.autoeng_render_live(build_live_state())
        except Exception:  # noqa: BLE001
            pass
        await _interruptible_sleep(LIVE_REFRESH)


# --------------------------------------------------------------------------- #
# Public start / stop / reset
# --------------------------------------------------------------------------- #
async def start(bot, account_ids, *, executor=None):
    """Launch the engine over the selected accounts. Returns (ok, message)."""
    adb.init()
    ids = [int(a) for a in account_ids]
    # SESSION SAFETY (golden rule): never let the engine open a 2nd connection
    # on an account that an OLD feature (automation/secretary/reply/linkdooni)
    # or a one-shot job already owns — that is exactly AUTH_FROM_ANOTHER. Drop
    # such accounts before assigning roles; refuse if too few remain.
    busy = []
    for aid in list(ids):
        try:
            if getattr(bot, "continuous_busy", None) and bot.continuous_busy(aid):
                busy.append(aid)
            elif aid in getattr(bot, "active_jobs", set()):
                busy.append(aid)
        except Exception:  # noqa: BLE001
            pass
    if busy:
        ids = [a for a in ids if a not in busy]
        try:
            await bot.log(bot.card("⚠️ Automation — skipped busy accounts", [
                "These run an older feature / job (session is in use):",
                ", ".join(str(b) for b in busy),
                "Turn those off first to include them.",
                bot.LINE, f"🕒 {bot.now()}"]))
        except Exception:  # noqa: BLE001
            pass
    roles = core.assign_roles(ids)
    if not roles:
        return False, (f"need at least {core.MIN_ACCOUNTS} free accounts "
                       f"({len(busy)} skipped as busy)" if busy
                       else f"need at least {core.MIN_ACCOUNTS} accounts")
    if not adb.list_texts():
        return False, "no ad text set"
    # resolve account dicts through the panel (db.get_account) once.
    engine.accounts = {}
    for i, aid in enumerate(ids, 1):
        acc = bot.db.get_account(aid) if hasattr(bot, "db") else {"id": aid,
                                                                   "phone": str(aid)}
        if not acc:
            continue
        acc = dict(acc)
        acc["_tag"] = f"A{i}"
        engine.accounts[aid] = acc
    engine.roles = {a: roles[a] for a in roles if a in engine.accounts}
    adb.save_roles(engine.roles)
    engine.reset_counters()
    engine.stop = False
    engine.running = True
    engine.started_at = time.time()
    adb.set("running", "1")
    adb.set("started_at", str(int(engine.started_at)))

    ex = executor or Executor(bot)
    scanners, senders = core.split_roles(engine.roles)
    engine.tasks = []
    engine.tasks.append(asyncio.create_task(_scan_driver(bot, ex)))
    for aid in scanners:
        engine.tasks.append(asyncio.create_task(_scanner_loop(bot, ex, aid)))
    for aid in senders:
        engine.tasks.append(asyncio.create_task(_sender_loop(bot, ex, aid)))
        engine.tasks.append(asyncio.create_task(_pv_loop(bot, ex, aid)))
    engine.task = asyncio.create_task(_live_loop(bot))
    return True, f"started · {len(scanners)} scan · {len(senders)} send"


async def _scan_driver(bot, ex):
    """Keep the link queue fed while the engine runs (and reserve isn't full)."""
    while not engine.stop:
        try:
            if not core.reserve_is_full(adb.count_unused_healthy(), RESERVE_TARGET):
                if len(adb.pending_links()) < 10:
                    await _extract_round(bot, ex)
        except Exception as e:  # noqa: BLE001
            await _err_card(bot, code=core.SYS_ERR, stage="scan",
                            acc={"phone": "-", "id": 0},
                            reason=repr(e)[:120], action="continuing")
        await _interruptible_sleep(20)


async def stop(bot):
    """Stop the engine, post the summary card, release the live loop."""
    if not engine.running:
        return False, "not running"
    engine.stop = True
    engine.running = False
    adb.set("running", "0")
    allt = list(engine.tasks) + ([engine.task] if engine.task else [])
    for t in allt:
        try:
            t.cancel()
        except Exception:  # noqa: BLE001
            pass
    # actually WAIT for the loops to finish cancelling, so no zombie task keeps
    # running against the DB after stop returns.
    if allt:
        try:
            await asyncio.gather(*allt, return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass
    engine.tasks = []
    engine.task = None
    state = build_live_state()
    try:
        await bot.log(live.summary_card(
            elapsed=state["elapsed"], totals=state["totals"], scan=state["scan"],
            quarantined=len(engine.quarantined), lost_groups=engine.lost_groups,
            clock=time.strftime("%H:%M:%S")))
    except Exception:  # noqa: BLE001
        pass
    return True, "stopped"


def reset(keep_groups=True):
    """Clear roles / queue / per-run state. Keeps healthy saved groups unless
    the owner explicitly wipes them."""
    adb.init()
    adb.reset_run(keep_groups=keep_groups)
    engine.roles = {}
    engine.accounts = {}
    engine.reset_counters()
    return True
