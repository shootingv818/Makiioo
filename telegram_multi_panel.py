"""Small additive panel adapter for telegram_multi_send."""
from __future__ import annotations

import contextlib

import db
import telegram_multi_send as multi

_registered = False
_selected: dict[int, list[int]] = {}


def register(*, client, events, Button, state, is_owner, safe_edit) -> None:
    global _registered
    if _registered:
        return
    _registered = True

    def accounts_for(user_id: int) -> list[dict]:
        rows = [row for row in db.tg_list_accounts() if row.get("status") == "active" and row.get("session")]
        valid = {int(row["rid"]) for row in rows}
        chosen = _selected.setdefault(user_id, [])
        chosen[:] = [rid for rid in chosen if rid in valid]
        return rows

    def select_text(user_id: int) -> tuple[str, list]:
        rows = accounts_for(user_id)
        chosen = _selected.setdefault(user_id, [])
        text = (
            "✈️ ارسال چنداکانتی تلگرام\n"
            "━━━━━━━━━━━━\n"
            "اکانت‌ها را به ترتیب اجرا انتخاب کن. هر اکانت فقط به مخاطبان خودش و دونه‌به‌دونه ارسال می‌کند."
        )
        buttons = []
        for row in rows:
            mark = "✅" if int(row["rid"]) in chosen else "▫️"
            buttons.append([Button.inline(
                f"{mark} {row['phone']} — {row.get('name') or '—'}",
                f"tgmsel_{row['rid']}".encode(),
            )])
        if chosen:
            buttons.append([Button.inline(f"شروع با {len(chosen)} اکانت ←", b"tgmnext")])
        buttons.extend([
            [Button.inline("📊 وضعیت ارسال‌ها", b"tg_multi_jobs")],
            [Button.inline("‹ بازگشت", b"tg")],
        ])
        if not rows:
            text += "\n\nاکانت فعال تلگرام موجود نیست."
        return text, buttons

    def jobs_view() -> tuple[str, list]:
        jobs = multi.list_jobs(limit=8)
        lines = ["📊 ارسال‌های چنداکانتی تلگرام", "━━━━━━━━━━━━"]
        buttons = []
        if not jobs:
            lines.append("هنوز ارسالی ثبت نشده است.")
        for job in jobs:
            jid, job_state = job["job_id"], job["state"]
            lines.append(
                f"• {jid[:8]} | {job_state} | ✅ {job['sent_count']}/{job['total']} "
                f"| ❌ {job['failed_count']} | ⚠️ {job['uncertain_count']}"
            )
            if job_state in ("queued", "running", "waiting", "stop_requested"):
                buttons.append([Button.inline(f"⏹ توقف {jid[:8]}", f"tgmstop_{jid}".encode())])
            elif job_state in ("paused", "failed"):
                buttons.append([Button.inline(f"▶️ ادامه {jid[:8]}", f"tgmresume_{jid}".encode())])
        buttons.extend([
            [Button.inline("♻️ بروزرسانی", b"tg_multi_jobs")],
            [Button.inline("‹ بازگشت", b"tg_multi")],
        ])
        return "\n".join(lines), buttons

    @client.on(events.CallbackQuery(data=b"tg_multi"))
    async def open_multi(event):
        if not is_owner(event):
            return
        state.pop(event.sender_id, None)
        text, buttons = select_text(event.sender_id)
        await safe_edit(event, text, buttons=buttons)

    @client.on(events.CallbackQuery(pattern=rb"tgmsel_(\d+)"))
    async def toggle_account(event):
        if not is_owner(event):
            return
        rid = int(event.pattern_match.group(1))
        valid = {int(row["rid"]) for row in accounts_for(event.sender_id)}
        if rid not in valid:
            await event.answer("اکانت فعال پیدا نشد.", alert=True)
            return
        chosen = _selected.setdefault(event.sender_id, [])
        if rid in chosen:
            chosen.remove(rid)
        else:
            chosen.append(rid)
        text, buttons = select_text(event.sender_id)
        await safe_edit(event, text, buttons=buttons)

    @client.on(events.CallbackQuery(data=b"tgmnext"))
    async def start_multi(event):
        if not is_owner(event):
            return
        valid = {int(row["rid"]): row for row in accounts_for(event.sender_id)}
        chosen = [valid[rid]["phone"] for rid in _selected.get(event.sender_id, []) if rid in valid]
        if not chosen:
            await event.answer("حداقل یک اکانت انتخاب کن.", alert=True)
            return
        messages = db.tg_msgs_get()
        if not messages:
            await event.answer("محتوای ارسال عادی خالی است؛ ابتدا متن یا فایل را در بخش ارسال تنظیم کن.", alert=True)
            return
        state.pop(event.sender_id, None)
        await safe_edit(
            event,
            "⏳ مخاطبان مستقل هر اکانت با ترتیب دوطرفه‌ها در حال آماده‌سازی است...",
            buttons=[[Button.inline("📊 وضعیت ارسال‌ها", b"tg_multi_jobs")]],
        )
        try:
            job = await multi.create_job(account_phones=chosen, content={"items": messages})
            await multi.start(job["job_id"])
        except Exception as exc:
            await safe_edit(
                event,
                f"❌ شروع ارسال ناموفق بود: {type(exc).__name__}: {str(exc)[:160]}",
                buttons=[[Button.inline("‹ بازگشت", b"tg_multi")]],
            )
            return
        _selected.pop(event.sender_id, None)
        await safe_edit(
            event,
            f"✅ ارسال ترتیبی شروع شد.\nاکانت‌ها: {len(chosen)} | مخاطبان: {job['total']} | دوطرفه: {job['mutual_total']}",
            buttons=[
                [Button.inline("📊 وضعیت ارسال‌ها", b"tg_multi_jobs")],
                [Button.inline("‹ پنل تلگرام", b"tg")],
            ],
        )

    @client.on(events.CallbackQuery(data=b"tg_multi_jobs"))
    async def show_jobs(event):
        if not is_owner(event):
            return
        text, buttons = jobs_view()
        await safe_edit(event, text, buttons=buttons)

    @client.on(events.CallbackQuery(pattern=rb"tgmstop_([a-f0-9]+)"))
    async def stop_job(event):
        if not is_owner(event):
            return
        jid = event.pattern_match.group(1).decode()
        with contextlib.suppress(Exception):
            await multi.stop(jid)
        text, buttons = jobs_view()
        await safe_edit(event, text, buttons=buttons)

    @client.on(events.CallbackQuery(pattern=rb"tgmresume_([a-f0-9]+)"))
    async def resume_job(event):
        if not is_owner(event):
            return
        jid = event.pattern_match.group(1).decode()
        with contextlib.suppress(Exception):
            await multi.resume(jid)
        text, buttons = jobs_view()
        await safe_edit(event, text, buttons=buttons)
