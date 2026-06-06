#!/usr/bin/env bash
set -euo pipefail

SELF="${BASH_SOURCE[0]:-$0}"
if [[ "$SELF" != */* ]]; then
  SELF="$(command -v "$SELF")"
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "$SELF")" && pwd -P)"
CONSOLE_COMMAND="summa-operator-path-smoke"

if command -v "$CONSOLE_COMMAND" >/dev/null 2>&1; then
  exec "$CONSOLE_COMMAND" "$@"
fi
exec python3 "$SCRIPT_DIR/operator_path_smoke.py" "$@"
