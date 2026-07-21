"""
Personal Rubika sender — controlled from a Telegram panel.
==========================================================

What it does (and ONLY this):
  * lets the owner log into THEIR OWN Rubika account (phone + code + 2FA),
  * forwards a message the owner marked in their OWN Saved Messages
    (e.g. caption ending in `کد135`) to their OWN contacts,
  * recipients are ordered: chat-first, then online, then last-seen,
  * configurable delay between sends (0.2 - 10s),
  * stops the whole run after MAX_ERRORS failed sends,
  * posts styled log cards to a private Telegram report group.

What it deliberately does NOT do: proxies, multi-account orchestration,
batch broadcasting, or "send to everyone" automation.

Panel text is Persian. Only the configured owner id may use it.
"""
import asyncio
import contextlib
import os
import random
import tempfile
import time
import zipfile
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError

import config
import crypto_util
import db
import rubika_client as rb
import worker
import account_conn
import features
import telegram_client as tg
import brain_control   # isolated brain stop/pause controller (bug fixes)
import worker_transfer  # isolated worker-transfer selection (exclude all tried)

# Make sure the data dir exists BEFORE the Telethon session file is created.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---- counter (total sends the bot has done), persisted in a small file ----
COUNTER_FILE = os.path.join(DATA_DIR, "send_count.txt")


def _read_counter() -> int:
    try:
        with open(COUNTER_FILE) as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _next_counter() -> int:
    n = _read_counter() + 1
    try:
        os.makedirs(os.path.dirname(COUNTER_FILE), exist_ok=True)
        with open(COUNTER_FILE, "w") as f:
            f.write(str(n))
    except Exception:
        pass
    return n


def now() -> str:
    # Timezone-aware (config.TIMEZONE, default Asia/Tehran) so log timestamps
    # are correct even when a worker runs on a foreign server.
    return config.now_str()


LINE = "-------------------------------"


def card(title: str, rows: list) -> str:
    return f"{title}\n{LINE}\n" + "\n".join(rows)


def panel_card(tag: str, rows: list, footer: str = None) -> str:
    """New English log-card shell:  | <emoji> - #<tag>  +  31-dash dividers
    +  `• Key : Value` rows  +  optional footer line."""
    out = f"| {tag}\n{LINE}\n" + "\n".join(rows) + f"\n{LINE}"
    if footer:
        out += f"\n{footer}"
    return out


bot = TelegramClient(os.path.join(DATA_DIR, "panel_bot"), config.API_ID, config.API_HASH)

# conversation state per owner: {"step": "..."}
state: dict = {}
# rubpy login clients mid-flow (waiting for code / password)
pending: dict = {}
# prepared sends waiting for confirmation: owner_id -> payload
pending_send: dict = {}
# prepared channels waiting for the "add members" step: owner_id -> payload
pending_channel: dict = {}
# stop flags per account id
stop_flags: dict = {}
# accounts currently running a send/channel job (in-memory busy lock)
active_jobs: set = set()
# running LOCAL automation tasks: account_id -> {"task":Task, "state":dict}
automation_tasks: dict = {}
# running LOCAL automation-EXTRAS tasks: account_id -> {"task":Task, "state":dict}
secretary_tasks: dict = {}
reply_tasks: dict = {}
channelreport_tasks: dict = {}
# YoudonoaAx UPDATE: live-control jobs keyed by account_id.
#   contact_jobs  -> Item 1 (contact import) + Item 2 (discovery) live controls
#   linkdooni_tasks -> Item 3 per-account send loops {account_id: {task,state}}
contact_jobs: dict = {}
linkdooni_tasks: dict = {}
linkdooni_engine: dict = {}        # {"task": Task, "stop": bool} for the orchestrator
# YoudonoaAx — Telegram section state (additive; never touches Rubika dicts).
tg_pending: dict = {}              # owner_id -> login ctx (code/password phase)
tg_jobs: dict = {}                 # phone -> live mutual-send control dict
# Single live Worker update queue. Prevents duplicate Update All clicks and
# overlap with the per-Worker update callback.
_worker_update_task = None
_worker_updating_ids: set = set()
_worker_update_lock_fd = None


def _alert_word(n: int) -> str:
    return {1: "ONE", 2: "TWO", 3: "THREE"}.get(n, str(n))


async def _wait_or_stop(account_id: int, seconds: float, step: float = 2.0) -> bool:
    """Sleep up to `seconds`; return True early if a manual stop was requested."""
    waited = 0.0
    while waited < seconds:
        if stop_flags.get(account_id):
            return True
        d = min(step, seconds - waited)
        await asyncio.sleep(d)
        waited += d
    return False


def automation_on(account_id: int) -> bool:
    try:
        return bool(db.get_automation(account_id).get("enabled"))
    except Exception:
        return False


def secretary_on(account_id: int) -> bool:
    try:
        return bool(db.get_secretary(account_id).get("enabled"))
    except Exception:
        return False


def channelreport_on(account_id: int) -> bool:
    try:
        return bool(db.get_channel_report(account_id).get("enabled"))
    except Exception:
        return False


def reply_on(account_id: int) -> bool:
    try:
        return bool(db.get_reply_responder(account_id).get("enabled"))
    except Exception:
        return False


def continuous_busy(account_id: int) -> bool:
    """True if ANY always-on feature (automation / secretary / channel report /
    reply responder) is active on the account. One-shot manual operations
    (send / channel / join) are blocked while this is True, so a one-shot never
    opens a second connection alongside the shared one (Feature 6)."""
    return (automation_on(account_id) or secretary_on(account_id)
            or channelreport_on(account_id) or reply_on(account_id))


def _pick_text(texts: list, last_idx):
    """Random text index, avoiding the same one as last time (if possible)."""
    if not texts:
        return None, None
    if len(texts) == 1:
        return 0, texts[0]
    choices = [i for i in range(len(texts)) if i != last_idx]
    i = random.choice(choices)
    return i, texts[i]


def is_owner(event) -> bool:
    """Allowed to USE the bot = the owner OR an admin added from the panel.
    (Name kept for minimal churn across existing handlers.)
    """
    try:
        allowed = set(config.ALLOWED_IDS) | set(db.list_admin_ids())
    except Exception:
        allowed = set(config.ALLOWED_IDS)
    return event.sender_id in allowed


def is_real_owner(event) -> bool:
    """Only the configured OWNER (used for admin/worker management)."""
    return config.OWNER_ID and event.sender_id == config.OWNER_ID


async def log(text: str):
    """Post a report card to the log group (never crash the bot)."""
    try:
        await bot.send_message(config.LOG_GROUP_ID, text)
    except Exception as e:  # noqa: BLE001
        print(f"[log error] {e}")


async def log_error(section: str, account: str, operation: str, error,
                    extra: list = None):
    """Post a TIDY error card to the log group instead of a bare repr dump.

    YoudonoaAx UPDATE (step 1): this is a PERSONAL bot, so the real error text
    is kept (never hidden) — it's just laid out as a structured card
    «بخش / اکانت / عملیات / متن خطا / زمان» so the log group stays readable
    instead of dumping a raw ``repr(e)`` line. Never raises (logging must never
    crash the bot)."""
    try:
        detail = error if isinstance(error, str) else repr(error)
    except Exception:  # noqa: BLE001
        detail = "?"
    rows = [
        f"• Section   : {section or '—'}",
        f"• Account   : {account or '—'}",
        f"• Operation : {operation or '—'}",
        f"• Error     : {str(detail)[:300]}",
    ]
    if extra:
        rows.extend(extra)
    rows.append(f"🕒 {now()}")
    await log(card("⚠️ ERROR", rows))


# --------------------------------------------------------------------------- #
# Portable Rubika session (v4): capture the 5 session values after a login,
# log them to the group as a copyable token, and distribute to remote workers
# so an account can run on ANY worker WITHOUT a fresh login code.
#   • IMPORT is WRITE-ONLY on the worker (session.insert, never connect) so it
#     can never trigger AUTH_FROM_ANOTHER.
#   • Logging the session text to the OWNER's own log group is intentional
#     (same central-log rule as the rest of this personal bot).
# --------------------------------------------------------------------------- #
def _session_values(client, phone, guid):
    """Read the 5 portable values off a freshly-logged-in client. Base-safe:
    only READS attributes that rb.finish_login already set."""
    try:
        return {
            "auth": getattr(client, "auth", None),
            "private_key": getattr(client, "private_key", None),
            "guid": str(guid) if guid else None,
            "phone": rb.normalize_phone(phone),
            "user_agent": getattr(client, "user_agent", None),
        }
    except Exception:  # noqa: BLE001
        return None


async def _post_session_token(phone, name, values):
    """Post the portable session to the log group: a readable card plus the raw
    token on its own line (monospace, easy to copy)."""
    if not values or not values.get("auth"):
        return
    try:
        token = db.session_pack(values)
        rows = [f"📱 {phone}"]
        if name:
            rows.append(f"👤 {name}")
        rows += [LINE,
                 "Use this token to log in on any server/worker without a code (keep it secret):"]
        await log(card("🔑 SESSION (PORTABLE)", rows))
        try:
            await bot.send_message(config.LOG_GROUP_ID, f"`{token}`",
                                   parse_mode="md")
        except Exception:  # noqa: BLE001
            await log(token)        # plain fallback if markdown is off
    except Exception:  # noqa: BLE001
        pass


async def _push_session_to_worker(w, values, timeout: int = 60):
    """WRITE the session onto one remote worker (no connect). Returns the
    worker's response dict; raises on transport error."""
    return await worker.api_call(w, "POST", "/session/import", {
        "phone": values.get("phone"),
        "auth": values.get("auth"),
        "private_key": values.get("private_key"),
        "guid": values.get("guid"),
        "user_agent": values.get("user_agent"),
    }, timeout=timeout)


async def _distribute_session(values, only_worker_id=None):
    """Push the session to every enabled REMOTE worker (or just one). Best-effort
    — never raises. Returns (ok_count, fail_count)."""
    ok = fail = 0
    try:
        workers = db.list_workers()
    except Exception:  # noqa: BLE001
        workers = []
    for w in workers:
        if worker.is_local(w) or not w.get("enabled"):
            continue
        if only_worker_id is not None and w.get("id") != only_worker_id:
            continue
        try:
            res = await _push_session_to_worker(w, values)
            if res.get("ok"):
                ok += 1
            else:
                fail += 1
        except Exception:  # noqa: BLE001
            fail += 1
    return ok, fail


async def _session_transfer_to_worker(neww, sess, phone) -> bool:
    """Code-free worker transfer: WRITE the stored session onto the chosen
    worker and verify it's alive. Returns True on success. Write-only on remote
    (no connect) so it can't cause AUTH_FROM_ANOTHER; the real connection happens
    later in the resume send (one connection at a time — conflict logic intact)."""
    try:
        if worker.is_local(neww):
            try:
                await account_conn.close(phone)
            except Exception:
                pass
            client = rb.open_client(phone)
            client.session.insert(
                auth=sess.get("auth"), guid=sess.get("guid"),
                user_agent=sess.get("user_agent"),
                phone_number=rb.normalize_phone(phone),
                private_key=sess.get("private_key"))
            ok = False
            try:
                await rb.connect_ready(client)
                me = await client.get_me()
                ok = bool(rb._guid_of(me))
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            return ok
        # remote: write-only import, then verify (no second live connection)
        res = await _push_session_to_worker(neww, sess)
        if not res.get("ok"):
            return False
        vr = await worker.api_call(neww, "POST", "/account/verify",
                                   {"phone": rb.normalize_phone(phone)}, timeout=90)
        return not vr.get("dead")
    except Exception as e:  # noqa: BLE001
        try:
            await log(card("⚠️ WORKER TRANSFER (SESSION) FAILED", [
                f"📱 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
        except Exception:
            pass
        return False


async def _log_invalid_auth(phone: str, detail: str = ""):
    """Log that an account's session is truly invalid (device kicked out / login
    revoked) AFTER a fresh-connection retry already failed. Per the owner this is
    expected, not a bug. Marks the account inactive so the panel shows a
    one-tap re-login button, and tells the user how to recover it.

    ``detail`` carries the REAL underlying error text so we stop guessing why a
    session was rejected.
    """
    try:
        for a in db.list_accounts():
            if rb.normalize_phone(a["phone"]) == rb.normalize_phone(phone):
                db.set_status(a["id"], "inactive")
                break
    except Exception:
        pass
    rows = [
        f"👤 Account : {phone}",
        "📵 This session was kicked out of Rubika (device logged out).",
        "All features for this account are temporarily stopped.",
        "🔁 To recover: Accounts -> this account -> Re-login.",
    ]
    if detail:
        rows.append(f"• Detail    : {detail[:200]}")
    rows.append(f"🕒 {now()}")
    await log(card("🔐 INVALID_AUTH — RE-LOGIN REQUIRED", rows))


async def _on_invalid_auth(phone: str):
    """account_conn handler: only MARK the account inactive (the feature loops
    do the logging when they catch InvalidAuthError, so we don't double-post)."""
    try:
        for a in db.list_accounts():
            if rb.normalize_phone(a["phone"]) == rb.normalize_phone(phone):
                db.set_status(a["id"], "inactive")
                break
    except Exception:
        pass


async def safe_edit(obj, *args, **kwargs):
    """Edit a message/callback, ignoring Telegram's 'content not modified'
    error (raised when the new text+buttons equal what's already shown)."""
    try:
        return await obj.edit(*args, **kwargs)
    except MessageNotModifiedError:
        return None


# --------------------------------------------------------------------------- #
# Menus
# --------------------------------------------------------------------------- #
def main_menu(owner: bool = True):
    rows = [
        [Button.inline("🚀 Send", b"send_menu"),
         Button.inline("🔁 Automation", b"automation")],
        [Button.inline("➕ Add Account", b"add_account"),
         Button.inline("👤 Accounts", b"accounts")],
        [Button.inline("📌 Content", b"marker"),
         Button.inline("⚙️ Send Speed", b"speed")],
        [Button.inline("🛠 Workers", b"workers"),
         Button.inline("💾 Backup", b"backup")],
        [Button.inline("🧠 Channel Brain", b"cbrain")],
        [Button.inline("🖼 PV Photo Archive (PDF)", b"pvexport")],
        [Button.inline("📤 Multi Send", b"multisend"),
         Button.inline("🧠 Brain", b"brain")],
        [Button.inline("➕ Add Contacts", b"contacts"),
         Button.inline("⚙️ Settings", b"settings")],
        [Button.inline("✈️ Telegram", b"tg"),
         Button.inline("🌐 Portal", b"portal_panel")],
    ]
    if owner:
        rows.append([Button.inline("👥 Admin Management", b"admins")])
    return rows


WELCOME = (
    "🤖 Rubika Tools\n"
    "Welcome 👋 Choose an option:"
)


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_owner(event):
        await event.respond("⛔ You do not have access to this bot.")
        return
    state.pop(event.sender_id, None)
    text = WELCOME
    try:
        import status_summary
        text = status_summary.format_card(await status_summary.get_summary()) + "\n\nChoose an option:"
    except Exception:
        pass
    await event.respond(text, buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    text = WELCOME
    try:
        import status_summary
        text = (status_summary.format_card(await status_summary.get_summary())
                + "\n\nChoose an option:")
    except Exception:
        pass
    await safe_edit(event, text, buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    if not is_owner(event):
        return
    p = pending.pop(event.sender_id, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass
    state.pop(event.sender_id, None)
    await safe_edit(event, "Cancelled. Main menu:", buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Add account
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"add_account"))
async def add_account_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_phone"}
    await safe_edit(event, 
        "📱 Send your Rubika account phone number.\nExample: `09123456789`",
        buttons=[[Button.inline("🔑 Login with Session (no code)", b"loginsess")],
                 [Button.inline("🔙 Cancel", b"cancel")]],
    )


@bot.on(events.CallbackQuery(data=b"loginsess"))
async def login_session_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_session"}
    await safe_edit(event,
        "🔑 Send the session token (the one you copied earlier from the log group).\n"
        "Format `YDSESS:...` — connects without a login code.",
        buttons=[[Button.inline("🔙 Cancel", b"cancel")]],
    )


# --------------------------------------------------------------------------- #
# Accounts list / dashboard
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"accounts"))
async def accounts_cb(event):
    if not is_owner(event):
        return
    await _render_accounts(event, 0)


async def _render_accounts(event, page: int = 0):
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, 
            "You haven't added any account yet.",
            buttons=[[Button.inline("➕ Add Account", b"add_account")],
                     [Button.inline("🔙 Back", b"home")]],
        )
        return
    page_items, nav, page, total_pages = _paginate(accounts, page, "accpage_")
    # keep the ORIGINAL 1-based numbering across pages
    offset = page * ACC_PAGE_SIZE
    buttons = []
    for i, acc in enumerate(page_items, start=offset + 1):
        mark = "" if acc["status"] == "active" else " ⚠️"
        buttons.append([Button.inline(f"{i}- {acc['phone']}{mark}",
                                      f"acc_{acc['id']}".encode())])
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("🔄 Check & Clean Dead Accounts",
                                  b"acc_sweep")])
    buttons.append([Button.inline("🔙 Back", b"home")])
    title = "👤 Your accounts:"
    if total_pages > 1:
        title += f"  (page {page + 1}/{total_pages})"
    await safe_edit(event, title, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"accpage_(\\d+)"))
async def accounts_page_cb(event):
    if not is_owner(event):
        return
    await _render_accounts(event, int(event.pattern_match.group(1)))


@bot.on(events.CallbackQuery(data=b"acc_sweep"))
async def accounts_sweep_cb(event):
    """Update step: check every account's session and flag the dead ones
    (sessions that were kicked out / logged out) as inactive, so the panel
    clearly shows which accounts need a re-login. Healthy accounts that were
    wrongly marked inactive are restored to active."""
    if not is_owner(event):
        return
    await safe_edit(event, "🔄 Checking the session of every account ... (this may take a while)")
    asyncio.create_task(run_accounts_sweep(event.sender_id))


async def run_accounts_sweep(owner_id: int):
    """Manual trigger of the SAME unified Watcher cycle as the periodic health
    engine (run_health_engine): it verifies sessions, self-heals, and quarantines
    confirmed-dead accounts through the canonical observer logic. The #watcher_health
    card is posted to the log group; Shot accounts are deleted only by the owner
    from the quarantine panel (never auto-deleted, never batch-deleted)."""
    await run_health_engine()
    q_count = 0
    try:
        from portal import observer as _observer
        q_count = len(_observer.quarantined_accounts())
    except Exception:
        pass
    rows = []
    if q_count:
        rows.append([Button.inline(f"🗑 Delete Shot Accounts ({q_count})",
                                   b"portal_quarantine")])
    rows.append([Button.inline("👤 Accounts", b"accounts")])
    rows.append([Button.inline("🏠 Main Menu", b"home")])
    try:
        await bot.send_message(
            owner_id,
            "🔄 Health check done; the #watcher_health card was posted to the log group."
            + (f"\n🔴 Shot accounts (quarantined): {q_count} — open the quarantine panel to delete."
               if q_count else "\n🟢 No shot accounts found."),
            buttons=rows)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Cleanup engine (موتور پاکسازی): groups where an account got banned/muted are
# recorded as candidates; the owner reviews them in a confirm/cancel panel and
# decides to leave them or keep them.
# --------------------------------------------------------------------------- #
async def _log_cleanup_candidate(account_id: int, phone: str, guid: str, name: str):
    """Log a freshly detected banned/muted group with a confirm/cancel panel."""
    rows = [
        f"👤 Account : {phone}",
        f"👥 Group : {name or guid}",
        f"🆔 {guid}",
        "⛔ This group has banned/muted the account (sending is not possible).",
        "Do you want it to leave?",
        f"🕒 {now()}",
    ]
    try:
        await bot.send_message(
            config.LOG_GROUP_ID, card("🧹 Cleanup Engine — Banned/Muted Group", rows),
            buttons=[[Button.inline("✅ Confirm Leave",
                                    f"clnyes_{account_id}_{guid}".encode())],
                     [Button.inline("🚫 Cancel (stay)",
                                    f"clnno_{account_id}_{guid}".encode())]])
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup log] {e}")


@bot.on(events.CallbackQuery(pattern=b"clnyes_(\\d+)_(.+)"))
async def cleanup_confirm_cb(event):
    """Owner confirmed leaving a banned/muted group."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    guid = event.pattern_match.group(2).decode()
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    await event.answer("Leaving the group ...")
    phone = acc["phone"]
    ok = False
    try:
        w = worker.worker_for_account(acc)
        if w and not worker.is_local(w):
            res = await worker.api_call(w, "POST", "/group/leave",
                                        {"phone": phone, "group_guid": guid},
                                        timeout=90)
            ok = bool(res.get("ok"))
        else:
            await account_conn.call(phone, rb.leave_group, guid, timeout=60)
            ok = True
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ Leave failed: {repr(e)[:120]}")
        return
    db.remove_cleanup_candidate(account_id, guid)
    await safe_edit(event, card("🧹 Cleanup Engine", [
        f"👤 Account : {phone}",
        f"👥 Group : {guid}",
        ("✅ Left the group." if ok else "⚠️ Leave result was unclear."),
        f"🕒 {now()}",
    ]))


@bot.on(events.CallbackQuery(pattern=b"clnno_(\\d+)_(.+)"))
async def cleanup_cancel_cb(event):
    """Owner chose to keep the group; just drop it from the candidate list."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    guid = event.pattern_match.group(2).decode()
    db.remove_cleanup_candidate(account_id, guid)
    await safe_edit(event, card("🧹 Cleanup Engine", [
        f"👥 Group : {guid}",
        "🚫 Cancelled — the group stays (removed from this list).",
        f"🕒 {now()}",
    ]))


@bot.on(events.CallbackQuery(pattern=b"acc_(\\d+)"))
async def account_menu_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    status = "ACTIVE ✅" if acc["status"] == "active" else "INACTIVE ⚠️ (invalid session)"
    text = card("👤 Account", [
        f"• Name   : {acc['name'] or '-'}",
        f"• Phone  : {acc['phone']}",
        f"• ID     : {acc['user_id']}",
        f"• Status : {status}",
    ])
    buttons = []
    if acc["status"] != "active":
        buttons.append([Button.inline("🔁 Re-login (session recovery)",
                                      f"relogin_{account_id}".encode())])
    buttons += [
        [Button.inline("🚀 Send", f"send_{account_id}".encode()),
         Button.inline("📢 Channel", f"chan_{account_id}".encode())],
        [Button.inline("🔑 Distribute Session to Workers", f"sessdist_{account_id}".encode())],
        [Button.inline("🗑 Delete Account", f"del_{account_id}".encode())],
        [Button.inline("🔙 Back", b"accounts")],
    ]
    await safe_edit(event, text, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"sessdist_(\\d+)"))
async def session_distribute_cb(event):
    """v4: push this account's stored session to every remote worker so it can
    run on ANY worker WITHOUT a fresh login code (write-only import)."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    sess = db.get_session_blob(account_id)
    if not sess or not sess.get("auth"):
        await event.answer(
            "There is no saved session for this account (accounts from before this update "
            "have no blob — re-login once).", alert=True)
        return
    await safe_edit(event, "⏳ Distributing the session to remote workers ...")
    ok, fail = await _distribute_session(sess)
    try:
        await log(card("🔑 SESSION DISTRIBUTED", [
            f"📱 {acc['phone']}", f"• Success : {ok} worker(s)",
            f"• Failed  : {fail} worker(s)", f"🕒 {now()}"]))
    except Exception:  # noqa: BLE001
        pass
    await safe_edit(event, card("🔑 Session Distribution", [
        f"📱 {acc['phone']}",
        f"• Success : {ok} worker(s)",
        f"• Failed  : {fail} worker(s)",
        "This account can now connect on any successful worker without a code."]),
        buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"del_(\\d+)"))
async def delete_confirm_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    await safe_edit(event, 
        "Are you sure you want to delete this account?",
        buttons=[[Button.inline("✅ Yes, delete", f"delyes_{account_id}".encode())],
                 [Button.inline("🔙 No", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"delyes_(\\d+)"))
async def delete_do_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.delete_account(account_id)
    await safe_edit(event, "Account deleted. ✅",
                     buttons=[[Button.inline("🔙 Back", b"accounts")]])


@bot.on(events.CallbackQuery(pattern=b"relogin_(\\d+)"))
async def relogin_cb(event):
    """Re-login an account whose session was invalidated (device kicked out).
    Reuses the normal login flow; on success its active features auto-recover."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    phone = acc["phone"]
    await safe_edit(event, "⏳ Preparing re-login ...")
    w = worker.worker_for_account(acc) or worker.ensure_master_worker()
    if w and not worker.is_local(w):
        # health-check the owning worker first, like the normal remote login
        try:
            await worker.check_worker(w)
        except Exception:
            pass
        w = db.get_worker(w["id"])
        if not (w and w["enabled"] and w["status"] == "ok"):
            await safe_edit(event,
                "❌ This account's worker is not healthy right now. Fix the worker status first.",
                buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
            return
        await handle_phone_remote(event, phone, w)
    else:
        await _begin_local_login(event, phone, w)


async def _recover_account_features(account_id: int, settle_delay: float = 0.0):
    """After a (re)login, relaunch every always-on feature that is still marked
    enabled for this account, so recovery is automatic.

    ``settle_delay``: wait this many seconds before relaunching, so a freshly
    created session has time to fully settle on Rubika's side. Relaunching heavy
    activity on a brand-new session immediately can make Rubika reject it with a
    (transient) INVALID_AUTH right after adding the account.
    """
    if settle_delay:
        await asyncio.sleep(settle_delay)
    acc = db.get_account(account_id)
    if not acc:
        return
    if automation_on(account_id):
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Automation recovery for {acc['phone']} failed: {repr(e)[:120]}")
    if secretary_on(account_id):
        try:
            await start_secretary(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Secretary recovery for {acc['phone']} failed: {repr(e)[:120]}")
    if channelreport_on(account_id):
        try:
            await start_channelreport(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Channel-report recovery for {acc['phone']} failed: {repr(e)[:120]}")
    if reply_on(account_id):
        try:
            await start_reply(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Reply recovery for {acc['phone']} failed: {repr(e)[:120]}")
    if automation_on(account_id) or secretary_on(account_id) or \
            channelreport_on(account_id) or reply_on(account_id):
        await log(card("♻️ FEATURES RECOVERED", [
            f"👤 Account : {acc['phone']}",
            "This account's active features were restarted after re-login.",
            f"🕒 {now()}"]))

    # Campaign: if globally enabled, auto-run the campaign sequence
    # (create channel -> forward marker -> send to contacts) with 5s delays.
    try:
        if _is_campaign_enabled():
            asyncio.create_task(_run_campaign(account_id))
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# Speed (delay) setting
# --------------------------------------------------------------------------- #
def speed_buttons():
    return [
        [Button.inline("0.2s", b"sp_0.2"), Button.inline("0.5s", b"sp_0.5"),
         Button.inline("1s", b"sp_1")],
        [Button.inline("2s", b"sp_2"), Button.inline("5s", b"sp_5"),
         Button.inline("10s", b"sp_10")],
        [Button.inline("🔙 Back", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"speed"))
async def speed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_delay"}
    await safe_edit(event, 
        f"⏱ Current delay: {db.get_delay()} seconds\n{LINE}\n"
        "Pick a speed, or send a number between 0.2 and 10:",
        buttons=speed_buttons(),
    )


@bot.on(events.CallbackQuery(pattern=b"sp_([0-9.]+)"))
async def speed_set_cb(event):
    if not is_owner(event):
        return
    value = config.clamp_delay(event.pattern_match.group(1).decode())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await safe_edit(event, f"✅ Delay set to {value} seconds.",
                     buttons=[[Button.inline("🔙 Main Menu", b"home")]])


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"backup"))
async def backup_cb(event):
    if not is_owner(event):
        return
    await event.answer("Building a full backup ...")
    try:
        archive = await build_backup_archive()
    except Exception as e:  # noqa: BLE001
        await event.answer(f"Backup build error: {repr(e)[:120]}", alert=True)
        return
    if not archive:
        await event.answer("There is nothing to back up yet.", alert=True)
        return
    try:
        await bot.send_file(
            event.sender_id, archive,
            caption=("💾 Full backup • " + now() +
                     "\nIncludes: database + all account sessions + counter"),
            force_document=True,
        )
        await event.answer("Backup sent.")
    finally:
        try:
            os.remove(archive)
        except Exception:
            pass


def _add_dir_to_zip(zf: zipfile.ZipFile, src_dir: str, arc_prefix: str):
    """Recursively add every file under src_dir into the zip under arc_prefix/."""
    if not os.path.isdir(src_dir):
        return
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, src_dir)
            zf.write(full, arcname=os.path.join(arc_prefix, rel))


async def _add_worker_sessions(zf: zipfile.ZipFile):
    """Worker-aware hook.

    When the Worker subsystem exists, this pulls each registered worker's
    session files over its SSH tunnel and stores them under
    `sessions/<worker_tag>/` inside the same archive. It is a safe no-op until
    the Worker module is added, so the backup never breaks.
    """
    try:
        import worker  # added together with the Worker subsystem
    except ImportError:
        return
    try:
        await worker.collect_sessions_into_zip(zf)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Worker session backup was incomplete: {repr(e)[:150]}")


async def build_backup_archive():
    """Bundle the master DB + all local session files + counter into one zip.

    Returns the path to a temporary .zip (caller deletes it) or None if there
    is nothing to back up.
    """
    has_db = os.path.exists(db.DB_PATH)
    has_sessions = os.path.isdir(rb.SESSIONS_DIR) and any(os.scandir(rb.SESSIONS_DIR))
    if not has_db and not has_sessions:
        return None

    fd, zip_path = tempfile.mkstemp(prefix="rubika_backup_", suffix=".zip", dir=DATA_DIR)
    os.close(fd)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if has_db:
            zf.write(db.DB_PATH, arcname="data.db")
        if os.path.exists(COUNTER_FILE):
            zf.write(COUNTER_FILE, arcname="send_count.txt")
        # local session files (master-side accounts)
        _add_dir_to_zip(zf, rb.SESSIONS_DIR, "sessions/local")
        # worker session files (no-op until the Worker subsystem is added)
        await _add_worker_sessions(zf)
    return zip_path


# --------------------------------------------------------------------------- #
# Send menu (pick which account)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Account-list pagination (presentation-only). 15 buttons per page with
# ◀️/▶️ navigation. First page has no ◀️ and last page has no ▶️.
# --------------------------------------------------------------------------- #
ACC_PAGE_SIZE = 15


def _paginate(items, page, cb_prefix, per_page=ACC_PAGE_SIZE):
    """Return (page_items, nav_row, page, total_pages) for a long button list."""
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    page_items = items[start:start + per_page]
    nav = []
    if page > 0:
        nav.append(Button.inline("◀️ Prev", f"{cb_prefix}{page - 1}".encode()))
    if page < total_pages - 1:
        nav.append(Button.inline("Next ▶️", f"{cb_prefix}{page + 1}".encode()))
    return page_items, nav, page, total_pages


async def _render_send_menu(event, page: int = 0):
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, "Add an account first.",
                         buttons=[[Button.inline("➕ Add Account", b"add_account")],
                                  [Button.inline("🔙 Back", b"home")]])
        return
    page_items, nav, page, total_pages = _paginate(accounts, page, "smpage_")
    buttons = [[Button.inline(f"🚀 {a['phone']}", f"sm_{a['id']}".encode())]
               for a in page_items]
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("🔙 Back", b"home")])
    title = "Which account should send?"
    if total_pages > 1:
        title += f"  (page {page + 1}/{total_pages})"
    await safe_edit(event, title, buttons=buttons)


@bot.on(events.CallbackQuery(data=b"send_menu"))
async def send_menu_cb(event):
    if not is_owner(event):
        return
    await _render_send_menu(event, 0)


@bot.on(events.CallbackQuery(pattern=b"smpage_(\\d+)"))
async def send_menu_page_cb(event):
    if not is_owner(event):
        return
    await _render_send_menu(event, int(event.pattern_match.group(1)))


