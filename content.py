"""Unified customer-facing content, templates, display settings and funnel events.

v6.4 keeps Telegram actions and callbacks in code while allowing administrators to
edit only presentation: text, media, parse mode and safe button labels. Templates
are draft-first, versioned and inherited as plan -> category -> global -> source.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable

import db

SCHEMA_VERSION = 640
TEXT_LIMIT = 3900
CAPTION_LIMIT = 1024
BUTTON_LIMIT = 64
PARSE_MODES = {"plain": None, "html": "HTML", "markdownv2": "MarkdownV2"}
SCOPE_GLOBAL = "global"
SCOPE_CATEGORY = "category"
SCOPE_PLAN = "plan"


@dataclass(frozen=True)
class ContentDefinition:
    key: str
    title: str
    category: str
    default_text: str
    fields: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    scopes: tuple[str, ...] = (SCOPE_GLOBAL,)
    kind: str = "text"  # text | button
    default_parse_mode: str = "plain"
    allow_media: bool = True
    help_text: str = ""


DEFINITIONS: tuple[ContentDefinition, ...] = (
    # Main / system
    ContentDefinition("welcome", "پیام خوش‌آمد", "main", "⚡ به Berserk VPN خوش آمدید\n\nسرویس پرسرعت و مطمئن شما فقط چند لمس فاصله دارد. از منوی پایین شروع کنید 👇"),
    ContentDefinition("main_menu", "پیام منوی اصلی", "main", "⚡ Berserk VPN آماده است\n\nاز منوی پایین، سرویس موردنظرتان را انتخاب کنید. برای شروع دوباره نیازی به ارسال /start نیست."),
    ContentDefinition("fallback", "پیام دستور نامفهوم", "main", "🤔 متوجه درخواست شما نشدم.\n\nاز منوی پایین استفاده کنید یا /start را بزنید."),
    ContentDefinition("account_banned", "پیام حساب مسدود", "main", "⛔ دسترسی این حساب محدود شده است.\n\nبرای بررسی بیشتر، از بخش پشتیبانی با ما در ارتباط باشید."),
    ContentDefinition("stale_action", "پیام دکمه منقضی", "main", "⌛ این دکمه دیگر فعال نیست. لطفاً از منوی اصلی دوباره وارد بخش موردنظر شوید."),
    ContentDefinition("generic_error", "خطای عمومی مشتری", "main", "⚠️ مشکلی پیش آمد؛ اطلاعات شما از بین نرفته است.\n\nلطفاً دوباره تلاش کنید یا از منوی اصلی ادامه دهید."),
    ContentDefinition("bot_disabled", "پیام خاموش بودن ربات", "main", "🛠 ربات موقتاً در حال بروزرسانی است.\n\nلطفاً کمی بعد دوباره امتحان کنید."),
    ContentDefinition("sales_closed", "پیام بسته بودن فروش", "buy", "⏸ فروش سرویس موقتاً متوقف شده است.\n\nسرویس‌های فعلی شما بدون مشکل فعال می‌مانند. به‌محض بازشدن فروش اطلاع‌رسانی می‌کنیم."),
    ContentDefinition("wallet_menu", "صفحه کیف پول", "payment", "💳 کیف پول شما\n\n{body}", ("body",)),
    ContentDefinition("wallet_amount_prompt", "درخواست مبلغ شارژ", "payment", "💳 افزایش موجودی کیف پول\n\nمبلغ موردنظر را فقط به‌صورت عدد ارسال کنید.\nحداقل مبلغ شارژ: {min_amount}", ("min_amount",)),
    ContentDefinition("wallet_payment", "اطلاعات پرداخت شارژ", "payment", "💳 پرداخت شارژ کیف پول\n\n💰 مبلغ: {amount}\n\nشماره کارت:\n{card_number}\nبه نام: {card_holder}\n\nبعد از واریز، تصویر رسید را همینجا ارسال کنید.", ("amount", "card_number", "card_holder", "topup_id"), required_fields=("amount", "card_number")),
    ContentDefinition("wallet_receipt_sent", "رسید برای بررسی ارسال شد", "payment", "✅ رسید شما برای بررسی ارسال شد\n\nپس از تأیید مدیریت، کیف پول شارژ می‌شود و اگر پرداخت مربوط به خرید باشد، همان سفارش به‌صورت خودکار ادامه پیدا می‌کند."),
    ContentDefinition("wallet_approved", "شارژ کیف پول تأیید شد", "payment", "✅ کیف پول شما شارژ شد\n\n💰 مبلغ شارژ: {amount}\n👛 موجودی فعلی: {balance}\n{purchase_result}", ("amount", "balance", "purchase_result")),
    ContentDefinition("wallet_rejected", "شارژ کیف پول رد شد", "payment", "❌ درخواست شارژ #{topup_id} تأیید نشد.\n\nدر صورت وجود ابهام، تصویر رسید و اطلاعات پرداخت را برای پشتیبانی ارسال کنید.", ("topup_id",)),
    ContentDefinition("referral_menu", "صفحه دعوت دوستان", "main", "👥 دعوت دوستان\n\n{body}", ("body",)),
    ContentDefinition("rules", "قوانین و شرایط خرید", "buy", "📜 قوانین و شرایط خرید\n\n• لینک سرویس اختصاصی است و نباید به دیگران واگذار شود.\n• برای خرابی واقعی سرویس از بخش پشتیبانی همان سرویس استفاده کنید.\n• بازپرداخت فقط بر اساس شرایط اعلام‌شده فروشگاه انجام می‌شود."),
    ContentDefinition("guide_home", "صفحه آموزش اتصال", "guide", "📚 آموزش اتصال\n\nدستگاه خود را انتخاب کنید تا مراحل نصب و اتصال را ببینید."),
    ContentDefinition("guide_android", "آموزش اندروید", "guide", "📱 آموزش اتصال اندروید\n\n1️⃣ یک برنامه سازگار با ساب‌لینک نصب کنید.\n2️⃣ لینک سرویس را از «سرویس‌های من» کپی کنید.\n3️⃣ داخل برنامه گزینه Import یا Subscription را بزنید.\n4️⃣ لینک را اضافه و بروزرسانی کنید."),
    ContentDefinition("guide_ios", "آموزش آیفون", "guide", "🍎 آموزش اتصال آیفون\n\n1️⃣ یک کلاینت سازگار نصب کنید.\n2️⃣ لینک سرویس را کپی کنید.\n3️⃣ از بخش Subscription یا Import، لینک را اضافه کنید.\n4️⃣ اتصال را فعال و تست کنید."),
    ContentDefinition("guide_windows", "آموزش ویندوز", "guide", "💻 آموزش اتصال ویندوز\n\n1️⃣ کلاینت مناسب را نصب کنید.\n2️⃣ لینک سرویس را از «سرویس‌های من» کپی کنید.\n3️⃣ از بخش Subscription لینک را اضافه کنید.\n4️⃣ Update Subscription را بزنید و اتصال را فعال کنید."),
    ContentDefinition("guide_mac", "آموزش مک", "guide", "🖥 آموزش اتصال مک\n\n1️⃣ کلاینت سازگار با macOS را نصب کنید.\n2️⃣ لینک سرویس را اضافه کنید.\n3️⃣ اشتراک را بروزرسانی و اتصال را فعال کنید."),
    ContentDefinition("guide_troubleshoot", "راهنمای مشکل اتصال", "guide", "❓ مشکل اتصال دارید؟\n\nابتدا اینترنت اصلی را بررسی و ساب‌لینک را بروزرسانی کنید. اگر مشکل ادامه داشت، از داخل همان سرویس گزینه «گزارش مشکل» را بزنید."),
    ContentDefinition("guide_update", "آموزش بروزرسانی ساب‌لینک", "guide", "🔄 بروزرسانی ساب‌لینک\n\nدر برنامه خود گزینه Update یا Refresh Subscription را بزنید تا اطلاعات و سرورها تازه شوند."),
    # Buy flow
    ContentDefinition(
        "buy_root", "صفحه اول خرید", "buy",
        "🛒 خرید سرویس\n\nنوع سرویس موردنظرتان را انتخاب کنید. هر دسته شامل بسته‌های متفاوت با حجم و مدت مشخص است.",
        ("categories_summary", "wallet_balance"), scopes=(SCOPE_GLOBAL,),
    ),
    ContentDefinition(
        "buy_category", "معرفی دسته و بسته‌ها", "buy",
        "{category_emoji} {category_title}\n\n{category_description}\n{shared_description}\n{shared_pre_purchase}\n{delivery_line}\n{devices_line}\n{start_mode_line}\n{wallet_balance_line}\n\nبسته موردنظرتان را انتخاب کنید:",
        ("category_emoji", "category_title", "category_description", "shared_description", "shared_pre_purchase", "delivery_line", "devices_line", "start_mode_line", "wallet_balance_line"),
        required_fields=("category_title",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY),
    ),
    ContentDefinition(
        "package_button", "متن دکمه هر بسته", "buy",
        "{volume} | {duration} | {price}",
        ("title", "volume", "duration", "price", "tag", "stock_status"),
        required_fields=("price",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN), kind="button", allow_media=False,
    ),
    ContentDefinition(
        "checkout", "صفحه تأیید خرید", "buy",
        "🛒 تأیید خرید\n\n💎 سرویس: {category_title}\n📦 بسته: {package_title}\n📊 حجم: {volume}\n⏳ مدت: {duration}\n{devices_line}\n\n💰 مبلغ: {price}\n{wallet_balance_line}\n{discount_line}\n{plan_description}\n{pre_purchase_text}\n\n{checkout_hint}",
        ("category_title", "package_title", "volume", "duration", "devices_line", "price", "wallet_balance_line", "discount_line", "plan_description", "pre_purchase_text", "checkout_hint"),
        required_fields=("package_title", "price"), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN),
    ),
    ContentDefinition(
        "payment_topup", "پرداخت کسری خرید", "payment",
        "💳 تکمیل پرداخت سفارش\n\n🧾 بسته: {package_title}\n🔢 تعداد: {quantity}\n💰 مبلغ اولیه: {subtotal}\n🎁 تخفیف: {discount}\n✅ مبلغ نهایی: {total}\n👛 موجودی کیف پول: {wallet_balance}\n📌 مبلغ قابل پرداخت: {payable}\n\nشماره کارت:\n{card_number}\nبه نام: {card_holder}\n\nبعد از واریز، تصویر رسید را همینجا ارسال کنید. پس از تأیید، سفارش شما به‌صورت خودکار تکمیل می‌شود.",
        ("package_title", "quantity", "subtotal", "discount", "total", "wallet_balance", "payable", "card_number", "card_holder", "payment_id"),
        required_fields=("payable", "card_number"), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN),
    ),
    ContentDefinition(
        "order_queued", "سفارش وارد صف ساخت شد", "orders",
        "🟠 سفارش شما ثبت شد\n\n🧾 شماره سفارش: #{order_id}\n💰 مبلغ نهایی: {total}\n\nآماده‌سازی سرویس کمی بیشتر از معمول طول کشیده است. نگران نباشید؛ سیستم به‌صورت خودکار دوباره تلاش می‌کند و سفارش یا برداشت تکراری ساخته نمی‌شود.",
        ("order_id", "total", "next_retry", "attempt", "max_attempts"), required_fields=("order_id",),
    ),
    ContentDefinition(
        "order_refunded", "بازپرداخت سفارش ناموفق", "orders",
        "⚫ مبلغ سفارش به کیف پول برگشت\n\n🧾 شماره سفارش: #{order_id}\n💰 مبلغ بازگشتی: {refund_amount}\n\nساخت سرویس پس از چند تلاش موفق نشد. مبلغ فقط یک بار به کیف پول شما برگردانده شد و سفارش برای بررسی مدیریت ثبت شده است.",
        ("order_id", "refund_amount"), required_fields=("order_id",),
    ),
    ContentDefinition(
        "purchase_success", "پیام خرید موفق", "orders",
        "✅ خرید با موفقیت انجام شد\n\n🧾 شماره سفارش: #{order_id}\n💎 پلن: {plan_title}\n🔢 تعداد: {quantity}\n💰 مبلغ اولیه: {subtotal}\n🎁 تخفیف: {discount}\n✅ مبلغ پرداخت‌شده: {total}\n👛 موجودی جدید: {balance_after}\n{test_notice}\n{post_purchase_text}\n\nسرویس شما آماده شده و اطلاعات اتصال در پیام بعدی ارسال می‌شود 🚀",
        ("order_id", "plan_title", "quantity", "subtotal", "discount", "total", "balance_after", "test_notice", "post_purchase_text"),
        required_fields=("order_id", "plan_title"), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN),
    ),
    ContentDefinition(
        "service_delivery", "تحویل سرویس", "orders",
        "🎉 سرویس شما آماده است\n\n🧾 سفارش: #{order_id}\n💎 پلن: {plan_title}\n👤 شناسه سرویس: {username}\n📦 حجم: {volume}\n⏳ مدت: {duration}\n📱 دستگاه: {devices}\n📅 تاریخ انقضا: {expire_date}\n\n🔗 لینک اختصاصی اتصال:\n{subscription_url}\n\n⚠️ این لینک شخصی است؛ آن را در اختیار دیگران قرار ندهید.",
        ("order_id", "plan_title", "username", "volume", "duration", "devices", "expire_date", "subscription_url", "provider_public_name", "created_at"),
        required_fields=("subscription_url",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN),
    ),
    ContentDefinition("bulk_request_sent", "ثبت درخواست خرید عمده", "buy", "✅ درخواست خرید عمده شما ثبت شد\n\n🎫 شماره پیگیری: #{ticket_id}\nبه‌زودی برای هماهنگی تعداد و شرایط خرید با شما تماس می‌گیریم.", ("ticket_id",), required_fields=("ticket_id",)),
    ContentDefinition("invalid_purchase", "درخواست خرید نامعتبر", "buy", "⚠️ این درخواست خرید معتبر نیست یا زمان آن گذشته است.\n\nلطفاً بسته را دوباره از فروشگاه انتخاب کنید."),
    ContentDefinition("invalid_quantity", "تعداد خرید نامعتبر", "buy", "⚠️ تعداد انتخاب‌شده قابل خرید نیست.\n\n{reason}", ("reason",)),
    ContentDefinition("insufficient_stock", "موجودی ناکافی بسته", "buy", "📦 موجودی این بسته برای تعداد انتخاب‌شده کافی نیست.\n\nتعداد کمتری انتخاب کنید یا یکی از بسته‌های دیگر را ببینید.", scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("purchase_config_error", "نقص تنظیمات ساخت سرویس", "buy", "🛠 آماده‌سازی خودکار این بسته موقتاً ممکن نیست.\n\nهزینه‌ای از شما کم نشده است. لطفاً بسته دیگری انتخاب کنید یا با پشتیبانی تماس بگیرید.", scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("no_services", "نبود سرویس فعال برای فروش", "buy", "😔 در حال حاضر سرویس فعالی برای فروش وجود ندارد.\n\nلطفاً کمی بعد دوباره سر بزنید یا با پشتیبانی در ارتباط باشید."),
    ContentDefinition("category_empty", "خالی بودن دسته", "buy", "📭 این دسته فعلاً بسته فعالی ندارد.\n\nمی‌توانید یکی از دسته‌های دیگر را انتخاب کنید."),
    ContentDefinition("plan_unavailable", "ناموجود بودن بسته", "buy", "❌ این بسته در حال حاضر موجود نیست.\n\nیکی از بسته‌های دیگر را انتخاب کنید یا بعداً دوباره بررسی کنید.", scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("provider_unavailable", "اختلال موقت تأمین‌کننده", "buy", "⚠️ این سرویس موقتاً در دسترس نیست.\n\nلطفاً بسته دیگری را انتخاب کنید؛ سرویس‌های فعلی شما بدون مشکل باقی می‌مانند.", scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    # Services
    ContentDefinition("services_empty", "خالی بودن سرویس‌های من", "services", "📭 هنوز سرویسی در حساب شما ثبت نشده است.\n\nاز بخش «خرید سرویس» اولین سرویس خود را تهیه کنید."),
    ContentDefinition("services_list", "بالای فهرست سرویس‌ها", "services", "📦 سرویس‌های من\n\nبرای مشاهده لینک، QR، میزان مصرف یا گزارش مشکل، یکی از سرویس‌ها را انتخاب کنید."),
    ContentDefinition(
        "service_button", "متن دکمه سرویس", "services", "{plan_title} | {username}",
        ("plan_title", "username", "status", "trial_label"), kind="button", allow_media=False,
    ),
    ContentDefinition(
        "service_detail", "جزئیات سرویس", "services",
        "📦 {plan_title}\n\n👤 شناسه: {username}\n🟢 وضعیت: {status}\n📊 مصرف: {usage}\n📅 تاریخ خرید: {purchase_date}\n⏳ انقضا: {expire_date}\n{provider_line}",
        ("plan_title", "username", "status", "usage", "purchase_date", "expire_date", "provider_line", "days_left"),
        required_fields=("plan_title",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN),
    ),
    ContentDefinition("service_not_found", "سرویس پیدا نشد", "services", "📭 این سرویس در حساب شما پیدا نشد یا دیگر در دسترس نیست.\n\nاز «سرویس‌های من» دوباره سرویس موردنظر را انتخاب کنید."),
    ContentDefinition("service_link", "نمایش لینک سرویس", "services", "📋 لینک سرویس «{username}»\n\n{subscription_url}\n\n⚠️ لینک اختصاصی است و نباید با افراد دیگر به اشتراک گذاشته شود.", ("username", "subscription_url"), required_fields=("subscription_url",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("service_qr", "کپشن QR سرویس", "services", "📱 QR اتصال سرویس {username}", ("username",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("usage_success", "نمایش مصرف سرویس", "services", "📊 مصرف فعلی سرویس\n\nمصرف‌شده: {used_volume}\nحجم کل: {total_volume}\nباقی‌مانده: {remaining_volume}", ("used_volume", "total_volume", "remaining_volume"), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("usage_error", "خطای دریافت مصرف", "services", "⚠️ دریافت میزان مصرف در حال حاضر ممکن نیست.\n\nچند دقیقه بعد دوباره امتحان کنید؛ در صورت تکرار مشکل با پشتیبانی تماس بگیرید."),
    ContentDefinition("wallet_invalid_amount", "مبلغ شارژ نامعتبر", "payment", "🔢 مبلغ شارژ را فقط به‌صورت عدد ارسال کنید.\n\nحداقل مبلغ قابل شارژ: {min_amount}\nمثال: 100000", ("min_amount",)),
    ContentDefinition("wallet_receipt_required", "درخواست تصویر رسید", "payment", "🧾 لطفاً تصویر واضح رسید پرداخت را ارسال کنید.\n\nمتن خالی یا فایل نامرتبط قابل بررسی نیست."),
    ContentDefinition("wallet_topup_missing", "نبود درخواست شارژ فعال", "payment", "ℹ️ درخواست شارژ فعالی برای حساب شما پیدا نشد.\n\nاز بخش کیف پول، فرایند شارژ را دوباره شروع کنید."),
    ContentDefinition("wallet_topup_duplicate", "درخواست شارژ تکراری", "payment", "⌛ این درخواست قبلاً ارسال یا بررسی شده است.\n\nبرای شارژ جدید، دوباره از بخش کیف پول شروع کنید."),
    # Discounts
    ContentDefinition("discount_prompt", "درخواست کد تخفیف", "discount", "🎁 کد تخفیف بسته «{plan_title}» را ارسال کنید.\n\nبعد از تأیید، به همان صفحه خرید برمی‌گردید.", ("plan_title",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("discount_valid", "کد تخفیف معتبر", "discount", "✅ کد {discount_code} با موفقیت اعمال شد\n\n🎁 میزان تخفیف: {discount_amount}\n💰 مبلغ نهایی: {final_price}\n\nحالا دکمه پرداخت همان بسته را بزنید.", ("discount_code", "discount_amount", "final_price"), required_fields=("discount_code",), scopes=(SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN)),
    ContentDefinition("discount_invalid", "کد تخفیف نامعتبر", "discount", "❌ {reason}\n\nکد دیگری ارسال کنید یا به صفحه بسته برگردید.", ("reason",), required_fields=("reason",)),
    ContentDefinition("discount_cleared", "حذف کد تخفیف", "discount", "🧹 کد تخفیف از این خرید حذف شد."),
    # Support
    ContentDefinition("issue_select", "انتخاب نوع خرابی", "support", "🎫 گزارش مشکل سرویس\n\nنوع مشکل را انتخاب کنید تا اطلاعات همان سرویس به‌صورت خودکار برای پشتیبانی ارسال شود."),
    ContentDefinition("issue_created", "ثبت موفق گزارش خرابی", "support", "✅ گزارش شما ثبت شد\n\n🎫 شماره تیکت: #{ticket_id}\n💎 سرویس: {plan_title}\n🛠 نوع مشکل: {issue_label}\n\nپشتیبانی پس از بررسی از طریق همین ربات پاسخ می‌دهد.", ("ticket_id", "plan_title", "issue_label"), required_fields=("ticket_id",)),
    ContentDefinition("support_intro", "شروع پشتیبانی عمومی", "support", "🎫 پشتیبانی Berserk\n\nموضوع خود را واضح و کامل بنویسید. برای خرابی یک سرویس، بهتر است از دکمه «گزارش مشکل» داخل همان سرویس استفاده کنید."),
    ContentDefinition("support_prompt", "درخواست پیام پشتیبانی", "support", "✍️ پیام یا سؤال خود را ارسال کنید.\n\nمی‌توانید متن، عکس یا فایل بفرستید؛ توضیح دقیق‌تر باعث بررسی سریع‌تر می‌شود."),
    ContentDefinition("support_created", "ثبت تیکت عمومی", "support", "✅ پیام شما ثبت شد\n\n🎫 شماره تیکت: #{ticket_id}\nپشتیبانی پس از بررسی از طریق همین ربات پاسخ می‌دهد.", ("ticket_id",), required_fields=("ticket_id",)),
    # Trial
    ContentDefinition("trial_build", "در حال ساخت تست", "trial", "⏳ در حال آماده‌سازی اکانت تست شما هستیم\n\n📦 حجم: {volume}\n⏳ اعتبار: {duration}\n📱 دستگاه: {devices}\n\nلطفاً چند لحظه صبر کنید.", ("volume", "duration", "devices")),
    ContentDefinition("trial_success", "تحویل تست", "trial", "🧪 اکانت تست شما آماده است\n\n👤 شناسه: {username}\n📦 حجم: {volume}\n⏳ اعتبار: {duration}\n\n🔗 لینک اتصال:\n{subscription_url}\n\nاین تست فقط یک بار برای هر حساب قابل دریافت است.", ("username", "volume", "duration", "subscription_url"), required_fields=("subscription_url",)),
    ContentDefinition("trial_qr", "کپشن QR اکانت تست", "trial", "📱 QR اکانت تست {username}\n\nاین تصویر اختصاصی است؛ آن را برای دیگران ارسال نکنید.", ("username",)),
    ContentDefinition("trial_duplicate", "تست قبلاً دریافت شده", "trial", "ℹ️ شما قبلاً اکانت تست دریافت کرده‌اید.\n\nهر حساب تلگرام فقط یک بار امکان دریافت تست دارد."),
    ContentDefinition("trial_error", "خطای ساخت تست", "trial", "⚠️ ساخت اکانت تست در حال حاضر ممکن نیست.\n\nلطفاً کمی بعد دوباره امتحان کنید."),
    # Safe button labels. Callbacks remain hard-coded.
    ContentDefinition("btn_pay", "دکمه پرداخت", "buttons", "💳 پرداخت و دریافت سرویس", kind="button", allow_media=False),
    ContentDefinition("btn_discount", "دکمه کد تخفیف", "buttons", "🎁 وارد کردن کد تخفیف", kind="button", allow_media=False),
    ContentDefinition("btn_back_packages", "دکمه بازگشت به بسته‌ها", "buttons", "⬅️ بازگشت به بسته‌ها", kind="button", allow_media=False),
    ContentDefinition("btn_bulk", "دکمه خرید عمده", "buttons", "📦 خرید عمده", kind="button", allow_media=False),
    ContentDefinition("btn_show_link", "دکمه نمایش لینک", "buttons", "📋 نمایش لینک", kind="button", allow_media=False),
    ContentDefinition("btn_show_qr", "دکمه نمایش QR", "buttons", "📱 نمایش QR", kind="button", allow_media=False),
    ContentDefinition("btn_usage", "دکمه بروزرسانی مصرف", "buttons", "📊 بروزرسانی مصرف", kind="button", allow_media=False),
    ContentDefinition("btn_issue", "دکمه گزارش مشکل", "buttons", "🎫 گزارش مشکل", kind="button", allow_media=False),
)

DEFINITION_MAP = {item.key: item for item in DEFINITIONS}
CATEGORIES = {
    "main": "👋 شروع و پیام‌های عمومی",
    "buy": "🛒 خرید و انتخاب بسته",
    "payment": "💳 پرداخت",
    "orders": "📦 سفارش و تحویل",
    "services": "🧾 سرویس‌های من",
    "discount": "🎁 تخفیف",
    "support": "🎫 پشتیبانی",
    "trial": "🧪 اکانت تست",
    "buttons": "🔘 متن دکمه‌ها",
    "guide": "📚 آموزش اتصال",
}


# Ready-made packs target the real v6.4 customer slots. Applying a pack only
# creates Drafts; published customer text remains untouched until review.
PLAN_TEMPLATE_PACK_SLOTS = ("package_button", "checkout", "purchase_success", "service_delivery")
PLAN_TEMPLATE_PACKS = {
    "professional": {
        "title": "✨ حرفه‌ای",
        "description": "متعادل، کامل و مناسب استفاده عمومی فروشگاه.",
        "slots": {
            "package_button": "✨ {title} | {volume} | {price}",
            "checkout": "✨ انتخاب حرفه‌ای شما\n\n📦 بسته: {package_title}\n📊 حجم: {volume}\n⏳ مدت: {duration}\n{devices_line}\n\n💰 مبلغ نهایی: {price}\n{wallet_balance_line}\n{discount_line}\n{plan_description}\n{pre_purchase_text}\n\n{checkout_hint}",
            "purchase_success": "✅ خرید شما با موفقیت نهایی شد\n\n🧾 سفارش: #{order_id}\n💎 پلن: {plan_title}\n🔢 تعداد: {quantity}\n💰 مبلغ پرداخت‌شده: {total}\n👛 موجودی جدید: {balance_after}\n{test_notice}\n{post_purchase_text}\n\nاطلاعات اتصال تا چند لحظه دیگر ارسال می‌شود 🚀",
            "service_delivery": "🎉 سرویس شما آماده است\n\n🧾 سفارش: #{order_id}\n💎 پلن: {plan_title}\n👤 شناسه: {username}\n📦 حجم: {volume}\n⏳ مدت: {duration}\n📱 دستگاه: {devices}\n📅 انقضا: {expire_date}\n\n🔗 لینک اختصاصی:\n{subscription_url}\n\n⚠️ این لینک شخصی است؛ آن را با دیگران به اشتراک نگذارید.",
        },
    },
    "minimal": {
        "title": "🤍 مینیمال",
        "description": "کوتاه، خلوت و سریع برای مشتریانی که متن ساده می‌پسندند.",
        "slots": {
            "package_button": "{volume} | {duration} | {price}",
            "checkout": "🛒 {package_title}\n\n📦 {volume}\n⏳ {duration}\n💰 {price}\n{discount_line}\n\n{checkout_hint}",
            "purchase_success": "✅ خرید انجام شد\n\nسفارش #{order_id}\nپلن: {plan_title}\nمبلغ: {total}\n\nاطلاعات سرویس در پیام بعدی ارسال می‌شود.",
            "service_delivery": "✅ سرویس آماده است\n\n{plan_title}\nشناسه: {username}\nانقضا: {expire_date}\n\n{subscription_url}",
        },
    },
    "vip": {
        "title": "💎 VIP",
        "description": "لحن پریمیوم و ویژه برای سرویس‌های VIP و حرفه‌ای.",
        "slots": {
            "package_button": "💎 {volume} | {duration} | {price}",
            "checkout": "💎 انتخاب ویژه VIP\n\n🚀 بسته: {package_title}\n📦 حجم: {volume}\n⏳ اعتبار: {duration}\n{devices_line}\n\n💰 مبلغ: {price}\n{wallet_balance_line}\n{discount_line}\n{plan_description}\n\n✨ پس از پرداخت، سرویس اختصاصی شما آماده می‌شود.\n{checkout_hint}",
            "purchase_success": "💎 خرید VIP شما ثبت شد\n\n🧾 سفارش: #{order_id}\n🚀 پلن: {plan_title}\n💰 پرداخت موفق: {total}\n👛 موجودی جدید: {balance_after}\n{post_purchase_text}\n\nسرویس اختصاصی شما در حال تحویل است ✨",
            "service_delivery": "💎 سرویس VIP شما آماده است\n\n🧾 سفارش: #{order_id}\n🚀 پلن: {plan_title}\n👤 شناسه اختصاصی: {username}\n📦 حجم: {volume}\n⏳ مدت: {duration}\n📅 انقضا: {expire_date}\n\n🔗 لینک اتصال اختصاصی:\n{subscription_url}\n\n🔐 برای حفظ کیفیت سرویس، لینک را فقط روی دستگاه‌های خودتان استفاده کنید.",
        },
    },
    "economy": {
        "title": "🌱 اقتصادی",
        "description": "صمیمی و مقرون‌به‌صرفه برای بسته‌های اقتصادی.",
        "slots": {
            "package_button": "🌱 {volume} | {duration} | {price}",
            "checkout": "🌱 خرید اقتصادی و به‌صرفه\n\n📦 بسته: {package_title}\n📊 حجم: {volume}\n⏳ مدت: {duration}\n\n💰 مبلغ: {price}\n{wallet_balance_line}\n{discount_line}\n{plan_description}\n\n✅ انتخاب مناسب برای استفاده روزمره\n{checkout_hint}",
            "purchase_success": "✅ خرید اقتصادی شما انجام شد\n\n🧾 سفارش: #{order_id}\n🌱 پلن: {plan_title}\n💰 مبلغ پرداخت‌شده: {total}\n👛 موجودی جدید: {balance_after}\n\nسرویس تا چند لحظه دیگر ارسال می‌شود.",
            "service_delivery": "🌱 سرویس شما آماده است\n\n🧾 سفارش: #{order_id}\n📦 پلن: {plan_title}\n👤 شناسه: {username}\n📊 حجم: {volume}\n⏳ مدت: {duration}\n📅 انقضا: {expire_date}\n\n🔗 لینک اتصال:\n{subscription_url}\n\n✅ برای اتصال بهتر، ساب‌لینک را در برنامه بروزرسانی کنید.",
        },
    },
    "technical": {
        "title": "🧩 فنی",
        "description": "منظم و اطلاعات‌محور برای مشتریان حرفه‌ای‌تر.",
        "slots": {
            "package_button": "🧩 {title} | {volume} | {duration} | {price}",
            "checkout": "🧩 مشخصات فنی سفارش\n\nPlan: {package_title}\nTraffic: {volume}\nDuration: {duration}\n{devices_line}\nPrice: {price}\n{discount_line}\n{plan_description}\n{pre_purchase_text}\n\n{checkout_hint}",
            "purchase_success": "✅ تراکنش موفق\n\nOrder ID: #{order_id}\nPlan: {plan_title}\nQuantity: {quantity}\nPaid: {total}\nBalance: {balance_after}\n\nProvisioning completed; connection data follows.",
            "service_delivery": "🧩 مشخصات سرویس\n\nOrder: #{order_id}\nPlan: {plan_title}\nUsername: {username}\nTraffic: {volume}\nDuration: {duration}\nDevices: {devices}\nExpires: {expire_date}\n\nSubscription URL:\n{subscription_url}",
        },
    },
    "sales": {
        "title": "🔥 فروش‌محور",
        "description": "انرژی بیشتر و دعوت واضح به خرید، بدون شلوغی اضافی.",
        "slots": {
            "package_button": "🔥 {title} | فقط {price}",
            "checkout": "🔥 این بسته را از دست ندهید\n\n✅ {package_title}\n📦 {volume} حجم\n⏳ {duration} اعتبار\n{devices_line}\n\n💰 فقط {price}\n{discount_line}\n{plan_description}\n\n🚀 تحویل سریع پس از پرداخت\n{checkout_hint}",
            "purchase_success": "🎉 انتخاب عالی بود؛ خرید شما انجام شد\n\n🧾 سفارش: #{order_id}\n🔥 پلن: {plan_title}\n💰 پرداخت‌شده: {total}\n{post_purchase_text}\n\nاطلاعات اتصال همین حالا برایتان ارسال می‌شود 🚀",
            "service_delivery": "🚀 سرویس شما آماده استفاده است\n\n🔥 پلن: {plan_title}\n📦 حجم: {volume}\n⏳ مدت: {duration}\n📅 انقضا: {expire_date}\n\n🔗 لینک اختصاصی شما:\n{subscription_url}\n\n✅ لینک را به برنامه اضافه کنید و از اتصال سریع لذت ببرید.",
        },
    },
    "trial": {
        "title": "🧪 تست رایگان",
        "description": "متن مناسب بسته‌های تست و سرویس‌های آزمایشی.",
        "slots": {
            "package_button": "🧪 تست {volume} | {duration} | {price}",
            "checkout": "🧪 دریافت سرویس آزمایشی\n\n📦 بسته: {package_title}\n📊 حجم تست: {volume}\n⏳ اعتبار: {duration}\n\n💰 هزینه: {price}\n{plan_description}\n\nاین سرویس برای بررسی کیفیت اتصال ارائه می‌شود.\n{checkout_hint}",
            "purchase_success": "🧪 درخواست تست شما ثبت شد\n\n🧾 سفارش: #{order_id}\n📦 سرویس: {plan_title}\n💰 مبلغ: {total}\n\nاطلاعات تست در پیام بعدی ارسال می‌شود.",
            "service_delivery": "🧪 اکانت تست شما آماده است\n\n👤 شناسه: {username}\n📦 حجم: {volume}\n⏳ اعتبار: {duration}\n📅 انقضا: {expire_date}\n\n🔗 لینک تست:\n{subscription_url}\n\n⚠️ هر کاربر فقط طبق قوانین فروشگاه امکان دریافت تست دارد.",
        },
    },
}


def list_plan_template_packs() -> list[tuple[str, str, str]]:
    return [(key, value["title"], value["description"]) for key, value in PLAN_TEMPLATE_PACKS.items()]


def get_plan_template_pack(pack_key: str) -> dict[str, Any]:
    pack = PLAN_TEMPLATE_PACKS.get(str(pack_key or "").strip().lower())
    if not pack:
        raise ValueError("قالب آماده پلن پیدا نشد.")
    return pack


def preview_plan_template_pack(pack_key: str, *, category_id=None, plan_id=None) -> list[dict[str, str]]:
    pack = get_plan_template_pack(pack_key)
    values = sample_context(category_id=category_id, plan_id=plan_id)
    previews: list[dict[str, str]] = []
    for slot_key in PLAN_TEMPLATE_PACK_SLOTS:
        item = definition(slot_key)
        template_text = pack["slots"][slot_key]
        validate_text(slot_key, template_text, "plain")
        safe_values = {field: _escape_value(values.get(field, ""), "plain") for field in item.fields}
        rendered = _clean_rendered(template_text.format_map(_SafeDict(safe_values)))
        if item.kind == "button" and len(rendered) > BUTTON_LIMIT:
            rendered = rendered[: BUTTON_LIMIT - 1].rstrip() + "…"
        previews.append({"slot_key": slot_key, "title": item.title, "text": rendered})
    return previews


def apply_plan_template_pack(pack_key: str, scope_type: str, scope_id: int = 0, *, admin_id=None) -> list[str]:
    """Save a coherent ready-made pack as Drafts without auto-publishing."""
    pack = get_plan_template_pack(pack_key)
    scope_type, scope_id = _normalize_scope(scope_type, scope_id)
    prepared: list[tuple[str, str]] = []
    for slot_key in PLAN_TEMPLATE_PACK_SLOTS:
        item = definition(slot_key)
        if scope_type not in item.scopes:
            raise ValueError(f"«{item.title}» در این دامنه قابل اعمال نیست.")
        template_text = pack["slots"][slot_key]
        prepared.append((slot_key, validate_text(slot_key, template_text, "plain")))
    for slot_key, template_text in prepared:
        save_draft(slot_key, template_text, scope_type, scope_id, parse_mode="plain", admin_id=admin_id)
    return [slot_key for slot_key, _ in prepared]

LEGACY_MESSAGE_MAP = {
    "welcome": "welcome",
    "main_menu": "main_menu",
    "menu_buy": "buy_root",
    "menu_wallet": "wallet_menu",
    "menu_referral": "referral_menu",
    "my_services_empty": "services_empty",
    "guide_home": "guide_home",
    "guide_android": "guide_android",
    "guide_ios": "guide_ios",
    "guide_windows": "guide_windows",
    "guide_mac": "guide_mac",
    "guide_troubleshoot": "guide_troubleshoot",
    "guide_update": "guide_update",
    "support_intro": "support_intro",
    "rules": "rules",
}

DEFAULT_DISPLAY_SETTINGS = {
    "show_wallet_balance": True,
    "show_devices": True,
    "show_delivery": True,
    "show_start_mode": True,
    "show_stock_status": True,
    "show_provider_public": True,
    "show_discount_button": True,
    "show_numeric_stock": False,
    "show_category_plan_count": False,
}

SAMPLE_CONTEXT = {
    "categories_summary": "💎 VIP\n🌱 اقتصادی",
    "wallet_balance": "250,000 تومان",
    "wallet_balance_line": "💳 موجودی کیف پول: 250,000 تومان",
    "category_emoji": "💎",
    "category_title": "سرویس‌های VIP",
    "category_description": "اتصال پرسرعت و پایدار برای استفاده روزمره، بازی و تماشای آنلاین ⚡",
    "shared_description": "🌍 سرورهای باکیفیت\n🔐 اتصال امن\n🚀 تحویل فوری",
    "shared_pre_purchase": "قبل از خرید از سازگاری برنامه خود مطمئن شوید.",
    "delivery_line": "🚚 تحویل: ساخت خودکار و فوری",
    "devices_line": "📱 دستگاه: ۳ دستگاه",
    "start_mode_line": "⏱ شروع اعتبار: اولین اتصال",
    "title": "VIP 100GB",
    "package_title": "100 گیگ یک‌ماهه",
    "plan_title": "VIP 100GB",
    "volume": "100 گیگ",
    "duration": "30 روز",
    "price": "350,000 تومان",
    "devices": "3 دستگاه",
    "tag": "پرفروش",
    "stock_status": "موجود",
    "discount_line": "🎁 تخفیف فعال: 35,000 تومان",
    "plan_description": "بسته مناسب استفاده سنگین و چند دستگاه.",
    "pre_purchase_text": "پس از پرداخت، سرویس به‌صورت خودکار ارسال می‌شود.",
    "checkout_hint": "برای پرداخت و دریافت سرویس، دکمه زیر را بزنید.",
    "quantity": "1",
    "subtotal": "350,000 تومان",
    "discount": "35,000 تومان",
    "total": "315,000 تومان",
    "payable": "65,000 تومان",
    "card_number": "6037-****-****-1234",
    "card_holder": "نمونه صاحب حساب",
    "payment_id": "42",
    "order_id": "1254",
    "refund_amount": "315,000 تومان",
    "balance_after": "100,000 تومان",
    "test_notice": "",
    "post_purchase_text": "",
    "username": "Berserk_1254",
    "expire_date": "۱۴۰۵/۰۵/۲۰",
    "subscription_url": "https://example.com/sub/REDACTED",
    "provider_public_name": "تحویل خودکار",
    "provider_line": "🚚 نوع تحویل: تحویل خودکار",
    "created_at": "۱۴۰۵/۰۴/۲۰",
    "status": "فعال",
    "usage": "23.4 از 100 گیگ",
    "purchase_date": "۱۴۰۵/۰۴/۲۰",
    "days_left": "30",
    "used_volume": "23.4 گیگ",
    "total_volume": "100 گیگ",
    "remaining_volume": "76.6 گیگ",
    "discount_code": "VIP20",
    "discount_amount": "70,000 تومان",
    "final_price": "280,000 تومان",
    "reason": "این کد تخفیف معتبر یا فعال نیست.",
    "ticket_id": "88",
    "issue_label": "سرعت پایین است",
    "trial_label": "",
    "next_retry": "2 دقیقه دیگر",
    "attempt": "2",
    "max_attempts": "3",
}


class _HTMLValidator(HTMLParser):
    VOID = {"br"}
    ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in self.ALLOWED:
            raise ValueError(f"تگ HTML مجاز نیست: <{tag}>")
        if tag == "a":
            allowed_attrs = {"href"}
            if any(name not in allowed_attrs for name, _ in attrs):
                raise ValueError("فقط ویژگی href برای لینک HTML مجاز است.")
        elif attrs and tag not in {"span", "tg-emoji"}:
            raise ValueError(f"ویژگی HTML برای <{tag}> مجاز نیست.")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() not in self.ALLOWED:
            raise ValueError(f"تگ HTML مجاز نیست: <{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            expected = self.stack[-1] if self.stack else "-"
            raise ValueError(f"ترتیب بسته‌شدن HTML خراب است؛ انتظار </{expected}> بود.")
        self.stack.pop()

    def finish(self):
        self.close()
        if self.stack:
            raise ValueError(f"تگ HTML بسته نشده است: <{self.stack[-1]}>")


def _columns(table: str) -> set[str]:
    db.cur.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in db.cur.fetchall()}


def init_schema() -> None:
    """Create schema 640 and migrate existing custom texts without deleting data."""
    with db.LOCK:
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS content_templates(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_key TEXT NOT NULL,
                scope_type TEXT NOT NULL DEFAULT 'global',
                scope_id INTEGER NOT NULL DEFAULT 0,
                published_text TEXT,
                draft_text TEXT,
                published_photo_file_id TEXT,
                draft_photo_file_id TEXT,
                parse_mode TEXT DEFAULT 'plain',
                draft_parse_mode TEXT,
                is_active INTEGER DEFAULT 1,
                updated_by TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                published_at TEXT,
                UNIQUE(slot_key, scope_type, scope_id)
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS content_template_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL,
                slot_key TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id INTEGER NOT NULL DEFAULT 0,
                text_value TEXT,
                photo_file_id TEXT,
                parse_mode TEXT DEFAULT 'plain',
                action TEXT DEFAULT 'publish',
                admin_id TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS content_display_settings(
                scope_type TEXT NOT NULL DEFAULT 'global',
                scope_id INTEGER NOT NULL DEFAULT 0,
                settings_json TEXT NOT NULL DEFAULT '{}',
                updated_by TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY(scope_type, scope_id)
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS purchase_funnel_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                category_id INTEGER,
                plan_id INTEGER,
                purchase_id INTEGER,
                session_key TEXT,
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute("CREATE INDEX IF NOT EXISTS idx_content_slot_scope ON content_templates(slot_key,scope_type,scope_id)")
        db.cur.execute("CREATE INDEX IF NOT EXISTS idx_content_versions_template ON content_template_versions(template_id,created_at)")
        db.cur.execute("CREATE INDEX IF NOT EXISTS idx_funnel_event_created ON purchase_funnel_events(event_type,created_at)")
        db.cur.execute("CREATE INDEX IF NOT EXISTS idx_funnel_user_created ON purchase_funnel_events(user_id,created_at)")

        # Migrate selected legacy message customizations. Defaults are source-owned and not duplicated.
        if "messages" in {r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            for legacy_key, slot_key in LEGACY_MESSAGE_MAP.items():
                db.cur.execute("SELECT * FROM messages WHERE key=?", (legacy_key,))
                row = db.cur.fetchone()
                if not row:
                    continue
                keys = set(row.keys())
                published = row["text"] if "text" in keys else None
                draft = row["draft_text"] if "draft_text" in keys else None
                photo = row["photo_file_id"] if "photo_file_id" in keys else None
                draft_photo = row["draft_photo_file_id"] if "draft_photo_file_id" in keys else None
                if any(v not in (None, "") for v in (published, draft, photo, draft_photo)):
                    db.cur.execute(
                        """
                        INSERT INTO content_templates(slot_key,scope_type,scope_id,published_text,draft_text,published_photo_file_id,draft_photo_file_id,parse_mode,is_active)
                        VALUES (?,?,0,?,?,?,?, 'plain',1)
                        ON CONFLICT(slot_key,scope_type,scope_id) DO NOTHING
                        """,
                        (slot_key, SCOPE_GLOBAL, published, draft, photo, draft_photo),
                    )

        # Close only deprecated text/template editor states. Their data tables are preserved,
        # but stale FSM sessions must not reopen a second editor after the upgrade.
        if "fsm_state" in {r["name"] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            db.cur.execute(
                """
                DELETE FROM fsm_state
                WHERE state LIKE '%waiting_message_edit'
                   OR state LIKE '%waiting_template_title'
                   OR state LIKE '%waiting_template_body'
                   OR state LIKE '%waiting_template_edit'
                   OR state LIKE '%waiting_template_assign'
                """
            )

        legacy_status_defaults = {
            "bot_disabled_message": "⛔ ربات موقتاً غیرفعال است.\n\nلطفاً کمی بعد دوباره مراجعه کنید.",
            "sales_closed_message": "⛔ فروش در حال حاضر بسته است.\n\nدر حال بروزرسانی موجودی سرویس‌ها هستیم. لطفاً بعداً دوباره تلاش کنید.",
        }
        for setting_key, slot_key in (("bot_disabled_message", "bot_disabled"), ("sales_closed_message", "sales_closed")):
            db.cur.execute("SELECT value FROM settings WHERE key=?", (setting_key,))
            legacy_row = db.cur.fetchone()
            legacy_text = (legacy_row["value"] if legacy_row else "") or ""
            if legacy_text.strip() and legacy_text.strip() != legacy_status_defaults[setting_key]:
                db.cur.execute(
                    """
                    INSERT INTO content_templates(slot_key,scope_type,scope_id,published_text,parse_mode,is_active)
                    VALUES (?,?,0,?,'plain',1)
                    ON CONFLICT(slot_key,scope_type,scope_id) DO NOTHING
                    """,
                    (slot_key, SCOPE_GLOBAL, legacy_text.strip()),
                )

        db.cur.execute(
            "INSERT INTO settings(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        db.conn.commit()


def definition(key: str) -> ContentDefinition:
    item = DEFINITION_MAP.get(str(key or ""))
    if not item:
        raise ValueError("کلید محتوایی معتبر نیست.")
    return item


def definitions_by_category(category: str) -> list[ContentDefinition]:
    return [item for item in DEFINITIONS if item.category == category]


def categories() -> list[tuple[str, str]]:
    return [(key, label) for key, label in CATEGORIES.items() if definitions_by_category(key)]


def _normalize_scope(scope_type: str, scope_id: int | None = None) -> tuple[str, int]:
    scope_type = (scope_type or SCOPE_GLOBAL).strip().lower()
    if scope_type not in {SCOPE_GLOBAL, SCOPE_CATEGORY, SCOPE_PLAN}:
        raise ValueError("دامنه قالب معتبر نیست.")
    sid = 0 if scope_type == SCOPE_GLOBAL else int(scope_id or 0)
    if scope_type != SCOPE_GLOBAL and sid <= 0:
        raise ValueError("شناسه دامنه معتبر نیست.")
    return scope_type, sid


def get_template(key: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0):
    definition(key)
    scope_type, scope_id = _normalize_scope(scope_type, scope_id)
    db.cur.execute("SELECT * FROM content_templates WHERE slot_key=? AND scope_type=? AND scope_id=?", (key, scope_type, scope_id))
    return db.cur.fetchone()


def _ensure_template(key: str, scope_type: str, scope_id: int, admin_id: str | int | None = None) -> int:
    item = definition(key)
    scope_type, scope_id = _normalize_scope(scope_type, scope_id)
    if scope_type not in item.scopes:
        raise ValueError("این متن در دامنه انتخاب‌شده قابل تخصیص نیست.")
    db.cur.execute(
        """
        INSERT INTO content_templates(slot_key,scope_type,scope_id,parse_mode,is_active,updated_by)
        VALUES (?,?,?,?,1,?)
        ON CONFLICT(slot_key,scope_type,scope_id) DO UPDATE SET is_active=1,updated_by=excluded.updated_by,updated_at=datetime('now')
        """,
        (key, scope_type, scope_id, item.default_parse_mode, str(admin_id) if admin_id is not None else None),
    )
    db.conn.commit()
    row = get_template(key, scope_type, scope_id)
    return int(row["id"])


def validate_text(key: str, text: str, parse_mode: str | None = None) -> str:
    item = definition(key)
    text = str(text or "").strip()
    mode = (parse_mode or item.default_parse_mode or "plain").lower()
    if mode not in PARSE_MODES:
        raise ValueError("حالت قالب‌بندی معتبر نیست.")
    if not text:
        raise ValueError("متن نمی‌تواند خالی باشد. برای بازگشت به پیش‌فرض از دکمه بازگردانی استفاده کنید.")
    if item.kind == "button":
        if "\n" in text:
            raise ValueError("متن دکمه باید تک‌خطی باشد.")
        if len(text) > BUTTON_LIMIT:
            raise ValueError(f"متن دکمه بیشتر از {BUTTON_LIMIT} کاراکتر است.")
        if "{" in text:
            fields = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", text))
        else:
            fields = set()
    else:
        if len(text) > TEXT_LIMIT:
            raise ValueError(f"متن از محدودیت امن {TEXT_LIMIT} کاراکتر عبور می‌کند.")
        fields = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", text))
    unknown = fields - set(item.fields)
    if unknown:
        raise ValueError("متغیر ناشناخته: " + ", ".join(sorted(unknown)))
    missing = set(item.required_fields) - fields
    if missing:
        raise ValueError("متغیر ضروری حذف شده است: " + ", ".join(sorted(missing)))
    # Detect malformed braces that str.format would otherwise fail on.
    stripped = re.sub(r"\{[a-zA-Z0-9_]+\}", "", text)
    if "{" in stripped or "}" in stripped:
        raise ValueError("آکولاد قالب خراب است. برای متن عادی از پرانتز استفاده کنید.")
    if mode == "html":
        validator = _HTMLValidator()
        validator.feed(text)
        validator.finish()
    elif mode == "markdownv2":
        if text.count("```") % 2:
            raise ValueError("بلوک کد MarkdownV2 بسته نشده است.")
        if text.replace("```", "").count("`") % 2:
            raise ValueError("کد تک‌خطی MarkdownV2 بسته نشده است.")
    return text


def save_draft(key: str, text: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, *, parse_mode: str | None = None, admin_id=None) -> int:
    item = definition(key)
    scope_type, scope_id = _normalize_scope(scope_type, scope_id)
    mode = (parse_mode or item.default_parse_mode).lower()
    clean = validate_text(key, text, mode)
    template_id = _ensure_template(key, scope_type, scope_id, admin_id)
    db.cur.execute(
        "UPDATE content_templates SET draft_text=?,draft_parse_mode=?,updated_by=?,updated_at=datetime('now') WHERE id=?",
        (clean, mode, str(admin_id) if admin_id is not None else None, template_id),
    )
    db.conn.commit()
    return template_id


def save_draft_photo(key: str, photo_file_id: str | None, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, *, admin_id=None) -> int:
    item = definition(key)
    if not item.allow_media:
        raise ValueError("این نوع محتوا رسانه ندارد.")
    template_id = _ensure_template(key, scope_type, scope_id, admin_id)
    db.cur.execute(
        "UPDATE content_templates SET draft_photo_file_id=?,updated_by=?,updated_at=datetime('now') WHERE id=?",
        (photo_file_id or "", str(admin_id) if admin_id is not None else None, template_id),
    )
    db.conn.commit()
    return template_id


def set_draft_parse_mode(key: str, parse_mode: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, *, admin_id=None) -> int:
    item = definition(key)
    mode = (parse_mode or "plain").lower()
    if mode not in PARSE_MODES:
        raise ValueError("حالت قالب‌بندی معتبر نیست.")
    template_id = _ensure_template(key, scope_type, scope_id, admin_id)
    row = get_template(key, scope_type, scope_id)
    current_text = row["draft_text"] or row["published_text"] or item.default_text
    validate_text(key, current_text, mode)
    db.cur.execute("UPDATE content_templates SET draft_parse_mode=?,updated_by=?,updated_at=datetime('now') WHERE id=?", (mode, str(admin_id) if admin_id is not None else None, template_id))
    db.conn.commit()
    return template_id


def _snapshot(row, action: str, admin_id=None) -> None:
    db.cur.execute(
        """
        INSERT INTO content_template_versions(template_id,slot_key,scope_type,scope_id,text_value,photo_file_id,parse_mode,action,admin_id)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (row["id"], row["slot_key"], row["scope_type"], row["scope_id"], row["published_text"], row["published_photo_file_id"], row["parse_mode"] or "plain", action, str(admin_id) if admin_id is not None else None),
    )


