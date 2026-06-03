# Changelog

All notable repository-visible changes are recorded here. Build numbers follow
`.project_metadata`, and `CURRENT_BUILD` must always have a visible entry.
Entries describe tracked repository changes rather than private local runtime
data, and this changelog complements tests, docs, and contract files rather
than replacing them. Each current-build entry must be substantive: name
concrete tracked surfaces or tools rather than only the changelog itself.

## [8.8.0.5]

### Added

- Added canonical ingest entrypoints `tools/scripts/ingest_gather_candidate_batch.py`
  and `tools/scripts/ingest_execution_artifacts.py`, plus the shared
  `tools/source_db_tools/canonical_ingest.py` helper, so validated gather and
  executor artifacts can populate the canonical store through the checked-in
  write API.
- Added deterministic curation helpers in
  `tools/source_db_tools/canonical_reconciliation.py` for exact work dedup,
  reviewable authority reconciliation, and structured contradiction detection
  that preserves wrong or impossible source claims instead of deleting them.

### Changed

- Extended `tools/scripts/run_topic_gather.py` and
  `tools/source_db_tools/canonical_store.py` so gather can inject bounded prior
  canonical state across cycles and still preserve one-shot mode by default.
- Connected `tools/scripts/build_candidate_feedback_plan.py`,
  `config/candidate_feedback_plan.schema.json`, and
  `config/gather_candidate_batch.schema.json` so feedback plans can rank the
  next facet or lead, feed that choice back into gather, and stamp cycle-depth,
  prior-state, and next-action metadata onto the resulting candidate batch.

### Validation

- Added regression suites
  `tests/test_canonical_store_write_api.py`,
  `tests/test_canonical_ingest_candidate_batch.py`,
  `tests/test_canonical_ingest_execution_artifacts.py`,
  `tests/test_gather_iteration.py`,
  `tests/test_candidate_feedback_selection.py`, and
  `tests/test_canonical_dedup_and_contradiction.py` to keep the Wave 2 ingest,
  iteration, feedback, and curation surfaces deterministic and local-only.

## [8.8.0.4]

### Added

- Added `tools/scripts/run_topic_gather.py` with prompt-bundle rendering,
  untrusted source wrapper enforcement, and `gather-candidate-batch.v1`
  validation so the gather stage can render and validate local candidate-batch
  artifacts.
- Added `tools/scripts/execute_source_adapter.py` with validated local
  execution artifacts such as `execution-record.json`, `capture-events.jsonl`,
  and `extraction-records.jsonl`, while keeping remote acquisition behind a
  refusal path.
- Added `tools/source_db_tools/canonical_store.py`,
  `tools/source_db_tools/init_canonical_store.py`, and
  `tools/source_db_tools/schema/migrations/0001_canonical_store.sql` to
  bootstrap, migrate, and validate the checked-in canonical store substrate.
- Added publication builders `tools/scripts/build_knowledge_tree_export.py`,
  `tools/scripts/build_public_knowledge_tree_presentation.py`, and
  `tools/scripts/build_publication_artifacts.py` so checked-in canonical rows
  can be exported and rendered through the public-safe presentation path.

### Changed

- Made the package installable as `summa-indexer` with checked-in console
  scripts for the current neutral operator tools, and kept the package version
  aligned to `.project_metadata`.
- Documented checked-in domain packs in `docs/project/DOMAIN_PACKS.md`,
  promoted `general.v1` to runtime, and aligned the README flagship example
  with the current pack surface.

### Validation

- Hardened repo validation with `ruff`, `ruff format --check`, `mypy`,
  `pytest-cov`, and `jsonschema`-backed schema integrity checks in
  `.github/workflows/repo-hygiene.yml`, `pyproject.toml`, and the matching
  packaging and schema-integrity tests.

### Documentation

- Added `CHANGELOG.md`, `docs/repo-layout.md`, and tracked-surface/reference
  repairs so placeholder scaffolding, script wrappers, and checked-in tool/docs
  links stay explicit.
- Removed the unsupported top-level `Bug_Fixer.sh` surface instead of adding it
  as a documented operator workflow.

## [8.8.0.3]

Historical baseline referenced by `.project_metadata`. Detailed changelog
entries were not preserved in this tree before `CHANGELOG.md` was added.
