#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PORT="${PORT:-8000}"
STATE_DIR="${STATE_DIR:-/tmp/colab-imagegen}"
mkdir -p "$STATE_DIR"

if [ -z "${API_BEARER_TOKEN:-}" ]; then
  echo "[start] API_BEARER_TOKEN is required" >&2
  exit 1
fi

API_LOG="$STATE_DIR/api.log"
TUNNEL_LOG="$STATE_DIR/cloudflared.log"
STATE_FILE="$STATE_DIR/service.env"

if [ -f "$STATE_FILE" ]; then
  # shellcheck source=/dev/null
  source "$STATE_FILE" || true
  if [ -n "${API_PID:-}" ] && kill -0 "$API_PID" >/dev/null 2>&1; then
    echo "[start] existing API process found (pid=$API_PID); stopping first"
    kill "$API_PID" || true
  fi
  if [ -n "${CLOUDFLARED_PID:-}" ] && kill -0 "$CLOUDFLARED_PID" >/dev/null 2>&1; then
    echo "[start] existing cloudflared process found (pid=$CLOUDFLARED_PID); stopping first"
    kill "$CLOUDFLARED_PID" || true
  fi
fi

: > "$API_LOG"
: > "$TUNNEL_LOG"

python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1 >>"$API_LOG" 2>&1 &
API_PID=$!

echo "[start] API pid=$API_PID"

deadline=$((SECONDS + 300))
until curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null; do
  if ! kill -0 "$API_PID" >/dev/null 2>&1; then
    echo "[start] API process exited unexpectedly" >&2
    tail -n 200 "$API_LOG" || true
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "[start] API did not become ready in time" >&2
    tail -n 200 "$API_LOG" || true
    exit 1
  fi
  sleep 2
done

echo "[start] API is healthy"

if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" >>"$TUNNEL_LOG" 2>&1 &
  TUNNEL_MODE="managed"
else
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" >>"$TUNNEL_LOG" 2>&1 &
  TUNNEL_MODE="quick"
fi

CLOUDFLARED_PID=$!
echo "[start] cloudflared pid=$CLOUDFLARED_PID mode=$TUNNEL_MODE"

PUBLIC_URL=""
if [ "$TUNNEL_MODE" = "quick" ]; then
  for _ in $(seq 1 30); do
    PUBLIC_URL="$(grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)"
    if [ -n "$PUBLIC_URL" ]; then
      break
    fi
    sleep 1
  done
fi

cat > "$STATE_FILE" <<STATE
API_PID=$API_PID
CLOUDFLARED_PID=$CLOUDFLARED_PID
PORT=$PORT
API_LOG=$API_LOG
TUNNEL_LOG=$TUNNEL_LOG
TUNNEL_MODE=$TUNNEL_MODE
PUBLIC_URL=$PUBLIC_URL
STATE

echo "[start] state file: $STATE_FILE"
if [ -n "$PUBLIC_URL" ]; then
  echo "[start] public URL: $PUBLIC_URL"
else
  echo "[start] public URL: check $TUNNEL_LOG"
fi
