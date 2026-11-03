#!/usr/bin/env python3
"""
send_order_lab.py — research lab for Rubika recipient ORDERING.
===============================================================

STANDALONE / RESEARCH ONLY.
  * The bot does NOT import this file. Nothing here changes bot behaviour.
  * It never deletes, never changes account status, never mass-sends.
  * `--probe` is strictly READ-ONLY. `--send-test` sends exactly ONE message
    whose text is a fixed "this is a test" notice.

Why this exists
---------------
The panel's current order comes from `rubika_client.get_ordered_recipients()`:
    1) contacts we already have a chat with (chat-recency order)
    2) then contacts that are "online"
    3) then the rest by `last_online` desc

Checking that against REAL Rubika API responses turned up three defects
(reproduced offline by `--selftest`):

  BUG 1 — "online" never matches.
      `_is_online()` reads  user["status"] == "online"  but the `getContacts`
      user object has NO `status` key at all. Real keys are:
        user_guid, first_name, last_name, phone, username, last_online,
        is_deleted, is_verified, online_time{type, exact_time|approximate_period}
      So tier (2) is DEAD CODE and the list silently degrades to "by
      last_online". (`presence_rank()` used by Pool Brain has the same defect.)
      Presence must be derived from `online_time`:
        type == "Exact"       -> exact_time is a real timestamp
        type == "Approximate" -> last-seen is privacy-masked

  BUG 2 — privacy-masked last-seen is treated as if it were exact.
      For `Approximate` users `last_online` is rounded down to a 30-minute grid
      (verified on a real response: 1723235400 % 1800 == 0), so:
        * a masked contact who is online RIGHT NOW can look up to 30 minutes
          stale and be ranked below someone last seen 5 minutes ago;
        * ordering inside the masked group is arbitrary within each 30-minute
          bucket — exactly the distinction "most recently online first" needs;
        * `approximate_period` ("Recently" / "LastWeek" / "LastMonth" /
          "LongTimeAgo") is the ONLY trustworthy signal for these users and the
          current sort ignores it completely: two masked contacts that share a
          rounded timestamp are ordered identically whether the bucket says
          "Recently" or "LongTimeAgo".

  BUG 3 — the real first tier is "has a chat", not "is online".
      Every contact with an existing chat is placed ahead of everyone else, so
      a contact who is online RIGHT NOW but has no chat history is sent to
      after the whole chat list. That contradicts "always hit the online ones
      first".

Mutual contacts (the Telegram question)
---------------------------------------
Rubika's API exposes NO `mutual_contact` flag (Telegram has one on UserFull).
Grepping every documented response sample, the only relationship-ish field is
`show_ask_spam`. BUT the chat object from ONE paginated `getChats` call carries
strong two-way signals, so mutuality can be approximated cheaply and reliably:

    last_seen_peer_mid != "0"   the peer OPENED our chat and read our messages
    last_message.is_mine False  the peer actually WROTE to us  (strongest)
    count_unseen > 0            they wrote and we have not read it yet
    show_ask_spam == True       Rubika is warning them we are a stranger
                                -> highest spam-report risk, excluded by default
    is_blocked == True          blocked us
    access lacks "SendMessages" we cannot message them at all
    online_time.type == "Exact" they are not hiding last-seen from us
                                (a weak but useful "they know me" hint)
    count_common_groups > 0     shared groups (only via per-contact
                                getChatInfo -> opt-in `--deep N` sampling)

So: yes, two-way contacts CAN be separated and sent first, without any extra
API cost beyond the `getChats` call the panel already makes.

Usage
-----
    # offline: prove the logic + reproduce the three bugs (no network, no login)
    python3 scripts/send_order_lab.py --selftest

    # read-only probe of a real account (run it while that account is IDLE)
    python3 scripts/send_order_lab.py --probe 98912xxxxxxx
    python3 scripts/send_order_lab.py --probe 98912xxxxxxx --strategy mutual
    python3 scripts/send_order_lab.py --probe 98912xxxxxxx --deep 25 --json out.json

    # send ONE fixed test message (text is hard-coded as a test notice)
    python3 scripts/send_order_lab.py --probe 98912xxxxxxx --send-test 989370024307
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# --------------------------------------------------------------------------- #
# Same sub-package path shim as main.py, so this script can be run directly
# from the repo root without touching any module.
# --------------------------------------------------------------------------- #
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The repo ROOT must come too: config.py lives there. main.py gets it for free
# (it IS in the root), but this script sits in scripts/, so sys.path[0] is
# scripts/ and `import config` would fail.
for _sub in ("", "core", "rubika", "workers", "bot_app", "status"):
    _p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
# config.py reads .env with python-dotenv relative to the CWD, so make the repo
# root the CWD no matter where the script was invoked from.
try:
    os.chdir(_ROOT)
except OSError:
    pass

# The fixed text used by --send-test. Deliberately explicit so a recipient
# cannot mistake it for a real/marketing message.
TEST_MESSAGE = "این پیام تست هست"

# A user is treated as "online right now" when their EXACT last-seen is within
# this many seconds. Rubika's own client flips to "online" at about this point.
ONLINE_WINDOW_SEC = 75

# Ranking of the privacy buckets Rubika returns for Approximate last-seen.
# VERIFIED against a live account (543 contacts): Rubika returns
#   Recently / WeekAgo / MonthAgo / LongAgo
# The Telegram-style spellings are kept only as harmless aliases.
_APPROX_ORDER = {
    "online": 0,
    "recently": 1,
    "weekago": 2, "lastweek": 2, "last week": 2,
    "monthago": 3, "lastmonth": 3, "last month": 3,
    "longago": 4, "longtimeago": 4, "long time ago": 4,
}

_APPROX_TIER = {
    "recently": "APPROX_RECENTLY",
    "weekago": "APPROX_WEEK", "lastweek": "APPROX_WEEK", "last week": "APPROX_WEEK",
    "monthago": "APPROX_MONTH", "lastmonth": "APPROX_MONTH", "last month": "APPROX_MONTH",
}


# =========================================================================== #
# PURE LOGIC  (no network — this is what --selftest exercises)
# =========================================================================== #
def _d(obj):
    """Normalise an API object to a plain dict (rubpy may wrap it)."""
    if isinstance(obj, dict):
        return obj
    for attr in ("original_update", "_data", "data", "__dict__"):
        v = getattr(obj, attr, None)
        if isinstance(v, dict):
            return v
    return {}


def presence_of(user) -> dict:
    """Decode a `getContacts` user into an HONEST presence record.

    Returns {exact: bool, ts: int, bucket: str, online: bool}

      exact  -> we were given a real timestamp (not privacy-masked)
      ts     -> that timestamp (0 when masked/unknown)
      bucket -> normalised Approximate period ("" when exact)
      online -> exact AND within ONLINE_WINDOW_SEC

    This is the fix for BUG 1 and BUG 2: `status` is never consulted (it does
    not exist), and a masked timestamp is NOT promoted to a real one.
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
        # Rubika can report the literal bucket "Online" with no exact time.
        return {"exact": False, "ts": 0, "bucket": bucket,
                "online": bucket == "online"}

    # No online_time at all: fall back to last_online but mark it NOT exact,
    # so it can never outrank a genuinely exact timestamp.
    try:
        ts = int(u.get("last_online") or 0)
    except (TypeError, ValueError):
        ts = 0
    return {"exact": False, "ts": ts, "bucket": "", "online": False}


