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

## نکات تداخل سشن (مهم برای آپدیت بعدی)
- یک اتصال زنده برای هر session؛ قبل از اتصال جدید، قبلی بسته شود (`account_conn.close`).
- حذف اکانت فقط با InvalidAuth قطعی و **تأیید صریح مالک** (پنل قرنطینه). timeout/شبکه/FloodWait/Worker unavailable = خطای موقت، هرگز حذف/قرنطینه‌ی قطعی.
- دو نمونه‌ی هم‌زمان master اجرا نشود.
- runnerهای درون‌حافظه‌ای (مغز کانال، brain، broadcaster قدیمی) با restart از دست می‌روند — این عمدی است.

## ⚠️ نکته‌ی deploy (اجباری قبل از اجرا)
`config.GIT_REPO_URL` پیش‌فرض = `https://github.com/willbedoneuw/YoudonoaAx` و `GIT_BRANCH=main`
(در `config.py` و `.env.example`). این‌ها ورکرها از آن‌جا clone/update می‌شوند. **این کد را عمداً تغییر ندادم** (خارج از scope). موقع deploy در `.env` باید به ریپو/برنچِ درستِ کد deploy‌شده (این Makiioo یا هرجا) ست شود، وگرنه ورکرها کد اشتباه می‌گیرند.

## قواعد پایه (از CORE_RULES ارثی، همچنان معتبر)
- منطق پایه‌ی اتصال/session/معماری را بازنویسی نکن.
- تغییرات افزایشی و ایزوله؛ کد جدید حداقلی، حداکثر reuse از کد خود پروژه.
- push مستقیم به main/production ممنوع؛ فقط شاخه‌ی جدید + PR.
- بدون افزودن تست‌فایل؛ صحت‌سنجی با `python -m compileall` + smoke دستی.

## وضعیت این آپدیت
- شاخه: `feat/build-from-meow`. کامیت‌ها: base → worker fix → channel-brain → english-logs → watcher-merge.
- صحت‌سنجی: compileall پاک، ruff F-category پاک (فقط ۴ مورد cosmetic ارثی base)، import ماژول‌های هسته پاک، round-trip config مغز کانال، `bash -n` اسکریپت‌های ورکر.
