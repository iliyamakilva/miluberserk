"""Environment configuration for the bot.

Only environment parsing belongs here. Runtime-editable settings are stored in
SQLite and exposed through :mod:`settings`.
"""

from __future__ import annotations

import json as _json
import os


def _get_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Hidden command used instead of /admin.
ADMIN_COMMAND = os.getenv("ADMIN_COMMAND", "panel_secret").strip().lstrip("/")

# ADMIN_ID can contain one or more comma-separated Telegram IDs.
_admin_ids_raw = os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(part)
    for part in _admin_ids_raw.replace(" ", "").split(",")
    if part.strip().isdigit()
}

# Sensitive actions such as restore are restricted to OWNER_ID when provided.
# For backward compatibility, all admins are treated as owners when it is absent.
OWNER_ID = _get_int("OWNER_ID")
OWNER_IDS = {OWNER_ID} if OWNER_ID else set(ADMIN_IDS)

REF_REWARD = _get_int("REF_REWARD", 30_000) or 30_000

# Railway Volume example: DB_PATH=/data/berserk.db
DB_PATH = os.getenv("DB_PATH", "berserk.db").strip() or "berserk.db"

MAX_TOTAL_REFERRALS = max(0, _get_int("MAX_TOTAL_REFERRALS", 50) or 0)
MAX_REFERRALS_PER_DAY = max(0, _get_int("MAX_REFERRALS_PER_DAY", 10) or 0)

# Broadcast pacing and automatic backup controls.
BROADCAST_DELAY = max(0.0, _get_float("BROADCAST_DELAY", 0.08))
BACKUP_INTERVAL_SECONDS = max(3600, _get_int("BACKUP_INTERVAL_SECONDS", 24 * 60 * 60) or 0)
BACKUP_RETENTION_COUNT = max(1, _get_int("BACKUP_RETENTION_COUNT", 30) or 30)

# Optional YouPanel integration. Credentials must be configured only as
# environment variables; access tokens are acquired at runtime and never
# persisted in SQLite or log output.
YOUPANEL_BASE_URL = os.getenv("YOUPANEL_BASE_URL", "").strip().rstrip("/")
YOUPANEL_USERNAME = os.getenv("YOUPANEL_USERNAME", "").strip()
YOUPANEL_PASSWORD = os.getenv("YOUPANEL_PASSWORD", "")
YOUPANEL_TIMEOUT_SECONDS = max(5, _get_int("YOUPANEL_TIMEOUT_SECONDS", 20) or 20)
YOUPANEL_VERIFY_SSL = _get_bool("YOUPANEL_VERIFY_SSL", True)
YOUPANEL_INBOUNDS_JSON = os.getenv(
    "YOUPANEL_INBOUNDS_JSON",
    '{"vless":["RTL-1","VLESS + WS","tcp","TUN"]}',
).strip()
# Trial catalog item is provider-agnostic. Generic TRIAL_* variables take
# precedence; legacy YOUPANEL_TRIAL_* names remain valid for compatibility.
TRIAL_PROVIDER_KEY = os.getenv("TRIAL_PROVIDER_KEY", "youpanel").strip().lower() or "youpanel"
TRIAL_ENABLED = _get_bool("TRIAL_ENABLED", _get_bool("YOUPANEL_TRIAL_ENABLED", True))
TRIAL_SIZE_MB = max(1, _get_int("TRIAL_SIZE_MB", _get_int("YOUPANEL_TRIAL_SIZE_MB", 200)) or 200)
TRIAL_DAYS = max(1, _get_int("TRIAL_DAYS", _get_int("YOUPANEL_TRIAL_DAYS", 1)) or 1)
TRIAL_MAX_DEVICES = max(1, _get_int("TRIAL_MAX_DEVICES", _get_int("YOUPANEL_TRIAL_MAX_DEVICES", 1)) or 1)

# Backward-compatible aliases used by older deployments and modules.
YOUPANEL_TRIAL_ENABLED = TRIAL_ENABLED
YOUPANEL_TRIAL_SIZE_MB = TRIAL_SIZE_MB
YOUPANEL_TRIAL_DAYS = TRIAL_DAYS
YOUPANEL_TRIAL_MAX_DEVICES = TRIAL_MAX_DEVICES


def youpanel_configured() -> bool:
    return bool(YOUPANEL_BASE_URL and YOUPANEL_USERNAME and YOUPANEL_PASSWORD)


# Optional PasarGuard integration. PasarGuard speaks the same
# admin-token / /api/user contract as YouPanel, but a bot may need to talk
# to more than one panel (e.g. a master panel and a secondary node), so
# panels are declared as a JSON array instead of single BASE_URL/USER/PASS
# variables. Each array item becomes its own selectable provider in the
# plan wizard, keyed by "key".
#
# Example PASARGUARD_PANELS_JSON:
# [
#   {
#     "key": "pasarguard_main",
#     "label": "پنل اصلی",
#     "base_url": "https://panel.example.com",
#     "username": "admin",
#     "password": "SECRET",
#     "group_ids": [1],
#     "verify_ssl": true,
#     "timeout": 20
#   }
# ]
# group_ids را از خودِ پنل، بخش Groups بردار (شماره‌ی گروهی که می‌خوای
# مشتری‌های این پلن باهاش ساخته بشن). دیگه نیازی به اسم inbound نیست —
# پاسارگارد از نسخه‌ی ۳ به بعد کاربر را به یک یا چند Group وصل می‌کند و
# خودِ Group تعیین می‌کند کدام inbound(ها) فعال باشند.
PASARGUARD_PANELS_JSON = os.getenv("PASARGUARD_PANELS_JSON", "[]").strip()


