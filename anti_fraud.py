"""Lightweight referral checks used by admin diagnostics.

The actual reward path also repeats these checks inside an atomic database
transaction so two concurrent first-purchase events cannot pay twice.
"""

from config import MAX_REFERRALS_PER_DAY, MAX_TOTAL_REFERRALS
from db import get_user, referral_count, referrals_rewarded_today


def is_referral_suspicious(ref_id, referred_id):
    ref_id, referred_id = str(ref_id), str(referred_id)
    if ref_id == referred_id:
        return True, "self_referral"

    referrer = get_user(ref_id)
    if not referrer:
        return True, "referrer_not_found"
    if referrer["banned"]:
        return True, "referrer_banned"
    if "is_test" in referrer.keys() and int(referrer["is_test"] or 0):
        return True, "test_referrer"

    referred = get_user(referred_id)
    if referred and referred["banned"]:
        return True, "referred_banned"
    if referred and "is_test" in referred.keys() and int(referred["is_test"] or 0):
        return True, "test_referred_user"

    if MAX_TOTAL_REFERRALS and referral_count(ref_id) >= MAX_TOTAL_REFERRALS:
        return True, "referral_cap_exceeded"
    if MAX_REFERRALS_PER_DAY and referrals_rewarded_today(ref_id) >= MAX_REFERRALS_PER_DAY:
        return True, "daily_referral_limit"
    return False, None


def flag_for_review(reason, ref_id, referred_id):
    return f"⚠️ پاداش رفرال بلاک شد ({reason})\nمعرف: {ref_id}\nمعرفی‌شده: {referred_id}"
