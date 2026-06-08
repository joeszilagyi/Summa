-- Add the remaining reconciliation hot-path indexes for claim and relation lookups.

CREATE INDEX ix_source_claim_provenance_event_claim_type
  ON source_claim(provenance_event_ref, claim_type);

CREATE INDEX ix_source_relationship_provenance_workspace_predicate
  ON source_relationship(provenance_event_ref, workspace_id, predicate);
