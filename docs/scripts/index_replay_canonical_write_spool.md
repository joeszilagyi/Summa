# Replay Canonical Write Spool

`replay_canonical_write_spool.py` replays validated local spool records created
when a canonical SQLite write could not be completed. A spool record is recovery
evidence for an intended canonical write. It is not a canonical row and it is
not treated as truth until replay succeeds through the normal canonical APIs.

## When Spool Records Are Written

Spool records may be written only when degraded mode is explicit, such as:

```bash
python3 tools/scripts/ingest_gather_candidate_batch.py \
  --db dbs/canonical.sqlite \
  --batch runs/gather/run-001/gather-candidate-batch.json \
  --degraded-spool \
  --spool-dir runs/spool
```

Supported operation kinds:

- `candidate_batch_ingest`
- `execution_artifact_ingest`
- `review_decision_apply`
- `cycle_evidence_write`

Invalid source artifacts do not create replayable spool records. The input must
validate first; otherwise replay would preserve a known-bad operation.

## Replay

Replay validates each spool record, validates the target canonical store, checks
schema compatibility, and then calls the same canonical ingest or review helper
used by the original command.

```bash
python3 tools/scripts/replay_canonical_write_spool.py \
  --db dbs/canonical.sqlite \
  --spool-path runs/spool/canonical-unavailable \
  --output runs/spool/replay-report.json
```

Dry-run mode validates and reports intended work without mutating the canonical
store or spool records:

```bash
python3 tools/scripts/replay_canonical_write_spool.py \
  --db dbs/canonical.sqlite \
  --spool-path runs/spool/canonical-unavailable \
  --dry-run
```

## Idempotence And Status

Replay does not delete spool records. Successful replay marks a record as
`replayed` and records result references. A later replay skips records already
marked `replayed`. Existing canonical ingest and review helpers remain
responsible for row-level idempotence.

Topic cycles that use degraded spooling report `degraded`, not `completed`, and
their manifests reference the pending spool artifacts.

## Privacy

Spool records are local/operator-private. They store artifact paths, artifact
hashes, operation metadata, failure reasons, and replay recipes. They do not
embed raw source payload bytes by default. Do not publish spool records without
a redaction review.
