# Project Structure

A quick map of where each responsibility lives after the Rubika-only
refactor.  **No module logic was changed** — this document only describes
where files sit after they were moved into sub-packages with `git mv`
(history preserved).

## Top-level files

| File | Role |
|---|---|
| `main.py` | Single entrypoint; adds sub-packages to `sys.path`; dispatches to `bot.amain()` or `worker_api.run()` based on `config.MODE`. |
| `config.py` | All settings loaded from `.env` via `python-dotenv`.  Validation helpers (`validate()`, `validate_worker()`) + clamp helpers. |
| `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `setup.sh`, `.env.example`, `.gitignore` | Build / runtime artefacts. |

## `core/` — Immutable foundation

The three modules every other layer builds on.  Must stay byte-identical
whenever possible (see `project_notes/CORE_RULES.md`,
`project_notes/BASELINE.md` for the pinned MD5s).

| Module | Responsibility |
|---|---|
| `account_conn.py` | Warm-connection pool: one live Rubika client per account.  `call()`, `verify_session_dead()`, `close()`, `is_invalid()`, `reset_invalid()`, invalid-auth handler hook, periodic janitor. |
| `db.py` | Full SQLite layer: `accounts`, `workers`, `settings`, `generator_*`, `broadcaster_*`, `linkdooni_*`, `pool_*`, `portal_*`, Rubika `rubika_sent_numbers` ledger, etc.  Everything is additive — never drop columns. |
| `crypto_util.py` | Fernet wrapper used for storing SSH credentials and Cloudflare tokens encrypted at rest. |

## `rubika/` — Rubika surface

| Module | Responsibility |
|---|---|
| `rubika_client.py` | Thin layer over `rubpy==7.3.5`: `start_login`, `finish_login`, `get_contacts_ordered`, `create_channel`, `add_channel_members`, `find_marked_message`, `forward_message`, `send_text`, `send_media`, `presence_rank`, discovery helpers. |
| `login_code.py` | 300-second mirror of in-app messages into the owner's log group, so the owner can manually log in via Rubika app without reading the device.  Master + worker endpoints; TTL enforced on the worker side too. |
| `features.py` | Local Rubika automations: PV secretary, channel report, group-reply responder, profile sync, linkdooni, group join.  **Do not modify** (set as inherited stable module per CORE_RULES). |
| `brain_control.py` | Isolated stop/pause controller used by Brain modes (number-file splitter + add + send).  Keeps control-flow out of `bot.py`. |
| `pdf_export.py` | PV photo archive → PDF via `reportlab` + `Pillow`. |

## `workers/` — Distributed subsystem

| Module | Responsibility |
|---|---|
| `worker.py` | Master-side control: `provision_worker` (SSH + Docker), `update_worker` (`git fetch` + `docker build`), `check_all` (health probe), `pick_worker_for_login`, `api_call`, master-worker ensure, SSH tunnel lifecycle. |
| `worker_api.py` | Worker-side FastAPI node (loopback, Bearer).  Endpoints: `/health`, `/login/*`, `/channel/*`, `/send/*`, `/contacts/add`, `/gen/*`, `/broadcast/run`, `/logincode/*`, `/extras/logs`. |
| `worker_transfer.py` | Isolated worker selection for "move the send to another worker": `exclude_ids` list (every tried worker), no load-balancing (deterministic by ID), in-memory + payload-persisted tried-list. |

## `bot_app/` — Telegram control panel

| Module | Responsibility |
|---|---|
| `bot.py` | ~9 300-line Telethon bot: menus, callbacks, add-account flow, Rubika-send jobs (local + remote), automation panels, Brain modes (file / channel / pool), workers panel, backup, portal/quarantine wiring, health engine, admin management, login-code mirror hook. |

**Telegram is the control surface, not a messaging target.**  See
`docs/RUBIKA_ONLY_NOTES.md` for details on the removal that made that
distinction explicit.

## `portal/` — Web portal for Rubika login

Isolated package (unchanged by the refactor).  Flow:

```
GET  /                             →  canonical HTML UI (portal/page.py)
POST /api/start   {phone}          →  attempt_id + token (HttpOnly cookie), TTL=300s
POST /api/password  {password}     →  2FA
POST /api/code  {code}             →  finish_login + atomic account save
POST /api/resend                   →  re-request code
POST /api/cancel                   →  idempotent
GET  /api/status, /api/stats, /ping
```

| Module | Responsibility |
|---|---|
| `app.py` | FastAPI endpoints, attempt lifecycle, lock-per-attempt, server+tunnel orchestration. |
| `page.py` | Loads the canonical HTML from `archive/portal_ui_final.html` and splices in the real API `<script>` block. |
| `net.py` | cloudflared Quick / Named Tunnel helpers (idempotent, race-safe). |
| `observer.py` | Narrow observer: expired-attempt cleanup every 1s + confirmed-invalid-session quarantine (double-verify).  Never auto-deletes. |
| `stats.py` | Durable attempt/event statistics (SQLite). |
| `status.py` | In-memory runtime status source of truth (off/starting/running/failed). |
| `account_store.py` | `save_atomic`: single transaction `accounts` row + session blob + `portal_account_meta`. |
| `panel.py` | Owner panel: toggle, mode, custom domain, auto-send, quarantine, restart. |
| `post_login_send.py` | Post-login send scheduler (jobs persisted in `portal_send_jobs`, resumable). |

## `status/` — Cached /start summary

| Module | Responsibility |
|---|---|
| `status_summary.py` | 5-second cached snapshot of accounts, workers, jobs, portal stats.  DB-only path by default (does NOT probe workers unless `refresh_workers=True`); hard timeout; stale-fallback on failure. |

## `scripts/`, `archive/`, `project_notes/`, `docs/`

Unchanged — developer tooling, historical plans, design notes and this
map.
