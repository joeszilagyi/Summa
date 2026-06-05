# Audit Rebuildability

`tools/scripts/audit_rebuildability.py` audits whether local run artifacts are
sufficient to rebuild a meaningful canonical SQLite store into a fresh temporary
database.

This is not a backup system and does not promote a rebuilt database to
authority. It is an operational durability check: the canonical store remains
the durable object, and the audit proves whether current artifacts can replay
the same canonical writes if needed.

## Command

Validation-only audit:

```bash
python3 tools/scripts/audit_rebuildability.py \
  --runs-dir runs \
  --replay-mode validate_only \
  --output runs/rebuildability-report.json
```

Rebuild into a fresh temp DB:

```bash
python3 tools/scripts/audit_rebuildability.py \
  --runs-dir runs \
  --replay-mode rebuild_temp \
  --output runs/rebuildability-report.json
```

Compare a rebuilt temp DB against an existing canonical store:

```bash
python3 tools/scripts/audit_rebuildability.py \
  --runs-dir runs \
  --canonical-db dbs/canonical.sqlite \
  --replay-mode compare_existing \
  --output runs/rebuildability-report.json
```

Equivalent package command:

```bash
summa-audit-rebuildability --runs-dir runs --output runs/rebuildability-report.json
```

Shell wrapper:

```bash
tools/scripts/Index_Audit_Rebuildability.sh --runs-dir runs --output runs/rebuildability-report.json
```

## Discovered Artifacts

The audit discovers known local artifact families:

- `gather-candidate-batch.json`
- source acquisition execution run directories containing `execution-record.json`,
  `capture-events.jsonl`, and `extraction-records.jsonl`
- canonical ingest reports
- topic-cycle manifests
- scheduled-cycle manifests
- feedback plans
- review-decision apply result artifacts
- canonical-write spool records
- graph-closure reports
- release-readiness reports
- publication artifacts, as reference-only material

Only artifacts with existing canonical replay APIs are replayed. Publication
artifacts, release-readiness reports, graph-closure reports, and standalone
review-decision result reports are reference artifacts; they are not canonical
write recipes.

## Replay Modes

`validate_only` discovers artifacts, validates what can be validated, checks
manifest references, and reports missing replay support. It does not initialize
or mutate a rebuild database and does not claim the store is rebuildable.

`rebuild_temp` initializes a fresh canonical SQLite database and replays
validated candidate-batch, execution-artifact, and canonical-write spool records
through the existing canonical APIs. It then validates the rebuilt store and
runs graph closure.

`compare_existing` performs `rebuild_temp`, then compares row counts and stable
key hashes against the supplied `--canonical-db`. The comparison is meaningful
state comparison, not byte-for-byte SQLite equality.

## Safety

The audit never mutates the production canonical store. A rebuild DB must be a
fresh temp DB or a caller-supplied non-existing path. Existing paths are refused
unless `--force-temp-overwrite` is supplied, and the rebuild path must not equal
the comparison canonical DB path.

Temporary rebuild DBs are removed by default. Use `--keep-temp-db` only for
local operator inspection.

## Graph Closure

When rebuild mode runs, graph closure is executed on the rebuilt DB. True orphan
errors make strict rebuildability fail. Unresolved tracked rows are reported as
warnings, not hidden.

## What Is Not Rebuilt

The audit does not fetch sources, invoke an LLM, copy raw payloads, replay public
publication artifacts, or infer facts from reports. Review decisions are
replayable only when represented by a canonical-write spool record or another
complete replay recipe. Standalone review-decision result reports are treated as
reference evidence.
