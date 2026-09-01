import logging

from aiogram import types
from config import ADMIN_IDS, TRIAL_ENABLED

logger = logging.getLogger(__name__)

# عنوان‌های پیش‌فرض فقط برای fallback هستند. عنوان واقعی دکمه‌های سیستمی از db.system_buttons خوانده می‌شود.
BTN_BUY = "🛒 خرید سرویس"
BTN_MY_SUBS = "📦 سرویس‌های من"
BTN_WALLET = "💳 کیف پول"
BTN_GUIDE = "📚 آموزش اتصال"
BTN_TRIAL = "🧪 اکانت تست"
BTN_REFERRAL = "👥 دعوت دوستان"
BTN_TICKET = "🎫 پشتیبانی"
BTN_ADMIN = "⚙️ مدیریت"
BTN_MAIN = "🏠 منوی اصلی"

DEFAULT_SYSTEM_BUTTON_TITLES = {
    "buy": BTN_BUY,
    "my_subs": BTN_MY_SUBS,
    "wallet": BTN_WALLET,
    "guide": BTN_GUIDE,
    "trial": BTN_TRIAL,
    "referral": BTN_REFERRAL,
    "ticket": BTN_TICKET,
    "admin": BTN_ADMIN,
}

_SYSTEM_BUTTONS = set(DEFAULT_SYSTEM_BUTTON_TITLES.values()) | {BTN_MAIN}


def is_admin_user(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False



def trial_available() -> bool:
    if not TRIAL_ENABLED:
        return False
    try:
        import subs
        import settings
        return bool(subs.get_provider_adapter(settings.trial_provider_key()).configured())
    except Exception:
        return False

def system_button_title(key: str) -> str:
    try:
        import db
        return db.system_button_title(key)
    except Exception:
        logger.debug("Could not read system button title for %s", key, exc_info=True)
        return DEFAULT_SYSTEM_BUTTON_TITLES.get(key, key)


def matches_system_button(text: str, key: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if text == DEFAULT_SYSTEM_BUTTON_TITLES.get(key):
        return True
    try:
        import db
        row = db.get_system_button(key)
        return bool(row and int(row["is_active"] or 0) == 1 and text == (row["title"] or row["default_title"]))
    except Exception:
        logger.debug("Could not match system button %s", key, exc_info=True)
        return False


def system_buttons_for_location(location="main", user_id=None):
    fallback = [
        ("buy", BTN_BUY),
        ("my_subs", BTN_MY_SUBS),
        ("wallet", BTN_WALLET),
        ("guide", BTN_GUIDE),
        ("referral", BTN_REFERRAL),
        ("ticket", BTN_TICKET),
    ]
    try:
        import db
        rows = db.list_system_buttons(location=location, active_only=True)
        result = []
        for row in rows:
            key = row["key"]
            if key == "admin" and not is_admin_user(user_id):
                continue
            if key == "trial" and (not trial_available()):
                continue
            result.append((key, row["title"] or row["default_title"]))
        return result
    except Exception:
        logger.debug("Could not load system buttons for %s", location, exc_info=True)
        if location == "buy":
            if trial_available():
                return [("trial", BTN_TRIAL)]
            return []
        if location != "main":
            return []
        result = fallback[:]
        if user_id is not None and is_admin_user(user_id):
            result.append(("admin", BTN_ADMIN))
        return result


def _audience_visible(audience, user_id=None):
    audience = (audience or "all").strip().lower()
    if audience == "all":
        return True
    if audience == "admins":
        return is_admin_user(user_id)
    if user_id is None:
        return False
    try:
        import db
        user = db.get_user(str(user_id))
        purchased = int(user["purchased"] or 0) if user else 0
        services = db.delivered_sub_count_by_user(str(user_id))
        is_test = bool(user and int(user["is_test"] or 0))
    except Exception:
        return False
    return {
        "buyers": purchased > 0,
        "no_buy": purchased == 0,
        "has_service": services > 0,
        "no_service": services == 0,
        "normal": not is_test,
        "test": is_test,
    }.get(audience, False)


def custom_button_rows_for_location(location="main", user_id=None):
    try:
        import db
        rows = db.list_active_custom_buttons(location)
    except Exception:
        logger.debug("Could not load custom button rows for %s", location, exc_info=True)
        return []
    result = []
    for row in rows:
        title = (row["title"] or "").strip()
        if not title or title in _SYSTEM_BUTTONS:
            continue
        if not _audience_visible(row["audience"] or "all", user_id):
            continue
        result.append(row)
    return result


def ordered_location_items(location="main", user_id=None):
    """Merge system and custom buttons by one effective sort order."""
    items = []
    try:
        import db
        for row in db.list_system_buttons(location=location, active_only=True):
            key = row["key"]
            if key == "admin" and not is_admin_user(user_id):
                continue
            if key == "trial" and (not trial_available()):
                continue
            items.append({
                "kind": "system", "key": key,
                "title": row["title"] or row["default_title"],
                "sort_order": int(row["sort_order"] or 100),
            })
        for row in custom_button_rows_for_location(location, user_id):
            items.append({
                "kind": "custom", "id": int(row["id"]),
                "title": row["title"], "sort_order": int(row["sort_order"] or 100),
            })
        items.sort(key=lambda item: (item["sort_order"], 0 if item["kind"] == "system" else 1, item.get("key") or item.get("id")))
        return items
    except Exception:
        logger.debug("Could not merge location buttons for %s", location, exc_info=True)
        return [
            {"kind": "system", "key": key, "title": title, "sort_order": index * 10}
            for index, (key, title) in enumerate(system_buttons_for_location(location, user_id), start=1)
        ]


SYSTEM_INLINE_CALLBACKS = {
    "buy": "buy",
    "my_subs": "my_subs",
    "wallet": "wallet",
    "guide": "guide_home",
    "trial": "trial",
    "referral": "referral",
    "ticket": "ticket_start",
    "admin": "open_admin_panel",
}


def inline_location_buttons(location="main", user_id=None):
    buttons = []
    for item in ordered_location_items(location, user_id):
        if item["kind"] == "system":
            callback = SYSTEM_INLINE_CALLBACKS.get(item["key"])
            if callback:
                buttons.append(types.InlineKeyboardButton(item["title"], callback_data=callback))
        else:
            buttons.append(types.InlineKeyboardButton(item["title"], callback_data=f"custom_btn_{item['id']}"))
    return buttons


def append_location_buttons(kb, location="main", user_id=None, row_width=1):
    for button in inline_location_buttons(location, user_id):
        if row_width > 1:
            kb.insert(button)
        else:
            kb.add(button)
    return kb


def main_reply_kb(user_id=None):
    """
    منوی ثابت پایین تلگرام.
    عنوان، ترتیب و فعال/غیرفعال بودن دکمه‌های سیستمی از پنل ادمین قابل تغییر است؛
    اما عملکرد اصلی دکمه‌ها در کد قفل می‌ماند.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=False)
    all_buttons = [item["title"] for item in ordered_location_items("main", user_id)]
    for index in range(0, len(all_buttons), 2):
        kb.row(*all_buttons[index:index + 2])
    return kb


def back_main_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def admin_back_inline():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت به پنل مدیریت", callback_data="adm_back"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb
