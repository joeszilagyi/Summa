# Index Run Topic Cycle

`tools/scripts/Index_Run_Topic_Cycle.sh` is the operator wrapper for
`tools/scripts/run_topic_cycle.py`.

Purpose:

- run one bounded local-first cycle for one topic workspace
- resolve the subject runtime and domain pack
- validate the canonical store before and after the cycle
- render the gather prompt and validate the emitted candidate batch
- ingest gather candidates through the canonical ingestion API outside pure
  dry-run mode
- optionally ingest validated acquisition execution artifacts or run a local
  source-adapter handoff
- optionally build a candidate-feedback plan before or after ingestion
- run graph-closure audit before cycle close and write a report artifact
- write a `topic-cycle-run.v1` manifest with stage statuses, artifacts, hashes,
  counts, warnings, and failures

Examples:

```bash
tools/scripts/Index_Run_Topic_Cycle.sh \
  --workspace /path/to/workspace \
  --db /path/to/canonical.sqlite \
  --run-dir /path/to/workspace/runs/topic-cycle/cycle-001 \
  --dry-run
```

```bash
tools/scripts/Index_Run_Topic_Cycle.sh \
  --workspace /path/to/workspace \
  --db /path/to/canonical.sqlite \
  --run-dir /path/to/workspace/runs/topic-cycle/cycle-002 \
  --mode local \
  --cycle-depth 2 \
  --use-prior-state \
  --feedback-plan auto \
  --build-next-feedback-plan
```

Dry-run behavior:

- validates inputs
- renders gather output and writes a cycle manifest
- may validate fixture artifacts when supplied
- does not mutate the supplied canonical database
- does not invoke a real LLM
- does not perform network access or remote fetch

Live-safe local behavior:

- `--mode local` can mutate the supplied canonical store through existing
  ingestion APIs
- local acquisition runs only when a source handoff is supplied
- remote fetch is still disabled in this command
- source claims remain proposed or needs-review records; the cycle does not
  apply review decisions
- graph closure checks attachment/reviewability, not factual truth

Graph closure:

- enabled by default
- writes `<run-dir>/graph-closure-report.json`
- records status, report path/hash, true orphan count, unresolved tracked count,
  repairable count, and quarantined count in the cycle manifest
- `--graph-closure-strict` fails the cycle on true orphan rows
- `--no-graph-closure` disables the audit explicitly and records that in the
  manifest

Relationship to smoke:

- `operator_path_smoke.py` is a fast health check
- `run_topic_cycle.py` is the operator-facing cycle contract
- the smoke path exercises this runner instead of maintaining a separate
  full-chain ingestion sequence

Overwrite behavior:

- existing completed cycle manifests are refused unless `--force` is supplied
- failed or partial manifests are refused unless a new run id or `--force` is
  used
- `--resume` is reserved for a later idempotent resume implementation

Current scope limits:

- no scheduler daemon
- no remote live fetch
- no review-decision application
- no publication rebuild by default
