#!/usr/bin/env python3
"""
🖥 Worker-side test — simulate the worker CONTAINER, not the master.
=====================================================================

A worker container has no telethon, no bot.py, and only the flat module path
the Dockerfile produces. This test:

  1. imports the worker-side modules with telethon/bot DELIBERATELY blocked,
     proving nothing master-only sneaks in at import time;
  2. builds the FastAPI app's endpoint table from worker_api.py source and
     checks every /autoeng/* route maps to the right autoeng_worker call;
  3. drives each worker primitive through a stubbed rubpy client, including
     the failure shapes a real worker returns (auth dead, rate limit, banned,
     approval-required), and asserts the classified reply the MASTER relies on.

  python3 scripts/worker_side_test.py --selftest
"""
import ast
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
# 1. Block master-only modules, then import the worker modules.
# --------------------------------------------------------------------------- #
class _Blocked:
    def __init__(self, name):
        self.name = name

    def find_module(self, fullname, path=None):
        return self if fullname.split(".")[0] == self.name else None

    def load_module(self, fullname):
        raise ImportError(f"{fullname} is NOT available inside a worker")


def test_worker_imports_without_master_modules():
    # telethon must never be needed by a worker module
    sys.meta_path.insert(0, _Blocked("telethon"))
    try:
        import importlib
        for mod in ("autoeng_core",):
            sys.modules.pop(mod, None)
            importlib.import_module(mod)
            ok(True, f"{mod} imports with telethon blocked")
    except ImportError as e:
        ok(False, f"worker module needs telethon: {e}")
    finally:
        sys.meta_path = [m for m in sys.meta_path
                         if not isinstance(m, _Blocked)]


# --------------------------------------------------------------------------- #
# 2. Endpoint table from source: route -> the autoeng_worker fn it calls.
# --------------------------------------------------------------------------- #
EXPECTED_ROUTES = {
    "/autoeng/preview": "preview_link",
    "/autoeng/join": "join_link",
    "/autoeng/send": "send_to",
    "/autoeng/leave": "leave",
    "/autoeng/pv": "fetch_pv",
    "/autoeng/sendfile": "send_file_to",
}