def publish(key: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, *, admin_id=None) -> bool:
    item = definition(key)
    row = get_template(key, scope_type, scope_id)
    if not row:
        return False
    draft_text = row["draft_text"]
    draft_photo = row["draft_photo_file_id"]
    draft_mode = row["draft_parse_mode"] or row["parse_mode"] or item.default_parse_mode
    if draft_text is None and draft_photo is None and row["draft_parse_mode"] is None:
        return False
    final_text = draft_text if draft_text is not None else (row["published_text"] or item.default_text)
    validate_text(key, final_text, draft_mode)
    _snapshot(row, "before_publish", admin_id)
    db.cur.execute(
        """
        UPDATE content_templates
        SET published_text=?,published_photo_file_id=COALESCE(draft_photo_file_id,published_photo_file_id),parse_mode=?,
            draft_text=NULL,draft_photo_file_id=NULL,draft_parse_mode=NULL,is_active=1,updated_by=?,updated_at=datetime('now'),published_at=datetime('now')
        WHERE id=?
        """,
        (final_text, draft_mode, str(admin_id) if admin_id is not None else None, row["id"]),
    )
    db.conn.commit()
    return True


def clear_draft(key: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0) -> bool:
    row = get_template(key, scope_type, scope_id)
    if not row:
        return False
    db.cur.execute("UPDATE content_templates SET draft_text=NULL,draft_photo_file_id=NULL,draft_parse_mode=NULL,updated_at=datetime('now') WHERE id=?", (row["id"],))
    db.conn.commit()
    return True


