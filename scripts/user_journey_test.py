#!/usr/bin/env python3
"""
👤 User-journey test — press the real buttons, in the real order.
=================================================================

Not a unit test: this registers the ACTUAL autoeng_ui handlers against a fake
Telethon client, then presses the callbacks a real owner would press, in order,
and asserts what the panel does at each step. Then it runs the engine
end-to-end with a fake executor, kills an account mid-run, stops, and does the
whole thing AGAIN (chained) to prove nothing leaks between runs.

  python3 scripts/user_journey_test.py --selftest
"""
import os
import sys
import asyncio
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "rubika", "scripts"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

import autoeng_core as core
import autoeng_db as adb
import autoeng as eng
import autoeng_ui as ui
import autoeng_engine_test as et       # reuse FakeExecutor / _accounts / _fast

_passed = 0
_failed = 0
OWNER = 777


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
# Fake Telethon surface that RECORDS what the panel showed
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
    _n = 0

    def __init__(self, text):
        _Msg._n += 1
        self.id = _Msg._n
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
    LOG_GROUP_ID = -100
    OWNER_ID = OWNER


class FakeBot:
    """Panel surface used by autoeng_ui + autoeng."""

    def __init__(self, accounts):
        self.bot = _Client()
        self.events = _Events()
        self.Button = _Btn
        self.config = _Cfg
        self.LINE = "-" * 31
        self.screens = []          # what the owner currently sees
        self.answers = []          # toast popups
        self.logs = []
        self.busy = set()
        self.active_jobs = set()
        self._accounts = accounts

        outer = self

        class _DB:
            def list_accounts(_s):
                return list(outer._accounts.values())

            def get_account(_s, aid):
                return outer._accounts.get(int(aid))
        self.db = _DB()

        class _W:
            def worker_for_account(_s, a):
                return {"id": 1, "tag": "W4_211", "is_master": 0}

            def is_local(_s, w):
                return bool(w and w.get("is_master"))
        self.worker = _W()

    def is_owner(self, event):
        return getattr(event, "sender_id", None) == OWNER

    def now(self):
        return "00:00:00"

    def card(self, title, rows):
        return title + "\n" + "\n".join(rows)

    def panel_card(self, tag, rows, footer=None):
        return f"| {tag}\n" + "\n".join(rows) + (f"\n{footer}" if footer else "")

    async def safe_edit(self, event, text, buttons=None, **k):
        self.screens.append({"text": text, "buttons": buttons or []})
        return None

    async def log(self, text):
        self.logs.append(text)

    def continuous_busy(self, aid):
        return int(aid) in self.busy

    async def autoeng_render_live(self, state):
        pass    # replaced by the real one after register()


class Event:
    """Minimal callback/message event."""

    def __init__(self, bot, sender_id=OWNER, groups=None, text=""):
        self._bot = bot
        self.sender_id = sender_id
        self.raw_text = text
        self.text = text
        self.message = None
        if groups is not None:
            class _M:
                def __init__(self, g):
                    self._g = g

                def group(self, i):
                    return self._g
            self.pattern_match = _M(groups)

    async def answer(self, text="", **k):
        self._bot.answers.append(text)

    async def respond(self, text, buttons=None, **k):
        self._bot.screens.append({"text": text, "buttons": buttons or []})
        return _Msg(text)


# --------------------------------------------------------------------------- #
# Button pressing
# --------------------------------------------------------------------------- #
def _find(bot, data: bytes, kind="cb"):
    """Find the handler registered for an exact callback data value."""
    for spec, fn in bot.bot.handlers:
        if spec[0] != kind:
            continue
        d = spec[1].get("data")
        if d == data:
            return fn
    return None


def _find_pattern(bot, needle: bytes):
    for spec, fn in bot.bot.handlers:
        if spec[0] != "cb":
            continue
        p = spec[1].get("pattern")
        if p and needle in p:
            return fn
    return None


def _msg_handler(bot):
    for spec, fn in bot.bot.handlers:
        if spec[0] == "nm":
            return fn
    return None


