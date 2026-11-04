"""
📋 Recipient ordering — ISOLATED, ADDITIVE module.
==================================================

Builds the send list with the priority the owner asked for:

    1  ONLINE NOW                 at the phone this second
    2  SEEN TODAY (open)          last-seen visible AND seen today
    3  RECENTLY (hidden)          last-seen hidden, Rubika says "Recently"
    4  OLDER (open)               last-seen visible but before today
    5  WEEK AGO (hidden)
    6  MONTH AGO (hidden)
    7  LONG AGO / unknown

and drops recipients that can never receive anything:
deleted accounts, Rubika-suspended accounts, blocked, no SendMessages access.

Why this module exists instead of a patch to rubika_client.py
-------------------------------------------------------------
`rubika_client.py` is pinned byte-identical by project_notes/CORE_RULES.md, so
its `get_ordered_recipients()` is NOT touched. This module reimplements the
ordering beside it and the call sites choose it, with a guarded fallback to the
original. `ordered_recipients()` returns the SAME `(ordered, stats)` shape, and
`stats` keeps its original keys (contacts / groups / with_chat), so every
existing caller keeps working.

The three defects this fixes (each verified against a live account of 543
contacts, see scripts/send_order_lab.py which holds the research harness):

  1. `_is_online()` in the base layer tests `user["status"]`, a key the
     getContacts user object does not have — 0 of 543 contacts carried it, so
     the "online first" tier never fired and the list silently degraded to a
     last_online sort. Presence must come from
     `online_time{type: Exact|Approximate}`.
  2. For Approximate (privacy-masked) users `last_online` is rounded down to a
     30-minute grid, so it was being sorted as if it were as precise as a real
     timestamp. Masked users are now ranked by their bucket instead.
  3. Everyone with an existing chat was placed ahead of everyone else, so a
     contact online RIGHT NOW but with no chat history landed after the whole
     chat list.

Everything here is read-only: it reads contacts and chats and returns an order.
It never sends, never writes to the DB, and never changes an account.
"""
from __future__ import annotations

import time

import db

# --------------------------------------------------------------------------- #
# Tunables (local on purpose: no new .env keys, no config.py change).
# --------------------------------------------------------------------------- #
ONLINE_WINDOW_SEC = 75      # exact last-seen inside this window == "online now"
PAGE_CAP = 300              # pagination safety cap

# VERIFIED on a live account: Rubika returns
#   Recently / WeekAgo / MonthAgo / LongAgo
# Telegram-style spellings kept as harmless aliases.
_APPROX_ORDER = {
    "online": 0,
    "recently": 1,
    "weekago": 2, "lastweek": 2, "last week": 2,
    "monthago": 3, "lastmonth": 3, "last month": 3,
    "longago": 4, "longtimeago": 4, "long time ago": 4,
}

TIERS = (
    "ONLINE_NOW",
    "TODAY_OPEN",
    "RECENTLY_HIDDEN",
    "EXACT_OLDER",
    "WEEK_HIDDEN",
    "MONTH_HIDDEN",
    "COLD",
)
_TIER_INDEX = {t: i for i, t in enumerate(TIERS)}

TIER_LABEL = {
    "ONLINE_NOW": "ONLINE NOW",
    "TODAY_OPEN": "SEEN TODAY (last-seen open)",
    "RECENTLY_HIDDEN": "RECENTLY (last-seen hidden)",
    "EXACT_OLDER": "OLDER (last-seen open)",
    "WEEK_HIDDEN": "WEEK AGO (hidden)",
    "MONTH_HIDDEN": "MONTH AGO (hidden)",
    "COLD": "LONG AGO / unknown",
}

STRATEGIES = ("priority", "hybrid", "mutual")
DEFAULT_STRATEGY = "priority"
K_STRATEGY = "send_order_strategy"       # app_settings key (no schema change)


