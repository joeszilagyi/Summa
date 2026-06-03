# Contributing

This repository is the live local-first Indexer workspace. Changes should keep
the current toolchain operable while moving the repo toward the topic-neutral
contracts described in `README.md`, `docs/README.md`, and `docs/repo-layout.md`.

## Change Flow

1. Start from the current `main` branch and inspect the relevant issue or design
   contract before editing.
2. Keep changes scoped to the issue being addressed. Do not mix serviceability,
   prompt-contract, runtime, data cleanup, and public-export work unless the
   issue explicitly calls for that coupling.
3. Prefer additive migration over destructive pruning. The repo is archive-first:
   unique leads, historical notes, state ledgers, and retained migration
   substrate may be demoted, reclassified, or superseded, but should not be
   deleted without an explicit issue and evidence.
4. Keep raw payloads, full extracted text, local logs, runtime bundles, secrets,
   and scratch outputs out of tracked history unless a storage/export policy
   explicitly promotes the artifact.
5. Follow the root `.editorconfig` for baseline editor behavior and
   `.gitattributes` for repository text/binary handling.
6. Do not add timestamped `.bak` files, editor/session backups, or working zips
   as archive substitutes. Git history and explicit archive docs cover retained
   history; local backup clutter belongs outside the index.
7. Run the narrowest relevant validation before opening or updating a PR. For
   broad repo changes, run `python3 -m pytest -q` when practical.
8. If `.project_metadata` `CURRENT_BUILD` changes, update `CHANGELOG.md` in the
   same change so the build bump has a human-readable repository entry.

## Supported Surfaces

Live supported surfaces include:

- `config/` schemas, domain packs, durability policies, and examples
- `tools/scripts/` neutral entrypoints and shared shell helpers
- `tools/prompts/` transitional prompt templates until prompt bundles fully
  replace them
- `tools/validators/`, `tools/pipeline_registry/`, and `tools/source_db_tools/`
- `tests/` fixtures and regression tests
- `docs/` architecture, operations, and migration contracts
- tracked `index/` scaffolding plus checked-in contracts and fixtures that
  describe local database and runtime surfaces

`runtime/`, local caches, local assistant notes, secrets, raw collateral
payloads, and generated test corpora are local-only by default. See
`TRACKED_SURFACE.md` for the boundary.

## Legacy and History Policy

Legacy material is retained to preserve migration context, replayability, and
historical baselines. It is not automatically a supported public API.

- `tools/scripts/legacy/` and `tools/legacy/`, when populated, are retained
  historical surfaces. Do not modify them for routine fixes unless the issue is
  specifically about legacy behavior or migration.
- Versioned SQLite baselines such as historical `tools/source_db_tools_v8_*` directories
  are treated as frozen historical baselines if they are restored or imported.
  The live SQLite line is `tools/source_db_tools/`, with history summarized in
  `docs/history/sqlite-tooling-history.md`.
- Legacy article-shaped substrate under `workspace-roots/` is transitional or
  private-export material by default. Convert it into subject briefs, catalogs,
  ledgers, manifests, metadata, or validated DB rows through explicit migration
  work rather than rewriting it in place casually.
- If a legacy surface is superseded, add a note, adapter, or migration path
  before removal. Removal needs an issue that names the retained replacement and
  explains why replay or audit value is no longer needed.

## Pull Requests

Use `.github/PULL_REQUEST_TEMPLATE.md`. At minimum, describe the purpose, scope,
validation performed, prompt-contract impact, state/catalog impact, and rollback
risk. If a change affects public/private boundaries, link to
`docs/project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md` or
`docs/project/STORAGE_AND_PUBLICATION_POLICY.md`.
