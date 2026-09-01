#!/usr/bin/env bash
set -euo pipefail

# ==========================================================================
# berserkv5 — حذف کامل نصب سرور (سرویس systemd + venv)
#
# استفاده:
#   sudo bash deploy/uninstall-server.sh
#
# این اسکریپت پاک می‌کند:
#   - سرویس systemd (berserk-bot.service) - متوقف و غیرفعال و حذف
#   - پوشه‌ی venv/ (محیط مجازی پایتون)
#
# این اسکریپت پاک نمی‌کند (مگر با پرچم صریح --purge-data):
#   - فایل .env (تنظیمات و توکن‌ها)
#   - فایل دیتابیس (DB_PATH) و پوشه‌ی backups/
#   - خود کد پروژه (فایل‌های .py)
# چون این‌ها معمولاً چیزهایی هستن که نمی‌خوای بی‌هوا از دست بری.
# ==========================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$APP_DIR/.env"
PURGE_DATA=0

if [[ "${1:-}" == "--purge-data" ]]; then
  PURGE_DATA=1
fi

if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi
say()  { echo -e "${C_BOLD}$*${C_RESET}"; }
ok()   { echo -e "${C_GREEN}✔ $*${C_RESET}"; }
warn() { echo -e "${C_YELLOW}⚠ $*${C_RESET}"; }
err()  { echo -e "${C_RED}✘ $*${C_RESET}"; }

if [[ $EUID -ne 0 ]]; then
  err "این اسکریپت به دسترسی root نیاز دارد."
  echo "اجرا کن: sudo bash deploy/uninstall-server.sh"
  exit 1
fi

echo ""
warn "این کار سرویس بات را متوقف و حذف می‌کند."
if [[ "$PURGE_DATA" -eq 1 ]]; then
  err "پرچم --purge-data فعال است: .env و دیتابیس هم پاک می‌شوند. این برگشت‌ناپذیر است!"
fi
read -rp "$(echo -e "${C_BOLD}ادامه می‌دهی؟ (yes/no): ${C_RESET}")" CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
  echo "لغو شد."
  exit 0
fi

say "== ۱) توقف و غیرفعال‌کردن سرویس =="
if systemctl list-unit-files | grep -q "^berserk-bot.service"; then
  systemctl stop berserk-bot.service 2>/dev/null || true
  systemctl disable berserk-bot.service 2>/dev/null || true
  rm -f /etc/systemd/system/berserk-bot.service
  systemctl daemon-reload
  ok "سرویس systemd حذف شد."
else
  warn "سرویسی به این اسم پیدا نشد؛ رد می‌شویم."
fi

say "== ۲) حذف virtualenv =="
if [[ -d "$APP_DIR/venv" ]]; then
  rm -rf "$APP_DIR/venv"
  ok "پوشه‌ی venv حذف شد."
else
  warn "venv وجود نداشت؛ رد می‌شویم."
fi

if [[ "$PURGE_DATA" -eq 1 ]]; then
  say "== ۳) پاک‌سازی کامل (--purge-data) =="
  if [[ -f "$ENV_FILE" ]]; then
    rm -f "$ENV_FILE"
    ok ".env حذف شد."
  fi
  DB_PATH_VALUE=$(grep -E '^DB_PATH=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
  if [[ -n "$DB_PATH_VALUE" && -f "$DB_PATH_VALUE" ]]; then
    rm -f "$DB_PATH_VALUE"
    ok "فایل دیتابیس حذف شد: $DB_PATH_VALUE"
  fi
else
  echo ""
  warn "دیتابیس و .env دست‌نخورده باقی ماندند (برای حذف کامل، دوباره با --purge-data اجرا کن)."
fi

echo ""
ok "🗑 حذف نصب سرور کامل شد."
echo "کد پروژه (فایل‌های .py) دست‌نخورده باقی مانده — اگر خواستی خودِ پوشه را هم حذف کنی:"
echo "  rm -rf $APP_DIR"
