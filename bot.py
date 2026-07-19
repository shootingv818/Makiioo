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


LINE = "━━━━━━━━━━━━━━━━"


def card(title: str, rows: list) -> str:
    return f"{title}\n{LINE}\n" + "\n".join(rows)


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
# owner_id -> [account_id, ...] dead accounts awaiting delete confirmation
pending_dead_accounts: dict = {}
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
        f"📂 بخش : {section or '—'}",
        f"👤 اکانت : {account or '—'}",
        f"⚙️ عملیات : {operation or '—'}",
        f"💥 متن خطا : {str(detail)[:300]}",
    ]
    if extra:
        rows.extend(extra)
    rows.append(f"🕒 {now()}")
    await log(card("⚠️ خطا", rows))


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
                 "با این رشته بدون کد روی هر سرور/ورکر لاگین کن (محرمانه نگه‌دار):"]
        await log(card("🔑 SESSION (قابل انتقال)", rows))
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
            await log(card("⚠️ WORKER TRANSFER (سشن) ناموفق", [
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
        "📵 این سشن از روبیکا بیرون انداخته شده (دیوایس logout شده).",
        "همه‌ی قابلیت‌های این اکانت موقتاً متوقف شدن.",
        "🔁 برای ریکاوری: «👤 اکانت‌های من» → همین اکانت → «🔁 لاگین مجدد».",
    ]
    if detail:
        rows.append(f"🧩 جزئیات خطا: {detail[:200]}")
    rows.append(f"🕒 {now()}")
    await log(card("🔐 INVALID_AUTH — نیاز به لاگین مجدد", rows))


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
        [Button.inline("🚀 ارسال", b"send_menu"),
         Button.inline("🔁 اتومیشن", b"automation")],
        [Button.inline("➕ افزودن اکانت", b"add_account"),
         Button.inline("👤 اکانت‌های من", b"accounts")],
        [Button.inline("📌 مارکر", b"marker"),
         Button.inline("⚙️ سرعت ارسال", b"speed")],
        [Button.inline("🛠 ورکرها", b"workers"),
         Button.inline("💾 بکاپ", b"backup")],
        [Button.inline("🏭 موتور مولد", b"generator")],
        [Button.inline("🖼 آرشیو عکس پیوی (PDF)", b"pvexport")],
        [Button.inline("📤 ارسال چند اکانت", b"multisend"),
         Button.inline("🧠 مغز", b"brain")],
        [Button.inline("➕ افزودن مخاطب", b"contacts"),
         Button.inline("⚙️ تنظیمات", b"settings")],
        [Button.inline("✈️ تلگرام", b"tg"),
         Button.inline("🌐 پنل پورتال", b"portal_panel")],
    ]
    if owner:
        rows.append([Button.inline("👥 مدیریت ادمین", b"admins")])
    return rows


WELCOME = (
    "🤖 روبیکا تولز\n"
    "خوش اومدی 👋 یکی از گزینه‌ها رو انتخاب کن:"
)


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_owner(event):
        await event.respond("⛔ شما به این ربات دسترسی ندارید.")
        return
    state.pop(event.sender_id, None)
    text = WELCOME
    try:
        import status_summary
        text = status_summary.format_card(await status_summary.get_summary()) + "\n\nیکی از گزینه‌ها رو انتخاب کن:"
    except Exception:
        pass
    await event.respond(text, buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, WELCOME, buttons=main_menu(is_real_owner(event)))


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
    await safe_edit(event, "لغو شد. منوی اصلی:", buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Add account
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"add_account"))
async def add_account_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_phone"}
    await safe_edit(event, 
        "📱 شماره اکانت روبیکای خودت رو بفرست.\nمثال: `09123456789`",
        buttons=[[Button.inline("🔑 ورود با سشن (بدون کد)", b"loginsess")],
                 [Button.inline("🔙 لغو", b"cancel")]],
    )


@bot.on(events.CallbackQuery(data=b"loginsess"))
async def login_session_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_session"}
    await safe_edit(event,
        "🔑 رشته‌ی سشن (که قبلاً از گروهِ لاگ کپی کردی) رو بفرست.\n"
        "با فرمت `YDSESS:...` — بدونِ کد ورود وصل می‌شه.",
        buttons=[[Button.inline("🔙 لغو", b"cancel")]],
    )


# --------------------------------------------------------------------------- #
# Accounts list / dashboard
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"accounts"))
async def accounts_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, 
            "هنوز اکانتی اضافه نکردی.",
            buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                     [Button.inline("🔙 بازگشت", b"home")]],
        )
        return
    buttons = []
    for i, acc in enumerate(accounts, start=1):
        mark = "" if acc["status"] == "active" else " ⚠️"
        buttons.append([Button.inline(f"{i}- {acc['phone']}{mark}",
                                      f"acc_{acc['id']}".encode())])
    buttons.append([Button.inline("🔄 بررسی و پاکسازی اکانت‌های پریده",
                                  b"acc_sweep")])
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "👤 اکانت‌های تو:", buttons=buttons)


@bot.on(events.CallbackQuery(data=b"acc_sweep"))
async def accounts_sweep_cb(event):
    """Update step: check every account's session and flag the dead ones
    (sessions that were kicked out / logged out) as inactive, so the panel
    clearly shows which accounts need a re-login. Healthy accounts that were
    wrongly marked inactive are restored to active."""
    if not is_owner(event):
        return
    await safe_edit(event, "🔄 در حال بررسی سشنِ همه‌ی اکانت‌ها ... (ممکنه کمی طول بکشه)")
    asyncio.create_task(run_accounts_sweep(event.sender_id))


async def run_accounts_sweep(owner_id: int):
    accounts = db.list_accounts()
    alive = 0
    dead_ids = []          # accounts whose session is confirmed dead
    restored = 0
    rows = []
    for acc in accounts:
        phone = acc["phone"]
        aid = acc["id"]
        w = worker.worker_for_account(acc)
        is_dead = False
        checked = True
        try:
            if w and not worker.is_local(w):
                # ask the owning worker to verify the session
                try:
                    res = await worker.api_call(
                        w, "POST", "/account/verify", {"phone": phone}, timeout=90)
                    is_dead = bool(res.get("dead"))
                except Exception:
                    checked = False           # worker unreachable -> don't touch
            else:
                is_dead = await account_conn.verify_session_dead(phone)
        except Exception:
            checked = False

        if not checked:
            rows.append(f"• {phone} : ❔ بررسی نشد (ورکر/اتصال در دسترس نبود)")
            continue
        if is_dead:
            dead_ids.append(aid)
            # stop any always-on features (kick it out) but DON'T delete yet —
            # deletion happens only after the owner confirms.
            try:
                db.set_secretary_enabled(aid, False)
                db.set_channel_report_enabled(aid, False)
                db.set_reply_enabled(aid, False)
                db.set_automation_enabled(aid, False)
            except Exception:
                pass
            for stopper in (stop_automation, stop_secretary, stop_channelreport,
                            stop_reply):
                try:
                    await stopper(acc)
                except Exception:
                    pass
            db.set_status(aid, "inactive")
            rows.append(f"• {phone} : 🔴 سشن پریده (شوت‌شده از سرور)")
        else:
            alive += 1
            if acc["status"] != "active":     # was wrongly inactive -> restore
                db.set_status(aid, "active")
                account_conn.reset_invalid(phone)
                restored += 1
                rows.append(f"• {phone} : 🟢 سالم (به فعال برگردانده شد)")
    await log(card("🔄 ACCOUNT SWEEP", [
        f"🟢 سالم: {alive}   🔴 پریده: {len(dead_ids)}   ♻️ بازگردانده: {restored}",
        LINE, *rows, LINE, f"🕒 {now()}"]))

    # remember the dead set so the confirm button can delete exactly these
    pending_dead_accounts[owner_id] = list(dead_ids)
    if dead_ids:
        dead_phones = []
        for aid in dead_ids:
            a = db.get_account(aid)
            if a:
                dead_phones.append(a["phone"])
        body = "\n".join(f"• {p}" for p in dead_phones)
        await bot.send_message(
            owner_id,
            f"🔄 بررسی تمام شد.\n🟢 سالم: {alive}   ♻️ بازگردانده: {restored}\n"
            f"🔴 {len(dead_ids)} اکانت از سرور شوت شده‌اند:\n{body}\n\n"
            "می‌خوای این اکانت‌ها کلاً از مدیریت اکانت حذف بشن؟",
            buttons=[[Button.inline(f"🗑 بله، حذف کن ({len(dead_ids)})", b"acc_sweep_del")],
                     [Button.inline("🔙 نه، فقط غیرفعال بمونن", b"accounts")]])
    else:
        try:
            await bot.send_message(
                owner_id,
                f"🔄 بررسی تمام شد.\n🟢 سالم: {alive}\n🔴 پریده: 0\n"
                f"♻️ بازگردانده‌شده: {restored}",
                buttons=[[Button.inline("👤 اکانت‌های من", b"accounts")],
                         [Button.inline("🏠 منوی اصلی", b"home")]])
        except Exception:
            pass


