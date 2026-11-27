#!/usr/bin/env python3
"""
🖥🔸 Remote two-phase + live-card regression tests.
===================================================

Pins the two production bugs seen on 2026-11-27 with a worker-held account:

  1. the order was still two-way-first, because send_order's tier 1 is
     TWOWAY_RECENT and the phase split only ran on the master's local path;
  2. the live card showed "Progress 620/0 · Sent 0" for a run that really sent
     36, because live_begin was called before the total was known, which put
     PhaseState into DONE and silently discarded every counter.

  python3 scripts/send_remote_phase_test.py --selftest
"""
import ast
import os
import sys
import types as _t

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "core", "rubika", "workers", "status"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)


def _stub(name, **attrs):
    if name in sys.modules:
        return
    m = _t.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


_stub("dotenv", load_dotenv=lambda *a, **k: None, find_dotenv=lambda *a, **k: "")
_rubpy = _t.ModuleType("rubpy")
_rubpy.__path__ = []
_rubpy.Client = object
sys.modules.setdefault("rubpy", _rubpy)
_stub("rubpy.types", Update=object)
_stub("rubpy.crypto", Crypto=object)
_stub("rubpy.sessions", SQLiteSession=object, StringSession=object)

import send_phase as sp          # noqa: E402
import send_live as sl           # noqa: E402

BOT = open(os.path.join(_ROOT, "bot_app", "bot.py"), encoding="utf-8").read()
WAPI = open(os.path.join(_ROOT, "workers", "worker_api.py"), encoding="utf-8").read()

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


def _fn_src(src, name):
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


# --------------------------------------------------------------------------- #
# BUG 1 — the worker must order strangers first
# --------------------------------------------------------------------------- #
def test_worker_accepts_and_applies_phase_order():
    ok("phase_order: bool = False" in WAPI,
       "SendIn has the additive phase_order flag")
    ok("send_phase.split_recipients(ordered)" in WAPI,
       "the worker splits its own ordered records by `mutual`")
    ok("recipients = _one + _two" in WAPI,
       "the worker puts one-way FIRST, then two-way")
    ok('"twoway_from": twoway_from' in WAPI,
       "the worker reports the boundary back to the master")
    ok("twoway_from = 0" in WAPI,
       "the boundary defaults to 0 (feature off) for resume/pool slices")


def test_master_requests_phase_order_only_for_fresh_runs():
    b = _fn_src(BOT, "run_send_remote")
    ok('"phase_order"' in b, "the master sends the phase_order flag")
    ok("not explicit_recipients" in b,
       "a resume/transfer keeps its frozen order (no re-phasing)")
    ok("not order_by_presence" in b,
       "a pool slice keeps presence ordering")


def test_worker_phase_jump():
    ok("SEND PHASE CHANGE" in WAPI, "the worker logs the phase flip")
    ok('job["phase"] = "twoway"' in WAPI, "the worker records the new phase")
    ok('job["oneway_blocked"] = True' in WAPI,
       "the worker remembers one-way was blocked")
    ok("oneway_ok" in WAPI and "twoway_ok" in WAPI,
       "the worker tallies each phase separately")


# --------------------------------------------------------------------------- #
# BUG 2 — the live card must never show 0 totals / swallow counters
# --------------------------------------------------------------------------- #
def test_live_begin_after_total_is_known():
    b = _fn_src(BOT, "run_send_remote")
    i_guids = b.find("guids = res.get(")
    i_begin = b.find("live_begin")
    ok(i_guids != -1 and i_begin != -1, "both points exist in run_send_remote")
    ok(i_begin > i_guids,
       "live_begin runs AFTER the worker returned the real total")
    ok("res.get(\"twoway_from\")" in b or "res.get('twoway_from')" in b,
       "the card is built with the worker's phase split")


def test_counters_never_vanish_in_done_state():
    """The exact production shape: a state built with no recipients (total was
    0) must still count what really happened."""
    st = sp.PhaseState([], [])
    eq(st.phase, sp.PHASE_DONE, "empty lists start in DONE (as before)")
    for _ in range(36):
        st.record_ok("W9_125")
    eq(st.total_sent(), 36, "36 real sends are counted, not discarded")
    st.record_fail("W9_125")
    eq(st.total_fail(), 1, "failures are counted too")
    eq(st.per_worker["W9_125"], {"sent": 36, "fail": 1}, "per-worker tally kept")


