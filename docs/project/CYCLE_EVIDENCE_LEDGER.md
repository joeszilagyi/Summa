# Cycle Evidence Ledger

Summa records bounded topic-cycle activity in a local cycle evidence ledger. The ledger is operational evidence: it answers what a cycle did, which stages ran, which artifacts were produced, which candidates were considered or deferred, which tools failed, and which operator overrides were active.

The ledger is not a source of truth for claims. It does not replace `provenance_event`, topic-cycle manifests, canonical source/capture/extraction rows, review-state history, or review-decision application. It records evidence about work performed elsewhere.

## Tables

The canonical SQLite store includes these operational supporting tables:

- `cycle_event`: one row per bounded topic-cycle run, keyed by a deterministic local `cycle_event_id`.
- `cycle_stage_event`: one row per recorded cycle stage, ordered by stage position.
- `cycle_artifact_ref`: references manifests, candidate batches, ingest reports, execution artifacts, feedback plans, and related local files by path, hash, type, validation posture, and privacy classification.
- `cycle_candidate_considered`: basic candidates already visible to the current cycle, such as gather candidates and feedback next actions.
- `cycle_candidate_excluded`: skipped/deferred candidates and stage-level deferrals already known to the current runner.
- `cycle_tool_failure`: failed stage/tool evidence with command name, failure kind, retryability, and summary.
- `cycle_operator_override`: explicit operator inputs such as `--force`, `--allow-network`, fixture paths, manual feedback plans, source handoffs, or execution-run fixtures.

These tables are supporting operational evidence tables, not canonical entity, relationship, assertion, or provenance-event families.

## Relationship To Existing Artifacts

Topic-cycle JSON manifests remain the durable run artifact. The ledger complements them by normalizing their stage, artifact, candidate, failure, and override evidence into queryable SQLite rows. When a non-dry-run topic cycle completes or fails after the canonical DB is available, `run_topic_cycle.py` records the manifest into the ledger.

Dry runs do not write ledger rows because dry-run mode promises not to mutate the supplied canonical database.

Scheduled topic cycles still use their runtime JSONL ledger for scheduler failure-state reconciliation. Each child topic cycle owns its SQLite cycle evidence rows, and the scheduled-run manifest records the child `cycle_event_id` when the child manifest exposes one.

## Privacy

Cycle evidence is local/private operator state. Artifact refs may include local paths, command names, fixture paths, failure summaries, and override reasons. Public publication and public knowledge-tree exports must not include raw cycle evidence unless a separate redacted diagnostic export explicitly permits it.

Do not store raw source payloads, raw extracted text, model prompts, private review notes, credentials, or accepted factual claims in the cycle evidence ledger.

## Querying

Use the importable helper module:

```python
from tools.source_db_tools import canonical_store, cycle_evidence_ledger

conn = canonical_store.connect_existing_read_only(db_path)
summary = cycle_evidence_ledger.summarize_cycle_evidence(conn, cycle_event_id)
```

The helper returns deterministic ordered stage and artifact summaries. Write helpers are transaction-friendly and use parameterized SQL.

## Adding Evidence

When adding a new cycle stage or operator input:

- Record stage status in the topic-cycle manifest first.
- Add ledger recording only for evidence already produced by the runner.
- Do not create source claims, mark review decisions, or infer missing facts.
- Classify artifact privacy as local operator state unless it is explicitly public-safe.
- Keep candidate considered/excluded rows basic unless a dedicated selection-explanation feature expands them.
