"""Runtime-editable settings stored in SQLite."""

from __future__ import annotations

from db import get_setting, get_setting_int, set_setting

DEFAULTS = {
    "plan_title": "یک ماهه | ۱۰۰ گیگ | ۳ کاربره",
    "plan_duration_label": "۳۰ روز",
    "plan_price": "100000",
    "ref_reward": "30000",
    "card_number": "0000-0000-0000-0000",
    "card_holder": "نام صاحب کارت",
    "min_topup": "50000",
    "low_stock_threshold": "5",
    "bot_enabled": "1",
    "bot_disabled_message": "⛔ ربات موقتاً غیرفعال است.\n\nلطفاً کمی بعد دوباره مراجعه کنید.",
    "sales_enabled": "1",
    "sales_closed_message": "⛔ فروش در حال حاضر بسته است.\n\nدر حال بروزرسانی موجودی سرویس‌ها هستیم. لطفاً بعداً دوباره تلاش کنید.",
    "force_join_enabled": "0",
    "force_join_channel": "",
    "force_join_invite_url": "",
    "force_join_message": "🔒 برای استفاده از ربات، ابتدا در کانال زیر عضو شوید و سپس دکمه «✅ عضو شدم» را بزنید.",
}


def ensure_defaults() -> None:
    for key, value in DEFAULTS.items():
        if get_setting(key) is None:
            set_setting(key, value)


def plan_title() -> str:
    return get_setting("plan_title", DEFAULTS["plan_title"])


def plan_duration_label() -> str:
    return get_setting("plan_duration_label", DEFAULTS["plan_duration_label"])


def plan_price() -> int:
    return get_setting_int("plan_price", int(DEFAULTS["plan_price"]))


def ref_reward() -> int:
    return get_setting_int("ref_reward", int(DEFAULTS["ref_reward"]))


def card_number() -> str:
    return get_setting("card_number", DEFAULTS["card_number"])


def card_holder() -> str:
    return get_setting("card_holder", DEFAULTS["card_holder"])


def min_topup() -> int:
    return get_setting_int("min_topup", int(DEFAULTS["min_topup"]))


def low_stock_threshold() -> int:
    return get_setting_int("low_stock_threshold", int(DEFAULTS["low_stock_threshold"]))


def bot_enabled() -> bool:
    return get_setting_int("bot_enabled", int(DEFAULTS["bot_enabled"])) == 1


def bot_disabled_message() -> str:
    return get_setting("bot_disabled_message", DEFAULTS["bot_disabled_message"])


def sales_enabled() -> bool:
    return get_setting_int("sales_enabled", int(DEFAULTS["sales_enabled"])) == 1


def sales_closed_message() -> str:
    return get_setting("sales_closed_message", DEFAULTS["sales_closed_message"])


def force_join_enabled() -> bool:
    return get_setting_int("force_join_enabled", int(DEFAULTS["force_join_enabled"])) == 1


def force_join_channel() -> str:
    return get_setting("force_join_channel", DEFAULTS["force_join_channel"])


def force_join_invite_url() -> str:
    return get_setting("force_join_invite_url", DEFAULTS["force_join_invite_url"])


def force_join_message() -> str:
    return get_setting("force_join_message", DEFAULTS["force_join_message"])


def force_join_configured() -> bool:
    return bool(force_join_channel().strip())


def trial_provider_key() -> str:
    """Which configured provider serves the free trial account.

    Runtime-switchable from the admin panel (Providers section), unlike
    most other provider settings which live in .env. Falls back to
    config.TRIAL_PROVIDER_KEY (the original env-only default) if the admin
    hasn't picked one explicitly yet.
    """
    from config import TRIAL_PROVIDER_KEY
    return get_setting("trial_provider_key", "") or TRIAL_PROVIDER_KEY
