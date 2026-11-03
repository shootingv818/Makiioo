"""
📎 Caption-file auto-upload + file send — ISOLATED, ADDITIVE module.
====================================================================

What the owner asked for (nothing more):
  * In the Connect section, set ONE file together with its caption (sent in a
    single Telegram message to the bot).
  * Every account that logs in gets that file uploaded into its own Rubika
    "Saved Messages" automatically, so it is READY to be forwarded. The caption
    is stored on the uploaded message and doubles as the forward marker.
  * Sometimes Rubika blocks an upload from a given IP. Exactly like the existing
    "move the send to another worker" flow, the upload is retried on another
    worker (its session is pushed write-only first) until one IP succeeds.
  * Success / failure is reported to the log group. If every worker fails, the
    final card shows the concrete error.
  * A "Send File" button forwards the pre-uploaded file from every ready account
    to its contacts by REUSING the existing send engine (bot.run_send /
    bot.run_send_remote) — no new send logic.

Design constraints honoured (project_notes/CORE_RULES.md):
  * `account_conn.py`, `rubika_client.py`, `worker.py`, `db.py`, `main.py` are
    NOT rewritten — this module only CALLS them.
  * No new DB table / schema change: config + per-account upload state live in
    the existing generic `app_settings` key/value store; the file bytes live on
    disk next to the SQLite database.
  * `bot.py` gets only small hooks: the 4-section menu already routes a
    "📎 Caption File" button here, plus one guarded `register(...)` in `amain`.
  * `worker_api.py` gets ONE additive endpoint `/capfile/upload` (Bearer), so an
    old worker simply 404s and the master reports it on the fail card.

Session-safety (project_notes/SESSION_AND_CONFLICTS.md):
  * Every upload opens exactly ONE connection, through `account_conn` (close ->
    call), never two at once on a session.
  * An account that is BUSY (active send/channel job, or an always-on
    automation that owns the warm connection) is SKIPPED and retried later.
  * Pushing a session to another worker uses the write-only `/session/import`
    (no connect -> can't trigger AUTH_FROM_ANOTHER); the real connection happens
    on that worker when it runs the upload.
  * Read/connect only: a dead session is reported, never auto-deleted or
    quarantined here.

Isolation / fault tolerance:
  * The background watcher loop can only end by cancellation; every iteration is
    wrapped so one bad account never stops the loop.
  * Every upload attempt is contained; the per-account state records
    uploaded/error with the signature of the current file so changing the file
    re-queues everyone and a transient error is retried after a cooldown.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time

import account_conn
import db
import rubika_client as rb

# NOTE: `worker`, `worker_transfer` and the `bot` panel module are imported
# LAZILY (master-only), so a worker node importing this module pulls in nothing
# but `account_conn` + `db` + `rubika_client`.

# --------------------------------------------------------------------------- #
# Tunables (kept local on purpose: no new .env keys, no config.py change).
# --------------------------------------------------------------------------- #
WATCH_INTERVAL = 20.0            # seconds between account scans
UPLOAD_TIMEOUT = 600             # per-attempt upload deadline (seconds)
RETRY_COOLDOWN = 300             # re-try an errored account after this long
MAX_UPLOAD_BYTES = 45 * 1024 * 1024
MAX_WORKER_TRIES = 6             # IP-fallback: how many workers to try per upload

# app_settings keys (generic key/value store — no schema change)
K_ENABLED = "capfile_enabled"
K_CAPTION = "capfile_caption"
K_NAME = "capfile_name"
K_SIG = "capfile_sig"
K_STATE = "capfile_state"        # JSON: {account_id: {sig,status,at,ts,error}}

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(db.__file__)),
                         "data", "caption_file")

_registered = False
_watch_task = None
_await_set: set = set()          # owner ids currently setting a new file


# --------------------------------------------------------------------------- #
# Config (master-side; backed by app_settings + one on-disk file)
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    return str(db.get_setting(K_ENABLED, "0")) == "1"


def set_enabled(value: bool) -> None:
    db.set_setting(K_ENABLED, "1" if value else "0")


def get_caption() -> str:
    return db.get_setting(K_CAPTION, "") or ""


def get_name() -> str:
    return db.get_setting(K_NAME, "") or ""


def get_sig() -> str:
    return db.get_setting(K_SIG, "") or ""


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
    return bool(get_sig()) and read_file_bytes() is not None


def _make_sig(data: bytes, caption: str) -> str:
    return (hashlib.sha256(data).hexdigest()[:16] + ":"
            + hashlib.sha256((caption or "").encode()).hexdigest()[:8])


def set_file(data: bytes, caption: str = "", name: str = "file") -> str:
    """Persist a new file + caption and RE-QUEUE every account for upload."""
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
    sig = _make_sig(data, caption)
    db.set_setting(K_NAME, name)
    db.set_setting(K_CAPTION, caption or "")
    db.set_setting(K_SIG, sig)
    db.set_setting(K_ENABLED, "1")
    reset_all_state()                                 # re-upload to everyone
    return sig


def clear_file() -> None:
    set_enabled(False)
    try:
        for f in os.listdir(_DATA_DIR):
            if f.startswith("current"):
                os.unlink(os.path.join(_DATA_DIR, f))
    except Exception:  # noqa: BLE001
        pass
    db.set_setting(K_SIG, "")
    reset_all_state()


# --------------------------------------------------------------------------- #
# Per-account upload state (JSON in app_settings; watcher is the only writer)
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    try:
        raw = db.get_setting(K_STATE, "") or ""
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        db.set_setting(K_STATE, json.dumps(d))
    except Exception:  # noqa: BLE001
        pass


def acc_state(account_id) -> dict:
    return _load_state().get(str(int(account_id)), {})


def set_acc(account_id, **fields) -> None:
    d = _load_state()
    key = str(int(account_id))
    cur = d.get(key, {})
    cur.update(fields)
    d[key] = cur
    _save_state(d)


def reset_all_state() -> None:
    _save_state({})


def counts() -> tuple:
    """(uploaded, errors) for the CURRENT file signature."""
    d = _load_state()
    sig = get_sig()
    up = sum(1 for v in d.values()
             if v.get("status") == "uploaded" and v.get("sig") == sig)
    err = sum(1 for v in d.values()
              if v.get("status") == "error" and v.get("sig") == sig)
    return up, err


# --------------------------------------------------------------------------- #
# Upload primitive — tolerant of rubpy build differences (same style as
# rubika_client._try_call). Only a SIGNATURE mismatch (TypeError) falls through
# to the next candidate; a real send failure (e.g. IP block) propagates so the
# caller can move to another worker.
# --------------------------------------------------------------------------- #
async def _upload_document(client, object_guid, data: bytes, caption: str,
                           filename: str):
    ext = os.path.splitext(filename or "")[1] or ".bin"
    fd, path = tempfile.mkstemp(prefix="capfile_", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as fh:
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
        try:
            os.unlink(path)
        except Exception:  # noqa: BLE001
            pass


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
        "📥 Stored in the account's Saved Messages (ready to send).",
        f"🕒 {bot.now()}",
    ])


def _fail_card(bot, acc, tried, err):
    return bot.card("📎 CAPTION FILE — Upload FAILED ❌", [
        f"📱 Account : {acc.get('phone')}",
        f"🖥 Workers tried : {len(tried)}",
        f"💥 Last error : {str(err)[:160]}",
        "➡️ Will retry automatically after a cooldown.",
        f"🕒 {bot.now()}",
    ])


def _dead_card(bot, acc):
    return bot.card("📎 CAPTION FILE — Session Invalid 🔴", [
        f"📱 Account : {acc.get('phone')}",
        "This account's session is not usable; another IP won't help.",
        "Re-login the account, then press “Re-upload All”.",
        f"🕒 {bot.now()}",
    ])


async def upload_for_account(bot, account: dict) -> tuple:
    """Pre-upload the caption-file into one account's Saved Messages, moving to
    another worker on failure (reuses pick_worker_for_transfer). Logs the
    outcome. Returns (ok: bool, detail: str)."""
    aid = int(account["id"])
    phone = account["phone"]
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


async def _watch_loop(bot) -> None:
    """Background scanner: upload the current file to any active account that
    doesn't have it yet. Never dies on a single bad account."""
    while True:
        try:
            await asyncio.sleep(WATCH_INTERVAL)
            if not is_enabled():
                continue
            sig = get_sig()
            if not sig or read_file_bytes() is None:
                continue
            for acc in db.list_accounts():
                if (acc.get("status") or "") != "active":
                    continue
                aid = int(acc["id"])
                st = acc_state(aid)
                if st.get("sig") == sig and st.get("status") == "uploaded":
                    continue
                if (st.get("sig") == sig and st.get("status") == "error"
                        and (time.time() - float(st.get("ts") or 0)) < RETRY_COOLDOWN):
                    continue
                if _is_busy(bot, aid):
                    continue
                ok, detail = await upload_for_account(bot, acc)
                set_acc(aid, sig=sig,
                        status="uploaded" if ok else "error",
                        at=bot.now(), ts=time.time(),
                        error="" if ok else str(detail)[:200])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await _safe_log(bot, bot.card("⚠️ CAPTION FILE — Watcher Error",
                                          [repr(e)[:160], f"🕒 {bot.now()}"]))
            await asyncio.sleep(WATCH_INTERVAL)


