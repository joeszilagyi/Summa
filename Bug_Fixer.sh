#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "$0")"

show_help() {
  cat <<EOF
Usage: $SCRIPT_NAME [--help|--check]

One-shot GitHub bug worker for $ROOT_DIR.

Each invocation does exactly one next bug action, then exits. It is designed to be
called from a shell loop.

Selection model:
  1. Look only at open GitHub issues labeled "bug".
  2. Prefer the most blocking bug that has no BUG-FIXER: PROPOSAL comment yet.
  3. If all open bugs already have proposals, pick the most blocking bug that has
     no BUG-FIXER: FOLLOW-UP comment yet.
  4. If every open bug already has at least one follow-up comment, pick the most
     blocking open bug and continue follow-up/closeout work.
  5. Stop only when there are no open GitHub issues labeled "bug".

Issue comment markers:
  BUG-FIXER: PROPOSAL
  BUG-FIXER: FOLLOW-UP

What a run does:
  Proposal phase:
    - inspect the chosen bug and relevant code
    - leave one proposal comment on that issue
    - keep this extra initial comment-only run even if a likely fix seems obvious
    - do not fix a second issue in the same run

  Follow-up phase:
    - review any existing fix state
    - if there is a viable actionable fix, accept, improve, or implement it now
    - build, test, and confirm the change
    - leave one substantive follow-up comment for this run when you make progress,
      validate a fix, or need to record the blocker that keeps the issue open
    - close the issue after a confirmed fix; otherwise leave it open

Default execution behavior:
  - runs codex non-interactively with --ephemeral
  - default Codex model: gpt-5.4
  - default sandbox mode passed to codex: --dangerously-bypass-approvals-and-sandbox
  - local commits allowed by default
  - pushes disabled by default
  - --check validates local prerequisites and resolved configuration without
    invoking Codex or mutating GitHub/repo state

Dry-run note:
  A faithful dry-run is not provided because issue selection and mutation are
  delegated to Codex after live GitHub inspection. Use --check before loop runs;
  operational safeguards are one issue action per invocation, proposal-first
  review, no PR creation, no push unless BUG_FIXER_PUSH=1, and close only after
  confirmed validation.

Environment overrides:
  BUG_FIXER_REPO    Override GitHub repo slug. Default: gh repo view result.
  CODEX_MODEL       Override model. Default: gpt-5.4
  CODEX_MODE        Override whitespace-separated Codex execution mode flags.
                    Default: --dangerously-bypass-approvals-and-sandbox
  BUG_FIXER_COMMIT  Allow local commit after validated fix. Default: 1
  BUG_FIXER_PUSH    Allow push after validated fix. Default: 0

Exit codes:
  0  Work done this run
  1  No work remaining
  2  Blocked
  3  Wrapper/setup error

Loop example:
  while ./$SCRIPT_NAME; do
    sleep 1
  done

Auto-push example:
  while BUG_FIXER_PUSH=1 ./$SCRIPT_NAME; do
    sleep 1
  done

Check example:
  ./$SCRIPT_NAME --check

Safer Codex mode example:
  CODEX_MODE=--full-auto ./$SCRIPT_NAME
EOF
}

require_boolean() {
  local name="$1"
  local value="$2"

  case "$value" in
    0|1)
      ;;
    *)
      echo "Bug_Fixer.sh: $name must be 0 or 1, got '$value'" >&2
      exit 3
      ;;
  esac
}

require_prereqs() {
  if ! command -v codex >/dev/null 2>&1; then
    echo "Bug_Fixer.sh: codex is not installed or not on PATH" >&2
    exit 3
  fi

  if ! command -v gh >/dev/null 2>&1; then
    echo "Bug_Fixer.sh: gh is not installed or not on PATH" >&2
    exit 3
  fi

  if ! git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Bug_Fixer.sh: $ROOT_DIR is not a git work tree" >&2
    exit 3
  fi

  if ! gh auth status >/dev/null 2>&1; then
    echo "Bug_Fixer.sh: gh is not authenticated" >&2
    exit 3
  fi
}

