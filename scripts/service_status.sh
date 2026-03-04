#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${STATE_DIR:-/tmp/colab-imagegen}"
STATE_FILE="$STATE_DIR/service.env"

if [ ! -f "$STATE_FILE" ]; then
  echo "[status] state file not found: $STATE_FILE"
  exit 1
fi

# shellcheck source=/dev/null
source "$STATE_FILE"

echo "[status] mode=$TUNNEL_MODE port=$PORT"
echo "[status] api_log=$API_LOG"
echo "[status] tunnel_log=$TUNNEL_LOG"

a_pid="stopped"
t_pid="stopped"
if [ -n "${API_PID:-}" ] && kill -0 "$API_PID" >/dev/null 2>&1; then
  a_pid="running($API_PID)"
fi
if [ -n "${CLOUDFLARED_PID:-}" ] && kill -0 "$CLOUDFLARED_PID" >/dev/null 2>&1; then
  t_pid="running($CLOUDFLARED_PID)"
fi

echo "[status] api=$a_pid"
echo "[status] cloudflared=$t_pid"

if [ -n "${PUBLIC_URL:-}" ]; then
  echo "[status] public_url=$PUBLIC_URL"
fi