def get_strategy() -> str:
    """Panel-selectable, defaults to the requested presence priority."""
    try:
        v = str(db.get_setting(K_STRATEGY, DEFAULT_STRATEGY) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return DEFAULT_STRATEGY
    return v if v in STRATEGIES else DEFAULT_STRATEGY


def set_strategy(value: str) -> None:
    v = str(value or "").strip().lower()
    db.set_setting(K_STRATEGY, v if v in STRATEGIES else DEFAULT_STRATEGY)


# --------------------------------------------------------------------------- #
# Field decoding
# --------------------------------------------------------------------------- #
def _d(obj):
    if isinstance(obj, dict):
        return obj
    for attr in ("original_update", "_data", "data", "__dict__"):
        v = getattr(obj, attr, None)
        if isinstance(v, dict):
            return v
    return {}


def _name_of(u) -> str:
    d = _d(u)
    return (str(d.get("first_name") or "") + " "
            + str(d.get("last_name") or "")).strip()


def _guid_of(o) -> str:
    d = _d(o)
    return str(d.get("user_guid") or d.get("object_guid") or "")


def presence_of(user) -> dict:
    """Decode a contact into an HONEST presence record.

    {exact, ts, bucket, online}
      exact  -> a real timestamp was given (not privacy-masked)
      ts     -> that timestamp (0 when masked/unknown)
      bucket -> normalised Approximate period ("" when exact)
      online -> exact AND inside ONLINE_WINDOW_SEC

    `status` is never consulted (it does not exist) and a masked timestamp is
    never promoted to a real one.
    """
    u = _d(user)
    ot = u.get("online_time")
    ot = ot if isinstance(ot, dict) else {}
    kind = str(ot.get("type") or "").strip().lower()

    if kind == "exact":
        ts = ot.get("exact_time")
        if ts is None:
            ts = u.get("last_online")
        try:
            ts = int(ts or 0)
        except (TypeError, ValueError):
            ts = 0
        age = time.time() - ts if ts else 1e9
        return {"exact": True, "ts": ts, "bucket": "",
                "online": bool(ts) and age <= ONLINE_WINDOW_SEC}

    if kind == "approximate":
        bucket = str(ot.get("approximate_period") or "").strip().lower()
        return {"exact": False, "ts": 0, "bucket": bucket,
                "online": bucket == "online"}

    try:
        ts = int(u.get("last_online") or 0)
    except (TypeError, ValueError):
        ts = 0
    return {"exact": False, "ts": ts, "bucket": "", "online": False}


def _day_start(ts: float | None = None) -> int:
    """Local midnight, so "today" means today on the operator's clock."""
    lt = time.localtime(ts if ts is not None else time.time())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, 0, 0, -1)))


def presence_tier(pres: dict, now: float | None = None) -> str:
    if pres["online"]:
        return "ONLINE_NOW"
    if pres["exact"] and pres["ts"]:
        return "TODAY_OPEN" if pres["ts"] >= _day_start(now) else "EXACT_OLDER"
    b = pres["bucket"]
    if b == "recently":
        return "RECENTLY_HIDDEN"
    if b in ("weekago", "lastweek", "last week"):
        return "WEEK_HIDDEN"
    if b in ("monthago", "lastmonth", "last month"):
        return "MONTH_HIDDEN"
    return "COLD"


def is_suspended(user) -> bool:
    """Rubika marks suspended accounts via warning_info (BlockUser /
    "این کاربر تعلیق می باشد"). They are NOT flagged is_deleted, so without
    this check every send to them is wasted."""
    w = _d(user).get("warning_info")
    if not w:
        return False
    if isinstance(w, dict):
        return ("BlockUser" in str(w.get("warning_id") or "")
                or "تعلیق" in str(w.get("text") or ""))
    return bool(w)


def chat_signals(chat) -> dict:
    """Two-way / deliverability signals carried by a chat object."""
    c = _d(chat)
    lm = c.get("last_message")
    lm = lm if isinstance(lm, dict) else {}
    access = c.get("access") or []
    if not isinstance(access, (list, tuple)):
        access = []
    try:
        unseen = int(c.get("count_unseen") or 0)
    except (TypeError, ValueError):
        unseen = 0
    peer = str(c.get("last_seen_peer_mid") or "0").strip()
    return {
        "guid": _guid_of(c),
        "type": str((c.get("abs_object") or {}).get("type")
                    or c.get("type") or "").strip().lower(),
        "peer_read_us": peer not in ("", "0"),
        "they_wrote": (lm.get("is_mine") is False) or unseen > 0,
        "ask_spam": c.get("show_ask_spam") is True,
        "blocked": c.get("is_blocked") is True,
        "can_send": ("SendMessages" in access) if access else True,
        "chat_time": c.get("time") or 0,
    }


def _group_rank(rec, strategy: str) -> int:
    if strategy == "mutual":
        return 0 if rec["mutual"] else 1
    if strategy == "hybrid":
        if rec["tier"] == "ONLINE_NOW":
            return 0
        return 1 if rec["mutual"] else 2
    return 0


def classify(user, sig: dict | None) -> dict:
    u = _d(user)
    pres = presence_of(user)
    sig = sig or {}
    excluded = ""
    if u.get("is_deleted") is True:
        excluded = "deleted"
    elif is_suspended(u):
        excluded = "suspended"
    elif sig.get("blocked"):
        excluded = "blocked us"
    elif sig.get("can_send") is False:
        excluded = "no send access"
    elif sig.get("ask_spam"):
        excluded = "spam risk"
    has_chat = bool(sig)
    return {
        "guid": _guid_of(u),
        "name": _name_of(u),
        "tier": presence_tier(pres),
        "excluded": excluded,
        "mutual": bool(has_chat and (sig.get("they_wrote")
                                     or sig.get("peer_read_us"))),
        "exact": pres["exact"],
        "ts": pres["ts"],
        "bucket": pres["bucket"],
        "online": pres["online"],
        "chat_time": sig.get("chat_time") or 0,
    }


