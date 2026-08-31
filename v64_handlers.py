"""v6.4 unified content center and purchase-experience administration.

The content center replaces the two older entry points (messages and plan
text templates) without duplicating customer callbacks or business actions.
"""
from __future__ import annotations

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

import content
import db
import menus
from config import ADMIN_IDS


class ContentStates(StatesGroup):
    waiting_text = State()
    waiting_photo = State()


_SCOPE_CHAR = {"g": content.SCOPE_GLOBAL, "c": content.SCOPE_CATEGORY, "p": content.SCOPE_PLAN}
_SCOPE_NAME = {content.SCOPE_GLOBAL: "عمومی", content.SCOPE_CATEGORY: "دسته", content.SCOPE_PLAN: "پلن"}
DISPLAY_LABELS = {
    "show_wallet_balance": "نمایش موجودی کیف پول",
    "show_devices": "نمایش تعداد دستگاه",
    "show_delivery": "نمایش روش تحویل",
    "show_start_mode": "نمایش شروع اعتبار",
    "show_stock_status": "نمایش موجود/ناموجود",
    "show_provider_public": "نمایش عنوان عمومی تأمین‌کننده",
    "show_discount_button": "نمایش دکمه تخفیف",
    "show_numeric_stock": "نمایش عدد واقعی موجودی",
    "show_category_plan_count": "نمایش تعداد بسته‌های دسته",
}
FUNNEL_LABELS = {
    "buy_open": "ورود به خرید",
    "category_view": "مشاهده دسته",
    "plan_checkout": "ورود به تأیید خرید",
    "payment_started": "شروع پرداخت",
    "payment_success": "پرداخت موفق",
    "purchase_delivered": "تحویل سرویس",
}

LEGACY_CONTENT_PREFIXES = (
    "msgcat_", "msgkey_", "msg_default_", "msg_copy_default_", "msg_edit_",
    "msg_prefix_", "msg_suffix_", "msg_preview_", "msg_publish_",
    "msg_clear_draft_", "msg_clear_published_", "v63_tpl_",
)


