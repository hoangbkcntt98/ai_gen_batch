#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/batch_logs"
SELF="$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")"

usage() {
  cat <<'EOF'
Usage:
  ./watcher.sh start FILE.py [--interval 60s] [PYTHON_SCRIPT_ARGS...]
  ./watcher.sh status FILE.py
  ./watcher.sh pid FILE.py
  ./watcher.sh stop FILE.py

Intervals accept seconds (60, 60s), minutes (5m), or hours (1h).
Default interval is 30m.
Unknown options and arguments are passed to FILE.py. Use -- before script
arguments that could be confused with watcher options.
EOF
}

safe_name() {
  local name
  name="$(basename -- "$1")"
  name="${name%.py}"
  name="${name//[^a-zA-Z0-9_.-]/_}"
  printf '%s' "$name"
}

pid_is_watcher() {
  local pid="$1" cmdline
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
  [[ "$cmdline" == *"$SELF"* && "$cmdline" == *"--worker"* ]]
}

remove_pid_if_owner() {
  local pid_file="$1" expected_pid="$2" recorded_pid=""
  [[ -f "$pid_file" ]] || return 0
  IFS= read -r recorded_pid <"$pid_file" || true
  if [[ "$recorded_pid" == "$expected_pid" ]]; then
    rm -f -- "$pid_file"
  fi
}

worker() {
  local script="$1" interval_seconds="$2" python="$3"
  local pid_file="$4" python_pid_file="$5" run_log="$6" watcher_log="$7"
  local lock_file="$8"
  shift 8
  local -a script_args=("$@")
  local child_pid="" exit_code

  cd -- "$SCRIPT_DIR" || exit 1
  export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
  exec 9>"$lock_file"

  stop_worker() {
    trap - TERM INT
    if [[ -n "$child_pid" ]]; then
      kill -TERM "$child_pid" 2>/dev/null || true
      wait "$child_pid" 2>/dev/null || true
    fi
    remove_pid_if_owner "$python_pid_file" "$child_pid"
    flock -u 9 2>/dev/null || true
    printf '%s STOP watcher_pid=%s\n' "$(date -Is)" "$$" >>"$watcher_log"
    remove_pid_if_owner "$pid_file" "$$"
    exit 0
  }
  trap stop_worker TERM INT

  while true; do
    if ! flock -n 9; then
      printf '%s SKIP reason=lock_held lock=%s\n' "$(date -Is)" "$lock_file" \
        >>"$watcher_log"
      sleep "$interval_seconds" &
      child_pid=$!
      wait "$child_pid" || true
      child_pid=""
      continue
    fi

    printf '%s START command=%q' "$(date -Is)" "$python" >>"$watcher_log"
    printf ' %q' "$script" "${script_args[@]}" >>"$watcher_log"
    printf '\n' >>"$watcher_log"

    "$python" -u "$script" "${script_args[@]}" >>"$run_log" 2>&1 &
    child_pid=$!
    printf '%s\n' "$child_pid" >"$python_pid_file"
    wait "$child_pid"
    exit_code=$?
    remove_pid_if_owner "$python_pid_file" "$child_pid"
    child_pid=""
    printf '%s END exit_code=%s\n' "$(date -Is)" "$exit_code" >>"$watcher_log"
    flock -u 9

    sleep "$interval_seconds" &
    child_pid=$!
    wait "$child_pid" || true
    child_pid=""
  done
}

if [[ "${1:-}" == "--worker" ]]; then
  shift
  worker "$@"
  exit $?
fi

if [[ "${1:-}" == "start" ]]; then
  shift
