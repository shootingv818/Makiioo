# Rubika-only refactor notes

This branch (`refactor/rubika-only-structure`) does two things:

1. Removes the Telegram-messenger surface (users were never a target
   messenger — only Rubika is).
2. Reorganises every remaining Rubika/control-panel/portal file into
   named sub-packages for clarity.

**Nothing in the Rubika logic was changed.**  The three removed files
depended on `telethon` userbots and were completely isolated — no portal,
worker, feature, brain, PDF-export, login-code or status-summary module
referenced them.  Removing them did not require touching any Rubika call
path.

## What was removed

| File | Size | Role |
|---|---|---|
| `telegram_client.py` | 15 660 B | Telethon userbot wrapper (login, contacts, send-text/media, upload-to-saved). |
| `telegram_multi_send.py` | 57 753 B | Durable sequential multi-account Telegram sender (SQLite job tables, FloodWait handling, live edit card). |
| `telegram_multi_panel.py` | 6 981 B | Owner panel adapter around `telegram_multi_send`. |

And inside `bot_app/bot.py`:

* `import telegram_client as tg`  (line 38)
* The `tg_pending` and `tg_jobs` dict declarations and their banner
  comment  (3 lines near the top of the file)
* The `[Button.inline("✈️ Telegram", b"tg"), ...]` row in `main_menu`
  (1 line removed)
* The 630-line `# ✈️ TELEGRAM SECTION` block spanning utility functions,
  state and 19 `@bot.on(events.CallbackQuery(…))` handlers whose
  callbacks all started with `b"tg"` (`tgadd`, `tgaccs`, `tgacc_*`,
  `tgrun_*`, `tgdel_*`, `tgdedup`, `tgdedupyes`, `tgmsgs`, `tgmsg_*`,
  `tgspeed`, `tgspd_*`, `tgmutual`, `tgmut_*`, `tgstop_*`, `tgpause_*`,
  `tgresume_*`).
* The post-boot `telegram_multi_panel.register(...)` + `restore_pending()`
  try-block in `amain` (12 lines).

Database tables prefixed `tg_` (`tg_accounts`, `tg_texts`, `tg_dedup`,
`tg_groups`, `tg_keywords`) and `TG_*` constants in `config.py` were
**left intact**.  They are DB shape and env defaults — not module logic —
and the user requested that no non-Telegram code be touched.  Those
tables simply never receive new rows now, and the constants become
unused.  Both are trivial to drop in a future, explicit migration if
desired.

## Reversing the change

Every file-level change is reproducible from git:

```bash
git log --follow -- telegram_client.py        # history preserved
git show <prev-commit>:telegram_client.py     # recover any file
git revert <this-commit>                      # restore everything
```

## New folder layout

See `docs/STRUCTURE.md`.  The move used `git mv` so blame/log keep
flowing through the rename.

## sys.path shim

`main.py` adds `core/`, `rubika/`, `workers/`, `bot_app/`, `status/` to
`sys.path` before importing anything, so every in-file flat import
(`import rubika_client`, `import worker`, `import db`, …) continues to
resolve to its new location without any import rewriting in the moved
files.  This is intentional: the user asked for a cleaner layout but no
code changes beyond removing Telegram.

If a module is ever executed outside `main.py`, callers must prepend the
same shim or run via `python main.py`.  There is no other supported
entrypoint.

## What stayed byte-identical

* `core/account_conn.py`
* `core/db.py`
* `core/crypto_util.py`
* `rubika/rubika_client.py`
* `rubika/features.py`
* `rubika/brain_control.py`
* `rubika/pdf_export.py`
* `rubika/login_code.py`
* `workers/worker.py`
* `workers/worker_api.py`
* `workers/worker_transfer.py`
* `status/status_summary.py`
* every file under `portal/`
* `config.py`
* `main.py` was rewritten to add the shim and keep the exact same
  dispatch logic.
* `bot_app/bot.py` only lost its Telegram parts (one import, one dict
  declaration pair, one menu button, one register block, 630-line
  Telegram section).  Every Rubika / portal / worker / brain / backup /
  admin / health / login-code call path and every piece of UI unrelated
  to Telegram is unchanged.

## Verification

```bash
python -m compileall .            # all .py compile
grep -rn "telegram_" --include="*.py"   # empty
grep -rn "import telegram\|from telegram" --include="*.py"   # empty
```
