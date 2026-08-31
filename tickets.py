"""Telegram support ticket flow.

Tickets are intentionally lightweight: the customer's message is copied to every
admin and admin replies are routed back through the bot. Delivery failures are
logged instead of being silently swallowed.
"""

import logging

from aiogram import Bot, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

import content
import db
import menus
from config import ADMIN_IDS

logger = logging.getLogger(__name__)


class TicketStates(StatesGroup):
    waiting_message = State()


def is_admin(user_id) -> bool:
    return int(user_id) in ADMIN_IDS


def cancel_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ لغو", callback_data="cancel_fsm"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


async def cb_ticket_start(c: types.CallbackQuery):
    await c.answer()
    sent = (await content.send(c.message, "support_prompt", reply_markup=cancel_kb()))[-1]
    db.track_bot_message(sent.chat.id, c.from_user.id, sent.message_id, "ticket_form", kind="temp")
    await TicketStates.waiting_message.set()


async def process_ticket_message(m: types.Message, state: FSMContext):
    bot = Bot.get_current()
    await state.finish()
    ticket_id = db.create_ticket(m.from_user.id)

    header = (
        f"🎫 تیکت جدید #{ticket_id}\n"
        f"👤 از: {m.from_user.full_name} (@{m.from_user.username or '---'}) | ID: {m.from_user.id}\n"
    )

    delivered_to_admins = 0
    for admin_id in ADMIN_IDS:
        try:
            if m.content_type == "text":
                sent = await bot.send_message(admin_id, header + "\n" + (m.text or ""))
            else:
                caption = header + ("\n" + m.caption if m.caption else "")
                sent = await bot.copy_message(
                    admin_id,
                    m.chat.id,
                    m.message_id,
                    caption=caption,
                )
            db.record_ticket_message(admin_id, sent.message_id, ticket_id, m.from_user.id)
            delivered_to_admins += 1
        except Exception:
            logger.exception("Could not deliver ticket %s to admin %s", ticket_id, admin_id)

    if delivered_to_admins == 0:
        logger.error("Ticket %s was stored but could not be delivered to any admin", ticket_id)

    await content.send(
        m, "support_created", {"ticket_id": ticket_id},
        reply_markup=menus.main_reply_kb(m.from_user.id),
    )


def _is_ticket_reply(message: types.Message) -> bool:
    if not message.reply_to_message or not is_admin(message.from_user.id):
        return False
    return db.get_ticket_message_map(
        message.from_user.id,
        message.reply_to_message.message_id,
    ) is not None


async def handle_ticket_reply(m: types.Message):
    bot = Bot.get_current()
    row = db.get_ticket_message_map(m.from_user.id, m.reply_to_message.message_id)
    if not row:
        return

    ticket_id, customer_id = row["ticket_id"], row["user_id"]
    header = f"💬 پاسخ پشتیبانی (تیکت #{ticket_id}):\n"

    try:
        if m.content_type == "text":
            await bot.send_message(
                int(customer_id),
                header + "\n" + (m.text or ""),
                reply_markup=menus.main_reply_kb(customer_id),
            )
        else:
            caption = header + ("\n" + m.caption if m.caption else "")
            await bot.copy_message(int(customer_id), m.chat.id, m.message_id, caption=caption)
            await bot.send_message(
                int(customer_id),
                "از منوی پایین می‌تونید ادامه بدید.",
                reply_markup=menus.main_reply_kb(customer_id),
            )
        db.log_admin_action(m.from_user.id, "reply_ticket", customer_id, f"ticket_id={ticket_id}")
        await m.reply("✅ پاسخ برای مشتری ارسال شد.")
    except Exception:
        logger.exception("Could not send ticket %s reply to user %s", ticket_id, customer_id)
        await m.reply("❌ ارسال پاسخ به مشتری ناموفق بود (احتمالاً ربات رو بلاک کرده).")


async def cb_open_tickets(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer()

    await c.answer()
    rows = db.list_open_tickets()
    if not rows:
        return await c.message.answer("تیکت باز وجود نداره.", reply_markup=menus.admin_back_inline())

    for row in rows:
        user = db.get_user(row["user_id"])
        username = user["username"] if user else ""
        text = (
            f"🎫 تیکت #{row['id']}\n"
            f"@{username or '-'} | ID: {row['user_id']}\n"
            f"{row['created_at']}"
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("✅ بستن تیکت", callback_data=f"ticket_close_{row['id']}"))
        kb.add(types.InlineKeyboardButton("⬅️ بازگشت به پنل مدیریت", callback_data="adm_back"))
        sent = await c.message.answer(text, reply_markup=kb)
        db.track_bot_message(sent.chat.id, c.from_user.id, sent.message_id, "open_tickets", kind="list")


async def cb_close_ticket(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer()

    await c.answer()
    ticket_id = int(c.data.split("_")[-1])
    db.close_ticket(ticket_id)
    db.log_admin_action(c.from_user.id, "close_ticket", details=f"ticket_id={ticket_id}")

    try:
        await c.message.edit_text((c.message.text or "") + "\n\n✅ بسته شد.")
    except Exception:
        logger.debug("Ticket message could not be edited", exc_info=True)
        await c.message.answer(f"تیکت #{ticket_id} بسته شد.", reply_markup=menus.admin_back_inline())


def register(dp):
    dp.register_callback_query_handler(cb_ticket_start, lambda c: c.data == "ticket_start")
    dp.register_message_handler(
        process_ticket_message,
        content_types=types.ContentTypes.ANY,
        state=TicketStates.waiting_message,
    )
    dp.register_message_handler(
        handle_ticket_reply,
        _is_ticket_reply,
        content_types=types.ContentTypes.ANY,
    )
    dp.register_callback_query_handler(cb_open_tickets, lambda c: c.data == "adm_tickets")
    dp.register_callback_query_handler(cb_close_ticket, lambda c: c.data.startswith("ticket_close_"))