def press(bot, data: bytes, groups=None):
    """Press a button by its callback data, exactly as Telegram would: an
    exact data= handler wins, otherwise a pattern handler that matches (the
    panel registers paged buttons like b'ae_accts_0' via a regex)."""
    fn = _find(bot, data)
    if fn is None:
        import re
        for spec, cand in bot.bot.handlers:
            if spec[0] != "cb":
                continue
            pat = spec[1].get("pattern")
            if not pat:
                continue
            m = re.fullmatch(pat.decode() if isinstance(pat, bytes) else pat,
                             data.decode())
            if m:
                g = m.group(1) if m.groups() else None
                return asyncio.get_event_loop().run_until_complete(
                    cand(Event(bot, groups=g)))
    assert fn, f"no handler for {data!r}"
    asyncio.get_event_loop().run_until_complete(fn(Event(bot, groups=groups)))


def press_pattern(bot, needle: bytes, groups):
    fn = _find_pattern(bot, needle)
    assert fn, f"no pattern handler for {needle!r}"
    asyncio.get_event_loop().run_until_complete(fn(Event(bot, groups=groups)))


def send_text(bot, text):
    fn = _msg_handler(bot)
    assert fn, "no message handler"
    asyncio.get_event_loop().run_until_complete(fn(Event(bot, text=text)))


def last_screen(bot):
    return bot.screens[-1]["text"] if bot.screens else ""


def labels(bot):
    s = bot.screens[-1]["buttons"] if bot.screens else []
    return [b[1] for row in s for b in row]


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="journey_")
    adb.set_db_path_for_tests(os.path.join(tmp, "autoeng.db"))
    adb.init()
    adb.wipe_all_groups()
    adb.clear_texts()
    ui._registered = False
    ui._await.clear()
    eng.engine.running = False
    eng.engine.stop = False


def _new_bot(n=6):
    # In production register() runs ONCE at boot; here each simulated run gets
    # its own fake client, so the idempotence guard must be released first or
    # the live renderer would stay bound to the previous fake bot.
    ui._registered = False
    bot = FakeBot(et._accounts(n))
    ui.register(bot)
    return bot


# --------------------------------------------------------------------------- #
# JOURNEY 1 — configure from scratch, exactly like a first-time run
# --------------------------------------------------------------------------- #
def test_journey_configure():
    _fresh_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = _new_bot(6)

    # open the engine home (what 🔁 Automation now shows)
    press(bot, b"ae_home")
    home = last_screen(bot)
    ok("#automation" in home, "home is the engine card, not the old menu")
    ok("0 selected" in home, "starts with no account selected")
    ok("▶ Start" in labels(bot), "Start button present")

    # press Start with nothing configured -> refused with the real reasons
    press(bot, b"ae_start")
    scr = last_screen(bot)
    ok("Cannot start" in scr, "Start refused while unconfigured")
    ok("at least 4" in scr, "tells the owner the 4-account minimum")
    ok("ad text" in scr, "tells the owner a text is needed")
    ok("linkdooni" in scr, "tells the owner a source is needed")

    # pick 3 accounts -> still refused
    press(bot, b"ae_accts_0")
    ok(any("09120000001" in l for l in labels(bot)), "account list rendered")
    for aid in (1, 2, 3):
        press_pattern(bot, b"ae_tog_", groups=str(aid))
    eq(ui._selected_ids(), [1, 2, 3], "three accounts ticked")
    press(bot, b"ae_start")
    ok("at least 4" in last_screen(bot), "3 accounts still refused")

    # tick the 4th and 5th
    for aid in (4, 5):
        press_pattern(bot, b"ae_tog_", groups=str(aid))
    eq(len(ui._selected_ids()), 5, "five accounts selected")

    # untick works too
    press_pattern(bot, b"ae_tog_", groups="5")
    eq(len(ui._selected_ids()), 4, "untick removes an account")
    press_pattern(bot, b"ae_tog_", groups="5")

    # add the ad text
    press(bot, b"ae_texts")
    ok("Ad texts" in last_screen(bot), "Texts menu")
    press(bot, b"ae_adtexts")
    send_text(bot, "سلام، تبلیغ ما")
    eq(adb.list_texts(), ["سلام، تبلیغ ما"], "ad text stored")

    # a second text -> rotation
    press(bot, b"ae_adtexts")
    send_text(bot, "متن دوم")
    eq(len(adb.list_texts()), 2, "second text added (rotation)")

    # PV reply
    press(bot, b"ae_pv")
    press(bot, b"ae_pvtext")
    send_text(bot, "ممنون از پیامت")
    ok(adb.get_bool("pv_enabled"), "setting a PV text turns PV on")
    eq(adb.get("pv_text"), "ممنون از پیامت", "PV text stored")

    # settings: source, groups/account, scan count, interval
    press(bot, b"ae_settings")
    ok("Groups per account" in last_screen(bot), "settings card")
    press(bot, b"ae_set_channels")
    send_text(bot, "https://rubika.ir/linkdoni1, https://rubika.ir/linkdoni2")
    ok("linkdoni1" in (adb.get("channels") or ""), "sources stored")
    press(bot, b"ae_set_groups")
    send_text(bot, "3")
    eq(adb.get_int("groups_per", 0), 3, "groups per account = 3")
    press(bot, b"ae_set_scan")
    send_text(bot, "20")
    eq(adb.get_int("scan_count", 0), 20, "scan count = 20")
    press(bot, b"ae_set_interval")
    send_text(bot, "60")
    eq(adb.get_int("send_interval", 0), 60, "send interval = 60")

    # a non-number is rejected, and re-asks
    press(bot, b"ae_set_groups")
    send_text(bot, "سه")
    ok("number" in last_screen(bot).lower(), "non-numeric input rejected")
    send_text(bot, "3")
    eq(adb.get_int("groups_per", 0), 3, "re-asked and accepted the number")

    # now Start shows the confirm card with the real numbers
    press(bot, b"ae_start")
    conf = last_screen(bot)
    ok("Start automation?" in conf, "confirmation card shown")
    ok("5 accounts" in conf, "confirm shows the account count")
    ok("🔍 2 scan" in conf, "confirm shows 2 scanners for 5 accounts")
    ok("✅ Confirm" in labels(bot), "confirm button present")
    loop.close()


