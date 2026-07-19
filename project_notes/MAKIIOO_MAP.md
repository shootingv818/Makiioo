# نقشه و نکات پروژه Makiioo (MAD)

> این فایل برای آپدیت‌های بعدی نوشته شده. قبل از هر تغییر بخوانش.
> مقصد: `shootingv818/Makiioo` — کد پایه = کپی عین آخرین کد `willbedoneuw/Meowv3`
> (برنچ `fix/tg-multi-send-robust-v1`، merge‌شده در main@`a6152c4`).
> مرجع فیکس ساخت ورکر (فقط مرجع، نه کپی کامل): `shootingv818/Haopooonwkkoo`.

---

## معماری کلی

یک کدبیس واحد + یک ایمیج Docker. حالت اجرا با `config.MODE`:
- `MODE=master` → پنل کنترل تلگرام (`bot.py`).
- `MODE=worker` → نود هدلس FastAPI (`worker_api.py`).
- entrypoint: `main.py` (بر اساس `config.MODE` سوییچ می‌کند).

### فایل‌های کلیدی
- `main.py` — سوییچ MODE.
- `bot.py` — پنل مستر تلگرام (بزرگ‌ترین فایل): منوها، ارسال، اتومیشن، ورکرها، مغز، **مغز کانال (جدید)**، Watcher/Health، بکاپ.
- `worker.py` — ارکستراسیون سمت‌مستر ورکر: `provision_worker`, `update_worker` (فیکس‌شده)، تونل SSH، `api_call`، health، `pick_worker_for_login`. مسیر آپدیت پیشرفته در bot.py است (پایین را ببین).
- `worker_api.py` — نود ورکر (FastAPI, Bearer token): `/health` `/login/*` `/channel/*` `/send/*` `/contacts/add` `/gen/*` `/broadcast/run` و ...
- `db.py` — SQLite (accounts, settings=app_settings key/value, workers, generator, broadcaster, ...). افزایشی؛ بازنویسی نکن.
- `rubika_client.py` — لایه Rubika (rubpy 7.3.5): login/contacts/create_channel/add_channel_members/find_marked_message/forward_message/send_text/get_contacts_full/seed_channel_with_contacts.
- `telegram_client.py` / `telegram_multi_send.py` / `telegram_multi_panel.py` — لایه تلگرام (Telethon).
- `account_conn.py` — Feature#6: یک اتصال گرم پایدار برای هر اکانت. `InvalidAuthError`, `call`, `verify_session_dead`, `close`, `is_invalid`, `reset_invalid`.
- `features.py` — اتوماسیون‌های محلی (منشی PV، گزارش کانال، پاسخ‌گو). **دست نخورد.**
- `brain_control.py` — کنترلر stop/pause ایزوله‌ی BRAIN.
- `portal/` — پنل پورتال + `observer.py` (Watcher canonical) + `panel.py` (پنل قرنطینه) + `post_login_send.py` (الگوی job/checkpoint: جدول `portal_send_jobs`).
- `crypto_util.py`, `status_summary.py`, `pdf_export.py`, `worker_transfer.py`.

---

## آپدیت‌های این نسخه (۴ بخش) — چه چیزی تغییر کرد

### ۱) فیکس ساخت/آپدیت ورکر — `worker.py` (فقط ۲ تابع)
- `provision_worker()`: نصب مقاوم Docker — توقف `unattended-upgrades`، `apt-get -o DPkg::Lock::Timeout=180`، نصب `docker.io` از مخزن توزیع + fallback به `get.docker.com`، سپس VERIFY (`DOCKER_OK`/`DOCKER_MISSING`) و خطای فارسی واضح در صورت نبود Docker.
- `update_worker()`: `git remote set-url origin` + `git fetch --depth 1` + `git checkout -B <branch> FETCH_HEAD` + `docker build --network=host` + recreate؛ شکستِ build به‌صورت exit غیرصفر برمی‌گردد.
- **منتقل نشد** (طبق نقشه، چیز اضافه نه): `master_code_version`, `worker_code_version`, `worker_transfer`, `collect_worker_sessions`.
- ⚠️ **دست‌نخورده ماند:** مسیر آپدیت فعال پیشرفته در `bot.py::_safe_worker_update_command` / `_safe_update_worker` (candidate-image build + version test + rollback). `worker.update_worker` call-site فعال ندارد؛ فیکسِ آن روی این مسیر اثر ندارد. `provision_worker` از `bot.py::provision_and_register` صدا زده می‌شود.

