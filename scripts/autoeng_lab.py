#!/usr/bin/env python3
"""
🔁 Automation Engine — standalone selftest lab (read-only, offline).
====================================================================

Mirrors the project's other labs (send_order_lab / name_render_lab):
  python3 scripts/autoeng_lab.py --selftest
It exercises the PURE logic (roles, distribution, error classification),
the live-log renderer, and the SEPARATE engine DB against a scratch file in
/tmp. It touches NO real account, NO worker, NO network, and never writes to
core/data/*.

An AST safety guard (same spirit as the other labs) fails the selftest if this
file ever gains a real send / worker-lifecycle / os.system call.
"""
import os
import sys
import ast
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "core", "rubika", "workers", "bot_app", "status"):
    p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if p not in sys.path:
        sys.path.insert(0, p)

import autoeng_core as core
import autoeng_log as live
import autoeng_db as adb

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
# Roles
# --------------------------------------------------------------------------- #
def test_roles():
    eq(core.scanner_count(3), 0, "3 accounts -> engine refuses")
    eq(core.scanner_count(4), 1, "4 -> 1 scanner")
    eq(core.scanner_count(5), 2, "5 -> 2")
    eq(core.scanner_count(6), 2, "6 -> 2")
    eq(core.scanner_count(7), 2, "7 -> 2")
    eq(core.scanner_count(8), 3, "8 -> 3")
    eq(core.scanner_count(9), 3, "9 -> 3")
    eq(core.scanner_count(10), 4, "10 -> 4")
    eq(core.scanner_count(20), 4, "20 -> capped at 4")

    roles = core.assign_roles([11, 12, 13, 14, 15])
    scanners, senders = core.split_roles(roles)
    eq(scanners, [11, 12], "first 2 are scanners")
    eq(senders, [13, 14, 15], "rest are senders")
    eq(core.assign_roles([1, 2, 3]), {}, "below min -> empty")


# --------------------------------------------------------------------------- #
# Link distribution
# --------------------------------------------------------------------------- #
def test_distribute():
    d = core.distribute_links(["a", "b", "c", "d", "e"], [1, 2])
    eq(d[1], ["a", "c", "e"], "round-robin scanner 1")
    eq(d[2], ["b", "d"], "round-robin scanner 2")

    d2 = core.distribute_links(["a", "b", "c"], [1, 2], dead={1})
    eq(d2, {2: ["a", "b", "c"]}, "dead scanner excluded")
    eq(core.distribute_links(["a"], [1], dead={1}), {}, "no live scanner")

    # dead mid-run: scanner 1 still had b,c pending -> moved to 2
    pend = {1: ["b", "c"], 2: ["x"]}
    new = core.reassign_dead_scanner(pend, 1, [1, 2], dead={1})
    eq(sorted(new.keys()), [2], "scanner 1 removed")
    eq(new[2], ["x", "b", "c"], "leftover appended to live scanner")


# --------------------------------------------------------------------------- #
# Group hand-off (hot distribution)
# --------------------------------------------------------------------------- #
def test_assign_group():
    # three senders, groups_per=3. empty -> first wins
    eq(core.assign_group({}, [5, 6, 7], 3), 5, "empty -> first sender")
    # 5 already has 1, others 0 -> pick a 0
    eq(core.assign_group({5: 1}, [5, 6, 7], 3), 6, "least-loaded sender")
    # all full -> None
    eq(core.assign_group({5: 3, 6: 3, 7: 3}, [5, 6, 7], 3), None, "all full")
    eq(core.capacity([5, 6, 7], 3), 9, "capacity 3x3")
    ok(core.reserve_is_full(30), "reserve full at 30")
    ok(not core.reserve_is_full(29), "reserve not full at 29")


