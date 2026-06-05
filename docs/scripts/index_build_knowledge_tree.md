# Index Build Knowledge Tree

`tools/scripts/Index_Build_Knowledge_Tree.sh` is the operator-facing wrapper for
`tools/scripts/build_publication_artifacts.py`.

Purpose:

- build the current end-to-end knowledge-tree publication chain
- read one canonical SQLite store
- write `knowledge_tree_export.json`
- write `public_presentation.json`
- render the static publication output
- run graph-closure preflight over the canonical store
- run the final public leak scan over the rendered site bundle

Current wrapper behavior:

- `--help` prints wrapper usage and the paired documentation path
- `--check` verifies that the selected Python interpreter exists and that
  `build_publication_artifacts.py` is present and readable
- `--dry-run` prints the exact Python command that would run
- any builder arguments after `--` are forwarded unchanged to
  `build_publication_artifacts.py`

Important safety rules:

- no network access is used by the wrapper
- no LLM is used by the wrapper
- the wrapper does not bootstrap or mutate the canonical store; the store is an
  input only
- generated files are written only under the caller-selected `--output-dir`
- the publication builder validates export and presentation artifacts before
  rendering and fails closed on invalid inputs
- graph-closure preflight is read-only; strict preflight fails on true orphan
  canonical rows
- the final public output is leak-scanned before the build returns success

Key inputs:

- wrapper options: `--help`, `--check`, `--dry-run`
- required forwarded builder inputs:
  - `--db`
  - `--output-dir`
- optional forwarded builder inputs:
  - `--generated-at`
  - `--build-id`
  - `--built-at`
  - `--export-id`
  - `--display-name`
  - `--workspace-id`
  - `--graph-closure-strict`
  - `--no-graph-closure-preflight`
  - `--format json|text`

Key outputs:

- `<output-dir>/knowledge_tree_export.json`
- `<output-dir>/public_presentation.json`
- `<output-dir>/search/local_search_projection.json`
- `<output-dir>/search/local_search_results.json`
- `<output-dir>/search/local_search.sqlite`
- `<output-dir>/static/`
- `<output-dir>/graph-closure-report.json`
- `<output-dir>/leak-scan-report.json`

Current failure behavior:

- missing or unreadable `--db` input fails before publication completes
- invalid export or presentation artifacts fail before rendering completes
- static renderer failures return nonzero
- public leak-scan failures return nonzero
- output path problems return nonzero

Example:

```bash
tools/scripts/Index_Build_Knowledge_Tree.sh -- \
  --db path/to/canonical.sqlite \
  --output-dir site-build \
  --generated-at 2026-06-03T12:00:00Z \
  --build-id build-20260603T120000Z \
  --built-at 2026-06-03T12:00:00Z
```

Relationship to F4:

- this wrapper currently runs the full publication producer chain implemented in
  `build_publication_artifacts.py`
- it does not require prebuilt `knowledge_tree_export.json` or
  `public_presentation.json` when used normally
- if you only need the static renderer, use `build_static_knowledge_tree.py`
  directly with already validated export and presentation inputs

When changing wrapper usage text, forwarded builder behavior, output layout, or
publication safety checks, keep this document, `Index_Build_Knowledge_Tree.sh`,
`build_publication_artifacts.py`, and the related publication tests aligned.
