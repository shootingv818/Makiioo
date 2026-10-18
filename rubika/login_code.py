"""
📩 Login-code mirror — ISOLATED, ADDITIVE module.
=================================================

Purpose (exactly what the owner asked for, nothing more):
  The owner taps "📩 Login Code" on an account in the accounts panel. The bot
  answers "ready — now request the code from the Rubika app", then mirrors every
  INCOMING TEXT message that arrives on that account into the log group, so the
  owner can read the Rubika login code and sign in manually.
  It stops on the "⏹ Stop" button, or automatically after TTL_SECONDS (300).

Design constraints honoured (project_notes/CORE_RULES.md):
  * `account_conn.py`, `rubika_client.py`, `worker.py`, `db.py`, `main.py` are
    NOT touched — this module only CALLS them.
  * No new DB table, no schema change: the mirror is short-lived (300 s) so its
    state is in-memory on purpose (same choice as brain / channel-brain).
  * `bot.py` gets only two tiny hooks (one button + one guarded `register`).
  * `worker_api.py` gets three additive endpoints (`/logincode/*`) that mirror
    the existing `/secretary/*` shape, so old workers simply 404 and the master
    reports that clearly instead of failing silently.

Session-safety (project_notes/SESSION_AND_CONFLICTS.md):
  * Reading ALWAYS goes through `account_conn.call()`, i.e. the account's own
    lock — never a raw `rb.open_client()`. One live connection per session.
  * Each poll is a SHORT call and the sleep happens OUTSIDE the lock, so the
    mirror never starves a send/automation of that account.
  * The mirror refuses to start while that account already has a running send
    job (`bot.active_jobs`), because a send opens its own raw client.
  * Nothing here ever changes an account's status or deletes anything.

Isolation: every failure path is contained. The runner can only end by
stop / timeout / confirmed-invalid session / repeated errors, and it ALWAYS
cleans its registry entry in `finally`, so a broken mirror cannot leave an
account marked busy or block a second attempt.
"""
from __future__ import annotations

import asyncio
import time

import account_conn
import rubika_client as rb

# NOTE: `worker` (master-side orchestration) is imported LAZILY inside the
# master-only helpers, so a worker node importing this module pulls in nothing
# but `account_conn` + `rubika_client`.

# --------------------------------------------------------------------------- #
# Tunables (kept local on purpose: no new .env keys, no config.py change).
# --------------------------------------------------------------------------- #
TTL_SECONDS = 300          # hard auto-close window, as requested
POLL_INTERVAL = 4.0        # seconds between polls (master and worker side)
RECENT_LIMIT = 5           # newest messages read per updated chat
SEEN_CAP = 400             # bounded dedup ledger (per mirror run)
MAX_POLL_ERRORS = 3        # consecutive transient errors before giving up
CALL_TIMEOUT = 60          # per-poll timeout on the account connection

# Chat kinds we never mirror (a mass-sending account would flood the log group).
_SKIP_CHAT_TYPES = ("group", "channel")

# master side: account_id -> state ; worker side: normalized phone -> state
_jobs: dict = {}
_worker_jobs: dict = {}
_registered = False


# --------------------------------------------------------------------------- #
# Shared polling core (used by BOTH the master-local path and the worker path).
# --------------------------------------------------------------------------- #
def new_state() -> dict:
    """Fresh mirror state. `primed` guarantees we only report messages that
    arrive AFTER the button was pressed (get_chats_updates otherwise replays
    roughly the last 200 seconds)."""
    return {
        "stop": False,
        "primed": False,
        "state": "",          # get_chats_updates cursor
        "self_guid": None,
        "seen": set(),
        "seen_order": [],
        "count": 0,
        "reason": "",
        "started_at": time.monotonic(),
    }


def _msg_id(msg):
    data = rb._data_of(msg)
    return data.get("message_id") or data.get("id")


def _msg_text(msg) -> str:
    """TEXT ONLY (the owner asked for text; a login code is always text)."""
    data = rb._data_of(msg)
    value = data.get("text")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _is_new(st: dict, key: str) -> bool:
    seen = st["seen"]
    if key in seen:
        return False
    seen.add(key)
    order = st["seen_order"]
    order.append(key)
    if len(order) > SEEN_CAP:
        drop = order[: len(order) - SEEN_CAP]
        del order[: len(order) - SEEN_CAP]
        for old in drop:
            seen.discard(old)
    return True


