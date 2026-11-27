"""
🔍 Curiosity mode — silent collector of RAW Rubika data.
=========================================================

The owner's ask: a Settings section that is ALWAYS ON quietly in the
background, never spams the log group, and can hand over a .txt file of
everything Rubika itself returned — so we can mine it for facts we don't know
yet (exactly how `getMySessions` was discovered).

It records RAW payloads/errors only; it never sends, never changes behaviour,
and every call site is wrapped so a logging failure can't break a feature.

Separate from the DIAGNOSTICS log (auth_diag.py), which DOES post to the log
group because that is the debugging/bug-fixing system.

Storage: a plain JSONL file next to the engine DB (rotated by size), so no
schema change and nothing pinned is touched.
"""
from __future__ import annotations

import json
import os
import time

try:
    import db as _coredb
    _DIR = os.path.join(os.path.dirname(os.path.abspath(_coredb.__file__)), "data")
except Exception:  # pragma: no cover
    _DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

LOG_PATH = os.path.join(_DIR, "curiosity.jsonl")
MAX_BYTES = 8 * 1024 * 1024       # rotate at 8MB (keeps one .1 backup)
MAX_VALUE_CHARS = 4000            # truncate a single huge payload

K_ENABLED = "curiosity_enabled"   # app_settings key (generic store)


# --------------------------------------------------------------------------- #
# enable flag (default ON — the owner wants it always collecting)
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    try:
        import db
        v = db.get_setting(K_ENABLED, "1")
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return True


def set_enabled(on: bool) -> None:
    try:
        import db
        db.set_setting(K_ENABLED, "1" if on else "0")
    except Exception:  # noqa: BLE001
        pass


def set_path_for_tests(path: str) -> None:
    global LOG_PATH
    LOG_PATH = path


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def _jsonable(obj, _depth=0):
    """Best-effort convert a rubpy object/response into plain JSON data."""
    if _depth > 6:
        return "<deep>"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj[:MAX_VALUE_CHARS]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth + 1) for k, v in list(obj.items())[:200]}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, _depth + 1) for v in list(obj)[:200]]
    # rubpy Update-like objects
    for attr in ("original_update", "to_dict", "_data", "data", "__dict__"):
        v = getattr(obj, attr, None)
        if callable(v):
            try:
                v = v()
            except Exception:  # noqa: BLE001
                v = None
        if isinstance(v, dict):
            return _jsonable(v, _depth + 1)
    return str(obj)[:MAX_VALUE_CHARS]


def _rotate_if_needed():
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_BYTES:
            bak = LOG_PATH + ".1"
            try:
                if os.path.exists(bak):
                    os.unlink(bak)
            except Exception:  # noqa: BLE001
                pass
            os.replace(LOG_PATH, bak)
    except Exception:  # noqa: BLE001
        pass


def record(kind: str, *, phone: str = "", worker: str = "", method: str = "",
           raw=None, note: str = "") -> None:
    """Append ONE raw observation. Silent and failure-proof.

    kind: 'sessions' | 'auth_error' | 'api_error' | 'raw' | 'preview' | ...
    raw : whatever Rubika returned (dict / Update / exception)
    """
    if not is_enabled():
        return
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        _rotate_if_needed()
        rec = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "phone": str(phone or ""),
            "worker": str(worker or ""),
            "method": str(method or ""),
            "note": str(note or "")[:400],
            "raw": _jsonable(raw),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - curiosity must never break a feature
        pass


def record_error(exc, *, phone: str = "", worker: str = "", method: str = "",
                 note: str = "") -> None:
    """Record an exception with its FULL Rubika body (status/status_det), which
    is exactly the data normally lost to a short repr()."""
    payload = {"type": type(exc).__name__, "text": str(exc)[:MAX_VALUE_CHARS]}
    for attr in ("args", "original_update", "status", "status_det", "response"):
        v = getattr(exc, attr, None)
        if v is not None:
            payload[attr] = _jsonable(v)
    record("auth_error" if _looks_auth(str(exc)) else "api_error",
           phone=phone, worker=worker, method=method, raw=payload, note=note)


