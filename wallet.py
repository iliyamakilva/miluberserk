"""Wallet top-up, receipt review, and targeted checkout flow."""

import logging

from aiogram import Bot, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

import db
import commerce
import content
import menus
import settings
import subs
from affiliate import reward_ref
from config import ADMIN_IDS
from utils import cleanup_qr, make_qr, parse_int

logger = logging.getLogger(__name__)


async def _bot_send_content(bot: Bot, chat_id: int, key: str, values=None, *, category_id=None, plan_id=None, reply_markup=None):
    result = content.render(key, values or {}, category_id=category_id, plan_id=plan_id)
    if result["photo_file_id"]:
        if len(result["text"]) <= content.CAPTION_LIMIT:
            return await bot.send_photo(chat_id, result["photo_file_id"], caption=result["text"], parse_mode=result["parse_mode_api"], reply_markup=reply_markup)
        await bot.send_photo(chat_id, result["photo_file_id"])
    return await bot.send_message(chat_id, result["text"], parse_mode=result["parse_mode_api"], reply_markup=reply_markup)


class TopupStates(StatesGroup):
    waiting_amount = State()
    waiting_receipt = State()


def cancel_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ لغو", callback_data="cancel_fsm"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def topup_button_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💳 شارژ کیف پول", callback_data="topup_start"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


async def cb_topup_start(c: types.CallbackQuery):
    await c.answer()
    await content.send(
        c.message, "wallet_amount_prompt", {"min_amount": content.money(settings.min_topup())},
        reply_markup=cancel_kb(),
    )
    await TopupStates.waiting_amount.set()


async def process_amount(m: types.Message, state: FSMContext):
    if m.content_type != "text":
        return await content.send(m, "wallet_invalid_amount", {"min_amount": content.money(settings.min_topup())}, reply_markup=cancel_kb())

    amount = parse_int(m.text)

    if amount is None:
        return await content.send(m, "wallet_invalid_amount", {"min_amount": content.money(settings.min_topup())}, reply_markup=cancel_kb())
    min_amount = settings.min_topup()

    if amount < min_amount:
        return await content.send(m, "wallet_invalid_amount", {"min_amount": content.money(min_amount)}, reply_markup=cancel_kb())

    topup_id = db.create_topup(m.from_user.id, amount)
    await state.update_data(topup_id=topup_id)

    await content.send(
        m, "wallet_payment",
        {
            "amount": content.money(amount),
            "card_number": settings.card_number(),
            "card_holder": settings.card_holder(),
            "topup_id": topup_id,
        },
        reply_markup=cancel_kb(),
    )
    await TopupStates.waiting_receipt.set()


async def process_receipt(m: types.Message, state: FSMContext):
    bot = Bot.get_current()

    if m.content_type not in ("photo", "text"):
        return await content.send(m, "wallet_receipt_required", reply_markup=cancel_kb())

    data = await state.get_data()
    topup_id = data.get("topup_id")

    if not topup_id:
        row = db.get_pending_receipt_topup(m.from_user.id)
        topup_id = row["id"] if row else None

    if not topup_id:
        await state.finish()
        return await content.send(m, "wallet_topup_missing", reply_markup=menus.main_reply_kb(m.from_user.id))

    topup = db.get_topup(topup_id)

    if not topup or topup["status"] != "awaiting_receipt":
        await state.finish()
        return await content.send(m, "wallet_topup_duplicate", reply_markup=menus.main_reply_kb(m.from_user.id))

    if not m.photo:
        return await content.send(m, "wallet_receipt_required", reply_markup=cancel_kb())

    photo = m.photo[-1]

    ok, reason, submitted_topup, previous_uses = db.submit_topup_receipt_atomic(
        topup_id,
        m.from_user.id,
        photo.file_unique_id,
    )
    if not ok:
        await state.finish()
        return await content.send(m, "wallet_topup_duplicate", reply_markup=menus.main_reply_kb(m.from_user.id))

    topup = submitted_topup
    is_duplicate = previous_uses > 0
    await state.finish()

    test_marker = "\n🧪 این درخواست متعلق به کاربر تست است." if int(topup["is_test"] or 0) else ""
    caption = (
        f"💳 درخواست شارژ جدید #{topup_id}\n"
        f"👤 کاربر: {m.from_user.full_name} (@{m.from_user.username or '---'}) | ID: {m.from_user.id}\n"
        f"💰 مبلغ: {topup['amount']:,} تومان"
        f"{test_marker}"
    )

    if is_duplicate:
        caption = (
            f"⚠️ هشدار: این عکس قبلاً {previous_uses} بار به‌عنوان رسید فرستاده شده!\n"
            "احتمال تقلب - قبل از تایید حتماً دستی بررسی کنید.\n\n"
            + caption
        )

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تایید شارژ", callback_data=f"topup_confirm_{topup_id}"),
        types.InlineKeyboardButton("❌ رد", callback_data=f"topup_reject_{topup_id}"),
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(admin_id, photo.file_id, caption=caption, reply_markup=kb)
        except Exception:
            logger.exception("Could not send topup %s receipt to admin %s", topup_id, admin_id)

    await content.send(
        m, "wallet_receipt_sent", reply_markup=menus.main_reply_kb(m.from_user.id),
    )



