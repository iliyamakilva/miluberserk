import asyncio
import logging
import sys
import traceback

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.utils import executor

import admin
import commerce
import content
import backup
import db
import expiry_alerts
import force_join
import menus
import messages
import pasarguard_backup
import settings
import subs
import tickets
import wallet
import v63_handlers
import v64_handlers
from affiliate import reward_ref
from config import (
    ADMIN_COMMAND, ADMIN_IDS, BOT_TOKEN, TRIAL_DAYS, TRIAL_ENABLED,
    TRIAL_MAX_DEVICES, TRIAL_SIZE_MB,
    validate,
)
from fsm_storage import SQLiteStorage
from utils import cleanup_qr, make_qr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

validate()

if ADMIN_COMMAND == "panel_secret":
    logger.warning(
        "ADMIN_COMMAND هنوز مقدار پیش‌فرضه! حتماً توی Railway یه اسم اختصاصی "
        "براش ست کنید، مثلاً panel_x7k9."
    )

bot = Bot(token=BOT_TOKEN)

db.init()
commerce.init_schema()
content.init_schema()
settings.ensure_defaults()

dp = Dispatcher(bot, storage=SQLiteStorage())

DISPOSABLE_MESSAGE_KINDS = ("menu", "temp", "preview", "list")


def wallet_menu_kb(include_bulk=False, user_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💳 شارژ کیف پول", callback_data="topup_start"))
    if include_bulk:
        kb.add(types.InlineKeyboardButton(content.render_button("btn_bulk"), callback_data="buy_bulk"))
    menus.append_location_buttons(kb, "wallet", user_id)
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def section_menu_kb(location, user_id=None, *, leading_buttons=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for button in leading_buttons or []:
        kb.add(button)
    menus.append_location_buttons(kb, location, user_id)
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def _audience_allowed(audience, user_id):
    audience = (audience or "all").strip().lower()
    if audience == "all":
        return True
    if audience == "admins":
        return menus.is_admin_user(user_id)
    try:
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


def _normalized_text(value):
    return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()


def _shared_plan_text(plans, field):
    """Return a non-empty field only when every plan uses the same text."""
    values = [_normalized_text(plan[field]) for plan in plans]
    if not values or any(not value for value in values):
        return ""
    return values[0] if all(value == values[0] for value in values[1:]) else ""


def _plan_is_unlimited(plan) -> bool:
    return bool(plan["unlimited_volume"]) if "unlimited_volume" in plan.keys() else False


def _plan_provider_settings_missing(plan) -> bool:
    """True when a provider-delivered plan is missing volume/duration setup.

    A plan explicitly marked as unlimited volume is exempt from the volume
    check (0 there means "no cap", not "not configured").
    """
    if not _plan_is_unlimited(plan) and int(plan["panel_data_limit_bytes"] or 0) <= 0:
        return True
    if int(plan["panel_duration_days"] or 0) <= 0:
        return True
    return False


def _package_label(plan):
    """Compact customer-facing package label without category counts or stock numbers."""
    parts = []
    if plan["volume_label"]:
        parts.append(str(plan["volume_label"]).strip())
    if plan["duration_label"]:
        parts.append(str(plan["duration_label"]).strip())
    if not parts:
        parts.append(str(plan["title"] or "بسته سرویس").strip())
    parts.append(f"{int(plan['price'] or 0):,} تومان")
    return " | ".join(parts)


def catalog_root_kb(user_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for category in db.list_plan_categories(active_only=True, include_empty=False):
        if not _audience_allowed(category["audience"], user_id):
            continue
        icon = (category["emoji"] or "📦").strip()
        label = f"{icon} {category['title']}"
        category_display = content.get_display_settings(category_id=category["id"])
        if category_display["show_category_plan_count"]:
            label += f" ({int(category['active_plan_count'] or 0)})"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"buy_cat_{category['id']}"))
    menus.append_location_buttons(kb, "buy", user_id)
    kb.add(types.InlineKeyboardButton(content.render_button("btn_bulk"), callback_data="buy_bulk"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def category_plans_kb(category_id, user_id=None):
    """Render customer-editable package labels while callbacks stay immutable."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for plan in db.list_plans(active_only=True, category_id=category_id):
        settings_view = content.get_display_settings(category_id=category_id, plan_id=plan["id"])
        provider_key = db.plan_provider_key(plan)
        stock_count = subs.stock_count(plan["id"]) if provider_key == "pool" else None
        unavailable = provider_key == "pool" and stock_count <= 0
        if unavailable:
            stock_status = "ناموجود"
        elif settings_view["show_numeric_stock"] and stock_count is not None:
            stock_status = f"موجودی: {stock_count}"
        else:
            stock_status = "موجود"
        label = content.render_button(
            "package_button",
            {
                "title": plan["title"] or "بسته سرویس",
                "volume": plan["volume_label"] or "-",
                "duration": plan["duration_label"] or "-",
                "price": content.money(plan["price"]),
                "tag": plan["tag"] or "",
                "stock_status": stock_status if settings_view["show_stock_status"] else "",
            },
            category_id=category_id,
            plan_id=plan["id"],
        )
        if unavailable and settings_view["show_stock_status"] and "ناموجود" not in label:
            label += " | ناموجود"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"buy_plan_{plan['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ دسته‌های سرویس", callback_data="buy"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def plan_action_kb(plan, max_qty=1, user_id=None):
    """Checkout actions only; texts are editable, callbacks remain hard-coded."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    plan_id = int(plan["id"])
    category_id = plan["category_id"] if "category_id" in plan.keys() else None
    display = content.get_display_settings(category_id=category_id, plan_id=plan_id)
    mode = db.plan_purchase_mode(plan)
    safe_max_qty = min(4, max(1, int(max_qty)))
    if mode == "direct" or (mode == "quantity" and safe_max_qty == 1):
        kb.add(types.InlineKeyboardButton(content.render_button("btn_pay"), callback_data=f"buy_qty_1_{plan_id}"))
    elif mode == "quantity":
        for qty in range(1, safe_max_qty + 1):
            kb.insert(types.InlineKeyboardButton(f"{qty} عدد", callback_data=f"buy_qty_{qty}_{plan_id}"))
    elif mode == "wholesale":
        kb.add(types.InlineKeyboardButton(content.render_button("btn_bulk"), callback_data="buy_bulk"))
    if mode in {"direct", "quantity"} and display["show_discount_button"]:
        kb.add(types.InlineKeyboardButton(content.render_button("btn_discount"), callback_data=f"discount_plan_{plan_id}"))
    if mode in {"direct", "quantity"} and db.plan_provider_key(plan) != "pool":
        kb.add(v63_handlers.custom_name_button(plan_id))
    if category_id:
        kb.add(types.InlineKeyboardButton(content.render_button("btn_back_packages"), callback_data=f"buy_cat_{category_id}"))
    else:
        kb.add(types.InlineKeyboardButton("⬅️ بازگشت به پلن‌ها", callback_data="buy"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


def plans_kb(category_id=None, user_id=None):
    """Compatibility helper; catalog screens now use categories."""
    if category_id is None:
        return catalog_root_kb(user_id)
    return category_plans_kb(category_id, user_id)


def buy_quantity_kb(max_qty: int, plan_id=None):
    plan = db.get_plan(plan_id) if plan_id is not None else None
    if plan:
        return plan_action_kb(plan, max_qty)
    kb = types.InlineKeyboardMarkup(row_width=2)
    for qty in range(1, min(4, max(1, int(max_qty))) + 1):
        kb.insert(types.InlineKeyboardButton(f"{qty} عدد", callback_data=f"buy_qty_{qty}"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb


async def send_main_menu(target, user_id: int):
    await _start_clean_section(target, user_id, "main")
    return await _send_content(
        target, user_id, "main_menu",
        reply_markup=menus.main_reply_kb(user_id), context="main", kind="menu",
    )

async def _safe_delete_callback_message(c: types.CallbackQuery):
    try:
        await c.message.delete()
        return True
    except Exception:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception as exc:
            logger.debug("could not remove callback keyboard: %s", exc)
        return False


async def _track_sent(user_id, sent, context="", kind="menu"):
    if sent is None:
        return
    if not isinstance(sent, (list, tuple)):
        sent = [sent]
    for msg in sent:
        try:
            db.track_bot_message(
                msg.chat.id,
                user_id,
                msg.message_id,
                context=context,
                kind=kind,
            )
        except Exception:
            logger.debug("Could not track bot message", exc_info=True)


async def _cleanup_user_messages(chat_id, user_id, kinds=DISPOSABLE_MESSAGE_KINDS):
    rows = db.list_tracked_bot_messages(chat_id, user_id, limit=60, kinds=kinds)
    for row in rows:
        try:
            await bot.delete_message(int(row["chat_id"]), int(row["message_id"]))
        except Exception:
            try:
                await bot.edit_message_reply_markup(
                    int(row["chat_id"]),
                    int(row["message_id"]),
                    reply_markup=None,
                )
            except Exception:
                logger.debug("Could not delete tracked message", exc_info=True)
        finally:
            db.clear_tracked_bot_message(row["chat_id"], row["message_id"])


async def _start_clean_section(target, user_id, context=""):
    try:
        chat_id = target.chat.id
    except Exception:
        try:
            chat_id = target.message.chat.id
        except Exception:
            return
    await _cleanup_user_messages(chat_id, user_id)


async def _send_template(
    target, user_id, key, body_text, reply_markup=None, context="", kind="menu"
):
    sent = await messages.send(target, key, body_text, reply_markup=reply_markup)
    await _track_sent(user_id, sent, context or key, kind=kind)
    return sent


async def _send_content(
    target, user_id, key, values=None, *, category_id=None, plan_id=None,
    reply_markup=None, context="", kind="menu"
):
    sent = await content.send(
        target, key, values or {}, category_id=category_id, plan_id=plan_id,
        reply_markup=reply_markup,
    )
    await _track_sent(user_id, sent, context or key, kind=kind)
    return sent


async def _send_answer(
    target, user_id, text, reply_markup=None, context="", kind="menu"
):
    chunks = messages.split_text(text)
    sent_messages = []
    for index, chunk in enumerate(chunks):
        sent_messages.append(
            await target.answer(
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        )
    await _track_sent(user_id, sent_messages, context, kind=kind)
    return sent_messages


async def render_buy(target, user_id: int, username: str = "", plan_id=None, category_id=None):
    """Render one clean catalog path using the v6.4 content engine."""
    await _start_clean_section(target, user_id, "buy")
    user_id_str = str(user_id)
    user = db.get_user(user_id_str)
    if user and user["banned"]:
        return await _send_content(target, user_id, "account_banned", reply_markup=menus.main_reply_kb(user_id), context="buy")
    if not _is_admin_user(user_id) and not settings.sales_enabled():
        return await _send_content(
            target, user_id, "sales_closed",
            reply_markup=menus.main_reply_kb(user_id), context="buy_closed",
        )
    db.touch_active(user_id_str, username, getattr(target.from_user, "full_name", None) if hasattr(target, "from_user") else None)
    if user is None:
        user, _ = db.get_or_create_user(user_id_str, username)
    balance = int(user["balance"] or 0) if user else 0
    session_key = f"tg:{user_id}:{getattr(getattr(target, 'chat', None), 'id', user_id)}:{getattr(target, 'message_id', 0)}"

    if plan_id is None and category_id is None:
        content.record_funnel(user_id, "buy_open", session_key=session_key)
        categories = [
            category for category in db.list_plan_categories(active_only=True, include_empty=False)
            if _audience_allowed(category["audience"], user_id)
        ]
        buy_extras = menus.inline_location_buttons("buy", user_id)
        if not categories and not buy_extras:
            return await _send_content(
                target, user_id, "no_services",
                reply_markup=menus.main_reply_kb(user_id), context="buy",
            )
        summary_lines = []
        display = content.get_display_settings()
        for category in categories:
            icon = category["emoji"] or "📦"
            label = f"{icon} {category['title']}"
            if display["show_category_plan_count"]:
                label += f" ({int(category['active_plan_count'] or 0)})"
            summary_lines.append(label)
            if category["description"]:
                summary_lines.append(str(category["description"]).strip())
        return await _send_content(
            target, user_id, "buy_root",
            {
                "categories_summary": "\n".join(summary_lines),
                "wallet_balance": content.money(balance),
            },
            reply_markup=catalog_root_kb(user_id), context="buy",
        )

    if plan_id is None and category_id is not None:
        category = db.get_plan_category(category_id)
        active_category_ids = {
            int(row["id"]) for row in db.list_plan_categories(active_only=True, include_empty=True)
            if _audience_allowed(row["audience"], user_id)
        }
        if not category or int(category["id"]) not in active_category_ids:
            return await render_buy(target, user_id, username)
        plans = db.list_plans(active_only=True, category_id=category_id)
        if not plans:
            return await _send_content(
                target, user_id, "category_empty", category_id=category_id,
                reply_markup=catalog_root_kb(user_id), context="buy_category",
            )
        content.record_funnel(user_id, "category_view", category_id=category_id, session_key=session_key)
        display = content.get_display_settings(category_id=category_id)
        category_description = _normalized_text(category["description"])
        shared_description = _shared_plan_text(plans, "description")
        if shared_description == category_description:
            shared_description = ""
        shared_pre_purchase = _shared_plan_text(plans, "pre_purchase_text")
        if shared_pre_purchase in {category_description, shared_description}:
            shared_pre_purchase = ""
        provider_keys = {db.plan_provider_key(plan) for plan in plans}
        delivery_line = ""
        if display["show_delivery"] and len(provider_keys) == 1:
            provider_key = next(iter(provider_keys))
            delivery_line = f"🚚 تحویل: {'آماده و فوری' if provider_key == 'pool' else 'ساخت خودکار'}"
        devices_line = ""
        device_values = {
            "نامحدود" if plan["panel_max_devices"] in (None, "", 0) else f"{plan['panel_max_devices']} دستگاه"
            for plan in plans
        }
        if display["show_devices"] and len(device_values) == 1:
            devices_line = f"📱 دستگاه: {next(iter(device_values))}"
        start_mode_line = ""
        start_modes = {
            "اولین اتصال" if (plan["panel_start_mode"] or "on_hold") == "on_hold" else "فوری"
            for plan in plans
        }
        if display["show_start_mode"] and len(start_modes) == 1:
            start_mode_line = f"⏱ شروع اعتبار: {next(iter(start_modes))}"
        return await _send_content(
            target, user_id, "buy_category",
            {
                "category_emoji": category["emoji"] or "📦",
                "category_title": category["title"] or "سرویس‌ها",
                "category_description": category_description,
                "shared_description": shared_description,
                "shared_pre_purchase": shared_pre_purchase,
                "delivery_line": delivery_line,
                "devices_line": devices_line,
                "start_mode_line": start_mode_line,
                "wallet_balance_line": f"💳 موجودی کیف پول: {content.money(balance)}" if display["show_wallet_balance"] else "",
            },
            category_id=category_id,
            reply_markup=category_plans_kb(category_id, user_id), context="buy_category",
        )

    plan = db.get_plan(plan_id)
    if not plan or int(plan["is_active"] or 0) != 1 or db.plan_purchase_mode(plan) == "disabled":
        return await _send_content(
            target, user_id, "plan_unavailable",
            reply_markup=catalog_root_kb(user_id), context="buy",
        )
    plan_id = int(plan["id"])
    category_id = int(plan["category_id"] or 0) or None
    provider_key = db.plan_provider_key(plan)
    stock = subs.stock_count(plan_id) if provider_key == "pool" else None
    max_per_order = max(1, min(100, int(plan["max_per_order"] or 1)))
    max_qty = min(max_per_order, 4, stock) if provider_key == "pool" and stock is not None else min(max_per_order, 4)
    category = db.get_plan_category(category_id) if category_id else None
    if category:
        active_category_ids = {
            int(row["id"]) for row in db.list_plan_categories(active_only=True, include_empty=True)
            if _audience_allowed(row["audience"], user_id)
        }
        if int(category["id"]) not in active_category_ids:
            return await _send_content(
                target, user_id, "plan_unavailable",
                reply_markup=catalog_root_kb(user_id), context="buy",
            )

    if provider_key == "pool" and stock <= 0:
        return await _send_content(
            target, user_id, "plan_unavailable", category_id=category_id, plan_id=plan_id,
            reply_markup=category_plans_kb(category_id, user_id), context="buy_checkout",
        )

    provider_notice = ""
    if provider_key != "pool":
        active_provider, provider_notice = commerce.resolve_provider_for_plan(plan)
        if not active_provider:
            return await _send_content(
                target, user_id, "provider_unavailable", category_id=category_id, plan_id=plan_id,
                reply_markup=category_plans_kb(category_id, user_id), context="buy_checkout",
            )
        try:
            provider = subs.get_provider_adapter(active_provider)
            if not provider.configured():
                raise subs.ProviderError("provider not configured")
        except Exception:
            return await _send_content(
                target, user_id, "provider_unavailable", category_id=category_id, plan_id=plan_id,
                reply_markup=category_plans_kb(category_id, user_id), context="buy_checkout",
            )
        if _plan_provider_settings_missing(plan):
            return await _send_content(
                target, user_id, "provider_unavailable", category_id=category_id, plan_id=plan_id,
                reply_markup=category_plans_kb(category_id, user_id), context="buy_checkout",
            )

    content.record_funnel(user_id, "plan_checkout", category_id=category_id, plan_id=plan_id, session_key=session_key)
    display = content.get_display_settings(category_id=category_id, plan_id=plan_id)
    mode = db.plan_purchase_mode(plan)
    if mode == "direct" or (mode == "quantity" and max_qty == 1):
        checkout_hint = "برای پرداخت و دریافت سرویس، دکمه زیر را بزنید."
    elif mode == "quantity":
        checkout_hint = "تعداد موردنظر را انتخاب کنید؛ در صورت کسری موجودی، مستقیم به پرداخت هدایت می‌شوید."
    else:
        checkout_hint = "این سرویس فقط با هماهنگی خرید عمده ارائه می‌شود."
    if provider_notice:
        checkout_hint = f"{provider_notice}\n{checkout_hint}"
    devices = ""
    if display["show_devices"]:
        devices = "نامحدود" if plan["panel_max_devices"] in (None, "", 0) else f"{plan['panel_max_devices']} دستگاه"
    return await _send_content(
        target, user_id, "checkout",
        {
            "category_title": category["title"] if category else (plan["title"] or "سرویس"),
            "package_title": plan["title"] or plan["volume_label"] or "بسته سرویس",
            "volume": plan["volume_label"] or "-",
            "duration": plan["duration_label"] or "-",
            "devices_line": f"📱 دستگاه: {devices}" if devices else "",
            "price": content.money(plan["price"]),
            "wallet_balance_line": f"💳 موجودی کیف پول: {content.money(balance)}" if display["show_wallet_balance"] else "",
            "discount_line": "",
            "plan_description": _normalized_text(plan["description"]),
            "pre_purchase_text": _normalized_text(plan["pre_purchase_text"]),
            "checkout_hint": checkout_hint,
        },
        category_id=category_id, plan_id=plan_id,
        reply_markup=plan_action_kb(plan, max_qty, user_id), context="buy_checkout",
    )

async def check_low_stock_alert(plan_id=None):
    """هشدار موجودی کم برای هر پلن، بدون ارسال تکراری تا وقتی موجودی دوباره بالا برود."""
    plans = []
    if plan_id is not None:
        plan = db.get_plan(plan_id)
        if plan:
            plans = [plan]
    else:
        plans = db.list_plans(active_only=True, limit=50)

    for plan in plans:
        if db.plan_delivery_type(plan) != "pool":
            continue
        threshold = int(plan["low_stock_threshold"] or settings.low_stock_threshold())
        current_stock = subs.stock_count(plan["id"])
        if current_stock > threshold:
            db.set_plan_low_stock_alerted(plan["id"], False)
            continue
        if db.is_plan_low_stock_alerted(plan["id"]):
            continue
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ موجودی پلن «{plan['title']}» کم شده است.\n"
                    f"موجودی فعلی: {current_stock} لینک\n"
                    f"حد هشدار: {threshold} لینک\n"
                    "لطفاً برای این پلن لینک جدید وارد کنید.",
                )
            except Exception as exc:
                logger.warning("could not send low-stock alert to admin %s: %s", admin_id, exc)
        db.set_plan_low_stock_alerted(plan["id"], True)


def _is_admin_user(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False


async def _send_bot_disabled(target, user_id):
    await _start_clean_section(target, user_id, "bot_disabled")
    return await _send_content(
        target, user_id, "bot_disabled", reply_markup=menus.main_reply_kb(user_id),
        context="bot_disabled", kind="menu",
    )


async def _send_sales_closed(target, user_id):
    await _start_clean_section(target, user_id, "sales_closed")
    return await _send_content(
        target, user_id, "sales_closed", reply_markup=menus.main_reply_kb(user_id),
        context="sales_closed", kind="menu",
    )


@dp.message_handler(lambda m: not _is_admin_user(m.from_user.id) and not settings.bot_enabled(), content_types=types.ContentTypes.ANY, state="*")
async def bot_disabled_message_handler(m: types.Message):
    await _send_bot_disabled(m, m.from_user.id)


@dp.callback_query_handler(lambda c: not _is_admin_user(c.from_user.id) and not settings.bot_enabled(), state="*")
async def bot_disabled_callback_handler(c: types.CallbackQuery):
    await c.answer()
    await _send_bot_disabled(c.message, c.from_user.id)


@dp.message_handler(commands=["start"])
async def start(m: types.Message):
    user_id = str(m.from_user.id)
    ref = None
    args = m.get_args()

    if args and args.isdigit() and args != user_id:
        ref = args

    row, _ = db.get_or_create_user(user_id, m.from_user.username, ref, m.from_user.full_name)
    db.touch_active(user_id, m.from_user.username, m.from_user.full_name)

    if row["banned"]:
        return await _send_content(m, m.from_user.id, "account_banned", reply_markup=menus.main_reply_kb(m.from_user.id), context="account_banned")

    await _start_clean_section(m, m.from_user.id, "welcome")
    await _send_template(
        m,
        m.from_user.id,
        "welcome",
        "⚡ Berserk VPN Ready\n\nمنوی اصلی پایین صفحه همیشه در دسترس شماست.",
        reply_markup=menus.main_reply_kb(m.from_user.id),
        context="welcome",
        kind="menu",
    )


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "buy"))
async def text_buy(m: types.Message):
    await render_buy(m, m.from_user.id, m.from_user.username or "")


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "my_subs"))
async def text_my_subs(m: types.Message):
    await show_my_subs(m, m.from_user.id, m.from_user.username or "")


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "wallet"))
async def text_wallet(m: types.Message):
    await show_wallet(m, m.from_user.id, m.from_user.username or "")


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "guide"))
async def text_guide(m: types.Message):
    await show_guide_menu(m, m.from_user.id, m.from_user.username or "")


