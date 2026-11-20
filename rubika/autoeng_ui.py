"""
🔁 Automation Engine — Telegram UI (ISOLATED, additive; register(bot)).
========================================================================

All the panel wiring for the new engine lives here so bot.py only needs three
tiny hooks (open the menu from the Automation button, register(...) in amain,
and restore() on boot). Same idiom as caption_file.register(bot).

Menu (agreed with the owner):
  🔁 Automation           ← opens THIS, replacing the old sub-menu
    [▶ Start] [⏹ Stop]
    [👥 Accounts] [📝 Texts]
    [⚙ Settings] [♻ Reset]
    [🔙 Back]
  👥 Accounts : tick accounts (paged), min 4; quarantined shown 🔴, unpickable.
  📝 Texts    : [📢 Ad Texts] [💬 PV Reply]   (PV reply lives under Texts)
  ⚙ Settings  : [⏱ Send Time] [📦 Groups/Account] [🔢 Scan Count]
                [🔗 Linkdooni] [🗂 Saved Groups]

Ad text may be a FILE+CAPTION too (reuses the existing caption_file module).

Live card: a NEW pinned message is created each Start (we do not chase the old
one) and edited every 30s by bot.autoeng_render_live (attached here). Errors
go out as separate coded messages (autoeng_log.error_card), posted by the
engine via bot.log.
"""
from __future__ import annotations

import autoeng as eng
import autoeng_core as core
import autoeng_db as adb
import autoeng_log as live

PAGE = 8                      # accounts per page
_await: dict = {}             # owner_id -> step string ("ad_text"/"pv_text"/...)
_registered = False


# --------------------------------------------------------------------------- #
# selection helpers (selected account ids persisted in the engine DB)
# --------------------------------------------------------------------------- #
def _selected_ids() -> list:
    raw = adb.get("selected_ids", "") or ""
    out = []
    for p in raw.split(","):
        p = p.strip()
        if p.isdigit():
            out.append(int(p))
    return out


def _save_selected(ids: list) -> None:
    adb.set("selected_ids", ",".join(str(i) for i in ids))


def _toggle(aid: int) -> None:
    ids = _selected_ids()
    if aid in ids:
        ids.remove(aid)
    else:
        ids.append(aid)
    _save_selected(ids)


def _is_running() -> bool:
    return eng.engine.running or adb.get_bool("running", False)


# --------------------------------------------------------------------------- #
# text builders (pure where possible)
# --------------------------------------------------------------------------- #
def home_text(bot) -> str:
    adb.init()
    sel = _selected_ids()
    roles = core.assign_roles(sel)
    n_scan = len([a for a in roles.values() if a == core.ROLE_SCANNER])
    n_send = len([a for a in roles.values() if a == core.ROLE_SENDER])
    saved = adb.count_unused_healthy()
    status = "🟢 RUNNING" if _is_running() else "⏹ OFF"
    rows = [
        f"• Status   : {status}",
        f"• Accounts : {len(sel)} selected (min {core.MIN_ACCOUNTS})",
        f"• Roles    : 🔍 {n_scan} scan · 📤 {n_send} send",
        f"• Texts    : {len(adb.list_texts())} · PV reply: "
        f"{'on' if adb.get_bool('pv_enabled') else 'off'}",
        f"• Saved    : {saved}/{eng.RESERVE_TARGET} groups",
    ]
    return bot.panel_card("⚙ - #automation", rows, footer=f"🕒 {bot.now()}")


def home_buttons(bot):
    B = bot.Button
    if _is_running():
        return [[B.inline("⏹ Stop", b"ae_stop")],
                [B.inline("🔄 Refresh", b"ae_home")],
                [B.inline("🔙 Back", b"home")]]
    return [
        [B.inline("▶ Start", b"ae_start"), B.inline("⏹ Stop", b"ae_stop")],
        [B.inline("👥 Accounts", b"ae_accts_0"), B.inline("📝 Texts", b"ae_texts")],
        [B.inline("⚙ Settings", b"ae_settings"), B.inline("♻ Reset", b"ae_reset")],
        [B.inline("🔙 Back", b"home")],
    ]


