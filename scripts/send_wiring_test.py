#!/usr/bin/env python3
"""
🔗 send wiring test — is the RESTORED send behaviour connected to bot.py?
=========================================================================

The phase split (one-way first) was removed at the owner's request. The order
is now send_order's own: ALL two-way contacts first (by last-seen), then all
one-way — enforced in send_order.build by a `has_chat`-first sort key. Server
rotation is the simple rule: `max_errors` consecutive failures -> 3-min wait ->
next server -> continue, up to the budget, then give up.

This test proves the WIRING of that restored behaviour and that the removed
phase logic is really gone from bot.py / worker_api.py.

  python3 scripts/send_wiring_test.py --selftest
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

BOT = os.path.join(_ROOT, "bot_app", "bot.py")
WAPI = os.path.join(_ROOT, "workers", "worker_api.py")
SRC = open(BOT, encoding="utf-8").read()
WSRC = open(WAPI, encoding="utf-8").read()
TREE = ast.parse(SRC)

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


def _fn(src, name):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


# --------------------------------------------------------------------------- #
# 1. The phase split is GONE
# --------------------------------------------------------------------------- #
def test_phase_logic_removed():
    for token in ("_phase_order(", "twoway_from", "phases_on", "can_jump",
                  "SEND PHASE CHANGE", "ONE-WAY BLOCKED", "go_twoway"):
        eq(SRC.count(token), 0, f"bot.py no longer references {token!r}")
    for token in ("twoway_from", "phase_order", "split_recipients",
                  "SEND PHASE CHANGE", "oneway_ok"):
        eq(WSRC.count(token), 0, f"worker_api.py no longer references {token!r}")


# --------------------------------------------------------------------------- #
# 2. Order comes straight from send_order (two-way first), on every path
# --------------------------------------------------------------------------- #
def test_every_path_uses_send_order_directly():
    # each prepare/order helper builds recipients as a plain list of the
    # send_order-ordered guids — no re-phasing in between.
    ok(SRC.count('[r["guid"] for r in ordered]') >= 4,
       "all local send paths use send_order's order verbatim")
    # worker builds the same way and no longer splits
    ok('recipients = [r["guid"] for r in ordered]' in WSRC,
       "worker uses send_order's order verbatim")


def test_send_order_has_chat_first():
    import send_order as so
    src = open(os.path.join(_ROOT, "rubika", "send_order.py"), encoding="utf-8").read()
    ok('0 if r["has_chat"] else 1' in src,
       "build() sorts has_chat (two-way) ahead of one-way")
    # and the sort key keeps presence order WITHIN each group
    body = _fn(src, "build")
    ok(body.index('has_chat') < body.index('_TIER_INDEX'),
       "has_chat is a higher-priority sort key than the presence tier")


# --------------------------------------------------------------------------- #
# 3. Simple server switch is wired into run_send
# --------------------------------------------------------------------------- #
def test_server_switch_wired():
    b = _fn(SRC, "run_send")
    ok("_rotate_in_place(" in b, "run_send calls the simple rotation helper")
    ok("_SWITCHED_REASON" in b, "a switched leg ends with the switch marker")
    r = _fn(SRC, "_rotate_in_place")
    ok("SWITCH_PAUSE_SEC" in r, "it waits the 3-minute gap")
    ok("auto_rotate" in r, "it performs the move via the proven transfer chain")
    ok("_do_resume" in r, "and resumes the remaining list on the new server")
    ok("switches_used" in r and "_rotation_max" in r,
       "it respects the switch budget and gives up when spent")
    # the switched leg must NOT also trigger the manual resume panel
    o = _fn(SRC, "_offer_resume_after_send")
    ok("_SWITCHED_REASON" in o and "return" in o,
       "a switched run skips the manual transfer panel")


def test_switch_after_max_errors():
    # rotation triggers off the same consecutive-error cap the loop already
    # uses (hit_max), not a separate hard-coded number.
    b = _fn(SRC, "run_send")
    ok("attempt_fail >= max_errors" in b, "hit_max uses max_errors (owner-set)")
    ok("switch_mode" in b, "rotation is gated by switch_mode")
    ok("_rotation_enabled()" in b, "switch_mode reads the enabled flag")


def test_rotation_budget_setting():
    import send_rotate
    send_rotate.set_max_switches(3)
    eq(send_rotate.max_switches(), 3, "budget tunable (default 3)")
    send_rotate.set_max_switches(0)
    eq(send_rotate.max_switches(), 0, "0 disables rotation")
    send_rotate.set_max_switches(3)


def test_no_more_fifty_message_spam():
    eq(SRC.count("SEND_LOG_EVERY"), 0, "the every-50 spam is gone")
    for fname in ("run_send", "run_send_remote"):
        b = _fn(SRC, fname)
        ok("live_begin" in b, f"{fname} starts the live card")
        ok("live_finish" in b, f"{fname} finishes the live card")


def test_live_card_starts_two_way_whole_list():
    # live_begin is now called with the whole list as one block (no phase split)
    for fname in ("run_send", "run_send_remote"):
        b = _fn(SRC, fname)
        ok("total_oneway=0" in b,
           f"{fname} starts the live card with no phase split")


def main():
    if "--selftest" not in sys.argv:
        print("usage: send_wiring_test.py --selftest")
        return 0
    for fn in (test_phase_logic_removed, test_every_path_uses_send_order_directly,
               test_send_order_has_chat_first, test_server_switch_wired,
               test_switch_after_max_errors, test_rotation_budget_setting,
               test_no_more_fifty_message_spam,
               test_live_card_starts_two_way_whole_list):
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
