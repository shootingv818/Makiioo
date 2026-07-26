"""Small in-memory source of truth for the portal lifecycle."""
from __future__ import annotations

import threading
import time

_ALLOWED = {"off", "starting", "running", "failed"}
_LOCK = threading.RLock()
_STATE = {
    "status": "off",
    "mode": "quick",
    "url": "",
    "server": False,
    "tunnel": False,
    "dns": "unchecked",
    "ssl": "unchecked",
    "domain_ping": "unchecked",
    "detail": "",
    "updated_at": time.time(),
}


def set_state(state: str, **fields) -> dict:
    if state not in _ALLOWED:
        raise ValueError(state)
    with _LOCK:
        _STATE.update(fields)
        _STATE["status"] = state
        _STATE["updated_at"] = time.time()
        return dict(_STATE)


def update(**fields) -> dict:
    with _LOCK:
        _STATE.update(fields)
        _STATE["updated_at"] = time.time()
        return dict(_STATE)


def snapshot() -> dict:
    with _LOCK:
        return dict(_STATE)


def clear_runtime(state: str = "off", detail: str = "") -> dict:
    return set_state(
        state,
        url="",
        server=False,
        tunnel=False,
        dns="unchecked",
        ssl="unchecked",
        domain_ping="unchecked",
        detail=detail,
    )
