#!/usr/bin/env bash
set -euo pipefail

# Documentation: docs/scripts/index_build_knowledge_tree.md
# When changing this wrapper, keep the paired builder documentation and tests in sync.

readonly SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly BUILDER="$SELF_DIR/build_publication_artifacts.py"
readonly PYTHON_BIN="${PYTHON:-python3}"

usage() {
  cat <<'EOF_USAGE'
Usage:
  Index_Build_Knowledge_Tree.sh [options] [-- <builder args>]

This wrapper runs the end-to-end knowledge-tree publication builder from the
same directory.

Documentation:
  docs/scripts/index_build_knowledge_tree.md

Options:
  -h, --help      Show this help text.
  --check          Validate that dependencies required by the wrapper are usable.
  --dry-run        Print the command that would run without executing it.

You can override the Python interpreter with the PYTHON environment variable
for local environments.
EOF_USAGE
}

ensure_dependencies() {
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "python executable not found: $PYTHON_BIN" >&2
    exit 1
  fi
  if [[ ! -f "$BUILDER" ]]; then
    echo "missing builder: $BUILDER" >&2
    exit 1
  fi
  if [[ ! -r "$BUILDER" ]]; then
    echo "builder is not readable: $BUILDER" >&2
    exit 1
  fi
}

run_builder() {
  local -a args=("$@")
  "$PYTHON_BIN" "$BUILDER" "${args[@]}"
}

DRY_RUN=0
builder_args=()

while [[ $# -gt 0 ]]; do
  case "${1-}" in
    -h|--help)
      usage
      exit 0
      ;;
    --check)
      ensure_dependencies
      echo "Index_Build_Knowledge_Tree.sh: ready (builder: $BUILDER, python: $PYTHON_BIN)" >&2
      exit 0
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --)
      shift
      builder_args=("$@")
      break
      ;;
    *)
      builder_args+=("$1")
      shift
      ;;
  esac
done

if (( DRY_RUN )); then
  ensure_dependencies
  printf 'DRY-RUN:'
  printf ' %q' "$PYTHON_BIN" "$BUILDER" "${builder_args[@]}"
  printf '\n'
  exit 0
fi

ensure_dependencies
run_builder "${builder_args[@]}"
