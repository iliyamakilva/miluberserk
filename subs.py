"""Subscription-link pool management.

Core invariants:
- an exact link is stored once;
- an available row can be assigned only once;
- returning a service reuses the same row instead of creating a duplicate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time

import aiohttp

import db
import commerce
from config import (
    YOUPANEL_BASE_URL,
    YOUPANEL_INBOUNDS_JSON,
    YOUPANEL_PASSWORD,
    YOUPANEL_TIMEOUT_SECONDS,
    TRIAL_MAX_DEVICES,
    YOUPANEL_USERNAME,
    YOUPANEL_VERIFY_SSL,
    youpanel_configured,
)

logger = logging.getLogger(__name__)


def _generate_account_name():
    return db.generate_service_code()


def get_sub(plan_id=None):
    plan_id = int(plan_id) if plan_id is not None else db.default_plan_id()
    with db.LOCK:
        db.cur.execute(
            """
            SELECT id, link, account_name, status, price_paid, assigned_at,
                   owner, purchase_id, plan_id, source_type, panel_username, is_trial
            FROM subs
            WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool'
            ORDER BY id
            LIMIT 1
            """,
            (plan_id,),
        )
        return db.cur.fetchone()


def get_sub_detail(sub_id):
    with db.LOCK:
        db.cur.execute(
            """
            SELECT id, link, account_name, status, price_paid, assigned_at,
                   owner, purchase_id, used, plan_id, source_type, panel_provider,
                   panel_username, panel_status, panel_data_limit, panel_used_traffic,
                   panel_expires_at, panel_duration_seconds, is_trial, last_synced_at
            FROM subs
            WHERE id=?
            """,
            (int(sub_id),),
        )
        return db.cur.fetchone()


def assign_sub(sub_id, user_id, price_paid=None):
    """Legacy one-link assignment kept for compatibility.

    Normal purchases should use db.complete_purchase(), which records purchase
    and ledger rows atomically.
    """
    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            user = db.get_user(user_id)
            is_test = int(user["is_test"] or 0) if user and "is_test" in user.keys() else 0
            account_name = _generate_account_name()
            db.cur.execute(
                """
                UPDATE subs
                SET used=1,
                    owner=?,
                    assigned_at=datetime('now'),
                    price_paid=?,
                    account_name=COALESCE(NULLIF(account_name, ''), ?),
                    status='delivered'
                WHERE id=? AND used=0
                """,
                (str(user_id), price_paid, account_name, int(sub_id)),
            )
            changed = db.cur.rowcount == 1
            if changed and not is_test:
                db._bump_daily_tx("sales")
            db.conn.commit()
            return changed
        except Exception:
            db.conn.rollback()
            raise


def add_sub(link, plan_id=None):
    link = (link or "").strip()
    if not link:
        return None

    plan_id = int(plan_id) if plan_id is not None else db.default_plan_id()
    plan = db.get_plan(plan_id)
    if not plan:
        raise ValueError("plan not found")
    if db.plan_delivery_type(plan) != "pool":
        raise ValueError("panel plans do not accept pooled links")

    with db.LOCK:
        try:
            db.cur.execute(
                """
                INSERT INTO subs(link, account_name, status, plan_id)
                VALUES (?, ?, 'available', ?)
                """,
                (link, _generate_account_name(), plan_id),
            )
            db.conn.commit()
            new_id = db.cur.lastrowid
        except sqlite3.IntegrityError:
            db.conn.rollback()
            return None

    db.set_low_stock_alerted(False)
    db.set_plan_low_stock_alerted(plan_id, False)
    return new_id


def add_subs_bulk(links, plan_id=None):
    plan_id = int(plan_id) if plan_id is not None else db.default_plan_id()
    plan = db.get_plan(plan_id)
    if not plan:
        raise ValueError("plan not found")
    if db.plan_delivery_type(plan) != "pool":
        raise ValueError("panel plans do not accept pooled links")

    normalized = []
    seen = set()
    for raw_link in links:
        link = (raw_link or "").strip()
        if link and link not in seen:
            normalized.append(link)
            seen.add(link)

    if not normalized:
        return 0

    added = 0
    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            for link in normalized:
                try:
                    db.cur.execute(
                        """
                        INSERT INTO subs(link, account_name, status, plan_id)
                        VALUES (?, ?, 'available', ?)
                        """,
                        (link, _generate_account_name(), plan_id),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    continue
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise

    if added:
        db.set_low_stock_alerted(False)
        db.set_plan_low_stock_alerted(plan_id, False)
    return added


def stock_count(plan_id=None):
    with db.LOCK:
        if plan_id is None:
            db.cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=0 AND COALESCE(source_type,'pool')='pool'")
        else:
            db.cur.execute(
                "SELECT COUNT(*) AS c FROM subs WHERE used=0 AND plan_id=? AND COALESCE(source_type,'pool')='pool'",
                (int(plan_id),),
            )
        return int(db.cur.fetchone()["c"] or 0)


def sold_count(plan_id=None):
    with db.LOCK:
        if plan_id is None:
            db.cur.execute("SELECT COUNT(*) AS c FROM subs WHERE used=1")
        else:
            db.cur.execute(
                "SELECT COUNT(*) AS c FROM subs WHERE used=1 AND plan_id=?",
                (int(plan_id),),
            )
        return int(db.cur.fetchone()["c"] or 0)


def user_subs(user_id, limit=None):
    sql = """
        SELECT id, link, account_name, assigned_at, price_paid, status,
               purchase_id, used, plan_id, source_type, panel_provider, panel_username,
               panel_status, panel_data_limit, panel_used_traffic, panel_expires_at,
               panel_duration_seconds, is_trial, last_synced_at
        FROM subs
        WHERE owner=? AND used=1
        ORDER BY assigned_at ASC, id ASC
    """
    params = [str(user_id)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with db.LOCK:
        db.cur.execute(sql, params)
        return db.cur.fetchall()


def short_link(link, size=34):
    link = link or ""
    return link if len(link) <= size else link[:size] + "..."


def link_counts():
    with db.LOCK:
        db.cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN used=0 THEN 1 ELSE 0 END) AS available,
                SUM(CASE WHEN used=1 THEN 1 ELSE 0 END) AS delivered,
                SUM(CASE WHEN status='disabled' THEN 1 ELSE 0 END) AS disabled
            FROM subs
            WHERE COALESCE(source_type,'pool')='pool'
            """
        )
        row = db.cur.fetchone()
    return {
        "total": int(row["total"] or 0),
        "available": int(row["available"] or 0),
        "delivered": int(row["delivered"] or 0),
        "disabled": int(row["disabled"] or 0),
    }


