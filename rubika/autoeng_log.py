"""
🔁 Automation Engine — live-log renderer (PURE, no I/O).
========================================================

render_live(state) -> list[str] of 1..N message bodies (each <= TG_LIMIT).
The engine edits message #0 in place every 30s; if the fleet is large enough
to overflow one Telegram message the overflow spills into pinned message #1,#2…

Card style matches bot.panel_card: `| ⚙ - #tag`, 31-dash dividers, `--| head`.
Keeping it here (not in bot.py) makes every byte of the layout unit-testable.

Status icons (agreed with the owner):
  🔍 scanner   🟢 sending   ⏳ joining   ⏸ waiting its send turn
  🟡 limited/suspect (still working)   🔴 quarantined/dead   ⚫ worker down
  💤 resting (reserve full)
"""
from __future__ import annotations

TG_LIMIT = 4096
LINE = "-------------------------------"

IC_SCAN = "🔍"
IC_SEND = "🟢"
IC_JOIN = "⏳"
IC_WAIT = "⏸"
IC_LIMIT = "🟡"
IC_DEAD = "🔴"
IC_WDOWN = "⚫"
IC_REST = "💤"


def _bar(done: int, total: int, width: int = 10) -> str:
    total = max(0, int(total))
    done = max(0, min(int(done), total if total else 0))
    if total <= 0:
        filled = 0
    else:
        filled = int(round(width * done / total))
        filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _fmt_elapsed(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _scan_line(a: dict) -> str:
    tag = a.get("worker") or "local"
    icon = a.get("icon") or IC_SCAN
    if a.get("icon") in (IC_DEAD, IC_WDOWN):
        label = a.get("note") or ("QUARANTINED" if icon == IC_DEAD else "WORKER DOWN")
        return f"• #{a['tag']} {icon} {label}"
    if a.get("resting"):
        return f"• #{a['tag']} {IC_REST} {tag} · RESTING"
    return (f"• #{a['tag']} {icon} {tag} · "
            f"{a.get('checked', 0)}/{a.get('assigned', 0)} · ok {a.get('ok', 0)}")


def _send_line(a: dict) -> str:
    icon = a.get("icon") or IC_SEND
    gp = f"G{a.get('groups', 0)}/{a.get('groups_target', 0)}"
    sd = f"S{a.get('sent', 0)}"
    pv = f"PV {a.get('pv_in', 0)}/{a.get('pv_out', 0)}"
    extra = ""
    if a.get("pv_queue"):
        pv += f" q{a['pv_queue']}"
    if a.get("note"):
        extra = f" · {a['note']}"
    return f"• #{a['tag']} {icon} {gp} · {sd} · {pv}{extra}"


def render_live(state: dict) -> list:
    """Build the pinned live message(s) from an engine snapshot.

    state = {
      "running": bool, "elapsed": int(sec), "clock": "14:32:10",
      "scan": {"extracted": n, "checked": n, "healthy": n, "dead": n,
               "approval": n, "error": n},
      "saved": n, "saved_max": 30,
      "totals": {"sent": n, "fail": n, "pv_in": n, "pv_out": n},
      "scanners": [ {tag, worker, icon, checked, assigned, ok, resting, note} ],
      "senders":  [ {tag, worker, icon, groups, groups_target, sent, fail,
                     pv_in, pv_out, pv_queue, note} ],
    }
    """
    scan = state.get("scan", {}) or {}
    tot = state.get("totals", {}) or {}
    head = f"| ⚙ - #automation"
    n_scan = len(state.get("scanners", []))
    n_send = len(state.get("senders", []))
    status = "LIVE" if state.get("running") else "STOPPED"

    top = [
        head,
        LINE,
        f"--| {status} · {_fmt_elapsed(state.get('elapsed', 0))} · {state.get('clock', '')}",
        f"• Scan  {_bar(scan.get('checked', 0), scan.get('extracted', 0))} "
        f"{scan.get('checked', 0)}/{scan.get('extracted', 0)}",
        f"• Saved {_bar(state.get('saved', 0), state.get('saved_max', 30))} "
        f"{state.get('saved', 0)}/{state.get('saved_max', 30)}",
        f"• Healthy {scan.get('healthy', 0)} · Dead {scan.get('dead', 0)} · "
        f"Appr {scan.get('approval', 0)} · Err {scan.get('error', 0)}",
        f"• Sent {tot.get('sent', 0)} · Fail {tot.get('fail', 0)} · "
        f"PV {tot.get('pv_in', 0)}/{tot.get('pv_out', 0)}",
        f"• Roles {IC_SCAN} {n_scan} · {IC_SEND} {n_send}",
    ]

    scan_block = [LINE, "--| Scan"] + [
        _scan_line(a) for a in state.get("scanners", [])]
    send_block = [LINE, "--| Send"] + [
        _send_line(a) for a in state.get("senders", [])]
    footer = [LINE, "--| 🌍 - engine : automation"]

    # Try to fit everything into one message; spill scanner/sender rows if not.
    whole = "\n".join(top + scan_block + send_block + footer)
    if len(whole) <= TG_LIMIT:
        return [whole]

    # Overflow: message 0 = header + scan; message 1.. = senders in chunks.
    msgs = ["\n".join(top + scan_block + footer)]
    rows = [_send_line(a) for a in state.get("senders", [])]
    chunk = [LINE, "--| Send (cont.)"]
    cur = list(chunk)
    for r in rows:
        trial = "\n".join([f"| ⚙ - #automation (2)"] + cur + [r] + footer)
        if len(trial) > TG_LIMIT and len(cur) > len(chunk):
            msgs.append("\n".join([f"| ⚙ - #automation"] + cur + footer))
            cur = list(chunk)
        cur.append(r)
    msgs.append("\n".join([f"| ⚙ - #automation"] + cur + footer))
    return msgs


# --------------------------------------------------------------------------- #
# Error card (separate message, NOT part of the live card).  Codes come from
# autoeng_core.classify_error; this renders them with the owner-facing reason.
# --------------------------------------------------------------------------- #
def error_card(*, code: str, stage: str, account: str, worker: str,
               reason: str, action: str, clock: str) -> str:
    return (
        "| ⚠ - #automation_error\n" + LINE + "\n"
        f"--| {code}\n"
        f"• Stage   : {stage}\n"
        f"• Account : {account}\n"
        f"• Worker  : {worker or 'local'}\n"
        f"• Reason  : {str(reason)[:180]}\n"
        f"• Action  : {action}\n" + LINE + "\n"
        f"🕒 {clock}"
    )


def summary_card(*, elapsed: int, totals: dict, scan: dict, quarantined: int,
                 lost_groups: int, clock: str) -> str:
    tot = totals or {}
    return (
        "| 🏁 - #automation_done\n" + LINE + "\n"
        f"• Ran      : {_fmt_elapsed(elapsed)}\n"
        f"• Sent     : {tot.get('sent', 0)} · Fail {tot.get('fail', 0)}\n"
        f"• PV       : in {tot.get('pv_in', 0)} / out {tot.get('pv_out', 0)}\n"
        f"• Healthy  : {(scan or {}).get('healthy', 0)} groups found\n"
        f"• Lost     : {lost_groups} groups · {quarantined} accounts quarantined\n"
        + LINE + f"\n🕒 {clock}"
    )