async def _create_and_send_trial(target, user_id: int, username: str = "", full_name: str = ""):
    await _start_clean_section(target, user_id, "trial")
    if not TRIAL_ENABLED:
        return await _send_content(target, user_id, "trial_error", reply_markup=menus.main_reply_kb(user_id), context="trial")
    try:
        trial_provider = subs.get_provider_adapter(settings.trial_provider_key())
    except subs.ProviderError:
        return await _send_content(target, user_id, "trial_error", reply_markup=menus.main_reply_kb(user_id), context="trial")
    if not trial_provider.configured():
        return await _send_content(target, user_id, "trial_error", reply_markup=menus.main_reply_kb(user_id), context="trial")
    user, _ = db.get_or_create_user(str(user_id), username, display_name=full_name)
    if int(user["banned"] or 0):
        return await _send_content(target, user_id, "account_banned", reply_markup=menus.main_reply_kb(user_id), context="trial")
    await _send_content(
        target, user_id, "trial_build",
        {"volume": f"{TRIAL_SIZE_MB} مگابایت", "duration": f"{TRIAL_DAYS} روز", "devices": f"{TRIAL_MAX_DEVICES} دستگاه"},
        context="trial_build", kind="temp",
    )
    try:
        item = await subs.create_trial_service(str(user_id), TRIAL_SIZE_MB, TRIAL_DAYS, provider_key=settings.trial_provider_key())
    except subs.ProviderError as exc:
        if exc.code == "already_claimed":
            return await _send_content(target, user_id, "trial_duplicate", reply_markup=menus.main_reply_kb(user_id), context="trial_result", kind="important")
        return await _send_content(target, user_id, "trial_error", reply_markup=menus.main_reply_kb(user_id), context="trial_result", kind="important")
    except Exception:
        logger.exception("trial provisioning failed for user %s", user_id)
        return await _send_content(target, user_id, "trial_error", reply_markup=menus.main_reply_kb(user_id), context="trial_result", kind="important")

    await _send_content(
        target, user_id, "trial_success",
        {
            "username": item["account_name"] or "اکانت تست",
            "volume": f"{TRIAL_SIZE_MB} مگابایت",
            "duration": f"{TRIAL_DAYS} روز از اولین اتصال",
            "subscription_url": item["link"],
        },
        reply_markup=menus.main_reply_kb(user_id), context="trial_delivery", kind="delivery",
    )
    qr_path = make_qr(item["link"], str(user_id))
    try:
        with open(qr_path, "rb") as qr_file:
            qr_caption = content.render("trial_qr", {"username": item["account_name"] or ""})
            sent = await target.answer_photo(qr_file, caption=qr_caption["text"], parse_mode=qr_caption["parse_mode_api"])
            await _track_sent(user_id, sent, "trial_qr", kind="delivery")
    finally:
        cleanup_qr(qr_path)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🧪 اکانت تست ساخته شد\nکاربر: {full_name or username or user_id}\nID: {user_id}\nPanel user: {item['panel_username'] or '-'}")
        except Exception:
            logger.debug("could not notify admin about trial", exc_info=True)


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "trial"))
async def text_trial(m: types.Message):
    await _create_and_send_trial(m, m.from_user.id, m.from_user.username or "", m.from_user.full_name or "")


