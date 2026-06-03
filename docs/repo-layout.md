# Repo Layout

This document names the durable tracked roots and the reserved local roots in
the current checkout. Pair it with [../TRACKED_SURFACE.md](../TRACKED_SURFACE.md)
for the tracked-versus-local boundary.

Top-level files:

- `README.md`: project purpose and high-level technical position
- `CONTRIBUTING.md`: contributor workflow, supported surfaces, and history
  policy
- `pyproject.toml`: packaging metadata and pytest configuration
- `.project_metadata`: current build marker
- `.gitignore`: local-only and generated-output boundary
- `.github/`: issue, PR, and workflow metadata

Tracked top-level directories:

- `config/`: checked-in schemas, domain packs, durability policies, examples,
  and view-model schemas
- `docs/`: project contracts, wrapper docs, tool-line notes, and history
  summaries
- `tools/`: implementation surfaces
- `tests/`: regression tests and tracked fixtures
- `index/`: tracked scaffolding only in the current tree

Current `config/` shape:

- `config/domain_packs/`: checked-in domain-pack and prompt-bundle contracts
- `config/durability_policies/`: crown-jewel backup policy inputs
- `config/view_models/`: view-model JSON Schema files
- remaining top-level JSON and schema files define validators, sidecars, public
  outputs, search projections, and runtime ledgers

Current `tools/` shape:

- `tools/scripts/`: operator entrypoints, planners, publication builders, view
  emitters, and shell wrappers
- `tools/scripts/lib/`: shared shell helpers
- `tools/scripts/legacy/`: placeholder-only historical staging root in this
  checkout; currently contains only `.gitkeep`
- `tools/common/`: shared Python helpers such as leak scanning, publication
  building, runtime ledgers, backup planning, and workspace locks
- `tools/source_db_tools/`: current SQLite helper line for canonical bootstrap,
  review/provenance helpers, registry loaders, safety checks, backfill, and
  simulation utilities
- `tools/prompts/`: checked-in prompt templates under `general/` and
  `organism/`
- `tools/validators/`: machine-readable contract validators
- `tools/pipeline_registry/`: tracked-surface inventory builder and checked-in
  registry contracts
- `tools/collateral/`: narrow local collateral helpers such as `pdf_extract.py`

Current `tests/` shape:

- `tests/`: regression tests for validators, wrappers, publication, leak
  scanning, source adapters, backup posture, and SQLite helpers
- `tests/fixtures/`: tracked safe corpora and validator fixtures, including
  public-bundle leak fixtures, source-adapter runtime fixtures, prompt fixtures,
  and static knowledge-tree inputs

Tracked scaffolding and placeholders:

- `.gitkeep` files are present only to keep intentionally empty directories
  visible in a clean checkout
- placeholder directories are not evidence that the corresponding runtime or
  index producer has been implemented
- `index/Dates/.gitkeep`: visible tracked scaffold for a reserved date-oriented
  index surface; the current checkout does not ship a producer that populates
  this directory
- `tools/scripts/legacy/.gitkeep`: visible tracked scaffold for intentionally
  retained legacy shell scripts; the empty directory does not imply missing live
  wrapper code

Reserved local or generated roots:

- `runtime/`: ignored local runtime state such as ledgers, locks, local
  registries, backups, and support bundles
- `dbs/`: ignored local SQLite stores and rollups
- `index/Places/`: ignored local output root; the reserved path is visible in
  `.gitignore`, not as a tracked populated directory
- `test_corpora/`, `.local/`, `out/`, `build/`, and `dist/`: ignored local or
  generated output roots

Current tree notes:

- `runtime/` and `dbs/` are part of the repo's operational vocabulary, but
  they are not tracked as populated directories in this checkout
- generated contents beneath `runtime/`, `dbs/`, and ignored `index/` roots are
  local state unless an explicit fixture or checked-in contract promotes them
- generated databases, runtime outputs, local payloads, and topic corpora stay
  out of Git by default unless a fixture or explicit checked-in contract says
  otherwise
- tracked fixture payloads are safe stand-ins for testing, not a license to
  commit real user data or local workspace state
