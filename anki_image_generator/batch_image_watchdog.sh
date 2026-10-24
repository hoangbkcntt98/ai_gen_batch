#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WATCH_DIR="$SCRIPT_DIR/batch_logs"
PID_FILE="$WATCH_DIR/watchdog.pid"
LOCK_FILE="$WATCH_DIR/watchdog.loop.lock"
LOG_FILE="$WATCH_DIR/watchdog.log"
SCRIPT_NAME="$(basename -- "$0")"
WATCH_INTERVAL_SECONDS="${WATCH_INTERVAL_SECONDS:-3600}"  # Default to 1 hour

find_watchdog_pid() {
  local pid command
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
      if [[ "$command" == *"$SCRIPT_DIR/$SCRIPT_NAME loop"* ]]; then
        echo "$pid"
        return 0
      fi
    fi
  fi

  while read -r pid command; do
    [[ "$pid" == "$$" ]] && continue
    if [[ "$command" == *"$SCRIPT_DIR/$SCRIPT_NAME loop"* ]]; then
      echo "$pid"
      return 0
    fi
  done < <(ps -eo pid=,args=)
  return 1
}

start_watchdog() {
  local running_pid
  if running_pid="$(find_watchdog_pid)"; then
    echo "$running_pid" >"$PID_FILE"
    echo "Watchdog already running: PID $running_pid"
    return 0
  fi

  mkdir -p "$WATCH_DIR"
  nohup "$SCRIPT_DIR/$SCRIPT_NAME" loop >>"$LOG_FILE" 2>&1 </dev/null &
  echo "$!" >"$PID_FILE"
  echo "Started watchdog: PID $!"
  echo "Log: $LOG_FILE"
}

stop_watchdog() {
  local pid
  if ! pid="$(find_watchdog_pid)"; then
    rm -f "$PID_FILE"
    echo "Watchdog not running."
    return 0
  fi

  echo "$pid" >"$PID_FILE"
  kill "$pid"
  while read -r child command; do
    if [[ "$command" == sleep* ]]; then
      kill "$child" 2>/dev/null || true
    fi
  done < <(ps -o pid=,args= --ppid "$pid" 2>/dev/null || true)
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
    if [[ -f "$PID_FILE" && "$(cat "$PID_FILE")" == "$$" ]]; then
      rm -f "$PID_FILE"
    fi
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
        "$SCRIPT_DIR/run_batch.sh" 9>&- </dev/null || \
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Could not start batch; see output above."
    fi

    if pgrep -f '[w]ord_kanji_generate\.py' >/dev/null; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Word/kanji batch active; skipping."
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] No word/kanji batch active; starting batch."
      nohup "$SCRIPT_DIR/.venv/bin/python" -u \
        "$SCRIPT_DIR/word_kanji_generate.py" \
        --config "$SCRIPT_DIR/config.json" \
        --type all \
        >>"$WATCH_DIR/word_kanji_generate.log" 2>&1 9>&- </dev/null &
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Started word/kanji batch: PID $!"
    fi
    sleep "$WATCH_INTERVAL_SECONDS"
  done
}

case "${1:-start}" in
  start) start_watchdog ;;
  stop) stop_watchdog ;;
  status)
    if pid="$(find_watchdog_pid)"; then
      echo "$pid" >"$PID_FILE"
      echo "Watchdog running: PID $pid"
    else
      rm -f "$PID_FILE"
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