def chat_signals(chat) -> dict:
    """Decode a `getChats` chat into the two-way / spam-risk signals."""
    c = _d(chat)
    lm = c.get("last_message")
    lm = lm if isinstance(lm, dict) else {}
    access = c.get("access") or []
    if not isinstance(access, (list, tuple)):
        access = []

    def _mid(key):
        v = str(c.get(key) or "0").strip()
        return v not in ("", "0")

    try:
        unseen = int(c.get("count_unseen") or 0)
    except (TypeError, ValueError):
        unseen = 0

    return {
        "guid": c.get("object_guid") or "",
        "type": str((c.get("abs_object") or {}).get("type")
                    or c.get("type") or "").strip().lower(),
        "peer_read_us": _mid("last_seen_peer_mid"),
        "they_wrote": (lm.get("is_mine") is False) or unseen > 0,
        "unseen": unseen,
        "ask_spam": c.get("show_ask_spam") is True,
        "blocked": c.get("is_blocked") is True,
        "can_send": ("SendMessages" in access) if access else True,
        "chat_time": c.get("time") or 0,
    }


# Tier ids, best -> worst. The *priority* of these is what a strategy changes.
TIERS = (
    "ONLINE_NOW",      # at the phone right now (exact presence inside window)
    "REPLIED",         # two-way: they wrote to us
    "READ_US",         # two-way: they read our messages
    "CHAT_ONE_WAY",    # chat exists but they never read it
    "EXACT_PRESENCE",  # no chat, but not hiding last-seen from us
    "APPROX_RECENTLY",
    "APPROX_WEEK",
    "APPROX_MONTH",
    "COLD",            # long ago / unknown
)

STRATEGIES = {
    # default: whoever is at the phone right now, then two-way, then presence
    "hybrid": ("ONLINE_NOW", "REPLIED", "READ_US", "CHAT_ONE_WAY",
               "EXACT_PRESENCE", "APPROX_RECENTLY", "APPROX_WEEK",
               "APPROX_MONTH", "COLD"),
    # safest for spam: every two-way contact before any stranger
    "mutual": ("REPLIED", "READ_US", "ONLINE_NOW", "CHAT_ONE_WAY",
               "EXACT_PRESENCE", "APPROX_RECENTLY", "APPROX_WEEK",
               "APPROX_MONTH", "COLD"),
    # literal reading of "online first, then most recent"
    "presence": ("ONLINE_NOW", "EXACT_PRESENCE", "REPLIED", "READ_US",
                 "CHAT_ONE_WAY", "APPROX_RECENTLY", "APPROX_WEEK",
                 "APPROX_MONTH", "COLD"),
}


def classify(user, sig: dict | None) -> dict:
    """Assign one contact to a tier and compute its in-tier sort key."""
    u = _d(user)
    pres = presence_of(user)
    sig = sig or {}

    excluded = ""
    if u.get("is_deleted") is True:
        excluded = "deleted account"
    elif sig.get("blocked"):
        excluded = "blocked us"
    elif sig.get("can_send") is False:
        excluded = "no SendMessages access"
    elif sig.get("ask_spam"):
        # Rubika itself is warning this peer that we are a stranger: messaging
        # them is what earns spam reports.
        excluded = "show_ask_spam (stranger / spam risk)"

    has_chat = bool(sig)
    if pres["online"]:
        tier = "ONLINE_NOW"
    elif has_chat and sig.get("they_wrote"):
        tier = "REPLIED"
    elif has_chat and sig.get("peer_read_us"):
        tier = "READ_US"
    elif has_chat:
        tier = "CHAT_ONE_WAY"
    elif pres["exact"] and pres["ts"]:
        tier = "EXACT_PRESENCE"
    else:
        tier = _APPROX_TIER.get(pres["bucket"], "COLD")

    return {
        "guid": u.get("user_guid") or u.get("object_guid") or "",
        "name": (str(u.get("first_name") or "") + " "
                 + str(u.get("last_name") or "")).strip(),
        "phone": u.get("phone") or "",
        "tier": tier,
        "excluded": excluded,
        "mutual": bool(has_chat and (sig.get("they_wrote") or sig.get("peer_read_us"))),
        "exact": pres["exact"],
        "ts": pres["ts"],
        "bucket": pres["bucket"],
        "online": pres["online"],
        "chat_time": sig.get("chat_time") or 0,
        "unseen": sig.get("unseen") or 0,
    }


def build_order(users, chats, strategy: str = "hybrid",
                include_excluded: bool = False):
    """THE PROPOSED LIST. Returns (ordered_records, report_dict).

    One paginated getContacts + one paginated getChats is all it costs — the
    same two calls the panel already performs.
    """
    prio = {t: i for i, t in enumerate(STRATEGIES.get(strategy, STRATEGIES["hybrid"]))}

    by_guid = {}
    for ch in chats or []:
        s = chat_signals(ch)
        if s["guid"] and s["type"] in ("user", ""):
            by_guid[s["guid"]] = s

    recs = []
    for u in users or []:
        g = _d(u).get("user_guid") or _d(u).get("object_guid")
        if not g:
            continue
        recs.append(classify(u, by_guid.get(g)))

    keep = [r for r in recs if include_excluded or not r["excluded"]]
    dropped = [r for r in recs if r["excluded"]]

    def sort_key(r):
        # inside a tier: exact presence first, then newest presence, then
        # newest chat activity, then the privacy bucket.
        return (
            prio.get(r["tier"], 99),
            0 if r["exact"] else 1,
            -int(r["ts"] or 0),
            -int(r["chat_time"] or 0),
            _APPROX_ORDER.get(r["bucket"], 9),
            r["guid"],
        )

    keep.sort(key=sort_key)

    counts = {}
    for r in keep:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    # Counters describe the list we would ACTUALLY send (`keep`), so an
    # excluded-but-online contact can never inflate "online now".
    report = {
        "strategy": strategy,
        "contacts": len(recs),
        "sendable": len(keep),
        "excluded": len(dropped),
        "excluded_reasons": _tally(r["excluded"] for r in dropped),
        "tiers": counts,
        "mutual": sum(1 for r in keep if r["mutual"]),
        "one_way": sum(1 for r in keep if not r["mutual"]),
        "online_now": sum(1 for r in keep if r["online"]),
        "exact_presence": sum(1 for r in keep if r["exact"]),
        "masked_presence": sum(1 for r in keep if not r["exact"]),
    }
    return keep, report


def legacy_order(users, chats):
    """Reproduce the CURRENT bot order byte-for-byte, for comparison:
    chat-first (chat order as returned), then `status == online`, then
    `last_online` desc. Used only to show the difference."""
    by_guid = {}
    for u in users or []:
        g = _d(u).get("user_guid") or _d(u).get("object_guid")
        if g:
            by_guid[g] = _d(u)

    user_chats = []
    for ch in chats or []:
        c = _d(ch)
        t = str((c.get("abs_object") or {}).get("type") or c.get("type") or "").lower()
        g = c.get("object_guid")
        if t == "user" and g and g in by_guid and g not in user_chats:
            user_chats.append(g)

    with_chat = [g for g in user_chats if g in by_guid]
    s = set(with_chat)
    rest = [g for g in by_guid if g not in s]

    def _lo(g):
        d = by_guid[g]
        v = d.get("last_online")
        if v is None:
            ot = d.get("online_time")
            if isinstance(ot, dict):
                v = ot.get("exact_time")
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _is_online(g):
        # the real bug: `status` is not a field of a getContacts user
        return str(by_guid[g].get("status") or "").lower() == "online"

    rest.sort(key=lambda g: (1 if _is_online(g) else 0, _lo(g)), reverse=True)
    return with_chat + rest