def list_links(kind="all", limit=15, offset=0):
    base = """
        SELECT id, link, used, owner, added_at, assigned_at, price_paid,
               account_name, status, purchase_id, plan_id, source_type
        FROM subs
    """
    queries = {
        "all": base + " WHERE COALESCE(source_type,'pool')='pool' ORDER BY id DESC LIMIT ? OFFSET ?",
        "available": base + " WHERE COALESCE(source_type,'pool')='pool' AND used=0 ORDER BY id DESC LIMIT ? OFFSET ?",
        "delivered": base + " WHERE COALESCE(source_type,'pool')='pool' AND used=1 ORDER BY id DESC LIMIT ? OFFSET ?",
        "disabled": base + " WHERE COALESCE(source_type,'pool')='pool' AND status='disabled' ORDER BY id DESC LIMIT ? OFFSET ?",
    }
    query = queries.get(kind, queries["all"])
    with db.LOCK:
        db.cur.execute(query, (int(limit), int(offset)))
        return db.cur.fetchall()


def search_links(query, limit=15):
    query = (query or "").strip()
    if not query:
        return []
    like = f"%{query}%"

    with db.LOCK:
        if query.isdigit():
            db.cur.execute(
                """
                SELECT id, link, used, owner, added_at, assigned_at, price_paid,
                       account_name, status, purchase_id, plan_id, source_type
                FROM subs
                WHERE COALESCE(source_type,'pool')='pool' AND (id=? OR owner=? OR link LIKE ? OR account_name LIKE ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(query), query, like, like, int(limit)),
            )
        else:
            db.cur.execute(
                """
                SELECT id, link, used, owner, added_at, assigned_at, price_paid,
                       account_name, status, purchase_id, plan_id
                FROM subs
                WHERE COALESCE(source_type,'pool')='pool' AND (link LIKE ? OR account_name LIKE ? OR owner LIKE ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (like, like, like, int(limit)),
            )
        return db.cur.fetchall()


def delete_available_link(link_id):
    """Delete only an unassigned row so purchase history cannot be orphaned."""
    link_id = int(link_id)
    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            db.cur.execute("SELECT id, used, source_type FROM subs WHERE id=?", (link_id,))
            row = db.cur.fetchone()
            if not row:
                db.conn.rollback()
                return False, "not_found"
            if (row["source_type"] or "pool") != "pool":
                db.conn.rollback()
                return False, "not_pool"
            if int(row["used"] or 0) == 1:
                db.conn.rollback()
                return False, "already_delivered"

            db.cur.execute("DELETE FROM subs WHERE id=? AND used=0 AND COALESCE(source_type,'pool')='pool'", (link_id,))
            changed = db.cur.rowcount == 1
            db.conn.commit()
            return (True, "deleted") if changed else (False, "not_deleted")
        except Exception:
            db.conn.rollback()
            raise


def return_delivered_link_to_pool(link_id, admin_id=None, reason=""):
    """Return the same subscription row to its plan pool without duplication."""
    link_id = int(link_id)
    reason = (reason or "manual_admin_return").strip()[:500]

    with db.LOCK:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            db.cur.execute(
                """
                SELECT id, link, used, owner, assigned_at, price_paid,
                       account_name, status, purchase_id, plan_id, source_type
                FROM subs
                WHERE id=?
                """,
                (link_id,),
            )
            row = db.cur.fetchone()
            if not row:
                db.conn.rollback()
                return False, "not_found", None
            if (row["source_type"] or "pool") != "pool":
                db.conn.rollback()
                return False, "not_pool", row
            if int(row["used"] or 0) != 1:
                db.conn.rollback()
                return False, "not_delivered", row

            old_owner = row["owner"]
            purchase_id = row["purchase_id"]
            plan_id = row["plan_id"]

            db.cur.execute(
                """
                UPDATE subs
                SET used=0,
                    owner=NULL,
                    assigned_at=NULL,
                    price_paid=NULL,
                    status='available',
                    purchase_id=NULL
                WHERE id=? AND used=1
                """,
                (link_id,),
            )
            if db.cur.rowcount != 1:
                db.conn.rollback()
                return False, "not_updated", row

            if old_owner:
                db.cur.execute(
                    """
                    UPDATE users
                    SET purchased=CASE WHEN purchased > 0 THEN purchased - 1 ELSE 0 END
                    WHERE id=?
                    """,
                    (str(old_owner),),
                )
                owner = db.get_user(old_owner)
                is_test = int(owner["is_test"] or 0) if owner and "is_test" in owner.keys() else 0
                db.cur.execute(
                    """
                    INSERT INTO ledger(
                        user_id, action, amount, balance_before, balance_after, note, is_test
                    ) VALUES (?, 'admin_return_sub_to_pool', 0, NULL, NULL, ?, ?)
                    """,
                    (
                        str(old_owner),
                        f"sub_id={link_id};purchase_id={purchase_id or '-'};"
                        f"admin_id={admin_id or '-'};reason={reason}",
                        is_test,
                    ),
                )

            db.cur.execute(
                """
                UPDATE purchase_items
                SET status='returned_to_pool',
                    reverted_at=datetime('now'),
                    reverted_by=?,
                    revert_reason=?
                WHERE sub_id=?
                  AND (? IS NULL OR purchase_id=?)
                  AND COALESCE(status, 'active') != 'returned_to_pool'
                """,
                (
                    str(admin_id) if admin_id is not None else None,
                    reason,
                    link_id,
                    purchase_id,
                    purchase_id,
                ),
            )
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise

    db.set_low_stock_alerted(False)
    if plan_id:
        db.set_plan_low_stock_alerted(plan_id, False)
    return True, "returned", row


def get_link_detail(link_id):
    with db.LOCK:
        db.cur.execute(
            """
            SELECT id, link, used, owner, added_at, assigned_at, price_paid,
                   account_name, status, purchase_id, plan_id
            FROM subs
            WHERE id=?
            """,
            (int(link_id),),
        )
        return db.cur.fetchone()


# -------------------- Provider core and YouPanel adapter --------------------

class ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "provider_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class YouPanelError(ProviderError):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message, code=code, status=status)


_panel_token: str | None = None
_panel_token_lock = asyncio.Lock()


def _panel_ssl():
    return None if YOUPANEL_VERIFY_SSL else False


def _panel_inbounds() -> dict:
    try:
        value = json.loads(YOUPANEL_INBOUNDS_JSON or "{}")
    except json.JSONDecodeError as exc:
        raise YouPanelError("invalid_inbounds", "YOUPANEL_INBOUNDS_JSON معتبر نیست.") from exc
    if not isinstance(value, dict) or not value:
        raise YouPanelError("invalid_inbounds", "حداقل یک inbound برای پنل تنظیم کنید.")
    return value


def panel_is_configured() -> bool:
    return youpanel_configured()


async def _panel_login(force: bool = False) -> str:
    global _panel_token
    if not panel_is_configured():
        raise YouPanelError("not_configured", "اتصال YouPanel در متغیرهای محیطی تنظیم نشده است.")
    if _panel_token and not force:
        return _panel_token
    async with _panel_token_lock:
        if _panel_token and not force:
            return _panel_token
        timeout = aiohttp.ClientTimeout(total=YOUPANEL_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{YOUPANEL_BASE_URL}/api/admin/token",
                    data={"username": YOUPANEL_USERNAME, "password": YOUPANEL_PASSWORD, "grant_type": "password"},
                    ssl=_panel_ssl(),
                ) as response:
                    payload = await _read_json(response)
                    if response.status != 200:
                        raise YouPanelError("login_failed", "ورود به YouPanel ناموفق بود.", response.status)
        except YouPanelError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise YouPanelError("network", "ارتباط با YouPanel برقرار نشد.") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise YouPanelError("invalid_login_response", "پاسخ ورود YouPanel توکن معتبر ندارد.")
        _panel_token = str(token)
        return _panel_token


async def _read_json(response: aiohttp.ClientResponse):
    try:
        return await response.json(content_type=None)
    except Exception:
        text = await response.text()
        return {"detail": text[:500]}


async def _panel_request(method: str, path: str, *, json_body=None, params=None, retry_auth=True):
    token = await _panel_login()
    timeout = aiohttp.ClientTimeout(total=YOUPANEL_TIMEOUT_SECONDS)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                f"{YOUPANEL_BASE_URL}{path}",
                headers=headers,
                json=json_body,
                params=params,
                ssl=_panel_ssl(),
            ) as response:
                payload = await _read_json(response)
                status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise YouPanelError("network", "ارتباط با YouPanel قطع یا زمان‌بر شد.") from exc
    if status == 401 and retry_auth:
        await _panel_login(force=True)
        return await _panel_request(method, path, json_body=json_body, params=params, retry_auth=False)
    if status < 200 or status >= 300:
        if status in {502, 503, 504, 520, 521, 522, 523, 524, 525, 526}:
            raise YouPanelError(
                "upstream_unavailable",
                "پنل تأمین‌کننده موقتاً در دسترس نیست. چند دقیقه دیگر دوباره تلاش کنید.",
                status,
            )
        detail = payload.get("detail") if isinstance(payload, dict) else None
        detail_text = str(detail or status).strip()
        if len(detail_text) > 300:
            detail_text = detail_text[:297] + "..."
        raise YouPanelError("api_error", f"خطای YouPanel: {detail_text}", status)
    if not isinstance(payload, dict):
        raise YouPanelError("invalid_response", "پاسخ YouPanel ساختار معتبر ندارد.", status)
    return payload


