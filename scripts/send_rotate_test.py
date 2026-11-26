#!/usr/bin/env python3
"""
🔄 Automatic rotation — offline tests (stubbed db/worker/worker_transfer).
==========================================================================

Proves send_rotate drives the PROJECT'S OWN transfer chain and nothing new:
  pick_worker_for_transfer(exclude_ids=tried) -> get_session_blob ->
  bot._session_transfer_to_worker -> db.set_account_worker
and that a tried server is never revisited, `exhausted` is reported when no
server is left, and every failure mode is surfaced instead of swallowed.

  python3 scripts/send_rotate_test.py --selftest
"""
import os
import sys
import types
import asyncio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "rubika"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

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
# Stubs for the pinned modules (we only verify they are CALLED correctly)
# --------------------------------------------------------------------------- #
CALLS = []

_state = {
    "accounts": {7: {"id": 7, "phone": "0912", "worker_id": 1}},
    "session": {"auth": "AUTH", "private_key": "K", "phone": "0912"},
    "settings": {},
    "account_worker": {},
    "transfer_ok": True,
    "pick": None,
}

dbm = types.ModuleType("db")
dbm.get_account = lambda aid: _state["accounts"].get(int(aid))
dbm.get_session_blob = lambda aid: (CALLS.append(("get_session_blob", aid))
                                    or _state["session"])


def _set_account_worker(aid, wid):
    CALLS.append(("set_account_worker", aid, wid))
    _state["account_worker"][aid] = wid


dbm.set_account_worker = _set_account_worker
dbm.get_int_setting = lambda k, d: int(_state["settings"].get(k, d))
dbm.set_setting = lambda k, v: _state["settings"].__setitem__(k, v)
sys.modules["db"] = dbm

wkm = types.ModuleType("worker")
wkm.worker_for_account = lambda acc: ({"id": 1, "tag": "W4_211",
                                       "ip": "46.8.226.22", "is_master": 0}
                                      if acc else None)
wkm.is_local = lambda w: bool(w and w.get("is_master"))
sys.modules["worker"] = wkm

wtm = types.ModuleType("worker_transfer")
_tried = {}
wtm.get_tried = lambda aid: list(_tried.get(int(aid), []))


def _add_tried(aid, wid):
    _tried.setdefault(int(aid), [])
    if int(wid) not in _tried[int(aid)]:
        _tried[int(aid)].append(int(wid))


wtm.add_tried = _add_tried
wtm.set_tried = lambda aid, ids: _tried.__setitem__(int(aid),
                                                    [int(x) for x in (ids or [])])
wtm.clear_tried = lambda aid: _tried.pop(int(aid), None)


async def _pick(exclude_ids=None, verify=True):
    CALLS.append(("pick_worker_for_transfer", sorted(exclude_ids or [])))
    return _state["pick"]


wtm.pick_worker_for_transfer = _pick
sys.modules["worker_transfer"] = wtm

rcm = types.ModuleType("rubika_client")
rcm.normalize_phone = lambda p: str(p)
sys.modules["rubika_client"] = rcm

import send_rotate as sr  # noqa: E402  (after stubs)
import send_phase as sp   # noqa: E402


class FakeBot:
    async def _session_transfer_to_worker(self, neww, sess, phone):
        CALLS.append(("_session_transfer_to_worker", neww.get("tag"), phone))
        return _state["transfer_ok"]


def _reset():
    CALLS.clear()
    _tried.clear()
    _state["transfer_ok"] = True
    _state["session"] = {"auth": "AUTH", "private_key": "K"}
    _state["pick"] = {"id": 2, "tag": "W7_933", "ip": "46.8.226.33",
                      "is_master": 0}
    _state["account_worker"].clear()
    _state["settings"].clear()


def run(c):
    return asyncio.run(c)


# --------------------------------------------------------------------------- #
def test_prime_tried_includes_current_server():
    _reset()
    acc = _state["accounts"][7]
    tried = sr.prime_tried(7, acc, payload_tried=[5])
    ok(5 in tried, "payload's tried servers restored")
    ok(1 in tried, "the account's CURRENT server is marked tried")


def test_successful_transfer_uses_existing_chain():
    _reset()
    res = run(sr.auto_transfer(FakeBot(), 7, "0912"))
    eq(res["ok"], True, "transfer succeeded")
    eq(res["tag"], "W7_933", "new worker tag returned")
    eq(res["ip"], "46.8.226.33", "new worker ip returned")
    names = [c[0] for c in CALLS]
    eq(names, ["pick_worker_for_transfer", "get_session_blob",
               "_session_transfer_to_worker", "set_account_worker"],
       "calls the project's OWN transfer chain, in order")
    eq(_state["account_worker"][7], 2, "account moved to the new worker id")
    ok(2 in sr.tried_for(7), "new worker recorded as tried BEFORE transferring")


def test_never_revisits_a_tried_server():
    _reset()
    sr.prime_tried(7, _state["accounts"][7], payload_tried=[5, 9])
    run(sr.auto_transfer(FakeBot(), 7, "0912"))
    pick = [c for c in CALLS if c[0] == "pick_worker_for_transfer"][0]
    eq(pick[1], [1, 5, 9], "excludes every previously tried server + current")


def test_exhausted_when_no_server_left():
    _reset()
    _state["pick"] = None
    res = run(sr.auto_transfer(FakeBot(), 7, "0912"))
    eq(res["ok"], False, "cannot transfer")
    eq(res["exhausted"], True, "reports exhausted so the caller stops rotating")
    ok("all tried" in res["error"], "explains why")


def test_transfer_failure_is_not_exhausted():
    _reset()
    _state["transfer_ok"] = False
    res = run(sr.auto_transfer(FakeBot(), 7, "0912"))
    eq(res["ok"], False, "transfer failed")
    eq(res["exhausted"], False, "a failed import is NOT exhaustion -> next switch allowed")
    ok("failed" in res["error"], "error surfaced, not swallowed")


def test_missing_session_blocks_codefree_transfer():
    _reset()
    _state["session"] = {}
    res = run(sr.auto_transfer(FakeBot(), 7, "0912"))
    eq(res["ok"], False, "no stored session -> no code-free transfer")
    ok("code-free" in res["error"], "says a code would be required")


def test_max_switches_setting():
    _reset()
    eq(sr.max_switches(), sp.DEFAULT_MAX_SWITCHES, "defaults to 3")
    sr.set_max_switches(5)
    eq(sr.max_switches(), 5, "panel-tunable via app_settings")
    sr.set_max_switches(0)
    eq(sr.max_switches(), 0, "zero disables rotation")


def test_current_server():
    _reset()
    tag, ip = sr.current_server(_state["accounts"][7])
    eq(tag, "W4_211", "current server tag")
    eq(ip, "46.8.226.22", "current server ip")
    eq(sr.current_server(None), ("local", ""), "no account -> local")


def main():
    if "--selftest" not in sys.argv:
        print("usage: send_rotate_test.py --selftest")
        return 0
    for fn in (test_prime_tried_includes_current_server,
               test_successful_transfer_uses_existing_chain,
               test_never_revisits_a_tried_server,
               test_exhausted_when_no_server_left,
               test_transfer_failure_is_not_exhausted,
               test_missing_session_blocks_codefree_transfer,
               test_max_switches_setting, test_current_server):
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