def _legacy_lastonline(users, guid):
    """The timestamp the CURRENT code would sort this contact by."""
    for u in users:
        if _d(u).get("user_guid") == guid:
            d = _d(u)
            v = d.get("last_online")
            if v is None:
                ot = d.get("online_time")
                if isinstance(ot, dict):
                    v = ot.get("exact_time")
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _tally(it):
    out = {}
    for v in it:
        out[v] = out.get(v, 0) + 1
    return out


# =========================================================================== #
# LIVE PROBE  (read-only)
# =========================================================================== #
async def _fetch_raw(client, what: str, cap: int = 200):
    """Paginate getContacts / getChats and return the RAW objects."""
    out = []
    seen = set()
    start = None
    key = "users" if what == "contacts" else "chats"
    fn = client.get_contacts if what == "contacts" else client.get_chats
    for _ in range(cap):
        res = await (fn(start) if start else fn())
        d = _d(res) or {}
        items = d.get(key)
        if items is None:
            items = getattr(res, key, None) or []
        for it in items:
            g = (_d(it).get("user_guid") or _d(it).get("object_guid") or "")
            if g and g in seen:
                continue
            if g:
                seen.add(g)
            out.append(_d(it))
        start = d.get("next_start_id") or d.get("next_start_index")
        if not start or not items:
            break
    return out


def _field_audit(users, chats):
    """Prove on LIVE data which fields actually exist (the bug evidence)."""
    def keys(objs):
        k = {}
        for o in objs:
            for kk in _d(o):
                k[kk] = k.get(kk, 0) + 1
        return k

    uk = keys(users)
    n = max(1, len(users))
    kinds = _tally(str((_d(u).get("online_time") or {}).get("type") or "-")
                   for u in users)
    buckets = _tally(str((_d(u).get("online_time") or {}).get("approximate_period") or "-")
                     for u in users
                     if str((_d(u).get("online_time") or {}).get("type") or "").lower()
                     == "approximate")
    masked_round = sum(
        1 for u in users
        if str((_d(u).get("online_time") or {}).get("type") or "").lower() == "approximate"
        and int(_d(u).get("last_online") or 0) % 1800 == 0)
    # `warning_info` showed up on a live account and is undocumented; it may be
    # Rubika's own scam/spam marker, which would beat show_ask_spam as a filter.
    warn = [_d(u).get("warning_info") for u in users if _d(u).get("warning_info")]
    unknown_buckets = sorted(
        {str((_d(u).get("online_time") or {}).get("approximate_period") or "").lower()
         for u in users} - set(_APPROX_TIER) - {"", "online"})
    return {
        "warning_info_count": len(warn),
        "warning_info_samples": warn[:3],
        "unmapped_buckets": unknown_buckets,
        "contacts_seen": len(users),
        "chats_seen": len(chats),
        "contact_has_status_field": uk.get("status", 0),
        "contact_status_pct": round(100.0 * uk.get("status", 0) / n, 1),
        "online_time_types": kinds,
        "approximate_buckets": buckets,
        "masked_ts_rounded_to_30min": masked_round,
        "contact_keys": sorted(uk),
        "chat_keys": sorted(keys(chats)),
    }


