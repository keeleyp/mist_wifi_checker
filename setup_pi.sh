#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_pi.sh  —  Runs ON the Raspberry Pi (called by deploy.sh)
#
# What it does:
#   1. Installs Python 3 venv support via apt
#   2. Creates a virtual environment and installs dependencies
#   3. Creates (or updates) a .env config file if one doesn't exist yet
#   4. Installs a systemd service so the app starts on boot
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PI_USER="${1:-pi}"
APP_DIR="/home/${PI_USER}/mist_wifi_checker"
SERVICE_NAME="mist-wifi-checker"

echo ""
echo "── System packages ───────────────────────────────────────────────────────"
sudo apt-get update -q
sudo apt-get install -y python3-venv python3-pip

echo ""
echo "── Python virtual environment ────────────────────────────────────────────"
cd "${APP_DIR}"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "Dependencies installed."

# ── Create .env if it doesn't already exist ───────────────────────────────────
if [ ! -f "${APP_DIR}/.env" ]; then
  echo ""
  echo "── Creating .env config file ─────────────────────────────────────────────"
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo ""
  echo "⚠️   A default .env has been created at ${APP_DIR}/.env"
  echo "    Edit it with your real credentials before the service will work:"
  echo ""
  echo "    nano ${APP_DIR}/.env"
  echo ""
else
  echo ".env already exists — leaving it unchanged."
fi

# ── systemd service ───────────────────────────────────────────────────────────
echo ""
echo "── Installing systemd service ────────────────────────────────────────────"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Mist WiFi Client Checker
Documentation=https://github.com/your-repo
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/gunicorn \\
    --bind 0.0.0.0:8080 \\
    --workers 2 \\
    --timeout 30 \\
    --access-logfile - \\
    app:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo ""
echo "── Service status ────────────────────────────────────────────────────────"
sudo systemctl status "${SERVICE_NAME}" --no-pager -l || true

echo ""
echo "✅  Setup complete!"
echo ""
