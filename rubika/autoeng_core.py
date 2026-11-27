"""
🔁 Automation Engine — PURE logic core (NO network, NO DB, NO rubpy).
=====================================================================

This module holds the decision logic of the new multi-account automation
engine so it can be unit-tested in isolation (scripts/autoeng_lab.py
--selftest). Everything here is a plain function over plain data; the async
orchestration that CALLS these lives in autoeng.py.

Design rules honoured (project_notes/CORE_RULES.md & SESSION_HANDOFF.md):
  * No new logic is invented where the project already proved a pattern — the
    role split, the round-robin and the error buckets mirror what bot.py /
    account_conn already do; this module only makes them explicit and testable.
  * core/, config.py, rubika_client.py, features.py are NOT imported here.
"""
from __future__ import annotations

import math

# --------------------------------------------------------------------------- #
# Roles.  min 4 accounts; scanners = floor(40%) capped at 4; the rest send.
# Agreed table:  4→1  5→2  6→2  7→2  8→3  9→3  10+→4
# --------------------------------------------------------------------------- #
MIN_ACCOUNTS = 4
SCANNER_CAP = 4
SCANNER_RATIO = 0.4

ROLE_SCANNER = "scanner"
ROLE_SENDER = "sender"


def scanner_count(n_accounts: int) -> int:
    """How many of `n_accounts` become scanners. 0 if below the minimum."""
    if n_accounts < MIN_ACCOUNTS:
        return 0
    return max(1, min(SCANNER_CAP, math.floor(n_accounts * SCANNER_RATIO)))


def assign_roles(account_ids: list) -> dict:
    """Return {account_id: role}. The FIRST `scanner_count` accounts (order as
    given by the owner's selection) scan; the rest send. Returns {} below the
    minimum so the caller refuses to start."""
    ids = [int(a) for a in account_ids]
    if len(ids) < MIN_ACCOUNTS:
        return {}
    k = scanner_count(len(ids))
    roles = {}
    for i, aid in enumerate(ids):
        roles[aid] = ROLE_SCANNER if i < k else ROLE_SENDER
    return roles


def split_roles(roles: dict) -> tuple:
    """(scanner_ids, sender_ids) preserving insertion order."""
    scanners = [a for a, r in roles.items() if r == ROLE_SCANNER]
    senders = [a for a, r in roles.items() if r == ROLE_SENDER]
    return scanners, senders


# --------------------------------------------------------------------------- #
# Link distribution to scanners — round-robin, skipping scanners that died.
# A dead scanner's not-yet-checked links are handed to the next live scanner
# (this is the "اگر اکانت وسط چک مرد، سهمش به نفر بعدی می‌رسه" rule).
# --------------------------------------------------------------------------- #
def distribute_links(links: list, scanner_ids: list, dead: set = None) -> dict:
    """Return {scanner_id: [links...]} splitting `links` round-robin over the
    LIVE scanners (those not in `dead`). Empty dict if no live scanner."""
    dead = set(dead or ())
    live = [s for s in scanner_ids if s not in dead]
    out = {s: [] for s in live}
    if not live:
        return {}
    for i, lk in enumerate(links):
        out[live[i % len(live)]].append(lk)
    return out


def reassign_dead_scanner(pending_by_scanner: dict, dead_id, scanner_ids: list,
                          dead: set) -> dict:
    """A scanner died mid-run: move its still-pending links to the remaining
    live scanners, round-robin. Returns the updated {scanner_id: [links]}."""
    leftover = list(pending_by_scanner.get(dead_id, []))
    new_map = {s: list(v) for s, v in pending_by_scanner.items() if s != dead_id}
    live = [s for s in scanner_ids if s not in dead and s != dead_id]
    if not live or not leftover:
        new_map.pop(dead_id, None)
        return new_map
    for i, lk in enumerate(leftover):
        new_map.setdefault(live[i % len(live)], []).append(lk)
    return new_map


# --------------------------------------------------------------------------- #
# Group hand-off to senders ("tنوری داغ" — assign each healthy group the moment
# it is found to the sender with the most free slots, capped at groups_per).
# --------------------------------------------------------------------------- #
def assign_group(current_counts: dict, sender_ids: list, groups_per: int):
    """Pick the sender for ONE newly-healthy group: the live sender with the
    fewest assigned groups that is still below `groups_per`. Returns the
    sender_id, or None when every sender is full. `current_counts` maps
    sender_id -> number of groups already assigned."""
    best = None
    best_n = None
    for sid in sender_ids:
        n = current_counts.get(sid, 0)
        if n >= groups_per:
            continue
        if best_n is None or n < best_n:
            best, best_n = sid, n
    return best


def capacity(sender_ids: list, groups_per: int) -> int:
    """Total groups the current sender set can hold."""
    return len(sender_ids) * max(0, int(groups_per))


def reserve_is_full(unused_healthy: int, threshold: int = 30) -> bool:
    """Scanners rest once this many unused healthy groups are banked."""
    return unused_healthy >= threshold


# --------------------------------------------------------------------------- #
# Error classification.  Codes: ACC / GRP / LNK / WRK / NET / SYS.
# Mirrors account_conn.is_auth_error for the auth tokens (passed in as
# `auth_tokens` so this module stays import-free and testable).
# --------------------------------------------------------------------------- #
AUTH_TOKENS = ("INVALID_AUTH", "INVALIDAUTH", "NOT_REGISTERED", "AUTH_FROM_ANOTHER")
RATE_TOKENS = ("TOO_REQUESTS", "TOO_MANY", "FLOOD", "RATE", "TRY_AGAIN",
               "SLOW_DOWN")
