# Documentation

`docs/` holds the checked-in maintenance notes, project contracts, safety
policies, and history summaries that sit next to Summa's code, schemas,
wrappers, validators, and fixtures.

Use `README.md` for the top-level project purpose. Use the docs tree for the
current operational and contract surface.

Documentation tree:

- [repo-layout.md](repo-layout.md): current repository shape, placeholder
  scaffolding, and reserved local output roots
- [../TRACKED_SURFACE.md](../TRACKED_SURFACE.md): tracked versus local-only
  boundary derived from the current tree and `.gitignore`
- [project/DOMAIN_PACKS.md](project/DOMAIN_PACKS.md): checked-in domain pack
  catalog, statuses, and runtime-readiness notes
- [project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md](project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md):
  current public/export boundary, leak-scan role, and never-publish families
- [project/STORAGE_AND_PUBLICATION_POLICY.md](project/STORAGE_AND_PUBLICATION_POLICY.md):
  local-first storage, backup, runtime, and publication posture
- [history/sqlite-tooling-history.md](history/sqlite-tooling-history.md):
  current SQLite helper line, preserved history references, and missing older
  baselines

How the tree is organized:

- `docs/project/`: project-level contracts, safety rules, and publication
  boundaries
- `docs/scripts/`: paired operator-wrapper docs that must stay aligned with
  wrapper help text and builder behavior
- `docs/tools/`: tool-line maintenance notes, especially for
  `tools/source_db_tools/` and `tools/pipeline_registry/`
- `docs/history/`: historical summaries when the live tree still references
  older baselines or migration context

How docs relate to code and tests:

- prose docs should match the current implementation, not aspirational design
- schemas and checked-in config live under `config/`
- validators live under `tools/validators/`
- regression tests and fixtures live under `tests/` and `tests/fixtures/`
- wrapper help text, paired docs, schemas, validators, and tests should move
  together

Package console entry points:

- installable operator commands use the `summa-*` prefix and are declared in
  `pyproject.toml`
- live runtime-spine commands such as gather execution, source-adapter
  execution, candidate ingestion, execution-artifact ingestion, topic cycles,
  scheduled cycles, workspace selection, review-decision application, and
  network-safety evaluation must have package console commands
- `tools/scripts/Index_*.sh` wrappers remain supported compatibility surfaces;
  each live wrapper must either map to a console command or have a specific
  exclusion reason in `tests/test_packaging_metadata.py`
- adding a new live operator wrapper without updating packaging or the explicit
  exclusion map fails the packaging metadata tests

If a behavior is only documented, say that clearly. If a validator or test
already defines the contract, treat the code and the test as the executable
source of truth and keep the prose in sync.
