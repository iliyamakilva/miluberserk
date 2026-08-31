"""Referral reward orchestration."""

import db
import settings
from anti_fraud import flag_for_review
from config import MAX_REFERRALS_PER_DAY, MAX_TOTAL_REFERRALS


def reward_ref(user_id):
    """Reward the referrer exactly once, using one atomic database transaction.

    Return values remain compatible with the old callers:
    ("rewarded", referrer_id), ("blocked", human_message), or
    ("skipped", reason).
    """
    status, reason, referrer_id = db.reward_referral_atomic(
        user_id,
        settings.ref_reward(),
        max_total=MAX_TOTAL_REFERRALS,
        max_per_day=MAX_REFERRALS_PER_DAY,
    )

    if status == "rewarded":
        return "rewarded", referrer_id
    if status == "blocked":
        return "blocked", flag_for_review(reason, referrer_id or "-", user_id)
    return "skipped", reason
