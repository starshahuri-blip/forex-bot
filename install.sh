#!/usr/bin/env bash
# One-time setup. Asks for your OANDA details, tests the connection, and
# installs the bot as a background service that restarts itself automatically.
#
#   bash install.sh         -> practice (fake money)
#   bash install.sh live    -> switch to real money
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
MODE="practice"

if [ "$1" = "live" ]; then
  echo ""
  echo "You are about to switch this bot to REAL MONEY."
  read -rp "Type LIVE in capitals to continue: " OK </dev/tty
  [ "$OK" = "LIVE" ] || { echo "Cancelled. Nothing changed."; exit 1; }
  MODE="live"
  rm -f .env
fi

if [ ! -f .env ]; then
  echo ""
  read -rp "Paste your OANDA $MODE API token, then press Enter: " TOKEN </dev/tty
  read -rp "Paste your OANDA $MODE account ID (looks like 101-001-1234567-001): " ACCT </dev/tty
  printf 'OANDA_TOKEN=%s\nOANDA_ACCOUNT=%s\nOANDA_ENV=%s\n' "$TOKEN" "$ACCT" "$MODE" > .env
  chmod 600 .env
fi

echo ""
if ! python3 bot.py --check; then
  rm -f .env
  echo "Could not connect. Check the token and account ID, then run this again."
  exit 1
fi

sudo tee /etc/systemd/system/forexbot.service >/dev/null <<EOF
[Unit]
Description=Forex trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=$(whoami)
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/bot.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable forexbot >/dev/null 2>&1
sudo systemctl restart forexbot
sleep 3
echo ""
if systemctl is-active --quiet forexbot; then
  echo "DONE. The bot is running in $MODE mode and will restart itself if the server reboots."
  echo "See what it is doing:   tail -n 30 $DIR/bot.log"
else
  echo "The bot did not start. Show this to Claude:"
  sudo journalctl -u forexbot -n 20 --no-pager
fi