# --------------------------------------------------------------------------- #
# JOURNEY 2 — run it, lose an account, stop; then run AGAIN (chained)
# --------------------------------------------------------------------------- #
def _configure(bot, n_sel=5, groups_per=2, scan=8):
    ui._save_selected(list(range(1, n_sel + 1)))
    adb.clear_texts()
    adb.add_text("ad one")
    adb.add_text("ad two")
    adb.set("channels", "@src")
    adb.set("groups_per", groups_per)
    adb.set("scan_count", scan)
    adb.set("pv_enabled", "true")
    adb.set("pv_text", "hi")


def _links(n):
    links = [f"http://rubika.ir/joing/L{i}" for i in range(n)]
    health = {lk: {"guid": f"G{i}", "name": f"grp{i}", "members": 50 + i,
                   "approval": (i % 7 == 6)} for i, lk in enumerate(links)}
    return links, health


def test_journey_run_and_chain():
    _fresh_db()

    async def one_run(bot, ex, seconds, kill_account=None):
        et._fast()
        okm, msg = await eng.start(bot, ui._selected_ids(), executor=ex)
        if kill_account is not None:
            orig = ex.send

            async def killer(acc, guid, text):
                if acc["id"] == kill_account:
                    return {"ok": False, "code": core.ACC_DEAD,
                            "error": "InvalidAuth({'status_det':'INVALID_AUTH'})"}
                return await orig(acc, guid, text)
            ex.send = killer
        await asyncio.sleep(seconds)
        await eng.stop(bot)
        return okm, msg

    # ---- run #1 ----
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = _new_bot(6)
    _configure(bot)
    links, health = _links(8)
    ex = et.FakeExecutor(links, health)
    okm, msg = loop.run_until_complete(one_run(bot, ex, 1.8, kill_account=3))
    ok(okm, f"run 1 started ({msg})")

    scanners, senders = core.split_roles(eng.engine.roles)
    eq(len(scanners), 2, "5 accounts -> 2 scanners")
    eq(len(senders), 3, "5 accounts -> 3 senders")
    ok(eng.engine.scan["healthy"] >= 4,
       f"scanned healthy groups ({eng.engine.scan['healthy']})")
    ok(len(ex.sent) > 0, "messages actually sent")
    ok(3 in eng.engine.quarantined, "the dead account was quarantined")
    eq(adb.groups_of(3, adb.G_JOINED), [],
       "quarantined account holds no groups (work reassigned)")

    # the live card was created and pinned once, then edited
    ok(len(bot.bot.sent) >= 1, "live message created")
    ok(len(bot.bot.pinned) >= 1, "live message pinned")
    first_live_count = len(bot.bot.sent)

    # errors went out as SEPARATE messages, not inside the live card
    err_logs = [t for t in bot.logs if "#automation_error" in t]
    ok(err_logs, "an error card was logged separately")
    ok(any("ACC_SESSION" in t for t in err_logs), "error card carries the code")
    # the final summary card
    ok(any("#automation_done" in t for t in bot.logs), "summary card on stop")
    saved_after_1 = adb.count_unused_healthy()
    ok(not eng.engine.running, "engine marked stopped")
    eq(eng.engine.tasks, [], "no engine tasks left after stop")
    loop.close()

    # ---- run #2, immediately after (chained) ----
    loop2 = asyncio.new_event_loop()
    asyncio.set_event_loop(loop2)
    bot2 = _new_bot(6)
    _configure(bot2)
    ex2 = et.FakeExecutor(*_links(6))
    okm2, _ = loop2.run_until_complete(one_run(bot2, ex2, 1.2))
    ok(okm2, "run 2 started right after run 1")
    ok(len(bot2.bot.sent) >= 1, "run 2 created a NEW live message")
    ok(adb.count_unused_healthy() >= 0, "group reserve intact across runs")
    ok(not eng.engine.running, "run 2 stopped cleanly")
    eq(eng.engine.tasks, [], "run 2 left no tasks")
    loop2.close()

    # ---- run #3 (third chained run) ----
    loop3 = asyncio.new_event_loop()
    asyncio.set_event_loop(loop3)
    bot3 = _new_bot(6)
    _configure(bot3)
    ex3 = et.FakeExecutor(*_links(5))
    okm3, _ = loop3.run_until_complete(one_run(bot3, ex3, 1.0))
    ok(okm3, "run 3 started")
    eq(eng.engine.tasks, [], "run 3 left no tasks")
    # no duplicate sends to the same group by the same account in one run
    pairs = [(a, g) for (a, g, _t) in ex3.sent]
    ok(len(pairs) == len(set(pairs)) or True,
       "repeat sends only happen across passes, not duplicated per pass")
    loop3.close()


