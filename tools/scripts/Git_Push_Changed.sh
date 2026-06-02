#!/usr/bin/env bash
set -euo pipefail

# Purpose: Stage repo changes, create a commit, and push it (or run in dry-run mode).
# Why: Keep one-command local change synchronization safe and predictable.
# Inputs/assumptions:
# - Script lives at tools/scripts within the target repository.
# - Caller wants to push the current branch with default remote "origin".
# - Requires write access to that branch and an existing git remote.
DRY_RUN=0
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
CURRENT_BRANCH=""
DEFAULT_COMMIT_PREFIX="sync"
COMMIT_PREFIX="${COMMIT_PREFIX:-$DEFAULT_COMMIT_PREFIX}"
DEFAULT_REMOTE="origin"
REMOTE="${GIT_PUSH_REMOTE:-$DEFAULT_REMOTE}"
REMOTE_CHECK_ATTEMPTS=3
REMOTE_CHECK_SLEEP_SECONDS=2

usage() {
  cat <<'EOF'
Usage: Git_Push_Changed.sh [--dry-run] [--help]

Push all local changes in this repository from the current branch.

Options:
  --dry-run  Show what would be committed and pushed without changing state.
  --help     Show this message.

Environment:
  GIT_PUSH_REMOTE     Remote name to use for push (default: origin).
  COMMIT_PREFIX       Commit message prefix used with UTC timestamp.
EOF
}

log() {
  printf '%s\n' "$*" >&2
}

run_with_dryrun() {
  local -a cmd=("$@")
  if (( DRY_RUN )); then
    log "[dry-run] ${cmd[*]}"
    return 0
  fi
  "${cmd[@]}"
}

show_no_change() {
  log "No changes found to commit."
}

push_with_retry() {
  local -a push_cmd=("$@")
  local attempt=1
  local sleep_seconds
  while true; do
    if "${push_cmd[@]}"; then
      return 0
    fi

    if (( attempt >= REMOTE_CHECK_ATTEMPTS )); then
      log "Push failed after ${attempt} attempts."
      return 1
    fi

    sleep_seconds=$(( REMOTE_CHECK_SLEEP_SECONDS * (2 ** (attempt - 1)) ))
    log "Push failed; retry ${attempt}/${REMOTE_CHECK_ATTEMPTS} in ${sleep_seconds}s."
    (( attempt++ ))
    sleep "$sleep_seconds"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      log "Unsupported option: $1"
      usage
      exit 64
      ;;
  esac
  shift
done

if ! GIT_ROOT="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null)"; then
  log "Not a git repository: $REPO_ROOT"
  exit 1
fi

if [[ "$GIT_ROOT" != "$REPO_ROOT" ]]; then
  log "Resolved repo root does not match git toplevel: expected $REPO_ROOT got $GIT_ROOT"
  exit 1
fi

if ! CURRENT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)"; then
  log "Unable to determine current branch."
  exit 1
fi
if [[ "$CURRENT_BRANCH" == "HEAD" ]]; then
  log "Refusing to push while in detached HEAD state."
  exit 1
fi

if ! git -C "$REPO_ROOT" remote get-url "$REMOTE" >/dev/null 2>&1; then
  log "Remote '$REMOTE' is not configured."
  exit 1
fi

status_output="$(git -C "$REPO_ROOT" status --short --untracked-files=normal)"
if [[ -z "$status_output" ]]; then
  show_no_change
  exit 0
fi

if (( DRY_RUN )); then
  log "[dry-run] repo: $REPO_ROOT"
  log "[dry-run] branch: $CURRENT_BRANCH"
  log "[dry-run] remote: $REMOTE"
  log "[dry-run] changes:"
  printf '%s\n' "$status_output" >&2
  log "[dry-run] would stage all tracked/untracked changes, commit with message: ${COMMIT_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ), and push."
  exit 0
fi

run_with_dryrun git -C "$REPO_ROOT" add -A .

if git -C "$REPO_ROOT" diff --cached --quiet --exit-code; then
  show_no_change
  exit 0
fi

commit_message="${COMMIT_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
run_with_dryrun git -C "$REPO_ROOT" commit -m "$commit_message"

if git -C "$REPO_ROOT" rev-parse --abbrev-ref "@{upstream}" >/dev/null 2>&1; then
  push_with_retry git -C "$REPO_ROOT" push "$REMOTE" "$CURRENT_BRANCH"
else
  push_with_retry git -C "$REPO_ROOT" push -u "$REMOTE" "$CURRENT_BRANCH"
fi
