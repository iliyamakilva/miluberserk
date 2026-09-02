#!/usr/bin/env bash
set -euo pipefail

# ==========================================================================
# berserkv5 — آپدیت سریع (git pull + وابستگی‌ها + ری‌استارت)
#
# استفاده:
#   sudo bash deploy/update.sh
# ==========================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'; C_CYAN='\033[36m'; C_RED='\033[31m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_CYAN=''; C_RED=''
fi
say()  { echo -e "${C_CYAN}${C_BOLD}$*${C_RESET}"; }
ok()   { echo -e "${C_GREEN}✔ $*${C_RESET}"; }
err()  { echo -e "${C_RED}✘ $*${C_RESET}"; }

cd "$APP_DIR"

if [[ ! -d venv ]]; then
  err "venv پیدا نشد؛ اول نصب کن: sudo bash deploy/install-server.sh"
  exit 1
fi

say "== ۱/۳: دریافت آخرین نسخه از گیت‌هاب =="
git pull

say "== ۲/۳: به‌روزرسانی وابستگی‌های پایتون =="
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate
ok "وابستگی‌ها به‌روز شدند."

say "== ۳/۳: ری‌استارت سرویس =="
if [[ $EUID -ne 0 ]]; then
  err "برای ری‌استارت سرویس به دسترسی root نیاز است. اجرا کن: sudo bash deploy/update.sh"
  exit 1
fi
systemctl restart berserk-bot
sleep 2
if systemctl is-active --quiet berserk-bot; then
  ok "🎉 بات با موفقیت آپدیت و ری‌استارت شد."
else
  err "بات بالا نیامد. لاگ رو ببین: journalctl -u berserk-bot -n 50 --no-pager"
  exit 1
fi
