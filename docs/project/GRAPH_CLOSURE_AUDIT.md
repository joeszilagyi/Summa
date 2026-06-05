# Graph Closure Audit

Summa graph closure is a read-only operating invariant for the canonical
SQLite store. It checks whether canonical rows are attached to the graph,
tracked as unresolved/reviewable, or true orphan errors.

Graph closure is not truth validation. A false source claim with provenance and
review state can pass graph closure. A true-looking claim with no provenance or
graph path fails graph closure.

## What It Checks

The audit covers canonical source, graph, review, and supporting rows,
including:

- `source_access`
- `capture_event`
- `extraction_record`
- `extraction_detected_entity`
- `source_claim`
- `source_relationship`
- `authority_reconciliation`
- `authority_merge_event`
- `review_state_history`
- `provenance_event`
- `work_subject`
- `authority_identifier`

Rows must be linked, deterministically resolvable by object reference, queued
for review/reconciliation, explicitly unresolved with provenance/context, or
intentionally exempt as supporting/operational evidence.

## Severity Policy

- `pass`: audited rows are attached or intentionally exempt.
- `no_rows`: the store is initialized and valid but contains no audited rows.
- `pass_with_unresolved`: unresolved tracked rows exist; they remain visible.
- `warning`: repairable or non-fatal closure issues exist.
- `fail`: true orphan rows exist.
- `unavailable`: no valid canonical store is available or the check is disabled.

Unresolved tracked rows are not hidden and are not treated as accepted facts.
True orphan rows fail strict mode.

## Command

```bash
python3 tools/scripts/audit_canonical_graph_closure.py \
  --db path/to/canonical.sqlite \
  --report-json runs/graph-closure-report.json \
  --strict
```

`--strict` exits nonzero only when true orphan errors exist. The audit never
repairs, deletes, accepts, verifies, or changes canonical rows.

## Cycle Operation

`tools/scripts/run_topic_cycle.py` runs graph closure by default before closing
a cycle and writes:

- `<run-dir>/graph-closure-report.json`

The topic-cycle manifest records:

- whether graph closure ran
- strict mode
- report path and hash
- true orphan count
- unresolved tracked count
- repairable count
- quarantined count

Use `--no-graph-closure` to disable the cycle check explicitly. Use
`--graph-closure-strict` to fail a cycle when true orphan rows are found.

## Doctor And Dashboard

`tools/scripts/local_doctor.py` reports a `graph_closure` section. The operator
dashboard renders that section with status and top issue summaries. Local doctor
does not run repair mode.

## Release Readiness

`tools/scripts/build_release_readiness_bundle.py` can stage or generate an
optional `graph-closure-report.json`. In strict graph-closure mode, true orphan
errors become release-readiness block findings. Unresolved tracked rows are
warnings.

## Publication Preflight

`tools/scripts/build_publication_artifacts.py` runs graph closure preflight by
default and writes `graph-closure-report.json` under the publication output
directory. Strict preflight fails publication on true orphan rows. Non-strict
preflight records the status while preserving existing publication behavior.

## Repair Boundary

Repair is explicit and outside this audit. None of the cycle, doctor, dashboard,
release-readiness, or publication integrations mutate the canonical store.

Graph-closure reports are local operator diagnostics. They do not include raw
payloads, raw extracted text, model prompts, or private review notes by design.