### ۲) مغز کانال (Channel Brain) — `bot.py` + `worker_api.py`
- جایگزین UI فعال broadcaster («🏭 موتور مولد» / `b"generator"`) شد → دکمه‌ی «🧠 مغز کانال» / `b"cbrain"`.
- منطق: **یک اکانت**، **N کانال**. برای هر کانال به ترتیب:
  `BUILD CONTACTS` (کشف تازه با `_discover_for_account`) → `CREATE CHANNEL` (بدون tag/username) → `ADD CONTACTS` (دقیقاً همان GUIDها) → `SEND CONTENT` (مارکر به کانال).
- مخاطب هر کانال **جدا و بدون تکرار** (ledger کشف `was_leeched`/`mark_leeched` تضمین می‌کند). عنوان همه‌ی کانال‌ها یکسان.
- **مقاوم:** try/except هر فاز و هر کانال؛ به هر دلیل متوقف نمی‌شود (فقط کارت خطا و ادامه).
- کارت لاگ زنده‌ی انگلیسی `| ⚙ - #channel` + نمودار؛ کارت پایانِ ثابت (آمار جامع)؛ کارت خطای جدا با کد دقیق.
- reuse محض primitiveها: `_discover_for_account`, `rb.create_channel`, `rb.add_channel_members`, `_send_to_guids`.
- config در `app_settings` (افزایشی): `cbrain_account_id`, `cbrain_title`, `cbrain_count`, `cbrain_per_channel`, `cbrain_prefix`. محتوا از `db.get_marker()`.
- **گسترش backward-compatible ورکر:** `/channel/create` فیلد `forward:bool=True` (وقتی `False` → فقط ساخت، بدون فوروارد مارکر). `/channel/add` فیلد `guids:list=None` (وقتی داده شود → دقیقاً همان‌ها دسته‌ای اضافه می‌شوند). رفتار پیش‌فرض و جریان تک‌کانال قدیمی تغییر نکرد.
- جدول‌های broadcaster در `db.py` **حذف نشدند** (فقط UI جایگزین شد؛ توابع db بی‌استفاده اما سالم).
- **persistence جدید نساختم** (طبق «چیز جدید خلق نکن»): مثل broadcaster/brain، runner درون‌حافظه‌ای و مقاوم است، بدون resume بین restart. (اگر بعداً resume خواستی → الگوی `portal_send_jobs` در `post_login_send.py`.)

### ۳) کارت‌های لاگ انگلیسی (scoped) — `bot.py`
- تبدیل‌شده به انگلیسی: کارت‌های **ارسال روبیکا** (`run_send` + `run_send_remote`: STARTED/PROGRESS هر `SEND_LOG_EVERY=50` بدون تغییر cadence/COOLDOWN/STOPPED/FINISHED/RESUME) و **Brain** (۱۸ کارت). مقادیر `reason` هم انگلیسی شد (فقط نمایش؛ هیچ‌جا با رشته مقایسه نمی‌شود).
- فقط **متنِ نمایش**؛ منطق/cadence/مقادیر f-string دست‌نخورده.
- **باقی‌مانده (هنوز تبدیل نشده، منتظر تأیید کاربر):** کارت‌های MULTI SEND، ردیف‌های CONTACT IMPORT، کارت زنده‌ی discovery، کارت‌های تلگرام، مدیریت ورکر، بکاپ.
- **مستثنی (دست نخورد):** اتوماسیون روبیکا (secretary/channelreport/reply/linkdooni/profile-sync/group-join)، prompt‌های منوی فارسی، DM‌های toast به مالک.

