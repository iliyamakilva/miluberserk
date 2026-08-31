"""Safe SQLite backup, inspection, retention, and restore helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import db
from config import (
    BACKUP_INTERVAL_SECONDS,
    BACKUP_RETENTION_COUNT,
    DB_PATH,
)

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups")).expanduser()
REQUIRED_SCHEMA = {
    "users": {"id", "balance", "purchased", "banned"},
    "subs": {"id", "link", "used", "owner"},
    "settings": {"key", "value"},
    "topups": {"id", "user_id", "amount", "status"},
    "ledger": {"id", "user_id", "action", "amount"},
}


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _query_count(cursor, sql):
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
        return _safe_int(row[0] if row else 0)
    except sqlite3.DatabaseError:
        return 0


def backup_file_name(prefix="backup", info=None):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    suffix = ""
    if info:
        suffix = f"_users-{info.get('users', 0)}_subs-{info.get('subs', 0)}"
    return f"berserk_{prefix}_{timestamp}{suffix}.db"


def inspect_sqlite_file(path):
    path = str(path)
    result = {
        "path": path,
        "file_name": os.path.basename(path),
        "file_size": os.path.getsize(path) if os.path.exists(path) else 0,
        "ok": False,
        "integrity_ok": False,
        "errors": [],
        "warnings": [],
        "tables": [],
        "counts": {},
        "schema_version": None,
    }

    if not os.path.exists(path):
        result["errors"].append("فایل وجود ندارد.")
        return result
    if result["file_size"] <= 0:
        result["errors"].append("فایل خالی است.")
        return result

    connection = None
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        cursor = connection.cursor()

        cursor.execute("PRAGMA integrity_check")
        integrity = cursor.fetchone()
        if integrity and str(integrity[0]).lower() == "ok":
            result["integrity_ok"] = True
        else:
            result["errors"].append(
                f"integrity_check ناموفق: {integrity[0] if integrity else '-'}"
            )

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        result["tables"] = sorted(tables)

        for table, required_columns in REQUIRED_SCHEMA.items():
            if table not in tables:
                result["errors"].append(f"جدول ضروری {table} وجود ندارد.")
                continue
            cursor.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in cursor.fetchall()}
            missing = required_columns - columns
            if missing:
                result["errors"].append(
                    f"ستون‌های ضروری جدول {table} ناقص است: {', '.join(sorted(missing))}"
                )

        if "settings" in tables:
            try:
                cursor.execute("SELECT value FROM settings WHERE key='schema_version'")
                row = cursor.fetchone()
                result["schema_version"] = _safe_int(row[0]) if row else None
            except sqlite3.DatabaseError:
                pass

        if result["schema_version"] is not None and result["schema_version"] >= 620:
            catalog_schema = {
                "plan_categories": {"id", "title", "sort_order", "is_active", "audience"},
                "admin_menu_items": {"key", "title", "callback_data", "sort_order", "is_active"},
                "plans": {"category_id", "purchase_mode", "provider_key", "provider_options_json"},
                "trial_claims": {"provider_key"},
            }
            for table, required_columns in catalog_schema.items():
                if table not in tables:
                    result["errors"].append(f"جدول ضروری نسخه 620 یعنی {table} وجود ندارد.")
                    continue
                cursor.execute(f"PRAGMA table_info({table})")
                columns = {row[1] for row in cursor.fetchall()}
                missing = required_columns - columns
                if missing:
                    result["errors"].append(
                        f"ستون‌های نسخه 620 جدول {table} ناقص است: {', '.join(sorted(missing))}"
                    )

        if result["schema_version"] is not None and result["schema_version"] >= 630:
            reliability_schema = {
                "provider_states": {"provider_key", "is_sales_enabled", "last_status", "capabilities_json"},
                "provider_logs": {"provider_key", "operation", "result", "created_at"},
                "provider_jobs": {"purchase_id", "active_provider", "status", "retry_count", "next_retry_at"},
                "provider_job_items": {"purchase_id", "item_index", "provider_username", "status"},
                "discounts": {"code", "discount_type", "value", "is_active"},
                "discount_redemptions": {"discount_id", "user_id", "purchase_id", "amount"},
                "campaigns": {"title", "inactivity_days", "message_text", "is_active"},
                "campaign_deliveries": {"campaign_id", "user_id", "status", "sent_at"},
                "plan_text_templates": {"title", "body", "is_system", "is_active"},
                "plans": {"fallback_provider_key", "template_id"},
                "plan_categories": {"template_id"},
                "purchases": {"subtotal_amount", "discount_amount", "retry_count", "review_required", "request_key"},
                "topups": {"discount_code", "request_key"},
                "tickets": {"service_id", "issue_type", "snapshot_json"},
            }
            for table, required_columns in reliability_schema.items():
                if table not in tables:
                    result["errors"].append(f"جدول ضروری نسخه 630 یعنی {table} وجود ندارد.")
                    continue
                cursor.execute(f"PRAGMA table_info({table})")
                columns = {row[1] for row in cursor.fetchall()}
                missing = required_columns - columns
                if missing:
                    result["errors"].append(
                        f"ستون‌های نسخه 630 جدول {table} ناقص است: {', '.join(sorted(missing))}"
                    )

        if result["schema_version"] is not None and result["schema_version"] >= 640:
            content_schema = {
                "content_templates": {"slot_key", "scope_type", "scope_id", "published_text", "draft_text", "parse_mode", "is_active"},
                "content_template_versions": {"template_id", "slot_key", "scope_type", "scope_id", "text_value", "action", "created_at"},
                "content_display_settings": {"scope_type", "scope_id", "settings_json", "updated_at"},
                "purchase_funnel_events": {"user_id", "event_type", "category_id", "plan_id", "purchase_id", "created_at"},
            }
            for table, required_columns in content_schema.items():
                if table not in tables:
                    result["errors"].append(f"جدول ضروری نسخه 640 یعنی {table} وجود ندارد.")
                    continue
                cursor.execute(f"PRAGMA table_info({table})")
                columns = {row[1] for row in cursor.fetchall()}
                missing = required_columns - columns
                if missing:
                    result["errors"].append(
                        f"ستون‌های نسخه 640 جدول {table} ناقص است: {', '.join(sorted(missing))}"
                    )

        if result["schema_version"] is None:
            result["warnings"].append(
                "این بک‌آپ قدیمی است و schema_version ندارد؛ migration هنگام اجرای ربات انجام می‌شود."
            )
        elif result["schema_version"] > db.SCHEMA_VERSION:
            result["errors"].append(
                "نسخه دیتابیس بک‌آپ از نسخه فعلی ربات جدیدتر است؛ برای جلوگیری از downgrade ناامن، ری‌استور متوقف شد."
            )

        result["counts"] = {
            "users": _query_count(cursor, "SELECT COUNT(*) FROM users"),
            "test_users": _query_count(
                cursor,
                "SELECT COUNT(*) FROM users WHERE COALESCE(is_test,0)=1",
            ),
            "subs": _query_count(cursor, "SELECT COUNT(*) FROM subs"),
            "subs_available": _query_count(cursor, "SELECT COUNT(*) FROM subs WHERE used=0"),
            "subs_sold": _query_count(cursor, "SELECT COUNT(*) FROM subs WHERE used=1"),
            "topups": _query_count(cursor, "SELECT COUNT(*) FROM topups"),
            "purchases": _query_count(cursor, "SELECT COUNT(*) FROM purchases"),
            "ledger": _query_count(cursor, "SELECT COUNT(*) FROM ledger"),
            "plans": _query_count(cursor, "SELECT COUNT(*) FROM plans"),
            "plan_categories": _query_count(cursor, "SELECT COUNT(*) FROM plan_categories"),
            "trial_claims": _query_count(cursor, "SELECT COUNT(*) FROM trial_claims"),
            "content_templates": _query_count(cursor, "SELECT COUNT(*) FROM content_templates"),
            "content_versions": _query_count(cursor, "SELECT COUNT(*) FROM content_template_versions"),
            "funnel_events": _query_count(cursor, "SELECT COUNT(*) FROM purchase_funnel_events"),
            "provider_services": _query_count(cursor, "SELECT COUNT(*) FROM subs WHERE COALESCE(source_type,'pool')!='pool"),
            "custom_buttons": _query_count(cursor, "SELECT COUNT(*) FROM custom_buttons"),
        }
        result["ok"] = result["integrity_ok"] and not result["errors"]
        return result
    except sqlite3.DatabaseError as exc:
        result["errors"].append(f"فایل SQLite معتبر نیست: {exc}")
        return result
    except OSError as exc:
        result["errors"].append(f"خطا در خواندن فایل: {exc}")
        return result
    finally:
        if connection is not None:
            connection.close()


def format_backup_info(info):
    status = "✅ سالم" if info.get("ok") else "❌ ناسالم / مشکوک"
    counts = info.get("counts") or {}
    lines = [
        "📦 اطلاعات فایل بک‌آپ",
        "",
        f"نام فایل: {info.get('file_name', '-')}",
        f"حجم فایل: {int(info.get('file_size') or 0):,} بایت",
        f"وضعیت سلامت: {status}",
        f"integrity_check: {'OK' if info.get('integrity_ok') else 'Failed'}",
        f"نسخه دیتابیس: {info.get('schema_version') or 'قدیمی/نامشخص'}",
        "",
        "📊 آمار داخل فایل:",
        f"کاربران: {counts.get('users', 0)} (تست: {counts.get('test_users', 0)})",
        f"کل سرویس‌ها: {counts.get('subs', 0)}",
        f"سرویس‌های آزاد: {counts.get('subs_available', 0)}",
        f"سرویس‌های تحویل‌شده: {counts.get('subs_sold', 0)}",
        f"دسته‌ها / پلن‌ها: {counts.get('plan_categories', 0)} / {counts.get('plans', 0)}",
        f"سرویس‌های تأمین‌کننده: {counts.get('provider_services', 0)}",
        f"اکانت‌های تست: {counts.get('trial_claims', 0)}",
        f"شارژها: {counts.get('topups', 0)}",
        f"خریدها: {counts.get('purchases', 0)}",
        f"تراکنش‌های کیف پول: {counts.get('ledger', 0)}",
        f"دکمه‌های اختصاصی: {counts.get('custom_buttons', 0)}",
    ]

    if info.get("errors"):
        lines.append("\n❌ خطاها:")
        lines.extend(f"• {error}" for error in info["errors"])
    if info.get("warnings"):
        lines.append("\n⚠️ هشدارها:")
        lines.extend(f"• {warning}" for warning in info["warnings"])
    return "\n".join(lines)


def _prune_backups(limit=BACKUP_RETENTION_COUNT):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in files[int(limit) :]:
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove old backup %s", path)


def create_backup_file(prefix="backup", admin_id=None, note=""):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = BACKUP_DIR / backup_file_name(f"{prefix}_tmp")
    destination = sqlite3.connect(str(temporary_path))
    try:
        with db.LOCK:
            db.conn.backup(destination)
    finally:
        destination.close()

    info = inspect_sqlite_file(str(temporary_path))
    if not info.get("ok"):
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise RuntimeError("بک‌آپ ساخته شد اما تست سلامت آن ناموفق بود.")

    final_path = BACKUP_DIR / backup_file_name(prefix, info.get("counts") or {})
    if final_path != temporary_path:
        try:
            temporary_path.replace(final_path)
        except OSError:
            final_path = temporary_path

    db.log_backup_operation(
        admin_id,
        f"create_{prefix}",
        final_path.name,
        os.path.getsize(final_path),
        "ok",
        note,
    )
    _prune_backups()
    return str(final_path)


async def _send_existing_backup(bot, chat_id, path):
    info = inspect_sqlite_file(path)
    with open(path, "rb") as file_obj:
        await bot.send_document(
            chat_id,
            file_obj,
            caption=(
                "💾 بک‌آپ دیتابیس Berserk VPN\n"
                f"زمان: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"وضعیت: {'سالم' if info.get('ok') else 'نیازمند بررسی'}\n"
                f"کاربران: {info.get('counts', {}).get('users', 0)} | "
                f"سرویس‌ها: {info.get('counts', {}).get('subs', 0)}"
            ),
        )


async def send_backup(bot, chat_id, admin_id=None):
    path = create_backup_file(admin_id=admin_id or chat_id)
    await _send_existing_backup(bot, chat_id, path)
    return path


async def send_backup_to_all_admins(bot, admin_ids):
    admin_ids = list(admin_ids)
    if not admin_ids:
        return None
    path = create_backup_file(prefix="daily", note="scheduled daily backup")
    for admin_id in admin_ids:
        try:
            await _send_existing_backup(bot, admin_id, path)
        except Exception:
            logger.exception("Could not send backup to admin %s", admin_id)
    return path


async def daily_backup_loop(bot, admin_ids, interval_seconds=BACKUP_INTERVAL_SECONDS):
    while True:
        await asyncio.sleep(max(3600, int(interval_seconds)))
        try:
            await send_backup_to_all_admins(bot, admin_ids)
        except Exception:
            logger.exception("Scheduled database backup failed")


def validate_sqlite_file(path):
    return bool(inspect_sqlite_file(path).get("ok"))


def list_local_backups(limit=10):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[: int(limit)]


def _write_restore_log_to_file(path, admin_id, source_name, safety_name):
    try:
        connection = sqlite3.connect(path)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT,
                operation_type TEXT NOT NULL,
                backup_file_name TEXT,
                file_size INTEGER,
                status TEXT DEFAULT 'ok',
                note TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        connection.execute(
            """
            INSERT INTO backup_logs(admin_id, operation_type, backup_file_name, file_size, status, note)
            VALUES (?, 'restore', ?, ?, 'ok', ?)
            """,
            (
                str(admin_id) if admin_id is not None else None,
                source_name,
                os.path.getsize(path),
                f"safety_backup={safety_name}",
            ),
        )
        connection.commit()
        connection.close()
    except sqlite3.DatabaseError:
        logger.exception("Could not append restore log to restored database")


def perform_restore(uploaded_path, admin_id=None):
    """Atomically replace the DB file. The caller must restart the process."""
    uploaded_path = str(uploaded_path)
    info = inspect_sqlite_file(uploaded_path)
    if not info.get("ok"):
        raise ValueError("backup file failed validation")

    safety_path = create_backup_file(
        prefix="pre_restore",
        admin_id=admin_id,
        note="automatic safety backup before restore",
    )

    db_path = Path(DB_PATH).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = db_path.parent / f".{db_path.name}.restore-{uuid4().hex}.tmp"
    shutil.copy2(uploaded_path, staging_path)

    staged_info = inspect_sqlite_file(str(staging_path))
    if not staged_info.get("ok"):
        staging_path.unlink(missing_ok=True)
        raise ValueError("staged backup failed validation")

    _write_restore_log_to_file(
        str(staging_path),
        admin_id,
        os.path.basename(uploaded_path),
        os.path.basename(safety_path),
    )

    try:
        with db.LOCK:
            try:
                db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            db.conn.commit()
            # Railway runs on Linux, where replacing the pathname while the old
            # connection is open is atomic. The process exits immediately after.
            os.replace(staging_path, db_path)
            db.conn.close()
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(db_path) + suffix).unlink()
            except FileNotFoundError:
                pass
        return safety_path
    except Exception:
        try:
            staging_path.unlink()
        except OSError:
            pass
        raise
