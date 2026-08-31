# Berserk VPN v5.2-commerce-core

این نسخه شامل موارد زیر است:

- تکمیل خرید ۱ تا ۴ عدد
- خرید عمده به صورت تیکت برای ادمین
- شناسه اختصاصی برای هر سرویس مثل `Berserk A7K92Q`
- نمایش بهتر «اشتراک‌های من»
- جزئیات خرید هر کاربر داخل بخش کاربران پنل ادمین
- migration ایمن دیتابیس
- لاگ خرید پایه و ledger مالی

## فایل‌ها
همه فایل‌های Python اصلی تمیز و چندخطی هستند. فایل‌های قبلی را با این فایل‌ها جایگزین کن.

## نصب
1. فایل‌ها را در GitHub آپلود و Commit کن.
2. در Railway حتما Deploy Latest Commit بزن.
3. فقط یک Instance از ربات با توکن روشن باشد.

## Variables مهم
```env
BOT_TOKEN=...
ADMIN_ID=123456789
ADMIN_COMMAND=panel_x7k9
DB_PATH=/data/berserk.db
```

`DB_PATH` اختیاری است، ولی برای Railway Volume بهتر است روی `/data/berserk.db` باشد.
