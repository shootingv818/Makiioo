"""
📎 Caption-file send method — ISOLATED, ADDITIVE module.
========================================================

What the owner asked for (nothing more):
  * In 📌 Content, set ONE file together with its caption (sent in a single
    Telegram message). The bot only STORES it — no upload happens here.
  * "File + Caption" is a SEND METHOD (next to Forward-marker / Plain-text /
    Channel). It is used ONLY when the owner chooses it on an account or when
    creating a channel.
  * When chosen, the file is uploaded into THAT account's Rubika "Saved
    Messages" with the caption (the caption doubles as the forward marker), then
    the EXISTING send engine (run_send / run_send_remote) forwards it to the
    contacts — or, for a channel, it is forwarded into the new channel.
  * If an IP is blocked, the upload is retried on another worker — exactly the
    existing "move to another worker" pattern (pick_worker_for_transfer +
    write-only /session/import). That transfer logic itself is NOT modified.
  * Every attempt is logged to the log group; a total failure reports the
    concrete error and nothing is sent.

Design constraints honoured (project_notes/CORE_RULES.md):
  * core/ (account_conn, db, crypto_util), rubika_client, worker, main, config,
    portal, features, login_code are NOT modified — this module only CALLS them.
  * No new DB table / schema change: the file+caption live in the generic
    app_settings key/value store + one on-disk file next to the database.
  * bot.py gets only thin hooks (a Content button, a send-method option, a
    channel-method option, and one guarded register(...) in amain).
  * worker_api.py gets ONE additive endpoint /capfile/upload.

Session-safety (project_notes/SESSION_AND_CONFLICTS.md):
  * The upload opens exactly ONE connection through account_conn (close->call).
  * It refuses while the account is busy (active send/channel job, or an
    always-on automation that owns the warm connection).
  * A non-home worker gets the session via write-only /session/import (no
    connect -> no AUTH_FROM_ANOTHER); it connects only to run the upload.
  * Read/connect only: a dead session is reported, never auto-deleted.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile

import account_conn
import db
import rubika_client as rb

# NOTE: `worker`, `worker_transfer` and the `bot` panel module are imported
# LAZILY (master-only), so a worker node importing this module pulls in nothing
# but `account_conn` + `db` + `rubika_client`.

# --------------------------------------------------------------------------- #
# Tunables (local on purpose: no new .env keys, no config.py change).
# --------------------------------------------------------------------------- #
UPLOAD_TIMEOUT = 600             # per-attempt upload deadline (seconds)
MAX_UPLOAD_BYTES = 45 * 1024 * 1024
MAX_WORKER_TRIES = 6             # IP-fallback: how many workers to try per upload

# app_settings keys (generic key/value store — no schema change)
K_CAPTION = "capfile_caption"
K_NAME = "capfile_name"
K_HAS = "capfile_has"            # "1" once a file is stored

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(db.__file__)),
                         "data", "caption_file")

_registered = False
_await_set: set = set()          # owner ids currently setting a new file


# --------------------------------------------------------------------------- #
# Config (file + caption, stored once; no upload here)
# --------------------------------------------------------------------------- #
def get_caption() -> str:
    return db.get_setting(K_CAPTION, "") or ""


def get_name() -> str:
    return db.get_setting(K_NAME, "") or ""


def _stored_path(name: str | None = None) -> str:
    name = name if name is not None else get_name()
    ext = os.path.splitext(name or "")[1] or ".bin"
    return os.path.join(_DATA_DIR, "current" + ext)


def read_file_bytes():
    """Return the stored file bytes, or None when no file is set."""
    try:
        path = _stored_path()
        if not os.path.isfile(path) and os.path.isdir(_DATA_DIR):
            for f in os.listdir(_DATA_DIR):            # any current.* fallback
                if f.startswith("current"):
                    path = os.path.join(_DATA_DIR, f)
                    break
        with open(path, "rb") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return None


def has_file() -> bool:
    return str(db.get_setting(K_HAS, "0")) == "1" and read_file_bytes() is not None


def set_file(data: bytes, caption: str = "", name: str = "file") -> None:
    """Persist a new file + caption. Nothing is uploaded here."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    try:
        for f in os.listdir(_DATA_DIR):               # drop old current.*
            if f.startswith("current"):
                try:
                    os.unlink(os.path.join(_DATA_DIR, f))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    name = name or "file"
    with open(_stored_path(name), "wb") as fh:
        fh.write(data)
    db.set_setting(K_NAME, name)
    db.set_setting(K_CAPTION, caption or "")
    db.set_setting(K_HAS, "1")


