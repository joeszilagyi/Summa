#!/usr/bin/env bash
# Workspace lock helper. Delegates locking semantics to the Python helper so
# shell entrypoints and Python tools share one metadata/quarantine contract.

workspace_lock_run() {
  local repo_root="$1"
  local workspace_id="$2"
  shift 2
  python3 "$repo_root/tools/common/workspace_lock.py" \
    --workspace-id "$workspace_id" \
    --command-name "${1:-workspace-lock}" \
    -- "$@"
}
