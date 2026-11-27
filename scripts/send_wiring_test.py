#!/usr/bin/env python3
"""
🔗 send wiring test — is the new send behaviour really CONNECTED to bot.py?
===========================================================================

The modules were tested in isolation already. This proves the WIRING:

  * both prepare paths store the phase-ordered list + `twoway_from`
  * run_send reads them, fixes the cap at 5, jumps phase, and asks to rotate
  * the every-50-recipients spam is gone from BOTH send paths
  * the live card is created, edited and finished on BOTH paths
  * rotation is hooked at _offer_resume_after_send (covers local AND remote)
  * auth errors go through the precise classifier + curiosity capture
  * Settings exposes the switch budget and curiosity, and the text step is
    registered in the message router (a missed step = silently ignored input)
  * every helper bot.py calls on send_integrate actually exists

  python3 scripts/send_wiring_test.py --selftest
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# send_integrate pulls in send_rotate -> db / worker, which live in core/ and
# workers/ — the same flat path main.py builds on the master.
for _sub in ("", "core", "rubika", "workers", "status"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

# dev box has no dotenv/rubpy (same as the project's other test setups): stub
# the bare minimum so config/db/rubika_client import, then the REAL modules
# under test run untouched.
import types as _t


def _stub(name, **attrs):
    if name in sys.modules:
        return
    m = _t.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


_stub("dotenv", load_dotenv=lambda *a, **k: None, find_dotenv=lambda *a, **k: "")
_rubpy = _t.ModuleType("rubpy")
_rubpy.__path__ = []                       # make it a package
_rubpy.Client = object
sys.modules.setdefault("rubpy", _rubpy)
_stub("rubpy.types", Update=object)
_stub("rubpy.crypto", Crypto=object)
_stub("rubpy.sessions", SQLiteSession=object, StringSession=object)

BOT = os.path.join(_ROOT, "bot_app", "bot.py")
SRC = open(BOT, encoding="utf-8").read()
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


def _fn(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _body(name):
    n = _fn(name)
    return ast.get_source_segment(SRC, n) if n else ""


# --------------------------------------------------------------------------- #
def test_prepare_paths_carry_phases():
    ok(SRC.count("_phase_order(ordered)") >= 2,
       "both prepare paths compute the phase split")
    ok(SRC.count('"twoway_from"') >= 2,
       "both prepare paths store twoway_from in the payload")
    # the phase helper must be guarded so a failure cannot block a send
    ph = _body("_phase_order")
    ok("except Exception" in ph and 'r["guid"] for r in ordered_records' in ph,
       "_phase_order falls back to the plain order on failure")


def test_run_send_uses_phases():
    b = _body("run_send")
    ok('payload.get("twoway_from")' in b, "run_send reads twoway_from")
    ok("phases_on" in b, "run_send tracks whether phases are active")
    ok("_sp_streak_limit()" in b,
       "the consecutive-error cap becomes the fixed 5 when phases are on")
    ok("idx = twoway_from" in b, "run_send jumps to the two-way block")
    ok("SEND PHASE CHANGE" in b, "the phase change is logged")
    ok("_ROTATE_REASON" in b, "run_send can ask for a server rotation")
    ok("should_rotate" in b, "rotation is only asked for when it makes sense")


def test_streak_limit_is_five():
    import send_phase
    eq(send_phase.STREAK_LIMIT, 5, "the streak limit is the owner's fixed 5")
    eq(send_phase.SWITCH_PAUSE_SEC, 180, "3 minutes between switches")
    eq(send_phase.DEFAULT_MAX_SWITCHES, 3, "default budget is 3")


def test_no_more_fifty_message_spam():
    eq(SRC.count("SEND_LOG_EVERY"), 0,
       "the every-50-recipients progress spam is gone from bot.py")
    for fname in ("run_send", "run_send_remote"):
        b = _body(fname)
        ok("live_begin" in b, f"{fname} starts the live card")
        ok("live_push" in b, f"{fname} edits the live card")
        ok("live_finish" in b, f"{fname} finishes the live card")


def test_rotation_hooked_for_both_paths():
    b = _body("_offer_resume_after_send")
    ok("auto_rotate" in b, "automatic rotation is hooked at the shared point")
    ok("_do_resume" in b, "after a successful transfer the list is resumed")
    ok("save_paused_send" in b,
       "the remaining list is persisted BEFORE transferring")
    ok("should_rotate" in b, "rotation is gated (never for a dead session)")
    ok("except Exception" in b,
       "a rotation failure falls back to the manual panel")
    # both callers reach it
    ok("_offer_resume_after_send" in _body("run_send"), "local path reaches it")
    ok("_offer_resume_after_send" in _body("run_send_remote"),
       "remote path reaches it")


def test_auth_errors_classified():
    b = _body("run_send")
    ok("note_auth_error" in b, "send failures go through the auth classifier")
    import auth_diag
    eq(auth_diag.classify_auth("AUTH_FROM_ANOTHER"), auth_diag.ACC_CONFLICT,
       "two-places-at-once is its own code")
    eq(auth_diag.classify_auth("INVALID_AUTH"), auth_diag.ACC_INVALID,
       "invalidated session is its own code")
    eq(auth_diag.classify_auth("NOT_REGISTERED"), auth_diag.ACC_GONE,
       "deleted account is its own code")
    eq(auth_diag.classify_auth("TOO_REQUESTS"), "",
       "a rate limit is NOT an auth error")
    ok(not auth_diag.rotation_helps(auth_diag.ACC_INVALID),
       "rotation never claimed to help an auth failure")


def test_rotation_refuses_on_auth_or_dead():
    import send_integrate as si
    si.reset_rotation(999)
    ok(not si.should_rotate(999, dead=True, reason="whatever"),
       "never rotate a dead session")
    ok(not si.should_rotate(999, dead=False,
                            reason="Session invalid (re-login needed)"),
       "never rotate on an invalid session")
    ok(not si.should_rotate(999, dead=False, reason="Manual stop by user"),
       "never rotate after a manual stop")
    ok(not si.should_rotate(999, dead=False, reason="INVALID_AUTH"),
       "never rotate on a raw auth body")
    ok(si.should_rotate(999, dead=False, reason="consecutive send errors"),
       "DO rotate on plain consecutive send errors")


def test_phase_helpers():
    import send_integrate as si
    recs = [{"guid": "s1", "mutual": False}, {"guid": "m1", "mutual": True},
            {"guid": "s2", "mutual": False}]
    guids, boundary = si.phase_split(recs)
    eq(guids, ["s1", "s2", "m1"], "one-way first, then two-way")
    eq(boundary, 2, "boundary marks where two-way starts")
    eq(si.phase_of(0, 2), "oneway", "index 0 is one-way")
    eq(si.phase_of(2, 2), "twoway", "index 2 is two-way")
    eq(si.phase_jump(0, 2, 3), 2, "jump lands on the two-way block")
    eq(si.phase_jump(2, 2, 3), None, "no jump once already two-way")
    eq(si.phase_jump(0, 0, 3), None, "no jump when there are no phases")
    eq(si.phase_jump(0, 3, 3), None, "no jump when there is no two-way block")


def test_settings_and_router():
    ok('b"set_switches"' in SRC, "Settings has the server-switch button")
    ok('b"curio"' in SRC, "Settings has the curiosity button")
    ok('"await_set_switches"' in SRC, "the switch-count step is created")
    ok(SRC.count('elif step == "await_set_switches"') == 1,
       "the step IS registered in the message router (else input is ignored)")
    ok("def handle_set_switches" in SRC, "its handler exists")
    for cb in ("curio_txt", "curio_tog", "curio_clear"):
        ok(f'b"{cb}"' in SRC, f"curiosity handler {cb} wired")
    ok("send_file" in _body("curiosity_txt_cb"), "the .txt is actually sent")


def test_every_called_helper_exists():
    """Every send_integrate.<x> bot.py calls must exist on the module."""
    import send_integrate as si
    called = set()
    for n in ast.walk(TREE):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "_si"):
            called.add(n.attr)
    ok(called, f"found send_integrate calls ({len(called)})")
    missing = sorted(a for a in called if not hasattr(si, a))
    eq(missing, [], "every send_integrate helper bot.py calls exists")
    # and the modules it fronts for
    for mod in ("send_phase", "send_live", "send_rotate", "auth_diag"):
        ok(mod in open(os.path.join(_ROOT, "rubika", "send_integrate.py"),
                       encoding="utf-8").read(),
           f"send_integrate fronts {mod}")


def main():
    if "--selftest" not in sys.argv:
        print("usage: send_wiring_test.py --selftest")
        return 0
    for fn in (test_prepare_paths_carry_phases, test_run_send_uses_phases,
               test_streak_limit_is_five, test_no_more_fifty_message_spam,
               test_rotation_hooked_for_both_paths, test_auth_errors_classified,
               test_rotation_refuses_on_auth_or_dead, test_phase_helpers,
               test_settings_and_router, test_every_called_helper_exists):
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
