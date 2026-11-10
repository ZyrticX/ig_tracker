#!/usr/bin/env bash
# התקנה על שרת Linux (Ubuntu/Debian) כשירות systemd שעולה לבד אחרי קריסה או ריסטארט.
# הרצה מתוך תיקיית הפרויקט בשרת:  bash deploy/install.sh
# אפשר להריץ שוב אחרי עדכון קוד: מתקין תלויות ומפעיל מחדש.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

[ -f .env ] || cp .env.example .env
chmod 600 .env

if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" 2>/dev/null; then
    sudo apt-get update -q && sudo apt-get install -y -q python3 python3-venv
fi
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

sudo tee /etc/systemd/system/ig-tracker.service >/dev/null <<EOF
[Unit]
Description=IG Tracker (Instagram followers + Telegram bot)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
# לא מפעילים עם ערכי דוגמה: זה היה מנסה להתחבר לאינסטגרם עם פרטים מזויפים
if grep -qE "^(IG_USERNAME=your_ig_account|TELEGRAM_BOT_TOKEN=123456:ABC)" .env; then
    echo "Installed, NOT started: fill $DIR/.env, then run: bash deploy/install.sh"
    exit 0
fi
sudo systemctl enable ig-tracker >/dev/null
sudo systemctl restart ig-tracker
sleep 3
systemctl --no-pager --lines=5 status ig-tracker || true
echo
echo "Done. Live logs: journalctl -u ig-tracker -f"