@dp.callback_query_handler(lambda c: c.data == "trial")
async def cb_trial(c: types.CallbackQuery):
    await c.answer()
    await _create_and_send_trial(c.message, c.from_user.id, c.from_user.username or "", c.from_user.full_name or "")


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "referral"))
async def text_referral(m: types.Message):
    await show_referral(m, m.from_user.id, m.from_user.username or "")


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "ticket"))
async def text_ticket(m: types.Message):
    await _start_clean_section(m, m.from_user.id, "support")
    await _send_template(
        m,
        m.from_user.id,
        "support_intro",
        "برای ارسال پیام به پشتیبانی، روی دکمه زیر بزنید:",
        reply_markup=section_menu_kb(
            "support", m.from_user.id,
            leading_buttons=[types.InlineKeyboardButton("🎫 ارسال پیام پشتیبانی", callback_data="ticket_start")],
        ),
        context="support",
        kind="menu",
    )


@dp.message_handler(lambda m: menus.matches_system_button(m.text, "admin"))
async def text_admin(m: types.Message):
    if not admin.is_admin(m.from_user.id):
        return
    await _start_clean_section(m, m.from_user.id, "admin")
    await _send_answer(
        m,
        m.from_user.id,
        "⚙️ پنل مدیریت Berserk VPN",
        reply_markup=admin.admin_menu_kb(),
        context="admin",
        kind="menu",
    )


