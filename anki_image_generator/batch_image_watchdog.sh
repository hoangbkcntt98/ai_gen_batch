#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WATCH_DIR="$SCRIPT_DIR/batch_logs"
PID_FILE="$WATCH_DIR/watchdog.pid"
LOCK_FILE="$WATCH_DIR/watchdog.lock"
LOG_FILE="$WATCH_DIR/watchdog.log"
SCRIPT_NAME="$(basename -- "$0")"
WATCH_INTERVAL_SECONDS="${WATCH_INTERVAL_SECONDS:-60}"

watchdog_is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid command
  pid="$(cat "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 1
  command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  [[ "$command" == *"$SCRIPT_NAME loop"* ]]
}

start_watchdog() {
  if watchdog_is_running; then
    echo "Watchdog already running: PID $(cat "$PID_FILE")"
    return 0
  fi

  mkdir -p "$WATCH_DIR"
  nohup "$SCRIPT_DIR/$SCRIPT_NAME" loop >>"$LOG_FILE" 2>&1 </dev/null &
  echo "$!" >"$PID_FILE"
  echo "Started watchdog: PID $!"
  echo "Log: $LOG_FILE"
}

stop_watchdog() {
  if ! watchdog_is_running; then
    rm -f "$PID_FILE"
    echo "Watchdog not running."
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid"
  echo "Stopped watchdog: PID $pid"
}

watch_loop() {
  if [[ ! "$WATCH_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "WATCH_INTERVAL_SECONDS must be a positive integer." >&2
    return 2
  fi

  mkdir -p "$WATCH_DIR"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Watchdog already running; exiting."
    return 0
  fi
  echo "$$" >"$PID_FILE"
  trap 'rm -f "$PID_FILE"' EXIT
  trap 'exit 0' INT TERM

  while true; do
    if pgrep -f '[b]atch_image_generate\.py' >/dev/null; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Image batch active; skipping."
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] No image batch active; starting default batch."
      env \
        CONFIG="$SCRIPT_DIR/config.json" \
        TASK_OPTION=1 \
        LIMIT= \
        STEP=2 \
        RECOVERY=false \
        CLEAN_ERROR=true \
        SET_IMAGES_IF_COMPLETED=true \
        DRY_RUN=false \
        MEDIA_DIR= \
        RUN_IN_BACKGROUND=true \
        "$SCRIPT_DIR/run_batch.sh" </dev/null || \
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Could not start batch; see output above."
    fi
    sleep "$WATCH_INTERVAL_SECONDS"
  done
}

case "${1:-start}" in
  start) start_watchdog ;;
  stop) stop_watchdog ;;
  status)
    if watchdog_is_running; then
      echo "Watchdog running: PID $(cat "$PID_FILE")"
    else
      echo "Watchdog not running."
      exit 1
    fi
    ;;
  loop) watch_loop ;;
  *)
    echo "Usage: $SCRIPT_NAME {start|stop|status}" >&2
    exit 2
    ;;
esac