def restore_default(key: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, *, admin_id=None) -> bool:
    item = definition(key)
    row = get_template(key, scope_type, scope_id)
    if row:
        _snapshot(row, "before_restore_default", admin_id)
        if scope_type == SCOPE_GLOBAL:
            db.cur.execute(
                """
                UPDATE content_templates SET published_text=NULL,draft_text=NULL,published_photo_file_id=NULL,draft_photo_file_id=NULL,
                    parse_mode=?,draft_parse_mode=NULL,is_active=1,updated_by=?,updated_at=datetime('now'),published_at=datetime('now') WHERE id=?
                """,
                (item.default_parse_mode, str(admin_id) if admin_id is not None else None, row["id"]),
            )
        else:
            # Disable the override but keep its version history so it can be restored later.
            db.cur.execute(
                """
                UPDATE content_templates
                SET published_text=NULL,draft_text=NULL,published_photo_file_id=NULL,draft_photo_file_id=NULL,
                    draft_parse_mode=NULL,is_active=0,updated_by=?,updated_at=datetime('now')
                WHERE id=?
                """,
                (str(admin_id) if admin_id is not None else None, row["id"]),
            )
        db.conn.commit()
        return True
    return scope_type != SCOPE_GLOBAL




def set_active(key: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, active: bool = True, *, admin_id=None) -> bool:
    """Enable or disable one published override without deleting text or history."""
    row = get_template(key, scope_type, scope_id)
    if not row:
        return False
    db.cur.execute(
        "UPDATE content_templates SET is_active=?,updated_by=?,updated_at=datetime('now') WHERE id=?",
        (1 if active else 0, str(admin_id) if admin_id is not None else None, row["id"]),
    )
    db.conn.commit()
    return True


