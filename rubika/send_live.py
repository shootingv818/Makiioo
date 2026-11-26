"""
📤 Live send card + server-switch cards — PURE renderers (no I/O).
===================================================================

Replaces the old "one log message every 50 recipients" noise with ONE live
message that is edited as the run progresses, in the project's card style
(`| ⚙ - #send`, 31-dash dividers). Shows which account, which worker/IP, which
phase (one-way vs two-way), progress, per-phase results and the switch budget.

Server switches are separate cards (owner asked for a log on every switch,
plus a follow-up saying whether it actually helped).
"""
from __future__ import annotations

import send_phase as sp

LINE = "-------------------------------"
TG_LIMIT = 4096

IC_ONEWAY = "🔸"
IC_TWOWAY = "🔹"
IC_DONE = "🏁"


def _bar(done: int, total: int, width: int = 10) -> str:
    total = max(0, int(total))
    done = max(0, min(int(done), total))
    filled = 0 if total <= 0 else max(0, min(width, int(round(width * done / total))))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _elapsed(sec: int) -> str:
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def phase_label(phase: str) -> str:
    if phase == sp.PHASE_ONEWAY:
        return f"{IC_ONEWAY} ONE-WAY  (no chat yet)"
    if phase == sp.PHASE_TWOWAY:
        return f"{IC_TWOWAY} TWO-WAY  (contacts with chat)"
    return f"{IC_DONE} FINISHED"


def render_live(*, phone: str, tag: str, worker_tag: str, worker_ip: str,
                state: sp.PhaseState, elapsed: int, clock: str,
                stopped: bool = False) -> str:
    """Build the live send card from a PhaseState snapshot."""
    one = state.stats[sp.PHASE_ONEWAY]
    two = state.stats[sp.PHASE_TWOWAY]
    q_total = len(state.queue())
    head = "LIVE" if not stopped else "STOPPED"

    # one-way line: show why it ended, when it did
    if state.phase == sp.PHASE_ONEWAY:
        one_line = (f"• One-way : {one['sent']} sent · {one['fail']} fail · "
                    f"streak {state.streak}/{sp.STREAK_LIMIT}")
    elif state.oneway_blocked:
        one_line = (f"• One-way : {one['sent']} sent · {one['fail']} fail · "
                    f"⛔ stopped ({sp.STREAK_LIMIT} in a row)")
    else:
        one_line = f"• One-way : {one['sent']} sent · {one['fail']} fail · done"

    if state.phase == sp.PHASE_TWOWAY:
        two_line = (f"• Two-way : {two['sent']} sent · {two['fail']} fail · "
                    f"streak {state.streak}/{sp.STREAK_LIMIT}")
    elif state.phase == sp.PHASE_DONE and (two["sent"] or two["fail"]):
        two_line = f"• Two-way : {two['sent']} sent · {two['fail']} fail · done"
    else:
        two_line = "• Two-way : waiting"

    rows = [
        "| ⚙ - #send",
        LINE,
        f"--| {head} · {_elapsed(elapsed)} · {clock}",
        f"• Account : {phone}  {('#' + tag) if tag else ''}".rstrip(),
        f"• Worker  : {worker_tag or 'local'}"
        + (f" · {worker_ip}" if worker_ip else ""),
        f"• Stage   : {phase_label(state.phase)}",
        LINE,
        f"• Progress {_bar(state.cursor, q_total)} {state.cursor}/{q_total}",
        f"• Sent {state.total_sent()} · Fail {state.total_fail()}",
        one_line,
        two_line,
        f"• Server  : {state.switches}/{state.max_switches} switches",
        LINE,
        "--| 🌍 - engine : send",
    ]
    out = "\n".join(rows)
    return out[:TG_LIMIT]


def switch_card(*, n: int, total: int, phone: str, reason: str,
                from_tag: str, from_ip: str, to_tag: str, to_ip: str,
                resume_at: int, clock: str) -> str:
    return "\n".join([
        "| 🔄 - #server_switch",
        LINE,
        f"--| SWITCH {n}/{total}",
        f"• Account : {phone}",
        f"• Reason  : {reason}",
        f"• From    : {from_tag or 'local'}" + (f" · {from_ip}" if from_ip else ""),
        f"• To      : {to_tag or 'local'}" + (f" · {to_ip}" if to_ip else ""),
        f"• Resume  : from #{resume_at} (no re-send)",
        LINE,
        f"🕒 {clock}",
    ])


def switch_result_card(*, n: int, total: int, phone: str, worker_tag: str,
                       worker_ip: str, ok_after: int, fail_after: int,
                       clock: str) -> str:
    verdict = sp.switch_verdict(ok_after, fail_after)
    good = ok_after > 0 and fail_after == 0
    return "\n".join([
        f"| {'✅' if good else '⚠'} - #server_switch",
        LINE,
        f"--| SWITCH {n}/{total} — {'WORKING' if good else 'NO LUCK'}",
        f"• Account : {phone}",
        f"• Worker  : {worker_tag or 'local'}"
        + (f" · {worker_ip}" if worker_ip else ""),
        f"• First sends after switch : {ok_after} ok · {fail_after} fail",
        f"• Verdict : {verdict}",
        LINE,
        f"🕒 {clock}",
    ])


def switch_failed_card(*, n: int, total: int, phone: str, error: str,
                       clock: str) -> str:
    """The transfer itself could not be done (no spare worker / import failed).
    The run continues on the current server; the next switch is still allowed."""
    return "\n".join([
        "| ⚠ - #server_switch",
        LINE,
        f"--| SWITCH {n}/{total} — TRANSFER FAILED",
        f"• Account : {phone}",
        f"• Error   : {str(error)[:160]}",
        "• Action  : staying on the current server",
        LINE,
        f"🕒 {clock}",
    ])


def final_card(*, phone: str, state: sp.PhaseState, elapsed: int,
               clock: str) -> str:
    """End-of-run summary: per-phase, per-worker, and which IP did best — the
    data that answers 'is the limit per-IP or per-account?' over time."""
    one = state.stats[sp.PHASE_ONEWAY]
    two = state.stats[sp.PHASE_TWOWAY]
    rows = [
        "| 🏁 - #send_done",
        LINE,
        f"• Account : {phone}",
        f"• Ran     : {_elapsed(elapsed)}",
        f"• Sent    : {state.total_sent()} · Fail {state.total_fail()}",
        f"• One-way : {one['sent']} sent · {one['fail']} fail",
        f"• Two-way : {two['sent']} sent · {two['fail']} fail",
        f"• Switches: {state.switches}/{state.max_switches}",
        LINE,
        "--| per server",
    ]
    best, best_n = None, -1
    for tag, v in state.per_worker.items():
        rows.append(f"• {tag} : {v['sent']} ok · {v['fail']} fail")
        if v["sent"] > best_n:
            best, best_n = tag, v["sent"]
    if best and len(state.per_worker) > 1:
        rows.append(f"• Best    : {best} ({best_n} ok)")
    rows += [LINE, f"🕒 {clock}"]
    return "\n".join(rows)[:TG_LIMIT]
