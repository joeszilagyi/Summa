#!/usr/bin/env bash
set -euo pipefail

# Documentation: docs/scripts/index_run_gather.md

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
TARGET_SCRIPT="$SELF_DIR/run_topic_gather.py"
LLM_RUNNER_LIB="$SELF_DIR/lib/llm_runner.sh"
LLM_RUNNER_BRIDGE="$SELF_DIR/lib/llm_runner_bridge.sh"

ensure_dependencies() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "python executable not found: $PYTHON_BIN"
  [[ -r "$TARGET_SCRIPT" ]] || fail "Missing gather driver: $TARGET_SCRIPT"
  [[ -r "$LLM_RUNNER_LIB" ]] || fail "Missing llm_runner library: $LLM_RUNNER_LIB"
  [[ -r "$LLM_RUNNER_BRIDGE" ]] || fail "Missing llm_runner bridge: $LLM_RUNNER_BRIDGE"
}

if [[ "${1:-}" == "--check" ]]; then
  ensure_dependencies
  printf 'Index_Run_Gather.sh: ready (driver: %s, python: %s)\n' "$TARGET_SCRIPT" "$PYTHON_BIN"
  exit 0
fi

ensure_dependencies
exec "$PYTHON_BIN" "$TARGET_SCRIPT" "$@"