### ۴) ادغام موتور سلامت با Watcher — `bot.py` + `portal/observer.py`
- `run_health_engine` = Watcher واحد اکانت: verify هر اکانت؛ dead → quarantine **canonical** از `observer._remove_confirmed_invalid` (double-verify، stop jobs، snapshot/disable اتومیشن، status=quarantined) به‌جای auto-inactive قدیمی؛ alive → restore + self-heal اتومیشنِ متوقف‌شده. **بدون auto-delete.**
- offline worker → اکانت‌هایش **UNCHECKED** (هرگز Shot).
- یک کارت انگلیسی `🩺 #watcher_health` + دکمه‌ی `[🗑 Delete Shot Accounts (N)]` → callback موجود `portal_quarantine` (پنل recheck/relogin/delete-with-owner-confirm).
- sweep دستی (`run_accounts_sweep`) → همان cycle را trigger می‌کند (batch-delete قدیمی دیگر پیشنهاد نمی‌شود).
- **جدا/دست‌نخورده:** `health_loop` (سلامت **ورکر**)، حلقه‌ی `observer.run` پورتال (cleanup ورود + quarantine مکرر canonical — idempotent با health engine، اکانت قرنطینه‌شده دوباره قرنطینه نمی‌شود).

---

---

## آپدیت این نشست — فیکس مغز کانال + نمایش ورکر + حذف کد مرده
> شاخه: `fix/channel-brain-worker-display` (از `feat/build-from-meow@dcbc0aa`).
> فقط ۲ فایل کد تغییر کرد: `bot.py`, `worker_api.py` (+ همین فایل نقشه).
> اصل کار: «منطق اصلی دست نخورد، فقط صحت/تحمل‌خطا بهتر شد و نمایش حرفه‌ای‌تر».

### ۱) فیکس صحت/تحمل‌خطای مغز کانال — `bot.py` + `worker_api.py`
- **باگ قبلی:** runner نتیجه‌ی واقعی ADD/SEND را مصرف نمی‌کرد؛ حتی با marker گمشده یا batch شکست‌خورده، کارت `CHANNEL DONE ✅` و `made += 1` ثبت می‌شد (موفقیت جعلی). خطای هر batch در `/channel/add` و `_cbrain_add_exact` بی‌صدا بلعیده می‌شد. CREATE بدون retry بود. runner در `try/finally` بیرونی نبود → احتمال نشتِ `cbrain_jobs`/live task.
- **`worker_api.py::/channel/add`** (افزایشی، backward-compatible): مسیر `guids` دیگر batch شکست‌خورده را موفق حساب نمی‌کند؛ `failed_batches` می‌شمارد و علاوه بر `ok`/`added`، کلیدهای `requested`/`accepted`/`failed`/`failed_batches` هم برمی‌گرداند. مسیر بدون `guids` (`seed_channel_with_contacts`) **دست‌نخورده**. کالرهایی که فقط `ok`/`added` می‌خوانند بی‌اثر می‌مانند.
- **`bot.py::_cbrain_add_exact`**: حالا dict `{requested, accepted, failed_batches}` برمی‌گرداند (parity کامل local/worker). با worker **قدیمی** که فقط `ok`/`added` دارد graceful است (`accepted <- added`, `failed_batches <- 0`).
- **`bot.py::_run_channel_brain`** بازنویسی شد:
  - کل حلقه در `try/except/finally`؛ `finally` همیشه `ctl["finished"]=True`، cancel live task، `cbrain_jobs.pop`، و ارسال کارت پایانی (حتی روی استثنای غیرمنتظره → کارت `PHASE: RUNNER`).
  - **CREATE retry:** `1 + config.RESUME_MAX_RETRIES` تلاش (پیش‌فرض ۳)، فاصله `config.CHANNEL_ADD_DELAY`، با همان GUIDهای frozen (بدون کشف دوباره). **config جدیدی ساخته نشد.**
  - مصرف واقعی `(ok, fail)` از `_send_to_guids`.
  - وضعیت صادقانه‌ی هر کانال با helper جدید `_cbrain_result_card`: `COMPLETED` فقط وقتی `built==target` و همه‌ی requested پذیرفته شده و `send ok>=1/fail==0`؛ وگرنه `PARTIAL`؛ `FAILED` فقط برای build-exception یا شکست CREATE بعد از همه‌ی retryها. **دیگر `CHANNEL DONE` جعلی نیست.**
  - `InvalidAuth` هر فاز = توقف کنترل‌شده‌ی کل run (final = SESSION INVALID).
  - final card: شمارش `COMPLETED/PARTIAL/FAILED` + built/accepted/sent واقعی.
