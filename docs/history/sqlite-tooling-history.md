# SQLite Tooling History

This document is the current summary for the live `tools/source_db_tools/`
line and the historical references that still appear in contributor-facing
docs.

## Current Line In Tree

The current SQLite helper line is `tools/source_db_tools/`.

Current checked-in surfaces include:

- canonical store bootstrap and forward-only migration helpers
- review queue and provenance helpers
- schema-profile and registry loaders
- identifier normalization and rights-retention helpers
- legacy backfill into durable tables
- SQLite safety helpers for integrity, backup, and restore verification
- source-locus seeding and source-query simulation helpers

The current tree also includes operator-facing scripts outside that directory
that depend on the SQLite line, such as:

- `tools/scripts/build_review_queue_view.py`
- `tools/scripts/build_candidate_feedback_plan.py`
- `tools/scripts/topic_backup_drill.py`
- `tools/scripts/build_knowledge_tree_export.py`

## Tables Created Today

Current checked-in table-creation surfaces are:

1. `tools/source_db_tools/schema/migrations/0001_canonical_store.sql`
2. `tools/source_db_tools/source_locus_seed.py`
3. `tools/source_db_tools/source_query_execution_simulation.py`

The canonical bootstrap currently creates these durable tables:

- `schema_version`
- `schema_migration_history`
- `provenance_event`
- `authority_record`
- `work`
- `capture_event`
- `extraction_record`
- `extraction_detected_entity`
- `work_subject`
- `source_relationship`
- `source_claim`
- `review_state_history`
- `authority_reconciliation`
- `topic_extension`
- `authority_identifier`
- `work_identifier`
- `source_access`
- `work_metadata`
- `work_url`
- `authority_merge_event`

The current source-locus helper creates or maintains:

- `source_locus`
- optional compatibility columns such as `lead.source_locus_id`,
  `source_access.source_locus_id`, and `source_access.source_lead_id` when the
  target tables already exist

The current simulation helper creates or maintains:

- `source_query_execution_simulation`
- `simulated_source_lead_candidate`

## Relation To The Canonical Graph Model

The current canonical mapping is summarized in
[../project/CANONICAL_GRAPH_MODEL.md](../project/CANONICAL_GRAPH_MODEL.md) and
its executable outline `config/canonical_graph_model_outline.json`.

Important distinction:

- the canonical graph model is the durable ownership model
- the wider SQLite helper line still contains older `source.sqlite`-oriented
  helpers, staging tables, and simulation utilities
- not every helper in `tools/source_db_tools/` operates only on the canonical
  bootstrap tables

Examples:

- `review_queue.py` still supports a broader review-target vocabulary, including
  compatibility tables such as `lead`
- `source_locus_seed.py` and
  `source_query_execution_simulation.py` operate on staging/simulation tables
  that the canonical outline classifies as noncanonical

## Historical References Still Mentioned

`CONTRIBUTING.md` still mentions historical `tools/source_db_tools_v8_*`
directories as examples of frozen baselines if they are ever restored or
imported.

Current checkout status:

- no `tools/source_db_tools_v8_*` directories are present in this tree
- this document is therefore a summary of the current live line, not a restored
  archive of those absent baselines

Do not invent behavior for those missing baselines. If one is restored later,
document it as a separate frozen historical reference.

## Current Gaps And Partial Restorations

Some SQLite-adjacent references are still transitional:

- `tools/source_db_tools/source_query_execution_simulation.py` references a
  `source_query_plan` helper line that is not restored as a standalone Python
  module in this checkout
- several view emitters and helper tests still exercise older
  `source.sqlite`-shaped tables alongside the canonical bootstrap

That split is current repo reality. Do not collapse it in prose.

## How Later Work Should Read This History

Use this history summary to distinguish three categories:

- current implementation: helpers, migrations, validators, and tests that are
  present in this checkout
- documented contract: canonical graph model and related sidecars
- missing or future restoration work: absent historical baselines or later
  migrations not yet added

If later SQLite work changes the live line, update this file, the paired
tool-line docs under `docs/tools/source_db_tools/`, and the related tests
together.
