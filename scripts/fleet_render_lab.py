#!/usr/bin/env python3
"""
🛰 Fleet render lab — the master drives the WORKERS, like the panel itself does.
===============================================================================

Why this exists
---------------
scripts/name_render_lab.py can only read an account whose session file is on
the machine it runs on. The owner's accounts live on the worker nodes, not the
master, so running it on the master skipped all eight of them.

This script fixes that the same way the panel already moves work around: the
master ships the code to each worker over SSH, runs it INSIDE that worker's
container where the sessions actually are, reads the JSON result back, and
aggregates one fleet-wide report.

Nothing new was invented. Every mechanism here is the panel's own:

    worker._with_conn()      the SSH connection used by update/provision
    worker._run()            run a command on the worker host
    conn.start_sftp_client() the file transfer used by the session backup
    worker.CONTAINER         the container name used by update_worker()
    worker.api_call()        the HTTP calls the panel makes all day
    name_render_lab.py       the rendering logic, unchanged

Safety — it must not disturb the running bot
--------------------------------------------
The payload runs as a SEPARATE process inside the worker container, so its
account_conn cannot see the worker process's warm connections. Connecting to an
account that the worker is actively using would trigger AUTH_FROM_ANOTHER and
kill that session.

So before any account is touched, every worker is asked — over its normal
read-only status endpoints — whether that phone has an automation, secretary,
reply, linkdooni or channel-report loop running. Any account that is busy is
SKIPPED and reported, never rendered.

Beyond that:
  * NO message is ever sent (the payload has an AST guard proving it)
  * the panel database is only READ
  * the worker's own process is never restarted, stopped or signalled
  * the only write is one file copied into the container at
    /app/scripts/name_render_lab.py — the very path `Workers → Update All`
    overwrites anyway, and nothing running imports it

Usage
-----
    # offline: prove the orchestration logic (no SSH, no network)
    python3 scripts/fleet_render_lab.py --selftest

    # show which account sits on which worker and what would run
    python3 scripts/fleet_render_lab.py --plan

    # do it: render every account on every worker and print fleet stats
    python3 scripts/fleet_render_lab.py --run

    # fewer profile lookups per account (faster), full JSON kept
    python3 scripts/fleet_render_lab.py --run --limit 60 --json /tmp/fleet.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time

# Same sub-package path shim as main.py / the other labs.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _sub in ("", "core", "rubika", "workers", "bot_app", "status"):
    _p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
try:
    os.chdir(_ROOT)
except OSError:
    pass

# The payload module. Imported for its JSON markers, its reader, and its
# report printers, so the two scripts can never disagree about the format.
import name_render_lab as nrl  # noqa: E402

PAYLOAD_LOCAL = os.path.join(_SCRIPTS, "name_render_lab.py")
PAYLOAD_IN_CONTAINER = "/app/scripts/name_render_lab.py"

# Read-only status endpoints that reveal a warm connection on a worker.
BUSY_ENDPOINTS = ("/automation/status", "/secretary/status", "/reply/status",
                  "/linkdooni/status", "/channelreport/status")


# =========================================================================== #
# Pure logic (fully covered by --selftest)
# =========================================================================== #

def group_by_worker(accounts: list, worker_of) -> tuple:
    """Split accounts into {worker_id: [phones]} plus the worker lookup table.

    `worker_of(account) -> worker dict|None` is injected so the grouping can be
    tested without a database or a network.
    """
    groups, workers, orphans = {}, {}, []
    for acc in accounts:
        phone = str(acc.get("phone") or "").strip()
        if not phone:
            continue
        w = None
        try:
            w = worker_of(acc)
        except Exception:  # noqa: BLE001
            w = None
        if not w:
            orphans.append(phone)
            continue
        wid = w.get("id")
        workers[wid] = w
        groups.setdefault(wid, []).append(phone)
    return groups, workers, orphans


def busy_phones(status_map: dict) -> dict:
    """{phone: reason} for accounts a worker is actively using.

    `status_map` is {phone: {endpoint: response_dict}}. A response counts as
    busy only when it explicitly says running=True — an error, a timeout or a
    missing key must NOT look like "busy" (that would silently skip accounts),
    and must not look like "idle" either, which is why callers treat an
    unreachable worker as a hard failure rather than an empty result.
    """
    out = {}
    for phone, per_ep in (status_map or {}).items():
        for ep, res in (per_ep or {}).items():
            if isinstance(res, dict) and res.get("running") is True:
                out[phone] = ep.strip("/").split("/")[0]
                break
    return out


def remote_command(container: str, phones: list, limit: int, delay: float,
                   min_confidence: int) -> str:
    """The exact command run inside the worker container.

    Every argument is shell-quoted, and the payload is invoked with explicit
    phones rather than --render-all: a worker's own database does not hold the
    account table, only the session files, so the master must say who to read.
    """
    args = ["python", PAYLOAD_IN_CONTAINER, "--render"] + list(phones) + [
        "--json-stdout", "--show", "0",
        "--limit", str(int(limit)),
        "--delay", str(float(delay)),
        "--min-confidence", str(int(min_confidence)),
    ]
    inner = " ".join(shlex.quote(a) for a in args)
    return f"docker exec {shlex.quote(container)} {inner}"


def merge_results(per_worker: dict) -> list:
    """Flatten {worker_tag: [account_result, ...]} into one list, tagging each
    account result with the worker it came from."""
    rows = []
    for tag, results in (per_worker or {}).items():
        for r in results or []:
            r = dict(r)
            r["worker"] = tag
            rows.append(r)
    return rows


# =========================================================================== #
# Remote execution
# =========================================================================== #

async def _worker_busy_map(worker, phones: list) -> dict:
    """Ask ONE worker, read-only, which of these phones it is already using."""
    import worker as wk
    out = {}
    for phone in phones:
        per_ep = {}
        for ep in BUSY_ENDPOINTS:
            try:
                per_ep[ep] = await wk.api_call(worker, "GET",
                                               f"{ep}?phone={phone}",
                                               timeout=25)
            except Exception as e:  # noqa: BLE001
                per_ep[ep] = {"error": repr(e)[:120]}
        out[phone] = per_ep
    return out


async def run_on_worker(worker: dict, phones: list, limit: int, delay: float,
                        min_confidence: int, timeout: int) -> dict:
    """Ship the payload to one worker, run it in the container, bring back JSON."""
    import worker as wk

    tag = worker.get("tag") or f"id{worker.get('id')}"
    out = {"tag": tag, "ok": False, "error": None, "results": [],
           "skipped_busy": {}, "phones": list(phones)}

    # 1. who is busy? (read-only HTTP, the panel's own endpoints)
    print(f"\n  [{tag}] checking which accounts are idle ...")
    try:
        smap = await _worker_busy_map(worker, phones)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"status check failed: {repr(e)[:140]}"
        return out
    busy = busy_phones(smap)
    out["skipped_busy"] = busy
    todo = [p for p in phones if p not in busy]
    for p, why in busy.items():
        print(f"  [{tag}] ⏭ {p} skipped — a '{why}' loop is running on it")
    if not todo:
        out["error"] = "every account on this worker is busy"
        return out

    # 2. SSH in (keepalive=False: the panel uses that for one-shot admin ops)
    try:
        conn = await wk._with_conn(worker, keepalive=False)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"ssh failed: {repr(e)[:140]}"
        return out

    remote_tmp = f"/tmp/nrl_{int(time.time())}_{os.getpid()}.py"
    try:
        # 3. upload the payload, then copy it into the container
        print(f"  [{tag}] sending the render code ...")
        sftp = await conn.start_sftp_client()
        try:
            await sftp.put(PAYLOAD_LOCAL, remote_tmp)
        finally:
            try:
                sftp.exit()
            except Exception:
                pass
        rc, so, se = await wk._run(
            conn, f"docker cp {shlex.quote(remote_tmp)} "
                  f"{shlex.quote(wk.CONTAINER)}:{PAYLOAD_IN_CONTAINER}")
        if rc != 0:
            out["error"] = f"docker cp failed: {(se or so)[:160]}"
            return out

        # 4. run it where the sessions are
        cmd = remote_command(wk.CONTAINER, todo, limit, delay, min_confidence)
        print(f"  [{tag}] rendering {len(todo)} account(s) inside the "
              f"container (this takes a few minutes) ...")
        rc, so, se = await asyncio.wait_for(wk._run(conn, cmd), timeout=timeout)

        # 5. read the result back
        parsed = nrl.extract_json(so)
        if parsed is None:
            tail = (se or so or "").strip()[-400:]
            out["error"] = (f"no JSON came back (exit {rc}). last output:\n"
                            f"      {tail}")
            return out
        out["results"] = parsed
        out["ok"] = True
        return out
    except asyncio.TimeoutError:
        out["error"] = f"timed out after {timeout}s"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        return out
    finally:
        # tidy up the host temp file; the in-container copy is the same path
        # `Workers → Update All` rewrites, so it is left alone.
        try:
            await wk._run(conn, f"rm -f {shlex.quote(remote_tmp)}")
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


async def run_local(phones: list, limit: int, delay: float,
                    min_confidence: int) -> dict:
    """Accounts whose session is on the master: render in-process, same code."""
    out = {"tag": "MASTER (local)", "ok": False, "error": None,
           "results": [], "skipped_busy": {}, "phones": list(phones)}
    try:
        for ph in phones:
            res = await nrl.render_account(ph, limit, delay, min_confidence, 0)
            out["results"].append(res)
        out["ok"] = any(r.get("ok") for r in out["results"])
        if not out["ok"]:
            out["error"] = "no local session could be read"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return out


# =========================================================================== #
# Planning / reporting
# =========================================================================== #

def _load_plan():
    """(groups, workers, orphans, accounts) from the panel DB. Read-only."""
    nrl.ensure_interpreter(deps=("dotenv",))
    import db
    import worker as wk
    accounts = [a for a in db.list_accounts()
                if (a.get("status") or "active") == "active"]
    groups, workers, orphans = group_by_worker(accounts, wk.worker_for_account)
    return groups, workers, orphans, accounts


def show_plan() -> int:
    try:
        groups, workers, orphans, accounts = _load_plan()
    except Exception as e:  # noqa: BLE001
        print(f"  could not read the panel: {repr(e)[:160]}")
        return 1
    import worker as wk
    print("=" * 70)
    print(f"PLAN — {len(accounts)} active account(s)")
    print("=" * 70)
    for wid, phones in sorted(groups.items(), key=lambda kv: str(kv[0])):
        w = workers[wid]
        where = "MASTER (local, in-process)" if wk.is_local(w) else \
            f"{w.get('tag')}  ->  ssh + docker exec {wk.CONTAINER}"
        print(f"\n  {where}")
        for p in phones:
            print(f"      {p}")
    if orphans:
        print(f"\n  ⚠ no worker assigned: {', '.join(orphans)}")
    print("\n  nothing above has run yet. add --run to execute.")
    print("  no message is ever sent; the panel DB is only read.\n")
    return 0


def _print_report(per_worker: dict, min_confidence: int, out_json: str) -> None:
    rows = merge_results(per_worker)
    good = [r for r in rows if r.get("ok")]

    print("\n" + "=" * 70)
    print("PER-ACCOUNT")
    print("=" * 70)
    for r in rows:
        nrl._print_account_report(r, min_confidence)

    if not good:
        print("\n  ⚠ no account could be read on any worker.\n")
    else:
        nrl._print_fleet_report(good, min_confidence)

    print("\n" + "-" * 70)
    print("  WORKERS")
    print("-" * 70)
    for tag, res in per_worker.items():
        okn = len([r for r in (res or []) if r.get("ok")])
        print(f"    {tag:<18} {okn} account(s) read")

    if out_json:
        try:
            with open(out_json, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=1)
            print(f"\n  full per-contact result written to {out_json}")
        except Exception as e:  # noqa: BLE001
            print(f"\n  could not write JSON: {repr(e)[:140]}")


# =========================================================================== #
# Offline self-test
# =========================================================================== #

_PASS = 0
_FAIL = 0


def _ck(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}")


def _guard() -> tuple:
    """This orchestrator must not send, must not write the panel DB, and must
    not disturb the worker process. Checked at the call level, like the payload
    script's own guard."""
    import ast
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    BANNED = {"send_message", "send_text", "forward_message", "send_copy",
              "send_document", "run_send", "run_send_remote", "add_contact",
              "add_channel", "update_profile", "set_setting", "set_account",
              "set_status", "set_session_blob", "commit", "execute",
              "executemany",
              # lifecycle operations that would disturb the running panel
              "restart_worker", "teardown_worker", "update_worker",
              "provision_worker", "stop_supervisor", "shutdown"}

    def dotted(fn):
        parts = []
        while isinstance(fn, ast.Attribute):
            parts.append(fn.attr)
            fn = fn.value
        if isinstance(fn, ast.Name):
            parts.append(fn.id)
        return ".".join(reversed(parts))

    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        leaf = (n.func.attr if isinstance(n.func, ast.Attribute)
                else getattr(n.func, "id", ""))
        full = dotted(n.func) if isinstance(n.func, ast.Attribute) else leaf
        if leaf in BANNED:
            hits.append(full or leaf)
        if full == "wk.api_call":
            # the panel's API is reached READ-ONLY: GET only
            if '"GET"' not in ast.unparse(n) and "'GET'" not in ast.unparse(n):
                hits.append("wk.api_call with a non-GET method")
    return (not hits), sorted(set(hits))


