#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$ROOT_DIR/scripts"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/ops.sh start
  bash scripts/ops.sh status
  bash scripts/ops.sh stop
  bash scripts/ops.sh restart
  bash scripts/ops.sh verify-uiparse [--base-url <url>] [--expect-engine-mode <mode>] [--image <path>] [--timeout-sec <n>] [--dump-json]
  bash scripts/ops.sh verify-uiparse-smoke
  bash scripts/ops.sh verify-regression
  bash scripts/ops.sh measure-uiparse-coldstart [--base-url <url>] [--expect-engine-mode <mode>] [--image <path>] [--timeout-sec <n>] [--runs <n>] [--restart-cmd <cmd>] [--restart-wait-timeout-sec <n>] [--pause-sec <n>]
  bash scripts/ops.sh recycle --endpoint <assignment-endpoint> [--pkg <npx-package-spec>] [--dry-run]

Commands:
  start      Start API + cloudflared tunnel.
  status     Show current process and tunnel status.
  stop       Stop API + cloudflared and remove state file.
  restart    Stop then start.
  verify-uiparse
             Verify /ui/parse and print evidence fields for M5 acceptance.
  verify-uiparse-smoke
             Run local mock + native import smoke regression for /ui/parse.
  verify-regression
             Run image/asr/ui_parse compatibility regression (mock + native import smoke).
  measure-uiparse-coldstart
             Measure /ui/parse cold-start/warm-start latency across samples.
  recycle    Stop service, then remove Colab assignment (if endpoint provided).

Options (verify-uiparse):
  --base-url            Service base URL. Defaults to http://127.0.0.1:$PORT from state/env.
  --expect-engine-mode  Expected engine mode, default: native.
  --image               Optional local image path for parsing.
  --timeout-sec         HTTP timeout seconds, default: 180.
  --dump-json           Print full health/ui_parse JSON.

Options (recycle):
  --endpoint   Colab assignment endpoint, for example: m-s-abc123
  --pkg        npx --package spec for colab-cli.
               Defaults to COLAB_CLI_PKG env var when set.
  --dry-run    Print recycle command without executing.
EOF
}

run_script() {
  local script_name="$1"
  shift
  bash "$SCRIPTS_DIR/$script_name" "$@"
}

cmd_start() {
  run_script "start_service.sh"
}

cmd_status() {
  run_script "service_status.sh"
}

cmd_stop() {
  run_script "stop_service.sh"
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_verify_uiparse() {
  local state_dir="${STATE_DIR:-/tmp/colab-imagegen}"
  local state_file="$state_dir/service.env"
  local base_url="${VERIFY_BASE_URL:-}"
  local expect_mode="native"
  local image_path=""
  local timeout_sec="180"
  local dump_json="0"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --base-url)
        base_url="${2:-}"
        shift 2
        ;;
      --expect-engine-mode)
        expect_mode="${2:-}"
        shift 2
        ;;
      --image)
        image_path="${2:-}"
        shift 2
        ;;
      --timeout-sec)
        timeout_sec="${2:-}"
        shift 2
        ;;
      --dump-json)
        dump_json="1"
        shift
        ;;
      *)
        echo "[ops] unknown option for verify-uiparse: $1" >&2
        usage
        return 2
        ;;
    esac
  done

  if [ -z "$base_url" ]; then
    local port="${PORT:-}"
    if [ -z "$port" ] && [ -f "$state_file" ]; then
      # shellcheck source=/dev/null
      source "$state_file" || true
      port="${PORT:-}"
    fi
    if [ -z "$port" ]; then
      port="8000"
    fi
    base_url="http://127.0.0.1:${port}"
  fi

  if [ -z "${API_BEARER_TOKEN:-}" ]; then
    echo "[ops] API_BEARER_TOKEN is required for verify-uiparse" >&2
    return 2
  fi

  local cmd=(python "$SCRIPTS_DIR/verify_uiparse_native.py"
    --base-url "$base_url"
    --expect-engine-mode "$expect_mode"
    --timeout-sec "$timeout_sec")

  if [ -n "$image_path" ]; then
    cmd+=(--image "$image_path")
  fi
  if [ "$dump_json" = "1" ]; then
    cmd+=(--dump-json)
  fi

  echo "[ops] verify-uiparse base_url=$base_url expect_engine_mode=$expect_mode"
  "${cmd[@]}"
}

cmd_verify_uiparse_smoke() {
  python "$SCRIPTS_DIR/verify_uiparse_smoke.py"
}

cmd_verify_regression() {
  python "$SCRIPTS_DIR/verify_runtime_regression.py"
}

cmd_measure_uiparse_coldstart() {
  if [ -z "${API_BEARER_TOKEN:-}" ]; then
    echo "[ops] API_BEARER_TOKEN is required for measure-uiparse-coldstart" >&2
    return 2
  fi
  python "$SCRIPTS_DIR/measure_uiparse_coldstart.py" "$@"
}

cmd_recycle() {
  local endpoint=""
  local pkg="${COLAB_CLI_PKG:-}"
  local dry_run="0"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --endpoint)
        endpoint="${2:-}"
        shift 2
        ;;
      --pkg)
        pkg="${2:-}"
        shift 2
        ;;
      --dry-run)
        dry_run="1"
        shift
        ;;
      *)
        echo "[ops] unknown option for recycle: $1" >&2
        usage
        return 2
        ;;
    esac
  done

  cmd_stop

  if [ -z "$endpoint" ]; then
    echo "[ops] recycle: no --endpoint provided, skip assignment removal"
    return 0
  fi

  if [ -z "$pkg" ]; then
    echo "[ops] recycle: COLAB_CLI_PKG or --pkg is required when endpoint is provided" >&2
    return 2
  fi

  local remove_cmd=(npx --yes --package="$pkg" colab-cli -- assign rm "$endpoint")
  if [ "$dry_run" = "1" ]; then
    echo "[ops] dry-run: ${remove_cmd[*]}"
    return 0
  fi

  echo "[ops] removing assignment endpoint=$endpoint"
  "${remove_cmd[@]}"
}

main() {
  local command="${1:-help}"
  shift || true

  case "$command" in
    start)
      cmd_start "$@"
      ;;
    status)
      cmd_status "$@"
      ;;
    stop)
      cmd_stop "$@"
      ;;
    restart)
      cmd_restart "$@"
      ;;
    verify-uiparse)
      cmd_verify_uiparse "$@"
      ;;
    verify-uiparse-smoke)
      cmd_verify_uiparse_smoke "$@"
      ;;
    verify-regression)
      cmd_verify_regression "$@"
      ;;
    measure-uiparse-coldstart)
      cmd_measure_uiparse_coldstart "$@"
      ;;
    recycle)
      cmd_recycle "$@"
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      echo "[ops] unknown command: $command" >&2
      usage
      return 2
      ;;
  esac
}

main "$@"