def _clean_panel_username(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value or "").strip("-_")
    return value[:48] or "bsv-user"


def provider_username_for_order(user_id, purchase_id, index: int) -> str:
    import settings
    return _clean_panel_username(f"{settings.service_username_prefix()}-{user_id}-{purchase_id}-{index}")


def provider_trial_username(user_id) -> str:
    return _clean_panel_username(f"trial-{user_id}")


# Backward-compatible names for older call sites.
panel_username_for_order = provider_username_for_order
panel_trial_username = provider_trial_username


def _panel_user_payload(username: str, data_limit_bytes: int, duration_days: int, start_mode="on_hold", reset_strategy="no_reset", max_devices=None):
    duration_seconds = max(1, int(duration_days)) * 86400
    active = start_mode == "active"
    return {
        "username": _clean_panel_username(username),
        "status": "active" if active else "on_hold",
        "expire": int(time.time()) + duration_seconds if active else None,
        "on_hold_expire_duration": None if active else duration_seconds,
        "data_limit": max(0, int(data_limit_bytes)),  # 0 = unlimited (panel convention)
        "data_limit_reset_strategy": reset_strategy or "no_reset",
        "inbounds": _panel_inbounds(),
        "proxies": {"vless": {"flow": ""}},
        "note": "created-by-berserk-bot",
        "backup_outbound_tags": [],
        "primary_outbound_tag": None,
        "routing_mode": "manual",
        "single_device_mode": "off",
        "max_devices": int(max_devices) if max_devices not in (None, "") else None,
        "user_location_label": None,
    }