# --------------------------------------------------------------------------- #
# Error classification  (every guard proven with a violating sample)
# --------------------------------------------------------------------------- #
def test_errors():
    eq(core.classify_error("INVALID_AUTH"), core.ACC_DEAD, "auth -> ACC_SESSION")
    eq(core.classify_error("AUTH_FROM_ANOTHER"), core.ACC_DEAD, "auth2")
    eq(core.classify_error("TOO_REQUESTS"), core.ACC_LIMIT, "rate -> ACC_LIMIT")
    eq(core.classify_error("you are BANNED from this group"),
       core.GRP_BLOCKED, "ban -> GRP_BLOCKED")
    eq(core.classify_error("JOIN_REQUEST sent", context="join"),
       core.GRP_APPROVAL, "approval -> GRP_APPROVAL")
    eq(core.classify_error("INVALID_LINK", context="join"),
       core.LNK_BAD, "bad link -> LNK_BAD")
    eq(core.classify_error("httpx ConnectError to worker", context="worker"),
       core.WRK_DOWN, "worker -> WRK_DOWN")
    eq(core.classify_error("Request TIMEOUT", context="worker"),
       core.WRK_DOWN, "worker RPC timeout -> WRK_DOWN")
    eq(core.classify_error("Request TIMEOUT"), core.NET_ERR, "net -> NET_ERR")
    eq(core.classify_error("something weird"), core.SYS_ERR, "fallback -> SYS")

    ok(core.is_fatal_account(core.ACC_DEAD), "ACC_DEAD is fatal")
    ok(not core.is_fatal_account(core.ACC_LIMIT), "ACC_LIMIT not fatal")
    ok(core.is_group_fault(core.GRP_BLOCKED), "GRP_BLOCKED is group fault")
    ok(not core.is_group_fault(core.NET_ERR), "NET_ERR not a group fault")
    ok(core.should_retry_link(core.NET_ERR), "retry link on NET")
    ok(not core.should_retry_link(core.LNK_BAD), "do NOT retry a bad link")


# --------------------------------------------------------------------------- #
# Live-log renderer
# --------------------------------------------------------------------------- #
def _sample_state(n_senders=6):
    return {
        "running": True, "elapsed": 4360, "clock": "14:32:10",
        "scan": {"extracted": 200, "checked": 140, "healthy": 48,
                 "dead": 70, "approval": 15, "error": 7},
        "saved": 22, "saved_max": 30,
        "totals": {"sent": 292, "fail": 12, "pv_in": 32, "pv_out": 31},
        "scanners": [
            {"tag": "A1", "worker": "W4_211", "checked": 35, "assigned": 50, "ok": 12},
            {"tag": "A3", "worker": "W7_933", "resting": True},
            {"tag": "A4", "icon": live.IC_DEAD, "note": "QUARANTINED · ACC_SESSION"},
        ],
        "senders": [
            {"tag": f"B{i}", "worker": "W7", "icon": live.IC_SEND,
             "groups": 3, "groups_target": 3, "sent": 80 - i, "fail": 0,
             "pv_in": 5, "pv_out": 5} for i in range(n_senders)
        ],
    }


def test_live():
    msgs = live.render_live(_sample_state())
    eq(len(msgs), 1, "small fleet -> one message")
    body = msgs[0]
    ok(body.startswith("| ⚙ - #automation"), "card header")
    ok("--| LIVE · 01:12:40 · 14:32:10" in body, "elapsed formatted")
    ok("140/200" in body, "scan counts present")
    ok("🔴 QUARANTINED · ACC_SESSION" in body, "dead scanner line")
    ok("💤" in body, "resting icon present")
    ok(all(len(m) <= live.TG_LIMIT for m in msgs), "within TG limit")

    eq(live._bar(5, 10, 10), "[█████░░░░░]", "bar half")
    eq(live._bar(0, 0, 10), "[░░░░░░░░░░]", "bar zero total safe")
    eq(live._bar(10, 10, 10), "[██████████]", "bar full")
    eq(live._fmt_elapsed(3661), "01:01:01", "elapsed hh:mm:ss")

    # huge fleet overflows into >1 pinned message, each within the limit
    big = live.render_live(_sample_state(n_senders=400))
    ok(len(big) >= 2, "huge fleet spills to multiple messages")
    ok(all(len(m) <= live.TG_LIMIT for m in big), "each overflow msg within limit")

    err = live.error_card(code="GRP_BLOCKED", stage="send", account="0912...",
                          worker="W4_211", reason="BANNED", action="replaced",
                          clock="14:00:00")
    ok("GRP_BLOCKED" in err and "replaced" in err, "error card renders")


