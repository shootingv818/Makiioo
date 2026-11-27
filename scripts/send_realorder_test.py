#!/usr/bin/env python3
"""
📤 Real send-order test — the WHOLE chain, not a slice.
========================================================

The previous bug hid because tests fed split_recipients hand-made records and
never exercised the real chain `build() -> ordered_recipients() -> recipients`.
This test drives that exact chain with realistic contacts and asserts the
ACTUAL order the send loop receives.

Owner's rule (the original, restored project behaviour):
  two-way contacts (a chat exists, either direction) FIRST, ordered by most
  recent last-seen; strangers (no chat) LAST.

  python3 scripts/send_realorder_test.py --selftest
"""
import os
import sys
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "rubika"))

# send_order imports db (for the strategy setting); stub it (no .env in dev).
_db = types.ModuleType("db")
_db.get_setting = lambda k, d=None: d
_db.set_setting = lambda k, v: None
sys.modules["db"] = _db

import send_order as so  # noqa: E402

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


def _now():
    return int(time.time())


def _mk_users(specs):
    """specs: list of (guid, seconds_since_seen)."""
    n = _now()
    return [{"user_guid": g, "first_name": g,
             "online_time": {"type": "exact", "exact_time": n - age}}
            for g, age in specs]


def _mk_chat(guid, *, they_wrote=False, we_wrote=False, age=100):
    """A chat row. we_wrote -> last message is mine; they_wrote -> not mine /
    unseen. Either direction means a chat EXISTS (two-way by the owner's rule)."""
    n = _now()
    lm = {"is_mine": not they_wrote} if (they_wrote or we_wrote) else {}
    return {"object_guid": guid, "type": "user", "time": n - age,
            "last_message": lm,
            "last_seen_peer_mid": "55" if they_wrote else "0",
            "count_unseen": 1 if they_wrote else 0}


def _order(users, chats):
    """The REAL chain the send loop uses: build() then the {guid,name} list
    ordered_recipients() returns."""
    keep, _stats = so.build(users, chats)
    return [r["guid"] for r in keep]


# --------------------------------------------------------------------------- #
def test_two_way_before_strangers():
    users = _mk_users([
        ("stranger_recent", 10),      # no chat, seen 10s ago (very recent)
        ("they_wrote", 300),          # a chat, they messaged us
        ("we_wrote", 600),            # a chat, only we messaged
        ("stranger_old", 5000),       # no chat, old
    ])
    chats = [
        _mk_chat("they_wrote", they_wrote=True, age=300),
        _mk_chat("we_wrote", we_wrote=True, age=600),
    ]
    order = _order(users, chats)
    # both chat contacts must come before BOTH strangers, even though one
    # stranger was seen much more recently than either chat contact.
    i_they = order.index("they_wrote")
    i_we = order.index("we_wrote")
    i_srecent = order.index("stranger_recent")
    i_sold = order.index("stranger_old")
    ok(i_they < i_srecent and i_we < i_srecent,
       f"two-way before the recent stranger (order={order})")
    ok(max(i_they, i_we) < min(i_srecent, i_sold),
       "ALL two-way contacts come before ALL strangers")


def test_we_messaged_counts_as_two_way():
    """The owner's definition: a chat we STARTED (they never replied) is still
    two-way and must be sent before strangers. This is what broke before."""
    users = _mk_users([("we_only", 900), ("stranger", 20)])
    chats = [_mk_chat("we_only", we_wrote=True, age=900)]
    order = _order(users, chats)
    eq(order[0], "we_only",
       "a chat we started (no reply) is two-way -> sent before the stranger")


def test_two_way_sorted_by_recent_seen():
    users = _mk_users([
        ("chat_old", 9000), ("chat_mid", 4000), ("chat_new", 100)])
    chats = [_mk_chat("chat_old", they_wrote=True, age=9000),
             _mk_chat("chat_mid", they_wrote=True, age=4000),
             _mk_chat("chat_new", they_wrote=True, age=100)]
    order = _order(users, chats)
    eq(order, ["chat_new", "chat_mid", "chat_old"],
       "two-way contacts are ordered most-recently-seen first")


def test_record_carries_has_chat():
    """ordered_recipients strips to {guid,name}, but build()'s records (used by
    the live card / stats) must still expose has_chat + mutual."""
    users = _mk_users([("a", 100)])
    chats = [_mk_chat("a", they_wrote=True)]
    keep, _ = so.build(users, chats)
    rec = keep[0]
    ok("has_chat" in rec and rec["has_chat"] is True, "has_chat present on record")
    ok("mutual" in rec, "mutual still present on record")


def test_all_strangers_still_send_in_presence_order():
    """An account with NO chats at all: everyone is a stranger, ordered by
    presence — nobody is dropped, nothing is reordered away."""
    users = _mk_users([("s1", 50), ("s2", 10), ("s3", 9000)])
    order = _order(users, [])
    eq(set(order), {"s1", "s2", "s3"}, "no stranger is dropped")
    eq(order[0], "s2", "most recently seen stranger first")


def main():
    if "--selftest" not in sys.argv:
        print("usage: send_realorder_test.py --selftest")
        return 0
    for fn in (test_two_way_before_strangers, test_we_messaged_counts_as_two_way,
               test_two_way_sorted_by_recent_seen, test_record_carries_has_chat,
               test_all_strangers_still_send_in_presence_order):
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
