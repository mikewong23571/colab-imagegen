#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[install] Python: $(python --version)"
python -m pip install --upgrade pip wheel setuptools
python -m pip install -r requirements.txt

if command -v cloudflared >/dev/null 2>&1; then
  echo "[install] cloudflared already installed: $(cloudflared --version | head -n 1)"
  exit 0
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)
    BIN_NAME="cloudflared-linux-amd64"
    ;;
  aarch64|arm64)
    BIN_NAME="cloudflared-linux-arm64"
    ;;
  *)
    echo "[install] unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

INSTALL_DIR="/usr/local/bin"
if [ ! -w "$INSTALL_DIR" ]; then
  INSTALL_DIR="$HOME/.local/bin"
  mkdir -p "$INSTALL_DIR"
  export PATH="$INSTALL_DIR:$PATH"
fi

URL="https://github.com/cloudflare/cloudflared/releases/latest/download/${BIN_NAME}"
echo "[install] downloading cloudflared from ${URL}"
curl -fsSL "$URL" -o "${INSTALL_DIR}/cloudflared"
chmod +x "${INSTALL_DIR}/cloudflared"

echo "[install] cloudflared installed: $(cloudflared --version | head -n 1)"
