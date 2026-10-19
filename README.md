# Makiioo — Rubika Panel

Owner-only Telegram control panel for multi-account **Rubika** messenger
automation.  Telegram is only the admin surface; every messaging/automation
target is Rubika.

```
┌─────────────────────────┐          ┌─────────────────────────┐
│   Owner (Telegram bot)  │  SSH +   │   Worker node(s)        │
│   bot_app/bot.py        │  HTTPS   │   workers/worker_api.py │
│   portal/app.py (web)   ├─────────▶│   loopback FastAPI      │
└─────────────────────────┘  tunnel  └─────────────────────────┘
            │
            └──  rubpy==7.3.5 Rubika SDK  ──▶  Rubika users / channels / groups
```

## Project layout

```
.
├── main.py                 Unified entrypoint (MODE=master | MODE=worker)
├── config.py               Env-based settings
├── requirements.txt
├── Dockerfile              Worker image (same code serves master too)
├── docker-compose.yml      Optional single-worker compose stack
├── setup.sh                First-run master bootstrap
├── .env.example
│
├── core/                   Immutable foundation (CORE_RULES — never rewrite)
│   ├── account_conn.py     One warm Rubika connection per account
│   ├── db.py               SQLite data layer (accounts, workers, jobs, ...)
│   └── crypto_util.py      Fernet encryption for SSH creds at rest
│
├── rubika/                 Rubika messenger surface
│   ├── rubika_client.py    rubpy wrapper (login, contacts, send, forward)
│   ├── login_code.py       300s in-app message mirror (manual re-login)
│   ├── features.py         PV secretary, channel report, reply responder
│   ├── brain_control.py    Isolated stop/pause controller for Brain modes
│   └── pdf_export.py       PV photo archive → PDF
│
├── workers/                Distributed master ↔ worker subsystem
│   ├── worker.py           Master-side control (provision, update, pick)
│   ├── worker_api.py       Worker-side FastAPI node (loopback + Bearer)
│   └── worker_transfer.py  Pick a worker never tried for this send
│
├── bot_app/                Telethon control panel
│   └── bot.py              Owner-only admin bot (buttons, menus, orchestration)
│
├── portal/                 Web portal for Rubika login
│   ├── app.py              FastAPI endpoints (/api/start, /code, /password, ...)
│   ├── page.py             Canonical HTML UI
│   ├── net.py              cloudflared Quick/Named Tunnel helpers
│   ├── observer.py         Attempt cleanup + account quarantine (canonical)
│   ├── stats.py            Attempt/event statistics (SQLite)
│   ├── status.py           In-memory runtime status
│   ├── account_store.py    Atomic account+session+worker persistence
│   ├── panel.py            Owner panel (quarantine, domain, auto-send)
│   └── post_login_send.py  Post-login send scheduler
│
├── status/
│   └── status_summary.py   Cached /start status card
│
├── scripts/                Developer harnesses
├── archive/                Historical plans and canonical UI
├── project_notes/          Design notes (CORE_RULES, SESSION_AND_CONFLICTS, …)
└── docs/                   Current layout + migration notes
    ├── STRUCTURE.md
    └── RUBIKA_ONLY_NOTES.md
```

## How the layout stays transparent to the code

`main.py` adds every sub-package directory to `sys.path` before importing
`config` or `bot`.  Every in-file `import rubika_client` / `import worker` /
etc. keeps working with its original flat name — no in-file imports needed
to change when files were tidied into their folders.

If a module is run outside `main.py`, prepend the shim yourself, or run via
`python main.py` (which is the only supported entrypoint).

## Running

```bash
# Master (Telegram control panel)
python main.py                    # or MODE=master

# Worker node (headless FastAPI)
MODE=worker python main.py
```

Required settings live in `.env` (see `.env.example`).  The master validates
them at boot and prints what is missing.

## What was removed

The three Telegram-messenger modules
(`telegram_client.py`, `telegram_multi_send.py`, `telegram_multi_panel.py`)
and their matching UI in `bot.py` were removed.  Telegram now exists only as
the owner-facing control bot (`bot_app/bot.py` on Telethon), **not** as a
messaging/automation target.  See `docs/RUBIKA_ONLY_NOTES.md` for details
and reversal pointers.

## Core rules preserved

* `core/account_conn.py`, `core/db.py` and `rubika/rubika_client.py` stay
  byte-identical with upstream (see `project_notes/BASELINE.md`).
* Changes are additive and isolated; business logic was NOT modified in
  this refactor — only the file layout and the Telegram feature removal.
* One live connection per Rubika session is still enforced.
* Account deletion still requires explicit owner confirmation (quarantine
  flow in `portal/observer.py`).
