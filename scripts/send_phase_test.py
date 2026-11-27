#!/usr/bin/env python3
"""
🔸🔹 Two-phase send + server rotation — offline tests.
======================================================

Proves the owner's exact rules:
  * phase 1 = one-way (no chat), phase 2 = two-way (has chat)
  * 5 CONSECUTIVE errors in phase 1 -> go two-way, SAME server
  * 5 CONSECUTIVE errors in phase 2 -> rotate server, resume at the cursor
  * rotation is automatic, runs to the end of the budget (a failed switch does
    not stop the next one), default 3 switches
  * a success resets the streak (scattered errors never trip a phase change)

  python3 scripts/send_phase_test.py --selftest
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "rubika"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

import send_phase as sp
import send_live as sl

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
def test_split():
    recs = [
        {"guid": "s1", "mutual": False},
        {"guid": "m1", "mutual": True},
        {"guid": "s2", "mutual": False},
        {"guid": "m2", "mutual": True},
        {"guid": "", "mutual": True},        # no guid -> dropped
    ]
    one, two = sp.split_recipients(recs)
    eq(one, ["s1", "s2"], "one-way = no chat, order preserved")
    eq(two, ["m1", "m2"], "two-way = has chat, order preserved")
    eq(sp.split_recipients([]), ([], []), "empty input safe")


def test_streak_resets_on_success():
    st = sp.PhaseState(["a", "b", "c", "d", "e", "f", "g"], ["x"])
    for _ in range(4):
        st.record_fail()
    eq(st.streak, 4, "4 consecutive failures")
    eq(st.next_action(), sp.ACT_SEND, "4 is not enough to switch phase")
    st.record_ok()
    eq(st.streak, 0, "a success resets the streak")
    eq(st.next_action(), sp.ACT_SEND, "still sending one-way")


def test_phase1_five_errors_goes_twoway_same_server():
    st = sp.PhaseState(["a", "b", "c", "d", "e", "f"], ["x", "y"])
    for _ in range(5):
        st.record_fail()
    eq(st.streak, 5, "5 consecutive one-way errors")
    eq(st.next_action(), sp.ACT_TO_TWOWAY, "phase 1 blocked -> go two-way")
    eq(st.switches, 0, "NO server switch in phase 1")
    st.go_twoway()
    eq(st.phase, sp.PHASE_TWOWAY, "now in two-way phase")
    eq(st.cursor, 0, "cursor restarts for the two-way list")
    eq(st.streak, 0, "streak cleared entering phase 2")
    ok(st.oneway_blocked, "remembers one-way was blocked (for the card)")
    eq(st.next_action(), sp.ACT_SEND, "sending two-way now")


def test_phase1_exhausted_goes_twoway():
    st = sp.PhaseState(["a"], ["x"])
    st.record_ok()
    eq(st.next_action(), sp.ACT_TO_TWOWAY, "one-way list finished -> two-way")
    ok(not st.oneway_blocked, "not marked blocked when it simply finished")


def test_phase2_five_errors_switches_server():
    st = sp.PhaseState([], ["x", "y", "z", "p", "q", "r", "s"],
                       max_switches=3)
    eq(st.phase, sp.PHASE_TWOWAY, "starts in two-way when no strangers")
    for _ in range(5):
        st.record_fail()
    eq(st.next_action(), sp.ACT_SWITCH, "5 two-way errors -> rotate server")
    cur = st.cursor
    st.note_switch()
    eq(st.switches, 1, "switch counted")
    eq(st.cursor, cur, "cursor PRESERVED -> resume, nobody messaged twice")
    eq(st.streak, 0, "streak cleared so the new IP is judged fresh")
    eq(st.next_action(), sp.ACT_SEND, "continues on the new server")


def test_rotation_runs_to_the_end():
    # owner: a failed switch must NOT stop the next one; go to the end (3)
    st = sp.PhaseState([], ["a"] * 40, max_switches=3)
    for n in range(1, 4):
        for _ in range(5):
            st.record_fail()
        eq(st.next_action(), sp.ACT_SWITCH, f"switch {n} allowed")
        st.note_switch()
        eq(st.switches, n, f"switch count = {n}")
    for _ in range(5):
        st.record_fail()
    eq(st.next_action(), sp.ACT_STOP, "budget exhausted after 3 -> stop")


def test_max_switches_tunable():
    st = sp.PhaseState([], ["a"] * 20, max_switches=1)
    for _ in range(5):
        st.record_fail()
    eq(st.next_action(), sp.ACT_SWITCH, "first switch allowed")
    st.note_switch()
    for _ in range(5):
        st.record_fail()
    eq(st.next_action(), sp.ACT_STOP, "max_switches=1 respected")
    eq(sp.DEFAULT_MAX_SWITCHES, 3, "default budget is 3")
    eq(sp.SWITCH_PAUSE_SEC, 180, "3-minute pause between switches")
    eq(sp.STREAK_LIMIT, 5, "streak limit fixed at 5")


def test_no_twoway_available():
    st = sp.PhaseState(["a", "b", "c", "d", "e", "f"], [])
    for _ in range(5):
        st.record_fail()
    eq(st.next_action(), sp.ACT_STOP, "no two-way list -> stop, never switch")


def test_skip_does_not_count():
    st = sp.PhaseState(["a", "b"], ["x"])
    st.record_fail()
    st.record_skip()
    eq(st.streak, 1, "skip does not extend the streak")
    eq(st.cursor, 2, "skip still advances the cursor")
    eq(st.total_sent(), 0, "skip is not a send")


def test_per_worker_tally_and_cards():
    st = sp.PhaseState(["a", "b"], ["x", "y"])
    st.record_ok("W4_211")
    st.record_fail("W4_211")
    eq(st.per_worker["W4_211"], {"sent": 1, "fail": 1}, "per-worker tally")

    live = sl.render_live(phone="989166810746", tag="A1", worker_tag="W4_211",
                          worker_ip="46.8.226.22", state=st, elapsed=252,
                          clock="17:42:10")
    ok(live.startswith("| ⚙ - #send"), "live card header")
    ok("ONE-WAY" in live, "shows the current phase")
    ok("00:04:12" in live, "elapsed formatted")
    ok("streak 1/5" in live, "shows the streak out of 5")
    ok("0/3 switches" in live, "shows the switch budget")
    ok(len(live) <= sl.TG_LIMIT, "live card within Telegram limit")

    sw = sl.switch_card(n=1, total=3, phone="0912", reason="5 consecutive errors",
                        from_tag="W4", from_ip="1.1.1.1", to_tag="W7",
                        to_ip="2.2.2.2", resume_at=312, clock="17:42:10")
    ok("SWITCH 1/3" in sw and "from #312" in sw, "switch card renders")

    good = sl.switch_result_card(n=1, total=3, phone="0912", worker_tag="W7",
                                 worker_ip="2.2.2.2", ok_after=5, fail_after=0,
                                 clock="17:43:02")
    ok("WORKING" in good and "IP was the problem" in good, "positive verdict")
    bad = sl.switch_result_card(n=1, total=3, phone="0912", worker_tag="W7",
                                worker_ip="2.2.2.2", ok_after=0, fail_after=5,
                                clock="17:43:02")
    ok("NO LUCK" in bad and "account-level" in bad, "negative verdict")

    fin = sl.final_card(phone="0912", state=st, elapsed=300, clock="18:00:00")
    ok("#send_done" in fin and "per server" in fin, "final summary card")


def test_switch_verdict():
    eq(sp.switch_verdict(5, 0), "the IP was the problem ✅", "clean after switch")
    eq(sp.switch_verdict(0, 5), "not an IP issue — account-level limit",
       "still failing -> account limit")
    ok("mixed" in sp.switch_verdict(3, 2), "mixed results")
    ok("inconclusive" in sp.switch_verdict(0, 1), "too little data")


def main():
    if "--selftest" not in sys.argv:
        print("usage: send_phase_test.py --selftest")
        return 0
    for fn in (test_split, test_streak_resets_on_success,
               test_phase1_five_errors_goes_twoway_same_server,
               test_phase1_exhausted_goes_twoway,
               test_phase2_five_errors_switches_server,
               test_rotation_runs_to_the_end, test_max_switches_tunable,
               test_no_twoway_available, test_skip_does_not_count,
               test_per_worker_tally_and_cards, test_switch_verdict):
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