- **⚠️ trade-off شناخته‌شده (عمدی، طبق نقشه):** چون `/channel/create` idempotent نیست، اگر create روی سرور موفق شود ولی پاسخش timeout/قطع شود، retry یک **کانال تکراری** (هم‌عنوان، بدون عضو) می‌سازد. ریسک کم و بی‌ضرر است. اگر روزی خواستی صفرش کنی: retry را روی خطاهای مبهم (timeout/network) gate کن — ولی این تغییرِ منطق است و نیاز به تأیید دارد.
- **دست‌نخورده:** ترتیب `BUILD→CREATE→ADD→SEND`، منبع محتوا (`db.get_marker()`)، discovery/ledger، هیچ DB/schema/persistence جدید. `accepted` = «پذیرفته‌شده توسط add API»، نه شمارش عضو واقعی (primitive واقعی نداریم؛ در کامنت‌ها تصریح شده).

### ۲) نمایش ورکر — صرفاً presentation انگلیسی/حرفه‌ای — `bot.py`
- helperهای جدید فقط-نمایشی (بعد از `_ping_text`): `_wk_type` (MASTER/REMOTE)، `_wk_state` (DISABLED/UPDATING/ACTIVE/BLOCKED/OFFLINE/UNKNOWN از `enabled`/`_worker_updating_ids`/`status`)، `_wk_route` (OK/BLOCKED/UNCHECKED از `file_ok`/`status`)، `_wk_status_block`.
- بازنویسی نمایشی: `worker_status_all_card`, `added_worker_card`, لیبل دکمه‌های `workers_cb`, `wk_detail_cb`, `w_versions_cb`, و پیام‌های کاربرپسند `provision_and_register`/`wk_update_cb`/`_update_all_workers`/`w_updall_cb`/timeout در `_safe_update_worker`.
- **هیچ منطقی تغییر نکرد:** `_safe_worker_update_command` **byte-identical** (با اسکریپت مقایسه تأیید شد)، قرارداد بازگشتی updater (`updated`/`current`/`failed`) و markerهای STAGE و parsing دست‌نخورده، همه‌ی `callback_data` عیناً یکسان (فقط لیبل/متن پنل)، health/selection/routing دست‌نخورده. promptهای ورودی و دکمه‌های action فارسی باقی ماندند.

### ۳) حذف کد قطعاً مرده — `bot.py`
- حذف‌شده: متغیر `pending_dead_accounts`، callback `b"acc_sweep_del"`، تابع `accounts_sweep_delete_cb`. (هیچ دکمه‌ای این callback را تولید نمی‌کرد و متغیر فقط داخل همین تابع مرده pop می‌شد.)
- **حذف نشد (زنده):** `acc_sweep`/`run_accounts_sweep`، `portal_quarantine` و delete canonical، `health_engine_loop`/`run_health_engine`، `observer.run`، جدول‌ها/helperهای generator/broadcaster در `db.py`، `/broadcast/run` و `/gen/*`.
- **۴ lint ارثی base** (دو F541، دو import تلگرام) عمداً دست‌نخورده (خارج scope).

