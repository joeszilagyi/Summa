#!/usr/bin/env bash
# Documentation: docs/scripts/index_export_redacted_diagnostics.md
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
TARGET="$REPO_ROOT/tools/scripts/export_redacted_diagnostics.py"

exec "$PYTHON" "$TARGET" "$@"