def list_versions(key: str, scope_type: str = SCOPE_GLOBAL, scope_id: int = 0, limit: int = 8):
    row = get_template(key, scope_type, scope_id)
    if not row:
        return []
    db.cur.execute("SELECT * FROM content_template_versions WHERE template_id=? ORDER BY id DESC LIMIT ?", (row["id"], max(1, min(20, int(limit)))))
    return db.cur.fetchall()


def restore_version(version_id: int, *, admin_id=None) -> bool:
    db.cur.execute("SELECT * FROM content_template_versions WHERE id=?", (int(version_id),))
    version = db.cur.fetchone()
    if not version:
        return False
    db.cur.execute("SELECT * FROM content_templates WHERE id=?", (version["template_id"],))
    row = db.cur.fetchone()
    if not row:
        return False
    _snapshot(row, "before_restore_version", admin_id)
    db.cur.execute(
        """
        UPDATE content_templates SET published_text=?,published_photo_file_id=?,parse_mode=?,draft_text=NULL,draft_photo_file_id=NULL,draft_parse_mode=NULL,
            is_active=1,updated_by=?,updated_at=datetime('now'),published_at=datetime('now') WHERE id=?
        """,
        (version["text_value"], version["photo_file_id"], version["parse_mode"] or "plain", str(admin_id) if admin_id is not None else None, row["id"]),
    )
    db.conn.commit()
    return True


