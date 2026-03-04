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
  bash scripts/ops.sh recycle --endpoint <assignment-endpoint> [--pkg <npx-package-spec>] [--dry-run]

Commands:
  start      Start API + cloudflared tunnel.
  status     Show current process and tunnel status.
  stop       Stop API + cloudflared and remove state file.
  restart    Stop then start.
  recycle    Stop service, then remove Colab assignment (if endpoint provided).

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