async def cb_confirm(c: types.CallbackQuery):
    bot = Bot.get_current()

    if int(c.from_user.id) not in ADMIN_IDS:
        return await c.answer("فقط ادمین می‌تواند این کار را انجام دهد.", show_alert=True)

    await c.answer()
    topup_id = int(c.data.split("_")[-1])
    ok, reason, topup, new_balance = db.approve_topup_atomic(topup_id, admin_id=c.from_user.id)

    if not ok:
        if reason == "not_found":
            return await _edit_safely(c, "این درخواست پیدا نشد.")
        return await _edit_safely(c, "این درخواست قبلاً بررسی شده.")

    db.log_admin_action(c.from_user.id, "approve_topup", topup["user_id"], f"topup_id={topup_id}; amount={topup['amount']}")
    user = db.get_user(topup["user_id"])
    purchase_result_note = ""

    target_qty = topup["target_quantity"] if "target_quantity" in topup.keys() else None
    target_plan_id = topup["target_plan_id"] if "target_plan_id" in topup.keys() else None
    target_unit_price = topup["target_unit_price"] if "target_unit_price" in topup.keys() else None
    discount_code = topup["discount_code"] if "discount_code" in topup.keys() else None

    if target_qty and target_plan_id and not topup["purchase_completed_at"]:
        was_first_purchase = int(user["purchased"] or 0) == 0 if user else False
        plan = db.get_plan(int(target_plan_id))
        category_id = plan["category_id"] if plan else None
        content.record_funnel(
            topup["user_id"], "payment_success", category_id=category_id, plan_id=target_plan_id,
            session_key=f"topup:{topup_id}", metadata={"topup_id": topup_id},
        )
        try:
            if plan and db.plan_provider_key(plan) != "pool":
                result = await subs.provision_provider_purchase(
                    topup["user_id"], int(target_qty), int(target_plan_id),
                    int(target_unit_price) if target_unit_price else None,
                    note=f"auto_after_topup_id={topup_id}", discount_code=discount_code,
                    request_key=f"topup:{topup_id}",
                )
            else:
                result = commerce.complete_pool_purchase(
                    topup["user_id"], int(target_qty), int(target_plan_id),
                    discount_code=discount_code, note=f"auto_after_topup_id={topup_id}",
                    request_key=f"topup:{topup_id}",
                )

            if result.get("queued"):
                new_balance = int(result.get("balance_after") or db.get_user(topup["user_id"])["balance"] or 0)
                db.mark_topup_purchase_completed(topup_id)
                purchase_result_note = f"🟠 سفارش #{result['purchase_id']} ثبت شد و در صف ساخت قرار گرفت."
                await _bot_send_content(
                    bot, int(topup["user_id"]), "order_queued",
                    {"order_id": result["purchase_id"], "total": content.money(result.get("amount") or topup["target_total"])},
                    category_id=category_id, plan_id=target_plan_id,
                    reply_markup=menus.main_reply_kb(topup["user_id"]),
                )
                content.record_funnel(topup["user_id"], "purchase_queued", category_id=category_id, plan_id=target_plan_id, purchase_id=result["purchase_id"], session_key=f"topup:{topup_id}")
                result = None

            elif result.get("refunded"):
                new_balance = int(db.get_user(topup["user_id"])["balance"] or 0)
                db.mark_topup_purchase_completed(topup_id)
                purchase_result_note = f"⚫ سفارش #{result['purchase_id']} ساخته نشد و مبلغ خرید به کیف پول برگشت."
                if commerce.claim_purchase_notification(result["purchase_id"], "refund"):
                    await _bot_send_content(
                        bot, int(topup["user_id"]), "order_refunded",
                        {"order_id": result["purchase_id"], "refund_amount": content.money(result.get("amount") or topup["target_total"])},
                        category_id=category_id, plan_id=target_plan_id,
                        reply_markup=menus.main_reply_kb(topup["user_id"]),
                    )
                content.record_funnel(topup["user_id"], "purchase_refunded", category_id=category_id, plan_id=target_plan_id, purchase_id=result["purchase_id"], session_key=f"topup:{topup_id}")
                result = None

            if result is not None:
                db.mark_topup_purchase_completed(topup_id)
                new_balance = result["balance_after"]
                purchase_result_note = f"🛒 خرید #{result['purchase_id']} خودکار تکمیل و اطلاعات سرویس ارسال شد."

                if was_first_purchase:
                    referral_status, referral_detail = reward_ref(topup["user_id"])
                    if referral_status == "rewarded":
                        try:
                            await bot.send_message(int(referral_detail), "💰 پاداش اولین خرید زیرمجموعه به کیف پول شما اضافه شد.")
                        except Exception:
                            logger.warning("Could not notify referrer %s after auto purchase", referral_detail, exc_info=True)
                    elif referral_status == "blocked":
                        for admin_id in ADMIN_IDS:
                            try:
                                await bot.send_message(admin_id, referral_detail)
                            except Exception:
                                logger.warning("Could not send referral fraud warning to admin %s", admin_id, exc_info=True)

                if commerce.claim_purchase_notification(result["purchase_id"], "delivery"):
                    await _bot_send_content(
                        bot, int(topup["user_id"]), "purchase_success",
                        {
                            "order_id": result["purchase_id"],
                            "plan_title": plan["title"] if plan else "سرویس",
                            "quantity": len(result["items"]),
                            "subtotal": content.money(result.get("subtotal") or result["amount"]),
                            "discount": content.money(result.get("discount_amount") or 0),
                            "total": content.money(result["amount"]),
                            "balance_after": content.money(result["balance_after"]),
                            "test_notice": "🧪 خرید آزمایشی" if result.get("is_test") else "",
                            "post_purchase_text": plan["post_purchase_text"] if plan and "post_purchase_text" in plan.keys() else "",
                        },
                        category_id=category_id, plan_id=target_plan_id,
                        reply_markup=menus.main_reply_kb(topup["user_id"]),
                    )
                    for index, item in enumerate(result["items"], start=1):
                        await _bot_send_content(
                            bot, int(topup["user_id"]), "service_delivery",
                            {
                                "order_id": result["purchase_id"],
                                "plan_title": plan["title"] if plan else "سرویس",
                                "username": item.get("account_name") or item.get("panel_username") or f"سرویس {index}",
                                "volume": plan["volume_label"] if plan else "-",
                                "duration": plan["duration_label"] if plan else "-",
                                "devices": "نامحدود" if not plan or plan["panel_max_devices"] in (None, "", 0) else f"{plan['panel_max_devices']} دستگاه",
                                "expire_date": item.get("panel_expires_at") or "-",
                                "subscription_url": item["link"],
                                "provider_public_name": "تحویل آماده" if plan and db.plan_provider_key(plan) == "pool" else "ساخت خودکار",
                                "created_at": item.get("assigned_at") or "-",
                            },
                            category_id=category_id, plan_id=target_plan_id,
                        )
                        qr_path = make_qr(item["link"], topup["user_id"])
                        try:
                            qr = content.render("service_qr", {"username": item.get("account_name") or f"سرویس {index}"}, category_id=category_id, plan_id=target_plan_id)
                            with open(qr_path, "rb") as f:
                                sent_service = await bot.send_photo(int(topup["user_id"]), f, caption=qr["text"], parse_mode=qr["parse_mode_api"])
                                db.track_bot_message(sent_service.chat.id, topup["user_id"], sent_service.message_id, "auto_purchase_delivery", kind="delivery")
                        finally:
                            cleanup_qr(qr_path)
                    content.record_funnel(topup["user_id"], "purchase_delivered", category_id=category_id, plan_id=target_plan_id, purchase_id=result["purchase_id"], session_key=f"topup:{topup_id}")

        except db.PurchaseError as exc:
            purchase_result_note = f"⚠️ پرداخت تأیید شد، اما خرید خودکار تکمیل نشد: {exc.message}"

    try:
        await _bot_send_content(
            bot, int(topup["user_id"]), "wallet_approved",
            {"amount": content.money(topup["amount"]), "balance": content.money(new_balance), "purchase_result": purchase_result_note},
            reply_markup=menus.main_reply_kb(topup["user_id"]),
        )
    except Exception:
        logger.warning("Could not notify user %s about approved topup", topup["user_id"], exc_info=True)

    await _edit_safely(c, f"✅ درخواست #{topup_id} تایید شد." + (f"\n{purchase_result_note}" if purchase_result_note else ""))

