# Execute Source Adapter

`tools/scripts/execute_source_adapter.py` is the workspace-local executor for
validated source-adapter handoffs.

## Purpose

- Consume one validated handoff artifact.
- Execute local acquisition paths without network access.
- Put remote-manifest execution behind the network safety gate.
- Emit execution, capture, extraction, and denial artifacts under a run
  directory instead of writing into canonical storage.

## Example

Dry-run:

`python3 tools/scripts/execute_source_adapter.py --handoff path/to/handoff.json --output runs/acquisition/example --dry-run`

## Safety

- No network by default.
- Remote-capable handoffs require a network safety gate request.
- Workspace-local artifacts are staging outputs only.
- Canonical persistence is out of scope for this surface.