async def _launch_local_send(bot, acc: dict, caption: str) -> bool:
    """Prepare + launch a LOCAL send that forwards the pre-uploaded file, by
    reusing bot.run_send unchanged (the caption is the Saved-Messages marker)."""
    phone = acc["phone"]
    aid = int(acc["id"])
    try:
        await account_conn.close(rb.normalize_phone(phone))
        client = rb.open_client(phone)
    except Exception as e:  # noqa: BLE001
        await _safe_log(bot, bot.card("⚠️ SEND FILE — Prepare Error",
                        [f"📱 {phone}", repr(e)[:150], f"🕒 {bot.now()}"]))
        return False
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, caption)
        if not mid:
            await _safe_log(bot, bot.card("⚠️ SEND FILE — Not Ready", [
                f"📱 {phone}",
                "The caption-file is not in this account's Saved Messages yet.",
                f"🕒 {bot.now()}"]))
            return False
        ordered, _stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await _safe_log(bot, bot.card("⚠️ SEND FILE — Prepare Error",
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
    }
    asyncio.create_task(bot.run_send(bot.config.OWNER_ID, payload))
    return True


async def send_file_all(bot) -> tuple:
    """Forward the pre-uploaded file from EVERY ready account to its contacts,
    reusing the existing send engine. Returns (launched_count, detail)."""
    if not has_file():
        return 0, "no file set"
    caption = get_caption()
    sig = get_sig()
    wk = _worker_mod()
    launched = 0
    for acc in db.list_accounts():
        if (acc.get("status") or "") != "active":
            continue
        aid = int(acc["id"])
        st = acc_state(aid)
        if not (st.get("status") == "uploaded" and st.get("sig") == sig):
            continue
        if _is_busy(bot, aid):
            continue
        try:
            w = wk.worker_for_account(acc)
        except Exception:  # noqa: BLE001
            w = None
        try:
            if w and not wk.is_local(w):
                # remote: reuse run_send_remote; the (additive) payload["marker"]
                # makes the worker forward THIS file's caption, not the global one.
                payload = {"account_id": aid, "phone": acc["phone"],
                           "worker_id": w["id"], "total": 0,
                           "marker": caption, "mode": "marker"}
                asyncio.create_task(bot.run_send_remote(bot.config.OWNER_ID, payload))
                launched += 1
            else:
                if await _launch_local_send(bot, acc, caption):
                    launched += 1
        except Exception as e:  # noqa: BLE001
            await _safe_log(bot, bot.card("⚠️ SEND FILE — Launch Error",
                            [f"📱 {acc.get('phone')}", repr(e)[:150],
                             f"🕒 {bot.now()}"]))
    return launched, ""


# --------------------------------------------------------------------------- #
# Panel rendering
# --------------------------------------------------------------------------- #
def _panel_text(bot) -> str:
    cap = get_caption()
    cap_show = (cap[:70] + "…") if len(cap) > 70 else (cap or "(none)")
    up, err = counts()
    total_active = 0
    try:
        total_active = sum(1 for a in db.list_accounts()
                           if (a.get("status") or "") == "active")
    except Exception:  # noqa: BLE001
        pass
    pending = max(0, total_active - up - err)
    return bot.card("📎 CAPTION FILE", [
        f"• State     : {'ON 🟢' if is_enabled() else 'OFF ⚪️'}",
        f"• File      : {get_name() or '(not set)'}",
        f"• Caption   : {cap_show}",
        bot.LINE,
        f"• Uploaded  : {up}    Pending : {pending}    Errors : {err}",
        "Any logged-in account gets the file in its Saved Messages",
        "automatically (auto-fallback to another worker if an IP is blocked).",
    ])


def _panel_buttons(bot):
    B = bot.Button
    toggle = "Turn OFF" if is_enabled() else "Turn ON"
    return [
        [B.inline("📁 Set File + Caption", b"capfile_set")],
        [B.inline(f"🔁 {toggle}", b"capfile_toggle"),
         B.inline("♻️ Re-upload All", b"capfile_reupload")],
        [B.inline("📎 Send File Now", b"capfile_send")],
        [B.inline("🔙 Back", b"sec_connect")],
    ]


# --------------------------------------------------------------------------- #
# Registration (called once from bot.amain, guarded there).
# --------------------------------------------------------------------------- #
def register(bot) -> None:
    """Attach the panel callbacks + the file-capture handler and start the
    background upload watcher. Idempotent."""
    global _registered, _watch_task
    if _registered:
        return
    _registered = True
    events, client = bot.events, bot.bot

    try:
        _watch_task = asyncio.create_task(_watch_loop(bot), name="capfile:watch")
    except Exception:  # noqa: BLE001
        _watch_task = None

    @client.on(events.CallbackQuery(data=b"capfile"))
    async def open_panel(event):
        if not bot.is_owner(event):
            return
        _await_set.discard(event.sender_id)
        await bot.safe_edit(event, _panel_text(bot), buttons=_panel_buttons(bot))

    @client.on(events.CallbackQuery(data=b"capfile_set"))
    async def set_file_cb(event):
        if not bot.is_owner(event):
            return
        _await_set.add(event.sender_id)
        await bot.safe_edit(event, bot.card("📎 CAPTION FILE — Set", [
            "Send the FILE together with its caption in ONE message.",
            "The caption becomes the forward marker; the file is uploaded to",
            "every logged-in account's Saved Messages automatically.",
            bot.LINE,
            "Waiting for your file …",
        ]), buttons=[[bot.Button.inline("🔙 Cancel", b"capfile")]])

    @client.on(events.CallbackQuery(data=b"capfile_toggle"))
    async def toggle_cb(event):
        if not bot.is_owner(event):
            return
        set_enabled(not is_enabled())
        await bot.safe_edit(event, _panel_text(bot), buttons=_panel_buttons(bot))

    @client.on(events.CallbackQuery(data=b"capfile_reupload"))
    async def reupload_cb(event):
        if not bot.is_owner(event):
            return
        if not has_file():
            await event.answer("No file is set yet.", alert=True)
            return
        reset_all_state()
        await event.answer("Queued re-upload to all accounts.")
        await bot.safe_edit(event, _panel_text(bot), buttons=_panel_buttons(bot))

    @client.on(events.CallbackQuery(data=b"capfile_send"))
    async def send_cb(event):
        if not bot.is_owner(event):
            return
        if not has_file():
            await event.answer("No file is set yet.", alert=True)
            return
        up, _err = counts()
        if up <= 0:
            await event.answer("No account has the file ready yet — wait for auto-upload.",
                               alert=True)
            return
        await bot.safe_edit(event, bot.card("📎 SEND FILE", [
            f"Launching send from {up} ready account(s) …",
            f"🕒 {bot.now()}"]),
            buttons=[[bot.Button.inline("🔙 Back", b"capfile")]])
        launched, _d = await send_file_all(bot)
        await bot.safe_edit(event, bot.card("📎 SEND FILE — Launched", [
            f"• Sends started : {launched}",
            "Each account posts its own progress cards in the log group.",
            f"🕒 {bot.now()}"]),
            buttons=[[bot.Button.inline("🔙 Back", b"capfile")]])

    @client.on(events.NewMessage())
    async def capture_file(event):
        # Only acts for an owner who just pressed "Set File"; otherwise it is a
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
            await event.respond(bot.card("📎 CAPTION FILE — Saved ✅", [
                f"• File     : {name} ({len(data)} bytes)",
                f"• Caption  : {caption[:80] or '(none)'}",
                "• Auto-upload to all logged-in accounts : queued",
                f"🕒 {bot.now()}"]), buttons=_panel_buttons(bot))
        except Exception as e:  # noqa: BLE001
            await event.respond(bot.card("⚠️ CAPTION FILE — Save Error",
                                [repr(e)[:160], f"🕒 {bot.now()}"]),
                                buttons=[[bot.Button.inline("🔙 Back", b"capfile")]])
        finally:
            if tmpdir:
                try:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
