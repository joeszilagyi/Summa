# AGENTS

Read this file first in every Codex session for this repository.

## Clean Sheets Definition

When the user asks for "clean sheets", "fully clean sheets", or equivalent, treat it as all of the following:

1. The working tree is clean.
2. The current branch is up to date with its upstream and any intended commits are pushed.
3. There are no open pull requests for the repo unless the user explicitly asks to keep one open.
4. There are no stale remote topic branches left behind from completed work unless the user explicitly asks to keep them.
5. Open GitHub issues are either:
   - fixed in code and closed, or
   - verified against the current tree and closed as stale with evidence.

If the user asks for a "github catch-up" or "clean sheets" update, report the state of all five items explicitly instead of only reporting local git status.

## Backlog Workflow

When working backlog items:

1. Start by checking:
   - `git status -sb`
   - current branch and upstream
   - open pull requests
   - open issues relevant to the current backlog pass
2. For each issue, verify whether the current code already satisfies the reported behavior.
3. If the issue still reproduces, make the smallest code change that fixes it.
4. If the issue is stale against the current tree, close it only after recording the verification evidence.
5. Keep changes scoped to the issue at hand unless the user explicitly asks for a broader cleanup.

## Repository Hygiene

- Prefer leaving the repository with no open PRs and no stale remote branches after a cleanup pass.
- Do not revert unrelated user changes.
- Prefer explicit verification over assumptions.
- If a user asks for "clean sheets", interpret that as including remote branch cleanup and issue cleanup, not only a clean worktree.