async def cb_reject(c: types.CallbackQuery):
    bot = Bot.get_current()

    if int(c.from_user.id) not in ADMIN_IDS:
        return await c.answer("فقط ادمین می‌تواند این کار را انجام دهد.", show_alert=True)

    await c.answer()
    topup_id = int(c.data.split("_")[-1])
    ok, reason, topup = db.reject_topup_atomic(topup_id, admin_id=c.from_user.id)
    if not ok:
        if reason == "not_found":
            return await _edit_safely(c, "این درخواست پیدا نشد.")
        return await _edit_safely(c, "این درخواست قبلاً بررسی شده.")

    db.log_admin_action(c.from_user.id, "reject_topup", topup["user_id"], f"topup_id={topup_id}; amount={topup['amount']}")

    try:
        await _bot_send_content(
            bot, int(topup["user_id"]), "wallet_rejected", {"topup_id": topup_id},
            reply_markup=menus.main_reply_kb(topup["user_id"]),
        )
    except Exception:
        logger.warning("Could not notify user %s about rejected topup", topup["user_id"], exc_info=True)

    await _edit_safely(c, f"❌ درخواست #{topup_id} رد شد.")


async def _edit_safely(c: types.CallbackQuery, text: str):
    try:
        if c.message.photo:
            await c.message.edit_caption(caption=text)
        else:
            await c.message.edit_text(text)
    except Exception:
        await c.message.answer(text)


def register(dp):
    dp.register_callback_query_handler(cb_topup_start, lambda c: c.data == "topup_start")

    dp.register_message_handler(
        process_amount,
        content_types=types.ContentTypes.ANY,
        state=TopupStates.waiting_amount,
    )

    dp.register_message_handler(
        process_receipt,
        content_types=types.ContentTypes.ANY,
        state=TopupStates.waiting_receipt,
    )

    dp.register_callback_query_handler(cb_confirm, lambda c: c.data.startswith("topup_confirm_"))
    dp.register_callback_query_handler(cb_reject, lambda c: c.data.startswith("topup_reject_"))