def _scope_candidates(item: ContentDefinition, category_id: int | None, plan_id: int | None) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    if plan_id and not category_id:
        plan = db.get_plan(int(plan_id))
        if plan and plan["category_id"]:
            category_id = int(plan["category_id"])
    if plan_id and SCOPE_PLAN in item.scopes:
        result.append((SCOPE_PLAN, int(plan_id)))
    if category_id and SCOPE_CATEGORY in item.scopes:
        result.append((SCOPE_CATEGORY, int(category_id)))
    result.append((SCOPE_GLOBAL, 0))
    return result


def resolved_template(key: str, *, category_id: int | None = None, plan_id: int | None = None, draft: bool = False):
    item = definition(key)
    for scope_type, scope_id in _scope_candidates(item, category_id, plan_id):
        row = get_template(key, scope_type, scope_id)
        if row and int(row["is_active"] or 0):
            text = row["draft_text"] if draft and row["draft_text"] is not None else row["published_text"]
            photo = row["draft_photo_file_id"] if draft and row["draft_photo_file_id"] is not None else row["published_photo_file_id"]
            mode = row["draft_parse_mode"] if draft and row["draft_parse_mode"] else row["parse_mode"]
            if text not in (None, "") or photo not in (None, ""):
                return {
                    "text": text if text not in (None, "") else item.default_text,
                    "photo_file_id": photo or None,
                    "parse_mode": (mode or item.default_parse_mode),
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "template_id": row["id"],
                }
    return {"text": item.default_text, "photo_file_id": None, "parse_mode": item.default_parse_mode, "scope_type": "source", "scope_id": 0, "template_id": None}


