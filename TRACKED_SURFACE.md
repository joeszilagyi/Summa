# Tracked Surface

This file defines the current repository boundary between source-controlled
contract surface and local operational state.

Tracked does not mean public. Untracked does not mean unimportant.

Tracked in the current tree:

- source code under `tools/`
- checked-in schemas, domain packs, durability policies, and examples under
  `config/`
- prose docs under `docs/`
- regression tests and fixtures under `tests/` and `tests/fixtures/`
- checked-in prompt templates under `tools/prompts/`
- tracked public-safe fixture inputs such as
  `tests/fixtures/source_adapter_runtime/**/*.pdf`
- placeholder scaffolding such as `index/Dates/.gitkeep` and
  `tools/scripts/legacy/.gitkeep`; these keep reserved surfaces visible in a
  clean checkout and do not mean the corresponding runtime feature is already
  implemented
- top-level project metadata such as `README.md`, `CONTRIBUTING.md`,
  `pyproject.toml`, `.project_metadata`, and `.github/`

Local-only or ignored by default:

- `runtime/**`
- `dbs/**`
- `index/Places/**`
- `test_corpora/**`
- `.local/**`
- `out/**`
- `build/`
- `dist/`
- `*.sqlite`, `*.sqlite3`, and `*.db`
- `*.pdf` except the explicitly re-included fixture paths in `.gitignore`
- archives such as `*.zip`, `*.tar`, and compressed variants
- raw payload captures, local caches, runtime bundles, logs, and scratch output

Boundary rules:

- tracked fixtures are safe contract examples, not real local user data
- local workspace registries, ledgers, backups, bundles, and SQLite stores may
  be operationally important even when Git ignores them
- tests and validators define contract behavior where code already exists
- raw payload archives, full extracted text, runtime logs, secrets, and local
  assistant notes should not be committed accidentally
- generated public bundles and rendered sites are local outputs unless a test
  fixture or explicit checked-in example includes them
- generated contents under reserved roots such as `runtime/`, `dbs/`, and
  ignored `index/` branches stay local unless a fixture or contract explicitly
  promotes them

Relationship to publication:

- publication is a filtered projection, not a dump of local storage
- public-safe boundaries are documented in
  [docs/project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md](docs/project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md)
- local-first storage and backup posture are summarized in
  [docs/project/STORAGE_AND_PUBLICATION_POLICY.md](docs/project/STORAGE_AND_PUBLICATION_POLICY.md)

If a new artifact family needs to become tracked, add the schema or contract,
the validator or test coverage, and the ignore-rule change together.
