#!/usr/bin/env bash
set -euo pipefail

# ==========================================================================
# berserkv5 — تغییر سریع تنظیمات بعد از نصب (بدون ادیت دستی .env)
#
# استفاده:
#   sudo bash deploy/configure.sh
# ==========================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

if [[ $EUID -ne 0 ]]; then
  err "این اسکریپت به دسترسی root نیاز دارد."
  echo "اجرا کن: sudo bash deploy/configure.sh"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  err ".env پیدا نشد. اول نصب کن: sudo bash deploy/install-server.sh"
  exit 1
fi

get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }

set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

mask() {
  local v="$1"
  local len=${#v}
  if [[ $len -le 6 ]]; then echo "******"; else echo "${v:0:3}***${v: -3}"; fi
}

CHANGED=0

while true; do
  echo ""
  say "=== تنظیمات berserkv5 — چی رو می‌خوای عوض کنی؟ ==="
  echo "1) توکن بات"
  echo "2) آیدی ادمین"
  echo "3) اضافه/ویرایش پنل PasarGuard"
  echo "4) نمایش تنظیمات فعلی (خلاصه)"
  echo "5) پایان و ری‌استارت (اگر تغییری داده باشی)"
  read -rp "$(echo -e "${C_BOLD}عدد رو انتخاب کن: ${C_RESET}")" CHOICE

  case "$CHOICE" in
    1)
      CURRENT=$(get_env BOT_TOKEN)
      echo "توکن فعلی: $(mask "$CURRENT")"
      read -rp "توکن جدید (Enter برای بی‌خیال‌شدن): " NEW_TOKEN
      if [[ -n "$NEW_TOKEN" ]]; then
        set_env BOT_TOKEN "$NEW_TOKEN"
        ok "توکن بات به‌روزرسانی شد."
        CHANGED=1
      fi
      ;;
    2)
      CURRENT=$(get_env ADMIN_ID)
      echo "آیدی فعلی: ${CURRENT:-خالی}"
      read -rp "آیدی جدید، با کاما جدا اگر چندتاست (Enter برای بی‌خیال‌شدن): " NEW_ADMIN
      if [[ -n "$NEW_ADMIN" ]]; then
        set_env ADMIN_ID "$NEW_ADMIN"
        ok "آیدی ادمین به‌روزرسانی شد."
        CHANGED=1
      fi
      ;;
    3)
      echo "اطلاعات پنل PasarGuard رو وارد کن (فعلاً فقط یک پنل پشتیبانی می‌شود؛ اگر چند پنل داری، بعداً دستی JSON را گسترش بده):"
      read -rp "آدرس پنل (مثل https://panel.example.com): " PG_BASE_URL
      read -rp "یوزرنیم ادمین پنل: " PG_USERNAME
      read -rp "پسورد ادمین پنل: " PG_PASSWORD
      read -rp "شناسه‌ی گروه (Group) در پنل — تو پنل خودت بخش Groups ببین (اگر چندتاست با کاما جدا کن) [1]: " PG_GROUPS
      PG_GROUPS="${PG_GROUPS:-1}"
      IFS=',' read -ra PG_GROUPS_ARR <<< "$PG_GROUPS"
      PG_GROUPS_JSON=$(printf '%s,' "${PG_GROUPS_ARR[@]}")
      PG_GROUPS_JSON="[${PG_GROUPS_JSON%,}]"
      PG_JSON="[{\"key\":\"pasarguard_main\",\"label\":\"پنل اصلی\",\"base_url\":\"${PG_BASE_URL%/}\",\"username\":\"${PG_USERNAME}\",\"password\":\"${PG_PASSWORD}\",\"group_ids\":${PG_GROUPS_JSON},\"verify_ssl\":true,\"timeout\":20}]"
      set_env PASARGUARD_PANELS_JSON "$PG_JSON"
      ok "پنل PasarGuard ذخیره شد."
      CHANGED=1
      ;;
    4)
      echo ""
      echo "BOT_TOKEN         = $(mask "$(get_env BOT_TOKEN)")"
      echo "ADMIN_ID          = $(get_env ADMIN_ID)"
      echo "ADMIN_COMMAND     = $(get_env ADMIN_COMMAND)"
      echo "DB_PATH           = $(get_env DB_PATH)"
      PG_RAW=$(get_env PASARGUARD_PANELS_JSON)
      if [[ "$PG_RAW" == "[]" || -z "$PG_RAW" ]]; then
        echo "PasarGuard        = تنظیم نشده"
      else
        echo "PasarGuard        = تنظیم شده ✔"
      fi
      ;;
    5)
      break
      ;;
    *)
      warn "گزینه‌ی نامعتبر."
      ;;
  esac
done

if [[ "$CHANGED" -eq 1 ]]; then
  say "در حال ری‌استارت سرویس..."
  systemctl restart berserk-bot
  sleep 2
  if systemctl is-active --quiet berserk-bot; then
    ok "بات با تنظیمات جدید روشن است."
  else
    err "بات بالا نیامد. لاگ رو ببین: journalctl -u berserk-bot -n 50 --no-pager"
  fi
else
  echo "تغییری اعمال نشد."
fi