resolve_repo_name() {
  if [[ -n "${BUG_FIXER_REPO:-}" ]]; then
    printf '%s\n' "$BUG_FIXER_REPO"
    return 0
  fi

  if ! gh repo view --json nameWithOwner -q .nameWithOwner; then
    echo "Bug_Fixer.sh: failed to determine GitHub repo; set BUG_FIXER_REPO to override" >&2
    exit 3
  fi
}

CHECK_ONLY=0
if (($# > 1)); then
  echo "Bug_Fixer.sh: expected at most one argument" >&2
  echo "Try '$SCRIPT_NAME --help'." >&2
  exit 3
fi

case "${1:-}" in
  "")
    ;;
  --help|-h)
    show_help
    exit 0
    ;;
  --check)
    CHECK_ONLY=1
    ;;
  *)
    echo "Bug_Fixer.sh: unknown argument: $1" >&2
    echo "Try '$SCRIPT_NAME --help'." >&2
    exit 3
    ;;
esac

CODEX_MODEL="${CODEX_MODEL:-gpt-5.4}"
CODEX_MODE="${CODEX_MODE:---dangerously-bypass-approvals-and-sandbox}"
COMMIT_MODE="${BUG_FIXER_COMMIT:-1}"
PUSH_MODE="${BUG_FIXER_PUSH:-0}"
require_boolean BUG_FIXER_COMMIT "$COMMIT_MODE"
require_boolean BUG_FIXER_PUSH "$PUSH_MODE"
read -r -a CODEX_MODE_ARGS <<< "$CODEX_MODE"

require_prereqs
REPO_NAME="$(resolve_repo_name)"

if ((CHECK_ONLY)); then
  cat <<EOF
Bug_Fixer.sh: check passed
  root: $ROOT_DIR
  repo: $REPO_NAME
  codex model: $CODEX_MODEL
  codex mode: $CODEX_MODE
  local commit allowed: $COMMIT_MODE
  push allowed: $PUSH_MODE
EOF
  exit 0
fi

OUTPUT_FILE="$(mktemp)"
trap 'rm -f "$OUTPUT_FILE"' EXIT

if ! codex exec \
  "${CODEX_MODE_ARGS[@]}" \
  --ephemeral \
  -C "$ROOT_DIR" \
  -m "$CODEX_MODEL" \
  -o "$OUTPUT_FILE" \
  - <<PROMPT
You are operating in the git repository at $ROOT_DIR for GitHub repo $REPO_NAME.

This wrapper is for a loop. Each invocation must handle exactly one next bug action and then stop.

Work only on open GitHub issues labeled bug in $REPO_NAME.

Use these exact issue comment markers so future runs can detect progress:
- BUG-FIXER: PROPOSAL
- BUG-FIXER: FOLLOW-UP

Bug state rules:
- An open bug is unreviewed if it does not yet have a comment containing BUG-FIXER: PROPOSAL.
- An open bug is pending first follow-up if it has a BUG-FIXER: PROPOSAL comment but no BUG-FIXER: FOLLOW-UP comment.
- An open bug is pending revisit if it already has at least one BUG-FIXER: FOLLOW-UP comment and the issue is still open.
- A bug is done-for-now only when the GitHub issue itself is closed.
- A previous BUG-FIXER: FOLLOW-UP comment does not make an open bug terminal.

Selection rules:
- First priority: the most blocking open bug that is still unreviewed.
- If every open bug already has a proposal, then pick the most blocking open bug that is pending first follow-up.
- If every open bug already has at least one follow-up comment, then pick the most blocking open bug that is pending revisit.
- Do no repo or GitHub mutation and stop only if there are no open GitHub issues labeled bug.

When judging "most blocking", prioritize in this order:
1. state corruption, cross-place writes, or destructive behavior
2. malformed or invalid data being promoted into tracked state
3. crashes or total workflow aborts
4. silent wrong behavior or bad dedupe logic
5. stale docs and weaker serviceability defects