### روش تست این آپدیت (بدون افزودن test-file به ریپو)
- شبکه‌ی سندباکس `INTEGRATIONS_ONLY` است؛ `telethon`/`rubpy`/`dotenv` نصب نیستند. برای import و تست runner واقعی، **stub سبک** ساخته شد در `/projects/sandbox/_stubs` (خارج ریپو): `dotenv.py`, `rubpy/`, `telethon/`.
- اجرا: `PYTHONPATH=/projects/sandbox/_stubs:<repo> API_ID=1 API_HASH=x BOT_TOKEN=1:AAA OWNER_ID=1 python3 ...`.
- هارنس‌ها (خارج ریپو): `test_cbrain.py` (۳۸ سناریوی runner: success/partial/failed/retry/InvalidAuth/stop/unexpected/cleanup)، `test_addexact.py` (۸ تست parity worker/local + backward-compat)، `test_worker_display.py` (۲۰ تست mapping/cards). همه PASS.
- گیت‌ها: `python3 -m compileall`، `ruff --select F821,F811,F823` (پاک)، grep نبود کد مرده و حفظ نمادهای زنده، git diff allowlist (فقط ۲ فایل کد)، semantic review.

---

---

## آپدیت این نشست (۲) — ارسال «متن ساده» + صفحه‌بندی لیست اکانت‌ها
> همان شاخه `fix/channel-brain-worker-display`. فقط `bot.py` + `worker_api.py` (+ این نقشه).

### ۱) ارسال «متن ساده» (بدون فوروارد) به مخاطبین
- **ایده:** یه متنِ ساده (بدون نیاز به پیام نشان‌دار) به همهٔ مخاطبین. **موتورش از قبل کامل آماده بود** — فقط به UI وصل شد:
  - محلی: `bot.run_send` با `mode="text"` + `payload["text"]` (از قبل بود).
  - ریموت مغز: `/send/to_list` با `mode="text"` (از قبل بود).
- **منبع متن:** setting مستقل `rb_plain_text` (کلید app_settings). helperها در bot.py: `get_plain_text()`/`set_plain_text()`. از بخش «📌 مارکر» → دکمهٔ «📝 متن ساده» ست/پاک می‌شه (`plaintext` callback + step `await_plain_text`). **کاملاً جدا از پورتال؛ `portal_auto_send_text` و کل پورتال دست‌نخورده.**
- **ارسال عادی:** منوی `send_mode_cb` حالا سه گزینه دارد: «📎 فوروارد مارکر» (`send_{id}`، بدون تغییر)، «✍️ متن ساده» (`sendtext_{id}`، جدید)، «📢 کانال» (بدون تغییر). هندلر `send_text_prepare_cb` محلی recipients را می‌خواند و payload با `mode="text"` می‌سازد؛ ریموت از `send_text_prepare_remote` استفاده می‌کند.
- **مغز:** `brain_send_cb` حالا می‌پرسد «📎 فوروارد مارکر» (`bsendgo`) یا «✍️ متن ساده» (`bsendgotext`، جدید). `_run_brain_send(owner_id, job, mode="marker", body="")` گسترش یافت: در حالت text محلی run_send(mode=text) و ریموت /send/to_list(mode=text)؛ در حالت marker دقیقاً مثل قبل.
- **worker_api (additive، پیش‌فرض `marker` → رفتار قبلی byte-preserved):**
  - `PrepareIn.mode` + `prepare`: در حالت text مارکر لازم نیست، فقط recipients شمرده می‌شود.
  - `SendIn.mode`+`SendIn.text` + `send_start`: در حالت text `find_marked_message` رد می‌شود (saved_guid/mid=None).
  - `_run_send`: شاخهٔ `if mode=="text": rb.send_text else rb.forward_message` (عین الگوی موجود `/send/to_list`).
  - `run_send_remote` (bot): `mode`/`text` را پاس می‌دهد و در حالت text شرط `marker_found` را نادیده می‌گیرد.

### ۲) صفحه‌بندی لیست اکانت‌ها
- helper `_paginate(items, page, cb_prefix, per_page=15)`: هر صفحه ۱۵ دکمه، صفحهٔ اول بدون «◀️ قبلی»، صفحهٔ آخر بدون «بعدی ▶️».
- اعمال شد روی: منوی ارسال (`send_menu` → `_render_send_menu` + callback `smpage_`) و لیست «👤 اکانت‌های من» (`accounts` → `_render_accounts` + callback `accpage_`). شماره‌گذاری اکانت‌ها بین صفحات پیوسته می‌ماند. `ACC_PAGE_SIZE=15`.

