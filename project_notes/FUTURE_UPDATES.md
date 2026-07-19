# موارد آینده ثبت‌شده در طرح

ترتیب آینده مصوب:

1. backup، ثبت rollback commit و MD5 هسته
2. اصلاح lifecycle پورتال و Custom Domain
3. ساخت storage و event model پورتال
4. پیاده‌سازی `attempt_id` و cleanup پنج‌دقیقه‌ای
5. اتصال UI تأییدشده به API واقعی
6. آمار، پنل پورتال و کارت خلاصه `/start`
7. کارت‌های گزارش کامل
8. ناظر محدود برای وضعیت پورتال و حذف session نامعتبر قطعی
9. متن خودکار پس از ورود با sender موجود
10. ارسال چنداکانتی تلگرام با صف مشترک، failover، cooldown و checkpoint
11. تست failure، concurrency و restart؛ سپس syntax check و کنترل MD5 هسته
12. push روی شاخه جدید، PR به `production` و deployment امن پس از merge

ایده‌های قابلیت «مغز»، بازطراحی کلی ربات و قابلیت‌های خارج از طرح جزو این آپدیت نیستند.