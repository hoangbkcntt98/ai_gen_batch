#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/batch_logs"
INTERVAL_SECONDS=43200

while true; do
  sleep "$INTERVAL_SECONDS"
  if [[ -d "$LOG_DIR" ]]; then
    find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -delete
  fi
done
