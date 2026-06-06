-- Add indexes for canonical reconciliation and relation-graph hot paths.

CREATE INDEX ix_source_access_canonical_url
  ON source_access(canonical_url);

CREATE INDEX ix_source_access_original_locator
  ON source_access(original_locator);

CREATE INDEX ix_source_claim_workspace_type_review
  ON source_claim(about_object_ref, claim_type, workspace_id, review_state);

CREATE INDEX ix_source_relationship_workspace_provenance_predicate
  ON source_relationship(provenance_event_ref, predicate, workspace_id);
