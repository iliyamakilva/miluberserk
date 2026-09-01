[Unit]
Description=Berserk VPN Telegram Bot (berserkv5)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# install.sh مقدار واقعی این دو مسیر رو موقع نصب جایگزین می‌کند
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
ExecStart=__APP_DIR__/venv/bin/python bot.py
Restart=always
RestartSec=5
User=__RUN_USER__

# سخت‌گیری امنیتی سبک؛ اگر به دسترسی بیشتری نیاز پیدا کردی همین‌جا شل کن
NoNewPrivileges=true
PrivateTmp=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=berserk-bot

[Install]
WantedBy=multi-user.target
