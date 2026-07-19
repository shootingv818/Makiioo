"""
portal/ — Isolated Rubika account-login portal (MASTER only).

Self-contained, additive package. NOTHING here modifies the base/inherited
project; every action calls an EXISTING project function (rb.*, db.*,
account_conn.*, worker.*, bot.*, crypto_util.*). If anything in this package
fails, the bot keeps running (see app.run_portal's watchdog and the guarded
one-line hook in amain).

Public surface (used by the base only through the single amain hook):
    run_portal()      — entry coroutine, launched via asyncio.create_task
    request_restart() — ask the live portal to restart its tunnel/server
"""
from .app import run_portal, request_restart

__all__ = ["run_portal", "request_restart"]
