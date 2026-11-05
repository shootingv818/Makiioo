"""
🫥 Forward-sender privacy — ISOLATED, ADDITIVE module.
======================================================

Purpose
-------
A forwarded message carries a "Forwarded from …" attribution, so a recipient
can tell at a glance that the message was mass-forwarded rather than written
to them. Rubika has the same account-level privacy switch Telegram does, and
it showed up in the owner's own `getPrivacySetting` response:

    "link_forward_message": "Everybody"

Setting it to "Nobody" is the supported way to stop a forward of our own
message from pointing back at us.

Why this design
---------------
This is an ACCOUNT SETTING, not a change to how we send. Nothing in
`rb.forward_message`, the marker path, brain, channel mode, pool or
worker-transfer is touched, which is exactly what the owner asked for: if the
result is not what we want, the same switch puts it back.

⚠️ UNVERIFIED EXPECTATION
-------------------------
In Telegram the equivalent setting makes the sender name *unlinked* rather
than removing the forward header outright. Whether Rubika drops the header
completely is NOT something this module can promise — it only reports what
the API accepted and what reading the setting back now returns. Judge the
result by looking at a real received message; `project_notes/FUTURE_UPDATES.md`
records the fallback (sending a copy instead of forwarding) if this is not
enough.

Safety
------
* Read-only by default: `read()` only calls getPrivacySetting.
* Every write is verified by READING THE SETTING BACK — the API's own reply is
  not treated as proof.
* The previous value is recorded in app_settings before the first change, so
  the original state is always recoverable.
* `rubika_client.py`, `account_conn.py`, `db.py`, `worker.py` and `portal/`
  are only called, never modified.
"""
from __future__ import annotations

import db

# The privacy key Rubika uses, and the two values it accepts for it.
KEY = "link_forward_message"
HIDDEN = "Nobody"
VISIBLE = "Everybody"

# app_settings key holding the pre-change value per account (no schema change).
_PREV_PREFIX = "fwdpriv_prev:"


def _prev_key(account_id) -> str:
    return f"{_PREV_PREFIX}{int(account_id)}"


def remember_previous(account_id, value: str) -> None:
    """Record the value seen BEFORE our first change, so it can be restored."""
    if not value:
        return
    if not db.get_setting(_prev_key(account_id), ""):
        db.set_setting(_prev_key(account_id), value)


def previous_value(account_id) -> str:
    return db.get_setting(_prev_key(account_id), "") or ""


def _settings_of(resp) -> dict:
    """Pull the privacy dict out of a getPrivacySetting reply.

    Rubika nests it under `privacy_setting`; accept a couple of shapes and
    fall back to the top level so an unexpected wrapper cannot make this
    silently report nothing.
    """
    d = resp if isinstance(resp, dict) else {}
    for key in ("privacy_setting", "privacy_settings", "settings"):
        v = d.get(key)
        if isinstance(v, dict):
            return v
    return d


async def read(client) -> dict:
    """Return {value, raw}. `value` is "" when the key is absent."""
    resp = await client.builder(name="getPrivacySetting", input={}, dict=True)
    s = _settings_of(resp)
    return {"value": str(s.get(KEY) or ""), "raw": s}


async def apply(client, hidden: bool, recheck_after: float = 2.0) -> dict:
    """Set the switch and VERIFY by reading it back — TWICE.

    Returns {ok, before, want, after, settled, error}. `ok` is True only when
    the value still reads as requested on a SECOND read a couple of seconds
    later. The immediate read-back is not enough: a server that accepts the
    write and then reverts it would otherwise be reported as success, which is
    exactly the failure mode observed on a live account (an apply reported
    Everybody → Nobody, and a later panel read showed Everybody again).
    """
    import asyncio

    want = HIDDEN if hidden else VISIBLE
    out = {"ok": False, "before": "", "want": want, "after": "",
           "settled": None, "error": ""}
    try:
        out["before"] = (await read(client))["value"]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"read failed: {type(e).__name__}: {str(e)[:120]}"
        return out
    try:
        await client.builder(name="setSetting", input={
            "settings": {KEY: want},
            "update_parameters": [KEY],
        }, dict=True)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"write failed: {type(e).__name__}: {str(e)[:120]}"
        # still read back: the write may have applied before the error
    try:
        out["after"] = (await read(client))["value"]
    except Exception as e:  # noqa: BLE001
        out["error"] = out["error"] or f"verify failed: {type(e).__name__}"
        return out
    if out["after"] != want:
        out["ok"] = False
        out["error"] = out["error"] or f"setting still reads {out['after'] or '?'}"
        return out

    # Took effect on the first read — now make sure it PERSISTS.
    if recheck_after and recheck_after > 0:
        try:
            await asyncio.sleep(recheck_after)
            out["settled"] = (await read(client))["value"]
        except Exception as e:  # noqa: BLE001
            out["error"] = f"recheck failed: {type(e).__name__}"
            out["ok"] = False
            return out
        if out["settled"] != want:
            out["ok"] = False
            out["error"] = (f"did NOT stick — reverted to "
                            f"{out['settled'] or '?'} after {recheck_after:g}s")
            return out
    out["ok"] = True
    out["error"] = ""
    return out


def is_hidden(value: str) -> bool:
    return str(value or "").strip().lower() == HIDDEN.lower()


def state_label(value: str) -> str:
    if not value:
        return "UNKNOWN"
    return "HIDDEN 🫥" if is_hidden(value) else f"VISIBLE ({value})"