async def poll_client(client, st: dict) -> list:
    """ONE pass on an already-open account client.

    Returns a list of `{"chat", "from", "text"}` dicts for INCOMING TEXT
    messages that appeared since the previous pass. Read-only: it never sends,
    marks, or modifies anything on the account.
    """
    if not st.get("self_guid"):
        st["self_guid"] = await asyncio.wait_for(rb.get_self_guid(client), timeout=30)
    self_guid = st.get("self_guid")

    result = await asyncio.wait_for(
        rb.get_chats_updates(client, st.get("state") or ""), timeout=45)
    chats, new_state = rb.parse_chats_updates(result)
    if new_state:
        st["state"] = str(new_state)

    if not st.get("primed"):
        # First pass only sets the cursor — no history dump into the log group.
        st["primed"] = True
        return []

    out = []
    for chat in chats or []:
        if st.get("stop"):
            break
        if rb.chat_type(chat) in _SKIP_CHAT_TYPES:
            continue
        guid = rb.chat_object_guid(chat)
        if not guid:
            continue
        try:
            messages = await asyncio.wait_for(
                rb.get_recent_messages(client, guid, RECENT_LIMIT), timeout=45)
        except Exception:  # noqa: BLE001
            continue
        for msg in reversed(list(messages or [])):        # oldest -> newest
            mid = _msg_id(msg)
            if mid is None:
                continue
            if not _is_new(st, f"{guid}:{mid}"):
                continue
            author = rb.message_author_guid(msg)
            if self_guid and author and author == self_guid:
                continue                                  # our own outgoing
            text = _msg_text(msg)
            if not text:
                continue                                  # text only
            name = rb._name_of(chat, default="") or guid
            out.append({"chat": guid, "from": str(name), "text": text})
    return out


# --------------------------------------------------------------------------- #
# WORKER SIDE (imported by worker_api.py — no Telegram / bot import here).
# --------------------------------------------------------------------------- #
async def worker_start(phone: str, ttl: float = TTL_SECONDS) -> dict:
    """Start (or restart) the mirror for one account on THIS worker."""
    await worker_stop(phone)
    st = new_state()
    st["queue"] = []
    st["ttl"] = max(10.0, float(ttl or TTL_SECONDS))
    st["error"] = ""
    key = rb.normalize_phone(phone)
    _worker_jobs[key] = st
    st["task"] = asyncio.create_task(_worker_loop(phone, st))
    return {"ok": True, "ttl": st["ttl"]}


async def worker_stop(phone: str) -> dict:
    st = _worker_jobs.pop(rb.normalize_phone(phone), None)
    if not st:
        return {"ok": True, "running": False}
    st["stop"] = True
    task = st.get("task")
    if task:
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:  # noqa: BLE001
            task.cancel()
    return {"ok": True, "running": False, "count": st.get("count", 0)}


def worker_drain(phone: str) -> dict:
    """Return + CLEAR the messages queued for the master."""
    st = _worker_jobs.get(rb.normalize_phone(phone))
    if not st:
        return {"running": False, "messages": [], "count": 0,
                "ttl_left": 0, "error": "", "reason": "not running"}
    messages = st.get("queue") or []
    st["queue"] = []
    left = max(0.0, float(st.get("ttl", TTL_SECONDS))
               - (time.monotonic() - st["started_at"]))
    return {
        "running": not st.get("stop"),
        "messages": messages,
        "count": int(st.get("count", 0)),
        "ttl_left": int(left),
        "error": st.get("error", ""),
        "reason": st.get("reason", ""),
    }