async def panel_create_user(username: str, data_limit_bytes: int, duration_days: int, start_mode="on_hold", reset_strategy="no_reset", max_devices=None):
    payload = _panel_user_payload(username, data_limit_bytes, duration_days, start_mode, reset_strategy, max_devices)
    result = await _panel_request("POST", "/api/user", json_body=payload)
    if not result.get("subscription_url") or not result.get("username"):
        raise YouPanelError("invalid_create_response", "پنل سرویس را ساخت اما لینک اشتراک برنگرداند.")
    return result


async def panel_get_user(username: str):
    try:
        return await _panel_request("GET", f"/api/user/{_clean_panel_username(username)}")
    except YouPanelError as exc:
        if getattr(exc, "status", None) == 404:
            return None
        raise


async def panel_delete_user(username: str):
    return await _panel_request("DELETE", f"/api/user/{_clean_panel_username(username)}")


async def panel_reset_usage(username: str):
    return await _panel_request("POST", f"/api/user/{_clean_panel_username(username)}/reset")


async def panel_revoke_subscription(username: str):
    result = await _panel_request("POST", f"/api/user/{_clean_panel_username(username)}/revoke_sub")
    if not result.get("subscription_url"):
        raise YouPanelError("invalid_revoke_response", "پنل لینک اشتراک جدید برنگرداند.")
    return result


async def panel_usage(username: str, start: str = "1970-01-01T00:00:00"):
    return await _panel_request("GET", f"/api/user/{_clean_panel_username(username)}/usage", params={"start": start})


async def panel_health_check():
    return await _panel_request("GET", "/api/admin")


class ProviderAdapter:
    key = "provider"
    label = "تأمین‌کننده"
    capabilities = {
        "create": False, "renew": False, "add_volume": False,
        "reset_usage": False, "revoke": False, "device_limit": False,
        "usage": False, "delete": False, "lookup": False,
    }

    def configured(self) -> bool:
        return False

    async def health(self):
        raise ProviderError("این تأمین‌کننده پیاده‌سازی نشده است.")

    async def get_user(self, username):
        return None

    async def create_user(self, username, *, data_limit_bytes, duration_days, start_mode="on_hold", reset_strategy="no_reset", max_devices=None, options=None):
        raise ProviderError("ساخت سرویس برای این تأمین‌کننده پیاده‌سازی نشده است.")

    async def delete_user(self, username):
        raise ProviderError("حذف سرویس برای این تأمین‌کننده پیاده‌سازی نشده است.")

    async def reset_usage(self, username):
        raise ProviderError("ریست مصرف برای این تأمین‌کننده پیاده‌سازی نشده است.")

    async def revoke_subscription(self, username):
        raise ProviderError("تعویض لینک برای این تأمین‌کننده پیاده‌سازی نشده است.")

    async def usage(self, username, start="1970-01-01T00:00:00"):
        raise ProviderError("گزارش مصرف برای این تأمین‌کننده پیاده‌سازی نشده است.")


class YouPanelProvider(ProviderAdapter):
    key = "youpanel"
    label = "YouPanel"
    capabilities = {
        "create": True, "renew": True, "add_volume": True,
        "reset_usage": True, "revoke": True, "device_limit": True,
        "usage": True, "delete": True, "lookup": True,
    }

    def configured(self) -> bool:
        return panel_is_configured()

    async def health(self):
        return await panel_health_check()

    async def get_user(self, username):
        return await panel_get_user(username)

    async def create_user(self, username, *, data_limit_bytes, duration_days, start_mode="on_hold", reset_strategy="no_reset", max_devices=None, options=None):
        return await panel_create_user(username, data_limit_bytes, duration_days, start_mode, reset_strategy, max_devices)

    async def delete_user(self, username):
        return await panel_delete_user(username)

    async def reset_usage(self, username):
        return await panel_reset_usage(username)

    async def revoke_subscription(self, username):
        return await panel_revoke_subscription(username)

    async def usage(self, username, start="1970-01-01T00:00:00"):
        return await panel_usage(username, start)


class PasarGuardError(ProviderError):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message, code=code, status=status)


