ALTER TABLE source_access
  ADD COLUMN provenance_event_ref TEXT;

CREATE INDEX ix_source_access_provenance_event_ref
  ON source_access(provenance_event_ref);