def build(users, chats, strategy: str | None = None, groups: int = 0):
    """PURE core. Returns (ordered_records, stats)."""
    strategy = strategy if strategy in STRATEGIES else get_strategy()
    sigs = {}
    for ch in chats or []:
        s = chat_signals(ch)
        if s["guid"] and s["type"] in ("user", ""):
            sigs[s["guid"]] = s

    recs = []
    for u in users or []:
        if _guid_of(u):
            recs.append(classify(u, sigs.get(_guid_of(u))))

    keep = [r for r in recs if not r["excluded"]]
    dropped = [r for r in recs if r["excluded"]]

    keep.sort(key=lambda r: (
        _group_rank(r, strategy),
        _TIER_INDEX.get(r["tier"], 99),
        -int(r["ts"] or 0),
        _APPROX_ORDER.get(r["bucket"], 9),
        -int(r["chat_time"] or 0),
        r["guid"],
    ))

    tiers = {}
    for r in keep:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    reasons = {}
    for r in dropped:
        reasons[r["excluded"]] = reasons.get(r["excluded"], 0) + 1

    stats = {
        # ---- ORIGINAL keys: every existing caller depends on these ----
        "contacts": len(recs),
        "groups": groups,
        "with_chat": len(set(sigs) & {r["guid"] for r in recs}),
        # ---- added ----
        "strategy": strategy,
        "sendable": len(keep),
        "excluded": len(dropped),
        "excluded_reasons": reasons,
        "online_now": tiers.get("ONLINE_NOW", 0),
        "today_open": tiers.get("TODAY_OPEN", 0),
        "recently_hidden": tiers.get("RECENTLY_HIDDEN", 0),
        "older_open": tiers.get("EXACT_OLDER", 0),
        "week_hidden": tiers.get("WEEK_HIDDEN", 0),
        "month_hidden": tiers.get("MONTH_HIDDEN", 0),
        "cold": tiers.get("COLD", 0),
        "mutual": sum(1 for r in keep if r["mutual"]),
        "tiers": tiers,
    }
    return keep, stats


# --------------------------------------------------------------------------- #
# Live fetch (read-only)
# --------------------------------------------------------------------------- #
async def _fetch(client, what: str):
    """Paginate getContacts / getChats and keep the RAW fields (online_time,
    warning_info, ... which the base get_contacts_full() strips)."""
    out, seen = [], set()
    start = None
    key = "users" if what == "contacts" else "chats"
    fn = client.get_contacts if what == "contacts" else client.get_chats
    for _ in range(PAGE_CAP):
        res = await (fn(start) if start else fn())
        d = _d(res) or {}
        items = d.get(key)
        if items is None:
            items = getattr(res, key, None) or []
        for it in items:
            g = _guid_of(it)
            if g and g in seen:
                continue
            if g:
                seen.add(g)
            out.append(_d(it))
        start = d.get("next_start_id") or d.get("next_start_index")
        if not start or not items:
            break
    return out


async def ordered_recipients(client, strategy: str | None = None):
    """DROP-IN replacement for rubika_client.get_ordered_recipients().

    Returns (ordered, stats) with ordered = [{guid, name}, ...] in send order
    and stats carrying the original keys plus the per-tier counts.
    """
    users = await _fetch(client, "contacts")
    chats = await _fetch(client, "chats")
    groups = sum(1 for c in chats
                 if str((_d(c).get("abs_object") or {}).get("type")
                        or _d(c).get("type") or "").lower() == "group")
    keep, stats = build(users, chats, strategy, groups=groups)
    return [{"guid": r["guid"], "name": r["name"]} for r in keep], stats


def summary_rows(stats: dict) -> list:
    """The `• Key : Value` rows for the login card (same shell as before)."""
    def g(k):
        v = stats.get(k)
        return "-" if v is None else v
    rows = [
        f"• Contacts      : {g('contacts')}",
    ]
    if stats.get("sendable") is not None:
        extra = stats.get("excluded") or 0
        rows.append(f"• Sendable      : {stats['sendable']}"
                    + (f"   (-{extra} dead/suspended)" if extra else ""))
    rows += [
        f"• Online Now    : {g('online_now')}",
        f"• Seen Today    : {g('today_open')}",
        f"• Recently      : {g('recently_hidden')}",
        f"• Groups        : {g('groups')}",
        f"• Chat Contacts : {g('with_chat')}",
    ]
    return rows
