# pipeline_registry

## Purpose

`tools/pipeline_registry/` holds the static contract registry for the live pipeline.

It is built from JSONL contract files and materialized into SQLite for queryable audits.

## Source of truth

Editable contracts live in:

- `tools/pipeline_registry/contracts/artifact_classes.jsonl`
- `tools/pipeline_registry/contracts/repo_path_rules.jsonl`
- `tools/pipeline_registry/contracts/surfaces.jsonl`
- `tools/pipeline_registry/contracts/surface_options.jsonl`
- `tools/pipeline_registry/contracts/surface_option_effects.jsonl`
- `tools/pipeline_registry/contracts/surface_io.jsonl`
- `tools/pipeline_registry/contracts/surface_dependencies.jsonl`

## Builder

Build the registry with:

```bash
python3 tools/pipeline_registry/build_pipeline_registry.py
```

Default output:

```text
dbs/rollups/pipeline_registry.sqlite
```

The DB is generated locally and ignored in Git.

Validate the contracts, schema, and repo inventory without replacing the
generated DB:

```bash
python3 tools/pipeline_registry/build_pipeline_registry.py --check
```

Successful builds write to a temporary SQLite file in the destination directory
and atomically replace the target DB only after validation and insertion finish.

Archive checkouts without a `.git` directory are supported when you supply
`--inventory-file` with a newline-delimited list of repo-relative paths. That
explicit inventory replaces the Git file list so contract coverage and
path-rule classification can still be checked from a source archive without
walking the entire filesystem. Git-specific departed file history is only
available when a previous generated registry DB or a Git checkout is available.

## What the builder validates

- current live surface coverage for the normalized tree
- vocabulary/enum correctness
- duplicate contract keys
- tracked-file inventory classification through `repo_path_rules.jsonl`
- exact alignment between files classified as `surface` and registered
  `surface.path` rows
- departed-file preservation from a previous generated DB

If a current live surface exists on disk but is missing from the contracts, the build fails closed.

## Useful queries

Show the current contract summary by surface:

```bash
sqlite3 dbs/rollups/pipeline_registry.sqlite \
  "select * from v_surface_contract_summary order by surface_key;"
```

Show which prompts are bound to which scripts:

```bash
sqlite3 dbs/rollups/pipeline_registry.sqlite \
  "select * from v_prompt_bindings order by surface_key, prompt_surface_key;"
```

Show the causal chain out of the live place-build driver:

```bash
sqlite3 dbs/rollups/pipeline_registry.sqlite \
  "select producer_surface_key, artifact_key, consumer_surface_key
     from v_causal_edges
    where producer_surface_key='script.index_build_place'
    order by artifact_key, consumer_surface_key;"
```

Show option impacts for one surface:

```bash
sqlite3 dbs/rollups/pipeline_registry.sqlite \
  "select option_name, effect_kind, coalesce(artifact_key, '<none>')
     from v_option_impacts
    where surface_key='script.index_build_place'
    order by option_name, effect_kind;"
```

Show current tracked files with no path-rule decision:

```bash
sqlite3 dbs/rollups/pipeline_registry.sqlite \
  "select repo_path from v_repo_unmapped_files order by repo_path;"
```

Show departed tracked files preserved from a previous build:

```bash
sqlite3 dbs/rollups/pipeline_registry.sqlite \
  "select * from v_repo_departed_files order by repo_path;"
```
