#!/usr/bin/env bash
set -euo pipefail

# Thin wrapper that runs the topic-workspace bootstrap logic from the same directory
# as this script, while preserving pass-through of command-line arguments.
fail() {
  echo "Error: $*" >&2
  exit 1
}

THIS_SCRIPT="${BASH_SOURCE[0]:-$0}"
if [[ "$THIS_SCRIPT" != */* ]]; then
  THIS_SCRIPT="$(command -v "$THIS_SCRIPT" || true)"
fi
[[ -f "$THIS_SCRIPT" ]] || fail "Unable to locate script file: $THIS_SCRIPT"

SELF_DIR="$(cd -- "$(dirname -- "$THIS_SCRIPT")" && pwd -P)"
BOOTSTRAP_SCRIPT="$SELF_DIR/bootstrap_topic_workspace.py"
[[ -f "$BOOTSTRAP_SCRIPT" ]] || fail "Missing bootstrap target: $BOOTSTRAP_SCRIPT"
CONSOLE_COMMAND="summa-new-topic"

if command -v "$CONSOLE_COMMAND" >/dev/null 2>&1; then
  exec "$CONSOLE_COMMAND" "$@"
fi

PYTHON_BIN="python3"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN is not installed or not in PATH"

exec "$PYTHON_BIN" "$BOOTSTRAP_SCRIPT" "$@"