@dp.message_handler(commands=["cancel"], state="*")
async def cmd_cancel(m: types.Message, state: FSMContext):
    current = await state.get_state()
    if current is not None:
        await state.finish()
        text = "❌ لغو شد."
    else:
        text = "چیزی برای لغو کردن نیست."
    await _start_clean_section(m, m.from_user.id, "cancelled")
    return await _send_answer(
        m,
        m.from_user.id,
        text,
        reply_markup=menus.main_reply_kb(m.from_user.id),
        context="cancelled",
        kind="menu",
    )


@dp.callback_query_handler(lambda c: c.data == "cancel_fsm", state="*")
async def cb_cancel_fsm(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    current = await state.get_state()

    if current is not None:
        await state.finish()

    await _cleanup_user_messages(c.message.chat.id, c.from_user.id)
    await _safe_delete_callback_message(c)

    sent = await bot.send_message(
        c.message.chat.id,
        "❌ لغو شد.",
        reply_markup=menus.main_reply_kb(c.from_user.id),
    )
    await _track_sent(c.from_user.id, sent, "cancelled", kind="menu")


@dp.callback_query_handler(lambda c: c.data == "back_main")
async def back_main(c: types.CallbackQuery):
    await c.answer()
    await _cleanup_user_messages(c.message.chat.id, c.from_user.id)
    await _safe_delete_callback_message(c)
    await _send_content(
        c.message, c.from_user.id, "main_menu",
        reply_markup=menus.main_reply_kb(c.from_user.id), context="main", kind="menu",
    )


@dp.callback_query_handler(lambda c: c.data == "buy")
async def buy(c: types.CallbackQuery):
    await c.answer()
    await render_buy(c.message, c.from_user.id, c.from_user.username or "")


@dp.callback_query_handler(lambda c: c.data.startswith("buy_cat_"))
async def buy_category(c: types.CallbackQuery):
    await c.answer()
    try:
        category_id = int(c.data.split("buy_cat_", 1)[1])
    except (TypeError, ValueError):
        return await render_buy(c.message, c.from_user.id, c.from_user.username or "")
    await render_buy(c.message, c.from_user.id, c.from_user.username or "", category_id=category_id)


@dp.callback_query_handler(lambda c: c.data.startswith("buy_plan_"))
async def buy_plan(c: types.CallbackQuery):
    await c.answer()
    try:
        plan_id = int(c.data.split("buy_plan_", 1)[1])
    except Exception:
        plan_id = None
    await render_buy(c.message, c.from_user.id, c.from_user.username or "", plan_id=plan_id)


@dp.callback_query_handler(lambda c: c.data.startswith("buy_qty_"))
async def buy_qty(c: types.CallbackQuery, state: FSMContext):
    user_id = str(c.from_user.id)
    user = db.get_user(user_id)

    if user and user["banned"]:
        await c.answer()
        return await _send_content(c.message, c.from_user.id, "account_banned", reply_markup=menus.main_reply_kb(c.from_user.id), context="account_banned")

    if not _is_admin_user(c.from_user.id) and not settings.sales_enabled():
        await c.answer()
        return await _send_sales_closed(c.message, c.from_user.id)

    await c.answer()

    try:
        parts = c.data.split("_")
        qty = int(parts[2])
        plan_id = int(parts[3]) if len(parts) > 3 else db.default_plan_id()
    except ValueError:
        return await _send_content(c.message, c.from_user.id, "invalid_purchase", reply_markup=catalog_root_kb(c.from_user.id), context="invalid_purchase")

    if qty < 1 or qty > 4:
        return await _send_content(c.message, c.from_user.id, "invalid_quantity", {"reason": "تعداد باید بین ۱ تا ۴ باشد."}, reply_markup=catalog_root_kb(c.from_user.id), context="invalid_quantity")

    if user is None:
        user, _ = db.get_or_create_user(user_id, c.from_user.username, display_name=c.from_user.full_name)

    plan = db.get_plan(plan_id)
    if not plan or int(plan["is_active"] or 0) != 1:
        return await _send_content(
            c.message, c.from_user.id, "plan_unavailable",
            reply_markup=catalog_root_kb(c.from_user.id), context="plan_unavailable",
        )

    purchase_mode = db.plan_purchase_mode(plan)
    if purchase_mode in {"disabled", "wholesale"}:
        return await _send_content(c.message, c.from_user.id, "invalid_quantity", {"reason": "خرید مستقیم این بسته در حال حاضر فعال نیست."}, category_id=plan["category_id"], plan_id=plan_id, reply_markup=catalog_root_kb(c.from_user.id), context="invalid_quantity")
    if purchase_mode == "direct" and qty != 1:
        return await _send_content(c.message, c.from_user.id, "invalid_quantity", {"reason": "این بسته فقط به‌صورت یک‌عددی قابل خرید است."}, category_id=plan["category_id"], plan_id=plan_id, reply_markup=catalog_root_kb(c.from_user.id), context="invalid_quantity")
    max_per_order = max(1, min(100, int(plan["max_per_order"] or 1)))
    if qty > max_per_order:
        return await _send_content(
            c.message, c.from_user.id, "invalid_quantity",
            {"reason": f"حداکثر تعداد مجاز در هر سفارش {max_per_order} عدد است."},
            category_id=plan["category_id"], plan_id=plan_id,
            reply_markup=catalog_root_kb(c.from_user.id), context="invalid_quantity",
        )
    provider_key = db.plan_provider_key(plan)
    delivery_type = "pool" if provider_key == "pool" else "provider"
    if delivery_type == "pool" and subs.stock_count(plan_id) < qty:
        return await _send_content(
            c.message, c.from_user.id, "insufficient_stock",
            category_id=plan["category_id"], plan_id=plan_id,
            reply_markup=category_plans_kb(plan["category_id"], c.from_user.id), context="insufficient_stock",
        )
    active_provider_key = provider_key
    if provider_key != "pool":
        active_provider_key, unavailable_reason = commerce.resolve_provider_for_plan(plan)
        if not active_provider_key:
            return await c.message.answer(
                unavailable_reason or "این سرویس موقتاً در دسترس نیست.",
                reply_markup=catalog_root_kb(c.from_user.id),
            )
        try:
            provider = subs.get_provider_adapter(active_provider_key)
            configured = provider.configured()
        except Exception:
            configured = False
        if not configured:
            return await _send_content(
                c.message, c.from_user.id, "purchase_config_error",
                category_id=plan["category_id"], plan_id=plan_id,
                reply_markup=category_plans_kb(plan["category_id"], c.from_user.id), context="purchase_config_error",
            )
    if provider_key != "pool" and _plan_provider_settings_missing(plan):
        return await _send_content(
            c.message, c.from_user.id, "purchase_config_error",
            category_id=plan["category_id"], plan_id=plan_id,
            reply_markup=category_plans_kb(plan["category_id"], c.from_user.id), context="purchase_config_error",
        )

    request_key = commerce.purchase_request_key(
        user_id,
        c.message.chat.id,
        c.message.message_id,
        c.data,
    )
    state_data = await state.get_data()
    discount_code = None
    if int(state_data.get("active_discount_plan_id") or 0) == plan_id:
        discount_code = (state_data.get("active_discount_code") or "").strip().upper() or None
    try:
        quote = commerce.quote_purchase(user_id, plan, qty, discount_code)
    except db.PurchaseError as exc:
        return await c.message.answer(
            exc.message,
            reply_markup=plan_action_kb(plan, min(4, max_per_order), c.from_user.id),
        )

    was_first_purchase = int(user["purchased"] or 0) == 0
    price = int(plan["price"])
    subtotal = int(quote["subtotal"])
    discount_amount = int(quote["amount"])
    total = int(quote["total"])
    balance = int(user["balance"] or 0)
    missing = max(0, total - balance)

    if missing > 0:
        topup_id = db.create_topup(
            user_id,
            missing,
            target_quantity=qty,
            target_plan_id=plan_id,
            target_total=total,
            target_unit_price=price,
            request_key=request_key,
        )
        commerce.attach_topup_discount(topup_id, discount_code)
        await state.update_data(topup_id=topup_id)
        await wallet.TopupStates.waiting_receipt.set()
        content.record_funnel(user_id, "payment_started", category_id=plan["category_id"], plan_id=plan_id, session_key=request_key)
        return await _send_content(
            c.message, c.from_user.id, "payment_topup",
            {
                "package_title": plan["title"] or plan["volume_label"] or "بسته سرویس",
                "quantity": qty,
                "subtotal": content.money(subtotal),
                "discount": content.money(discount_amount),
                "total": content.money(total),
                "wallet_balance": content.money(balance),
                "payable": content.money(missing),
                "card_number": settings.card_number(),
                "card_holder": settings.card_holder(),
                "payment_id": topup_id,
            },
            category_id=plan["category_id"], plan_id=plan_id,
            reply_markup=wallet.cancel_kb(), context="targeted_topup", kind="important",
        )

    try:
        if provider_key != "pool":
            result = await subs.provision_provider_purchase(
                user_id,
                qty,
                plan_id,
                price,
                discount_code=discount_code,
                request_key=request_key,
                custom_base_username=state_data.get("custom_service_name"),
            )
        else:
            result = commerce.complete_pool_purchase(
                user_id,
                qty,
                plan_id,
                discount_code=discount_code,
                request_key=request_key,
            )
    except db.PurchaseError as exc:
        if exc.code == "insufficient_balance":
            return await c.message.answer(exc.message, reply_markup=wallet_menu_kb(include_bulk=True))
        return await c.message.answer(exc.message, reply_markup=menus.main_reply_kb(c.from_user.id))

    await state.update_data(active_discount_code=None, active_discount_plan_id=None, custom_service_name=None, active_custom_name_plan_id=None)
    await state.reset_state(with_data=False)

    if result.get("queued"):
        content.record_funnel(user_id, "purchase_queued", category_id=plan["category_id"], plan_id=plan_id, purchase_id=result["purchase_id"], session_key=request_key)
        return await _send_content(
            c.message, c.from_user.id, "order_queued",
            {"order_id": result["purchase_id"], "total": content.money(result.get("amount") or total)},
            reply_markup=menus.main_reply_kb(c.from_user.id), context="purchase_queued", kind="important",
        )

    if result.get("refunded"):
        commerce.claim_purchase_notification(result["purchase_id"], "refund")
        content.record_funnel(user_id, "purchase_refunded", category_id=plan["category_id"], plan_id=plan_id, purchase_id=result["purchase_id"], session_key=request_key)
        return await _send_content(
            c.message, c.from_user.id, "order_refunded",
            {"order_id": result["purchase_id"], "refund_amount": content.money(result.get("amount") or total)},
            reply_markup=menus.main_reply_kb(c.from_user.id), context="purchase_refunded", kind="important",
        )

    if was_first_purchase:
        status, detail = reward_ref(user_id)

        if status == "rewarded":
            try:
                await bot.send_message(
                    int(detail),
                    "💰 یکی از زیرمجموعه‌های شما اولین خرید واقعی خود را انجام داد. پاداش رفرال به کیف پول شما اضافه شد.",
                )
            except Exception:
                logger.warning("could not notify referrer %s", detail)

        elif status == "blocked":
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, detail)
                except Exception as exc:
                    logger.warning("could not send referral-fraud alert to admin %s: %s", admin_id, exc)

    await check_low_stock_alert(plan_id)

    post_purchase_text = (plan["post_purchase_text"] if "post_purchase_text" in plan.keys() else "") or ""
    test_notice = "🧪 این خرید آزمایشی است و در گزارش فروش واقعی محاسبه نمی‌شود." if result.get("is_test") else ""
    commerce.claim_purchase_notification(result["purchase_id"], "delivery")
    content.record_funnel(user_id, "payment_success", category_id=plan["category_id"], plan_id=plan_id, purchase_id=result["purchase_id"], session_key=request_key)
    await _send_content(
        c.message, c.from_user.id, "purchase_success",
        {
            "order_id": result["purchase_id"],
            "plan_title": plan["title"] or "سرویس",
            "quantity": len(result["items"]),
            "subtotal": content.money(result.get("subtotal") or result["amount"]),
            "discount": content.money(result.get("discount_amount") or 0),
            "total": content.money(result["amount"]),
            "balance_after": content.money(result["balance_after"]),
            "test_notice": test_notice,
            "post_purchase_text": post_purchase_text.strip(),
        },
        category_id=plan["category_id"], plan_id=plan_id,
        reply_markup=menus.main_reply_kb(c.from_user.id), context="purchase_result", kind="important",
    )

    for index, item in enumerate(result["items"], start=1):
        expire_date = item.get("panel_expires_at") or item.get("expires_at") or "-"
        provider_public = "تحویل آماده" if db.plan_provider_key(plan) == "pool" else "ساخت خودکار"
        await _send_content(
            c.message, c.from_user.id, "service_delivery",
            {
                "order_id": result["purchase_id"],
                "plan_title": plan["title"] or "سرویس",
                "username": item.get("account_name") or item.get("panel_username") or f"سرویس {index}",
                "volume": plan["volume_label"] or "-",
                "duration": plan["duration_label"] or "-",
                "devices": "نامحدود" if plan["panel_max_devices"] in (None, "", 0) else f"{plan['panel_max_devices']} دستگاه",
                "expire_date": expire_date,
                "subscription_url": item["link"],
                "provider_public_name": provider_public,
                "created_at": item.get("assigned_at") or "-",
            },
            category_id=plan["category_id"], plan_id=plan_id,
            context="purchase_link", kind="delivery",
        )
        qr_path = make_qr(item["link"], user_id)
        try:
            qr_caption = content.render(
                "service_qr", {"username": item.get("account_name") or f"سرویس {index}"},
                category_id=plan["category_id"], plan_id=plan_id,
            )
            with open(qr_path, "rb") as f:
                sent_photo = await c.message.answer_photo(
                    f, caption=qr_caption["text"], parse_mode=qr_caption["parse_mode_api"],
                )
                await _track_sent(c.from_user.id, sent_photo, "purchase_qr", kind="delivery")
        finally:
            cleanup_qr(qr_path)

    content.record_funnel(user_id, "purchase_delivered", category_id=plan["category_id"], plan_id=plan_id, purchase_id=result["purchase_id"], session_key=request_key)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛒 خرید جدید\n"
                f"کاربر: {c.from_user.full_name} (@{c.from_user.username or '-'})\n"
                f"ID: {user_id}\n"
                f"شماره خرید: #{result['purchase_id']}\n"
                f"پلن: {plan['title']}\n"
                f"تعداد: {len(result['items'])}\n"
                f"مبلغ: {result['amount']:,} تومان"
                + ("\n🧪 خرید کاربر تست" if result.get("is_test") else ""),
            )
        except Exception as exc:
            logger.warning("could not send purchase notification to admin %s: %s", admin_id, exc)