def _escape_markdown_v2(value: str) -> str:
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", value)


def _escape_value(value: Any, mode: str) -> str:
    text = "" if value is None else str(value)
    if mode == "html":
        return html.escape(text, quote=True)
    if mode == "markdownv2":
        return _escape_markdown_v2(text)
    return text


def render(key: str, values: dict[str, Any] | None = None, *, category_id: int | None = None, plan_id: int | None = None, draft: bool = False) -> dict[str, Any]:
    item = definition(key)
    resolved = resolved_template(key, category_id=category_id, plan_id=plan_id, draft=draft)
    mode = resolved["parse_mode"] or item.default_parse_mode
    raw_values = dict(SAMPLE_CONTEXT)
    raw_values.update(values or {})
    safe_values = {field: _escape_value(raw_values.get(field, ""), mode) for field in item.fields}
    try:
        text = str(resolved["text"] or item.default_text).format_map(_SafeDict(safe_values))
    except (ValueError, KeyError) as exc:
        raise ValueError(f"رندر قالب ناموفق بود: {exc}") from exc
    text = _clean_rendered(text)
    if item.kind == "button":
        text = re.sub(r"<[^>]+>", "", text).replace("\n", " ").strip()
        if len(text) > BUTTON_LIMIT:
            text = text[: BUTTON_LIMIT - 1].rstrip() + "…"
        return {**resolved, "text": text, "parse_mode_api": None}
    if len(text) > 4096:
        raise ValueError("متن نهایی از محدودیت تلگرام عبور کرد؛ متن قالب یا مقادیر را کوتاه‌تر کنید.")
    return {**resolved, "text": text, "parse_mode_api": PARSE_MODES.get(mode)}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def _clean_rendered(text: str) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    clean: list[str] = []
    blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and blank:
            continue
        clean.append(line)
        blank = is_blank
    return "\n".join(clean).strip()