def test_progress_bar_never_shows_zero_denominator():
    st = sp.PhaseState([], [])
    for _ in range(620):
        st.record_ok("W9_125")
    body = sl.render_live(phone="989962124887", tag="", worker_tag="W9_125",
                          worker_ip="45.38.60.138", state=st, elapsed=96,
                          clock="15:39:25", stopped=True)
    ok("620/0" not in body, "the nonsensical 620/0 bar is gone")
    ok("620/620" in body, f"the bar uses a sane denominator\n{body}")
    ok("Sent 620" in body, "the real send count is shown")


def test_live_sync_remote_mirrors_worker_phases():
    import send_integrate as si
    si.live_begin(4242, "0912", total_oneway=100, total_twoway=50,
                  worker_tag="W9", worker_ip="1.2.3.4")
    si.live_sync_remote(4242, worker_tag="W9", oneway_ok=30, oneway_fail=5,
                        twoway_ok=6, twoway_fail=0, total_ok=36, total_fail=5,
                        phase="twoway")
    st = si.live_state(4242)
    eq(st.stats[sp.PHASE_ONEWAY], {"sent": 30, "fail": 5}, "one-way mirrored")
    eq(st.stats[sp.PHASE_TWOWAY], {"sent": 6, "fail": 0}, "two-way mirrored")
    eq(st.total_sent(), 36, "totals match the worker")
    eq(st.phase, sp.PHASE_TWOWAY, "phase mirrored")
    ok(st.oneway_blocked, "a flip to two-way marks one-way as blocked")

    # OLD worker without the phase fields -> nothing is lost
    si.live_begin(4243, "0912", total_oneway=0, total_twoway=10,
                  worker_tag="W4", worker_ip="")
    si.live_sync_remote(4243, worker_tag="W4", total_ok=12, total_fail=3)
    st2 = si.live_state(4243)
    eq(st2.total_sent(), 12, "old worker's totals still counted")
    eq(st2.total_fail(), 3, "old worker's failures still counted")


def test_no_twoway_contacts_asks_for_rotation():
    """PRODUCTION 2026-11-27 18:14: the account had 1699 contacts and ZERO
    two-way ones, so twoway_from == n. The jump landed on the END of the list:
    the run 'FINISHED' with 1508 recipients never tried and no rotation. Now
    the jump is refused in that case and a rotation is requested instead."""
    # the pure helper always knew this; the call sites did not use it
    eq(sp.PhaseState([1, 2, 3], []).next_action.__self__.two_way, [],
       "a state with no two-way block has an empty two-way list")
    st = sp.PhaseState([1, 2, 3, 4, 5, 6], [])     # strangers only
    for _ in range(5):
        st.record_fail()
    eq(st.next_action(), sp.ACT_STOP,
       "no two-way list -> never claim a phase jump is possible")

    # worker: only jumps when the two-way block really exists
    ok("idx < _tw < n" in WAPI,
       "the worker jumps ONLY when a two-way block exists")
    ok("_tw >= n and idx < n" in WAPI,
       "with no two-way contacts the worker stops so the master can rotate")
    ok("ONE-WAY BLOCKED" in WAPI, "the worker says why it stopped")

    # master: same guard, and rotation is no longer gated on being able to jump
    b = _fn_src(BOT, "run_send")
    ok("can_jump" in b, "the master distinguishes 'a split exists' from 'can jump'")
    ok("if can_jump and idx < twoway_from" in b,
       "the master jumps only when there IS a two-way block")
    ok("ONE-WAY BLOCKED" in b, "the master reports the no-fallback case")
    i_jump = b.find("if can_jump and idx < twoway_from")
    i_rot = b.find("_ROTATE_REASON")
    ok(i_jump != -1 and i_rot != -1 and i_rot > i_jump,
       "rotation is requested AFTER the jump option is exhausted")


def main():
    if "--selftest" not in sys.argv:
        print("usage: send_remote_phase_test.py --selftest")
        return 0
    for fn in (test_no_twoway_contacts_asks_for_rotation,
               test_worker_accepts_and_applies_phase_order,
               test_master_requests_phase_order_only_for_fresh_runs,
               test_worker_phase_jump, test_live_begin_after_total_is_known,
               test_counters_never_vanish_in_done_state,
               test_progress_bar_never_shows_zero_denominator,
               test_live_sync_remote_mirrors_worker_phases):
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
