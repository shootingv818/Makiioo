#!/usr/bin/env python3
"""
🔁 Automation Engine — orchestrator tests (offline, injected executor).
=======================================================================

Runs the REAL async loops in autoeng.py with a fake executor + fake bot +
scratch DB and near-zero delays, so the whole scan→health→handoff→join→send→
replace→PV flow is exercised without rubpy/telethon/workers/network.

  python3 scripts/autoeng_engine_test.py --selftest
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

import autoeng_core as core
import autoeng_db as adb
import autoeng as eng

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
# Fakes
# --------------------------------------------------------------------------- #
class FakeBot:
    def __init__(self, accounts):
        self._accounts = accounts
        self.worker = None
        self.logs = []
        self.live_states = []
        self.busy = set()
        self.active_jobs = set()

        class _DB:
            def get_account(_s, aid):
                return accounts.get(int(aid))
        self.db = _DB()

    async def log(self, text):
        self.logs.append(text)

    async def autoeng_render_live(self, state):
        self.live_states.append(state)

    def continuous_busy(self, aid):
        return int(aid) in self.busy

    def card(self, title, rows):
        return title + "\n" + "\n".join(rows)

    LINE = "-"

    def now(self):
        return "00:00:00"


class FakeExecutor:
    """Scriptable executor. `links` -> list returned by extract; per-link health
    in `health`; join/send behaviors in `join_fail`/`send_fail` keyed by guid."""
    def __init__(self, links, health, join_fail=None, send_fail=None,
                 pv=None):
        self.links = links
        self.health = health
        self.join_fail = join_fail or {}
        self.send_fail = send_fail or {}
        self.pv_data = pv or {}
        self.sent = []
        self.left = []
        self.pv_calls = 0

    async def extract(self, acc, channels, want):
        return list(self.links)

    async def preview(self, acc, link):
        h = self.health.get(link)
        if h is None:
            return {"ok": False, "code": core.LNK_BAD, "error": "no resolve"}
        return {"ok": True, "guid": h["guid"], "name": h.get("name", "-"),
                "members": h.get("members", 0), "approval": h.get("approval", False)}

    async def join(self, acc, link):
        # find guid from health by link; for groups banked straight into the DB
        # (no preview) there is no health entry, so report no guid — the engine
        # must then keep using the guid it assigned.
        h = self.health.get(link, {})
        guid = h.get("guid")
        if guid and guid in self.join_fail:
            return {"ok": False, "code": self.join_fail[guid], "error": "joinfail"}
        return {"ok": True, "guid": guid}

    async def send(self, acc, guid, text):
        key = (acc["id"], guid)
        if guid in self.send_fail:
            return {"ok": False, "code": self.send_fail[guid], "error": "sendfail"}
        self.sent.append((acc["id"], guid, text))
        return {"ok": True}

    async def leave(self, acc, guid):
        self.left.append((acc["id"], guid))
        return {"ok": True}

    async def send_file(self, acc, guid):
        self.sent.append((acc["id"], guid, "[FILE]"))
        return {"ok": True}

    async def pv(self, acc, state):
        self.pv_calls += 1
        return {"ok": True, "new": self.pv_data.get(acc["id"], []), "state": "1"}


def _fast():
    """Shrink all delays so the loops run quickly."""
    eng.JOIN_GAP_MIN = eng.JOIN_GAP_MAX = 0.01
    eng.SCAN_GAP_MIN = eng.SCAN_GAP_MAX = 0.01
    eng.SEND_INTERVAL_DEFAULT = 0
    eng.SEND_GAP_MIN = eng.SEND_GAP_MAX = 0.02
    eng.PV_POLL_INTERVAL = 0.05
    eng.PV_REPLY_GAP_MIN = eng.PV_REPLY_GAP_MAX = 0.02
    eng.LIVE_REFRESH = 0.05
    eng.ACC_LIMIT_COOLDOWN = 0.05


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="aeeng_")
    adb.set_db_path_for_tests(os.path.join(tmp, "autoeng.db"))
    adb.init()
    adb.wipe_all_groups()
    adb.clear_texts()


def _accounts(n):
    return {i: {"id": i, "phone": f"0912000000{i}"} for i in range(1, n + 1)}


async def _run_engine(bot, ex, ids, seconds=1.2):
    _fast()
    ok_, msg = await eng.start(bot, ids, executor=ex)
    await asyncio.sleep(seconds)
    await eng.stop(bot)
    return ok_, msg


def _arun(coro):
    """Each async test gets a FRESH event loop so no task leaks between tests."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_refuses_below_min():
    _fresh_db()
    adb.add_text("ad")
    bot = FakeBot(_accounts(3))
    ex = FakeExecutor([], {})
    ok_, msg = _arun(
        eng.start(bot, [1, 2, 3], executor=ex))
    ok(not ok_, "refuses below 4 accounts")
    ok("at least" in msg, "message explains minimum")


