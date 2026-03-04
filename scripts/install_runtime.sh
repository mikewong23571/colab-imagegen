#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

is_true() {
  local value="${1:-}"
  case "${value,,}" in
    1|true|yes|on)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

echo "[install] Python: $(python --version)"
python -m pip install --upgrade pip wheel setuptools
python -m pip install -r requirements.txt

OMNIPARSER_ENABLED="${OMNIPARSER_ENABLED:-0}"
if is_true "$OMNIPARSER_ENABLED"; then
  OMNIPARSER_REPO_URL="${OMNIPARSER_REPO_URL:-https://github.com/microsoft/OmniParser.git}"
  OMNIPARSER_REPO_REF="${OMNIPARSER_REPO_REF:-master}"
  OMNIPARSER_DIR="${OMNIPARSER_DIR:-/content/.cache/omniparser/repo}"
  OMNIPARSER_WEIGHTS_DIR="${OMNIPARSER_WEIGHTS_DIR:-/content/.cache/omniparser/weights}"
  OMNIPARSER_DOWNLOAD_WEIGHTS="${OMNIPARSER_DOWNLOAD_WEIGHTS:-1}"

  echo "[install][omniparser] enabled=1 repo=${OMNIPARSER_REPO_URL} ref=${OMNIPARSER_REPO_REF}"
  if [ ! -d "${OMNIPARSER_DIR}/.git" ]; then
    mkdir -p "$(dirname "${OMNIPARSER_DIR}")"
    git clone "${OMNIPARSER_REPO_URL}" "${OMNIPARSER_DIR}"
  fi

  git -C "${OMNIPARSER_DIR}" fetch --all --tags
  git -C "${OMNIPARSER_DIR}" checkout "${OMNIPARSER_REPO_REF}"
  python -m pip install -r "${OMNIPARSER_DIR}/requirements.txt"
  # paddleocr/paddlex currently imports langchain.docstore.document at runtime.
  # Newer langchain releases removed this module, so pin to a compatible major.
  python -m pip install "langchain<0.2"

  if is_true "$OMNIPARSER_DOWNLOAD_WEIGHTS"; then
    echo "[install][omniparser] downloading weights to ${OMNIPARSER_WEIGHTS_DIR}"
    mkdir -p "${OMNIPARSER_WEIGHTS_DIR}"
    for f in \
      icon_detect/train_args.yaml \
      icon_detect/model.pt \
      icon_detect/model.yaml \
      icon_caption/config.json \
      icon_caption/generation_config.json \
      icon_caption/model.safetensors
    do
      huggingface-cli download microsoft/OmniParser-v2.0 "$f" --local-dir "${OMNIPARSER_WEIGHTS_DIR}"
    done

    if [ -d "${OMNIPARSER_WEIGHTS_DIR}/icon_caption" ] && [ ! -d "${OMNIPARSER_WEIGHTS_DIR}/icon_caption_florence" ]; then
      mv "${OMNIPARSER_WEIGHTS_DIR}/icon_caption" "${OMNIPARSER_WEIGHTS_DIR}/icon_caption_florence"
    fi
  fi

  echo "[install][omniparser] done"
fi

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
