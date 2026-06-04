# Evaluate Topic Saturation

`tools/scripts/evaluate_topic_saturation.py` derives an operational saturation
state for one topic workspace.

Purpose:

- read recent canonical ingest provenance for one subject
- compute accepted and reviewable yield over a bounded lookback window
- detect repeated low-yield cycles under a named policy
- report scheduler action such as `run`, `deprioritize`, `cooldown`, or `halt`
- expose the reason codes and recent yield summary for operators

Example:

```bash
python3 tools/scripts/evaluate_topic_saturation.py \
  --workspace /path/to/workspace \
  --db /path/to/canonical.sqlite \
  --policy config/topic_saturation_policy.v1.json \
  --format json
```

Policy:

- the checked-in default policy is `config/topic_saturation_policy.v1.json`
- the schema is `config/topic_saturation_policy.v1.schema.json`
- policy fields control lookback cycles, useful-yield thresholds, accepted
  versus reviewable yield mode, backlog pressure, cooldown, and scheduler action

Important boundaries:

- saturation is operational, not epistemic
- a saturated topic is not complete or true; it is not worth more expansion
  under the current budget and recent yield
- the evaluator is read-only and does not delete topics, claims, leads, or
  canonical rows
- the evaluator does not apply review decisions
- no source fetching, LLM calls, or network access are performed

Scheduler integration:

- `select_scheduled_workspaces.py` preserves existing behavior unless a
  saturation policy and canonical DB are supplied
- saturated workspaces can be deprioritized or skipped with explicit reasons
- `--include-saturated` records an override and allows operator-directed runs
- `run_scheduled_topic_cycles.py` respects saturation state in planned-run
  records and defers halted or cooling-down workspaces unless an override is
  recorded
