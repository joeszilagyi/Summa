# Canonical Store Bootstrap

`tools/source_db_tools/init_canonical_store.py` is the operator-facing bootstrap
and check surface for the canonical SQLite store.

## Purpose

- Create the checked-in canonical schema on an empty SQLite database.
- Record schema version and applied migration metadata inside the database.
- Re-run safely as an idempotent forward-only migration step.
- Validate an existing store without mutating it.

## Commands

Initialize or migrate to the current checked-in schema version:

`python3 tools/source_db_tools/init_canonical_store.py --db path/to/canonical.sqlite`

Validate an existing store without changing it:

`python3 tools/source_db_tools/init_canonical_store.py --db path/to/canonical.sqlite --check`

## Backing Tables

The canonical family-to-table mapping stays in
`config/canonical_graph_model_outline.json`. The bootstrap also creates the
supporting durable tables currently required by local source DB helpers:

- `authority_identifier`
- `authority_merge_event`
- `source_access`
- `work_identifier`
- `work_metadata`
- `work_url`

Schema metadata is stored in:

- `schema_version`
- `schema_migration_history`

## Migration Policy

- Forward-only
- Non-destructive
- No silent downgrade
- No `DROP TABLE`
- Append-only review and provenance history remains intact

## Staging Boundary

The bootstrap does not convert staging or simulation tables into canonical
records. `source_locus`, `source_query_execution_simulation`, and
`simulated_source_lead_candidate` remain explicitly noncanonical staging tables.

## Scope Limits

- F1 gather execution is not implemented here.
- F2 acquisition execution is not implemented here.
- F4 publication output is not implemented here.
- This bootstrap only creates durable local storage; it does not import runtime
  artifacts into canonical rows by itself.

## Operator Visibility

- `tools/scripts/local_doctor.py` reports the canonical store as `absent`,
  `uninitialized`, `invalid`, `initialized_empty`, or `populated`.
- `initialized_empty` is distinct from `absent`; an empty valid store is not an
  automatic operator failure.
- Canonical family counts and last-ingest timestamps appear only when the store
  contains recognized ingest provenance events.