async def probe(phone: str, strategy: str, deep: int, out_json: str,
                show: int, send_test: str):
    import account_conn
    import rubika_client as rb

    print(f"\n  connecting to {phone} (read-only) ...")
    await account_conn.close(rb.normalize_phone(phone))
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        users = await _fetch_raw(client, "contacts")
        chats = await _fetch_raw(client, "chats")

        audit = _field_audit(users, chats)
        print_header("LIVE FIELD AUDIT  (bug evidence)")
        print(f"  contacts fetched        : {audit['contacts_seen']}")
        print(f"  chats fetched           : {audit['chats_seen']}")
        print(f"  contacts with 'status'  : {audit['contact_has_status_field']}"
              f"  ({audit['contact_status_pct']}%)   <- bot's _is_online() reads this")
        print(f"  online_time.type        : {audit['online_time_types']}")
        print(f"  approximate buckets     : {audit['approximate_buckets']}")
        print(f"  masked ts on 30-min grid: {audit['masked_ts_rounded_to_30min']}"
              "   <- fake precision")
        if audit["unmapped_buckets"]:
            print(f"  ⚠ UNMAPPED buckets      : {audit['unmapped_buckets']}"
                  "   <- tell me, they fall to COLD")
        if audit["warning_info_count"]:
            print(f"  warning_info present on : {audit['warning_info_count']}"
                  f"   sample={audit['warning_info_samples']}")
        print(f"  contact keys            : {', '.join(audit['contact_keys'])}")

        ordered, report = build_order(users, chats, strategy)
        legacy = legacy_order(users, chats)
        print_verdict(audit, report, legacy, ordered)
        print_report(report)
        print_diff(legacy, [r["guid"] for r in ordered], ordered)

        # The real online test: who is online RIGHT NOW per the fixed logic.
        # Open Rubika on your phone and compare this list.
        on = [r for r in ordered if r["online"]]
        print_header(f"ONLINE RIGHT NOW — fixed logic ({len(on)})")
        if not on:
            print("  nobody online this second. Re-run in a minute;")
            print("  ONLINE_NOW needs an EXACT last-seen inside "
                  f"{ONLINE_WINDOW_SEC}s.")
        for i, r in enumerate(on[:40], 1):
            secs = int(time.time() - r["ts"]) if r["ts"] else -1
            print(f"  {i:>3}. {r['name'][:26]:<26} seen {secs:>3}s ago"
                  f"   legacy position #{legacy.index(r['guid']) + 1}")

        print_header(f"PROPOSED FIRST {show}  (strategy={strategy})")
        for i, r in enumerate(ordered[:show], 1):
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
                    if r["ts"] else (r["bucket"] or "-"))
            print(f"  {i:>3}. {r['tier']:<16} mutual={'Y' if r['mutual'] else 'n'} "
                  f"exact={'Y' if r['exact'] else 'n'}  {when:<17} {r['name'][:22]}")

        deep_info = None
        if deep > 0:
            deep_info = await _deep_sample(client, ordered, deep)

        if send_test:
            await _send_one_test(client, send_test)

        if out_json:
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump({"audit": audit, "report": report,
                           "deep": deep_info,
                           "order": ordered}, f, ensure_ascii=False, indent=1)
            print(f"\n  full result written to {out_json}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _items(resp, *prefer):
    """Pull the list payload out of ANY Rubika response.

    Learned the hard way: guessing key names produced false zeros twice
    (getTopChatUsers actually returns `abs_users`, not `top_chat_users`).
    So after the known keys we fall back to self-discovery — the first value
    that is a list of dicts — and we always report WHICH key was used.
    Returns (items, key_name).
    """
    d = _d(resp)
    for k in (*prefer, "abs_users", "users", "top_chat_users", "objects",
              "blocked_users", "contacts", "chats"):
        v = d.get(k)
        if isinstance(v, list) and v:
            return v, k
    for k, v in d.items():                      # self-discovering fallback
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v, f"{k} (auto-detected)"
    return [], "none"


def _guid(obj):
    """Rubika uses object_guid in some responses and user_guid in others."""
    d = _d(obj)
    return str(d.get("object_guid") or d.get("user_guid") or "")


def _name_of_user(u):
    d = _d(u)
    return (str(d.get("first_name") or "") + " "
            + str(d.get("last_name") or "")).strip() or _guid(u)


async def _raw(client, method: str, payload: dict | None = None):
    """Call ANY Rubika API method, including ones rubpy does not wrap.
    rubpy exposes a generic `builder(name=..., input=..., dict=True)`."""
    return await client.builder(name=method, input=payload or {}, dict=True)


async def mutual_probe(phone: str, show: int, out_json: str):
    """Settle the question: is there a precise, Telegram-style mutual signal?

    Probes four endpoints that the panel does not use today (two of them are
    not even wrapped by rubpy) and then runs a CORRELATION TEST to decide
    whether 'Exact last-seen' can be trusted as a 'they saved me' signal.
    Strictly read-only.
    """
    import account_conn
    import rubika_client as rb

    print(f"\n  connecting to {phone} (read-only) ...")
    await account_conn.close(rb.normalize_phone(phone))
    client = rb.open_client(phone)
    found = {}
    try:
        await rb.connect_ready(client)
        users = await _fetch_raw(client, "contacts")
        chats = await _fetch_raw(client, "chats")
        sigs = {}
        for ch in chats:
            s = chat_signals(ch)
            if s["guid"]:
                sigs[s["guid"]] = s

        # ---- 1. our own privacy settings: learn the enum vocabulary -------
        print_header("1. getPrivacySetting  (what Exact/Approximate mean)")
        try:
            ps = await _raw(client, "getPrivacySetting")
            found["privacy"] = ps
            for item in (ps.get("privacy_settings") or ps.get("settings") or []):
                print(f"   {item}")
            if not (ps.get("privacy_settings") or ps.get("settings")):
                print(f"   {json.dumps(ps, ensure_ascii=False)[:600]}")
        except Exception as e:
            print(f"   unavailable: {repr(e)[:120]}")

        # ---- 2. Rubika's OWN affinity ranking ----------------------------
        print_header("2. getTopChatUsers  (Rubika's own 'who you talk to most')")
        top = []
        try:
            tr = await _raw(client, "getTopChatUsers")
            found["top_chat_users"] = tr
            raw_top, key = _items(tr, "top_chat_users")
            top = [g for g in (_guid(t) for t in raw_top) if g]
            print(f"   payload key   : {key}")
            print(f"   returned      : {len(top)} users")
            if raw_top:
                print(f"   object keys   : {sorted(_d(raw_top[0]))}")
            contact_guids = {str(_d(u).get("user_guid")) for u in users}
            two_way = {g for g, s in sigs.items()
                       if s["they_wrote"] or s["peer_read_us"]}
            inter = [g for g in top if g in two_way]
            no_chat = [g for g in top if g not in sigs]
            not_contact = [g for g in top if g not in contact_guids]
            print(f"   already in our two-way set : {len(inter)}/{len(top)}")
            print(f"   in top list but NO chat    : {len(no_chat)}"
                  "   <- EXTRA two-way signal")
            print(f"   not even in your contacts  : {len(not_contact)}")
            # show the ones that are contacts but our chat heuristic missed
            extra = [g for g in no_chat if g in contact_guids]
            if extra:
                names = {str(_d(u).get("user_guid")): _name_of_user(u)
                         for u in users}
                print(f"   -> {len(extra)} contact(s) Rubika ranks as top chats"
                      " that our chat-based test missed:")
                for g in extra[:12]:
                    print(f"        {names.get(g, g)[:30]}")
            found["top_extra_contacts"] = len(extra)
        except Exception as e:
            print(f"   unavailable: {repr(e)[:120]}")

        # ---- 3. authoritative bulk presence ------------------------------
        print_header("3. getContactsLastOnline  (fresher presence than getContacts?)")
        guids = [str(_d(u).get("user_guid")) for u in users if _d(u).get("user_guid")]
        live = {}
        try:
            for i in range(0, min(len(guids), 600), 100):
                r = await _raw(client, "getContactsLastOnline",
                               {"user_guids": guids[i:i + 100]})
                blob = (r.get("contacts_last_online") or r.get("users") or r)
                if isinstance(blob, dict):
                    for g, v in blob.items():
                        if isinstance(v, (int, float, str)):
                            live[g] = v
                elif isinstance(blob, list):
                    for it in blob:
                        d = _d(it)
                        g = d.get("user_guid") or d.get("object_guid")
                        if g:
                            live[g] = (d.get("last_online")
                                       or d.get("time") or d.get("online_time"))
                await asyncio.sleep(0.2)
            found["last_online_sample"] = dict(list(live.items())[:5])
            print(f"   entries returned : {len(live)}")
            if live:
                print(f"   sample           : {found['last_online_sample']}")
            now = time.time()
            fresh = 0
            for g, v in live.items():
                try:
                    if now - int(v) <= ONLINE_WINDOW_SEC:
                        fresh += 1
                except (TypeError, ValueError):
                    pass
            base = sum(1 for u in users if presence_of(u)["online"])
            print(f"   online per getContacts        : {base}")
            print(f"   online per getContactsLastOnline: {fresh}")
            if fresh > base:
                print("   -> this endpoint sees MORE online users; worth adopting")
        except Exception as e:
            print(f"   unavailable: {repr(e)[:120]}")

        # ---- 4. definitive block list ------------------------------------
        print_header("4. getBlockedUsers  (definitive, vs chat is_blocked)")
        try:
            br = await _raw(client, "getBlockedUsers", {"start_id": None})
            found["blocked_raw"] = br
            blocked, bkey = _items(br, "blocked_users")
            found["blocked"] = len(blocked)
            print(f"   payload key   : {bkey}")
            print(f"   blocked users : {len(blocked)}")
            print(f"   chat-derived  : {sum(1 for s in sigs.values() if s['blocked'])}")
            want = (found.get("privacy") or {}).get(
                "privacy_setting", {}).get("count_blocked_user")
            if want is not None:
                print(f"   privacy says  : {want}"
                      + ("   ✓ match" if int(want) == len(blocked)
                         else "   ⚠ mismatch — parse still wrong"))
        except Exception as e:
            print(f"   unavailable: {repr(e)[:120]}")

        # ---- 5. THE CORRELATION TEST -------------------------------------
        print_header("5. CORRELATION — does 'Exact last-seen' mean 'they saved me'?")
        eng_tot = eng_ex = non_tot = non_ex = 0
        for u in users:
            d = _d(u)
            if d.get("is_deleted"):
                continue
            g = d.get("user_guid")
            s = sigs.get(g)
            engaged = bool(s and (s["they_wrote"] or s["peer_read_us"]))
            ex = presence_of(u)["exact"]
            if engaged:
                eng_tot += 1
                eng_ex += 1 if ex else 0
            else:
                non_tot += 1
                non_ex += 1 if ex else 0
        pe = 100.0 * eng_ex / max(1, eng_tot)
        pn = 100.0 * non_ex / max(1, non_tot)
        print(f"   engaged (they read/wrote) : {eng_ex}/{eng_tot} exact"
              f" = {pe:.0f}%")
        print(f"   never engaged             : {non_ex}/{non_tot} exact"
              f" = {pn:.0f}%")
        lift = pe - pn
        print(f"   lift                      : {lift:+.0f} percentage points")
        if lift >= 25:
            print("   => STRONG: Exact last-seen is a usable 'they saved me' proxy.")
        elif lift >= 10:
            print("   => WEAK but real; usable only as a tie-breaker.")
        else:
            print("   => NO usable signal. Exact last-seen does NOT indicate")
            print("      mutuality on this account — people who demonstrably")
            print("      read your messages still hide their last-seen.")
            print("      Conclusion: chat-derived signals remain the only")
            print("      reliable way to detect two-way contacts.")
        found["correlation"] = {"engaged_total": eng_tot, "engaged_exact": eng_ex,
                                "other_total": non_tot, "other_exact": non_ex,
                                "lift_pp": round(lift, 1)}

        if out_json:
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump(found, f, ensure_ascii=False, indent=1, default=str)
            print(f"\n  raw responses written to {out_json}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _paged(client, method: str, key: str, payload_key: str = "start_id",
                 cap: int = 300):
    """Paginate ANY list endpoint and return (items, diagnostics).

    Diagnostics exist because a silently-truncated page would make every
    downstream count wrong (this is what put the two-way count in doubt).
    """
    out, seen, pages = [], set(), []
    start = None
    for _ in range(cap):
        payload = {payload_key: start} if start else {}
        if method == "getChatsUpdates":
            payload = {"state": int(time.time()) - 86400 * 365}
        r = await _raw(client, method, payload)
        items, used = _items(r, key)
        new = 0
        for it in items:
            g = _guid(it)
            if g and g in seen:
                continue
            if g:
                seen.add(g)
            out.append(it)
            new += 1
        nxt = r.get("next_start_id") or r.get("next_start_index")
        pages.append({"got": len(items), "new": new, "key": used,
                      "next_start_id": str(nxt)[:14] if nxt else None,
                      "has_continue": r.get("has_continue")})
        start = nxt
        if not start or not items:
            break
        await asyncio.sleep(0.15)
    return out, pages


async def verify_probe(phone: str, sample_n: int, out_json: str):
    """Settle whether '16 two-way out of 543' is REAL or an undercount.

    Two independent checks:
      A. is the getChats list complete? (pagination diagnostics + a second
         opinion from getChatsUpdates)
      B. ground truth: call getChatInfo on contacts that getChats says have NO
         chat. If they DO have chat data, the two-way count was undercounted,
         and the sample tells us by how much.
    Strictly read-only.
    """
    import account_conn
    import rubika_client as rb

    print(f"\n  connecting to {phone} (read-only) ...")
    await account_conn.close(rb.normalize_phone(phone))
    client = rb.open_client(phone)
    res = {}
    try:
        await rb.connect_ready(client)

        users, cpages = await _paged(client, "getContacts", "users")
        chats, hpages = await _paged(client, "getChats", "chats")
        cg = {str(_d(u).get("user_guid")) for u in users if _d(u).get("user_guid")}
        chat_guids = {_guid(c) for c in chats if _guid(c)}

        print_header("A. is the getChats list COMPLETE?")
        print(f"   contacts : {len(users)} over {len(cpages)} page(s)")
        print(f"   chats    : {len(chats)} over {len(hpages)} page(s)")
        for i, p in enumerate(hpages, 1):
            print(f"     chat page {i}: got={p['got']} new={p['new']} "
                  f"key={p['key']} next={p['next_start_id']} "
                  f"has_continue={p['has_continue']}")
        if len(hpages) == 1 and hpages[0]["got"] and not hpages[0]["next_start_id"]:
            print("   -> ONE page, no continuation token: Rubika is reporting")
            print("      this as the whole chat list.")
        # second opinion
        try:
            upd, upages = await _paged(client, "getChatsUpdates", "chats")
            ug = {_guid(c) for c in upd if _guid(c)}
            print(f"   getChatsUpdates : {len(ug)} chats"
                  f"   (not in getChats: {len(ug - chat_guids)})")
            if ug - chat_guids:
                print("   ⚠ getChats was INCOMPLETE — updates knows more chats")
            chat_guids |= ug
            for c in upd:
                if _guid(c) not in {_guid(x) for x in chats}:
                    chats.append(c)
        except Exception as e:
            print(f"   getChatsUpdates unavailable: {repr(e)[:90]}")

        user_chats = {_guid(c) for c in chats
                      if chat_signals(c)["type"] in ("user", "")}
        with_chat = len(cg & user_chats)
        print(f"\n   contacts WITH a chat    : {with_chat}")
        print(f"   contacts with NO chat   : {len(cg - user_chats)}")
        if len(cg) and with_chat / len(cg) < 0.3:
            print("   ⚠ most contacts show no chat. For an account that has")
            print("     been mass-sending, that is suspicious -> check B.")

        print_header(f"B. GROUND TRUTH — getChatInfo on {sample_n} 'no-chat' contacts")
        nochat = [g for g in cg if g not in user_chats][:sample_n]
        hits = eng = 0
        rows = []
        for g in nochat:
            try:
                r = await _raw(client, "getChatInfo", {"object_guid": g})
                ch = _d(r).get("chat") or {}
                s = chat_signals(ch) if ch else {}
                has = bool(ch)
                engaged = bool(s.get("they_wrote") or s.get("peer_read_us"))
                hits += 1 if has else 0
                eng += 1 if engaged else 0
                rows.append({"guid": g, "has_chat": has, "engaged": engaged,
                             "last_msg": bool(ch.get("last_message")),
                             "peer_mid": ch.get("last_seen_peer_mid"),
                             "ask_spam": ch.get("show_ask_spam"),
                             "common_groups": _d(r).get("count_common_groups")})
            except Exception as e:
                rows.append({"guid": g, "error": repr(e)[:70]})
            await asyncio.sleep(0.35)
        n = max(1, len([r for r in rows if "error" not in r]))
        print(f"   sampled                     : {n}")
        print(f"   actually HAVE a chat        : {hits}  ({100*hits//n}%)")
        print(f"   show two-way engagement     : {eng}  ({100*eng//n}%)")
        res["sample"] = rows

        print_header("VERDICT — is '16 two-way' trustworthy?")
        if hits == 0:
            print("   ✓ CONFIRMED. None of the sampled 'no-chat' contacts has")
            print("     any chat, so getChats was complete and the two-way")
            print("     count is real: this account simply never had a")
            print("     conversation with most of its contacts.")
            print("     (Mass-sending with no reply leaves no two-way signal:")
            print("      the recipient has to open or answer the chat.)")
        else:
            est = len(cg - user_chats) * eng // n
            print(f"   ✗ UNDERCOUNT. {hits} of {n} sampled contacts DO have a")
            print("     chat that getChats never listed.")
            print(f"     Extrapolated missed two-way contacts: ~{est}")
            print("     The 16 figure is WRONG — detection must use")
            print("     getChatInfo (or getChatsUpdates) rather than getChats.")
        if out_json:
            with open(out_json, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=1, default=str)
            print(f"\n   detail written to {out_json}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _deep_sample(client, ordered, n: int):
    """OPT-IN: per-contact getChatInfo on a small sample, to measure how much
    extra signal `count_common_groups` adds. 1 API call per contact."""
    print_header(f"DEEP SAMPLE  ({n} contacts, getChatInfo)")
    fn = getattr(client, "get_chat_info", None)
    if fn is None:
        print("  this rubpy build has no get_chat_info() — skipped")
        return None
    rows = []
    for r in ordered[:n]:
        try:
            res = _d(await fn(r["guid"]))
            rows.append({"guid": r["guid"], "tier": r["tier"],
                         "common_groups": res.get("count_common_groups"),
                         "ask_spam": (res.get("chat") or {}).get("show_ask_spam")})
        except Exception as e:
            rows.append({"guid": r["guid"], "error": repr(e)[:80]})
        await asyncio.sleep(0.4)
    withg = sum(1 for x in rows if (x.get("common_groups") or 0) > 0)
    spam = sum(1 for x in rows if x.get("ask_spam") is True)
    print(f"  sampled            : {len(rows)}")
    print(f"  with common groups : {withg}")
    print(f"  show_ask_spam=True : {spam}   <- would be excluded")
    return {"rows": rows, "with_common_groups": withg, "ask_spam": spam}


async def _send_one_test(client, to_phone: str):
    """Send EXACTLY ONE message, with the fixed test text, to one number."""
    import rubika_client as rb
    print_header("SINGLE TEST SEND")
    norm = rb.normalize_phone(to_phone)
    print(f"  target : {norm}")
    print(f"  text   : {TEST_MESSAGE!r}")
    guid = None
    try:
        res = _d(await client.add_address_book(norm, "Test"))
        guid = (res.get("user") or {}).get("user_guid")
    except Exception as e:
        print(f"  add_address_book failed ({repr(e)[:70]}); trying contacts ...")
    if not guid:
        try:
            for u in await _fetch_raw(client, "contacts"):
                if rb.normalize_phone(str(u.get("phone") or "")) == norm:
                    guid = u.get("user_guid")
                    break
        except Exception:
            pass
    if not guid:
        print("  ✗ could not resolve a guid for that number — nothing sent")
        return
    try:
        await rb.send_text(client, guid, TEST_MESSAGE)
        print(f"  ✓ sent to {guid}")
    except Exception as e:
        print(f"  ✗ send failed: {repr(e)[:160]}")


# =========================================================================== #
# OFFLINE SELFTEST  (fixtures mirror the documented API responses)
# =========================================================================== #
def _fx():
    now = int(time.time())
    users = [
        # at the phone right now (exact, 20s ago)
        {"user_guid": "u_online", "first_name": "Online Now", "phone": "9891",
         "last_online": now - 20, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 20}},
        # wrote to us (two-way), seen 3h ago
        {"user_guid": "u_replied", "first_name": "Replied", "phone": "9892",
         "last_online": now - 10800, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 10800}},
        # read our messages, seen 2 days ago
        {"user_guid": "u_read", "first_name": "ReadUs", "phone": "9893",
         "last_online": now - 172800, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 172800}},
        # chat exists, never read it
        {"user_guid": "u_oneway", "first_name": "OneWay", "phone": "9894",
         "last_online": now - 600, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 600}},
        # no chat, exact presence 5 min ago
        {"user_guid": "u_exact", "first_name": "ExactNoChat", "phone": "9895",
         "last_online": now - 300, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 300}},
        # privacy-masked "Recently" with a rounded timestamp
        {"user_guid": "u_masked", "first_name": "MaskedRecent", "phone": "9896",
         "last_online": (now // 1800) * 1800, "is_deleted": False,
         "online_time": {"type": "Approximate", "approximate_period": "Recently"}},
        # BUG-2 pair: IDENTICAL rounded timestamp, opposite privacy buckets.
        # Fixed (not clock-derived) so the demonstration is deterministic.
        {"user_guid": "u_mask_recent", "first_name": "MaskRecently",
         "phone": "9811", "last_online": 1723235400, "is_deleted": False,
         "online_time": {"type": "Approximate", "approximate_period": "Recently"}},
        {"user_guid": "u_mask_old", "first_name": "MaskLongAgo",
         "phone": "9812", "last_online": 1723235400, "is_deleted": False,
         "online_time": {"type": "Approximate",
                         "approximate_period": "LongTimeAgo"}},
        # REAL bucket spellings observed on a live account (543 contacts):
        # Recently / WeekAgo / MonthAgo / LongAgo
        {"user_guid": "u_weekago", "first_name": "RealWeekAgo", "phone": "9813",
         "last_online": 1723235400, "is_deleted": False,
         "online_time": {"type": "Approximate", "approximate_period": "WeekAgo"}},
        {"user_guid": "u_monthago", "first_name": "RealMonthAgo", "phone": "9814",
         "last_online": 1723235400, "is_deleted": False,
         "online_time": {"type": "Approximate", "approximate_period": "MonthAgo"}},
        {"user_guid": "u_longago", "first_name": "RealLongAgo", "phone": "9815",
         "last_online": 1723235400, "is_deleted": False,
         "online_time": {"type": "Approximate", "approximate_period": "LongAgo"}},
        # spam-risk: Rubika warns the peer we are a stranger
        {"user_guid": "u_spam", "first_name": "StrangerSpam", "phone": "9897",
         "last_online": now - 30, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 30}},
        # deleted + blocked
        {"user_guid": "u_del", "first_name": "Deleted", "phone": "9898",
         "last_online": now - 60, "is_deleted": True,
         "online_time": {"type": "Exact", "exact_time": now - 60}},
        {"user_guid": "u_blocked", "first_name": "Blocked", "phone": "9899",
         "last_online": now - 60, "is_deleted": False,
         "online_time": {"type": "Exact", "exact_time": now - 60}},
    ]
    U = lambda g, **kw: dict(  # noqa: E731
        {"object_guid": g, "abs_object": {"type": "User"}, "access": ["SendMessages"],
         "last_seen_my_mid": "100", "last_seen_peer_mid": "0", "count_unseen": 0,
         "is_blocked": False, "time": 1000}, **kw)
    chats = [
        U("u_replied", last_message={"is_mine": False}, last_seen_peer_mid="100",
          time=5000),
        U("u_read", last_seen_peer_mid="100", last_message={"is_mine": True},
          time=4000),
        U("u_oneway", last_seen_peer_mid="0", last_message={"is_mine": True},
          time=9000),
        U("u_spam", show_ask_spam=True, last_message={"is_mine": True}),
        U("u_blocked", is_blocked=True, last_message={"is_mine": True}),
        U("u_online", last_seen_peer_mid="0", last_message={"is_mine": True},
          time=1),
        {"object_guid": "g_group", "abs_object": {"type": "Group"},
         "access": ["SendMessages"], "time": 9999},
    ]
    return users, chats


