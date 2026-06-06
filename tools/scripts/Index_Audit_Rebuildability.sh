#!/usr/bin/env bash
# Documentation: docs/scripts/index_audit_rebuildability.md
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
TARGET_SCRIPT="$SELF_DIR/audit_rebuildability.py"
CONSOLE_COMMAND="summa-audit-rebuildability"

if command -v "$CONSOLE_COMMAND" >/dev/null 2>&1; then
  exec "$CONSOLE_COMMAND" "$@"
fi

exec "$PYTHON" "$TARGET_SCRIPT" "$@"