def _admin(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False


def _idx_from_key(key: str) -> int:
    for idx, item in enumerate(content.DEFINITIONS):
        if item.key == key:
            return idx
    raise ValueError("slot not found")


def _item(idx: int):
    if idx < 0 or idx >= len(content.DEFINITIONS):
        raise ValueError("slot not found")
    return content.DEFINITIONS[idx]


def _scope(char: str, scope_id: int) -> tuple[str, int]:
    scope_type = _SCOPE_CHAR.get(char)
    if not scope_type:
        raise ValueError("scope invalid")
    return scope_type, 0 if scope_type == content.SCOPE_GLOBAL else int(scope_id)


def _scope_char(scope_type: str) -> str:
    return {content.SCOPE_GLOBAL: "g", content.SCOPE_CATEGORY: "c", content.SCOPE_PLAN: "p"}[scope_type]


async def _replace(c: types.CallbackQuery, text: str, reply_markup=None, parse_mode=None):
    try:
        await c.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await c.message.delete()
        except Exception:
            pass
        await c.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


def _home_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🎨 قالب‌های آماده پلن", callback_data="ct_pack_home"))
    for key, label in content.categories():
        kb.add(types.InlineKeyboardButton(label, callback_data=f"ct_cat_{key}"))
    kb.add(types.InlineKeyboardButton("⚙️ تنظیمات نمایش اطلاعات", callback_data="ct_display"))
    kb.add(types.InlineKeyboardButton("📈 گزارش قیف خرید", callback_data="ct_funnel"))
    kb.add(types.InlineKeyboardButton("⬅️ شخصی‌سازی ربات", callback_data="adm_section_personalize"))
    return kb


def _category_kb(category: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for item in content.definitions_by_category(category):
        idx = _idx_from_key(item.key)
        icon = "🔘" if item.kind == "button" else "📝"
        kb.add(types.InlineKeyboardButton(f"{icon} {item.title}", callback_data=f"ct_d_{idx}_g_0"))
    kb.add(types.InlineKeyboardButton("⬅️ مرکز محتوا", callback_data="adm_content"))
    return kb


def _scope_label(scope_type: str, scope_id: int) -> str:
    if scope_type == content.SCOPE_GLOBAL:
        return "عمومی فروشگاه"
    if scope_type == content.SCOPE_CATEGORY:
        row = db.get_plan_category(scope_id)
        return f"دسته: {row['title'] if row else scope_id}"
    row = db.get_plan(scope_id)
    return f"پلن: {row['title'] if row else scope_id}"


def _status_text(item, row, scope_type, scope_id):
    published = row["published_text"] if row else None
    draft = row["draft_text"] if row else None
    parse_mode = (row["draft_parse_mode"] or row["parse_mode"]) if row else item.default_parse_mode
    media = bool(row and (row["draft_photo_file_id"] or row["published_photo_file_id"]))
    active = bool(row and int(row["is_active"] or 0))
    if published and active:
        source = "سفارشی فعال"
    elif published and not active:
        source = "سفارشی غیرفعال؛ فعلاً ارث‌بری/پیش‌فرض"
    else:
        source = "ارث‌بری" if scope_type != content.SCOPE_GLOBAL else "پیش‌فرض سورس"
    return (
        f"📝 {item.title}\n\n"
        f"📍 دامنه: {_scope_label(scope_type, scope_id)}\n"
        f"📚 منبع فعال: {source}\n"
        f"🧪 Draft: {'دارد' if draft is not None or (row and row['draft_photo_file_id'] is not None) else 'ندارد'}\n"
        f"🎨 قالب‌بندی: {parse_mode or 'plain'}\n"
        f"🖼 رسانه: {'دارد' if media else 'ندارد'}\n"
        f"🔢 متغیرهای مجاز: {', '.join('{' + f + '}' for f in item.fields) if item.fields else 'ندارد'}\n\n"
        "تغییرات ابتدا Draft هستند؛ بعد از پیش‌نمایش، آن‌ها را منتشر کنید."
    )


def _detail_kb(idx: int, scope_type: str, scope_id: int):
    item = _item(idx)
    ch = _scope_char(scope_type)
    row = content.get_template(item.key, scope_type, scope_id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✏️ ویرایش Draft", callback_data=f"ct_e_{idx}_{ch}_{scope_id}"))
    if item.allow_media:
        kb.add(types.InlineKeyboardButton("🖼 تنظیم عکس Draft", callback_data=f"ct_ph_{idx}_{ch}_{scope_id}"))
    if item.kind != "button":
        kb.add(types.InlineKeyboardButton("🎨 نوع قالب‌بندی", callback_data=f"ct_pm_{idx}_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("👁 پیش‌نمایش واقعی", callback_data=f"ct_pr_{idx}_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("✅ انتشار Draft", callback_data=f"ct_pub_{idx}_{ch}_{scope_id}"))
    if row and any(row[name] not in (None, "") for name in ("published_text", "published_photo_file_id")):
        active_label = "⏸ غیرفعال‌کردن این قالب" if int(row["is_active"] or 0) else "▶️ فعال‌کردن این قالب"
        kb.add(types.InlineKeyboardButton(active_label, callback_data=f"ct_ac_{idx}_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("🧹 حذف Draft", callback_data=f"ct_cd_{idx}_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("🕘 نسخه‌های قبلی", callback_data=f"ct_hi_{idx}_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("♻️ بازگردانی پیش‌فرض/ارث‌بری", callback_data=f"ct_rs_{idx}_{ch}_{scope_id}"))
    if len(item.scopes) > 1:
        kb.add(types.InlineKeyboardButton("🎯 انتخاب دامنه دیگر", callback_data=f"ct_sc_{idx}"))
    kb.add(types.InlineKeyboardButton("⬅️ دسته محتوا", callback_data=f"ct_cat_{item.category}"))
    return kb


def _scope_select_kb(idx: int):
    item = _item(idx)
    kb = types.InlineKeyboardMarkup(row_width=1)
    if content.SCOPE_GLOBAL in item.scopes:
        kb.add(types.InlineKeyboardButton("🌐 عمومی فروشگاه", callback_data=f"ct_d_{idx}_g_0"))
    if content.SCOPE_CATEGORY in item.scopes:
        kb.add(types.InlineKeyboardButton("🗂 انتخاب دسته", callback_data=f"ct_scat_{idx}"))
    if content.SCOPE_PLAN in item.scopes:
        kb.add(types.InlineKeyboardButton("🏷 انتخاب پلن", callback_data=f"ct_splan_{idx}"))
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت", callback_data=f"ct_d_{idx}_g_0"))
    return kb


def _category_select_kb(idx: int, *, prefix="ct_d"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in db.list_plan_categories(active_only=False, include_empty=True, limit=50):
        callback = f"{prefix}_{idx}_c_{row['id']}" if prefix == "ct_d" else f"{prefix}_c_{row['id']}"
        kb.add(types.InlineKeyboardButton(f"{row['emoji'] or '📦'} {row['title']}", callback_data=callback))
    kb.add(types.InlineKeyboardButton("⬅️ انتخاب دامنه", callback_data=f"ct_sc_{idx}"))
    return kb


def _plan_select_kb(idx: int, *, prefix="ct_d"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in db.list_plans(active_only=False, limit=50):
        callback = f"{prefix}_{idx}_p_{row['id']}" if prefix == "ct_d" else f"{prefix}_p_{row['id']}"
        kb.add(types.InlineKeyboardButton(f"🏷 {row['title']}", callback_data=callback))
    kb.add(types.InlineKeyboardButton("⬅️ انتخاب دامنه", callback_data=f"ct_sc_{idx}"))
    return kb


async def cb_content_home(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    await c.answer()
    await _replace(
        c,
        "🧠 مرکز محتوا و تجربه مشتری\n\n"
        "تمام متن‌های مهم، عکس‌ها، متن دکمه‌ها و نمایش اطلاعات از این بخش مدیریت می‌شوند.\n\n"
        "✅ ویرایش Draft-first\n✅ پیش‌نمایش با پلن واقعی\n✅ انتشار کنترل‌شده\n✅ تاریخچه و بازگردانی\n✅ ارث‌بری پلن ← دسته ← عمومی\n\n"
        "منطق خرید، پرداخت و Callbackها از متن جداست؛ بنابراین تغییر نوشته‌ها اکشن تکراری نمی‌سازد.",
        _home_kb(),
    )


def _pack_list_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, title, _description in content.list_plan_template_packs():
        kb.add(types.InlineKeyboardButton(title, callback_data=f"ct_pack_show_{key}"))
    kb.add(types.InlineKeyboardButton("⬅️ مرکز محتوا", callback_data="adm_content"))
    return kb


def _pack_detail_kb(pack_key: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("👁 پیش‌نمایش کامل", callback_data=f"ct_pack_preview_{pack_key}"))
    kb.add(types.InlineKeyboardButton("🌐 اعمال عمومی به‌صورت Draft", callback_data=f"ct_pack_do_{pack_key}_g_0"))
    kb.add(types.InlineKeyboardButton("🗂 اعمال Draft روی یک دسته", callback_data=f"ct_pack_cats_{pack_key}"))
    kb.add(types.InlineKeyboardButton("🏷 اعمال Draft روی یک پلن", callback_data=f"ct_pack_plans_{pack_key}"))
    kb.add(types.InlineKeyboardButton("⬅️ قالب‌های آماده", callback_data="ct_pack_home"))
    return kb


def _pack_scope_rows(pack_key: str, scope_type: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if scope_type == content.SCOPE_CATEGORY:
        for row in db.list_plan_categories(active_only=False, include_empty=True, limit=50):
            kb.add(types.InlineKeyboardButton(f"{row['emoji'] or '📦'} {row['title']}", callback_data=f"ct_pack_do_{pack_key}_c_{row['id']}"))
    else:
        for row in db.list_plans(active_only=False, limit=50):
            kb.add(types.InlineKeyboardButton(f"🏷 {row['title']}", callback_data=f"ct_pack_do_{pack_key}_p_{row['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت به قالب", callback_data=f"ct_pack_show_{pack_key}"))
    return kb


def _pack_result_kb(pack_key: str, scope_type: str, scope_id: int, slot_keys: list[str]):
    ch = _scope_char(scope_type)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for slot_key in slot_keys:
        idx = _idx_from_key(slot_key)
        kb.add(types.InlineKeyboardButton(f"✏️ بررسی و ویرایش: {_item(idx).title}", callback_data=f"ct_d_{idx}_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("👁 پیش‌نمایش قالب آماده", callback_data=f"ct_pack_preview_{pack_key}"))
    kb.add(types.InlineKeyboardButton("⬅️ قالب‌های آماده", callback_data="ct_pack_home"))
    return kb


async def cb_pack_home(c: types.CallbackQuery):
    await c.answer()
    await _replace(c, "🎨 قالب‌های آماده پلن\n\nیک سبک آماده را انتخاب کنید تا متن دکمه بسته، صفحه تأیید خرید، پیام خرید موفق و تحویل سرویس با یک لحن هماهنگ ساخته شوند.\n\n🔒 اعمال قالب فقط Draft می‌سازد؛ متن فعلی مشتری تغییر نمی‌کند تا هر بخش را بررسی و منتشر کنید.", _pack_list_kb())


async def cb_pack_show(c: types.CallbackQuery):
    pack_key = c.data.split("ct_pack_show_", 1)[1]
    try:
        pack = content.get_plan_template_pack(pack_key)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    await c.answer()
    await _replace(c, f"{pack['title']}\n\n{pack['description']}\n\nاین مجموعه چهار بخش واقعی مسیر مشتری را آماده می‌کند:\n• متن دکمه بسته\n• صفحه تأیید خرید\n• پیام خرید موفق\n• پیام تحویل سرویس\n\nپس از اعمال، هر چهار مورد به‌صورت Draft قابل ویرایش، پیش‌نمایش، انتشار و بازگردانی هستند.", _pack_detail_kb(pack_key))


async def cb_pack_preview(c: types.CallbackQuery):
    pack_key = c.data.split("ct_pack_preview_", 1)[1]
    try:
        pack = content.get_plan_template_pack(pack_key)
        rows = content.preview_plan_template_pack(pack_key)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    await c.answer()
    lines = [f"👁 پیش‌نمایش {pack['title']}", ""]
    for index, row in enumerate(rows, start=1):
        lines.extend([f"{index}️⃣ {row['title']}", row["text"], "────────────"])
    await c.message.answer("\n".join(lines).rstrip("─\n ")[:4096])


async def cb_pack_categories(c: types.CallbackQuery):
    pack_key = c.data.split("ct_pack_cats_", 1)[1]
    try:
        content.get_plan_template_pack(pack_key)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    await c.answer()
    await _replace(c, "🗂 دسته‌ای را انتخاب کنید. چهار متن به‌صورت Draft روی همان دسته ذخیره می‌شوند:", _pack_scope_rows(pack_key, content.SCOPE_CATEGORY))


async def cb_pack_plans(c: types.CallbackQuery):
    pack_key = c.data.split("ct_pack_plans_", 1)[1]
    try:
        content.get_plan_template_pack(pack_key)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    await c.answer()
    await _replace(c, "🏷 پلنی را انتخاب کنید. چهار متن به‌صورت Draft فقط روی همان پلن ذخیره می‌شوند:", _pack_scope_rows(pack_key, content.SCOPE_PLAN))


async def cb_pack_apply(c: types.CallbackQuery):
    try:
        payload = c.data.split("ct_pack_do_", 1)[1]
        pack_key, ch, scope_id_text = payload.rsplit("_", 2)
        scope_type, scope_id = _scope(ch, int(scope_id_text))
        pack = content.get_plan_template_pack(pack_key)
        slot_keys = content.apply_plan_template_pack(pack_key, scope_type, scope_id, admin_id=c.from_user.id)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    db.log_admin_action(c.from_user.id, "content_apply_plan_pack", details=f"pack={pack_key};scope={scope_type}:{scope_id};slots={','.join(slot_keys)}")
    await c.answer("قالب به‌صورت Draft ذخیره شد")
    await _replace(c, f"✅ {pack['title']} به‌صورت Draft اعمال شد\n\n📍 دامنه: {_scope_label(scope_type, scope_id)}\nمتن منتشرشده مشتری هنوز تغییر نکرده است. هر بخش را از دکمه‌های زیر بررسی، ویرایش و سپس منتشر کنید.\n\nبرای لغو کامل، وارد هر بخش شوید و «حذف Draft» را بزنید؛ برای بازگشت متن منتشرشده نیز تاریخچه و بازگردانی در دسترس است.", _pack_result_kb(pack_key, scope_type, scope_id, slot_keys))


async def cb_content_category(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    category = c.data.split("ct_cat_", 1)[1]
    if category not in content.CATEGORIES:
        return await c.answer("دسته نامعتبر", show_alert=True)
    await c.answer()
    await _replace(c, content.CATEGORIES[category], _category_kb(category))


async def cb_content_detail(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    try:
        _, _, idx_s, ch, sid_s = c.data.split("_", 4)
        idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s))
        item = _item(idx)
        if scope_type not in item.scopes:
            raise ValueError
    except Exception:
        return await c.answer("درخواست نامعتبر", show_alert=True)
    await c.answer()
    row = content.get_template(item.key, scope_type, scope_id)
    await _replace(c, _status_text(item, row, scope_type, scope_id), _detail_kb(idx, scope_type, scope_id))


async def cb_scope_select(c: types.CallbackQuery):
    if not _admin(c.from_user.id):
        return await c.answer()
    idx = int(c.data.rsplit("_", 1)[1])
    await c.answer()
    await _replace(c, f"🎯 دامنه «{_item(idx).title}» را انتخاب کنید:", _scope_select_kb(idx))


async def cb_scope_category(c: types.CallbackQuery):
    idx = int(c.data.rsplit("_", 1)[1]); await c.answer()
    await _replace(c, "🗂 دسته موردنظر را انتخاب کنید:", _category_select_kb(idx))


async def cb_scope_plan(c: types.CallbackQuery):
    idx = int(c.data.rsplit("_", 1)[1]); await c.answer()
    await _replace(c, "🏷 پلن موردنظر را انتخاب کنید:", _plan_select_kb(idx))


async def cb_edit(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id):
        return await c.answer()
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    row = content.get_template(item.key, scope_type, scope_id)
    current = (row["draft_text"] if row and row["draft_text"] is not None else None) or (row["published_text"] if row else None) or item.default_text
    await state.update_data(content_idx=idx, content_scope=scope_type, content_scope_id=scope_id)
    await ContentStates.waiting_text.set()
    await c.answer()
    helper = " ".join("{" + field + "}" for field in item.fields) or "بدون متغیر"
    await c.message.answer(
        f"✏️ متن جدید «{item.title}» را بفرستید.\n\n"
        f"متغیرهای مجاز:\n{helper}\n\n"
        f"متن فعلی:\n{current[:1800]}",
        reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ لغو و خروج از ویرایش", callback_data=f"ct_cancel_{idx}_{ch}_{scope_id}")),
    )


async def process_text(m: types.Message, state: FSMContext):
    if not _admin(m.from_user.id):
        return
    data = await state.get_data(); idx = int(data.get("content_idx") or -1); item = _item(idx)
    scope_type = data.get("content_scope") or content.SCOPE_GLOBAL; scope_id = int(data.get("content_scope_id") or 0)
    if not m.text:
        return await m.answer("لطفاً متن ارسال کنید؛ فایل یا استیکر قابل ذخیره نیست.")
    row = content.get_template(item.key, scope_type, scope_id)
    parse_mode = (row["draft_parse_mode"] or row["parse_mode"]) if row else item.default_parse_mode
    try:
        content.save_draft(item.key, m.text, scope_type, scope_id, parse_mode=parse_mode, admin_id=m.from_user.id)
    except Exception as exc:
        return await m.answer(f"❌ Draft ذخیره نشد:\n{exc}\n\nمتن اصلاح‌شده را دوباره بفرستید.")
    await state.finish()
    db.log_admin_action(m.from_user.id, "content_save_draft", details=f"slot={item.key};scope={scope_type}:{scope_id}")
    ch = _scope_char(scope_type)
    await m.answer("✅ Draft ذخیره شد. قبل از انتشار، پیش‌نمایش را بررسی کنید.", reply_markup=_detail_kb(idx, scope_type, scope_id))


async def cb_photo(c: types.CallbackQuery, state: FSMContext):
    if not _admin(c.from_user.id):
        return await c.answer()
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s))
    await state.update_data(content_idx=idx, content_scope=scope_type, content_scope_id=scope_id)
    await ContentStates.waiting_photo.set(); await c.answer()
    cancel_kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("❌ لغو و خروج از ویرایش", callback_data=f"ct_cancel_{idx}_{ch}_{scope_id}"))
    await c.message.answer("🖼 عکس را ارسال کنید. برای حذف عکس Draft، کلمه «حذف» را بفرستید.", reply_markup=cancel_kb)


async def process_photo(m: types.Message, state: FSMContext):
    if not _admin(m.from_user.id):
        return
    data = await state.get_data(); idx = int(data.get("content_idx") or -1); item = _item(idx)
    scope_type = data.get("content_scope") or content.SCOPE_GLOBAL; scope_id = int(data.get("content_scope_id") or 0)
    try:
        if m.photo:
            file_id = m.photo[-1].file_id
        elif (m.text or "").strip() == "حذف":
            file_id = ""
        else:
            return await m.answer("لطفاً عکس بفرستید یا کلمه «حذف» را ارسال کنید.")
        content.save_draft_photo(item.key, file_id, scope_type, scope_id, admin_id=m.from_user.id)
    except Exception as exc:
        return await m.answer(f"❌ ذخیره عکس ناموفق بود: {exc}")
    await state.finish()
    await m.answer("✅ رسانه Draft ذخیره شد.", reply_markup=_detail_kb(idx, scope_type, scope_id))


async def cb_parse_modes(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); await c.answer()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for mode, label in (("plain", "متن ساده"), ("html", "HTML"), ("markdownv2", "MarkdownV2")):
        kb.add(types.InlineKeyboardButton(label, callback_data=f"ct_ps_{idx}_{ch}_{scope_id}_{mode}"))
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت", callback_data=f"ct_d_{idx}_{ch}_{scope_id}"))
    await _replace(c, "🎨 نوع قالب‌بندی Draft را انتخاب کنید:", kb)


async def cb_set_parse_mode(c: types.CallbackQuery):
    parts = c.data.split("_")
    idx = int(parts[2]); ch = parts[3]; scope_id = int(parts[4]); mode = parts[5]
    scope_type, scope_id = _scope(ch, scope_id); item = _item(idx)
    try:
        content.set_draft_parse_mode(item.key, mode, scope_type, scope_id, admin_id=c.from_user.id)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    await c.answer("ذخیره شد")
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


async def cb_preview(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    await c.answer()
    category_id = scope_id if scope_type == content.SCOPE_CATEGORY else None
    plan_id = scope_id if scope_type == content.SCOPE_PLAN else None
    try:
        result = content.preview(item.key, category_id=category_id, plan_id=plan_id, scope_type=scope_type, scope_id=scope_id)
        prefix = "👁 پیش‌نمایش Draft\n\n"
        if result["photo_file_id"]:
            if len(result["text"]) <= content.CAPTION_LIMIT:
                await c.message.answer_photo(result["photo_file_id"], caption=prefix + result["text"], parse_mode=result["parse_mode_api"])
            else:
                await c.message.answer_photo(result["photo_file_id"])
                await c.message.answer(prefix + result["text"], parse_mode=result["parse_mode_api"])
        else:
            await c.message.answer(prefix + result["text"], parse_mode=result["parse_mode_api"])
    except Exception as exc:
        await c.message.answer(f"❌ پیش‌نمایش ناموفق بود:\n{exc}")


async def cb_publish(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    try:
        ok = content.publish(item.key, scope_type, scope_id, admin_id=c.from_user.id)
    except Exception as exc:
        return await c.answer(str(exc), show_alert=True)
    if ok:
        db.log_admin_action(c.from_user.id, "content_publish", details=f"slot={item.key};scope={scope_type}:{scope_id}")
    await c.answer("منتشر شد" if ok else "Draft برای انتشار وجود ندارد", show_alert=not ok)
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


async def cb_toggle_active(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    row = content.get_template(item.key, scope_type, scope_id)
    if not row:
        return await c.answer("قالب سفارشی برای تغییر وضعیت وجود ندارد", show_alert=True)
    new_active = not bool(int(row["is_active"] or 0))
    content.set_active(item.key, scope_type, scope_id, new_active, admin_id=c.from_user.id)
    db.log_admin_action(c.from_user.id, "content_toggle_active", details=f"slot={item.key};scope={scope_type}:{scope_id};active={int(new_active)}")
    await c.answer("قالب فعال شد" if new_active else "قالب غیرفعال شد؛ متن ارث‌بری نمایش داده می‌شود")
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


async def cb_cancel_edit(c: types.CallbackQuery, state: FSMContext):
    try:
        _, _, idx_s, ch, sid_s = c.data.split("_", 4)
        idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s))
    except Exception:
        return await c.answer("درخواست نامعتبر", show_alert=True)
    await state.finish()
    await c.answer("ویرایش لغو شد")
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


async def cb_clear_draft(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    content.clear_draft(item.key, scope_type, scope_id)
    await c.answer("Draft حذف شد")
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


async def cb_restore(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    content.restore_default(item.key, scope_type, scope_id, admin_id=c.from_user.id)
    db.log_admin_action(c.from_user.id, "content_restore_default", details=f"slot={item.key};scope={scope_type}:{scope_id}")
    await c.answer("بازگردانی شد")
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


async def cb_history(c: types.CallbackQuery):
    _, _, idx_s, ch, sid_s = c.data.split("_", 4)
    idx = int(idx_s); scope_type, scope_id = _scope(ch, int(sid_s)); item = _item(idx)
    rows = content.list_versions(item.key, scope_type, scope_id, 8); await c.answer()
    kb = types.InlineKeyboardMarkup(row_width=1)
    lines = [f"🕘 نسخه‌های قبلی — {item.title}", ""]
    for row in rows:
        lines.append(f"#{row['id']} | {row['created_at']} | {row['action']}")
        kb.add(types.InlineKeyboardButton(f"♻️ بازگردانی نسخه #{row['id']}", callback_data=f"ct_vr_{row['id']}_{idx}_{ch}_{scope_id}"))
    if not rows:
        lines.append("هنوز نسخه قبلی ثبت نشده است. تاریخچه با اولین انتشار یا بازگردانی ساخته می‌شود.")
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت", callback_data=f"ct_d_{idx}_{ch}_{scope_id}"))
    await _replace(c, "\n".join(lines), kb)


async def cb_restore_version(c: types.CallbackQuery):
    parts = c.data.split("_")
    version_id = int(parts[2]); idx = int(parts[3]); ch = parts[4]; scope_id = int(parts[5])
    ok = content.restore_version(version_id, admin_id=c.from_user.id)
    await c.answer("نسخه بازگردانی شد" if ok else "نسخه پیدا نشد", show_alert=not ok)
    c.data = f"ct_d_{idx}_{ch}_{scope_id}"
    await cb_content_detail(c)


# ---------------- display settings ----------------

def _display_scope_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🌐 تنظیمات عمومی", callback_data="ct_ds_g_0"))
    kb.add(types.InlineKeyboardButton("🗂 تنظیمات یک دسته", callback_data="ct_dscat"))
    kb.add(types.InlineKeyboardButton("🏷 تنظیمات یک پلن", callback_data="ct_dsplan"))
    kb.add(types.InlineKeyboardButton("⬅️ مرکز محتوا", callback_data="adm_content"))
    return kb


def _display_detail_kb(scope_type: str, scope_id: int):
    ch = _scope_char(scope_type)
    settings = content.get_display_settings(category_id=scope_id if scope_type == content.SCOPE_CATEGORY else None, plan_id=scope_id if scope_type == content.SCOPE_PLAN else None)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for idx, key in enumerate(DISPLAY_LABELS):
        mark = "✅" if settings[key] else "❌"
        kb.add(types.InlineKeyboardButton(f"{mark} {DISPLAY_LABELS[key]}", callback_data=f"ct_dt_{ch}_{scope_id}_{idx}"))
    kb.add(types.InlineKeyboardButton("♻️ حذف تنظیمات این دامنه", callback_data=f"ct_dc_{ch}_{scope_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ انتخاب دامنه", callback_data="ct_display"))
    return kb


async def cb_display(c: types.CallbackQuery):
    await c.answer()
    await _replace(c, "⚙️ تنظیمات نمایش\n\nتنظیمات پلن روی دسته و تنظیمات دسته روی عمومی اولویت دارد. اطلاعات فنی و عدد موجودی به‌صورت پیش‌فرض مخفی هستند.", _display_scope_kb())


async def cb_display_cat(c: types.CallbackQuery):
    await c.answer()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in db.list_plan_categories(active_only=False, include_empty=True, limit=50):
        kb.add(types.InlineKeyboardButton(f"{row['emoji'] or '📦'} {row['title']}", callback_data=f"ct_ds_c_{row['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت", callback_data="ct_display"))
    await _replace(c, "دسته را انتخاب کنید:", kb)


async def cb_display_plan(c: types.CallbackQuery):
    await c.answer()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in db.list_plans(active_only=False, limit=50):
        kb.add(types.InlineKeyboardButton(f"🏷 {row['title']}", callback_data=f"ct_ds_p_{row['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ بازگشت", callback_data="ct_display"))
    await _replace(c, "پلن را انتخاب کنید:", kb)


async def cb_display_detail(c: types.CallbackQuery):
    _, _, ch, sid_s = c.data.split("_", 3)
    scope_type, scope_id = _scope(ch, int(sid_s)); await c.answer()
    await _replace(c, f"⚙️ تنظیمات نمایش — {_scope_label(scope_type, scope_id)}", _display_detail_kb(scope_type, scope_id))


async def cb_display_toggle(c: types.CallbackQuery):
    parts = c.data.split("_")
    ch = parts[2]; scope_id = int(parts[3]); idx = int(parts[4]); scope_type, scope_id = _scope(ch, scope_id)
    key = list(DISPLAY_LABELS)[idx]
    current = content.get_display_settings(category_id=scope_id if scope_type == content.SCOPE_CATEGORY else None, plan_id=scope_id if scope_type == content.SCOPE_PLAN else None)[key]
    content.set_display_setting(scope_type, scope_id, key, not current, admin_id=c.from_user.id)
    await c.answer("بروزرسانی شد")
    c.data = f"ct_ds_{ch}_{scope_id}"
    await cb_display_detail(c)


async def cb_display_clear(c: types.CallbackQuery):
    _, _, ch, sid_s = c.data.split("_", 3)
    scope_type, scope_id = _scope(ch, int(sid_s)); content.clear_display_settings(scope_type, scope_id)
    await c.answer("تنظیمات این دامنه حذف شد")
    c.data = f"ct_ds_{ch}_{scope_id}"
    await cb_display_detail(c)


async def cb_funnel(c: types.CallbackQuery):
    await c.answer()
    rows = content.funnel_report(30)
    lines = ["📈 قیف خرید — ۳۰ روز اخیر", ""]
    previous = None
    for key, count in rows:
        rate = ""
        if previous not in (None, 0):
            rate = f" | تبدیل مرحله: {count / previous * 100:.1f}%"
        lines.append(f"{FUNNEL_LABELS.get(key, key)}: {count}{rate}")
        previous = count
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⬅️ مرکز محتوا", callback_data="adm_content"))
    await _replace(c, "\n".join(lines), kb)


async def cb_legacy_content_redirect(c: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await c.answer("این ویرایشگر با مرکز محتوای جدید جایگزین شده است.")
    await _replace(
        c,
        "🧠 این بخش قدیمی غیرفعال شده است.\n\nهمه متن‌ها، قالب‌ها، پیش‌نمایش و بازگردانی حالا فقط از مرکز محتوای یکپارچه انجام می‌شوند.",
        _home_kb(),
    )


async def cb_content_denied(c: types.CallbackQuery):
    await c.answer("این بخش فقط برای مدیران فعال است.", show_alert=True)


def _allowed(c, predicate) -> bool:
    return _admin(c.from_user.id) and bool(predicate)


def register(dp):
    # Reject crafted callbacks from non-admin users before any content action runs.
    dp.register_callback_query_handler(
        cb_content_denied,
        lambda c: (c.data.startswith("ct_") or c.data in {"adm_content", "adm_messages", "adm_plan_templates"}) and not _admin(c.from_user.id),
        state="*",
    )
    dp.register_callback_query_handler(
        cb_legacy_content_redirect,
        lambda c: _allowed(c, any(c.data.startswith(prefix) for prefix in LEGACY_CONTENT_PREFIXES)),
        state="*",
    )
    # Legacy content entry callbacks are intentionally redirected to one center,
    # so no parallel message/template menus remain.
    dp.register_callback_query_handler(cb_content_home, lambda c: _allowed(c, c.data in {"adm_content", "adm_messages", "adm_plan_templates"}), state="*")
    dp.register_callback_query_handler(cb_pack_home, lambda c: _allowed(c, c.data == "ct_pack_home"), state="*")
    dp.register_callback_query_handler(cb_pack_show, lambda c: _allowed(c, c.data.startswith("ct_pack_show_")), state="*")
    dp.register_callback_query_handler(cb_pack_preview, lambda c: _allowed(c, c.data.startswith("ct_pack_preview_")), state="*")
    dp.register_callback_query_handler(cb_pack_categories, lambda c: _allowed(c, c.data.startswith("ct_pack_cats_")), state="*")
    dp.register_callback_query_handler(cb_pack_plans, lambda c: _allowed(c, c.data.startswith("ct_pack_plans_")), state="*")
    dp.register_callback_query_handler(cb_pack_apply, lambda c: _allowed(c, c.data.startswith("ct_pack_do_")), state="*")
    dp.register_callback_query_handler(cb_content_category, lambda c: _allowed(c, c.data.startswith("ct_cat_")), state="*")
    dp.register_callback_query_handler(cb_content_detail, lambda c: _allowed(c, c.data.startswith("ct_d_")), state="*")
    dp.register_callback_query_handler(cb_scope_select, lambda c: _allowed(c, c.data.startswith("ct_sc_")), state="*")
    dp.register_callback_query_handler(cb_scope_category, lambda c: _allowed(c, c.data.startswith("ct_scat_")), state="*")
    dp.register_callback_query_handler(cb_scope_plan, lambda c: _allowed(c, c.data.startswith("ct_splan_")), state="*")
    dp.register_callback_query_handler(cb_edit, lambda c: _allowed(c, c.data.startswith("ct_e_")), state="*")
    dp.register_message_handler(process_text, content_types=types.ContentTypes.TEXT, state=ContentStates.waiting_text)
    dp.register_callback_query_handler(cb_photo, lambda c: _allowed(c, c.data.startswith("ct_ph_")), state="*")
    dp.register_message_handler(process_photo, content_types=types.ContentTypes.ANY, state=ContentStates.waiting_photo)
    dp.register_callback_query_handler(cb_cancel_edit, lambda c: _allowed(c, c.data.startswith("ct_cancel_")), state="*")
    dp.register_callback_query_handler(cb_parse_modes, lambda c: _allowed(c, c.data.startswith("ct_pm_")), state="*")
    dp.register_callback_query_handler(cb_set_parse_mode, lambda c: _allowed(c, c.data.startswith("ct_ps_")), state="*")
    dp.register_callback_query_handler(cb_preview, lambda c: _allowed(c, c.data.startswith("ct_pr_")), state="*")
    dp.register_callback_query_handler(cb_publish, lambda c: _allowed(c, c.data.startswith("ct_pub_")), state="*")
    dp.register_callback_query_handler(cb_toggle_active, lambda c: _allowed(c, c.data.startswith("ct_ac_")), state="*")
    dp.register_callback_query_handler(cb_clear_draft, lambda c: _allowed(c, c.data.startswith("ct_cd_")), state="*")
    dp.register_callback_query_handler(cb_restore, lambda c: _allowed(c, c.data.startswith("ct_rs_")), state="*")
    dp.register_callback_query_handler(cb_history, lambda c: _allowed(c, c.data.startswith("ct_hi_")), state="*")
    dp.register_callback_query_handler(cb_restore_version, lambda c: _allowed(c, c.data.startswith("ct_vr_")), state="*")
    dp.register_callback_query_handler(cb_display, lambda c: _allowed(c, c.data == "ct_display"), state="*")
    dp.register_callback_query_handler(cb_display_cat, lambda c: _allowed(c, c.data == "ct_dscat"), state="*")
    dp.register_callback_query_handler(cb_display_plan, lambda c: _allowed(c, c.data == "ct_dsplan"), state="*")
    dp.register_callback_query_handler(cb_display_detail, lambda c: _allowed(c, c.data.startswith("ct_ds_")), state="*")
    dp.register_callback_query_handler(cb_display_toggle, lambda c: _allowed(c, c.data.startswith("ct_dt_")), state="*")
    dp.register_callback_query_handler(cb_display_clear, lambda c: _allowed(c, c.data.startswith("ct_dc_")), state="*")
    dp.register_callback_query_handler(cb_funnel, lambda c: _allowed(c, c.data == "ct_funnel"), state="*")

