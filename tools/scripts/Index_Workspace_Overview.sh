#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONSOLE_COMMAND="summa-workspace-overview"

if command -v "$CONSOLE_COMMAND" >/dev/null 2>&1; then
  exec "$CONSOLE_COMMAND" "$@"
fi

exec python3 "$SCRIPT_DIR/build_workspace_overview_view.py" "$@"