class PasarGuardProvider(ProviderAdapter):
    """PasarGuard adapter built on the official `pasarguard` PyPI client.

    Each instance represents one panel with its own token cache, so
    multiple panels can be registered as independent, selectable providers
    at the same time — see PASARGUARD_PANELS_JSON in config.py.

    Field names below (hwid_limit, group_ids, on_hold_expire_duration) are
    taken from the panel's own dashboard labels and the official client's
    documented quick-start example. A couple of fields (the exact
    DataLimitResetStrategy member names) could not be independently
    confirmed from public docs, so PasarGuardError messages are left
    unmodified from the underlying library/HTTP error on purpose — if a
    field name turns out to be wrong, the panel's own validation error
    will surface directly instead of being swallowed.
    """

    capabilities = {
        "create": True, "renew": True, "add_volume": True,
        "reset_usage": True, "revoke": True, "device_limit": True,
        "usage": True, "delete": True, "lookup": True,
    }

    def __init__(self, key, label, base_url, username, password, group_ids,
                 verify_ssl=True, timeout=20):
        self.key = key
        self.label = label
        self._username = username
        self._password = password
        self._group_ids = group_ids if isinstance(group_ids, list) else []
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        try:
            from pasarguard import PasarguardAPI
        except ImportError as exc:
            raise PasarGuardError(
                "missing_dependency",
                "پکیج pasarguard نصب نیست. `pip install pasarguard` را اجرا کن.",
            ) from exc
        self._api = PasarguardAPI(base_url=base_url, verify=verify_ssl, timeout=float(timeout))

    def configured(self) -> bool:
        return bool(self._username and self._password and self._group_ids)

    async def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        async with self._token_lock:
            if self._token and not force:
                return self._token
            import httpx
            try:
                result = await self._api.get_token(username=self._username, password=self._password)
            except httpx.HTTPStatusError as exc:
                raise PasarGuardError("login_failed", f"ورود به {self.label} ناموفق بود ({exc.response.status_code}).", exc.response.status_code) from exc
            except httpx.RequestError as exc:
                raise PasarGuardError("network", f"ارتباط با {self.label} برقرار نشد.") from exc
            self._token = result.access_token
            return self._token

    async def _call(self, method_name: str, *args, retry_auth=True, **kwargs):
        """Call a method on the underlying client, auto-refreshing the
        token once on 401 before giving up."""
        import httpx
        token = await self._get_token()
        method = getattr(self._api, method_name)
        try:
            return await method(*args, token=token, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401 and retry_auth:
                await self._get_token(force=True)
                return await self._call(method_name, *args, retry_auth=False, **kwargs)
            if status in {502, 503, 504}:
                raise PasarGuardError("upstream_unavailable", f"{self.label} موقتاً در دسترس نیست.", status) from exc
            detail = exc.response.text
            if len(detail) > 300:
                detail = detail[:297] + "..."
            raise PasarGuardError("api_error", f"خطای {self.label} ({status}): {detail}", status) from exc
        except httpx.RequestError as exc:
            raise PasarGuardError("network", f"ارتباط با {self.label} قطع یا زمان‌بر شد.") from exc

    def _build_user_create(self, username, data_limit_bytes, duration_days, start_mode, reset_strategy, max_devices):
        from pasarguard import UserCreate, UserStatus
        duration_seconds = max(1, int(duration_days)) * 86400
        active = start_mode == "active"
        kwargs = dict(
            username=_clean_panel_username(username),
            status=UserStatus.ACTIVE if active else UserStatus.ON_HOLD,
            data_limit=max(0, int(data_limit_bytes)),  # 0 = unlimited (panel convention)
            group_ids=self._group_ids,
            note="created-by-berserk-bot",
        )
        if active:
            kwargs["expire"] = int(time.time()) + duration_seconds
        else:
            kwargs["on_hold_expire_duration"] = duration_seconds
        if reset_strategy:
            kwargs["data_limit_reset_strategy"] = reset_strategy
        if max_devices not in (None, ""):
            kwargs["hwid_limit"] = int(max_devices)
        return UserCreate(**kwargs)

    async def health(self):
        return await self._call("get_current_admin")

    def _to_dict(self, obj):
        """Normalize a pasarguard SDK Pydantic response into a plain dict.

        The rest of subs.py (shared with YouPanelProvider) expects
        dict-like items with .get()/dict(item) support; the SDK returns
        typed Pydantic models instead, which broke every downstream
        consumer of create_user/get_user/revoke_subscription.
        """
        if obj is None or isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "dict"):
            return obj.dict()
        return obj

    async def get_user(self, username):
        try:
            result = await self._call("get_user", _clean_panel_username(username))
        except PasarGuardError as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        return self._to_dict(result)

    async def create_user(self, username, *, data_limit_bytes, duration_days, start_mode="on_hold", reset_strategy="no_reset", max_devices=None, options=None):
        user = self._build_user_create(username, data_limit_bytes, duration_days, start_mode, reset_strategy, max_devices)
        result = await self._call("create_user", user)
        if not getattr(result, "subscription_url", None):
            raise PasarGuardError("invalid_create_response", f"{self.label} سرویس را ساخت اما لینک اشتراک برنگرداند.")
        return self._to_dict(result)

    async def delete_user(self, username):
        return self._to_dict(await self._call("remove_user", _clean_panel_username(username)))

    async def reset_usage(self, username):
        from pasarguard import BulkUsersSelection
        return self._to_dict(await self._call("bulk_reset_users_data_usage", BulkUsersSelection(usernames=[_clean_panel_username(username)])))

    async def revoke_subscription(self, username):
        from pasarguard import UserModify
        result = await self._call("modify_user", _clean_panel_username(username), UserModify(revoke_sub=True))
        if not getattr(result, "subscription_url", None):
            raise PasarGuardError("invalid_revoke_response", f"{self.label} لینک اشتراک جدید برنگرداند.")
        return self._to_dict(result)

    async def usage(self, username, start="1970-01-01T00:00:00"):
        result = await self._call("get_user_usage", _clean_panel_username(username), start=start)
        return self._to_dict(result)

    async def list_groups(self):
        """Fetch {id, name} for every group defined on this panel — used by
        the admin panel so the admin can pick a group by NAME instead of
        hunting for its numeric id manually."""
        groups = await self._call("get_all_groups")
        return [{"id": g.id, "name": getattr(g, "name", None) or f"#{g.id}"} for g in groups]


