#!/usr/bin/env bash
set -euo pipefail

# Wrapper around tools/common/crown_jewel_backup.py for backward compatibility with
# tooling that calls this legacy script name.
usage() {
  cat <<'USAGE'
Usage: Index_Plan_Crown_Jewel_Backup.sh [ARGS...]

This script is a thin compatibility wrapper around:
  tools/common/crown_jewel_backup.py

It forwards all arguments to the target Python script.
For options and behavior details, run:
  ./tools/common/crown_jewel_backup.py --help

Use --help or -h to show this message.
USAGE
}

fail() {
  echo "Error: $*" >&2
  exit 1
}

THIS_SCRIPT="${BASH_SOURCE[0]}"
THIS_SCRIPT_DIR="${THIS_SCRIPT%/*}"
if [[ "$THIS_SCRIPT_DIR" == "$THIS_SCRIPT" ]]; then
  THIS_SCRIPT_DIR="."
fi
SELF_DIR="$(cd -- "$THIS_SCRIPT_DIR" && pwd)"
REPO_ROOT="$(cd -- "$SELF_DIR/../.." && pwd)"
TARGET_SCRIPT="$REPO_ROOT/tools/common/crown_jewel_backup.py"

[[ -f "$TARGET_SCRIPT" ]] || fail "Expected target script not found: $TARGET_SCRIPT"
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed or not in PATH"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

exec python3 "$TARGET_SCRIPT" "$@"
