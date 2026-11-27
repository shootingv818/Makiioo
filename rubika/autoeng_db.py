"""
🔁 Automation Engine — SEPARATE SQLite store.
==============================================

The engine keeps ALL of its state here, in its OWN database file, so that
core/db.py (pinned, must stay byte-identical) is never touched. The file lives
next to the main DB under core/data/ — which the Dockerfile symlinks onto the
mounted volume on workers, so it survives container restarts just like data.db.

Everything the master needs to resume after a restart is persisted here:
roles, the scan queue, group lifecycle, ad texts, PV once-per-person ledger,
PV counters, per-account send stats, and the engine settings/flags.

Only the MASTER uses this DB (the engine brain runs on the master; workers are
stateless executors), so there is exactly one writer.
"""
from __future__ import annotations

import os
import sqlite3
import time

# Reuse core/db.py ONLY to locate the data dir (not its schema). This keeps the
# engine DB on the same mounted volume without importing any table logic.
try:
    import db as _coredb  # noqa: F401  (worker/master both expose `db` on path)
    _DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(_coredb.__file__)), "data")
except Exception:  # pragma: no cover - fallback for standalone tests
    _DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

DB_PATH = os.path.join(_DATA_DIR, "autoeng.db")

# Group lifecycle states.
G_HEALTHY = "healthy"      # scanned OK, not yet handed to a sender
G_ASSIGNED = "assigned"    # handed to a sender, not joined yet
G_JOINED = "joined"        # sender joined and is sending
G_MUTED = "muted"          # muted/banned for its owner -> freed/replaced
G_DEAD = "dead"            # group gone / left
G_EXCLUDED = "excluded"    # banned several accounts -> blocked for everyone

# Link scan states.
L_PENDING = "pending"
L_SCANNING = "scanning"
L_DONE = "done"
L_ERROR = "error"

# Account roles.
R_SCANNER = "scanner"
R_SENDER = "sender"


def _now() -> float:
    return time.time()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def set_db_path_for_tests(path: str) -> None:
    """Point the engine DB at a scratch file (used by the selftest lab)."""
    global DB_PATH
    DB_PATH = path