GROUP_TOKENS = ("ACCESS_DENIED", "NOT_IN_GROUP", "NO_ACCESS", "FORBIDDEN",
                "MUTE", "BANNED", "KICKED", "NOT_ALLOWED", "SENDING_IS_DISABLED",
                "DISABLED", "MEMBER_REMOVED")
APPROVAL_TOKENS = ("JOIN_REQUEST", "PENDING", "NEED_APPROVAL", "REQUEST_SENT",
                   "APPROVE")
LINK_TOKENS = ("INVALID_LINK", "LINK", "NOT_EXISTS", "NOT_FOUND", "WRONG_LINK",
               "GROUP_NOT_EXISTS", "INVALID_INPUT")
NET_TOKENS = ("TIMEOUT", "TIMED OUT", "NETWORK", "CONNECTION", "CONNECT",
              "TEMPORAR", "SERVER", "TRANSPORT", "SSL", "EOF")
WORKER_TOKENS = ("WORKER", "502", "503", "504", "BEARER", "UNAUTHORIZED",
                 "ASGI", "HTTPX", "CONNECTERROR")

# engine-specific codes
ACC_DEAD = "ACC_SESSION"      # session invalid -> quarantine
ACC_RESTRICTED = "ACC_RESTRICTED"   # Rubika showed a user-facing alert: the
#                                     account is TEMPORARILY restricted, not
#                                     dead. Cool down and retry; never
#                                     quarantine on this alone.
ACC_LIMIT = "ACC_LIMIT"       # rate limited -> cool down, NOT a group fault
GRP_BLOCKED = "GRP_BLOCKED"   # muted / banned / cannot send here
GRP_APPROVAL = "GRP_APPROVAL"  # join needs admin approval -> skip
GRP_GONE = "GRP_GONE"         # group deleted / not a member anymore
LNK_BAD = "LNK_BAD"           # invalid / expired link
WRK_DOWN = "WRK_DOWN"         # worker unreachable / old
NET_ERR = "NET_ERR"           # transient network
SYS_ERR = "SYS_ERR"           # internal engine fault


def classify_error(err, *, context: str = "send") -> str:
    """Map an exception (or its string) to an engine error code. `context` is
    one of 'scan' | 'join' | 'send' | 'pv' | 'worker' and only nudges a few
    ambiguous buckets. This is deliberately ordered: auth, then rate, then the
    context-specific buckets, then transport, then catch-all."""
    text = (str(err) or "").upper()
    if any(t in text for t in AUTH_TOKENS):
        return ACC_DEAD
    if any(t in text for t in RATE_TOKENS):
        return ACC_LIMIT
    if any(t in text for t in WORKER_TOKENS):
        return WRK_DOWN
    if context == "worker":
        # a failed worker RPC (incl. a timeout/connection error reaching it)
        # means the worker is the problem, not the account.
        return WRK_DOWN
    if context in ("join", "scan"):
        if any(t in text for t in APPROVAL_TOKENS):
            return GRP_APPROVAL
        if any(t in text for t in LINK_TOKENS):
            return LNK_BAD
    if any(t in text for t in APPROVAL_TOKENS):
        return GRP_APPROVAL
    if any(t in text for t in GROUP_TOKENS):
        return GRP_BLOCKED
    if context == "send" and ("NOT_EXISTS" in text or "NOT_FOUND" in text):
        return GRP_GONE
    if any(t in text for t in LINK_TOKENS):
        return LNK_BAD if context in ("join", "scan") else GRP_GONE
    if any(t in text for t in NET_TOKENS):
        return NET_ERR
    return SYS_ERR


def alert_message(err) -> str:
    """Rubika's own user-facing text, when it ships one.

    A real sample from production:
      InvalidAuth({'status':'ERROR_GENERIC','status_det':'INVALID_AUTH',
                   'client_show_message':{'link':{'type':'alert',
                   'alert_data':{'message':'کاربر گرامی، ...'}}}})

    When this is present the account is being RESTRICTED (Rubika wants to show
    the user a message), not logged out — so it must not be treated as a dead
    session. Returns "" when there is no such message.
    """
    text = str(err) or ""
    if "client_show_message" not in text:
        return ""
    # pull out the 'message': '...' value without eval'ing anything
    key = "'message':"
    i = text.find(key)
    if i < 0:
        key = '"message":'
        i = text.find(key)
    if i < 0:
        return "restricted (no message text)"
    rest = text[i + len(key):].lstrip()
    if not rest:
        return "restricted"
    quote = rest[0]
    if quote not in ("'", '"'):
        return "restricted"
    out = []
    for ch in rest[1:]:
        if ch == quote:
            break
        out.append(ch)
    return "".join(out).strip()[:300] or "restricted"


def is_restricted(err) -> bool:
    """True when an auth-looking error is really a temporary restriction."""
    return bool(alert_message(err))


def is_fatal_account(code: str) -> bool:
    """Codes that mean the ACCOUNT, not the group, is the problem."""
    return code in (ACC_DEAD,)


def is_group_fault(code: str) -> bool:
    """Codes that mean this GROUP should be dropped for this account."""
    return code in (GRP_BLOCKED, GRP_GONE, GRP_APPROVAL)


def should_retry_link(code: str) -> bool:
    """A scan/join failure that is NOT the link's fault -> retry elsewhere."""
    return code in (NET_ERR, ACC_LIMIT, WRK_DOWN)