@dp.callback_query_handler(lambda c: c.data == "confirm_buy")
async def confirm_buy(c: types.CallbackQuery):
    await c.answer()
    await render_buy(c.message, c.from_user.id, c.from_user.username or "", plan_id=db.default_plan_id())


@dp.callback_query_handler(lambda c: c.data == "buy_bulk")
async def buy_bulk(c: types.CallbackQuery):
    await c.answer()
    if not _is_admin_user(c.from_user.id) and not settings.sales_enabled():
        return await _send_sales_closed(c.message, c.from_user.id)
    user_id = str(c.from_user.id)
    db.touch_active(user_id, c.from_user.username)
    ticket_id = db.create_ticket(user_id)

    text = (
        f"📦 درخواست خرید عمده #{ticket_id}\n\n"
        f"کاربر: {c.from_user.full_name}\n"
        f"یوزرنیم: @{c.from_user.username or '-'}\n"
        f"User ID: {user_id}\n\n"
        "لطفاً با کاربر صحبت کنید و تعداد/شرایط خرید عمده را دستی هماهنگ کنید."
    )

    for admin_id in ADMIN_IDS:
        try:
            sent = await bot.send_message(admin_id, text)
            db.record_ticket_message(admin_id, sent.message_id, ticket_id, user_id)
        except Exception as exc:
            logger.warning("could not create bulk-purchase admin ticket message for %s: %s", admin_id, exc)

    await _send_content(
        c.message, c.from_user.id, "bulk_request_sent", {"ticket_id": ticket_id},
        reply_markup=menus.main_reply_kb(c.from_user.id), context="bulk_request",
    )