elif [[ "${1:-}" == "status" || "${1:-}" == "pid" || "${1:-}" == "stop" ]]; then
  action="$1"
  target="${2:-}"
  if [[ -z "$target" ]]; then
    usage >&2
    exit 2
  fi
  name="$(safe_name "$target")"
  pid_file="$LOG_DIR/${name}.watcher.pid"
  python_pid_file="$LOG_DIR/${name}.python.pid"
  lock_file="$LOG_DIR/${name}.lock"
  if [[ ! -f "$pid_file" ]]; then
    echo "Watcher not running for $target."
    exit 1
  fi
  pid="$(cat -- "$pid_file")"
  if ! pid_is_watcher "$pid"; then
    rm -f -- "$pid_file"
    echo "Removed stale PID file: $pid_file"
    exit 1
  fi
  python_pid=""
  if [[ -f "$python_pid_file" ]]; then
    IFS= read -r python_pid <"$python_pid_file" || true
    if [[ ! "$python_pid" =~ ^[0-9]+$ ]] || ! kill -0 "$python_pid" 2>/dev/null; then
      remove_pid_if_owner "$python_pid_file" "$python_pid"
      python_pid=""
    fi
  fi
  if [[ "$action" == "status" || "$action" == "pid" ]]; then
    echo "Script: $target"
    echo "Watcher PID: $pid"
    if [[ -n "$python_pid" ]]; then
      echo "Python process PID: $python_pid"
    else
      echo "Python process: not running (watcher waiting for next interval)"
    fi
    [[ "$action" == "status" ]] || exit 0
    echo "Python log: $LOG_DIR/${name}.log"
    echo "Watcher log: $LOG_DIR/${name}.watcher.log"
    echo "Lock file: $lock_file"
    exit 0
  fi
  kill -TERM "$pid"
  echo "Stopped watcher for $target: PID $pid"
  exit 0
fi

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

script="$1"
shift
interval="30m"
script_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval)
      [[ $# -ge 2 ]] || { echo "--interval requires a value." >&2; exit 2; }
      interval="$2"
      shift 2
      ;;
    --)
      shift
      script_args=("$@")
      break
      ;;
    *)
      script_args+=("$1")
      shift
      ;;
  esac
done

if [[ "$interval" =~ ^([1-9][0-9]*)(s|m|h)?$ ]]; then
  amount="${BASH_REMATCH[1]}"
  unit="${BASH_REMATCH[2]:-s}"
  case "$unit" in
    s) interval_seconds="$amount" ;;
    m) interval_seconds=$((amount * 60)) ;;
    h) interval_seconds=$((amount * 3600)) ;;
  esac
else
  echo "Invalid interval: $interval (examples: 60s, 5m, 1h)." >&2
  exit 2
fi

if [[ "$script" != /* ]]; then
  script="$SCRIPT_DIR/$script"
fi
if [[ ! -f "$script" || "$script" != *.py ]]; then
  echo "Python file not found: $script" >&2
  exit 2
fi
script="$(cd -- "$(dirname -- "$script")" && pwd)/$(basename -- "$script")"
python="$SCRIPT_DIR/.venv/bin/python"
[[ -x "$python" ]] || python="$(command -v python3 || true)"
if [[ -z "$python" ]]; then
  echo "Python interpreter not found; install Python 3 or create .venv." >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "flock is required for per-script run locks." >&2
  exit 1
fi

mkdir -p -- "$LOG_DIR"
name="$(safe_name "$script")"
pid_file="$LOG_DIR/${name}.watcher.pid"
python_pid_file="$LOG_DIR/${name}.python.pid"
lock_file="$LOG_DIR/${name}.lock"
run_log="$LOG_DIR/${name}.log"
watcher_log="$LOG_DIR/${name}.watcher.log"
if [[ -f "$pid_file" ]]; then
  old_pid="$(cat -- "$pid_file")"
  if pid_is_watcher "$old_pid"; then
    echo "Watcher already running for $script: PID $old_pid" >&2
    exit 1
  fi
  rm -f -- "$pid_file"
fi

nohup bash "$SELF" --worker \
  "$script" "$interval_seconds" "$python" \
  "$pid_file" "$python_pid_file" "$run_log" "$watcher_log" "$lock_file" \
  "${script_args[@]}" \
  </dev/null >/dev/null 2>&1 &
watcher_pid=$!
printf '%s\n' "$watcher_pid" >"$pid_file"
sleep 0.2
if ! pid_is_watcher "$watcher_pid"; then
  remove_pid_if_owner "$pid_file" "$watcher_pid"
  echo "Watcher failed to start; check $watcher_log." >&2
  exit 1
fi
echo "Started watcher for $script: PID $watcher_pid"
echo "Interval: ${interval_seconds}s after each run completes"
echo "PID file: $pid_file"
echo "Lock file: $lock_file"
echo "Python log: $run_log"
echo "Watcher log: $watcher_log"
