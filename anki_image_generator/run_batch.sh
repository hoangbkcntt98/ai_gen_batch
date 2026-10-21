#!/usr/bin/env bash
set -euo pipefail

# Defaults can be edited here or overridden through environment variables.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
CONFIG="${CONFIG:-$SCRIPT_DIR/config.json}"
LIMIT="${LIMIT:-}"
STEP="${STEP:-1}"
RECOVERY="${RECOVERY:-false}"
DRY_RUN="${DRY_RUN:-false}"
MEDIA_DIR="${MEDIA_DIR:-}"
RUN_IN_BACKGROUND="${RUN_IN_BACKGROUND:-true}"

prompt() {
  local label="$1" default="$2"
  if [[ -t 0 ]]; then
    read -r -p "$label [$default]: " answer
    REPLY="${answer:-$default}"
  else
    REPLY="$default"
  fi
}

prompt "Config file" "$CONFIG"; CONFIG="$REPLY"
prompt "Limit (empty uses config limit)" "$LIMIT"; LIMIT="$REPLY"
prompt "Concurrent jobs (--step)" "$STEP"; STEP="$REPLY"
prompt "Retry eligible errors? (true/false)" "$RECOVERY"; RECOVERY="$REPLY"
prompt "Dry run? (true/false)" "$DRY_RUN"; DRY_RUN="$REPLY"
prompt "Media directory (empty uses config)" "$MEDIA_DIR"; MEDIA_DIR="$REPLY"
prompt "Run in background? (true/false)" "$RUN_IN_BACKGROUND"; RUN_IN_BACKGROUND="$REPLY"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
  echo "Virtual environment not found: $VENV_DIR" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

cd "$SCRIPT_DIR"
args=(--config "$CONFIG" --step "$STEP")

if [[ -n "$LIMIT" ]]; then
  args+=(--limit "$LIMIT")
fi
if [[ "$RECOVERY" == "true" ]]; then
  args+=(--recovery)
fi
if [[ "$DRY_RUN" == "true" ]]; then
  args+=(--dry-run)
fi
if [[ -n "$MEDIA_DIR" ]]; then
  args+=(--media-dir "$MEDIA_DIR")
fi

if [[ "$RUN_IN_BACKGROUND" == "true" ]]; then
  LOG_BASE="${LOG_BASE:-$(python - "$CONFIG" "$SCRIPT_DIR" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
config = json.loads(config_path.read_text(encoding="utf-8-sig"))
configured = config.get("log_path", "")
if not isinstance(configured, str):
    raise SystemExit("Config log_path must be a string.")
if configured:
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = config_path.resolve().parent / path
else:
    path = Path(sys.argv[2])
print(path.resolve())
PY
)}"
  RUN_DIR="${RUN_DIR:-$LOG_BASE/$(date +%Y%m%d%H%M%S)}"
  LOG_FILE="${LOG_FILE:-$RUN_DIR/batch.log}"
  PID_FILE="${PID_FILE:-$RUN_DIR/batch.pid}"
  mkdir -p "$(dirname -- "$LOG_FILE")" "$(dirname -- "$PID_FILE")"
  nohup python -u "$SCRIPT_DIR/batch_image_generate.py" "${args[@]}" \
    >"$LOG_FILE" 2>&1 </dev/null &
  pid=$!
  echo "$pid" >"$PID_FILE"
  echo "Started background job: PID $pid"
  echo "Run directory: $RUN_DIR"
  echo "Log: $LOG_FILE"
  echo "PID file: $PID_FILE"
else
  exec python -u "$SCRIPT_DIR/batch_image_generate.py" "${args[@]}"
fi