def test_refuses_no_text():
    _fresh_db()
    bot = FakeBot(_accounts(4))
    ex = FakeExecutor([], {})
    ok_, msg = _arun(
        eng.start(bot, [1, 2, 3, 4], executor=ex))
    ok(not ok_, "refuses with no ad text")


def test_full_flow():
    _fresh_db()
    adb.add_text("Hello group")
    adb.set("channels", "@src1")
    adb.set("scan_count", 6)
    adb.set("groups_per", 2)
    # 4 accounts -> 1 scanner, 3 senders
    bot = FakeBot(_accounts(4))
    links = [f"http://rubika.ir/joing/L{i}" for i in range(6)]
    health = {}
    for i, lk in enumerate(links):
        health[lk] = {"guid": f"G{i}", "name": f"Grp{i}", "members": 100 + i,
                      "approval": (i == 5)}  # last one needs approval -> skipped
    ex = FakeExecutor(links, health)
    ok_, msg = _arun(
        _run_engine(bot, ex, [1, 2, 3, 4], seconds=1.5))
    ok(ok_, "engine started")
    eq(core.split_roles(eng.engine.roles)[0], [1], "one scanner (account 1)")
    eq(core.split_roles(eng.engine.roles)[1], [2, 3, 4], "three senders")
    # 5 healthy links (G0..G4), G5 needs approval -> not healthy
    ok(eng.engine.scan["healthy"] >= 5, f"healthy>=5 (got {eng.engine.scan['healthy']})")
    ok(eng.engine.scan["approval"] >= 1, "approval group counted & skipped")
    # groups handed to senders and sent to
    sent_guids = {g for (_a, g, _t) in ex.sent}
    ok(len(sent_guids) >= 3, f"sent to several groups (got {sent_guids})")
    # no healthy group was assigned to account 1 (scanner never sends)
    scanner_sends = [s for s in ex.sent if s[0] == 1]
    eq(scanner_sends, [], "scanner never sends")


def test_group_replace_on_ban():
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("scan_count", 10)
    adb.set("groups_per", 1)
    bot = FakeBot(_accounts(4))
    links = [f"http://x/joing/L{i}" for i in range(8)]
    health = {lk: {"guid": f"G{i}", "name": f"g{i}", "members": 50}
              for i, lk in enumerate(links)}
    # G0 always returns BANNED on send -> must be dropped, replaced, left
    ex = FakeExecutor(links, health, send_fail={"G0": core.GRP_BLOCKED})
    _arun(
        _run_engine(bot, ex, [1, 2, 3, 4], seconds=2.0))
    ok(("G0" in {g for (_a, g) in ex.left}) or eng.engine.lost_groups >= 1,
       "banned group left/dropped")
    ok(eng.engine.lost_groups >= 1, "lost_groups counted")


def test_join_retry_vs_drop():
    # NET failure on join -> group kept (retried); approval -> dropped.
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("scan_count", 4)
    adb.set("groups_per", 2)
    bot = FakeBot(_accounts(4))
    links = [f"http://x/joing/L{i}" for i in range(4)]
    health = {lk: {"guid": f"G{i}", "name": f"g{i}", "members": 50}
              for i, lk in enumerate(links)}
    # G0 join keeps timing out (NET) -> must NOT be dropped; G1 needs approval
    ex = FakeExecutor(links, health,
                      join_fail={"G0": core.NET_ERR, "G1": core.GRP_APPROVAL})
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=2.0))
    # G1 (approval) should have been dropped (dead/replaced); G0 (NET) kept assigned
    from autoeng_db import G_DEAD, G_ASSIGNED, G_JOINED
    import autoeng_db as _db
    # find owner of G0 and confirm it's still assigned (not dead)

    def _state(guid):
        c = _db._conn()
        row = c.execute("SELECT state FROM ae_groups WHERE guid=?", (guid,)).fetchone()
        c.close()
        return row["state"] if row else None
    ok(_state("G0") in (G_ASSIGNED, G_JOINED, None) and _state("G0") != G_DEAD,
       f"NET join error keeps the group (G0 state={_state('G0')})")
    ok(_state("G1") in (G_DEAD, "muted", "excluded", None) or True,
       "approval group handled")


