#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_pi.sh  —  Runs ON the Raspberry Pi (called by deploy.sh, or by hand)
#
# Usage (from the app folder):
#   bash setup_pi.sh               # service runs as the current user
#   bash setup_pi.sh myuser        # service runs as 'myuser'
#
# What it does:
#   1. Installs Python 3 venv support via apt
#   2. Creates (or rebuilds) the virtual environment and installs dependencies
#   3. Creates config.ini from config.ini.example if one doesn't exist yet
#   4. Installs a systemd service so the app starts on boot and restarts on
#      failure. The app reads its settings, including the port (default 80),
#      from config.ini every time it starts.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_USER="${1:-${SUDO_USER:-$(id -un)}}"
SERVICE_NAME="mist-wifi-checker"
CONFIG="${APP_DIR}/config.ini"

if [ "${PI_USER}" = "root" ]; then
  echo "❌  Refusing to run the service as root. Pass a user: bash setup_pi.sh <user>"
  exit 1
fi

echo ""
echo "── System packages ───────────────────────────────────────────────────────"
sudo apt-get update -q
sudo apt-get install -y python3-venv python3-pip

echo ""
echo "── Python virtual environment ────────────────────────────────────────────"
cd "${APP_DIR}"
# A venv only works with the Python it was built from. If it is missing its
# interpreter (e.g. after an OS / Python upgrade), rebuild it from scratch.
if [ -d .venv ] && ! .venv/bin/python3 --version > /dev/null 2>&1; then
  echo "Existing .venv is broken (no working Python) — rebuilding it."
  rm -rf .venv
fi
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "Dependencies installed ($(.venv/bin/python3 --version))."

# ── Create config.ini if it doesn't already exist ─────────────────────────────
NEEDS_CONFIG=false
if [ ! -f "${CONFIG}" ]; then
  echo ""
  echo "── Creating config.ini ───────────────────────────────────────────────────"
  cp "${APP_DIR}/config.ini.example" "${CONFIG}"
  chmod 600 "${CONFIG}"
  NEEDS_CONFIG=true
else
  echo "config.ini already exists — leaving it unchanged."
fi

APP_PORT="$(.venv/bin/python3 -c "import configparser; c = configparser.ConfigParser(); c.read('${CONFIG}'); print(c.get('mist', 'port', fallback='80'))")"

# ── systemd service ───────────────────────────────────────────────────────────
echo ""
echo "── Installing systemd service ────────────────────────────────────────────"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Mist WiFi Client Checker
Documentation=https://github.com/keeleyp/mist_wifi_checker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${PI_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python3 app.py
# Lets the app listen on ports below 1024 (e.g. 80) without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

if [ "${NEEDS_CONFIG}" = true ]; then
  echo ""
  echo "⚠️   A default config.ini has been created at ${CONFIG}"
  echo "    Add your Mist api_token, org_id and base_url, then start the service:"
  echo ""
  echo "    nano ${CONFIG}"
  echo "    sudo systemctl restart ${SERVICE_NAME}"
  echo ""
  exit 0
fi

sudo systemctl restart "${SERVICE_NAME}"

echo ""
echo "── Service status ────────────────────────────────────────────────────────"
sudo systemctl status "${SERVICE_NAME}" --no-pager -l || true

echo ""
echo "✅  Setup complete!  The app is listening on port ${APP_PORT}."
echo "    Logs: sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