def init() -> None:
    c = _conn()
    cur = c.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS ae_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS ae_roles (
            account_id INTEGER PRIMARY KEY,
            role       TEXT,
            slot       INTEGER
        );
        CREATE TABLE IF NOT EXISTS ae_links (
            link        TEXT PRIMARY KEY,
            state       TEXT DEFAULT 'pending',
            scanner_id  INTEGER,
            result      TEXT,
            seen_at     REAL,
            checked_at  REAL
        );
        CREATE TABLE IF NOT EXISTS ae_groups (
            guid         TEXT PRIMARY KEY,
            link         TEXT,
            name         TEXT,
            state        TEXT,
            owner_id     INTEGER,
            members      INTEGER DEFAULT 0,
            can_send     INTEGER DEFAULT 1,
            ban_count    INTEGER DEFAULT 0,
            discovered_at REAL,
            joined_at    REAL,
            last_checked REAL
        );
        CREATE TABLE IF NOT EXISTS ae_banhits (
            guid       TEXT,
            account_id INTEGER,
            PRIMARY KEY (guid, account_id)
        );
        CREATE TABLE IF NOT EXISTS ae_texts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT
        );
        CREATE TABLE IF NOT EXISTS ae_replied (
            account_id INTEGER,
            user_guid  TEXT,
            at         REAL,
            PRIMARY KEY (account_id, user_guid)
        );
        CREATE TABLE IF NOT EXISTS ae_pv (
            account_id INTEGER PRIMARY KEY,
            received   INTEGER DEFAULT 0,
            replied    INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ae_stats (
            account_id INTEGER PRIMARY KEY,
            sent       INTEGER DEFAULT 0,
            fail       INTEGER DEFAULT 0
        );
        """
    )
    c.commit()
    c.close()


# --------------------------------------------------------------------------- #
# settings (generic key/value; the engine's own flags live here)
# --------------------------------------------------------------------------- #
def get(key: str, default=None):
    c = _conn()
    row = c.execute("SELECT value FROM ae_settings WHERE key=?", (key,)).fetchone()
    c.close()
    return row["value"] if row else default


def set(key: str, value) -> None:
    c = _conn()
    c.execute("INSERT INTO ae_settings(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, str(value)))
    c.commit()
    c.close()


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, default))
    except Exception:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
def save_roles(roles: dict) -> None:
    c = _conn()
    c.execute("DELETE FROM ae_roles")
    for slot, (aid, role) in enumerate(roles.items()):
        c.execute("INSERT INTO ae_roles(account_id,role,slot) VALUES(?,?,?)",
                  (int(aid), role, slot))
    c.commit()
    c.close()


def load_roles() -> dict:
    c = _conn()
    rows = c.execute("SELECT account_id, role FROM ae_roles ORDER BY slot").fetchall()
    c.close()
    return {r["account_id"]: r["role"] for r in rows}


# --------------------------------------------------------------------------- #
# links (scan queue)
# --------------------------------------------------------------------------- #
def link_seen(link: str) -> bool:
    """Insert a link as pending. Returns True if it is NEW (not seen before)."""
    c = _conn()
    try:
        c.execute("INSERT INTO ae_links(link,state,seen_at) VALUES(?,?,?)",
                  (link, L_PENDING, _now()))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        c.close()


def set_link_result(link: str, state: str, result: str = "") -> None:
    c = _conn()
    c.execute("UPDATE ae_links SET state=?, result=?, checked_at=? WHERE link=?",
              (state, result, _now(), link))
    c.commit()
    c.close()


def requeue_link(link: str) -> None:
    c = _conn()
    c.execute("UPDATE ae_links SET state=?, scanner_id=NULL WHERE link=?",
              (L_PENDING, link))
    c.commit()
    c.close()


def pending_links() -> list:
    c = _conn()
    rows = c.execute("SELECT link FROM ae_links WHERE state=?", (L_PENDING,)).fetchall()
    c.close()
    return [r["link"] for r in rows]


def claim_link(scanner_id: int):
    """Atomically take ONE pending link for `scanner_id` (so two scanners never
    check the same link). Returns the link string or None. Uses IMMEDIATE so
    concurrent claimers serialize on the write lock."""
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT link FROM ae_links WHERE state=? "
                        "ORDER BY seen_at LIMIT 1", (L_PENDING,)).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        c.execute("UPDATE ae_links SET state=?, scanner_id=? WHERE link=?",
                  (L_SCANNING, int(scanner_id), row["link"]))
        c.execute("COMMIT")
        return row["link"]
    except Exception:  # noqa: BLE001
        try:
            c.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #
def upsert_group(guid: str, link: str, name: str, state: str,
                 members: int = 0, can_send: int = 1) -> None:
    c = _conn()
    c.execute(
        "INSERT INTO ae_groups(guid,link,name,state,members,can_send,"
        "discovered_at,last_checked) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(guid) DO UPDATE SET link=excluded.link, name=excluded.name, "
        "state=excluded.state, members=excluded.members, "
        "can_send=excluded.can_send, last_checked=excluded.last_checked",
        (guid, link, name, state, members, can_send, _now(), _now()))
    c.commit()
    c.close()


def set_group_state(guid: str, state: str, owner_id=None) -> None:
    c = _conn()
    if owner_id is None:
        c.execute("UPDATE ae_groups SET state=?, last_checked=? WHERE guid=?",
                  (state, _now(), guid))
    else:
        c.execute("UPDATE ae_groups SET state=?, owner_id=?, last_checked=? WHERE guid=?",
                  (state, int(owner_id), _now(), guid))
    c.commit()
    c.close()


def mark_group_joined(guid: str, owner_id: int) -> None:
    c = _conn()
    c.execute("UPDATE ae_groups SET state=?, owner_id=?, joined_at=? WHERE guid=?",
              (G_JOINED, int(owner_id), _now(), guid))
    c.commit()
    c.close()


def count_unused_healthy() -> int:
    """Healthy groups banked but not yet handed to a sender (reserve size)."""
    c = _conn()
    n = c.execute("SELECT COUNT(*) n FROM ae_groups WHERE state=?",
                  (G_HEALTHY,)).fetchone()["n"]
    c.close()
    return n


def take_reserve_group():
    """Pop one healthy, unassigned group for a replacement. Returns a row dict
    or None. Marks it assigned atomically so two callers can't grab the same."""
    c = _conn()
    try:
        row = c.execute("SELECT * FROM ae_groups WHERE state=? ORDER BY discovered_at "
                        "LIMIT 1", (G_HEALTHY,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE ae_groups SET state=? WHERE guid=?", (G_ASSIGNED, row["guid"]))
        c.commit()
        return dict(row)
    finally:
        c.close()


def groups_of(owner_id: int, state: str = None) -> list:
    c = _conn()
    if state:
        rows = c.execute("SELECT * FROM ae_groups WHERE owner_id=? AND state=?",
                         (int(owner_id), state)).fetchall()
    else:
        rows = c.execute("SELECT * FROM ae_groups WHERE owner_id=?",
                         (int(owner_id),)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def group_counts_by_owner(sender_ids: list) -> dict:
    """{sender_id: number of assigned/joined groups} for the hot-distribution."""
    c = _conn()
    out = {int(s): 0 for s in sender_ids}
    rows = c.execute("SELECT owner_id, COUNT(*) n FROM ae_groups "
                     "WHERE state IN (?,?) GROUP BY owner_id",
                     (G_ASSIGNED, G_JOINED)).fetchall()
    c.close()
    for r in rows:
        if r["owner_id"] in out:
            out[r["owner_id"]] = r["n"]
    return out


def add_ban_hit(guid: str, account_id: int) -> int:
    """Record that `account_id` was banned in `guid`; return the distinct count
    of accounts banned there (for the global-exclusion rule)."""
    c = _conn()
    try:
        c.execute("INSERT OR IGNORE INTO ae_banhits(guid,account_id) VALUES(?,?)",
                  (guid, int(account_id)))
        c.execute("UPDATE ae_groups SET ban_count=("
                  "SELECT COUNT(*) FROM ae_banhits WHERE guid=?) WHERE guid=?",
                  (guid, guid))
        c.commit()
        n = c.execute("SELECT COUNT(*) n FROM ae_banhits WHERE guid=?",
                      (guid,)).fetchone()["n"]
        return n
    finally:
        c.close()


def is_excluded(guid: str) -> bool:
    c = _conn()
    row = c.execute("SELECT state FROM ae_groups WHERE guid=?", (guid,)).fetchone()
    c.close()
    return bool(row and row["state"] == G_EXCLUDED)


# --------------------------------------------------------------------------- #
# ad texts (rotating)
# --------------------------------------------------------------------------- #
def add_text(text: str) -> None:
    c = _conn()
    c.execute("INSERT INTO ae_texts(text) VALUES(?)", (text,))
    c.commit()
    c.close()


def list_texts() -> list:
    c = _conn()
    rows = c.execute("SELECT text FROM ae_texts ORDER BY id").fetchall()
    c.close()
    return [r["text"] for r in rows]


def clear_texts() -> None:
    c = _conn()
    c.execute("DELETE FROM ae_texts")
    c.commit()
    c.close()


# --------------------------------------------------------------------------- #
# PV ledger (once-per-person, durable on the MASTER — fixes worker B5)
# --------------------------------------------------------------------------- #
def already_replied(account_id: int, user_guid: str) -> bool:
    c = _conn()
    row = c.execute("SELECT 1 FROM ae_replied WHERE account_id=? AND user_guid=?",
                    (int(account_id), user_guid)).fetchone()
    c.close()
    return bool(row)


def mark_replied(account_id: int, user_guid: str) -> None:
    c = _conn()
    c.execute("INSERT OR IGNORE INTO ae_replied(account_id,user_guid,at) "
              "VALUES(?,?,?)", (int(account_id), user_guid, _now()))
    c.commit()
    c.close()


def incr_pv(account_id: int, received: int = 0, replied: int = 0) -> None:
    c = _conn()
    c.execute("INSERT INTO ae_pv(account_id,received,replied) VALUES(?,?,?) "
              "ON CONFLICT(account_id) DO UPDATE SET "
              "received=received+?, replied=replied+?",
              (int(account_id), received, replied, received, replied))
    c.commit()
    c.close()


def get_pv(account_id: int) -> dict:
    c = _conn()
    row = c.execute("SELECT received,replied FROM ae_pv WHERE account_id=?",
                    (int(account_id),)).fetchone()
    c.close()
    return {"received": row["received"] if row else 0,
            "replied": row["replied"] if row else 0}


# --------------------------------------------------------------------------- #
# per-account send stats
# --------------------------------------------------------------------------- #
def incr_stats(account_id: int, sent: int = 0, fail: int = 0) -> None:
    c = _conn()
    c.execute("INSERT INTO ae_stats(account_id,sent,fail) VALUES(?,?,?) "
              "ON CONFLICT(account_id) DO UPDATE SET sent=sent+?, fail=fail+?",
              (int(account_id), sent, fail, sent, fail))
    c.commit()
    c.close()


def get_stats(account_id: int) -> dict:
    c = _conn()
    row = c.execute("SELECT sent,fail FROM ae_stats WHERE account_id=?",
                    (int(account_id),)).fetchone()
    c.close()
    return {"sent": row["sent"] if row else 0, "fail": row["fail"] if row else 0}


# --------------------------------------------------------------------------- #
# reset (keeps healthy saved groups; clears roles / queue / per-run state)
# --------------------------------------------------------------------------- #
def clear_stats() -> None:
    """Zero the per-account send counters. The live card is a per-RUN view, so
    a new run must not inherit the previous run's totals."""
    c = _conn()
    c.execute("DELETE FROM ae_stats")
    c.commit()
    c.close()


def reset_run(keep_groups: bool = True) -> None:
    c = _conn()
    c.execute("DELETE FROM ae_roles")
    c.execute("DELETE FROM ae_stats")
    c.execute("DELETE FROM ae_links WHERE state!=?", (L_DONE,))
    if keep_groups:
        # release assignments but KEEP healthy/saved groups; drop the ownership
        # of assigned/joined back to the reserve so a fresh run redistributes.
        c.execute("UPDATE ae_groups SET state=?, owner_id=NULL WHERE state IN (?,?)",
                  (G_HEALTHY, G_ASSIGNED, G_JOINED))
    else:
        c.execute("DELETE FROM ae_groups")
        c.execute("DELETE FROM ae_banhits")
    c.execute("DELETE FROM ae_settings WHERE key IN "
              "('running','live_msg_ids','started_at')")
    c.commit()
    c.close()


def wipe_all_groups() -> None:
    c = _conn()
    c.execute("DELETE FROM ae_groups")
    c.execute("DELETE FROM ae_banhits")
    c.execute("DELETE FROM ae_links")
    c.commit()
    c.close()
