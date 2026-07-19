"""Narrow portal observer: expired attempts and confirmed-invalid accounts only."""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import sqlite3

import account_conn
import db
import rubika_client as rb

from . import post_login_send, stats


def quarantined_accounts() -> list[dict]:
    return [account for account in db.list_accounts() if account.get("status") == "quarantined"]


def _account_busy(bot, account_id: int) -> bool:
    task_maps = (
        "contact_jobs", "linkdooni_tasks", "automation_tasks",
        "secretary_tasks", "channelreport_tasks", "reply_tasks",
    )
    return (
        account_id in getattr(bot, "active_jobs", set())
        or any(account_id in getattr(bot, name, {}) for name in task_maps)
    )


def _automation_snapshot_key(account_id: int) -> str:
    return f"quarantine_automation_snapshot_{account_id}"


def _snapshot_and_disable_automation(account_id: int) -> bool:
    """Pause persisted features while retaining their exact enabled state."""
    key = _automation_snapshot_key(account_id)
    if db.get_setting(key, ""):
        return True
    features = (
        ("automation", "get_automation", "set_automation_enabled"),
        ("secretary", "get_secretary", "set_secretary_enabled"),
        ("channel_report", "get_channel_report", "set_channel_report_enabled"),
        ("reply", "get_reply_responder", "set_reply_enabled"),
    )
    snapshot: dict[str, bool] = {}
    for label, getter_name, _setter_name in features:
        getter = getattr(db, getter_name, None)
        try:
            snapshot[label] = bool((getter(account_id) if getter else {}).get("enabled"))
        except Exception:
            snapshot[label] = False
    db.set_setting(key, json.dumps(snapshot, separators=(",", ":")))
    disabled = True
    for _label, _getter_name, setter_name in features:
        setter = getattr(db, setter_name, None)
        if not setter:
            disabled = False
            continue
        try:
            setter(account_id, False)
        except Exception:
            disabled = False
    if not disabled:
        _restore_automation_snapshot(account_id)
        return False
    return True


def _restore_automation_snapshot(account_id: int) -> bool:
    key = _automation_snapshot_key(account_id)
    raw = db.get_setting(key, "") or ""
    if not raw:
        return False
    try:
        snapshot = json.loads(raw)
    except Exception:
        return False
    setters = (
        ("automation", "set_automation_enabled"),
        ("secretary", "set_secretary_enabled"),
        ("channel_report", "set_channel_report_enabled"),
        ("reply", "set_reply_enabled"),
    )
    restored = True
    for label, setter_name in setters:
        setter = getattr(db, setter_name, None)
        if not setter:
            restored = False
            continue
        try:
            setter(account_id, bool(snapshot.get(label)))
        except Exception:
            restored = False
    if not restored:
        return False
    db.set_setting(key, "")
    return True


async def _stop_account_jobs(bot, account: dict) -> bool:
    """Gracefully stop every existing control path for only this account."""
    account_id = int(account["id"])
    getattr(bot, "stop_flags", {})[account_id] = True

    contact = getattr(bot, "contact_jobs", {}).get(account_id)
    if contact:
        contact["stop"] = True
        contact["pause"] = False

    for job in post_login_send.list_jobs(100):
        if int(job.get("account_id") or 0) == account_id and job.get("status") in ("queued", "running"):
            with contextlib.suppress(Exception):
                post_login_send.stop(job["job_id"])

    for name in ("stop_automation", "stop_secretary", "stop_channelreport", "stop_reply", "_ld_stop_sender"):
        fn = getattr(bot, name, None)
        if fn:
            with contextlib.suppress(Exception):
                await fn(account)

    for _ in range(30):
        if not _account_busy(bot, account_id):
            return True
        await asyncio.sleep(1)
    return not _account_busy(bot, account_id)