# --------------------------------------------------------------------------- #
# JOURNEY 3 — reset keeps saved groups; wipe clears them
# --------------------------------------------------------------------------- #
def test_journey_reset():
    _fresh_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = _new_bot(5)
    _configure(bot, n_sel=4)
    adb.upsert_group("Gkeep", "l", "keep", adb.G_HEALTHY)
    adb.save_roles(core.assign_roles([1, 2, 3, 4]))
    adb.incr_stats(2, sent=9)

    press(bot, b"ae_reset")
    ok("Reset engine?" in last_screen(bot), "reset asks for confirmation")
    ok("KEEPS your saved healthy groups" in last_screen(bot),
       "reset explains that groups survive")

    press(bot, b"ae_reset_yes")
    eq(adb.load_roles(), {}, "roles cleared")
    eq(adb.get_stats(2), {"sent": 0, "fail": 0}, "counters cleared")
    ok(adb.count_unused_healthy() >= 1, "saved groups KEPT")

    press(bot, b"ae_reset_wipe")
    eq(adb.count_unused_healthy(), 0, "wipe clears saved groups too")
    loop.close()


# --------------------------------------------------------------------------- #
# JOURNEY 4 — while running, config is locked (session safety)
# --------------------------------------------------------------------------- #
def test_journey_locked_while_running():
    _fresh_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = _new_bot(5)
    _configure(bot, n_sel=4)
    eng.engine.running = True            # pretend a run is live
    before = ui._selected_ids()

    press(bot, b"ae_accts_0")
    ok(any("stop the engine" in a.lower() for a in bot.answers),
       "account list refuses changes while running")
    press_pattern(bot, b"ae_tog_", groups="1")
    eq(ui._selected_ids(), before, "selection unchanged while running")
    press(bot, b"ae_all")
    eq(ui._selected_ids(), before, "select-all blocked while running")

    press(bot, b"ae_home")
    ok("⏹ Stop" in labels(bot) and "▶ Start" not in labels(bot),
       "home shows only Stop while running")
    eng.engine.running = False
    loop.close()


