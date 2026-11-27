---
inclusion: always
---

# Makiioo — قواعدِ پروژه (هر نشست اول این را بخوان)

> مکملِ `bnaoao-update-context.md`. وضعیتِ کاملِ کارِ در جریان در
> **`project_notes/SESSION_HANDOFF.md`** است — آن را هم بخوان.

## شناسنامه

| | |
|---|---|
| ریپو | `shootingv818/Makiioo` — https://github.com/shootingv818/Makiioo |
| شاخهٔ کاری | `feat/caption-file-and-panel` |
| PR | **#6** → پایه `refactor/rubika-only-structure` |
| سرور | `/opt/rubika-panel` · سرویس `rubika-panel` |
| مفسر | `/opt/rubika-panel/venv/bin/python` ← **نه** `python3` سیستم (dotenv ندارد) |
| ورکرها | Docker، کانتینر `v2rubby-worker` · SSH با asyncssh + HTTP API |
| پلتفرم | فقط روبیکا (تلگرام حذف شده) · `rubpy==7.3.5` |

### deploy
```bash
cd /opt/rubika-panel && git fetch origin feat/caption-file-and-panel \
  && git reset --hard origin/feat/caption-file-and-panel \
  && systemctl restart rubika-panel
```
بعدش از پنل: **⚙ Settings → 🛠 Workers → Update All**
تغییرِ فقط-اسکریپت ری‌استارت لازم ندارد. کاربر root است، `sudo` لازم نیست.

> هر دستوری که به مالک می‌دهی و به کدِ تازه پوش‌شده نیاز دارد، **باید
> `git fetch` را در همان خط داشته باشد.** یک بار این را جا انداختم و یک
> رفت‌وبرگشت هدر رفت.

---

## قواعدِ مالک (نقض نکن)

1. **اول دقیق بررسی کن، بعد بساز.** یک بار روی فرضِ تست‌نشده فیچر ساخته شد
   (`forward_privacy`) و بعد از تستِ مالک کامل حذف شد. فرضِ تست‌نشده = ممنوع.
2. **کدِ جدید خلق نکن — از منطقِ خودِ سورس استفاده کن.** الگویی که لازم داری
   جایی در پروژه هست؛ پیدایش کن.
3. **زیاد تست کن، بعد تحویل بده.**
4. **فایل‌های pinned دست نخورند.**
5. **«کار می‌کند» بدونِ شاهدِ واقعی ممنوع.** تفکیک کن: اندازه‌گیری‌شده /
   استنباط / نامعلوم. اگر تست نکرده‌ای، صریح بگو.
6. اسکریپت‌های آزمایشی **نباید روی رباتِ اصلی اثر بگذارند** و به ربات وصل نشوند.
7. پاسخ **فارسی**، کوتاه و مستقیم. **یک خط دستور** بده، نه ده خط.
8. اگر اشتباهی که قبلاً گفتی غلط بود، **خودت فوراً تصحیح کن** — حتی کامنتی که
   در کد نوشته شده.

---

## فایل‌های pinned — byte-identical بمانند

```
core/            rubika/rubika_client.py    workers/worker.py
main.py          config.py                  portal/
rubika/features.py                          rubika/login_code.py
```
تأیید (خروجیِ خالی = درست):
```bash
git diff --stat origin/refactor/rubika-only-structure -- core/ \
  rubika/rubika_client.py workers/worker.py main.py config.py portal/ \
  rubika/features.py rubika/login_code.py
```
رفتارِ درست: ماژولِ ایزولهٔ نو بساز که اینها را **صدا بزند**. مشکلِ مسیرِ داده
با symlink در Dockerfile حل شد، نه با ویرایشِ `db.py`.

---

## قاعدهٔ طلاییِ session

```
AUTH_FROM_ANOTHER  =  یک session، دو اتصالِ زنده
```
- `core/account_conn.py:87` این خطا را «session باطل» می‌شناسد.
- `/session/import` عمداً **write-only** است (`session.insert`، بدونِ connect).
- قبل از هر اتصال: `await account_conn.close(phone)`.
- یک اکانت را **هرگز** از دو جا همزمان وصل نکن.
- فایلِ session با **glob** پیدا می‌شود، نه `os.path.exists`:
  `glob.glob(rb.session_path(phone) + "*")` (ایدیومِ `portal/observer.py:228`) —
  چون rubpy پسوندِ خودش را اضافه می‌کند.

---

## تله‌های دیده‌شده (تکرار نکن)