# --------------------------------------------------------------------------- #
# Engine DB (scratch file in /tmp)
# --------------------------------------------------------------------------- #
def test_db():
    tmp = tempfile.mkdtemp(prefix="aedb_")
    adb.set_db_path_for_tests(os.path.join(tmp, "autoeng.db"))
    adb.init()

    # settings
    adb.set("scan_count", 100)
    eq(adb.get_int("scan_count", 0), 100, "settings int roundtrip")
    adb.set("pv_enabled", "true")
    ok(adb.get_bool("pv_enabled"), "settings bool roundtrip")

    # roles persist and reload
    roles = core.assign_roles([10, 11, 12, 13, 14])
    adb.save_roles(roles)
    eq(adb.load_roles(), roles, "roles persist")

    # link dedup (only NEW links accepted)
    ok(adb.link_seen("http://g/1"), "new link accepted")
    ok(not adb.link_seen("http://g/1"), "dup link rejected")
    eq(adb.pending_links(), ["http://g/1"], "pending list")
    adb.set_link_result("http://g/1", adb.L_DONE, "healthy")
    eq(adb.pending_links(), [], "done link no longer pending")

    # groups + reserve + hot distribution counts
    adb.upsert_group("g1", "http://g/1", "Grp1", adb.G_HEALTHY, members=120)
    adb.upsert_group("g2", "http://g/2", "Grp2", adb.G_HEALTHY, members=80)
    eq(adb.count_unused_healthy(), 2, "two healthy banked")
    row = adb.take_reserve_group()
    ok(row and row["guid"] in ("g1", "g2"), "take one reserve group")
    eq(adb.count_unused_healthy(), 1, "reserve shrank")

    adb.mark_group_joined(row["guid"], 13)
    eq(adb.group_counts_by_owner([13, 14])[13], 1, "owner group count")

    # cross-account ban -> exclusion
    eq(adb.add_ban_hit("g2", 13), 1, "first ban hit")
    eq(adb.add_ban_hit("g2", 14), 2, "second distinct ban hit")
    adb.set_group_state("g2", adb.G_EXCLUDED)
    ok(adb.is_excluded("g2"), "group excluded for all")

    # texts rotation source
    adb.add_text("t1")
    adb.add_text("t2")
    eq(adb.list_texts(), ["t1", "t2"], "texts stored in order")

    # PV ledger (durable once-per-person) + counters
    ok(not adb.already_replied(13, "uX"), "not replied yet")
    adb.mark_replied(13, "uX")
    ok(adb.already_replied(13, "uX"), "replied recorded")
    adb.incr_pv(13, received=3, replied=2)
    eq(adb.get_pv(13), {"received": 3, "replied": 2}, "pv counters")

    # send stats
    adb.incr_stats(13, sent=10, fail=1)
    adb.incr_stats(13, sent=5)
    eq(adb.get_stats(13), {"sent": 15, "fail": 1}, "send stats accumulate")

    # reset keeps healthy groups, clears roles+stats+queue
    adb.upsert_group("g3", "http://g/3", "Grp3", adb.G_HEALTHY)
    adb.reset_run(keep_groups=True)
    eq(adb.load_roles(), {}, "roles cleared on reset")
    eq(adb.get_stats(13), {"sent": 0, "fail": 0}, "stats cleared on reset")
    ok(adb.count_unused_healthy() >= 1, "healthy groups survive reset")


# --------------------------------------------------------------------------- #
# AST safety guard — prove it fires on a violating sample (self-check)
# --------------------------------------------------------------------------- #
_FORBIDDEN_CALLS = ("system", "Popen", "forward_message", "send_message",
                    "run_send", "run_send_remote")


def _scan_forbidden(src: str) -> list:
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in _FORBIDDEN_CALLS:
                hits.append(name)
    return hits


def test_guard():
    with open(os.path.abspath(__file__)) as fh:
        hits = _scan_forbidden(fh.read())
    eq(hits, [], "lab file contains no real send/system calls")
    # prove the guard is not hollow: it MUST catch a violating snippet
    bad = "import os\nos.system('rm -rf /')\nclient.send_message(g, t)\n"
    found = _scan_forbidden(bad)
    ok("system" in found and "send_message" in found, "guard catches violations")


def main():
    if "--selftest" not in sys.argv:
        print("usage: autoeng_lab.py --selftest")
        return 0
    for fn in (test_roles, test_distribute, test_assign_group, test_errors,
               test_live, test_db, test_guard):
        fn()
    print(f"\n  ==== {_passed} passed, {_failed} failed ====\n")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
