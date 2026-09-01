#!/usr/bin/env bash
set -euo pipefail

# ==========================================================================
# berserkv5 — نصب و راه‌اندازی تعاملی روی سرور خودت (Ubuntu 22.04 / 24.04)
#
# استفاده:
#   1) کد بات رو روی سرور بذار (git clone یا scp)
#   2) از داخل ریشه‌ی پروژه اجرا کن:
#        sudo bash deploy/install-server.sh
#   3) سوال‌ها رو جواب بده — بقیه‌ش خودکاره.
#
# اجرای دوباره‌اش امنه (idempotent): اگر .env از قبل باشه، می‌پرسه که
# نگهش داره یا از اول تنظیم کنه؛ سرویس systemd و venv هم به‌روزرسانی می‌شن
# نه از اول ساخته.
# ==========================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
PYTHON_BIN="python3.11"
ENV_FILE="$APP_DIR/.env"

# ---- رنگ‌ها برای خوانایی بهتر (اگر ترمینال پشتیبانی نکنه، بی‌اثره) ----
if [[ -t 1 ]]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_CYAN='\033[36m'; C_RED='\033[31m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_RED=''
fi

say()   { echo -e "${C_CYAN}${C_BOLD}$*${C_RESET}"; }
ok()    { echo -e "${C_GREEN}✔ $*${C_RESET}"; }
warn()  { echo -e "${C_YELLOW}⚠ $*${C_RESET}"; }
err()   { echo -e "${C_RED}✘ $*${C_RESET}"; }

if [[ $EUID -ne 0 ]]; then
  err "این اسکریپت به دسترسی root نیاز دارد."
  echo "اجرا کن: sudo bash deploy/install-server.sh"
  exit 1
fi

clear 2>/dev/null || true
echo -e "${C_BOLD}"
echo "  ____                       _         _   _ ____  _   _ "
echo " | __ )  ___ _ __ ___  ___ | |__     \\ \\   / /  _ \\| \\ | |"
echo " |  _ \\ / _ \\ '__/ __|/ _ \\| '_ \\     \\ \\ / /| |_) |  \\| |"
echo " | |_) |  __/ |  \\__ \\  __/| |_) |     \\ V / |  __/| |\\  |"
echo " |____/ \\___|_|  |___/\\___||_.__/       \\_/  |_|   |_| \\_|"
echo -e "${C_RESET}"
say "نصب‌کننده‌ی تعاملی berserkv5"
echo ""

# --------------------------------------------------------------------------
# توابع کمکی برای پرسیدن ورودی
# --------------------------------------------------------------------------

# prompt_required <label> <var_name> [default]
prompt_required() {
  local label="$1" var_name="$2" default="${3:-}"
  local value=""
  while [[ -z "$value" ]]; do
    if [[ -n "$default" ]]; then
      read -rp "$(echo -e "${C_BOLD}${label}${C_RESET} [${default}]: ")" value
      value="${value:-$default}"
    else
      read -rp "$(echo -e "${C_BOLD}${label}${C_RESET}: ")" value
    fi
    if [[ -z "$value" ]]; then
      warn "این مقدار الزامی است، دوباره وارد کن."
    fi
  done
  printf -v "$var_name" '%s' "$value"
}

# prompt_optional <label> <var_name> [default]
prompt_optional() {
  local label="$1" var_name="$2" default="${3:-}"
  local value=""
  read -rp "$(echo -e "${C_BOLD}${label}${C_RESET} [${default:-خالی}]: ")" value
  value="${value:-$default}"
  printf -v "$var_name" '%s' "$value"
}

# prompt_yesno <label> [default y/n] -> returns 0 for yes, 1 for no
prompt_yesno() {
  local label="$1" default="${2:-n}" answer=""
  local hint="y/N"
  [[ "$default" == "y" ]] && hint="Y/n"
  read -rp "$(echo -e "${C_BOLD}${label}${C_RESET} [${hint}]: ")" answer
  answer="${answer:-$default}"
  [[ "$answer" =~ ^([yY]|بله|آره)$ ]]
}