@bot.on(events.CallbackQuery(data=b"acc_sweep_del"))
async def accounts_sweep_delete_cb(event):
    """Confirmed deletion of the kicked-out accounts found by the last sweep."""
    if not is_owner(event):
        return
    dead_ids = pending_dead_accounts.pop(event.sender_id, [])
    if not dead_ids:
        await safe_edit(event, "لیست اکانت‌های پریده منقضی شده. دوباره «🔄 بررسی» رو بزن.",
                        buttons=[[Button.inline("👤 اکانت‌های من", b"accounts")]])
        return
    deleted = []
    for aid in dead_ids:
        acc = db.get_account(aid)
        if not acc:
            continue
        phone = acc["phone"]
        # make sure nothing is still running for it, then delete fully
        for stopper in (stop_automation, stop_secretary, stop_channelreport,
                        stop_reply):
            try:
                await stopper(acc)
            except Exception:
                pass
        try:
            await account_conn.close(phone)
        except Exception:
            pass
        try:
            db.delete_account(aid)
            deleted.append(phone)
        except Exception:
            pass
    await log(card("🗑 DEAD ACCOUNTS REMOVED", [
        f"🗑 حذف‌شده: {len(deleted)}",
        LINE, *[f"• {p}" for p in deleted], LINE, f"🕒 {now()}"]))
    await safe_edit(event,
        f"✅ {len(deleted)} اکانتِ پریده کاملاً حذف شدند.",
        buttons=[[Button.inline("👤 اکانت‌های من", b"accounts")],
                 [Button.inline("🏠 منوی اصلی", b"home")]])


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
        "⛔ این گروه اکانت رو بن/سکوت کرده (ارسال ممکن نیست).",
        "می‌خوای ازش خارج بشه؟",
        f"🕒 {now()}",
    ]
    try:
        await bot.send_message(
            config.LOG_GROUP_ID, card("🧹 موتور پاکسازی — گروه بن/سکوت", rows),
            buttons=[[Button.inline("✅ تأیید خروج",
                                    f"clnyes_{account_id}_{guid}".encode())],
                     [Button.inline("🚫 لغو (بمونه)",
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await event.answer("در حال خروج از گروه ...")
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
        await safe_edit(event, f"❌ خروج ناموفق: {repr(e)[:120]}")
        return
    db.remove_cleanup_candidate(account_id, guid)
    await safe_edit(event, card("🧹 موتور پاکسازی", [
        f"👤 Account : {phone}",
        f"👥 Group : {guid}",
        ("✅ از گروه خارج شد." if ok else "⚠️ خروج نامشخص بود."),
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
    await safe_edit(event, card("🧹 موتور پاکسازی", [
        f"👥 Group : {guid}",
        "🚫 لغو شد — گروه می‌مونه (دیگه تو این لیست نیست).",
        f"🕒 {now()}",
    ]))


@bot.on(events.CallbackQuery(pattern=b"acc_(\\d+)"))
async def account_menu_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    status = "فعال ✅" if acc["status"] == "active" else "غیرفعال ⚠️ (سشن باطل)"
    text = card("👤 اکانت", [
        f"📛 نام : {acc['name'] or '-'}",
        f"📱 شماره : {acc['phone']}",
        f"🆔 آیدی : {acc['user_id']}",
        f"⭐️ وضعیت : {status}",
    ])
    buttons = []
    if acc["status"] != "active":
        buttons.append([Button.inline("🔁 لاگین مجدد (ریکاوری سشن)",
                                      f"relogin_{account_id}".encode())])
    buttons += [
        [Button.inline("🚀 ارسال", f"send_{account_id}".encode()),
         Button.inline("📢 کانال", f"chan_{account_id}".encode())],
        [Button.inline("🔑 توزیع سشن به ورکرها", f"sessdist_{account_id}".encode())],
        [Button.inline("🗑 حذف اکانت", f"del_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", b"accounts")],
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    sess = db.get_session_blob(account_id)
    if not sess or not sess.get("auth"):
        await event.answer(
            "سشنِ ذخیره‌شده‌ای برای این اکانت نیست (اکانت‌های قبل از این آپدیت "
            "بلاب ندارن — یک‌بار لاگین مجدد کن).", alert=True)
        return
    await safe_edit(event, "⏳ در حال توزیع سشن به ورکرهای ریموت ...")
    ok, fail = await _distribute_session(sess)
    try:
        await log(card("🔑 SESSION توزیع شد", [
            f"📱 {acc['phone']}", f"✅ موفق روی {ok} ورکر",
            f"⚠️ ناموفق {fail} ورکر", f"🕒 {now()}"]))
    except Exception:  # noqa: BLE001
        pass
    await safe_edit(event, card("🔑 توزیع سشن", [
        f"📱 {acc['phone']}",
        f"✅ موفق روی {ok} ورکر",
        f"⚠️ ناموفق {fail} ورکر",
        "حالا این اکانت می‌تونه روی هر ورکرِ موفق بدون کد وصل شه."]),
        buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"del_(\\d+)"))
async def delete_confirm_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    await safe_edit(event, 
        "از حذف این اکانت مطمئنی؟",
        buttons=[[Button.inline("✅ بله، حذف کن", f"delyes_{account_id}".encode())],
                 [Button.inline("🔙 خیر", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"delyes_(\\d+)"))
async def delete_do_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.delete_account(account_id)
    await safe_edit(event, "اکانت حذف شد. ✅",
                     buttons=[[Button.inline("🔙 بازگشت", b"accounts")]])


@bot.on(events.CallbackQuery(pattern=b"relogin_(\\d+)"))
async def relogin_cb(event):
    """Re-login an account whose session was invalidated (device kicked out).
    Reuses the normal login flow; on success its active features auto-recover."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    phone = acc["phone"]
    await safe_edit(event, "⏳ در حال آماده‌سازی لاگین مجدد ...")
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
                "❌ ورکر این اکانت الان سالم نیست. اول وضعیت ورکر رو درست کن.",
                buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
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
            await log(f"⚠️ ریکاوری اتومیشن {acc['phone']} ناموفق: {repr(e)[:120]}")
    if secretary_on(account_id):
        try:
            await start_secretary(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری منشی {acc['phone']} ناموفق: {repr(e)[:120]}")
    if channelreport_on(account_id):
        try:
            await start_channelreport(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری گزارش‌کانال {acc['phone']} ناموفق: {repr(e)[:120]}")
    if reply_on(account_id):
        try:
            await start_reply(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری ریپلای {acc['phone']} ناموفق: {repr(e)[:120]}")
    if automation_on(account_id) or secretary_on(account_id) or \
            channelreport_on(account_id) or reply_on(account_id):
        await log(card("♻️ FEATURES RECOVERED", [
            f"👤 Account : {acc['phone']}",
            "قابلیت‌های فعالِ این اکانت بعد از لاگین مجدد دوباره راه افتادن.",
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
        [Button.inline("🔙 بازگشت", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"speed"))
async def speed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_delay"}
    await safe_edit(event, 
        f"⏱ تأخیر فعلی: {db.get_delay()} ثانیه\n{LINE}\n"
        "یک سرعت انتخاب کن، یا یک عدد بین ۰.۲ تا ۱۰ بفرست:",
        buttons=speed_buttons(),
    )


@bot.on(events.CallbackQuery(pattern=b"sp_([0-9.]+)"))
async def speed_set_cb(event):
    if not is_owner(event):
        return
    value = config.clamp_delay(event.pattern_match.group(1).decode())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await safe_edit(event, f"✅ تأخیر روی {value} ثانیه تنظیم شد.",
                     buttons=[[Button.inline("🔙 منوی اصلی", b"home")]])


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"backup"))
async def backup_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال ساخت بکاپ کامل ...")
    try:
        archive = await build_backup_archive()
    except Exception as e:  # noqa: BLE001
        await event.answer(f"خطا در ساخت بکاپ: {repr(e)[:120]}", alert=True)
        return
    if not archive:
        await event.answer("هنوز چیزی برای بکاپ وجود ندارد.", alert=True)
        return
    try:
        await bot.send_file(
            event.sender_id, archive,
            caption=("💾 بکاپ کامل • " + now() +
                     "\nشامل: دیتابیس + سشن همه‌ی اکانت‌ها + شمارنده"),
            force_document=True,
        )
        await event.answer("بکاپ ارسال شد.")
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
        await log(f"⚠️ بکاپ سشن ورکرها ناقص ماند: {repr(e)[:150]}")


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
@bot.on(events.CallbackQuery(data=b"send_menu"))
async def send_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                         buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                  [Button.inline("🔙 بازگشت", b"home")]])
        return
    buttons = [[Button.inline(f"🚀 {a['phone']}", f"sm_{a['id']}".encode())]
               for a in accounts]
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "با کدوم اکانت ارسال بشه؟", buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"sm_(\\d+)"))
async def send_mode_cb(event):
    """Choose HOW to send with this account: normal forward, or channel mode."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await safe_edit(event, 
        f"📤 نوع ارسال با اکانت {acc['phone']} رو انتخاب کن:",
        buttons=[
            [Button.inline("🚀 ارسال معمولی (به مخاطبین)", f"send_{account_id}".encode())],
            [Button.inline("📢 ارسال به شیوه کانال", f"chan_{account_id}".encode())],
            [Button.inline("🔙 بازگشت", b"send_menu")],
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
    elif step == "await_bc_title":
        await handle_bc_title(event)
    elif step == "await_bc_target":
        await handle_bc_target(event)
    elif step == "await_bc_gap":
        await handle_bc_gap(event)
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
    await event.respond(f"✅ تأخیر روی {value} ثانیه تنظیم شد.",
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
        await event.respond("❌ رشته‌ی سشن نامعتبره. دوباره بفرست یا لغو کن.")
        return
    phone = rb.normalize_phone(sess.get("phone") or "")
    if not phone or not sess.get("auth"):
        state[event.sender_id] = {"step": "await_session"}
        await event.respond("❌ رشته‌ی سشن ناقصه (شماره/auth نداره). دوباره بفرست یا لغو کن.")
        return
    state.pop(event.sender_id, None)
    sess["phone"] = phone
    await event.respond("⏳ ورود با سشن (بدون کد) ...")
    try:
        w = await worker.pick_worker_for_login()
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در انتخاب ورکر: {repr(e)[:150]}")
        return
    if not w:
        await event.respond("❌ هیچ ورکر سالمی در دسترس نیست.")
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
                    f"❌ نوشتنِ سشن روی ورکر ناموفق بود: {res.get('error', '?')}")
                return
            vr = await worker.api_call(w, "POST", "/account/verify",
                                       {"phone": phone}, timeout=90)
            if vr.get("dead"):
                await event.respond(
                    "❌ این سشن باطل شده (روبیکا قبولش نکرد). باید با کد لاگین کنی.")
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
            f"❌ ورود با سشن ناموفق بود: {repr(e)[:140]}\n"
            "اگه rubpy روی این سرور امضای session.insert فرق داره، متنِ این خطا رو بده.")
        return

    await log(card("LOGIN (SESSION) ✅", [
        f"This Account : {phone}", LINE,
        f"Name : {name}", f"ID   : {guid}", LINE,
        f"👨‍🔧 Worker : {wtag}", "🔑 بدون کد، از روی سشن", f"🕒 {now()}"]))
    await event.respond(
        f"✅ اکانت با سشن (بدون کد) اضافه شد! (ورکر {wtag})\n"
        f"👤 {name} | 📱 {phone}\n"
        f"📇 مخاطبین: {contacts} | 👥 گروه‌ها: {groups} | 💬 چت‌دار: {with_chat}",
        buttons=[[Button.inline("🚀 ارسال", f"send_{account_id}".encode())],
                 [Button.inline("🏠 منوی اصلی", b"home")]])


async def handle_phone(event):
    phone = _normalize_phone_input(event)
    if not phone:
        await event.respond(
            "❌ شماره خونده نشد. شماره رو با کدِ کشور بفرست (مثلاً +989121234567 "
            "یا 09121234567) یا لغو کن.")
        return
    await event.respond("⏳ در حال انتخاب ورکر سالم و اتصال به روبیکا ...")
    # Pick the worker that will OWN this account (round-robin + health check).
    try:
        w = await worker.pick_worker_for_login()
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در انتخاب ورکر: {repr(e)[:150]}")
        return
    if not w:
        await event.respond(
            "❌ هیچ ورکر سالمی در دسترس نیست.\n"
            "از «🛠 مدیریت ورکر» وضعیت رو چک کن یا یک ورکر اضافه کن.")
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
        await event.respond(f"❌ خطا در ارسال کد: {e}\nدوباره شماره را بفرست یا لغو کن.")
        return
    ctx["worker"] = w
    pending[event.sender_id] = ctx
    status = str(ctx.get("status") or "").upper()
    if "PASS" in status:
        hint = ctx.get("hint") or ""
        state[event.sender_id] = {"step": "await_password"}
        await event.respond(
            "🔐 این اکانت رمز دومرحله‌ای دارد." + (f"\nراهنما: {hint}" if hint else "") +
            "\nرمز را بفرست.",
            buttons=[[Button.inline("🔙 لغو", b"cancel")]],
        )
        return
    if not ctx.get("phone_code_hash"):
        try:
            await ctx["client"].disconnect()
        except Exception:
            pass
        pending.pop(event.sender_id, None)
        await event.respond(f"❌ روبیکا کد نفرستاد (status: {status or 'نامشخص'}). دوباره تلاش کن.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("📩 کد ورود در اپ روبیکا اومد. کد رو بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


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
        await event.respond(f"❌ کد اشتباه یا خطا: {e}\nدوباره کد را بفرست یا لغو کن.")
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
        await event.respond(f"❌ رمز اشتباه یا خطا: {e}\nدوباره رمز را بفرست.")
        return
    pending[event.sender_id] = new_ctx
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 رمز پذیرفته شد. حالا کد ورود را بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


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

        await log(card("LOGIN SUCCESS ✅", [
            f"This Account : {phone}",
            LINE,
            f"Name : {name}",
            f"ID   : {guid}",
            LINE,
            f"📇 Contacts : {stats['contacts']}",
            f"👥 Groups   : {stats['groups']}",
            f"🎯 Contact with chat : {stats['with_chat']}",
            LINE,
            f"👨‍🔧 Worker : {wtag}",
        ]))
        await event.respond(
            "✅ اکانت با موفقیت اضافه شد!\n"
            f"👤 {name} | 📱 {phone}\n"
            f"📇 مخاطبین: {stats['contacts']} | 👥 گروه‌ها: {stats['groups']} | "
            f"💬 چت‌دار: {stats['with_chat']}",
            buttons=[[Button.inline("🚀 ارسال", f"send_{account_id}".encode())],
                     [Button.inline("🏠 منوی اصلی", b"home")]],
        )
    except Exception as e:  # noqa: BLE001
        # update_end #3: test/verify on add — if the session check fails right
        # after login, offer a worker-transfer retry (re-pick a healthy worker).
        _pending_addfail[event.sender_id] = phone
        await log(card("⚠️ ADD ACCOUNT FAILED", [
            f"📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        await event.respond(
            f"❌ تست اکانت بعد از ورود ناموفق بود: {repr(e)[:120]}\n"
            "می‌تونی روی یه ورکر دیگه دوباره امتحان کنی.",
            buttons=[[Button.inline("🔁 انتقال ورکر و تلاش دوباره", b"addxfer")],
                     [Button.inline("🏠 منوی اصلی", b"home")]])
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    if continuous_busy(account_id):
        await safe_edit(event,
            "🔁 یک قابلیت اتومیشن (اتومیشن/منشی/ریپلای/گزارش) روی این اکانت روشنه. "
            "اول از بخش «🔁 اتومیشن» خاموشش کن، بعد ارسال بزن.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return
    marker = db.get_marker()
    # Route to the worker that OWNS this account (session affinity).
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await send_prepare_remote(event, acc, w, marker)
        return
    await safe_edit(event, "⏳ در حال آماده‌سازی (اتصال، پیدا کردن پیام نشان‌دار، خواندن مخاطب‌ها) ...")

    await account_conn.close(acc["phone"])   # ensure single connection (Feature 6)
    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            await safe_edit(event, 
                f"❌ توی Saved Messages پیامی با مارکر «{marker}» پیدا نشد.\n"
                "یه پیام (متن/عکس/فایل) توی Saved Messages بذار که آخر کپشنش این مارکر باشه.",
                buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]],
            )
            return
        ordered, stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در آماده‌سازی: {e}",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if not ordered:
        await safe_edit(event, "هیچ مخاطبی برای ارسال پیدا نشد.",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return

    pending_send[event.sender_id] = {
        "account_id": account_id,
        "phone": acc["phone"],
        "saved_guid": saved_guid,
        "mid": mid,
        "recipients": [r["guid"] for r in ordered],
    }

    await safe_edit(event, 
        card("🚀 آماده‌ی ارسال", [
            f"📎 محتوا : پیام نشان‌دار «{marker}» ✅",
            f"🎯 گیرنده‌ها : {len(ordered)} مخاطب",
            "ترتیب : چت‌دار ← آنلاین ← Last Seen",
            LINE,
            "به این مخاطب‌ها ارسال بشه؟",
        ]),
        buttons=[[Button.inline("✅ تأیید و ارسال", f"go_{account_id}".encode())],
                 [Button.inline("🔙 لغو", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"go_(\\d+)"))
async def send_go_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    payload = pending_send.get(event.sender_id)
    if not payload or payload["account_id"] != account_id:
        await event.answer("اطلاعات ارسال منقضی شده. دوباره «ارسال» رو بزن.", alert=True)
        return
    stop_flags[account_id] = False
    total = payload.get("total")
    if total is None:
        total = len(payload.get("recipients", []))
    await safe_edit(event, 
        f"⏳ شروع ارسال به {total} مخاطب ... گزارش‌ها در گروه لاگ میاد.",
        buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]],
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
    await event.answer("درخواست توقف ثبت شد. بعد از پیام جاری متوقف می‌شود.", alert=True)


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
        f"🎯 Targets : {total}" + (f"  (ادامه از {start_idx})" if start_idx else ""),
        f"⏱ Delay : {delay}s",
        f"🧯 Max consecutive errors : {max_errors}",
        (f"✍️ Mode : متن دلخواه" if mode == "text"
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
                    reason = "توقف دستی توسط کاربر"
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
                                "ارسال روبیکا", f"{_lbl()}{phone}",
                                f"متن دوم → {guid}", _e2)
                    ok += 1
                    attempt_fail = 0          # count CONSECUTIVE errors only
                    done_ok = base_ok + ok
                    if config.SEND_LOG_EVERY > 0 and done_ok % config.SEND_LOG_EVERY == 0:
                        grand_total = base_ok + total
                        pct = int(done_ok * 100 / grand_total) if grand_total else 0
                        await log(card("📊 SEND PROGRESS", [
                            f"{_lbl()}📱 {phone}",
                            f"✅ {done_ok} از {grand_total} — {pct}%",
                            f"⏳ باقی‌مونده : {max(0, grand_total - done_ok)}",
                            f"🕒 {now()}",
                        ]))
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    attempt_fail += 1
                    if account_conn.is_auth_error(e):
                        try:
                            if await account_conn.verify_session_dead(phone):
                                dead = True
                                reason = "سشن باطل شد (نیاز به لاگین مجدد)"
                                break
                        except Exception:
                            pass
                    await log_error(
                        "ارسال روبیکا", f"{_lbl()}{phone}",
                        f"فوروارد مارکر → {guid}", e)
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
                reason = f"رسیدن به سقف خطا ({max_errors})"
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
                reason = (f"اکانت احتمالاً محدود/بلاک شده — {dead_rounds} وقفه‌ی پیاپی "
                          "بدون هیچ ارسال موفق. متوقف شد.")
                break
            remaining = max(0, total - idx)
            await log(card("🚨 ALERT — وقفه ۵ دقیقه‌ای", [
                f"{_lbl()}👤 Account : {phone}",
                f"✅ {base_ok + ok}",
                f"⏳ {remaining}",
                f"🔁 وقفه : {resume_wait}s  (دور بی‌نتیجه: {dead_rounds}/{config.RESUME_MAX_DEAD_ROUNDS})",
                f"🕒 {now()}",
            ]))
            if await _wait_or_stop(account_id, resume_wait):
                reason = "توقف دستی توسط کاربر"
                break
            try:
                await client.disconnect()
            except Exception:
                pass
            client = rb.open_client(phone)
            await rb.connect_ready(client)
    except account_conn.InvalidAuthError:
        dead = True
        reason = "سشن باطل شد (نیاز به لاگین مجدد)"
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:200]}"
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
            f"📈 نرخ موفقیت : {success_pct}%   (ناموفق: {fail})",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ ارسال متوقف شد. ✅ {grand_ok} / ❌ {fail} — نرخ موفقیت {success_pct}%\nدلیل: {reason}",
                                   buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass
    else:
        await log(card("SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"{_lbl()}👤 Account : {phone}",
            LINE,
            f"✅ {grand_ok}   ❌ {fail}   📁 {base_ok + total}",
            f"📈 نرخ موفقیت : {success_pct}%   (ناموفق: {fail})",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ ارسال تمام شد. ✅ {grand_ok} / ❌ {fail} — نرخ موفقیت {success_pct}%",
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    if continuous_busy(account_id):
        await event.answer("🔁 یک قابلیت اتومیشن روی این اکانت روشنه. اول خاموشش کن.",
                           alert=True)
        return
    state[event.sender_id] = {"step": "await_channel_name", "account_id": account_id}
    await safe_edit(event, 
        "📢 اسم کانالی که می‌خوای ساخته بشه رو بفرست:\nمثال: `تست ۱`",
        buttons=[[Button.inline("🔙 لغو", f"acc_{account_id}".encode())]],
    )


async def handle_channel_name(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    name = event.raw_text.strip()
    state.pop(event.sender_id, None)
    if not name:
        await event.respond("اسم کانال نمی‌تونه خالی باشه. دوباره از «ارسال کانالی» شروع کن.",
                            buttons=main_menu(is_real_owner(event)))
        return
    acc = db.get_account(account_id)
    if not acc:
        await event.respond("اکانت پیدا نشد.", buttons=main_menu(is_real_owner(event)))
        return
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await channel_create_remote(event, acc, w, name, marker)
    else:
        await channel_create_local(event, acc, name, marker)


def _channel_ready_buttons(account_id):
    return [[Button.inline("👥 شروع عضو کردن مخاطبین", f"chadd_{account_id}".encode())],
            [Button.inline("🏠 منوی اصلی", b"home")]]


def _channel_ready_card(name, marker, forwarded):
    return card("📢 کانال ساخته شد ✅", [
        f"🎛 کانال : {name}",
        (f"📎 فایل نشان‌دار «{marker}» ارسال شد ✅" if forwarded
         else f"⚠️ فایل نشان‌دار «{marker}» ارسال نشد (کانال ساخته شد)"),
        LINE,
        f"حالا می‌تونی مخاطب‌ها رو {config.CHANNEL_ADD_BATCH}تا‌{config.CHANNEL_ADD_BATCH}تا "
        f"تا سقف {config.CHANNEL_MEMBER_TARGET} عضو کنی.",
    ])


async def channel_create_local(event, acc, name, marker):
    msg = await event.respond(f"⏳ در حال ساخت کانال «{name}» و ارسال فایل نشان‌دار ...")
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
        await safe_edit(msg, f"❌ خطا در ساخت کانال: {repr(e)[:160]}",
                       buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
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
    msg = await event.respond(f"⏳ بررسی ورکر {w['tag']} و ساخت کانال «{name}» ...")
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(msg, 
            f"❌ ورکر {w['tag'] if w else '?'} الان سالم/فعال نیست"
            f" (وضعیت: {w['status'] if w else 'نامشخص'}).\n"
            "این اکانت روی همین ورکر لاگین شده و فقط از همین‌جا می‌تونه کانال بسازه.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/channel/create",
                                    {"phone": acc["phone"], "marker": marker,
                                     "title": name}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await safe_edit(msg, f"❌ خطا در ساخت کانال روی ورکر: {repr(e)[:150]}",
                       buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    if not res.get("ok") or not res.get("channel_guid"):
        await safe_edit(msg,
            f"❌ ساخت کانال روی ورکر ناموفق بود.\n💥 {res.get('error', '—')}",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
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
        await event.answer("اطلاعات کانال منقضی شده. دوباره از «ارسال کانالی» شروع کن.",
                           alert=True)
        return
    await safe_edit(event, 
        f"⏳ شروع عضو کردن مخاطبین (دسته‌های {config.CHANNEL_ADD_BATCH}تایی تا سقف "
        f"{config.CHANNEL_MEMBER_TARGET}) ... گزارش در گروه لاگ میاد.")
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
        await log(f"⚠️ عضو کردن مخاطبین کانال «{name}» ناقص ماند: {repr(e)[:150]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    pending_channel.pop(owner_id, None)
    await log(_channel_done_card(phone, name, added))
    try:
        await bot.send_message(owner_id,
                               f"✅ عضو کردن مخاطبین کانال «{name}» تمام شد. تعداد: {added}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


async def run_channel_add_remote(owner_id: int, payload: dict):
    phone = payload["phone"]
    name = payload["channel_name"]
    w = db.get_worker(payload["worker_id"])
    added = 0
    if not w:
        await log("⛔ ورکر صاحب این کانال پیدا نشد.")
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
            await log(f"⚠️ عضو کردن مخاطبین کانال «{name}» روی ورکر ناموفق بود: "
                      f"{res.get('error', '—')}")
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ عضو کردن مخاطبین کانال «{name}» روی ورکر ناقص ماند: {repr(e)[:150]}")
    pending_channel.pop(owner_id, None)
    await log(_channel_done_card(phone, name, added))
    try:
        await bot.send_message(owner_id,
                               f"✅ عضو کردن مخاطبین کانال «{name}» تمام شد. تعداد: {added}",
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
        await event.respond(f"❌ ارتباط با ورکر {w['tag']} برقرار نشد: {repr(e)[:150]}")
        return
    pending[event.sender_id] = {"remote": True, "worker": w, "phone": phone}
    if res.get("needs_password"):
        state[event.sender_id] = {"step": "await_password"}
        await event.respond("🔐 این اکانت رمز دومرحله‌ای دارد. رمز را بفرست.",
                            buttons=[[Button.inline("🔙 لغو", b"cancel")]])
        return
    if res.get("needs_code"):
        state[event.sender_id] = {"step": "await_code"}
        await event.respond(f"📩 کد ورود اومد (ورکر {w['tag']}). کد رو بفرست.",
                            buttons=[[Button.inline("🔙 لغو", b"cancel")]])
        return
    pending.pop(event.sender_id, None)
    await event.respond(f"❌ ورکر کد نفرستاد (status: {res.get('status')}). دوباره تلاش کن.")


async def handle_code_remote(event, ctx):
    w = ctx["worker"]
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        res = await worker.api_call(w, "POST", "/login/code",
                                    {"phone": ctx["phone"], "code": code}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ کد اشتباه یا خطا: {repr(e)[:150]}\nدوباره کد را بفرست یا لغو کن.")
        return
    if not res.get("ok"):
        await event.respond("❌ ورود ناموفق بود. دوباره تلاش کن یا لغو کن.")
        return
    await complete_account_remote(event, ctx, res)


async def handle_password_remote(event, ctx):
    w = ctx["worker"]
    password = event.raw_text.strip()
    try:
        await worker.api_call(w, "POST", "/login/password",
                              {"phone": ctx["phone"], "password": password})
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز اشتباه یا خطا: {repr(e)[:150]}\nدوباره رمز را بفرست.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 رمز پذیرفته شد. حالا کد ورود را بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


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

    await log(card("LOGIN SUCCESS ✅", [
        f"This Account : {phone}",
        LINE,
        f"Name : {name}",
        f"ID   : {guid}",
        LINE,
        f"📇 Contacts : {contacts}",
        f"👥 Groups   : {groups}",
        f"🎯 Contact with chat : {with_chat}",
        LINE,
        f"👨‍🔧 Worker : {w['tag']}",
    ]))
    await event.respond(
        f"✅ اکانت اضافه شد (ورکر {w['tag']})!\n"
        f"👤 {name} | 📱 {phone}\n"
        f"📇 مخاطبین: {contacts} | 👥 گروه‌ها: {groups} | 💬 چت‌دار: {with_chat}",
        buttons=[[Button.inline("🚀 ارسال", f"send_{account_id}".encode())],
                 [Button.inline("🏠 منوی اصلی", b"home")]],
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
    await safe_edit(event, 
        f"📌 مارکر فعلی: «{db.get_marker()}»\n{LINE}\n"
        "مارکر جدید رو بفرست (متنی که آخر کپشن پیام نشان‌دارت می‌ذاری):",
        buttons=[[Button.inline(
            ("✍️ متن دوم روبیکا : روشن" if cur2 else "✍️ متن دوم روبیکا : خاموش"),
            b"rbtext2")],
                 [Button.inline("🔙 بازگشت", b"home")]],
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
        "✍️ متنِ دومِ روبیکا رو بفرست (بعد از پیامِ مارکر، این هم به همون مخاطب "
        "فرستاده می‌شه — همیشه متنه).\n"
        f"الان: {('«'+cur[:80]+'»') if cur else '—'}\n"
        "برای خاموش‌کردن/پاک‌کردن، فقط یه نقطه (.) بفرست.",
        buttons=[[Button.inline("🔙 بازگشت", b"marker")]])


async def handle_rb_text2(event):
    state.pop(event.sender_id, None)
    txt = (event.raw_text or "").strip()
    if txt == ".":
        db.set_rb_text2("")
        await event.respond("🗑 متنِ دومِ روبیکا پاک شد.",
                            buttons=main_menu(is_real_owner(event)))
        return
    if not txt:
        await event.respond("متن خالیه.", buttons=main_menu(is_real_owner(event)))
        return
    db.set_rb_text2(txt)
    await event.respond("✅ متنِ دومِ روبیکا ذخیره شد. (موقعِ ارسال بعد از مارکر فرستاده می‌شه.)",
                        buttons=main_menu(is_real_owner(event)))


async def handle_marker(event):
    marker = event.raw_text.strip()
    if not marker:
        await event.respond("مارکر نمی‌تونه خالی باشه. دوباره بفرست.")
        return
    db.set_marker(marker)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ مارکر روی «{marker}» تنظیم شد.",
                        buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Admin management (OWNER ONLY)
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"admins"))
async def admins_cb(event):
    if not is_real_owner(event):
        await event.answer("فقط مالک ربات به این بخش دسترسی دارد.", alert=True)
        return
    admins = db.list_admins()
    rows = [[Button.inline(f"🗑 {a['name'] or a['user_id']}",
                           f"deladmin_{a['user_id']}".encode())] for a in admins]
    rows.append([Button.inline("➕ افزودن ادمین", b"admin_add")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    body = "\n".join(f"• {a['name'] or '-'} ({a['user_id']})" for a in admins) \
        if admins else "هنوز ادمینی اضافه نشده."
    await safe_edit(event, "👥 مدیریت ادمین‌ها:\n" + body, buttons=rows)


@bot.on(events.CallbackQuery(data=b"admin_add"))
async def admin_add_cb(event):
    if not is_real_owner(event):
        await event.answer("فقط مالک.", alert=True)
        return
    state[event.sender_id] = {"step": "await_admin_id"}
    await safe_edit(event, 
        "🆔 آیدی عددی تلگرام ادمین جدید رو بفرست (مثلاً `123456789`).\n"
        "می‌تونی اسم رو هم با فاصله بعدش بدی: `123456789 علی`",
        buttons=[[Button.inline("🔙 بازگشت", b"admins")]],
    )


async def handle_admin_id(event):
    if not is_real_owner(event):
        state.pop(event.sender_id, None)
        return
    parts = event.raw_text.strip().split(maxsplit=1)
    try:
        uid = int(parts[0])
    except (ValueError, IndexError):
        await event.respond("آیدی باید عدد باشه. دوباره بفرست.")
        return
    name = parts[1] if len(parts) > 1 else ""
    db.add_admin(uid, name)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ ادمین {uid} اضافه شد. حالا می‌تونه با ربات کار کنه.",
                        buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(pattern=b"deladmin_(\\d+)"))
async def deladmin_cb(event):
    if not is_real_owner(event):
        await event.answer("فقط مالک.", alert=True)
        return
    uid = int(event.pattern_match.group(1))
    db.remove_admin(uid)
    await event.answer("ادمین حذف شد.")
    await admins_cb(event)


# --------------------------------------------------------------------------- #
# Worker panel: status cards
# --------------------------------------------------------------------------- #
def _ping_text(w) -> str:
    p = w.get("ping_ms", -1)
    return f"{p}ms" if (p is not None and p >= 0) else "—"


def worker_status_all_card(workers) -> str:
    lines = ["🛠 STATU WORKER ALL", LINE]
    for w in workers:
        lines.append(f"🖥 {w['ip']} {w['tag']}")
        lines.append(LINE)
        lines.append(f"{worker.status_emoji(w)} {w['ip']} -{_ping_text(w)} - {worker.file_label(w)}")
        # When unhealthy, show the diagnostic reason so the cause is visible.
        if not w.get("file_ok"):
            d = worker.health_detail(w["id"])
            if d:
                lines.append(f"ℹ️ {d}")
        lines.append(LINE)
    lines.append(f"🕒 {now()}")
    return "\n".join(lines)


def added_worker_card(w) -> str:
    rows = [
        "🛠 ADDED WORKER", LINE,
        f"🖥 {w['ip']} {w['tag']}", LINE,
        "🛠 Statu Worker", LINE,
        f"{worker.status_emoji(w)} {w['ip']} -{_ping_text(w)} - {worker.file_label(w)}",
    ]
    if not w.get("file_ok"):
        d = worker.health_detail(w["id"])
        if d:
            rows.append(f"ℹ️ {d}")
    rows += [LINE, f"🕒 {now()}"]
    return "\n".join(rows)


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
        off = "" if w["enabled"] else " (خاموش)"
        kind = "🏠" if w["is_master"] else "🖥"
        rows.append([Button.inline(
            f"{worker.status_emoji(w)} {kind} {w['tag']} • {w['ip']}{off}",
            f"wk_{w['id']}".encode())])
    rows.append([Button.inline("➕ افزودن ورکر", b"wk_add"),
                 Button.inline("🔄 رفرش وضعیت", b"wk_refresh")])
    rows.append([Button.inline("⬆️ آپدیت همه", b"w_updall"),
                 Button.inline("🧾 نسخه‌ها", b"w_versions")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "🛠 مدیریت ورکرها\n(روی هر کدوم بزن برای جزئیات و مدیریت)", buttons=rows)


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
                "timeout: عملیات Worker از سقف امن زمانی عبور کرد؛ "
                "rollback ایمن برای مرحله تعویض فعال است.")
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
        await event.answer("یک آپدیت Worker همین حالا در حال اجراست؛ دوباره شروع نشد.", alert=True)
        return
    workers = [w for w in db.list_workers() if not w["is_master"] and w["enabled"]]
    if not workers:
        await event.answer("ورکرِ ریموتِ فعالی برای آپدیت نیست.", alert=True)
        return
    if not _acquire_worker_update_lock():
        await event.answer("یک آپدیت Worker در پردازش دیگر در حال اجراست؛ دوباره شروع نشد.",
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
        f"⬆️ آپدیت امن {len(workers)} ورکر شروع شد (برنچ: {config.GIT_BRANCH}).\n"
        "هر Worker فقط یک‌بار و ترتیبی اجرا می‌شود؛ نسخه قبلی تا آماده‌شدن image جدید حفظ می‌شود.",
        buttons=[[Button.inline("🔙 ورکرها", b"workers")]])


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
                await bot.send_message(chat_id, f"⏳ آپدیت امن {tag} • {ip} ...")
            status, detail = await _safe_update_worker(w)
            if status == "updated":
                updated_n += 1
                msg = f"✅ {tag} • {ip} آپدیت شد.\n{detail}"
            elif status == "current":
                current_n += 1
                msg = f"⏭ {tag} • {ip} از قبل به‌روز بود؛ رد شد.\n{detail}"
            else:
                fail_n += 1
                msg = f"❌ {tag} • {ip} ناموفق؛ rollback ایمن فعال بود.\n{detail[-700:]}"
        finally:
            _worker_updating_ids.discard(wid)
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, msg)
        if chat_id != config.LOG_GROUP_ID:
            with contextlib.suppress(Exception):
                await log(msg)
    final = card("⬆️ آپدیت امن همه Workerها — پایان", [
        f"✅ آپدیت‌شده : {updated_n}",
        f"⏭ از قبل به‌روز : {current_n}",
        f"❌ ناموفق : {fail_n}",
        f"🕒 {now()}",
    ])
    with contextlib.suppress(Exception):
        await bot.send_message(chat_id, final)


@bot.on(events.CallbackQuery(data=b"w_versions"))
async def w_versions_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال گرفتنِ نسخه‌ها ...")
    rows = [f"🏠 مستر : {master_code_version()}", LINE]
    for w in db.list_workers():
        if w["is_master"]:
            continue
        ver = "—"
        try:
            res = await worker.api_call(w, "GET", "/health")
            ver = str(res.get("version") or "—")
        except Exception as e:  # noqa: BLE001
            ver = f"خطا: {repr(e)[:40]}"
        rows.append(f"🖥 {w['tag']} • {w['ip']} : {ver}")
    await safe_edit(event, card("🧾 نسخه‌ها", rows + [LINE, f"🕒 {now()}"]),
                    buttons=[[Button.inline("🔙 ورکرها", b"workers")]])


@bot.on(events.CallbackQuery(data=b"wk_refresh"))
async def wk_refresh_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال بررسی هم‌زمان همه‌ی ورکرها ...")
    await log_status_all(refresh=True)
    await workers_cb(event)


@bot.on(events.CallbackQuery(data=b"wk_add"))
async def wk_add_cb(event):
    if not is_owner(event):
        return
    if not crypto_util.is_configured():
        await event.answer("اول WORKER_SECRET رو توی .env تنظیم کن (راهنما در README).",
                           alert=True)
        return
    state[event.sender_id] = {"step": "wk_ip", "wk": {}}
    await safe_edit(event, "🖥 آی‌پی سرور ورکر رو بفرست:",
                     buttons=[[Button.inline("🔙 لغو", b"workers")]])


async def handle_worker_step(event, step):
    st = state.get(event.sender_id)
    if not st:
        return
    wk = st.setdefault("wk", {})
    val = event.raw_text.strip()
    if step == "wk_ip":
        wk["ip"] = val
        st["step"] = "wk_port"
        await event.respond("🔌 پورت SSH رو بفرست (پیش‌فرض 22 — اگه همونه فقط `22` بفرست):",
                            buttons=[[Button.inline("🔙 لغو", b"workers")]])
    elif step == "wk_port":
        try:
            wk["port"] = int(val)
        except ValueError:
            wk["port"] = 22
        st["step"] = "wk_user"
        await event.respond("👤 یوزرنیم SSH (مثلاً `root`):",
                            buttons=[[Button.inline("🔙 لغو", b"workers")]])
    elif step == "wk_user":
        wk["user"] = val
        st["step"] = "wk_pass"
        await event.respond("🔑 پسورد SSH رو بفرست:",
                            buttons=[[Button.inline("🔙 لغو", b"workers")]])
    elif step == "wk_pass":
        wk["pass"] = val
        state.pop(event.sender_id, None)
        await provision_and_register(event, wk)


async def provision_and_register(event, wk):
    msg = await event.respond("🚀 شروع نصب ورکر روی سرور ...")

    # Reserve the worker tag up-front so the "building" log and the final
    # "added" card share the SAME tag.
    tag = worker.gen_tag()
    await log(card("🛠 WORKER BUilDING....", [
        f"🖥 {wk['ip']} {tag}",
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
        await safe_edit(msg, f"❌ نصب ناموفق: {prov.get('error')}",
                       buttons=[[Button.inline("🔙 بازگشت", b"workers")]])
        return
    wid = await worker.register_provisioned(wk["ip"], wk.get("port", 22),
                                            wk["user"], wk["pass"], prov)
    w = db.get_worker(wid)
    # Give the freshly started container time to fully come up before the
    # first health check; checking immediately on connect gave a misleading
    # status. Wait 30s, then verify.
    await safe_edit(msg, "⏳ ورکر نصب شد. ۳۰ ثانیه صبر برای آماده‌شدن کامل و بررسی وضعیت ...")
    await asyncio.sleep(30)
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(wid)
    await safe_edit(msg, f"✅ ورکر {w['tag']} اضافه و بررسی شد.",
                   buttons=[[Button.inline("🛠 مدیریت ورکر", b"workers")],
                            [Button.inline("🏠 منوی اصلی", b"home")]])
    await log(added_worker_card(w))
    await log_status_all(refresh=False)


@bot.on(events.CallbackQuery(pattern=b"wk_(\\d+)"))
async def wk_detail_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    n_acc = db.count_accounts_on_worker(wid)
    sent = db.worker_sent_today(wid)
    lines = [
        f"🛠 ورکر {w['tag']}", LINE,
        f"🖥 IP : {w['ip']}",
        f"نوع : {'Master (محلی)' if w['is_master'] else 'Worker'}",
        f"وضعیت : {worker.status_emoji(w)} {w['status']}",
        f"پینگ : {_ping_text(w)}",
        f"فایل : {worker.file_label(w)}",
        f"اکانت‌ها : {n_acc}",
        f"ارسال امروز : {sent}",
        f"فعال : {'بله' if w['enabled'] else 'خیر'}",
        f"آخرین بررسی : {w.get('last_checked') or '—'}",
    ]
    rows = []
    if not w["is_master"]:
        toggle = "⏸ قطع" if w["enabled"] else "▶️ وصل"
        rows.append([Button.inline(toggle, f"wktog_{wid}".encode()),
                     Button.inline("♻️ ری‌استارت", f"wkrst_{wid}".encode())])
        rows.append([Button.inline("⬆️ آپدیت", f"wkupd_{wid}".encode()),
                     Button.inline("🗑 حذف", f"wkdel_{wid}".encode())])
    else:
        # Local master worker: only allow enabling/disabling it as a worker
        # (no remote restart/update/teardown — it runs in-process).
        toggle = "⏸ خاموش‌کردن لوکال" if w["enabled"] else "▶️ روشن‌کردن لوکال"
        rows.append([Button.inline(toggle, f"wktog_{wid}".encode())])
    rows.append([Button.inline("🔄 بررسی این ورکر", f"wkchk_{wid}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"workers")])
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"wktog_(\\d+)"))
async def wk_toggle_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    db.set_worker_enabled(wid, not w["enabled"])
    await event.answer("وضعیت تغییر کرد.")
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkrst_(\\d+)"))
async def wk_restart_cb(event):
    if not is_owner(event):
        return
    if _worker_updates_busy():
        await event.answer("هنگام آپدیت Worker امکان ری‌استارت نیست.", alert=True)
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w["is_master"]:
        await event.answer("روی مستر قابل اجرا نیست.", alert=True)
        return
    if not _acquire_worker_update_lock():
        await event.answer("یک عملیات Worker در پردازش دیگر در حال اجراست.", alert=True)
        return
    await event.answer("در حال ری‌استارت ...")
    try:
        await worker.close_tunnel(wid)
        await worker.restart_worker(w)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در ری‌استارت: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 بازگشت", f"wk_{wid}".encode())]])
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
        await event.answer("روی مستر قابل اجرا نیست.", alert=True)
        return
    if _worker_updates_busy():
        await event.answer("یک آپدیت Worker همین حالا در حال اجراست؛ دوباره شروع نشد.", alert=True)
        return
    if not _acquire_worker_update_lock():
        await event.answer("یک آپدیت Worker در پردازش دیگر در حال اجراست؛ دوباره شروع نشد.",
                           alert=True)
        return
    _worker_updating_ids.add(wid)
    status = "failed"
    detail = "آپدیت شروع نشد."
    try:
        with contextlib.suppress(Exception):
            await safe_edit(event, f"⬆️ آپدیت امن Worker {w['tag']} شروع شد؛ نسخه قبلی تا تست image جدید حفظ می‌شود...")
        status, detail = await _safe_update_worker(w)
    finally:
        _worker_updating_ids.discard(wid)
        _release_worker_update_lock()
    if status == "failed":
        await safe_edit(event, f"❌ آپدیت ناموفق؛ rollback ایمن فعال بود.\n{detail[-700:]}",
                         buttons=[[Button.inline("🔙 بازگشت", f"wk_{wid}".encode())]])
        return
    if status == "current":
        await safe_edit(event, f"⏭ Worker {w['tag']} از قبل به‌روز بود؛ تغییری انجام نشد.\n{detail}",
                         buttons=[[Button.inline("🧾 نسخه‌ها", b"w_versions")],
                                  [Button.inline("🔙 بازگشت", f"wk_{wid}".encode())]])
        return
    await safe_edit(event, f"✅ Worker {w['tag']} با موفقیت آپدیت شد.\n{detail}",
                    buttons=[[Button.inline("🧾 نسخه‌ها", b"w_versions")],
                             [Button.inline("🔙 بازگشت", f"wk_{wid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"wkchk_(\\d+)"))
async def wk_check_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    await event.answer("در حال بررسی ...")
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
        "حذف کامل این ورکر؟ (کانتینر و سورس روی سرور هم پاک می‌شه)",
        buttons=[[Button.inline("✅ بله، حذف کن", f"wkdely_{wid}".encode())],
                 [Button.inline("🔙 خیر", f"wk_{wid}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"wkdely_(\\d+)"))
async def wk_del_do_cb(event):
    if not is_owner(event):
        return
    if _worker_updates_busy():
        await event.answer("هنگام آپدیت Worker امکان حذف نیست.", alert=True)
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    if not _acquire_worker_update_lock():
        await event.answer("یک عملیات Worker در پردازش دیگر در حال اجراست.", alert=True)
        return
    try:
        await safe_edit(event, "🗑 در حال پاک‌سازی سرور و حذف ورکر ...")
        if not w["is_master"]:
            try:
                await worker.teardown_worker(w)
            except Exception:
                pass
        db.delete_worker(wid)
    finally:
        _release_worker_update_lock()
    await safe_edit(event, f"✅ ورکر {w['tag']} حذف شد.",
                     buttons=[[Button.inline("🔙 بازگشت", b"workers")]])


# --------------------------------------------------------------------------- #
# Remote send (account owned by a remote worker)
# --------------------------------------------------------------------------- #
async def send_prepare_remote(event, acc, w, marker):
    if continuous_busy(acc["id"]):
        await safe_edit(event,
            "🔁 یک قابلیت اتومیشن (اتومیشن/منشی/ریپلای/گزارش) روی این اکانت روشنه. "
            "اول از بخش «🔁 اتومیشن» خاموشش کن، بعد ارسال بزن.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    await safe_edit(event, f"⏳ بررسی ورکر {w['tag']} و آماده‌سازی ...")
    # CHECK the worker right before using it.
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(event, 
            f"❌ ورکر {w['tag'] if w else '?'} الان سالم/فعال نیست"
            f" (وضعیت: {w['status'] if w else 'نامشخص'}).\n"
            "این اکانت روی همین ورکر لاگین شده و فقط از همین‌جا می‌تونه بفرسته.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/prepare",
                                    {"phone": acc["phone"], "marker": marker})
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در آماده‌سازی روی ورکر: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    if not res.get("marker_found"):
        await safe_edit(event, 
            f"❌ توی Saved Messages ورکر پیامی با مارکر «{marker}» نبود.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    total = res.get("total", 0)
    if total == 0:
        await safe_edit(event, "هیچ مخاطبی پیدا نشد.",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    pending_send[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"],
        "remote": True, "worker_id": w["id"], "total": total,
    }
    await safe_edit(event, 
        card(f"🚀 آماده‌ی ارسال (ورکر {w['tag']})", [
            f"📎 محتوا : پیام نشان‌دار «{marker}» ✅",
            f"🎯 گیرنده‌ها : {total} مخاطب",
            "ترتیب : چت‌دار ← آنلاین ← Last Seen",
            LINE,
            "به این مخاطب‌ها ارسال بشه؟",
        ]),
        buttons=[[Button.inline("✅ تأیید و ارسال", f"go_{acc['id']}".encode())],
                 [Button.inline("🔙 لغو", f"acc_{acc['id']}".encode())]],
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
        await log("⛔ ورکر صاحب این اکانت پیدا نشد.")
        pending_send.pop(owner_id, None)
        return

    active_jobs.add(account_id)
    if is_resume:
        await log(card("🔁 ادامه شروع شد", [
            f"🛠 Count : {count:03d}",
            f"📱 Phone : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            f"🕒 {now()}",
            LINE,
            f"⏳ باقی‌مونده برای ارسال : {len(explicit_recipients or [])}",
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
            f"📌 Marker : «{marker}» Found ✅",
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
        })
        if not res.get("ok") or not res.get("marker_found"):
            reason = "مارکر روی ورکر پیدا نشد"
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
                    reason = f"قطع ارتباط با ورکر: {repr(e)[:120]}"
                    break
                ok = stt.get("ok", 0)
                fail = stt.get("fail", 0)
                # progress log every SEND_LOG_EVERY successful sends (+ percent)
                if config.SEND_LOG_EVERY > 0 and ok // config.SEND_LOG_EVERY > last_log_mark:
                    last_log_mark = ok // config.SEND_LOG_EVERY
                    pct = int(ok * 100 / total) if total else 0
                    await log(card("📊 SEND PROGRESS", [
                        f"📱 {phone} (ورکر {w['tag']})",
                        f"✅ {ok} از {total} — {pct}%",
                        f"⏳ باقی‌مونده : {max(0, total - ok - fail)}",
                        f"🕒 {now()}",
                    ]))
                # auto-resume happening on the worker -> master posts the ALERT
                rc = stt.get("retry_count", 0)
                if rc > prev_retry:
                    prev_retry = rc
                    remaining = max(0, total - ok - fail)
                    await log(card("🚨 ALERT — وقفه ۵ دقیقه‌ای", [
                        f"✅ {ok}",
                        f"⏳ {remaining}",
                        f"🔁 دور {rc}",
                        f"👤 Account : {phone}",
                    ]))
                if stt.get("done"):
                    r = stt.get("reason")
                    if r == "manual_stop":
                        reason = "توقف دستی توسط کاربر"
                    elif r and str(r).startswith("max_errors"):
                        reason = f"رسیدن به سقف خطا ({db.get_max_errors()})"
                    elif r:
                        reason = str(r)
                    break
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:150]}"

    try:
        db.incr_worker_sent(w["id"], ok)
    except Exception:
        pass
    active_jobs.discard(account_id)
    dur = str(datetime.now() - started).split(".")[0]
    pending_send.pop(owner_id, None)
    is_owner_user = owner_id == config.OWNER_ID

    if reason:
        await log(card("🔁 ادامه متوقف شد ⛔" if is_resume else "⛔ SEND STOPPED", [
            f"👤 Account : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            f"📊 ✅ {ok}   ❌ {fail}   📁 {total}",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ ارسال متوقف شد. ✅ {ok} / ❌ {fail} از {total}\nدلیل: {reason}",
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
            "dead": ("blocked" in str(reason)) or ("باطل" in str(reason)),
            "reason": reason,
        })
    else:
        # fully finished -> nothing remaining; clear any stale paused record.
        try:
            db.delete_paused_send(account_id)
        except Exception:
            pass
        await log(card("🔁 ادامه تمام شد ✅" if is_resume else "SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"👤 Account : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            LINE,
            f"✅ {ok}   ❌ {fail}   📁 {total}",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ ارسال تمام شد. ✅ {ok} / ❌ {fail} از {total}",
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
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                        buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                 [Button.inline("🔙 بازگشت", b"home")]])
        return
    rows = []
    for a in accounts:
        on = automation_on(a["id"])
        rows.append([Button.inline(f"{'🟢' if on else '⚪️'} {a['phone']}",
                                   f"auto_{a['id']}".encode())])
    rows.append([Button.inline("🪪 سینک اسم/بیو همه اکانت‌ها", b"psync")])
    rows.append([Button.inline("📨 لینکدونی (موتور گروه‌ها)", b"linkdooni")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "🔁 اتومیشن — یک اکانت انتخاب کن:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auto_(\\d+)"))
async def automation_account_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    au = db.get_automation(account_id)
    texts = db.list_automation_texts(account_id)
    on = bool(au["enabled"])
    lines = [
        f"🔁 اتومیشن — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"فاصله : {au['interval_sec']} ثانیه",
        f"تعداد متن‌ها : {len(texts)}",
        f"مجموع ارسال : {au['sent_total']}",
        LINE,
        f"🤖 منشی : {'🟢' if secretary_on(account_id) else '⚪️'}   "
        f"📊 گزارش کانال : {'🟢' if channelreport_on(account_id) else '⚪️'}   "
        f"↩️ ریپلای : {'🟢' if reply_on(account_id) else '⚪️'}",
    ]
    rows = [
        [Button.inline("➕ افزودن متن", f"auadd_{account_id}".encode()),
         Button.inline("🗑 پاک‌کردن متن‌ها", f"auclr_{account_id}".encode())],
        [Button.inline("🔗 لیست گروه‌ها", f"aulnk_{account_id}".encode())],
        [Button.inline("⏱ تنظیم فاصله", f"auint_{account_id}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"autog_{account_id}".encode())],
        [Button.inline("🤖 منشی پیوی", f"secm_{account_id}".encode()),
         Button.inline("📊 گزارش کانال", f"crm_{account_id}".encode())],
        [Button.inline("↩️ پاسخ‌گوی ریپلای", f"rpm_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", b"automation")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auadd_(\\d+)"))
async def automation_add_text_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_text", "account_id": account_id}
    await safe_edit(event, "✍️ متنی که می‌خوای به گروه‌ها بره رو بفرست (می‌تونی چند تا پشت‌هم بفرستی):",
                    buttons=[[Button.inline("✅ تمام / بازگشت", f"auto_{account_id}".encode())]])


async def handle_auto_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    text = event.raw_text.strip()
    if not text:
        await event.respond("متن خالیه. دوباره بفرست.")
        return
    db.add_automation_text(account_id, text)
    n = len(db.list_automation_texts(account_id))
    await event.respond(
        f"✅ متن اضافه شد (مجموع: {n}). متن بعدی رو بفرست یا برگرد.",
        buttons=[[Button.inline("✅ تمام / بازگشت", f"auto_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"auclr_(\\d+)"))
async def automation_clear_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.clear_automation_texts(account_id)
    await event.answer("همه‌ی متن‌ها پاک شد.")
    await automation_account_cb(event)


@bot.on(events.CallbackQuery(pattern=b"auint_(\\d+)"))
async def automation_interval_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_interval", "account_id": account_id}
    await safe_edit(event,
        f"⏱ یک عدد بین {config.AUTOMATION_MIN_INTERVAL} تا {config.AUTOMATION_MAX_INTERVAL} "
        "بفرست (فاصله‌ی هر دور به ثانیه):",
        buttons=[[Button.inline("🔙 بازگشت", f"auto_{account_id}".encode())]])


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
    await event.respond(f"✅ فاصله روی {iv} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"auto_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"autog_(\\d+)"))
async def automation_toggle_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    au = db.get_automation(account_id)
    if not au["enabled"]:                       # turning ON
        if not db.list_automation_texts(account_id):
            await event.answer("اول حداقل یک متن اضافه کن.", alert=True)
            return
        if account_id in active_jobs:
            await event.answer("این اکانت الان در حال ارساله. صبر کن تموم شه.", alert=True)
            return
        # start FIRST; only mark enabled if it actually launched (so a dead/old
        # worker can't leave the account stuck in a broken "on" state).
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع اتومیشن ناموفق: {repr(e)[:120]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    links = db.list_automation_links(account_id)
    body = "\n".join(f"• {ln}" for ln in links) if links else "هنوز لینکی اضافه نشده."
    lines = [f"🔗 لیست گروه‌های {acc['phone']}", LINE, body, LINE,
             "می‌تونی لینک گروه‌های شخصی‌ت رو اضافه کنی، بعد «عضو شو» بزنی تا "
             "همین اکانت عضوشون بشه."]
    rows = [
        [Button.inline("➕ افزودن لینک", f"auladd_{account_id}".encode()),
         Button.inline("🗑 پاک‌کردن", f"aulclr_{account_id}".encode())],
        [Button.inline("✅ عضو شو (و ذخیره در لیست مشترک)", f"auljoin_{account_id}".encode())],
        [Button.inline(f"📥 عضو از لیست مشترک ({db.count_verified_group_links()})",
                       f"aushared_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{account_id}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auladd_(\\d+)"))
async def automation_link_add_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_link", "account_id": account_id}
    await safe_edit(event, "🔗 لینک گروه روبیکا رو بفرست (می‌تونی چند تا پشت‌هم بفرستی):",
                    buttons=[[Button.inline("✅ تمام / بازگشت", f"aulnk_{account_id}".encode())]])


async def handle_auto_link(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    link = event.raw_text.strip()
    if not link.startswith("http"):
        await event.respond("یه لینکِ معتبر بفرست (با https شروع شه).")
        return
    db.add_automation_link(account_id, link)
    n = len(db.list_automation_links(account_id))
    await event.respond(
        f"✅ لینک اضافه شد (مجموع: {n}). لینک بعدی رو بفرست یا برگرد.",
        buttons=[[Button.inline("✅ تمام / بازگشت", f"aulnk_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"aulclr_(\\d+)"))
async def automation_link_clear_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.clear_automation_links(account_id)
    await event.answer("لینک‌ها پاک شد.")
    await automation_links_cb(event)


@bot.on(events.CallbackQuery(pattern=b"auljoin_(\\d+)"))
async def automation_link_join_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    links = db.list_automation_links(account_id)
    if not links:
        await event.answer("اول حداقل یه لینک اضافه کن.", alert=True)
        return
    if continuous_busy(account_id):
        await event.answer("🔁 یک قابلیت اتومیشن روی این اکانت روشنه. اول خاموشش کن، بعد «عضو شو» بزن.",
                           alert=True)
        return
    if account_id in active_jobs:
        await event.answer("این اکانت الان مشغوله. صبر کن.", alert=True)
        return
    await safe_edit(event, f"⏳ {acc['phone']} داره عضو {len(links)} گروه می‌شه ... "
                    "گزارش در گروه لاگ میاد.")
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
        await log(f"⚠️ عضو شدن در گروه‌های «{phone}» ناقص ماند: {repr(e)[:150]}")
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
                                        reason="بن/سکوت یا عدم امکان ارسال")
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
                await log(f"⚠️ اتومیشن «{phone}» خطای دور (ادامه می‌دهد): {repr(e)[:150]}")
            st["heartbeat"] = time.monotonic()
            waited = 0
            while waited < st["interval"] and not st["stop"]:
                await asyncio.sleep(1)
                waited += 1
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ اتومیشن «{phone}» با خطا متوقف شد: {repr(e)[:150]}")


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
                            ("علت: تسک متوقف شده بود" if dead else "علت: هنگ بی‌صدا (بدون فعالیت)"),
                            "اتومیشن دوباره راه‌اندازی شد.",
                            f"🕒 {now()}"]))
                        try:
                            await start_automation(acc)
                        except Exception as e:  # noqa: BLE001
                            await log(f"⚠️ self-heal اتومیشن {acc['phone']} ناموفق: {repr(e)[:120]}")
                    if t and t["state"].get("groups") is not None:
                        groups = t["state"].get("groups")
                rows = [f"👤 Account : {acc['phone']}", f"✅ مجموع ارسال : {sent}"]
                if groups is not None:
                    rows.append(f"👥 گروه‌ها : {groups}")
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
            await log(f"⚠️ بازگردانی اتومیشن {acc['phone']} ناموفق: {repr(e)[:120]}")


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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    sec = db.get_secretary(aid)
    on = bool(sec["enabled"])
    mode = sec.get("mode") or "marker"
    lines = [
        f"🤖 منشی پیوی — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"حالت جواب : {'متن دلخواه' if mode == 'text' else 'مارکر (پیام نشان‌دار)'}",
        f"متن دلخواه : {((sec.get('text') or '—')[:40])}",
        f"فاصله چک : {sec.get('interval_sec')} ثانیه",
        f"مجموع جواب‌ها : {sec.get('replied_total')}",
        LINE,
        "فقط به «اولین پیامِ» هر نفر جواب داده می‌شه.",
    ]
    rows = [
        [Button.inline("📌 حالت مارکر" + (" ✅" if mode == "marker" else ""),
                       f"secmodem_{aid}".encode()),
         Button.inline("✍️ حالت متن" + (" ✅" if mode == "text" else ""),
                       f"secmodet_{aid}".encode())],
        [Button.inline("✍️ تنظیم متن دلخواه", f"sectext_{aid}".encode())],
        [Button.inline("⏱ تنظیم فاصله", f"secint_{aid}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"sectog_{aid}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"secmodem_(\\d+)"))
async def secretary_mode_marker_cb(event):
    if not is_owner(event):
        return
    db.set_secretary_mode(int(event.pattern_match.group(1)), "marker")
    await event.answer("حالت: مارکر")
    await secretary_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"secmodet_(\\d+)"))
async def secretary_mode_text_cb(event):
    if not is_owner(event):
        return
    db.set_secretary_mode(int(event.pattern_match.group(1)), "text")
    await event.answer("حالت: متن دلخواه")
    await secretary_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"sectext_(\\d+)"))
async def secretary_set_text_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_sec_text", "account_id": aid}
    await safe_edit(event, "✍️ متنِ جوابِ منشی رو بفرست:",
                    buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


async def handle_sec_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("متن خالیه. دوباره بفرست.")
        return
    db.set_secretary_text(aid, txt)
    db.set_secretary_mode(aid, "text")
    state.pop(event.sender_id, None)
    await event.respond("✅ متن منشی تنظیم شد و حالت روی «متن دلخواه» رفت.",
                        buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"secint_(\\d+)"))
async def secretary_interval_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_sec_interval", "account_id": aid}
    await safe_edit(event,
        f"⏱ فاصله‌ی چک پیوی (ثانیه) بین {config.SECRETARY_MIN_INTERVAL} تا "
        f"{config.SECRETARY_MAX_INTERVAL} بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


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
    await event.respond(f"✅ فاصله روی {iv} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"sectog_(\\d+)"))
async def secretary_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    sec = db.get_secretary(aid)
    if not sec["enabled"]:
        if aid in active_jobs:
            await event.answer("این اکانت الان مشغول یک عملیات تک‌باریه. صبر کن.", alert=True)
            return
        if (sec.get("mode") or "marker") == "text" and not (sec.get("text") or "").strip():
            await event.answer("اول متن دلخواه رو تنظیم کن یا حالت مارکر رو انتخاب کن.",
                               alert=True)
            return
        try:
            await start_secretary(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع منشی ناموفق: {repr(e)[:110]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    cr = db.get_channel_report(aid)
    on = bool(cr["enabled"])
    lines = [
        f"📊 گزارش کانال — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"کانال : {cr.get('channel_guid') or '—'}",
        f"عنوان : {cr.get('channel_title') or '—'}",
        f"فاصله : {cr.get('interval_sec')} ثانیه",
        LINE,
        "هر بازه: تعداد اعضا + بازدید آخرین پست → گروه لاگ.",
    ]
    rows = [
        [Button.inline("📢 تنظیم کانال (لینک/یوزرنیم/گایید)", f"crset_{aid}".encode())],
        [Button.inline("⏱ تنظیم فاصله", f"crint_{aid}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"crtog_{aid}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"crset_(\\d+)"))
async def channelreport_set_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_cr_channel", "account_id": aid}
    await safe_edit(event,
        "📢 لینک یا یوزرنیم یا گاییدِ کانال رو بفرست:\n"
        "مثال: `@my_channel` یا `https://rubika.ir/my_channel` یا `c0...`",
        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


async def handle_cr_channel(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    ref = event.raw_text.strip()
    if not ref:
        await event.respond("خالیه. دوباره بفرست.")
        return
    db.set_channel_report_target(aid, ref, "")
    state.pop(event.sender_id, None)
    await event.respond("✅ کانال ثبت شد. (موقع گزارش، یوزرنیم/لینک خودکار به گایید تبدیل می‌شه)",
                        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"crint_(\\d+)"))
async def channelreport_interval_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_cr_interval", "account_id": aid}
    await safe_edit(event,
        f"⏱ فاصله‌ی گزارش (ثانیه) بین {config.CHANNEL_REPORT_MIN_INTERVAL} تا "
        f"{config.CHANNEL_REPORT_MAX_INTERVAL} بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


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
    await event.respond(f"✅ فاصله روی {iv} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"crtog_(\\d+)"))
async def channelreport_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    cr = db.get_channel_report(aid)
    if not cr["enabled"]:
        if aid in active_jobs:
            await event.answer("این اکانت الان مشغول یک عملیات تک‌باریه. صبر کن.", alert=True)
            return
        if not (cr.get("channel_guid") or "").strip():
            await event.answer("اول کانال رو تنظیم کن.", alert=True)
            return
        try:
            await start_channelreport(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع گزارش ناموفق: {repr(e)[:110]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    rr = db.get_reply_responder(aid)
    on = bool(rr["enabled"])
    lines = [
        f"↩️ پاسخ‌گوی ریپلای — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"متن جواب : {((rr.get('text') or '—')[:40])}",
        f"تأخیر : {rr.get('delay_sec')} ثانیه",
        f"مجموع جواب‌ها : {rr.get('replied_total')}",
        LINE,
        "وقتی توی گروه به این اکانت ریپلای بزنن، جواب خودکار می‌ده (فعلاً فقط متن).",
    ]
    rows = [
        [Button.inline("✍️ تنظیم متن", f"rptext_{aid}".encode())],
        [Button.inline("⏱ تنظیم تأخیر", f"rpdelay_{aid}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"rptog_{aid}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"rptext_(\\d+)"))
async def reply_set_text_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_rp_text", "account_id": aid}
    await safe_edit(event, "✍️ متنِ جوابِ ریپلای رو بفرست:",
                    buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


async def handle_rp_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("متن خالیه. دوباره بفرست.")
        return
    db.set_reply_text(aid, txt)
    state.pop(event.sender_id, None)
    await event.respond("✅ متن ریپلای تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"rpdelay_(\\d+)"))
async def reply_set_delay_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_rp_delay", "account_id": aid}
    await safe_edit(event,
        f"⏱ تأخیرِ جواب (ثانیه) بین {config.REPLY_MIN_DELAY} تا "
        f"{config.REPLY_MAX_DELAY} بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


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
    await event.respond(f"✅ تأخیر روی {d} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"rptog_(\\d+)"))
async def reply_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    rr = db.get_reply_responder(aid)
    if not rr["enabled"]:
        if aid in active_jobs:
            await event.answer("این اکانت الان مشغول یک عملیات تک‌باریه. صبر کن.", alert=True)
            return
        if not (rr.get("text") or "").strip():
            await event.answer("اول متن جواب رو تنظیم کن.", alert=True)
            return
        try:
            await start_reply(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع ریپلای ناموفق: {repr(e)[:110]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
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
        "🪪 سینک اسم/بیو همه اکانت‌ها", LINE,
        f"نام : {name or '—'}",
        f"بیو : {p.get('bio') or '—'}",
        LINE,
        "این مقدار روی همه‌ی اکانت‌ها اعمال می‌شه (عکس لازم نیست).",
    ]
    rows = [
        [Button.inline("✏️ تنظیم نام/بیو", b"psyncset")],
        [Button.inline("🚀 اعمال روی همه", b"psyncgo")],
        [Button.inline("🔙 بازگشت", b"automation")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(data=b"psyncset"))
async def psync_set_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_psync"}
    await safe_edit(event,
        "✏️ نام رو در خط اول و بیو رو در خط دوم بفرست:\n"
        "خط اول = نام کامل (با اولین فاصله به نام/نام‌خانوادگی تقسیم می‌شه)\n"
        "خط دوم = بیو\n\nمثال:\nعلی رضایی\nسلام، خوش اومدی 🌹",
        buttons=[[Button.inline("🔙 بازگشت", b"psync")]])


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
        f"✅ ثبت شد:\nنام: {name_line or '—'}\nبیو: {bio or '—'}\n"
        "حالا «🚀 اعمال روی همه» رو بزن.",
        buttons=[[Button.inline("🔙 بازگشت", b"psync")]])


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
        await event.answer("هیچ اکانتی نیست.", alert=True)
        return
    p = db.get_profile_sync()
    if not (p.get("first_name") or p.get("last_name") or p.get("bio")):
        await event.answer("اول نام/بیو رو تنظیم کن.", alert=True)
        return
    await safe_edit(event,
        f"⏳ در حال اعمال نام/بیو روی {len(accounts)} اکانت ... گزارش در گروه لاگ میاد.")
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
                rows.append(f"• {phone} : ✅ عوض شد")
            else:
                unchanged += 1
                rows.append(f"• {phone} : ⏸ بدون تغییر")
        except account_conn.InvalidAuthError:
            failed += 1
            rows.append(f"• {phone} : 🔐 سشن باطل (لاگین مجدد)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            rows.append(f"• {phone} : ❌ {repr(e)[:60]}")
        await asyncio.sleep(config.PROFILE_SYNC_DELAY)
    await log(card("🪪 PROFILE SYNC", [
        f"✅ تغییر: {changed}   ⏸ بدون تغییر: {unchanged}   ❌ خطا: {failed}",
        LINE, *rows, LINE, f"🕒 {now()}"]))
    try:
        await bot.send_message(config.OWNER_ID,
                               f"🪪 سینک پروفایل تمام شد. ✅ {changed} / ⏸ {unchanged} / ❌ {failed}",
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
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    links = db.list_verified_group_links()
    if not links:
        await event.answer("لیست مشترک خالیه. اول با یک اکانت «عضو شو» بزن تا پر شه.",
                           alert=True)
        return
    if continuous_busy(aid):
        await event.answer("یک قابلیت اتومیشن روی این اکانت روشنه. اول خاموشش کن.", alert=True)
        return
    if aid in active_jobs:
        await event.answer("این اکانت مشغوله. صبر کن.", alert=True)
        return
    await safe_edit(event,
        f"⏳ {acc['phone']} داره از لیست مشترک ({len(links)}) عضو می‌شه ... "
        "گزارش در گروه لاگ میاد.")
    asyncio.create_task(run_group_join(acc, links))


# --------------------------------------------------------------------------- #
# 🏭 Generator engine (موتور مولد): one account creates a channel/group, the
# others join it, the owner makes them admins (we poll user_is_admin), then all
# accounts seed their contacts (sequentially, anti-duplicate). Fully logged.
# Local accounts go through account_conn; worker accounts via /gen/* endpoints.
# Never touches the automation logic or the base source.
# --------------------------------------------------------------------------- #
def generator_menu_text():
    b = db.get_broadcaster()
    sel = db.list_broadcaster_account_ids()
    return card("📢 پخش کانالی (موتور مولد)", [
        f"اسم مشترک کانال‌ها : {b.get('title') or '—'}",
        f"اکانت‌های انتخاب‌شده : {len(sel)}",
        f"سقف عضوگیری (هر کانال) : {b.get('member_target')}",
        f"فاصله بین اکانت‌ها : {b.get('gap_seconds')} ثانیه",
        LINE,
        "هر اکانتِ انتخابی، کانالِ خودش رو می‌سازه (با اسم مشترک + یوزرنیم رندوم)، "
        "پیامِ مارکر رو می‌فرسته، و مخاطبینِ خودش رو عضو می‌کنه. نوبتی + لاگ کامل.",
    ])


def generator_menu_buttons():
    return [
        [Button.inline("✏️ اسم مشترک کانال", b"bc_title")],
        [Button.inline("👥 انتخاب اکانت‌ها", b"bc_accounts"),
         Button.inline("🎯 سقف عضوگیری", b"bc_target")],
        [Button.inline("⏱ فاصله بین اکانت‌ها", b"bc_gap")],
        [Button.inline("▶️ شروع پخش کانالی", b"bc_start")],
        [Button.inline("🔙 بازگشت", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"generator"))
async def generator_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, generator_menu_text(), buttons=generator_menu_buttons())


@bot.on(events.CallbackQuery(data=b"bc_title"))
async def bc_title_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_bc_title"}
    await safe_edit(event, "✏️ اسمِ مشترکِ کانال‌ها رو بفرست (همه‌ی کانال‌ها این اسم رو می‌گیرن):",
                    buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


async def handle_bc_title(event):
    title = event.raw_text.strip()
    if not title:
        await event.respond("اسم خالیه. دوباره بفرست.")
        return
    db.set_broadcaster(title=title)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ اسم مشترک روی «{title}» تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


@bot.on(events.CallbackQuery(data=b"bc_accounts"))
async def bc_accounts_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("اول یک اکانت اضافه کن.", alert=True)
        return
    sel = set(db.list_broadcaster_account_ids())
    rows = []
    for a in accounts:
        mark = "✅" if a["id"] in sel else "⬜️"
        rows.append([Button.inline(f"{mark} {a['phone']}",
                                   f"bcacc_{a['id']}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"generator")])
    await safe_edit(event, "👥 اکانت‌هایی که کانال می‌سازن رو انتخاب کن (بزن تا تیک بخوره):",
                    buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"bcacc_(\\d+)"))
async def bc_acc_toggle_cb(event):
    if not is_owner(event):
        return
    db.toggle_broadcaster_account(int(event.pattern_match.group(1)))
    await bc_accounts_cb(event)


@bot.on(events.CallbackQuery(data=b"bc_target"))
async def bc_target_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_bc_target"}
    await safe_edit(event, "🎯 سقفِ عضوگیری برای هر کانال رو بفرست (عدد، مثلاً 300):",
                    buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


async def handle_bc_target(event):
    try:
        n = max(1, int(event.raw_text.strip()))
    except ValueError:
        await event.respond("یه عدد بفرست.")
        return
    db.set_broadcaster(member_target=n)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ سقف عضوگیری روی {n} تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


@bot.on(events.CallbackQuery(data=b"bc_gap"))
async def bc_gap_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_bc_gap"}
    await safe_edit(event, "⏱ فاصله بین اکانت‌ها رو به ثانیه بفرست (مثلاً 8):",
                    buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


async def handle_bc_gap(event):
    try:
        n = max(1, int(event.raw_text.strip()))
    except ValueError:
        await event.respond("یه عدد (ثانیه) بفرست.")
        return
    db.set_broadcaster(gap_seconds=n)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ فاصله روی {n} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


@bot.on(events.CallbackQuery(data=b"bc_start"))
async def bc_start_cb(event):
    if not is_owner(event):
        return
    b = db.get_broadcaster()
    sel = db.list_broadcaster_account_ids()
    if not b.get("title"):
        await event.answer("اول اسمِ مشترک رو تنظیم کن.", alert=True)
        return
    if not sel:
        await event.answer("اول حداقل یک اکانت انتخاب کن.", alert=True)
        return
    await safe_edit(event,
        f"📢 پخش کانالی شروع شد روی {len(sel)} اکانت. نوبتی و با لاگ کامل پیش می‌ره.",
        buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
    asyncio.create_task(run_broadcaster(event.sender_id))


async def _broadcast_one(acc, title, member_target, marker):
    """Make this account's OWN channel, set a random username, forward the
    marked post, then seed its OWN contacts. Local or worker."""
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        res = await worker.api_call(w, "POST", "/broadcast/run", {
            "phone": acc["phone"], "title": title, "marker": marker,
            "member_target": member_target}, timeout=900)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "broadcast failed"))
        return res.get("object_guid"), res.get("username", ""), \
            res.get("forwarded", False), res.get("added", 0)

    async def _do(client):
        guid = await rb.create_channel(client, title)
        username = ""
        try:
            username = await rb.assign_random_channel_username(client, guid)
        except Exception:
            username = ""
        forwarded = False
        try:
            saved_guid, mid = await rb.find_marked_message(client, marker)
            if mid:
                await rb.forward_message(client, saved_guid, guid, mid)
                forwarded = True
        except Exception:
            forwarded = False
        added = 0
        try:
            added = await rb.seed_channel_with_contacts(
                client, guid, target=member_target,
                batch=config.CHANNEL_ADD_BATCH, delay=config.CHANNEL_ADD_DELAY)
        except Exception:
            added = 0
        return guid, username, forwarded, added
    return await account_conn.call(acc["phone"], _do, timeout=900)


async def run_broadcaster(owner_id: int):
    b = db.get_broadcaster()
    title = b.get("title")
    member_target = int(b.get("member_target") or config.CHANNEL_MEMBER_TARGET)
    gap = int(b.get("gap_seconds") or config.BROADCAST_GAP_SECONDS)
    marker = db.get_marker()
    ids = db.list_broadcaster_account_ids()
    accounts = [db.get_account(i) for i in ids]
    accounts = [a for a in accounts if a]

    await log(card("📢 پخش کانالی — شروع", [
        f"🎛 اسم مشترک : {title}",
        f"👥 اکانت‌ها : {len(accounts)}",
        f"🎯 سقف هر کانال : {member_target}",
        f"⏱ فاصله : {gap}s",
        f"🕒 {now()}"]))

    made = 0
    total_added = 0
    failed = 0
    # SEQUENTIAL — never parallel (safe for worker accounts too). One account's
    # failure (auth/hang/anything) must NEVER stop the whole run.
    for acc in accounts:
        phone = acc["phone"]
        try:
            # hard per-account time cap so one stuck account can't freeze the run
            guid, username, forwarded, added = await asyncio.wait_for(
                _broadcast_one(acc, title, member_target, marker), timeout=1200)
            made += 1
            total_added += added
            await log(card("📢 پخش کانالی — کانال ساخته شد ✅", [
                f"👤 {phone}",
                f"🆔 {guid}",
                (f"🔗 @{username}" if username else "⚠️ یوزرنیم ست نشد"),
                ("📎 پیام مارکر فرستاده شد" if forwarded
                 else f"⚠️ مارکر «{marker}» پیدا/فرستاده نشد"),
                f"➕ مخاطبینِ اضافه‌شده : {added}",
                f"🕒 {now()}"]))
        except account_conn.InvalidAuthError:
            failed += 1
            await _log_invalid_auth(phone)
            await log("📢 پخش کانالی: این اکانت رد شد، ادامه می‌دیم ➡️")
        except asyncio.TimeoutError:
            failed += 1
            await log(card("📢 پخش کانالی — اکانت کند/هنگ (رد شد)", [
                f"👤 {phone}", "بیش از حد طول کشید، رد شد و ادامه می‌دیم ➡️",
                f"🕒 {now()}"]))
        except Exception as e:  # noqa: BLE001
            failed += 1
            await log(card("📢 پخش کانالی — خطا (رد شد، ادامه)", [
                f"👤 {phone}", f"💥 {repr(e)[:160]}",
                "این اکانت کانال نساخت، ولی پروسه ادامه داره ➡️",
                f"🕒 {now()}"]))
        # gap between accounts — never let the sleep itself break the loop
        try:
            await asyncio.sleep(gap)
        except Exception:
            pass

    await log(card("📢 پخش کانالی — پایان ✅", [
        f"🎛 اسم : {title}",
        f"✅ کانال‌های ساخته‌شده : {made}/{len(accounts)}",
        f"❌ ناموفق : {failed}",
        f"➕ کلِ مخاطبینِ اضافه‌شده : {total_added}",
        f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id,
                               f"📢 پخش کانالی تمام شد.\nکانال: {made}/{len(accounts)} | "
                               f"مخاطب اضافه‌شده: {total_added}",
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
        await event.answer("اول یک اکانت اضافه کن.", alert=True)
        return
    rows = [[Button.inline(f"🖼 {a['phone']}", f"pvx_{a['id']}".encode())]
            for a in accounts]
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event,
        "🖼 از کدوم اکانت عکس‌های پیوی‌ها رو جمع کنم و PDF بفرستم؟\n"
        "(فقط عکس — فیلم/گیف نه. هیچ عکسی جا نمی‌مونه.)", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"pvx_(\\d+)"))
async def pvexport_run_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await safe_edit(event,
        f"⏳ شروع جمع‌آوری عکس‌های پیویِ {acc['phone']} ... این ممکنه چند دقیقه طول بکشه. "
        "وقتی آماده شد، PDF برات ارسال می‌شه.",
        buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
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
            "🖼 آرشیو عکس پیوی — فایل نهایی کامل ✅" if final
            else "🖼 آرشیو عکس پیوی (تجمعی زنده)", [
                f"📱 {phone}",
                f"🖼 عکس‌های این فایل (تجمعی) : {n}",
                ("🏁 پایان" if final else "⏳ ادامه دارد ..."),
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
        await log(card("📸 جمع‌آوری زنده", [
            f"📱 {phone}", f"🖼 تا الان پیدا شد : {len(photos_so_far)}", f"🕒 {now()}"]))
        await _pv_build_and_send(phone, list(photos_so_far), final=False)
        last_sent["n"] = len(photos_so_far)

    try:
        photos = await _pv_collect_photos(acc, on_batch=on_batch)
    except account_conn.InvalidAuthError:
        await _log_invalid_auth(phone)
        return
    except Exception as e:  # noqa: BLE001
        await log(card("🖼 آرشیو عکس پیوی — خطا", [
            f"👤 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        try:
            await bot.send_message(owner_id, f"❌ جمع‌آوری عکس‌های {phone} ناموفق: {repr(e)[:120]}")
        except Exception:
            pass
        return

    if not photos:
        await bot.send_message(owner_id, f"ℹ️ هیچ عکسی در پیوی‌های {phone} پیدا نشد.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
        return

    total_photos = len(photos)

    # Remote accounts return all photos at once (no live stream), so do the
    # cumulative growing sends here: 20, 40, 60, ... to the group.
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        i = batch
        while i < total_photos:
            await log(card("📸 جمع‌آوری", [
                f"📱 {phone}", f"🖼 تا الان : {i} از {total_photos}", f"🕒 {now()}"]))
            await _pv_build_and_send(phone, photos[:i], final=False)
            i += batch
        last_sent["n"] = min(i, total_photos)

    # FINAL complete one-piece file (ALL photos) to the group.
    try:
        n_final = await _pv_build_and_send(phone, photos, final=True)
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ آرشیو عکس — خطای فایل نهایی", [f"👤 {phone}", f"💥 {repr(e)[:140]}"]))
        n_final = total_photos

    # final "پایان" summary card to the group
    await log(card("🏁 آرشیو عکس پیوی — پایان", [
        f"👤 {phone}",
        f"🖼 مجموع عکس‌ها : {total_photos}",
        f"📄 فایل نهایی کامل ارسال شد ({n_final} عکس)",
        f"🕒 {now()}"]))

    # also send the full one-piece PDF to the owner.
    import pdf_export
    out_path = os.path.join(DATA_DIR, f"pv_{phone}_{int(datetime.now().timestamp())}.pdf")
    try:
        n = await asyncio.to_thread(pdf_export.build_pdf, photos, out_path)
    except Exception as e:  # noqa: BLE001
        await bot.send_message(owner_id, f"❌ ساخت PDF نهایی ناموفق: {repr(e)[:120]}")
        return
    try:
        await bot.send_file(owner_id, out_path,
                            caption=f"🖼 آرشیو کامل عکس‌های پیویِ {phone}\nتعداد: {n} عکس",
                            force_document=True)
        await bot.send_message(owner_id, "✅ آرشیو کامل ارسال شد.",
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
                await log(f"⚠️ بازگردانی منشی {acc['phone']} ناموفق: {repr(e)[:120]}")
    for cr in db.list_enabled_channel_reports():
        acc = db.get_account(cr["account_id"])
        if acc:
            try:
                await start_channelreport(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ بازگردانی گزارش‌کانال {acc['phone']} ناموفق: {repr(e)[:120]}")
    for rr in db.list_enabled_reply_responders():
        acc = db.get_account(rr["account_id"])
        if acc:
            try:
                await start_reply(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ بازگردانی ریپلای {acc['phone']} ناموفق: {repr(e)[:120]}")


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
    accounts = db.list_accounts()
    alive = 0
    dead = 0
    skipped = 0
    healed = 0
    dead_rows = []
    for acc in accounts:
        phone = acc["phone"]
        aid = acc["id"]
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
                    checked = False
            else:
                is_dead = await account_conn.verify_session_dead(phone)
        except Exception:
            checked = False

        if not checked:
            skipped += 1
            continue
        if is_dead:
            dead += 1
            dead_rows.append(f"• {phone} : 🔴 شوت‌شده")
            if config.HEALTH_ENGINE_AUTODISABLE_DEAD and acc["status"] == "active":
                # stop features + flag inactive (never auto-delete; that's manual)
                try:
                    db.set_secretary_enabled(aid, False)
                    db.set_channel_report_enabled(aid, False)
                    db.set_reply_enabled(aid, False)
                    db.set_automation_enabled(aid, False)
                except Exception:
                    pass
                for stopper in (stop_automation, stop_secretary,
                                stop_channelreport, stop_reply):
                    try:
                        await stopper(acc)
                    except Exception:
                        pass
                db.set_status(aid, "inactive")
        else:
            alive += 1
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
        f"🟢 سالم : {alive}",
        f"🔴 شوت‌شده : {dead}",
        f"♻️ اتومیشن‌های ترمیم‌شده : {healed}",
    ]
    if skipped:
        rows.append(f"❔ بررسی‌نشده (ورکر در دسترس نبود) : {skipped}")
    if dead_rows:
        rows.append(LINE)
        rows.extend(dead_rows)
        rows.append("برای حذفِ کامل: «👤 اکانت‌های من» → «🔄 بررسی و پاکسازی».")
    rows.append(LINE)
    rows.append(f"🕒 {now()}")
    await log(card("🩺 موتور سلامت و خودتعمیر", rows))


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
                        kind = "بلاک" if r["status"] == "blocked" else "قطع"
                        await log(card("🚨 WORKER ALERT", [
                            f"👨‍🔧 {r['tag']} • {r['ip']}",
                            f"وضعیت: 🟢 سالم  ←  🔴 {kind}",
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
        parts.append(f"✍️ {n_text} متن")
    if n_media:
        parts.append(f"🖼 {n_media} فایل")
    return f"{len(msgs)} آیتم ({' + '.join(parts)})"


def _tg_menu_buttons():
    """Simple navigation back into the Telegram panel (used after config saves)."""
    return [[Button.inline("🔙 پنل تلگرام", b"tg")]]


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
    rows.append([Button.inline("➕ افزودن اکانت", b"tgadd")])
    rows.append([Button.inline("📤 ارسال چنداکانتی", b"tg_multi"),
                 Button.inline("📊 وضعیت ارسال‌ها", b"tg_multi_jobs")])
    rows.append([Button.inline("🔙 بازگشت به روبیکا", b"home")])
    head = card("✈️ پنل تلگرام", [
        f"👤 اکانت‌ها : {len(accs)}",
        "یک اکانت انتخاب کن، محتوای ارسال رو تنظیم کن و به مخاطبینش بفرست.",
    ])
    await safe_edit(event, head, buttons=rows)


# ----- login -----
@bot.on(events.CallbackQuery(data=b"tgadd"))
async def tg_add_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_tg_phone"}
    await safe_edit(event,
        "✈️ شمارهٔ اکانتِ تلگرام رو با کدِ کشور بفرست (مثلاً +98912...).",
        buttons=[[Button.inline("🔙 لغو", b"tg")]])


async def handle_tg_phone(event):
    phone = _normalize_phone_input(event)
    if not phone:
        await event.respond(
            "❌ شماره خونده نشد. شماره رو با کدِ کشور بفرست (مثلاً +989121234567 "
            "یا 09121234567) یا لغو کن.")
        return
    await event.respond("⏳ اتصال به تلگرام و ارسالِ کد ...")
    try:
        ctx = await tg.start_login(phone)
    except Exception as e:  # noqa: BLE001
        state.pop(event.sender_id, None)
        await event.respond(f"❌ خطا در ارسال کد: {repr(e)[:160]}")
        return
    tg_pending[event.sender_id] = ctx
    state[event.sender_id] = {"step": "await_tg_code"}
    await event.respond("📩 کدِ ورودِ تلگرام اومد. کد رو بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"tg")]])


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
            await event.respond("🔐 این اکانت رمزِ دومرحله‌ای داره. رمز رو بفرست.",
                                buttons=[[Button.inline("🔙 لغو", b"tg")]])
            return
        await event.respond(f"❌ کد اشتباه/خطا: {repr(e)[:160]}\nدوباره کد رو بفرست یا لغو کن.")
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
        await event.respond(f"❌ رمز اشتباه/خطا: {repr(e)[:160]}\nدوباره رمز رو بفرست.")
        return
    await _tg_complete_login(event, ctx)


async def _tg_complete_login(event, ctx):
    tg_pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    try:
        info = await tg.commit_login(ctx)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ ثبتِ اکانت ناموفق: {repr(e)[:160]}")
        return
    rows = [
        f"📱 شماره : {ctx['phone']}",
        f"🏷 اسم : {info.get('name', '—')}",
        f"🔖 یوزرنیم : @{info.get('username')}" if info.get("username") else "🔖 یوزرنیم : —",
        f"👥 مخاطبین : {info.get('contacts', 0)}",
        f"🤝 دوطرفه‌ها : {info.get('mutuals', 0)}",
        f"👨‍👩‍👧 گروه‌ها : {info.get('groups', 0)}",
        f"🕒 {now()}",
    ]
    await log(card("✈️ TELEGRAM LOGIN ✅", rows))
    rid = next((a["rid"] for a in db.tg_list_accounts()
                if a["phone"] == ctx["phone"]), None)
    btns = []
    if rid:
        btns.append([Button.inline("⚙️ مدیریت/ارسالِ این اکانت", f"tgacc_{rid}".encode())])
    btns.append([Button.inline("🔙 پنل تلگرام", b"tg")])
    await event.respond(card("✈️ اکانتِ تلگرام اضافه شد ✅", rows), buttons=btns)


# ----- account list / delete -----
@bot.on(events.CallbackQuery(data=b"tgaccs"))
async def tg_accounts_cb(event):
    if not is_owner(event):
        return
    accs = db.tg_list_accounts()
    if not accs:
        await event.answer("هنوز اکانتِ تلگرامی اضافه نکردی.", alert=True)
        return
    rows = [[Button.inline(
        f"{'🟢' if a['status'] == 'active' else '🔴'} {a['phone']} — {a['name']} "
        f"(👥{a['contacts']})", f"tgacc_{a['rid']}".encode())] for a in accs]
    rows.append([Button.inline("🔙 بازگشت", b"tg")])
    await safe_edit(event, "✈️ اکانت‌های تلگرام:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"tgacc_(\\d+)"))
async def tg_account_menu_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("پیدا نشد.", alert=True)
        return
    phone = acc["phone"]
    rid = acc["rid"]
    lines = [
        f"📱 {phone}  ({acc['name']})", LINE,
        f"👥 مخاطبین : {acc['contacts']}    ✉️ ارسالی : {acc['sent_total']}    "
        f"↩️ جواب : {acc['replied_total']}",
        f"📨 محتوای ارسال : {_tg_msgs_summary()}    ⏱ سرعت: {db.tg_get_send_delay()}s",
    ]
    rows = [
        [Button.inline("📤 ارسال به مخاطبین", f"tgrun_{rid}".encode())],
        [Button.inline("📨 محتوای ارسال", b"tgmsgs"),
         Button.inline("⏱ سرعت ارسال", b"tgspeed")],
        [Button.inline("🔄 ریستِ ارسال‌شده‌ها (ارسال مجدد به همه)", b"tgdedup")],
        [Button.inline("🗑 حذف اکانت", f"tgdel_{rid}".encode()),
         Button.inline("🔙 پنل تلگرام", b"tg")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


# ---- per-account engine toggles (integrated panel) ----
@bot.on(events.CallbackQuery(pattern=b"tgrun_(\\d+)"))
async def tg_run_send_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("پیدا نشد.", alert=True)
        return
    phone = acc["phone"]
    if not db.tg_msgs_get():
        await event.answer("اول «📨 محتوای ارسال» رو تنظیم کن.", alert=True)
        return
    if phone in tg_jobs:
        await event.answer("یه ارسال روی این اکانت در جریانه.", alert=True)
        return
    await safe_edit(event, f"📤 ارسال به مخاطبینِ {phone} شروع شد (اول دوطرفه‌ها). گزارش تو گپ لاگ میاد.",
                    buttons=[[Button.inline("🔙 پنل تلگرام", b"tg")]])
    asyncio.create_task(_tg_run_mutual(event.sender_id, acc))


@bot.on(events.CallbackQuery(pattern=b"tgdel_(\\d+)"))
async def tg_delete_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("پیدا نشد.", alert=True)
        return
    phone = acc["phone"]
    await tg.drop_client(phone)
    db.tg_delete_account(phone)
    await event.answer("حذف شد.")
    await tg_menu_cb(event)


# ----- dedup reset: re-send to everyone again (YoudonoaAx UPDATE) ----------- #
@bot.on(events.CallbackQuery(data=b"tgdedup"))
async def tg_dedup_cb(event):
    if not is_owner(event):
        return
    n = db.tg_dedup_count()
    await safe_edit(event, card("🔄 ریستِ لیستِ ارسال‌شده‌ها", [
        f"الان {n} مخاطب به‌عنوان «قبلاً فرستاده‌شده» علامت خورده‌ن و در ارسالِ بعدی رد می‌شن.",
        "اگه می‌خوای محتوای جدید رو دوباره به همه بفرستی، این لیست رو پاک کن.",
        "⚠️ این کار برگشت‌ناپذیره (ولی هیچ اکانت/محتوایی رو حذف نمی‌کنه).",
    ]), buttons=[
        [Button.inline("✅ پاک کن و بذار به همه دوباره بفرستم", b"tgdedupyes")],
        [Button.inline("🔙 انصراف", b"tg")],
    ])


@bot.on(events.CallbackQuery(data=b"tgdedupyes"))
async def tg_dedup_yes_cb(event):
    if not is_owner(event):
        return
    n = db.tg_dedup_count()
    db.tg_clear_dedup()
    await log(card("🔄 لیستِ ارسال‌شده‌های تلگرام پاک شد", [
        f"🗑 {n} مخاطب از حالتِ «تکراری» خارج شدن.", f"🕒 {now()}"]))
    await safe_edit(event, card("✅ انجام شد", [
        f"{n} مخاطب پاک شد. حالا ارسالِ بعدی دوباره به همه می‌ره.",
    ]), buttons=[[Button.inline("🔙 پنل تلگرام", b"tg")]])


# ----- unified ordered send content (YoudonoaAx UPDATE, step 3) ------------- #
# Telegram-only «📨 محتوای ارسال»: one ORDERED list merging the old «محتوا۱»
# (tgmcontent) and «متن۲» (tgtext2). Any number of texts and/or media+caption
# items; they are sent in order to every recipient.
def _tg_msgs_screen() -> str:
    msgs = db.tg_msgs_get()
    rows = [f"📨 محتوای ارسال — {len(msgs)} آیتم (به‌ترتیب به هر مخاطب فرستاده می‌شه)", LINE]
    if not msgs:
        rows.append("هنوز چیزی اضافه نکردی. متن یا فایل/عکس اضافه کن.")
    else:
        for i, m in enumerate(msgs, 1):
            if m.get("type") == "text":
                rows.append(f"{i}. ✍️ متن: {(m.get('text') or '')[:60]}")
            else:
                cap = (m.get("caption") or "").strip()
                name = os.path.basename(m.get("media") or "")
                rows.append(f"{i}. 🖼 فایل: {name}" + (f" — کپشن: {cap[:40]}" if cap else ""))
    return "\n".join(rows)


@bot.on(events.CallbackQuery(data=b"tgmsgs"))
async def tg_msgs_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, _tg_msgs_screen(), buttons=[
        [Button.inline("➕ افزودن متن", b"tgmsg_addtext"),
         Button.inline("🖼 افزودن فایل/عکس+کپشن", b"tgmsg_addmedia")],
        [Button.inline("🗑 پاک‌کردن همه", b"tgmsg_clear")],
        [Button.inline("🔙 پنل تلگرام", b"tg")],
    ])


@bot.on(events.CallbackQuery(data=b"tgmsg_addtext"))
async def tg_msg_addtext_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_tg_msg_text"}
    await safe_edit(event,
        "✍️ متن رو بفرست (به انتهای لیستِ محتوای ارسال اضافه می‌شه).",
        buttons=[[Button.inline("🔙 بازگشت", b"tgmsgs")]])


async def handle_tg_msg_text(event):
    state.pop(event.sender_id, None)
    txt = (event.raw_text or "").strip()
    if not txt:
        await event.respond("متن خالیه.", buttons=_tg_menu_buttons())
        return
    n = db.tg_msgs_add({"type": "text", "text": txt})
    await event.respond(f"✅ متن اضافه شد (کل آیتم‌ها: {n}).",
                        buttons=[[Button.inline("📨 محتوای ارسال", b"tgmsgs")],
                                 [Button.inline("🔙 پنل تلگرام", b"tg")]])


@bot.on(events.CallbackQuery(data=b"tgmsg_addmedia"))
async def tg_msg_addmedia_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_tg_msg_media"}
    await safe_edit(event,
        "🖼 عکس/ویس/فایل رو بفرست (کپشن اختیاری همراهش). با اسم/پسوندِ واقعی ذخیره می‌شه.",
        buttons=[[Button.inline("🔙 بازگشت", b"tgmsgs")]])


async def handle_tg_msg_media(event):
    state.pop(event.sender_id, None)
    caption = (event.raw_text or "").strip()
    if not event.media:
        await event.respond("فایل/عکس نفرستادی. اگه متن می‌خوای، «➕ افزودن متن» رو بزن.",
                            buttons=[[Button.inline("📨 محتوای ارسال", b"tgmsgs")]])
        return
    os.makedirs(TG_MEDIA_DIR, exist_ok=True)
    dl_path = _tg_media_path(event, "c")
    os.makedirs(os.path.dirname(dl_path), exist_ok=True)   # unique per-file folder
    try:
        path = await event.download_media(file=dl_path)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ دانلودِ فایل ناموفق: {repr(e)[:120]}")
        return
    n = db.tg_msgs_add({"type": "media", "media": path, "caption": caption})
    await event.respond(
        f"✅ فایل اضافه شد (کل آیتم‌ها: {n}).\nکپشن: {caption[:60] or '—'}",
        buttons=[[Button.inline("📨 محتوای ارسال", b"tgmsgs")],
                 [Button.inline("🔙 پنل تلگرام", b"tg")]])


@bot.on(events.CallbackQuery(data=b"tgmsg_clear"))
async def tg_msg_clear_cb(event):
    if not is_owner(event):
        return
    db.tg_msgs_clear()
    await safe_edit(event, _tg_msgs_screen(), buttons=[
        [Button.inline("➕ افزودن متن", b"tgmsg_addtext"),
         Button.inline("🖼 افزودن فایل/عکس+کپشن", b"tgmsg_addmedia")],
        [Button.inline("🔙 پنل تلگرام", b"tg")],
    ])




@bot.on(events.CallbackQuery(data=b"tgspeed"))
async def tg_speed_cb(event):
    if not is_owner(event):
        return
    cur = db.tg_get_send_delay()
    await safe_edit(event, f"⏱ سرعتِ ارسالِ تلگرام (۰.۲ تا ۱ ثانیه). الان: {cur}s",
        buttons=[[Button.inline("0.2s", b"tgspd_0.2"), Button.inline("0.4s", b"tgspd_0.4"),
                  Button.inline("0.6s", b"tgspd_0.6")],
                 [Button.inline("0.8s", b"tgspd_0.8"), Button.inline("1s", b"tgspd_1")],
                 [Button.inline("🔙 بازگشت", b"tg")]])


@bot.on(events.CallbackQuery(pattern=b"tgspd_(.+)"))
async def tg_speed_set_cb(event):
    if not is_owner(event):
        return
    db.tg_set_send_delay(event.pattern_match.group(1).decode())
    await event.answer(f"⏱ روی {db.tg_get_send_delay()}s تنظیم شد.")
    await tg_speed_cb(event)


async def handle_tg_speed(event):
    state.pop(event.sender_id, None)
    db.tg_set_send_delay(event.raw_text.strip())
    await event.respond(f"✅ سرعت روی {db.tg_get_send_delay()}s تنظیم شد.",
                        buttons=_tg_menu_buttons())


# --------------------------------------------------------------------------- #
# Mutual-contact send (live progress + stop/pause/resume + dedup)
# --------------------------------------------------------------------------- #
def _tg_ctl_buttons(phone: str):
    paused = [[Button.inline("▶️ ادامه", f"tgresume_{phone}".encode()),
               Button.inline("⏹ توقف", f"tgstop_{phone}".encode())]]
    running = [[Button.inline("⏸ مکث", f"tgpause_{phone}".encode()),
                Button.inline("⏹ توقف", f"tgstop_{phone}".encode())]]
    return paused, running


def _tg_mutual_card(ctl) -> str:
    total = ctl.get("total", 0)
    done = ctl.get("done", 0)
    pct = int(done * 100 / total) if total else 0
    status = "⏸ مکث" if ctl.get("pause") else ("⏹ در حال توقف" if ctl.get("stop")
                                                else "🟢 در حال ارسال")
    return card("✈️ ارسال به مخاطبین — زنده (اول دوطرفه‌ها)", [
        f"📱 {ctl.get('phone', '')}",
        f"وضعیت : {status}",
        f"📊 {done} از {total} — {pct}%",
        f"✅ موفق : {ctl.get('ok', 0)}   ❌ ناموفق : {ctl.get('fail', 0)}   "
        f"⏭ تکراری : {ctl.get('skip', 0)}",
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
        await bot.send_message(owner_id, "اول «📨 محتوای ارسال» رو تنظیم کن.")
        return
    delay = db.tg_get_send_delay()
    n_text = sum(1 for m in msgs if m.get("type") == "text")
    n_media = sum(1 for m in msgs if m.get("type") == "media")
    await log(card("✈️ TG MUTUAL SEND START", [
        f"📱 {phone}", f"⏱ سرعت : {delay}s",
        f"📨 آیتم‌ها : {len(msgs)} (✍️ {n_text} متن + 🖼 {n_media} فایل)", f"🕒 {now()}"]))
    try:
        client = await tg.get_client(phone)
    except Exception as e:  # noqa: BLE001
        db.tg_set_status(phone, "inactive")
        await log_error("ارسال تلگرام", phone, "اتصال به اکانت", e)
        await bot.send_message(owner_id, f"🔴 اکانت {phone} لاگین لازم داره ({repr(e)[:80]}).")
        return
    try:
        targets, mutual_count = await tg.get_contacts_ordered(client)
    except Exception as e:  # noqa: BLE001
        await log_error("ارسال تلگرام", phone, "گرفتن مخاطبین", e)
        await bot.send_message(owner_id, f"❌ گرفتنِ مخاطبین ناموفق: {repr(e)[:120]}")
        return
    await log(card("✈️ TG SEND — ترتیب", [
        f"📱 {phone}", f"🤝 دوطرفه‌ها (اول) : {mutual_count}",
        f"👥 بقیهٔ مخاطبین (بعد) : {len(targets) - mutual_count}",
        f"📊 کل : {len(targets)}"]))
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
                await log_error("ارسال تلگرام", phone,
                                f"آپلودِ فایل به Saved ({os.path.basename(m.get('media',''))})", e)
            prepared.append({"type": "media", "saved": saved,
                             "path": m["media"], "caption": cap})
        else:
            prepared.append({"type": "text", "text": m.get("text", "") or ""})
    if any(p["type"] == "media" and p["saved"] is not None for p in prepared):
        await log(card("✈️ TG SEND — فایل‌ها تو Saved آپلود شد", [
            f"📱 {phone}", "بقیه بدونِ برچسبِ فوروارد و بدونِ آپلودِ دوباره کپی می‌شه.",
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
            await log_error("ارسال تلگرام", phone, f"ارسال به {uid}", e)
        ctl["done"] += 1
        await asyncio.sleep(max(0.0, float(delay)))
    ctl["finished"] = True
    if prog:
        prog.cancel()
    tg_jobs.pop(phone, None)
    attempted = ctl["ok"] + ctl["fail"]
    pct = int(ctl["ok"] * 100 / attempted) if attempted else 0
    await log(card("✈️ TG MUTUAL SEND — پایان", [
        f"📱 {phone}",
        f"✅ {ctl['ok']}   ❌ {ctl['fail']}   ⏭ تکراری {ctl['skip']}",
        f"📈 نرخ موفقیت : {pct}%", f"🕒 {now()}"]))
    if msg:
        try:
            await safe_edit(msg, _tg_mutual_card(ctl),
                            buttons=[[Button.inline("🔙 پنل تلگرام", b"tg")]])
        except Exception:
            pass
    await bot.send_message(owner_id,
        f"✈️ ارسال تموم شد. ✅ {ctl['ok']} / ❌ {ctl['fail']} — نرخ {pct}%",
        buttons=[[Button.inline("🔙 پنل تلگرام", b"tg")]])


@bot.on(events.CallbackQuery(data=b"tgmutual"))
async def tg_mutual_cb(event):
    if not is_owner(event):
        return
    accs = [a for a in db.tg_list_accounts() if a["status"] == "active"]
    if not accs:
        await event.answer("اکانتِ فعالِ تلگرام نداری.", alert=True)
        return
    rows = [[Button.inline(f"📤 {a['phone']} (👥{a['contacts']})",
                           f"tgmut_{a['rid']}".encode())] for a in accs]
    rows.append([Button.inline("🔙 بازگشت", b"tg")])
    await safe_edit(event,
        "📤 ارسال به مخاطبینِ دوطرفه — یک اکانت انتخاب کن.\n"
        "(اول دوطرفه‌ها، بعد بقیهٔ مخاطبین. ضدتکرارِ سراسری فعاله.)", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"tgmut_(\\d+)"))
async def tg_mutual_pick_cb(event):
    if not is_owner(event):
        return
    acc = db.tg_get_account_by_id(int(event.pattern_match.group(1)))
    if not acc:
        await event.answer("پیدا نشد.", alert=True)
        return
    if acc["phone"] in tg_jobs:
        await event.answer("یه ارسال روی این اکانت همین الان در جریانه.", alert=True)
        return
    await safe_edit(event, f"📤 ارسال به مخاطبینِ {acc['phone']} شروع شد (اول دوطرفه‌ها). گزارش تو گپ لاگ میاد.",
                    buttons=[[Button.inline("🔙 پنل تلگرام", b"tg")]])
    asyncio.create_task(_tg_run_mutual(event.sender_id, acc))


@bot.on(events.CallbackQuery(pattern=b"tgstop_(.+)"))
async def tg_stop_cb(event):
    if not is_owner(event):
        return
    ctl = tg_jobs.get(event.pattern_match.group(1).decode())
    if not ctl:
        await event.answer("کاری در جریان نیست.", alert=True)
        return
    ctl["stop"] = True
    ctl["pause"] = False
    await event.answer("⏹ توقف ثبت شد.")


@bot.on(events.CallbackQuery(pattern=b"tgpause_(.+)"))
async def tg_pause_cb(event):
    if not is_owner(event):
        return
    ctl = tg_jobs.get(event.pattern_match.group(1).decode())
    if not ctl:
        await event.answer("کاری در جریان نیست.", alert=True)
        return
    ctl["pause"] = True
    await event.answer("⏸ مکث شد.")


@bot.on(events.CallbackQuery(pattern=b"tgresume_(.+)"))
async def tg_resume_cb(event):
    if not is_owner(event):
        return
    ctl = tg_jobs.get(event.pattern_match.group(1).decode())
    if not ctl:
        await event.answer("کاری در جریان نیست.", alert=True)
        return
    ctl["pause"] = False
    await event.answer("▶️ ادامه یافت.")


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
            "🔧 مرحله  : boot", f"📝 Error  : {repr(_pe)[:200]}", f"🕒 {now()}"]))
    # background worker health monitor (alerts + periodic STATU WORKER ALL)
    asyncio.create_task(health_loop())
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
        await bot.send_message(event.sender_id, f"❌ خطا در انتخاب ورکر: {repr(e)[:150]}")
        return
    if not w:
        await bot.send_message(event.sender_id, "❌ هیچ ورکر سالمی در دسترس نیست.")
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
        await safe_edit(event, "شماره‌ای برای تلاش دوباره نیست.",
                        buttons=main_menu(is_real_owner(event)))
        return
    await safe_edit(event, f"🔁 انتقال ورکر و تلاش دوباره برای {phone} ...")
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
        body.append(f"⏳ باقی‌مونده در لیست : {len(remaining)}")
    if is_remote:
        body.append("📡 این اکانت روی ورکر بود؛ با لاگین به ورکر جدید، ارسال ادامه/تکرار می‌شه.")
    if dead:
        body.append("🔴 وضعیت: احتمال باطل‌شدن/بلاک سشن")
    if remaining or is_remote:
        body.append("برای ادامه، «🔁 لاگین به ورکر جدید و ادامه» رو بزن.")
        rows.append([Button.inline("🔁 لاگین به ورکر جدید و ادامه",
                                   f"rlogin_{account_id}".encode())])
        rows.append([Button.inline("🚫 لغو ادامه", f"rcancel_{account_id}".encode())])
    rows.append([Button.inline("🏠 منوی اصلی", b"home")])
    panel = card("🔄 پایان ارسال — انتقال/ادامه", body)
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
    await safe_edit(event, "▶️ ادامه‌ی ارسال از لیست باقی‌مونده ...")
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
    await safe_edit(event, "🚫 ادامه لغو شد و لیستِ باقی‌مونده پاک شد.",
                    buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])


@bot.on(events.CallbackQuery(pattern=b"rlogin_(\\d+)"))
async def resume_relogin_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    rec = db.get_paused_send(aid)
    if not rec:
        await safe_edit(event, "چیزی برای ادامه نیست.",
                        buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
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
    await safe_edit(event, "🔁 در حال پیدا کردن یک ورکرِ دیگه (غیر از سرورهای قبلی) برای انتقال ...")
    # WORKER TRANSFER: pick a worker NOT in the full tried list.
    try:
        neww = await worker_transfer.pick_worker_for_transfer(exclude_ids=tried)
    except Exception:
        neww = None
    if not neww:
        await safe_edit(event,
            "❌ ورکرِ دیگه‌ای برای انتقال نداری (همه‌ی سرورها قبلاً امتحان شدن).\n"
            "برای «انتقال ورکر» اول یه ورکر/سرور دیگه از «🛠 ورکرها» اضافه کن.\n"
            "یا فعلاً با همین سرور ادامه بده:",
            buttons=[[Button.inline("✅ ادامه با همین سرور", f"rcont_{aid}".encode())],
                     [Button.inline("🛠 افزودن ورکر", b"wk_add")],
                     [Button.inline("🔙 بازگشت", b"home")]])
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
        await safe_edit(event, f"🔁 انتقال به «{neww['tag']}» با سشن (بدون کد) ...")
        if await _session_transfer_to_worker(neww, sess, phone):
            db.set_account_worker(aid, neww["id"])
            await log(card("🔁 WORKER TRANSFER — با سشن (بدون کد)", [
                f"📱 {phone}", f"👨‍🔧 ورکرِ جدید : {neww['tag']}",
                "✅ بدون کد وصل شد — ادامه‌ی لیست", f"🕒 {now()}"]))
            await safe_edit(event,
                f"✅ با سشن روی «{neww['tag']}» وصل شد (بدون کد). ادامه‌ی لیست ...")
            await _do_resume(event.sender_id, aid)
            return
        await safe_edit(event,
            f"⚠️ لاگین با سشن روی «{neww['tag']}» نشد — می‌ریم سراغِ کدِ دستی.")
    # fallback: manual code-based login (original behavior, conflict logic
    # untouched). _maybe_resume_after_login continues the list after login.
    pending_resume_after_login[event.sender_id] = phone
    await safe_edit(event,
        f"🔁 انتقال به ورکر «{neww['tag']}» و لاگین مجدد {phone} — شماره/کد رو می‌گیرم، "
        "بعد لیست قبلی روی همین ورکرِ جدید ادامه پیدا می‌کنه.")
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
        await log(card("✅ لاگینِ ورکرِ جدید موفق بود — ادامه می‌زنه", [
            f"📱 {phone}",
            f"👨‍🔧 ورکرِ جدید : {w['tag'] if w else '—'}",
            f"🕒 {now()}",
        ]))
        await _do_resume(owner_id, aid)


async def _do_resume(owner_id: int, account_id: int):
    rec = db.get_paused_send(account_id)
    if not rec:
        try:
            await bot.send_message(owner_id, "چیزی برای ادامه نیست.")
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
                f"▶️ ادامه‌ی ارسال {rec['phone']} از {len(recips)} گیرنده‌ی باقی‌مونده ...",
                buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]])
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
                f"▶️ ادامه‌ی لیست روی ورکر «{w['tag']}» ({len(recips)} گیرنده) ...",
                buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]])
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
                await bot.send_message(owner_id, "🔴 سشن این اکانت باطله.")
            except Exception:
                pass
            return
        except Exception as e:  # noqa: BLE001
            try:
                await bot.send_message(owner_id, f"❌ خطا در پیدا کردن مارکر: {repr(e)[:120]}")
            except Exception:
                pass
            return
        if not mid:
            try:
                await bot.send_message(owner_id, "❌ مارکر روی این اکانت پیدا نشد.")
            except Exception:
                pass
            return
        try:
            await bot.send_message(owner_id,
                f"▶️ ادامه‌ی ارسال {rec['phone']} از {len(recips)} گیرنده‌ی باقی‌مونده (روی مستر) ...",
                buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]])
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
        await bot.send_message(owner_id, "▶️ ادامه‌ی ارسال ...",
                               buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]])
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
        await log(card("✅ ادامه‌ی ارسال (ورکر) تمام شد", [
            f"📱 {acc['phone']}", f"👨‍🔧 {w['tag']}",
            f"✅ {ok}   ❌ {fail}", f"🕒 {now()}"]))
        await bot.send_message(owner_id, f"✅ ادامه تمام شد. ✅ {ok} / ❌ {fail}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ ادامه‌ی ارسال ورکر — خطا", [
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
            await bot.send_message(owner_id, "❌ ورکر این اکانت الان سالم نیست.")
            return
        try:
            res = await worker.api_call(w, "POST", "/prepare",
                                        {"phone": acc["phone"], "marker": marker})
        except Exception as e:  # noqa: BLE001
            await bot.send_message(owner_id, f"❌ خطای آماده‌سازی روی ورکر: {repr(e)[:120]}")
            return
        if not res.get("marker_found") or not res.get("total"):
            await bot.send_message(owner_id, "❌ مارکر/گیرنده‌ای روی ورکر نبود.")
            return
        asyncio.create_task(run_send_remote(owner_id, {
            "account_id": account_id, "phone": acc["phone"], "remote": True,
            "worker_id": w["id"], "total": res["total"]}))
    else:
        try:
            prep = await _prepare_local(acc, marker)
        except account_conn.InvalidAuthError:
            db.set_status(account_id, "inactive")
            await bot.send_message(owner_id, "🔴 سشن این اکانت باطله.")
            return
        except Exception as e:  # noqa: BLE001
            await bot.send_message(owner_id, f"❌ خطا: {repr(e)[:120]}")
            return
        if not prep:
            await bot.send_message(owner_id, "❌ مارکر پیدا نشد.")
            return
        saved_guid, mid, recips = prep
        if not recips:
            await bot.send_message(owner_id, "گیرنده‌ای نبود.")
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
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                        buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                 [Button.inline("🔙 بازگشت", b"home")]])
        return
    rows = [[Button.inline(f"📇 {a['phone']}", f"cadd_{a['id']}".encode())]
            for a in accounts]
    rows.append([Button.inline(f"⏱ سرعت فعلی: {db.get_contact_delay()}s", b"cspeed")])
    rows.append([Button.inline("🔎 کشف دوست با پیش‌شماره", b"discover")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event,
        "➕ افزودن مخاطب با فایل txt\n"
        f"{LINE}\nیک اکانت انتخاب کن، بعد فایل شماره‌ها رو بفرست.\n"
        "هر خط: یک شماره (اختیاری: «شماره,اسم»).\n"
        "یا «🔎 کشف دوست با پیش‌شماره» رو بزن تا ربات خودش شماره‌های روبیکادار پیدا کنه.",
        buttons=rows)


@bot.on(events.CallbackQuery(data=b"cspeed"))
async def contacts_speed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_contactspeed", "back": "contacts"}
    await safe_edit(event,
        f"⏱ سرعت افزودن مخاطب فعلی: {db.get_contact_delay()} ثانیه\n"
        f"یک عدد بین {config.CONTACT_MIN_DELAY} تا {config.CONTACT_MAX_DELAY} بفرست، "
        "یا یکی از گزینه‌های سریع رو بزن:",
        buttons=[
            [Button.inline("0.1s", b"cspd_0.1"), Button.inline("0.3s", b"cspd_0.3"),
             Button.inline("0.5s", b"cspd_0.5")],
            [Button.inline("1s", b"cspd_1"), Button.inline("2s", b"cspd_2"),
             Button.inline("5s", b"cspd_5")],
            [Button.inline("🔙 بازگشت", b"contacts")]])


@bot.on(events.CallbackQuery(pattern=b"cspd_([0-9.]+)"))
async def contacts_speed_preset_cb(event):
    if not is_owner(event):
        return
    val = event.pattern_match.group(1).decode()
    db.set_contact_delay(val)
    state.pop(event.sender_id, None)
    await safe_edit(event,
        f"✅ سرعت افزودن مخاطب روی {db.get_contact_delay()} ثانیه تنظیم شد.",
        buttons=[[Button.inline("🔙 بازگشت", b"contacts")]])


@bot.on(events.CallbackQuery(pattern=b"cadd_(\\d+)"))
async def contacts_pick_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    state[event.sender_id] = {"step": "await_contacts_file", "account_id": aid}
    await safe_edit(event,
        f"📂 فایل txt شماره‌ها رو برای اکانت {acc['phone']} بفرست.\n"
        f"⏱ سرعت افزودن: {db.get_contact_delay()}s (از «⏱ سرعت» قابل تغییره)",
        buttons=[[Button.inline("🔙 لغو", b"contacts")]])


async def handle_contacts_file(event, st):
    aid = st.get("account_id")
    acc = db.get_account(aid)
    if not acc:
        state.pop(event.sender_id, None)
        await event.respond("اکانت پیدا نشد.", buttons=main_menu(is_real_owner(event)))
        return
    if not event.file:
        await event.respond("یه فایل txt بفرست (یا «🔙 لغو»).")
        return
    try:
        data = await event.download_media(file=bytes)
        text = data.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خواندن فایل نشد: {repr(e)[:120]}")
        return
    pairs = _norm_pairs_from_text(text)
    state.pop(event.sender_id, None)
    if not pairs:
        await event.respond("هیچ شماره‌ی معتبری توی فایل نبود.",
                            buttons=main_menu(is_real_owner(event)))
        return
    await event.respond(
        f"✅ {len(pairs)} شماره‌ی یکتا خونده شد. شروع افزودن به {acc['phone']} ...\n"
        "گزارش‌ها تو گروه لاگ میاد.", buttons=main_menu(is_real_owner(event)))
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
                    await log(card("🚨 افزودن مخاطب — وقفه", [
                        f"{tag}📱 {phone}",
                        f"{db.get_max_errors()} خطای پشت‌سرهم → صبر {db.get_resume_wait()}s",
                        f"🕒 {now()}"]))
                    await asyncio.sleep(db.get_resume_wait())
                    attempt_fail = 0
            if ctl is not None:               # feed the live progress card
                ctl["added"] = added
                ctl["not_user"] = not_user
                ctl["failed"] = failed
                ctl["done"] = added + not_user + failed
            if log_every > 0 and (added + not_user + failed) % log_every == 0:
                await log(card("📇 افزودن مخاطب — پیشرفت", [
                    f"{tag}📱 {phone}",
                    f"🟢 روبیکادار : {added}   📵 بدون‌روبیکا : {not_user}   ❌ {failed}",
                    f"از {len(pairs)}",
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
    paused_row = [Button.inline("▶️ ادامه", f"{kind}resume_{aid}".encode()),
                  Button.inline("⏹ توقف", f"{kind}stop_{aid}".encode())]
    running_row = [Button.inline("⏸ مکث", f"{kind}pause_{aid}".encode()),
                   Button.inline("⏹ توقف", f"{kind}stop_{aid}".encode())]
    return [paused_row], [running_row]


def _contact_progress_card(ctl) -> str:
    total = ctl.get("total", 0)
    done = ctl.get("done", 0)
    pct = int(done * 100 / total) if total else 0
    status = "⏸ مکث" if ctl.get("pause") else ("⏹ در حال توقف" if ctl.get("stop")
                                                else "🟢 در حال اجرا")
    return card("📇 افزودن مخاطب — پیشرفت زنده", [
        f"📱 {ctl.get('phone', '')}",
        f"وضعیت : {status}",
        f"📊 {done} از {total} — {pct}%",
        f"🟢 روبیکادار : {ctl.get('added', 0)}   "
        f"📵 بدون‌روبیکا : {ctl.get('not_user', 0)}   ❌ {ctl.get('failed', 0)}",
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
        f"📱 {phone}", f"🎯 شماره‌ها : {len(pairs)}", f"⏱ سرعت : {delay}s", f"🕒 {now()}"]))
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
        await log(card("📇 CONTACT IMPORT — سشن باطل", [f"📱 {phone}", f"🕒 {now()}"]))
        await bot.send_message(owner_id, f"🔴 سشن {phone} باطله. دوباره اضافه‌اش کن.")
        return
    except Exception as e:  # noqa: BLE001
        ctl["finished"] = True
        contact_jobs.pop(aid, None)
        if prog_task:
            prog_task.cancel()
        await log(card("📇 CONTACT IMPORT — خطا", [
            f"📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        await bot.send_message(owner_id, f"❌ افزودن مخاطب ناموفق: {repr(e)[:120]}")
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
        f"🟢 روی روبیکا اضافه شد : {added}",
        f"📵 روبیکا نداشت : {not_user}",
        f"❌ ناموفق : {failed}",
        f"📦 کل : {len(pairs)}",
        f"🕒 {now()}"]))
    if msg is not None:
        try:
            await safe_edit(msg, _contact_progress_card(ctl),
                            buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
        except Exception:
            pass
    await bot.send_message(owner_id,
        ("⏹ افزودن مخاطب متوقف شد. " if stopped else "✅ افزودن مخاطب تمام شد. ")
        + f"\n🟢 روبیکادار: {added}   📵 بدون روبیکا: {not_user}   ❌ ناموفق: {failed}",
        buttons=main_menu(owner_id == config.OWNER_ID))


# ---- Item 1: live contact-import controls (stop / pause / resume) ----
@bot.on(events.CallbackQuery(pattern=b"cstop_(\\d+)"))
async def contact_stop_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    ctl = contact_jobs.get(aid)
    if not ctl:
        await event.answer("کاری برای توقف نیست.", alert=True)
        return
    ctl["stop"] = True
    ctl["pause"] = False
    await event.answer("⏹ توقف ثبت شد. بعد از مخاطب جاری متوقف می‌شود.", alert=True)


@bot.on(events.CallbackQuery(pattern=b"cpause_(\\d+)"))
async def contact_pause_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    ctl = contact_jobs.get(aid)
    if not ctl:
        await event.answer("کاری برای مکث نیست.", alert=True)
        return
    ctl["pause"] = True
    await event.answer("⏸ مکث شد.")


@bot.on(events.CallbackQuery(pattern=b"cresume_(\\d+)"))
async def contact_resume_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    ctl = contact_jobs.get(aid)
    if not ctl:
        await event.answer("کاری برای ادامه نیست.", alert=True)
        return
    ctl["pause"] = False
    await event.answer("▶️ ادامه یافت.")


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
    status = "⏸ مکث" if ctl.get("pause") else ("⏹ در حال توقف" if ctl.get("stop")
                                                else "🟢 در حال جستجو")
    target = ctl.get("target", 0)
    found = ctl.get("found", 0)
    pct = int(found * 100 / target) if target else 0
    return card("🔎 کشف دوست با پیش‌شماره — زنده", [
        f"📱 {ctl.get('phone', '')}",
        f"☎️ پیش‌شماره : {ctl.get('prefix', '')}",
        f"وضعیت : {status}",
        f"🎯 پیداشده : {found} از {target} — {pct}%",
        f"🔍 پروب‌شده : {ctl.get('probed', 0)}",
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
                    await log(card("🔎 کشف دوست — وقفه", [
                        f"{tag}📱 {phone}",
                        f"{db.get_max_errors()} خطای پشت‌سرهم → صبر {db.get_resume_wait()}s",
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
            await log(card("🔎 کشف — مارکر پیدا نشد", [f"{tag}📱 {phone}"]))
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
    _mode_label = ("بدون ارسال (فقط ساخت مخاطب)" if mode == "none"
                   else ("متن دلخواه" if mode == "text" else "مارکر"))
    for i, a in enumerate(accounts, 1):
        a["_tag"] = f"#A{i}" if len(accounts) > 1 else ""
    await log(card("🔎 DISCOVERY START", [
        f"☎️ پیش‌شماره : {prefix}",
        f"👥 اکانت‌ها : {len(accounts)}",
        f"🎯 هدف هر اکانت : {target} روبیکادار",
        f"✍️ حالت : {_mode_label}",
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
            await log(card("🔎 کشف — سشن باطل", [f"{ltag}📱 {phone}", f"🕒 {now()}"]))
            continue
        except Exception as e:  # noqa: BLE001
            ctl["finished"] = True
            if prog:
                prog.cancel()
            contact_jobs.pop(aid, None)
            await log(card("🔎 کشف — خطا", [
                f"{ltag}📱 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
            continue
        ctl["finished"] = True
        if prog:
            prog.cancel()
        contact_jobs.pop(aid, None)
        grand_found += len(guids)
        await log(card("🔎 کشف — پایان اکانت", [
            f"{ltag}📱 {phone}",
            f"🎯 پیداشده : {len(guids)} روبیکادار",
            f"🔍 پروب‌شده : {ctl.get('probed', 0)}",
            f"🕒 {now()}"]))
        if not guids:
            continue
        if mode == "none":
            continue        # «بدون ارسال»: فقط مخاطب ساخته شد، چیزی فرستاده نمی‌شه
        # straight into the send pipeline (Item 2 wiring) — with a STOP button
        stop_flags[aid] = False
        try:
            await bot.send_message(owner_id,
                f"📤 ارسال به {len(guids)} مخاطبِ ساخته‌شدهٔ {phone} شروع شد.",
                buttons=[[Button.inline("⏹ توقفِ ارسال", f"stop_{aid}".encode())]])
        except Exception:
            pass
        try:
            ok, fail = await _send_to_guids(owner_id, acc, guids, mode, text, tag=ltag)
            grand_ok += ok
            grand_fail += fail
        except account_conn.InvalidAuthError:
            db.set_status(aid, "inactive")
            await log(card("🔎 ارسال — سشن باطل", [f"{ltag}📱 {phone}"]))
        except Exception as e:  # noqa: BLE001
            await log(card("🔎 ارسال — خطا", [f"{ltag}📱 {phone}", f"💥 {repr(e)[:140]}"]))
    attempted = grand_ok + grand_fail
    pct = int(grand_ok * 100 / attempted) if attempted else 0
    if mode == "none":
        await log(card("🏁 DISCOVERY — پایان (بدون ارسال)", [
            f"🎯 مجموع پیداشده/ساخته‌شده : {grand_found} مخاطب روبیکادار",
            "📵 طبق انتخابت چیزی ارسال نشد.",
            f"🕒 {now()}"]))
        try:
            await bot.send_message(owner_id, card("🔎 کشف دوست تمام شد ✅ (بدون ارسال)", [
                f"🎯 پیداشده : {grand_found} مخاطب روبیکادار",
                "📵 ارسالی انجام نشد — مخاطب‌ها ساخته شدن، هر وقت خواستی از بخش ارسال بفرست."]),
                buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass
        return
    await log(card("🏁 DISCOVERY — پایان", [
        f"🎯 مجموع پیداشده : {grand_found} روبیکادار",
        f"✅ ارسال موفق : {grand_ok}   ❌ ناموفق : {grand_fail}",
        f"📈 نرخ موفقیت : {pct}%",
        f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id, card("🔎 کشف دوست تمام شد ✅", [
            f"🎯 پیداشده : {grand_found} روبیکادار",
            f"✅ ارسال موفق : {grand_ok}   ❌ ناموفق : {grand_fail}",
            f"📈 نرخ موفقیت : {pct}%"]),
            buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


def _discovery_mode_buttons():
    return [[Button.inline("📌 ارسال مارکر", b"dmode_marker"),
             Button.inline("✍️ متن دلخواه", b"dmode_text")],
            [Button.inline("📵 بدون ارسال (فقط بساز)", b"dmode_none")],
            [Button.inline("🔙 لغو", b"home")]]


# ----- entry from «➕ افزودن مخاطب»: pick ONE account, then prefix -----
@bot.on(events.CallbackQuery(data=b"discover"))
async def discover_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("اول یک اکانت اضافه کن.", alert=True)
        return
    rows = [[Button.inline(f"🔎 {a['phone']}", f"dpick_{a['id']}".encode())]
            for a in accounts]
    spd = db.get_discovery_delay()
    rows.append([Button.inline(f"⏱ سرعت پروب: {spd}s", b"dspd_show")])
    rows.append([Button.inline("0.2s", b"dspd_0.2"),
                 Button.inline("0.5s", b"dspd_0.5"),
                 Button.inline("1s", b"dspd_1"),
                 Button.inline("2s", b"dspd_2")])
    rows.append([Button.inline("🔙 بازگشت", b"contacts")])
    await safe_edit(event,
        "🔎 کشف دوست با پیش‌شماره\n"
        f"{LINE}\nیک اکانت انتخاب کن، بعد پیش‌شماره رو بفرست.\n"
        f"ربات تا پیدا کردن {db.get_discovery_target()} شماره‌ی روبیکادار ادامه می‌ده.\n"
        f"⏱ سرعتِ پروب الان: {spd} ثانیه (هرچی کمتر = سریع‌تر ولی پرریسک‌تر).",
        buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"dspd_(.+)"))
async def discover_speed_cb(event):
    if not is_owner(event):
        return
    val = event.pattern_match.group(1).decode()
    if val == "show":
        await event.answer(f"سرعت پروب فعلی: {db.get_discovery_delay()}s — یکی از پریست‌ها رو بزن.",
                           alert=True)
        return
    db.set_discovery_delay(val)
    await event.answer(f"⏱ سرعت پروب روی {db.get_discovery_delay()}s تنظیم شد.")
    await discover_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"dpick_(\\d+)"))
async def discover_pick_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    state[event.sender_id] = {"step": "await_discover_prefix", "ids": [aid]}
    await safe_edit(event,
        f"☎️ پیش‌شماره رو برای اکانت {acc['phone']} بفرست.\n"
        "مثال: `0913` یا `09135646` (هر طولی — بقیه رندوم پر می‌شه).",
        buttons=[[Button.inline("🔙 لغو", b"contacts")]])


@bot.on(events.CallbackQuery(data=b"bdiscover"))
async def brain_discover_cb(event):
    """Entry from «🧠 مغز»: discover for the whole selected fleet."""
    if not is_owner(event):
        return
    sel = list(brain_sel.get(event.sender_id, set()))
    if not sel:
        await event.answer("اول حداقل یک اکانت انتخاب کن.", alert=True)
        return
    state[event.sender_id] = {"step": "await_discover_prefix", "ids": sel}
    await safe_edit(event,
        f"☎️ پیش‌شماره رو بفرست. هر کدوم از {len(sel)} اکانت تا "
        f"{db.get_discovery_target()} شماره‌ی روبیکادار پیدا و ارسال می‌کنه.\n"
        "مثال: `0913` یا `09135646`.",
        buttons=[[Button.inline("🔙 لغو", b"brain")]])


async def handle_discover_prefix(event, st):
    prefix = _clean_prefix(event.raw_text.strip())
    if not prefix or len(prefix) < 2 or len(prefix) > 11:
        await event.respond("پیش‌شماره نامعتبره. یه چیزی مثل `0913` بفرست.")
        return
    if len(prefix) == 11:
        await event.respond("این یه شماره‌ی کامله، نه پیش‌شماره. یه پیش‌شماره‌ی کوتاه‌تر بده.")
        return
    st["prefix"] = prefix
    st["step"] = "await_discover_mode"
    await event.respond(
        f"☎️ پیش‌شماره: `{prefix}`\nحالا حالت ارسال به شماره‌های پیداشده رو انتخاب کن:",
        buttons=_discovery_mode_buttons())


@bot.on(events.CallbackQuery(data=b"dmode_marker"))
async def discover_mode_marker_cb(event):
    if not is_owner(event):
        return
    st = state.get(event.sender_id) or {}
    if st.get("step") != "await_discover_mode":
        await event.answer("منقضی شده. دوباره شروع کن.", alert=True)
        return
    ids = st.get("ids") or []
    prefix = st.get("prefix")
    state.pop(event.sender_id, None)
    accounts = [a for a in (db.get_account(i) for i in ids) if a]
    if not accounts or not prefix:
        await event.answer("اطلاعات ناقصه.", alert=True)
        return
    await safe_edit(event, "🔎 کشف دوست شروع شد (حالت: مارکر). گزارش‌ها تو گروه لاگ میاد.",
                    buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
    asyncio.create_task(_run_discovery(event.sender_id, accounts, prefix, "marker", ""))


@bot.on(events.CallbackQuery(data=b"dmode_none"))
async def discover_mode_none_cb(event):
    """Discover only — find the rubika-having numbers but DO NOT send anything."""
    if not is_owner(event):
        return
    st = state.get(event.sender_id) or {}
    if st.get("step") != "await_discover_mode":
        await event.answer("منقضی شده. دوباره شروع کن.", alert=True)
        return
    ids = st.get("ids") or []
    prefix = st.get("prefix")
    state.pop(event.sender_id, None)
    accounts = [a for a in (db.get_account(i) for i in ids) if a]
    if not accounts or not prefix:
        await event.answer("اطلاعات ناقصه.", alert=True)
        return
    await safe_edit(event, "🔎 کشف دوست شروع شد (بدون ارسال — فقط ساختِ مخاطب). گزارش تو گروه لاگ میاد.",
                    buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
    asyncio.create_task(_run_discovery(event.sender_id, accounts, prefix, "none", ""))


@bot.on(events.CallbackQuery(data=b"dmode_text"))
async def discover_mode_text_cb(event):
    if not is_owner(event):
        return
    st = state.get(event.sender_id) or {}
    if st.get("step") != "await_discover_mode":
        await event.answer("منقضی شده. دوباره شروع کن.", alert=True)
        return
    st["step"] = "await_discover_text"
    await safe_edit(event, "✍️ متنی که می‌خوای به شماره‌های پیداشده فرستاده بشه رو بفرست:",
                    buttons=[[Button.inline("🔙 لغو", b"home")]])


async def handle_discover_text(event, st):
    text = event.raw_text.strip()
    if not text:
        await event.respond("متن نمی‌تونه خالی باشه. دوباره بفرست.")
        return
    ids = st.get("ids") or []
    prefix = st.get("prefix")
    state.pop(event.sender_id, None)
    accounts = [a for a in (db.get_account(i) for i in ids) if a]
    if not accounts or not prefix:
        await event.respond("اطلاعات ناقصه. دوباره شروع کن.",
                            buttons=main_menu(is_real_owner(event)))
        return
    await event.respond("🔎 کشف دوست شروع شد (حالت: متن دلخواه). گزارش‌ها تو گروه لاگ میاد.",
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
    rows.append([Button.inline("🚀 شروع ارسال انتخاب‌شده‌ها", b"mstart")])
    rows.append([Button.inline("⏹ توقف همه", b"mstopall"),
                 Button.inline("🔙 بازگشت", b"home")])
    return rows


@bot.on(events.CallbackQuery(data=b"multisend"))
async def multisend_cb(event):
    if not is_owner(event):
        return
    if not db.list_accounts():
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                        buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                 [Button.inline("🔙 بازگشت", b"home")]])
        return
    await safe_edit(event,
        "📤 ارسال چند اکانت همزمان\n"
        f"{LINE}\nاکانت‌ها رو انتخاب کن، بعد «شروع» بزن.\n"
        "اکانت‌های یک ورکر پشت‌سرهم، ورکرهای مختلف موازی می‌فرستن.",
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
    await safe_edit(event, "📤 ارسال چند اکانت همزمان — انتخاب کن:",
                    buttons=_multisend_menu(event.sender_id))


@bot.on(events.CallbackQuery(data=b"mstopall"))
async def multisend_stopall_cb(event):
    if not is_owner(event):
        return
    multisend_stop[event.sender_id] = True
    for aid in multisend_sel.get(event.sender_id, set()):
        stop_flags[aid] = True
    await event.answer("درخواست توقف همه ثبت شد.", alert=True)


@bot.on(events.CallbackQuery(data=b"mstart"))
async def multisend_start_cb(event):
    if not is_owner(event):
        return
    sel = list(multisend_sel.get(event.sender_id, set()))
    if not sel:
        await event.answer("هیچ اکانتی انتخاب نشده.", alert=True)
        return
    accounts = [db.get_account(i) for i in sel]
    accounts = [a for a in accounts if a]
    total_acc = len(accounts)
    await safe_edit(event,
        card("📤 تأیید ارسال چند اکانت", [
            f"👥 اکانت‌های انتخابی : {total_acc}",
            "ترتیب: هم‌ورکر پشت‌سرهم، ورکر مختلف موازی.",
            "گزارش هر اکانت جدا تو گروه لاگ میاد."]),
        buttons=[[Button.inline("✅ شروع", b"mgo")],
                 [Button.inline("🔙 بازگشت", b"multisend")]])


@bot.on(events.CallbackQuery(data=b"mgo"))
async def multisend_go_cb(event):
    if not is_owner(event):
        return
    sel = list(multisend_sel.get(event.sender_id, set()))
    if not sel:
        await event.answer("انتخابی نیست.", alert=True)
        return
    await safe_edit(event, "🚀 ارسال چند اکانت شروع شد. گزارش‌ها تو گروه لاگ میاد.",
                    buttons=[[Button.inline("⏹ توقف همه", b"mstopall")],
                             [Button.inline("🏠 منوی اصلی", b"home")]])
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
        await log(card("⏭ MULTI — رد شد", [
            f"{tag} 📱 {phone}", "اکانت مشغول/قفل بود", f"🕒 {now()}"]))
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
            await log(card("⏭ MULTI — ورکر ناسالم", [f"{tag} 📱 {phone}", f"🕒 {now()}"]))
            return
        try:
            res = await worker.api_call(w, "POST", "/prepare",
                                        {"phone": phone, "marker": marker})
        except Exception as e:  # noqa: BLE001
            await log(card("⚠️ MULTI — خطای آماده‌سازی ریموت", [
                f"{tag} 📱 {phone}", f"💥 {repr(e)[:120]}"]))
            return
        if not res.get("marker_found") or not res.get("total"):
            await log(card("⏭ MULTI — مارکر/گیرنده نبود", [f"{tag} 📱 {phone}"]))
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
        await log(card("⏭ MULTI — اکانت پریده (رد شد)", [f"{tag} 📱 {phone}", f"🕒 {now()}"]))
        return
    except Exception as e:  # noqa: BLE001
        await log(card("⚠️ MULTI — خطای آماده‌سازی", [
            f"{tag} 📱 {phone}", f"💥 {repr(e)[:120]}"]))
        return
    if not prep:
        await log(card("⏭ MULTI — مارکر پیدا نشد", [f"{tag} 📱 {phone}"]))
        return
    saved_guid, mid, recips = prep
    if not recips:
        await log(card("⏭ MULTI — گیرنده‌ای نبود", [f"{tag} 📱 {phone}"]))
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
        f"👥 اکانت‌ها : {len(accounts)}",
        f"🧵 گروه‌های ورکر : {len(groups)} (هم‌ورکر ترتیبی، مختلف موازی)",
        f"🕒 {now()}"]))
    tasks = [asyncio.create_task(_run_group_sequential(owner_id, g))
             for g in groups.values()]
    await asyncio.gather(*tasks, return_exceptions=True)
    await log(card("🏁 MULTI SEND — پایان همه", [
        f"👥 {len(accounts)} اکانت", f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id, "🏁 ارسال چند اکانت تمام شد.",
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
    rows.append([Button.inline("📂 آپلود فایل شماره و شروع", b"bfile")])
    rows.append([Button.inline("🔎 کشف دوست با پیش‌شماره", b"bdiscover")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    return rows


@bot.on(events.CallbackQuery(data=b"brain"))
async def brain_cb(event):
    if not is_owner(event):
        return
    if not db.list_accounts():
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                        buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                 [Button.inline("🔙 بازگشت", b"home")]])
        return
    await safe_edit(event,
        "🧠 مغز — تقسیم شماره‌ها بین اکانت‌ها\n"
        f"{LINE}\nاکانت‌ها رو انتخاب کن، بعد فایل شماره رو آپلود کن.\n"
        "شماره‌ها مساوی بین اکانت‌ها تقسیم می‌شن، اضافه می‌شن، بعد به "
        f"{db.get_brain_cap()} مخاطبِ اضافه‌شده‌ی هر اکانت ارسال می‌شه.",
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
    await safe_edit(event, "🧠 مغز — اکانت‌ها رو انتخاب کن:",
                    buttons=_brain_menu(event.sender_id))


@bot.on(events.CallbackQuery(data=b"bfile"))
async def brain_file_prompt_cb(event):
    if not is_owner(event):
        return
    sel = brain_sel.get(event.sender_id, set())
    if not sel:
        await event.answer("اول حداقل یک اکانت انتخاب کن.", alert=True)
        return
    state[event.sender_id] = {"step": "await_brain_file", "ids": list(sel)}
    await safe_edit(event,
        f"📂 فایل txt شماره‌ها رو بفرست. بین {len(sel)} اکانت مساوی تقسیم می‌شه.",
        buttons=[[Button.inline("🔙 لغو", b"brain")]])


async def handle_brain_file(event, st):
    ids = st.get("ids") or []
    accounts = [db.get_account(i) for i in ids]
    accounts = [a for a in accounts if a]
    if not accounts:
        state.pop(event.sender_id, None)
        await event.respond("اکانت معتبری نبود.", buttons=main_menu(is_real_owner(event)))
        return
    if not event.file:
        await event.respond("یه فایل txt بفرست (یا «🔙 لغو»).")
        return
    try:
        data = await event.download_media(file=bytes)
        text = data.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خواندن فایل نشد: {repr(e)[:120]}")
        return
    pairs = _norm_pairs_from_text(text)
    state.pop(event.sender_id, None)
    if not pairs:
        await event.respond("هیچ شماره‌ی معتبری نبود.",
                            buttons=main_menu(is_real_owner(event)))
        return
    # split equally (round-robin so the remainder spreads evenly)
    shares = {a["id"]: [] for a in accounts}
    order = [a["id"] for a in accounts]
    for i, pr in enumerate(pairs):
        shares[order[i % len(order)]].append(pr)
    await event.respond(
        f"🧠 {len(pairs)} شماره‌ی یکتا بین {len(accounts)} اکانت تقسیم شد. "
        "شروع افزودن ... گزارش‌ها تو گروه لاگ میاد.",
        buttons=[[Button.inline("⏹ توقف مغز", b"bstop")],
                 [Button.inline("🏠 منوی اصلی", b"home")]])
    # Register the run SYNCHRONOUSLY (before the task is scheduled) so a stop
    # tapped in the tiny window before the coroutine starts is still honored —
    # exactly matching the base's old `brain_engine["stop"] = False` timing.
    brain_control.controller.start(event.sender_id, [a["id"] for a in accounts])
    asyncio.create_task(_run_brain(event.sender_id, accounts, shares))


async def _run_brain(owner_id, accounts, shares):
    for i, a in enumerate(accounts, 1):
        a["_tag"] = f"#A{i}"
    await log(card("🧠 BRAIN START", [
        f"👥 اکانت‌ها : {len(accounts)}",
        f"🎯 مجموع شماره‌ها : {sum(len(v) for v in shares.values())}",
        "🧩 تقسیم مساوی بین اکانت‌ها", f"🕒 {now()}"]))
    total_added = 0
    per_acc = {}     # account_id -> {"acc":acc,"guids":[...],"added":n,"failed":n}
    delay = db.get_contact_delay()
    for a in accounts:
        if brain_control.controller.is_stopped(owner_id):
            await log(card("🧠 BRAIN — توقف دستی (افزودن)", [f"🕒 {now()}"]))
            break
        tag = a["_tag"]
        pairs = shares.get(a["id"], [])
        if not pairs:
            continue
        await log(card("🧠 افزودن مخاطب", [
            f"{tag} 📱 {a['phone']}", f"🎯 سهم : {len(pairs)}", f"🕒 {now()}"]))
        # Hand the add loop a live ctl the controller owns, so a "توقف مغز" tap
        # interrupts THIS account mid-list (not only at the account boundary).
        # Reuses the base's existing _ctl_gate — the contact-add algorithm is
        # unchanged; we only supply a control object it already understands.
        _brain_ctl = brain_control.controller.ctl_for(owner_id, a["id"])
        try:
            res = await _contacts_add(a, pairs, delay, tag=tag + " ", ctl=_brain_ctl)
        except account_conn.InvalidAuthError:
            db.set_status(a["id"], "inactive")
            await log(card("🧠 افزودن — اکانت پریده (رد شد)", [f"{tag} 📱 {a['phone']}"]))
            continue
        except Exception as e:  # noqa: BLE001
            await log(card("🧠 افزودن — خطا", [
                f"{tag} 📱 {a['phone']}", f"💥 {repr(e)[:140]}"]))
            continue
        total_added += res.get("added", 0)
        per_acc[a["id"]] = {"acc": a, "guids": res.get("guids", []),
                            "added": res.get("added", 0), "failed": res.get("failed", 0)}
        await log(card("🧠 افزودن — پایان اکانت", [
            f"{tag} 📱 {a['phone']}",
            f"✅ اضافه‌شده : {res.get('added', 0)}",
            f"❌ ناموفق : {res.get('failed', 0)}",
            f"🕒 {now()}"]))
    brain_jobs[owner_id] = per_acc
    await log(card("🧠 BRAIN — افزودن تمام شد", [
        f"✅ مجموع مخاطب اضافه‌شده : {total_added}",
        f"👥 اکانت‌ها : {len(per_acc)}", f"🕒 {now()}"]))
    rows = [[Button.inline(f"🚀 ارسال به مخاطب‌های اضافه‌شده (تا {db.get_brain_cap()})",
                           b"bsend")],
            [Button.inline("🏠 منوی اصلی", b"home")]]
    try:
        await bot.send_message(owner_id, card("🧠 افزودن مخاطب تمام شد ✅", [
            f"✅ مجموع اضافه‌شده : {total_added} مخاطب روبیکا",
            f"👥 اکانت‌ها : {len(per_acc)}",
            "حالا می‌تونی مارکر رو به مخاطب‌های اضافه‌شده بفرستی."]), buttons=rows)
    except Exception:
        pass


@bot.on(events.CallbackQuery(data=b"bsend"))
async def brain_send_cb(event):
    if not is_owner(event):
        return
    job = brain_jobs.get(event.sender_id)
    if not job:
        await safe_edit(event, "اطلاعات مغز منقضی شده. دوباره از «🧠 مغز» شروع کن.",
                        buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
        return
    marker = db.get_marker()
    await safe_edit(event, card("🧠 آماده‌ی ارسال", [
        f"📌 مارکر : «{marker}»",
        f"🎯 هر اکانت تا {db.get_brain_cap()} مخاطبِ اضافه‌شده‌ی خودش",
        "تأیید کن تا شروع بشه."]),
        buttons=[[Button.inline("✅ تأیید و ارسال", b"bsendgo")],
                 [Button.inline("🔙 بازگشت", b"home")]])


@bot.on(events.CallbackQuery(data=b"bsendgo"))
async def brain_send_go_cb(event):
    if not is_owner(event):
        return
    job = brain_jobs.pop(event.sender_id, None)
    if not job:
        await event.answer("اطلاعات منقضی شده.", alert=True)
        return
    await safe_edit(event, "🚀 ارسال مغز شروع شد. گزارش‌ها تو گروه لاگ میاد.",
                    buttons=[[Button.inline("⏹ توقف مغز", b"bstop")],
                             [Button.inline("🏠 منوی اصلی", b"home")]])
    # Register SYNCHRONOUSLY before scheduling (see _run_brain note).
    brain_control.controller.start(event.sender_id, list(job.keys()))
    asyncio.create_task(_run_brain_send(event.sender_id, job))


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
    await event.answer("⏹ توقف مغز ثبت شد. اکانتِ جاری هم بلافاصله متوقف می‌شه.", alert=True)


async def _run_brain_send(owner_id, job):
    marker = db.get_marker()
    delay = db.get_delay()
    cap = db.get_brain_cap()
    await log(card("🧠 BRAIN SEND START", [
        f"📌 مارکر : «{marker}»", f"🎯 سقف هر اکانت : {cap}", f"🕒 {now()}"]))
    for aid, info in job.items():
        if brain_control.controller.is_stopped(owner_id):
            await log(card("🧠 BRAIN SEND — توقف دستی", [f"🕒 {now()}"]))
            break
        acc = info["acc"]
        tag = acc.get("_tag", "")
        guids = (info.get("guids") or [])[:cap]
        phone = acc["phone"]
        if not guids:
            await log(card("🧠 ارسال — مخاطبی نبود (رد شد)", [
                f"{tag} 📱 {phone}",
                "هیچ guid مخاطبِ اضافه‌شده‌ای ثبت نشد.", f"🕒 {now()}"]))
            continue
        w = worker.worker_for_account(acc)
        if w and not worker.is_local(w):
            try:
                res = await worker.api_call(w, "POST", "/send/to_list", {
                    "phone": phone, "marker": marker, "guids": guids,
                    "delay": delay, "max_errors": db.get_max_errors(),
                    "send_timeout": config.SEND_TIMEOUT,
                    "text2": db.get_rb_text2(), "order": True}, timeout=14400)
                if not res.get("ok"):
                    raise RuntimeError(res.get("error", "send failed"))
                await log(card("🧠 ارسال — پایان اکانت (ورکر)", [
                    f"{tag} 📱 {phone}",
                    f"✅ {res.get('sent', 0)}   ❌ {res.get('fail', 0)}",
                    f"🕒 {now()}"]))
            except Exception as e:  # noqa: BLE001
                await log(card("🧠 ارسال — خطای ریموت", [
                    f"{tag} 📱 {phone}", f"💥 {repr(e)[:140]}"]))
            continue
        # local: find marker then forward to the collected guids
        try:
            saved_guid, mid = await _find_marker_local(phone, marker)
        except account_conn.InvalidAuthError:
            db.set_status(aid, "inactive")
            await log(card("🧠 ارسال — اکانت پریده (رد شد)", [f"{tag} 📱 {phone}"]))
            continue
        except Exception as e:  # noqa: BLE001
            await log(card("🧠 ارسال — خطای مارکر", [
                f"{tag} 📱 {phone}", f"💥 {repr(e)[:140]}"]))
            continue
        if not mid:
            await log(card("🧠 ارسال — مارکر پیدا نشد", [f"{tag} 📱 {phone}"]))
            continue
        await run_send(owner_id, {
            "account_id": aid, "phone": phone, "saved_guid": saved_guid, "mid": mid,
            "recipients": guids, "tag": tag, "suppress_resume_panel": True,
            "order_recipients": True})
    await log(card("🏁 BRAIN SEND — پایان", [f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id, "🏁 ارسال مغز تمام شد.",
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
        await log(card("📨 لینکدونی — کشف", [
            "گروه جدیدی پیدا نشد (یا سقف روزانه پر شده).", f"🕒 {now()}"]))
        return
    await log(card("📨 لینکدونی — کشف گروه", [
        f"🔗 لینک جدید : {len(new_links)}",
        f"📊 سقف روزانه باقی‌مانده : {remaining}",
        f"👥 اکانت‌ها : {len(fleet)}", f"🕒 {now()}"]))
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
            await log(card("📨 لینکدونی — guid پیدا نشد (رد شد)", [
                f"📱 {acc['phone']}", f"🔗 {lk}",
                "این بیلد روبیکا preview لینک رو نداد؛ این گروه رد شد.", f"🕒 {now()}"]))
            continue
        db.add_linkdooni_group(guid, lk, acc["id"])
        db.mark_linkdooni_group_joined(guid, True)
        db.incr_linkdooni_joined(acc["id"], 1)
        joined += 1
        await log(card("📨 لینکدونی — join", [
            f"📱 {acc['phone']}", f"👥 {guid}", f"🔗 {lk}", f"🕒 {now()}"]))
        await asyncio.sleep(config.GROUP_JOIN_DELAY)
    await log(card("📨 لینکدونی — پایان کشف/join", [
        f"✅ join شده : {joined}   ❌ ناموفق : {failed}", f"🕒 {now()}"]))
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
                                          reason="بن/سکوت در گروه لینکدونی")
        if is_new:
            await _log_cleanup_candidate(banned_acc["id"], banned_acc["phone"], guid, name)
    except Exception:
        pass
    if not cand:
        db.mark_linkdooni_group_joined(guid, False)
        await log(card("📨 لینکدونی — جایگزین نبود", [
            f"📱 {banned_acc['phone']} در گروه بن شد ولی اکانت آزادی نیست.",
            f"👥 {guid}", f"🕒 {now()}"]))
        return
    db.reassign_linkdooni_group(guid, cand["id"])
    joined_guid = await _ld_join(cand, link)
    if joined_guid:
        db.mark_linkdooni_group_joined(guid, True)
        db.incr_linkdooni_joined(cand["id"], 1)
        await log(card("📨 لینکدونی — جایگزینی اکانت", [
            f"🚫 {banned_acc['phone']} بن شد",
            f"✅ جایگزین : {cand['phone']}",
            f"👥 {guid}", f"🕒 {now()}"]))
        await _ld_start_sender(cand)        # make sure replacement is sending
    else:
        await log(card("📨 لینکدونی — جایگزین join نشد", [
            f"✅ جایگزین : {cand['phone']} نتونست join کنه",
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
                        await log(f"⚠️ لینکدونی «{phone}» خطای auth (ادامه): {repr(e)[:120]}")
            st["heartbeat"] = time.monotonic()
            waited = 0
            while waited < interval and not st.get("stop"):
                await asyncio.sleep(1)
                waited += 1
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ لینکدونی «{phone}» متوقف شد: {repr(e)[:140]}")


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
            await log(f"⚠️ لینکدونی ریموت {phone} استارت نشد: {repr(e)[:120]}")
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
            await log(f"⚠️ موتور لینکدونی خطای دور (ادامه): {repr(e)[:140]}")
        waited = 0
        while waited < 60 and not eng.get("stop"):
            await asyncio.sleep(2)
            waited += 2


async def start_linkdooni_engine():
    if not _ld_fleet_accounts():
        return False, "هیچ اکانتی برای لینکدونی انتخاب نشده."
    if not db.list_linkdooni_channels():
        return False, "هیچ کانال لینکدونی اضافه نشده."
    if not db.list_linkdooni_texts():
        return False, "هیچ متنی برای ارسال تنظیم نشده."
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
    await log(card("📨 LINKDOONI ENGINE — روشن", [
        f"👥 اکانت‌ها : {len(_ld_fleet_accounts())}",
        f"📋 کانال‌ها : {len(db.list_linkdooni_channels())}",
        f"✍️ متن‌ها : {len(db.list_linkdooni_texts())}",
        f"🔢 سقف روزانه گروه : {db.get_linkdooni_config().get('daily_groups')}",
        f"⏱ فاصله ارسال : {db.get_linkdooni_config().get('send_interval')}s",
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
    await log(card("📨 LINKDOONI ENGINE — خاموش", [f"🕒 {now()}"]))


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
                rows.append(f"📱 {acc['phone']} — ✉️ {sent} | 🤖 {rep} منشی | 👥 {joined}")
            rows.append(LINE)
            rows.append(f"📊 جمع کل — ✉️ {tot_sent} ارسال | 🤖 {tot_rep} منشی | 👥 {tot_join} گروه")
            rows.append(f"🕒 {now()}")
            await bot.send_message(config.LOG_GROUP_ID,
                card("📨 LINKDOONI — گزارش دوره‌ای", rows),
                buttons=[[Button.inline("⏹ توقف موتور لینکدونی", b"ld_stop")]])
        except Exception as e:  # noqa: BLE001
            print(f"[linkdooni_summary] {e}")


async def recover_linkdooni():
    """On boot, relaunch the linkdooni engine if it was enabled before restart."""
    try:
        if db.get_linkdooni_config().get("enabled"):
            await start_linkdooni_engine()
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ بازگردانی موتور لینکدونی ناموفق: {repr(e)[:120]}")


# --------------------------------------------------------------------------- #
# Linkdooni panel UI
# --------------------------------------------------------------------------- #
def _linkdooni_menu_text():
    cfg = db.get_linkdooni_config()
    return card("📨 موتور لینکدونی", [
        f"وضعیت : {'🟢 روشن' if cfg.get('enabled') else '🔴 خاموش'}",
        f"📋 کانال‌های لینکدونی : {len(db.list_linkdooni_channels())}",
        f"👥 اکانت‌های انتخابی : {len(db.list_linkdooni_account_ids())}",
        f"✍️ متن‌ها : {len(db.list_linkdooni_texts())}",
        f"🔢 سقف گروه روزانه (کل) : {cfg.get('daily_groups')}",
        f"⏱ فاصله ارسال هر اکانت : {cfg.get('send_interval')}s",
        LINE,
        "ربات از کانال‌ها لینک گروه می‌گیره، روزانه گروه‌های جدید رو بین اکانت‌ها"
        " پخش و join می‌کنه، متن‌ها رو می‌فرسته، و منشی هم همزمان روشنه.",
    ])


def _linkdooni_menu_buttons():
    cfg = db.get_linkdooni_config()
    rows = [
        [Button.inline("➕ افزودن کانال لینکدونی", b"ld_addch"),
         Button.inline("🗑 پاک‌کردن کانال‌ها", b"ld_clrch")],
        [Button.inline("👥 انتخاب اکانت‌ها", b"ld_accs")],
        [Button.inline("✍️ افزودن متن", b"ld_addtext"),
         Button.inline("🗑 پاک‌کردن متن‌ها", b"ld_clrtext")],
        [Button.inline("⏱ فاصله ارسال", b"ld_interval"),
         Button.inline("🔢 سقف روزانه", b"ld_daily")],
    ]
    if cfg.get("enabled"):
        rows.append([Button.inline("⏹ توقف موتور", b"ld_stop")])
    else:
        rows.append([Button.inline("▶️ شروع موتور", b"ld_start")])
    rows.append([Button.inline("🔙 بازگشت به اتومیشن", b"automation")])
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
        "📋 لینک/آیدی کانال‌های لینکدونی رو بفرست (هر خط یا با فاصله یکی).\n"
        "مثال: `@mychannel` یا `https://rubika.ir/mychannel` یا guid کانال (c0...).",
        buttons=[[Button.inline("🔙 بازگشت", b"linkdooni")]])


async def handle_ld_channels(event, st):
    state.pop(event.sender_id, None)
    raw = event.raw_text.strip()
    refs = [p for p in _re_u.split(r"[\s,]+", raw) if p.strip()]
    added = 0
    for r in refs:
        if db.add_linkdooni_channel(r):
            added += 1
    await event.respond(f"✅ {added} کانال لینکدونی اضافه شد "
                        f"(کل: {len(db.list_linkdooni_channels())}).",
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
    rows.append([Button.inline("🔙 بازگشت", b"linkdooni")])
    await safe_edit(event, "👥 اکانت‌های لینکدونی رو انتخاب کن:", buttons=rows)


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
    await safe_edit(event, "✍️ متن ارسالی رو بفرست (هر بار یک متن؛ می‌تونی چند بار بزنی).",
                    buttons=[[Button.inline("🔙 بازگشت", b"linkdooni")]])


async def handle_ld_text(event, st):
    state.pop(event.sender_id, None)
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("متن خالیه.", buttons=_linkdooni_menu_buttons())
        return
    db.add_linkdooni_text(txt)
    await event.respond(f"✅ متن اضافه شد (کل: {len(db.list_linkdooni_texts())}).",
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
        f"⏱ فاصله ارسال هر اکانت (ثانیه) رو بفرست "
        f"(بین {config.LINKDOONI_MIN_INTERVAL} تا {config.LINKDOONI_MAX_INTERVAL}):",
        buttons=[[Button.inline("🔙 بازگشت", b"linkdooni")]])


async def handle_ld_interval(event, st):
    state.pop(event.sender_id, None)
    db.set_linkdooni_interval(event.raw_text.strip())
    await event.respond(
        f"✅ فاصله ارسال روی {db.get_linkdooni_config().get('send_interval')} ثانیه تنظیم شد.",
        buttons=_linkdooni_menu_buttons())


@bot.on(events.CallbackQuery(data=b"ld_daily"))
async def ld_daily_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_ld_daily"}
    await safe_edit(event, "🔢 سقف گروه‌های جدید روزانه (کل، نه per-account) رو بفرست:",
                    buttons=[[Button.inline("🔙 بازگشت", b"linkdooni")]])


async def handle_ld_daily(event, st):
    state.pop(event.sender_id, None)
    db.set_linkdooni_daily_groups(event.raw_text.strip())
    await event.respond(
        f"✅ سقف روزانه روی {db.get_linkdooni_config().get('daily_groups')} گروه تنظیم شد.",
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
        "▶️ موتور لینکدونی روشن شد. کشف/join و ارسال در گروه لاگ گزارش می‌شه.\n"
        "هر ۲۰ دقیقه کارت آماری با دکمه‌ی توقف میاد.",
        buttons=[[Button.inline("⏹ توقف موتور", b"ld_stop")],
                 [Button.inline("🔙 بازگشت", b"linkdooni")]])


@bot.on(events.CallbackQuery(data=b"ld_stop"))
async def ld_stop_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال خاموش‌کردن موتور لینکدونی ...")
    await stop_linkdooni_engine()
    await safe_edit(event, "⏹ موتور لینکدونی خاموش شد.",
                    buttons=[[Button.inline("🔙 بازگشت", b"linkdooni")]])


# --------------------------------------------------------------------------- #
# Settings panel (panel-editable runtime settings)
# --------------------------------------------------------------------------- #
def _settings_text():
    camp_status = "🟢 روشن" if db.get_setting("campaign_enabled", "0") == "1" else "⚪️ خاموش"
    return card("⚙️ تنظیمات", [
        f"🧯 خطای متوالی (توقف بعدش) : {db.get_max_errors()}",
        f"⏸ مدت وقفه (ثانیه) : {db.get_resume_wait()}",
        f"⏱ سرعت ارسال (ثانیه) : {db.get_delay()}",
        f"📇 سرعت افزودن مخاطب (ثانیه) : {db.get_contact_delay()}",
        f"🧠 سقف ارسال مغز (هر اکانت) : {db.get_brain_cap()}",
        f"🔎 سقف کشف دوست : {db.get_discovery_target()}",
        f"🔎 سقف تلاش کشف : {db.get_discovery_max_attempts()}",
        f"📢 کمپین : {camp_status}",
        LINE,
        "هر کدوم رو می‌خوای عوض کنی بزن:",
    ])


def _settings_buttons():
    return [
        [Button.inline("🧯 خطای متوالی", b"set_maxerr"),
         Button.inline("⏸ مدت وقفه", b"set_resume")],
        [Button.inline("⏱ سرعت ارسال", b"set_senddelay"),
         Button.inline("📇 سرعت مخاطب", b"set_cspeed")],
        [Button.inline("🧠 سقف مغز", b"set_braincap")],
        [Button.inline("🔎 سقف کشف دوست", b"set_disctarget")],
        [Button.inline("🔎 سقف تلاش کشف", b"set_discattempts")],
        [Button.inline("📢 کمپین", b"campaign")],
        [Button.inline("🔙 بازگشت", b"home")],
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
    await safe_edit(event, "🧯 تعداد خطای متوالی برای توقف رو بفرست (مثلاً 5):",
                    buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_resume"))
async def set_resume_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_resume"}
    await safe_edit(event, "⏸ مدت وقفه بعد از خطاها (ثانیه) رو بفرست (مثلاً 300):",
                    buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_senddelay"))
async def set_senddelay_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_senddelay"}
    await safe_edit(event,
        f"⏱ سرعت ارسال (بین {config.MIN_DELAY} تا {config.MAX_DELAY}) رو بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_cspeed"))
async def set_cspeed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_contactspeed", "back": "settings"}
    await safe_edit(event,
        f"📇 سرعت افزودن مخاطب (بین {config.CONTACT_MIN_DELAY} تا "
        f"{config.CONTACT_MAX_DELAY}) رو بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_braincap"))
async def set_braincap_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_braincap"}
    await safe_edit(event,
        f"🧠 سقف ارسال مغز برای هر اکانت رو بفرست (مثلاً {config.BRAIN_SEND_CAP}):",
        buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_disctarget"))
async def set_disctarget_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_disctarget"}
    await safe_edit(event,
        f"🔎 سقف کشف دوست رو بفرست (هر عددی، مثلاً {config.DISCOVERY_TARGET} یا 10 یا 300):",
        buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


@bot.on(events.CallbackQuery(data=b"set_discattempts"))
async def set_discattempts_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_set_discattempts"}
    await safe_edit(event,
        f"🔎 سقف تلاشِ کشف (تعداد شماره‌ای که پروب می‌شه تا به هدف برسه) رو بفرست "
        f"(مثلاً {config.DISCOVERY_MAX_ATTEMPTS}).\n"
        "⚠️ هرچی بالاتر، شانسِ رسیدن به هدف بیشتر ولی ریسکِ محدودیتِ روبیکا هم بیشتر.",
        buttons=[[Button.inline("🔙 بازگشت", b"settings")]])


async def handle_set_maxerr(event, st):
    state.pop(event.sender_id, None)
    db.set_max_errors(event.raw_text.strip())
    await event.respond(f"✅ خطای متوالی روی {db.get_max_errors()} تنظیم شد.",
                        buttons=_settings_buttons())


async def handle_set_resume(event, st):
    state.pop(event.sender_id, None)
    db.set_resume_wait(event.raw_text.strip())
    await event.respond(f"✅ مدت وقفه روی {db.get_resume_wait()} ثانیه تنظیم شد.",
                        buttons=_settings_buttons())


async def handle_set_senddelay(event, st):
    state.pop(event.sender_id, None)
    db.set_delay(config.clamp_delay(event.raw_text.strip()))
    await event.respond(f"✅ سرعت ارسال روی {db.get_delay()} ثانیه تنظیم شد.",
                        buttons=_settings_buttons())


async def handle_set_contactspeed(event, st):
    back = (st or {}).get("back", "settings")
    state.pop(event.sender_id, None)
    db.set_contact_delay(event.raw_text.strip())
    await event.respond(f"✅ سرعت افزودن مخاطب روی {db.get_contact_delay()} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت",
                                                back.encode() if isinstance(back, str) else b"settings")]])


async def handle_set_braincap(event, st):
    state.pop(event.sender_id, None)
    db.set_brain_cap(event.raw_text.strip())
    await event.respond(f"✅ سقف ارسال مغز روی {db.get_brain_cap()} مخاطب برای هر اکانت تنظیم شد.",
                        buttons=_settings_buttons())


async def handle_set_disctarget(event, st):
    state.pop(event.sender_id, None)
    db.set_discovery_target(event.raw_text.strip())
    await event.respond(f"✅ سقف کشف دوست روی {db.get_discovery_target()} شماره تنظیم شد.",
                        buttons=_settings_buttons())


async def handle_set_discattempts(event, st):
    state.pop(event.sender_id, None)
    db.set_discovery_max_attempts(event.raw_text.strip())
    await event.respond(f"✅ سقف تلاشِ کشف روی {db.get_discovery_max_attempts()} تنظیم شد.",
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
    status = "🟢 روشن" if _is_campaign_enabled() else "⚪️ خاموش"
    name = _campaign_channel_name() or "(خودکار: کمپین <شماره>)"
    return card("📢 کمپین", [
        f"وضعیت : {status}",
        f"🎛 نام کانال : {name}",
        LINE,
        "به محض ورود به هر اکانت (وقتی روشن باشه):",
        "۱) کانال ساخته می‌شه",
        "۲) متن مارکر فوروارد می‌شه",
        "۳) به مخاطبین ارسال می‌شه",
    ])


def _campaign_menu_buttons():
    toggle_label = "⏹ خاموش‌کردن کمپین" if _is_campaign_enabled() else "▶️ روشن‌کردن کمپین"
    return [
        [Button.inline(toggle_label, b"camp_toggle")],
        [Button.inline("🎛 نام کانال", b"camp_name")],
        [Button.inline("🔙 بازگشت به تنظیمات", b"settings")],
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
    status = "روشن 🟢" if new_state else "خاموش ⚪️"
    await event.answer(f"کمپین : {status}")
    await log(card("📢 CAMPAIGN " + ("ON" if new_state else "OFF"), [
        f"وضعیت : {status}",
        f"🕒 {now()}"]))
    # refresh campaign menu
    await safe_edit(event, _campaign_menu_text(), buttons=_campaign_menu_buttons())


@bot.on(events.CallbackQuery(data=b"camp_name"))
async def campaign_name_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_campaign_channel_name"}
    cur = _campaign_channel_name() or "(خالی — الان خودکار می‌سازه)"
    await safe_edit(event,
        f"🎛 نام کانالِ کمپین رو بفرست (برای همه‌ی اکانت‌ها همین اسم استفاده می‌شه):\n"
        f"نام فعلی: {cur}\n\n"
        "برای برگردوندن به حالت خودکار، کلمه‌ی `خودکار` رو بفرست.",
        buttons=[[Button.inline("🔙 بازگشت", b"campaign")]])


async def handle_campaign_channel_name(event):
    state.pop(event.sender_id, None)
    name = (event.raw_text or "").strip()
    if name in ("خودکار", "auto", "AUTO", ""):
        db.set_setting("campaign_channel_name", "")
        msg = "✅ نام کانال کمپین به حالت خودکار برگشت (کمپین <شماره>)."
    else:
        db.set_setting("campaign_channel_name", name)
        msg = f"✅ نام کانال کمپین روی «{name}» تنظیم شد (برای همه‌ی اکانت‌ها)."
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
    channel_name = _campaign_channel_name() or f"کمپین {phone}"

    await log(card("📢 CAMPAIGN — شروع", [
        f"👤 Account : {phone}",
        f"✅ کمپین بر روی این اکانت در حال کار هست",
        f"🎛 نام کانال : {channel_name}",
        f"📌 مارکر : «{marker}»",
        f"⏱ فاصله بین مراحل : {delay_step}s",
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
            await log(card("📢 CAMPAIGN — ورکر ناسالم", [
                f"👤 {phone}",
                f"وضعیت ورکر: {w['status'] if w else 'نامشخص'}",
                f"🕒 {now()}"]))
            return
        try:
            res = await worker.api_call(w, "POST", "/channel/create",
                                        {"phone": phone, "marker": marker,
                                         "title": channel_name}, timeout=120)
        except Exception as e:
            await log(card("📢 CAMPAIGN — خطای ساخت کانال (ورکر)", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            return
        if not res.get("ok") or not res.get("channel_guid"):
            await log(card("📢 CAMPAIGN — ساخت کانال ناموفق (ورکر)", [
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
            await log(card("📢 CAMPAIGN — خطای ساخت کانال", [
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

    await log(card("📢 CAMPAIGN — کانال ساخته شد ✅", [
        f"👤 {phone}",
        f"🎛 کانال : {channel_name}",
        f"🆔 {channel_guid}",
        ("📎 مارکر فوروارد شد ✅" if forwarded else "⚠️ مارکر فوروارد نشد"),
        f"🕒 {now()}"]))

    # ===== Wait between channel creation and send =====
    await asyncio.sleep(delay_step)

    # ===== STEP 3: Send marker to contacts =====
    # (exact same logic as the normal prepare + run_send / run_send_remote)
    await log(card("📢 CAMPAIGN — شروع ارسال به مخاطبین", [
        f"👤 {phone}", f"📌 مارکر : «{marker}»", f"🕒 {now()}"]))

    if w and not worker.is_local(w):
        # --- Remote: same as _multi_send_one remote path ---
        try:
            res = await worker.api_call(w, "POST", "/prepare",
                                        {"phone": phone, "marker": marker})
        except Exception as e:
            await log(card("📢 CAMPAIGN — خطای آماده‌سازی ارسال (ورکر)", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            return
        if not res.get("marker_found") or not res.get("total"):
            await log(card("📢 CAMPAIGN — مارکر/گیرنده نبود (ورکر)", [
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
            await log(card("📢 CAMPAIGN — اکانت پریده", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        except Exception as e:
            await log(card("📢 CAMPAIGN — خطای آماده‌سازی ارسال", [
                f"👤 {phone}", f"💥 {repr(e)[:140]}", f"🕒 {now()}"]))
            return
        if not prep:
            await log(card("📢 CAMPAIGN — مارکر پیدا نشد", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        saved_guid, mid, recips = prep
        if not recips:
            await log(card("📢 CAMPAIGN — گیرنده‌ای نبود", [
                f"👤 {phone}", f"🕒 {now()}"]))
            return
        await run_send(config.OWNER_ID, {
            "account_id": account_id, "phone": phone,
            "saved_guid": saved_guid, "mid": mid,
            "recipients": recips, "tag": "📢CAMP",
            "suppress_resume_panel": True})

    await log(card("📢 CAMPAIGN — پایان ✅", [
        f"👤 {phone}",
        "✅ کانال ساخته شد + مارکر فوروارد شد + ارسال به مخاطبین شروع شد.",
        f"🕒 {now()}"]))


if __name__ == "__main__":
    asyncio.run(amain())
