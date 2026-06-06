#!/usr/bin/env bash
set -euo pipefail

# Wrapper to run the topic-workspace scheduler selector with a consistent script path.
# The selector does the real filtering logic; this shim only resolves that script's
# location and forwards args.
fail() {
  echo "Error: $*" >&2
  exit 1
}

THIS_SCRIPT="${BASH_SOURCE[0]}"
SCRIPT_DIR="${THIS_SCRIPT%/*}"
if [[ "$SCRIPT_DIR" == "$THIS_SCRIPT" ]]; then
  SCRIPT_DIR="."
fi
SELF_DIR="$(cd -- "$SCRIPT_DIR" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TARGET_SCRIPT="$SELF_DIR/select_scheduled_workspaces.py"
CONSOLE_COMMAND="summa-select-scheduled-workspaces"

if command -v "$CONSOLE_COMMAND" >/dev/null 2>&1; then
  exec "$CONSOLE_COMMAND" "$@"
fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "${PYTHON_BIN} is not installed or not in PATH"
[[ -r "$TARGET_SCRIPT" ]] || fail "Target script not readable: $TARGET_SCRIPT"

exec "$PYTHON_BIN" "$TARGET_SCRIPT" "$@"