# --------------------------------------------------------------------------
# مرحله ۰: اگر .env از قبل هست، بپرس چیکار کنیم
# --------------------------------------------------------------------------
SKIP_WIZARD=0
if [[ -f "$ENV_FILE" ]]; then
  warn "یک .env از قبل وجود دارد."
  if prompt_yesno "می‌خوای نگهش داری و فقط نصب/سرویس رو به‌روزرسانی کنم؟" "y"; then
    SKIP_WIZARD=1
    ok ".env دست‌نخورده باقی می‌ماند."
  else
    cp "$ENV_FILE" "${ENV_FILE}.bak.$(date +%s)"
    ok "یک نسخه پشتیبان از .env قدیمی گرفته شد."
  fi
fi

if [[ "$SKIP_WIZARD" -eq 0 ]]; then
  echo ""
  say "== مرحله ۱ از ۳: اطلاعات ضروری =="
  prompt_required "توکن بات (از @BotFather)" BOT_TOKEN
  prompt_required "آیدی عددی ادمین (با کاما جدا کن اگر چند نفرن)" ADMIN_ID
  prompt_optional "دستور مخفی پنل ادمین (به‌جای /admin)" ADMIN_COMMAND "panel_secret"
  prompt_optional "مسیر فایل دیتابیس" DB_PATH "/var/lib/berserk-bot/berserk.db"

  echo ""
  say "== مرحله ۲ از ۳: اتصال به پنل PasarGuard (اختیاری) =="
  echo "بدون این تنظیم هم بات بالا می‌آید؛ فقط پلن‌های «تحویل خودکار» کار نمی‌کنند تا بعداً پرش کنی."
  PG_PANELS_JSON="[]"
  if prompt_yesno "همین الان یک پنل PasarGuard وصل کنم؟" "n"; then
    prompt_required "آدرس پنل (مثل https://panel.example.com)" PG_BASE_URL
    prompt_required "یوزرنیم ادمین پنل" PG_USERNAME
    prompt_required "پسورد ادمین پنل" PG_PASSWORD
    prompt_optional "نام inbound (اگر چندتاست با کاما جدا کن)" PG_INBOUND "REALITY-1"
    IFS=',' read -ra PG_INBOUND_ARR <<< "$PG_INBOUND"
    PG_INBOUND_JSON=$(printf '"%s",' "${PG_INBOUND_ARR[@]}")
    PG_INBOUND_JSON="[${PG_INBOUND_JSON%,}]"
    PG_PANELS_JSON="[{\"key\":\"pasarguard_main\",\"label\":\"پنل اصلی\",\"base_url\":\"${PG_BASE_URL%/}\",\"username\":\"${PG_USERNAME}\",\"password\":\"${PG_PASSWORD}\",\"inbounds\":{\"vless\":${PG_INBOUND_JSON}},\"flow\":\"\",\"verify_ssl\":true,\"timeout\":20}]"
    ok "تنظیمات پنل ذخیره شد."
  else
    warn "رد شد. بعداً می‌تونی PASARGUARD_PANELS_JSON رو داخل .env دستی پر کنی."
  fi

  echo ""
  say "== مرحله ۳ از ۳: عضویت اجباری کانال (اختیاری) =="
  echo "این بخش رو هر وقت خواستی از داخل خودِ ربات (تنظیمات ⚙️) هم می‌تونی روشن/خاموش کنی."
  FORCE_JOIN_NOTE="می‌تونی از پنل ادمین ربات (تنظیمات) فعالش کنی."

  # --- نوشتن .env نهایی ---
  cat > "$ENV_FILE" <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