def _parse_pasarguard_panels() -> list[dict]:
    try:
        raw = _json.loads(PASARGUARD_PANELS_JSON or "[]")
    except _json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    panels: list[dict] = []
    seen_keys: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        username = str(item.get("username") or "").strip()
        password = str(item.get("password") or "")
        if not (key and base_url and username and password) or key in seen_keys:
            continue
        seen_keys.add(key)
        group_ids_raw = item.get("group_ids")
        group_ids: list[int] = []
        if isinstance(group_ids_raw, list):
            for g in group_ids_raw:
                try:
                    group_ids.append(int(g))
                except (TypeError, ValueError):
                    continue
        panels.append(
            {
                "key": key,
                "label": str(item.get("label") or key).strip(),
                "base_url": base_url,
                "username": username,
                "password": password,
                "group_ids": group_ids,
                "verify_ssl": bool(item.get("verify_ssl", True)),
                "timeout": max(5, int(item.get("timeout", 20) or 20)),
            }
        )
    return panels


PASARGUARD_PANELS = _parse_pasarguard_panels()


# ---------------------------------------------------------------------------
# PasarGuard backup automation (DB dump + config files -> Telegram)
# ---------------------------------------------------------------------------
# MODE:
#   "local" — bot process and PasarGuard panel are on the SAME machine (the
#             current setup). The backup command just runs as a local
#             subprocess, no network hop needed.
#   "ssh"   — panel is on a DIFFERENT server than wherever the bot runs.
#             Requires PASARGUARD_BACKUP_SSH_* below and the `asyncssh`
#             package (not installed by default, since it's only needed in
#             this mode).
#
# PASARGUARD_BACKUP_COMMAND is a single shell command *you* write, because
# only you know your container names and compose paths (they're not guessed
# here to avoid silently running the wrong thing against real data). It must:
#   1) dump the panel DB and tar it together with config/certs into one file
#   2) print the ABSOLUTE PATH of that file as the LAST line of stdout
# Example (adjust container name / paths to match your docker-compose.yml):
#   OUT=/root/pasarguard-backups/pg_$(date +%Y%m%d_%H%M%S).tar.gz && \
#   docker exec pasarguard-db pg_dump -U pasarguard pasarguard > /tmp/pg.sql && \
#   tar czf "$OUT" /tmp/pg.sql /opt/pasarguard/.env /opt/pasarguard/docker-compose.yml && \
#   rm -f /tmp/pg.sql && echo "$OUT"
PASARGUARD_BACKUP_ENABLED = _get_bool("PASARGUARD_BACKUP_ENABLED", False)
PASARGUARD_BACKUP_MODE = os.getenv("PASARGUARD_BACKUP_MODE", "local").strip().lower()
PASARGUARD_BACKUP_COMMAND = os.getenv("PASARGUARD_BACKUP_COMMAND", "").strip()
PASARGUARD_BACKUP_RETENTION_COUNT = max(1, _get_int("PASARGUARD_BACKUP_RETENTION_COUNT", 14) or 14)
PASARGUARD_BACKUP_INTERVAL_SECONDS = max(3600, _get_int("PASARGUARD_BACKUP_INTERVAL_SECONDS", 24 * 60 * 60) or 0)
PASARGUARD_BACKUP_DIR = os.getenv("PASARGUARD_BACKUP_DIR", "/root/pasarguard-backups").strip()

# Only read/used when PASARGUARD_BACKUP_MODE=ssh
PASARGUARD_BACKUP_SSH_HOST = os.getenv("PASARGUARD_BACKUP_SSH_HOST", "").strip()
PASARGUARD_BACKUP_SSH_PORT = _get_int("PASARGUARD_BACKUP_SSH_PORT", 22) or 22
PASARGUARD_BACKUP_SSH_USER = os.getenv("PASARGUARD_BACKUP_SSH_USER", "root").strip()
PASARGUARD_BACKUP_SSH_KEY_PATH = os.getenv("PASARGUARD_BACKUP_SSH_KEY_PATH", "").strip()


def pasarguard_backup_configured() -> bool:
    if not PASARGUARD_BACKUP_COMMAND:
        return False
    if PASARGUARD_BACKUP_MODE == "ssh":
        return bool(PASARGUARD_BACKUP_SSH_HOST and PASARGUARD_BACKUP_SSH_USER and PASARGUARD_BACKUP_SSH_KEY_PATH)
    return True


def validate() -> None:
    """Fail early with a clear error when required variables are missing."""
    missing: list[str] = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_IDS:
        missing.append("ADMIN_ID")

    if missing:
        raise SystemExit(
            "متغیر(های) محیطی الزامی تنظیم نشده: " + ", ".join(missing)
        )