def selftest() -> int:
    users, chats = _fx()
    npass = nfail = 0

    def ck(name, cond):
        nonlocal npass, nfail
        if cond:
            npass += 1
            print(f"  ✅ {name}")
        else:
            nfail += 1
            print(f"  ❌ {name}")

    print_header("1. BUG 1 — 'online' never matches in the current code")
    legacy = legacy_order(users, chats)
    ck("no contact carries a 'status' field",
       not any("status" in u for u in users))
    ck("legacy puts an existing chat ahead of the ONLINE contact (BUG 3)",
       legacy.index("u_oneway") < legacy.index("u_online"))
    # BUG 2, proven deterministically: the two masked contacts share a rounded
    # timestamp, so the legacy sort cannot tell "Recently" from "LongTimeAgo"
    # and leaves them adjacent in input order — the bucket has zero effect.
    ck("legacy ignores approximate_period entirely (BUG 2)",
       abs(legacy.index("u_mask_recent") - legacy.index("u_mask_old")) == 1)
    ck("legacy sorts masked purely on the rounded ts (BUG 2)",
       _legacy_lastonline(users, "u_mask_recent")
       == _legacy_lastonline(users, "u_mask_old"))
    ck("legacy keeps the spam-risk stranger in the list",
       "u_spam" in legacy)
    ck("legacy keeps the deleted account", "u_del" in legacy)

    print_header("2. presence_of() decodes online_time correctly")
    ck("exact 20s ago -> online", presence_of(users[0])["online"] is True)
    ck("exact 3h ago -> not online", presence_of(users[1])["online"] is False)
    ck("exact flagged exact", presence_of(users[2])["exact"] is True)
    ck("masked flagged NOT exact", presence_of(users[5])["exact"] is False)
    ck("masked ts is dropped (not trusted)", presence_of(users[5])["ts"] == 0)
    ck("masked bucket decoded", presence_of(users[5])["bucket"] == "recently")
    ck("literal Approximate/Online counts as online",
       presence_of({"online_time": {"type": "Approximate",
                                    "approximate_period": "Online"}})["online"] is True)
    ck("missing online_time -> not exact, not online",
       presence_of({"last_online": 123}) == {"exact": False, "ts": 123,
                                             "bucket": "", "online": False})

    print_header("3. chat_signals() reads the two-way signals")
    s = {c["object_guid"]: chat_signals(c) for c in chats if "object_guid" in c}
    ck("they_wrote detected", s["u_replied"]["they_wrote"] is True)
    ck("peer_read_us detected", s["u_read"]["peer_read_us"] is True)
    ck("peer_read_us False when mid == 0", s["u_oneway"]["peer_read_us"] is False)
    ck("ask_spam detected", s["u_spam"]["ask_spam"] is True)
    ck("blocked detected", s["u_blocked"]["blocked"] is True)
    ck("group typed as group", chat_signals(chats[-1])["type"] == "group")

    print_header("4. tiers + exclusions")
    ordered, rep = build_order(users, chats, "hybrid")
    tier = {r["guid"]: r["tier"] for r in ordered}
    ck("online -> ONLINE_NOW", tier.get("u_online") == "ONLINE_NOW")
    ck("they wrote -> REPLIED", tier.get("u_replied") == "REPLIED")
    ck("they read -> READ_US", tier.get("u_read") == "READ_US")
    ck("chat, never read -> CHAT_ONE_WAY", tier.get("u_oneway") == "CHAT_ONE_WAY")
    ck("no chat + exact -> EXACT_PRESENCE", tier.get("u_exact") == "EXACT_PRESENCE")
    ck("masked -> APPROX_RECENTLY", tier.get("u_masked") == "APPROX_RECENTLY")
    ck("spam-risk excluded", "u_spam" not in tier)
    ck("deleted excluded", "u_del" not in tier)
    ck("blocked excluded", "u_blocked" not in tier)
    ck("group never enters the list", "g_group" not in tier)
    ck("3 excluded", rep["excluded"] == 3)
    ck("exclusion reasons reported", len(rep["excluded_reasons"]) == 3)

    print_header("5. ordering per strategy")
    g = [r["guid"] for r in ordered]
    ck("hybrid: ONLINE first", g[0] == "u_online")
    ck("hybrid: two-way before one-way",
       g.index("u_replied") < g.index("u_oneway")
       and g.index("u_read") < g.index("u_oneway"))
    ck("hybrid: real presence before masked",
       g.index("u_exact") < g.index("u_masked"))
    ck("hybrid: FIXES bug 3 (online before chat-only)",
       g.index("u_online") < g.index("u_oneway"))
    ck("hybrid: FIXES bug 2 (exact before masked)",
       g.index("u_exact") < g.index("u_masked"))
    # the fix for BUG 2: identical rounded ts, but the bucket now decides
    ck("hybrid: FIXES bug 2 ('Recently' beats 'LongTimeAgo')",
       g.index("u_mask_recent") < g.index("u_mask_old"))
    ck("hybrid: 'LongTimeAgo' lands in COLD",
       tier.get("u_mask_old") == "COLD")
    # regression: the REAL Rubika spellings must map to their own tiers, not COLD
    ck("real 'WeekAgo' -> APPROX_WEEK (not COLD)",
       tier.get("u_weekago") == "APPROX_WEEK")
    ck("real 'MonthAgo' -> APPROX_MONTH (not COLD)",
       tier.get("u_monthago") == "APPROX_MONTH")
    ck("real 'LongAgo' -> COLD", tier.get("u_longago") == "COLD")
    ck("WeekAgo ranks above MonthAgo",
       g.index("u_weekago") < g.index("u_monthago"))
    ck("MonthAgo ranks above LongAgo",
       g.index("u_monthago") < g.index("u_longago"))

    gm = [r["guid"] for r in build_order(users, chats, "mutual")[0]]
    ck("mutual: two-way ahead of the online stranger",
       gm.index("u_replied") < gm.index("u_online"))
    ck("mutual: REPLIED is first", gm[0] == "u_replied")

    gp = [r["guid"] for r in build_order(users, chats, "presence")[0]]
    ck("presence: online first", gp[0] == "u_online")
    ck("presence: exact presence outranks two-way",
       gp.index("u_exact") < gp.index("u_replied"))

    print_header("6. report counters")
    ck("counts contacts", rep["contacts"] == 14)
    ck("sendable = 11", rep["sendable"] == 11)
    ck("mutual counted", rep["mutual"] == 2)
    ck("one_way counted", rep["one_way"] == 9)
    # only the sendable online contact counts: the online-but-excluded ones
    # (spam-risk / deleted / blocked) must NOT inflate this number.
    ck("online_now counts sendable only", rep["online_now"] == 1)
    ck("exact presence counted", rep["exact_presence"] == 5)
    ck("masked presence counted", rep["masked_presence"] == 6)
    ck("include_excluded returns everyone",
       len(build_order(users, chats, "hybrid", include_excluded=True)[0]) == 14)

    print_header("7. response-shape extraction (the false-zero bug)")
    # getTopChatUsers really answers with `abs_users` + object_guid. Guessing
    # key names gave a false "0 users" twice, so this must be self-discovering.
    real_top = {"abs_users": [
        {"object_guid": "u_a", "type": "User", "first_name": "مریم",
         "last_name": "اسدی"},
        {"object_guid": "u_b", "type": "User", "first_name": "امیر"}]}
    items, key = _items(real_top, "top_chat_users")
    ck("finds the real 'abs_users' key", key == "abs_users" and len(items) == 2)
    ck("reads object_guid", [_guid(x) for x in items] == ["u_a", "u_b"])
    ck("builds a display name", _name_of_user(items[0]) == "مریم اسدی")
    ck("preferred key wins when present",
       _items({"top_chat_users": [{"user_guid": "z"}],
               "abs_users": [{"object_guid": "y"}]}, "top_chat_users")[1]
       == "top_chat_users")
    disc, dkey = _items({"some_unknown_field": [{"object_guid": "u_x"}]})
    ck("self-discovers an UNKNOWN key (no more false zeros)",
       len(disc) == 1 and "auto-detected" in dkey)
    ck("empty response -> ('none')", _items({}) == ([], "none"))
    ck("ignores non-dict lists", _items({"ids": [1, 2, 3]})[1] == "none")
    ck("guid falls back to user_guid", _guid({"user_guid": "u_q"}) == "u_q")

    print_header("8. robustness")
    ck("empty input is safe", build_order([], [])[0] == [])
    ck("users with no guid are skipped",
       build_order([{"first_name": "x"}], [])[0] == [])
    ck("garbage online_time is safe",
       presence_of({"online_time": "nope", "last_online": "x"})["ts"] == 0)
    # with no chat data at all, only the deleted account is excluded (blocked /
    # ask_spam are chat-derived and therefore unknown) -> 14 - 1 = 13
    ck("None chat list is safe", len(build_order(users, None)[0]) == 13)
    ck("unknown strategy falls back to hybrid",
       [r["guid"] for r in build_order(users, chats, "zzz")[0]] == g)

    print(f"\n  ==== {npass} passed, {nfail} failed ====\n")
    return 1 if nfail else 0


