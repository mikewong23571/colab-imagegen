#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${STATE_DIR:-/tmp/colab-imagegen}"
STATE_FILE="$STATE_DIR/service.env"

if [ ! -f "$STATE_FILE" ]; then
  echo "[stop] no state file at $STATE_FILE"
  exit 0
fi

# shellcheck source=/dev/null
source "$STATE_FILE"

if [ -n "${API_PID:-}" ] && kill -0 "$API_PID" >/dev/null 2>&1; then
  echo "[stop] stopping API pid=$API_PID"
  kill "$API_PID" || true
fi

if [ -n "${CLOUDFLARED_PID:-}" ] && kill -0 "$CLOUDFLARED_PID" >/dev/null 2>&1; then
  echo "[stop] stopping cloudflared pid=$CLOUDFLARED_PID"
  kill "$CLOUDFLARED_PID" || true
fi

rm -f "$STATE_FILE"
echo "[stop] done"