@bot.on(events.CallbackQuery(pattern=b"sm_(\\d+)"))
async def send_mode_cb(event):
    """Choose HOW to send with this account: normal forward, or channel mode."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    await safe_edit(event, 
        f"📤 Choose the send type for account {acc['phone']}:",
        buttons=[
            [Button.inline("📎 Forward Marker to Contacts", f"send_{account_id}".encode())],
            [Button.inline("✍️ Plain Text to Contacts", f"sendtext_{account_id}".encode())],
            [Button.inline("📢 Channel-style Send", f"chan_{account_id}".encode())],
            [Button.inline("🔙 Back", b"send_menu")],
        ],
    )


# --------------------------------------------------------------------------- #
# Message router (conversation steps)
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage)
async def message_router(event):
    if not is_owner(event):
        return
    if event.raw_text.startswith("/start"):
        return
    st = state.get(event.sender_id)
    if not st:
        return
    step = st.get("step")
    if step == "await_phone":
        await handle_phone(event)
    elif step == "await_session":
        await handle_session_login(event)
    elif step == "await_code":
        await handle_code(event)
    elif step == "await_password":
        await handle_password(event)
    elif step == "await_delay":
        await handle_delay(event)
    elif step == "await_marker":
        await handle_marker(event)
    elif step == "await_rb_text2":
        await handle_rb_text2(event)
    elif step == "await_plain_text":
        await handle_plain_text(event)
    elif step == "await_channel_name":
        await handle_channel_name(event)
    elif step == "await_campaign_channel_name":
        await handle_campaign_channel_name(event)
    elif step == "await_auto_text":
        await handle_auto_text(event)
    elif step == "await_auto_interval":
        await handle_auto_interval(event)
    elif step == "await_auto_link":
        await handle_auto_link(event)
    elif step == "await_admin_id":
        await handle_admin_id(event)
    elif step == "await_sec_text":
        await handle_sec_text(event)
    elif step == "await_sec_interval":
        await handle_sec_interval(event)
    elif step == "await_cr_channel":
        await handle_cr_channel(event)
    elif step == "await_cr_interval":
        await handle_cr_interval(event)
    elif step == "await_rp_text":
        await handle_rp_text(event)
    elif step == "await_rp_delay":
        await handle_rp_delay(event)
    elif step == "await_psync":
        await handle_psync_input(event)
    elif step == "await_cb_title":
        await handle_cb_title(event)
    elif step == "await_cb_count":
        await handle_cb_count(event)
    elif step == "await_cb_per":
        await handle_cb_per(event)
    elif step == "await_cb_prefix":
        await handle_cb_prefix(event)
    elif step in ("wk_ip", "wk_port", "wk_user", "wk_pass"):
        await handle_worker_step(event, step)
    elif step == "await_contacts_file":
        await handle_contacts_file(event, st)
    elif step == "await_brain_file":
        await handle_brain_file(event, st)
    elif step == "await_set_maxerr":
        await handle_set_maxerr(event, st)
    elif step == "await_set_resume":
        await handle_set_resume(event, st)
    elif step == "await_set_senddelay":
        await handle_set_senddelay(event, st)
    elif step == "await_set_contactspeed":
        await handle_set_contactspeed(event, st)
    elif step == "await_set_braincap":
        await handle_set_braincap(event, st)
    elif step == "await_set_disctarget":
        await handle_set_disctarget(event, st)
    elif step == "await_set_discattempts":
        await handle_set_discattempts(event, st)
    elif step == "await_discover_prefix":
        await handle_discover_prefix(event, st)
    elif step == "await_discover_text":
        await handle_discover_text(event, st)
    elif step == "await_ld_channels":
        await handle_ld_channels(event, st)
    elif step == "await_ld_text":
        await handle_ld_text(event, st)
    elif step == "await_ld_interval":
        await handle_ld_interval(event, st)
    elif step == "await_ld_daily":
        await handle_ld_daily(event, st)
    elif step == "await_tg_phone":
        await handle_tg_phone(event)
    elif step == "await_tg_code":
        await handle_tg_code(event)
    elif step == "await_tg_password":
        await handle_tg_password(event)
    elif step == "await_tg_msg_text":
        await handle_tg_msg_text(event)
    elif step == "await_tg_msg_media":
        await handle_tg_msg_media(event)
    elif step == "await_tg_speed":
        await handle_tg_speed(event)


async def handle_delay(event):
    value = config.clamp_delay(event.raw_text.strip())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Delay set to {value} seconds.",
                        buttons=main_menu(is_real_owner(event)))


_FA_AR_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                              "01234567890123456789")


def _normalize_phone_input(event) -> str:
    """Accept almost any phone format (FA/AR digits, spaces, dashes, 00/0/+98,
    shared contact card) and return a clean +98XXXXXXXXXX style string.
    Returns "" when no digits could be found."""
    raw = (getattr(event, "raw_text", "") or "").strip()
    # shared contact card (user tapped "share contact")
    if not raw:
        try:
            contact = getattr(getattr(event, "message", None), "contact", None) \
                or getattr(event, "contact", None)
            if contact and getattr(contact, "phone_number", None):
                raw = str(contact.phone_number)
        except Exception:
            raw = ""
    if not raw:
        return ""
    raw = raw.translate(_FA_AR_DIGITS)
    has_plus = "+" in raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("98"):
        return "+" + digits
    if digits.startswith("0") and len(digits) >= 10:
        return "+98" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "+98" + digits
    return "+" + digits


async def handle_session_login(event):
    """v4: no-code login — the owner pastes a YDSESS token (from the log group),
    we import it onto a chosen worker (WRITE-only) and register the account
    WITHOUT any login code."""
    raw = (event.raw_text or "").strip()
    try:
        sess = db.session_unpack(raw)
    except Exception:  # noqa: BLE001
        state[event.sender_id] = {"step": "await_session"}
        await event.respond("❌ Invalid session token. Send it again or cancel.")
        return
    phone = rb.normalize_phone(sess.get("phone") or "")
    if not phone or not sess.get("auth"):
        state[event.sender_id] = {"step": "await_session"}
        await event.respond("❌ Session token is incomplete (missing phone/auth). Send it again or cancel.")
        return
    state.pop(event.sender_id, None)
    sess["phone"] = phone
    await event.respond("⏳ Logging in with session (no code) ...")
    try:
        w = await worker.pick_worker_for_login()
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Error picking a worker: {repr(e)[:150]}")
        return
    if not w:
        await event.respond("❌ No healthy worker is available.")
        return
    wtag = w.get("tag", "-")
    try:
        if worker.is_local(w):
            # ----- LOCAL import + one-connection verify -----
            try:
                await account_conn.close(phone)
            except Exception:
                pass
            client = rb.open_client(phone)
            client.session.insert(
                auth=sess.get("auth"), guid=sess.get("guid"),
                user_agent=sess.get("user_agent"), phone_number=phone,
                private_key=sess.get("private_key"))
            await rb.connect_ready(client)
            try:
                me = await client.get_me()
                guid = rb._guid_of(me) or sess.get("guid") or "-"
                name = rb._name_of(me)
                ordered, stats = await rb.get_ordered_recipients(client)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            account_id = db.add_account(phone, name, str(guid), rb.session_path(phone))
            if w.get("id"):
                db.set_account_worker(account_id, w["id"])
            sess["guid"] = str(guid)
            db.set_session_blob(account_id, sess)
            contacts, groups, with_chat = (stats["contacts"], stats["groups"],
                                           stats["with_chat"])
        else:
            # ----- REMOTE import (write-only) + verify -----
            res = await _push_session_to_worker(w, sess)
            if not res.get("ok"):
                await event.respond(
                    f"❌ Writing the session to the worker failed: {res.get('error', '?')}")
                return
            vr = await worker.api_call(w, "POST", "/account/verify",
                                       {"phone": phone}, timeout=90)
            if vr.get("dead"):
                await event.respond(
                    "❌ This session is invalid (Rubika rejected it). You must log in with a code.")
                return
            name = sess.get("name") or "-"
            guid = sess.get("guid") or "-"
            account_id = db.add_account(phone, name, str(guid), "")
            db.set_account_worker(account_id, w["id"])
            db.set_session_blob(account_id, sess)
            contacts = groups = with_chat = 0
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ SESSION LOGIN FAILED", [
            f"📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        await event.respond(
            f"❌ Session login failed: {repr(e)[:140]}\n"
            "If rubpy's session.insert signature differs on this server, share this error text.")
        return

    await log(panel_card("✅ - #rubika_login", [
        "• Status        : SUCCESS",
        f"• Phone         : {phone}",
        f"• Name          : {name}",
        f"• GUID          : {guid}",
        "• Login Method  : SESSION",
        "• Session Saved : YES",
        f"• Time          : {now()}",
    ], footer=f"--| 🌍 - Worker : #{wtag}"))
    await event.respond(
        f"✅ Account added via session (no code)! (worker {wtag})\n"
        f"👤 {name} | 📱 {phone}\n"
        f"📇 Contacts: {contacts} | 👥 Groups: {groups} | 💬 With chat: {with_chat}",
        buttons=[[Button.inline("🚀 Send", f"send_{account_id}".encode())],
                 [Button.inline("🏠 Main Menu", b"home")]])


async def handle_phone(event):
    phone = _normalize_phone_input(event)
    if not phone:
        await event.respond(
            "❌ Couldn't read the number. Send it with the country code (e.g. +989121234567 "
            "or 09121234567) or cancel.")
        return
    await event.respond("⏳ Picking a healthy worker and connecting to Rubika ...")
    # Pick the worker that will OWN this account (round-robin + health check).
    try:
        w = await worker.pick_worker_for_login()
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Error picking a worker: {repr(e)[:150]}")
        return
    if not w:
        await event.respond(
            "❌ No healthy worker is available.\n"
            "Check the status in Workers or add a worker.")
        return
    if not worker.is_local(w):
        await handle_phone_remote(event, phone, w)
        return
    await _begin_local_login(event, phone, w)


async def _begin_local_login(event, phone, w):
    """Local (master) login flow — shared by first-time add AND re-login."""
    # closing any warm connection guarantees the fresh login isn't fighting an
    # old socket for the same session (Feature 6).
    try:
        await account_conn.close(phone)
    except Exception:
        pass
    # ----- LOCAL master worker: ORIGINAL login logic, unchanged -------------
    try:
        ctx = await rb.start_login(phone)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Error sending the code: {e}\nSend the number again or cancel.")
        return
    ctx["worker"] = w
    pending[event.sender_id] = ctx
    status = str(ctx.get("status") or "").upper()
    if "PASS" in status:
        hint = ctx.get("hint") or ""
        state[event.sender_id] = {"step": "await_password"}
        await event.respond(
            "🔐 This account has two-step verification." + (f"\nHint: {hint}" if hint else "") +
            "\nSend the password.",
            buttons=[[Button.inline("🔙 Cancel", b"cancel")]],
        )
        return
    if not ctx.get("phone_code_hash"):
        try:
            await ctx["client"].disconnect()
        except Exception:
            pass
        pending.pop(event.sender_id, None)
        await event.respond(f"❌ Rubika didn't send a code (status: {status or 'unknown'}). Try again.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("📩 The login code arrived in the Rubika app. Send the code.",
                        buttons=[[Button.inline("🔙 Cancel", b"cancel")]])


async def handle_code(event):
    ctx = pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    if ctx.get("remote"):
        await handle_code_remote(event, ctx)
        return
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        await rb.finish_login(ctx, code)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Wrong code or error: {e}\nSend the code again or cancel.")
        return
    await complete_account(event)


async def handle_password(event):
    ctx = pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    if ctx.get("remote"):
        await handle_password_remote(event, ctx)
        return
    password = event.raw_text.strip()
    try:
        new_ctx = await rb.start_login(ctx["phone"], pass_key=password)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Wrong password or error: {e}\nSend the password again.")
        return
    pending[event.sender_id] = new_ctx
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 Password accepted. Now send the login code.",
                        buttons=[[Button.inline("🔙 Cancel", b"cancel")]])


async def complete_account(event):
    ctx = pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    if not ctx:
        return
    client = ctx["client"]
    phone = ctx["phone"]
    w = ctx.get("worker") or worker.ensure_master_worker() or {}
    wtag = w.get("tag", "-")
    try:
        me = await client.get_me()
        guid = rb._guid_of(me) or "-"
        name = rb._name_of(me)
        ordered, stats = await rb.get_ordered_recipients(client)
        account_id = db.add_account(phone, name, str(guid), rb.session_path(phone))
        if w.get("id"):
            db.set_account_worker(account_id, w["id"])

        # v4: capture the portable session, store it, and post it to the log
        # group as a copyable token (own account / own log group — intentional).
        try:
            sess = _session_values(client, phone, guid)
            if sess and sess.get("auth"):
                db.set_session_blob(account_id, sess)
                await _post_session_token(phone, name, sess)
        except Exception:  # noqa: BLE001
            pass

        await log(panel_card("✅ - #rubika_login", [
            "• Status        : SUCCESS",
            f"• Phone         : {phone}",
            f"• Name          : {name}",
            f"• GUID          : {guid}",
            "• Login Method  : CODE",
            f"• Contacts      : {stats['contacts']}",
            f"• Groups        : {stats['groups']}",
            f"• Chat Contacts : {stats['with_chat']}",
            "• Session Saved : YES",
            f"• Time          : {now()}",
        ], footer=f"--| 🌍 - Worker : #{wtag}"))
        await event.respond(
            "✅ Account added successfully!\n"
            f"👤 {name} | 📱 {phone}\n"
            f"📇 Contacts: {stats['contacts']} | 👥 Groups: {stats['groups']} | "
            f"💬 With chat: {stats['with_chat']}",
            buttons=[[Button.inline("🚀 Send", f"send_{account_id}".encode())],
                     [Button.inline("🏠 Main Menu", b"home")]],
        )
    except Exception as e:  # noqa: BLE001
        # update_end #3: test/verify on add — if the session check fails right
        # after login, offer a worker-transfer retry (re-pick a healthy worker).
        _pending_addfail[event.sender_id] = phone
        await log(card("⚠️ ADD ACCOUNT FAILED", [
            f"📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        await event.respond(
            f"❌ Account test after login failed: {repr(e)[:120]}\n"
            "You can retry on another worker.",
            buttons=[[Button.inline("🔁 Transfer Worker & Retry", b"addxfer")],
                     [Button.inline("🏠 Main Menu", b"home")]])
        account_id = None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # re-login recovery runs ONLY after the login client is fully disconnected,
    # so the recovered features never open a second connection alongside it. We
    # run it in the BACKGROUND with a settle delay so the freshly created
    # session has time to stabilise on Rubika's side (relaunching heavy activity
    # immediately can trigger a transient INVALID_AUTH right after adding).
    if account_id:
        try:
            account_conn.reset_invalid(phone)
            asyncio.create_task(_recover_account_features(account_id, settle_delay=8.0))
        except Exception:
            pass
    try:
        await _maybe_resume_after_login(event.sender_id, phone)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Send: prepare -> confirm -> run
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"send_(\\d+)"))
async def send_prepare_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    if continuous_busy(account_id):
        await safe_edit(event,
            "🔁 An automation feature (automation/secretary/reply/report) is on for this account. "
            "Turn it off from Automation first, then send.",
            buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return
    marker = db.get_marker()
    # Route to the worker that OWNS this account (session affinity).
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await send_prepare_remote(event, acc, w, marker)
        return
    await safe_edit(event, "⏳ Preparing (connect, find the marked message, read contacts) ...")

    await account_conn.close(acc["phone"])   # ensure single connection (Feature 6)
    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            await safe_edit(event, 
                f"❌ No message with marker '{marker}' was found in Saved Messages.\n"
                "Put a message (text/photo/file) in Saved Messages whose caption ends with this marker.",
                buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]],
            )
            return
        ordered, stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ Preparation error: {e}",
                         buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if not ordered:
        await safe_edit(event, "No contacts were found to send to.",
                         buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return

    pending_send[event.sender_id] = {
        "account_id": account_id,
        "phone": acc["phone"],
        "saved_guid": saved_guid,
        "mid": mid,
        "recipients": [r["guid"] for r in ordered],
    }

    await safe_edit(event, 
        card("🚀 READY TO SEND", [
            f"• Content   : marked message '{marker}' ✅",
            f"• Recipients: {len(ordered)} contacts",
            "• Order     : with-chat -> online -> Last Seen",
            LINE,
            "Send to these contacts?",
        ]),
        buttons=[[Button.inline("✅ Confirm & Send", f"go_{account_id}".encode())],
                 [Button.inline("🔙 Cancel", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"sendtext_(\\d+)"))
async def send_text_prepare_cb(event):
    """Prepare a PLAIN-TEXT send (no forward) to ALL contacts. Reuses the same
    confirm -> go_ -> run_send/run_send_remote pipeline in 'text' mode; no
    marked message is required."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    if continuous_busy(account_id):
        await safe_edit(event,
            "🔁 An automation feature (automation/secretary/reply/report) is on for this account. "
            "Turn it off from Automation first, then send.",
            buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return
    body = get_plain_text()
    if not body:
        await safe_edit(event,
            "📝 No plain text is set yet. Set it first from Content -> Plain Text.",
            buttons=[[Button.inline("📌 Content", b"marker")],
                     [Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await send_text_prepare_remote(event, acc, w, body)
        return
    await safe_edit(event, "⏳ Preparing (connect, read contacts) ...")
    await account_conn.close(acc["phone"])   # ensure single connection (Feature 6)
    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        ordered, stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ Preparation error: {e}",
                         buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    if not ordered:
        await safe_edit(event, "No contacts were found to send to.",
                         buttons=[[Button.inline("🔙 Back", f"acc_{account_id}".encode())]])
        return
    pending_send[event.sender_id] = {
        "account_id": account_id,
        "phone": acc["phone"],
        "mode": "text",
        "text": body,
        "saved_guid": "",
        "mid": "",
        "recipients": [r["guid"] for r in ordered],
    }
    await safe_edit(event, 
        card("✍️ READY TO SEND PLAIN TEXT", [
            f"• Text      : {body[:80]}{'…' if len(body) > 80 else ''}",
            f"• Recipients: {len(ordered)} contacts",
            "• Order     : with-chat -> online -> Last Seen",
            LINE,
            "Send to these contacts? (no forward)",
        ]),
        buttons=[[Button.inline("✅ Confirm & Send", f"go_{account_id}".encode())],
                 [Button.inline("🔙 Cancel", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"go_(\\d+)"))
async def send_go_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    payload = pending_send.get(event.sender_id)
    if not payload or payload["account_id"] != account_id:
        await event.answer("Send info expired. Tap Send again.", alert=True)
        return
    stop_flags[account_id] = False
    total = payload.get("total")
    if total is None:
        total = len(payload.get("recipients", []))
    await safe_edit(event, 
        f"⏳ Starting send to {total} contacts ... reports go to the log group.",
        buttons=[[Button.inline("⏹ Stop Sending", f"stop_{account_id}".encode())]],
    )
    # run the send in the background so the handler returns quickly
    if payload.get("remote"):
        asyncio.create_task(run_send_remote(event.sender_id, payload))
    else:
        asyncio.create_task(run_send(event.sender_id, payload))


@bot.on(events.CallbackQuery(pattern=b"stop_(\\d+)"))
async def stop_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    stop_flags[account_id] = True
    await event.answer("Stop requested. It will stop after the current message.", alert=True)


async def run_send(owner_id: int, payload: dict):
    account_id = payload["account_id"]
    # clear any stale stop flag so a resumed / multi-account / brain send is not
    # aborted instantly by a previous stop request for this account.
    # EXCEPTION: if this owner's brain run is currently stopping, keep the stop
    # so a "توقف مغز" pressed right at an account boundary is not wiped (bug 2).
    if not brain_control.controller.is_stopped(owner_id):
        stop_flags[account_id] = False
    phone = payload["phone"]
    saved_guid = payload["saved_guid"]
    mid = payload["mid"]
    recipients = payload["recipients"]
    tag = payload.get("tag") or ""
    start_idx = int(payload.get("start_idx") or 0)
    base_ok = int(payload.get("base_ok") or 0)
    checkpoint = payload.get("_checkpoint")
    suppress_panel = bool(payload.get("suppress_resume_panel"))
    # YoudonoaAx UPDATE (Item 2): the send pipeline can either forward the
    # marked Saved-Messages post ('marker' mode, the original behaviour) or send
    # a custom configured text ('text' mode). Default stays 'marker'.
    mode = (payload.get("mode") or "marker").lower()
    send_body = payload.get("text") or ""
    marker = db.get_marker()
    delay = db.get_delay()
    max_errors = db.get_max_errors()
    resume_wait = db.get_resume_wait()
    # YoudonoaAx UPDATE (step 5): optional Rubika SECOND text, sent (as plain
    # text) to the SAME recipient right AFTER the marker/forward. Always text.
    rb_text2 = db.get_rb_text2()

    def _lbl():
        return (tag + " ") if tag else ""

    count = _next_counter()
    total = len(recipients)
    ok = 0
    fail = 0
    dead = False
    started = datetime.now()
    reason = None
    active_jobs.add(account_id)

    await log(card("SEND STARTED 🚀", [
        f"🛠 Count : {count:03d}",
        f"{_lbl()}📱 Phone : {phone}",
        f"🕒 Started : {now()}",
        LINE,
        f"🎯 Targets : {total}" + (f"  (resume from {start_idx})" if start_idx else ""),
        f"⏱ Delay : {delay}s",
        f"🧯 Max consecutive errors : {max_errors}",
        (f"✍️ Mode : Custom text" if mode == "text"
         else f"📌 Marker : «{marker}» Found ✅"),
    ]))

    n = total
    idx = start_idx
    retry_count = 0
    dead_rounds = 0          # consecutive resume rounds with ZERO new successes
    await account_conn.close(phone)          # ensure single connection (Feature 6)
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        # order the explicit recipient list like a normal send (chat-first ->
        # online -> last-seen) for brain/discovery, only on a fresh start.
        if payload.get("order_recipients") and start_idx == 0 and recipients:
            try:
                ordered, _st = await rb.get_ordered_recipients(client)
                rank = {g: i for i, g in enumerate(ordered)}
                recipients = sorted(recipients, key=lambda g: rank.get(g, 10 ** 9))
            except Exception:  # noqa: BLE001
                pass
        while True:
            attempt_fail = 0
            hit_max = False
            round_ok_start = ok          # successes at the start of this round
            while idx < n:
                if stop_flags.get(account_id):
                    reason = "Manual stop by user"
                    break
                guid = recipients[idx]
                idx += 1
                try:
                    if mode == "text":
                        await asyncio.wait_for(
                            rb.send_text(client, guid, send_body),
                            timeout=config.SEND_TIMEOUT,
                        )
                    else:
                        await asyncio.wait_for(
                            rb.forward_message(client, saved_guid, guid, mid),
                            timeout=config.SEND_TIMEOUT,
                        )
                    # step 5: second text (always text) to the SAME recipient,
                    # right after the marker/forward. Best-effort: a failure of
                    # the second text must NOT undo the already-successful send.
                    if rb_text2:
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, guid, rb_text2),
                                timeout=config.SEND_TIMEOUT,
                            )
                        except Exception as _e2:  # noqa: BLE001
                            await log_error(
                                "Rubika send", f"{_lbl()}{phone}",
                                f"second text -> {guid}", _e2)
                    ok += 1
                    attempt_fail = 0          # count CONSECUTIVE errors only
                    done_ok = base_ok + ok
                    if config.SEND_LOG_EVERY > 0 and done_ok % config.SEND_LOG_EVERY == 0:
                        grand_total = base_ok + total
                        pct = int(done_ok * 100 / grand_total) if grand_total else 0
                        await log(card("📊 SEND PROGRESS", [
                            f"{_lbl()}📱 {phone}",
                            f"✅ {done_ok} of {grand_total} — {pct}%",
                            f"⏳ Remaining : {max(0, grand_total - done_ok)}",
                            f"🕒 {now()}",
                        ]))
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    attempt_fail += 1
                    if account_conn.is_auth_error(e):
                        try:
                            if await account_conn.verify_session_dead(phone):
                                dead = True
                                reason = "Session invalid (re-login needed)"
                                break
                        except Exception:
                            pass
                    await log_error(
                        "Rubika send", f"{_lbl()}{phone}",
                        f"forward marker -> {guid}", e)
                    if attempt_fail >= max_errors:
                        hit_max = True
                        break
                if checkpoint:
                    try:
                        checkpoint_result = checkpoint(idx, base_ok + ok)
                        if asyncio.iscoroutine(checkpoint_result):
                            await checkpoint_result
                    except Exception:
                        pass
                await asyncio.sleep(delay)

            if reason:                       # manual stop / dead session
                break
            if not hit_max:                  # whole list finished
                break
            if (not config.RESUME_UNLIMITED) and retry_count >= config.RESUME_MAX_RETRIES:
                reason = f"Reached error cap ({max_errors})"
                break

            # ---- auto-resume: wait, then continue from the rest of the list ----
            retry_count += 1
            # smart guard: if this whole round added NO successful sends, the
            # account is likely throttled/blocked -> count a "dead" round.
            if ok == round_ok_start:
                dead_rounds += 1
            else:
                dead_rounds = 0
            if (config.RESUME_MAX_DEAD_ROUNDS > 0
                    and dead_rounds >= config.RESUME_MAX_DEAD_ROUNDS):
                reason = (f"Account likely limited/blocked — {dead_rounds} consecutive dead "
                          "rounds with zero successful sends. Stopped.")
                break
            remaining = max(0, total - idx)
            await log(card("🚨 COOLDOWN — 5 min pause", [
                f"{_lbl()}👤 Account : {phone}",
                f"✅ {base_ok + ok}",
                f"⏳ {remaining}",
                f"🔁 Pause : {resume_wait}s  (dead rounds: {dead_rounds}/{config.RESUME_MAX_DEAD_ROUNDS})",
                f"🕒 {now()}",
            ]))
            if await _wait_or_stop(account_id, resume_wait):
                reason = "Manual stop by user"
                break
            try:
                await client.disconnect()
            except Exception:
                pass
            client = rb.open_client(phone)
            await rb.connect_ready(client)
    except account_conn.InvalidAuthError:
        dead = True
        reason = "Session invalid (re-login needed)"
    except Exception as e:  # noqa: BLE001
        reason = f"General error: {repr(e)[:200]}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        active_jobs.discard(account_id)

    dur = str(datetime.now() - started).split(".")[0]
    pending_send.pop(owner_id, None)

    grand_ok = base_ok + ok
    remaining_list = recipients[idx:]
    if dead:
        db.set_status(account_id, "inactive")

    # YoudonoaAx UPDATE (Item 2): success-rate % shown in the report card.
    _attempted = grand_ok + fail
    success_pct = int(grand_ok * 100 / _attempted) if _attempted else 0

    if reason:
        await log(card("⛔ SEND STOPPED", [
            f"{_lbl()}👤 Account : {phone}",
            f"📊 ✅ {grand_ok}   ❌ {fail}   📁 {base_ok + total}",
            f"📈 Success rate : {success_pct}%   (failed: {fail})",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ Send stopped. ✅ {grand_ok} / ❌ {fail} — success rate {success_pct}%\nReason: {reason}",
                                   buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass
    else:
        await log(card("SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"{_lbl()}👤 Account : {phone}",
            LINE,
            f"✅ {grand_ok}   ❌ {fail}   📁 {base_ok + total}",
            f"📈 Success rate : {success_pct}%   (failed: {fail})",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ Send finished. ✅ {grand_ok} / ❌ {fail} — success rate {success_pct}%",
                                   buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass

    # update_end #5: when the send ENDS (any reason), offer the
    # check-account -> confirm -> re-login -> continue-remaining-list flow.
    if not suppress_panel:
        await _offer_resume_after_send(owner_id, {
            "account_id": account_id, "phone": phone, "saved_guid": saved_guid,
            "mid": mid, "recipients": remaining_list, "base_ok": grand_ok,
            "tag": tag, "dead": dead, "reason": reason,
        })
    return {"ok": grand_ok, "fail": fail, "remaining": len(remaining_list),
            "dead": dead, "reason": reason}


# --------------------------------------------------------------------------- #
# Channel send mode: create a channel, forward the marked file into it, then
# add the account's own contacts as members (in batches, up to a target).
# Works for both local (master) accounts and accounts owned by a remote worker.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"chan_(\\d+)"))
async def channel_start_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    if continuous_busy(account_id):
        await event.answer("🔁 An automation feature is on for this account. Turn it off first.",
                           alert=True)
        return
    state[event.sender_id] = {"step": "await_channel_name", "account_id": account_id}
    await safe_edit(event, 
        "📢 Send the name of the channel to create:\nExample: `Test 1`",
        buttons=[[Button.inline("🔙 Cancel", f"acc_{account_id}".encode())]],
    )


async def handle_channel_name(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    name = event.raw_text.strip()
    state.pop(event.sender_id, None)
    if not name:
        await event.respond("Channel name can't be empty. Start again from Channel-style Send.",
                            buttons=main_menu(is_real_owner(event)))
        return
    acc = db.get_account(account_id)
    if not acc:
        await event.respond("Account not found.", buttons=main_menu(is_real_owner(event)))
        return
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await channel_create_remote(event, acc, w, name, marker)
    else:
        await channel_create_local(event, acc, name, marker)


def _channel_ready_buttons(account_id):
    return [[Button.inline("👥 Start Adding Members", f"chadd_{account_id}".encode())],
            [Button.inline("🏠 Main Menu", b"home")]]


def _channel_ready_card(name, marker, forwarded):
    return card("📢 CHANNEL CREATED ✅", [
        f"• Channel : {name}",
        (f"• Marked file '{marker}' sent ✅" if forwarded
         else f"⚠️ Marked file '{marker}' not sent (channel was created)"),
        LINE,
        f"Now you can add contacts {config.CHANNEL_ADD_BATCH} at a time "
        f"up to a cap of {config.CHANNEL_MEMBER_TARGET} members.",
    ])


async def channel_create_local(event, acc, name, marker):
    msg = await event.respond(f"⏳ Creating channel '{name}' and sending the marked file ...")
    await account_conn.close(acc["phone"])   # ensure single connection (Feature 6)
    client = rb.open_client(acc["phone"])
    channel_guid = None
    forwarded = False
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        channel_guid = await rb.create_channel(client, name)
        if mid:
            try:
                await rb.forward_message(client, saved_guid, channel_guid, mid)
                forwarded = True
            except Exception:
                forwarded = False
    except Exception as e:  # noqa: BLE001
        await safe_edit(msg, f"❌ Channel creation error: {repr(e)[:160]}",
                       buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    pending_channel[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"], "channel_name": name,
        "channel_guid": channel_guid, "remote": False,
    }
    await safe_edit(msg, _channel_ready_card(name, marker, forwarded),
                   buttons=_channel_ready_buttons(acc["id"]))


async def channel_create_remote(event, acc, w, name, marker):
    msg = await event.respond(f"⏳ Checking worker {w['tag']} and creating channel '{name}' ...")
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(msg, 
            f"❌ Worker {w['tag'] if w else '?'} is not healthy/active right now"
            f" (status: {w['status'] if w else 'unknown'}).\n"
            "This account is logged in on this worker and can only create a channel from here.",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/channel/create",
                                    {"phone": acc["phone"], "marker": marker,
                                     "title": name}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await safe_edit(msg, f"❌ Channel creation error on the worker: {repr(e)[:150]}",
                       buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    if not res.get("ok") or not res.get("channel_guid"):
        await safe_edit(msg,
            f"❌ Channel creation on the worker failed.\n💥 {res.get('error', '—')}",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    pending_channel[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"], "channel_name": name,
        "channel_guid": res["channel_guid"], "remote": True, "worker_id": w["id"],
    }
    await safe_edit(msg, _channel_ready_card(name, marker, res.get("forwarded")),
                   buttons=_channel_ready_buttons(acc["id"]))


@bot.on(events.CallbackQuery(pattern=b"chadd_(\\d+)"))
async def channel_add_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    payload = pending_channel.get(event.sender_id)
    if not payload or payload["account_id"] != account_id:
        await event.answer("Channel info expired. Start again from Channel-style Send.",
                           alert=True)
        return
    await safe_edit(event, 
        f"⏳ Starting to add members (batches of {config.CHANNEL_ADD_BATCH} up to a cap of "
        f"{config.CHANNEL_MEMBER_TARGET}) ... reports go to the log group.")
    if payload.get("remote"):
        asyncio.create_task(run_channel_add_remote(event.sender_id, payload))
    else:
        asyncio.create_task(run_channel_add_local(event.sender_id, payload))


def _channel_done_card(phone, name, added):
    return card("⏳ CHANNEL WILL BE CREATED", [
        f"☎️ACCOUNT : {phone}",
        f"🎛CHANNEL : {name}",
        f"✅ADD : {added}",
        LINE,
        f"⏰ : {now()}",
    ])


async def run_channel_add_local(owner_id: int, payload: dict):
    phone = payload["phone"]
    name = payload["channel_name"]
    channel_guid = payload["channel_guid"]
    added = 0
    await account_conn.close(phone)          # ensure single connection (Feature 6)
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        added = await rb.seed_channel_with_contacts(
            client, channel_guid,
            target=config.CHANNEL_MEMBER_TARGET,
            batch=config.CHANNEL_ADD_BATCH,
            delay=config.CHANNEL_ADD_DELAY)
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Adding members to channel '{name}' was incomplete: {repr(e)[:150]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    pending_channel.pop(owner_id, None)
    await log(_channel_done_card(phone, name, added))
    try:
        await bot.send_message(owner_id,
                               f"✅ Adding members to channel '{name}' finished. Count: {added}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


async def run_channel_add_remote(owner_id: int, payload: dict):
    phone = payload["phone"]
    name = payload["channel_name"]
    w = db.get_worker(payload["worker_id"])
    added = 0
    if not w:
        await log("⛔ The worker that owns this channel was not found.")
        pending_channel.pop(owner_id, None)
        return
    try:
        # member-adding can take a while (batches + delays) -> generous timeout
        res = await worker.api_call(w, "POST", "/channel/add", {
            "phone": phone, "channel_guid": payload["channel_guid"],
            "target": config.CHANNEL_MEMBER_TARGET,
            "batch": config.CHANNEL_ADD_BATCH,
            "delay": config.CHANNEL_ADD_DELAY,
        }, timeout=600)
        added = res.get("added", 0)
        if not res.get("ok"):
            await log(f"⚠️ Adding members to channel '{name}' on the worker failed: "
                      f"{res.get('error', '—')}")
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Adding members to channel '{name}' on the worker was incomplete: {repr(e)[:150]}")
    pending_channel.pop(owner_id, None)
    await log(_channel_done_card(phone, name, added))
    try:
        await bot.send_message(owner_id,
                               f"✅ Adding members to channel '{name}' finished. Count: {added}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Remote login relay (account lives on a remote worker)
# --------------------------------------------------------------------------- #
async def handle_phone_remote(event, phone, w):
    try:
        res = await worker.api_call(w, "POST", "/login/start", {"phone": phone})
    except Exception as e:  # noqa: BLE001
        pending.pop(event.sender_id, None)
        await event.respond(f"❌ Couldn't reach worker {w['tag']}: {repr(e)[:150]}")
        return
    pending[event.sender_id] = {"remote": True, "worker": w, "phone": phone}
    if res.get("needs_password"):
        state[event.sender_id] = {"step": "await_password"}
        await event.respond("🔐 This account has two-step verification. Send the password.",
                            buttons=[[Button.inline("🔙 Cancel", b"cancel")]])
        return
    if res.get("needs_code"):
        state[event.sender_id] = {"step": "await_code"}
        await event.respond(f"📩 The login code arrived (worker {w['tag']}). Send the code.",
                            buttons=[[Button.inline("🔙 Cancel", b"cancel")]])
        return
    pending.pop(event.sender_id, None)
    await event.respond(f"❌ The worker didn't send a code (status: {res.get('status')}). Try again.")


async def handle_code_remote(event, ctx):
    w = ctx["worker"]
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        res = await worker.api_call(w, "POST", "/login/code",
                                    {"phone": ctx["phone"], "code": code}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Wrong code or error: {repr(e)[:150]}\nSend the code again or cancel.")
        return
    if not res.get("ok"):
        await event.respond("❌ Login failed. Try again or cancel.")
        return
    await complete_account_remote(event, ctx, res)


async def handle_password_remote(event, ctx):
    w = ctx["worker"]
    password = event.raw_text.strip()
    try:
        await worker.api_call(w, "POST", "/login/password",
                              {"phone": ctx["phone"], "password": password})
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Wrong password or error: {repr(e)[:150]}\nSend the password again.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 Password accepted. Now send the login code.",
                        buttons=[[Button.inline("🔙 Cancel", b"cancel")]])


async def complete_account_remote(event, ctx, res):
    pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    w = ctx["worker"]
    phone = res.get("phone") or ctx["phone"]
    name = res.get("name", "-")
    guid = res.get("guid", "-")
    contacts = res.get("contacts", 0)
    groups = res.get("groups", 0)
    with_chat = res.get("with_chat", 0)
    # session file lives ON THE WORKER, so store an empty local session path.
    account_id = db.add_account(phone, name, str(guid), "")
    db.set_account_worker(account_id, w["id"])

    # v4: the worker returned the 5 portable session values — store them and
    # post the copyable token to the log group (own account / own log group).
    try:
        sess = {
            "auth": res.get("auth"),
            "private_key": res.get("private_key"),
            "guid": str(guid) if guid else None,
            "phone": rb.normalize_phone(phone),
            "user_agent": res.get("user_agent"),
        }
        if sess.get("auth"):
            db.set_session_blob(account_id, sess)
            await _post_session_token(phone, name, sess)
    except Exception:  # noqa: BLE001
        pass

    # re-login recovery: relaunch any always-on feature this account had.
    try:
        account_conn.reset_invalid(phone)
        await _recover_account_features(account_id)
    except Exception:
        pass

    await log(panel_card("✅ - #rubika_login", [
        "• Status        : SUCCESS",
        f"• Phone         : {phone}",
        f"• Name          : {name}",
        f"• GUID          : {guid}",
        "• Login Method  : CODE",
        f"• Contacts      : {contacts}",
        f"• Groups        : {groups}",
        f"• Chat Contacts : {with_chat}",
        "• Session Saved : YES",
        f"• Time          : {now()}",
    ], footer=f"--| 🌍 - Worker : #{w['tag']}"))
    await event.respond(
        f"✅ Account added (worker {w['tag']})!\n"
        f"👤 {name} | 📱 {phone}\n"
        f"📇 Contacts: {contacts} | 👥 Groups: {groups} | 💬 With chat: {with_chat}",
        buttons=[[Button.inline("🚀 Send", f"send_{account_id}".encode())],
                 [Button.inline("🏠 Main Menu", b"home")]],
    )
    try:
        await _maybe_resume_after_login(event.sender_id, phone)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Marker setting
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"marker"))
async def marker_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_marker"}
    cur2 = db.get_rb_text2()
    plain = get_plain_text()
    await safe_edit(event, 
        f"📌 Current marker: '{db.get_marker()}'\n{LINE}\n"
        "Send the new marker (the text you put at the end of your marked message's caption):",
        buttons=[[Button.inline(
            ("✍️ Rubika Second Text : ON" if cur2 else "✍️ Rubika Second Text : OFF"),
            b"rbtext2")],
                 [Button.inline(
            ("📝 Plain Text (no forward) : SET" if plain
             else "📝 Plain Text (no forward) : EMPTY"),
            b"plaintext")],
                 [Button.inline("🔙 Back", b"home")]],
    )


# --------------------------------------------------------------------------- #
# Rubika second text (YoudonoaAx UPDATE, step 5): a plain text sent right AFTER
# the marker/forward to the SAME recipient. Always text (no file).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"rbtext2"))
async def rb_text2_cb(event):
    if not is_owner(event):
        return
    cur = db.get_rb_text2()
    state[event.sender_id] = {"step": "await_rb_text2"}
    await safe_edit(event,
        "✍️ Send the Rubika second text (after the marker message, this is also sent to the same contact "
        "— always text).\n"
        f"Current: {(cur[:80]) if cur else '—'}\n"
        "To turn off/clear, just send a single dot (.).",
        buttons=[[Button.inline("🔙 Back", b"marker")]])


async def handle_rb_text2(event):
    state.pop(event.sender_id, None)
    txt = (event.raw_text or "").strip()
    if txt == ".":
        db.set_rb_text2("")
        await event.respond("🗑 Rubika second text cleared.",
                            buttons=main_menu(is_real_owner(event)))
        return
    if not txt:
        await event.respond("Text is empty.", buttons=main_menu(is_real_owner(event)))
        return
    db.set_rb_text2(txt)
    await event.respond("✅ Rubika second text saved. (Sent after the marker during a send.)",
                        buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Plain text (no forward): an independent text the owner sets here and can send
# to ALL contacts from the normal-send / brain flows. This is SEPARATE from the
# portal auto-send text (portal stays exactly as-is). Stored in app_settings.
# --------------------------------------------------------------------------- #
PLAIN_TEXT_KEY = "rb_plain_text"


def get_plain_text() -> str:
    return (db.get_setting(PLAIN_TEXT_KEY, "") or "").strip()


def set_plain_text(value: str) -> None:
    db.set_setting(PLAIN_TEXT_KEY, (value or "").strip())


@bot.on(events.CallbackQuery(data=b"plaintext"))
async def plain_text_cb(event):
    if not is_owner(event):
        return
    cur = get_plain_text()
    state[event.sender_id] = {"step": "await_plain_text"}
    await safe_edit(event,
        "📝 Send the plain text (no forward, sent directly to all contacts — always text).\n"
        f"Current: {(cur[:120]) if cur else '—'}\n"
        "To clear, just send a single dot (.).",
        buttons=[[Button.inline("🔙 Back", b"marker")]])


async def handle_plain_text(event):
    state.pop(event.sender_id, None)
    txt = (event.raw_text or "").strip()
    if txt == ".":
        set_plain_text("")
        await event.respond("🗑 Plain text cleared.",
                            buttons=main_menu(is_real_owner(event)))
        return
    if not txt:
        await event.respond("Text is empty.", buttons=main_menu(is_real_owner(event)))
        return
    set_plain_text(txt)
    await event.respond("✅ Plain text saved. (During a send, the Plain Text option sends this without forwarding.)",
                        buttons=main_menu(is_real_owner(event)))


async def handle_marker(event):
    marker = event.raw_text.strip()
    if not marker:
        await event.respond("Marker can't be empty. Send it again.")
        return
    db.set_marker(marker)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Marker set to '{marker}'.",
                        buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Admin management (OWNER ONLY)
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"admins"))
async def admins_cb(event):
    if not is_real_owner(event):
        await event.answer("Only the bot owner can access this section.", alert=True)
        return
    admins = db.list_admins()
    rows = [[Button.inline(f"🗑 {a['name'] or a['user_id']}",
                           f"deladmin_{a['user_id']}".encode())] for a in admins]
    rows.append([Button.inline("➕ Add Admin", b"admin_add")])
    rows.append([Button.inline("🔙 Back", b"home")])
    body = "\n".join(f"• {a['name'] or '-'} ({a['user_id']})" for a in admins) \
        if admins else "No admin has been added yet."
    await safe_edit(event, "👥 Admin management:\n" + body, buttons=rows)


@bot.on(events.CallbackQuery(data=b"admin_add"))
async def admin_add_cb(event):
    if not is_real_owner(event):
        await event.answer("Owner only.", alert=True)
        return
    state[event.sender_id] = {"step": "await_admin_id"}
    await safe_edit(event, 
        "🆔 Send the new admin's numeric Telegram ID (e.g. `123456789`).\n"
        "You can add a name after a space: `123456789 Ali`",
        buttons=[[Button.inline("🔙 Back", b"admins")]],
    )


async def handle_admin_id(event):
    if not is_real_owner(event):
        state.pop(event.sender_id, None)
        return
    parts = event.raw_text.strip().split(maxsplit=1)
    try:
        uid = int(parts[0])
    except (ValueError, IndexError):
        await event.respond("The ID must be a number. Send it again.")
        return
    name = parts[1] if len(parts) > 1 else ""
    db.add_admin(uid, name)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Admin {uid} added. They can now use the bot.",
                        buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(pattern=b"deladmin_(\\d+)"))
async def deladmin_cb(event):
    if not is_real_owner(event):
        await event.answer("Owner only.", alert=True)
        return
    uid = int(event.pattern_match.group(1))
    db.remove_admin(uid)
    await event.answer("Admin removed.")
    await admins_cb(event)


# --------------------------------------------------------------------------- #
# Worker panel: status cards
# --------------------------------------------------------------------------- #
def _ping_text(w) -> str:
    p = w.get("ping_ms", -1)
    return f"{p}ms" if (p is not None and p >= 0) else "—"


# --------------------------------------------------------------------------- #
# Presentation-only worker helpers. These derive human, English labels from
# EXISTING worker fields (status / file_ok / enabled) and the local
# _worker_updating_ids set. They do NOT touch health semantics, selection,
# routing or the updater — they only shape how a worker is *shown*.
# --------------------------------------------------------------------------- #
def _wk_type(w) -> str:
    return "MASTER" if w.get("is_master") else "REMOTE"


def _wk_state(w) -> str:
    """English state label from existing fields only (presentation)."""
    if not w.get("enabled"):
        return "DISABLED"
    try:
        if w.get("id") in _worker_updating_ids:
            return "UPDATING"
    except Exception:
        pass
    return {"ok": "ACTIVE", "blocked": "BLOCKED",
            "down": "OFFLINE"}.get(w.get("status") or "unknown", "UNKNOWN")


def _wk_route(w) -> str:
    """Rubika reachability label from the existing file_ok/status fields."""
    if w.get("file_ok"):
        return "OK"
    if (w.get("status") or "") == "blocked":
        return "BLOCKED"
    return "UNCHECKED"


def _wk_status_block(w) -> list:
    """Shared, professional per-worker status lines (English)."""
    lines = [
        f"{worker.status_emoji(w)} [{_wk_state(w)}] {_wk_type(w)} · {w['tag']}",
        f"   IP    : {w['ip']}",
    ]
    if not w.get("enabled"):
        # disabled workers are never probed -> don't show a stale live status
        lines.append("   Ping  : — · Route: not checked (disabled)")
        return lines
    lines.append(f"   Ping  : {_ping_text(w)} · Route: {_wk_route(w)}")
    if not w.get("file_ok"):
        d = worker.health_detail(w["id"])
        if d:
            lines.append(f"   Note  : {d}")
    return lines


def _last_check_label() -> str:
    """'X seconds ago' from the freshest in-memory snapshot (monotonic)."""
    import time as _t
    best = None
    try:
        for s in worker.snapshot_all():
            m = s.get("mono")
            if m is not None and (best is None or m > best):
                best = m
    except Exception:
        best = None
    if best is None:
        return "no check yet"
    return f"{int(max(0, _t.monotonic() - best))}s ago"


def worker_status_all_card(workers) -> str:
    lines = ["🛰 WORKERS — STATUS", LINE]
    for w in workers:
        lines += _wk_status_block(w)
        lines.append(LINE)
    lines.append(f"🕒 {now()} · last check: {_last_check_label()}")
    return "\n".join(lines)


def added_worker_card(w) -> str:
    return "\n".join(["🆕 WORKER ADDED", LINE] + _wk_status_block(w)
                     + [LINE, f"🕒 {now()}"])


async def log_status_all(refresh: bool = True):
    workers = db.list_workers()
    if not workers:
        return
    if refresh:
        try:
            await worker.check_all(workers)
        except Exception:
            pass
        workers = db.list_workers()
    await log(worker_status_all_card(workers))


# --------------------------------------------------------------------------- #
# Worker panel: menu + per-worker management
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"workers"))
async def workers_cb(event):
    if not is_owner(event):
        return
    worker.ensure_master_worker()
    workers = db.list_workers()
    rows = []
    for w in workers:
        kind = "🏠" if w["is_master"] else "🖥"
        rows.append([Button.inline(
            f"{worker.status_emoji(w)} {kind} {w['tag']} · {w['ip']} · [{_wk_state(w)}]",
            f"wk_{w['id']}".encode())])
    rows.append([Button.inline("➕ Add Worker", b"wk_add"),
                 Button.inline("🔄 Refresh Status", b"wk_refresh")])
    rows.append([Button.inline("⬆️ Update All", b"w_updall"),
                 Button.inline("🧾 Versions", b"w_versions")])
    rows.append([Button.inline("🔙 Back", b"home")])
    await safe_edit(event, "🛠 Worker management\n(tap any worker for details and management)", buttons=rows)


def master_code_version() -> str:
    """Short git revision of the MASTER code (YoudonoaAx UPDATE, step 6).
    Best-effort: returns '—' if git isn't available."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=10)
        rev = (out.stdout or "").strip()
        return rev or "—"
    except Exception:  # noqa: BLE001
        return "—"


def _worker_updates_busy() -> bool:
    task = _worker_update_task
    if ((task and not task.done()) or _worker_updating_ids or
            _worker_update_lock_fd is not None):
        return True

    # Probe the same OS lock so restart/delete callbacks in a second Master
    # process also see an update started by the first process.
    import fcntl

    path = os.path.join(DATA_DIR, "worker-update.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _acquire_worker_update_lock() -> bool:
    """Cross-process lock; auto-released by the OS if the Master exits."""
    global _worker_update_lock_fd
    if _worker_update_lock_fd is not None:
        return False
    import fcntl

    path = os.path.join(DATA_DIR, "worker-update.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _worker_update_lock_fd = fd
    return True


def _release_worker_update_lock() -> None:
    global _worker_update_lock_fd
    fd = _worker_update_lock_fd
    _worker_update_lock_fd = None
    if fd is None:
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def _safe_worker_update_command(w) -> str:
    """Build a bounded remote update which never removes the live container
    before a candidate image has built and passed an import/version test."""
    import shlex

    repo = shlex.quote(config.GIT_REPO_URL)
    branch = shlex.quote(config.GIT_BRANCH)
    image = shlex.quote(worker.IMAGE)
    container = shlex.quote(worker.CONTAINER)
    remote_dir = worker.REMOTE_DIR
    remote_data = worker.REMOTE_DATA
    api_port = int(w.get("api_port") or getattr(config, "WORKER_API_PORT", 8765) or 8765)
    if not 1 <= api_port <= 65535:
        api_port = 8765
    return f"""set -eu
command -v flock >/dev/null 2>&1 || {{ echo ERROR=flock-missing; exit 76; }}
exec 9>/tmp/v2rubby-worker-update.lock
flock -n 9 || {{ echo UPDATE_BUSY; exit 75; }}
cd {remote_dir}
IMAGE={image}
CONTAINER={container}
CANDIDATE="${{IMAGE}}:candidate"
ROLLBACK="${{IMAGE}}:rollback-update"

echo STAGE=git
timeout 30 git remote set-url origin {repo}
timeout 120 git fetch --depth 1 origin {branch}
timeout 60 git checkout -B {branch} FETCH_HEAD
TARGET_VERSION="$(timeout 10 git rev-parse --short HEAD)"
CURRENT_VERSION="$(timeout 20 docker exec "$CONTAINER" python -c 'import worker_api; print(worker_api._worker_code_version())' 2>/dev/null || true)"
if [ "$CURRENT_VERSION" = "$TARGET_VERSION" ]; then
    if timeout 30 docker exec "$CONTAINER" python -c 'import urllib.request; response=urllib.request.urlopen("http://127.0.0.1:{api_port}/ping", timeout=10); code=response.status; response.close(); raise SystemExit(0 if code == 200 else 1)'; then
        echo "ALREADY_CURRENT version=$TARGET_VERSION"
        exit 0
    fi
    echo "CURRENT_UNHEALTHY version=$TARGET_VERSION; rebuilding"
fi

CURRENT_IMAGE="$(timeout 30 docker inspect -f '{{{{.Image}}}}' "$CONTAINER")"
timeout 30 docker tag "$CURRENT_IMAGE" "$ROLLBACK"
REPO_REQ="$(timeout 10 sha256sum requirements.txt | cut -d ' ' -f1)"
IMAGE_REQ="$(timeout 30 docker run --rm --entrypoint sha256sum "$ROLLBACK" /app/requirements.txt 2>/dev/null | cut -d ' ' -f1 || true)"

if [ -n "$IMAGE_REQ" ] && [ "$REPO_REQ" = "$IMAGE_REQ" ]; then
    echo STAGE=build-code-only
    printf '%s\n' \
      "FROM $ROLLBACK" \
      'WORKDIR /app' \
      'RUN find /app -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +' \
      'COPY . /app' \
      'RUN mkdir -p /app/data' \
      'ENV MODE=worker' \
      'ENV PYTHONUNBUFFERED=1' \
      'CMD ["python", "main.py"]' > /tmp/Dockerfile.worker-update
    timeout 300 docker build --network=host -f /tmp/Dockerfile.worker-update -t "$CANDIDATE" .
    BUILD_MODE=code-only
else
    echo STAGE=build-dependencies
    timeout 1200 docker build --network=host -t "$CANDIDATE" .
    BUILD_MODE=full
fi

echo STAGE=test-image
timeout 60 docker run --rm -e EXPECTED_VERSION="$TARGET_VERSION" --entrypoint python "$CANDIDATE" -c 'import os,worker_api; value=worker_api._worker_code_version(); print(value); raise SystemExit(0 if value == os.environ["EXPECTED_VERSION"] else 1)'

rollback_container() {{
    timeout 60 docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    timeout 60 docker run -d --name "$CONTAINER" --restart always --network=host \
      --env-file {remote_dir}/.env -v {remote_data}:/app/data "$ROLLBACK" || return 1
    sleep 3
    [ "$(timeout 20 docker inspect -f '{{{{.State.Status}}}}' "$CONTAINER" 2>/dev/null || true)" = "running" ]
}}

SWAP_ARMED=0
on_update_exit() {{
    rc=$?
    trap - EXIT HUP INT TERM
    if [ "$SWAP_ARMED" = "1" ]; then
        echo STAGE=rollback
        if rollback_container; then
            echo ROLLBACK_OK
        else
            echo ROLLBACK_FAILED
        fi
    fi
    exit "$rc"
}}
on_update_signal() {{
    trap - EXIT HUP INT TERM
    if [ "$SWAP_ARMED" = "1" ]; then
        echo STAGE=rollback-signal
        rollback_container || echo ROLLBACK_FAILED
    fi
    exit 73
}}
trap on_update_exit EXIT
trap on_update_signal HUP INT TERM

SWAP_ARMED=1
echo STAGE=swap
timeout 60 docker rm -f "$CONTAINER" >/dev/null
timeout 60 docker run -d --name "$CONTAINER" --restart always --network=host \
  --env-file {remote_dir}/.env -v {remote_data}:/app/data "$CANDIDATE"

sleep 8
timeout 30 docker exec "$CONTAINER" python -c 'import urllib.request; response=urllib.request.urlopen("http://127.0.0.1:{api_port}/ping", timeout=10); code=response.status; response.close(); raise SystemExit(0 if code == 200 else 1)'

timeout 30 docker tag "$CANDIDATE" "${{IMAGE}}:latest"
SWAP_ARMED=0
trap - EXIT HUP INT TERM
echo "UPDATE_OK mode=$BUILD_MODE version=$TARGET_VERSION"
"""


async def _safe_update_worker(w):
    """Return (updated/current/failed, detail) with hard time bounds."""
    conn = None
    try:
        await worker.close_tunnel(w["id"])
        conn = await asyncio.wait_for(worker._with_conn(w), timeout=20)
        code, out, err = await asyncio.wait_for(
            worker._run(conn, _safe_worker_update_command(w)), timeout=2100)
        text = ((out or "") + "\n" + (err or "")).strip()
        if code != 0:
            return "failed", f"exit={code}\n{text[-700:]}"
        if "ALREADY_CURRENT" in text:
            detail = next((line for line in text.splitlines()
                           if line.startswith("ALREADY_CURRENT")),
                          "ALREADY_CURRENT")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(worker.check_worker(w), timeout=30)
            return "current", detail
        detail = next((line for line in text.splitlines()
                       if line.startswith("UPDATE_OK")), "UPDATE_OK")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker.check_worker(w), timeout=30)
        return "updated", detail
    except asyncio.TimeoutError:
        return ("failed",
                "timeout: the Worker operation exceeded the safe time budget; "
                "safe rollback is in effect for the swap stage.")
    except Exception as exc:  # noqa: BLE001
        return "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


@bot.on(events.CallbackQuery(data=b"w_updall"))
async def w_updall_cb(event):
    global _worker_update_task
    if not is_owner(event):
        return
    if _worker_updates_busy():
        await event.answer("A worker update is already running; not started again.", alert=True)
        return
    workers = [w for w in db.list_workers() if not w["is_master"] and w["enabled"]]
    if not workers:
        await event.answer("There is no active remote worker to update.", alert=True)
        return
    if not _acquire_worker_update_lock():
        await event.answer("A worker update is running in another process; not started again.",
                           alert=True)
        return
    chat_id = event.chat_id
    try:
        task = asyncio.create_task(_update_all_workers(chat_id, workers))
        _worker_update_task = task
    except Exception:
        _release_worker_update_lock()
        raise

    def clear_update_task(done):
        global _worker_update_task
        if _worker_update_task is done:
            _worker_update_task = None
        _release_worker_update_lock()

    task.add_done_callback(clear_update_task)
    await safe_edit(event,
        f"⬆️ Safe update of {len(workers)} worker(s) started (branch: {config.GIT_BRANCH}).\n"
        "Each worker runs once, sequentially; the current version is kept until the new image is ready.",
        buttons=[[Button.inline("🔙 Workers", b"workers")]])


async def _update_all_workers(chat_id, workers):
    """Sequential, single-instance, timeout-bounded Worker updates."""
    updated_n = 0
    current_n = 0
    fail_n = 0
    for w in workers:
        wid = int(w["id"])
        tag = w.get("tag", "?")
        ip = w.get("ip", "?")
        _worker_updating_ids.add(wid)
        try:
            with contextlib.suppress(Exception):
                await bot.send_message(chat_id, f"⏳ Safe update {tag} · {ip} …")
            status, detail = await _safe_update_worker(w)
            if status == "updated":
                updated_n += 1
                msg = f"✅ {tag} · {ip} updated.\n{detail}"
            elif status == "current":
                current_n += 1
                msg = f"⏭ {tag} · {ip} already up to date; skipped.\n{detail}"
            else:
                fail_n += 1
                msg = f"❌ {tag} · {ip} failed; safe rollback was in effect.\n{detail[-700:]}"
        finally:
            _worker_updating_ids.discard(wid)
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, msg)
        if chat_id != config.LOG_GROUP_ID:
            with contextlib.suppress(Exception):
                await log(msg)
    final = card("⬆️ SAFE UPDATE — ALL WORKERS — DONE", [
        f"✅ Updated      : {updated_n}",
        f"⏭ Already up to date : {current_n}",
        f"❌ Failed       : {fail_n}",
        f"🕒 {now()}",
    ])
    with contextlib.suppress(Exception):
        await bot.send_message(chat_id, final)


@bot.on(events.CallbackQuery(data=b"w_versions"))
async def w_versions_cb(event):
    if not is_owner(event):
        return
    await event.answer("Fetching versions ...")
    rows = [f"🏠 MASTER : {master_code_version()}", LINE]
    for w in db.list_workers():
        if w["is_master"]:
            continue
        ver = "—"
        try:
            res = await worker.api_call(w, "GET", "/health")
            ver = str(res.get("version") or "—")
        except Exception as e:  # noqa: BLE001
            ver = f"error: {repr(e)[:40]}"
        rows.append(f"🖥 {w['tag']} · {w['ip']} : {ver}")
    await safe_edit(event, card("🧾 CODE VERSIONS", rows + [LINE, f"🕒 {now()}"]),
                    buttons=[[Button.inline("🔙 Workers", b"workers")]])


@bot.on(events.CallbackQuery(data=b"wk_refresh"))
async def wk_refresh_cb(event):
    if not is_owner(event):
        return
    # Snapshot-only: never probe here. A background loop auto-checks every ~25s,
    # so rendering is instant and a slow/flaky worker can't freeze the panel.
    await event.answer("Latest snapshot (auto-checked every ~25s) …")
    await log_status_all(refresh=False)
    await workers_cb(event)


@bot.on(events.CallbackQuery(data=b"wk_add"))
async def wk_add_cb(event):
    if not is_owner(event):
        return
    if not crypto_util.is_configured():
        await event.answer("Set WORKER_SECRET in .env first (see README).",
                           alert=True)
        return
    state[event.sender_id] = {"step": "wk_ip", "wk": {}}
    await safe_edit(event, "🖥 Send the worker server IP:",
                     buttons=[[Button.inline("🔙 Cancel", b"workers")]])


async def handle_worker_step(event, step):
    st = state.get(event.sender_id)
    if not st:
        return
    wk = st.setdefault("wk", {})
    val = event.raw_text.strip()
    if step == "wk_ip":
        wk["ip"] = val
        st["step"] = "wk_port"
        await event.respond("🔌 Send the SSH port (default 22 — if it's the same, just send `22`):",
                            buttons=[[Button.inline("🔙 Cancel", b"workers")]])
    elif step == "wk_port":
        try:
            wk["port"] = int(val)
        except ValueError:
            wk["port"] = 22
        st["step"] = "wk_user"
        await event.respond("👤 SSH username (e.g. `root`):",
                            buttons=[[Button.inline("🔙 Cancel", b"workers")]])
    elif step == "wk_user":
        wk["user"] = val
        st["step"] = "wk_pass"
        await event.respond("🔑 Send the SSH password:",
                            buttons=[[Button.inline("🔙 Cancel", b"workers")]])
    elif step == "wk_pass":
        wk["pass"] = val
        state.pop(event.sender_id, None)
        await provision_and_register(event, wk)


async def provision_and_register(event, wk):
    msg = await event.respond("🚀 Provisioning worker on the server …")

    # Reserve the worker tag up-front so the "building" log and the final
    # "added" card share the SAME tag.
    tag = worker.gen_tag()
    await log(card("🛠 WORKER — BUILDING …", [
        f"🖥 {wk['ip']} · {tag}",
        LINE,
        f"🕒 {now()}",
    ]))

    async def progress(text):
        try:
            await safe_edit(msg, text)
        except Exception:
            pass

    prov = await worker.provision_worker(wk["ip"], wk.get("port", 22),
                                         wk["user"], wk["pass"],
                                         tag=tag, on_progress=progress)
    if not prov.get("ok"):
        await safe_edit(msg, f"❌ Provisioning failed: {prov.get('error')}",
                       buttons=[[Button.inline("🔙 Back", b"workers")]])
        return
    wid = await worker.register_provisioned(wk["ip"], wk.get("port", 22),
                                            wk["user"], wk["pass"], prov)
    w = db.get_worker(wid)
    # Give the freshly started container time to fully come up before the
    # first health check; checking immediately on connect gave a misleading
    # status. Wait 30s, then verify.
    await safe_edit(msg, "⏳ Worker installed. Waiting 30s for it to fully come up, then checking …")
    await asyncio.sleep(30)
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(wid)
    # start the persistent tunnel supervisor for the freshly added worker
    try:
        worker.start_supervisor(w)
    except Exception:
        pass
    await safe_edit(msg, f"✅ Worker {w['tag']} added and checked.",
                   buttons=[[Button.inline("🛠 Worker Management", b"workers")],
                            [Button.inline("🏠 Main Menu", b"home")]])
    await log(added_worker_card(w))
    await log_status_all(refresh=False)


@bot.on(events.CallbackQuery(pattern=b"wk_(\\d+)"))
async def wk_detail_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("Worker not found.", alert=True)
        return
    n_acc = db.count_accounts_on_worker(wid)
    sent = db.worker_sent_today(wid)
    lines = [
        f"🛠 WORKER · {w['tag']}", LINE,
        f"Type       : {_wk_type(w)}{' (local)' if w['is_master'] else ''}",
        f"State      : {worker.status_emoji(w)} {_wk_state(w)}",
        f"IP         : {w['ip']}",
        f"Ping       : {_ping_text(w)}",
        f"Route      : {_wk_route(w)}",
        f"Accounts   : {n_acc}",
        f"Sent today : {sent}",
        f"Enabled    : {'yes' if w['enabled'] else 'no'}",
        f"Last check : {w.get('last_checked') or '—'}",
    ]
    if not w.get("file_ok"):
        d = worker.health_detail(wid)
        if d:
            lines.append(f"Note       : {d}")
    rows = []
    if not w["is_master"]:
        toggle = "⏸ Disable" if w["enabled"] else "▶️ Enable"
        rows.append([Button.inline(toggle, f"wktog_{wid}".encode()),
                     Button.inline("♻️ Restart", f"wkrst_{wid}".encode())])
        rows.append([Button.inline("⬆️ Update", f"wkupd_{wid}".encode()),
                     Button.inline("🗑 Delete", f"wkdel_{wid}".encode())])
    else:
        # Local master worker: only allow enabling/disabling it as a worker
        # (no remote restart/update/teardown — it runs in-process).
        toggle = "⏸ Disable Local" if w["enabled"] else "▶️ Enable Local"
        rows.append([Button.inline(toggle, f"wktog_{wid}".encode())])
    rows.append([Button.inline("🔄 Check This Worker", f"wkchk_{wid}".encode())])
    rows.append([Button.inline("🔙 Back", b"workers")])
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"wktog_(\\d+)"))
async def wk_toggle_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    new_enabled = not w["enabled"]
    db.set_worker_enabled(wid, new_enabled)
    # start/stop the persistent tunnel supervisor to match the new state
    try:
        if new_enabled and not w["is_master"]:
            worker.start_supervisor(db.get_worker(wid))
        else:
            await worker.stop_supervisor(wid)
    except Exception:
        pass
    await event.answer("Status changed.")
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkrst_(\\d+)"))
async def wk_restart_cb(event):
    if not is_owner(event):
        return
    if _worker_updates_busy():
        await event.answer("Cannot restart while a worker update is running.", alert=True)
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w["is_master"]:
        await event.answer("Not available on the master.", alert=True)
        return
    if not _acquire_worker_update_lock():
        await event.answer("A worker operation is running in another process.", alert=True)
        return
    await event.answer("Restarting ...")
    try:
        await worker.close_tunnel(wid)
        await worker.restart_worker(w)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ Restart error: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 Back", f"wk_{wid}".encode())]])
        return
    finally:
        _release_worker_update_lock()
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkupd_(\\d+)"))
async def wk_update_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w["is_master"]:
        await event.answer("Not available on the master.", alert=True)
        return
    if _worker_updates_busy():
        await event.answer("A worker update is already running; not started again.", alert=True)
        return
    if not _acquire_worker_update_lock():
        await event.answer("A worker update is running in another process; not started again.",
                           alert=True)
        return
    _worker_updating_ids.add(wid)
    status = "failed"
    detail = "Update did not start."
    try:
        with contextlib.suppress(Exception):
            await safe_edit(event, f"⬆️ Safe update of Worker {w['tag']} started; the current version is kept until the new image passes its test …")
        status, detail = await _safe_update_worker(w)
    finally:
        _worker_updating_ids.discard(wid)
        _release_worker_update_lock()
    if status == "failed":
        await safe_edit(event, f"❌ Update failed; safe rollback was in effect.\n{detail[-700:]}",
                         buttons=[[Button.inline("🔙 Back", f"wk_{wid}".encode())]])
        return
    if status == "current":
        await safe_edit(event, f"⏭ Worker {w['tag']} was already up to date; no change.\n{detail}",
                         buttons=[[Button.inline("🧾 Versions", b"w_versions")],
                                  [Button.inline("🔙 Back", f"wk_{wid}".encode())]])
        return
    await safe_edit(event, f"✅ Worker {w['tag']} updated successfully.\n{detail}",
                    buttons=[[Button.inline("🧾 Versions", b"w_versions")],
                             [Button.inline("🔙 Back", f"wk_{wid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"wkchk_(\\d+)"))
async def wk_check_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    await event.answer("Checking ...")
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkdel_(\\d+)"))
async def wk_del_confirm_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    await safe_edit(event, 
        "Fully delete this worker? (its container and source on the server are also removed)",
        buttons=[[Button.inline("✅ Yes, delete", f"wkdely_{wid}".encode())],
                 [Button.inline("🔙 No", f"wk_{wid}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"wkdely_(\\d+)"))
async def wk_del_do_cb(event):
    if not is_owner(event):
        return
    if _worker_updates_busy():
        await event.answer("Cannot delete while a worker update is running.", alert=True)
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    if not _acquire_worker_update_lock():
        await event.answer("A worker operation is running in another process.", alert=True)
        return
    try:
        await safe_edit(event, "🗑 Cleaning the server and deleting the worker ...")
        try:
            await worker.stop_supervisor(wid)   # cancel supervisor + drop tunnel
        except Exception:
            pass
        if not w["is_master"]:
            try:
                await worker.teardown_worker(w)
            except Exception:
                pass
        db.delete_worker(wid)
    finally:
        _release_worker_update_lock()
    await safe_edit(event, f"✅ Worker {w['tag']} deleted.",
                     buttons=[[Button.inline("🔙 Back", b"workers")]])


# --------------------------------------------------------------------------- #
# Remote send (account owned by a remote worker)
# --------------------------------------------------------------------------- #
async def send_prepare_remote(event, acc, w, marker):
    if continuous_busy(acc["id"]):
        await safe_edit(event,
            "🔁 An automation feature (automation/secretary/reply/report) is on for this account. "
            "Turn it off from Automation first, then send.",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    await safe_edit(event, f"⏳ Checking worker {w['tag']} and preparing ...")
    # CHECK the worker right before using it.
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(event, 
            f"❌ Worker {w['tag'] if w else '?'} is not healthy/active right now"
            f" (status: {w['status'] if w else 'unknown'}).\n"
            "This account is logged in on this worker and can only send from here.",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/prepare",
                                    {"phone": acc["phone"], "marker": marker})
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ Preparation error on the worker: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    if not res.get("marker_found"):
        await safe_edit(event, 
            f"❌ No message with marker '{marker}' was in the worker's Saved Messages.",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    total = res.get("total", 0)
    if total == 0:
        await safe_edit(event, "No contacts were found.",
                         buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    pending_send[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"],
        "remote": True, "worker_id": w["id"], "total": total,
    }
    await safe_edit(event, 
        card(f"🚀 READY TO SEND (worker {w['tag']})", [
            f"• Content   : marked message '{marker}' ✅",
            f"• Recipients: {total} contacts",
            "• Order     : with-chat -> online -> Last Seen",
            LINE,
            "Send to these contacts?",
        ]),
        buttons=[[Button.inline("✅ Confirm & Send", f"go_{acc['id']}".encode())],
                 [Button.inline("🔙 Cancel", f"acc_{acc['id']}".encode())]],
    )


async def send_text_prepare_remote(event, acc, w, body):
    """Prepare a PLAIN-TEXT (no forward) send for an account owned by a remote
    worker. Mirrors send_prepare_remote but skips the marked-post requirement."""
    if continuous_busy(acc["id"]):
        await safe_edit(event,
            "🔁 An automation feature (automation/secretary/reply/report) is on for this account. "
            "Turn it off from Automation first, then send.",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    await safe_edit(event, f"⏳ Checking worker {w['tag']} and preparing ...")
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(event, 
            f"❌ Worker {w['tag'] if w else '?'} is not healthy/active right now"
            f" (status: {w['status'] if w else 'unknown'}).\n"
            "This account is logged in on this worker and can only send from here.",
            buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/prepare",
                                    {"phone": acc["phone"], "marker": "", "mode": "text"})
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ Preparation error on the worker: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    total = res.get("total", 0)
    if total == 0:
        await safe_edit(event, "No contacts were found.",
                         buttons=[[Button.inline("🔙 Back", f"acc_{acc['id']}".encode())]])
        return
    pending_send[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"],
        "remote": True, "worker_id": w["id"], "total": total,
        "mode": "text", "text": body,
    }
    await safe_edit(event, 
        card(f"✍️ READY TO SEND PLAIN TEXT (worker {w['tag']})", [
            f"• Text      : {body[:80]}{'…' if len(body) > 80 else ''}",
            f"• Recipients: {total} contacts",
            "• Order     : with-chat -> online -> Last Seen",
            LINE,
            "Send to these contacts? (no forward)",
        ]),
        buttons=[[Button.inline("✅ Confirm & Send", f"go_{acc['id']}".encode())],
                 [Button.inline("🔙 Cancel", f"acc_{acc['id']}".encode())]],
    )


async def run_send_remote(owner_id: int, payload: dict):
    account_id = payload["account_id"]
    # clear any stale stop flag so a resumed / multi-account / brain send is not
    # aborted instantly by a previous stop request for this account.
    stop_flags[account_id] = False
    phone = payload["phone"]
    w = db.get_worker(payload["worker_id"])
    marker = db.get_marker()
    delay = db.get_delay()
    count = _next_counter()
    total = payload.get("total", 0)
    # plain-text mode: send a configured text (no forward). Default stays marker.
    mode = (payload.get("mode") or "marker").lower()
    send_body = payload.get("text") or ""
    # resume fix: an explicit remaining list means this run is a resume /
    # worker-transfer (full engine, same as a normal send).
    explicit_recipients = payload.get("recipients") or None
    is_resume = bool(payload.get("is_resume") or explicit_recipients)
    guids = list(explicit_recipients) if explicit_recipients else []
    ok = 0
    fail = 0
    reason = None
    started = datetime.now()

    if not w:
        await log("⛔ The worker that owns this account was not found.")
        pending_send.pop(owner_id, None)
        return

    active_jobs.add(account_id)
    if is_resume:
        await log(card("🔁 RESUME STARTED", [
            f"🛠 Count : {count:03d}",
            f"📱 Phone : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            f"🕒 {now()}",
            LINE,
            f"⏳ Remaining to send : {len(explicit_recipients or [])}",
            f"⏱ Delay : {delay}s",
        ]))
    else:
        await log(card("SEND STARTED 🚀", [
            f"🛠 Count : {count:03d}",
            f"📱 Phone : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            f"🕒 Started : {now()}",
            LINE,
            f"🎯 Targets : {total}",
            f"⏱ Delay : {delay}s",
            ("✍️ Mode : Custom text" if mode == "text"
             else f"📌 Marker : «{marker}» Found ✅"),
        ]))

    prev_retry = 0
    last_log_mark = 0
    try:
        res = await worker.api_call(w, "POST", "/send/start", {
            "phone": phone, "marker": marker, "delay": delay,
            "max_errors": db.get_max_errors(), "send_timeout": config.SEND_TIMEOUT,
            "resume_wait": db.get_resume_wait(),
            "max_retries": 0 if config.RESUME_UNLIMITED else config.RESUME_MAX_RETRIES,
            "text2": db.get_rb_text2(),   # step 5: optional Rubika second text
            "recipients": explicit_recipients or [],  # resume fix: remaining list
            "mode": mode, "text": send_body,   # plain-text (no forward) mode
        })
        # in text mode there is no marked post to find; only require ok
        if not res.get("ok") or (mode != "text" and not res.get("marker_found")):
            reason = "Marker not found on worker"
        else:
            job_id = res["job_id"]
            total = res.get("total", total)
            # mirror the EXACT ordered list the worker is using, so we can
            # compute the precise remaining slice (guids[ok+fail:]) on stop.
            guids = res.get("guids") or guids
            while True:
                if stop_flags.get(account_id):
                    try:
                        await worker.api_call(w, "POST", f"/send/stop/{job_id}")
                    except Exception:
                        pass
                await asyncio.sleep(2)
                try:
                    stt = await worker.api_call(w, "GET", f"/send/status/{job_id}")
                except Exception as e:  # noqa: BLE001
                    reason = f"Lost connection to worker: {repr(e)[:120]}"
                    break
                ok = stt.get("ok", 0)
                fail = stt.get("fail", 0)
                # progress log every SEND_LOG_EVERY successful sends (+ percent)
                if config.SEND_LOG_EVERY > 0 and ok // config.SEND_LOG_EVERY > last_log_mark:
                    last_log_mark = ok // config.SEND_LOG_EVERY
                    pct = int(ok * 100 / total) if total else 0
                    await log(card("📊 SEND PROGRESS", [
                        f"📱 {phone} (worker {w['tag']})",
                        f"✅ {ok} of {total} — {pct}%",
                        f"⏳ Remaining : {max(0, total - ok - fail)}",
                        f"🕒 {now()}",
                    ]))
                # auto-resume happening on the worker -> master posts the ALERT
                rc = stt.get("retry_count", 0)
                if rc > prev_retry:
                    prev_retry = rc
                    remaining = max(0, total - ok - fail)
                    await log(card("🚨 COOLDOWN — 5 min pause", [
                        f"✅ {ok}",
                        f"⏳ {remaining}",
                        f"🔁 Round {rc}",
                        f"👤 Account : {phone}",
                    ]))
                if stt.get("done"):
                    r = stt.get("reason")
                    if r == "manual_stop":
                        reason = "Manual stop by user"
                    elif r and str(r).startswith("max_errors"):
                        reason = f"Reached error cap ({db.get_max_errors()})"
                    elif r:
                        reason = str(r)
                    break
    except Exception as e:  # noqa: BLE001
        reason = f"General error: {repr(e)[:150]}"

    try:
        db.incr_worker_sent(w["id"], ok)
    except Exception:
        pass
    active_jobs.discard(account_id)
    dur = str(datetime.now() - started).split(".")[0]
    pending_send.pop(owner_id, None)
    is_owner_user = owner_id == config.OWNER_ID

    if reason:
        await log(card("🔁 RESUME STOPPED ⛔" if is_resume else "⛔ SEND STOPPED", [
            f"👤 Account : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            f"📊 ✅ {ok}   ❌ {fail}   📁 {total}",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ Send stopped. ✅ {ok} / ❌ {fail} of {total}\nReason: {reason}",
                                   buttons=main_menu(is_owner_user))
        except Exception:
            pass
        # resume fix: pass the REAL remaining list (guids[ok+fail:]) so the
        # resume / worker-transfer continues from where it stopped — not from
        # scratch. (ok+fail == processed index on the worker.)
        remaining = guids[(ok + fail):] if guids else []
        await _offer_resume_after_send(owner_id, {
            "account_id": account_id, "phone": phone, "remote": True,
            "worker_id": w["id"], "recipients": remaining, "base_ok": ok, "tag": "",
            "dead": ("blocked" in str(reason)) or ("invalid" in str(reason)),
            "reason": reason,
        })
    else:
        # fully finished -> nothing remaining; clear any stale paused record.
        try:
            db.delete_paused_send(account_id)
        except Exception:
            pass
        await log(card("🔁 RESUME FINISHED ✅" if is_resume else "SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"👤 Account : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            LINE,
            f"✅ {ok}   ❌ {fail}   📁 {total}",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ Send finished. ✅ {ok} / ❌ {fail} of {total}",
                                   buttons=main_menu(is_owner_user))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Automation: rotate texts to an account's groups, repeatedly.
# Works for local (master) accounts and accounts owned by a remote worker.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"automation"))
async def automation_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, "Add an account first.",
                        buttons=[[Button.inline("➕ Add Account", b"add_account")],
                                 [Button.inline("🔙 Back", b"home")]])
        return
    rows = []
    for a in accounts:
        on = automation_on(a["id"])
        rows.append([Button.inline(f"{'🟢' if on else '⚪️'} {a['phone']}",
                                   f"auto_{a['id']}".encode())])
    rows.append([Button.inline("🪪 Sync Name/Bio for All Accounts", b"psync")])
    rows.append([Button.inline("📨 Linkdooni (Groups Engine)", b"linkdooni")])
    rows.append([Button.inline("🔙 Back", b"home")])
    await safe_edit(event, "🔁 Automation — pick an account:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auto_(\\d+)"))
async def automation_account_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    au = db.get_automation(account_id)
    texts = db.list_automation_texts(account_id)
    on = bool(au["enabled"])
    lines = [
        f"🔁 Automation — {acc['phone']}", LINE,
        f"• Status : {'🟢 ON' if on else '⚪️ OFF'}",
        f"• Interval : {au['interval_sec']} seconds",
        f"• Texts    : {len(texts)}",
        f"• Total sent : {au['sent_total']}",
        LINE,
        f"🤖 Secretary : {'🟢' if secretary_on(account_id) else '⚪️'}   "
        f"📊 Channel report : {'🟢' if channelreport_on(account_id) else '⚪️'}   "
        f"↩️ Reply : {'🟢' if reply_on(account_id) else '⚪️'}",
    ]
    rows = [
        [Button.inline("➕ Add Text", f"auadd_{account_id}".encode()),
         Button.inline("🗑 Clear Texts", f"auclr_{account_id}".encode())],
        [Button.inline("🔗 Groups List", f"aulnk_{account_id}".encode())],
        [Button.inline("⏱ Set Interval", f"auint_{account_id}".encode())],
        [Button.inline("⏹ Turn Off" if on else "▶️ Turn On",
                       f"autog_{account_id}".encode())],
        [Button.inline("🤖 PV Secretary", f"secm_{account_id}".encode()),
         Button.inline("📊 Channel Report", f"crm_{account_id}".encode())],
        [Button.inline("↩️ Reply Responder", f"rpm_{account_id}".encode())],
        [Button.inline("🔙 Back", b"automation")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auadd_(\\d+)"))
async def automation_add_text_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_text", "account_id": account_id}
    await safe_edit(event, "✍️ Send the text to post to groups (you can send several in a row):",
                    buttons=[[Button.inline("✅ Done / Back", f"auto_{account_id}".encode())]])


async def handle_auto_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    text = event.raw_text.strip()
    if not text:
        await event.respond("Text is empty. Send it again.")
        return
    db.add_automation_text(account_id, text)
    n = len(db.list_automation_texts(account_id))
    await event.respond(
        f"✅ Text added (total: {n}). Send the next text or go back.",
        buttons=[[Button.inline("✅ Done / Back", f"auto_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"auclr_(\\d+)"))
async def automation_clear_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.clear_automation_texts(account_id)
    await event.answer("All texts cleared.")
    await automation_account_cb(event)


@bot.on(events.CallbackQuery(pattern=b"auint_(\\d+)"))
async def automation_interval_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_interval", "account_id": account_id}
    await safe_edit(event,
        f"⏱ Send a number between {config.AUTOMATION_MIN_INTERVAL} and {config.AUTOMATION_MAX_INTERVAL} "
        "(the interval per round, in seconds):",
        buttons=[[Button.inline("🔙 Back", f"auto_{account_id}".encode())]])


async def handle_auto_interval(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    db.set_automation_interval(account_id, event.raw_text.strip())
    iv = db.get_automation(account_id)["interval_sec"]
    state.pop(event.sender_id, None)
    acc = db.get_account(account_id)
    if acc and automation_on(account_id):   # apply new interval to a live loop
        await stop_automation(acc)
        await start_automation(acc)
    await event.respond(f"✅ Interval set to {iv} seconds.",
                        buttons=[[Button.inline("🔙 Back", f"auto_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"autog_(\\d+)"))
async def automation_toggle_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    au = db.get_automation(account_id)
    if not au["enabled"]:                       # turning ON
        if not db.list_automation_texts(account_id):
            await event.answer("Add at least one text first.", alert=True)
            return
        if account_id in active_jobs:
            await event.answer("This account is sending right now. Wait until it finishes.", alert=True)
            return
        # start FIRST; only mark enabled if it actually launched (so a dead/old
        # worker can't leave the account stuck in a broken "on" state).
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"Failed to start automation: {repr(e)[:120]}\n"
                               "If the account is on a worker, update the worker first.", alert=True)
            return
        db.set_automation_enabled(account_id, True)
        await log(card("🔁 AUTOMATION ON", [
            f"👤 Account : {acc['phone']}",
            f"⏱ Interval : {au['interval_sec']}s",
            f"🕒 {now()}",
        ]))
    else:                                       # turning OFF
        db.set_automation_enabled(account_id, False)
        await stop_automation(acc)
        await log(card("🔁 AUTOMATION OFF", [
            f"👤 Account : {acc['phone']}",
            f"🕒 {now()}",
        ]))
    await automation_account_cb(event)


# ---- per-account group-link list: this ONE account joins your personal groups ----
@bot.on(events.CallbackQuery(pattern=b"aulnk_(\\d+)"))
async def automation_links_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    links = db.list_automation_links(account_id)
    body = "\n".join(f"• {ln}" for ln in links) if links else "No link has been added yet."
    lines = [f"🔗 Group list for {acc['phone']}", LINE, body, LINE,
             "You can add your own group links, then tap Join so "
             "this account joins them."]
    rows = [
        [Button.inline("➕ Add Link", f"auladd_{account_id}".encode()),
         Button.inline("🗑 Clear", f"aulclr_{account_id}".encode())],
        [Button.inline("✅ Join (and save to shared list)", f"auljoin_{account_id}".encode())],
        [Button.inline(f"📥 Join from shared list ({db.count_verified_group_links()})",
                       f"aushared_{account_id}".encode())],
        [Button.inline("🔙 Back", f"auto_{account_id}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auladd_(\\d+)"))
async def automation_link_add_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_link", "account_id": account_id}
    await safe_edit(event, "🔗 Send the Rubika group link (you can send several in a row):",
                    buttons=[[Button.inline("✅ Done / Back", f"aulnk_{account_id}".encode())]])


async def handle_auto_link(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    link = event.raw_text.strip()
    if not link.startswith("http"):
        await event.respond("Send a valid link (starting with https).")
        return
    db.add_automation_link(account_id, link)
    n = len(db.list_automation_links(account_id))
    await event.respond(
        f"✅ Link added (total: {n}). Send the next link or go back.",
        buttons=[[Button.inline("✅ Done / Back", f"aulnk_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"aulclr_(\\d+)"))
async def automation_link_clear_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.clear_automation_links(account_id)
    await event.answer("Links cleared.")
    await automation_links_cb(event)


@bot.on(events.CallbackQuery(pattern=b"auljoin_(\\d+)"))
async def automation_link_join_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    links = db.list_automation_links(account_id)
    if not links:
        await event.answer("Add at least one link first.", alert=True)
        return
    if continuous_busy(account_id):
        await event.answer("🔁 An automation feature is on for this account. Turn it off first, then tap Join.",
                           alert=True)
        return
    if account_id in active_jobs:
        await event.answer("This account is busy right now. Wait.", alert=True)
        return
    await safe_edit(event, f"⏳ {acc['phone']} is joining {len(links)} groups ... "
                    "reports go to the log group.")
    asyncio.create_task(run_group_join(acc, links))


async def run_group_join(acc: dict, links: list):
    account_id = acc["id"]
    phone = acc["phone"]
    active_jobs.add(account_id)
    joined = 0
    failed = 0
    joined_links = []
    try:
        w = worker.worker_for_account(acc)
        if w and not worker.is_local(w):
            res = await worker.api_call(w, "POST", "/group/join",
                                        {"phone": phone, "links": links}, timeout=600)
            joined = res.get("joined", 0)
            failed = res.get("failed", 0)
            joined_links = res.get("joined_links", []) or []
        else:
            await account_conn.close(phone)   # ensure single connection (Feature 6)
            client = rb.open_client(phone)
            try:
                await rb.connect_ready(client)
                for link in links:
                    try:
                        await asyncio.wait_for(rb.join_group_by_link(client, link),
                                               timeout=60)
                        joined += 1
                        joined_links.append(link)
                    except Exception:
                        failed += 1
                    await asyncio.sleep(config.GROUP_JOIN_DELAY)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        # Feature 4: remember every successfully joined link in the SHARED
        # verified list so the other accounts can re-use it.
        for ln in joined_links:
            try:
                db.add_verified_group_link(ln, added_by=phone)
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Joining groups for '{phone}' was incomplete: {repr(e)[:150]}")
    finally:
        active_jobs.discard(account_id)
    await log(card("🔗 GROUP JOIN", [
        f"👤 Account : {phone}",
        f"✅ Joined : {joined}",
        f"❌ Failed : {failed}",
        f"💾 Saved to shared : {len(joined_links)}",
        f"🕒 {now()}",
    ]))


async def run_automation_local(account_id: int, phone: str, st: dict):
    """Local automation loop — ONE connection per pass (Feature 6), the same
    open->work->close shape as the original source automation and the working
    "send" path. Every interval we open one connection, send a random text to
    each group on it (tiny random pause between groups), then close it and
    sleep. The per-account lock inside connection() means secretary / reply /
    channel report on the SAME account never hold a connection at the same time
    -> no parallel clients, and opening once-per-pass -> no connect churn."""
    fails: dict = {}          # guid -> consecutive failures
    last_text: dict = {}
    try:
        while not st["stop"]:
            st["heartbeat"] = time.monotonic()   # watchdog: prove we're alive
            try:
                # ONE connection for this whole pass (per-account lock inside
                # connection() prevents parallel use by secretary/reply).
                async with account_conn.connection(phone) as client:
                    try:
                        groups = await asyncio.wait_for(
                            rb.get_group_guids(client), timeout=60)
                    except Exception:
                        # could not read groups this pass -> drop the (maybe
                        # wedged) socket and try again next round. NEVER kill the
                        # loop, NEVER open a second connection to "verify".
                        groups = []
                        account_conn.drop_connection(phone)
                    st["groups"] = len(groups)
                    for g in groups:
                        if st["stop"]:
                            break
                        guid = g["guid"]
                        if guid in st["skipped"]:
                            continue
                        idx, txt = _pick_text(st["texts"], last_text.get(guid))
                        if txt is None:
                            break
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, guid, txt),
                                timeout=config.SEND_TIMEOUT)
                        except Exception:
                            # ANY send failure (banned/muted group, transient
                            # auth hiccup, timeout, ...) is treated EXACTLY like
                            # the original code: count it against THIS group and
                            # mute the group after 3 strikes. We do NOT declare
                            # the account dead and do NOT stop the loop — that
                            # false-positive was what silently halted automation.
                            fails[guid] = fails.get(guid, 0) + 1
                            if fails[guid] >= 3:
                                st["skipped"].add(guid)
                                # cleanup engine: record this banned/muted group
                                # as a candidate (logged once) for the owner to
                                # review + confirm leaving.
                                try:
                                    is_new = db.add_cleanup_candidate(
                                        account_id, guid, g.get("name", ""),
                                        reason="banned/muted or unable to send")
                                    if is_new:
                                        await _log_cleanup_candidate(
                                            account_id, phone, guid, g.get("name", ""))
                                except Exception:
                                    pass
                        else:
                            st["sent"] += 1
                            last_text[guid] = idx
                            fails[guid] = 0
                            try:              # a brief DB lock must NOT count as a send error
                                db.incr_automation_sent(account_id, 1)
                            except Exception:
                                pass
                        await asyncio.sleep(random.uniform(
                            config.AUTOMATION_GROUP_DELAY_MIN,
                            config.AUTOMATION_GROUP_DELAY_MAX))
                    # recovery: if every group ended up muted, reset + reconnect
                    if groups and all(g["guid"] in st["skipped"] for g in groups):
                        st["skipped"].clear()
                        fails.clear()
                        account_conn.drop_connection(phone)
            except Exception as e:  # noqa: BLE001
                # a whole-pass error: drop the connection so the next pass is
                # fresh, log once, and CONTINUE (never kill automation).
                account_conn.drop_connection(phone)
                await log(f"⚠️ Automation '{phone}' round error (continuing): {repr(e)[:150]}")
            st["heartbeat"] = time.monotonic()
            waited = 0
            while waited < st["interval"] and not st["stop"]:
                await asyncio.sleep(1)
                waited += 1
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Automation '{phone}' stopped with an error: {repr(e)[:150]}")


async def start_automation(acc: dict):
    """Start the automation loop for an account (local task or remote worker job)."""
    account_id = acc["id"]
    texts = db.list_automation_texts(account_id)
    interval = db.get_automation(account_id)["interval_sec"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/automation/start",
                              {"phone": acc["phone"], "texts": texts, "interval": interval})
        return
    # local
    old = automation_tasks.pop(account_id, None)
    if old:
        old["state"]["stop"] = True
        try:                                  # let the old loop fully stop first
            await asyncio.wait_for(old["task"], timeout=5)
        except Exception:
            pass
    st = {"stop": False, "sent": 0, "groups": 0, "skipped": set(),
          "texts": texts, "interval": interval, "heartbeat": time.monotonic()}
    task = asyncio.create_task(run_automation_local(account_id, acc["phone"], st))
    automation_tasks[account_id] = {"task": task, "state": st}


async def stop_automation(acc: dict):
    account_id = acc["id"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/automation/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    t = automation_tasks.pop(account_id, None)
    if t:
        t["state"]["stop"] = True


async def automation_summary_loop():
    """Every AUTOMATION_SUMMARY_INTERVAL, post a per-account total. Also self-
    heals automations that stopped: relaunches a worker automation whose
    container restarted AND a LOCAL automation whose task died or hung (no
    heartbeat within 3x its interval)."""
    while True:
        await asyncio.sleep(config.AUTOMATION_SUMMARY_INTERVAL)
        try:
            for au in db.list_enabled_automations():
                acc = db.get_account(au["account_id"])
                if not acc:
                    continue
                w = worker.worker_for_account(acc)
                sent = au["sent_total"]
                groups = None
                if w and not worker.is_local(w):
                    try:
                        stt = await worker.api_call(
                            w, "GET", f"/automation/status?phone={acc['phone']}")
                        if not stt.get("running"):   # worker restarted -> relaunch
                            await start_automation(acc)
                        sent = stt.get("sent", sent)
                        groups = stt.get("groups")
                    except Exception:
                        pass
                else:
                    # LOCAL self-heal: relaunch if the task is gone, finished,
                    # or hung (heartbeat older than 3x interval -> silent stall).
                    t = automation_tasks.get(au["account_id"])
                    interval = au.get("interval_sec") or config.AUTOMATION_MIN_INTERVAL
                    stale = (3 * max(interval, 10)) + 120
                    dead = (not t) or t["task"].done()
                    hung = False
                    if t and not dead:
                        hb = t["state"].get("heartbeat", 0)
                        hung = (time.monotonic() - hb) > stale
                    if dead or hung:
                        if hung:                       # a hung task must be cancelled
                            try:
                                t["state"]["stop"] = True
                                t["task"].cancel()
                            except Exception:
                                pass
                        await log(card("♻️ AUTOMATION SELF-HEAL", [
                            f"👤 Account : {acc['phone']}",
                            ("Reason: task had stopped" if dead else "Reason: silent hang (no activity)"),
                            "Automation restarted.",
                            f"🕒 {now()}"]))
                        try:
                            await start_automation(acc)
                        except Exception as e:  # noqa: BLE001
                            await log(f"⚠️ Automation self-heal for {acc['phone']} failed: {repr(e)[:120]}")
                    if t and t["state"].get("groups") is not None:
                        groups = t["state"].get("groups")
                rows = [f"👤 Account : {acc['phone']}", f"• Total sent : {sent}"]
                if groups is not None:
                    rows.append(f"• Groups : {groups}")
                rows.append(f"🕒 {now()}")
                await log(card("🔁 AUTOMATION SUMMARY", rows))
        except Exception as e:  # noqa: BLE001
            print(f"[automation_summary] {e}")


async def recover_automations():
    """On boot, relaunch every automation that was enabled before restart."""
    for au in db.list_enabled_automations():
        acc = db.get_account(au["account_id"])
        if not acc:
            continue
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Automation restore for {acc['phone']} failed: {repr(e)[:120]}")


# --------------------------------------------------------------------------- #
# Automation EXTRAS — start/stop + panel UI (secretary / channel report /
# reply responder), profile sync, shared-list join, recovery + worker relay.
# All LOCAL loops run on the shared connection (account_conn); remote accounts
# are driven through new worker endpoints (see worker_api.py).
# --------------------------------------------------------------------------- #
async def _start_local(tasks: dict, account_id: int, factory):
    """(Re)start a local feature loop, replacing any previous one."""
    old = tasks.pop(account_id, None)
    if old:
        old["state"]["stop"] = True
        try:
            await asyncio.wait_for(old["task"], timeout=5)
        except Exception:
            pass
    st = {"stop": False, "replied": 0}
    task = asyncio.create_task(factory(st))
    tasks[account_id] = {"task": task, "state": st}


def _stop_local(tasks: dict, account_id: int):
    t = tasks.pop(account_id, None)
    if t:
        t["state"]["stop"] = True
        # also cancel the task so it stops promptly even if it is mid-sleep or
        # mid-call; otherwise a long interval could keep it running one more pass
        # after the user turned the feature off in the panel.
        task = t.get("task")
        if task and not task.done():
            task.cancel()


# ---- Feature 1: secretary ----
async def start_secretary(acc: dict):
    aid = acc["id"]
    phone = acc["phone"]
    try:                                   # prime cursor: don't reply to old PVs
        db.set_secretary_state(aid, "")
    except Exception:
        pass
    sec = db.get_secretary(aid)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/secretary/start", {
            "phone": phone, "mode": sec.get("mode") or "marker",
            "text": sec.get("text") or "", "marker": db.get_marker(),
            "interval": sec.get("interval_sec") or config.SECRETARY_INTERVAL})
        return
    await _start_local(secretary_tasks, aid,
                       lambda st: features.run_secretary_local(aid, phone, st))


async def stop_secretary(acc: dict):
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/secretary/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    _stop_local(secretary_tasks, acc["id"])


# ---- Feature 2: channel report ----
async def start_channelreport(acc: dict):
    aid = acc["id"]
    phone = acc["phone"]
    cr = db.get_channel_report(aid)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/channelreport/start", {
            "phone": phone, "channel_guid": cr.get("channel_guid") or "",
            "channel_title": cr.get("channel_title") or "",
            "interval": cr.get("interval_sec") or config.CHANNEL_REPORT_INTERVAL})
        return
    await _start_local(channelreport_tasks, aid,
                       lambda st: features.run_channel_report_local(aid, phone, st))


async def stop_channelreport(acc: dict):
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/channelreport/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    _stop_local(channelreport_tasks, acc["id"])


# ---- Feature 5: reply responder ----
async def start_reply(acc: dict):
    aid = acc["id"]
    phone = acc["phone"]
    rr = db.get_reply_responder(aid)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/reply/start", {
            "phone": phone, "text": rr.get("text") or "",
            "delay": rr.get("delay_sec") or config.REPLY_DELAY})
        return
    await _start_local(reply_tasks, aid,
                       lambda st: features.run_reply_local(aid, phone, st))


async def stop_reply(acc: dict):
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/reply/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    _stop_local(reply_tasks, acc["id"])


# --------------------------------------------------------------------------- #
# Secretary panel
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"secm_(\\d+)"))
async def secretary_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    sec = db.get_secretary(aid)
    on = bool(sec["enabled"])
    mode = sec.get("mode") or "marker"
    lines = [
        f"🤖 PV Secretary — {acc['phone']}", LINE,
        f"• Status : {'🟢 ON' if on else '⚪️ OFF'}",
        f"• Reply mode : {'custom text' if mode == 'text' else 'marker (marked message)'}",
        f"• Custom text : {((sec.get('text') or '—')[:40])}",
        f"• Check interval : {sec.get('interval_sec')} seconds",
        f"• Total replies : {sec.get('replied_total')}",
        LINE,
        "Only the first message from each person gets a reply.",
    ]
    rows = [
        [Button.inline("📌 Marker Mode" + (" ✅" if mode == "marker" else ""),
                       f"secmodem_{aid}".encode()),
         Button.inline("✍️ Text Mode" + (" ✅" if mode == "text" else ""),
                       f"secmodet_{aid}".encode())],
        [Button.inline("✍️ Set Custom Text", f"sectext_{aid}".encode())],
        [Button.inline("⏱ Set Interval", f"secint_{aid}".encode())],
        [Button.inline("⏹ Turn Off" if on else "▶️ Turn On",
                       f"sectog_{aid}".encode())],
        [Button.inline("🔙 Back", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"secmodem_(\\d+)"))
async def secretary_mode_marker_cb(event):
    if not is_owner(event):
        return
    db.set_secretary_mode(int(event.pattern_match.group(1)), "marker")
    await event.answer("Mode: marker")
    await secretary_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"secmodet_(\\d+)"))
async def secretary_mode_text_cb(event):
    if not is_owner(event):
        return
    db.set_secretary_mode(int(event.pattern_match.group(1)), "text")
    await event.answer("Mode: custom text")
    await secretary_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"sectext_(\\d+)"))
async def secretary_set_text_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_sec_text", "account_id": aid}
    await safe_edit(event, "✍️ Send the secretary's reply text:",
                    buttons=[[Button.inline("🔙 Back", f"secm_{aid}".encode())]])


async def handle_sec_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("Text is empty. Send it again.")
        return
    db.set_secretary_text(aid, txt)
    db.set_secretary_mode(aid, "text")
    state.pop(event.sender_id, None)
    await event.respond("✅ Secretary text set and the mode switched to custom text.",
                        buttons=[[Button.inline("🔙 Back", f"secm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"secint_(\\d+)"))
async def secretary_interval_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_sec_interval", "account_id": aid}
    await safe_edit(event,
        f"⏱ PV check interval (seconds) between {config.SECRETARY_MIN_INTERVAL} and "
        f"{config.SECRETARY_MAX_INTERVAL}, send it:",
        buttons=[[Button.inline("🔙 Back", f"secm_{aid}".encode())]])


async def handle_sec_interval(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    db.set_secretary_interval(aid, event.raw_text.strip())
    state.pop(event.sender_id, None)
    acc = db.get_account(aid)
    if acc and secretary_on(aid):          # apply new interval to a live loop
        await stop_secretary(acc)
        await start_secretary(acc)
    iv = db.get_secretary(aid)["interval_sec"]
    await event.respond(f"✅ Interval set to {iv} seconds.",
                        buttons=[[Button.inline("🔙 Back", f"secm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"sectog_(\\d+)"))
async def secretary_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    sec = db.get_secretary(aid)
    if not sec["enabled"]:
        if aid in active_jobs:
            await event.answer("This account is busy with a one-shot operation. Wait.", alert=True)
            return
        if (sec.get("mode") or "marker") == "text" and not (sec.get("text") or "").strip():
            await event.answer("Set a custom text first, or choose marker mode.",
                               alert=True)
            return
        try:
            await start_secretary(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"Failed to start the secretary: {repr(e)[:110]}\n"
                               "If the account is on a worker, update the worker first.", alert=True)
            return
        db.set_secretary_enabled(aid, True)
        await log(card("🤖 SECRETARY ON", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    else:
        db.set_secretary_enabled(aid, False)
        await stop_secretary(acc)
        await log(card("🤖 SECRETARY OFF", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    await secretary_menu_cb(event)


# --------------------------------------------------------------------------- #
# Channel report panel
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"crm_(\\d+)"))
async def channelreport_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    cr = db.get_channel_report(aid)
    on = bool(cr["enabled"])
    lines = [
        f"📊 Channel Report — {acc['phone']}", LINE,
        f"• Status : {'🟢 ON' if on else '⚪️ OFF'}",
        f"• Channel : {cr.get('channel_guid') or '—'}",
        f"• Title : {cr.get('channel_title') or '—'}",
        f"• Interval : {cr.get('interval_sec')} seconds",
        LINE,
        "Each interval: member count + last post views -> log group.",
    ]
    rows = [
        [Button.inline("📢 Set Channel (link/username/guid)", f"crset_{aid}".encode())],
        [Button.inline("⏱ Set Interval", f"crint_{aid}".encode())],
        [Button.inline("⏹ Turn Off" if on else "▶️ Turn On",
                       f"crtog_{aid}".encode())],
        [Button.inline("🔙 Back", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"crset_(\\d+)"))
async def channelreport_set_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_cr_channel", "account_id": aid}
    await safe_edit(event,
        "📢 Send the channel link, username, or guid:\n"
        "Example: `@my_channel` or `https://rubika.ir/my_channel` or `c0...`",
        buttons=[[Button.inline("🔙 Back", f"crm_{aid}".encode())]])


async def handle_cr_channel(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    ref = event.raw_text.strip()
    if not ref:
        await event.respond("Empty. Send it again.")
        return
    db.set_channel_report_target(aid, ref, "")
    state.pop(event.sender_id, None)
    await event.respond("✅ Channel saved. (at report time, username/link is auto-converted to guid)",
                        buttons=[[Button.inline("🔙 Back", f"crm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"crint_(\\d+)"))
async def channelreport_interval_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_cr_interval", "account_id": aid}
    await safe_edit(event,
        f"⏱ Report interval (seconds) between {config.CHANNEL_REPORT_MIN_INTERVAL} and "
        f"{config.CHANNEL_REPORT_MAX_INTERVAL}, send it:",
        buttons=[[Button.inline("🔙 Back", f"crm_{aid}".encode())]])


async def handle_cr_interval(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    db.set_channel_report_interval(aid, event.raw_text.strip())
    state.pop(event.sender_id, None)
    acc = db.get_account(aid)
    if acc and channelreport_on(aid):
        await stop_channelreport(acc)
        await start_channelreport(acc)
    iv = db.get_channel_report(aid)["interval_sec"]
    await event.respond(f"✅ Interval set to {iv} seconds.",
                        buttons=[[Button.inline("🔙 Back", f"crm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"crtog_(\\d+)"))
async def channelreport_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    cr = db.get_channel_report(aid)
    if not cr["enabled"]:
        if aid in active_jobs:
            await event.answer("This account is busy with a one-shot operation. Wait.", alert=True)
            return
        if not (cr.get("channel_guid") or "").strip():
            await event.answer("Set the channel first.", alert=True)
            return
        try:
            await start_channelreport(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"Failed to start the report: {repr(e)[:110]}\n"
                               "If the account is on a worker, update the worker first.", alert=True)
            return
        db.set_channel_report_enabled(aid, True)
        await log(card("📊 CHANNEL REPORT ON", [
            f"👤 Account : {acc['phone']}",
            f"🆔 Channel : {cr.get('channel_guid')}",
            f"🕒 {now()}"]))
    else:
        db.set_channel_report_enabled(aid, False)
        await stop_channelreport(acc)
        await log(card("📊 CHANNEL REPORT OFF", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    await channelreport_menu_cb(event)


# --------------------------------------------------------------------------- #
# Reply responder panel
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"rpm_(\\d+)"))
async def reply_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    rr = db.get_reply_responder(aid)
    on = bool(rr["enabled"])
    lines = [
        f"↩️ Reply Responder — {acc['phone']}", LINE,
        f"• Status : {'🟢 ON' if on else '⚪️ OFF'}",
        f"• Reply text : {((rr.get('text') or '—')[:40])}",
        f"• Delay : {rr.get('delay_sec')} seconds",
        f"• Total replies : {rr.get('replied_total')}",
        LINE,
        "When someone replies to this account in a group, it auto-answers (text only for now).",
    ]
    rows = [
        [Button.inline("✍️ Set Text", f"rptext_{aid}".encode())],
        [Button.inline("⏱ Set Delay", f"rpdelay_{aid}".encode())],
        [Button.inline("⏹ Turn Off" if on else "▶️ Turn On",
                       f"rptog_{aid}".encode())],
        [Button.inline("🔙 Back", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"rptext_(\\d+)"))
async def reply_set_text_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_rp_text", "account_id": aid}
    await safe_edit(event, "✍️ Send the reply-responder text:",
                    buttons=[[Button.inline("🔙 Back", f"rpm_{aid}".encode())]])


async def handle_rp_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("Text is empty. Send it again.")
        return
    db.set_reply_text(aid, txt)
    state.pop(event.sender_id, None)
    await event.respond("✅ Reply text set.",
                        buttons=[[Button.inline("🔙 Back", f"rpm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"rpdelay_(\\d+)"))
async def reply_set_delay_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_rp_delay", "account_id": aid}
    await safe_edit(event,
        f"⏱ Reply delay (seconds) between {config.REPLY_MIN_DELAY} and "
        f"{config.REPLY_MAX_DELAY}, send it:",
        buttons=[[Button.inline("🔙 Back", f"rpm_{aid}".encode())]])


async def handle_rp_delay(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    db.set_reply_delay(aid, event.raw_text.strip())
    state.pop(event.sender_id, None)
    acc = db.get_account(aid)
    if acc and reply_on(aid):
        await stop_reply(acc)
        await start_reply(acc)
    d = db.get_reply_responder(aid)["delay_sec"]
    await event.respond(f"✅ Delay set to {d} seconds.",
                        buttons=[[Button.inline("🔙 Back", f"rpm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"rptog_(\\d+)"))
async def reply_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    rr = db.get_reply_responder(aid)
    if not rr["enabled"]:
        if aid in active_jobs:
            await event.answer("This account is busy with a one-shot operation. Wait.", alert=True)
            return
        if not (rr.get("text") or "").strip():
            await event.answer("Set the reply text first.", alert=True)
            return
        try:
            await start_reply(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"Failed to start the reply responder: {repr(e)[:110]}\n"
                               "If the account is on a worker, update the worker first.", alert=True)
            return
        db.set_reply_enabled(aid, True)
        await log(card("↩️ REPLY RESPONDER ON", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    else:
        db.set_reply_enabled(aid, False)
        await stop_reply(acc)
        await log(card("↩️ REPLY RESPONDER OFF", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    await reply_menu_cb(event)


# --------------------------------------------------------------------------- #
# Feature 3: profile (name + bio) sync across ALL accounts
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"psync"))
async def psync_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    p = db.get_profile_sync()
    name = (str(p.get("first_name") or "") + " " + str(p.get("last_name") or "")).strip()
    lines = [
        "🪪 Sync Name/Bio for All Accounts", LINE,
        f"• Name : {name or '—'}",
        f"• Bio  : {p.get('bio') or '—'}",
        LINE,
        "This value is applied to all accounts (no photo needed).",
    ]
    rows = [
        [Button.inline("✏️ Set Name/Bio", b"psyncset")],
        [Button.inline("🚀 Apply to All", b"psyncgo")],
        [Button.inline("🔙 Back", b"automation")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(data=b"psyncset"))
async def psync_set_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_psync"}
    await safe_edit(event,
        "✏️ Send the name on the first line and the bio on the second line:\n"
        "First line = full name (split into first/last at the first space)\n"
        "Second line = bio\n\nExample:\nAli Rezaei\nHi, welcome 🌹",
        buttons=[[Button.inline("🔙 Back", b"psync")]])


async def handle_psync_input(event):
    txt = event.raw_text
    parts = txt.split("\n", 1)
    name_line = parts[0].strip()
    bio = parts[1].strip() if len(parts) > 1 else ""
    np = name_line.split(" ", 1)
    first = np[0].strip() if np else ""
    last = np[1].strip() if len(np) > 1 else ""
    db.set_profile_sync(first, last, bio)
    state.pop(event.sender_id, None)
    await event.respond(
        f"✅ Saved:\nName: {name_line or '—'}\nBio: {bio or '—'}\n"
        "Now tap Apply to All.",
        buttons=[[Button.inline("🔙 Back", b"psync")]])


async def _apply_profile_local(client, first, last, bio):
    """Compare current profile to target; update only if different. Returns
    True if changed, False if already identical."""
    cur = await rb.get_my_profile(client)
    same = ((cur.get("first_name") or "") == first
            and (cur.get("last_name") or "") == last
            and (cur.get("bio") or "") == bio)
    if same:
        return False
    await rb.update_profile(client, first_name=first, last_name=last, bio=bio)
    return True


@bot.on(events.CallbackQuery(data=b"psyncgo"))
async def psync_go_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("There are no accounts.", alert=True)
        return
    p = db.get_profile_sync()
    if not (p.get("first_name") or p.get("last_name") or p.get("bio")):
        await event.answer("Set the name/bio first.", alert=True)
        return
    await safe_edit(event,
        f"⏳ Applying name/bio to {len(accounts)} accounts ... reports go to the log group.")
    asyncio.create_task(run_profile_sync())


async def run_profile_sync():
    p = db.get_profile_sync()
    first = p.get("first_name") or ""
    last = p.get("last_name") or ""
    bio = p.get("bio") or ""
    accounts = db.list_accounts()
    changed = unchanged = failed = 0
    rows = []
    for acc in accounts:
        phone = acc["phone"]
        try:
            w = worker.worker_for_account(acc)
            if w and not worker.is_local(w):
                res = await worker.api_call(w, "POST", "/profile/update", {
                    "phone": phone, "first_name": first, "last_name": last,
                    "bio": bio}, timeout=120)
                ch = res.get("changed")
            else:
                ch = await account_conn.call(phone, _apply_profile_local,
                                             first, last, bio, timeout=60)
            if ch:
                changed += 1
                rows.append(f"• {phone} : ✅ changed")
            else:
                unchanged += 1
                rows.append(f"• {phone} : ⏸ unchanged")
        except account_conn.InvalidAuthError:
            failed += 1
            rows.append(f"• {phone} : 🔐 invalid session (re-login)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            rows.append(f"• {phone} : ❌ {repr(e)[:60]}")
        await asyncio.sleep(config.PROFILE_SYNC_DELAY)
    await log(card("🪪 PROFILE SYNC", [
        f"✅ Changed: {changed}   ⏸ Unchanged: {unchanged}   ❌ Errors: {failed}",
        LINE, *rows, LINE, f"🕒 {now()}"]))
    try:
        await bot.send_message(config.OWNER_ID,
                               f"🪪 Profile sync finished. ✅ {changed} / ⏸ {unchanged} / ❌ {failed}",
                               buttons=main_menu(True))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Feature 4: a single account joins the SHARED verified group-link list.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"aushared_(\\d+)"))
async def automation_shared_join_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    links = db.list_verified_group_links()
    if not links:
        await event.answer("The shared list is empty. Tap Join with an account first to fill it.",
                           alert=True)
        return
    if continuous_busy(aid):
        await event.answer("An automation feature is on for this account. Turn it off first.", alert=True)
        return
    if aid in active_jobs:
        await event.answer("This account is busy. Wait.", alert=True)
        return
    await safe_edit(event,
        f"⏳ {acc['phone']} is joining from the shared list ({len(links)}) ... "
        "reports go to the log group.")
    asyncio.create_task(run_group_join(acc, links))


# --------------------------------------------------------------------------- #
# 🧠 Channel Brain (مغز کانال): ONE account builds N identical channels. For
# each channel it discovers a FRESH, non-overlapping slice of contacts (the
# base discovery ledger guarantees no repeats), creates the channel (same
# title, no tag/username), adds EXACTLY that slice, then sends the content
# (marker) to the channel. Sequential and resilient: any failure on one
# channel is logged on a separate error card and the run keeps going. Reuses
# base primitives only — _discover_for_account / rb.create_channel /
# rb.add_channel_members / _send_to_guids and the worker endpoints
# /channel/create · /channel/add · /send/to_list. Replaces the old broadcaster
# UI; the broadcaster DB helpers stay untouched.
# --------------------------------------------------------------------------- #
def _cbrain_cfg() -> dict:
    aid = db.get_setting("cbrain_account_id", "") or ""
    try:
        acc = db.get_account(int(aid)) if str(aid).strip() else None
    except Exception:
        acc = None
    return {
        "account": acc,
        "title": db.get_setting("cbrain_title", "") or "",
        "count": db.get_int_setting("cbrain_count", 0),
        "per_channel": db.get_int_setting("cbrain_per_channel", db.get_discovery_target()),
        "prefix": db.get_setting("cbrain_prefix", "") or "",
    }


def cbrain_menu_text():
    c = _cbrain_cfg()
    acc = c["account"]
    return card("🧠 Channel Brain", [
        f"• Account   : {acc['phone'] if acc else '—'}",
        f"• Channel name : {c['title'] or '—'}",
        f"• Channel count : {c['count'] or '—'}",
        f"• Contacts / channel : {c['per_channel']}",
        f"• Discovery prefix : {c['prefix'] or '—'}",
        LINE,
        "Pick an account, set the channel name and count. For each channel fresh "
        "contacts are discovered (separate, no reuse), the channel is created, contacts are added, "
        "then the marker is sent. Sequential and non-stop.",
    ])


def cbrain_menu_buttons():
    return [
        [Button.inline("👤 Select Account", b"cb_acc")],
        [Button.inline("✏️ Channel Name", b"cb_title"),
         Button.inline("🔢 Channel Count", b"cb_count")],
        [Button.inline("👥 Contacts per Channel", b"cb_per"),
         Button.inline("☎️ Discovery Prefix", b"cb_prefix")],
        [Button.inline("▶️ Start Channel Brain", b"cb_start")],
        [Button.inline("🔙 Back", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"cbrain"))
async def cbrain_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, cbrain_menu_text(), buttons=cbrain_menu_buttons())


@bot.on(events.CallbackQuery(data=b"cb_acc"))
async def cbrain_acc_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("Add an account first.", alert=True)
        return
    sel = db.get_setting("cbrain_account_id", "") or ""
    rows = []
    for a in accounts:
        mark = "🔘" if str(a["id"]) == str(sel) else "⚪️"
        rows.append([Button.inline(f"{mark} {a['phone']}",
                                   f"cbacc_{a['id']}".encode())])
    rows.append([Button.inline("🔙 Back", b"cbrain")])
    await safe_edit(event, "👤 Pick one account for Channel Brain (single choice):",
                    buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"cbacc_(\\d+)"))
async def cbrain_acc_pick_cb(event):
    if not is_owner(event):
        return
    db.set_setting("cbrain_account_id", str(int(event.pattern_match.group(1))))
    await cbrain_menu_cb(event)


@bot.on(events.CallbackQuery(data=b"cb_title"))
async def cbrain_title_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_cb_title"}
    await safe_edit(event, "✏️ Send the channels' name (all channels get this same name):",
                    buttons=[[Button.inline("🔙 Back", b"cbrain")]])


async def handle_cb_title(event):
    title = event.raw_text.strip()
    if not title:
        await event.respond("Name is empty. Send it again.")
        return
    db.set_setting("cbrain_title", title)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Channel name set to '{title}'.",
                        buttons=[[Button.inline("🔙 Back", b"cbrain")]])


@bot.on(events.CallbackQuery(data=b"cb_count"))
async def cbrain_count_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_cb_count"}
    await safe_edit(event, "🔢 How many channels do you want? (send a number, e.g. 5):",
                    buttons=[[Button.inline("🔙 Back", b"cbrain")]])


async def handle_cb_count(event):
    try:
        n = max(1, int(event.raw_text.strip()))
    except ValueError:
        await event.respond("Send a number.")
        return
    db.set_setting("cbrain_count", str(n))
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Channel count set to {n}.",
                        buttons=[[Button.inline("🔙 Back", b"cbrain")]])


@bot.on(events.CallbackQuery(data=b"cb_per"))
async def cbrain_per_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_cb_per"}
    await safe_edit(event, "👥 How many contacts to discover per channel? (send a number, e.g. 150):",
                    buttons=[[Button.inline("🔙 Back", b"cbrain")]])


async def handle_cb_per(event):
    try:
        n = max(1, int(event.raw_text.strip()))
    except ValueError:
        await event.respond("Send a number.")
        return
    db.set_setting("cbrain_per_channel", str(n))
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Contacts per channel set to {n}.",
                        buttons=[[Button.inline("🔙 Back", b"cbrain")]])


@bot.on(events.CallbackQuery(data=b"cb_prefix"))
async def cbrain_prefix_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_cb_prefix"}
    await safe_edit(event, "☎️ Send the contact-discovery prefix (e.g. 0913 or 09135646):",
                    buttons=[[Button.inline("🔙 Back", b"cbrain")]])


async def handle_cb_prefix(event):
    prefix = _clean_prefix(event.raw_text.strip())
    if not prefix:
        await event.respond("Invalid prefix. Send it again (e.g. 0913).")
        return
    db.set_setting("cbrain_prefix", prefix)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ Prefix set to '{prefix}'.",
                        buttons=[[Button.inline("🔙 Back", b"cbrain")]])


@bot.on(events.CallbackQuery(data=b"cb_start"))
async def cbrain_start_cb(event):
    if not is_owner(event):
        return
    c = _cbrain_cfg()
    if not c["account"]:
        await event.answer("Pick an account first.", alert=True)
        return
    if not c["title"]:
        await event.answer("Set the channel name first.", alert=True)
        return
    if not c["count"]:
        await event.answer("Set the channel count first.", alert=True)
        return
    if not c["prefix"]:
        await event.answer("Set the discovery prefix first.", alert=True)
        return
    if cbrain_jobs.get(event.sender_id):
        await event.answer("A Channel Brain run is already in progress.", alert=True)
        return
    await safe_edit(event,
        f"🧠 Channel Brain started: {c['count']} channels on {c['account']['phone']}.\n"
        "The live progress panel and reports will follow.",
        buttons=[[Button.inline("⏹ Stop Channel Brain", b"cb_stop")],
                 [Button.inline("🏠 Main Menu", b"home")]])
    asyncio.create_task(_run_channel_brain(event.sender_id, c))


@bot.on(events.CallbackQuery(data=b"cb_stop"))
async def cbrain_stop_cb(event):
    if not is_owner(event):
        return
    ctl = cbrain_jobs.get(event.sender_id)
    if not ctl:
        await event.answer("Nothing to stop.", alert=True)
        return
    ctl["stop"] = True
    ctl["pause"] = False
    await event.answer("Stop requested. It will stop after the current step.", alert=True)


def _cbrain_bar(done, total, width=10):
    total = max(1, int(total or 0))
    filled = min(width, int(round(width * min(done, total) / total)))
    return "█" * filled + "░" * (width - filled)


def _cbrain_live_card(ctl) -> str:
    """Live progress card (English), styled after the user's #channel sketch."""
    idx = ctl.get("channel_index", 0)
    total = ctl.get("count", 0)
    per = ctl.get("per_channel", 0)
    found = ctl.get("found", 0)
    added = ctl.get("channel_added", 0)
    status = "STOPPING" if ctl.get("stop") else "RUNNING"
    return "\n".join([
        "| ⚙ - #channel",
        LINE,
        f"--| Phone - {ctl.get('phone', '')}",
        f"• Name CH : {ctl.get('title', '')}",
        f"• Channel : {idx}/{total}",
        f"• Phase   : {ctl.get('phase', '-')}",
        f"• Contacts: {found}/{per}  [{_cbrain_bar(found, per)}]",
        f"• Added   : {added}",
        f"• Status  : {status}",
        LINE,
        f"--| 🌍 - Worker : {ctl.get('worker_tag', 'local')}",
        f"⏰ : {now()}",
    ])


async def _cbrain_live_loop(owner_id, ctl, msg):
    while not ctl.get("finished"):
        try:
            await safe_edit(msg, _cbrain_live_card(ctl),
                            buttons=[[Button.inline("⏹ Stop Channel Brain", b"cb_stop")]])
        except Exception:
            pass
        await asyncio.sleep(max(1.0, config.CONTACT_PROGRESS_EVERY))


def _cbrain_error_card(phone, ch_index, phase, err) -> str:
    return card("❌ CHANNEL BRAIN — ERROR", [
        f"☎️ ACCOUNT : {phone}",
        f"🔢 CHANNEL : {ch_index}",
        f"⚙️ PHASE   : {phase}",
        f"💥 ERROR   : {type(err).__name__}: {str(err)[:300]}",
        f"⏰ : {now()}",
    ])


# Per-channel result card. Title carries the HONEST status so we never print a
# green "DONE" when the channel is only partially built or the content was not
# delivered. "ADDED" is reported as "accepted by the add API", not a verified
# member count (the base primitive gives no real count).
_CBRAIN_STATUS = {
    "COMPLETED": "🧠 CHANNEL BRAIN — CHANNEL COMPLETED ✅",
    "PARTIAL":   "🧠 CHANNEL BRAIN — CHANNEL PARTIAL ⚠️",
    "FAILED":    "🧠 CHANNEL BRAIN — CHANNEL FAILED ❌",
}


def _cbrain_result_card(status, phone, ch_index, count, title,
                        built, per, accepted, requested,
                        send_ok, send_fail, note="") -> str:
    lines = [
        f"☎️ ACCOUNT : {phone}",
        f"🔢 CHANNEL : {ch_index}/{count}",
        f"🎛 NAME    : {title}",
        f"👥 BUILT   : {built}/{per}",
        f"➕ ADDED   : {accepted}/{requested}  (accepted by API)",
        f"📤 SENT    : ok={send_ok} · fail={send_fail}",
    ]
    if note:
        lines.append(f"ℹ️ NOTE    : {note}")
    lines.append(f"⏰ : {now()}")
    return card(_CBRAIN_STATUS.get(status, _CBRAIN_STATUS["PARTIAL"]), lines)


async def _cbrain_create_channel(acc, title):
    """Create ONE channel with the given title (create-only, no marker forward).
    Local or worker. Returns the channel guid."""
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        res = await worker.api_call(w, "POST", "/channel/create", {
            "phone": acc["phone"], "marker": "", "title": title,
            "forward": False}, timeout=120)
        if not res.get("ok") or not res.get("channel_guid"):
            raise RuntimeError(res.get("error", "channel create failed"))
        return res["channel_guid"]

    async def _do(client):
        return await rb.create_channel(client, title)
    return await account_conn.call(acc["phone"], _do, timeout=120)


async def _cbrain_add_exact(acc, channel_guid, guids):
    """Add EXACTLY these guids to the channel, in batches. Local or worker.
    Returns a dict {"requested", "accepted", "failed_batches"}. 'accepted' is
    what the add API accepted (NOT a verified member count). Local and worker
    paths report the SAME shape so the runner can judge success identically."""
    requested = len(guids or [])
    if not guids:
        return {"requested": 0, "accepted": 0, "failed_batches": 0}
    batch = config.CHANNEL_ADD_BATCH
    delay = config.CHANNEL_ADD_DELAY
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        res = await worker.api_call(w, "POST", "/channel/add", {
            "phone": acc["phone"], "channel_guid": channel_guid,
            "guids": list(guids), "batch": batch, "delay": delay,
        }, timeout=1800)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "channel add failed"))
        accepted = res.get("accepted", res.get("added", 0))
        return {"requested": res.get("requested", requested),
                "accepted": accepted,
                "failed_batches": res.get("failed_batches", 0)}

    async def _do(client):
        accepted = 0
        failed_batches = 0
        step = max(1, int(batch))
        for i in range(0, len(guids), step):
            chunk = guids[i:i + step]
            try:
                await rb.add_channel_members(client, channel_guid, chunk)
                accepted += len(chunk)
            except Exception:
                # count the failed batch instead of silently pretending it
                # succeeded; keep going with the remaining batches.
                failed_batches += 1
            if i + step < len(guids):
                await asyncio.sleep(max(0.0, float(delay)))
        return {"requested": requested, "accepted": accepted,
                "failed_batches": failed_batches}
    return await account_conn.call(acc["phone"], _do, timeout=1800)


async def _run_channel_brain(owner_id, cfg):
    acc = cfg["account"]
    title = cfg["title"]
    count = int(cfg["count"])
    per_channel = int(cfg["per_channel"])
    prefix = cfg["prefix"]
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    worker_tag = w["tag"] if (w and not worker.is_local(w)) else "local"

    ctl = {"stop": False, "pause": False, "finished": False,
           "phone": acc["phone"], "title": title, "count": count,
           "per_channel": per_channel, "worker_tag": worker_tag,
           "channel_index": 0, "phase": "START",
           "found": 0, "probed": 0, "channel_added": 0}
    cbrain_jobs[owner_id] = ctl

    await log(card("🧠 CHANNEL BRAIN — START", [
        f"☎️ ACCOUNT : {acc['phone']}",
        f"🎛 NAME    : {title}",
        f"🔢 CHANNELS: {count}",
        f"👥 PER CH  : {per_channel}",
        f"☎️ PREFIX  : {prefix}",
        f"🌍 WORKER  : {worker_tag}",
        f"⏰ : {now()}"]))

    try:
        msg = await bot.send_message(
            owner_id, _cbrain_live_card(ctl),
            buttons=[[Button.inline("⏹ Stop Channel Brain", b"cb_stop")]])
    except Exception:
        msg = None
    loop_task = asyncio.create_task(_cbrain_live_loop(owner_id, ctl, msg)) if msg else None

    # honest per-outcome counters (COMPLETED means truly finished, not just
    # "the loop ran once"). PARTIAL/FAILED are tracked separately.
    completed = 0
    partial = 0
    failed = 0
    total_built = 0
    total_accepted = 0
    total_sent = 0
    account_dead = False

    # CREATE is retried with the SAME frozen slice (no re-discovery); total
    # attempts = 1 + RESUME_MAX_RETRIES, spaced by CHANNEL_ADD_DELAY. No new
    # config knob is introduced.
    create_attempts = 1 + max(0, int(config.RESUME_MAX_RETRIES))

    try:
        for ch_index in range(1, count + 1):
            if ctl.get("stop"):
                break
            ctl["channel_index"] = ch_index
            ctl["found"] = 0
            ctl["probed"] = 0
            ctl["channel_added"] = 0

            # ---- Phase 1: BUILD CONTACTS (fresh, non-overlapping slice) ----
            ctl["phase"] = "BUILD CONTACTS"
            guids = []
            try:
                guids = await _discover_for_account(acc, prefix, per_channel, ctl,
                                                    tag=f"#CH{ch_index} ")
            except account_conn.InvalidAuthError:
                account_dead = True
                db.set_status(acc["id"], "inactive")
                await log(_cbrain_error_card(acc["phone"], ch_index, "BUILD CONTACTS",
                                             RuntimeError("session invalid")))
                break
            except Exception as e:  # noqa: BLE001
                # BUILD exception -> this channel is FAILED (nothing created).
                failed += 1
                await log(_cbrain_error_card(acc["phone"], ch_index, "BUILD CONTACTS", e))
                continue
            total_built += len(guids)
            if ctl.get("stop"):
                break

            # ---- Phase 2: CREATE CHANNEL (create-only, same title) + retry ----
            ctl["phase"] = "CREATE CHANNEL"
            channel_guid = None
            create_err = None
            for attempt in range(1, create_attempts + 1):
                try:
                    channel_guid = await _cbrain_create_channel(acc, title)
                    create_err = None
                    break
                except account_conn.InvalidAuthError:
                    account_dead = True
                    db.set_status(acc["id"], "inactive")
                    await log(_cbrain_error_card(acc["phone"], ch_index, "CREATE CHANNEL",
                                                 RuntimeError("session invalid")))
                    break
                except Exception as e:  # noqa: BLE001
                    create_err = e
                    if attempt < create_attempts:
                        await asyncio.sleep(max(0.0, float(config.CHANNEL_ADD_DELAY)))
            if account_dead:
                break
            if not channel_guid:
                # CREATE failed after every retry -> FAILED, move to next channel.
                failed += 1
                await log(_cbrain_error_card(
                    acc["phone"], ch_index,
                    f"CREATE CHANNEL (after {create_attempts} attempts)",
                    create_err or RuntimeError("channel create failed")))
                await log(_cbrain_result_card(
                    "FAILED", acc["phone"], ch_index, count, title,
                    len(guids), per_channel, 0, len(guids), 0, 0,
                    note="channel not created"))
                continue

            # ---- Phase 3: ADD CONTACTS (exact frozen slice) ----
            ctl["phase"] = "ADD CONTACTS"
            add_res = {"requested": len(guids), "accepted": 0, "failed_batches": 0}
            add_error = None
            try:
                add_res = await _cbrain_add_exact(acc, channel_guid, guids)
            except account_conn.InvalidAuthError:
                account_dead = True
                db.set_status(acc["id"], "inactive")
                await log(_cbrain_error_card(acc["phone"], ch_index, "ADD CONTACTS",
                                             RuntimeError("session invalid")))
                break
            except Exception as e:  # noqa: BLE001
                add_error = e
                await log(_cbrain_error_card(acc["phone"], ch_index, "ADD CONTACTS", e))
            requested = int(add_res.get("requested", len(guids)))
            accepted = int(add_res.get("accepted", 0))
            failed_batches = int(add_res.get("failed_batches", 0))
            ctl["channel_added"] = accepted
            total_accepted += accepted

            # ---- Phase 4: SEND CONTENT (marker -> the channel) ----
            ctl["phase"] = "SEND CONTENT"
            send_ok = send_fail = 0
            marker_missing = False
            if marker:
                try:
                    send_ok, send_fail = await _send_to_guids(
                        owner_id, acc, [channel_guid], "marker", "",
                        tag=f"#CH{ch_index} ")
                    # (0, 0) means the marker message was not found -> nothing sent.
                    marker_missing = (send_ok == 0 and send_fail == 0)
                except account_conn.InvalidAuthError:
                    account_dead = True
                    db.set_status(acc["id"], "inactive")
                    await log(_cbrain_error_card(acc["phone"], ch_index, "SEND CONTENT",
                                                 RuntimeError("session invalid")))
                    break
                except Exception as e:  # noqa: BLE001
                    await log(_cbrain_error_card(acc["phone"], ch_index, "SEND CONTENT", e))
            else:
                marker_missing = True
                await log(_cbrain_error_card(acc["phone"], ch_index, "SEND CONTENT",
                                             RuntimeError("no marker set — content skipped")))
            total_sent += send_ok

            # ---- Honest per-channel verdict (no fake DONE) ----
            built_full = len(guids) >= per_channel
            add_full = (add_error is None and failed_batches == 0
                        and requested > 0 and accepted >= requested)
            send_done = (send_ok >= 1 and send_fail == 0)
            if built_full and add_full and send_done:
                status = "COMPLETED"
                completed += 1
                note = ""
            else:
                status = "PARTIAL"
                partial += 1
                reasons = []
                if not built_full:
                    reasons.append("build shortfall")
                if add_error is not None:
                    reasons.append("add error")
                elif failed_batches > 0:
                    reasons.append(f"{failed_batches} add batch(es) failed")
                elif requested == 0:
                    reasons.append("no contacts to add")
                elif accepted < requested:
                    reasons.append("some contacts not accepted")
                if marker_missing:
                    if not marker:
                        reasons.append("no marker set — content skipped")
                    else:
                        reasons.append("marker message not found — content not sent")
                elif send_fail > 0:
                    reasons.append("send failure")
                note = ", ".join(reasons)
            await log(_cbrain_result_card(
                status, acc["phone"], ch_index, count, title,
                len(guids), per_channel, accepted, requested,
                send_ok, send_fail, note=note))
    except Exception as e:  # noqa: BLE001
        # Unexpected failure of the whole runner: surface it, then let `finally`
        # clean up the live task / job registry and post the final card.
        await log(_cbrain_error_card(acc["phone"], ctl.get("channel_index", 0),
                                     "RUNNER", e))
    finally:
        # ---- finish (ALWAYS runs, even on an unexpected exception) ----
        ctl["finished"] = True
        if loop_task:
            loop_task.cancel()
        cbrain_jobs.pop(owner_id, None)
        stopped = ctl.get("stop")
        status_line = ("🔴 SESSION INVALID" if account_dead
                       else ("⏹ STOPPED" if stopped else "✅ FINISHED"))
        final = card("🧠 CHANNEL BRAIN — " + status_line, [
            f"☎️ ACCOUNT        : {acc['phone']}",
            f"🎛 NAME           : {title}",
            f"✅ COMPLETED      : {completed}/{count}",
            f"⚠️ PARTIAL        : {partial}",
            f"❌ FAILED         : {failed}",
            f"👥 CONTACTS BUILT : {total_built}",
            f"➕ TOTAL ACCEPTED : {total_accepted}",
            f"📤 TOTAL SENT     : {total_sent}",
            f"⏰ : {now()}"])
        try:
            await log(final)
        except Exception:
            pass
        if msg is not None:
            try:
                await safe_edit(msg, final,
                                buttons=[[Button.inline("🏠 Main Menu", b"home")]])
            except Exception:
                pass
        try:
            await bot.send_message(owner_id, final,
                                   buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 🖼 PV image -> PDF export: download EVERY photo from an account's private
# (user) chats and send them back as a single PDF. Local or worker. No photo
# is skipped (only photos — videos/gifs/files are ignored, as requested).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"pvexport"))
async def pvexport_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("Add an account first.", alert=True)
        return
    rows = [[Button.inline(f"🖼 {a['phone']}", f"pvx_{a['id']}".encode())]
            for a in accounts]
    rows.append([Button.inline("🔙 Back", b"home")])
    await safe_edit(event,
        "🖼 From which account should I collect PV photos and send a PDF?\n"
        "(photos only — no video/gif. No photo is skipped.)", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"pvx_(\\d+)"))
async def pvexport_run_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    await safe_edit(event,
        f"⏳ Starting to collect PV photos for {acc['phone']} ... this may take a few minutes. "
        "When it's ready, the PDF will be sent to you.",
        buttons=[[Button.inline("🏠 Main Menu", b"home")]])
    asyncio.create_task(run_pv_export(event.sender_id, acc))


async def _pv_collect_photos(acc, on_batch=None) -> list:
    """Return a list of raw image byte-blobs from the account's PV chats.
    Local: download directly (and call on_batch(list_so_far) every
    PV_GROUP_BATCH photos for LIVE cumulative sending). Worker: ask worker."""
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        import base64
        res = await worker.api_call(w, "POST", "/pvexport/run", {
            "phone": acc["phone"], "max_chats": config.PV_EXPORT_MAX_CHATS,
            "max_photos": config.PV_EXPORT_MAX_PHOTOS}, timeout=1800)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "pvexport failed"))
        return [base64.b64decode(x) for x in (res.get("photos_b64") or [])]

    batch = max(1, int(config.PV_GROUP_BATCH))

    async def _do(client):
        out = []
        guids = await rb.get_chat_list_guids(client, only_users=True)
        for g in guids[:config.PV_EXPORT_MAX_CHATS]:
            async for _mid, fi in rb.iter_chat_photos(client, g):
                try:
                    blob = await rb.download_photo(client, fi)
                    if blob:
                        out.append(blob)
                        # LIVE cumulative: every `batch` photos -> send all so far
                        if on_batch is not None and len(out) % batch == 0:
                            try:
                                await on_batch(list(out))
                            except Exception:
                                pass
                except Exception:
                    continue
                if len(out) >= config.PV_EXPORT_MAX_PHOTOS:
                    return out
        return out
    return await account_conn.call(acc["phone"], _do, timeout=1800)


async def _pv_build_and_send(phone, photos, final=False):
    """Build a PDF of ALL `photos` so far and send it to the LOG GROUP.
    Cumulative: each call includes everything collected up to now."""
    import pdf_export
    path = os.path.join(
        DATA_DIR, f"pv_{phone}_{len(photos)}_{int(datetime.now().timestamp())}.pdf")
    try:
        n = await asyncio.to_thread(pdf_export.build_pdf, photos, path)
        cap = card(
            "🖼 PV Photo Archive — Full Final File ✅" if final
            else "🖼 PV Photo Archive (live cumulative)", [
                f"📱 {phone}",
                f"• Photos in this file (cumulative) : {n}",
                ("🏁 Done" if final else "⏳ Continuing ..."),
                f"🕒 {now()}"])
        await bot.send_file(config.LOG_GROUP_ID, path, caption=cap, force_document=True)
        return n
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


async def run_pv_export(owner_id: int, acc):
    phone = acc["phone"]
    batch = max(1, int(config.PV_GROUP_BATCH))
    last_sent = {"n": 0}

    # LIVE cumulative: every `batch` photos found -> send a PDF of EVERYTHING
    # collected so far (20, then 40, then 60, ... growing) to the log group.
    async def on_batch(photos_so_far):
        await log(card("📸 Live Collection", [
            f"📱 {phone}", f"• Found so far : {len(photos_so_far)}", f"🕒 {now()}"]))
        await _pv_build_and_send(phone, list(photos_so_far), final=False)
        last_sent["n"] = len(photos_so_far)

    try:
        photos = await _pv_collect_photos(acc, on_batch=on_batch)
    except account_conn.InvalidAuthError:
        await _log_invalid_auth(phone)
        return
    except Exception as e:  # noqa: BLE001
        await log(card("🖼 PV Photo Archive — Error", [
            f"👤 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        try:
            await bot.send_message(owner_id, f"❌ Collecting photos for {phone} failed: {repr(e)[:120]}")
        except Exception:
            pass
        return

    if not photos:
        await bot.send_message(owner_id, f"ℹ️ No photos were found in {phone}'s PVs.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
        return

    total_photos = len(photos)

    # Remote accounts return all photos at once (no live stream), so do the
    # cumulative growing sends here: 20, 40, 60, ... to the group.
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        i = batch
        while i < total_photos:
            await log(card("📸 Collection", [
                f"📱 {phone}", f"• So far : {i} of {total_photos}", f"🕒 {now()}"]))
            await _pv_build_and_send(phone, photos[:i], final=False)
            i += batch
        last_sent["n"] = min(i, total_photos)

    # FINAL complete one-piece file (ALL photos) to the group.
    try:
        n_final = await _pv_build_and_send(phone, photos, final=True)
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ Photo Archive — Final File Error", [f"👤 {phone}", f"💥 {repr(e)[:140]}"]))
        n_final = total_photos

    # final "پایان" summary card to the group
    await log(card("🏁 PV Photo Archive — Done", [
        f"👤 {phone}",
        f"• Total photos : {total_photos}",
        f"• Full final file sent ({n_final} photos)",
        f"🕒 {now()}"]))

    # also send the full one-piece PDF to the owner.
    import pdf_export
    out_path = os.path.join(DATA_DIR, f"pv_{phone}_{int(datetime.now().timestamp())}.pdf")
    try:
        n = await asyncio.to_thread(pdf_export.build_pdf, photos, out_path)
    except Exception as e:  # noqa: BLE001
        await bot.send_message(owner_id, f"❌ Building the final PDF failed: {repr(e)[:120]}")
        return
    try:
        await bot.send_file(owner_id, out_path,
                            caption=f"🖼 Full PV photo archive for {phone}\nCount: {n} photos",
                            force_document=True)
        await bot.send_message(owner_id, "✅ Full archive sent.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Recovery (on boot) + worker relay loop for the EXTRAS.
# --------------------------------------------------------------------------- #
async def recover_extras():
    """Relaunch every EXTRA feature that was enabled before a restart."""
    for sec in db.list_enabled_secretaries():
        acc = db.get_account(sec["account_id"])
        if acc:
            try:
                await start_secretary(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ Secretary restore for {acc['phone']} failed: {repr(e)[:120]}")
    for cr in db.list_enabled_channel_reports():
        acc = db.get_account(cr["account_id"])
        if acc:
            try:
                await start_channelreport(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ Channel-report restore for {acc['phone']} failed: {repr(e)[:120]}")
    for rr in db.list_enabled_reply_responders():
        acc = db.get_account(rr["account_id"])
        if acc:
            try:
                await start_reply(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ Reply restore for {acc['phone']} failed: {repr(e)[:120]}")


async def _heal_remote_extra(acc, status_path, starter):
    w = worker.worker_for_account(acc)
    if not (w and not worker.is_local(w)):
        return
    try:
        stt = await worker.api_call(w, "GET", f"{status_path}?phone={acc['phone']}")
        if not stt.get("running"):           # worker container restarted
            await starter(acc)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Health & self-heal engine (موتور سلامت و خودتعمیر).
# Periodically: verify every account's session, optionally deactivate the dead
# ones, relaunch any enabled-but-stopped automation, and post one overall
# system-health card. This is the automatic counterpart of the manual sweep.
# --------------------------------------------------------------------------- #
async def health_engine_loop():
    while True:
        await asyncio.sleep(max(300, config.HEALTH_ENGINE_INTERVAL))
        try:
            await run_health_engine()
        except Exception as e:  # noqa: BLE001
            print(f"[health_engine] {e}")


async def run_health_engine():
    """Unified account WATCHER cycle = 🩺 health + self-heal + the portal
    observer's canonical quarantine, merged into one. For each account it
    verifies the session; a confirmed-dead one is quarantined through the SAME
    canonical logic the observer uses (double-verify, stop this account's jobs,
    snapshot/disable its automations, status=quarantined — NEVER auto-deleted:
    the owner decides from the quarantine panel). A recovered account is
    restored and its features relaunched; a healthy account with an enabled but
    stalled automation is self-healed. An offline worker leaves its accounts
    UNCHECKED (never marked Shot). Posts ONE English #watcher_health card with a
    button that opens the existing quarantine panel. The separate WORKER health
    loop (health_loop) is untouched."""
    import bot as _botmod                     # the bot module (for observer)
    from portal import observer as _observer  # canonical Watcher logic
    accounts = db.list_accounts()
    alive = 0
    shot = 0
    unchecked = 0
    healed = 0
    shot_rows = []
    for acc in accounts:
        phone = acc["phone"]
        aid = acc["id"]
        if acc.get("status") == "quarantined":
            shot += 1
            shot_rows.append(f"• {phone} : 🔴 Shot (quarantined)")
            continue
        w = worker.worker_for_account(acc)
        is_dead = False
        checked = True
        try:
            if w and not worker.is_local(w):
                try:
                    res = await worker.api_call(
                        w, "POST", "/account/verify", {"phone": phone}, timeout=90)
                    is_dead = bool(res.get("dead"))
                except Exception:
                    checked = False           # offline worker -> UNCHECKED
            else:
                is_dead = await account_conn.verify_session_dead(phone)
        except Exception:
            checked = False

        if not checked:
            unchecked += 1
            continue
        if is_dead:
            # Canonical quarantine (double-verify, stop jobs, snapshot/disable,
            # status=quarantined; never deletes). Idempotent and safe. Honors
            # the HEALTH_ENGINE_AUTODISABLE_DEAD switch for the auto action.
            if config.HEALTH_ENGINE_AUTODISABLE_DEAD:
                try:
                    await _observer._remove_confirmed_invalid(_botmod, acc)
                except Exception:
                    pass
            shot += 1
            shot_rows.append(f"• {phone} : 🔴 Shot")
        else:
            alive += 1
            # restore an account that recovered on its own (observer logic)
            try:
                if _observer._restore_automation_snapshot(aid):
                    await _recover_account_features(aid)
            except Exception:
                pass
            # self-heal: an account that is healthy AND has automation enabled
            # but whose local task is gone -> relaunch it.
            if automation_on(aid):
                t = automation_tasks.get(aid)
                w2 = worker.worker_for_account(acc)
                local = not (w2 and not worker.is_local(w2))
                if local and ((not t) or t["task"].done()):
                    try:
                        await start_automation(acc)
                        healed += 1
                    except Exception:
                        pass

    rows = [
        f"🟢 Healthy : {alive}",
        f"🔴 Shot : {shot}",
        f"♻️ Healed automations : {healed}",
    ]
    if unchecked:
        rows.append(f"❔ Unchecked (worker offline) : {unchecked}")
    if shot_rows:
        rows.append(LINE)
        rows.extend(shot_rows)
    rows.append(LINE)
    rows.append(f"🕒 {now()}")
    q_count = 0
    try:
        q_count = len(_observer.quarantined_accounts())
    except Exception:
        pass
    buttons = ([[Button.inline(f"🗑 Delete Shot Accounts ({q_count})",
                               b"portal_quarantine")]] if q_count else None)
    # #watcher_health needs an inline button, so send directly (log() is
    # text-only). Never crash the loop.
    try:
        await bot.send_message(config.LOG_GROUP_ID, card("🩺 #watcher_health", rows),
                               buttons=buttons)
    except Exception as e:  # noqa: BLE001
        print(f"[watcher_health] {e}")


async def extras_worker_loop():
    """Every 30s: drain queued log lines from each remote worker (so worker-side
    secretary/reply/report events show up in the master log group), and relaunch
    any remote EXTRA whose worker restarted."""
    while True:
        await asyncio.sleep(30)
        try:
            for w in db.list_enabled_workers():
                if worker.is_local(w):
                    continue
                try:
                    res = await worker.api_call(w, "GET", "/extras/logs", timeout=30)
                    for line in (res.get("logs") or []):
                        await log(line)
                except Exception:
                    pass
            for sec in db.list_enabled_secretaries():
                acc = db.get_account(sec["account_id"])
                if acc:
                    await _heal_remote_extra(acc, "/secretary/status", start_secretary)
            for cr in db.list_enabled_channel_reports():
                acc = db.get_account(cr["account_id"])
                if acc:
                    await _heal_remote_extra(acc, "/channelreport/status", start_channelreport)
            for rr in db.list_enabled_reply_responders():
                acc = db.get_account(rr["account_id"])
                if acc:
                    await _heal_remote_extra(acc, "/reply/status", start_reply)
        except Exception as e:  # noqa: BLE001
            print(f"[extras_worker_loop] {e}")


# --------------------------------------------------------------------------- #
# Background health monitor: immediate alerts + periodic STATU WORKER ALL.
# --------------------------------------------------------------------------- #
async def worker_snapshot_loop():
    """Single source of worker health. Every ~25s it probes all ENABLED workers
    using ONLY warm tunnels (never opens a cold SSH connect — the per-worker
    supervisor owns (re)connection), stores the result in worker's in-memory
    snapshot, posts an alert on a healthy->unhealthy transition, and posts the
    full status card on the slow HEALTH_INTERVAL cadence. The Refresh button
    renders this snapshot instantly and never probes."""
    import time as _t
    prev_status: dict = {}
    last_report = 0.0
    while True:
        try:
            workers = db.list_workers()
            if workers:
                results = await worker.check_all(workers, warm_only=True)
                for r in results:
                    old = prev_status.get(r["id"])
                    if old == "ok" and r["status"] != "ok":
                        kind = "blocked" if r["status"] == "blocked" else "down"
                        await log(card("🚨 WORKER ALERT", [
                            f"👨‍🔧 {r['tag']} • {r['ip']}",
                            f"status: 🟢 healthy  ->  🔴 {kind}",
                            f"detail: {r.get('detail') or '—'}",
                            f"🕒 {now()}",
                        ]))
                    prev_status[r["id"]] = r["status"]
                now_t = _t.monotonic()
                if now_t - last_report >= config.HEALTH_INTERVAL:
                    await log(worker_status_all_card(db.list_workers()))
                    last_report = now_t
        except Exception as e:  # noqa: BLE001
            print(f"[worker_snapshot_loop] {e}")
        await asyncio.sleep(25)


async def health_loop():
    import time as _t
    prev_status: dict = {}
    last_report = 0.0
    quick = min(300, max(60, config.HEALTH_INTERVAL))
    while True:
        try:
            workers = db.list_workers()
            if workers:
                results = await worker.check_all(workers)
                for r in results:
                    old = prev_status.get(r["id"])
                    if old == "ok" and r["status"] != "ok":
                        kind = "blocked" if r["status"] == "blocked" else "down"
                        await log(card("🚨 WORKER ALERT", [
                            f"👨‍🔧 {r['tag']} • {r['ip']}",
                            f"status: 🟢 healthy  ->  🔴 {kind}",
                            f"🕒 {now()}",
                        ]))
                    prev_status[r["id"]] = r["status"]
                now_t = _t.monotonic()
                if now_t - last_report >= config.HEALTH_INTERVAL:
                    await log(worker_status_all_card(db.list_workers()))
                    last_report = now_t
        except Exception as e:  # noqa: BLE001
            print(f"[health_loop] {e}")
        await asyncio.sleep(quick)


# =========================================================================== #
# ✈️ TELEGRAM SECTION (additive — mirrors the Rubika side, never touches it)
# Built with Telethon userbots, reusing config.API_ID / config.API_HASH.
# Phases delivered here: foundation+login, send-to-mutual-contacts (text/media
# +caption, speed 0.2-1, live stats), and concurrent tabchi (group posting,
# text config, group-count at start, pinned live stats, human typing).
# =========================================================================== #
TG_MEDIA_DIR = os.path.join(DATA_DIR, "tg_media")


def _tg_media_path(event, prefix: str = "tg") -> str:
    """Build a download path that keeps the EXACT real file name + extension.

    YoudonoaAx UPDATE (step 4): each file goes into its OWN unique sub-folder,
    so the file itself keeps its EXACT original name (e.g. ``myfile.zip``) with
    NO timestamp prefix, while collisions are still avoided by the unique
    folder. Media that genuinely has no name (photos / voice) gets a sensible
    name WITH the correct extension."""
    real = ""
    ext = ""
    try:
        f = getattr(event, "file", None)
        real = getattr(f, "name", None) or ""
        ext = getattr(f, "ext", None) or ""
    except Exception:  # noqa: BLE001
        real, ext = "", ""
    real = os.path.basename(real).strip()
    sub = os.path.join(TG_MEDIA_DIR, str(int(time.time() * 1000)))  # unique folder
    if real:
        return os.path.join(sub, real)
    safe_ext = ext if ext.startswith(".") else (("." + ext) if ext else "")
    return os.path.join(sub, f"{prefix}_{int(time.time())}{safe_ext}")


def _tg_typing_secs() -> float:
    return random.uniform(config.TG_TYPING_MIN, config.TG_TYPING_MAX)


def _tg_msgs_summary() -> str:
    """One-line summary of the unified ordered send list (step 3)."""
    msgs = db.tg_msgs_get()
    if not msgs:
        return "—"
    n_text = sum(1 for m in msgs if m.get("type") == "text")
    n_media = sum(1 for m in msgs if m.get("type") == "media")
    parts = []
    if n_text:
        parts.append(f"✍️ {n_text} text")
    if n_media:
        parts.append(f"🖼 {n_media} file")
    return f"{len(msgs)} items ({' + '.join(parts)})"


def _tg_menu_buttons():
    """Simple navigation back into the Telegram panel (used after config saves)."""
    return [[Button.inline("🔙 Telegram Panel", b"tg")]]


@bot.on(events.CallbackQuery(data=b"tg"))
async def tg_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    accs = db.tg_list_accounts()
    rows = []
    for a in accs:
        mark = "🟢" if a["status"] == "active" else "🔴"
        rows.append([Button.inline(f"{mark} {a['phone']} — {a['name']}",
                                   f"tgacc_{a['rid']}".encode())])
    rows.append([Button.inline("➕ Add Account", b"tgadd")])
    rows.append([Button.inline("📤 Multi-account Send", b"tg_multi"),
                 Button.inline("📊 Send Status", b"tg_multi_jobs")])
    rows.append([Button.inline("🔙 Back to Rubika", b"home")])
    head = card("✈️ TELEGRAM PANEL", [
        f"• Accounts : {len(accs)}",
        "Pick an account, set the send content, and send it to its contacts.",
    ])
    await safe_edit(event, head, buttons=rows)


# ----- login -----
@bot.on(events.CallbackQuery(data=b"tgadd"))
async def tg_add_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_tg_phone"}
    await safe_edit(event,
        "✈️ Send the Telegram account phone with country code (e.g. +98912...).",
        buttons=[[Button.inline("🔙 Cancel", b"tg")]])


async def handle_tg_phone(event):
    phone = _normalize_phone_input(event)
    if not phone:
        await event.respond(
            "❌ Couldn't read the number. Send it with the country code (e.g. +989121234567 "
            "or 09121234567) or cancel.")
        return
    await event.respond("⏳ Connecting to Telegram and sending the code ...")
    try:
        ctx = await tg.start_login(phone)
    except Exception as e:  # noqa: BLE001
        state.pop(event.sender_id, None)
        await event.respond(f"❌ Error sending the code: {repr(e)[:160]}")
        return
    tg_pending[event.sender_id] = ctx
    state[event.sender_id] = {"step": "await_tg_code"}
    await event.respond("📩 The Telegram login code arrived. Send the code.",
                        buttons=[[Button.inline("🔙 Cancel", b"tg")]])


async def handle_tg_code(event):
    ctx = tg_pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        await tg.finish_login(ctx, code)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "SessionPasswordNeededError":
            state[event.sender_id] = {"step": "await_tg_password"}
            await event.respond("🔐 This account has two-step verification. Send the password.",
                                buttons=[[Button.inline("🔙 Cancel", b"tg")]])
            return
        await event.respond(f"❌ Wrong code/error: {repr(e)[:160]}\nSend the code again or cancel.")
        return
    await _tg_complete_login(event, ctx)


async def handle_tg_password(event):
    ctx = tg_pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    try:
        await tg.finish_password(ctx, event.raw_text.strip())
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Wrong password/error: {repr(e)[:160]}\nSend the password again.")
        return
    await _tg_complete_login(event, ctx)


async def _tg_complete_login(event, ctx):
    tg_pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    try:
        info = await tg.commit_login(ctx)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Registering the account failed: {repr(e)[:160]}")
        return
    rows = [
        "• Status          : SUCCESS",
        f"• Phone           : {ctx['phone']}",
        f"• Name            : {info.get('name', '—')}",
        (f"• Username        : @{info.get('username')}"
         if info.get("username") else "• Username        : —"),
        f"• Contacts        : {info.get('contacts', 0)}",
        f"• Mutual Contacts : {info.get('mutuals', 0)}",
        f"• Groups          : {info.get('groups', 0)}",
        "• Session Saved   : YES",
        f"• Time            : {now()}",
    ]
    tg_login_card = panel_card("✅ - #telegram_login", rows,
                               footer="--| ✈️ - Platform : Telegram")
    await log(tg_login_card)
    rid = next((a["rid"] for a in db.tg_list_accounts()
                if a["phone"] == ctx["phone"]), None)
    btns = []
    if rid:
        btns.append([Button.inline("⚙️ Manage / Send this Account", f"tgacc_{rid}".encode())])
    btns.append([Button.inline("🔙 Telegram Panel", b"tg")])
    await event.respond(tg_login_card, buttons=btns)


# ----- account list / delete -----
@bot.on(events.CallbackQuery(data=b"tgaccs"))
async def tg_accounts_cb(event):
    if not is_owner(event):
        return
    accs = db.tg_list_accounts()
    if not accs:
        await event.answer("You haven't added a Telegram account yet.", alert=True)
        return
    rows = [[Button.inline(
        f"{'🟢' if a['status'] == 'active' else '🔴'} {a['phone']} — {a['name']} "
        f"(👥{a['contacts']})", f"tgacc_{a['rid']}".encode())] for a in accs]
    rows.append([Button.inline("🔙 Back", b"tg")])
    await safe_edit(event, "✈️ Telegram accounts:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"tgacc_(\\d+)"))
async def tg_account_menu_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("Not found.", alert=True)
        return
    phone = acc["phone"]
    rid = acc["rid"]
    lines = [
        f"📱 {phone}  ({acc['name']})", LINE,
        f"👥 Contacts : {acc['contacts']}    ✉️ Sent : {acc['sent_total']}    "
        f"↩️ Replies : {acc['replied_total']}",
        f"📨 Send content : {_tg_msgs_summary()}    ⏱ Speed: {db.tg_get_send_delay()}s",
    ]
    rows = [
        [Button.inline("📤 Send to Contacts", f"tgrun_{rid}".encode())],
        [Button.inline("📨 Send Content", b"tgmsgs"),
         Button.inline("⏱ Send Speed", b"tgspeed")],
        [Button.inline("🔄 Reset Sent List (re-send to all)", b"tgdedup")],
        [Button.inline("🗑 Delete Account", f"tgdel_{rid}".encode()),
         Button.inline("🔙 Telegram Panel", b"tg")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


# ---- per-account engine toggles (integrated panel) ----
@bot.on(events.CallbackQuery(pattern=b"tgrun_(\\d+)"))
async def tg_run_send_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("Not found.", alert=True)
        return
    phone = acc["phone"]
    if not db.tg_msgs_get():
        await event.answer("Set Send Content first.", alert=True)
        return
    if phone in tg_jobs:
        await event.answer("A send is already in progress on this account.", alert=True)
        return
    await safe_edit(event, f"📤 Sending to {phone}'s contacts started (mutuals first). Reports go to the log group.",
                    buttons=[[Button.inline("🔙 Telegram Panel", b"tg")]])
    asyncio.create_task(_tg_run_mutual(event.sender_id, acc))


@bot.on(events.CallbackQuery(pattern=b"tgdel_(\\d+)"))
async def tg_delete_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("Not found.", alert=True)
        return
    phone = acc["phone"]
    await tg.drop_client(phone)
    db.tg_delete_account(phone)
    await event.answer("Deleted.")
    await tg_menu_cb(event)


# ----- dedup reset: re-send to everyone again (YoudonoaAx UPDATE) ----------- #
@bot.on(events.CallbackQuery(data=b"tgdedup"))
async def tg_dedup_cb(event):
    if not is_owner(event):
        return
    n = db.tg_dedup_count()
    await safe_edit(event, card("🔄 RESET SENT LIST", [
        f"Right now {n} contacts are marked as already-sent and will be skipped next time.",
        "If you want to send new content to everyone again, clear this list.",
        "⚠️ This is irreversible (but it deletes no account/content).",
    ]), buttons=[
        [Button.inline("✅ Clear and re-send to everyone", b"tgdedupyes")],
        [Button.inline("🔙 Cancel", b"tg")],
    ])


@bot.on(events.CallbackQuery(data=b"tgdedupyes"))
async def tg_dedup_yes_cb(event):
    if not is_owner(event):
        return
    n = db.tg_dedup_count()
    db.tg_clear_dedup()
    await log(card("🔄 TELEGRAM SENT LIST CLEARED", [
        f"🗑 {n} contacts removed from the duplicate state.", f"🕒 {now()}"]))
    await safe_edit(event, card("✅ Done", [
        f"{n} contacts cleared. The next send goes to everyone again.",
    ]), buttons=[[Button.inline("🔙 Telegram Panel", b"tg")]])


# ----- unified ordered send content (YoudonoaAx UPDATE, step 3) ------------- #
# Telegram-only «📨 محتوای ارسال»: one ORDERED list merging the old «محتوا۱»
# (tgmcontent) and «متن۲» (tgtext2). Any number of texts and/or media+caption
# items; they are sent in order to every recipient.
def _tg_msgs_screen() -> str:
    msgs = db.tg_msgs_get()
    rows = [f"📨 Send content — {len(msgs)} items (sent to each contact in order)", LINE]
    if not msgs:
        rows.append("You haven't added anything yet. Add text or a file/photo.")
    else:
        for i, m in enumerate(msgs, 1):
            if m.get("type") == "text":
                rows.append(f"{i}. ✍️ Text: {(m.get('text') or '')[:60]}")
            else:
                cap = (m.get("caption") or "").strip()
                name = os.path.basename(m.get("media") or "")
                rows.append(f"{i}. 🖼 File: {name}" + (f" — caption: {cap[:40]}" if cap else ""))
    return "\n".join(rows)


@bot.on(events.CallbackQuery(data=b"tgmsgs"))
async def tg_msgs_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, _tg_msgs_screen(), buttons=[
        [Button.inline("➕ Add Text", b"tgmsg_addtext"),
         Button.inline("🖼 Add File/Photo + Caption", b"tgmsg_addmedia")],
        [Button.inline("🗑 Clear All", b"tgmsg_clear")],
        [Button.inline("🔙 Telegram Panel", b"tg")],
    ])


@bot.on(events.CallbackQuery(data=b"tgmsg_addtext"))
async def tg_msg_addtext_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_tg_msg_text"}
    await safe_edit(event,
        "✍️ Send the text (it's appended to the send-content list).",
        buttons=[[Button.inline("🔙 Back", b"tgmsgs")]])


async def handle_tg_msg_text(event):
    state.pop(event.sender_id, None)
    txt = (event.raw_text or "").strip()
    if not txt:
        await event.respond("Text is empty.", buttons=_tg_menu_buttons())
        return
    n = db.tg_msgs_add({"type": "text", "text": txt})
    await event.respond(f"✅ Text added (total items: {n}).",
                        buttons=[[Button.inline("📨 Send Content", b"tgmsgs")],
                                 [Button.inline("🔙 Telegram Panel", b"tg")]])


@bot.on(events.CallbackQuery(data=b"tgmsg_addmedia"))
async def tg_msg_addmedia_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_tg_msg_media"}
    await safe_edit(event,
        "🖼 Send the photo/voice/file (optional caption). It's saved with its real name/extension.",
        buttons=[[Button.inline("🔙 Back", b"tgmsgs")]])


async def handle_tg_msg_media(event):
    state.pop(event.sender_id, None)
    caption = (event.raw_text or "").strip()
    if not event.media:
        await event.respond("You didn't send a file/photo. For text, tap Add Text.",
                            buttons=[[Button.inline("📨 Send Content", b"tgmsgs")]])
        return
    os.makedirs(TG_MEDIA_DIR, exist_ok=True)
    dl_path = _tg_media_path(event, "c")
    os.makedirs(os.path.dirname(dl_path), exist_ok=True)   # unique per-file folder
    try:
        path = await event.download_media(file=dl_path)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ File download failed: {repr(e)[:120]}")
        return
    n = db.tg_msgs_add({"type": "media", "media": path, "caption": caption})
    await event.respond(
        f"✅ File added (total items: {n}).\nCaption: {caption[:60] or '—'}",
        buttons=[[Button.inline("📨 Send Content", b"tgmsgs")],
                 [Button.inline("🔙 Telegram Panel", b"tg")]])


@bot.on(events.CallbackQuery(data=b"tgmsg_clear"))
async def tg_msg_clear_cb(event):
    if not is_owner(event):
        return
    db.tg_msgs_clear()
    await safe_edit(event, _tg_msgs_screen(), buttons=[
        [Button.inline("➕ Add Text", b"tgmsg_addtext"),
         Button.inline("🖼 Add File/Photo + Caption", b"tgmsg_addmedia")],
        [Button.inline("🔙 Telegram Panel", b"tg")],
    ])




@bot.on(events.CallbackQuery(data=b"tgspeed"))
async def tg_speed_cb(event):
    if not is_owner(event):
        return
    cur = db.tg_get_send_delay()
    await safe_edit(event, f"⏱ Telegram send speed (0.2 to 1 second). Current: {cur}s",
        buttons=[[Button.inline("0.2s", b"tgspd_0.2"), Button.inline("0.4s", b"tgspd_0.4"),
                  Button.inline("0.6s", b"tgspd_0.6")],
                 [Button.inline("0.8s", b"tgspd_0.8"), Button.inline("1s", b"tgspd_1")],
                 [Button.inline("🔙 Back", b"tg")]])


@bot.on(events.CallbackQuery(pattern=b"tgspd_(.+)"))
async def tg_speed_set_cb(event):
    if not is_owner(event):
        return
    db.tg_set_send_delay(event.pattern_match.group(1).decode())
    await event.answer(f"⏱ Set to {db.tg_get_send_delay()}s.")
    await tg_speed_cb(event)


async def handle_tg_speed(event):
    state.pop(event.sender_id, None)
    db.tg_set_send_delay(event.raw_text.strip())
    await event.respond(f"✅ Speed set to {db.tg_get_send_delay()}s.",
                        buttons=_tg_menu_buttons())


# --------------------------------------------------------------------------- #
# Mutual-contact send (live progress + stop/pause/resume + dedup)
# --------------------------------------------------------------------------- #
def _tg_ctl_buttons(phone: str):
    paused = [[Button.inline("▶️ Resume", f"tgresume_{phone}".encode()),
               Button.inline("⏹ Stop", f"tgstop_{phone}".encode())]]
    running = [[Button.inline("⏸ Pause", f"tgpause_{phone}".encode()),
                Button.inline("⏹ Stop", f"tgstop_{phone}".encode())]]
    return paused, running


def _tg_mutual_card(ctl) -> str:
    total = ctl.get("total", 0)
    done = ctl.get("done", 0)
    pct = int(done * 100 / total) if total else 0
    status = "⏸ Pause" if ctl.get("pause") else ("⏹ Stopping" if ctl.get("stop")
                                                else "🟢 Sending")
    return card("✈️ Send to Contacts — Live (mutuals first)", [
        f"📱 {ctl.get('phone', '')}",
        f"• Status : {status}",
        f"📊 {done} of {total} — {pct}%",
        f"✅ OK : {ctl.get('ok', 0)}   ❌ Fail : {ctl.get('fail', 0)}   "
        f"⏭ Skipped : {ctl.get('skip', 0)}",
        f"🕒 {now()}",
    ])


async def _tg_mutual_progress_loop(phone, ctl, msg):
    while not ctl.get("finished"):
        paused, running = _tg_ctl_buttons(phone)
        try:
            await safe_edit(msg, _tg_mutual_card(ctl),
                            buttons=paused if ctl.get("pause") else running)
        except Exception:
            pass
        await asyncio.sleep(max(1.0, config.TG_STATS_REFRESH))


async def _tg_run_mutual(owner_id, acc):
    phone = acc["phone"]
    # step 3: unified ORDERED content list (texts and/or media+caption).
    msgs = db.tg_msgs_get()
    if not msgs:
        await bot.send_message(owner_id, "Set Send Content first.")
        return
    delay = db.tg_get_send_delay()
    n_text = sum(1 for m in msgs if m.get("type") == "text")
    n_media = sum(1 for m in msgs if m.get("type") == "media")
    await log(card("✈️ TG MUTUAL SEND START", [
        f"📱 {phone}", f"• Speed : {delay}s",
        f"• Items : {len(msgs)} (✍️ {n_text} text + 🖼 {n_media} file)", f"🕒 {now()}"]))
    try:
        client = await tg.get_client(phone)
    except Exception as e:  # noqa: BLE001
        db.tg_set_status(phone, "inactive")
        await log_error("Telegram send", phone, "account connect", e)
        await bot.send_message(owner_id, f"🔴 Account {phone} needs a login ({repr(e)[:80]}).")
        return
    try:
        targets, mutual_count = await tg.get_contacts_ordered(client)
    except Exception as e:  # noqa: BLE001
        await log_error("Telegram send", phone, "fetch contacts", e)
        await bot.send_message(owner_id, f"❌ Fetching contacts failed: {repr(e)[:120]}")
        return
    await log(card("✈️ TG SEND — Order", [
        f"📱 {phone}", f"• Mutuals (first) : {mutual_count}",
        f"• Other contacts (after) : {len(targets) - mutual_count}",
        f"• Total : {len(targets)}"]))
    ctl = {"stop": False, "pause": False, "ok": 0, "fail": 0, "skip": 0,
           "total": len(targets), "done": 0, "phone": phone, "finished": False,
           "mutuals": mutual_count}
    tg_jobs[phone] = ctl
    _, running = _tg_ctl_buttons(phone)
    try:
        msg = await bot.send_message(owner_id, _tg_mutual_card(ctl), buttons=running)
    except Exception:
        msg = None
    prog = asyncio.create_task(_tg_mutual_progress_loop(phone, ctl, msg)) if msg else None
    # Pre-upload each MEDIA item ONCE to Saved Messages, then copy it to everyone
    # (no per-recipient re-upload — much faster). Build a prepared, ORDERED plan
    # mirroring the content list; text items are sent directly (no upload cost).
    prepared = []
    for m in msgs:
        if m.get("type") == "media" and m.get("media"):
            cap = m.get("caption", "") or ""
            saved = None
            try:
                saved = await tg.upload_to_saved(client, m["media"], cap)
            except Exception as e:  # noqa: BLE001
                saved = None
                await log_error("Telegram send", phone,
                                f"upload file to Saved ({os.path.basename(m.get('media',''))})", e)
            prepared.append({"type": "media", "saved": saved,
                             "path": m["media"], "caption": cap})
        else:
            prepared.append({"type": "text", "text": m.get("text", "") or ""})
    if any(p["type"] == "media" and p["saved"] is not None for p in prepared):
        await log(card("✈️ TG SEND — Files uploaded to Saved", [
            f"📱 {phone}", "The rest are copied without a forward tag and without re-uploading.",
            f"🕒 {now()}"]))
    for u in targets:
        if await _ctl_gate(ctl):
            break
        uid = getattr(u, "id", None)
        if db.tg_was_sent(uid):
            ctl["skip"] += 1
            ctl["done"] += 1
            continue
        try:
            # send every content item to THIS recipient, in order (1 -> 2 -> 3)
            for p in prepared:
                if p["type"] == "media":
                    if p["saved"] is not None:
                        await tg.send_saved_media(client, u, p["saved"],
                                                  p["caption"])   # copy, no fwd tag
                    else:
                        # fallback: per-recipient upload if the Saved copy failed
                        await tg.send_media(client, u, p["path"], p["caption"],
                                            typing=0)   # no typing delay = faster
                    db.tg_incr_sent(phone, 1)
                elif p["text"]:
                    await tg.send_text(client, u, p["text"], typing=0)  # faster
                    db.tg_incr_sent(phone, 1)
                if len(prepared) > 1:
                    await asyncio.sleep(0.05)   # tiny gap only when multi-item
            ctl["ok"] += 1
            db.tg_mark_sent(uid)
        except Exception as e:  # noqa: BLE001
            ctl["fail"] += 1
            await log_error("Telegram send", phone, f"send to {uid}", e)
        ctl["done"] += 1
        await asyncio.sleep(max(0.0, float(delay)))
    ctl["finished"] = True
    if prog:
        prog.cancel()
    tg_jobs.pop(phone, None)
    attempted = ctl["ok"] + ctl["fail"]
    pct = int(ctl["ok"] * 100 / attempted) if attempted else 0
    await log(card("✈️ TG MUTUAL SEND — Done", [
        f"📱 {phone}",
        f"✅ {ctl['ok']}   ❌ {ctl['fail']}   ⏭ Skipped {ctl['skip']}",
        f"• Success rate : {pct}%", f"🕒 {now()}"]))
    if msg:
        try:
            await safe_edit(msg, _tg_mutual_card(ctl),
                            buttons=[[Button.inline("🔙 Telegram Panel", b"tg")]])
        except Exception:
            pass
    await bot.send_message(owner_id,
        f"✈️ Send finished. ✅ {ctl['ok']} / ❌ {ctl['fail']} — rate {pct}%",
        buttons=[[Button.inline("🔙 Telegram Panel", b"tg")]])


@bot.on(events.CallbackQuery(data=b"tgmutual"))
async def tg_mutual_cb(event):
    if not is_owner(event):
        return
    accs = [a for a in db.tg_list_accounts() if a["status"] == "active"]
    if not accs:
        await event.answer("You have no active Telegram account.", alert=True)
        return
    rows = [[Button.inline(f"📤 {a['phone']} (👥{a['contacts']})",
                           f"tgmut_{a['rid']}".encode())] for a in accs]
    rows.append([Button.inline("🔙 Back", b"tg")])
    await safe_edit(event,
        "📤 Send to mutual contacts — pick an account.\n"
        "(mutuals first, then other contacts. Global de-dup is on.)", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"tgmut_(\\d+)"))
async def tg_mutual_pick_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("Not found.", alert=True)
        return
    if acc["phone"] in tg_jobs:
        await event.answer("A send is in progress on this account right now.", alert=True)
        return
    await safe_edit(event, f"📤 Sending to {acc['phone']}'s contacts started (mutuals first). Reports go to the log group.",
                    buttons=[[Button.inline("🔙 Telegram Panel", b"tg")]])
    asyncio.create_task(_tg_run_mutual(event.sender_id, acc))


@bot.on(events.CallbackQuery(pattern=b"tgstop_(.+)"))
async def tg_stop_cb(event):
    if not is_owner(event):
        return
    ctl = tg_jobs.get(event.pattern_match.group(1).decode())
    if not ctl:
        await event.answer("Nothing is in progress.", alert=True)
        return
    ctl["stop"] = True
    ctl["pause"] = False
    await event.answer("⏹ Stop requested.")


@bot.on(events.CallbackQuery(pattern=b"tgpause_(.+)"))
async def tg_pause_cb(event):
    if not is_owner(event):
        return
    ctl = tg_jobs.get(event.pattern_match.group(1).decode())
    if not ctl:
        await event.answer("Nothing is in progress.", alert=True)
        return
    ctl["pause"] = True
    await event.answer("⏸ Paused.")


@bot.on(events.CallbackQuery(pattern=b"tgresume_(.+)"))
async def tg_resume_cb(event):
    if not is_owner(event):
        return
    ctl = tg_jobs.get(event.pattern_match.group(1).decode())
    if not ctl:
        await event.answer("Nothing is in progress.", alert=True)
        return
    ctl["pause"] = False
    await event.answer("▶️ Resumed.")


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
async def amain():
    problems = config.validate()
    if problems:
        print("Missing settings in .env: " + ", ".join(problems))
        return
    db.init()
    worker.ensure_master_worker()
    # Feature 6 wiring: shared-connection logger + invalid-auth handler + janitor
    features.set_logger(log)
    account_conn.set_invalid_auth_handler(_on_invalid_auth)
    account_conn.start_janitor()
    await bot.start(bot_token=config.BOT_TOKEN)
    # Additive Telegram multi-account panel; no client/session logic lives here.
    try:
        import telegram_multi_panel
        import telegram_multi_send
        telegram_multi_panel.register(
            client=bot, events=events, Button=Button, state=state,
            is_owner=is_owner, safe_edit=safe_edit,
        )
        await telegram_multi_send.restore_pending()
    except Exception as _tme:
        await log(card("⚠️ - #Telegram_Multi_Error", [f"🔧 boot: {repr(_tme)[:180]}"]))
    await log(card("Online", [f"Rubika Project {config.VERSION}", LINE, f"🕒 {now()}"]))
    print(f"Panel is running (version {config.VERSION}).")
    # ---- Portal (isolated, additive) ----
    try:
        import portal
        asyncio.create_task(portal.run_portal())
    except Exception as _pe:
        await log(card("⚠️ - #Portal_Error", [
            "#portal #error", "-------------------------------",
            "🔧 Step  : boot", f"📝 Error  : {repr(_pe)[:200]}", f"🕒 {now()}"]))
    # Worker connectivity: pre-warm every enabled remote worker's SSH tunnel in
    # parallel (bounded), then start a persistent per-worker supervisor that
    # keeps each tunnel alive and rebuilds it in the background on failure.
    # This restores the old "warm connection" behaviour so the status card and
    # api_call hit an already-open tunnel instead of a cold SSH connect.
    try:
        await worker.prewarm_all()
        await worker.start_all_supervisors()
    except Exception as _we:
        print(f"[worker warmup] {_we}")
    # background worker health monitor: warm-tunnel-only snapshot every ~25s,
    # alerts on healthy->unhealthy, periodic full card. The Refresh button only
    # renders this snapshot (never probes).
    asyncio.create_task(worker_snapshot_loop())
    # automation: periodic summary log + relaunch any automation enabled before restart
    asyncio.create_task(automation_summary_loop())
    await recover_automations()
    # automation EXTRAS: relaunch enabled features + drain remote worker logs/heal
    asyncio.create_task(extras_worker_loop())
    # health & self-heal engine (verify sessions, relaunch stalled automation,
    # post overall health card) — موتور سلامت و خودتعمیر
    asyncio.create_task(health_engine_loop())
    # Item 3: linkdooni engine — periodic stats card + relaunch if it was
    # enabled before the restart.
    asyncio.create_task(linkdooni_summary_loop())
    await recover_extras()
    await recover_linkdooni()
    try:
        await bot.run_until_disconnected()
    finally:
        try:
            await account_conn.close_all()
        except Exception:
            pass
        await worker.shutdown()


# =========================================================================== #
# update_end NEW FEATURES (additive only — nothing above is removed).
#   • account-add worker-transfer retry
#   • post-send "check account -> re-login -> continue remaining list"
#   • contact import from a .txt file (adjustable speed, log every 100)
#   • multi-account send (auto tags, same-worker sequential, cross-worker
#     parallel, skip dead, stop-all, summary)
#   • brain (split a number file across accounts, add, then send to 150 each)
#   • settings panel (max errors / resume wait / send delay / contact speed)
# =========================================================================== #
import re as _re_u

_pending_addfail = {}              # owner_id -> phone (failed add -> transfer)
pending_resume_after_login = {}    # owner_id -> phone (resume the list on login)
multisend_sel = {}                 # owner_id -> set(account_id)
multisend_stop = {}                # owner_id -> bool
brain_sel = {}                     # owner_id -> set(account_id)
brain_jobs = {}                    # owner_id -> dict (per-account collected guids)
cbrain_jobs = {}                   # owner_id -> live ctl dict for 🧠 مغز کانال
# Brain stop/pause is now owned by the isolated brain_control.controller
# (per-owner, mid-account interruptible). See brain_control.py.


def _norm_pairs_from_text(text: str):
    """Parse a txt body into deduped (phone, name) pairs. Lines may be just a
    number, or 'number,name' / 'number<TAB>name'."""
    out = []
    seen = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in _re_u.split(r"[,\t;]", line) if p.strip()]
        if not parts:
            continue
        ph = rb.normalize_phone(parts[0])
        if not ph or len(ph) < 10 or ph in seen:
            continue
        seen.add(ph)
        name = parts[1] if len(parts) > 1 else ""
        out.append((ph, name))
    return out


# --------------------------------------------------------------------------- #
# Account-add: worker-transfer retry
# --------------------------------------------------------------------------- #
async def _begin_add_for_phone(event, phone):
    try:
        w = await worker.pick_worker_for_login()
    except Exception as e:  # noqa: BLE001
        await bot.send_message(event.sender_id, f"❌ Error picking a worker: {repr(e)[:150]}")
        return
    if not w:
        await bot.send_message(event.sender_id, "❌ No healthy worker is available.")
        return
    if not worker.is_local(w):
        await handle_phone_remote(event, phone, w)
    else:
        await _begin_local_login(event, phone, w)


@bot.on(events.CallbackQuery(data=b"addxfer"))
async def addxfer_cb(event):
    if not is_owner(event):
        return
    phone = _pending_addfail.pop(event.sender_id, None)
    if not phone:
        await safe_edit(event, "There is no number to retry.",
                        buttons=main_menu(is_real_owner(event)))
        return
    await safe_edit(event, f"🔁 Transferring worker and retrying for {phone} ...")
    await _begin_add_for_phone(event, phone)


# --------------------------------------------------------------------------- #
# Post-send: "check account -> confirm/re-login -> continue remaining list"
# --------------------------------------------------------------------------- #
async def _offer_resume_after_send(owner_id: int, info: dict):
    account_id = info["account_id"]
    phone = info["phone"]
    remaining = info.get("recipients") or []
    dead = info.get("dead")
    is_remote = bool(info.get("remote"))
    if remaining or is_remote:
        payload = {
            "saved_guid": info.get("saved_guid"), "mid": info.get("mid"),
            "recipients": remaining, "base_ok": int(info.get("base_ok") or 0),
            "tag": info.get("tag") or "", "remote": is_remote,
            "worker_id": info.get("worker_id"),
            # worker_transfer: persist the full list of tried workers so that
            # repeated transfers never revisit the same server.
            "tried_workers": worker_transfer.get_tried(account_id),
        }
        # Also record current worker as tried (it just failed/stopped)
        if info.get("worker_id"):
            worker_transfer.add_tried(account_id, info["worker_id"])
            payload["tried_workers"] = worker_transfer.get_tried(account_id)
        try:
            db.save_paused_send(account_id, owner_id, phone, payload)
        except Exception:
            pass
    rows = []
    body = [f"📱 {phone}"]
    if remaining:
        body.append(f"• Remaining in list : {len(remaining)}")
    if is_remote:
        body.append("📡 This account was on a worker; logging into a new worker resumes/repeats the send.")
    if dead:
        body.append("🔴 Status: possible session invalidation/block")
    if remaining or is_remote:
        body.append("To continue, tap Login to New Worker & Continue.")
        rows.append([Button.inline("🔁 Login to New Worker & Continue",
                                   f"rlogin_{account_id}".encode())])
        rows.append([Button.inline("🚫 Cancel Continue", f"rcancel_{account_id}".encode())])
    rows.append([Button.inline("🏠 Main Menu", b"home")])
    panel = card("🔄 Send End — Transfer/Continue", body)
    try:
        await bot.send_message(owner_id, panel, buttons=rows)
    except Exception:
        pass
    # also surface the same controls in the log group (not only the bot PV)
    try:
        await bot.send_message(config.LOG_GROUP_ID, panel, buttons=rows)
    except Exception:
        pass


@bot.on(events.CallbackQuery(pattern=b"rcont_(\\d+)"))
async def resume_continue_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    await safe_edit(event, "▶️ Resuming the send from the remaining list ...")
    await _do_resume(event.sender_id, aid)


@bot.on(events.CallbackQuery(pattern=b"rcancel_(\\d+)"))
async def resume_cancel_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    # R9: discard the paused/remaining list so it won't be offered again.
    try:
        db.delete_paused_send(aid)
    except Exception:
        pass
    # worker_transfer: clear tried history (send cancelled, fresh start next time)
    worker_transfer.clear_tried(aid)
    await safe_edit(event, "🚫 Continue cancelled and the remaining list was cleared.",
                    buttons=[[Button.inline("🏠 Main Menu", b"home")]])


@bot.on(events.CallbackQuery(pattern=b"rlogin_(\\d+)"))
async def resume_relogin_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    rec = db.get_paused_send(aid)
    if not rec:
        await safe_edit(event, "There is nothing to continue.",
                        buttons=[[Button.inline("🏠 Main Menu", b"home")]])
        return
    phone = rec["phone"]
    acc = db.get_account(aid)
    # resolve the account's CURRENT server robustly (worker_id may be None ->
    # that means it lives on the master). Excluding this guarantees the transfer
    # always lands on a DIFFERENT server (or cleanly says there isn't one).
    cur_w = worker.worker_for_account(acc) if acc else None
    cur_wid = cur_w["id"] if cur_w else None
    # worker_transfer: restore tried list from payload and add current worker
    tried = list(rec["payload"].get("tried_workers") or [])
    worker_transfer.set_tried(aid, tried)
    if cur_wid and cur_wid not in tried:
        worker_transfer.add_tried(aid, cur_wid)
    tried = worker_transfer.get_tried(aid)
    await safe_edit(event, "🔁 Finding another worker (other than the previous servers) to transfer ...")
    # WORKER TRANSFER: pick a worker NOT in the full tried list.
    try:
        neww = await worker_transfer.pick_worker_for_transfer(exclude_ids=tried)
    except Exception:
        neww = None
    if not neww:
        await safe_edit(event,
            "❌ No other worker to transfer to (all servers were already tried).\n"
            "For Transfer Worker, add another worker/server from Workers first.\n"
            "Or continue with this server for now:",
            buttons=[[Button.inline("✅ Continue with This Server", f"rcont_{aid}".encode())],
                     [Button.inline("🛠 Add Worker", b"wk_add")],
                     [Button.inline("🔙 Back", b"home")]])
        return
    # Record the new worker as tried BEFORE transferring
    worker_transfer.add_tried(aid, neww["id"])
    # v4: try a CODE-FREE worker transfer first, using the stored session.
    # Import is write-only on the new worker; the real connection happens only
    # later in the resume send (one at a time) — session conflict logic intact.
    sess = None
    try:
        sess = db.get_session_blob(aid)
    except Exception:
        sess = None
    if sess and sess.get("auth"):
        sess["phone"] = rb.normalize_phone(phone)
        await safe_edit(event, f"🔁 Transferring to '{neww['tag']}' via session (no code) ...")
        if await _session_transfer_to_worker(neww, sess, phone):
            db.set_account_worker(aid, neww["id"])
            await log(card("🔁 WORKER TRANSFER — via session (no code)", [
                f"📱 {phone}", f"• New worker : {neww['tag']}",
                "✅ Connected without a code — continuing the list", f"🕒 {now()}"]))
            await safe_edit(event,
                f"✅ Connected on '{neww['tag']}' via session (no code). Continuing the list ...")
            await _do_resume(event.sender_id, aid)
            return
        await safe_edit(event,
            f"⚠️ Session login on '{neww['tag']}' failed — falling back to a manual code.")
    # fallback: manual code-based login (original behavior, conflict logic
    # untouched). _maybe_resume_after_login continues the list after login.
    pending_resume_after_login[event.sender_id] = phone
    await safe_edit(event,
        f"🔁 Transferring to worker '{neww['tag']}' and re-logging in {phone} — I'll get the phone/code, "
        "then the previous list continues on this new worker.")
    # start the login on the CHOSEN new worker (local or remote)
    if not worker.is_local(neww):
        await handle_phone_remote(event, phone, neww)
    else:
        await _begin_local_login(event, phone, neww)


async def _maybe_resume_after_login(owner_id: int, phone: str):
    target = pending_resume_after_login.pop(owner_id, None)
    if not target:
        return
    if rb.normalize_phone(target) != rb.normalize_phone(phone):
        pending_resume_after_login[owner_id] = target
        return
    aid = None
    for a in db.list_accounts():
        if rb.normalize_phone(a["phone"]) == rb.normalize_phone(phone):
            aid = a["id"]
            break
    if aid is not None:
        # R3: a successful login on the NEW worker (after a transfer) is logged
        # to the log group before continuing the remaining list.
        acc = db.get_account(aid)
        w = worker.worker_for_account(acc) if acc else None
        await log(card("✅ New worker login succeeded — continuing", [
            f"📱 {phone}",
            f"• New worker : {w['tag'] if w else '—'}",
            f"🕒 {now()}",
        ]))
        await _do_resume(owner_id, aid)


async def _do_resume(owner_id: int, account_id: int):
    rec = db.get_paused_send(account_id)
    if not rec:
        try:
            await bot.send_message(owner_id, "There is nothing to continue.")
        except Exception:
            pass
        return
    p = rec["payload"]
    # worker_transfer: restore tried workers from payload (survives restart)
    worker_transfer.set_tried(account_id, p.get("tried_workers") or [])
    db.delete_paused_send(account_id)
    recips = p.get("recipients") or []
    acc = db.get_account(account_id)
    w = worker.worker_for_account(acc) if acc else None
    is_remote_now = bool(w and not worker.is_local(w))

    # 1) precise local resume: exact remaining list AND account is local now
    if recips and p.get("mid") and not is_remote_now:
        payload = {
            "account_id": account_id, "phone": rec["phone"],
            "saved_guid": p.get("saved_guid"), "mid": p.get("mid"),
            "recipients": recips, "base_ok": int(p.get("base_ok") or 0),
            "tag": p.get("tag") or "",
        }
        try:
            await bot.send_message(owner_id,
                f"▶️ Resuming send for {rec['phone']} from {len(recips)} remaining recipients ...",
                buttons=[[Button.inline("⏹ Stop Sending", f"stop_{account_id}".encode())]])
        except Exception:
            pass
        asyncio.create_task(run_send(owner_id, payload))
        return

    # 2) exact list known + account is now on a REMOTE worker (e.g. after a
    #    worker transfer) -> continue with the FULL send engine on that worker
    #    (same progress / auto-resume / repeatable-transfer as a normal send),
    #    sending EXACTLY the remaining guids — not the whole list again.
    if recips and is_remote_now:
        try:
            await bot.send_message(owner_id,
                f"▶️ Continuing the list on worker '{w['tag']}' ({len(recips)} recipients) ...",
                buttons=[[Button.inline("⏹ Stop Sending", f"stop_{account_id}".encode())]])
        except Exception:
            pass
        asyncio.create_task(run_send_remote(owner_id, {
            "account_id": account_id, "phone": rec["phone"], "remote": True,
            "worker_id": w["id"], "total": len(recips),
            "recipients": recips, "is_resume": True,
        }))
        return

    # 2.5) remaining list known but NO mid (came from a REMOTE send) AND the
    #      account is now LOCAL (master) — e.g. a worker transfer that landed on
    #      the master. Find the marker LOCALLY, then send EXACTLY the remaining
    #      list — NOT from scratch. (Without this, it fell through to a fresh
    #      send and re-sent everyone who was already messaged.)
    if recips and not p.get("mid") and not is_remote_now:
        marker = db.get_marker()
        try:
            saved_guid, mid = await _find_marker_local(rec["phone"], marker)
        except account_conn.InvalidAuthError:
            db.set_status(account_id, "inactive")
            try:
                await bot.send_message(owner_id, "🔴 This account's session is invalid.")
            except Exception:
                pass
            return
        except Exception as e:  # noqa: BLE001
            try:
                await bot.send_message(owner_id, f"❌ Error finding the marker: {repr(e)[:120]}")
            except Exception:
                pass
            return
        if not mid:
            try:
                await bot.send_message(owner_id, "❌ The marker was not found on this account.")
            except Exception:
                pass
            return
        try:
            await bot.send_message(owner_id,
                f"▶️ Resuming send for {rec['phone']} from {len(recips)} remaining recipients (on master) ...",
                buttons=[[Button.inline("⏹ Stop Sending", f"stop_{account_id}".encode())]])
        except Exception:
            pass
        asyncio.create_task(run_send(owner_id, {
            "account_id": account_id, "phone": rec["phone"],
            "saved_guid": saved_guid, "mid": mid,
            "recipients": recips, "base_ok": int(p.get("base_ok") or 0),
            "tag": p.get("tag") or "",
        }))
        return

    # 3) remote (no precise list) -> fresh send routed by the current worker
    try:
        await bot.send_message(owner_id, "▶️ Resuming the send ...",
                               buttons=[[Button.inline("⏹ Stop Sending", f"stop_{account_id}".encode())]])
    except Exception:
        pass
    await _resume_fresh_send(owner_id, account_id)


async def _resume_remote_list(owner_id, account_id, guids):
    acc = db.get_account(account_id)
    if not acc:
        return
    w = worker.worker_for_account(acc)
    marker = db.get_marker()
    try:
        res = await worker.api_call(w, "POST", "/send/to_list", {
            "phone": acc["phone"], "marker": marker, "guids": guids,
            "delay": db.get_delay(), "max_errors": db.get_max_errors(),
            "send_timeout": config.SEND_TIMEOUT,
            "text2": db.get_rb_text2()}, timeout=14400)
        ok = res.get("sent", 0)
        fail = res.get("fail", 0)
        await log(card("✅ Continue send (worker) finished", [
            f"📱 {acc['phone']}", f"👨‍🔧 {w['tag']}",
            f"✅ {ok}   ❌ {fail}", f"🕒 {now()}"]))
        await bot.send_message(owner_id, f"✅ Continue finished. ✅ {ok} / ❌ {fail}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ Continue send (worker) — Error", [
            f"📱 {acc['phone']}", f"💥 {repr(e)[:140]}"]))


async def _resume_fresh_send(owner_id, account_id):
    acc = db.get_account(account_id)
    if not acc:
        return
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.check_worker(w)
        except Exception:
            pass
        w = db.get_worker(w["id"])
        if not (w and w["enabled"] and w["status"] == "ok"):
            await bot.send_message(owner_id, "❌ This account's worker is not healthy right now.")
            return
        try:
            res = await worker.api_call(w, "POST", "/prepare",
                                        {"phone": acc["phone"], "marker": marker})
        except Exception as e:  # noqa: BLE001
            await bot.send_message(owner_id, f"❌ Preparation error on the worker: {repr(e)[:120]}")
            return
        if not res.get("marker_found") or not res.get("total"):
            await bot.send_message(owner_id, "❌ No marker/recipient on the worker.")
            return
        asyncio.create_task(run_send_remote(owner_id, {
            "account_id": account_id, "phone": acc["phone"], "remote": True,
            "worker_id": w["id"], "total": res["total"]}))
    else:
        try:
            prep = await _prepare_local(acc, marker)
        except account_conn.InvalidAuthError:
            db.set_status(account_id, "inactive")
            await bot.send_message(owner_id, "🔴 This account's session is invalid.")
            return
        except Exception as e:  # noqa: BLE001
            await bot.send_message(owner_id, f"❌ Error: {repr(e)[:120]}")
            return
        if not prep:
            await bot.send_message(owner_id, "❌ Marker not found.")
            return
        saved_guid, mid, recips = prep
        if not recips:
            await bot.send_message(owner_id, "There were no recipients.")
            return
        asyncio.create_task(run_send(owner_id, {
            "account_id": account_id, "phone": acc["phone"],
            "saved_guid": saved_guid, "mid": mid, "recipients": recips}))


# --------------------------------------------------------------------------- #
# Contact import from a .txt file
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"contacts"))
async def contacts_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, "Add an account first.",
                        buttons=[[Button.inline("➕ Add Account", b"add_account")],
                                 [Button.inline("🔙 Back", b"home")]])
        return
    rows = [[Button.inline(f"📇 {a['phone']}", f"cadd_{a['id']}".encode())]
            for a in accounts]
    rows.append([Button.inline(f"⏱ Current speed: {db.get_contact_delay()}s", b"cspeed")])
    rows.append([Button.inline("🔎 Discover Friends by Prefix", b"discover")])
    rows.append([Button.inline("🔙 Back", b"home")])
    await safe_edit(event,
        "➕ Add Contacts from a txt file\n"
        f"{LINE}\nPick an account, then send the numbers file.\n"
        "One number per line (optional: 'number,name').\n"
        "Or tap Discover Friends by Prefix to let the bot find Rubika numbers itself.",
        buttons=rows)


@bot.on(events.CallbackQuery(data=b"cspeed"))
async def contacts_speed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_contactspeed", "back": "contacts"}
    await safe_edit(event,
        f"⏱ Current contact-add speed: {db.get_contact_delay()} seconds\n"
        f"Send a number between {config.CONTACT_MIN_DELAY} and {config.CONTACT_MAX_DELAY}, "
        "or tap one of the quick presets:",
        buttons=[
            [Button.inline("0.1s", b"cspd_0.1"), Button.inline("0.3s", b"cspd_0.3"),
             Button.inline("0.5s", b"cspd_0.5")],
            [Button.inline("1s", b"cspd_1"), Button.inline("2s", b"cspd_2"),
             Button.inline("5s", b"cspd_5")],
            [Button.inline("🔙 Back", b"contacts")]])


@bot.on(events.CallbackQuery(pattern=b"cspd_([0-9.]+)"))
async def contacts_speed_preset_cb(event):
    if not is_owner(event):
        return
    val = event.pattern_match.group(1).decode()
    db.set_contact_delay(val)
    state.pop(event.sender_id, None)
    await safe_edit(event,
        f"✅ Contact-add speed set to {db.get_contact_delay()} seconds.",
        buttons=[[Button.inline("🔙 Back", b"contacts")]])


@bot.on(events.CallbackQuery(pattern=b"cadd_(\\d+)"))
async def contacts_pick_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    state[event.sender_id] = {"step": "await_contacts_file", "account_id": aid}
    await safe_edit(event,
        f"📂 Send the numbers txt file for account {acc['phone']}.\n"
        f"⏱ Add speed: {db.get_contact_delay()}s (change it from Speed)",
        buttons=[[Button.inline("🔙 Cancel", b"contacts")]])


async def handle_contacts_file(event, st):
    aid = st.get("account_id")
    acc = db.get_account(aid)
    if not acc:
        state.pop(event.sender_id, None)
        await event.respond("Account not found.", buttons=main_menu(is_real_owner(event)))
        return
    if not event.file:
        await event.respond("Send a txt file (or tap Cancel).")
        return
    try:
        data = await event.download_media(file=bytes)
        text = data.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Couldn't read the file: {repr(e)[:120]}")
        return
    pairs = _norm_pairs_from_text(text)
    state.pop(event.sender_id, None)
    if not pairs:
        await event.respond("There were no valid numbers in the file.",
                            buttons=main_menu(is_real_owner(event)))
        return
    await event.respond(
        f"✅ Read {len(pairs)} unique numbers. Starting to add to {acc['phone']} ...\n"
        "Reports go to the log group.", buttons=main_menu(is_real_owner(event)))
    asyncio.create_task(run_contact_import(event.sender_id, acc, pairs))


async def _ctl_gate(ctl) -> bool:
    """Honor an optional live control dict (Item 1/2). Returns True if the job
    must STOP. Blocks here while it is PAUSED. ``ctl`` may be None."""
    if not ctl:
        return False
    if ctl.get("stop"):
        return True
    while ctl.get("pause") and not ctl.get("stop"):
        await asyncio.sleep(0.5)
    return bool(ctl.get("stop"))


async def _contacts_add_local(phone, pairs, delay, log_every, tag="", ctl=None):
    async def _do(client):
        added = 0        # on Rubika (real contact)
        not_user = 0     # added to address book but no Rubika account
        failed = 0
        attempt_fail = 0
        guids = []
        for ph, name in pairs:
            if await _ctl_gate(ctl):          # live stop/pause (Item 1)
                break
            try:
                r = await asyncio.wait_for(
                    rb.add_contact(client, ph, name or config.CONTACT_DEFAULT_FIRST),
                    timeout=config.SEND_TIMEOUT)
                attempt_fail = 0
                if r.get("on_rubika"):
                    added += 1
                    if r.get("guid"):
                        guids.append(r["guid"])
                else:
                    not_user += 1
            except Exception:
                failed += 1
                attempt_fail += 1
                if attempt_fail >= db.get_max_errors():
                    await log(card("🚨 Add Contacts — Pause", [
                        f"{tag}📱 {phone}",
                        f"{db.get_max_errors()} consecutive errors -> waiting {db.get_resume_wait()}s",
                        f"🕒 {now()}"]))
                    await asyncio.sleep(db.get_resume_wait())
                    attempt_fail = 0
            if ctl is not None:               # feed the live progress card
                ctl["added"] = added
                ctl["not_user"] = not_user
                ctl["failed"] = failed
                ctl["done"] = added + not_user + failed
            if log_every > 0 and (added + not_user + failed) % log_every == 0:
                await log(card("📇 Add Contacts — Progress", [
                    f"{tag}📱 {phone}",
                    f"🟢 On Rubika : {added}   📵 Not on Rubika : {not_user}   ❌ {failed}",
                    f"of {len(pairs)}",
                    f"🕒 {now()}"]))
            await asyncio.sleep(max(0.0, float(delay)))
        return {"added": added, "not_user": not_user, "failed": failed, "guids": guids}
    return await account_conn.call(phone, _do, timeout=14400)


async def _contacts_add(acc, pairs, delay, tag="", ctl=None):
    """Add contacts on local OR remote account. Returns dict with
    added (on Rubika) / not_user / failed / guids.

    When ``ctl`` is given (Item 1 live progress), the REMOTE path is driven in
    chunks so pause/stop stay responsive and the progress card updates between
    chunks instead of waiting for the whole list to finish on the worker."""
    phone = acc["phone"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        if ctl is None:
            res = await worker.api_call(w, "POST", "/contacts/add", {
                "phone": phone, "numbers": [p for p, _n in pairs],
                "delay": delay, "default_first": config.CONTACT_DEFAULT_FIRST,
            }, timeout=14400)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "contacts add failed"))
            return {"added": res.get("added", 0), "not_user": res.get("not_user", 0),
                    "failed": res.get("failed", 0), "guids": res.get("guids", [])}
        # chunked remote add with live control
        added = not_user = failed = 0
        guids = []
        chunk = max(1, config.CONTACT_REMOTE_CHUNK)
        for i in range(0, len(pairs), chunk):
            if await _ctl_gate(ctl):
                break
            part = pairs[i:i + chunk]
            res = await worker.api_call(w, "POST", "/contacts/add", {
                "phone": phone, "numbers": [p for p, _n in part],
                "delay": delay, "default_first": config.CONTACT_DEFAULT_FIRST,
            }, timeout=14400)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "contacts add failed"))
            added += res.get("added", 0)
            not_user += res.get("not_user", 0)
            failed += res.get("failed", 0)
            guids.extend(res.get("guids", []) or [])
            ctl["added"] = added
            ctl["not_user"] = not_user
            ctl["failed"] = failed
            ctl["done"] = added + not_user + failed
        return {"added": added, "not_user": not_user, "failed": failed, "guids": guids}
    return await _contacts_add_local(phone, pairs, delay, config.CONTACT_LOG_EVERY,
                                     tag, ctl=ctl)


def _ctl_buttons(aid: int, kind: str = "c"):
    """Stop/pause/resume buttons for a live job. kind 'c' = contact import,
    'd' = discovery (so the two never clash)."""
    paused_row = [Button.inline("▶️ Resume", f"{kind}resume_{aid}".encode()),
                  Button.inline("⏹ Stop", f"{kind}stop_{aid}".encode())]
    running_row = [Button.inline("⏸ Pause", f"{kind}pause_{aid}".encode()),
                   Button.inline("⏹ Stop", f"{kind}stop_{aid}".encode())]
    return [paused_row], [running_row]


def _contact_progress_card(ctl) -> str:
    total = ctl.get("total", 0)
    done = ctl.get("done", 0)
    pct = int(done * 100 / total) if total else 0
    status = "⏸ Pause" if ctl.get("pause") else ("⏹ Stopping" if ctl.get("stop")
                                                else "🟢 Running")
    return card("📇 Add Contacts — Live Progress", [
        f"📱 {ctl.get('phone', '')}",
        f"• Status : {status}",
        f"📊 {done} of {total} — {pct}%",
        f"🟢 On Rubika : {ctl.get('added', 0)}   "
        f"📵 Not on Rubika : {ctl.get('not_user', 0)}   ❌ {ctl.get('failed', 0)}",
        f"🕒 {now()}",
    ])


async def _contact_progress_loop(owner_id: int, aid: int, ctl: dict, msg):
    """Edit the live progress message every CONTACT_PROGRESS_EVERY seconds until
    the job is marked finished."""
    while not ctl.get("finished"):
        paused_btns, running_btns = _ctl_buttons(aid, "c")
        try:
            await safe_edit(msg, _contact_progress_card(ctl),
                            buttons=paused_btns if ctl.get("pause") else running_btns)
        except Exception:
            pass
        await asyncio.sleep(max(1.0, config.CONTACT_PROGRESS_EVERY))


async def run_contact_import(owner_id: int, acc, pairs):
    phone = acc["phone"]
    aid = acc["id"]
    delay = db.get_contact_delay()
    ctl = {"stop": False, "pause": False, "added": 0, "not_user": 0,
           "failed": 0, "total": len(pairs), "done": 0, "phone": phone,
           "finished": False}
    contact_jobs[aid] = ctl
    await log(card("📇 CONTACT IMPORT START", [
        f"📱 {phone}", f"• Numbers : {len(pairs)}", f"• Speed : {delay}s", f"🕒 {now()}"]))
    _, running_btns = _ctl_buttons(aid, "c")
    try:
        msg = await bot.send_message(owner_id, _contact_progress_card(ctl),
                                     buttons=running_btns)
    except Exception:
        msg = None
    prog_task = None
    if msg is not None:
        prog_task = asyncio.create_task(_contact_progress_loop(owner_id, aid, ctl, msg))
    try:
        res = await _contacts_add(acc, pairs, delay, ctl=ctl)
    except account_conn.InvalidAuthError:
        db.set_status(aid, "inactive")
        ctl["finished"] = True
        contact_jobs.pop(aid, None)
        if prog_task:
            prog_task.cancel()
        await log(card("📇 CONTACT IMPORT — Invalid Session", [f"📱 {phone}", f"🕒 {now()}"]))
        await bot.send_message(owner_id, f"🔴 {phone}'s session is invalid. Add it again.")
        return
    except Exception as e:  # noqa: BLE001
        ctl["finished"] = True
        contact_jobs.pop(aid, None)
        if prog_task:
            prog_task.cancel()
        await log(card("📇 CONTACT IMPORT — Error", [
            f"📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        await bot.send_message(owner_id, f"❌ Adding contacts failed: {repr(e)[:120]}")
        return
    ctl["finished"] = True
    if prog_task:
        prog_task.cancel()
    stopped = ctl.get("stop")
    contact_jobs.pop(aid, None)
    added = res.get("added", 0)
    not_user = res.get("not_user", 0)
    failed = res.get("failed", 0)
    title = "📇 CONTACT IMPORT STOPPED ⏹" if stopped else "📇 CONTACT IMPORT FINISHED ✅"
    await log(card(title, [
        f"📱 {phone}",
        f"• Added on Rubika : {added}",
        f"• Not on Rubika : {not_user}",
        f"• Failed : {failed}",
        f"• Total : {len(pairs)}",
        f"🕒 {now()}"]))
    if msg is not None:
        try:
            await safe_edit(msg, _contact_progress_card(ctl),
                            buttons=[[Button.inline("🏠 Main Menu", b"home")]])
        except Exception:
            pass
    await bot.send_message(owner_id,
        ("⏹ Adding contacts stopped. " if stopped else "✅ Adding contacts finished. ")
        + f"\n🟢 On Rubika: {added}   📵 Not on Rubika: {not_user}   ❌ Failed: {failed}",
        buttons=main_menu(owner_id == config.OWNER_ID))


# ---- Item 1: live contact-import controls (stop / pause / resume) ----
@bot.on(events.CallbackQuery(pattern=b"cstop_(\\d+)"))
async def contact_stop_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    ctl = contact_jobs.get(aid)
    if not ctl:
        await event.answer("Nothing to stop.", alert=True)
        return
    ctl["stop"] = True
    ctl["pause"] = False
    await event.answer("⏹ Stop requested. It will stop after the current contact.", alert=True)


@bot.on(events.CallbackQuery(pattern=b"cpause_(\\d+)"))
async def contact_pause_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    ctl = contact_jobs.get(aid)
    if not ctl:
        await event.answer("Nothing to pause.", alert=True)
        return
    ctl["pause"] = True
    await event.answer("⏸ Paused.")


@bot.on(events.CallbackQuery(pattern=b"cresume_(\\d+)"))
async def contact_resume_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    ctl = contact_jobs.get(aid)
    if not ctl:
        await event.answer("Nothing to resume.", alert=True)
        return
    ctl["pause"] = False
    await event.answer("▶️ Resumed.")


# --------------------------------------------------------------------------- #
# Item 2: prefix-based contact-discovery engine (موتور کشف دوست با پیش‌شماره)
# Used by BOTH «➕ افزودن مخاطب» (single account) and «🧠 مغز» (the selected
# fleet). The user gives a prefix of ANY length (e.g. 0913 or 09135646); the
# bot fills the rest of the 11 digits at random, probes them via add_contact
# until DISCOVERY_TARGET (150) Rubika-having numbers are found, keeps a simple
# anti-repeat ledger (leeched_numbers), then feeds the found guids straight into
# the send pipeline in either 'marker' or 'text' mode.
# --------------------------------------------------------------------------- #
def _clean_prefix(raw: str):
    """Normalise a user-supplied prefix into a 0-leading mobile prefix (<=11)."""
    digits = _re_u.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if digits.startswith("98"):
        digits = "0" + digits[2:]
    if not digits.startswith("0"):
        digits = "0" + digits
    return digits[:11]


def _gen_number(prefix: str) -> str:
    need = 11 - len(prefix)
    suffix = "".join(random.choice("0123456789") for _ in range(max(0, need)))
    return prefix + suffix


def _next_candidate(prefix: str, session_seen: set):
    """Return (display_number, normalized) that is NOT already leeched/seen."""
    for _ in range(300):
        num = _gen_number(prefix)
        norm = rb.normalize_phone(num)
        if not norm or norm in session_seen:
            continue
        session_seen.add(norm)
        if db.was_leeched(norm):
            continue
        return num, norm
    return None, None


def _discovery_card(ctl) -> str:
    status = "⏸ Pause" if ctl.get("pause") else ("⏹ Stopping" if ctl.get("stop")
                                                else "🟢 Searching")
    target = ctl.get("target", 0)
    found = ctl.get("found", 0)
    pct = int(found * 100 / target) if target else 0
    return card("🔎 Discover Friends by Prefix — Live", [
        f"📱 {ctl.get('phone', '')}",
        f"• Prefix : {ctl.get('prefix', '')}",
        f"• Status : {status}",
        f"• Found : {found} of {target} — {pct}%",
        f"• Probed : {ctl.get('probed', 0)}",
        f"🕒 {now()}",
    ])


async def _discovery_progress_loop(aid: int, ctl: dict, msg):
    while not ctl.get("finished"):
        paused_btns, running_btns = _ctl_buttons(aid, "c")  # reuse contact controls
        try:
            await safe_edit(msg, _discovery_card(ctl),
                            buttons=paused_btns if ctl.get("pause") else running_btns)
        except Exception:
            pass
        await asyncio.sleep(max(1.0, config.CONTACT_PROGRESS_EVERY))


async def _discover_for_account(acc, prefix, target, ctl, tag=""):
    """Probe random numbers built from `prefix` until `target` Rubika-having
    guids are found (or attempts/stop). Returns the list of found guids."""
    phone = acc["phone"]
    delay = db.get_discovery_delay()
    max_attempts = db.get_discovery_max_attempts()
    session_seen: set = set()
    w = worker.worker_for_account(acc)

    if w and not worker.is_local(w):
        # remote: probe in chunks via the existing /contacts/add endpoint.
        found = []
        probed = 0
        chunk = max(1, config.CONTACT_REMOTE_CHUNK)
        while len(found) < target and probed < max_attempts:
            if await _ctl_gate(ctl):
                break
            nums = []
            for _ in range(chunk):
                disp, norm = _next_candidate(prefix, session_seen)
                if not disp:
                    break
                nums.append((disp, norm))
            if not nums:
                break
            res = await worker.api_call(w, "POST", "/contacts/add", {
                "phone": phone, "numbers": [d for d, _n in nums],
                "delay": delay, "default_first": config.CONTACT_DEFAULT_FIRST,
            }, timeout=7200)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "discovery probe failed"))
            probed += len(nums)
            for _d, norm in nums:
                db.mark_leeched(norm, False)
            for g in (res.get("guids", []) or []):
                if g not in found:
                    found.append(g)
            ctl["found"] = len(found)
            ctl["probed"] = probed
        return found[:target]

    # local: probe one-by-one so the ledger + live counter are exact.
    async def _do(client):
        found = []
        probed = 0
        attempt_fail = 0
        while len(found) < target and probed < max_attempts:
            if await _ctl_gate(ctl):
                break
            disp, norm = _next_candidate(prefix, session_seen)
            if not disp:
                break
            probed += 1
            try:
                r = await asyncio.wait_for(
                    rb.add_contact(client, disp, config.CONTACT_DEFAULT_FIRST),
                    timeout=config.SEND_TIMEOUT)
                attempt_fail = 0
                on_r = bool(r.get("on_rubika"))
                db.mark_leeched(norm, on_r)
                if on_r and r.get("guid") and r["guid"] not in found:
                    found.append(r["guid"])
            except Exception:
                attempt_fail += 1
                if attempt_fail >= db.get_max_errors():
                    await log(card("🔎 Discovery — Pause", [
                        f"{tag}📱 {phone}",
                        f"{db.get_max_errors()} consecutive errors -> waiting {db.get_resume_wait()}s",
                        f"🕒 {now()}"]))
                    await asyncio.sleep(db.get_resume_wait())
                    attempt_fail = 0
            ctl["found"] = len(found)
            ctl["probed"] = probed
            await asyncio.sleep(max(0.0, float(delay)))
        return found[:target]

    return await account_conn.call(phone, _do, timeout=86400)


async def _send_to_guids(owner_id, acc, guids, mode, text, tag=""):
    """Send to a fixed list of guids in 'marker' or 'text' mode. Returns
    (ok, fail). Reuses run_send locally; uses /send/to_list remotely."""
    if not guids:
        return 0, 0
    phone = acc["phone"]
    aid = acc["id"]
    delay = db.get_delay()
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        res = await worker.api_call(w, "POST", "/send/to_list", {
            "phone": phone, "marker": marker, "guids": guids, "delay": delay,
            "max_errors": db.get_max_errors(), "send_timeout": config.SEND_TIMEOUT,
            "mode": mode, "text": text, "text2": db.get_rb_text2(),
            "order": True}, timeout=14400)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "send failed"))
        return res.get("sent", 0), res.get("fail", 0)
    # local
    saved_guid = mid = None
    if mode != "text":
        saved_guid, mid = await _find_marker_local(phone, marker)
        if not mid:
            await log(card("🔎 Discovery — Marker Not Found", [f"{tag}📱 {phone}"]))
            return 0, 0
    r = await run_send(owner_id, {
        "account_id": aid, "phone": phone, "saved_guid": saved_guid, "mid": mid,
        "recipients": guids, "tag": tag, "suppress_resume_panel": True,
        "mode": mode, "text": text, "order_recipients": True})
    return (r or {}).get("ok", 0), (r or {}).get("fail", 0)


async def _run_discovery(owner_id, accounts, prefix, mode, text):
    """Discover DISCOVERY_TARGET rubika-having numbers per account, then send to
    them in the chosen mode, and report the success rate."""
    target = db.get_discovery_target()
    _mode_label = ("no send (build contacts only)" if mode == "none"
                   else ("custom text" if mode == "text" else "marker"))
    for i, a in enumerate(accounts, 1):
        a["_tag"] = f"#A{i}" if len(accounts) > 1 else ""
    await log(card("🔎 DISCOVERY START", [
        f"• Prefix : {prefix}",
        f"• Accounts : {len(accounts)}",
        f"• Target per account : {target} on Rubika",
        f"• Mode : {_mode_label}",
        f"🕒 {now()}"]))
    grand_ok = grand_fail = grand_found = 0
    for acc in accounts:
        aid = acc["id"]
        phone = acc["phone"]
        tag = (acc.get("_tag") or "")
        ltag = (tag + " ") if tag else ""
        ctl = {"stop": False, "pause": False, "found": 0, "probed": 0,
               "target": target, "phone": phone, "prefix": prefix,
               "finished": False}
        contact_jobs[aid] = ctl
        _, running_btns = _ctl_buttons(aid, "c")
        try:
            msg = await bot.send_message(owner_id, _discovery_card(ctl),
                                         buttons=running_btns)
        except Exception:
            msg = None
        prog = asyncio.create_task(_discovery_progress_loop(aid, ctl, msg)) if msg else None
        try:
            guids = await _discover_for_account(acc, prefix, target, ctl, tag=ltag)
        except account_conn.InvalidAuthError:
            db.set_status(aid, "inactive")
            ctl["finished"] = True
            if prog:
                prog.cancel()
            contact_jobs.pop(aid, None)
            await log(card("🔎 Discovery — Invalid Session", [f"{ltag}📱 {phone}", f"🕒 {now()}"]))
            continue
        except Exception as e:  # noqa: BLE001
            ctl["finished"] = True
            if prog:
                prog.cancel()
            contact_jobs.pop(aid, None)
            await log(card("🔎 Discovery — Error", [
                f"{ltag}📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
            continue
        ctl["finished"] = True
        if prog:
            prog.cancel()
        contact_jobs.pop(aid, None)
        grand_found += len(guids)
        await log(card("🔎 Discovery — Account Done", [
            f"{ltag}📱 {phone}",
            f"• Found : {len(guids)} on Rubika",
            f"• Probed : {ctl.get('probed', 0)}",
            f"🕒 {now()}"]))
        if not guids:
            continue
        if mode == "none":
            continue        # «بدون ارسال»: فقط مخاطب ساخته شد، چیزی فرستاده نمی‌شه
        # straight into the send pipeline (Item 2 wiring) — with a STOP button
        stop_flags[aid] = False
        try:
            await bot.send_message(owner_id,
                f"📤 Sending to {len(guids)} built contacts of {phone} started.",
                buttons=[[Button.inline("⏹ Stop Sending", f"stop_{aid}".encode())]])
        except Exception:
            pass
        try:
            ok, fail = await _send_to_guids(owner_id, acc, guids, mode, text, tag=ltag)
            grand_ok += ok
            grand_fail += fail
        except account_conn.InvalidAuthError:
            db.set_status(aid, "inactive")
            await log(card("🔎 Send — Invalid Session", [f"{ltag}📱 {phone}"]))
        except Exception as e:  # noqa: BLE001
            await log(card("🔎 Send — Error", [f"{ltag}📱 {phone}", f"💥 {repr(e)[:140]}"]))
    attempted = grand_ok + grand_fail
    pct = int(grand_ok * 100 / attempted) if attempted else 0
    if mode == "none":
        await log(card("🏁 DISCOVERY — Done (no send)", [
            f"• Total found/built : {grand_found} Rubika contacts",
            "📵 Nothing was sent, per your choice.",
            f"🕒 {now()}"]))
        try:
            await bot.send_message(owner_id, card("🔎 Friend discovery finished ✅ (no send)", [
                f"• Found : {grand_found} Rubika contacts",
                "📵 No send performed — contacts were built; send from the Send section whenever you like."]),
                buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass
        return
    await log(card("🏁 DISCOVERY — Done", [
        f"• Total found : {grand_found} on Rubika",
        f"✅ Sent OK : {grand_ok}   ❌ Failed : {grand_fail}",
        f"• Success rate : {pct}%",
        f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id, card("🔎 Friend discovery finished ✅", [
            f"• Found : {grand_found} on Rubika",
            f"✅ Sent OK : {grand_ok}   ❌ Failed : {grand_fail}",
            f"• Success rate : {pct}%"]),
            buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


def _discovery_mode_buttons():
    return [[Button.inline("📌 Send Marker", b"dmode_marker"),
             Button.inline("✍️ Custom Text", b"dmode_text")],
            [Button.inline("📵 No Send (build only)", b"dmode_none")],
            [Button.inline("🔙 Cancel", b"home")]]


# ----- entry from «➕ افزودن مخاطب»: pick ONE account, then prefix -----
@bot.on(events.CallbackQuery(data=b"discover"))
async def discover_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("Add an account first.", alert=True)
        return
    rows = [[Button.inline(f"🔎 {a['phone']}", f"dpick_{a['id']}".encode())]
            for a in accounts]
    spd = db.get_discovery_delay()
    rows.append([Button.inline(f"⏱ Probe speed: {spd}s", b"dspd_show")])
    rows.append([Button.inline("0.2s", b"dspd_0.2"),
                 Button.inline("0.5s", b"dspd_0.5"),
                 Button.inline("1s", b"dspd_1"),
                 Button.inline("2s", b"dspd_2")])
    rows.append([Button.inline("🔙 Back", b"contacts")])
    await safe_edit(event,
        "🔎 Discover Friends by Prefix\n"
        f"{LINE}\nPick an account, then send the prefix.\n"
        f"The bot continues until it finds {db.get_discovery_target()} Rubika numbers.\n"
        f"⏱ Current probe speed: {spd} seconds (lower = faster but riskier).",
        buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"dspd_(.+)"))
async def discover_speed_cb(event):
    if not is_owner(event):
        return
    val = event.pattern_match.group(1).decode()
    if val == "show":
        await event.answer(f"Current probe speed: {db.get_discovery_delay()}s — tap a preset.",
                           alert=True)
        return
    db.set_discovery_delay(val)
    await event.answer(f"⏱ Probe speed set to {db.get_discovery_delay()}s.")
    await discover_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"dpick_(\\d+)"))
async def discover_pick_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("Account not found.", alert=True)
        return
    state[event.sender_id] = {"step": "await_discover_prefix", "ids": [aid]}
    await safe_edit(event,
        f"☎️ Send the prefix for account {acc['phone']}.\n"
        "Example: `0913` or `09135646` (any length — the rest is filled randomly).",
        buttons=[[Button.inline("🔙 Cancel", b"contacts")]])


@bot.on(events.CallbackQuery(data=b"bdiscover"))
async def brain_discover_cb(event):
    """Entry from «🧠 مغز»: discover for the whole selected fleet."""
    if not is_owner(event):
        return
    sel = list(brain_sel.get(event.sender_id, set()))
    if not sel:
        await event.answer("Pick at least one account first.", alert=True)
        return
    state[event.sender_id] = {"step": "await_discover_prefix", "ids": sel}
    await safe_edit(event,
        f"☎️ Send the prefix. Each of the {len(sel)} accounts will find and send up to "
        f"{db.get_discovery_target()} Rubika numbers.\n"
        "Example: `0913` or `09135646`.",
        buttons=[[Button.inline("🔙 Cancel", b"brain")]])


async def handle_discover_prefix(event, st):
    prefix = _clean_prefix(event.raw_text.strip())
    if not prefix or len(prefix) < 2 or len(prefix) > 11:
        await event.respond("Invalid prefix. Send something like `0913`.")
        return
    if len(prefix) == 11:
        await event.respond("That's a full number, not a prefix. Give a shorter prefix.")
        return
    st["prefix"] = prefix
    st["step"] = "await_discover_mode"
    await event.respond(
        f"☎️ Prefix: `{prefix}`\nNow choose how to send to the found numbers:",
        buttons=_discovery_mode_buttons())


@bot.on(events.CallbackQuery(data=b"dmode_marker"))
async def discover_mode_marker_cb(event):
    if not is_owner(event):
        return
    st = state.get(event.sender_id) or {}
    if st.get("step") != "await_discover_mode":
        await event.answer("Expired. Start again.", alert=True)
        return
    ids = st.get("ids") or []
    prefix = st.get("prefix")
    state.pop(event.sender_id, None)
    accounts = [a for a in (db.get_account(i) for i in ids) if a]
    if not accounts or not prefix:
        await event.answer("Info is incomplete.", alert=True)
        return
    await safe_edit(event, "🔎 Friend discovery started (mode: marker). Reports go to the log group.",
                    buttons=[[Button.inline("🏠 Main Menu", b"home")]])
    asyncio.create_task(_run_discovery(event.sender_id, accounts, prefix, "marker", ""))


@bot.on(events.CallbackQuery(data=b"dmode_none"))
async def discover_mode_none_cb(event):
    """Discover only — find the rubika-having numbers but DO NOT send anything."""
    if not is_owner(event):
        return
    st = state.get(event.sender_id) or {}
    if st.get("step") != "await_discover_mode":
        await event.answer("Expired. Start again.", alert=True)
        return
    ids = st.get("ids") or []
    prefix = st.get("prefix")
    state.pop(event.sender_id, None)
    accounts = [a for a in (db.get_account(i) for i in ids) if a]
    if not accounts or not prefix:
        await event.answer("Info is incomplete.", alert=True)
        return
    await safe_edit(event, "🔎 Friend discovery started (no send — build contacts only). Reports go to the log group.",
                    buttons=[[Button.inline("🏠 Main Menu", b"home")]])
    asyncio.create_task(_run_discovery(event.sender_id, accounts, prefix, "none", ""))


@bot.on(events.CallbackQuery(data=b"dmode_text"))
async def discover_mode_text_cb(event):
    if not is_owner(event):
        return
    st = state.get(event.sender_id) or {}
    if st.get("step") != "await_discover_mode":
        await event.answer("Expired. Start again.", alert=True)
        return
    st["step"] = "await_discover_text"
    await safe_edit(event, "✍️ Send the text to send to the found numbers:",
                    buttons=[[Button.inline("🔙 Cancel", b"home")]])


async def handle_discover_text(event, st):
    text = event.raw_text.strip()
    if not text:
        await event.respond("Text can't be empty. Send it again.")
        return
    ids = st.get("ids") or []
    prefix = st.get("prefix")
    state.pop(event.sender_id, None)
    accounts = [a for a in (db.get_account(i) for i in ids) if a]
    if not accounts or not prefix:
        await event.respond("Info is incomplete. Start again.",
                            buttons=main_menu(is_real_owner(event)))
        return
    await event.respond("🔎 Friend discovery started (mode: custom text). Reports go to the log group.",
                        buttons=main_menu(is_real_owner(event)))
    asyncio.create_task(_run_discovery(event.sender_id, accounts, prefix, "text", text))


# --------------------------------------------------------------------------- #
# Multi-account send
# --------------------------------------------------------------------------- #
def _multisend_menu(owner_id):
    sel = multisend_sel.setdefault(owner_id, set())
    rows = []
    for a in db.list_accounts():
        mark = "✅" if a["id"] in sel else "⬜️"
        tag = "" if a["status"] == "active" else " ⚠️"
        rows.append([Button.inline(f"{mark} {a['phone']}{tag}",
                                   f"msel_{a['id']}".encode())])
    rows.append([Button.inline("🚀 Start Selected Sends", b"mstart")])
    rows.append([Button.inline("⏹ Stop All", b"mstopall"),
                 Button.inline("🔙 Back", b"home")])
    return rows


@bot.on(events.CallbackQuery(data=b"multisend"))
async def multisend_cb(event):
    if not is_owner(event):
        return
    if not db.list_accounts():
        await safe_edit(event, "Add an account first.",
                        buttons=[[Button.inline("➕ Add Account", b"add_account")],
                                 [Button.inline("🔙 Back", b"home")]])
        return
    await safe_edit(event,
        "📤 Concurrent Multi-account Send\n"
        f"{LINE}\nSelect accounts, then tap Start.\n"
        "Accounts on the same worker run sequentially; different workers run in parallel.",
        buttons=_multisend_menu(event.sender_id))


@bot.on(events.CallbackQuery(pattern=b"msel_(\\d+)"))
async def multisend_sel_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    sel = multisend_sel.setdefault(event.sender_id, set())
    if aid in sel:
        sel.discard(aid)
    else:
        sel.add(aid)
    await safe_edit(event, "📤 Concurrent Multi-account Send — select:",
                    buttons=_multisend_menu(event.sender_id))


@bot.on(events.CallbackQuery(data=b"mstopall"))
async def multisend_stopall_cb(event):
    if not is_owner(event):
        return
    multisend_stop[event.sender_id] = True
    for aid in multisend_sel.get(event.sender_id, set()):
        stop_flags[aid] = True
    await event.answer("Stop-all requested.", alert=True)


@bot.on(events.CallbackQuery(data=b"mstart"))
async def multisend_start_cb(event):
    if not is_owner(event):
        return
    sel = list(multisend_sel.get(event.sender_id, set()))
    if not sel:
        await event.answer("No account selected.", alert=True)
        return
    accounts = [db.get_account(i) for i in sel]
    accounts = [a for a in accounts if a]
    total_acc = len(accounts)
    await safe_edit(event,
        card("📤 CONFIRM MULTI-ACCOUNT SEND", [
            f"• Selected accounts : {total_acc}",
            "• Order : same-worker sequential, different-worker parallel.",
            "Each account's report goes separately to the log group."]),
        buttons=[[Button.inline("✅ Start", b"mgo")],
                 [Button.inline("🔙 Back", b"multisend")]])


@bot.on(events.CallbackQuery(data=b"mgo"))
async def multisend_go_cb(event):
    if not is_owner(event):
        return
    sel = list(multisend_sel.get(event.sender_id, set()))
    if not sel:
        await event.answer("Nothing selected.", alert=True)
        return
    await safe_edit(event, "🚀 Multi-account send started. Reports go to the log group.",
                    buttons=[[Button.inline("⏹ Stop All", b"mstopall")],
                             [Button.inline("🏠 Main Menu", b"home")]])
    asyncio.create_task(_run_multi_send(event.sender_id, sel))


async def _prepare_local(acc, marker):
    await account_conn.close(acc["phone"])
    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            return None
        ordered, _stats = await rb.get_ordered_recipients(client)
        return saved_guid, mid, [r["guid"] for r in ordered]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _multi_send_one(owner_id, acc, tag):
    aid = acc["id"]
    phone = acc["phone"]
    if continuous_busy(aid) or aid in active_jobs:
        await log(card("⏭ MULTI — Skipped", [
            f"{tag} 📱 {phone}", "account was busy/locked", f"🕒 {now()}"]))
        return
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.check_worker(w)
        except Exception:
            pass
        w = db.get_worker(w["id"])
        if not (w and w["enabled"] and w["status"] == "ok"):
            await log(card("⏭ MULTI — Unhealthy Worker", [f"{tag} 📱 {phone}", f"🕒 {now()}"]))
            return
        try:
            res = await worker.api_call(w, "POST", "/prepare",
                                        {"phone": phone, "marker": marker})
        except Exception as e:  # noqa: BLE001
            await log(card("⚠️ MULTI — Remote Prep Error", [
                f"{tag} 📱 {phone}", f"💥 {repr(e)[:120]}"]))
            return
        if not res.get("marker_found") or not res.get("total"):
            await log(card("⏭ MULTI — No Marker/Recipient", [f"{tag} 📱 {phone}"]))
            return
        await run_send_remote(owner_id, {
            "account_id": aid, "phone": phone, "remote": True,
            "worker_id": w["id"], "total": res["total"]})
        return
    # local
    try:
        prep = await _prepare_local(acc, marker)
    except account_conn.InvalidAuthError:
        db.set_status(aid, "inactive")
        await log(card("⏭ MULTI — Dead Account (skipped)", [f"{tag} 📱 {phone}", f"🕒 {now()}"]))
        return
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ MULTI — Prep Error", [
            f"{tag} 📱 {phone}", f"💥 {repr(e)[:120]}"]))
        return
    if not prep:
        await log(card("⏭ MULTI — Marker Not Found", [f"{tag} 📱 {phone}"]))
        return
    saved_guid, mid, recips = prep
    if not recips:
        await log(card("⏭ MULTI — No Recipient", [f"{tag} 📱 {phone}"]))
        return
    await run_send(owner_id, {
        "account_id": aid, "phone": phone, "saved_guid": saved_guid, "mid": mid,
        "recipients": recips, "tag": tag, "suppress_resume_panel": True})


async def _run_group_sequential(owner_id, accs):
    for acc in accs:
        if multisend_stop.get(owner_id):
            break
        await _multi_send_one(owner_id, acc, acc.get("_tag", ""))


async def _run_multi_send(owner_id, account_ids):
    accounts = [db.get_account(i) for i in account_ids]
    accounts = [a for a in accounts if a]
    if not accounts:
        return
    for i, a in enumerate(accounts, 1):
        a["_tag"] = f"#A{i}"
    multisend_stop[owner_id] = False
    groups = {}
    for a in accounts:
        w = worker.worker_for_account(a)
        wid = w["id"] if w else 0
        groups.setdefault(wid, []).append(a)
    await log(card("📤 MULTI SEND START", [
        f"• Accounts : {len(accounts)}",
        f"• Worker groups : {len(groups)} (same-worker sequential, different parallel)",
        f"🕒 {now()}"]))
    tasks = [asyncio.create_task(_run_group_sequential(owner_id, g))
             for g in groups.values()]
    await asyncio.gather(*tasks, return_exceptions=True)
    await log(card("🏁 MULTI SEND — All Done", [
        f"• {len(accounts)} accounts", f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id, "🏁 Multi-account send finished.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Brain: split a heavy number file across selected accounts, add contacts,
# then forward the marked message to up to BRAIN_SEND_CAP of each account's
# freshly-added contacts.
# --------------------------------------------------------------------------- #
def _brain_menu(owner_id):
    sel = brain_sel.setdefault(owner_id, set())
    rows = []
    for a in db.list_accounts():
        mark = "✅" if a["id"] in sel else "⬜️"
        rows.append([Button.inline(f"{mark} {a['phone']}", f"bsel_{a['id']}".encode())])
    rows.append([Button.inline("📂 Upload Numbers File & Start", b"bfile")])
    rows.append([Button.inline("🔎 Discover Friends by Prefix", b"bdiscover")])
    rows.append([Button.inline("🔙 Back", b"home")])
    return rows


@bot.on(events.CallbackQuery(data=b"brain"))
async def brain_cb(event):
    if not is_owner(event):
        return
    if not db.list_accounts():
        await safe_edit(event, "Add an account first.",
                        buttons=[[Button.inline("➕ Add Account", b"add_account")],
                                 [Button.inline("🔙 Back", b"home")]])
        return
    await safe_edit(event,
        "🧠 Brain — Split Numbers Across Accounts\n"
        f"{LINE}\nSelect accounts, then upload the numbers file.\n"
        "Numbers are split evenly across accounts, added, then sent to "
        f"{db.get_brain_cap()} added contacts per account.",
        buttons=_brain_menu(event.sender_id))


@bot.on(events.CallbackQuery(pattern=b"bsel_(\\d+)"))
async def brain_sel_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    sel = brain_sel.setdefault(event.sender_id, set())
    if aid in sel:
        sel.discard(aid)
    else:
        sel.add(aid)
    await safe_edit(event, "🧠 Brain — select accounts:",
                    buttons=_brain_menu(event.sender_id))


@bot.on(events.CallbackQuery(data=b"bfile"))
async def brain_file_prompt_cb(event):
    if not is_owner(event):
        return
    sel = brain_sel.get(event.sender_id, set())
    if not sel:
        await event.answer("Pick at least one account first.", alert=True)
        return
    state[event.sender_id] = {"step": "await_brain_file", "ids": list(sel)}
    await safe_edit(event,
        f"📂 Send the numbers txt file. It's split evenly across {len(sel)} accounts.",
        buttons=[[Button.inline("🔙 Cancel", b"brain")]])


async def handle_brain_file(event, st):
    ids = st.get("ids") or []
    accounts = [db.get_account(i) for i in ids]
    accounts = [a for a in accounts if a]
    if not accounts:
        state.pop(event.sender_id, None)
        await event.respond("No valid account.", buttons=main_menu(is_real_owner(event)))
        return
    if not event.file:
        await event.respond("Send a txt file (or tap Cancel).")
        return
    try:
        data = await event.download_media(file=bytes)
        text = data.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ Couldn't read the file: {repr(e)[:120]}")
        return
    pairs = _norm_pairs_from_text(text)
    state.pop(event.sender_id, None)
    if not pairs:
        await event.respond("There were no valid numbers.",
                            buttons=main_menu(is_real_owner(event)))
        return
    # split equally (round-robin so the remainder spreads evenly)
    shares = {a["id"]: [] for a in accounts}
    order = [a["id"] for a in accounts]
    for i, pr in enumerate(pairs):
        shares[order[i % len(order)]].append(pr)
    await event.respond(
        f"🧠 {len(pairs)} unique numbers split across {len(accounts)} accounts. "
        "Starting to add ... reports go to the log group.",
        buttons=[[Button.inline("⏹ Stop Brain", b"bstop")],
                 [Button.inline("🏠 Main Menu", b"home")]])
    # Register the run SYNCHRONOUSLY (before the task is scheduled) so a stop
    # tapped in the tiny window before the coroutine starts is still honored —
    # exactly matching the base's old `brain_engine["stop"] = False` timing.
    brain_control.controller.start(event.sender_id, [a["id"] for a in accounts])
    asyncio.create_task(_run_brain(event.sender_id, accounts, shares))


async def _run_brain(owner_id, accounts, shares):
    for i, a in enumerate(accounts, 1):
        a["_tag"] = f"#A{i}"
    await log(card("🧠 BRAIN START", [
        f"👥 Accounts : {len(accounts)}",
        f"🎯 Total numbers : {sum(len(v) for v in shares.values())}",
        "🧩 Split evenly across accounts", f"🕒 {now()}"]))
    total_added = 0
    per_acc = {}     # account_id -> {"acc":acc,"guids":[...],"added":n,"failed":n}
    delay = db.get_contact_delay()
    for a in accounts:
        if brain_control.controller.is_stopped(owner_id):
            await log(card("🧠 BRAIN — MANUAL STOP (adding)", [f"🕒 {now()}"]))
            break
        tag = a["_tag"]
        pairs = shares.get(a["id"], [])
        if not pairs:
            continue
        await log(card("🧠 ADD CONTACTS", [
            f"{tag} 📱 {a['phone']}", f"🎯 Share : {len(pairs)}", f"🕒 {now()}"]))
        # Hand the add loop a live ctl the controller owns, so a "توقف مغز" tap
        # interrupts THIS account mid-list (not only at the account boundary).
        # Reuses the base's existing _ctl_gate — the contact-add algorithm is
        # unchanged; we only supply a control object it already understands.
        _brain_ctl = brain_control.controller.ctl_for(owner_id, a["id"])
        try:
            res = await _contacts_add(a, pairs, delay, tag=tag + " ", ctl=_brain_ctl)
        except account_conn.InvalidAuthError:
            db.set_status(a["id"], "inactive")
            await log(card("🧠 ADD — account dropped (skipped)", [f"{tag} 📱 {a['phone']}"]))
            continue
        except Exception as e:  # noqa: BLE001
            await log(card("🧠 ADD — ERROR", [
                f"{tag} 📱 {a['phone']}", f"💥 {repr(e)[:140]}"]))
            continue
        total_added += res.get("added", 0)
        per_acc[a["id"]] = {"acc": a, "guids": res.get("guids", []),
                            "added": res.get("added", 0), "failed": res.get("failed", 0)}
        await log(card("🧠 ADD — account done", [
            f"{tag} 📱 {a['phone']}",
            f"✅ Added : {res.get('added', 0)}",
            f"❌ Failed : {res.get('failed', 0)}",
            f"🕒 {now()}"]))
    brain_jobs[owner_id] = per_acc
    await log(card("🧠 BRAIN — ADDING FINISHED", [
        f"✅ Total contacts added : {total_added}",
        f"👥 Accounts : {len(per_acc)}", f"🕒 {now()}"]))
    rows = [[Button.inline(f"🚀 Send to added contacts (up to {db.get_brain_cap()})",
                           b"bsend")],
            [Button.inline("🏠 Main Menu", b"home")]]
    try:
        await bot.send_message(owner_id, card("🧠 ADD CONTACTS FINISHED ✅", [
            f"✅ Total added : {total_added} Rubika contacts",
            f"👥 Accounts : {len(per_acc)}",
            "Now you can forward the marker to the added contacts."]), buttons=rows)
    except Exception:
        pass


@bot.on(events.CallbackQuery(data=b"bsend"))
async def brain_send_cb(event):
    if not is_owner(event):
        return
    job = brain_jobs.get(event.sender_id)
    if not job:
        await safe_edit(event, "Brain info expired. Start again from Brain.",
                        buttons=[[Button.inline("🏠 Main Menu", b"home")]])
        return
    marker = db.get_marker()
    plain = get_plain_text()
    await safe_edit(event, card("🧠 READY TO SEND", [
        f"📌 Marker : «{marker}»",
        f"📝 Plain text : {('«'+plain[:60]+'»') if plain else '—'}",
        f"🎯 Up to {db.get_brain_cap()} added contacts per account",
        "Choose the send method:"]),
        buttons=[[Button.inline("📎 Forward Marker", b"bsendgo")],
                 [Button.inline("✍️ Plain Text", b"bsendgotext")],
                 [Button.inline("🔙 Back", b"home")]])


@bot.on(events.CallbackQuery(data=b"bsendgo"))
async def brain_send_go_cb(event):
    if not is_owner(event):
        return
    job = brain_jobs.pop(event.sender_id, None)
    if not job:
        await event.answer("Info expired.", alert=True)
        return
    await safe_edit(event, "🚀 Brain send started. Reports go to the log group.",
                    buttons=[[Button.inline("⏹ Stop Brain", b"bstop")],
                             [Button.inline("🏠 Main Menu", b"home")]])
    # Register SYNCHRONOUSLY before scheduling (see _run_brain note).
    brain_control.controller.start(event.sender_id, list(job.keys()))
    asyncio.create_task(_run_brain_send(event.sender_id, job))


@bot.on(events.CallbackQuery(data=b"bsendgotext"))
async def brain_send_go_text_cb(event):
    """Brain SEND phase in PLAIN-TEXT mode (no forward) to the added contacts."""
    if not is_owner(event):
        return
    body = get_plain_text()
    if not body:
        await safe_edit(event,
            "📝 No plain text is set yet. Set it first from Content -> Plain Text.",
            buttons=[[Button.inline("📌 Content", b"marker")],
                     [Button.inline("🏠 Main Menu", b"home")]])
        return
    job = brain_jobs.pop(event.sender_id, None)
    if not job:
        await event.answer("Info expired.", alert=True)
        return
    await safe_edit(event, "🚀 Brain send (plain text) started. Reports go to the log group.",
                    buttons=[[Button.inline("⏹ Stop Brain", b"bstop")],
                             [Button.inline("🏠 Main Menu", b"home")]])
    brain_control.controller.start(event.sender_id, list(job.keys()))
    asyncio.create_task(_run_brain_send(event.sender_id, job, mode="text", body=body))


@bot.on(events.CallbackQuery(data=b"bstop"))
async def brain_stop_cb(event):
    if not is_owner(event):
        return
    owner_id = event.sender_id
    # Stop ONLY this owner's brain accounts. controller.stop() flips the live
    # ctls (so the account currently ADDING halts mid-list) and returns exactly
    # the accounts in this run — never unrelated DB accounts (bug 3). We also
    # raise the legacy stop_flags so any in-flight local run_send loops (the
    # SEND phase) break at their next message.
    try:
        ids = brain_control.controller.stop(owner_id)
        for aid in ids:
            stop_flags[aid] = True
    except Exception:
        pass
    await event.answer("⏹ Stop Brain requested. The current account also stops immediately.", alert=True)


async def _run_brain_send(owner_id, job, mode="marker", body=""):
    marker = db.get_marker()
    delay = db.get_delay()
    cap = db.get_brain_cap()
    mode = (mode or "marker").lower()
    await log(card("🧠 BRAIN SEND START", [
        (f"✍️ Plain text : «{body[:60]}»" if mode == "text"
         else f"📌 Marker : «{marker}»"),
        f"🎯 Cap per account : {cap}", f"🕒 {now()}"]))
    for aid, info in job.items():
        if brain_control.controller.is_stopped(owner_id):
            await log(card("🧠 BRAIN SEND — MANUAL STOP", [f"🕒 {now()}"]))
            break
        acc = info["acc"]
        tag = acc.get("_tag", "")
        guids = (info.get("guids") or [])[:cap]
        phone = acc["phone"]
        if not guids:
            await log(card("🧠 SEND — no contacts (skipped)", [
                f"{tag} 📱 {phone}",
                "No added-contact guid was recorded.", f"🕒 {now()}"]))
            continue
        w = worker.worker_for_account(acc)
        if w and not worker.is_local(w):
            try:
                res = await worker.api_call(w, "POST", "/send/to_list", {
                    "phone": phone, "marker": marker, "guids": guids,
                    "delay": delay, "max_errors": db.get_max_errors(),
                    "send_timeout": config.SEND_TIMEOUT,
                    "text2": db.get_rb_text2(), "order": True,
                    "mode": mode, "text": body}, timeout=14400)
                if not res.get("ok"):
                    raise RuntimeError(res.get("error", "send failed"))
                await log(card("🧠 SEND — account done (worker)", [
                    f"{tag} 📱 {phone}",
                    f"✅ {res.get('sent', 0)}   ❌ {res.get('fail', 0)}",
                    f"🕒 {now()}"]))
            except Exception as e:  # noqa: BLE001
                await log(card("🧠 SEND — remote error", [
                    f"{tag} 📱 {phone}", f"💥 {repr(e)[:140]}"]))
            continue
        # local plain-text mode: no marker needed, send_text via run_send
        if mode == "text":
            await run_send(owner_id, {
                "account_id": aid, "phone": phone, "saved_guid": "", "mid": "",
                "mode": "text", "text": body,
                "recipients": guids, "tag": tag, "suppress_resume_panel": True,
                "order_recipients": True})
            continue
        # local: find marker then forward to the collected guids
        try:
            saved_guid, mid = await _find_marker_local(phone, marker)
        except account_conn.InvalidAuthError:
            db.set_status(aid, "inactive")
            await log(card("🧠 SEND — account dropped (skipped)", [f"{tag} 📱 {phone}"]))
            continue
        except Exception as e:  # noqa: BLE001
            await log(card("🧠 SEND — marker error", [
                f"{tag} 📱 {phone}", f"💥 {repr(e)[:140]}"]))
            continue
        if not mid:
            await log(card("🧠 SEND — marker not found", [f"{tag} 📱 {phone}"]))
            continue
        await run_send(owner_id, {
            "account_id": aid, "phone": phone, "saved_guid": saved_guid, "mid": mid,
            "recipients": guids, "tag": tag, "suppress_resume_panel": True,
            "order_recipients": True})
    await log(card("🏁 BRAIN SEND — DONE", [f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id, "🏁 Brain send finished.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


async def _find_marker_local(phone, marker):
    await account_conn.close(phone)
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        return await rb.find_marked_message(client, marker)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# =========================================================================== #
# Item 3: linkdooni engine (موتور لینکدونی)
#   • harvest GROUP invite links from "linkdooni" channels,
#   • discover up to N NEW groups/day (grand total), split round-robin across
#     the selected fleet, each account joins its share,
#   • send the configured texts into those groups at a per-account interval,
#   • auto-replace an account that gets banned/muted in a group with a free one,
#   • run the PV secretary alongside,
#   • post a per-account stats card every LINKDOONI_SUMMARY_INTERVAL,
#   • a single stop button shuts the whole engine down.
# Works for local accounts (full: send + ban-replacement) and remote/worker
# accounts (join + scheduled send via the worker /linkdooni endpoints).
# =========================================================================== #
def _ld_fleet_accounts() -> list:
    out = []
    for aid in db.list_linkdooni_account_ids():
        a = db.get_account(aid)
        if a and a.get("status") == "active":
            out.append(a)
    return out


def _ld_pick_reader(fleet: list):
    """Prefer a local account to read channels; fall back to the first one."""
    for a in fleet:
        w = worker.worker_for_account(a)
        if not w or worker.is_local(w):
            return a
    return fleet[0] if fleet else None


async def _ld_extract_links(reader, channels: list, want: int) -> list:
    """Harvest group invite links from the channels using `reader`'s session."""
    phone = reader["phone"]
    w = worker.worker_for_account(reader)
    refs = [c["ref"] for c in channels]
    if w and not worker.is_local(w):
        try:
            res = await worker.api_call(w, "POST", "/linkdooni/extract", {
                "phone": phone, "channels": refs,
                "scan": config.LINKDOONI_CHANNEL_SCAN}, timeout=600)
            return res.get("links", []) or []
        except Exception:
            return []
    links = []
    async with account_conn.connection(phone) as client:
        for ref in refs:
            if len(links) >= want * 3:        # gather a healthy surplus
                break
            try:
                guid, _title = await asyncio.wait_for(
                    rb.resolve_channel(client, ref), timeout=60)
            except Exception:
                continue
            try:
                msgs = await asyncio.wait_for(
                    rb.get_recent_messages(client, guid, config.LINKDOONI_CHANNEL_SCAN),
                    timeout=90)
            except Exception:
                msgs = []
            for m in msgs:
                for lk in rb.extract_group_links(rb._msg_text_of(m)):
                    if lk not in links:
                        links.append(lk)
    return links


async def _ld_resolve_guid(reader, link: str):
    """Best-effort: resolve a group invite link to its guid WITHOUT joining."""
    phone = reader["phone"]
    w = worker.worker_for_account(reader)
    if w and not worker.is_local(w):
        return None       # remote preview unsupported -> rely on join result
    try:
        async with account_conn.connection(phone) as client:
            return await rb.get_group_guid_by_link(client, link)
    except Exception:
        return None


async def _ld_join(acc, link: str):
    """Join a group via link on `acc`. Returns the group guid (or None)."""
    phone = acc["phone"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            res = await worker.api_call(w, "POST", "/group/join",
                                        {"phone": phone, "links": [link]}, timeout=120)
            return None if not res.get("joined") else "joined"
        except Exception:
            return None
    try:
        async with account_conn.connection(phone) as client:
            res = await asyncio.wait_for(rb.join_group_by_link(client, link), timeout=90)
            return rb.join_result_group_guid(res)
    except Exception:
        return None


async def _ld_discover_join():
    """One discovery+join cycle, respecting the daily grand-total cap."""
    cfg = db.get_linkdooni_config()
    fleet = _ld_fleet_accounts()
    channels = db.list_linkdooni_channels()
    if not fleet or not channels:
        return
    daily = int(cfg.get("daily_groups") or config.LINKDOONI_DAILY_GROUPS)
    remaining = daily - db.linkdooni_groups_today_count()
    if remaining <= 0:
        return
    reader = _ld_pick_reader(fleet)
    links = await _ld_extract_links(reader, channels, remaining)
    # keep only NEW links (dedup ledger), cap to the remaining daily quota
    new_links = []
    for lk in links:
        if len(new_links) >= remaining:
            break
        if db.linkdooni_seen_link(lk):
            new_links.append(lk)
    if not new_links:
        await log(card("📨 Linkdooni — Discovery", [
            "No new group found (or the daily cap is reached).", f"🕒 {now()}"]))
        return
    await log(card("📨 Linkdooni — Group Discovery", [
        f"• New links : {len(new_links)}",
        f"• Daily cap left : {remaining}",
        f"• Accounts : {len(fleet)}", f"🕒 {now()}"]))
    joined = 0
    failed = 0
    for i, lk in enumerate(new_links):
        acc = fleet[i % len(fleet)]
        guid = await _ld_resolve_guid(reader, lk)
        joined_guid = await _ld_join(acc, lk)
        if not guid:
            guid = joined_guid if (joined_guid and joined_guid != "joined") else None
        if not guid:
            failed += 1
            await log(card("📨 Linkdooni — guid not found (skipped)", [
                f"📱 {acc['phone']}", f"🔗 {lk}",
                "This Rubika build didn't return the link preview; this group was skipped.", f"🕒 {now()}"]))
            continue
        db.add_linkdooni_group(guid, lk, acc["id"])
        db.mark_linkdooni_group_joined(guid, True)
        db.incr_linkdooni_joined(acc["id"], 1)
        joined += 1
        await log(card("📨 Linkdooni — Join", [
            f"📱 {acc['phone']}", f"👥 {guid}", f"🔗 {lk}", f"🕒 {now()}"]))
        await asyncio.sleep(config.GROUP_JOIN_DELAY)
    await log(card("📨 Linkdooni — Discovery/Join Done", [
        f"✅ Joined : {joined}   ❌ Failed : {failed}", f"🕒 {now()}"]))
    # (re)start senders so newly joined groups start receiving messages
    for acc in fleet:
        await _ld_start_sender(acc)


async def _ld_replace(banned_acc, guid: str, name: str, link: str):
    """A group banned/muted `banned_acc`: hand it to a free fleet account."""
    fleet = _ld_fleet_accounts()
    cand = None
    for a in fleet:
        if a["id"] != banned_acc["id"]:
            cand = a
            break
    # record the ban for the cleanup engine (same as automation does)
    try:
        is_new = db.add_cleanup_candidate(banned_acc["id"], guid, name,
                                          reason="banned/muted in a Linkdooni group")
        if is_new:
            await _log_cleanup_candidate(banned_acc["id"], banned_acc["phone"], guid, name)
    except Exception:
        pass
    if not cand:
        db.mark_linkdooni_group_joined(guid, False)
        await log(card("📨 Linkdooni — No Replacement", [
            f"📱 {banned_acc['phone']} was banned in the group but no free account is available.",
            f"👥 {guid}", f"🕒 {now()}"]))
        return
    db.reassign_linkdooni_group(guid, cand["id"])
    joined_guid = await _ld_join(cand, link)
    if joined_guid:
        db.mark_linkdooni_group_joined(guid, True)
        db.incr_linkdooni_joined(cand["id"], 1)
        await log(card("📨 Linkdooni — Account Replacement", [
            f"🚫 {banned_acc['phone']} was banned",
            f"✅ Replacement : {cand['phone']}",
            f"👥 {guid}", f"🕒 {now()}"]))
        await _ld_start_sender(cand)        # make sure replacement is sending
    else:
        await log(card("📨 Linkdooni — Replacement Didn't Join", [
            f"⚠️ Replacement {cand['phone']} couldn't join",
            f"👥 {guid}", f"🕒 {now()}"]))


async def _run_linkdooni_local(account_id: int, phone: str, st: dict):
    """Local linkdooni send loop — ONE connection per pass. Reads the account's
    assigned+joined groups FRESH each pass (so reassignments are picked up),
    sends a random configured text to each, and triggers auto-replacement when a
    group bans/mutes the account (3 strikes)."""
    fails: dict = {}
    last_text: dict = {}
    try:
        while not st.get("stop"):
            st["heartbeat"] = time.monotonic()
            texts = db.list_linkdooni_texts()
            interval = (db.get_linkdooni_config().get("send_interval")
                        or config.LINKDOONI_SEND_INTERVAL)
            if texts:
                try:
                    async with account_conn.connection(phone) as client:
                        groups = db.list_linkdooni_groups(account_id, joined_only=True)
                        st["groups"] = len(groups)
                        for g in groups:
                            if st.get("stop"):
                                break
                            guid = g["group_guid"]
                            idx, txt = _pick_text(texts, last_text.get(guid))
                            if txt is None:
                                break
                            try:
                                await asyncio.wait_for(
                                    rb.send_text(client, guid, txt),
                                    timeout=config.SEND_TIMEOUT)
                                st["sent"] = st.get("sent", 0) + 1
                                last_text[guid] = idx
                                fails[guid] = 0
                                try:
                                    db.incr_linkdooni_sent(account_id, 1)
                                except Exception:
                                    pass
                            except Exception:
                                fails[guid] = fails.get(guid, 0) + 1
                                if fails[guid] >= 3:
                                    acc = db.get_account(account_id)
                                    if acc:
                                        try:
                                            await _ld_replace(acc, guid,
                                                              g.get("name", ""),
                                                              g.get("link", ""))
                                        except Exception:
                                            pass
                                    fails[guid] = 0
                            await asyncio.sleep(random.uniform(
                                config.LINKDOONI_GROUP_DELAY_MIN,
                                config.LINKDOONI_GROUP_DELAY_MAX))
                except Exception as e:  # noqa: BLE001
                    account_conn.drop_connection(phone)
                    if account_conn.is_auth_error(e):
                        await log(f"⚠️ Linkdooni '{phone}' auth error (continuing): {repr(e)[:120]}")
            st["heartbeat"] = time.monotonic()
            waited = 0
            while waited < interval and not st.get("stop"):
                await asyncio.sleep(1)
                waited += 1
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Linkdooni '{phone}' stopped: {repr(e)[:140]}")


async def _ld_start_sender(acc):
    """Start/refresh the linkdooni send loop for one account (local or remote)."""
    aid = acc["id"]
    phone = acc["phone"]
    interval = config.clamp_linkdooni_interval(
        db.get_linkdooni_config().get("send_interval"))
    texts = db.list_linkdooni_texts()
    groups = db.list_linkdooni_groups(aid, joined_only=True)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        if not texts or not groups:
            return
        try:
            await worker.api_call(w, "POST", "/linkdooni/start", {
                "phone": phone,
                "group_guids": [g["group_guid"] for g in groups],
                "texts": texts, "interval": interval}, timeout=60)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Remote Linkdooni {phone} didn't start: {repr(e)[:120]}")
        return
    # local: (re)start the task only if it is not already running
    t = linkdooni_tasks.get(aid)
    if t and not t["task"].done():
        return
    stx = {"stop": False, "sent": (t["state"].get("sent", 0) if t else 0),
           "groups": 0, "heartbeat": time.monotonic()}
    task = asyncio.create_task(_run_linkdooni_local(aid, phone, stx))
    linkdooni_tasks[aid] = {"task": task, "state": stx}


async def _ld_stop_sender(acc):
    aid = acc["id"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/linkdooni/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    t = linkdooni_tasks.pop(aid, None)
    if t:
        t["state"]["stop"] = True
        task = t.get("task")
        if task and not task.done():
            task.cancel()


async def _ld_start_secretary_fleet():
    for acc in _ld_fleet_accounts():
        try:
            db.set_secretary_enabled(acc["id"], True)
            await start_secretary(acc)
        except Exception:
            pass


async def _ld_stop_secretary_fleet():
    for aid in db.list_linkdooni_account_ids():
        acc = db.get_account(aid)
        if not acc:
            continue
        try:
            db.set_secretary_enabled(aid, False)
            await stop_secretary(acc)
        except Exception:
            pass


async def _linkdooni_engine_loop():
    eng = linkdooni_engine
    last_discover = 0.0
    await _ld_start_secretary_fleet()
    while not eng.get("stop"):
        try:
            nowm = time.monotonic()
            if last_discover == 0.0 or (nowm - last_discover) >= config.LINKDOONI_DISCOVER_INTERVAL:
                await _ld_discover_join()
                last_discover = time.monotonic()
            # keep local senders alive (self-heal dead tasks)
            for acc in _ld_fleet_accounts():
                await _ld_start_sender(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ Linkdooni engine round error (continuing): {repr(e)[:140]}")
        waited = 0
        while waited < 60 and not eng.get("stop"):
            await asyncio.sleep(2)
            waited += 2


async def start_linkdooni_engine():
    if not _ld_fleet_accounts():
        return False, "No account selected for Linkdooni."
    if not db.list_linkdooni_channels():
        return False, "No Linkdooni channel added."
    if not db.list_linkdooni_texts():
        return False, "No text set to send."
    db.set_linkdooni_enabled(True)
    old = linkdooni_engine.get("task")
    if old and not old.done():
        linkdooni_engine["stop"] = True
        try:
            await asyncio.wait_for(old, timeout=5)
        except Exception:
            pass
    linkdooni_engine["stop"] = False
    linkdooni_engine["task"] = asyncio.create_task(_linkdooni_engine_loop())
    await log(card("📨 LINKDOONI ENGINE — ON", [
        f"• Accounts : {len(_ld_fleet_accounts())}",
        f"• Channels : {len(db.list_linkdooni_channels())}",
        f"• Texts : {len(db.list_linkdooni_texts())}",
        f"• Daily group cap : {db.get_linkdooni_config().get('daily_groups')}",
        f"• Send interval : {db.get_linkdooni_config().get('send_interval')}s",
        f"🕒 {now()}"]))
    return True, "ok"


async def stop_linkdooni_engine():
    db.set_linkdooni_enabled(False)
    linkdooni_engine["stop"] = True
    t = linkdooni_engine.get("task")
    if t and not t.done():
        try:
            await asyncio.wait_for(t, timeout=5)
        except Exception:
            t.cancel()
    for acc in _ld_fleet_accounts():
        await _ld_stop_sender(acc)
    await _ld_stop_secretary_fleet()
    await log(card("📨 LINKDOONI ENGINE — OFF", [f"🕒 {now()}"]))


async def linkdooni_summary_loop():
    """Every LINKDOONI_SUMMARY_INTERVAL, post the per-account stats card (sends +
    secretary replies + joined groups) with a stop button — only while the
    engine is enabled."""
    while True:
        await asyncio.sleep(config.LINKDOONI_SUMMARY_INTERVAL)
        try:
            cfg = db.get_linkdooni_config()
            if not cfg.get("enabled"):
                continue
            fleet_ids = db.list_linkdooni_account_ids()
            rows = []
            tot_sent = tot_rep = tot_join = 0
            for aid in fleet_ids:
                acc = db.get_account(aid)
                if not acc:
                    continue
                la = db.get_linkdooni_account(aid)
                sent = int(la.get("sent_total", 0) or 0)
                joined = int(la.get("joined_total", 0) or 0)
                try:
                    rep = int(db.get_secretary(aid).get("replied_total", 0) or 0)
                except Exception:
                    rep = 0
                tot_sent += sent
                tot_rep += rep
                tot_join += joined
                rows.append(f"📱 {acc['phone']} — ✉️ {sent} | 🤖 {rep} secretary | 👥 {joined}")
            rows.append(LINE)
            rows.append(f"📊 Totals — ✉️ {tot_sent} sent | 🤖 {tot_rep} secretary | 👥 {tot_join} groups")
            rows.append(f"🕒 {now()}")
            await bot.send_message(config.LOG_GROUP_ID,
                card("📨 LINKDOONI — Periodic Report", rows),
                buttons=[[Button.inline("⏹ Stop Linkdooni Engine", b"ld_stop")]])
        except Exception as e:  # noqa: BLE001
            print(f"[linkdooni_summary] {e}")


async def recover_linkdooni():
    """On boot, relaunch the linkdooni engine if it was enabled before restart."""
    try:
        if db.get_linkdooni_config().get("enabled"):
            await start_linkdooni_engine()
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ Linkdooni engine restore failed: {repr(e)[:120]}")


# --------------------------------------------------------------------------- #
# Linkdooni panel UI
# --------------------------------------------------------------------------- #
def _linkdooni_menu_text():
    cfg = db.get_linkdooni_config()
    return card("📨 Linkdooni Engine", [
        f"• Status : {'🟢 ON' if cfg.get('enabled') else '🔴 OFF'}",
        f"• Linkdooni channels : {len(db.list_linkdooni_channels())}",
        f"• Selected accounts : {len(db.list_linkdooni_account_ids())}",
        f"• Texts : {len(db.list_linkdooni_texts())}",
        f"• Daily group cap (total) : {cfg.get('daily_groups')}",
        f"• Send interval per account : {cfg.get('send_interval')}s",
        LINE,
        "The bot pulls group links from channels, spreads new groups daily across accounts,"
        " joins them, sends the texts, and keeps the secretary on at the same time.",
    ])


def _linkdooni_menu_buttons():
    cfg = db.get_linkdooni_config()
    rows = [
        [Button.inline("➕ Add Linkdooni Channel", b"ld_addch"),
         Button.inline("🗑 Clear Channels", b"ld_clrch")],
        [Button.inline("👥 Select Accounts", b"ld_accs")],
        [Button.inline("✍️ Add Text", b"ld_addtext"),
         Button.inline("🗑 Clear Texts", b"ld_clrtext")],
        [Button.inline("⏱ Send Interval", b"ld_interval"),
         Button.inline("🔢 Daily Cap", b"ld_daily")],
    ]
    if cfg.get("enabled"):
        rows.append([Button.inline("⏹ Stop Engine", b"ld_stop")])
    else:
        rows.append([Button.inline("▶️ Start Engine", b"ld_start")])
    rows.append([Button.inline("🔙 Back to Automation", b"automation")])
    return rows


@bot.on(events.CallbackQuery(data=b"linkdooni"))
async def linkdooni_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, _linkdooni_menu_text(), buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_addch"))
async def ld_addch_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_ld_channels"}
    await safe_edit(event,
        "📋 Send the Linkdooni channel links/IDs (one per line or space-separated).\n"
        "Example: `@mychannel` or `https://rubika.ir/mychannel` or the channel guid (c0...).",
        buttons=[[Button.inline("🔙 Back", b"linkdooni")]])


async def handle_ld_channels(event, st):
    state.pop(event.sender_id, None)
    raw = event.raw_text.strip()
    refs = [p for p in _re_u.split(r"[\s,]+", raw) if p.strip()]
    added = 0
    for r in refs:
        if db.add_linkdooni_channel(r):
            added += 1
    await event.respond(f"✅ Added {added} Linkdooni channels "
                        f"(total: {len(db.list_linkdooni_channels())}).",
                        buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_clrch"))
async def ld_clrch_cb(event):
    if not is_owner(event):
        return
    db.clear_linkdooni_channels()
    await safe_edit(event, _linkdooni_menu_text(), buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_accs"))
async def ld_accs_cb(event):
    if not is_owner(event):
        return
    sel = set(db.list_linkdooni_account_ids())
    rows = []
    for a in db.list_accounts():
        mark = "✅" if a["id"] in sel else "⬜️"
        tag = "" if a["status"] == "active" else " ⚠️"
        rows.append([Button.inline(f"{mark} {a['phone']}{tag}",
                                   f"ldacc_{a['id']}".encode())])
    rows.append([Button.inline("🔙 Back", b"linkdooni")])
    await safe_edit(event, "👥 Select the Linkdooni accounts:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"ldacc_(\\d+)"))
async def ld_acc_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    db.toggle_linkdooni_account(aid)
    await ld_accs_cb(event)


@bot.on(events.CallbackQuery(data=b"ld_addtext"))
async def ld_addtext_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_ld_text"}
    await safe_edit(event, "✍️ Send the text to post (one at a time; you can send several).",
                    buttons=[[Button.inline("🔙 Back", b"linkdooni")]])


async def handle_ld_text(event, st):
    state.pop(event.sender_id, None)
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("Text is empty.", buttons=_linkdooni_menu_buttons())
        return
    db.add_linkdooni_text(txt)
    await event.respond(f"✅ Text added (total: {len(db.list_linkdooni_texts())}).",
                        buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_clrtext"))
async def ld_clrtext_cb(event):
    if not is_owner(event):
        return
    db.clear_linkdooni_texts()
    await safe_edit(event, _linkdooni_menu_text(), buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_interval"))
async def ld_interval_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_ld_interval"}
    await safe_edit(event,
        f"⏱ Send the per-account send interval (seconds) "
        f"(between {config.LINKDOONI_MIN_INTERVAL} and {config.LINKDOONI_MAX_INTERVAL}):",
        buttons=[[Button.inline("🔙 Back", b"linkdooni")]])


async def handle_ld_interval(event, st):
    state.pop(event.sender_id, None)
    db.set_linkdooni_interval(event.raw_text.strip())
    await event.respond(
        f"✅ Send interval set to {db.get_linkdooni_config().get('send_interval')} seconds.",
        buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_daily"))
async def ld_daily_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_ld_daily"}
    await safe_edit(event, "🔢 Send the daily new-groups cap (total, not per-account):",
                    buttons=[[Button.inline("🔙 Back", b"linkdooni")]])


async def handle_ld_daily(event, st):
    state.pop(event.sender_id, None)
    db.set_linkdooni_daily_groups(event.raw_text.strip())
    await event.respond(
        f"✅ Daily cap set to {db.get_linkdooni_config().get('daily_groups')} groups.",
        buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_start"))
async def ld_start_cb(event):
    if not is_owner(event):
        return
    ok, msg = await start_linkdooni_engine()
    if not ok:
        await event.answer(msg, alert=True)
        return
    await safe_edit(event,
        "▶️ Linkdooni engine started. Discovery/join and sending are reported to the log group.\n"
        "A stats card with a stop button arrives every 20 minutes.",
        buttons=[[Button.inline("⏹ Stop Engine", b"ld_stop")],
                 [Button.inline("🔙 Back", b"linkdooni")]])


@bot.on(events.CallbackQuery(data=b"ld_stop"))
async def ld_stop_cb(event):
    if not is_owner(event):
        return
    await event.answer("Stopping the Linkdooni engine ...")
    await stop_linkdooni_engine()
    await safe_edit(event, "⏹ Linkdooni engine stopped.",
                    buttons=[[Button.inline("🔙 Back", b"linkdooni")]])


# --------------------------------------------------------------------------- #
# Settings panel (panel-editable runtime settings)
# --------------------------------------------------------------------------- #
def _settings_text():
    camp_status = "🟢 ON" if db.get_setting("campaign_enabled", "0") == "1" else "⚪️ OFF"
    return card("⚙️ Settings", [
        f"• Consecutive errors (then stop) : {db.get_max_errors()}",
        f"• Pause duration (seconds) : {db.get_resume_wait()}",
        f"• Send speed (seconds) : {db.get_delay()}",
        f"• Contact-add speed (seconds) : {db.get_contact_delay()}",
        f"• Brain send cap (per account) : {db.get_brain_cap()}",
        f"• Friend-discovery cap : {db.get_discovery_target()}",
        f"• Discovery attempts cap : {db.get_discovery_max_attempts()}",
        f"• Campaign : {camp_status}",
        LINE,
        "Tap whichever you want to change:",
    ])


def _settings_buttons():
    return [
        [Button.inline("🧯 Consecutive Errors", b"set_maxerr"),
         Button.inline("⏸ Pause Duration", b"set_resume")],
        [Button.inline("⏱ Send Speed", b"set_senddelay"),
         Button.inline("📇 Contact Speed", b"set_cspeed")],
        [Button.inline("🧠 Brain Cap", b"set_braincap")],
        [Button.inline("🔎 Friend-discovery Cap", b"set_disctarget")],
        [Button.inline("🔎 Discovery Attempts Cap", b"set_discattempts")],
        [Button.inline("📢 Campaign", b"campaign")],
        [Button.inline("🔙 Back", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"settings"))
async def settings_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, _settings_text(), buttons=_settings_buttons())


@bot.on(events.CallbackQuery(data=b"set_maxerr"))
async def set_maxerr_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_maxerr"}
    await safe_edit(event, "🧯 Send the number of consecutive errors before stopping (e.g. 5):",
                    buttons=[[Button.inline("🔙 Back", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_resume"))
async def set_resume_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_resume"}
    await safe_edit(event, "⏸ Send the pause duration after errors (seconds, e.g. 300):",
                    buttons=[[Button.inline("🔙 Back", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_senddelay"))
async def set_senddelay_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_senddelay"}
    await safe_edit(event,
        f"⏱ Send the send speed (between {config.MIN_DELAY} and {config.MAX_DELAY}):",
        buttons=[[Button.inline("🔙 Back", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_cspeed"))
async def set_cspeed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_contactspeed", "back": "settings"}
    await safe_edit(event,
        f"📇 Contact-add speed (between {config.CONTACT_MIN_DELAY} and "
        f"{config.CONTACT_MAX_DELAY}), send it:",
        buttons=[[Button.inline("🔙 Back", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_braincap"))
async def set_braincap_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_braincap"}
    await safe_edit(event,
        f"🧠 Send the Brain send cap per account (e.g. {config.BRAIN_SEND_CAP}):",
        buttons=[[Button.inline("🔙 Back", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_disctarget"))
async def set_disctarget_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_disctarget"}
    await safe_edit(event,
        f"🔎 Send the friend-discovery cap (any number, e.g. {config.DISCOVERY_TARGET} or 10 or 300):",
        buttons=[[Button.inline("🔙 Back", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_discattempts"))
async def set_discattempts_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_discattempts"}
    await safe_edit(event,
        f"🔎 Send the discovery attempts cap (how many numbers are probed to reach the target) "
        f"(e.g. {config.DISCOVERY_MAX_ATTEMPTS}).\n"
        "⚠️ Higher = better chance of reaching the target, but higher risk of a Rubika limit.",
        buttons=[[Button.inline("🔙 Back", b"settings")]])


async def handle_set_maxerr(event, st):
    state.pop(event.sender_id, None)
    db.set_max_errors(event.raw_text.strip())
    await event.respond(f"✅ Consecutive errors set to {db.get_max_errors()}.",
                        buttons=_settings_buttons())


async def handle_set_resume(event, st):
    state.pop(event.sender_id, None)
    db.set_resume_wait(event.raw_text.strip())
    await event.respond(f"✅ Pause duration set to {db.get_resume_wait()} seconds.",
                        buttons=_settings_buttons())


async def handle_set_senddelay(event, st):
    state.pop(event.sender_id, None)
    db.set_delay(config.clamp_delay(event.raw_text.strip()))
    await event.respond(f"✅ Send speed set to {db.get_delay()} seconds.",
                        buttons=_settings_buttons())


async def handle_set_contactspeed(event, st):
    back = (st or {}).get("back", "settings")
    state.pop(event.sender_id, None)
    db.set_contact_delay(event.raw_text.strip())
    await event.respond(f"✅ Contact-add speed set to {db.get_contact_delay()} seconds.",
                        buttons=[[Button.inline("🔙 Back",
                                                back.encode() if isinstance(back, str) else b"settings")]])


async def handle_set_braincap(event, st):
    state.pop(event.sender_id, None)
    db.set_brain_cap(event.raw_text.strip())
    await event.respond(f"✅ Brain send cap set to {db.get_brain_cap()} contacts per account.",
                        buttons=_settings_buttons())


async def handle_set_disctarget(event, st):
    state.pop(event.sender_id, None)
    db.set_discovery_target(event.raw_text.strip())
    await event.respond(f"✅ Friend-discovery cap set to {db.get_discovery_target()} numbers.",
                        buttons=_settings_buttons())


async def handle_set_discattempts(event, st):
    state.pop(event.sender_id, None)
    db.set_discovery_max_attempts(event.raw_text.strip())
    await event.respond(f"✅ Discovery attempts cap set to {db.get_discovery_max_attempts()}.",
                        buttons=_settings_buttons())


# --------------------------------------------------------------------------- #
# Campaign (کمپین): GLOBAL toggle in Settings. When ON, upon any account login:
#   1) Create a channel (same logic as channel_create_local / channel_create_remote)
#   2) Forward the marker text into it (same logic as base)
#   3) Send (marker forward) to contacts (same logic as run_send / run_send_remote)
# All steps separated by CAMPAIGN_STEP_DELAY (default 5 seconds).
# This section is ADDITIVE — it uses the exact same functions as the base.
# --------------------------------------------------------------------------- #

def _is_campaign_enabled() -> bool:
    return db.get_setting("campaign_enabled", "0") == "1"


def _campaign_channel_name() -> str:
    """The configured channel name for campaign, or empty (means auto-name)."""
    return (db.get_setting("campaign_channel_name", "") or "").strip()


def _campaign_menu_text() -> str:
    status = "🟢 ON" if _is_campaign_enabled() else "⚪️ OFF"
    name = _campaign_channel_name() or "(auto: campaign <number>)"
    return card("📢 Campaign", [
        f"• Status : {status}",
        f"• Channel name : {name}",
        LINE,
        "As soon as any account logs in (when enabled):",
        "1) a channel is created",
        "2) the marker message is forwarded",
        "3) it's sent to the contacts",
    ])


def _campaign_menu_buttons():
    toggle_label = "⏹ Turn Off Campaign" if _is_campaign_enabled() else "▶️ Turn On Campaign"
    return [
        [Button.inline(toggle_label, b"camp_toggle")],
        [Button.inline("🎛 Channel Name", b"camp_name")],
        [Button.inline("🔙 Back to Settings", b"settings")],
    ]


@bot.on(events.CallbackQuery(data=b"campaign"))
async def campaign_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, _campaign_menu_text(), buttons=_campaign_menu_buttons())


@bot.on(events.CallbackQuery(data=b"camp_toggle"))
async def campaign_toggle_cb(event):
    if not is_owner(event):
        return
    cur = db.get_setting("campaign_enabled", "0") == "1"
    new_state = not cur
    db.set_setting("campaign_enabled", "1" if new_state else "0")
    status = "ON 🟢" if new_state else "OFF ⚪️"
    await event.answer(f"Campaign : {status}")
    await log(card("📢 CAMPAIGN " + ("ON" if new_state else "OFF"), [
        f"• Status : {status}",
        f"🕒 {now()}"]))
    # refresh campaign menu
    await safe_edit(event, _campaign_menu_text(), buttons=_campaign_menu_buttons())


@bot.on(events.CallbackQuery(data=b"camp_name"))
async def campaign_name_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_campaign_channel_name"}
    cur = _campaign_channel_name() or "(empty — auto-generates for now)"
    await safe_edit(event,
        f"🎛 Send the campaign channel name (this same name is used for all accounts):\n"
        f"Current name: {cur}\n\n"
        "To return to auto mode, send the word `auto`.",
        buttons=[[Button.inline("🔙 Back", b"campaign")]])


async def handle_campaign_channel_name(event):
    state.pop(event.sender_id, None)
    name = (event.raw_text or "").strip()
    if name in ("خودکار", "auto", "AUTO", ""):
        db.set_setting("campaign_channel_name", "")
        msg = "✅ Campaign channel name reset to auto (campaign <number>)."
    else:
        db.set_setting("campaign_channel_name", name)
        msg = f"✅ Campaign channel name set to '{name}' (for all accounts)."
    await event.respond(msg, buttons=_campaign_menu_buttons())


async def _run_campaign(account_id: int):
    """Auto-run the campaign sequence for an account after login.
    Uses the EXACT same logic as the base channel_create_local/remote + run_send.

    Steps:
      1) Create channel + forward marker (same as channel_create_local/remote)
      2) Wait CAMPAIGN_STEP_DELAY
      3) Send marker to contacts (same as run_send / run_send_remote)
    """
    acc = db.get_account(account_id)
    if not acc:
        return
    phone = acc["phone"]

    if not _is_campaign_enabled():
        return

    marker = db.get_marker()
    delay_step = config.CAMPAIGN_STEP_DELAY
    # Use the configured campaign channel name for ALL accounts; fall back to
    # an auto name if the owner hasn't set one.
    channel_name = _campaign_channel_name() or f"campaign {phone}"

    await log(card("📢 CAMPAIGN — Start", [
        f"👤 Account : {phone}",
        f"✅ Campaign is running on this account",
        f"• Channel name : {channel_name}",
        f"• Marker : '{marker}'",
        f"• Step interval : {delay_step}s",
        f"🕒 {now()}"]))

    w = worker.worker_for_account(acc)

    # ===== STEP 1 & 2: Create channel + forward marker =====
    # (exact same logic as channel_create_local / channel_create_remote)
    channel_guid = None
    forwarded = False

    if w and not worker.is_local(w):
        # --- Remote: same logic as channel_create_remote ---
        try:
            await worker.check_worker(w)
        except Exception:
            pass
        w = db.get_worker(w["id"])
        if not (w and w["enabled"] and w["status"] == "ok"):
            await log(card("📢 CAMPAIGN — Unhealthy Worker", [
                f"👤 {phone}",
                f"worker status: {w['status'] if w else 'unknown'}",
                f"🕒 {now()}"]))
            return
        try:
            res = await worker.api_call(w, "POST", "/channel/create",
                                        {"phone": phone, "marker": marker,
                                         "title": channel_name}, timeout=120)
        except Exception as e:
            await log(card("📢 CAMPAIGN — Channel Creation Error (worker)", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            return
        if not res.get("ok") or not res.get("channel_guid"):
            await log(card("📢 CAMPAIGN — Channel Creation Failed (worker)", [
                f"👤 {phone}", f"💥 {res.get('error', '—')}", f"🕒 {now()}"]))
            return
        channel_guid = res["channel_guid"]
        forwarded = bool(res.get("forwarded"))
    else:
        # --- Local: same logic as channel_create_local ---
        await account_conn.close(phone)
        client = rb.open_client(phone)
        try:
            await rb.connect_ready(client)
            saved_guid, mid = await rb.find_marked_message(client, marker)
            channel_guid = await rb.create_channel(client, channel_name)
            if mid:
                try:
                    await rb.forward_message(client, saved_guid, channel_guid, mid)
                    forwarded = True
                except Exception:
                    forwarded = False
        except Exception as e:
            await log(card("📢 CAMPAIGN — Channel Creation Error", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            try:
                await client.disconnect()
            except Exception:
                pass
            return
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    await log(card("📢 CAMPAIGN — Channel Created ✅", [
        f"👤 {phone}",
        f"• Channel : {channel_name}",
        f"🆔 {channel_guid}",
        ("📎 Marker forwarded ✅" if forwarded else "⚠️ Marker not forwarded"),
        f"🕒 {now()}"]))

    # ===== Wait between channel creation and send =====
    await asyncio.sleep(delay_step)

    # ===== STEP 3: Send marker to contacts =====
    # (exact same logic as the normal prepare + run_send / run_send_remote)
    await log(card("📢 CAMPAIGN — Start Sending to Contacts", [
        f"👤 {phone}", f"• Marker : '{marker}'", f"🕒 {now()}"]))

    if w and not worker.is_local(w):
        # --- Remote: same as _multi_send_one remote path ---
        try:
            res = await worker.api_call(w, "POST", "/prepare",
                                        {"phone": phone, "marker": marker})
        except Exception as e:
            await log(card("📢 CAMPAIGN — Send Prep Error (worker)", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            return
        if not res.get("marker_found") or not res.get("total"):
            await log(card("📢 CAMPAIGN — No Marker/Recipient (worker)", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        await run_send_remote(config.OWNER_ID, {
            "account_id": account_id, "phone": phone, "remote": True,
            "worker_id": w["id"], "total": res["total"]})
    else:
        # --- Local: same as _multi_send_one local path ---
        try:
            prep = await _prepare_local(acc, marker)
        except account_conn.InvalidAuthError:
            db.set_status(account_id, "inactive")
            await log(card("📢 CAMPAIGN — Dead Account", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        except Exception as e:
            await log(card("📢 CAMPAIGN — Send Prep Error", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            return
        if not prep:
            await log(card("📢 CAMPAIGN — Marker Not Found", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        saved_guid, mid, recips = prep
        if not recips:
            await log(card("📢 CAMPAIGN — No Recipient", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        await run_send(config.OWNER_ID, {
            "account_id": account_id, "phone": phone,
            "saved_guid": saved_guid, "mid": mid,
            "recipients": recips, "tag": "📢CAMP",
            "suppress_resume_panel": True})

    await log(card("📢 CAMPAIGN — Done ✅", [
        f"👤 {phone}",
        "✅ Channel created + marker forwarded + sending to contacts started.",
        f"🕒 {now()}"]))


if __name__ == "__main__":
    asyncio.run(amain())