ADMIN_COMMAND=${ADMIN_COMMAND}
OWNER_ID=
DB_PATH=${DB_PATH}
REF_REWARD=30000
MAX_TOTAL_REFERRALS=50
MAX_REFERRALS_PER_DAY=10
BROADCAST_DELAY=0.08
BACKUP_INTERVAL_SECONDS=86400
BACKUP_RETENTION_COUNT=30
YOUPANEL_BASE_URL=
YOUPANEL_USERNAME=
YOUPANEL_PASSWORD=
YOUPANEL_TIMEOUT_SECONDS=20
YOUPANEL_VERIFY_SSL=true
YOUPANEL_INBOUNDS_JSON={}
PASARGUARD_PANELS_JSON=${PG_PANELS_JSON}
TRIAL_PROVIDER_KEY=youpanel
TRIAL_ENABLED=true
TRIAL_SIZE_MB=200
TRIAL_DAYS=1
TRIAL_MAX_DEVICES=1
PASARGUARD_BACKUP_ENABLED=false
PASARGUARD_BACKUP_MODE=local
PASARGUARD_BACKUP_COMMAND=
PASARGUARD_BACKUP_DIR=/root/pasarguard-backups
PASARGUARD_BACKUP_RETENTION_COUNT=14
PASARGUARD_BACKUP_INTERVAL_SECONDS=86400
PASARGUARD_BACKUP_SSH_HOST=
PASARGUARD_BACKUP_SSH_PORT=22
PASARGUARD_BACKUP_SSH_USER=root
PASARGUARD_BACKUP_SSH_KEY_PATH=
EOF
  chmod 600 "$ENV_FILE"
  ok ".env ساخته شد. ($FORCE_JOIN_NOTE)"
  mkdir -p "$(dirname "$DB_PATH")" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# نصب پیش‌نیازها + venv + سرویس (idempotent، مستقل از اینکه ویزارد بالا اجرا شد یا نه)
# --------------------------------------------------------------------------
echo ""
say "== نصب پیش‌نیازهای سیستم =="
# نکته: aiohttp==3.8.6 (پین‌شده در requirements.txt) روی پایتون 3.12
# (پیش‌فرض Ubuntu 24.04) wheel آماده ندارد و build آن معمولاً fail می‌شود؛
# برای همین دقیقاً Python 3.11 (هم‌نسخه‌ی Railway) نصب می‌کنیم.
apt-get update -y -qq
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  apt-get install -y -qq software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y -qq
fi
apt-get install -y -qq "$PYTHON_BIN" "${PYTHON_BIN}-venv" "${PYTHON_BIN}-dev" build-essential libssl-dev libffi-dev
ok "پایتون و پیش‌نیازها نصب شدند."

say "== ساخت virtualenv و نصب وابستگی‌ها =="
cd "$APP_DIR"
if [[ ! -d venv ]]; then
  "$PYTHON_BIN" -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate
ok "وابستگی‌های پایتون نصب شدند."

say "== نصب سرویس systemd =="
SERVICE_SRC="$APP_DIR/deploy/berserk-bot.service"
SERVICE_DST="/etc/systemd/system/berserk-bot.service"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" "$SERVICE_SRC" > "$SERVICE_DST"
systemctl daemon-reload
systemctl enable berserk-bot.service -q
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"
ok "سرویس systemd آماده شد."

echo ""
say "🎉 نصب کامل شد!"
echo ""
if prompt_yesno "همین الان بات رو استارت کنم؟" "y"; then
  systemctl restart berserk-bot
  sleep 2
  if systemctl is-active --quiet berserk-bot; then
    ok "بات روشن است."
  else
    err "بات بالا نیامد. لاگ رو ببین: journalctl -u berserk-bot -n 50 --no-pager"
  fi
else
  warn "هر وقت آماده بودی: sudo systemctl start berserk-bot"
fi

echo ""
echo "دستورهای مفید:"
echo "  sudo systemctl status berserk-bot    # وضعیت"
echo "  journalctl -u berserk-bot -f         # لاگ زنده"
echo "  sudo systemctl restart berserk-bot   # ری‌استارت بعد از هر تغییر کد یا .env"
echo "  nano $ENV_FILE                       # ویرایش تنظیمات (بعدش حتماً restart کن)"
