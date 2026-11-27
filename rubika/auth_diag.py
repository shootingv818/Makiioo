"""
🩺 Auth diagnostics — split the THREE auth failures the project already knows.
==============================================================================

`account_conn.is_auth_error()` (pinned) already recognises exactly four tokens:
    INVALID_AUTH · INVALIDAUTH · NOT_REGISTERED · AUTH_FROM_ANOTHER
but it returns a single bool, so every one of them surfaces as one opaque
"session invalid". They mean three COMPLETELY different things and need
different handling — that is all this module adds. The token list is READ from
account_conn so the two can never drift apart.

  AUTH_FROM_ANOTHER -> one session used from TWO places at once (the project's
                       own "golden rule"). The account is fine; we connected
                       from the wrong server. Fix the placement, not the login.
  INVALID_AUTH      -> this session was invalidated elsewhere (a login from
                       another device/IP kicked it). A re-login is required;
                       rotating the IP will NOT help.
  NOT_REGISTERED    -> the account itself is gone/banned. Nothing helps.

Also records the one piece of context the old cards never had: WHERE the
operation ran (server tag + IP) and what the engine did about it.
"""
from __future__ import annotations

import account_conn

# Codes (prefix-compatible with autoeng_core's ACC_* family)
ACC_CONFLICT = "ACC_CONFLICT"      # AUTH_FROM_ANOTHER
ACC_INVALID = "ACC_INVALID"        # INVALID_AUTH
ACC_GONE = "ACC_GONE"              # NOT_REGISTERED
ACC_AUTH_OTHER = "ACC_SESSION"     # auth-looking but unrecognised

LINE = "-------------------------------"

MEANING = {
    ACC_CONFLICT: "one session used from two places at once",
    ACC_INVALID: "this session was invalidated elsewhere (login from another device)",
    ACC_GONE: "the account no longer exists / is banned",
    ACC_AUTH_OTHER: "auth failure of an unrecognised kind",
}

ADVICE = {
    ACC_CONFLICT: "run this account ONLY where its session lives — do not "
                  "connect it from a second server",
    ACC_INVALID: "re-login this account; changing the IP will not help",
    ACC_GONE: "remove the account; nothing can recover it",
    ACC_AUTH_OTHER: "collect the raw body (curiosity mode) and investigate",
}

# rotating the server is pointless for ALL auth failures: the session, not the
# IP, is the problem. (Learned the hard way — see FUTURE_UPDATES.)
ROTATION_HELPS = False


def _text_of(err) -> str:
    return (str(err) or "").upper()


def is_auth(err) -> bool:
    """Delegate to the pinned classifier so behaviour can never diverge."""
    try:
        return bool(account_conn.is_auth_error(err))
    except Exception:  # noqa: BLE001
        t = _text_of(err)
        return any(k in t for k in ("INVALID_AUTH", "INVALIDAUTH",
                                    "NOT_REGISTERED", "AUTH_FROM_ANOTHER"))


def classify_auth(err) -> str:
    """Return the specific auth code, or "" when it is not an auth error.
    Order matters: AUTH_FROM_ANOTHER contains no other token, but a body can
    mention several, so the most specific/actionable is checked first."""
    if not is_auth(err):
        return ""
    t = _text_of(err)
    if "AUTH_FROM_ANOTHER" in t:
        return ACC_CONFLICT
    if "NOT_REGISTERED" in t:
        return ACC_GONE
    if "INVALID_AUTH" in t or "INVALIDAUTH" in t:
        return ACC_INVALID
    return ACC_AUTH_OTHER


def rotation_helps(code: str) -> bool:
    """Should the send engine rotate the server for this code? Never, for auth."""
    return False if code in MEANING else False


def diag_card(*, code: str, stage: str, phone: str, worker_tag: str,
              worker_ip: str, session_home: str = "", raw: str = "",
              action: str = "", clock: str = "") -> str:
    """The richer error card: adds WHERE it ran, where the session belongs,
    the plain-language meaning and what to do."""
    rows = [
        "| ⚠ - #auth_diag",
        LINE,
        f"--| {code or 'ACC_SESSION'}",
        f"• Stage    : {stage}",
        f"• Account  : {phone}",
        f"• Ran on   : {worker_tag or 'local'}"
        + (f" · {worker_ip}" if worker_ip else ""),
    ]
    if session_home:
        rows.append(f"• Session  : belongs to {session_home}")
    rows += [
        f"• Means    : {MEANING.get(code, 'unknown')}",
        f"• Do       : {ADVICE.get(code, 'investigate')}",
        f"• Rotation : {'would help' if rotation_helps(code) else 'pointless (session, not IP)'}",
    ]
    if action:
        rows.append(f"• Action   : {action}")
    if raw:
        rows.append(f"• Raw      : {str(raw)[:200]}")
    rows += [LINE, f"🕒 {clock}"]
    return "\n".join(rows)


def placement_mismatch(worker_tag: str, session_home: str) -> bool:
    """True when we are about to run an account somewhere other than where its
    session lives — the exact cause of AUTH_FROM_ANOTHER."""
    a = (worker_tag or "local").strip().lower()
    b = (session_home or "").strip().lower()
    return bool(b) and a != b
