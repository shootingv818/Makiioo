"""
🔁 Automation Engine — executor primitives (run on master AND worker).
======================================================================

The engine BRAIN lives on the master (autoeng.py). These are the small,
one-shot actions it drives, each opening exactly ONE short connection through
account_conn so that:

  * a join returns the real group GUID (fixes the old remote "joined"-string
    gap where remote groups were never registered);
  * every failure is classified into an engine code and reported back, so the
    master — not a silent worker loop — decides to drop/replace a group
    (fixes the old remote loops that swallowed errors with `except: pass`);
  * the PV once-per-person ledger stays on the master (workers keep no state),
    so a worker restart never re-answers everyone;
  * the per-account lock is held only for one action, never a whole pass, so a
    PV reply can interleave between group sends instead of waiting minutes.

Imports stay worker-safe: account_conn + rubika_client + autoeng_core only —
NO bot / telethon / db import. Served by the additive worker_api endpoints
(/autoeng/*) and called directly for local (master-held) accounts.
"""
from __future__ import annotations

import asyncio

import account_conn
import rubika_client as rb
import autoeng_core as core

PREVIEW_TIMEOUT = 60
JOIN_TIMEOUT = 90
SEND_TIMEOUT = 60
PV_TIMEOUT = 90


# --------------------------------------------------------------------------- #
# Tolerant readers over a group-preview / group-info response. The exact field
# names differ across rubpy builds, so we read defensively — the FIRST real run
# against a live account (the owner's planned test) confirms which keys appear.
# --------------------------------------------------------------------------- #
def _dig(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _extract_health(raw) -> dict:
    """Pull {guid, name, members, approval} from a preview response, tolerant of
    nesting. `approval` True means joining needs admin approval (join-request).
    `members`/`approval` may be None when the build does not expose them."""
    d = rb._data_of(raw) or {}
    grp = d.get("group") if isinstance(d.get("group"), dict) else d
    guid = rb._guid_of(raw) or _dig(grp, "group_guid", "object_guid")
    name = _dig(grp, "group_title", "title", "name") or rb._name_of(raw)
    members = _dig(grp, "count_members", "member_count", "members_count")
    # join-approval flag: different builds spell it differently.
    approval = _dig(grp, "join_type", "group_join_type")
    appr = None
    if isinstance(approval, str):
        appr = approval.strip().lower() in ("request", "approval", "admin")
    reqflag = _dig(grp, "is_join_request", "request_to_join", "need_approval")
    if reqflag is not None:
        appr = bool(reqflag) or bool(appr)
    try:
        members = int(members) if members is not None else None
    except Exception:  # noqa: BLE001
        members = None
    return {"guid": guid, "name": name or "-", "members": members,
            "approval": appr}


async def _preview_raw(client, link: str):
    """Call rubpy's join-link preview WITHOUT joining. Tolerant of method name /
    signature diffs (same style as rubika_client's own wrappers)."""
    hash_part = (link or "").rstrip("/").split("/")[-1]
    for name in ("group_preview_by_join_link", "get_group_info_by_link",
                 "group_preview", "get_join_link_info"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            return await rb._try_call(fn, [
                lambda a=link: ((a,), {}),
                lambda a=hash_part: ((a,), {}),
                lambda a=link: ((), {"link": a}),
                lambda a=hash_part: ((), {"hash": a}),
            ])
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    return None


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
async def preview_link(phone: str, link: str) -> dict:
    """Check a group link WITHOUT joining. Returns:
      {ok, guid, name, members, approval, error?, code?}
    `ok` False with a code when the link cannot be resolved."""
    pn = rb.normalize_phone(phone)

    async def _do(client):
        raw = await asyncio.wait_for(_preview_raw(client, link), timeout=PREVIEW_TIMEOUT)
        if raw is None:
            return None
        return _extract_health(raw)

    try:
        info = await account_conn.call(pn, _do, timeout=PREVIEW_TIMEOUT + 15)
        if not info or not info.get("guid"):
            return {"ok": False, "code": core.LNK_BAD,
                    "error": "link did not resolve to a group"}
        return {"ok": True, **info}
    except account_conn.InvalidAuthError:
        return {"ok": False, "code": core.ACC_DEAD, "error": "session invalid"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": core.classify_error(e, context="scan"),
                "error": repr(e)[:160]}


async def join_link(phone: str, link: str) -> dict:
    """Join a group via link. Returns {ok, guid, code?, error?}. The GUID is
    always returned on success so the master can register the group."""
    pn = rb.normalize_phone(phone)

    async def _do(client):
        res = await asyncio.wait_for(rb.join_group_by_link(client, link),
                                     timeout=JOIN_TIMEOUT)
        return rb.join_result_group_guid(res)

    try:
        guid = await account_conn.call(pn, _do, timeout=JOIN_TIMEOUT + 15)
        if not guid:
            return {"ok": False, "code": core.LNK_BAD,
                    "error": "joined but no group guid returned"}
        return {"ok": True, "guid": guid}
    except account_conn.InvalidAuthError:
        return {"ok": False, "code": core.ACC_DEAD, "error": "session invalid"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": core.classify_error(e, context="join"),
                "error": repr(e)[:160]}


async def send_to(phone: str, guid: str, text: str) -> dict:
    """Send ONE text to ONE group. Returns {ok, code?, error?}."""
    pn = rb.normalize_phone(phone)

    async def _do(client):
        await asyncio.wait_for(rb.send_text(client, guid, text), timeout=SEND_TIMEOUT)
        return True

    try:
        await account_conn.call(pn, _do, timeout=SEND_TIMEOUT + 15)
        return {"ok": True}
    except account_conn.InvalidAuthError:
        return {"ok": False, "code": core.ACC_DEAD, "error": "session invalid"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": core.classify_error(e, context="send"),
                "error": repr(e)[:160]}


async def leave(phone: str, guid: str) -> dict:
    pn = rb.normalize_phone(phone)

    async def _do(client):
        await asyncio.wait_for(rb.leave_group(client, guid), timeout=SEND_TIMEOUT)
        return True

    try:
        await account_conn.call(pn, _do, timeout=SEND_TIMEOUT + 15)
        return {"ok": True}
    except account_conn.InvalidAuthError:
        return {"ok": False, "code": core.ACC_DEAD, "error": "session invalid"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": core.classify_error(e, context="send"),
                "error": repr(e)[:160]}


async def fetch_pv(phone: str, state=None) -> dict:
    """Return NEW private-chat senders since `state`. The master applies the
    once-per-person ledger and decides the reply, so this is pure read:
      {ok, new: [{guid, name}], state, code?, error?}
    The first call (state falsy) only primes the cursor and returns no senders,
    so pre-existing chats are never answered."""
    pn = rb.normalize_phone(phone)

    async def _do(client):
        res = await asyncio.wait_for(rb.get_chats_updates(client, state),
                                     timeout=PV_TIMEOUT)
        chats, new_state = rb.parse_chats_updates(res)
        self_guid = None
        try:
            self_guid = await rb.get_self_guid(client)
        except Exception:  # noqa: BLE001
            self_guid = None
        out = []
        for ch in chats or []:
            if rb.chat_type(ch) != "user":
                continue
            g = rb.chat_object_guid(ch)
            if not g or g == self_guid:
                continue
            # only chats whose newest message is from the OTHER side (someone
            # messaged us) — never our own outgoing.
            lm = rb.chat_last_message(ch)
            author = rb.message_author_guid(lm) if lm else None
            if author and self_guid and author == self_guid:
                continue
            out.append({"guid": g, "name": rb._name_of(ch)})
        return {"new": out, "state": new_state}

    try:
        primed = bool(state)
        res = await account_conn.call(pn, _do, timeout=PV_TIMEOUT + 15)
        if not primed:
            # first pass just establishes the cursor.
            return {"ok": True, "new": [], "state": res.get("state")}
        return {"ok": True, "new": res.get("new", []), "state": res.get("state")}
    except account_conn.InvalidAuthError:
        return {"ok": False, "code": core.ACC_DEAD, "error": "session invalid",
                "new": [], "state": state}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": core.classify_error(e, context="pv"),
                "error": repr(e)[:160], "new": [], "state": state}