_PROVIDER_REGISTRY = {"youpanel": YouPanelProvider()}


def _register_pasarguard_panels():
    from config import PASARGUARD_PANELS
    for panel in PASARGUARD_PANELS:
        _PROVIDER_REGISTRY[panel["key"]] = PasarGuardProvider(
            key=panel["key"],
            label=panel["label"],
            base_url=panel["base_url"],
            username=panel["username"],
            password=panel["password"],
            group_ids=panel["group_ids"],
            verify_ssl=panel["verify_ssl"],
            timeout=panel["timeout"],
        )


_register_pasarguard_panels()


async def list_all_pasarguard_groups():
    """Fetch groups from every registered PasarGuard panel.

    Returns a dict of {panel_key: {"label": str, "groups": [...], "error": str|None}}.
    """
    results = {}
    for key, provider in _PROVIDER_REGISTRY.items():
        if not isinstance(provider, PasarGuardProvider):
            continue
        entry = {"label": provider.label, "groups": [], "error": None}
        try:
            entry["groups"] = await provider.list_groups()
        except ProviderError as exc:
            entry["error"] = str(exc)
        results[key] = entry
    return results


def list_provider_adapters(configured_only=False):
    values = list(_PROVIDER_REGISTRY.values())
    if configured_only:
        values = [provider for provider in values if provider.configured()]
    return values


def get_provider_adapter(key):
    key = (key or "").strip().lower()
    provider = _PROVIDER_REGISTRY.get(key)
    if not provider:
        raise ProviderError(f"تأمین‌کننده «{key or '-'}» در این نسخه نصب نشده است.")
    return provider


def provider_label(key):
    if (key or "pool") == "pool":
        return "استخر لینک"
    provider = _PROVIDER_REGISTRY.get((key or "").strip().lower())
    return provider.label if provider else key


async def provider_health_check(key):
    provider = get_provider_adapter(key)
    commerce.ensure_provider(provider.key, provider.capabilities)
    if not provider.configured():
        commerce.record_provider_log(provider.key, "health", "error", error_code="not_configured", error_message="تنظیمات کامل نیست.")
        raise ProviderError(f"تنظیمات {provider.label} کامل نیست.")
    started = time.monotonic()
    try:
        result = await provider.health()
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        commerce.record_provider_log(provider.key, "health", "error", response_ms=elapsed,
                                     error_code=getattr(exc, "code", "health_error"), error_message=str(exc))
        raise
    elapsed = int((time.monotonic() - started) * 1000)
    commerce.record_provider_log(provider.key, "health", "success", response_ms=elapsed)
    return result


def _normalize_existing_provider_item(item, username):
    if not item or not isinstance(item, dict):
        return None
    link = (item.get("subscription_url") or item.get("link") or "").strip()
    if not link:
        return None
    normalized = dict(item)
    normalized["username"] = normalized.get("username") or username
    normalized["subscription_url"] = link
    return normalized


