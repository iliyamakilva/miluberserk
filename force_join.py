"""Mandatory channel-membership gate.

Implemented as an aiogram middleware so it sits in front of every message
and callback handler with a single point of control, instead of adding a
membership-check lambda to each existing handler (which is fragile and easy
to forget on new handlers).

Admins and calls related to the "check membership again" flow itself are
always let through. Everyone else is blocked with a join prompt until
bot.get_chat_member confirms they are a member/administrator/creator of the
configured channel.

If the channel is misconfigured or Telegram returns an error (e.g. the bot
is not an admin of the channel, or the channel id is wrong), the gate
fails OPEN (lets the user through) rather than bricking the whole bot, and
logs a warning so the admin notices in the logs.
"""

from __future__ import annotations

import logging

from aiogram import types
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import settings
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

RECHECK_CALLBACK = "force_join_check"

_MEMBER_STATUSES = {"member", "administrator", "creator"}

# Small in-memory cache so we don't hit get_chat_member on every single
# message/callback for the same user; membership rarely changes second to
# second. Cleared automatically after CACHE_SECONDS.
_cache: dict[str, tuple[bool, float]] = {}
CACHE_SECONDS = 60.0


def _is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS


def _join_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    invite_url = settings.force_join_invite_url().strip()
    channel = settings.force_join_channel().strip()
    if invite_url:
        kb.add(InlineKeyboardButton("📢 عضویت در کانال", url=invite_url))
    elif channel.startswith("@"):
        kb.add(InlineKeyboardButton("📢 عضویت در کانال", url=f"https://t.me/{channel.lstrip('@')}"))
    kb.add(InlineKeyboardButton("✅ عضو شدم", callback_data=RECHECK_CALLBACK))
    return kb


async def is_member(bot, user_id: int, *, use_cache: bool = True) -> bool:
    import time as _time

    channel = settings.force_join_channel().strip()
    if not channel:
        return True
    cache_key = f"{channel}:{user_id}"
    if use_cache:
        cached = _cache.get(cache_key)
        if cached and _time.monotonic() - cached[1] < CACHE_SECONDS:
            return cached[0]
    try:
        member = await bot.get_chat_member(channel, user_id)
        result = member.status in _MEMBER_STATUSES
    except TelegramAPIError as exc:
        logger.warning("force_join: get_chat_member failed for channel=%s user=%s: %s", channel, user_id, exc)
        # Fail open: a misconfigured channel/permission should not lock
        # every user out of the bot.
        return True
    _cache[cache_key] = (result, _time.monotonic())
    return result


def invalidate_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _cache.clear()
        return
    channel = settings.force_join_channel().strip()
    _cache.pop(f"{channel}:{user_id}", None)


async def _send_prompt(bot, chat_id: int) -> None:
    await bot.send_message(chat_id, settings.force_join_message(), reply_markup=_join_kb())


class ForceJoinMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()

    async def on_process_message(self, message: types.Message, data: dict):
        if not settings.force_join_enabled() or not settings.force_join_configured():
            return
        if _is_admin(message.from_user.id):
            return
        if await is_member(message.bot, message.from_user.id):
            return
        await _send_prompt(message.bot, message.chat.id)
        raise CancelHandler()

    async def on_process_callback_query(self, call: types.CallbackQuery, data: dict):
        if call.data == RECHECK_CALLBACK:
            return
        if not settings.force_join_enabled() or not settings.force_join_configured():
            return
        if _is_admin(call.from_user.id):
            return
        if await is_member(call.bot, call.from_user.id):
            return
        await call.answer("ابتدا در کانال عضو شوید.", show_alert=True)
        raise CancelHandler()


async def cb_force_join_check(c: types.CallbackQuery):
    invalidate_cache(c.from_user.id)
    if await is_member(c.bot, c.from_user.id, use_cache=False):
        await c.answer("✅ عضویت شما تایید شد.", show_alert=False)
        try:
            await c.message.delete()
        except TelegramAPIError:
            pass
        # Let the user know they can carry on; re-sending /start is the
        # simplest way to hand them back into the normal flow.
        await c.message.answer("✅ عضویت شما تایید شد. برای شروع /start را بزنید.")
    else:
        await c.answer("هنوز در کانال عضو نشده‌اید.", show_alert=True)


def register(dp) -> None:
    dp.middleware.setup(ForceJoinMiddleware())
    dp.register_callback_query_handler(cb_force_join_check, lambda c: c.data == RECHECK_CALLBACK, state="*")