### تست
- ۹۰ تست PASS (۳۸ مغز کانال + ۸ parity + ۲۰ نمایش ورکر + ۱۷ متن‌ساده/مغز + ۷ worker `_run_send`). گیت‌ها: compileall، ruff `F821/F811/F823`، `_safe_worker_update_command` byte-identical، مسیر مارکرِ فعلی دست‌نخورده. هارنس‌ها: `/projects/sandbox/_stubs/test_sendtext.py`, `test_worker_send.py`.

---

## نکات تداخل سشن (مهم برای آپدیت بعدی)
- یک اتصال زنده برای هر session؛ قبل از اتصال جدید، قبلی بسته شود (`account_conn.close`).
- حذف اکانت فقط با InvalidAuth قطعی و **تأیید صریح مالک** (پنل قرنطینه). timeout/شبکه/FloodWait/Worker unavailable = خطای موقت، هرگز حذف/قرنطینه‌ی قطعی.
- دو نمونه‌ی هم‌زمان master اجرا نشود.
- runnerهای درون‌حافظه‌ای (مغز کانال، brain، broadcaster قدیمی) با restart از دست می‌روند — این عمدی است.
- **مغز کانال تک‌اجراست:** `cbrain_jobs[owner_id]` به‌عنوان قفل عمل می‌کند؛ تا وقتی پاک نشود اجرای دوم شروع نمی‌شود. بعد از این آپدیت، `finally` تضمین می‌کند این قفل و live task **همیشه** آزاد شوند (حتی روی خطای غیرمنتظره)، پس دیگر «مغز گیرکرده» رخ نمی‌دهد.
- **stubهای تست** (`/projects/sandbox/_stubs`) عمداً خارج ریپو هستند تا با کد پروژه commit نشوند؛ اگر سشن/کلونِ تازه گرفتی و خواستی دوباره تست کنی، طبق «روش تست این آپدیت» بالا بازشان بساز.

## ⚠️ نکته‌ی deploy (اجباری قبل از اجرا)
`config.GIT_REPO_URL` پیش‌فرض = `https://github.com/willbedoneuw/YoudonoaAx` و `GIT_BRANCH=main`
(در `config.py` و `.env.example`). این‌ها ورکرها از آن‌جا clone/update می‌شوند. **این کد را عمداً تغییر ندادم** (خارج از scope). موقع deploy در `.env` باید به ریپو/برنچِ درستِ کد deploy‌شده (این Makiioo یا هرجا) ست شود، وگرنه ورکرها کد اشتباه می‌گیرند.

## قواعد پایه (از CORE_RULES ارثی، همچنان معتبر)
- منطق پایه‌ی اتصال/session/معماری را بازنویسی نکن.
- تغییرات افزایشی و ایزوله؛ کد جدید حداقلی، حداکثر reuse از کد خود پروژه.
- push مستقیم به main/production ممنوع؛ فقط شاخه‌ی جدید + PR.
- بدون افزودن تست‌فایل؛ صحت‌سنجی با `python -m compileall` + smoke دستی.

## وضعیت آپدیت‌ها
- شاخه‌ی قبلی: `feat/build-from-meow`. کامیت‌ها: base → worker fix → channel-brain → english-logs → watcher-merge.
- شاخه‌ی این نشست: `fix/channel-brain-worker-display` (از `dcbc0aa`). سه بخش: فیکس مغز کانال + نمایش ورکر + حذف کد مرده. فقط `bot.py`/`worker_api.py` (+ این نقشه).
- صحت‌سنجی این نشست: compileall پاک، ruff `F821/F811/F823` پاک، `_safe_worker_update_command` byte-identical، ۶۶ تست (۳۸+۸+۲۰) PASS، grep دیف allowlist، semantic review سبز (تنها نکته: trade-off عمدیِ retry تکراری‌سازی کانال، بالا توضیح داده شد).