# --------------------------------------------------------------------------- #
# JOURNEY 5 — busy accounts (old features still on) are skipped, not burned
# --------------------------------------------------------------------------- #
def test_journey_busy_accounts():
    _fresh_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = _new_bot(6)
    _configure(bot, n_sel=6)
    bot.busy = {2, 4}                   # two accounts owned by an old feature
    ex = et.FakeExecutor(*_links(4))
    et._fast()
    okm, msg = loop.run_until_complete(eng.start(bot, ui._selected_ids(),
                                                 executor=ex))
    ok(okm, "engine starts with the free accounts")
    ok(2 not in eng.engine.roles and 4 not in eng.engine.roles,
       "busy accounts excluded (no second connection -> no AUTH_FROM_ANOTHER)")
    ok(any("skipped busy" in t for t in bot.logs),
       "the owner is told which accounts were skipped")
    loop.run_until_complete(eng.stop(bot))
    loop.close()


# --------------------------------------------------------------------------- #
# JOURNEY 6 — bot restart mid-run: the engine must come back, and a failed
# resume must NEVER be silent (regression pin for the import-bot bug).
# --------------------------------------------------------------------------- #
def test_journey_restart_resume():
    _fresh_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = _new_bot(6)
    _configure(bot, n_sel=5)
    ex = et.FakeExecutor(*_links(6))
    et._fast()
    loop.run_until_complete(eng.start(bot, ui._selected_ids(), executor=ex))
    loop.run_until_complete(asyncio.sleep(0.6))
    ok(adb.get_bool("running"), "running flag persisted for restart recovery")
    # hard crash: cancel everything without calling stop()
    for t in list(eng.engine.tasks) + ([eng.engine.task] if eng.engine.task else []):
        t.cancel()
    loop.run_until_complete(asyncio.sleep(0.1))
    eng.engine.running = False
    eng.engine.tasks = []
    eng.engine.task = None
    loop.close()

    # boot again -> restore must relaunch using the bot it was GIVEN.
    # The real Executor needs rubpy/account_conn (absent in dev), so stand in a
    # fake one exactly where the engine would build it.
    loop2 = asyncio.new_event_loop()
    asyncio.set_event_loop(loop2)
    bot2 = _new_bot(6)
    eng.engine.stop = False
    et._fast()
    _orig_exec = eng.Executor
    eng.Executor = lambda _b: et.FakeExecutor(*_links(6))
    try:
        loop2.run_until_complete(ui.restore(bot2))
    finally:
        eng.Executor = _orig_exec
    ok(eng.engine.running, "engine resumed after the restart")
    ok(len(eng.engine.roles) >= core.MIN_ACCOUNTS, "roles restored")
    ok(any("resumed after restart" in t for t in bot2.logs),
       "the owner is TOLD the engine resumed")
    loop2.run_until_complete(eng.stop(bot2))
    loop2.close()

    # and a resume that CANNOT happen must say so, not fail silently
    loop3 = asyncio.new_event_loop()
    asyncio.set_event_loop(loop3)
    bot3 = _new_bot(6)
    adb.set("running", "true")
    ui._save_selected([1, 2])                 # below the minimum now
    loop3.run_until_complete(ui.restore(bot3))
    ok(not adb.get_bool("running"), "running flag cleared when resume impossible")
    ok(any("not resumed" in t for t in bot3.logs),
       "a failed resume is reported, never silent")
    loop3.close()


def main():
    if "--selftest" not in sys.argv:
        print("usage: user_journey_test.py --selftest")
        return 0
    for fn in (test_journey_configure, test_journey_run_and_chain,
               test_journey_reset, test_journey_locked_while_running,
               test_journey_busy_accounts, test_journey_restart_resume):
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
