"""
Unified entrypoint for the Rubika-only panel.

  MODE=master  (default) -> run the Telegram panel bot (bot_app/bot.py)
  MODE=worker            -> run the headless worker API node (workers/worker_api.py)

The mode is read from the environment / .env via config.MODE. This lets the
exact same codebase + Docker image serve as either a master or a worker, which
is what the auto-provisioning flow relies on.

The project is organised into logical sub-packages:

  core/      --- immutable foundation (CORE_RULES.md): account_conn, db,
                  crypto_util.  Never rewrite these.
  rubika/    --- Rubika messenger surface: rubika_client (rubpy wrapper),
                  login_code mirror, features (automations), brain_control,
                  pdf_export.
  workers/   --- distributed-mode subsystem: worker (master-side control),
                  worker_api (worker-side FastAPI node), worker_transfer.
  bot_app/   --- Telethon control panel (bot.py).  This is a management tool
                  for the owner, NOT a messaging target.
  portal/    --- web portal for Rubika login (FastAPI + cloudflared).
  status/    --- cached status summary card.

A tiny ``sys.path`` shim below exposes every sub-package's modules under
their original flat names (``import rubika_client`` etc.) so no in-file
imports needed to change when the layout was tidied.  This is explicit:
only the sub-package directories listed below are added to the path.

Run locally:
    python main.py                 # master (or set MODE=worker in .env)
"""
import os
import sys

# --- sub-package path shim (keeps every in-file ``import X`` working) ------
_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ("core", "rubika", "workers", "bot_app", "status"):
    _path = os.path.join(_ROOT, _sub)
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

import config  # noqa: E402  (must come after path shim)


def main():
    if config.MODE == "worker":
        import worker_api
        worker_api.run()
    else:
        import asyncio
        import bot
        asyncio.run(bot.amain())


if __name__ == "__main__":
    main()