# =========================================================================== #
# output helpers
# =========================================================================== #
def print_verdict(audit, rep, legacy, ordered):
    """The two answers, on real data, in one screen."""
    n = audit["contacts_seen"]
    has_status = audit["contact_has_status_field"]
    masked = rep["masked_presence"]
    tiers = rep["tiers"]
    replied = tiers.get("REPLIED", 0)
    read_us = tiers.get("READ_US", 0)
    spam = rep["excluded_reasons"].get("show_ask_spam (stranger / spam risk)", 0)

    byg = {r["guid"]: r for r in ordered}
    first_on = next((i + 1 for i, g in enumerate(legacy)
                     if byg.get(g, {}).get("online")), None)

    print_header("VERDICT — on YOUR real data")
    print("  Q1: is the bot's online-first logic correct?")
    if has_status == 0:
        print(f"      ✗ BROKEN. 0 of {n} contacts carry a 'status' field,")
        print("        which is what the bot's _is_online() tests. So the")
        print("        'online first' tier NEVER fires on this account.")
    else:
        print(f"      ? {has_status} of {n} contacts DO carry 'status'"
              " — unexpected, inspect contact_keys above.")
    print(f"      → with the fixed logic, {rep['online_now']} contact(s) are"
          " online right now.")
    if first_on:
        print(f"      → the first online contact sits at position #{first_on}"
              " in the bot's")
        print("        current order, vs #1 in the proposed order.")
    pct = round(100.0 * masked / max(1, rep["sendable"]), 1)
    print(f"      → {masked} ({pct}%) hide their last-seen, so the bot's"
          " last_online")
    print("        sort is unreliable for them (30-min rounded buckets).")

    print("\n  Q2: do two-way (mutual) contacts exist, and can we split them?")
    if replied + read_us:
        print(f"      ✓ YES — {replied + read_us} two-way contacts found:")
        print(f"          {replied} wrote to you, {read_us} read your messages")
        print(f"      → {rep['one_way']} are one-way (never engaged).")
        print("      → Rubika has NO mutual flag, but these come free from the")
        print("        getChats call the panel already makes.")
    else:
        print("      ✗ none detected on this account (no chat history yet).")
    if spam:
        print(f"      ⚠ {spam} contact(s) carry show_ask_spam=True — Rubika is")
        print("        warning them you're a stranger. The bot messages these")
        print("        today; the proposed list excludes them.")