def clear_file() -> None:
    try:
        for f in os.listdir(_DATA_DIR):
            if f.startswith("current"):
                os.unlink(os.path.join(_DATA_DIR, f))
    except Exception:  # noqa: BLE001
        pass
    db.set_setting(K_HAS, "0")


# --------------------------------------------------------------------------- #
# Upload primitive — tolerant of rubpy build differences (same style as
# rubika_client._try_call). Only a SIGNATURE mismatch (TypeError) falls through
# to the next candidate; a real send failure (e.g. IP block) propagates so the
# caller can move to another worker.
# --------------------------------------------------------------------------- #
async def _upload_document(client, object_guid, data: bytes, caption: str,
                           filename: str):
    # Preserve the EXACT original file name: rubpy derives the document's name
    # from the path basename, so we write the bytes to <tmpdir>/<original name>
    # — nothing is renamed and nothing is appended to the name or the caption.
    name = os.path.basename(filename or "") or "file"
    tmpdir = tempfile.mkdtemp(prefix="capup_")
    path = os.path.join(tmpdir, name)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        cap = caption or None
        last = None
        fn = getattr(client, "send_document", None)
        if fn is not None:
            for make in (
                lambda: ((object_guid, path), {"caption": cap}),
                lambda: ((), {"object_guid": object_guid,
                              "document": path, "caption": cap}),
            ):
                a, k = make()
                try:
                    return await fn(*a, **k)
                except TypeError as e:          # wrong signature -> try next
                    last = e
                    continue
        fn = getattr(client, "send_message", None)
        if fn is not None:
            for make in (
                lambda: ((object_guid,), {"text": cap, "file_inline": path}),
                lambda: ((), {"object_guid": object_guid,
                              "text": cap, "file_inline": path}),
            ):
                a, k = make()
                try:
                    return await fn(*a, **k)
                except TypeError as e:
                    last = e
                    continue
        raise RuntimeError(f"this rubpy build has no file-send method ({last})")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _run_upload(phone: str, data: bytes, caption: str, filename: str) -> dict:
    """Open exactly ONE connection and upload the file to Saved Messages."""
    pn = rb.normalize_phone(phone)
    await account_conn.close(pn)                      # single connection

    async def _do(client):
        guid = await rb.get_self_guid(client)
        await _upload_document(client, guid, data, caption, filename)
        return guid

    try:
        guid = await account_conn.call(pn, _do, timeout=UPLOAD_TIMEOUT)
        return {"ok": True, "guid": guid}
    except account_conn.InvalidAuthError:
        return {"ok": False, "error": "session invalid", "dead": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200]}


# --------------------------------------------------------------------------- #
# WORKER SIDE (served by worker_api.py /capfile/upload — no bot/telegram import)
# --------------------------------------------------------------------------- #
async def worker_upload(phone: str, file_b64: str, caption: str = "",
                        filename: str = "file") -> dict:
    try:
        data = base64.b64decode(file_b64 or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"bad file: {e}"}
    if not data:
        return {"ok": False, "error": "empty file"}
    return await _run_upload(phone, data, caption, filename)


# --------------------------------------------------------------------------- #
# MASTER SIDE
# --------------------------------------------------------------------------- #
def _worker_mod():
    import worker
    return worker


def _worker_transfer_mod():
    import worker_transfer
    return worker_transfer


def _is_busy(bot, account_id: int) -> bool:
    """An upload opens its own connection; never run it while a send/channel job
    or an always-on automation already owns this account's connection."""
    try:
        if int(account_id) in getattr(bot, "active_jobs", set()):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        if bot.continuous_busy(int(account_id)):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _safe_log(bot, text: str) -> None:
    try:
        await bot.log(text)
    except Exception:  # noqa: BLE001
        pass


def _ok_card(bot, acc, w, nbytes):
    return bot.card("📎 CAPTION FILE — Uploaded ✅", [
        f"📱 Account : {acc.get('phone')}",
        f"🖥 Worker  : {(w.get('tag') or 'local') if w else 'local'}",
        f"📦 Size    : {nbytes} bytes",
        "📥 Stored in the account's Saved Messages (ready to forward).",
        f"🕒 {bot.now()}",
    ])


