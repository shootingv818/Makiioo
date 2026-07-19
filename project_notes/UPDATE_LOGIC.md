# منطق آپدیت و نقاط اتصال

این آپدیت در سند مصوب ثبت شده و هنوز پیاده‌سازی نشده است. منطق آن افزایشی و ایزوله است:

1. lifecycle پورتال و دامنه اصلاح شود: ابتدا server و `/ping`، سپس tunnel و بررسی DNS/SSL و `https://domain/ping`؛ وضعیت فقط با پاسخ واقعی `running` شود.
2. ورود پورتال با normalize شماره، کنترل duplicate و ظرفیت اتمیک، `attempt_id` یکتا و TTL دقیق ۳۰۰ ثانیه انجام شود.
3. endpointهای start/password/code/resend از gating، TTL و ownership مشترک استفاده کنند؛ cleanup مستقل client، session، lock و context منقضی را پاک کند.
4. کد اشتباه، timeout، نیاز به رمز، قطعی شبکه و خطای داخلی از هم تفکیک شوند؛ آمار بر پایه attempt یکتا ثبت شود.
5. ورود موفق فقط پس از ذخیره کامل account، session، تعداد مخاطبان و Worker نهایی شود؛ خطای log نباید ذخیره موفق را ناموفق کند.
6. UI مرجع `archive/portal_ui_final.html` بدون بازنویسی جریان JavaScript به API واقعی متصل شود.
7. متن خودکار پس از ورود و ارسال چنداکانتی از منطق sender، Worker، checkpoint، توقف/ادامه و تنظیمات موجود استفاده کنند.

## نقاط اتصال پیشنهادی سند

- `portal/stats.py`: attempt، event، آمار روزانه/کل و نرخ موفقیت
- `portal/observer.py`: cleanup پنج‌دقیقه‌ای و حذف فقط برای invalid session قطعی
- `portal/post_login_send.py`: اتصال پورتال به sender موجود
- `portal/status.py`: وضعیت واقعی server/tunnel/domain و cache خلاصه
- `telegram_multi_send.py`: صف، cooldown، failover و checkpoint تلگرام
- `status_summary.py`: کارت کوتاه `/start` از cache
- `bot.py`: فقط hook، دکمه یا route کوچک و مشخص
- DB موجود: فقط جدول‌های اختصاصی ماژول‌های جدید، بدون بازنویسی `db.py`