def print_header(t):
    print("\n" + "-" * 68)
    print(f"  {t}")
    print("-" * 68)


def print_report(rep):
    print_header(f"CLASSIFICATION  (strategy={rep['strategy']})")
    print(f"  contacts        : {rep['contacts']}")
    print(f"  sendable        : {rep['sendable']}")
    print(f"  excluded        : {rep['excluded']}  {rep['excluded_reasons']}")
    print(f"  two-way (mutual): {rep['mutual']}")
    print(f"  one-way         : {rep['one_way']}")
    print(f"  online NOW      : {rep['online_now']}")
    print(f"  exact presence  : {rep['exact_presence']}")
    print(f"  masked presence : {rep['masked_presence']}")
    print("  tiers:")
    for t in TIERS:
        if rep["tiers"].get(t):
            print(f"      {t:<16} {rep['tiers'][t]}")


def print_diff(legacy, proposed, recs):
    print_header("CURRENT BOT ORDER  vs  PROPOSED")
    pos = {g: i for i, g in enumerate(proposed)}
    dropped = [g for g in legacy if g not in pos]
    print(f"  legacy list   : {len(legacy)}")
    print(f"  proposed list : {len(proposed)}")
    print(f"  removed by the spam/validity filter : {len(dropped)}")
    moved = sum(1 for i, g in enumerate(legacy) if g in pos and pos[g] != i)
    print(f"  contacts whose position changes     : {moved}")
    byg = {r["guid"]: r for r in recs}
    first_on = next((i for i, g in enumerate(legacy)
                     if byg.get(g, {}).get("online")), None)
    if first_on is not None:
        print(f"  first ONLINE contact sits at legacy position #{first_on + 1}"
              f" -> proposed #1")


