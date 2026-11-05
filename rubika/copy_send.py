"""
🫥 Hidden-sender send — ISOLATED, ADDITIVE module.
==================================================

What this solves
----------------
Sending with `forwardMessages` stamps a "Forwarded from …" line on the message,
so a recipient can see it was mass-forwarded. The Rubika app offers
"پنهان کردن نام ارسال کننده" in its forward dialog, and no Rubika library
(`rubpy`, `pyrubi`, `RubikaLib`) exposes any parameter for it — all three send
only `from_object_guid`, `to_object_guid`, `message_ids`, `rnd`.

The app achieves it by NOT forwarding: it sends the content as a brand-new
message, so no forward header is ever created. That is also how it is done on
Telegram (copy instead of forward). This module does the same:

    1. read the marked Saved-Messages post ONCE and keep its `file_inline`
       (a reference to the media already uploaded on Rubika's servers)
    2. for each recipient, `send_message(guid, text=caption, file_inline=…)`

The media is NOT re-uploaded — the same server-side file reference is reused —
so the send keeps its normal pace.

How it is turned on
-------------------
Through exactly ONE send method: "🫥 File + Caption, HIDDEN" in an account's
🚀 Send menu. That route uploads the owner's stored file into the account's own
Saved Messages first (`caption_file.send_for_account(hidden=True)`, with the
existing worker/IP fallback) and then sends it with this module, so the content
is guaranteed to exist on the sending account before the run starts.

There is no global switch: the behaviour is driven only by `hide_sender` in the
send payload, so every other method — forward marker, plain text, file+caption,
channel — is completely unaffected.

Safety
------
* `rubika_client.py` is pinned and only CALLED here, never modified; its
  `forward_message` stays in place and is still used whenever the switch is off
  or the marked post cannot be read.
* If the source post cannot be read, the caller FALLS BACK to forwarding, so a
  send can never be blocked by this module.
* Channel creation still forwards (a channel post has no "personal message" to
  disguise), and the pool/brain/worker-transfer control flow is untouched.
"""
from __future__ import annotations

import rubika_client as rb

async def read_marked_full(client, marker: str):
    """Find the marked post in Saved Messages and return the WHOLE thing.

    `rb.find_marked_message` only returns (guid, message_id), which is enough
    to forward but not to re-send: we need the media reference and the caption.
    Returns {guid, mid, text, file_inline} or None when not found.
    Deliberately mirrors the paging of rb.find_marked_message so behaviour
    stays identical to the forward path.
    """
    saved_guid = await rb.get_self_guid(client)
    max_id = None
    for _ in range(50):
        try:
            if max_id:
                result = await client.get_messages(saved_guid, max_id, "20")
            else:
                result = await client.get_messages(saved_guid, "0", "20")
        except Exception:  # noqa: BLE001
            break
        messages = getattr(result, "messages", None)
        if messages is None and isinstance(result, dict):
            messages = result.get("messages", [])
        if not messages:
            break
        for msg in messages:
            if marker in rb._msg_text_of(msg):
                d = rb._data_of(msg)
                return {
                    "guid": saved_guid,
                    "mid": rb._msg_id_of(msg),
                    "text": rb._msg_text_of(msg),
                    "file_inline": d.get("file_inline"),
                    "sticker": d.get("sticker"),
                }
        max_id = rb._msg_id_of(messages[-1])
        if not max_id:
            break
    return None


def has_media(src: dict) -> bool:
    return bool((src or {}).get("file_inline"))


async def send_copy(client, to_guid: str, src: dict):
    """Send the marked content as a NEW message — no forward attribution.

    Tolerant of rubpy signature differences in the same spirit as
    rubika_client._try_call: only a TypeError (wrong signature) moves on to the
    next form; a real send failure propagates so the caller counts it as a
    failed recipient exactly like a failed forward.
    """
    text = (src or {}).get("text") or None
    fi = (src or {}).get("file_inline")
    if not fi:
        # text-only marked post: a plain message is already attribution-free
        return await rb.send_text(client, to_guid, text or "")

    fn = getattr(client, "send_message", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no send_message()")
    last = None
    for make in (
        lambda: ((to_guid,), {"text": text, "file_inline": fi}),
        lambda: ((), {"object_guid": to_guid, "text": text, "file_inline": fi}),
        lambda: ((to_guid, text), {"file_inline": fi}),
    ):
        args, kwargs = make()
        try:
            return await fn(*args, **kwargs)
        except TypeError as e:          # wrong signature for this build
            last = e
            continue
    raise RuntimeError(f"send_message rejected every known signature ({last})")


async def read_source(client, marker: str):
    """Read the source once for a whole send run.

    Returns (src, reason). `src` is None when hidden sending is not possible,
    and `reason` says why so the caller can log it and fall back to forwarding.
    """
    try:
        src = await read_marked_full(client, marker)
    except Exception as e:  # noqa: BLE001
        return None, f"read failed: {type(e).__name__}"
    if not src:
        return None, "marked post not found"
    return src, "ok"