def _fail_card(bot, acc, tried, err):
    return bot.card("📎 CAPTION FILE — Upload FAILED ❌", [
        f"📱 Account : {acc.get('phone')}",
        f"🖥 Workers tried : {len(tried)}",
        f"💥 Last error : {str(err)[:160]}",
        "Nothing was sent.",
        f"🕒 {bot.now()}",
    ])


def _dead_card(bot, acc):
    return bot.card("📎 CAPTION FILE — Session Invalid 🔴", [
        f"📱 Account : {acc.get('phone')}",
        "This account's session is not usable; another IP won't help.",
        "Re-login the account, then try again.",
        f"🕒 {bot.now()}",
    ])


async def upload_for_account(bot, account: dict) -> tuple:
    """Upload the caption-file into one account's Saved Messages, moving to
    another worker on failure (reuses pick_worker_for_transfer). Logs the
    outcome. Returns (ok: bool, detail: str)."""
    aid = int(account["id"])
    phone = account["phone"]
    if _is_busy(bot, aid):
        return False, "account is busy (a send/automation is running)"
    data = read_file_bytes()
    if not data:
        return False, "no file set"
    if len(data) > MAX_UPLOAD_BYTES:
        return False, "file too large"
    caption = get_caption()
    name = get_name() or "file"
    file_b64 = base64.b64encode(data).decode()
    wk = _worker_mod()
    wt = _worker_transfer_mod()
    try:
        home = wk.worker_for_account(account)
    except Exception:  # noqa: BLE001
        home = None

    tried: list = []
    last_err = "no worker available"
    for attempt in range(MAX_WORKER_TRIES):
        if attempt == 0 and home:
            w = home
        else:
            try:
                w = await wt.pick_worker_for_transfer(exclude_ids=list(tried))
            except Exception as e:  # noqa: BLE001
                last_err = f"worker pick failed: {repr(e)[:120]}"
                break
        if not w:
            break
        wid = w.get("id")
        if wid in tried:
            break
        tried.append(wid)
        try:
            if wk.is_local(w):
                res = await _run_upload(phone, data, caption, name)
            else:
                # a non-home worker needs the session written first (write-only).
                if not (home and wid == home.get("id")):
                    sess = db.get_session_blob(aid)
                    if sess and sess.get("auth"):
                        try:
                            await wk.api_call(w, "POST", "/session/import", {
                                "phone": sess.get("phone") or phone,
                                "auth": sess.get("auth"),
                                "private_key": sess.get("private_key"),
                                "guid": sess.get("guid"),
                                "user_agent": sess.get("user_agent"),
                            }, timeout=60)
                        except Exception as e:  # noqa: BLE001
                            last_err = f"session push failed: {repr(e)[:120]}"
                            continue
                res = await wk.api_call(w, "POST", "/capfile/upload", {
                    "phone": phone, "caption": caption,
                    "file_b64": file_b64, "filename": name,
                }, timeout=UPLOAD_TIMEOUT + 60)
            if res and res.get("ok"):
                await _safe_log(bot, _ok_card(bot, account, w, len(data)))
                return True, (w.get("tag") or "local")
            last_err = (res or {}).get("error") or "unknown error"
            if (res or {}).get("dead"):
                await _safe_log(bot, _dead_card(bot, account))
                return False, "session invalid"
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)[:160]

    await _safe_log(bot, _fail_card(bot, account, tried, last_err))
    return False, last_err


async def _launch_local_send(bot, acc: dict, caption: str,
                             hidden: bool = False) -> bool:
    """Prepare + launch a LOCAL send that forwards the pre-uploaded file, by
    reusing bot.run_send unchanged (the caption is the Saved-Messages marker)."""
    phone = acc["phone"]
    aid = int(acc["id"])
    try:
        await account_conn.close(rb.normalize_phone(phone))
        client = rb.open_client(phone)
    except Exception as e:  # noqa: BLE001
        await _safe_log(bot, bot.card("⚠️ FILE SEND — Prepare Error",
                        [f"📱 {phone}", repr(e)[:150], f"🕒 {bot.now()}"]))
        return False
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, caption)
        if not mid:
            await _safe_log(bot, bot.card("⚠️ FILE SEND — Not Found", [
                f"📱 {phone}",
                "The uploaded file was not found in Saved Messages.",
                f"🕒 {bot.now()}"]))
            return False
        ordered, _stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await _safe_log(bot, bot.card("⚠️ FILE SEND — Prepare Error",
                        [f"📱 {phone}", repr(e)[:150], f"🕒 {bot.now()}"]))
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    if not ordered:
        return False
    payload = {
        "account_id": aid, "phone": phone,
        "saved_guid": saved_guid, "mid": mid,
        "recipients": [r["guid"] for r in ordered],
        # the engine must look up THIS caption, not the global marker
        "marker": caption,
        "hide_sender": bool(hidden),
    }
    asyncio.create_task(bot.run_send(bot.config.OWNER_ID, payload))
    return True