async def show_home(bot, event):
    await bot.safe_edit(event, home_text(bot), buttons=home_buttons(bot))


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def register(bot) -> None:
    global _registered
    if _registered:
        return
    _registered = True
    adb.init()
    events, client, B = bot.events, bot.bot, bot.Button

    def owner(event):
        return bot.is_owner(event)

    # ---- attach the live-card renderer the engine calls every 30s ---------- #
    async def autoeng_render_live(state):
        bodies = live.render_live(state)
        ids = [i for i in (adb.get("live_msg_ids", "") or "").split(",") if i]
        # (re)create if we have no stored ids or the count changed.
        if len(ids) != len(bodies):
            ids = []
            for b in bodies:
                try:
                    m = await client.send_message(bot.config.LOG_GROUP_ID, b)
                    ids.append(str(m.id))
                    try:
                        await client.pin_message(bot.config.LOG_GROUP_ID, m,
                                                 notify=False)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
            adb.set("live_msg_ids", ",".join(ids))
            return
        for mid, b in zip(ids, bodies):
            try:
                await client.edit_message(bot.config.LOG_GROUP_ID, int(mid), b)
            except Exception:  # noqa: BLE001 - 'not modified' etc. are harmless
                pass

    bot.autoeng_render_live = autoeng_render_live

    # ---- home ------------------------------------------------------------- #
    @client.on(events.CallbackQuery(data=b"ae_home"))
    async def _home(event):
        if not owner(event):
            return
        _await.pop(event.sender_id, None)
        await show_home(bot, event)

    # ---- start / confirm -------------------------------------------------- #
    @client.on(events.CallbackQuery(data=b"ae_start"))
    async def _start(event):
        if not owner(event):
            return
        sel = _selected_ids()
        problems = []
        if len(sel) < core.MIN_ACCOUNTS:
            problems.append(f"select at least {core.MIN_ACCOUNTS} accounts")
        if not adb.list_texts():
            problems.append("add at least one ad text")
        if not (adb.get("channels", "") or "").strip():
            problems.append("set at least one linkdooni source")
        if problems:
            await bot.safe_edit(event, bot.card("▶ Cannot start yet", [
                "• " + p for p in problems] + [bot.LINE, f"🕒 {bot.now()}"]),
                buttons=[[B.inline("🔙 Back", b"ae_home")]])
            return
        roles = core.assign_roles(sel)
        n_scan = len([a for a in roles.values() if a == core.ROLE_SCANNER])
        groups_per = adb.get_int("groups_per", 3)
        await bot.safe_edit(event, bot.card("▶ Start automation?", [
            f"👥 {len(sel)} accounts · 🔍 {n_scan} scan · 📤 {len(sel) - n_scan} send",
            f"📦 {groups_per} groups/account · 🗂 {adb.count_unused_healthy()} ready",
            f"📝 {len(adb.list_texts())} text(s) · 💬 PV reply: "
            f"{'on' if adb.get_bool('pv_enabled') else 'off'}",
            bot.LINE, f"🕒 {bot.now()}"]),
            buttons=[[B.inline("✅ Confirm", b"ae_confirm"),
                      B.inline("❌ Cancel", b"ae_home")]])

    @client.on(events.CallbackQuery(data=b"ae_confirm"))
    async def _confirm(event):
        if not owner(event):
            return
        if _is_running():
            await event.answer("already running")
            return await show_home(bot, event)
        # fresh live message each start.
        adb.set("live_msg_ids", "")
        import bot as _self
        ok, msg = await eng.start(_self, _selected_ids())
        await event.answer(msg[:80])
        await show_home(bot, event)

    # ---- stop / reset ----------------------------------------------------- #
    @client.on(events.CallbackQuery(data=b"ae_stop"))
    async def _stop(event):
        if not owner(event):
            return
        import bot as _self
        await eng.stop(_self)
        adb.set("live_msg_ids", "")
        await event.answer("stopped")
        await show_home(bot, event)

    @client.on(events.CallbackQuery(data=b"ae_reset"))
    async def _reset(event):
        if not owner(event):
            return
        if _is_running():
            await event.answer("stop the engine first")
            return
        await bot.safe_edit(event, bot.card("♻ Reset engine?", [
            "Clears roles, link queue, group assignments and counters.",
            "KEEPS your saved healthy groups.",
            bot.LINE, f"🕒 {bot.now()}"]),
            buttons=[[B.inline("✅ Reset", b"ae_reset_yes"),
                      B.inline("🗑 Reset + wipe groups", b"ae_reset_wipe")],
                     [B.inline("🔙 Back", b"ae_home")]])

    @client.on(events.CallbackQuery(data=b"ae_reset_yes"))
    async def _reset_yes(event):
        if not owner(event):
            return
        eng.reset(keep_groups=True)
        await event.answer("reset (groups kept)")
        await show_home(bot, event)

    @client.on(events.CallbackQuery(data=b"ae_reset_wipe"))
    async def _reset_wipe(event):
        if not owner(event):
            return
        eng.reset(keep_groups=False)
        await event.answer("reset + groups wiped")
        await show_home(bot, event)

    # ---- accounts (paged tick list) --------------------------------------- #
    @client.on(events.CallbackQuery(pattern=b"ae_accts_(\\d+)"))
    async def _accounts(event):
        if not owner(event):
            return
        if _is_running():
            await event.answer("stop the engine to change accounts")
            return
        page = int(event.pattern_match.group(1))
        accs = bot.db.list_accounts()
        sel = set(_selected_ids())
        total = len(accs)
        start = page * PAGE
        chunk = accs[start:start + PAGE]
        rows = []
        for a in chunk:
            aid = a["id"]
            quarantined = (a.get("status") or "").lower() in ("quarantined", "dead")
            if quarantined:
                label = f"🔴 {a.get('phone')} · {a.get('status')}"
                rows.append([B.inline(label, b"ae_noop")])
                continue
            mark = "✅" if aid in sel else "⬜"
            try:
                w = bot.worker.worker_for_account(a) if hasattr(bot, "worker") else None
                wtag = (w.get("tag") if w and not bot.worker.is_local(w)
                        else "local") if w else "local"
            except Exception:  # noqa: BLE001 - never break the menu on a worker hiccup
                wtag = "?"
            label = f"{mark} {a.get('phone')} · {wtag}"
            rows.append([B.inline(label, f"ae_tog_{aid}".encode())])
        nav = []
        if start > 0:
            nav.append(B.inline("◀ Prev", f"ae_accts_{page - 1}".encode()))
        if start + PAGE < total:
            nav.append(B.inline("Next ▶", f"ae_accts_{page + 1}".encode()))
        if nav:
            rows.append(nav)
        rows.append([B.inline("✅ Select all", b"ae_all"),
                     B.inline("⬜ Clear", b"ae_clr")])
        rows.append([B.inline("🔙 Back", b"ae_home")])
        head = bot.card("👥 Accounts", [
            f"• {len(sel)} selected (min {core.MIN_ACCOUNTS})",
            f"• page {page + 1}/{max(1, (total + PAGE - 1) // PAGE)}"])
        await bot.safe_edit(event, head, buttons=rows)

    @client.on(events.CallbackQuery(data=b"ae_noop"))
    async def _noop(event):
        if owner(event):
            await event.answer("quarantined — not selectable")

    @client.on(events.CallbackQuery(pattern=b"ae_tog_(\\d+)"))
    async def _tog(event):
        if not owner(event):
            return
        if _is_running():
            await event.answer("stop the engine to change accounts")
            return
        _toggle(int(event.pattern_match.group(1)))
        # redraw current page (page 0 is fine; re-render keeps it simple)
        event.pattern_match = type("M", (), {"group": lambda _s, _i: "0"})()
        await _accounts(event)

    @client.on(events.CallbackQuery(data=b"ae_all"))
    async def _all(event):
        if not owner(event):
            return
        if _is_running():
            return await event.answer("stop the engine first")
        ids = [a["id"] for a in bot.db.list_accounts()
               if (a.get("status") or "").lower() not in ("quarantined", "dead")]
        _save_selected(ids)
        event.pattern_match = type("M", (), {"group": lambda _s, _i: "0"})()
        await _accounts(event)

    @client.on(events.CallbackQuery(data=b"ae_clr"))
    async def _clr(event):
        if not owner(event):
            return
        if _is_running():
            return await event.answer("stop the engine first")
        _save_selected([])
        event.pattern_match = type("M", (), {"group": lambda _s, _i: "0"})()
        await _accounts(event)

    # ---- texts (ad texts + PV reply) -------------------------------------- #
    @client.on(events.CallbackQuery(data=b"ae_texts"))
    async def _texts(event):
        if not owner(event):
            return
        _await.pop(event.sender_id, None)
        await bot.safe_edit(event, bot.card("📝 Texts", [
            f"• Ad texts : {len(adb.list_texts())}",
            f"• PV reply : {'on' if adb.get_bool('pv_enabled') else 'off'}",
            bot.LINE, "Several ad texts rotate across sends."]),
            buttons=[[B.inline("📢 Ad Texts", b"ae_adtexts"),
                      B.inline("💬 PV Reply", b"ae_pv")],
                     [B.inline("🔙 Back", b"ae_home")]])

    @client.on(events.CallbackQuery(data=b"ae_adtexts"))
    async def _adtexts(event):
        if not owner(event):
            return
        _await[event.sender_id] = "ad_text"
        preview = "\n".join(f"  {i+1}. {t[:50]}" for i, t in
                            enumerate(adb.list_texts())) or "  (none)"
        await bot.safe_edit(event, bot.card("📢 Ad Texts", [
            preview, bot.LINE,
            "Send a TEXT to add it. Several rotate across sends.",
            "A FILE + caption also works (stored as the file-send text)."]),
            buttons=[[B.inline("🗑 Clear all", b"ae_clrtexts")],
                     [B.inline("🔙 Back", b"ae_texts")]])

    @client.on(events.CallbackQuery(data=b"ae_clrtexts"))
    async def _clrtexts(event):
        if not owner(event):
            return
        adb.clear_texts()
        adb.set("text_is_file", "false")
        await event.answer("cleared")
        await _adtexts(event)

    @client.on(events.CallbackQuery(data=b"ae_pv"))
    async def _pv(event):
        if not owner(event):
            return
        cur = adb.get("pv_text", "") or "(none)"
        await bot.safe_edit(event, bot.card("💬 PV Reply", [
            f"• Status : {'on' if adb.get_bool('pv_enabled') else 'off'}",
            f"• Text   : {cur[:80]}",
            bot.LINE, "Auto-reply once per person who PVs an ad."]),
            buttons=[[B.inline(("🔴 Turn off" if adb.get_bool('pv_enabled')
                                else "🟢 Turn on"), b"ae_pvtog")],
                     [B.inline("✍ Set reply text", b"ae_pvtext")],
                     [B.inline("🔙 Back", b"ae_texts")]])

    @client.on(events.CallbackQuery(data=b"ae_pvtog"))
    async def _pvtog(event):
        if not owner(event):
            return
        adb.set("pv_enabled", "false" if adb.get_bool("pv_enabled") else "true")
        await _pv(event)

    @client.on(events.CallbackQuery(data=b"ae_pvtext"))
    async def _pvtext(event):
        if not owner(event):
            return
        _await[event.sender_id] = "pv_text"
        await bot.safe_edit(event, bot.card("✍ PV reply text", [
            "Send the text to auto-reply to a new PV."]),
            buttons=[[B.inline("🔙 Back", b"ae_pv")]])

    # ---- settings --------------------------------------------------------- #
    @client.on(events.CallbackQuery(data=b"ae_settings"))
    async def _settings(event):
        if not owner(event):
            return
        _await.pop(event.sender_id, None)
        await bot.safe_edit(event, bot.card("⚙ Settings", [
            f"• Send time (interval s) : {adb.get_int('send_interval', eng.SEND_INTERVAL_DEFAULT)}",
            f"• Groups per account     : {adb.get_int('groups_per', 3)}",
            f"• Scan count (new links) : {adb.get_int('scan_count', 50)}",
            f"• Linkdooni sources      : {len([c for c in (adb.get('channels','') or '').split(',') if c.strip()])}",
            f"• Saved groups           : {adb.count_unused_healthy()}"]),
            buttons=[[B.inline("⏱ Send Time", b"ae_set_interval"),
                      B.inline("📦 Groups/Account", b"ae_set_groups")],
                     [B.inline("🔢 Scan Count", b"ae_set_scan"),
                      B.inline("🔗 Linkdooni", b"ae_set_channels")],
                     [B.inline("🗂 Saved Groups", b"ae_saved")],
                     [B.inline("🔙 Back", b"ae_home")]])

    def _ask(step, title, hint, back=b"ae_settings"):
        async def _h(event):
            if not owner(event):
                return
            _await[event.sender_id] = step
            await bot.safe_edit(event, bot.card(title, [hint]),
                                buttons=[[B.inline("🔙 Back", back)]])
        return _h

    client.on(events.CallbackQuery(data=b"ae_set_interval"))(
        _ask("send_interval", "⏱ Send Time",
             "Send a number: seconds between send passes per account (e.g. 60)."))
    client.on(events.CallbackQuery(data=b"ae_set_groups"))(
        _ask("groups_per", "📦 Groups per Account",
             "Send a number: how many groups each sender gets (e.g. 3)."))
    client.on(events.CallbackQuery(data=b"ae_set_scan"))(
        _ask("scan_count", "🔢 Scan Count",
             "Send a number: how many NEW links to extract per round (e.g. 50)."))
    client.on(events.CallbackQuery(data=b"ae_set_channels"))(
        _ask("channels", "🔗 Linkdooni Sources",
             "Send the linkdooni source links/refs, comma or space separated."))

    @client.on(events.CallbackQuery(data=b"ae_saved"))
    async def _saved(event):
        if not owner(event):
            return
        n = adb.count_unused_healthy()
        await bot.safe_edit(event, bot.card("🗂 Saved Groups", [
            f"• Unused healthy groups : {n}",
            f"• Reserve target        : {eng.RESERVE_TARGET}",
            bot.LINE, "Kept across runs; wiped only via Reset + wipe."]),
            buttons=[[B.inline("🔙 Back", b"ae_settings")]])

    # ---- text input capture (own handler, gated — no bot.py router edit) --- #
    @client.on(events.NewMessage())
    async def _capture(event):
        step = _await.get(event.sender_id)
        if not step or not owner(event):
            return
        # a FILE for ad text -> reuse caption_file storage, register it as text
        if step == "ad_text" and getattr(event, "message", None) and \
                getattr(event.message, "media", None):
            _await.pop(event.sender_id, None)
            try:
                import caption_file
                import tempfile
                import os
                tmp = tempfile.mkdtemp(prefix="aetext_")
                path = await event.message.download_media(file=tmp)
                with open(path, "rb") as fh:
                    data = fh.read()
                cap = (getattr(event.message, "text", "") or "").strip()
                caption_file.set_file(data, cap, os.path.basename(path) or "file")
                adb.clear_texts()
                adb.add_text("[FILE] " + (cap or "(file+caption)"))
                adb.set("text_is_file", "true")   # sends will upload the file
                await event.respond(bot.card("📢 Ad Text — file stored ✅", [
                    "This send will upload the file + caption.",
                    f"🕒 {bot.now()}"]),
                    buttons=[[B.inline("🔙 Back", b"ae_texts")]])
            except Exception as e:  # noqa: BLE001
                await event.respond(f"error: {repr(e)[:120]}")
            return
        txt = (getattr(event, "raw_text", "") or getattr(event, "text", "") or "").strip()
        if not txt:
            return
        _await.pop(event.sender_id, None)
        if step == "ad_text":
            if adb.get_bool("text_is_file", False):
                # switching from a file to plain text(s): drop the file marker.
                adb.clear_texts()
                adb.set("text_is_file", "false")
            adb.add_text(txt)
            await event.respond(bot.card("📢 Ad Text added ✅",
                                [f"total: {len(adb.list_texts())}", f"🕒 {bot.now()}"]),
                                buttons=[[B.inline("🔙 Back", b"ae_texts")]])
        elif step == "pv_text":
            adb.set("pv_text", txt)
            adb.set("pv_enabled", "true")
            await event.respond(bot.card("💬 PV reply saved ✅",
                                ["PV auto-reply turned on.", f"🕒 {bot.now()}"]),
                                buttons=[[B.inline("🔙 Back", b"ae_texts")]])
        elif step == "channels":
            adb.set("channels", ",".join(
                p.strip() for p in txt.replace("\n", ",").replace(" ", ",").split(",")
                if p.strip()))
            await event.respond(bot.card("🔗 Linkdooni sources saved ✅",
                                [f"🕒 {bot.now()}"]),
                                buttons=[[B.inline("🔙 Back", b"ae_settings")]])
        elif step in ("send_interval", "groups_per", "scan_count"):
            if not txt.isdigit():
                await event.respond("send a number, please")
                _await[event.sender_id] = step
                return
            adb.set(step, int(txt))
            await event.respond(bot.card("⚙ Saved ✅",
                                [f"{step} = {int(txt)}", f"🕒 {bot.now()}"]),
                                buttons=[[B.inline("🔙 Back", b"ae_settings")]])


# --------------------------------------------------------------------------- #
# boot restore (called from amain): relaunch if it was running before restart.
# --------------------------------------------------------------------------- #
async def restore(bot) -> None:
    adb.init()
    if not adb.get_bool("running", False):
        return
    ids = _selected_ids()
    if len(ids) < core.MIN_ACCOUNTS:
        adb.set("running", "0")
        return
    adb.set("live_msg_ids", "")
    try:
        import bot as _self
        await eng.start(_self, ids)
    except Exception:  # noqa: BLE001
        adb.set("running", "0")
