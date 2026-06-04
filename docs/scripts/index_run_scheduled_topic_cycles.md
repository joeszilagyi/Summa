# Index Run Scheduled Topic Cycles

`tools/scripts/Index_Run_Scheduled_Topic_Cycles.sh` is the operator wrapper for
`tools/scripts/run_scheduled_topic_cycles.py`.

Purpose:

- consume `planned-run.v1` records produced by `select_scheduled_workspaces.py`
- enforce each selected workspace's recorded `run_budget`
- run one bounded topic cycle per selected workspace
- append terminal runtime-ledger events for the existing scheduler
  failure-state reconciliation path
- write a `scheduled-topic-cycles-run.v1` manifest with per-workspace outcomes

Example:

```bash
tools/scripts/Index_Select_Scheduled_Workspaces.sh \
  --registry runtime/config/topic_workspaces.local.json \
  --planned-runs-jsonl runtime/planned-runs.jsonl \
  --run-budget-max-attempts 2 \
  --run-budget-max-runtime-seconds 900

tools/scripts/Index_Run_Scheduled_Topic_Cycles.sh \
  --selection runtime/planned-runs.jsonl \
  --db /path/to/canonical.sqlite \
  --run-dir runs/scheduled-topic-cycles/scheduled-001 \
  --mode dry-run
```

Budget behavior:

- workspaces whose prior terminal runtime-ledger attempts already meet
  `run_budget.max_attempts` are deferred
- cycle runtime is measured per workspace and compared to
  `run_budget.max_runtime_seconds`
- failures are appended as `command_failure` runtime-ledger events
- successes are appended as `command_end` runtime-ledger events

Safety model:

- this is not a daemon and does not loop indefinitely
- remote fetch is not enabled
- no network access is performed by default
- review decisions and authority merges are not applied
- child cycles use `run_topic_cycle.py` rather than duplicating stage logic
