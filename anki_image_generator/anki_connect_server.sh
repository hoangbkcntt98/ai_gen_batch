#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/batch_logs"
PID_FILE="$LOG_DIR/anki-connect-server.pid"
LOG_FILE="$LOG_DIR/anki-connect-server.log"
UVX="/home/ubuntu/.local/bin/uvx"
COLLECTION="/home/ubuntu/anki/data/collection.anki2"

is_server_pid() {
  local pid="$1" command
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  command="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
  [[ "$command" == *"anki-connect-server api"* ]]
}

start_server() {
  mkdir -p "$LOG_DIR"
  if [[ -f "$PID_FILE" ]] && is_server_pid "$(cat "$PID_FILE")"; then
    echo "AnkiConnect server already running: PID $(cat "$PID_FILE")"
    return 0
  fi
  rm -f "$PID_FILE"

  setsid nohup env ANKICONNECT_COLLECTION_PATH="$COLLECTION" \
    "$UVX" --with httpx anki-connect-server api \
    >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  sleep 2
  if is_server_pid "$pid"; then
    echo "Started AnkiConnect server: PID $pid"
    echo "PID file: $PID_FILE"
    echo "Log: $LOG_FILE"
  else
    rm -f "$PID_FILE"
    echo "AnkiConnect server failed to start. Check: $LOG_FILE" >&2
    return 1
  fi
}

stop_server() {
  local pid
  if [[ ! -f "$PID_FILE" ]]; then
    echo "AnkiConnect server not running (no PID file)."
    return 0
  fi
  pid="$(cat "$PID_FILE")"
  if ! is_server_pid "$pid"; then
    rm -f "$PID_FILE"
    echo "AnkiConnect server not running; removed stale PID file."
    return 0
  fi
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill "$pid"
  rm -f "$PID_FILE"
  echo "Stopped AnkiConnect server: PID $pid"
}

status_server() {
  if [[ -f "$PID_FILE" ]] && is_server_pid "$(cat "$PID_FILE")"; then
    echo "AnkiConnect server running: PID $(cat "$PID_FILE")"
  else
    echo "AnkiConnect server not running."
    return 1
  fi
}

case "${1:-start}" in
  start) start_server ;;
  stop) stop_server ;;
  status) status_server ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
