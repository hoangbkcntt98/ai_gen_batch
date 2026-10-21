#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
  echo "Virtual environment not found: $VENV_DIR" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
cd "$SCRIPT_DIR"
exec python -u "$SCRIPT_DIR/setting_image_to_notes.py" "$@"