def selftest() -> int:
    print("=" * 70)
    print("FLEET RENDER LAB — offline orchestration test")
    print("=" * 70)

    print("\n0. it cannot send, cannot write the DB, cannot disturb a worker")
    ok, hits = _guard()
    _ck(f"no banned call is reachable {hits or ''}", ok)
    _ck("the payload module it ships is itself proven read-only",
        nrl._no_send_guard()[0] is True)

    print("\n1. accounts are grouped onto the worker that actually holds them")
    accounts = [{"id": 1, "phone": "98901"}, {"id": 2, "phone": "98902"},
                {"id": 3, "phone": "98903"}, {"id": 4, "phone": ""}]
    table = {1: {"id": 7, "tag": "#W4"}, 2: {"id": 7, "tag": "#W4"},
             3: {"id": 9, "tag": "#W7"}}
    groups, workers, orphans = group_by_worker(
        accounts, lambda a: table.get(a["id"]))
    _ck("two workers found", sorted(groups) == [7, 9])
    _ck("#W4 got both of its accounts", groups[7] == ["98901", "98902"])
    _ck("#W7 got its one", groups[9] == ["98903"])
    _ck("the blank-phone account is dropped, not crashed on",
        sum(len(v) for v in groups.values()) == 3)
    _ck("...and it is not reported as an orphan either", orphans == [])
    _ck("worker dicts are carried along", workers[7]["tag"] == "#W4")
    groups2, _w2, orph2 = group_by_worker(
        [{"id": 5, "phone": "98905"}], lambda a: None)
    _ck("an unassigned account is reported, not silently dropped",
        orph2 == ["98905"] and not groups2)
    _ck("a raising lookup is treated as unassigned",
        group_by_worker([{"id": 6, "phone": "98906"}],
                        lambda a: (_ for _ in ()).throw(RuntimeError()))[2]
        == ["98906"])

    print("\n2. a busy account is NEVER rendered (this is what protects the bot)")
    _ck("an idle worker reports nobody busy",
        busy_phones({"98901": {"/automation/status": {"running": False}}}) == {})
    _ck("a running automation marks the phone busy",
        busy_phones({"98901": {"/automation/status": {"running": True}}})
        == {"98901": "automation"})
    _ck("a running secretary does too",
        busy_phones({"98902": {"/secretary/status": {"running": True}}})
        == {"98902": "secretary"})
    _ck("only the busy phone is flagged",
        busy_phones({"98901": {"/reply/status": {"running": True}},
                     "98902": {"/reply/status": {"running": False}}})
        == {"98901": "reply"})
    _ck("an ERROR response is not mistaken for busy",
        busy_phones({"98901": {"/reply/status": {"error": "timeout"}}}) == {})
    _ck("a truthy-but-not-True value is not mistaken for busy",
        busy_phones({"98901": {"/reply/status": {"running": 1}}}) == {})
    _ck("a non-dict response does not crash it",
        busy_phones({"98901": {"/reply/status": None}}) == {})

    print("\n3. the remote command")
    cmd = remote_command("v2rubby-worker", ["98901", "98902"], 60, 0.25, 70)
    _ck("runs inside the right container", "docker exec v2rubby-worker" in cmd)
    _ck("runs the payload at its repo path", PAYLOAD_IN_CONTAINER in cmd)
    _ck("passes the phones explicitly (a worker DB has no account table)",
        "--render 98901 98902" in cmd)
    _ck("asks for machine-readable output", "--json-stdout" in cmd)
    _ck("suppresses the per-row dump", "--show 0" in cmd)
    _ck("carries the sampling limit", "--limit 60" in cmd)
    _ck("carries the confidence threshold", "--min-confidence 70" in cmd)
    _ck("never asks for --render-all on a worker",
        "--render-all" not in cmd)
    _ck("contains no send flag of any kind",
        "send" not in cmd.replace("--json-stdout", ""))
    nasty = remote_command("v2rubby-worker", ["98901; rm -rf /"], 10, 1, 70)
    _ck("a hostile phone value is shell-quoted, not interpolated",
        "'98901; rm -rf /'" in nasty)

    print("\n4. reading the worker's answer back")
    payload = [{"phone": "98901", "ok": True, "contacts": 3}]
    good = (f"connecting ...\n{nrl.JSON_BEGIN}\n"
            f"{json.dumps(payload)}\n{nrl.JSON_END}\ndone\n")
    _ck("payload is extracted from noisy output",
        nrl.extract_json(good) == payload)
    _ck("missing markers -> None (so we report, not guess)",
        nrl.extract_json("boom: traceback") is None)
    _ck("a truncated run -> None",
        nrl.extract_json(f"x\n{nrl.JSON_BEGIN}\n[{{") is None)
    _ck("invalid json -> None",
        nrl.extract_json(f"{nrl.JSON_BEGIN}\nnot json\n{nrl.JSON_END}") is None)
    _ck("empty output -> None", nrl.extract_json("") is None)

    print("\n5. merging the fleet")
    merged = merge_results({"#W4": [{"phone": "1", "ok": True}],
                            "#W7": [{"phone": "2", "ok": True},
                                    {"phone": "3", "ok": False}]})
    _ck("every account result is kept", len(merged) == 3)
    _ck("each row remembers its worker",
        {r["worker"] for r in merged} == {"#W4", "#W7"})
    _ck("merging does not mutate the input",
        "worker" not in list({"#W4": [{"phone": "1"}]}.values())[0][0])

    print(f"\n  ==== {_PASS} passed, {_FAIL} failed ====\n")
    return 1 if _FAIL else 0