def render_button(key: str, values: dict[str, Any] | None = None, *, category_id=None, plan_id=None) -> str:
    item = definition(key)
    if item.kind != "button":
        raise ValueError("این کلید از نوع دکمه نیست.")
    return render(key, values, category_id=category_id, plan_id=plan_id)["text"]


async def send(target, key: str, values: dict[str, Any] | None = None, *, category_id=None, plan_id=None, reply_markup=None, draft=False):
    result = render(key, values, category_id=category_id, plan_id=plan_id, draft=draft)
    text = result["text"]
    photo = result["photo_file_id"]
    parse_mode = result["parse_mode_api"]
    if photo:
        if len(text) <= CAPTION_LIMIT:
            return [await target.answer_photo(photo, caption=text, parse_mode=parse_mode, reply_markup=reply_markup)]
        sent = [await target.answer_photo(photo)]
        sent.append(await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup))
        return sent
    return [await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)]


def preview(key: str, *, category_id=None, plan_id=None, scope_type=SCOPE_GLOBAL, scope_id=0) -> dict[str, Any]:
    # draft preview respects explicit scope by selecting its real related plan/category.
    values = sample_context(category_id=category_id, plan_id=plan_id)
    return render(key, values, category_id=category_id, plan_id=plan_id, draft=True)


