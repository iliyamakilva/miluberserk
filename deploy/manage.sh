#!/usr/bin/env bash
set -euo pipefail

# ==========================================================================
# berserkv5 — پنل مدیریت متنی سرور (منوی شماره‌دار)
#
# استفاده:
#   sudo bash deploy/manage.sh
# ==========================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$APP_DIR/deploy"
ENV_FILE="$APP_DIR/.env"

if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_CYAN='\033[36m'; C_RED='\033[31m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_RED=''
fi
say()  { echo -e "${C_CYAN}${C_BOLD}$*${C_RESET}"; }
ok()   { echo -e "${C_GREEN}✔ $*${C_RESET}"; }
warn() { echo -e "${C_YELLOW}⚠ $*${C_RESET}"; }
err()  { echo -e "${C_RED}✘ $*${C_RESET}"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    err "این گزینه به دسترسی root نیاز دارد. کل اسکریپت رو با sudo اجرا کن."
    return 1
  fi
  return 0
}

show_sales_stats() {
  if [[ ! -f "$ENV_FILE" ]]; then
    err ".env پیدا نشد؛ هنوز نصب نشده."
    return
  fi
  local db_path
  db_path=$(grep -E '^DB_PATH=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  db_path="${db_path:-$APP_DIR/berserk.db}"
  if [[ ! -f "$db_path" ]]; then
    err "فایل دیتابیس پیدا نشد: $db_path"
    return
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    warn "sqlite3 نصب نیست، در حال نصب..."
    apt-get install -y -qq sqlite3
  fi
  echo ""
  say "📊 آمار فروش (مستقیم از دیتابیس)"
  local total_users total_purchases total_revenue today_revenue
  total_users=$(sqlite3 "$db_path" "SELECT COUNT(*) FROM users;")
  total_purchases=$(sqlite3 "$db_path" "SELECT COUNT(*) FROM purchases WHERE status='completed';")
  total_revenue=$(sqlite3 "$db_path" "SELECT COALESCE(SUM(amount),0) FROM purchases WHERE status='completed' AND is_test=0;" 2>/dev/null || echo "0")
  today_revenue=$(sqlite3 "$db_path" "SELECT COALESCE(SUM(amount),0) FROM purchases WHERE status='completed' AND date(created_at)=date('now');" 2>/dev/null || echo "0")
  echo "👤 تعداد کاربران ثبت‌شده : $total_users"
  echo "🛒 تعداد خرید موفق کل    : $total_purchases"
  echo "💰 مجموع درآمد کل        : $total_revenue تومان"
  echo "📅 درآمد امروز           : $today_revenue تومان"
}

pause() {
  echo ""
  read -rp "برای بازگشت به منو، Enter بزن..." _
}

while true; do
  clear 2>/dev/null || true
  echo -e "${C_BOLD}"
  echo " ╔══════════════════════════════════════╗"
  echo " ║      berserkv5 — پنل مدیریت سرور      ║"
  echo " ╚══════════════════════════════════════╝"
  echo -e "${C_RESET}"
  if systemctl list-unit-files 2>/dev/null | grep -q "^berserk-bot.service"; then
    if systemctl is-active --quiet berserk-bot; then
      echo -e "وضعیت فعلی: ${C_GREEN}روشن ✔${C_RESET}"
    else
      echo -e "وضعیت فعلی: ${C_RED}خاموش ✘${C_RESET}"
    fi
  else
    echo -e "وضعیت فعلی: ${C_YELLOW}نصب نشده${C_RESET}"
  fi
  echo ""
  echo "[1] نصب کامل بات (اولین بار)"
  echo "[2] آپدیت بات (git pull + وابستگی‌ها + ری‌استارت)"
  echo "[3] حذف کامل بات از سرور"
  echo "[4] مشاهده وضعیت بات"
  echo "[5] مشاهده لاگ زنده"
  echo "[6] ری‌استارت بات"
  echo "[7] توقف بات"
  echo "[8] مشاهده آمار فروش"
  echo "[9] تغییر توکن / آیدی ادمین / پنل PasarGuard"
  echo "[0] خروج"
  echo ""
  read -rp "$(echo -e "${C_BOLD}یک عدد انتخاب کن: ${C_RESET}")" CHOICE

  case "$CHOICE" in
    1) require_root && bash "$DEPLOY_DIR/install-server.sh"; pause ;;
    2) require_root && bash "$DEPLOY_DIR/update.sh"; pause ;;
    3)
      require_root || { pause; continue; }
      read -rp "دیتابیس و .env هم پاک بشن؟ (yes/no): " PURGE
      if [[ "$PURGE" == "yes" ]]; then
        bash "$DEPLOY_DIR/uninstall-server.sh" --purge-data
      else
        bash "$DEPLOY_DIR/uninstall-server.sh"
      fi
      pause
      ;;
    4) systemctl status berserk-bot --no-pager || true; pause ;;
    5)
      echo "برای خروج از لاگ زنده، Ctrl+C بزن."
      sleep 1
      journalctl -u berserk-bot -f || true
      ;;
    6) require_root && systemctl restart berserk-bot && ok "ری‌استارت شد."; pause ;;
    7) require_root && systemctl stop berserk-bot && ok "متوقف شد."; pause ;;
    8) show_sales_stats; pause ;;
    9) require_root && bash "$DEPLOY_DIR/configure.sh"; pause ;;
    0) exit 0 ;;
    *) warn "گزینه‌ی نامعتبر."; sleep 1 ;;
  esac
done