async def send_for_account(bot, acc: dict, hidden: bool = False) -> tuple:
    """The 'File + Caption' send method for ONE account: upload the file to its
    Saved Messages (worker/IP fallback) then send it to the contacts using the
    EXISTING send engine. Returns (ok, detail).

    `hidden=True` additionally asks the engine for send method #5, so each
    recipient gets a NEW message instead of a forward (no "Forwarded from"
    line). The lookup text is always THIS file's caption, never the unrelated
    global marker.
    """
    if not has_file():
        return False, "no file set"
    aid = int(acc["id"])
    ok, detail = await upload_for_account(bot, acc)
    if not ok:
        return False, detail                           # fail card already logged
    caption = get_caption()
    wk = _worker_mod()
    try:
        w = wk.worker_for_account(acc)
    except Exception:  # noqa: BLE001
        w = None
    if w and not wk.is_local(w):
        # remote: reuse run_send_remote; the additive payload["marker"] makes the
        # worker forward THIS file's caption, not the global marker.
        payload = {"account_id": aid, "phone": acc["phone"],
                   "worker_id": w["id"], "total": 0,
                   "marker": caption, "mode": "marker",
                   "hide_sender": bool(hidden)}
        asyncio.create_task(bot.run_send_remote(bot.config.OWNER_ID, payload))
        return True, (w.get("tag") or "")
    launched = await _launch_local_send(bot, acc, caption, hidden=hidden)
    if not launched:
        return False, "uploaded, but could not start the local send"
    return True, "local"


# --------------------------------------------------------------------------- #
# Registration (called once from bot.amain, guarded there). Only the SET flow:
# a Content button opens it, and a file message is captured + stored.
# --------------------------------------------------------------------------- #
def register(bot) -> None:
    """Attach the 'set file+caption' callback + the file-capture handler.
    Idempotent. No background work, no auto-upload."""
    global _registered
    if _registered:
        return
    _registered = True
    events, client = bot.events, bot.bot

    @client.on(events.CallbackQuery(data=b"capfile_set"))
    async def set_file_cb(event):
        if not bot.is_owner(event):
            return
        _await_set.add(event.sender_id)
        cur = get_caption()
        await bot.safe_edit(event, bot.card("📎 FILE + CAPTION", [
            f"• Current : {get_name() or '(not set)'}",
            f"• Caption : {(cur[:70] + '…') if len(cur) > 70 else (cur or '(none)')}",
            bot.LINE,
            "Send the FILE together with its caption in ONE message.",
            "It is only stored now; it uploads when you pick",
            "“File + Caption” on an account's Send, or when creating a channel.",
        ]), buttons=[[bot.Button.inline("🔙 Back", b"marker")]])

    @client.on(events.NewMessage())
    async def capture_file(event):
        # Only acts for an owner who just pressed "File + Caption"; otherwise a
        # no-op so it coexists with the panel's message_router.
        if event.sender_id not in _await_set:
            return
        if not bot.is_owner(event):
            return
        if not getattr(event, "message", None) or not getattr(event.message, "media", None):
            await event.respond("Please send a FILE (with its caption).")
            return
        _await_set.discard(event.sender_id)
        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="capin_")
            path = await event.message.download_media(file=tmpdir)
            with open(path, "rb") as fh:
                data = fh.read()
            name = os.path.basename(path) or "file"
            caption = (getattr(event.message, "text", "") or "").strip()
            set_file(data, caption, name)
            await event.respond(bot.card("📎 FILE + CAPTION — Saved ✅", [
                f"• File    : {name} ({len(data)} bytes)",
                f"• Caption : {caption[:80] or '(none)'}",
                "Pick it from an account's 🚀 Send → “File + Caption”.",
                f"🕒 {bot.now()}"]),
                buttons=[[bot.Button.inline("📌 Back to Content", b"marker")]])
        except Exception as e:  # noqa: BLE001
            await event.respond(bot.card("⚠️ FILE + CAPTION — Save Error",
                                [repr(e)[:160], f"🕒 {bot.now()}"]),
                                buttons=[[bot.Button.inline("🔙 Back", b"marker")]])
        finally:
            if tmpdir:
                try:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
