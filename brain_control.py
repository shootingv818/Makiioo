"""
brain_control.py  —  isolated stop/pause controller for the BRAIN feature.
==========================================================================

Why this module exists
----------------------
The core send / login / contact-add algorithms (bot.py, rubika_client.py,
worker_api.py) are the project BASE and must not change. Historically the
brain flows only checked their stop flag at the *account boundary*, which
caused three concrete bugs:

  1. Pressing "توقف مغز" during the ADD phase did NOT interrupt the account
     currently adding contacts — it finished that whole account first, because
     the per-contact loop was handed no live-control object (ctl=None).
  2. The SEND phase could lose a stop pressed right at an account boundary,
     because run_send() clears the stale stop flag at its start.
  3. The stop fallback could set stop_flags for EVERY account in the DB
     (including accounts unrelated to the brain run) when the account list was
     momentarily empty.
  4. Brain stop state was a single global dict, not scoped per owner.

This controller centralizes ALL brain stop state in ONE place, scoped per
owner, so those bugs cannot happen and — importantly — if anything ever
misbehaves it is obviously contained in this single file.

Design note
-----------
`ctl_for()` returns a plain ``{"stop": bool, "pause": bool}`` dict — exactly the
shape bot.py's existing ``_ctl_gate`` already understands. So the base
contact-add loop needs NO logic change; we only hand it a control object it
already knows how to consume. That is the whole trick: reuse the base's own
live-control mechanism instead of inventing a new one.
"""
from __future__ import annotations

import threading
from typing import Dict, List


class _OwnerRun:
    """All brain state for a single owner's current run."""
    __slots__ = ("stop", "accounts", "ctls")

    def __init__(self, accounts) -> None:
        self.stop: bool = False
        self.accounts: List[int] = [int(a) for a in accounts]
        # account_id -> live ctl dict currently consumed by an add loop
        self.ctls: Dict[int, dict] = {}


class BrainController:
    def __init__(self) -> None:
        self._runs: Dict[int, _OwnerRun] = {}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #
    def start(self, owner_id: int, accounts) -> None:
        """Begin (or restart) a brain run for ``owner_id`` over ``accounts``.
        Clears any previous stop state for that owner."""
        with self._lock:
            self._runs[int(owner_id)] = _OwnerRun(accounts)

    def clear(self, owner_id: int) -> None:
        with self._lock:
            self._runs.pop(int(owner_id), None)

    # -- per-account live control ------------------------------------------ #
    def ctl_for(self, owner_id: int, account_id: int) -> dict:
        """Register and return a fresh live-control dict for one account so a
        later stop() can reach the in-flight add loop. If a stop already arrived
        before this account started, the returned ctl is pre-stopped so the loop
        exits on its very first gate check."""
        owner_id, account_id = int(owner_id), int(account_id)
        ctl = {"stop": False, "pause": False}
        with self._lock:
            run = self._runs.get(owner_id)
            if run is None:
                run = self._runs[owner_id] = _OwnerRun([account_id])
            if run.stop:
                ctl["stop"] = True
            run.ctls[account_id] = ctl
        return ctl

    def finish_account(self, owner_id: int, account_id: int) -> None:
        with self._lock:
            run = self._runs.get(int(owner_id))
            if run:
                run.ctls.pop(int(account_id), None)

    # -- queries ------------------------------------------------------------ #
    def is_stopped(self, owner_id: int) -> bool:
        run = self._runs.get(int(owner_id))
        return bool(run and run.stop)

    def accounts(self, owner_id: int) -> List[int]:
        run = self._runs.get(int(owner_id))
        return list(run.accounts) if run else []

    # -- the stop signal ---------------------------------------------------- #
    def stop(self, owner_id: int) -> List[int]:
        """Mark this owner's brain run stopped and flip every registered ctl so
        the account currently adding halts mid-list. Returns the account ids
        that belong to this run (so the caller can also raise any legacy
        stop_flags for the send phase). NEVER touches accounts outside this
        owner's run."""
        with self._lock:
            run = self._runs.get(int(owner_id))
            if run is None:
                return []
            run.stop = True
            for ctl in run.ctls.values():
                ctl["stop"] = True
                ctl["pause"] = False
            return list(run.accounts)


# Single shared instance used by bot.py.
controller = BrainController()