async def _worker_loop(phone: str, st: dict) -> None:
    """Worker-side mirror loop: SHORT locked poll, sleep outside the lock,
    self-closing after its own TTL so a dead master can never leave it on."""
    errors = 0
    try:
        while not st.get("stop"):
            if (time.monotonic() - st["started_at"]) >= float(st.get("ttl", TTL_SECONDS)):
                st["reason"] = "timeout"
                break
            try:
                found = await account_conn.call(phone, poll_client, st,
                                                timeout=CALL_TIMEOUT)
                errors = 0
            except account_conn.InvalidAuthError:
                st["error"] = "session invalid"
                st["reason"] = "session invalid"
                break
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors >= MAX_POLL_ERRORS:
                    st["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                    st["reason"] = "poll error"
                    break
                found = []
            for item in found or []:
                st["count"] = int(st.get("count", 0)) + 1
                st.setdefault("queue", []).append(item)
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        st["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        st["reason"] = "loop error"
    finally:
        st["stop"] = True


# --------------------------------------------------------------------------- #
# MASTER SIDE
# --------------------------------------------------------------------------- #
def is_running(account_id: int) -> bool:
    st = _jobs.get(int(account_id))
    return bool(st and not st.get("stop"))


def _worker_mod():
    """Lazy import of the master-side worker orchestration module."""
    import worker
    return worker


def _worker_of(acc: dict):
    """Return the remote worker dict for this account, or None when local."""
    try:
        wk = _worker_mod()
        w = wk.worker_for_account(acc)
    except Exception:  # noqa: BLE001
        return None
    return w if (w and not wk.is_local(w)) else None


def _busy_reason(bot, account_id: int) -> str:
    """A send opens its own raw client; running the mirror at the same time
    would put two live connections on one session. Refuse instead."""
    try:
        if int(account_id) in getattr(bot, "active_jobs", set()):
            return "این اکانت الان یک ارسال فعال دارد؛ بعد از پایان ارسال دوباره امتحان کن."
    except Exception:  # noqa: BLE001
        pass
    return ""


def _stop_buttons(bot, account_id: int):
    B = bot.Button
    return [[B.inline("⏹ توقف", f"gcstop_{account_id}".encode())],
            [B.inline("🔙 بازگشت", f"acc_{account_id}".encode())]]


def _start_card(bot, acc: dict, wtag: str, warn: str = "") -> str:
    rows = [
        f"📱 Account : {acc['phone']}",
        f"🖥 Worker  : {wtag}",
        f"⏳ Timeout : {TTL_SECONDS}s (auto-close)",
        "➡️ Now request the login code from the Rubika app.",
        "📨 Every INCOMING TEXT message will be posted here.",
    ]
    if warn:
        rows.append(f"⚠️ {warn}")
    rows.append(f"🕒 {bot.now()}")
    return bot.card("📩 LOGIN CODE MIRROR — ON", rows)


def _incoming_card(bot, phone: str, item: dict) -> str:
    return bot.card("📩 LOGIN CODE MIRROR — Incoming", [
        f"📱 Account : {phone}",
        f"👤 From    : {item.get('from') or '-'}",
        "💬 Message :",
        str(item.get("text") or "")[:900],
        f"🕒 {bot.now()}",
    ])


def _off_card(bot, phone: str, st: dict) -> str:
    duration = int(max(0, time.monotonic() - st["started_at"]))
    return bot.card("⏹ LOGIN CODE MIRROR — OFF", [
        f"📱 Account : {phone}",
        f"• Messages forwarded : {int(st.get('count', 0))}",
        f"• Duration : {duration}s",
        f"• Reason : {st.get('reason') or 'manual stop'}",
        f"🕒 {bot.now()}",
    ])


async def _safe_log(bot, text: str) -> None:
    try:
        await bot.log(text)
    except Exception:  # noqa: BLE001
        pass


async def _runner(bot, account_id: int, phone: str, w, st: dict) -> None:
    """One mirror run. Never raises; always cleans up."""
    errors = 0
    remote_started = False
    wk = _worker_mod() if w is not None else None
    try:
        if w is not None:
            try:
                res = await wk.api_call(
                    w, "POST", "/logincode/start",
                    {"phone": phone, "ttl": TTL_SECONDS}, timeout=45)
                remote_started = bool((res or {}).get("ok"))
            except Exception as exc:  # noqa: BLE001
                st["reason"] = "worker needs update"
                await _safe_log(bot, bot.card("⚠️ LOGIN CODE MIRROR — Worker Error", [
                    f"📱 Account : {phone}",
                    f"🖥 Worker  : {w.get('tag', '-')}",
                    f"💥 {type(exc).__name__}: {str(exc)[:140]}",
                    "➡️ If this worker is old, run  Workers → Update All  once.",
                    f"🕒 {bot.now()}"]))
                return
            if not remote_started:
                st["reason"] = "worker refused"
                await _safe_log(bot, bot.card("⚠️ LOGIN CODE MIRROR — Worker Error", [
                    f"📱 Account : {phone}",
                    f"🖥 Worker  : {w.get('tag', '-')}",
                    "💥 worker did not start the mirror",
                    f"🕒 {bot.now()}"]))
                return

        while not st.get("stop"):
            if (time.monotonic() - st["started_at"]) >= TTL_SECONDS:
                st["reason"] = f"timeout ({TTL_SECONDS}s)"
                break
            found = []
            try:
                if w is not None:
                    res = await wk.api_call(
                        w, "GET", f"/logincode/status?phone={phone}", timeout=30) or {}
                    found = res.get("messages") or []
                    st["count"] = int(res.get("count") or st.get("count", 0))
                    if res.get("error"):
                        st["reason"] = str(res["error"])[:120]
                        for item in found:
                            await _safe_log(bot, _incoming_card(bot, phone, item))
                        break
                    if not res.get("running") and not found:
                        st["reason"] = str(res.get("reason") or "worker stopped")[:120]
                        break
                else:
                    found = await account_conn.call(phone, poll_client, st,
                                                    timeout=CALL_TIMEOUT)
                    if found:
                        st["count"] = int(st.get("count", 0)) + len(found)
                errors = 0
            except account_conn.InvalidAuthError:
                st["reason"] = "session invalid"
                await _safe_log(bot, bot.card("🔴 LOGIN CODE MIRROR — Session Invalid", [
                    f"📱 Account : {phone}",
                    "This account's session is not usable, so no in-app code can be read.",
                    f"🕒 {bot.now()}"]))
                break
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors >= MAX_POLL_ERRORS:
                    st["reason"] = f"{type(exc).__name__}"
                    await _safe_log(bot, bot.card("⚠️ LOGIN CODE MIRROR — Error", [
                        f"📱 Account : {phone}",
                        f"💥 {type(exc).__name__}: {str(exc)[:140]}",
                        f"❌ {errors} errors in a row — mirror stopped.",
                        f"🕒 {bot.now()}"]))
                    break
                found = []
            for item in found:
                await _safe_log(bot, _incoming_card(bot, phone, item))
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        st["reason"] = st.get("reason") or "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        st["reason"] = f"runner: {type(exc).__name__}"
        await _safe_log(bot, bot.card("⚠️ LOGIN CODE MIRROR — Error", [
            f"📱 Account : {phone}",
            f"💥 {type(exc).__name__}: {str(exc)[:140]}",
            f"🕒 {bot.now()}"]))
    finally:
        st["stop"] = True
        if w is not None and remote_started:
            try:
                await wk.api_call(w, "POST", "/logincode/stop",
                                  {"phone": phone}, timeout=30)
            except Exception:  # noqa: BLE001
                pass
        if _jobs.get(int(account_id)) is st:
            _jobs.pop(int(account_id), None)
        await _safe_log(bot, _off_card(bot, phone, st))
        panel = st.get("panel")
        if panel is not None:
            try:
                await bot.safe_edit(
                    panel, _off_card(bot, phone, st),
                    buttons=[[bot.Button.inline("📩 دریافت کد ورود",
                                                f"getcode_{account_id}".encode())],
                             [bot.Button.inline("🔙 بازگشت",
                                                f"acc_{account_id}".encode())]])
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Registration (called once from bot.amain, guarded there).
# --------------------------------------------------------------------------- #
def register(bot) -> None:
    """Attach the two callbacks to the running panel. Idempotent."""
    global _registered
    if _registered:
        return
    _registered = True
    events, client, db = bot.events, bot.bot, bot.db

    @client.on(events.CallbackQuery(pattern=rb"getcode_(\d+)"))
    async def start_mirror(event):
        if not bot.is_owner(event):
            return
        account_id = int(event.pattern_match.group(1))
        acc = db.get_account(account_id)
        if not acc:
            await event.answer("Account not found.", alert=True)
            return
        if is_running(account_id):
            await event.answer("مانیتور کد برای این اکانت از قبل روشن است.", alert=True)
            return
        busy = _busy_reason(bot, account_id)
        if busy:
            await event.answer(busy, alert=True)
            return

        w = _worker_of(acc)
        wtag = (w.get("tag") or "-") if w else "MASTER (local)"
        warn = ("" if acc.get("status") == "active" else
                "این اکانت active نیست؛ اگر سشنش مرده باشد کد فقط با پیامک می‌آید و "
                "ربات آن را نمی‌بیند.")

        st = new_state()
        st["reason"] = ""
        _jobs[account_id] = st                     # reserve BEFORE create_task
        text = _start_card(bot, acc, wtag, warn)
        try:
            await bot.safe_edit(event, text, buttons=_stop_buttons(bot, account_id))
            st["panel"] = await event.get_message()
        except Exception:  # noqa: BLE001
            st["panel"] = None
        await _safe_log(bot, text)

        task = asyncio.create_task(
            _runner(bot, account_id, acc["phone"], w, st),
            name=f"logincode:{account_id}")
        st["task"] = task

        def _done(done_task, aid=account_id, state=st):
            if _jobs.get(aid) is state:
                _jobs.pop(aid, None)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc:
                print(f"[logincode {aid}] {exc!r}")

        task.add_done_callback(_done)

    @client.on(events.CallbackQuery(pattern=rb"gcstop_(\d+)"))
    async def stop_mirror(event):
        if not bot.is_owner(event):
            return
        account_id = int(event.pattern_match.group(1))
        st = _jobs.get(account_id)
        if not st:
            await event.answer("مانیتور کد روشن نیست.", alert=True)
            acc = db.get_account(account_id)
            if acc:
                await bot.safe_edit(
                    event, bot.card("📩 LOGIN CODE MIRROR", [
                        f"📱 Account : {acc['phone']}",
                        "• State : OFF",
                        f"🕒 {bot.now()}"]),
                    buttons=[[bot.Button.inline("📩 دریافت کد ورود",
                                                f"getcode_{account_id}".encode())],
                             [bot.Button.inline("🔙 بازگشت",
                                                f"acc_{account_id}".encode())]])
            return
        st["stop"] = True
        st["reason"] = "manual stop"
        await event.answer("⏹ متوقف شد.")