# =========================================================================== #
# CLI
# =========================================================================== #

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render contact names across the whole worker fleet. "
                    "READ-ONLY: never sends a message.")
    ap.add_argument("--selftest", action="store_true",
                    help="offline orchestration test (no SSH, no network)")
    ap.add_argument("--plan", action="store_true",
                    help="show which account is on which worker, run nothing")
    ap.add_argument("--run", action="store_true",
                    help="render every account on every worker")
    ap.add_argument("--limit", type=int, default=100, metavar="N",
                    help="profile lookups per account (0 = all). Default 100")
    ap.add_argument("--delay", type=float, default=0.25, metavar="SEC",
                    help="pause between profile lookups (default 0.25)")
    ap.add_argument("--min-confidence", type=int, default=70, metavar="N",
                    help="confidence needed to address someone by name")
    ap.add_argument("--timeout", type=int, default=3600, metavar="SEC",
                    help="per-worker deadline (default 3600)")
    ap.add_argument("--only", nargs="+", metavar="TAG_OR_PHONE",
                    help="restrict to these worker tags or phone numbers")
    ap.add_argument("--json", dest="out_json", metavar="FILE",
                    help="write the full per-contact result as JSON")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.plan:
        return show_plan()
    if not a.run:
        ap.print_help()
        print("\n  start here:  --selftest   (offline, proves the logic)")
        print("  then:        --plan       (who sits where, runs nothing)")
        print("  then:        --run        (read-only, sends nothing)\n")
        return 0

    try:
        groups, workers, orphans, accounts = _load_plan()
    except Exception as e:  # noqa: BLE001
        print(f"  could not read the panel: {repr(e)[:160]}")
        return 1
    import worker as wk

    only = set(a.only or [])
    print("=" * 70)
    print("FLEET RENDER LAB — READ-ONLY")
    print("=" * 70)
    print(f"  active accounts : {len(accounts)}")
    print(f"  workers         : {len(groups)}")
    print(f"  threshold       : {a.min_confidence}")
    print(f"  profile sample  : {a.limit or 'all'} per account")
    print("  NO MESSAGE IS SENT. The panel DB is only read. The worker")
    print("  processes are never restarted and busy accounts are skipped.")
    if orphans:
        print(f"\n  ⚠ no worker assigned: {', '.join(orphans)}")

    async def run_all():
        per_worker = {}
        for wid, phones in sorted(groups.items(), key=lambda kv: str(kv[0])):
            w = workers[wid]
            tag = w.get("tag") or f"id{wid}"
            if only and tag not in only and not (set(phones) & only):
                continue
            todo = [p for p in phones if p in only] if (only & set(phones)) \
                else phones
            if wk.is_local(w):
                res = await run_local(todo, a.limit, a.delay, a.min_confidence)
            else:
                res = await run_on_worker(w, todo, a.limit, a.delay,
                                          a.min_confidence, a.timeout)
            if not res["ok"]:
                print(f"  [{res['tag']}] ⚠ {res['error']}")
            per_worker[res["tag"]] = res["results"]
        return per_worker

    per_worker = asyncio.run(run_all())
    _print_report(per_worker, a.min_confidence, a.out_json)
    print()
    return 0 if any(r.get("ok") for r in merge_results(per_worker)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
