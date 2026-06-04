# Index Apply Review Decision

`tools/scripts/Index_Apply_Review_Decision.sh` is the operator wrapper for
`tools/scripts/apply_review_decision.py`.

Purpose:

- apply explicit reviewer decisions to already-detected review targets
- record provenance and review-state history for every applied decision
- apply authority merge decisions through the canonical curation API
- demote or reject claims and relationships without deleting source rows
- resolve contradiction review targets without erasing the contradiction record

Supported actions:

- `accept_merge`: accepts an `authority_reconciliation:<id>` target, records an
  authority merge event, marks the losing authority as demoted, and repoints
  safe canonical authority references to the winning authority.
- `reject_merge`: rejects an `authority_reconciliation:<id>` target without
  repointing references or creating a merge event.
- `reject_claim`: marks a `source_claim:<id>` target rejected while preserving
  the claim row and its provenance.
- `mark_contradicted`: marks a `source_claim:<id>` or
  `source_relationship:<id>` target as needing review because the contradiction
  remains visible.
- `reject_relationship`: marks a `source_relationship:<id>` target rejected
  while preserving the relationship row.
- `resolve_contradiction`: marks a `source_relationship:<id>` target whose
  predicate is `contradicts` as reviewed while preserving the underlying claims.

Examples:

```bash
tools/scripts/Index_Apply_Review_Decision.sh \
  --db /path/to/canonical.sqlite \
  --target authority_reconciliation:12 \
  --decision accept_merge \
  --reviewer operator \
  --reason "Reviewed same controlled identity"
```

```bash
tools/scripts/Index_Apply_Review_Decision.sh \
  --db /path/to/canonical.sqlite \
  --target source_claim:34 \
  --decision reject_claim \
  --reviewer operator \
  --reason "Chronologically impossible under reviewed source context"
```

Dry-run behavior:

- validates the canonical store, target, action, reviewer, and reason
- reports intended graph mutations and reference repoints
- writes no provenance, review-state history, merge events, or graph changes

Authority merge behavior:

- only applies to authority reconciliation review targets
- refuses incompatible authority types
- refuses ambiguous targets that do not identify a losing authority row
- preserves both winning and losing authority rows
- records an `authority_merge_event`
- sets `merged_into_authority_record_id` on the losing authority when the schema
  supports it
- repoints safe canonical references such as detected-entity and work-subject
  authority links
- does not rewrite provenance, review history, raw extraction records, or source
  text

Preservation rules:

- source claims are never deleted by this command
- authority rows are never deleted by this command
- contradiction relationships remain durable review/audit records
- the command does not decide that the opposite claim is true
- accepted, verified, or canonical truth states are not assigned by default

Idempotence and safety:

- applied decisions run in a transaction
- repeated merge application returns an already-applied result instead of adding
  duplicate merge events
- repeated rejection or contradiction resolution is a no-op when the target is
  already in the requested terminal state
- `--expected-current-state` can be used as an optimistic safety check before
  mutation

Relationship to other tools:

- `build_review_queue_view.py` remains read-only and does not mutate graph state
- reconciliation and contradiction detectors find review targets
- this command applies an explicit reviewer decision to one target
- it does not add detection, triage, remote acquisition, publication, or UI
  behavior
