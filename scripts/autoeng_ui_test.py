#!/usr/bin/env python3
"""
🔁 Automation Engine — UI logic tests (offline, fake Telethon bot).
===================================================================

Telethon isn't installed in dev, so this fakes the bot surface autoeng_ui
needs (client.on decorator, send/edit/pin, Button, card helpers) and checks the
pure UI logic: account selection, home render, start-guard messages, the live
pinned-message create/edit lifecycle, and restore gating.

  python3 scripts/autoeng_ui_test.py --selftest
"""
import os
import sys
import asyncio
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "rubika"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

import autoeng_db as adb
import autoeng as eng
import autoeng_ui as ui

_passed = 0
_failed = 0


def ok(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  ✗ FAIL: {msg}")


def eq(a, b, msg):
    ok(a == b, f"{msg} (got {a!r}, want {b!r})")


# --------------------------------------------------------------------------- #
# Fake Telethon bot surface
# --------------------------------------------------------------------------- #
class _Btn:
    @staticmethod
    def inline(label, data=None):
        return ("btn", label, data)


class _Events:
    def CallbackQuery(self, **k):
        return ("cb", k)

    def NewMessage(self, **k):
        return ("nm", k)


class _Msg:
    _seq = 0

    def __init__(self, text):
        _Msg._seq += 1
        self.id = _Msg._seq
        self.text = text


class _Client:
    def __init__(self):
        self.handlers = []
        self.sent = []
        self.edits = []
        self.pinned = []

    def on(self, spec):
        def deco(fn):
            self.handlers.append((spec, fn))
            return fn
        return deco

    async def send_message(self, chat, text, **k):
        self.sent.append((chat, text))
        return _Msg(text)

    async def edit_message(self, chat, mid, text, **k):
        self.edits.append((chat, mid, text))
        return True

    async def pin_message(self, chat, msg, **k):
        self.pinned.append(getattr(msg, "id", msg))
        return True


class _Cfg:
    LOG_GROUP_ID = -100123
    OWNER_ID = 777


class _FakeBot:
    def __init__(self, accounts=None):
        self.bot = _Client()
        self.events = _Events()
        self.Button = _Btn
        self.config = _Cfg
        self.LINE = "-------------------------------"
        self._accounts = accounts or []

        class _DB:
            def list_accounts(_s):
                return self._accounts

            def get_account(_s, aid):
                for a in self._accounts:
                    if a["id"] == int(aid):
                        return a
                return None
        self.db = _DB()

        class _W:
            def worker_for_account(_s, a):
                return None

            def is_local(_s, w):
                return True
        self.worker = _W()

    def is_owner(self, event):
        return True

    def now(self):
        return "00:00:00"

    def card(self, title, rows):
        return title + "\n" + "\n".join(rows)

    def panel_card(self, tag, rows, footer=None):
        return f"| {tag}\n" + "\n".join(rows) + (f"\n{footer}" if footer else "")

    async def safe_edit(self, event, *a, **k):
        return None

    async def log(self, text):
        pass


def _fresh():
    tmp = tempfile.mkdtemp(prefix="aeui_")
    adb.set_db_path_for_tests(os.path.join(tmp, "autoeng.db"))
    adb.init()
    ui._registered = False
    ui._await.clear()
    eng.engine.running = False


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_selection():
    _fresh()
    eq(ui._selected_ids(), [], "empty selection")
    ui._toggle(3)
    ui._toggle(5)
    eq(ui._selected_ids(), [3, 5], "toggle adds")
    ui._toggle(3)
    eq(ui._selected_ids(), [5], "toggle removes")
    ui._save_selected([1, 2, 3, 4])
    eq(ui._selected_ids(), [1, 2, 3, 4], "save/load selection")


def test_home_render():
    _fresh()
    ui._save_selected([1, 2, 3, 4, 5])
    adb.add_text("hi")
    bot = _FakeBot()
    txt = ui.home_text(bot)
    ok("5 selected" in txt, "home shows selection count")
    ok("🔍 2 scan · 📤 3 send" in txt, "home shows role split")
    btns = ui.home_buttons(bot)
    labels = [b[1] for row in btns for b in row]
    ok("▶ Start" in labels and "♻ Reset" in labels, "home has engine buttons")
    # running -> only Stop/Refresh/Back
    eng.engine.running = True
    labels2 = [b[1] for row in ui.home_buttons(bot) for b in row]
    ok("⏹ Stop" in labels2 and "▶ Start" not in labels2, "running hides Start")
    eng.engine.running = False


def test_register_wires_handlers():
    _fresh()
    bot = _FakeBot()
    ui.register(bot)
    ok(len(bot.bot.handlers) >= 15, f"many handlers registered ({len(bot.bot.handlers)})")
    ok(hasattr(bot, "autoeng_render_live"), "live renderer attached to bot")


def test_live_message_lifecycle():
    _fresh()
    bot = _FakeBot()
    ui.register(bot)
    # a small state -> one pinned message created + id stored
    state = {"running": True, "elapsed": 10, "clock": "00:00:10",
             "scan": {"extracted": 10, "checked": 5, "healthy": 3, "dead": 2,
                      "approval": 0, "error": 0},
             "saved": 3, "saved_max": 30,
             "totals": {"sent": 1, "fail": 0, "pv_in": 0, "pv_out": 0},
             "scanners": [{"tag": "A1", "worker": "local", "checked": 5,
                           "assigned": 10, "ok": 3}],
             "senders": [{"tag": "A2", "worker": "local", "groups": 1,
                          "groups_target": 2, "sent": 1, "fail": 0,
                          "pv_in": 0, "pv_out": 0}]}
    run(bot.autoeng_render_live(state))
    eq(len(bot.bot.sent), 1, "first render creates one message")
    eq(len(bot.bot.pinned), 1, "message pinned")
    ids = adb.get("live_msg_ids", "")
    ok(ids, "live message id stored")
    # second render edits in place (no new send)
    run(bot.autoeng_render_live(state))
    eq(len(bot.bot.sent), 1, "second render does NOT create a new message")
    ok(len(bot.bot.edits) >= 1, "second render edits the existing message")


def test_restore_gating():
    _fresh()
    bot = _FakeBot()
    # not running -> restore is a no-op (no crash, no start)
    run(ui.restore(bot))
    ok(not eng.engine.running, "restore no-op when not flagged running")
    # flagged running but too few accounts -> clears the flag, does not start
    adb.set("running", "true")
    ui._save_selected([1, 2])
    run(ui.restore(bot))
    ok(not adb.get_bool("running"), "restore clears running when <min accounts")


def main():
    if "--selftest" not in sys.argv:
        print("usage: autoeng_ui_test.py --selftest")
        return 0
    for fn in (test_selection, test_home_render, test_register_wires_handlers,
               test_live_message_lifecycle, test_restore_gating):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            global _failed
            _failed += 1
            import traceback
            print(f"  ✗ EXCEPTION in {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n  ==== {_passed} passed, {_failed} failed ====\n")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