async def process_provider_job(purchase_id: int):
    purchase = commerce.get_order_detail(purchase_id)
    if not purchase:
        raise db.PurchaseError("purchase_not_found", "سفارش پیدا نشد.")
    if purchase["status"] == "completed":
        db.cur.execute("SELECT * FROM subs WHERE purchase_id=? ORDER BY id", (int(purchase_id),))
        return {"completed": True, "queued": False, "purchase_id": int(purchase_id), "items": [dict(r) for r in db.cur.fetchall()]}
    if purchase["status"] == "refunded":
        return {"completed": False, "queued": False, "refunded": True, "purchase_id": int(purchase_id), "items": []}
    if not commerce.claim_provider_job(purchase_id):
        return {"completed": False, "queued": True, "purchase_id": int(purchase_id), "items": []}

    job = commerce.get_provider_job(purchase_id)
    plan = db.get_plan(purchase["plan_id"])
    if not job or not plan:
        commerce.release_provider_job(purchase_id)
        raise db.PurchaseError("invalid_job", "اطلاعات صف سفارش ناقص است.")
    provider_key = job["active_provider"]
    fallback = (job["fallback_provider"] or "").strip().lower()
    provider_options = db.plan_provider_options(plan)

    try:
        provider = get_provider_adapter(provider_key)
        commerce.ensure_provider(provider.key, provider.capabilities)
        if not provider.configured() or not commerce.provider_sales_enabled(provider.key):
            if fallback and fallback != provider_key and commerce.provider_sales_enabled(fallback):
                fallback_adapter = get_provider_adapter(fallback)
                if fallback_adapter.configured():
                    commerce.switch_job_provider(purchase_id, fallback)
                    commerce.record_provider_log(provider_key, "failover", "success", purchase_id=purchase_id,
                                                 plan_id=plan["id"], user_id=purchase["user_id"],
                                                 error_message=f"switched_to={fallback}")
                    commerce.release_provider_job(purchase_id)
                    return await process_provider_job(purchase_id)
            raise ProviderError(f"فروش یا اتصال {provider.label} فعال نیست.")

        ready_items = []
        rows = commerce.list_provider_job_items(purchase_id)
        for row in rows:
            username = provider_username_for_order(purchase["user_id"], purchase_id, int(row["item_index"]))
            if row["status"] == "ready" and row["subscription_url"]:
                payload = json.loads(row["payload_json"] or "{}")
                payload.update({"username": username, "subscription_url": row["subscription_url"], "account_name": db.generate_service_code()})
                ready_items.append(payload)
                continue

            started = time.monotonic()
            try:
                existing = await provider.get_user(username)
                item = _normalize_existing_provider_item(existing, username)
                if item is None:
                    plan_is_unlimited = bool(plan["unlimited_volume"]) if "unlimited_volume" in plan.keys() else False
                    item = await provider.create_user(
                        username,
                        data_limit_bytes=(
                            0 if plan_is_unlimited else
                            int(plan["panel_data_limit_bytes"] or 0)
                            + int(purchase["bonus_volume_mb"] or 0) * 1024 * 1024
                        ),
                        duration_days=int(plan["panel_duration_days"] or 0),
                        start_mode=plan["panel_start_mode"] or "on_hold",
                        reset_strategy=plan["panel_reset_strategy"] or "no_reset",
                        max_devices=plan["panel_max_devices"],
                        options=provider_options,
                    )
                elapsed = int((time.monotonic() - started) * 1000)
                commerce.record_provider_log(provider.key, "create", "success", user_id=purchase["user_id"],
                                             plan_id=plan["id"], purchase_id=purchase_id, response_ms=elapsed)
                commerce.set_job_item_ready(purchase_id, row["item_index"], provider.key, username, item)
                normalized = dict(item)
                normalized["account_name"] = db.generate_service_code()
                ready_items.append(normalized)
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                commerce.set_job_item_error(purchase_id, row["item_index"], str(exc))
                commerce.record_provider_log(provider.key, "create", "error", user_id=purchase["user_id"],
                                             plan_id=plan["id"], purchase_id=purchase_id, response_ms=elapsed,
                                             error_code=getattr(exc, "code", "provider_error"), error_message=str(exc))
                raise

        finalized = commerce.mark_provider_purchase_completed(purchase_id, ready_items)
        p = finalized["purchase"]
        return {
            "purchase_id": int(p["id"]), "quantity": int(p["quantity"]), "unit_price": int(p["unit_price"]),
            "subtotal": int(p["subtotal_amount"] or p["amount"]), "discount_amount": int(p["discount_amount"] or 0),
            "amount": int(p["amount"]), "balance_before": None, "balance_after": int(db.get_user(p["user_id"])["balance"] or 0),
            "is_test": int(p["is_test"] or 0), "items": finalized["items"], "provider": p["provider"],
            "queued": False, "completed": True,
        }
    except Exception as exc:
        # Runtime failover is safe only before any item has been created.  Once a
        # partial order exists, retries stay on the same provider to avoid a mixed
        # delivery and duplicate remote accounts.
        current_job = commerce.get_provider_job(purchase_id)
        current_rows = commerce.list_provider_job_items(purchase_id)
        ready_count = sum(1 for item in current_rows if item["status"] == "ready")
        runtime_fallback = ((current_job["fallback_provider"] if current_job else None) or "").strip().lower()
        current_provider = ((current_job["active_provider"] if current_job else provider_key) or "").strip().lower()
        primary_provider = ((current_job["primary_provider"] if current_job else provider_key) or "").strip().lower()
        if (
            ready_count == 0
            and runtime_fallback
            and current_provider == primary_provider
            and runtime_fallback != current_provider
            and commerce.provider_sales_enabled(runtime_fallback)
        ):
            try:
                fallback_adapter = get_provider_adapter(runtime_fallback)
                if fallback_adapter.configured():
                    commerce.switch_job_provider(purchase_id, runtime_fallback)
                    commerce.record_provider_log(
                        current_provider,
                        "runtime_failover",
                        "success",
                        purchase_id=purchase_id,
                        plan_id=plan["id"],
                        user_id=purchase["user_id"],
                        error_message=f"switched_to={runtime_fallback};cause={str(exc)[:300]}",
                    )
                    commerce.release_provider_job(purchase_id)
                    return await process_provider_job(purchase_id)
            except Exception as fallback_exc:
                commerce.record_provider_log(
                    runtime_fallback,
                    "runtime_failover",
                    "error",
                    purchase_id=purchase_id,
                    plan_id=plan["id"],
                    user_id=purchase["user_id"],
                    error_code=getattr(fallback_exc, "code", "fallback_error"),
                    error_message=str(fallback_exc),
                )
        schedule = commerce.schedule_provider_retry(purchase_id, str(exc))
        if schedule.get("final"):
            cleanup_errors = []
            job = commerce.get_provider_job(purchase_id)
            active_key = (job["active_provider"] if job else provider_key) or provider_key
            try:
                cleanup_provider = get_provider_adapter(active_key)
            except Exception:
                cleanup_provider = None
            for row in commerce.list_provider_job_items(purchase_id):
                username = provider_username_for_order(purchase["user_id"], purchase_id, int(row["item_index"]))
                if not cleanup_provider:
                    continue
                try:
                    await cleanup_provider.delete_user(username)
                except ProviderError as cleanup_exc:
                    if getattr(cleanup_exc, "status", None) != 404:
                        cleanup_errors.append(str(cleanup_exc))
                except Exception as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            detail = str(exc)
            if cleanup_errors:
                detail += "; cleanup=" + " | ".join(cleanup_errors)
            commerce.refund_purchase_once(purchase_id, detail, review_required=True)
            return {"purchase_id": int(purchase_id), "queued": False, "completed": False, "refunded": True, "items": [], "error": str(exc)}
        return {"purchase_id": int(purchase_id), "queued": True, "completed": False, "items": [],
                "retry_count": schedule.get("retry_count"), "retry_delay": schedule.get("delay"), "error": str(exc)}
    finally:
        commerce.release_provider_job(purchase_id)