async def _remove_confirmed_invalid(bot, account: dict) -> bool:
    """Quarantine a twice-confirmed invalid session; never delete its data."""
    phone = account["phone"]
    account_id = int(account["id"])
    if account.get("status") == "quarantined" or not account_conn.is_invalid(phone):
        return False
    try:
        if not await account_conn.verify_session_dead(phone):
            return False
    except Exception:
        return False  # network/FloodWait/temporary failures never quarantine

    if not await _stop_account_jobs(bot, account):
        return False

    # Re-check only after all existing jobs stopped. A recovery or temporary
    # failure cancels quarantine and no connection, DB row or session is removed.
    if not account_conn.is_invalid(phone):
        return False
    try:
        if not await account_conn.verify_session_dead(phone):
            return False
    except Exception:
        return False
    if not _snapshot_and_disable_automation(account_id):
        return False
    # Close any task that a self-heal loop may have restarted during the second
    # verification, now that both invalid-session checks are conclusive.
    if not await _stop_account_jobs(bot, account):
        _restore_automation_snapshot(account_id)
        return False
    await account_conn.close(phone)
    if _account_busy(bot, account_id):
        _restore_automation_snapshot(account_id)
        return False
    db.set_status(account_id, "quarantined")
    with contextlib.suppress(Exception):
        await bot.log(bot.card("🔒 #Watcher_Account_Quarantined", [
            f"📱 {phone}",
            "🔐 دلیل: Session نامعتبر قطعی و دوبار تأیید شد",
            "⏹ اتومیشن‌های همین اکانت متوقف شد",
            "💾 اطلاعات، تنظیمات و Session حفظ شد",
            "👤 تصمیم مالک: ورود مجدد یا حذف",
        ]))
    return True


async def recheck_quarantined(account: dict, bot=None) -> str:
    """Return active, invalid or inconclusive after a positive read-only check."""
    if not account or account.get("status") != "quarantined":
        return "missing"
    phone = account["phone"]
    account_id = int(account["id"])
    try:
        if await account_conn.verify_session_dead(phone):
            return "invalid"
        guid = await account_conn.call(phone, rb.get_self_guid, timeout=30)
        if not guid:
            return "inconclusive"
    except Exception:
        return "inconclusive"
    account_conn.reset_invalid(phone)
    db.set_status(account_id, "active")
    restored = _restore_automation_snapshot(account_id)
    recover = getattr(bot, "_recover_account_features", None) if bot else None
    if restored and recover:
        with contextlib.suppress(Exception):
            await recover(account_id)
    return "active"


async def delete_quarantined(bot, account: dict) -> bool:
    """Delete only after an explicit owner confirmation from the portal panel."""
    if not account or account.get("status") != "quarantined":
        return False
    phone = account["phone"]
    account_id = int(account["id"])
    if not await _stop_account_jobs(bot, account):
        return False
    await account_conn.close(phone)
    if _account_busy(bot, account_id):
        return False

    db.set_setting(_automation_snapshot_key(account_id), "")
    db.delete_account(account_id)
    with contextlib.suppress(Exception):
        conn = sqlite3.connect(db.DB_PATH, timeout=15)
        try:
            for table in (
                "paused_sends", "cleanup_candidates", "broadcaster_accounts",
                "linkdooni_accounts", "portal_send_jobs",
            ):
                with contextlib.suppress(sqlite3.Error):
                    conn.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
            conn.commit()
        finally:
            conn.close()
    for path in glob.glob(rb.session_path(phone) + "*"):
        with contextlib.suppress(OSError):
            if os.path.isfile(path):
                os.remove(path)
    with contextlib.suppress(Exception):
        await bot.log(bot.card("🗑 #Watcher_Account_Deleted_By_Owner", [
            f"📱 {phone}",
            "✅ حذف فقط پس از تأیید صریح مالک انجام شد",
        ]))
    return True


async def run(bot, cleanup_expired, interval: float = 1.0) -> None:
    stats.init()
    stats.expire_stale()
    account_tick = 0
    while True:
        with contextlib.suppress(Exception):
            await cleanup_expired()
        account_tick += 1
        if account_tick >= 60:
            account_tick = 0
            for account in db.list_accounts():
                with contextlib.suppress(Exception):
                    if account.get("status") == "active" and _restore_automation_snapshot(int(account["id"])):
                        recover = getattr(bot, "_recover_account_features", None)
                        if recover:
                            await recover(int(account["id"]))
                    else:
                        await _remove_confirmed_invalid(bot, account)
        await asyncio.sleep(interval)
