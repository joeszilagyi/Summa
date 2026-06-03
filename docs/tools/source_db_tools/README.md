# Source DB Tools

This directory holds operator and maintainer notes for the current
`tools/source_db_tools` helper surfaces. The reset repo keeps these tools
lightweight and local-first, so the docs here focus on the checked-in
registries, validation boundaries, and read/write expectations that matter when
changing them.

Current referenced guides:

- `canonical_store_bootstrap.md`: canonical SQLite bootstrap, check, and
  forward-only migration behavior
- `claim_types.md`: claim-type registry loader and claim evidence/review checks
- `relationship_predicates.md`: relationship predicate registry loader and
  evidence requirements
- `schema_profile_validation.md`: profile-boundary validation and the
  `validate_schema_profile.py` CLI
- `confidence_model.md`: confidence score/dimension helpers shared by profile
  validation
- `legacy_backfill.md`: conservative legacy lead/entity backfill into durable
  source/work tables

The helper modules in `tools/source_db_tools/*.py` treat these docs as the
canonical maintenance notes. When a module header points at one of these files,
keep the paired code, registry input, and tests in sync.