async def provision_provider_purchase(
    user_id,
    quantity,
    plan_id,
    unit_price=None,
    note="",
    discount_code=None,
    request_key=None,
    custom_base_username=None,
):
    # unit_price is accepted for backward compatibility; the current plan price is authoritative.
    reservation = commerce.begin_provider_purchase(
        user_id,
        quantity,
        plan_id,
        discount_code=discount_code,
        note=note,
        request_key=request_key,
        custom_base_username=custom_base_username,
    )
    existing_status = (reservation.get("status") or "").lower()
    if reservation.get("existing") and existing_status in {"retry", "provisioning", "paid"}:
        result = {
            "purchase_id": int(reservation["purchase_id"]),
            "queued": True,
            "completed": False,
            "items": reservation.get("items") or [],
            "existing": True,
            "status": existing_status,
        }
    elif reservation.get("existing") and existing_status == "refunded":
        result = {
            "purchase_id": int(reservation["purchase_id"]),
            "queued": False,
            "completed": False,
            "refunded": True,
            "items": [],
            "existing": True,
            "status": existing_status,
        }
    else:
        result = await process_provider_job(reservation["purchase_id"])
    result.setdefault("balance_before", reservation["balance_before"])
    result.setdefault("balance_after", reservation["balance_after"])
    result.setdefault("subtotal", reservation["subtotal"])
    result.setdefault("discount_amount", reservation["discount_amount"])
    result.setdefault("amount", reservation["amount"])
    result.setdefault("quantity", reservation["quantity"])
    result.setdefault("unit_price", reservation["unit_price"])
    result.setdefault("is_test", reservation["is_test"])
    result.setdefault("provider", reservation["provider"])
    result.setdefault("provider_notice", reservation.get("provider_notice"))
    return result


async def recover_due_provider_jobs(limit: int = 20):
    results = []
    for row in commerce.due_provider_jobs(limit):
        try:
            result = await process_provider_job(int(row["purchase_id"]))
            results.append(result)
        except Exception:
            logger.exception("provider queue recovery failed for purchase %s", row["purchase_id"])
    return results


async def provision_panel_purchase(user_id, quantity, plan_id, unit_price=None, note=""):
    """Compatibility alias for v6.1 integrations."""
    return await provision_provider_purchase(user_id, quantity, plan_id, unit_price, note)


async def create_trial_service(user_id, size_mb: int, days: int, provider_key="youpanel"):
    provider = get_provider_adapter(provider_key)
    if not provider.configured():
        raise ProviderError(f"اتصال {provider.label} تنظیم نشده است.")
    username = provider_trial_username(user_id)
    ok, reason, claim = db.begin_trial_claim(user_id, username, provider_key=provider_key)
    if not ok:
        if reason == "already_claimed":
            raise ProviderError("برای این حساب قبلاً اکانت تست ثبت شده است.", code="already_claimed")
        raise ProviderError("امکان شروع اکانت تست وجود ندارد: " + str(reason))
    try:
        item = await provider.create_user(
            username,
            data_limit_bytes=int(size_mb) * 1024 * 1024,
            duration_days=int(days),
            start_mode="on_hold",
            reset_strategy="no_reset",
            max_devices=TRIAL_MAX_DEVICES,
            options={"trial": True},
        )
        return db.complete_trial_claim(user_id, item, provider_key=provider_key)
    except Exception as exc:
        try:
            await provider.delete_user(username)
        except ProviderError as cleanup_exc:
            if getattr(cleanup_exc, "status", None) != 404:
                logger.warning("trial cleanup failed for %s: %s", username, getattr(cleanup_exc, "message", str(cleanup_exc)))
        except Exception:
            logger.warning("trial cleanup failed for %s", username, exc_info=True)
        db.fail_trial_claim(user_id, str(exc))
        raise


async def recover_stale_provider_purchases(minutes: int = 15):
    """Compatibility entry point; v6.3 recovers idempotent queued jobs."""
    results = await recover_due_provider_jobs(50)
    return [int(item["purchase_id"]) for item in results if item.get("completed") or item.get("refunded")]


# Compatibility alias for deployments and call sites from v6.1.
recover_stale_panel_purchases = recover_stale_provider_purchases


async def recover_stale_trial_claims(minutes: int = 15):
    """Delete deterministic orphan trial users and reopen failed claims."""
    recovered = []
    for claim in db.list_stale_trial_claims(minutes):
        username = claim["panel_username"]
        provider_key = claim["provider_key"] if "provider_key" in claim.keys() else "youpanel"
        error = "startup_trial_recovery"
        try:
            provider = get_provider_adapter(provider_key)
            await provider.delete_user(username)
        except ProviderError as exc:
            if getattr(exc, "status", None) != 404:
                error += f"; cleanup={getattr(exc, 'message', str(exc))}"
        except Exception as exc:
            error += f"; cleanup={str(exc)[:200]}"
        if db.fail_trial_claim(claim["user_id"], error):
            recovered.append(str(claim["user_id"]))
    return recovered