| تله | واقعیت |
|---|---|
| `pydantic==2.6.4` | `x: bool = None` و ارسالِ `None` → ۴۲۲. الگوی درست: `action: str = "read"` |
| `getContacts` | **لیبلِ خودِ مالک** را می‌دهد، نه اسمِ طرف. `status` هرگز نمی‌آید (۰ از ۵۴۳) |
| `last_online` ماسک‌شده | روی شبکهٔ ۳۰ دقیقه‌ای رُند است → فقط به برچسبِ سطل اعتماد کن (`Recently`/`WeekAgo`/`MonthAgo`/`LongAgo`) |
| `link_forward_message` | **رد شد با تستِ واقعی**: setSetting موفق، برچسبِ فوروارد ماند |
| پنهان‌کردن نام فرستنده | هیچ پارامتر API ندارد. اپ هم کپی می‌کند نه فوروارد → `copy_send.py` |
| `file_inline` | بی‌نهایت بار قابلِ استفاده بدونِ آپلودِ مجدد |
| مسیرِ venv | `venv/bin/python` یک symlink به پایتونِ سیستم است → با `abspath` مقایسه کن نه `realpath` |

---

## استانداردِ تست

```bash
export PYTHONPATH=/projects/sandbox/_stubs:/projects/sandbox/Makiioo/rubika:/projects/sandbox/Makiioo/status
python3 _stubs/test_copysend.py  _stubs/test_worker_contract.py \
        _stubs/test_send_order.py  _stubs/test_health.py \
        _stubs/test_capfile.py  _stubs/test_integration_static.py \
        _stubs/test_scope.py
python3 scripts/send_order_lab.py  --selftest
python3 scripts/name_render_lab.py --selftest
python3 scripts/fleet_render_lab.py --selftest
```
**~۶۴۲ assertion، صفر خطا.** دو تا سه دور اجرا کن و دیفِ pinned را نشان بده.

### دو درسِ متدولوژیک (گران تمام شد)
1. **تستی که کپیِ منطق را می‌سنجد بی‌ارزش است** — همیشه تابعِ واقعی را تست کن.
   پارسرِ `ExecStart` با نسخهٔ دومِ منطق تست شده بود: سبز، و کدِ واقعی خراب.
2. **هر گارد باید با نمونهٔ متخلف ثابت شود.** گاردی که شلیک نمی‌کند گارد نیست.

---

## اسکریپت‌های `scripts/` (مستقل، فقط‌خواندنی، به ربات وصل نیستند)

| اسکریپت | کار |
|---|---|
| `send_order_lab.py` | پروبِ ترتیب ارسال · `--selftest` `--probe` `--verify` |
| `name_render_lab.py` | رندرِ اسمِ مخاطب + تطبیقِ دو منبعی · `--selftest` `--list` `--render` |
| `fleet_render_lab.py` | ارکستراتور: مستر → ورکرها با SSH + `docker exec` · `--selftest` `--plan` `--run` |

هر دو اسکریپتِ رندر یک **گاردِ AST** دارند که در `--selftest` اجرا می‌شود و
fail می‌دهد اگر: ارسالی در دسترس باشد · در DB نوشته شود · `api_call` با
غیرِ `GET` · عملیاتِ lifecycle ورکر · `os.system`/`Popen` · `execv` به چیزی
غیر از یک `.py` داخلِ ریپو · patchِ `sys.modules` · `exec` بدونِ `flush`.
**هر گارد با نمونهٔ متخلف تست شده.**

---

## الگوهای کدِ قابلِ استفادهٔ مجدد

```python
# SSH به ورکر (یک‌باره، مثل update)
conn = await worker._with_conn(w, keepalive=False)
rc, out, err = await worker._run(conn, "docker ps")
sftp = await conn.start_sftp_client()

# فراخوانیِ API ورکر
res = await worker.api_call(w, "GET", f"/automation/status?phone={ph}", timeout=25)

# ترتیبِ گیرنده‌ها (با fallback گارد‌شده)
ordered, stats = await send_order.ordered_recipients(client)

# فراخوانیِ خامِ API روبیکا (rubpy wrap نکرده)
r = await client.builder(name="getUserInfo", input={"user_guid": g}, dict=True)

# path shim برای اسکریپت‌های scripts/
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "core", "rubika", "workers", "bot_app", "status"): ...
```

---

## GitHub در این محیط

```bash
git push                                      # ✅
gh api repos/{owner}/{repo}/pulls -f ...      # ✅ ساختِ PR
gh pr create / gh pr view / gh issue          # ❌ GraphQL، همیشه fail
gh api "repos/shootingv818/Makiioo/pulls?state=all&per_page=10"
```
