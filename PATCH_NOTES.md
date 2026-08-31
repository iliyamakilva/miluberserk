# Berserk v5 Patch Stabilization 01

این بسته rewrite نیست؛ برای ترمیم فایل‌های فعلی GitHub آماده شده که در Raw به‌صورت فشرده/خراب دیده می‌شوند.

## فایل‌های جایگزین‌شده

- config.py
- menus.py
- db.py
- subs.py
- wallet.py
- tickets.py
- admin.py
- bot.py
- requirements.txt

## تغییرات فایل به فایل

### config.py
- خواندن BOT_TOKEN و ADMIN_ID از ENV
- اضافه شدن DB_PATH برای Railway Volume
- validate واضح برای ENVهای ضروری

### menus.py
- منوی ثابت پایین
- دکمه بازگشت به منوی اصلی
- دکمه بازگشت به پنل مدیریت

### db.py
- فرمت صحیح Python
- استفاده از DB_PATH
- حفظ جدول‌های users/subs/topups/receipts/tickets/messages
- migration ایمن price_paid/account_name

### subs.py
- حفظ pool ساب‌لینک
- هر لینک یک‌بار مصرف
- جلوگیری از crash در لینک تکراری
- شناسه Berserk برای هر لینک

### wallet.py
- fix نهایی process_receipt
- بعد از ارسال رسید، منوی پایین برمی‌گردد
- تایید/رد رسید هم منو را برای کاربر برمی‌گرداند

### tickets.py
- بعد از ارسال تیکت، منوی پایین برمی‌گردد
- جواب ادمین هم منوی پایین را حفظ می‌کند
- دکمه بازگشت در تیکت‌های ادمین

### admin.py
- پنل مدیریت کامل‌تر با back button
- جستجوی کاربر + نمایش ساب‌های خریداری‌شده همان کاربر
- تنظیمات، بکاپ، ریستور، شارژها، تیکت‌ها

### bot.py
- منوی پایین + inline خرید
- خرید ۱ تا ۴ عدد
- خرید عمده به تیکت
- برگشت‌های استاندارد
- error handler با برگشت منو
