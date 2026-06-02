# Network Safety Gate

`network-safety-gate-request.v1` is the shared preflight contract for any
future network executor in Summa.

Current repo state matters here: the existing remote-related tools are still
dry-run planners and explicitly record `network_access_attempted: false`. This
gate exists so any later live fetch or acquisition surface has one required
decision point before network access begins.

## Required checks

- explicit host or URL-prefix allowlist
- dry-run reporting of planned actions only
- rate-limit posture
- side-effect budget
- user-agent and robots posture
- dirty-worktree refusal when the executor declares that a clean worktree is
  required

## Non-goals

- no external network access in CI
- no hidden uploads, pushes, or account mutation
- no bypass path around the shared gate helper
