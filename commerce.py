"""v6.3 reliability, discounts, reporting and plan-template services.

The module deliberately keeps the new business rules out of Telegram handlers.
All wallet debits, discount redemptions, queue state transitions and refunds are
transactional and idempotent.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

import db

SCHEMA_VERSION = 640
ORDER_RETRY_DELAYS = (30, 120)
ALLOWED_TEMPLATE_FIELDS = {
    "title", "category", "volume", "duration", "price", "devices",
    "delivery", "start_mode", "username", "subscription_url", "expire_date",
    "description", "tag",
}
REQUIRED_TEMPLATE_FIELDS = {"title", "price"}

DEFAULT_PLAN_TEMPLATES = {
    "professional": (
        "حرفه‌ای",
        "✨ {title}\n\n📂 دسته: {category}\n📦 حجم: {volume}\n⏳ مدت: {duration}\n"
        "📱 دستگاه: {devices}\n🚚 تحویل: {delivery}\n💰 قیمت: {price} تومان\n\n{description}",
    ),
    "minimal": (
        "مینیمال",
        "{title}\n{volume} | {duration}\n{price} تومان",
    ),
    "vip": (
        "VIP",
        "💎 {title}\n\n⚡ حجم: {volume}\n⏳ اعتبار: {duration}\n📱 دستگاه: {devices}\n"
        "🚀 تحویل: {delivery}\n💰 {price} تومان\n\n{description}",
    ),
    "economy": (
        "اقتصادی",
        "🌱 {title}\n\n📦 {volume}\n⏳ {duration}\n💰 {price} تومان\n\n{description}",
    ),
    "technical": (
        "فنی",
        "🧩 {title}\nCategory: {category}\nTraffic: {volume}\nDuration: {duration}\n"
        "Devices: {devices}\nDelivery: {delivery}\nStart: {start_mode}\nPrice: {price} تومان",
    ),
    "sales": (
        "فروش‌محور",
        "🔥 {title}\n\n✅ {volume} حجم\n✅ {duration} اعتبار\n✅ تحویل {delivery}\n"
        "💰 فقط {price} تومان\n\n{description}",
    ),
    "trial": (
        "تست رایگان",
        "🧪 {title}\n\n📦 حجم تست: {volume}\n⏳ اعتبار: {duration}\n📱 دستگاه: {devices}\n"
        "💳 هزینه: {price} تومان",
    ),
}


def _columns(table: str) -> set[str]:
    db.cur.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in db.cur.fetchall()}


def _add_column(table: str, name: str, definition: str) -> None:
    if name not in _columns(table):
        db.cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_schema() -> None:
    """Migrate v6.2 databases to schema 630 without destructive operations."""
    with db.LOCK:
        _add_column("plans", "fallback_provider_key", "TEXT")
        _add_column("plans", "template_id", "INTEGER")
        _add_column("plan_categories", "template_id", "INTEGER")
        _add_column("purchases", "subtotal_amount", "INTEGER DEFAULT 0")
        _add_column("purchases", "discount_amount", "INTEGER DEFAULT 0")
        _add_column("purchases", "discount_id", "INTEGER")
        _add_column("purchases", "discount_code", "TEXT")
        _add_column("purchases", "original_provider", "TEXT")
        _add_column("purchases", "retry_count", "INTEGER DEFAULT 0")
        _add_column("purchases", "max_retries", "INTEGER DEFAULT 3")
        _add_column("purchases", "next_retry_at", "TEXT")
        _add_column("purchases", "last_attempt_at", "TEXT")
        _add_column("purchases", "provider_request_id", "TEXT")
        _add_column("purchases", "provider_username", "TEXT")
        _add_column("purchases", "review_required", "INTEGER DEFAULT 0")
        _add_column("purchases", "bonus_volume_mb", "INTEGER DEFAULT 0")
        _add_column("purchases", "delivery_notified_at", "TEXT")
        _add_column("purchases", "refund_notified_at", "TEXT")
        _add_column("purchases", "request_key", "TEXT")
        _add_column("tickets", "service_id", "INTEGER")
        _add_column("tickets", "issue_type", "TEXT")
        _add_column("tickets", "snapshot_json", "TEXT DEFAULT '{}'")
        _add_column("topups", "discount_code", "TEXT")
        _add_column("topups", "request_key", "TEXT")

        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_states(
                provider_key TEXT PRIMARY KEY,
                is_sales_enabled INTEGER DEFAULT 1,
                last_status TEXT DEFAULT 'unknown',
                last_checked_at TEXT,
                response_ms INTEGER,
                last_error TEXT,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                services_created INTEGER DEFAULT 0,
                capabilities_json TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_key TEXT NOT NULL,
                operation TEXT NOT NULL,
                user_id TEXT,
                plan_id INTEGER,
                purchase_id INTEGER,
                result TEXT NOT NULL,
                response_ms INTEGER,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_jobs(
                purchase_id INTEGER PRIMARY KEY,
                primary_provider TEXT NOT NULL,
                active_provider TEXT NOT NULL,
                fallback_provider TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                next_retry_at TEXT,
                last_error TEXT,
                locked_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_job_items(
                purchase_id INTEGER NOT NULL,
                item_index INTEGER NOT NULL,
                provider_key TEXT NOT NULL,
                provider_username TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                subscription_url TEXT,
                payload_json TEXT DEFAULT '{}',
                last_error TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY(purchase_id, item_index),
                UNIQUE(provider_key, provider_username)
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS discounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                title TEXT DEFAULT '',
                discount_type TEXT NOT NULL DEFAULT 'percent',
                value INTEGER NOT NULL DEFAULT 0,
                max_uses INTEGER DEFAULT 0,
                per_user_limit INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                starts_at TEXT,
                ends_at TEXT,
                min_amount INTEGER DEFAULT 0,
                category_id INTEGER,
                plan_id INTEGER,
                first_purchase_only INTEGER DEFAULT 0,
                new_users_only INTEGER DEFAULT 0,
                renewals_only INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS discount_redemptions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discount_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                purchase_id INTEGER NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(discount_id, purchase_id)
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaigns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                trigger_type TEXT DEFAULT 'inactive_days',
                inactivity_days INTEGER DEFAULT 7,
                message_text TEXT NOT NULL,
                discount_id INTEGER,
                is_active INTEGER DEFAULT 1,
                starts_at TEXT,
                ends_at TEXT,
                last_run_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_deliveries(
                campaign_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT DEFAULT 'sent',
                error TEXT,
                sent_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY(campaign_id, user_id)
            )
            """
        )
        db.cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_text_templates(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_key TEXT UNIQUE,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                is_system INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )

        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_provider_logs_provider_created ON provider_logs(provider_key, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_provider_jobs_due ON provider_jobs(status, next_retry_at)",
            "CREATE INDEX IF NOT EXISTS idx_discounts_active_code ON discounts(is_active, code)",
            "CREATE INDEX IF NOT EXISTS idx_discount_redemptions_user ON discount_redemptions(user_id, discount_id)",
            "CREATE INDEX IF NOT EXISTS idx_campaigns_active ON campaigns(is_active, trigger_type)",
            "CREATE INDEX IF NOT EXISTS idx_tickets_service ON tickets(service_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_purchases_review ON purchases(review_required, status, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_request_key ON purchases(request_key) WHERE request_key IS NOT NULL AND TRIM(request_key)<>''",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_topups_request_key ON topups(request_key) WHERE request_key IS NOT NULL AND TRIM(request_key)<>''",
        ]
        for statement in indexes:
            db.cur.execute(statement)

        for key, (title, body) in DEFAULT_PLAN_TEMPLATES.items():
            db.cur.execute(
                """
                INSERT INTO plan_text_templates(template_key,title,body,is_system,is_active)
                VALUES (?,?,?,1,1)
                ON CONFLICT(template_key) DO UPDATE SET
                    title=excluded.title,
                    is_system=1
                """,
                (key, title, body),
            )

        db.cur.execute("UPDATE purchases SET subtotal_amount=amount WHERE COALESCE(subtotal_amount,0)=0")
        db.cur.execute("UPDATE purchases SET original_provider=provider WHERE original_provider IS NULL OR TRIM(original_provider)=''")
        db.cur.execute(
            "INSERT INTO settings(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        db.conn.commit()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def redact_sensitive_text(value: str | None, limit: int = 1000) -> str:
    """Remove URLs, credentials, bearer values and JWT-like strings from logs."""
    clean = (value or "")[: max(0, int(limit))]
    clean = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", clean)
    clean = re.sub(
        r"(?i)\b(password|passwd|token|access_token|refresh_token|jwt|authorization|cookie)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        clean,
    )
    clean = re.sub(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b", "[JWT_REDACTED]", clean)
    clean = re.sub(r"(?i)https?://\S+", "[URL_REDACTED]", clean)
    return clean


def ensure_provider(provider_key: str, capabilities: dict[str, Any] | None = None) -> None:
    key = (provider_key or "").strip().lower()
    if not key or key == "pool":
        return
    db.cur.execute(
        """
        INSERT INTO provider_states(provider_key,capabilities_json)
        VALUES (?,?)
        ON CONFLICT(provider_key) DO UPDATE SET
            capabilities_json=excluded.capabilities_json,
            updated_at=datetime('now')
        """,
        (key, _json(capabilities or {})),
    )
    db.conn.commit()


def get_provider_state(provider_key: str):
    key = (provider_key or "").strip().lower()
    db.cur.execute("SELECT * FROM provider_states WHERE provider_key=?", (key,))
    return db.cur.fetchone()


def list_provider_states():
    db.cur.execute("SELECT * FROM provider_states ORDER BY provider_key")
    return db.cur.fetchall()


def provider_sales_enabled(provider_key: str) -> bool:
    key = (provider_key or "").strip().lower()
    if key == "pool":
        return True
    row = get_provider_state(key)
    return True if row is None else bool(int(row["is_sales_enabled"] or 0))


def set_provider_sales(provider_key: str, enabled: bool) -> None:
    ensure_provider(provider_key)
    db.cur.execute(
        "UPDATE provider_states SET is_sales_enabled=?,updated_at=datetime('now') WHERE provider_key=?",
        (1 if enabled else 0, (provider_key or "").strip().lower()),
    )
    db.conn.commit()


def record_provider_log(
    provider_key: str,
    operation: str,
    result: str,
    *,
    user_id: str | None = None,
    plan_id: int | None = None,
    purchase_id: int | None = None,
    response_ms: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    clean_error = redact_sensitive_text(error_message, 1000)
    with db.LOCK:
        ensure_provider(provider_key)
        db.cur.execute(
            """
            INSERT INTO provider_logs(provider_key,operation,user_id,plan_id,purchase_id,result,response_ms,error_code,error_message)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            ((provider_key or "").lower(), operation, str(user_id) if user_id is not None else None,
             int(plan_id) if plan_id is not None else None, int(purchase_id) if purchase_id is not None else None,
             result, response_ms, error_code, clean_error),
        )
        if result == "success":
            db.cur.execute(
                """
                UPDATE provider_states SET last_status='online',last_checked_at=datetime('now'),response_ms=?,
                    last_error=NULL,success_count=success_count+1,updated_at=datetime('now')
                WHERE provider_key=?
                """,
                (response_ms, (provider_key or "").lower()),
            )
        else:
            db.cur.execute(
                """
                UPDATE provider_states SET last_status='error',last_checked_at=datetime('now'),response_ms=?,
                    last_error=?,failure_count=failure_count+1,updated_at=datetime('now')
                WHERE provider_key=?
                """,
                (response_ms, clean_error, (provider_key or "").lower()),
            )
        db.conn.commit()


def increment_provider_services(provider_key: str, count: int) -> None:
    ensure_provider(provider_key)
    db.cur.execute(
        "UPDATE provider_states SET services_created=services_created+?,updated_at=datetime('now') WHERE provider_key=?",
        (max(0, int(count)), (provider_key or "").lower()),
    )
    db.conn.commit()


def list_provider_logs(provider_key: str, limit: int = 20):
    db.cur.execute(
        "SELECT * FROM provider_logs WHERE provider_key=? ORDER BY id DESC LIMIT ?",
        ((provider_key or "").lower(), max(1, min(100, int(limit)))),
    )
    return db.cur.fetchall()


def resolve_provider_for_plan(plan, *, require_enabled: bool = True) -> tuple[str | None, str | None]:
    primary = db.plan_provider_key(plan)
    if primary == "pool":
        return "pool", None
    fallback = (plan["fallback_provider_key"] if "fallback_provider_key" in plan.keys() else None) or ""
    if not require_enabled or provider_sales_enabled(primary):
        return primary, None
    if fallback and fallback != primary and provider_sales_enabled(fallback):
        return fallback, f"تأمین‌کننده اصلی متوقف است؛ سفارش از {fallback} ساخته می‌شود."
    return None, "فروش این سرویس موقتاً متوقف شده است."


def set_plan_fallback(plan_id: int, provider_key: str | None) -> None:
    value = (provider_key or "").strip().lower() or None
    db.cur.execute("UPDATE plans SET fallback_provider_key=?,updated_at=datetime('now') WHERE id=?", (value, int(plan_id)))
    db.conn.commit()


def normalize_request_key(value: str | None) -> str | None:
    """Return a bounded idempotency key safe for SQLite unique indexes."""
    value = (value or "").strip()
    return value[:180] or None


def purchase_request_key(user_id, chat_id, message_id, callback_data) -> str:
    """Stable key for repeated taps on the same Telegram purchase action."""
    return normalize_request_key(
        f"tg:{user_id}:{chat_id}:{message_id}:{callback_data or ''}"
    )


def _existing_purchase_result(purchase) -> dict[str, Any]:
    """Rebuild a public purchase result without charging or provisioning again."""
    row = dict(purchase)
    db.cur.execute("SELECT * FROM subs WHERE purchase_id=? ORDER BY id", (int(row["id"]),))
    items = [dict(item) for item in db.cur.fetchall()]
    status = (row.get("status") or "").lower()
    user = db.get_user(row["user_id"])
    return {
        "purchase_id": int(row["id"]),
        "quantity": int(row.get("quantity") or 0),
        "unit_price": int(row.get("unit_price") or 0),
        "subtotal": int(row.get("subtotal_amount") or row.get("amount") or 0),
        "discount_amount": int(row.get("discount_amount") or 0),
        "amount": int(row.get("amount") or 0),
        "balance_before": None,
        "balance_after": int(user["balance"] or 0) if user else 0,
        "is_test": int(row.get("is_test") or 0),
        "items": items,
        "provider": row.get("provider") or "pool",
        "queued": status in {"paid", "provisioning", "retry"},
        "completed": status == "completed",
        "refunded": status == "refunded",
        "existing": True,
        "status": status,
    }


def get_purchase_by_request_key(request_key: str | None):
    request_key = normalize_request_key(request_key)
    if not request_key:
        return None
    db.cur.execute("SELECT * FROM purchases WHERE request_key=?", (request_key,))
    return db.cur.fetchone()


def _discount_row(code: str):
    db.cur.execute("SELECT * FROM discounts WHERE code=? COLLATE NOCASE", ((code or "").strip(),))
    return db.cur.fetchone()


def validate_discount(
    code: str | None,
    user_id: str,
    plan,
    quantity: int,
    subtotal: int,
    *,
    purpose: str = "purchase",
) -> dict[str, Any]:
    if not code:
        return {"valid": True, "discount": None, "amount": 0, "total": int(subtotal), "bonus_volume": 0}
    row = _discount_row(code)
    if not row or not int(row["is_active"] or 0):
        return {"valid": False, "message": "کد تخفیف معتبر یا فعال نیست."}
    now_sql = "datetime('now')"
    db.cur.execute(
        f"SELECT CASE WHEN (? IS NULL OR datetime(?)<={now_sql}) AND (? IS NULL OR datetime(?)>={now_sql}) THEN 1 ELSE 0 END AS ok",
        (row["starts_at"], row["starts_at"], row["ends_at"], row["ends_at"]),
    )
    if not int(db.cur.fetchone()["ok"]):
        return {"valid": False, "message": "زمان استفاده از این کد تخفیف نیست."}
    if int(row["max_uses"] or 0) > 0 and int(row["used_count"] or 0) >= int(row["max_uses"]):
        return {"valid": False, "message": "ظرفیت استفاده از این کد تمام شده است."}
    db.cur.execute("SELECT COUNT(*) AS c FROM discount_redemptions WHERE discount_id=? AND user_id=?", (row["id"], str(user_id)))
    if int(db.cur.fetchone()["c"] or 0) >= max(1, int(row["per_user_limit"] or 1)):
        return {"valid": False, "message": "سقف استفاده شما از این کد تکمیل شده است."}
    if int(row["min_amount"] or 0) > int(subtotal):
        return {"valid": False, "message": f"حداقل مبلغ سفارش برای این کد {int(row['min_amount']):,} تومان است."}
    if row["plan_id"] is not None and int(row["plan_id"]) != int(plan["id"]):
        return {"valid": False, "message": "این کد برای پلن انتخاب‌شده قابل استفاده نیست."}
    if row["category_id"] is not None and int(row["category_id"]) != int(plan["category_id"] or 0):
        return {"valid": False, "message": "این کد برای دسته انتخاب‌شده قابل استفاده نیست."}
    if int(row["renewals_only"] or 0) and purpose != "renewal":
        return {"valid": False, "message": "این کد فقط برای تمدید است."}
    db.cur.execute("SELECT COUNT(*) AS c FROM purchases WHERE user_id=? AND status='completed'", (str(user_id),))
    completed = int(db.cur.fetchone()["c"] or 0)
    if int(row["first_purchase_only"] or 0) and completed > 0:
        return {"valid": False, "message": "این کد فقط برای اولین خرید است."}
    if int(row["new_users_only"] or 0):
        db.cur.execute("SELECT joined_at FROM users WHERE id=?", (str(user_id),))
        user = db.cur.fetchone()
        if not user:
            return {"valid": False, "message": "کاربر پیدا نشد."}
        db.cur.execute("SELECT CASE WHEN datetime(?) >= datetime('now','-7 days') THEN 1 ELSE 0 END AS ok", (user["joined_at"],))
        if not int(db.cur.fetchone()["ok"]):
            return {"valid": False, "message": "این کد فقط برای کاربران جدید است."}

    kind = (row["discount_type"] or "percent").lower()
    value = max(0, int(row["value"] or 0))
    amount = 0
    bonus_volume = 0
    if kind == "percent":
        amount = min(int(subtotal), int(subtotal) * min(value, 100) // 100)
    elif kind == "fixed":
        amount = min(int(subtotal), value)
    elif kind == "free":
        amount = int(subtotal)
    elif kind == "bonus_volume":
        if db.plan_provider_key(plan) == "pool":
            return {"valid": False, "message": "هدیه حجم فقط برای پلن‌های ساخت خودکار قابل اعمال است."}
        bonus_volume = value
    else:
        return {"valid": False, "message": "نوع کد تخفیف پشتیبانی نمی‌شود."}
    return {
        "valid": True,
        "discount": dict(row),
        "amount": amount,
        "total": max(0, int(subtotal) - amount),
        "bonus_volume": bonus_volume,
    }


def quote_purchase(user_id: str, plan, quantity: int, discount_code: str | None = None) -> dict[str, Any]:
    subtotal = int(plan["price"] or 0) * int(quantity)
    result = validate_discount(discount_code, str(user_id), plan, int(quantity), subtotal)
    if not result.get("valid"):
        raise db.PurchaseError("invalid_discount", result.get("message") or "کد تخفیف معتبر نیست.")
    return {"subtotal": subtotal, **result}


def _redeem_discount_tx(quote: dict[str, Any], user_id: str, purchase_id: int) -> None:
    discount = quote.get("discount")
    if not discount:
        return
    db.cur.execute(
        "INSERT INTO discount_redemptions(discount_id,user_id,purchase_id,amount) VALUES (?,?,?,?)",
        (int(discount["id"]), str(user_id), int(purchase_id), int(quote["amount"] or 0)),
    )
    db.cur.execute("UPDATE discounts SET used_count=used_count+1,updated_at=datetime('now') WHERE id=?", (int(discount["id"]),))



def attach_topup_discount(topup_id: int, discount_code: str | None) -> None:
    db.cur.execute("UPDATE topups SET discount_code=? WHERE id=?", (((discount_code or "").strip().upper() or None), int(topup_id)))
    db.conn.commit()

def complete_pool_purchase(
    user_id: str,
    quantity: int,
    plan_id: int,
    discount_code: str | None = None,
    note: str = "",
    request_key: str | None = None,
) -> dict[str, Any]:
    user_id, quantity, plan_id = str(user_id), int(quantity), int(plan_id)
    request_key = normalize_request_key(request_key)
    plan = db.get_plan(plan_id)
    if not plan:
        raise db.PurchaseError("plan_not_found", "پلن پیدا نشد.")
    if db.plan_provider_key(plan) != "pool":
        raise db.PurchaseError("wrong_delivery_type", "این پلن استخری نیست.")
    mode = db.plan_purchase_mode(plan)
    if not int(plan["is_active"] or 0) or mode in {"disabled", "wholesale"}:
        raise db.PurchaseError("purchase_mode", "خرید این پلن فعال نیست.")
    if quantity < 1 or quantity > max(1, int(plan["max_per_order"] or 1)) or (mode == "direct" and quantity != 1):
        raise db.PurchaseError("invalid_quantity", "تعداد انتخاب‌شده معتبر نیست.")

    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            if request_key:
                db.cur.execute("SELECT * FROM purchases WHERE request_key=?", (request_key,))
                existing = db.cur.fetchone()
                if existing:
                    if (
                        str(existing["user_id"]) != user_id
                        or int(existing["plan_id"] or 0) != plan_id
                        or int(existing["quantity"] or 0) != quantity
                    ):
                        raise db.PurchaseError(
                            "idempotency_conflict",
                            "کلید این عملیات با سفارش دیگری تداخل دارد.",
                        )
                    result = _existing_purchase_result(existing)
                    db.conn.commit()
                    return result
            db.cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
            user = db.cur.fetchone()
            if not user:
                raise db.PurchaseError("user_not_found", "کاربر پیدا نشد.")
            if int(user["banned"] or 0):
                raise db.PurchaseError("banned", "حساب شما مسدود است.")
            quote = quote_purchase(user_id, plan, quantity, discount_code)
            total = int(quote["total"])
            before = int(user["balance"] or 0)
            if before < total:
                raise db.PurchaseError("insufficient_balance", "موجودی کیف پول کافی نیست.")
            db.cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool'", (plan_id,))
            if int(db.cur.fetchone()["c"] or 0) < quantity:
                raise db.PurchaseError("insufficient_stock", "موجودی سرویس کافی نیست.")
            is_test = int(user["is_test"] or 0)
            discount = quote.get("discount")
            db.cur.execute(
                """
                INSERT INTO purchases(user_id,quantity,amount,unit_price,status,note,plan_id,is_test,provider,
                    original_provider,subtotal_amount,discount_amount,discount_id,discount_code,bonus_volume_mb,request_key,completed_at)
                VALUES (?,?,?,?, 'completed', ?,?,?, 'pool','pool',?,?,?,?,?,?,datetime('now'))
                """,
                (user_id, quantity, total, int(plan["price"] or 0), note or "", plan_id, is_test,
                 int(quote["subtotal"]), int(quote["amount"]), int(discount["id"]) if discount else None,
                 discount["code"] if discount else None, int(quote.get("bonus_volume") or 0), request_key),
            )
            purchase_id = int(db.cur.lastrowid)
            after = before - total
            db.cur.execute("UPDATE users SET balance=?,purchased=purchased+? WHERE id=?", (after, quantity, user_id))
            db.cur.execute(
                "INSERT INTO ledger(user_id,action,amount,balance_before,balance_after,note,is_test) VALUES (?,?,?,?,?,?,?)",
                (user_id, "purchase", -total, before, after, f"purchase_id={purchase_id};discount={int(quote['amount'])}", is_test),
            )
            db.cur.execute("SELECT * FROM subs WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool' ORDER BY id LIMIT ?", (plan_id, quantity))
            available = db.cur.fetchall()
            items = []
            for sub in available:
                account_name = sub["account_name"] or db.generate_service_code()
                db.cur.execute(
                    """UPDATE subs SET used=1,owner=?,assigned_at=datetime('now'),price_paid=?,account_name=?,status='delivered',purchase_id=?
                       WHERE id=? AND used=0""",
                    (user_id, int(plan["price"] or 0), account_name, purchase_id, sub["id"]),
                )
                if db.cur.rowcount != 1:
                    raise db.PurchaseError("stock_race", "موجودی همزمان تغییر کرد. دوباره تلاش کنید.")
                db.cur.execute("SELECT * FROM subs WHERE id=?", (sub["id"],))
                assigned = db.cur.fetchone()
                db.cur.execute(
                    """INSERT INTO purchase_items(purchase_id,sub_id,user_id,account_name,link,price_paid,assigned_at,status,plan_id)
                       VALUES (?,?,?,?,?,?,?,'active',?)""",
                    (purchase_id, assigned["id"], user_id, assigned["account_name"], assigned["link"],
                     int(plan["price"] or 0), assigned["assigned_at"], plan_id),
                )
                items.append(dict(assigned))
            _redeem_discount_tx(quote, user_id, purchase_id)
            if not is_test:
                db._bump_daily_tx("sales", quantity)
            db.conn.commit()
            return {
                "purchase_id": purchase_id, "quantity": quantity, "unit_price": int(plan["price"] or 0),
                "subtotal": int(quote["subtotal"]), "discount_amount": int(quote["amount"]), "amount": total,
                "balance_before": before, "balance_after": after, "is_test": is_test, "items": items,
                "bonus_volume": int(quote.get("bonus_volume") or 0), "queued": False,
            }
        except db.PurchaseError:
            db.conn.rollback()
            raise
        except Exception as exc:
            db.conn.rollback()
            raise db.PurchaseError("unexpected", "خطای داخلی خرید رخ داد.") from exc


def begin_provider_purchase(
    user_id: str,
    quantity: int,
    plan_id: int,
    discount_code: str | None = None,
    note: str = "",
    request_key: str | None = None,
) -> dict[str, Any]:
    user_id, quantity, plan_id = str(user_id), int(quantity), int(plan_id)
    request_key = normalize_request_key(request_key)
    plan = db.get_plan(plan_id)
    if not plan:
        raise db.PurchaseError("plan_not_found", "پلن پیدا نشد.")
    primary = db.plan_provider_key(plan)
    if primary == "pool":
        raise db.PurchaseError("wrong_delivery_type", "این پلن از استخر تحویل می‌شود.")
    active, reason = resolve_provider_for_plan(plan)
    if not active:
        raise db.PurchaseError("provider_sales_stopped", reason or "فروش این سرویس موقتاً متوقف است.")
    mode = db.plan_purchase_mode(plan)
    if not int(plan["is_active"] or 0) or mode in {"disabled", "wholesale"}:
        raise db.PurchaseError("purchase_mode", "خرید این پلن فعال نیست.")
    if quantity < 1 or quantity > max(1, int(plan["max_per_order"] or 1)) or (mode == "direct" and quantity != 1):
        raise db.PurchaseError("invalid_quantity", "تعداد انتخاب‌شده معتبر نیست.")
    if not (plan["unlimited_volume"] if "unlimited_volume" in plan.keys() else False) and int(plan["panel_data_limit_bytes"] or 0) <= 0:
        raise db.PurchaseError("invalid_provider_plan", "حجم ساخت خودکار پلن تنظیم نشده است.")
    if int(plan["panel_duration_days"] or 0) <= 0:
        raise db.PurchaseError("invalid_provider_plan", "مدت ساخت خودکار پلن تنظیم نشده است.")
    fallback = ((plan["fallback_provider_key"] if "fallback_provider_key" in plan.keys() else None) or "").strip().lower() or None

    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            if request_key:
                db.cur.execute("SELECT * FROM purchases WHERE request_key=?", (request_key,))
                existing = db.cur.fetchone()
                if existing:
                    if (
                        str(existing["user_id"]) != user_id
                        or int(existing["plan_id"] or 0) != plan_id
                        or int(existing["quantity"] or 0) != quantity
                    ):
                        raise db.PurchaseError(
                            "idempotency_conflict",
                            "کلید این عملیات با سفارش دیگری تداخل دارد.",
                        )
                    result = _existing_purchase_result(existing)
                    db.conn.commit()
                    return result
            db.cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
            user = db.cur.fetchone()
            if not user:
                raise db.PurchaseError("user_not_found", "کاربر پیدا نشد.")
            if int(user["banned"] or 0):
                raise db.PurchaseError("banned", "حساب شما مسدود است.")
            quote = quote_purchase(user_id, plan, quantity, discount_code)
            total = int(quote["total"])
            before = int(user["balance"] or 0)
            if before < total:
                raise db.PurchaseError("insufficient_balance", "موجودی کیف پول کافی نیست.")
            is_test = int(user["is_test"] or 0)
            discount = quote.get("discount")
            db.cur.execute(
                """
                INSERT INTO purchases(user_id,quantity,amount,unit_price,status,note,plan_id,is_test,provider,original_provider,
                    subtotal_amount,discount_amount,discount_id,discount_code,bonus_volume_mb,request_key,retry_count,max_retries)
                VALUES (?,?,?,?, 'paid', ?,?,?,?,?,?,?,?,?,?,?,0,3)
                """,
                (user_id, quantity, total, int(plan["price"] or 0), note or "", plan_id, is_test, active, primary,
                 int(quote["subtotal"]), int(quote["amount"]), int(discount["id"]) if discount else None,
                 discount["code"] if discount else None, int(quote.get("bonus_volume") or 0), request_key),
            )
            purchase_id = int(db.cur.lastrowid)
            after = before - total
            db.cur.execute("UPDATE users SET balance=? WHERE id=?", (after, user_id))
            db.cur.execute(
                "INSERT INTO ledger(user_id,action,amount,balance_before,balance_after,note,is_test) VALUES (?,?,?,?,?,?,?)",
                (user_id, "purchase", -total, before, after,
                 f"purchase_id={purchase_id};provider={active};status=paid;discount={int(quote['amount'])}", is_test),
            )
            db.cur.execute(
                """INSERT INTO provider_jobs(purchase_id,primary_provider,active_provider,fallback_provider,status,max_retries)
                   VALUES (?,?,?,?, 'pending',3)""",
                (purchase_id, primary, active, fallback),
            )
            for index in range(1, quantity + 1):
                username = f"bsv-{user_id}-{purchase_id}-{index}"
                db.cur.execute(
                    """INSERT INTO provider_job_items(purchase_id,item_index,provider_key,provider_username,status)
                       VALUES (?,?,?,?, 'pending')""",
                    (purchase_id, index, active, username[:48]),
                )
            _redeem_discount_tx(quote, user_id, purchase_id)
            db.conn.commit()
            return {
                "purchase_id": purchase_id, "user_id": user_id, "quantity": quantity,
                "unit_price": int(plan["price"] or 0), "subtotal": int(quote["subtotal"]),
                "discount_amount": int(quote["amount"]), "amount": total,
                "balance_before": before, "balance_after": after, "plan_id": plan_id,
                "is_test": is_test, "provider": active, "original_provider": primary,
                "fallback_provider": fallback, "provider_notice": reason,
                "bonus_volume": int(quote.get("bonus_volume") or 0),
                "existing": False,
                "status": "paid",
                "request_key": request_key,
            }
        except db.PurchaseError:
            db.conn.rollback()
            raise
        except Exception as exc:
            db.conn.rollback()
            raise db.PurchaseError("unexpected", "شروع سفارش پنلی ناموفق بود.") from exc


def get_provider_job(purchase_id: int):
    db.cur.execute("SELECT * FROM provider_jobs WHERE purchase_id=?", (int(purchase_id),))
    return db.cur.fetchone()


def list_provider_job_items(purchase_id: int):
    db.cur.execute("SELECT * FROM provider_job_items WHERE purchase_id=? ORDER BY item_index", (int(purchase_id),))
    return db.cur.fetchall()


def claim_provider_job(purchase_id: int) -> bool:
    with db.LOCK:
        db.cur.execute(
            """
            UPDATE provider_jobs SET locked_at=datetime('now'),status='processing',updated_at=datetime('now')
            WHERE purchase_id=? AND (locked_at IS NULL OR locked_at<datetime('now','-5 minutes'))
              AND status IN ('pending','retry','processing')
            """,
            (int(purchase_id),),
        )
        ok = db.cur.rowcount == 1
        if ok:
            db.cur.execute(
                "UPDATE purchases SET status='provisioning',last_attempt_at=datetime('now') WHERE id=? AND status!='completed'",
                (int(purchase_id),),
            )
        db.conn.commit()
        return ok


def release_provider_job(purchase_id: int) -> None:
    db.cur.execute("UPDATE provider_jobs SET locked_at=NULL,updated_at=datetime('now') WHERE purchase_id=?", (int(purchase_id),))
    db.conn.commit()


def set_job_item_ready(purchase_id: int, item_index: int, provider_key: str, username: str, item: dict[str, Any]) -> None:
    safe = {
        "username": item.get("username") or username,
        "status": item.get("status") or "active",
        "data_limit": item.get("data_limit") or 0,
        "used_traffic": item.get("used_traffic") or 0,
        "expire": item.get("expire"),
        "on_hold_expire_duration": item.get("on_hold_expire_duration"),
    }
    db.cur.execute(
        """
        UPDATE provider_job_items SET provider_key=?,provider_username=?,status='ready',subscription_url=?,payload_json=?,last_error=NULL,
            updated_at=datetime('now') WHERE purchase_id=? AND item_index=?
        """,
        ((provider_key or "").lower(), username, (item.get("subscription_url") or item.get("link") or "").strip(),
         _json(safe), int(purchase_id), int(item_index)),
    )
    db.conn.commit()


def set_job_item_error(purchase_id: int, item_index: int, error: str) -> None:
    db.cur.execute(
        "UPDATE provider_job_items SET status='error',last_error=?,updated_at=datetime('now') WHERE purchase_id=? AND item_index=?",
        ((error or "")[:1000], int(purchase_id), int(item_index)),
    )
    db.conn.commit()


def switch_job_provider(purchase_id: int, provider_key: str) -> None:
    key = (provider_key or "").lower()
    with db.LOCK:
        db.cur.execute(
            "UPDATE provider_jobs SET active_provider=?,status='pending',locked_at=NULL,updated_at=datetime('now') WHERE purchase_id=?",
            (key, int(purchase_id)),
        )
        db.cur.execute(
            "UPDATE provider_job_items SET provider_key=?,status=CASE WHEN status='ready' THEN status ELSE 'pending' END,last_error=NULL,updated_at=datetime('now') WHERE purchase_id=?",
            (key, int(purchase_id)),
        )
        db.cur.execute("UPDATE purchases SET provider=? WHERE id=?", (key, int(purchase_id)))
        db.conn.commit()


def schedule_provider_retry(purchase_id: int, error: str) -> dict[str, Any]:
    with db.LOCK:
        db.conn.execute("BEGIN IMMEDIATE")
        db.cur.execute("SELECT * FROM provider_jobs WHERE purchase_id=?", (int(purchase_id),))
        job = db.cur.fetchone()
        if not job:
            db.conn.rollback()
            return {"scheduled": False, "final": True}
        retry_count = int(job["retry_count"] or 0) + 1
        max_retries = max(1, int(job["max_retries"] or 3))
        if retry_count < max_retries:
            delay = ORDER_RETRY_DELAYS[min(retry_count - 1, len(ORDER_RETRY_DELAYS) - 1)]
            db.cur.execute(
                """
                UPDATE provider_jobs SET status='retry',retry_count=?,next_retry_at=datetime('now',?),last_error=?,locked_at=NULL,
                    updated_at=datetime('now') WHERE purchase_id=?
                """,
                (retry_count, f"+{delay} seconds", (error or "")[:1000], int(purchase_id)),
            )
            db.cur.execute(
                """UPDATE purchases SET status='retry',retry_count=?,next_retry_at=datetime('now',?),provision_error=?
                   WHERE id=?""",
                (retry_count, f"+{delay} seconds", (error or "")[:1000], int(purchase_id)),
            )
            db.conn.commit()
            return {"scheduled": True, "final": False, "retry_count": retry_count, "delay": delay}
        db.cur.execute(
            """UPDATE provider_jobs SET status='admin_review',retry_count=?,last_error=?,locked_at=NULL,next_retry_at=NULL,
               updated_at=datetime('now') WHERE purchase_id=?""",
            (retry_count, (error or "")[:1000], int(purchase_id)),
        )
        db.cur.execute(
            """UPDATE purchases SET status='admin_review',retry_count=?,review_required=1,provision_error=?,next_retry_at=NULL
               WHERE id=?""",
            (retry_count, (error or "")[:1000], int(purchase_id)),
        )
        db.conn.commit()
        return {"scheduled": False, "final": True, "retry_count": retry_count}


def due_provider_jobs(limit: int = 20):
    db.cur.execute(
        """
        SELECT j.*,p.user_id,p.plan_id,p.quantity,p.amount
        FROM provider_jobs j JOIN purchases p ON p.id=j.purchase_id
        WHERE (j.status='pending' OR (j.status='retry' AND j.next_retry_at<=datetime('now'))
               OR (j.status='processing' AND j.locked_at<datetime('now','-5 minutes')))
          AND p.status!='completed'
        ORDER BY j.purchase_id LIMIT ?
        """,
        (max(1, min(100, int(limit))),),
    )
    return db.cur.fetchall()


def list_order_queue(limit: int = 30):
    db.cur.execute(
        """
        SELECT p.*,pl.title AS plan_title,u.username,j.active_provider,j.primary_provider,j.fallback_provider,j.last_error AS job_error
        FROM purchases p
        LEFT JOIN plans pl ON pl.id=p.plan_id
        LEFT JOIN users u ON u.id=p.user_id
        LEFT JOIN provider_jobs j ON j.purchase_id=p.id
        WHERE p.status IN ('paid','provisioning','retry','admin_review') OR COALESCE(p.review_required,0)=1
        ORDER BY CASE p.status WHEN 'admin_review' THEN 0 WHEN 'retry' THEN 1 WHEN 'provisioning' THEN 2 ELSE 3 END,p.id DESC
        LIMIT ?
        """,
        (max(1, min(100, int(limit))),),
    )
    return db.cur.fetchall()


def mark_provider_purchase_completed(purchase_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    # db.finalize_provider_purchase already handles idempotent re-entry.
    result = db.finalize_provider_purchase(int(purchase_id), items)
    purchase = result["purchase"]
    db.cur.execute("UPDATE provider_jobs SET status='completed',locked_at=NULL,next_retry_at=NULL,updated_at=datetime('now') WHERE purchase_id=?", (int(purchase_id),))
    db.cur.execute("UPDATE purchases SET retry_count=COALESCE(retry_count,0),review_required=0 WHERE id=?", (int(purchase_id),))
    db.conn.commit()
    increment_provider_services(purchase["provider"] or "provider", len(items))
    return result


def refund_purchase_once(purchase_id: int, error: str, *, review_required: bool = True) -> tuple[bool, str, dict[str, Any] | None]:
    purchase_id = int(purchase_id)
    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            db.cur.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,))
            purchase = db.cur.fetchone()
            if not purchase:
                db.conn.rollback()
                return False, "not_found", None
            if purchase["refunded_at"]:
                db.conn.commit()
                return True, "already_refunded", dict(purchase)
            if purchase["status"] == "completed":
                db.conn.rollback()
                return False, "already_completed", dict(purchase)
            db.cur.execute("SELECT * FROM users WHERE id=?", (purchase["user_id"],))
            user = db.cur.fetchone()
            if not user:
                db.conn.rollback()
                return False, "user_not_found", dict(purchase)
            before = int(user["balance"] or 0)
            amount = int(purchase["amount"] or 0)
            after = before + amount
            db.cur.execute("UPDATE users SET balance=? WHERE id=?", (after, purchase["user_id"]))
            db.cur.execute(
                "INSERT INTO ledger(user_id,action,amount,balance_before,balance_after,note,is_test) VALUES (?,?,?,?,?,?,?)",
                (purchase["user_id"], "purchase_refund", amount, before, after,
                 f"purchase_id={purchase_id};error={(error or '')[:250]}", int(purchase["is_test"] or 0)),
            )
            db.cur.execute(
                """UPDATE purchases SET status='refunded',provision_error=?,refunded_at=datetime('now'),review_required=? WHERE id=?""",
                ((error or "")[:1000], 1 if review_required else 0, purchase_id),
            )
            if purchase["discount_id"]:
                db.cur.execute(
                    "DELETE FROM discount_redemptions WHERE discount_id=? AND purchase_id=?",
                    (int(purchase["discount_id"]), purchase_id),
                )
                if db.cur.rowcount:
                    db.cur.execute(
                        "UPDATE discounts SET used_count=MAX(0,used_count-1),updated_at=datetime('now') WHERE id=?",
                        (int(purchase["discount_id"]),),
                    )
            db.cur.execute(
                """UPDATE provider_jobs SET status='admin_review',last_error=?,locked_at=NULL,next_retry_at=NULL,updated_at=datetime('now')
                   WHERE purchase_id=?""",
                ((error or "")[:1000], purchase_id),
            )
            db.conn.commit()
            return True, "refunded", dict(purchase)
        except Exception:
            db.conn.rollback()
            raise



def claim_purchase_notification(purchase_id: int, kind: str) -> bool:
    column = "delivery_notified_at" if kind == "delivery" else "refund_notified_at"
    with db.LOCK:
        db.cur.execute(
            f"UPDATE purchases SET {column}=datetime('now') WHERE id=? AND {column} IS NULL",
            (int(purchase_id),),
        )
        ok = db.cur.rowcount == 1
        db.conn.commit()
        return ok

def release_purchase_notification(purchase_id: int, kind: str) -> None:
    """Release a notification claim only when the primary user message could not be sent."""
    column = "delivery_notified_at" if kind == "delivery" else "refund_notified_at"
    with db.LOCK:
        db.cur.execute(
            f"UPDATE purchases SET {column}=NULL WHERE id=?",
            (int(purchase_id),),
        )
        db.conn.commit()

def get_order_detail(purchase_id: int):
    db.cur.execute(
        """SELECT p.*,pl.title AS plan_title,u.username,j.active_provider,j.primary_provider,j.fallback_provider,j.last_error AS job_error
           FROM purchases p LEFT JOIN plans pl ON pl.id=p.plan_id LEFT JOIN users u ON u.id=p.user_id
           LEFT JOIN provider_jobs j ON j.purchase_id=p.id WHERE p.id=?""",
        (int(purchase_id),),
    )
    return db.cur.fetchone()


def create_service_issue(user_id: str, service_id: int, issue_type: str) -> tuple[int, dict[str, Any]]:
    db.cur.execute(
        """
        SELECT s.*,p.id AS order_id,p.status AS order_status,
               p.provision_error AS last_provider_error,pl.title AS plan_title
        FROM subs s LEFT JOIN purchases p ON p.id=s.purchase_id LEFT JOIN plans pl ON pl.id=s.plan_id
        WHERE s.id=? AND s.owner=? AND s.used=1
        """,
        (int(service_id), str(user_id)),
    )
    service = db.cur.fetchone()
    if not service:
        raise ValueError("service_not_found")
    snapshot = {
        "service_id": int(service["id"]),
        "order_id": service["order_id"],
        "plan": service["plan_title"] or "-",
        "status": service["panel_status"] or service["status"] or "-",
        "used_traffic": int(service["panel_used_traffic"] or 0),
        "expires_at": service["panel_expires_at"],
        "provider": service["panel_provider"] or service["source_type"] or "pool",
        "last_provider_error": redact_sensitive_text(service["last_provider_error"], 300),
    }
    db.cur.execute(
        "INSERT INTO tickets(user_id,service_id,issue_type,snapshot_json) VALUES (?,?,?,?)",
        (str(user_id), int(service_id), (issue_type or "other")[:80], _json(snapshot)),
    )
    db.conn.commit()
    return int(db.cur.lastrowid), snapshot


def get_service_issue(ticket_id: int):
    db.cur.execute("SELECT * FROM tickets WHERE id=?", (int(ticket_id),))
    return db.cur.fetchone()


def sales_overview() -> dict[str, int]:
    result = {}
    periods = {"today": "date(created_at)=date('now')", "week": "created_at>=datetime('now','-7 days')", "month": "created_at>=datetime('now','-30 days')", "all": "1=1"}
    for key, where in periods.items():
        db.cur.execute(f"SELECT COALESCE(SUM(amount),0) AS revenue,COUNT(*) AS orders FROM purchases WHERE status='completed' AND COALESCE(is_test,0)=0 AND {where}")
        row = db.cur.fetchone()
        result[f"{key}_revenue"] = int(row["revenue"] or 0)
        result[f"{key}_orders"] = int(row["orders"] or 0)
    db.cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(amount),0) AS s FROM purchases WHERE status IN ('failed','admin_review','refunded') AND COALESCE(is_test,0)=0")
    row = db.cur.fetchone()
    result["problem_orders"] = int(row["c"] or 0)
    result["problem_amount"] = int(row["s"] or 0)
    db.cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(amount),0) AS s FROM purchases WHERE status='refunded' AND COALESCE(is_test,0)=0")
    row = db.cur.fetchone()
    result["refund_orders"] = int(row["c"] or 0)
    result["refund_amount"] = int(row["s"] or 0)
    return result


def plan_performance(limit: int = 20):
    # Aggregate purchases and active services separately. Joining their raw rows
    # would multiply revenue by the number of active subscriptions.
    db.cur.execute(
        """
        SELECT pl.id,pl.title,pl.cost_price,
               COALESCE(sa.orders,0) AS orders,
               COALESCE(sa.units,0) AS units,
               COALESCE(sa.revenue,0) AS revenue,
               COALESCE(sa.revenue,0)-(COALESCE(pl.cost_price,0)*COALESCE(sa.units,0)) AS estimated_profit,
               COALESCE(ss.active_customers,0) AS active_customers
        FROM plans pl
        LEFT JOIN (
            SELECT plan_id,COUNT(*) AS orders,COALESCE(SUM(quantity),0) AS units,
                   COALESCE(SUM(amount),0) AS revenue
            FROM purchases
            WHERE status='completed' AND COALESCE(is_test,0)=0
            GROUP BY plan_id
        ) sa ON sa.plan_id=pl.id
        LEFT JOIN (
            SELECT plan_id,COUNT(DISTINCT owner) AS active_customers
            FROM subs
            WHERE used=1 AND COALESCE(is_trial,0)=0
            GROUP BY plan_id
        ) ss ON ss.plan_id=pl.id
        ORDER BY revenue DESC,pl.id LIMIT ?
        """,
        (max(1, min(100, int(limit))),),
    )
    return db.cur.fetchall()


def category_performance(limit: int = 20):
    db.cur.execute(
        """
        SELECT c.id,c.title,c.emoji,COUNT(p.id) AS orders,COALESCE(SUM(p.quantity),0) AS units,COALESCE(SUM(p.amount),0) AS revenue
        FROM plan_categories c LEFT JOIN plans pl ON pl.category_id=c.id
        LEFT JOIN purchases p ON p.plan_id=pl.id AND p.status='completed' AND COALESCE(p.is_test,0)=0
        GROUP BY c.id ORDER BY revenue DESC,c.sort_order LIMIT ?
        """,
        (max(1, min(100, int(limit))),),
    )
    return db.cur.fetchall()


def trial_conversion(days: int) -> dict[str, Any]:
    days = max(1, int(days))
    db.cur.execute("SELECT COUNT(*) AS c FROM trial_claims WHERE status='completed'")
    trials = int(db.cur.fetchone()["c"] or 0)
    db.cur.execute(
        """
        SELECT COUNT(DISTINCT t.user_id) AS c
        FROM trial_claims t JOIN purchases p ON p.user_id=t.user_id AND p.status='completed' AND COALESCE(p.is_test,0)=0
        WHERE t.status='completed' AND p.created_at>=t.updated_at AND p.created_at<=datetime(t.updated_at, ?)
        """,
        (f"+{days} days",),
    )
    converted = int(db.cur.fetchone()["c"] or 0)
    return {"days": days, "trials": trials, "converted": converted, "rate": round(converted * 100 / trials, 1) if trials else 0.0}


def top_customers(limit: int = 10):
    db.cur.execute(
        """
        SELECT u.id,u.username,u.display_name,COUNT(p.id) AS orders,COALESCE(SUM(p.quantity),0) AS units,COALESCE(SUM(p.amount),0) AS spent
        FROM users u JOIN purchases p ON p.user_id=u.id AND p.status='completed' AND COALESCE(p.is_test,0)=0
        GROUP BY u.id ORDER BY spent DESC,orders DESC LIMIT ?
        """,
        (max(1, min(50, int(limit))),),
    )
    return db.cur.fetchall()


def inventory_report():
    db.cur.execute(
        """
        SELECT pl.id,pl.title,
          SUM(CASE WHEN COALESCE(s.source_type,'pool')='pool' AND s.used=0 THEN 1 ELSE 0 END) AS pool_stock,
          SUM(CASE WHEN COALESCE(s.source_type,'pool')='pool' AND s.used=1 THEN 1 ELSE 0 END) AS pool_sold,
          SUM(CASE WHEN COALESCE(s.source_type,'pool')!='pool' AND s.used=1 THEN 1 ELSE 0 END) AS provider_services
        FROM plans pl LEFT JOIN subs s ON s.plan_id=pl.id GROUP BY pl.id ORDER BY pl.sort_order,pl.id
        """
    )
    return db.cur.fetchall()


def list_discounts(limit: int = 50):
    db.cur.execute("SELECT * FROM discounts ORDER BY id DESC LIMIT ?", (max(1, min(100, int(limit))),))
    return db.cur.fetchall()


def create_discount(data: dict[str, Any]) -> int:
    code = re.sub(r"\s+", "", str(data.get("code") or "")).upper()[:32]
    if not code:
        raise ValueError("کد تخفیف خالی است.")
    kind = str(data.get("discount_type") or "percent").lower()
    if kind not in {"percent", "fixed", "free", "bonus_volume"}:
        raise ValueError("نوع تخفیف معتبر نیست.")
    db.cur.execute(
        """
        INSERT INTO discounts(code,title,discount_type,value,max_uses,per_user_limit,starts_at,ends_at,min_amount,
          category_id,plan_id,first_purchase_only,new_users_only,renewals_only,is_active)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (code, str(data.get("title") or "")[:100], kind, max(0, int(data.get("value") or 0)),
         max(0, int(data.get("max_uses") or 0)), max(1, int(data.get("per_user_limit") or 1)),
         data.get("starts_at") or None, data.get("ends_at") or None, max(0, int(data.get("min_amount") or 0)),
         int(data["category_id"]) if data.get("category_id") else None, int(data["plan_id"]) if data.get("plan_id") else None,
         1 if data.get("first_purchase_only") else 0, 1 if data.get("new_users_only") else 0,
         1 if data.get("renewals_only") else 0, 1 if data.get("is_active", True) else 0),
    )
    db.conn.commit()
    return int(db.cur.lastrowid)


def toggle_discount(discount_id: int) -> bool:
    db.cur.execute("UPDATE discounts SET is_active=CASE is_active WHEN 1 THEN 0 ELSE 1 END,updated_at=datetime('now') WHERE id=?", (int(discount_id),))
    db.conn.commit()
    return db.cur.rowcount == 1


def list_campaigns(limit: int = 50):
    db.cur.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT ?", (max(1, min(100, int(limit))),))
    return db.cur.fetchall()


def create_campaign(title: str, inactivity_days: int, message_text: str, discount_id: int | None = None) -> int:
    db.cur.execute(
        "INSERT INTO campaigns(title,inactivity_days,message_text,discount_id) VALUES (?,?,?,?)",
        ((title or "کمپین بازگشت")[:100], max(1, int(inactivity_days)), (message_text or "")[:3500], int(discount_id) if discount_id else None),
    )
    db.conn.commit()
    return int(db.cur.lastrowid)


def toggle_campaign(campaign_id: int) -> bool:
    db.cur.execute("UPDATE campaigns SET is_active=CASE is_active WHEN 1 THEN 0 ELSE 1 END,updated_at=datetime('now') WHERE id=?", (int(campaign_id),))
    db.conn.commit()
    return db.cur.rowcount == 1


def due_campaign_recipients(limit_per_campaign: int = 100):
    db.cur.execute(
        """SELECT * FROM campaigns WHERE is_active=1 AND (starts_at IS NULL OR datetime(starts_at)<=datetime('now'))
           AND (ends_at IS NULL OR datetime(ends_at)>=datetime('now')) ORDER BY id"""
    )
    campaigns = db.cur.fetchall()
    result = []
    for campaign in campaigns:
        days = max(1, int(campaign["inactivity_days"] or 7))
        db.cur.execute(
            """
            SELECT u.id,u.username FROM users u
            WHERE COALESCE(u.banned,0)=0
              AND NOT EXISTS(SELECT 1 FROM campaign_deliveries d WHERE d.campaign_id=? AND d.user_id=u.id)
              AND EXISTS(SELECT 1 FROM purchases p0 WHERE p0.user_id=u.id AND p0.status='completed')
              AND NOT EXISTS(SELECT 1 FROM purchases p WHERE p.user_id=u.id AND p.status='completed' AND p.created_at>=datetime('now',?))
            ORDER BY u.last_active DESC LIMIT ?
            """,
            (campaign["id"], f"-{days} days", max(1, min(500, int(limit_per_campaign)))),
        )
        for user in db.cur.fetchall():
            result.append((campaign, user))
    return result


def record_campaign_delivery(campaign_id: int, user_id: str, status: str = "sent", error: str = "") -> None:
    db.cur.execute(
        "INSERT OR REPLACE INTO campaign_deliveries(campaign_id,user_id,status,error,sent_at) VALUES (?,?,?,?,datetime('now'))",
        (int(campaign_id), str(user_id), status, (error or "")[:500]),
    )
    db.cur.execute("UPDATE campaigns SET last_run_at=datetime('now') WHERE id=?", (int(campaign_id),))
    db.conn.commit()


def list_plan_templates(active_only: bool = False):
    sql = "SELECT * FROM plan_text_templates"
    if active_only:
        sql += " WHERE is_active=1"
    sql += " ORDER BY is_system DESC,id"
    db.cur.execute(sql)
    return db.cur.fetchall()


def get_plan_template(template_id: int):
    db.cur.execute("SELECT * FROM plan_text_templates WHERE id=?", (int(template_id),))
    return db.cur.fetchone()


def create_plan_template(title: str, body: str) -> int:
    validate_template(body)
    db.cur.execute("INSERT INTO plan_text_templates(title,body,is_system,is_active) VALUES (?,?,0,1)", ((title or "قالب جدید")[:100], body))
    db.conn.commit()
    return int(db.cur.lastrowid)


def update_plan_template(template_id: int, body: str) -> bool:
    validate_template(body)
    db.cur.execute("UPDATE plan_text_templates SET body=?,updated_at=datetime('now') WHERE id=?", (body, int(template_id)))
    db.conn.commit()
    return db.cur.rowcount == 1


def copy_plan_template(template_id: int) -> int:
    row = get_plan_template(template_id)
    if not row:
        raise ValueError("قالب پیدا نشد.")
    return create_plan_template(f"کپی {row['title']}", row["body"])


def toggle_plan_template(template_id: int) -> bool:
    db.cur.execute("UPDATE plan_text_templates SET is_active=CASE is_active WHEN 1 THEN 0 ELSE 1 END,updated_at=datetime('now') WHERE id=?", (int(template_id),))
    db.conn.commit()
    return db.cur.rowcount == 1


def restore_system_template(template_id: int) -> bool:
    row = get_plan_template(template_id)
    if not row or not int(row["is_system"] or 0):
        return False
    default = DEFAULT_PLAN_TEMPLATES.get(row["template_key"])
    if not default:
        return False
    db.cur.execute("UPDATE plan_text_templates SET title=?,body=?,is_active=1,updated_at=datetime('now') WHERE id=?", (default[0], default[1], int(template_id)))
    db.conn.commit()
    return True


def set_default_plan_template(template_id: int | None) -> None:
    db.set_setting("default_plan_template_id", str(int(template_id)) if template_id else "")


def set_category_template(category_id: int, template_id: int | None) -> None:
    db.cur.execute("UPDATE plan_categories SET template_id=?,updated_at=datetime('now') WHERE id=?", (int(template_id) if template_id else None, int(category_id)))
    db.conn.commit()


def set_plan_template(plan_id: int, template_id: int | None) -> None:
    db.cur.execute("UPDATE plans SET template_id=?,updated_at=datetime('now') WHERE id=?", (int(template_id) if template_id else None, int(plan_id)))
    db.conn.commit()


class _TelegramHTMLValidator(HTMLParser):
    _VOID_TAGS = {"br"}
    _ALLOWED_TAGS = {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote", "br",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in self._ALLOWED_TAGS:
            raise ValueError(f"تگ HTML مجاز نیست: <{tag}>")
        if tag not in self._VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() not in self._ALLOWED_TAGS:
            raise ValueError(f"تگ HTML مجاز نیست: <{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            expected = self.stack[-1] if self.stack else "-"
            raise ValueError(f"ترتیب بسته‌شدن HTML خراب است؛ انتظار </{expected}> بود.")
        self.stack.pop()

    def finish(self):
        self.close()
        if self.stack:
            raise ValueError(f"تگ HTML بسته نشده است: <{self.stack[-1]}>")


def validate_template(body: str) -> None:
    body = str(body or "").strip()
    if not body:
        raise ValueError("متن قالب خالی است.")
    if len(body) > 3900:
        raise ValueError("متن قالب از محدودیت امن تلگرام عبور می‌کند.")
    fields = set(re.findall(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})", body))
    unknown = fields - ALLOWED_TEMPLATE_FIELDS
    if unknown:
        raise ValueError("متغیر ناشناخته: " + ", ".join(sorted(unknown)))
    missing = REQUIRED_TEMPLATE_FIELDS - fields
    if missing:
        raise ValueError("متغیر ضروری حذف شده: " + ", ".join(sorted(missing)))
    if "<" in body or ">" in body:
        if body.count("<") != body.count(">"):
            raise ValueError("ساختار HTML نامتوازن است.")
        parser = _TelegramHTMLValidator()
        try:
            parser.feed(body)
            parser.finish()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("ساختار HTML معتبر نیست.") from exc
    if body.count("```") % 2:
        raise ValueError("بلوک Markdown بسته نشده است.")
    without_blocks = body.replace("```", "")
    if without_blocks.count("`") % 2:
        raise ValueError("کد Markdown بسته نشده است.")


class _SafeFormat(dict):
    def __missing__(self, key):
        return "-"


def _template_context(plan, category=None, service: dict[str, Any] | None = None) -> dict[str, str]:
    category_title = category["title"] if category else "-"
    provider_key = db.plan_provider_key(plan)
    delivery = "آماده" if provider_key == "pool" else "خودکار"
    devices = "نامحدود" if plan["panel_max_devices"] in (None, "", 0) else str(plan["panel_max_devices"])
    start_mode = "اولین اتصال" if (plan["panel_start_mode"] or "on_hold") == "on_hold" else "فوری"
    data = {
        "title": str(plan["title"] or "-"),
        "category": str(category_title or "-"),
        "volume": str(plan["volume_label"] or "-"),
        "duration": str(plan["duration_label"] or "-"),
        "price": f"{int(plan['price'] or 0):,}",
        "devices": devices,
        "delivery": delivery,
        "start_mode": start_mode,
        "description": str(plan["description"] or ""),
        "tag": str(plan["tag"] or ""),
        "username": "-",
        "subscription_url": "-",
        "expire_date": "-",
    }
    if service:
        data.update({
            "username": str(service.get("panel_username") or service.get("account_name") or "-"),
            "subscription_url": str(service.get("link") or service.get("subscription_url") or "-"),
            "expire_date": str(service.get("expire_date") or service.get("panel_expires_at") or "-"),
        })
    return data


def resolve_template_for_plan(plan):
    template_id = plan["template_id"] if "template_id" in plan.keys() else None
    category = db.get_plan_category(plan["category_id"]) if plan["category_id"] else None
    if not template_id and category and "template_id" in category.keys():
        template_id = category["template_id"]
    if not template_id:
        raw = db.get_setting("default_plan_template_id", "")
        template_id = int(raw) if str(raw).isdigit() else None
    if template_id:
        row = get_plan_template(int(template_id))
        if row and int(row["is_active"] or 0):
            return row, category
    db.cur.execute("SELECT * FROM plan_text_templates WHERE template_key='professional' LIMIT 1")
    return db.cur.fetchone(), category


def render_plan_text(plan, *, service: dict[str, Any] | None = None) -> str:
    template, category = resolve_template_for_plan(plan)
    body = template["body"] if template else DEFAULT_PLAN_TEMPLATES["professional"][1]
    try:
        validate_template(body)
    except ValueError:
        body = DEFAULT_PLAN_TEMPLATES["professional"][1]
    return body.format_map(_SafeFormat(_template_context(plan, category, service))).strip()


def template_preview(template_id: int, plan_id: int | None = None) -> str:
    template = get_plan_template(template_id)
    if not template:
        raise ValueError("قالب پیدا نشد.")
    plan = db.get_plan(plan_id) if plan_id else None
    if not plan:
        plans = db.list_plans(active_only=False, limit=1)
        plan = plans[0] if plans else None
    if not plan:
        sample = {
            "title": "اقتصادی 50GB", "category": "اقتصادی", "volume": "50GB", "duration": "30 روز",
            "price": "180,000", "devices": "2", "delivery": "خودکار", "start_mode": "اولین اتصال",
            "username": "bsv-sample", "subscription_url": "https://example.invalid/sub", "expire_date": "30 روز دیگر",
            "description": "سرویس نمونه برای پیش‌نمایش", "tag": "پرفروش",
        }
        return template["body"].format_map(_SafeFormat(sample))
    category = db.get_plan_category(plan["category_id"]) if plan["category_id"] else None
    return template["body"].format_map(_SafeFormat(_template_context(plan, category)))