def _looks_auth(text: str) -> bool:
    t = (text or "").upper()
    return any(k in t for k in ("INVALID_AUTH", "INVALIDAUTH", "NOT_REGISTERED",
                                "AUTH_FROM_ANOTHER"))


# --------------------------------------------------------------------------- #
# stats + export
# --------------------------------------------------------------------------- #
def _read_lines():
    out = []
    for path in (LOG_PATH + ".1", LOG_PATH):
        try:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln:
                        out.append(ln)
        except Exception:  # noqa: BLE001
            continue
    return out


def stats() -> dict:
    """Small summary for the Settings card (no heavy parsing)."""
    lines = _read_lines()
    kinds, phones, methods = {}, set(), {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        k = r.get("kind", "?")
        kinds[k] = kinds.get(k, 0) + 1
        if r.get("phone"):
            phones.add(r["phone"])
        m = r.get("method")
        if m:
            methods[m] = methods.get(m, 0) + 1
    size = 0
    for p in (LOG_PATH, LOG_PATH + ".1"):
        try:
            size += os.path.getsize(p)
        except Exception:  # noqa: BLE001
            pass
    return {"records": len(lines), "kinds": kinds, "accounts": len(phones),
            "methods": methods, "bytes": size, "enabled": is_enabled()}


def export_txt(dest_path: str = None) -> str:
    """Write a human-readable .txt of everything collected and return its path.
    Grouped by kind, newest last, with a summary header."""
    lines = _read_lines()
    dest_path = dest_path or os.path.join(
        _DIR, "curiosity_export_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
    st = stats()
    by_kind = {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        by_kind.setdefault(r.get("kind", "?"), []).append(r)
    with open(dest_path, "w", encoding="utf-8") as fh:
        fh.write("CURIOSITY EXPORT — raw Rubika observations\n")
        fh.write("generated: %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        fh.write("records: %d · accounts: %d · bytes: %d\n"
                 % (st["records"], st["accounts"], st["bytes"]))
        fh.write("kinds: %s\n" % json.dumps(st["kinds"], ensure_ascii=False))
        fh.write("methods: %s\n" % json.dumps(st["methods"], ensure_ascii=False))
        fh.write("=" * 70 + "\n")
        for kind in sorted(by_kind):
            fh.write("\n\n######## %s (%d) ########\n"
                     % (kind.upper(), len(by_kind[kind])))
            for r in by_kind[kind]:
                fh.write("\n--- %s | %s | %s | %s\n"
                         % (r.get("t", ""), r.get("phone", "-"),
                            r.get("worker", "-"), r.get("method", "-")))
                if r.get("note"):
                    fh.write("note: %s\n" % r["note"])
                fh.write(json.dumps(r.get("raw"), ensure_ascii=False,
                                    indent=2)[:20000] + "\n")
    return dest_path


def clear() -> None:
    for p in (LOG_PATH, LOG_PATH + ".1"):
        try:
            if os.path.exists(p):
                os.unlink(p)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# the one ACTIVE probe worth doing: list the account's own sessions.
# Read-only. Answers "how many devices/sessions does this account have?" —
# the discovery that explains INVALID_AUTH vs AUTH_FROM_ANOTHER.
# --------------------------------------------------------------------------- #
async def probe_sessions(client, phone: str = "", worker: str = "") -> dict:
    """Call getMySessions and RECORD the raw answer. Returns a small summary
    (or {} when the build/account doesn't support it). Never raises."""
    try:
        fn = getattr(client, "get_my_sessions", None)
        if fn is None:
            record("sessions", phone=phone, worker=worker,
                   method="get_my_sessions", note="method not in this rubpy build")
            return {}
        res = await fn()
        record("sessions", phone=phone, worker=worker, method="getMySessions",
               raw=res, note="raw session list")
        d = _jsonable(res)
        sessions = []
        if isinstance(d, dict):
            for key in ("sessions", "my_sessions", "data"):
                v = d.get(key)
                if isinstance(v, list):
                    sessions = v
                    break
        return {"count": len(sessions), "sessions": sessions}
    except Exception as e:  # noqa: BLE001
        record_error(e, phone=phone, worker=worker, method="getMySessions")
        return {}