def test_route_table():
    src = open(os.path.join(_ROOT, "workers", "worker_api.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        route = None
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and str(dec.args[0].value).startswith("/autoeng/")):
                route = dec.args[0].value
        if not route:
            continue
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                if (isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "autoeng_worker"):
                    called.add(sub.func.attr)
        found[route] = sorted(called)
    for route, fn in EXPECTED_ROUTES.items():
        ok(route in found, f"{route} exists")
        eq(found.get(route), [fn], f"{route} -> autoeng_worker.{fn}")
    eq(sorted(found), sorted(EXPECTED_ROUTES), "no unexpected /autoeng route")


# --------------------------------------------------------------------------- #
# 3. Drive the primitives through a stubbed client (worker runtime behaviour).
# --------------------------------------------------------------------------- #
class _InvalidAuth(RuntimeError):
    pass


class _FakeClient(dict):
    def __getattr__(self, n):
        try:
            return self[n]
        except KeyError:
            return None


_beh = {}
_client = _FakeClient()


def _install_stubs():
    ac = types.ModuleType("account_conn")
    ac.InvalidAuthError = _InvalidAuth

    async def _call(phone, fn, *a, timeout=None, **k):
        if _beh.get("auth_dead"):
            raise _InvalidAuth("InvalidAuth({'status_det': 'INVALID_AUTH'})")
        return await fn(_client)

    ac.call = _call
    sys.modules["account_conn"] = ac

    rc = types.ModuleType("rubika_client")
    rc.normalize_phone = lambda p: str(p)
    rc._data_of = lambda o: o if isinstance(o, dict) else {}
    rc._guid_of = lambda o: (o or {}).get("group_guid") or (o or {}).get("object_guid")
    rc._name_of = lambda o, default="-": (o or {}).get("group_title") or default
    rc.chat_type = lambda c: (c or {}).get("type", "")
    rc.chat_object_guid = lambda c: (c or {}).get("object_guid")
    rc.chat_last_message = lambda c: (c or {}).get("last_message") or {}
    rc.message_author_guid = lambda m: (m or {}).get("author_object_guid")

    async def _try_call(fn, attempts):
        last = None
        for mk in attempts:
            a, k = mk()
            try:
                return await fn(*a, **k)
            except TypeError as e:
                last = e
        raise last or RuntimeError("no attempt")

    rc._try_call = _try_call

    async def _join(client, link):
        if _beh.get("join_error"):
            raise RuntimeError(_beh["join_error"])
        return {"group_guid": "GJOIN"}

    rc.join_group_by_link = _join
    rc.join_result_group_guid = lambda r: (r or {}).get("group_guid")

    async def _send(client, guid, text):
        if _beh.get("send_error"):
            raise RuntimeError(_beh["send_error"])
        client.setdefault("sent", []).append((guid, text))
        return True

    rc.send_text = _send

    async def _leave(client, guid):
        if _beh.get("leave_error"):
            raise RuntimeError(_beh["leave_error"])
        return True

    rc.leave_group = _leave
    rc.get_self_guid = lambda c: _async_val("ME")
    rc.get_chats_updates = lambda c, s: _async_val(
        _beh.get("pv") or {"chats": [], "new_state": "1"})
    rc.parse_chats_updates = lambda r: (r.get("chats", []), r.get("new_state"))
    sys.modules["rubika_client"] = rc


async def _async_val(v):
    return v


def _reset(behavior=None, preview=None):
    _beh.clear()
    _beh.update(behavior or {})
    _client.clear()

    async def _preview(link):
        if _beh.get("preview_raise"):
            raise RuntimeError(_beh["preview_raise"])
        return preview

    _client["group_preview_by_join_link"] = _preview


def run(c):
    return asyncio.run(c)


def test_primitives():
    _install_stubs()
    import importlib
    sys.modules.pop("autoeng_worker", None)
    w = importlib.import_module("autoeng_worker")
    core = w.core

    # preview: healthy open group
    _reset(preview={"group": {"group_guid": "G1", "group_title": "Shop",
                              "count_members": 342, "join_type": "Open"}})
    r = run(w.preview_link("0912", "http://rubika.ir/joing/A"))
    ok(r["ok"] and r["guid"] == "G1" and r["members"] == 342,
       f"preview healthy group {r}")
    eq(r["approval"], False, "open group -> approval False")

    # preview: approval-required
    _reset(preview={"group": {"group_guid": "G2", "join_type": "Request"}})
    eq(run(w.preview_link("0912", "l"))["approval"], True,
       "request group -> approval True")

    # preview: dead session
    _reset(behavior={"auth_dead": True}, preview=None)
    eq(run(w.preview_link("0912", "l"))["code"], core.ACC_DEAD,
       "auth dead -> ACC_SESSION")

    # join: ok / approval / rate-limit
    _reset()
    r = run(w.join_link("0912", "l"))
    ok(r["ok"] and r["guid"] == "GJOIN", "join returns the real guid")
    _reset(behavior={"join_error": "JOIN_REQUEST pending"})
    eq(run(w.join_link("0912", "l"))["code"], core.GRP_APPROVAL,
       "join approval classified")
    _reset(behavior={"join_error": "TOO_REQUESTS"})
    eq(run(w.join_link("0912", "l"))["code"], core.ACC_LIMIT,
       "join rate-limit classified")

    # send: ok / banned / limit / dead
    _reset()
    ok(run(w.send_to("0912", "G1", "hi"))["ok"], "send ok")
    eq(_client.get("sent"), [("G1", "hi")], "message really passed through")
    _reset(behavior={"send_error": "ACCESS_DENIED you are muted"})
    eq(run(w.send_to("0912", "G1", "x"))["code"], core.GRP_BLOCKED,
       "muted -> GRP_BLOCKED")
    _reset(behavior={"send_error": "TOO_REQUESTS"})
    eq(run(w.send_to("0912", "G1", "x"))["code"], core.ACC_LIMIT,
       "rate-limit -> ACC_LIMIT (never a group fault)")
    _reset(behavior={"auth_dead": True})
    eq(run(w.send_to("0912", "G1", "x"))["code"], core.ACC_DEAD,
       "dead session on send")

    # leave
    _reset()
    ok(run(w.leave("0912", "G1"))["ok"], "leave ok")
    _reset(behavior={"leave_error": "boom"})
    ok(not run(w.leave("0912", "G1"))["ok"], "leave error surfaced")

    # pv: first poll primes, second returns only incoming user chats
    _reset(behavior={"pv": {"chats": [
        {"type": "user", "object_guid": "U1",
         "last_message": {"author_object_guid": "U1"}},
        {"type": "user", "object_guid": "U2",
         "last_message": {"author_object_guid": "ME"}},
        {"type": "group", "object_guid": "G9"},
    ], "new_state": "55"}})
    first = run(w.fetch_pv("0912", None))
    eq(first["new"], [], "first PV poll only primes the cursor")
    eq(first["state"], "55", "cursor advanced")
    second = run(w.fetch_pv("0912", "55"))
    eq([x["guid"] for x in second["new"]], ["U1"],
       "only incoming user chats (self-authored + groups skipped)")


def main():
    if "--selftest" not in sys.argv:
        print("usage: worker_side_test.py --selftest")
        return 0
    for fn in (test_worker_imports_without_master_modules, test_route_table,
               test_primitives):
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