async def show_my_subs(target, user_id: int, username: str = ""):
    await _start_clean_section(target, user_id, "my_subs")
    user_id_str = str(user_id)
    db.touch_active(user_id_str, username, getattr(target.from_user, "full_name", None) if hasattr(target, "from_user") else None)
    rows = subs.user_subs(user_id_str)

    if not rows:
        return await _send_content(
            target, user_id, "services_empty",
            reply_markup=section_menu_kb("my_services", user_id), context="my_subs",
        )

    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        plan = db.get_plan(r["plan_id"]) if "plan_id" in r.keys() and r["plan_id"] else None
        plan_title = plan["title"] if plan else "سرویس"
        trial_label = "🧪 تست" if "is_trial" in r.keys() and int(r["is_trial"] or 0) else ""
        label = content.render_button(
            "service_button",
            {
                "plan_title": plan_title,
                "username": r["account_name"] or "-",
                "status": r["panel_status"] or r["status"] or "",
                "trial_label": trial_label,
            },
            category_id=plan["category_id"] if plan else None,
            plan_id=plan["id"] if plan else None,
        )
        kb.add(types.InlineKeyboardButton(label, callback_data=f"service_detail_{r['id']}"))
    menus.append_location_buttons(kb, "my_services", user_id)
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    await _send_content(target, user_id, "services_list", reply_markup=kb, context="my_subs")

