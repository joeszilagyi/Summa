# Network Safety Gate

`network-safety-gate-request.v1` is the shared preflight contract for network
executors in Summa.

Remote URL-manifest acquisition is disabled by default. The source-adapter
executor may retrieve remote bytes only when a validated remote handoff, a gate
request, a gate `allow` decision, and explicit operator `--allow-network` opt-in
are all present. Gate denial, dry-run mode, missing gate requests, and missing
operator opt-in perform no network access and record `network_access_attempted:
false`.

## Required checks

- explicit host or URL-prefix allowlist
- dry-run reporting of planned actions only
- rate-limit posture
- side-effect budget
- user-agent and robots posture
- dirty-worktree refusal when the executor declares that a clean worktree is
  required

## Remote Acquisition Semantics

- Only explicit URL-manifest handoff entries may be fetched.
- Only `GET` and `HEAD` methods are accepted by the gate.
- Redirects are checked at each hop and are not followed outside the echoed
  allowlist.
- The executor uses the gate request's user agent and records the gate decision
  hash in execution output.
- Fetched payload bytes are transient run artifacts under the executor output
  directory, not public data and not canonical facts.
- `network_access_attempted` means an HTTP request was attempted. It is true
  for connect, timeout, HTTP error, and successful response attempts.
- CI coverage uses localhost fixture servers only; it does not access the live
  internet.

## Non-goals

- no external network access in CI
- no hidden uploads, pushes, or account mutation
- no bypass path around the shared gate helper
- no crawling, recursive link following, browser automation, cookies,
  authentication, or general web search