def list_accounts() -> int:
    """Print the panel's accounts so you can copy a real phone number.
    READ-ONLY: a single SELECT, no connection to Rubika."""
    try:
        import db
        import worker
    except Exception as e:
        print(f"  could not load the panel modules: {repr(e)[:160]}")
        print("  run this from the repo root (e.g. cd /opt/rubika-panel)")
        return 1
    try:
        rows = db.list_accounts()
    except Exception as e:
        print(f"  could not read the database: {repr(e)[:160]}")
        return 1
    if not rows:
        print("\n  no accounts in this panel yet.\n")
        return 0
    print_header(f"ACCOUNTS ({len(rows)})")
    print(f"  {'id':>4}  {'phone':<16} {'status':<12} worker")
    for a in rows:
        try:
            w = worker.worker_for_account(a) or {}
            wtag = w.get("tag") or "-"
            if w and worker.is_local(w):
                wtag += " (local)"
        except Exception:
            wtag = "?"
        print(f"  {a['id']:>4}  {str(a.get('phone') or '-'):<16} "
              f"{str(a.get('status') or '-'):<12} {wtag}")
    first = str(rows[0].get("phone") or "")
    print(f"\n  then run:   --probe {first}")
    print(f"  with test:  --probe {first} --send-test 989370024307\n")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Research lab for Rubika recipient ordering (standalone).")
    ap.add_argument("--selftest", action="store_true",
                    help="run the offline logic tests (no network/login)")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="list this panel's accounts (id / phone / status)")
    ap.add_argument("--probe", metavar="PHONE",
                    help="READ-ONLY probe of a real account")
    ap.add_argument("--verify", metavar="PHONE",
                    help="prove or disprove the two-way count: getChats "
                         "pagination audit + getChatInfo ground truth")
    ap.add_argument("--sample", type=int, default=20, metavar="N",
                    help="how many 'no-chat' contacts --verify inspects")
    ap.add_argument("--mutual", metavar="PHONE",
                    help="READ-ONLY deep probe for a precise mutual signal "
                         "(getTopChatUsers / getContactsLastOnline / "
                         "getPrivacySetting / getBlockedUsers + correlation test)")
    ap.add_argument("--strategy", default="hybrid", choices=sorted(STRATEGIES),
                    help="ordering strategy (default: hybrid)")
    ap.add_argument("--deep", type=int, default=0, metavar="N",
                    help="also getChatInfo for the first N contacts (slow)")
    ap.add_argument("--show", type=int, default=25, metavar="N",
                    help="how many of the proposed list to print")
    ap.add_argument("--json", dest="out_json", metavar="FILE",
                    help="write the full result as JSON")
    ap.add_argument("--send-test", metavar="PHONE",
                    help=f"send ONE message ({TEST_MESSAGE!r}) to this number")
    a = ap.parse_args()

    if a.selftest:
        raise SystemExit(selftest())
    if a.do_list:
        raise SystemExit(list_accounts())
    if a.verify:
        print("  ⚠️  read-only. Run while that account has no active send.")
        asyncio.run(verify_probe(a.verify, a.sample, a.out_json))
        raise SystemExit(0)
    if a.mutual:
        print("  ⚠️  read-only. Run while that account has no active send.")
        asyncio.run(mutual_probe(a.mutual, a.show, a.out_json))
        raise SystemExit(0)
    if not a.probe:
        ap.print_help()
        print("\n  tip: start with   --selftest   (offline, proves the logic)")
        print("       then           --list       (shows your account numbers)")
        raise SystemExit(0)
    if a.send_test:
        print("\n  ⚠️  --send-test will send ONE real message.")
        print(f"      text: {TEST_MESSAGE!r}")
    print("  ⚠️  run this only while that account has NO active send/automation")
    print("      (one live connection per session).")
    asyncio.run(probe(a.probe, a.strategy, a.deep, a.out_json, a.show, a.send_test))


if __name__ == "__main__":
    main()
