"""Expiry / usage warnings for PasarGuard-delivered services.

Scoped to PasarGuard only (per explicit instruction) — YouPanel-delivered
items are skipped even if a YouPanel provider happens to be configured.

Runs as a background loop (see expiry_alert_loop), polling every active
provider-delivered subscription's LIVE status from the panel (not the
cached creation-time payload, which goes stale) and nudging the customer
once per threshold crossed:
  - 3 days and 1 day before expiry (only once each is active, not on_hold)
  - 80% and 95% of data used (skipped entirely for unlimited plans)

Each (purchase, item, warning_type) combination is only ever sent once —
tracked in the expiry_warnings_sent table — so restarts or re-runs never
spam the same customer twice for the same threshold.
"""

from __future__ import annotations

import asyncio
import logging
import time

import commerce
import db
import subs
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 6 * 3600  # every 6 hours
PER_ITEM_DELAY_SECONDS = 0.3  # be gentle with the panel API

EXPIRY_THRESHOLDS_DAYS = (3, 1)
USAGE_THRESHOLDS_PERCENT = (80, 95)


def _renewal_kb(plan_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔄 تمدید همین سرویس", callback_data=f"buy_plan_{plan_id}"))
    return kb


async def _check_one_item(bot, row) -> None:
    purchase_id = int(row["purchase_id"])
    item_index = int(row["item_index"])
    provider_key = row["provider_key"]
    username = row["provider_username"]
    user_id = row["user_id"]
    plan_id = row["plan_id"]

    try:
        provider = subs.get_provider_adapter(provider_key)
    except Exception:
        return
    if not isinstance(provider, subs.PasarGuardProvider):
        return  # explicitly PasarGuard-only for now

    try:
        info = await provider.get_user(username)
    except subs.ProviderError as exc:
        logger.warning("expiry_alerts: get_user failed for %s/%s: %s", provider_key, username, exc)
        return
    if not info:
        return

    plan = db.get_plan(plan_id)
    plan_title = plan["title"] if plan else "سرویس شما"

    # --- expiry check (only meaningful once the service is actually active) ---
    status = str(info.get("status") or "")
    expire = info.get("expire")
    if status == "active" and expire:
        days_left = (int(expire) - int(time.time())) / 86400
        for threshold in EXPIRY_THRESHOLDS_DAYS:
            warning_type = f"expire_{threshold}d"
            if days_left <= threshold and not commerce.warning_already_sent(purchase_id, item_index, warning_type):
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ سرویس «{plan_title}» شما تا حدود {max(0, round(days_left))} روز دیگر منقضی می‌شود.\n\n"
                        "برای جلوگیری از قطعی، همین حالا تمدید کنید.",
                        reply_markup=_renewal_kb(plan_id),
                    )
                except Exception:
                    logger.exception("expiry_alerts: failed to notify user %s", user_id)
                commerce.mark_warning_sent(purchase_id, item_index, warning_type)
                break  # don't also fire the looser threshold in the same pass

    # --- usage check (skip unlimited plans, where data_limit is 0) ---
    data_limit = int(info.get("data_limit") or 0)
    used_traffic = int(info.get("used_traffic") or 0)
    if data_limit > 0:
        percent_used = (used_traffic / data_limit) * 100
        for threshold in USAGE_THRESHOLDS_PERCENT:
            warning_type = f"usage_{threshold}p"
            if percent_used >= threshold and not commerce.warning_already_sent(purchase_id, item_index, warning_type):
                try:
                    await bot.send_message(
                        user_id,
                        f"📊 حجم مصرفی سرویس «{plan_title}» شما به حدود {round(percent_used)}٪ رسیده است.\n\n"
                        "پیشنهاد می‌کنیم زودتر تمدید کنید تا دچار قطعی نشوید.",
                        reply_markup=_renewal_kb(plan_id),
                    )
                except Exception:
                    logger.exception("expiry_alerts: failed to notify user %s", user_id)
                commerce.mark_warning_sent(purchase_id, item_index, warning_type)
                break


async def check_and_send_alerts(bot) -> None:
    rows = commerce.list_active_provider_items()
    for row in rows:
        await _check_one_item(bot, row)
        await asyncio.sleep(PER_ITEM_DELAY_SECONDS)


async def expiry_alert_loop(bot, interval_seconds: int = CHECK_INTERVAL_SECONDS) -> None:
    while True:
        try:
            await check_and_send_alerts(bot)
        except Exception:
            logger.exception("expiry_alert_loop: unexpected error during scheduled run")
        await asyncio.sleep(max(3600, int(interval_seconds or 0)))
