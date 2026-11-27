#!/usr/bin/env python3
"""
🔌 Wiring test — does every host attribute my modules call actually EXIST?
==========================================================================

The isolated modules (autoeng*, send_*, auth_diag, curiosity) receive the
panel module as `bot` and call `bot.<something>`. A typo or a renamed helper
would only explode at runtime, in production, mid-send. This test resolves
every `bot.<attr>` / `self.bot.<attr>` reference statically against bot.py's
real symbol table — the same "prove it with symtable, not with hope" idiom as
the project's _stubs/test_scope.py.

It also checks:
  * the new worker endpoints are registered and their models exist
  * worker-side modules import NOTHING master-only (no telethon/bot import)
  * every module compiles and its intra-project imports resolve on disk

  python3 scripts/autoeng_wiring_test.py --selftest
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(_ROOT, "bot_app", "bot.py")
WAPI = os.path.join(_ROOT, "workers", "worker_api.py")

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


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _top_level_names(path):
    """Every module-level name bot.py exposes (defs, classes, assignments,
    imports) — i.e. what `import bot as _self` can reach."""
    tree = ast.parse(_src(path))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name):
                            names.add(e.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # names defined inside a top-level if/try still land in the module
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add((a.asname or a.name).split(".")[0])
    return names


def _host_attr_refs(path, host_vars):
    """Collect <hostvar>.<attr> references, where hostvar is a name we know
    holds the panel module (parameter `bot`, or self.bot in Executor)."""
    tree = ast.parse(_src(path))
    refs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        v = node.value
        # bot.<attr>
        if isinstance(v, ast.Name) and v.id in host_vars:
            refs.add(node.attr)
        # self.bot.<attr>
        if (isinstance(v, ast.Attribute) and v.attr == "bot"
                and isinstance(v.value, ast.Name) and v.value.id == "self"):
            refs.add(node.attr)
    return refs


# attributes we attach to bot at runtime (not present statically in bot.py)
_RUNTIME_ATTACHED = {"autoeng_render_live"}
# names resolved via a nested attribute chain (bot.db.x, bot.config.X) — the
# root is what must exist on bot; the leaf belongs to that module.
_CHAINED_ROOTS = {"db", "config", "worker", "events", "bot", "Button"}


def test_host_attributes_exist():
    bot_names = _top_level_names(BOT)
    ok(len(bot_names) > 200, f"parsed bot.py symbols ({len(bot_names)})")

    targets = {
        "rubika/autoeng_ui.py": {"bot"},
        "rubika/autoeng.py": {"bot"},
        "rubika/send_rotate.py": {"bot"},
        "rubika/caption_file.py": {"bot"},
    }
    for rel, hostvars in targets.items():
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            continue
        refs = _host_attr_refs(path, hostvars)
        ok(len(refs) > 0, f"{rel}: found host references")
        missing = sorted(r for r in refs
                         if r not in bot_names
                         and r not in _RUNTIME_ATTACHED
                         and not r.startswith("__"))
        eq(missing, [], f"{rel}: every bot.<attr> exists in bot.py")


def test_runtime_attached_is_really_attached():
    """bot.autoeng_render_live is set by autoeng_ui.register(); prove the
    assignment exists, otherwise the live card would silently never render."""
    src = _src(os.path.join(_ROOT, "rubika", "autoeng_ui.py"))
    ok("bot.autoeng_render_live = autoeng_render_live" in src,
       "autoeng_ui attaches the live renderer onto bot")
    botsrc = _src(BOT)
    ok("autoeng_ui.register(" in botsrc, "bot.py registers autoeng_ui")
    ok("_aeui.restore(" in botsrc or "autoeng_ui.restore(" in botsrc,
       "bot.py restores the engine on boot")
    ok("autoeng_ui.show_home(" in botsrc,
       "the Automation button opens the new engine")


def test_worker_endpoints_registered():
    src = _src(WAPI)
    for ep in ("/autoeng/preview", "/autoeng/join", "/autoeng/send",
               "/autoeng/leave", "/autoeng/pv", "/autoeng/sendfile"):
        ok(f'"{ep}"' in src, f"worker endpoint {ep} registered")
    for model in ("AutoEngLinkIn", "AutoEngSendIn", "AutoEngPvIn",
                  "AutoEngFileIn"):
        ok(f"class {model}(BaseModel)" in src, f"model {model} defined")
    # every endpoint must authenticate (the project's own rule)
    tree = ast.parse(src)
    unauth = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("autoeng_"):
            continue
        body_src = ast.dump(node)
        if "_auth" not in body_src:
            unauth.append(node.name)
    eq(unauth, [], "every /autoeng/* handler calls _auth()")
    # pydantic 2.6.4 trap: no `x: bool = None` (project's documented 422 bug)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("AutoEng"):
            for st in node.body:
                if (isinstance(st, ast.AnnAssign) and st.value is not None
                        and isinstance(st.value, ast.Constant)
                        and st.value.value is None):
                    bad.append(f"{node.name}.{getattr(st.target, 'id', '?')}")
    eq(bad, [], "no `field: T = None` in the new models (pydantic 422 trap)")


def test_worker_modules_are_worker_safe():
    """A worker container has no telethon and no bot.py context: the modules it
    imports must not pull them in at module level."""
    forbidden = {"telethon", "bot"}
    for rel in ("rubika/autoeng_worker.py", "rubika/autoeng_core.py",
                "rubika/caption_file.py", "rubika/copy_send.py",
                "rubika/send_order.py"):
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            continue
        tree = ast.parse(_src(path))
        hits = []
        for node in tree.body:          # module level ONLY (lazy imports ok)
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in forbidden:
                        hits.append(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden:
                    hits.append(node.module)
        eq(hits, [], f"{rel}: no master-only import at module level")


def test_intra_project_imports_resolve():
    """Every `import X` inside my modules must exist as a file in the project
    (the flat-module layout the worker/master both rely on)."""
    search_dirs = ["", "core", "rubika", "workers", "bot_app", "status"]
    stdlib_ok = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
    third_party = {"telethon", "rubpy", "fastapi", "pydantic", "uvicorn",
                   "httpx", "asyncssh", "cryptography", "reportlab", "PIL",
                   "dotenv", "starlette"}
    mine = ["rubika/autoeng.py", "rubika/autoeng_core.py", "rubika/autoeng_db.py",
            "rubika/autoeng_log.py", "rubika/autoeng_ui.py",
            "rubika/autoeng_worker.py", "rubika/send_phase.py",
            "rubika/send_live.py", "rubika/send_rotate.py",
            "rubika/auth_diag.py", "rubika/curiosity.py"]
    for rel in mine:
        path = os.path.join(_ROOT, rel)
        tree = ast.parse(_src(path))
        unresolved = []
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in stdlib_ok or n in third_party or n == "__future__":
                    continue
                found = any(os.path.exists(os.path.join(_ROOT, d, n + ".py"))
                            or os.path.isdir(os.path.join(_ROOT, d, n))
                            for d in search_dirs)
                if not found:
                    unresolved.append(n)
        eq(sorted(set(unresolved)), [], f"{rel}: all imports resolve on disk")


def test_bulk_delete_reuses_single_delete():
    src = _src(BOT)
    ok("def _shot_accounts()" in src, "shot-account collector exists")
    ok("acc_delshot_yes" in src, "two-step confirmation handler exists")
    # must use the SAME db.delete_account the single delete uses
    ok(src.count("db.delete_account(") >= 2,
       "bulk delete reuses db.delete_account (same call as single delete)")


def test_rotation_reuses_project_chain():
    src = _src(os.path.join(_ROOT, "rubika", "send_rotate.py"))
    for needed in ("worker_transfer.pick_worker_for_transfer",
                   "db.get_session_blob", "_session_transfer_to_worker",
                   "db.set_account_worker", "worker_transfer.add_tried"):
        ok(needed in src, f"rotation calls the project's own {needed}")
    # and must NOT re-implement the import itself
    ok("/session/import" not in src,
       "rotation does NOT re-implement /session/import (delegates instead)")


def main():
    if "--selftest" not in sys.argv:
        print("usage: autoeng_wiring_test.py --selftest")
        return 0
    for fn in (test_host_attributes_exist, test_runtime_attached_is_really_attached,
               test_worker_endpoints_registered, test_worker_modules_are_worker_safe,
               test_intra_project_imports_resolve,
               test_bulk_delete_reuses_single_delete,
               test_rotation_reuses_project_chain):
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
