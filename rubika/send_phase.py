"""
🔸🔹 Two-phase send + automatic server rotation — PURE logic (no I/O).
=====================================================================

The owner's rules, exactly:

  PHASE 1 — ONE-WAY  : send to contacts we have NO chat with ("strangers"),
                       newest/recently-seen first.
  switch             : after 5 CONSECUTIVE send errors in phase 1, stop
                       phase 1 and move to phase 2 on the SAME server
                       (Rubika is refusing strangers; the IP is fine).
  PHASE 2 — TWO-WAY  : send to contacts we DO have a chat with.
  server rotation    : after 5 CONSECUTIVE send errors in phase 2, the IP is
                       the problem -> move the session to the next worker
                       (existing code-free transfer) and CONTINUE from the
                       same cursor. Fully automatic, no confirmation.
                       Up to `max_switches` (panel, default 3), 3 min apart,
                       and it always runs to the end (a failed switch does
                       NOT stop the next one).

`STREAK_LIMIT` is fixed at 5 per the owner. Only real send failures count;
one success resets the streak.

This module is deliberately pure so every rule is unit-testable offline; the
async driver that performs sends/transfers lives in bot.py's send engine.
"""
from __future__ import annotations

STREAK_LIMIT = 5          # fixed: 5 consecutive errors
DEFAULT_MAX_SWITCHES = 3  # panel-tunable
SWITCH_PAUSE_SEC = 180    # 3 minutes between switches

PHASE_ONEWAY = "oneway"
PHASE_TWOWAY = "twoway"
PHASE_DONE = "done"

# what the driver should do next
ACT_SEND = "send"             # keep sending in the current phase
ACT_TO_TWOWAY = "to_twoway"   # phase 1 exhausted/blocked -> phase 2, same server
ACT_SWITCH = "switch"         # phase 2 blocked -> rotate to the next worker
ACT_STOP = "stop"             # nothing left to do


def split_recipients(ordered_records: list) -> tuple:
    """Split send_order's ordered records into (one_way, two_way) guid lists,
    PRESERVING the existing order inside each group.

    The owner's definition decides the split:
        one-way = we have NO chat with them  (send_order's `has_chat` is False)
        two-way = a chat exists              (`has_chat` is True)

    NOT `mutual`, which is stricter (a chat PLUS they wrote or read). Using
    `mutual` put "has a chat but never replied" into the one-way bucket, so the
    send still went to chat contacts early — the exact symptom reported.

    `has_chat` is additive, so a record from an older build that lacks it falls
    back to `mutual` (previous behaviour) rather than being misclassified.
    """
    one_way, two_way = [], []
    for r in ordered_records or []:
        if not isinstance(r, dict):
            continue
        guid = r.get("guid")
        if not guid:
            continue
        if "has_chat" in r:
            is_two = bool(r.get("has_chat"))
        else:
            is_two = bool(r.get("mutual"))
        (two_way if is_two else one_way).append(guid)
    return one_way, two_way


class PhaseState:
    """Tracks phase, per-phase counters, the consecutive-error streak and the
    server-switch budget. The driver calls record_ok/record_fail and then
    next_action()."""

    def __init__(self, one_way: list, two_way: list,
                 max_switches: int = DEFAULT_MAX_SWITCHES):
        self.one_way = list(one_way or [])
        self.two_way = list(two_way or [])
        self.max_switches = max(0, int(max_switches))
        self.phase = PHASE_ONEWAY if self.one_way else (
            PHASE_TWOWAY if self.two_way else PHASE_DONE)
        self.cursor = 0                 # index inside the CURRENT phase list
        self.streak = 0                 # consecutive failures in this phase
        self.switches = 0               # server switches used
        self.stats = {
            PHASE_ONEWAY: {"sent": 0, "fail": 0},
            PHASE_TWOWAY: {"sent": 0, "fail": 0},
        }
        # per-worker tally so the final card can say which IP did best
        self.per_worker = {}
        self.oneway_blocked = False     # phase 1 ended on the 5-error rule

    # ---- current queue -------------------------------------------------- #
    def queue(self) -> list:
        if self.phase == PHASE_ONEWAY:
            return self.one_way
        if self.phase == PHASE_TWOWAY:
            return self.two_way
        return []

    def remaining(self) -> int:
        return max(0, len(self.queue()) - self.cursor)

    def current_guid(self):
        q = self.queue()
        return q[self.cursor] if self.cursor < len(q) else None

    def total_sent(self) -> int:
        return sum(v["sent"] for v in self.stats.values())

    def total_fail(self) -> int:
        return sum(v["fail"] for v in self.stats.values())

    # ---- outcome recording ---------------------------------------------- #
    def _tally(self, worker_tag: str, ok: bool):
        w = self.per_worker.setdefault(worker_tag or "local",
                                       {"sent": 0, "fail": 0})
        w["sent" if ok else "fail"] += 1

    def _bucket(self) -> str:
        """Which phase a result belongs to. A result recorded while the state
        says DONE must NEVER vanish (that silently showed 'Sent 0' for a run
        that really sent 36), so it is attributed to the phase we were last
        working in."""
        if self.phase in self.stats:
            return self.phase
        return PHASE_TWOWAY if (self.two_way or self.oneway_blocked) else PHASE_ONEWAY

    def record_ok(self, worker_tag: str = "local"):
        self.stats[self._bucket()]["sent"] += 1
        self._tally(worker_tag, True)
        self.streak = 0                 # a success breaks the streak
        self.cursor += 1

    def record_fail(self, worker_tag: str = "local"):
        self.stats[self._bucket()]["fail"] += 1
        self._tally(worker_tag, False)
        self.streak += 1
        self.cursor += 1

    def record_skip(self):
        """Recipient skipped without an attempt (e.g. already sent) — moves the
        cursor but touches neither counters nor the streak."""
        self.cursor += 1

    # ---- the decision --------------------------------------------------- #
    def next_action(self) -> str:
        if self.phase == PHASE_DONE:
            return ACT_STOP

        hit_streak = self.streak >= STREAK_LIMIT
        exhausted = self.remaining() <= 0

        if self.phase == PHASE_ONEWAY:
            if hit_streak:
                self.oneway_blocked = True
                return ACT_TO_TWOWAY if self.two_way else ACT_STOP
            if exhausted:
                return ACT_TO_TWOWAY if self.two_way else ACT_STOP
            return ACT_SEND

        # phase 2 (two-way)
        if hit_streak:
            # the IP is the suspect here -> rotate, while budget remains.
            return ACT_SWITCH if self.switches < self.max_switches else ACT_STOP
        if exhausted:
            return ACT_STOP
        return ACT_SEND

    # ---- transitions ---------------------------------------------------- #
    def go_twoway(self):
        """Enter phase 2 on the SAME server; cursor restarts for its own list."""
        self.phase = PHASE_TWOWAY if self.two_way else PHASE_DONE
        self.cursor = 0
        self.streak = 0

    def note_switch(self):
        """A server switch happened: keep the cursor (resume where we stopped —
        nobody is messaged twice) and clear the streak so the new IP is judged
        on its own."""
        self.switches += 1
        self.streak = 0

    def finish(self):
        self.phase = PHASE_DONE


def switch_verdict(ok_after: int, fail_after: int) -> str:
    """After a switch the driver reports the first few results. This turns them
    into the owner-facing verdict (and the per-IP vs per-account datum)."""
    if ok_after > 0 and fail_after == 0:
        return "the IP was the problem ✅"
    if ok_after > 0:
        return "partly better (mixed results)"
    if fail_after >= STREAK_LIMIT:
        return "not an IP issue — account-level limit"
    return "inconclusive"