@dp.callback_query_handler(lambda c: c.data == "my_subs")
async def my_subs(c: types.CallbackQuery):
    await c.answer()
    await show_my_subs(c.message, c.from_user.id, c.from_user.username or "")




def guide_menu_kb(user_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📱 آموزش اندروید", callback_data="guide_android"))
    kb.add(types.InlineKeyboardButton("🍎 آموزش آیفون", callback_data="guide_ios"))
    kb.add(types.InlineKeyboardButton("💻 آموزش ویندوز", callback_data="guide_windows"))
    kb.add(types.InlineKeyboardButton("🖥 آموزش مک", callback_data="guide_mac"))
    kb.add(types.InlineKeyboardButton("❓ مشکل اتصال دارم", callback_data="guide_troubleshoot"))
    kb.add(types.InlineKeyboardButton("🔄 بروزرسانی ساب‌لینک", callback_data="guide_update"))

    menus.append_location_buttons(kb, "guide", user_id)

    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
    return kb

_GUIDE_DEFAULTS = {
    "guide_home": "📚 آموزش اتصال\n\nدستگاه خود را انتخاب کنید:",
    "guide_android": "📱 آموزش اندروید\n\n۱. یک برنامه سازگار با ساب‌لینک نصب کنید.\n۲. لینک سرویس را از بخش سرویس‌های من کپی کنید.\n۳. داخل برنامه گزینه Import/Subscription را بزنید.\n۴. لینک را وارد و بروزرسانی کنید.",
    "guide_ios": "🍎 آموزش آیفون\n\n۱. یک کلاینت سازگار نصب کنید.\n۲. ساب‌لینک را کپی کنید.\n۳. از بخش Subscription یا Import، لینک را اضافه کنید.\n۴. اتصال را تست کنید.",
    "guide_windows": "💻 آموزش ویندوز\n\n۱. برنامه مناسب ویندوز را نصب کنید.\n۲. لینک سرویس را از بخش سرویس‌های من کپی کنید.\n۳. از بخش Subscription لینک را اضافه کنید.\n۴. Update subscription را بزنید.",
    "guide_mac": "🖥 آموزش مک\n\n۱. کلاینت سازگار با مک را نصب کنید.\n۲. لینک سرویس را اضافه کنید.\n۳. ساب‌لینک را بروزرسانی و اتصال را فعال کنید.",
    "guide_troubleshoot": "❓ مشکل اتصال دارم\n\nاول اینترنت اصلی را بررسی کنید، سپس ساب‌لینک را بروزرسانی کنید. اگر مشکل ادامه داشت، از بخش پشتیبانی پیام بدهید.",
    "guide_update": "🔄 بروزرسانی ساب‌لینک\n\nدر برنامه خود گزینه Update/Refresh Subscription را بزنید تا لیست سرورها تازه شود.",
}


async def show_guide_menu(target, user_id: int, username: str = ""):
    db.touch_active(str(user_id), username)
    await _start_clean_section(target, user_id, "guide")
    await _send_template(
        target,
        user_id,
        "guide_home",
        _GUIDE_DEFAULTS["guide_home"],
        reply_markup=guide_menu_kb(user_id),
        context="guide",
    )


async def show_guide_page(target, key: str, user_id: int):
    await _start_clean_section(target, user_id, key)
    await _send_template(
        target,
        user_id,
        key,
        _GUIDE_DEFAULTS.get(key, "📚 آموزش اتصال"),
        reply_markup=guide_menu_kb(user_id),
        context="guide_page",
    )


@dp.callback_query_handler(lambda c: c.data == "guide_home")
async def cb_guide_home(c: types.CallbackQuery):
    await c.answer()
    await show_guide_menu(c.message, c.from_user.id, c.from_user.username or "")


@dp.callback_query_handler(lambda c: c.data.startswith("guide_") and c.data != "guide_home")
async def cb_guide_page(c: types.CallbackQuery):
    await c.answer()
    key = c.data
    await show_guide_page(c.message, key, c.from_user.id)


def _custom_button_allowed(row, user_id):
    return _audience_allowed(row["audience"] or "all", user_id)


async def render_custom_button(target, row, user_id: int, username: str = ""):
    await _start_clean_section(target, user_id, "custom_button")
    db.touch_active(str(user_id), username)
    if not _custom_button_allowed(row, user_id):
        return await _send_answer(
            target, user_id, "این دکمه برای حساب شما فعال نیست.",
            reply_markup=menus.main_reply_kb(user_id), context="custom_button", kind="menu"
        )

    button_type = row["button_type"] or "text"
    payload = row["payload"] or ""
    title = row["title"] or "دکمه اختصاصی"

    if button_type == "link":
        if not payload.startswith(("http://", "https://", "tg://")):
            return await _send_answer(
                target, user_id, "لینک این دکمه معتبر نیست. لطفاً به پشتیبانی اطلاع دهید.",
                reply_markup=menus.main_reply_kb(user_id), context="custom_button", kind="menu"
            )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(title, url=payload))
        kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="back_main"))
        return await _send_answer(
            target, user_id, f"برای باز کردن «{title}» روی دکمه زیر بزنید:",
            reply_markup=kb, context="custom_button", kind="menu"
        )

    if button_type == "support":
        return await _send_template(
            target,
            user_id,
            "support_intro",
            payload or "برای ارسال پیام به پشتیبانی، روی دکمه زیر بزنید:",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🎫 ارسال پیام پشتیبانی", callback_data="ticket_start")
            ),
            context="custom_button_support",
            kind="menu",
        )

    if button_type == "buy_plan":
        plan_id = int(payload) if str(payload).strip().isdigit() else None
        return await render_buy(target, user_id, username, plan_id=plan_id)

    if button_type == "file":
        if payload:
            try:
                sent = await target.answer_document(
                    payload, caption=title, reply_markup=menus.main_reply_kb(user_id)
                )
                await _track_sent(user_id, sent, "custom_button_file", kind="important")
                return sent
            except Exception:
                logger.warning("Custom button file_id is unavailable", exc_info=True)
        return await _send_answer(
            target, user_id, "فایل این دکمه در دسترس نیست.",
            reply_markup=menus.main_reply_kb(user_id), context="custom_button", kind="menu"
        )

    # text / faq / guide / submenu are rendered as safe text in the bot UI.
    return await _send_answer(
        target, user_id, payload or title,
        reply_markup=menus.main_reply_kb(user_id), context="custom_button", kind="menu"
    )


