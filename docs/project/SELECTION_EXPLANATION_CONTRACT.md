# Selection Explanation Contract

Summa selection explanation records are local operational evidence for choices the
runtime already makes. They explain what was considered, what was selected, what
was excluded, and which policy or override shaped the decision.

Selection explanations do not decide truth. They do not accept claims, verify
relationships, bypass source safety gates, or execute acquisitions. The cycle
runner, scheduler, feedback planner, source safety checks, and canonical review
tools remain the authority for execution and curation.

## Current Surfaces

The first contract shape is `selection-explanation.v1`.

Current producers:

- `tools/scripts/build_candidate_feedback_plan.py` writes a
  `selection_explanation` object into each candidate feedback plan.
- `tools/scripts/select_scheduled_workspaces.py` writes a
  `selection_explanation` object into each scheduler selection report and stamps
  planned-run records with the explanation id.
- `tools/scripts/run_topic_cycle.py` records feedback-plan selection explanation
  references in the topic-cycle manifest.
- `tools/source_db_tools/cycle_evidence_ledger.py` records considered and
  excluded feedback candidates into the cycle evidence ledger when a cycle
  manifest is recorded.

The feedback planner also supports `--record-selection-ledger` for local
operator runs that want to persist the planner's selection evidence directly.
That option writes only LA1 cycle evidence ledger rows.

## Required Evidence

Each explanation records:

- `selection_kind`, such as `feedback_next_action` or `scheduled_workspace`.
- The selected candidate and its rationale.
- Every candidate already considered by the planner or selector.
- Excluded or deferred candidates with explicit reasons.
- Policy id and policy context.
- Budget or limit context when the current planner exposes it.
- Operator overrides when a manual choice bypasses normal candidate ranking.

The selected candidate must appear in the considered-candidate list unless an
operator override is explicitly recorded. Excluded candidates must keep their
reason; exclusion is evidence, not deletion.

## Privacy Posture

Selection explanations are local/operator evidence. They may contain workspace
ids, source lead labels, scheduler policy details, URLs already present in local
operator artifacts, and operator override reasons. They are not public
publication data and should not be exported publicly without an explicit
redaction layer.

## Adding A New Selection Surface

When adding a new runtime selection surface:

1. Emit a `selection-explanation.v1` object or write equivalent rows through the
   cycle evidence ledger.
2. Include selected, considered, and excluded candidates.
3. Preserve existing scoring and policy behavior; do not invent missing scores.
4. Record operator overrides explicitly.
5. Add tests that fail if selected candidates are not considered or exclusions
   lack reasons.
