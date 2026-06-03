# Legacy Backfill

`tools/source_db_tools/legacy_backfill.py` performs a conservative, idempotent
backfill from retained legacy lead/entity rows into the durable source/work
schema.

Operational posture:

- use `--dry-run` first to preview inserts and warnings
- no legacy rows are deleted
- writes are shaped to be safe to re-run
- helper inference is intentionally cautious and leaves ambiguous rows in
  review-oriented states

Current behavior includes:

- provisional `work_type` inference from legacy text and identifiers
- normalization of identifiers through `identifier_normalization.py`
- insertion of work rows, metadata, identifiers, source-access rows, and
  related staging data without destructive cleanup
- machine-readable `legacy-backfill-report.v1` output

When changing behavior or CLI options, keep this document, the module header,
and any backfill regression tests aligned. Avoid widening automatic promotion
semantics without corresponding review-state and confidence-policy updates.