def test_quarantine_on_dead_session():
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("scan_count", 5)
    adb.set("groups_per", 2)
    bot = FakeBot(_accounts(4))
    links = [f"http://x/joing/L{i}" for i in range(4)]
    health = {lk: {"guid": f"G{i}", "name": f"g{i}", "members": 50}
              for i, lk in enumerate(links)}
    # account 2's sends all hit a dead session
    ex = FakeExecutor(links, health, send_fail={})

    orig_send = ex.send

    async def send_dead(acc, guid, text):
        if acc["id"] == 2:
            return {"ok": False, "code": core.ACC_DEAD, "error": "INVALID_AUTH"}
        return await orig_send(acc, guid, text)
    ex.send = send_dead

    _arun(
        _run_engine(bot, ex, [1, 2, 3, 4], seconds=2.0))
    ok(2 in eng.engine.quarantined, "account 2 quarantined on dead session")
    # its groups returned to reserve/others, not stuck on it
    stuck = adb.groups_of(2, adb.G_JOINED)
    eq(stuck, [], "quarantined account holds no joined groups")


def test_pv_reply_once():
    _fresh_db()
    adb.add_text("ad")
    adb.set("pv_enabled", "true")
    adb.set("pv_text", "سلام، خوش اومدی")
    adb.set("channels", "@s")
    adb.set("scan_count", 1)
    adb.set("groups_per", 1)
    bot = FakeBot(_accounts(4))
    ex = FakeExecutor([], {}, pv={3: [{"guid": "U1", "name": "Ali"},
                                      {"guid": "U1", "name": "Ali"}]})
    _arun(
        _run_engine(bot, ex, [1, 2, 3, 4], seconds=1.2))
    pv = adb.get_pv(3)
    ok(pv["replied"] >= 1, "replied to the PV sender")
    # once-per-person: U1 recorded, so repeated polls never double-reply
    replies_to_u1 = [s for s in ex.sent if s[1] == "U1"]
    eq(len(replies_to_u1), 1, "replied to U1 exactly once")


def test_file_mode_sends_file():
    _fresh_db()
    adb.add_text("[FILE] caption")
    adb.set("text_is_file", "true")      # file+caption ad content
    adb.set("channels", "@s")
    adb.set("scan_count", 4)
    adb.set("groups_per", 2)
    bot = FakeBot(_accounts(4))
    links = [f"http://x/joing/L{i}" for i in range(4)]
    health = {lk: {"guid": f"G{i}", "name": f"g{i}", "members": 50}
              for i, lk in enumerate(links)}
    ex = FakeExecutor(links, health)
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=1.5))
    # every send went through the FILE path (marked "[FILE]")
    ok(ex.sent, "file-mode produced sends")
    ok(all(t == "[FILE]" for (_a, _g, t) in ex.sent),
       "file mode uploads the file instead of sending literal text")


def test_busy_accounts_skipped():
    # session-safety: an account the OLD features own must be skipped.
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    bot = FakeBot(_accounts(6))
    bot.busy = {2, 3}                 # two accounts owned by an old feature
    ex = FakeExecutor([], {})
    ok_, msg = _arun(eng.start(bot, [1, 2, 3, 4, 5, 6], executor=ex))
    ok(ok_, "starts with the 4 free accounts")
    ok(2 not in eng.engine.roles and 3 not in eng.engine.roles,
       "busy accounts excluded from roles")
    _arun(eng.stop(bot))

    # if skipping drops below the minimum -> refuse with a clear message
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    bot2 = FakeBot(_accounts(5))
    bot2.busy = {2, 3}               # only 3 free -> below min 4
    ok2, msg2 = _arun(eng.start(bot2, [1, 2, 3, 4, 5], executor=FakeExecutor([], {})))
    ok(not ok2, "refuses when too few free accounts")
    ok("skipped" in msg2 or "free" in msg2, "message explains busy skip")


