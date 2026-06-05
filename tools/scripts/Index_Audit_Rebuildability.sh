#!/usr/bin/env bash
# Documentation: docs/scripts/index_audit_rebuildability.md
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
TARGET_SCRIPT="$SELF_DIR/audit_rebuildability.py"

exec "$PYTHON" "$TARGET_SCRIPT" "$@"
