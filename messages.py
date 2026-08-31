"""Compatibility layer for legacy message calls.

Since v6.4 all editable customer text is stored by :mod:`content`. Existing
handlers keep using messages.send/compose, while the admin sees one unified
content center with draft, preview, publish, media and version history.
"""
from __future__ import annotations

import content

MESSAGE_KEYS = [
    ("welcome", "پیام خوش‌آمد /start"),
    ("main_menu", "پیام منوی اصلی"),
    ("menu_buy", "پیام صفحه خرید"),
    ("menu_wallet", "پیام صفحه کیف پول"),
    ("menu_referral", "پیام صفحه دعوت دوستان"),
    ("my_services_empty", "متن خالی بودن سرویس‌های من"),
    ("guide_home", "متن اصلی آموزش اتصال"),
    ("guide_android", "آموزش اتصال اندروید"),
    ("guide_ios", "آموزش اتصال آیفون"),
    ("guide_windows", "آموزش اتصال ویندوز"),
    ("guide_mac", "آموزش اتصال مک"),
    ("guide_troubleshoot", "راهنمای مشکل اتصال"),
    ("guide_update", "آموزش بروزرسانی ساب‌لینک"),
    ("support_intro", "متن شروع پشتیبانی"),
    ("rules", "قوانین و شرایط خرید"),
]

_VALID_KEYS = {key for key, _ in MESSAGE_KEYS}
DYNAMIC_MESSAGE_KEYS = {"menu_buy", "menu_wallet", "menu_referral"}
LEGACY_TO_CONTENT = dict(content.LEGACY_MESSAGE_MAP)
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


def is_valid_key(key):
    return key in _VALID_KEYS


def is_dynamic_key(key):
    return key in DYNAMIC_MESSAGE_KEYS


def _slot(key):
    slot = LEGACY_TO_CONTENT.get(key)
    if not slot:
        raise ValueError("invalid message key")
    return slot


def split_text(text, limit=TELEGRAM_TEXT_LIMIT):
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def get(key):
    slot = _slot(key)
    row = content.get_template(slot, content.SCOPE_GLOBAL, 0)
    if not row:
        return None, None
    return row["published_text"], row["published_photo_file_id"]


def get_draft(key):
    slot = _slot(key)
    row = content.get_template(slot, content.SCOPE_GLOBAL, 0)
    if not row:
        return None, None
    return row["draft_text"], row["draft_photo_file_id"]


def compose(key, default_text):
    slot = _slot(key)
    result = content.render(slot, {"body": default_text})
    return result["text"], result["photo_file_id"]


def compose_preview(key, default_text):
    slot = _slot(key)
    result = content.render(slot, {"body": default_text}, draft=True)
    return result["text"], result["photo_file_id"]


async def send(target, key, body_text, reply_markup=None):
    slot = _slot(key)
    return await content.send(target, slot, {"body": body_text}, reply_markup=reply_markup)


def _validate_key(key):
    if key not in _VALID_KEYS:
        raise ValueError("invalid message key")


def set_text(key, text):
    _validate_key(key)
    slot = _slot(key)
    content.save_draft(slot, text, admin_id=None)
    content.publish(slot)


def set_photo(key, photo_file_id):
    _validate_key(key)
    slot = _slot(key)
    content.save_draft_photo(slot, photo_file_id)
    content.publish(slot)


def set_draft_text(key, text):
    _validate_key(key)
    content.save_draft(_slot(key), text)


def set_draft_photo(key, photo_file_id):
    _validate_key(key)
    content.save_draft_photo(_slot(key), photo_file_id)


def publish_draft(key):
    _validate_key(key)
    return content.publish(_slot(key))


def clear_draft(key):
    _validate_key(key)
    return content.clear_draft(_slot(key))


def clear(key):
    _validate_key(key)
    return content.restore_default(_slot(key))