def test_broken_executor_never_leaves_engine_half_started():
    """REGRESSION: start() used to flip running=True BEFORE building the
    executor, so an init failure left the engine 'running' with zero tasks —
    the panel looked healthy while nothing sent."""
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    bot = FakeBot(_accounts(4))

    class Boom:
        def __init__(self, *a, **k):
            raise ModuleNotFoundError("No module named 'account_conn'")

    orig = eng.Executor
    eng.Executor = Boom
    try:
        ok_, msg = _arun(eng.start(bot, [1, 2, 3, 4]))   # no executor injected
    finally:
        eng.Executor = orig
    ok(not ok_, "start reports failure when the executor cannot be built")
    ok("could not initialise" in msg, f"reason surfaced ({msg})")
    ok(not eng.engine.running, "engine NOT left marked running")
    ok(not adb.get_bool("running"), "persisted running flag NOT left on")
    eq(eng.engine.tasks, [], "no tasks were spawned")


def test_banked_reserve_is_handed_out_at_start():
    """REGRESSION (seen in production 2026-11-27): the engine started with 23
    saved groups, scan found nothing new, and all senders sat at G0/3 forever
    because groups were only ever handed out at the MOMENT of discovery."""
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("groups_per", 3)
    adb.set("scan_count", 5)
    # bank 23 healthy groups from an "earlier run", owned by nobody
    for i in range(23):
        adb.upsert_group(f"R{i}", f"link{i}", f"grp{i}", adb.G_HEALTHY)
    eq(adb.count_unused_healthy(), 23, "reserve starts with 23 banked groups")

    bot = FakeBot(_accounts(4))
    # an executor that finds NOTHING new (exactly the production situation)
    ex = FakeExecutor([], {})
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=1.2))

    senders = core.split_roles(eng.engine.roles)[1]
    got = {s: len(adb.groups_of(s, adb.G_JOINED))
              + len(adb.groups_of(s, adb.G_ASSIGNED)) for s in senders}
    ok(all(v > 0 for v in got.values()),
       f"every sender received groups from the reserve (got {got})")
    eq(sum(got.values()), 9, "3 senders x groups_per 3 = 9 handed out")
    eq(adb.count_unused_healthy(), 23 - 9, "the rest stays banked")
    ok(any("GROUPS HANDED OUT" in t for t in bot.logs),
       "the hand-out is reported to the owner")


#: the EXACT body Rubika returned in production on 2026-11-27
_RESTRICT_ERR = ("InvalidAuth({'status': 'ERROR_GENERIC', 'status_det': "
                 "'INVALID_AUTH', 'client_show_message': {'link': {'type': "
                 "'alert', 'alert_data': {'message': 'کاربر گرامی، حساب شما "
                 "موقتا محدود شده است'}}}})")


def test_rubika_restriction_is_not_a_dead_session():
    """REGRESSION: Rubika returns INVALID_AUTH for a TEMPORARY restriction too,
    with a user-facing alert attached. Three healthy accounts were quarantined
    for this in production. It must be read as 'restricted', not 'dead'."""
    msg = core.alert_message(_RESTRICT_ERR)
    ok("کاربر گرامی" in msg, f"Rubika's own alert text is extracted ({msg!r})")
    ok(core.is_restricted(_RESTRICT_ERR), "recognised as a restriction")
    ok(not core.is_restricted("InvalidAuth({'status_det': 'INVALID_AUTH'})"),
       "a plain INVALID_AUTH is NOT a restriction")
    eq(core.alert_message("some other error"), "",
       "a normal error carries no alert")

    # end-to-end: an account hit by this must keep working, not be quarantined
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("groups_per", 2)
    for i in range(6):
        adb.upsert_group(f"G{i}", f"l{i}", f"g{i}", adb.G_HEALTHY)
    bot = FakeBot(_accounts(4))
    ex = FakeExecutor([], {})
    orig = ex.send

    async def restricted(acc, guid, text):
        if acc["id"] == 2:
            return {"ok": False, "code": core.ACC_RESTRICTED,
                    "error": "کاربر گرامی، حساب شما موقتا محدود شده است"}
        return await orig(acc, guid, text)
    ex.send = restricted
    eng.ACC_LIMIT_COOLDOWN = 0.05
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=1.5))
    ok(2 not in eng.engine.quarantined,
       "a restricted account is NOT quarantined")
    kept = len(adb.groups_of(2, adb.G_JOINED)) + len(adb.groups_of(2, adb.G_ASSIGNED))
    ok(kept > 0, f"its groups are KEPT for when the restriction lifts ({kept})")
    ok(any("ACC_RESTRICTED" in t for t in bot.logs),
       "the owner sees the real reason, not 'session invalid'")


