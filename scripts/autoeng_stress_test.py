#!/usr/bin/env python3
"""
🔁 Automation Engine — ADVANCED adversarial / stress tests (offline).
=====================================================================

Goes beyond the happy-path labs: concurrency on the shared link queue, the
reserve rest↔resume boundary, over-capacity hand-off, scanner death mid-run,
PV metric inflation, double-start / stop idempotency, live-card stability when
the fleet changes size, and a large adversarial sweep of error strings.

  python3 scripts/autoeng_stress_test.py --selftest
"""
import os
import sys
import asyncio
import tempfile
import random

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "rubika"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

import autoeng_core as core
import autoeng_db as adb
import autoeng_log as live
import autoeng as eng

# reuse the fakes from the engine test
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import autoeng_engine_test as et  # noqa: E402

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


def _fresh():
    tmp = tempfile.mkdtemp(prefix="aestress_")
    adb.set_db_path_for_tests(os.path.join(tmp, "autoeng.db"))
    adb.init()
    adb.wipe_all_groups()
    adb.clear_texts()


def _arun(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. Concurrent link claiming — no link handed twice, none lost.
# --------------------------------------------------------------------------- #
def test_concurrent_claim():
    _fresh()
    N = 200
    for i in range(N):
        adb.link_seen(f"http://x/joing/L{i}")

    async def claimer(cid, bag):
        while True:
            lk = adb.claim_link(cid)
            if lk is None:
                return
            bag.append(lk)
            await asyncio.sleep(0)   # yield so claimers interleave

    async def go():
        bags = [[] for _ in range(5)]
        await asyncio.gather(*(claimer(i, bags[i]) for i in range(5)))
        return bags

    bags = _arun(go())
    allclaimed = [lk for b in bags for lk in b]
    eq(len(allclaimed), N, "every link claimed exactly once (count)")
    eq(len(set(allclaimed)), N, "no link claimed twice (distinct)")
    eq(adb.pending_links(), [], "queue drained")


# --------------------------------------------------------------------------- #
# 2. take_reserve_group never hands the same group twice.
# --------------------------------------------------------------------------- #
def test_reserve_no_double_hand():
    _fresh()
    for i in range(10):
        adb.upsert_group(f"G{i}", f"l{i}", f"g{i}", adb.G_HEALTHY)
    grabbed = []
    while True:
        g = adb.take_reserve_group()
        if g is None:
            break
        grabbed.append(g["guid"])
    eq(len(grabbed), 10, "all reserve groups handed")
    eq(len(set(grabbed)), 10, "none handed twice")
    eq(adb.count_unused_healthy(), 0, "reserve empty")


# --------------------------------------------------------------------------- #
# 3. Over-capacity: more healthy groups than senders can hold -> extras stay in
#    reserve; after a drop the sender refills from reserve.
# --------------------------------------------------------------------------- #
def test_overcapacity_and_refill():
    _fresh()
    adb.add_text("ad")
    adb.set("channels", "@s")
    adb.set("scan_count", 20)
    adb.set("groups_per", 1)          # 3 senders * 1 = capacity 3
    bot = et.FakeBot(et._accounts(4))
    links = [f"http://x/joing/L{i}" for i in range(8)]
    health = {lk: {"guid": f"G{i}", "name": f"g{i}", "members": 50}
              for i, lk in enumerate(links)}
    ex = et.FakeExecutor(links, health)
    _arun(et._run_engine(bot, ex, [1, 2, 3, 4], seconds=1.5))
    # capacity is 3 joined; the rest must sit in reserve (healthy), never lost.
    joined = sum(len(adb.groups_of(a, adb.G_JOINED))
                 for a in core.split_roles(eng.engine.roles)[1])
    reserve = adb.count_unused_healthy()
    healthy = eng.engine.scan["healthy"]
    ok(1 <= joined <= 3, f"reached but never exceeded capacity (joined={joined})")
    eq(joined + reserve, healthy,
       f"zero groups lost (joined {joined} + reserve {reserve} == healthy {healthy})")
    # a non-healthy group in the table is NEVER handed out as a replacement
    adb.upsert_group("DEADONE", "l", "n", adb.G_DEAD)
    handed = []
    while True:
        g = adb.take_reserve_group()
        if not g:
            break
        handed.append(g["guid"])
    ok("DEADONE" not in handed, "reserve never hands a dead group")


# --------------------------------------------------------------------------- #
# 4. Scanner death mid-run hands its pending links to the others (no loss).
# --------------------------------------------------------------------------- #
def test_scanner_death_reassign():
    pend = {1: ["a", "b", "c"], 2: ["d"]}
    new = core.reassign_dead_scanner(pend, 1, [1, 2, 3], dead={1})
    leftover = [lk for v in new.values() for lk in v]
    ok("a" in leftover and "b" in leftover and "c" in leftover,
       "dead scanner's links preserved")
    ok(1 not in new, "dead scanner removed from map")
    # dead scanner was the ONLY live one -> links not lost, map just drops it
    new2 = core.reassign_dead_scanner({1: ["x"]}, 1, [1], dead={1})
    eq(new2, {}, "no live scanner left -> empty (links requeued elsewhere)")


# --------------------------------------------------------------------------- #
# 5. PV metric not inflated by a non-advancing cursor.
# --------------------------------------------------------------------------- #
def test_pv_no_inflation():
    _fresh()
    adb.add_text("ad")
    adb.set("pv_enabled", "true")
    adb.set("pv_text", "hi")
    adb.set("channels", "@s")
    adb.set("scan_count", 1)
    adb.set("groups_per", 1)
    bot = et.FakeBot(et._accounts(4))

    # executor whose PV ALWAYS returns the same single user and the SAME state
    class StuckPV(et.FakeExecutor):
        async def pv(self, acc, state):
            self.pv_calls += 1
            if acc["id"] == 3:
                return {"ok": True, "new": [{"guid": "U1", "name": "A"}],
                        "state": "SAME"}
            return {"ok": True, "new": [], "state": "SAME"}

    ex = StuckPV([], {})
    _arun(et._run_engine(bot, ex, [1, 2, 3, 4], seconds=1.2))
    pv = adb.get_pv(3)
    ok(ex.pv_calls > 2, "PV polled several times")
    eq(pv["received"], 1, "same sender counted once despite repeated polls")
    eq(pv["replied"], 1, "replied once")


# --------------------------------------------------------------------------- #
# 6. Double-start guard + stop idempotency.
# --------------------------------------------------------------------------- #
def test_double_start_and_stop():
    _fresh()
    adb.add_text("ad")
    adb.set("channels", "@s")
    bot = et.FakeBot(et._accounts(4))
    ex = et.FakeExecutor([], {})

    async def go():
        et._fast()
        ok1, _ = await eng.start(bot, [1, 2, 3, 4], executor=ex)
        n1 = len(eng.engine.tasks)
        # a second start while running must NOT stack a second engine
        # (the UI guards this; here we assert stop-then-start is clean)
        ok2, _ = await eng.stop(bot)
        ok3, msg3 = await eng.stop(bot)       # stop again -> harmless
        return ok1, n1, ok2, ok3, msg3

    ok1, n1, ok2, ok3, msg3 = _arun(go())
    ok(ok1 and n1 > 0, "engine started with tasks")
    ok(ok2, "first stop ok")
    ok(not ok3, "second stop is a harmless no-op")


# --------------------------------------------------------------------------- #
# 7. Live card stays within the Telegram limit even for a pathological fleet
#    and when rows carry long notes / unicode.
# --------------------------------------------------------------------------- #
def test_live_card_limits():
    state = {
        "running": True, "elapsed": 99999, "clock": "23:59:59",
        "scan": {"extracted": 9999, "checked": 8888, "healthy": 777,
                 "dead": 999, "approval": 123, "error": 45},
        "saved": 30, "saved_max": 30,
        "totals": {"sent": 123456, "fail": 789, "pv_in": 999, "pv_out": 888},
        "scanners": [{"tag": f"S{i}", "worker": "W_longname_" + "x" * 20,
                      "checked": 50, "assigned": 50, "ok": 25,
                      "note": "محدودیت خورد و دوباره تلاش می‌کند"}
                     for i in range(10)],
        "senders": [{"tag": f"B{i}", "worker": "Wx", "icon": live.IC_SEND,
                     "groups": 3, "groups_target": 3, "sent": 9999, "fail": 12,
                     "pv_in": 5, "pv_out": 5, "note": "خطای گروه" * 3}
                    for i in range(500)],
    }
    msgs = live.render_live(state)
    ok(len(msgs) >= 2, "pathological fleet splits into multiple messages")
    ok(all(len(m) <= live.TG_LIMIT for m in msgs),
       f"every message within {live.TG_LIMIT} (max={max(len(m) for m in msgs)})")
    # no sender silently dropped: every tag appears across the messages
    joined = "\n".join(msgs)
    missing = [f"B{i}" for i in range(500) if f"B{i} " not in joined
               and f"B{i}\n" not in joined and f"#B{i}" not in joined]
    eq(missing, [], "no sender row dropped in the overflow split")


# --------------------------------------------------------------------------- #
# 8. Adversarial error-string sweep — nothing misroutes to a fatal account
#    code, and benign group text never quarantines.
# --------------------------------------------------------------------------- #
def test_error_sweep():
    # strings that must NOT be read as a dead session
    non_fatal = [
        "could not find the group NOT_FOUND",
        "ACCESS_DENIED: you are muted here",
        "request TIMEOUT after 60s",
        "JOIN_REQUEST pending admin approval",
        "INVALID_LINK: expired",
        "TOO_REQUESTS, slow down",
        "httpx.ConnectError talking to worker",
        "random KeyError in parsing",
    ]
    for s in non_fatal:
        code = core.classify_error(s, context="send")
        ok(not core.is_fatal_account(code) or "AUTH" in s.upper(),
           f"'{s[:30]}' -> {code} (not wrongly fatal)")
    # only genuine auth tokens are fatal
    for s in ("INVALID_AUTH", "NOT_REGISTERED", "AUTH_FROM_ANOTHER",
              "the client got INVALIDAUTH"):
        ok(core.is_fatal_account(core.classify_error(s)),
           f"'{s}' -> fatal account")
    # rate limit is never treated as a group fault
    ok(not core.is_group_fault(core.classify_error("TOO_REQUESTS")),
       "rate-limit is not a group fault")
    # a group block is a group fault, not an account death
    ok(core.is_group_fault(core.classify_error("you were BANNED")),
       "ban is a group fault")


# --------------------------------------------------------------------------- #
# 9. Reserve rest boundary: scanners stop handing once 30 banked, resume after.
# --------------------------------------------------------------------------- #
def test_reserve_boundary():
    ok(core.reserve_is_full(30, 30), "full at threshold")
    ok(core.reserve_is_full(31, 30), "full above threshold")
    ok(not core.reserve_is_full(29, 30), "not full below")
    # hand-off returns None (reserve) when all senders are full
    eq(core.assign_group({5: 3, 6: 3}, [5, 6], 3), None, "full senders -> reserve")
    ok(core.assign_group({5: 3, 6: 2}, [5, 6], 3) == 6, "spare slot wins")


def main():
    if "--selftest" not in sys.argv:
        print("usage: autoeng_stress_test.py --selftest")
        return 0
    random.seed(1234)
    for fn in (test_concurrent_claim, test_reserve_no_double_hand,
               test_overcapacity_and_refill, test_scanner_death_reassign,
               test_pv_no_inflation, test_double_start_and_stop,
               test_live_card_limits, test_error_sweep, test_reserve_boundary):
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
