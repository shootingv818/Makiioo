#!/usr/bin/env python3
"""
🔁 Automation Engine — worker-primitive tests (offline, stubbed).
=================================================================

rubpy / telethon are not installed in dev (same as the project's other test
setups), so this stubs `account_conn` and `rubika_client` with just the surface
autoeng_worker uses, then drives preview/join/send/pv through controllable fake
responses. Read-only; no real network, no DB writes.

  python3 scripts/autoeng_worker_test.py --selftest
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
# Stub `account_conn`
# --------------------------------------------------------------------------- #
class _InvalidAuth(RuntimeError):
    pass


class _FakeClient(dict):
    """Behaves like a dict (for the stub helpers) but also exposes rubpy-style
    methods via attribute access (so autoeng_worker's getattr lookup works)."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            return None


_fake_client = _FakeClient(behavior={})


async def _call(phone, fn, *a, timeout=None, **k):
    if _fake_client["behavior"].get("auth_dead"):
        raise _InvalidAuth("INVALID_AUTH")
    return await fn(_fake_client)


ac = types.ModuleType("account_conn")
ac.InvalidAuthError = _InvalidAuth
ac.call = _call
sys.modules["account_conn"] = ac


# --------------------------------------------------------------------------- #
# Stub `rubika_client` (only what autoeng_worker touches)
# --------------------------------------------------------------------------- #
rc = types.ModuleType("rubika_client")


def normalize_phone(p):
    return str(p)


def _data_of(obj):
    return obj if isinstance(obj, dict) else {}


def _guid_of(obj):
    d = _data_of(obj)
    for k in ("object_guid", "user_guid", "guid", "group_guid"):
        if d.get(k):
            return d[k]
    return None


def _name_of(obj, default="-"):
    d = _data_of(obj)
    return d.get("name") or d.get("title") or d.get("group_title") or default


def chat_type(ch):
    return (_data_of(ch).get("type") or "").lower()


def chat_object_guid(ch):
    return _data_of(ch).get("object_guid")


def chat_last_message(ch):
    lm = _data_of(ch).get("last_message")
    return lm if isinstance(lm, dict) else {}


def message_author_guid(m):
    return _data_of(m).get("author_object_guid")


async def _try_call(fn, attempts):
    last = None
    for make in attempts:
        a, k = make()
        try:
            return await fn(*a, **k)
        except TypeError as e:
            last = e
            continue
    raise last or RuntimeError("no attempt")


async def join_group_by_link(client, link):
    b = client["behavior"]
    if b.get("join_error"):
        raise RuntimeError(b["join_error"])
    return {"group_guid": b.get("join_guid", "G_joined")}


def join_result_group_guid(res):
    return _data_of(res).get("group_guid")


async def send_text(client, guid, text):
    b = client["behavior"]
    if b.get("send_error"):
        raise RuntimeError(b["send_error"])
    client.setdefault("sent", []).append((guid, text))
    return True


async def leave_group(client, guid):
    return True


async def get_self_guid(client):
    return client["behavior"].get("self_guid", "ME")


async def get_chats_updates(client, state):
    return client["behavior"].get("pv_result", {"chats": [], "new_state": "100"})


def parse_chats_updates(res):
    return res.get("chats", []), res.get("new_state")


rc.normalize_phone = normalize_phone
rc._data_of = _data_of
rc._guid_of = _guid_of
rc._name_of = _name_of
rc.chat_type = chat_type
rc.chat_object_guid = chat_object_guid
rc.chat_last_message = chat_last_message
rc.message_author_guid = message_author_guid
rc._try_call = _try_call
rc.join_group_by_link = join_group_by_link
rc.join_result_group_guid = join_result_group_guid
rc.send_text = send_text
rc.leave_group = leave_group
rc.get_self_guid = get_self_guid
rc.get_chats_updates = get_chats_updates
rc.parse_chats_updates = parse_chats_updates
sys.modules["rubika_client"] = rc

import autoeng_worker as w  # noqa: E402  (after stubs)


def _reset(behavior=None, client_preview=None):
    _fake_client.clear()
    _fake_client["behavior"] = behavior or {}
    if client_preview is not None:
        _fake_client["behavior"]["_preview"] = client_preview
    # provide a group_preview_by_join_link method on the fake client
    async def _preview(link):
        pv = _fake_client["behavior"].get("_preview")
        if pv == "__raise__":
            raise RuntimeError(_fake_client["behavior"].get("preview_error", "boom"))
        return pv
    _fake_client["group_preview_by_join_link"] = _preview


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_preview_ok():
    _reset(client_preview={"group": {"group_guid": "G1", "group_title": "Shop",
                                     "count_members": 342, "join_type": "Open"}})
    r = run(w.preview_link("0912", "http://rubika.ir/joing/ABC"))
    eq(r["ok"], True, "preview ok")
    eq(r["guid"], "G1", "guid extracted")
    eq(r["members"], 342, "members extracted")
    eq(r["approval"], False, "open group -> no approval")


def test_preview_approval():
    _reset(client_preview={"group": {"group_guid": "G2", "group_title": "VIP",
                                     "count_members": 10, "join_type": "Request"}})
    r = run(w.preview_link("0912", "http://rubika.ir/joing/XYZ"))
    eq(r["approval"], True, "request group -> approval required")


def test_preview_unresolved():
    _reset(client_preview=None)
    r = run(w.preview_link("0912", "http://rubika.ir/joing/BAD"))
    eq(r["ok"], False, "unresolved -> not ok")
    eq(r["code"], w.core.LNK_BAD, "unresolved -> LNK_BAD")


def test_preview_auth_dead():
    _reset(behavior={"auth_dead": True})
    r = run(w.preview_link("0912", "http://x/joing/1"))
    eq(r["code"], w.core.ACC_DEAD, "dead session -> ACC_DEAD")


def test_join_ok():
    _reset(behavior={"join_guid": "GJ"})
    r = run(w.join_link("0912", "http://x/joing/1"))
    eq(r["ok"], True, "join ok")
    eq(r["guid"], "GJ", "join returns guid (fixes remote 'joined' gap)")


def test_join_approval():
    _reset(behavior={"join_error": "JOIN_REQUEST sent to admins"})
    r = run(w.join_link("0912", "http://x/joing/1"))
    eq(r["ok"], False, "approval join not ok")
    eq(r["code"], w.core.GRP_APPROVAL, "approval classified")


def test_send_ok_and_errors():
    _reset()
    r = run(w.send_to("0912", "G1", "hi"))
    eq(r["ok"], True, "send ok")
    eq(_fake_client.get("sent"), [("G1", "hi")], "message recorded")

    _reset(behavior={"send_error": "you are BANNED"})
    r = run(w.send_to("0912", "G1", "hi"))
    eq(r["code"], w.core.GRP_BLOCKED, "banned send -> GRP_BLOCKED")

    _reset(behavior={"send_error": "TOO_REQUESTS slow down"})
    r = run(w.send_to("0912", "G1", "hi"))
    eq(r["code"], w.core.ACC_LIMIT, "rate send -> ACC_LIMIT (not a group fault)")


def test_pv_prime_then_new():
    # first pass (no state) primes and returns nothing
    _reset(behavior={"self_guid": "ME", "pv_result": {
        "chats": [{"type": "user", "object_guid": "U1",
                   "last_message": {"author_object_guid": "U1"}, "name": "Ali"}],
        "new_state": "200"}})
    r1 = run(w.fetch_pv("0912", None))
    eq(r1["new"], [], "first pass primes, returns no senders")
    eq(r1["state"], "200", "cursor advanced")

    # second pass with state returns the incoming user, skips self-authored
    _reset(behavior={"self_guid": "ME", "pv_result": {
        "chats": [
            {"type": "user", "object_guid": "U1",
             "last_message": {"author_object_guid": "U1"}, "name": "Ali"},
            {"type": "user", "object_guid": "U2",
             "last_message": {"author_object_guid": "ME"}, "name": "mine"},
            {"type": "group", "object_guid": "GG", "name": "grp"},
        ], "new_state": "300"}})
    r2 = run(w.fetch_pv("0912", "200"))
    eq([x["guid"] for x in r2["new"]], ["U1"],
       "only the incoming user chat (skip self-authored + groups)")


def main():
    if "--selftest" not in sys.argv:
        print("usage: autoeng_worker_test.py --selftest")
        return 0
    for fn in (test_preview_ok, test_preview_approval, test_preview_unresolved,
               test_preview_auth_dead, test_join_ok, test_join_approval,
               test_send_ok_and_errors, test_pv_prime_then_new):
        fn()
    print(f"\n  ==== {_passed} passed, {_failed} failed ====\n")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