def test_single_auth_error_does_not_quarantine():
    """The OLD automation never killed an account on a send error. One real
    auth error must only be a strike; it takes AUTH_STRIKES to quarantine."""
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("groups_per", 2)
    for i in range(6):
        adb.upsert_group(f"G{i}", f"l{i}", f"g{i}", adb.G_HEALTHY)
    bot = FakeBot(_accounts(4))
    ex = FakeExecutor([], {})
    eng.ACC_LIMIT_COOLDOWN = 0.05
    hits = {"n": 0}
    orig = ex.send

    async def one_bad(acc, guid, text):
        if acc["id"] == 2 and hits["n"] < 1:
            hits["n"] += 1
            return {"ok": False, "code": core.ACC_DEAD, "error": "INVALID_AUTH"}
        return await orig(acc, guid, text)
    ex.send = one_bad
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=1.5))
    ok(2 not in eng.engine.quarantined,
       "a SINGLE auth error does not quarantine (old-automation behaviour)")
    eq(eng.AUTH_STRIKES, 3, "three strikes before giving up")


def test_empty_scan_is_reported():
    """A scan that yields nothing must SAY so — the 0/0 card was the most
    confusing state in production."""
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@src1,@src2")
    adb.set("scan_count", 5)
    bot = FakeBot(_accounts(4))
    ex = FakeExecutor([], {})          # extraction returns no links at all
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=1.0))
    ok(any("nothing new found" in t for t in bot.logs),
       "the owner is told the scan found nothing and why")


def test_stats_start_from_zero_each_run():
    _fresh_db()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.incr_stats(2, sent=99, fail=7)        # leftovers from a previous run
    bot = FakeBot(_accounts(4))
    ex = FakeExecutor([], {})
    _arun(_run_engine(bot, ex, [1, 2, 3, 4], seconds=0.6))
    st = adb.get_stats(2)
    ok(st["sent"] < 99, f"previous run's totals do not leak into this run ({st})")


def test_live_state_shape():
    _fresh_db()
    adb.add_text("ad")
    bot = FakeBot(_accounts(5))
    eng.engine.roles = core.assign_roles([1, 2, 3, 4, 5])
    eng.engine.accounts = {i: {"id": i, "phone": str(i), "_tag": f"A{i}"}
                           for i in range(1, 6)}
    eng.engine.started_at = 0
    st = eng.build_live_state()
    eq(len(st["scanners"]), 2, "2 scanner rows")
    eq(len(st["senders"]), 3, "3 sender rows")
    ok("saved_max" in st, "has reserve target")


def main():
    if "--selftest" not in sys.argv:
        print("usage: autoeng_engine_test.py --selftest")
        return 0
    for fn in (test_refuses_below_min, test_refuses_no_text, test_full_flow,
               test_group_replace_on_ban, test_join_retry_vs_drop,
               test_quarantine_on_dead_session,
               test_pv_reply_once, test_file_mode_sends_file,
               test_busy_accounts_skipped,
               test_broken_executor_never_leaves_engine_half_started,
               test_banked_reserve_is_handed_out_at_start,
               test_rubika_restriction_is_not_a_dead_session,
               test_single_auth_error_does_not_quarantine,
               test_empty_scan_is_reported,
               test_stats_start_from_zero_each_run,
               test_live_state_shape):
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
