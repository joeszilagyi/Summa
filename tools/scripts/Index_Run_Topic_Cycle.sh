#!/usr/bin/env bash
set -euo pipefail

# Documentation: docs/scripts/index_run_topic_cycle.md

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

THIS_SCRIPT="${BASH_SOURCE[0]:-$0}"
if [[ "$THIS_SCRIPT" != */* ]]; then
  THIS_SCRIPT="$(command -v "$THIS_SCRIPT" || true)"
fi
[[ -f "$THIS_SCRIPT" ]] || fail "Unable to locate script file: $THIS_SCRIPT"

SELF_DIR="$(cd -- "$(dirname -- "$THIS_SCRIPT")" && pwd -P)"
PYTHON_BIN="${PYTHON:-python3}"
TARGET_SCRIPT="$SELF_DIR/run_topic_cycle.py"
CONSOLE_COMMAND="summa-run-topic-cycle"

if command -v "$CONSOLE_COMMAND" >/dev/null 2>&1; then
  exec "$CONSOLE_COMMAND" "$@"
fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python executable not found: $PYTHON_BIN"
[[ -r "$TARGET_SCRIPT" ]] || fail "Missing topic-cycle runner: $TARGET_SCRIPT"

exec "$PYTHON_BIN" "$TARGET_SCRIPT" "$@"
