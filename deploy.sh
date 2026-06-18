#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh  —  Push the Mist WiFi Checker to a Raspberry Pi and start it
#
# Usage:
#   ./deploy.sh                          # defaults: pi@raspberrypi.local
#   ./deploy.sh 192.168.1.42             # custom IP, default user 'pi'
#   ./deploy.sh 192.168.1.42 myuser      # custom IP and user
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PI_HOST="${1:-raspberrypi.local}"
PI_USER="${2:-pi}"
REMOTE="${PI_USER}@${PI_HOST}"
REMOTE_DIR="/home/${PI_USER}/mist_wifi_checker"

# Shared SSH control socket — one password prompt, reused for everything
SSH_SOCKET="/tmp/mist-deploy-${PI_USER}-${PI_HOST}.sock"
SSH_OPTS="-o ControlMaster=auto -o ControlPath=${SSH_SOCKET} -o ControlPersist=60 -o PasswordAuthentication=yes"

echo ""
echo "▶  Deploying Mist WiFi Checker to ${REMOTE}:${REMOTE_DIR}"
echo ""

# ── 0. Open the shared connection (single password prompt) ───────────────────
echo "🔑  Connecting to ${PI_HOST} (you will be prompted for your password once)..."
ssh ${SSH_OPTS} "${REMOTE}" "mkdir -p ${REMOTE_DIR}"

# ── 1. Copy files (reuses the open connection) ───────────────────────────────
echo ""
echo "📦  Syncing files..."
rsync -avz --progress \
  -e "ssh ${SSH_OPTS}" \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'deploy.sh' \
  "$(dirname "$0")/" \
  "${REMOTE}:${REMOTE_DIR}/"

# ── 2. Run the setup script on the Pi (reuses the open connection) ───────────
echo ""
echo "🔧  Running setup on the Pi..."
ssh ${SSH_OPTS} "${REMOTE}" "bash ${REMOTE_DIR}/setup_pi.sh ${PI_USER}"

# ── 3. Close the shared connection ───────────────────────────────────────────
ssh -O exit -o ControlPath="${SSH_SOCKET}" "${REMOTE}" 2>/dev/null || true

echo ""
echo "✅  Done!  The service is running on http://${PI_HOST}:8080"
echo ""
echo "   Useful commands on the Pi:"
echo "   sudo systemctl status mist-wifi-checker"
echo "   sudo journalctl -u mist-wifi-checker -f"
echo ""
