#!/usr/bin/env bash
# One-shot installer for the autodl-openai-gateway on a small Linux VPS.
# Run as a regular user with sudo access; the script will sudo internally.
#
#   bash deploy/install.sh           # install to /opt/autodl-openai-gateway
#   GATEWAY_PORT=9000 bash deploy/install.sh
#
# Re-running is safe: it reinstalls the venv in place and re-enables the
# systemd unit.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/autodl-openai-gateway}"
SERVICE_NAME="${SERVICE_NAME:-autodl-openai-gateway}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required (this installer targets systemd hosts)" >&2
    exit 1
fi

echo "==> Installing into $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$USER":"$USER" "$INSTALL_DIR"

# Copy the project (assumes we're running from the repo root).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
sudo rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.git' \
    "$PROJECT_ROOT"/ "$INSTALL_DIR"/

echo "==> Creating venv + installing dependencies"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q -U pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo "==> Installing systemd unit"
sudo cp "$INSTALL_DIR/deploy/$SERVICE_NAME.service" \
    /etc/systemd/system/$SERVICE_NAME.service
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo "==> Verifying"
sleep 1
if curl -fsS "http://127.0.0.1:${GATEWAY_PORT:-8000}/healthz" >/dev/null; then
    echo "==> OK: gateway is responding on :${GATEWAY_PORT:-8000}"
else
    echo "==> WARNING: /healthz did not return 200; check:" >&2
    echo "    sudo journalctl -u $SERVICE_NAME -n 50" >&2
    exit 1
fi

echo
echo "Done. Useful commands:"
echo "  sudo systemctl status  $SERVICE_NAME"
echo "  sudo journalctl -fu     $SERVICE_NAME"
echo "  sudo systemctl restart  $SERVICE_NAME"
