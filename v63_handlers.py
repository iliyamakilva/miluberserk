"""Telegram UI for v6.3 features.

New actions are attached to the existing Services, Reports and Message sections;
there is no second admin dashboard or duplicate purchase path.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

import commerce
import content
import db
import menus
import subs
from config import ADMIN_IDS
from utils import cleanup_qr, format_dual_datetime, make_qr

logger = logging.getLogger(__name__)

ISSUE_LABELS = {
    "connect": "🔴 وصل نمی‌شود",
    "slow": "🐌 سرعت پایین است",
    "device": "📱 دستگاه جدید وصل نمی‌شود",
    "volume": "📉 حجم اشتباه است",
    "link": "🔗 لینک کار نمی‌کند",
    "other": "❓ مشکل دیگر",
}


class V63States(StatesGroup):
    waiting_discount_code = State()
    waiting_discount_create_code = State()
    waiting_discount_create_value = State()
    waiting_discount_create_limits = State()
    waiting_discount_create_scope = State()
    waiting_campaign_title = State()
    waiting_campaign_config = State()
    waiting_template_title = State()
    waiting_template_body = State()
    waiting_template_edit = State()
    waiting_template_assign = State()


def _admin(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False


async def _replace(c: types.CallbackQuery, text: str, reply_markup=None):
    try:
        await c.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await c.message.answer(text, reply_markup=reply_markup)


async def _replace_content(c: types.CallbackQuery, key: str, values=None, *, category_id=None, plan_id=None, reply_markup=None):
    try:
        await c.message.delete()
    except Exception:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    return await content.send(
        c.message, key, values or {}, category_id=category_id, plan_id=plan_id,
        reply_markup=reply_markup,
    )


async def _bot_send_content(bot: Bot, chat_id: int, key: str, values=None, *, category_id=None, plan_id=None, reply_markup=None):
    result = content.render(key, values or {}, category_id=category_id, plan_id=plan_id)
    if result["photo_file_id"]:
        if len(result["text"]) <= content.CAPTION_LIMIT:
            return await bot.send_photo(chat_id, result["photo_file_id"], caption=result["text"], parse_mode=result["parse_mode_api"], reply_markup=reply_markup)
        await bot.send_photo(chat_id, result["photo_file_id"])
    return await bot.send_message(chat_id, result["text"], parse_mode=result["parse_mode_api"], reply_markup=reply_markup)


def _back(callback="adm_back"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت", callback_data=callback))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


# -------------------- Discount selection in current purchase path --------------------

def discount_plan_button(plan_id: int):
    return types.InlineKeyboardButton("🎁 وارد کردن کد تخفیف", callback_data=f"discount_plan_{int(plan_id)}")


async def cb_discount_plan(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    plan_id = int(c.data.rsplit("_", 1)[1])
    plan = db.get_plan(plan_id)
    if not plan:
        return await content.send(c.message, "plan_unavailable", reply_markup=menus.back_main_inline())
    await state.update_data(active_discount_plan_id=plan_id)
    await V63States.waiting_discount_code.set()
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ حذف کد تخفیف", callback_data=f"discount_clear_{plan_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت به پلن", callback_data=f"buy_plan_{plan_id}"))
    await content.send(
        c.message, "discount_prompt", {"plan_title": plan["title"] or "سرویس"},
        category_id=plan["category_id"], plan_id=plan_id, reply_markup=kb,
    )


async def process_discount_code(m: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_id = int(data.get("active_discount_plan_id") or 0)
    plan = db.get_plan(plan_id)
    if not plan:
        await state.finish()
        return await content.send(m, "plan_unavailable", reply_markup=menus.main_reply_kb(m.from_user.id))
    code = (m.text or "").strip().upper()
    try:
        quote = commerce.quote_purchase(str(m.from_user.id), plan, 1, code)
    except db.PurchaseError as exc:
        return await content.send(m, "discount_invalid", {"reason": exc.message})
    await state.update_data(active_discount_code=code, active_discount_plan_id=plan_id)
    await state.reset_state(with_data=False)
    await content.send(
        m, "discount_valid",
        {
            "discount_code": code,
            "discount_amount": content.money(quote["amount"]),
            "final_price": content.money(quote["total"]),
        },
        category_id=plan["category_id"], plan_id=plan_id,
        reply_markup=menus.main_reply_kb(m.from_user.id),
    )
    import bot as bot_module
    await bot_module.render_buy(m, m.from_user.id, m.from_user.username or "", plan_id=plan_id)


async def cb_discount_clear(c: types.CallbackQuery, state: FSMContext):
    await c.answer("کد حذف شد")
    plan_id = int(c.data.rsplit("_", 1)[1])
    await state.update_data(active_discount_code=None, active_discount_plan_id=None)
    await state.reset_state(with_data=False)
    await content.send(c.message, "discount_cleared")
    import bot as bot_module
    await bot_module.render_buy(c.message, c.from_user.id, c.from_user.username or "", plan_id=plan_id)


# -------------------- User service details and service-linked issues --------------------

def service_detail_kb(service_id: int, source_type: str):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(content.render_button("btn_show_link"), callback_data=f"service_link_{service_id}"),
        types.InlineKeyboardButton(content.render_button("btn_show_qr"), callback_data=f"service_qr_{service_id}"),
    )
    if source_type != "pool":
        kb.add(types.InlineKeyboardButton(content.render_button("btn_usage"), callback_data=f"service_usage_{service_id}"))
    kb.add(types.InlineKeyboardButton(content.render_button("btn_issue"), callback_data=f"service_issue_{service_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ سرویس‌های من", callback_data="my_subs"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def _owned_service(service_id: int, user_id: int):
    row = subs.get_sub_detail(service_id)
    if not row or str(row["owner"] or "") != str(user_id) or not int(row["used"] or 0):
        return None
    return row


async def cb_service_detail(c: types.CallbackQuery):
    await c.answer()
    service_id = int(c.data.rsplit("_", 1)[1])
    row = _owned_service(service_id, c.from_user.id)
    if not row:
        return await content.send(c.message, "service_not_found", reply_markup=menus.back_main_inline())
    plan = db.get_plan(row["plan_id"]) if row["plan_id"] else None
    used = int(row["panel_used_traffic"] or 0)
    limit = int(row["panel_data_limit"] or 0)
    usage_text = "-"
    if limit:
        usage_text = f"{used / 1024**3:.2f} از {limit / 1024**3:.2f} گیگ"
    source = row["source_type"] or "pool"
    display = content.get_display_settings(category_id=plan["category_id"] if plan else None, plan_id=plan["id"] if plan else None)
    provider_public = ""
    if display["show_provider_public"]:
        provider_public = subs.provider_label(source) if source != "pool" else "تحویل آماده"
    await _replace_content(
        c, "service_detail",
        {
            "plan_title": plan["title"] if plan else "سرویس",
            "username": row["account_name"] or "-",
            "status": row["panel_status"] or row["status"] or "نامشخص",
            "usage": usage_text,
            "purchase_date": format_dual_datetime(row["assigned_at"]),
            "expire_date": row["panel_expires_at"] or "-",
            "provider_line": f"🚚 نوع تحویل: {provider_public}" if provider_public else "",
            "days_left": "-",
        },
        category_id=plan["category_id"] if plan else None,
        plan_id=plan["id"] if plan else None,
        reply_markup=service_detail_kb(service_id, source),
    )


async def cb_service_link(c: types.CallbackQuery):
    await c.answer()
    service_id = int(c.data.rsplit("_", 1)[1])
    row = _owned_service(service_id, c.from_user.id)
    if not row:
        return await content.send(c.message, "service_not_found", reply_markup=menus.back_main_inline())
    plan = db.get_plan(row["plan_id"]) if row["plan_id"] else None
    await content.send(
        c.message, "service_link",
        {"username": row["account_name"] or "-", "subscription_url": row["link"]},
        category_id=plan["category_id"] if plan else None,
        plan_id=plan["id"] if plan else None,
        reply_markup=service_detail_kb(service_id, row["source_type"] or "pool"),
    )


async def cb_service_qr(c: types.CallbackQuery):
    await c.answer()
    service_id = int(c.data.rsplit("_", 1)[1])
    row = _owned_service(service_id, c.from_user.id)
    if not row:
        return await content.send(c.message, "service_not_found", reply_markup=menus.back_main_inline())
    plan = db.get_plan(row["plan_id"]) if row["plan_id"] else None
    rendered = content.render(
        "service_qr", {"username": row["account_name"] or "-"},
        category_id=plan["category_id"] if plan else None,
        plan_id=plan["id"] if plan else None,
    )
    path = make_qr(row["link"], c.from_user.id)
    try:
        with open(path, "rb") as f:
            await c.message.answer_photo(f, caption=rendered["text"], parse_mode=rendered["parse_mode_api"])
    finally:
        cleanup_qr(path)


async def _refresh_service(service_id: int, user_id: int | None = None):
    row = subs.get_sub_detail(service_id)
    if not row or (user_id is not None and str(row["owner"] or "") != str(user_id)):
        raise ValueError("service_not_found")
    provider_key = row["panel_provider"] or row["source_type"]
    if not provider_key or provider_key == "pool" or not row["panel_username"]:
        raise subs.ProviderError("این سرویس گزارش مصرف آنلاین ندارد.")
    provider = subs.get_provider_adapter(provider_key)
    result = await provider.usage(row["panel_username"])
    used = result.get("used_traffic") or result.get("used") or result.get("total") or 0
    db.update_panel_sub_usage(service_id, int(used or 0))
    commerce.record_provider_log(provider_key, "usage", "success", user_id=row["owner"], plan_id=row["plan_id"], purchase_id=row["purchase_id"])
    return subs.get_sub_detail(service_id), result


async def cb_service_usage(c: types.CallbackQuery):
    await c.answer("در حال بررسی...")
    service_id = int(c.data.rsplit("_", 1)[1])
    try:
        row, result = await _refresh_service(service_id, c.from_user.id)
    except Exception:
        return await content.send(c.message, "usage_error")
    plan = db.get_plan(row["plan_id"]) if row["plan_id"] else None
    used = int(row["panel_used_traffic"] or 0)
    limit = int(row["panel_data_limit"] or 0)
    remaining = max(0, limit - used) if limit else 0
    await content.send(
        c.message, "usage_success",
        {
            "used_volume": content.bytes_gb(used),
            "total_volume": content.bytes_gb(limit, dash_if_zero=True),
            "remaining_volume": content.bytes_gb(remaining, dash_if_zero=True),
        },
        category_id=plan["category_id"] if plan else None,
        plan_id=plan["id"] if plan else None,
        reply_markup=service_detail_kb(service_id, row["source_type"] or "pool"),
    )


async def cb_service_issue(c: types.CallbackQuery):
    await c.answer()
    service_id = int(c.data.rsplit("_", 1)[1])
    row = _owned_service(service_id, c.from_user.id)
    if not row:
        return await content.send(c.message, "service_not_found", reply_markup=menus.back_main_inline())
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, label in ISSUE_LABELS.items():
        kb.add(types.InlineKeyboardButton(label, callback_data=f"issue_create_{service_id}_{key}"))
    kb.add(types.InlineKeyboardButton("⬅️ جزئیات سرویس", callback_data=f"service_detail_{service_id}"))
    plan = db.get_plan(row["plan_id"]) if row["plan_id"] else None
    await _replace_content(
        c, "issue_select",
        category_id=plan["category_id"] if plan else None,
        plan_id=plan["id"] if plan else None,
        reply_markup=kb,
    )


async def cb_issue_create(c: types.CallbackQuery):
    await c.answer()
    raw = c.data.split("issue_create_", 1)[1]
    service_text, issue_type = raw.rsplit("_", 1)
    service_id = int(service_text)
    try:
        ticket_id, snapshot = commerce.create_service_issue(str(c.from_user.id), service_id, issue_type)
    except ValueError:
        return await content.send(c.message, "service_not_found", reply_markup=menus.back_main_inline())
    user = db.get_user(str(c.from_user.id))
    header = (
        f"🎫 گزارش خرابی سرویس — تیکت #{ticket_id}\n\n"
        f"کاربر: @{(user['username'] if user else '') or '-'} | ID: {c.from_user.id}\n"
        f"نوع مشکل: {ISSUE_LABELS.get(issue_type, issue_type)}\n"
        f"سرویس: {snapshot['plan']} | ID: {service_id}\n"
        f"Order: #{snapshot.get('order_id') or '-'}\n"
        f"Provider: {subs.provider_label(snapshot.get('provider'))}\n"
        f"مصرف: {snapshot.get('used_traffic', 0) / 1024**3:.2f} GB\n"
        f"انقضا: {snapshot.get('expires_at') or '-'}\n"
        f"وضعیت: {snapshot.get('status') or '-'}\n"
        f"آخرین خطای API: {snapshot.get('last_provider_error') or '-'}\n\n"
        "🔐 لینک اشتراک و اطلاعات ورود عمداً در این پیام ثبت نشده‌اند."
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔄 بررسی سرویس", callback_data=f"issue_admin_check_{ticket_id}"),
        types.InlineKeyboardButton("📊 بررسی مصرف", callback_data=f"issue_admin_usage_{ticket_id}"),
        types.InlineKeyboardButton("♻️ Revoke Link", callback_data=f"issue_admin_revoke_{ticket_id}"),
        types.InlineKeyboardButton("📩 نحوه پاسخ", callback_data=f"issue_admin_reply_{ticket_id}"),
    )
    kb.add(types.InlineKeyboardButton("🗑 حذف سرویس", callback_data=f"issue_admin_delete_confirm_{ticket_id}"))
    bot = Bot.get_current()
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            sent = await bot.send_message(admin_id, header, reply_markup=kb)
            db.record_ticket_message(admin_id, sent.message_id, ticket_id, str(c.from_user.id))
            delivered += 1
        except Exception:
            logger.exception("could not deliver service issue %s to admin %s", ticket_id, admin_id)
    service_row = subs.get_sub_detail(service_id)
    plan = db.get_plan(service_row["plan_id"]) if service_row and service_row["plan_id"] else None
    issue_label = ISSUE_LABELS.get(issue_type, issue_type)
    if not delivered:
        issue_label += " — تیکت ذخیره شد و ارسال فوری دوباره بررسی می‌شود"
    await content.send(
        c.message, "issue_created",
        {"ticket_id": ticket_id, "plan_title": snapshot.get("plan") or "سرویس", "issue_label": issue_label},
        category_id=plan["category_id"] if plan else None,
        plan_id=plan["id"] if plan else None,
        reply_markup=service_detail_kb(service_id, snapshot.get("provider") or "pool"),
    )


def _issue_service(ticket_id: int):
    ticket = commerce.get_service_issue(ticket_id)
    if not ticket or not ticket["service_id"]:
        return ticket, None
    return ticket, subs.get_sub_detail(ticket["service_id"])


async def cb_issue_admin_check(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer("فقط ادمین", show_alert=True)
    await c.answer()
    ticket_id = int(c.data.rsplit("_", 1)[1])
    ticket, row = _issue_service(ticket_id)
    if not row:
        return await c.message.answer("سرویس پیدا نشد.")
    await c.message.answer(
        f"🔄 وضعیت سرویس تیکت #{ticket_id}\n"
        f"DB status: {row['status'] or '-'}\nPanel status: {row['panel_status'] or '-'}\n"
        f"آخرین Sync: {row['last_synced_at'] or '-'}\nProvider: {row['panel_provider'] or row['source_type'] or '-'}"
    )


async def cb_issue_admin_usage(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer("فقط ادمین", show_alert=True)
    await c.answer("در حال بررسی...")
    ticket_id = int(c.data.rsplit("_", 1)[1])
    ticket, row = _issue_service(ticket_id)
    if not row:
        return await c.message.answer("سرویس پیدا نشد.")
    try:
        row, _ = await _refresh_service(row["id"])
        await c.message.answer(f"📊 مصرف به‌روز شد: {int(row['panel_used_traffic'] or 0)/1024**3:.2f} GB")
    except Exception as exc:
        await c.message.answer(f"❌ بررسی مصرف ناموفق بود: {exc}")


async def cb_issue_admin_revoke(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer("فقط ادمین", show_alert=True)
    await c.answer("در حال تعویض لینک...")
    ticket_id = int(c.data.rsplit("_", 1)[1])
    ticket, row = _issue_service(ticket_id)
    if not row or not row["panel_username"]:
        return await c.message.answer("این سرویس قابلیت Revoke ندارد.")
    provider_key = row["panel_provider"] or row["source_type"]
    try:
        provider = subs.get_provider_adapter(provider_key)
        result = await provider.revoke_subscription(row["panel_username"])
        db.update_panel_sub(row["id"], result)
        db.log_admin_action(c.from_user.id, "revoke_service_link", row["owner"], f"ticket_id={ticket_id};service_id={row['id']}")
        await c.message.answer("✅ لینک سرویس تعویض شد. لینک جدید فقط برای مالک سرویس ارسال می‌شود.")
        await Bot.get_current().send_message(int(row["owner"]), f"♻️ لینک سرویس شما به‌روزرسانی شد:\n\n{result.get('subscription_url')}")
    except Exception as exc:
        await c.message.answer(f"❌ تعویض لینک ناموفق بود: {exc}")


async def cb_issue_admin_reply(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer("فقط ادمین", show_alert=True)
    await c.answer(show_alert=True, text="روی پیام تیکت Reply بزنید؛ پاسخ مستقیم برای کاربر ارسال می‌شود.")


async def cb_issue_admin_delete_confirm(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer("فقط ادمین", show_alert=True)
    await c.answer()
    ticket_id = int(c.data.rsplit("_", 1)[1])
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⚠️ تأیید حذف از Provider", callback_data=f"issue_admin_delete_{ticket_id}"))
    kb.add(types.InlineKeyboardButton("❌ لغو", callback_data=f"issue_admin_check_{ticket_id}"))
    await c.message.answer("این عملیات سرویس را از Provider حذف می‌کند و قابل بازگشت نیست.", reply_markup=kb)


async def cb_issue_admin_delete(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer("فقط ادمین", show_alert=True)
    await c.answer()
    ticket_id = int(c.data.rsplit("_", 1)[1])
    ticket, row = _issue_service(ticket_id)
    if not row:
        return await c.message.answer("سرویس پیدا نشد.")
    if (row["source_type"] or "pool") == "pool":
        return await c.message.answer("حذف سرویس استخری از این اکشن مجاز نیست.")
    try:
        provider = subs.get_provider_adapter(row["panel_provider"] or row["source_type"])
        await provider.delete_user(row["panel_username"])
        db.mark_panel_sub_deleted(row["id"])
        db.log_admin_action(c.from_user.id, "delete_provider_service", row["owner"], f"ticket_id={ticket_id};service_id={row['id']}")
        await c.message.answer("✅ سرویس از Provider حذف و در دیتابیس علامت‌گذاری شد.")
    except Exception as exc:
        await c.message.answer(f"❌ حذف سرویس ناموفق بود: {exc}")


# -------------------- Provider management --------------------

def providers_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for provider in subs.list_provider_adapters(configured_only=False):
        commerce.ensure_provider(provider.key, provider.capabilities)
        state = commerce.get_provider_state(provider.key)
        online = state and state["last_status"] == "online"
        sales = not state or int(state["is_sales_enabled"] or 0)
        icon = "🟢" if online and sales else ("⛔" if not sales else "🟠")
        kb.add(types.InlineKeyboardButton(f"{icon} {provider.label}", callback_data=f"v63_provider_{provider.key}"))
    kb.add(types.InlineKeyboardButton("⬅️ خدمات و کاتالوگ", callback_data="adm_section_services"))
    return kb


async def cb_providers(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer()
    await _replace(c, "🔌 مدیریت تأمین‌کننده‌ها\n\nوضعیت، توقف فروش، قابلیت‌ها و لاگ هر Provider در یک صفحه مدیریت می‌شود.", providers_menu_kb())


def provider_detail_kb(key: str):
    state = commerce.get_provider_state(key)
    sales = True if not state else bool(int(state["is_sales_enabled"] or 0))
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔍 تست اتصال", callback_data=f"v63_provider_health_{key}"))
    kb.add(types.InlineKeyboardButton("⛔ توقف فروش" if sales else "✅ فعال‌کردن فروش", callback_data=f"v63_provider_toggle_{key}"))
    kb.add(types.InlineKeyboardButton("📜 لاگ Provider", callback_data=f"v63_provider_logs_{key}"))
    kb.add(types.InlineKeyboardButton("⬅️ تأمین‌کننده‌ها", callback_data="adm_providers"))
    return kb


def _provider_text(key: str) -> str:
    provider = subs.get_provider_adapter(key)
    commerce.ensure_provider(provider.key, provider.capabilities)
    state = commerce.get_provider_state(key)
    caps = provider.capabilities
    cap_labels = {
        "create": "ساخت سرویس", "renew": "تمدید", "add_volume": "افزایش حجم", "reset_usage": "ریست مصرف",
        "revoke": "تعویض لینک", "device_limit": "محدودیت دستگاه", "usage": "گزارش مصرف", "delete": "حذف",
    }
    lines = [f"🔌 {provider.label}", ""]
    lines += [
        f"وضعیت اتصال: {(state['last_status'] if state else 'unknown')}",
        f"فروش جدید: {'فعال' if not state or int(state['is_sales_enabled'] or 0) else 'متوقف'}",
        f"آخرین بررسی: {(state['last_checked_at'] if state else None) or '-'}",
        f"زمان پاسخ: {str(state['response_ms'])+'ms' if state and state['response_ms'] is not None else '-'}",
        f"سرویس ساخته‌شده: {int(state['services_created'] or 0) if state else 0}",
        f"خطاها: {int(state['failure_count'] or 0) if state else 0}",
        f"آخرین خطا: {(state['last_error'] if state else None) or '-'}",
        "",
        "قابلیت‌ها:",
    ]
    for cap, label in cap_labels.items():
        lines.append(f"{'✅' if caps.get(cap) else '❌'} {label}")
    return "\n".join(lines)


async def cb_provider_detail(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer()
    key = c.data.split("v63_provider_", 1)[1]
    await _replace(c, _provider_text(key), provider_detail_kb(key))


async def cb_provider_health(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    key = c.data.split("v63_provider_health_", 1)[1]
    await c.answer("در حال تست...")
    try:
        await subs.provider_health_check(key)
        text = "✅ اتصال برقرار است.\n\n" + _provider_text(key)
    except Exception as exc:
        text = f"❌ تست اتصال ناموفق بود: {exc}\n\n" + _provider_text(key)
    await _replace(c, text, provider_detail_kb(key))


async def cb_provider_toggle(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    key = c.data.split("v63_provider_toggle_", 1)[1]
    state = commerce.get_provider_state(key)
    enabled = True if not state else bool(int(state["is_sales_enabled"] or 0))
    commerce.set_provider_sales(key, not enabled)
    db.log_admin_action(c.from_user.id, "toggle_provider_sales", details=f"provider={key};enabled={not enabled}")
    await c.answer("ذخیره شد")
    await _replace(c, _provider_text(key), provider_detail_kb(key))


async def cb_provider_logs(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    key = c.data.split("v63_provider_logs_", 1)[1]
    await c.answer()
    rows = commerce.list_provider_logs(key, 20)
    lines = [f"📜 آخرین لاگ‌های {subs.provider_label(key)}", ""]
    for row in rows:
        lines.append(
            f"#{row['id']} | {row['created_at']}\n{row['operation']} | {row['result']} | {row['response_ms'] or '-'}ms\n"
            f"Order: {row['purchase_id'] or '-'} | Error: {row['error_code'] or '-'} / {row['error_message'] or '-'}\n"
        )
    if not rows:
        lines.append("هنوز لاگی ثبت نشده است.")
    await _replace(c, "\n".join(lines)[:3900], provider_detail_kb(key))


async def cb_plan_fallback(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    plan_id = int(c.data.rsplit("_", 1)[1])
    plan = db.get_plan(plan_id)
    if not plan:
        return await c.answer("پلن پیدا نشد", show_alert=True)
    await c.answer()
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("بدون جایگزین", callback_data=f"v63_plan_fallback_set_{plan_id}_none"))
    primary = db.plan_provider_key(plan)
    for provider in subs.list_provider_adapters(configured_only=False):
        if provider.key != primary:
            kb.add(types.InlineKeyboardButton(provider.label, callback_data=f"v63_plan_fallback_set_{plan_id}_{provider.key}"))
    kb.add(types.InlineKeyboardButton("⬅️ تنظیمات پلن", callback_data=f"plan_settings_{plan_id}"))
    await _replace(
        c,
        f"🔀 Provider جایگزین برای «{plan['title']}»\n\n"
        f"اصلی: {subs.provider_label(primary)}\n"
        f"فعلی: {subs.provider_label(plan['fallback_provider_key']) if plan['fallback_provider_key'] else '-'}\n\n"
        "جایگزین فقط وقتی استفاده می‌شود که فروش Provider اصلی متوقف باشد یا قبل از ساخت در دسترس نباشد.",
        kb,
    )


async def cb_plan_fallback_set(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    raw = c.data.split("v63_plan_fallback_set_", 1)[1]
    plan_text, provider_key = raw.split("_", 1)
    plan_id = int(plan_text)
    commerce.set_plan_fallback(plan_id, None if provider_key == "none" else provider_key)
    db.log_admin_action(c.from_user.id, "set_plan_fallback", details=f"plan_id={plan_id};provider={provider_key}")
    await c.answer("ذخیره شد")
    c.data = f"v63_plan_fallback_{plan_id}"
    await cb_plan_fallback(c)


# -------------------- Order queue --------------------

def queue_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    rows = commerce.list_order_queue(30)
    for row in rows:
        icon = {"paid": "🔵", "provisioning": "🟣", "retry": "🟠", "admin_review": "🔴", "refunded": "⚫"}.get(row["status"], "🟡")
        kb.add(types.InlineKeyboardButton(f"{icon} #{row['id']} | {row['plan_title'] or '-'} | {row['status']}", callback_data=f"v63_order_{row['id']}"))
    kb.add(types.InlineKeyboardButton("🔄 پردازش صف آماده", callback_data="v63_order_run_due"))
    kb.add(types.InlineKeyboardButton("⬅️ خدمات و کاتالوگ", callback_data="adm_section_services"))
    return kb


async def cb_order_queue(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer()
    rows = commerce.list_order_queue(30)
    text = f"📋 صف سفارش و بازیابی\n\nتعداد موارد قابل نمایش: {len(rows)}\nسفارش‌های completed در این فهرست تکرار نمی‌شوند."
    await _replace(c, text, queue_menu_kb())


def order_detail_kb(purchase_id: int, status: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if status not in {"completed", "refunded"}:
        kb.add(types.InlineKeyboardButton("🔄 تلاش مجدد اکنون", callback_data=f"v63_order_retry_{purchase_id}"))
        kb.add(types.InlineKeyboardButton("💳 بازپرداخت امن", callback_data=f"v63_order_refund_confirm_{purchase_id}"))
    if status in {"admin_review", "refunded"}:
        kb.add(types.InlineKeyboardButton("✅ علامت بررسی شد", callback_data=f"v63_order_reviewed_{purchase_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ صف سفارش‌ها", callback_data="adm_order_queue"))
    return kb


async def cb_order_detail(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer()
    purchase_id = int(c.data.rsplit("_", 1)[1])
    row = commerce.get_order_detail(purchase_id)
    if not row:
        return await c.message.answer("سفارش پیدا نشد.")
    text = (
        f"📋 سفارش #{row['id']}\n\n"
        f"کاربر: @{row['username'] or '-'} | {row['user_id']}\n"
        f"پلن: {row['plan_title'] or '-'}\n"
        f"وضعیت: {row['status']}\n"
        f"Provider اصلی: {row['primary_provider'] or row['original_provider'] or '-'}\n"
        f"Provider فعال: {row['active_provider'] or row['provider'] or '-'}\n"
        f"Fallback: {row['fallback_provider'] or '-'}\n"
        f"تلاش: {int(row['retry_count'] or 0)}/{int(row['max_retries'] or 3)}\n"
        f"تلاش بعدی: {row['next_retry_at'] or '-'}\n"
        f"مبلغ: {int(row['amount'] or 0):,} تومان\n"
        f"تخفیف: {int(row['discount_amount'] or 0):,} تومان\n"
        f"خطا: {row['job_error'] or row['provision_error'] or '-'}"
    )
    await _replace(c, text, order_detail_kb(purchase_id, row["status"]))


async def cb_order_retry(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    purchase_id = int(c.data.rsplit("_", 1)[1])
    await c.answer("در حال پردازش...")
    # Make the job immediately claimable without creating a second order.
    db.cur.execute("UPDATE provider_jobs SET status='pending',next_retry_at=NULL,locked_at=NULL WHERE purchase_id=?", (purchase_id,))
    db.cur.execute("UPDATE purchases SET status='paid',next_retry_at=NULL WHERE id=? AND status!='completed'", (purchase_id,))
    db.conn.commit()
    result = await subs.process_provider_job(purchase_id)
    db.log_admin_action(c.from_user.id, "retry_provider_order", details=f"purchase_id={purchase_id};result={result.get('completed')}")
    await c.message.answer("✅ پردازش انجام شد." if result.get("completed") else ("🟠 سفارش دوباره در صف قرار گرفت." if result.get("queued") else "⚫ سفارش بازپرداخت شد."))
    row = commerce.get_order_detail(purchase_id)
    await _replace(c, f"وضعیت جدید سفارش #{purchase_id}: {row['status'] if row else '-'}", order_detail_kb(purchase_id, row["status"] if row else ""))


async def cb_order_run_due(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer("در حال پردازش صف...")
    results = await subs.recover_due_provider_jobs(30)
    await _replace(c, f"✅ پردازش صف تمام شد.\nتعداد بررسی‌شده: {len(results)}", queue_menu_kb())


async def cb_order_refund_confirm(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    purchase_id = int(c.data.rsplit("_", 1)[1])
    await c.answer()
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⚠️ تأیید بازپرداخت", callback_data=f"v63_order_refund_{purchase_id}"))
    kb.add(types.InlineKeyboardButton("❌ لغو", callback_data=f"v63_order_{purchase_id}"))
    await c.message.answer("بازپرداخت فقط یک بار ثبت می‌شود؛ تکرار این اکشن دوباره پول اضافه نمی‌کند.", reply_markup=kb)


async def cb_order_refund(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    purchase_id = int(c.data.rsplit("_", 1)[1])
    await c.answer()
    ok, reason, purchase = commerce.refund_purchase_once(purchase_id, "manual_admin_refund", review_required=True)
    db.log_admin_action(c.from_user.id, "refund_provider_order", purchase["user_id"] if purchase else None, f"purchase_id={purchase_id};reason={reason}")
    await c.message.answer("✅ بازپرداخت ثبت شد." if ok else f"❌ بازپرداخت انجام نشد: {reason}")


async def cb_order_reviewed(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    purchase_id = int(c.data.rsplit("_", 1)[1])
    db.cur.execute("UPDATE purchases SET review_required=0 WHERE id=?", (purchase_id,))
    db.conn.commit()
    await c.answer("بررسی شد")
    await _replace(c, f"✅ سفارش #{purchase_id} از فهرست نیازمند بررسی خارج شد.", queue_menu_kb())


# -------------------- Business reports --------------------

def reports_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💰 خلاصه فروش", callback_data="v63_reports_overview"))
    kb.add(types.InlineKeyboardButton("🏷 عملکرد پلن‌ها", callback_data="v63_reports_plans"))
    kb.add(types.InlineKeyboardButton("🗂 عملکرد دسته‌ها", callback_data="v63_reports_categories"))
    kb.add(types.InlineKeyboardButton("🧪 تبدیل تست به خرید", callback_data="v63_reports_trials"))
    kb.add(types.InlineKeyboardButton("🏆 مشتریان ارزشمند", callback_data="v63_reports_customers"))
    kb.add(types.InlineKeyboardButton("📦 موجودی و سرویس‌ها", callback_data="v63_reports_inventory"))
    kb.add(types.InlineKeyboardButton("⬅️ گزارش‌ها", callback_data="adm_section_reports"))
    return kb


async def cb_reports(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer()
    await _replace(c, "📊 گزارش‌های تجاری واقعی\n\nفروش آزمایشی از درآمد واقعی حذف شده است.", reports_kb())


async def cb_reports_overview(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    r = commerce.sales_overview()
    text = (
        "💰 خلاصه فروش\n\n"
        f"امروز: {r['today_revenue']:,} تومان | {r['today_orders']} سفارش\n"
        f"۷ روز: {r['week_revenue']:,} تومان | {r['week_orders']} سفارش\n"
        f"۳۰ روز: {r['month_revenue']:,} تومان | {r['month_orders']} سفارش\n"
        f"کل: {r['all_revenue']:,} تومان | {r['all_orders']} سفارش\n\n"
        f"سفارش مشکل‌دار: {r['problem_orders']} | {r['problem_amount']:,} تومان\n"
        f"بازپرداخت: {r['refund_orders']} | {r['refund_amount']:,} تومان"
    )
    await _replace(c, text, reports_kb())


async def cb_reports_plans(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    lines = ["🏷 عملکرد پلن‌ها", ""]
    for row in commerce.plan_performance(20):
        lines.append(
            f"{row['title']}\nفروش: {int(row['units'] or 0)} | درآمد: {int(row['revenue'] or 0):,}\n"
            f"سود تقریبی: {int(row['estimated_profit'] or 0):,} | مشتری فعال: {int(row['active_customers'] or 0)}\n"
        )
    await _replace(c, "\n".join(lines)[:3900], reports_kb())


async def cb_reports_categories(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    lines = ["🗂 عملکرد دسته‌ها", ""]
    for row in commerce.category_performance(20):
        lines.append(f"{row['emoji'] or '📦'} {row['title']}\nفروش: {int(row['units'] or 0)} | درآمد: {int(row['revenue'] or 0):,} تومان\n")
    await _replace(c, "\n".join(lines)[:3900], reports_kb())


async def cb_reports_trials(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    stats = [commerce.trial_conversion(d) for d in (1, 3, 7)]
    lines = ["🧪 تبدیل تست به خرید", ""]
    for row in stats:
        lines.append(f"تا {row['days']} روز: {row['converted']} از {row['trials']} نفر — {row['rate']}٪")
    await _replace(c, "\n".join(lines), reports_kb())


async def cb_reports_customers(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    lines = ["🏆 مشتریان ارزشمند", ""]
    for i, row in enumerate(commerce.top_customers(10), 1):
        lines.append(f"{i}. @{row['username'] or '-'} | {row['id']}\n{int(row['orders'] or 0)} سفارش | {int(row['spent'] or 0):,} تومان")
    await _replace(c, "\n\n".join(lines)[:3900], reports_kb())


async def cb_reports_inventory(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    lines = ["📦 موجودی و سرویس‌ها", ""]
    for row in commerce.inventory_report():
        lines.append(f"{row['title']}\nآماده: {int(row['pool_stock'] or 0)} | فروخته‌شده استخری: {int(row['pool_sold'] or 0)} | Provider: {int(row['provider_services'] or 0)}")
    await _replace(c, "\n\n".join(lines)[:3900], reports_kb())


# -------------------- Discounts and campaigns admin --------------------

def discounts_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ ساخت کد تخفیف", callback_data="v63_discount_create"))
    for row in commerce.list_discounts(30):
        icon = "🟢" if int(row["is_active"] or 0) else "⚫"
        kb.add(types.InlineKeyboardButton(f"{icon} {row['code']} | {row['discount_type']} {row['value']}", callback_data=f"v63_discount_{row['id']}"))
    kb.add(types.InlineKeyboardButton("📣 کمپین‌ها", callback_data="v63_campaigns"))
    kb.add(types.InlineKeyboardButton("⬅️ خدمات و کاتالوگ", callback_data="adm_section_services"))
    return kb


async def cb_discounts(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    await _replace(c, "🎁 تخفیف‌ها و کمپین‌ها\n\nکدها در همان اکشن خرید فعلی اعمال می‌شوند و مسیر خرید جدا نمی‌سازند.", discounts_menu_kb())


async def cb_discount_detail(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    discount_id = int(c.data.rsplit("_", 1)[1])
    db.cur.execute("SELECT * FROM discounts WHERE id=?", (discount_id,))
    row = db.cur.fetchone()
    if not row: return await c.message.answer("کد پیدا نشد.")
    text = (
        f"🎁 {row['code']}\n\nعنوان: {row['title'] or '-'}\nنوع: {row['discount_type']}\nمقدار: {row['value']}\n"
        f"استفاده: {row['used_count']}/{row['max_uses'] or '∞'}\nهر کاربر: {row['per_user_limit']}\n"
        f"حداقل سفارش: {int(row['min_amount'] or 0):,}\nپلن: {row['plan_id'] or 'همه'} | دسته: {row['category_id'] or 'همه'}\n"
        f"اولین خرید: {'بله' if row['first_purchase_only'] else 'خیر'} | کاربر جدید: {'بله' if row['new_users_only'] else 'خیر'} | تمدید: {'بله' if row['renewals_only'] else 'خیر'}\n"
        f"شروع: {row['starts_at'] or '-'} | پایان: {row['ends_at'] or '-'}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("فعال/غیرفعال", callback_data=f"v63_discount_toggle_{discount_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ تخفیف‌ها", callback_data="adm_discounts"))
    await _replace(c, text, kb)


async def cb_discount_toggle(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    discount_id = int(c.data.rsplit("_", 1)[1])
    commerce.toggle_discount(discount_id)
    db.log_admin_action(c.from_user.id, "toggle_discount", details=f"discount_id={discount_id}")
    await c.answer("ذخیره شد")
    c.data = f"v63_discount_{discount_id}"
    await cb_discount_detail(c)


async def cb_discount_create(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    await state.finish()
    await V63States.waiting_discount_create_code.set()
    await c.message.answer("کد تخفیف را بفرستید؛ مثال: VIP20", reply_markup=_back("adm_discounts"))


async def process_discount_create_code(m: types.Message, state: FSMContext):
    code = (m.text or "").strip().upper().replace(" ", "")
    if not code:
        return await m.answer("کد معتبر بفرستید.")
    await state.update_data(discount_code=code)
    kb = types.InlineKeyboardMarkup(row_width=2)
    for key, label in (("percent", "درصدی"), ("fixed", "مبلغ ثابت"), ("free", "رایگان"), ("bonus_volume", "هدیه حجم MB")):
        kb.insert(types.InlineKeyboardButton(label, callback_data=f"v63_discount_type_{key}"))
    await m.answer("نوع کد را انتخاب کنید:", reply_markup=kb)


async def cb_discount_type(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id): return await c.answer()
    kind = c.data.split("v63_discount_type_", 1)[1]
    await c.answer()
    await state.update_data(discount_type=kind)
    if kind == "free":
        await state.update_data(discount_value=100)
        await V63States.waiting_discount_create_limits.set()
        return await c.message.answer("محدودیت‌ها را به‌شکل max_uses,per_user,min_amount بفرستید. مثال: 100,1,0")
    await V63States.waiting_discount_create_value.set()
    await c.message.answer("مقدار را عددی بفرستید؛ برای درصد مثلاً 20، برای مبلغ تومان و برای هدیه حجم MB.")


async def process_discount_create_value(m: types.Message, state: FSMContext):
    try:
        value = max(0, int((m.text or "").replace(",", "").strip()))
    except ValueError:
        return await m.answer("فقط عدد بفرستید.")
    await state.update_data(discount_value=value)
    await V63States.waiting_discount_create_limits.set()
    await m.answer("محدودیت‌ها: max_uses,per_user,min_amount\nمثال: 100,1,50000\nصفر در max_uses یعنی نامحدود.")


async def process_discount_create_limits(m: types.Message, state: FSMContext):
    try:
        values = [int(x.strip().replace(",", "")) for x in (m.text or "").split("|")]
        if len(values) != 3:
            values = [int(x.strip().replace(",", "")) for x in (m.text or "").split()]
        if len(values) != 3:
            raise ValueError
    except ValueError:
        return await m.answer("فرمت درست: 100 | 1 | 50000")
    await state.update_data(max_uses=max(0, values[0]), per_user_limit=max(1, values[1]), min_amount=max(0, values[2]))
    await V63States.waiting_discount_create_scope.set()
    await m.answer(
        "محدوده و شرایط را در یک خط بفرستید:\n"
        "all\nیا: plan:3 | first | new | start:2026-07-20 | end:2026-08-20\n"
        "گزینه‌ها: plan:ID، category:ID، first، new، renew، start:YYYY-MM-DD، end:YYYY-MM-DD"
    )


async def process_discount_create_scope(m: types.Message, state: FSMContext):
    data = await state.get_data()
    tokens = [x.strip() for x in (m.text or "all").split("|") if x.strip()]
    payload = {
        "code": data["discount_code"], "title": data["discount_code"], "discount_type": data["discount_type"],
        "value": data["discount_value"], "max_uses": data["max_uses"], "per_user_limit": data["per_user_limit"],
        "min_amount": data["min_amount"], "is_active": True,
    }
    for token in tokens:
        low = token.lower()
        if low.startswith("plan:"): payload["plan_id"] = int(low.split(":",1)[1])
        elif low.startswith("category:"): payload["category_id"] = int(low.split(":",1)[1])
        elif low == "first": payload["first_purchase_only"] = True
        elif low == "new": payload["new_users_only"] = True
        elif low == "renew": payload["renewals_only"] = True
        elif low.startswith("start:"): payload["starts_at"] = low.split(":",1)[1]
        elif low.startswith("end:"): payload["ends_at"] = low.split(":",1)[1]
    try:
        discount_id = commerce.create_discount(payload)
    except Exception as exc:
        return await m.answer(f"❌ ساخت کد ناموفق بود: {exc}")
    await state.finish()
    db.log_admin_action(m.from_user.id, "create_discount", details=f"discount_id={discount_id};code={payload['code']}")
    await m.answer(f"✅ کد {payload['code']} ساخته شد.", reply_markup=discounts_menu_kb())


def campaigns_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ ساخت کمپین بازگشت", callback_data="v63_campaign_create"))
    for row in commerce.list_campaigns(30):
        icon = "🟢" if int(row["is_active"] or 0) else "⚫"
        kb.add(types.InlineKeyboardButton(f"{icon} {row['title']} | {row['inactivity_days']} روز", callback_data=f"v63_campaign_toggle_{row['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ تخفیف‌ها", callback_data="adm_discounts"))
    return kb


async def cb_campaigns(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    await _replace(c, "📣 کمپین بازگشت مشتری\n\nهر کمپین برای هر کاربر فقط یک بار ارسال می‌شود.", campaigns_kb())


async def cb_campaign_toggle(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    campaign_id = int(c.data.rsplit("_", 1)[1])
    commerce.toggle_campaign(campaign_id)
    await c.answer("وضعیت تغییر کرد")
    await _replace(c, "📣 کمپین‌ها", campaigns_kb())


async def cb_campaign_create(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    await V63States.waiting_campaign_title.set()
    await c.message.answer("عنوان کمپین را بفرستید.")


async def process_campaign_title(m: types.Message, state: FSMContext):
    await state.update_data(campaign_title=(m.text or "کمپین بازگشت")[:100])
    await V63States.waiting_campaign_config.set()
    await m.answer(
        "تنظیم کمپین را بفرستید:\nروزهای عدم خرید | discount_id یا 0 | متن پیام\n"
        "مثال:\n7 | 3 | 🎁 برای بازگشت شما یک تخفیف ویژه فعال شده است."
    )


async def process_campaign_config(m: types.Message, state: FSMContext):
    parts = [x.strip() for x in (m.text or "").split("|", 2)]
    if len(parts) != 3:
        return await m.answer("فرمت درست نیست. مثال: 7 | 3 | متن پیام")
    try:
        days = max(1, int(parts[0])); discount_id = int(parts[1]) or None
    except ValueError:
        return await m.answer("روز و شناسه تخفیف باید عدد باشند.")
    data = await state.get_data()
    campaign_id = commerce.create_campaign(data.get("campaign_title") or "کمپین بازگشت", days, parts[2], discount_id)
    await state.finish()
    db.log_admin_action(m.from_user.id, "create_campaign", details=f"campaign_id={campaign_id}")
    await m.answer("✅ کمپین ساخته شد.", reply_markup=campaigns_kb())


async def run_due_campaigns(bot: Bot, limit_per_campaign: int = 100):
    sent = 0
    for campaign, user in commerce.due_campaign_recipients(limit_per_campaign):
        text = campaign["message_text"]
        if campaign["discount_id"]:
            db.cur.execute("SELECT code FROM discounts WHERE id=? AND is_active=1", (campaign["discount_id"],))
            discount = db.cur.fetchone()
            if discount:
                text += f"\n\nکد تخفیف: {discount['code']}"
        try:
            await bot.send_message(int(user["id"]), text, reply_markup=menus.main_reply_kb(user["id"]))
            commerce.record_campaign_delivery(campaign["id"], user["id"], "sent")
            sent += 1
        except Exception as exc:
            commerce.record_campaign_delivery(campaign["id"], user["id"], "failed", str(exc))
    return sent


async def campaign_loop(bot: Bot):
    while True:
        try:
            await run_due_campaigns(bot)
        except Exception:
            logger.exception("campaign worker failed")
        await asyncio.sleep(6 * 60 * 60)


async def provider_queue_loop(bot: Bot):
    """Process due provider jobs and deliver each result at most once."""
    from affiliate import reward_ref

    while True:
        try:
            results = await subs.recover_due_provider_jobs(30)
            for result in results:
                purchase_id = int(result.get("purchase_id") or 0)
                if not purchase_id:
                    continue
                order = commerce.get_order_detail(purchase_id)
                if not order:
                    continue
                user_id = int(order["user_id"])
                plan = db.get_plan(order["plan_id"])
                category_id = plan["category_id"] if plan else None
                plan_id = plan["id"] if plan else None

                if result.get("completed") and commerce.claim_purchase_notification(purchase_id, "delivery"):
                    try:
                        await _bot_send_content(
                            bot, user_id, "purchase_success",
                            {
                                "order_id": purchase_id,
                                "plan_title": plan["title"] if plan else "سرویس",
                                "quantity": len(result.get("items") or []),
                                "subtotal": content.money(order["subtotal_amount"] or order["amount"]),
                                "discount": content.money(order["discount_amount"] or 0),
                                "total": content.money(order["amount"]),
                                "balance_after": content.money(db.get_user(str(user_id))["balance"]),
                                "test_notice": "🧪 خرید آزمایشی" if int(order["is_test"] or 0) else "",
                                "post_purchase_text": plan["post_purchase_text"] if plan and "post_purchase_text" in plan.keys() else "",
                            },
                            category_id=category_id, plan_id=plan_id,
                            reply_markup=menus.main_reply_kb(user_id),
                        )
                    except Exception:
                        commerce.release_purchase_notification(purchase_id, "delivery")
                        raise

                    for index, item in enumerate(result.get("items") or [], 1):
                        try:
                            await _bot_send_content(
                                bot, user_id, "service_delivery",
                                {
                                    "order_id": purchase_id,
                                    "plan_title": plan["title"] if plan else "سرویس",
                                    "username": item.get("account_name") or item.get("panel_username") or f"سرویس {index}",
                                    "volume": plan["volume_label"] if plan else "-",
                                    "duration": plan["duration_label"] if plan else "-",
                                    "devices": "نامحدود" if not plan or plan["panel_max_devices"] in (None, "", 0) else f"{plan['panel_max_devices']} دستگاه",
                                    "expire_date": item.get("panel_expires_at") or "-",
                                    "subscription_url": item["link"],
                                    "provider_public_name": "ساخت خودکار",
                                    "created_at": item.get("assigned_at") or "-",
                                },
                                category_id=category_id, plan_id=plan_id,
                            )
                            path = make_qr(item["link"], user_id)
                            try:
                                qr = content.render("service_qr", {"username": item.get("account_name") or f"سرویس {index}"}, category_id=category_id, plan_id=plan_id)
                                with open(path, "rb") as f:
                                    await bot.send_photo(user_id, f, caption=qr["text"], parse_mode=qr["parse_mode_api"])
                            finally:
                                cleanup_qr(path)
                        except Exception:
                            logger.warning("could not send queued service #%s for purchase #%s", index, purchase_id, exc_info=True)

                    content.record_funnel(
                        user_id, "purchase_delivered", category_id=category_id, plan_id=plan_id,
                        purchase_id=purchase_id, session_key=order["request_key"],
                    )
                    referral_status, referral_detail = reward_ref(str(user_id))
                    if referral_status == "rewarded":
                        try:
                            await bot.send_message(int(referral_detail), "💰 پاداش اولین خرید زیرمجموعه به کیف پول شما اضافه شد.")
                        except Exception:
                            logger.warning("could not notify queued-purchase referrer", exc_info=True)
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, f"✅ سفارش صف #{purchase_id} تکمیل و برای کاربر ارسال شد.")
                        except Exception:
                            logger.debug("could not notify admin about queue completion", exc_info=True)

                elif result.get("refunded") and commerce.claim_purchase_notification(purchase_id, "refund"):
                    try:
                        await _bot_send_content(
                            bot, user_id, "order_refunded",
                            {"order_id": purchase_id, "refund_amount": content.money(order["amount"])},
                            reply_markup=menus.main_reply_kb(user_id),
                        )
                        content.record_funnel(
                            user_id, "purchase_refunded", category_id=category_id, plan_id=plan_id,
                            purchase_id=purchase_id, session_key=order["request_key"],
                        )
                    except Exception:
                        commerce.release_purchase_notification(purchase_id, "refund")
                        raise
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, f"⚠️ سفارش #{purchase_id} بازپرداخت شد و نیازمند بررسی ادمین است.")
                        except Exception:
                            logger.debug("could not notify admin about queue refund", exc_info=True)
        except Exception:
            logger.exception("provider queue worker failed")
        await asyncio.sleep(20)


# -------------------- Plan template library --------------------

def templates_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ ساخت قالب", callback_data="v63_tpl_create"))
    for row in commerce.list_plan_templates(False):
        icon = "🟢" if int(row["is_active"] or 0) else "⚫"
        system = "🔒" if int(row["is_system"] or 0) else ""
        kb.add(types.InlineKeyboardButton(f"{icon}{system} {row['title']}", callback_data=f"v63_tpl_{row['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ مدیریت پیام‌ها", callback_data="adm_messages"))
    return kb


async def cb_templates(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    await _replace(c, "📝 کتابخانه قالب‌های متن پلن\n\nاولویت اجرا: قالب پلن ← قالب دسته ← قالب عمومی.", templates_kb())


async def cb_template_detail(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    row = commerce.get_plan_template(template_id)
    if not row: return await c.message.answer("قالب پیدا نشد.")
    text = f"📝 {row['title']}\n\nوضعیت: {'فعال' if row['is_active'] else 'غیرفعال'}\nسیستمی: {'بله' if row['is_system'] else 'خیر'}\n\n{row['body']}"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("👁 پیش‌نمایش واقعی", callback_data=f"v63_tpl_preview_{template_id}"))
    kb.add(types.InlineKeyboardButton("✏️ ویرایش", callback_data=f"v63_tpl_edit_{template_id}"))
    kb.add(types.InlineKeyboardButton("📋 کپی قالب", callback_data=f"v63_tpl_copy_{template_id}"))
    kb.add(types.InlineKeyboardButton("🟢 فعال/غیرفعال", callback_data=f"v63_tpl_toggle_{template_id}"))
    kb.add(types.InlineKeyboardButton("🎯 تخصیص به عمومی/دسته/پلن", callback_data=f"v63_tpl_assign_{template_id}"))
    if int(row["is_system"] or 0):
        kb.add(types.InlineKeyboardButton("♻️ بازگردانی نسخه اصلی", callback_data=f"v63_tpl_restore_{template_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ قالب‌ها", callback_data="adm_plan_templates"))
    await _replace(c, text[:3900], kb)


async def cb_template_preview(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    await c.answer()
    try:
        preview = commerce.template_preview(template_id)
    except Exception as exc:
        preview = f"❌ {exc}"
    await c.message.answer("👁 پیش‌نمایش با اطلاعات واقعی/نمونه:\n\n" + preview[:3800])


async def cb_template_copy(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    new_id = commerce.copy_plan_template(template_id)
    await c.answer("کپی شد")
    await _replace(c, f"✅ کپی قالب با شناسه {new_id} ساخته شد.", templates_kb())


async def cb_template_toggle(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    commerce.toggle_plan_template(template_id)
    await c.answer("ذخیره شد")
    c.data = f"v63_tpl_{template_id}"
    await cb_template_detail(c)


async def cb_template_restore(c: types.CallbackQuery):
    if not _admin(c.from_user.id): return await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    ok = commerce.restore_system_template(template_id)
    await c.answer("بازگردانی شد" if ok else "امکان بازگردانی نیست")
    c.data = f"v63_tpl_{template_id}"
    await cb_template_detail(c)


async def cb_template_create(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id): return await c.answer()
    await c.answer()
    await V63States.waiting_template_title.set()
    await c.message.answer("عنوان قالب جدید را بفرستید.")


async def process_template_title(m: types.Message, state: FSMContext):
    await state.update_data(template_title=(m.text or "قالب جدید")[:100])
    await V63States.waiting_template_body.set()
    await m.answer(
        "متن قالب را بفرستید. متغیرهای مجاز:\n"
        "{title} {category} {volume} {duration} {price} {devices} {delivery} {start_mode} "
        "{username} {subscription_url} {expire_date} {description} {tag}\n\n"
        "متغیرهای ضروری: {title} و {price}"
    )


async def process_template_body(m: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        template_id = commerce.create_plan_template(data.get("template_title") or "قالب جدید", m.text or "")
    except Exception as exc:
        return await m.answer(f"❌ قالب ذخیره نشد: {exc}")
    await state.finish()
    db.log_admin_action(m.from_user.id, "create_plan_template", details=f"template_id={template_id}")
    await m.answer("✅ قالب ساخته شد.", reply_markup=templates_kb())


async def cb_template_edit(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id): return await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    await state.update_data(template_edit_id=template_id)
    await V63States.waiting_template_edit.set()
    await c.answer()
    await c.message.answer("متن جدید قالب را بفرستید. قبل از ذخیره، متغیرها و محدودیت تلگرام بررسی می‌شوند.")


async def process_template_edit(m: types.Message, state: FSMContext):
    data = await state.get_data(); template_id = int(data.get("template_edit_id") or 0)
    try:
        commerce.update_plan_template(template_id, m.text or "")
    except Exception as exc:
        return await m.answer(f"❌ ذخیره نشد: {exc}")
    await state.finish()
    db.log_admin_action(m.from_user.id, "edit_plan_template", details=f"template_id={template_id}")
    await m.answer("✅ قالب به‌روزرسانی شد.", reply_markup=templates_kb())


async def cb_template_assign(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id): return await c.answer()
    template_id = int(c.data.rsplit("_", 1)[1])
    await state.update_data(template_assign_id=template_id)
    await V63States.waiting_template_assign.set()
    await c.answer()
    await c.message.answer("محل تخصیص را بفرستید:\ndefault\ncategory:ID\nplan:ID")


async def process_template_assign(m: types.Message, state: FSMContext):
    data = await state.get_data(); template_id = int(data.get("template_assign_id") or 0)
    raw = (m.text or "").strip().lower()
    try:
        if raw == "default": commerce.set_default_plan_template(template_id)
        elif raw.startswith("category:"): commerce.set_category_template(int(raw.split(":",1)[1]), template_id)
        elif raw.startswith("plan:"): commerce.set_plan_template(int(raw.split(":",1)[1]), template_id)
        else: raise ValueError("فرمت نامعتبر")
    except Exception as exc:
        return await m.answer(f"❌ تخصیص انجام نشد: {exc}")
    await state.finish()
    db.log_admin_action(m.from_user.id, "assign_plan_template", details=f"template_id={template_id};target={raw}")
    await m.answer("✅ قالب تخصیص داده شد.", reply_markup=templates_kb())


def register(dp):
    # User discount and service callbacks.
    dp.register_callback_query_handler(cb_discount_plan, lambda c: c.data.startswith("discount_plan_"), state="*")
    dp.register_callback_query_handler(cb_discount_clear, lambda c: c.data.startswith("discount_clear_"), state="*")
    dp.register_message_handler(process_discount_code, content_types=types.ContentTypes.TEXT, state=V63States.waiting_discount_code)
    dp.register_callback_query_handler(cb_service_detail, lambda c: c.data.startswith("service_detail_"))
    dp.register_callback_query_handler(cb_service_link, lambda c: c.data.startswith("service_link_"))
    dp.register_callback_query_handler(cb_service_qr, lambda c: c.data.startswith("service_qr_"))
    dp.register_callback_query_handler(cb_service_usage, lambda c: c.data.startswith("service_usage_"))
    dp.register_callback_query_handler(cb_service_issue, lambda c: c.data.startswith("service_issue_"))
    dp.register_callback_query_handler(cb_issue_create, lambda c: c.data.startswith("issue_create_"))
    dp.register_callback_query_handler(cb_issue_admin_check, lambda c: c.data.startswith("issue_admin_check_"))
    dp.register_callback_query_handler(cb_issue_admin_usage, lambda c: c.data.startswith("issue_admin_usage_"))
    dp.register_callback_query_handler(cb_issue_admin_revoke, lambda c: c.data.startswith("issue_admin_revoke_"))
    dp.register_callback_query_handler(cb_issue_admin_reply, lambda c: c.data.startswith("issue_admin_reply_"))
    dp.register_callback_query_handler(cb_issue_admin_delete_confirm, lambda c: c.data.startswith("issue_admin_delete_confirm_"))
    dp.register_callback_query_handler(cb_issue_admin_delete, lambda c: c.data.startswith("issue_admin_delete_"))

    # Provider overrides are registered before legacy admin provider handlers.
    dp.register_callback_query_handler(cb_providers, lambda c: c.data == "adm_providers")
    dp.register_callback_query_handler(cb_provider_health, lambda c: c.data.startswith("v63_provider_health_"))
    dp.register_callback_query_handler(cb_provider_toggle, lambda c: c.data.startswith("v63_provider_toggle_"))
    dp.register_callback_query_handler(cb_provider_logs, lambda c: c.data.startswith("v63_provider_logs_"))
    dp.register_callback_query_handler(cb_provider_detail, lambda c: c.data.startswith("v63_provider_"))
    dp.register_callback_query_handler(cb_plan_fallback_set, lambda c: c.data.startswith("v63_plan_fallback_set_"))
    dp.register_callback_query_handler(cb_plan_fallback, lambda c: c.data.startswith("v63_plan_fallback_"))

    # Order queue.
    dp.register_callback_query_handler(cb_order_queue, lambda c: c.data == "adm_order_queue")
    dp.register_callback_query_handler(cb_order_run_due, lambda c: c.data == "v63_order_run_due")
    dp.register_callback_query_handler(cb_order_retry, lambda c: c.data.startswith("v63_order_retry_"))
    dp.register_callback_query_handler(cb_order_refund_confirm, lambda c: c.data.startswith("v63_order_refund_confirm_"))
    dp.register_callback_query_handler(cb_order_refund, lambda c: c.data.startswith("v63_order_refund_"))
    dp.register_callback_query_handler(cb_order_reviewed, lambda c: c.data.startswith("v63_order_reviewed_"))
    dp.register_callback_query_handler(cb_order_detail, lambda c: c.data.startswith("v63_order_"))

    # Reports.
    dp.register_callback_query_handler(cb_reports, lambda c: c.data == "adm_sales_report")
    dp.register_callback_query_handler(cb_reports_overview, lambda c: c.data == "v63_reports_overview")
    dp.register_callback_query_handler(cb_reports_plans, lambda c: c.data == "v63_reports_plans")
    dp.register_callback_query_handler(cb_reports_categories, lambda c: c.data == "v63_reports_categories")
    dp.register_callback_query_handler(cb_reports_trials, lambda c: c.data == "v63_reports_trials")
    dp.register_callback_query_handler(cb_reports_customers, lambda c: c.data == "v63_reports_customers")
    dp.register_callback_query_handler(cb_reports_inventory, lambda c: c.data == "v63_reports_inventory")

    # Discounts and campaigns.
    dp.register_callback_query_handler(cb_discounts, lambda c: c.data == "adm_discounts")
    dp.register_callback_query_handler(cb_discount_create, lambda c: c.data == "v63_discount_create", state="*")
    dp.register_callback_query_handler(cb_discount_type, lambda c: c.data.startswith("v63_discount_type_"), state="*")
    dp.register_callback_query_handler(cb_discount_toggle, lambda c: c.data.startswith("v63_discount_toggle_"))
    dp.register_callback_query_handler(cb_discount_detail, lambda c: c.data.startswith("v63_discount_"))
    dp.register_message_handler(process_discount_create_code, content_types=types.ContentTypes.TEXT, state=V63States.waiting_discount_create_code)
    dp.register_message_handler(process_discount_create_value, content_types=types.ContentTypes.TEXT, state=V63States.waiting_discount_create_value)
    dp.register_message_handler(process_discount_create_limits, content_types=types.ContentTypes.TEXT, state=V63States.waiting_discount_create_limits)
    dp.register_message_handler(process_discount_create_scope, content_types=types.ContentTypes.TEXT, state=V63States.waiting_discount_create_scope)
    dp.register_callback_query_handler(cb_campaigns, lambda c: c.data == "v63_campaigns")
    dp.register_callback_query_handler(cb_campaign_toggle, lambda c: c.data.startswith("v63_campaign_toggle_"))
    dp.register_callback_query_handler(cb_campaign_create, lambda c: c.data == "v63_campaign_create", state="*")
    dp.register_message_handler(process_campaign_title, content_types=types.ContentTypes.TEXT, state=V63States.waiting_campaign_title)
    dp.register_message_handler(process_campaign_config, content_types=types.ContentTypes.TEXT, state=V63States.waiting_campaign_config)

    # The legacy plan-template editor is not registered in v6.4.
    # All customer text and plan/category overrides are managed by v64_handlers.