@dp.callback_query_handler(lambda c: c.data.startswith("custom_btn_"))
async def callback_custom_button(c: types.CallbackQuery):
    await c.answer()
    try:
        button_id = int(c.data.split("custom_btn_", 1)[1])
    except (TypeError, ValueError):
        return
    row = db.get_custom_button(button_id)
    if row and int(row["is_active"] or 0):
        await render_custom_button(c.message, row, c.from_user.id, c.from_user.username or "")


@dp.message_handler(lambda m: db.get_active_custom_button_by_title(m.text or "") is not None)
async def text_custom_button(m: types.Message):
    row = db.get_active_custom_button_by_title(m.text or "")
    await render_custom_button(m, row, m.from_user.id, m.from_user.username or "")

async def show_wallet(target, user_id: int, username: str = ""):
    await _start_clean_section(target, user_id, "wallet")
    user_id_str = str(user_id)
    db.touch_active(user_id_str, username, getattr(target.from_user, "full_name", None) if hasattr(target, "from_user") else None)
    user = db.get_user(user_id_str)

    if user is None:
        user, _ = db.get_or_create_user(user_id_str, username)

    bal = user["balance"] if user else 0
    purchased = user["purchased"] if user else 0

    await _send_template(
        target,
        user_id,
        "menu_wallet",
        f"💳 موجودی: {bal:,} تومان\n📦 تعداد خرید: {purchased}",
        reply_markup=wallet_menu_kb(user_id=user_id),
        context="wallet",
        kind="menu",
    )


@dp.callback_query_handler(lambda c: c.data == "wallet")
async def wallet_menu(c: types.CallbackQuery):
    await c.answer()
    await show_wallet(c.message, c.from_user.id, c.from_user.username or "")


async def show_referral(target, user_id: int, username: str = ""):
    await _start_clean_section(target, user_id, "referral")
    user_id_str = str(user_id)
    bot_user = (await bot.get_me()).username
    link = f"https://t.me/{bot_user}?start={user_id_str}"
    count = db.referral_count(user_id_str)
    reward = settings.ref_reward()

    await _send_template(
        target,
        user_id,
        "menu_referral",
        f"👥 لینک دعوت اختصاصی شما:\n{link}\n\n"
        f"تعداد زیرمجموعه: {count} نفر\n"
        f"پاداش هر اولین خرید واقعی زیرمجموعه: {reward:,} تومان\n\n"
        "پاداش فقط بعد از اولین خرید واقعی زیرمجموعه پرداخت می‌شود.",
        reply_markup=menus.main_reply_kb(user_id),
        context="referral",
        kind="menu",
    )


@dp.callback_query_handler(lambda c: c.data == "referral")
async def referral(c: types.CallbackQuery):
    await c.answer()
    await show_referral(c.message, c.from_user.id, c.from_user.username or "")


wallet.register(dp)
tickets.register(dp)
force_join.register(dp)
v64_handlers.register(dp)
v63_handlers.register(dp)
admin.register(dp)


@dp.errors_handler()
async def global_error_handler(update: types.Update, exception: Exception):
    logger.exception("خطای پیش‌بینی‌نشده: %s", exception)

    try:
        if update.message:
            await content.send(update.message, "generic_error", reply_markup=menus.main_reply_kb(update.message.from_user.id))
        elif update.callback_query:
            await content.send(update.callback_query.message, "generic_error", reply_markup=menus.main_reply_kb(update.callback_query.from_user.id))
    except Exception as notify_exc:
        logger.debug("could not notify user about handler error: %s", notify_exc)

    tb = traceback.format_exc()[-1500:]
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🚨 خطای پیش‌بینی‌نشده:\n{tb}")
        except Exception as admin_notify_exc:
            logger.error("could not deliver error report to admin %s: %s", admin_id, admin_notify_exc)
    return True


@dp.message_handler(content_types=types.ContentTypes.ANY, state="*")
async def fallback_message(m: types.Message, state: FSMContext):
    await _start_clean_section(m, m.from_user.id, "fallback")
    await _send_content(
        m, m.from_user.id, "fallback",
        reply_markup=menus.main_reply_kb(m.from_user.id), context="fallback", kind="menu",
    )


@dp.callback_query_handler(lambda c: True, state="*")
async def fallback_callback(c: types.CallbackQuery):
    text = content.render("stale_action")["text"]
    await c.answer(text[:190], show_alert=True)


async def on_startup(dispatcher):
    if not db.database_health():
        raise RuntimeError("SQLite quick_check failed during startup")
    try:
        recovered = await subs.recover_stale_provider_purchases(15)
        recovered_trials = await subs.recover_stale_trial_claims(15)
        if recovered:
            logger.warning("processed stale provider purchases: %s", recovered)
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, f"⚠️ سفارش‌های نیمه‌کاره بدون ساخت تکراری بررسی شدند: {recovered}")
                except Exception:
                    logger.debug("could not notify admin about provider recovery", exc_info=True)
        if recovered_trials:
            logger.warning("recovered stale trial claims: %s", recovered_trials)
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, f"⚠️ درخواست‌های تست نیمه‌کاره آزاد شدند: {recovered_trials}")
                except Exception:
                    logger.debug("could not notify admin about trial recovery", exc_info=True)
    except Exception:
        logger.exception("stale provider purchase recovery failed")
    asyncio.create_task(backup.daily_backup_loop(bot, ADMIN_IDS), name="daily-backup")
    asyncio.create_task(pasarguard_backup.pasarguard_backup_loop(bot, ADMIN_IDS), name="pasarguard-backup")
    asyncio.create_task(expiry_alerts.expiry_alert_loop(bot), name="expiry-alerts")
    asyncio.create_task(v63_handlers.provider_queue_loop(bot), name="provider-order-queue")
    asyncio.create_task(v63_handlers.campaign_loop(bot), name="sales-campaigns")
    logger.info("Berserk VPN v6.4 started; database health OK; content, backup, provider queue and campaigns scheduled.")


if __name__ == "__main__":
    logger.info("Berserk VPN bot starting...")
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        timeout=30,
        relax=1.0,
        fast=False,
    )