def sample_context(*, category_id=None, plan_id=None) -> dict[str, Any]:
    values = dict(SAMPLE_CONTEXT)
    plan = db.get_plan(plan_id) if plan_id else None
    category = None
    if plan:
        category_id = plan["category_id"]
    if category_id:
        category = db.get_plan_category(category_id)
    if not plan:
        plans = db.list_plans(active_only=True, category_id=category_id, limit=1) if category_id else db.list_plans(active_only=True, limit=1)
        plan = plans[0] if plans else None
    if not category and plan and plan["category_id"]:
        category = db.get_plan_category(plan["category_id"])
    if category:
        values.update({
            "category_title": category["title"] or "سرویس‌ها",
            "category_emoji": category["emoji"] or "📦",
            "category_description": category["description"] or "",
        })
    if plan:
        values.update({
            "title": plan["title"] or "بسته سرویس",
            "package_title": plan["title"] or plan["volume_label"] or "بسته سرویس",
            "plan_title": plan["title"] or "سرویس",
            "volume": plan["volume_label"] or "-",
            "duration": plan["duration_label"] or "-",
            "price": money(plan["price"]),
            "devices": "نامحدود" if plan["panel_max_devices"] in (None, "", 0) else f"{plan['panel_max_devices']} دستگاه",
            "plan_description": plan["description"] or "",
            "pre_purchase_text": plan["pre_purchase_text"] or "",
            "tag": plan["tag"] or "",
        })
    return values


def money(value: Any) -> str:
    try:
        return f"{int(value or 0):,} تومان"
    except (TypeError, ValueError):
        return str(value or "0 تومان")


def bytes_gb(value: Any, *, dash_if_zero: bool = False) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if not number and dash_if_zero:
        return "-"
    return f"{number / 1024**3:.2f} گیگ"


def get_display_settings(*, category_id: int | None = None, plan_id: int | None = None) -> dict[str, bool]:
    result = dict(DEFAULT_DISPLAY_SETTINGS)
    if plan_id and not category_id:
        plan = db.get_plan(int(plan_id))
        if plan and plan["category_id"]:
            category_id = int(plan["category_id"])
    candidates = [(SCOPE_GLOBAL, 0)]
    if category_id:
        candidates.append((SCOPE_CATEGORY, int(category_id)))
    if plan_id:
        candidates.append((SCOPE_PLAN, int(plan_id)))
    for scope_type, scope_id in candidates:
        db.cur.execute("SELECT settings_json FROM content_display_settings WHERE scope_type=? AND scope_id=?", (scope_type, scope_id))
        row = db.cur.fetchone()
        if not row:
            continue
        try:
            data = json.loads(row["settings_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        for key in DEFAULT_DISPLAY_SETTINGS:
            if key in data:
                result[key] = bool(data[key])
    return result


def set_display_setting(scope_type: str, scope_id: int, key: str, value: bool, *, admin_id=None) -> None:
    if key not in DEFAULT_DISPLAY_SETTINGS:
        raise ValueError("تنظیم نمایش معتبر نیست.")
    scope_type, scope_id = _normalize_scope(scope_type, scope_id)
    db.cur.execute("SELECT settings_json FROM content_display_settings WHERE scope_type=? AND scope_id=?", (scope_type, scope_id))
    row = db.cur.fetchone()
    try:
        data = json.loads(row["settings_json"] or "{}") if row else {}
    except json.JSONDecodeError:
        data = {}
    data[key] = bool(value)
    db.cur.execute(
        """
        INSERT INTO content_display_settings(scope_type,scope_id,settings_json,updated_by)
        VALUES (?,?,?,?)
        ON CONFLICT(scope_type,scope_id) DO UPDATE SET settings_json=excluded.settings_json,updated_by=excluded.updated_by,updated_at=datetime('now')
        """,
        (scope_type, scope_id, json.dumps(data, ensure_ascii=False), str(admin_id) if admin_id is not None else None),
    )
    db.conn.commit()


def clear_display_settings(scope_type: str, scope_id: int) -> None:
    scope_type, scope_id = _normalize_scope(scope_type, scope_id)
    db.cur.execute("DELETE FROM content_display_settings WHERE scope_type=? AND scope_id=?", (scope_type, scope_id))
    db.conn.commit()


def record_funnel(user_id: str | int, event_type: str, *, category_id=None, plan_id=None, purchase_id=None, session_key=None, metadata: dict[str, Any] | None = None) -> None:
    allowed = {"buy_open", "category_view", "plan_checkout", "payment_started", "payment_success", "purchase_queued", "purchase_refunded", "purchase_delivered", "purchase_cancelled"}
    if event_type not in allowed:
        return
    db.cur.execute(
        """
        INSERT INTO purchase_funnel_events(user_id,event_type,category_id,plan_id,purchase_id,session_key,metadata_json)
        VALUES (?,?,?,?,?,?,?)
        """,
        (str(user_id), event_type, int(category_id) if category_id else None, int(plan_id) if plan_id else None, int(purchase_id) if purchase_id else None, session_key, json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))),
    )
    db.conn.commit()


def funnel_report(days: int = 30) -> list[tuple[str, int]]:
    days = max(1, min(365, int(days)))
    db.cur.execute(
        """
        SELECT event_type,COUNT(DISTINCT user_id || ':' || COALESCE(session_key, substr(created_at,1,13))) AS total
        FROM purchase_funnel_events
        WHERE created_at>=datetime('now',?)
        GROUP BY event_type
        """,
        (f"-{days} days",),
    )
    counts = {row["event_type"]: int(row["total"] or 0) for row in db.cur.fetchall()}
    order = ["buy_open", "category_view", "plan_checkout", "payment_started", "payment_success", "purchase_delivered"]
    return [(key, counts.get(key, 0)) for key in order]