Phase 1 behavior for an unreviewed bug:
- Inspect the issue, the relevant code, and any tests.
- Leave exactly one substantive issue comment beginning with BUG-FIXER: PROPOSAL.
- Keep this proposal-first pass even if a likely fix already seems obvious; later inspection may reveal a better outcome or additional hijinks.
- The proposal comment must include:
  - why this issue is blocking relative to the other open bugs
  - the likely root cause
  - the fix shape you recommend
  - the tests or validation you would use
- Do not close the issue.
- Do not work a second issue in the same run.

Phase 2 behavior for an open bug that already has a proposal:
- Review the current repo state and any existing local fix work relevant to that issue.
- Treat this phase as execution-first. If there is a viable actionable fix, do the fix work in this run instead of leaving another analysis-only pass.
- If the bug is already fixed well, validate it, confirm the result, leave a follow-up comment, and close the issue.
- If it is not fixed yet and a viable actionable fix exists, implement the proper fix in the repo, add or update tests when appropriate, build/test/confirm the change, and then close the issue after the follow-up comment.
- If you cannot produce and confirm a viable actionable fix in this run, leave the issue open and explain the blocker or residual risk in the follow-up comment.
- If you make a code change and validation passes, you may create a local commit. BUG_FIXER_COMMIT is set to $COMMIT_MODE.
- If BUG_FIXER_COMMIT is 1, a local commit is allowed and preferred after a real fix.
- If BUG_FIXER_PUSH is 1, you may push the current branch after a successful validating commit. BUG_FIXER_PUSH is set to $PUSH_MODE.
- If BUG_FIXER_PUSH is 0, do not push.
- Leave at most one new substantive issue comment beginning with BUG-FIXER: FOLLOW-UP in this run.
- If the issue already has earlier follow-up comments, add another follow-up only when this run produced new implementation, validation, or blocker information.
- The follow-up comment must include:
  - whether you accepted an existing fix, improved it, or implemented a new one
  - the files changed, if any
  - the validation run
  - any residual risk or open question
- Close the issue only after you have confirmed a real fix with successful validation.
- Do not open a PR.
- Do not work a second issue in the same run.

General constraints:
- Leave proposal-phase issues open.
- Close a follow-up issue only after a confirmed fix with successful validation; otherwise leave it open.
- Never lock an issue.
- Avoid unrelated repo edits.
- Respect the existing working tree if it is dirty; do not revert unrelated changes.
- Prefer gh CLI for issue discovery and issue comments if needed.

Your final response must end with exactly one of these lines:
- BUG_FIXER_STATUS: WORK_DONE
- BUG_FIXER_STATUS: NO_WORK_REMAINING
- BUG_FIXER_STATUS: BLOCKED

Use WORK_DONE if you posted either required issue comment, or if you also implemented a fix.
Use NO_WORK_REMAINING only if there are no open GitHub issues labeled bug.
Use BLOCKED only if you made a real attempt but could not proceed because of auth, tooling, or repo-state blockers.
PROMPT
then
  if [[ -s "$OUTPUT_FILE" ]]; then
    cat "$OUTPUT_FILE" >&2
  else
    echo "Bug_Fixer.sh: codex execution failed" >&2
  fi
  exit 3
fi

cat "$OUTPUT_FILE"

# The wrapper contract is the final non-empty line, not an incidental marker
# mention earlier in the agent response.
STATUS_LINE="$(awk 'NF { line = $0 } END { print line }' "$OUTPUT_FILE")"

case "$STATUS_LINE" in
  "BUG_FIXER_STATUS: WORK_DONE")
    exit 0
    ;;
  "BUG_FIXER_STATUS: NO_WORK_REMAINING")
    exit 1
    ;;
  "BUG_FIXER_STATUS: BLOCKED")
    exit 2
    ;;
  *)
    echo "Bug_Fixer.sh: missing BUG_FIXER_STATUS marker in Codex output" >&2
    exit 3
    ;;
esac
