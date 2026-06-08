# Execute Source Adapter

`tools/scripts/execute_source_adapter.py` is the workspace-local executor for
validated source-adapter handoffs.

## Purpose

- Consume one validated handoff artifact.
- Execute local acquisition paths without network access.
- Execute remote URL-manifest acquisition only after network safety gate allow
  and explicit operator opt-in.
- Emit execution, capture, extraction, and denial artifacts under a run
  directory instead of writing into canonical storage.

## Example

Dry-run:

`python3 tools/scripts/execute_source_adapter.py --handoff path/to/handoff.json --workspace-root . --output runs/acquisition/example --dry-run`

Gated remote URL fetch:

`python3 tools/scripts/execute_source_adapter.py --handoff path/to/remote-handoff.jsonl --network-safety-request path/to/gate-request.json --allow-network --workspace-root . --output runs/acquisition/remote-example`

## Safety

- No network by default.
- Remote-capable handoffs require a network safety gate request.
- Remote fetch additionally requires a gate `allow` decision and
  `--allow-network`.
- Output directories are confined to the declared workspace root.
- Gate denials and missing opt-in perform no fetch and keep
  `network_access_attempted` false.
- Fetched bytes are transient run-directory artifacts and are treated as
  untrusted source data.
- The executor does not crawl, search, run JavaScript, use credentials, or
  follow redirects outside the allowlist.
- Workspace-local artifacts are staging outputs only.
- Canonical persistence is out of scope for this surface.
