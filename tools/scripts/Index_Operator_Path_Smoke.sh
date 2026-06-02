#!/usr/bin/env bash
set -euo pipefail

SELF="${BASH_SOURCE[0]:-$0}"
if [[ "$SELF" != */* ]]; then
  SELF="$(command -v "$SELF")"
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "$SELF")" && pwd -P)"
exec python3 "$SCRIPT_DIR/operator_path_smoke.py" "$@"
