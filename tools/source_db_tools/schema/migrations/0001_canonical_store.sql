-- Canonical store bootstrap schema v1.
-- This migration is forward-only and non-destructive. Do not add DROP TABLE
-- or CREATE TABLE AS SELECT statements here.

CREATE TABLE schema_version (
  schema_namespace TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  current_migration_id TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  applied_by TEXT NOT NULL,
  ddl_hash TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE schema_migration_history (
  migration_id TEXT PRIMARY KEY,
  schema_namespace TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  applied_at TEXT NOT NULL,
  applied_by TEXT NOT NULL,
  ddl_hash TEXT NOT NULL,
  notes TEXT,
  UNIQUE(schema_namespace, schema_version)
);

CREATE TABLE provenance_event (
  provenance_event_id INTEGER PRIMARY KEY,
  provenance_event_key_v1 TEXT NOT NULL UNIQUE,
  object_namespace TEXT NOT NULL,
  object_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor_type TEXT,
  actor_id TEXT,
  actor_label TEXT,
  tool_name TEXT,
  tool_version TEXT,
  model_name TEXT,
  prompt_id TEXT,
  run_id TEXT,
  source_object_namespace TEXT,
  source_object_id TEXT,
  event_timestamp TEXT NOT NULL,
  confidence_score REAL,
  note_text TEXT,
  record_last_updated TEXT NOT NULL
);

CREATE TABLE authority_record (
  authority_record_id INTEGER PRIMARY KEY,
  authority_key_v1 TEXT NOT NULL UNIQUE,
  authority_type TEXT NOT NULL,
  preferred_label TEXT NOT NULL,
  label_norm TEXT,
  sort_label TEXT,
  source_namespace TEXT,
  source_id TEXT,
  reconciliation_status TEXT NOT NULL DEFAULT 'unreviewed',
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  confidence_score REAL,
  authority_level TEXT,
  workspace_id TEXT,
  public_blocker TEXT,
  publication_state TEXT,
  provenance_event_ref TEXT,
  merged_into_authority_record_id INTEGER,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(merged_into_authority_record_id) REFERENCES authority_record(authority_record_id)
);

CREATE TABLE work (
  work_id INTEGER PRIMARY KEY,
  work_key_v1 TEXT NOT NULL UNIQUE,
  work_type TEXT,
  title TEXT,
  rights_posture TEXT,
  refetchability_status TEXT,
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  publication_state TEXT,
  confidence_score REAL,
  raw_cite_text TEXT,
  workspace_id TEXT,
  authority_level TEXT,
  public_blocker TEXT,
  accepted_for_citation INTEGER NOT NULL DEFAULT 0,
  provenance_event_ref TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  created_at TEXT DEFAULT NULL,
  record_last_updated TEXT NOT NULL
);

CREATE TABLE capture_event (
  capture_event_id INTEGER PRIMARY KEY,
  work_id INTEGER,
  source_locus_ref TEXT,
  original_locator TEXT,
  captured_at TEXT NOT NULL,
  capture_method TEXT NOT NULL,
  content_hash TEXT,
  byte_count INTEGER,
  mime_type TEXT,
  byte_retention_status TEXT,
  full_text_retention_status TEXT,
  refetchability_status TEXT,
  payload_storage_policy_class TEXT,
  quality_warnings_json TEXT,
  transient_payload_note TEXT,
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  workspace_id TEXT,
  public_blocker TEXT,
  provenance_event_ref TEXT,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(work_id) REFERENCES work(work_id)
);

CREATE TABLE extraction_record (
  extraction_id INTEGER PRIMARY KEY,
  capture_event_id INTEGER NOT NULL,
  extractor_name TEXT,
  extractor_version TEXT,
  extraction_method TEXT,
  summary_short TEXT,
  input_hash TEXT,
  output_hash TEXT,
  byte_count_in INTEGER,
  byte_count_out INTEGER,
  encoding_handling TEXT,
  extraction_status TEXT NOT NULL,
  bad_utf8_handling TEXT,
  truncation_status TEXT,
  hostile_replay_flags_json TEXT,
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  workspace_id TEXT,
  public_blocker TEXT,
  provenance_event_ref TEXT,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(capture_event_id) REFERENCES capture_event(capture_event_id)
);

CREATE TABLE extraction_detected_entity (
  detected_entity_id INTEGER PRIMARY KEY,
  extraction_id INTEGER,
  capture_event_id INTEGER,
  entity_label TEXT NOT NULL,
  normalized_label TEXT,
  entity_type TEXT,
  source_span_start INTEGER,
  source_span_end INTEGER,
  authority_record_id INTEGER,
  review_state TEXT NOT NULL DEFAULT 'proposed',
  confidence_score REAL,
  provenance_event_ref TEXT,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(extraction_id) REFERENCES extraction_record(extraction_id),
  FOREIGN KEY(capture_event_id) REFERENCES capture_event(capture_event_id),
  FOREIGN KEY(authority_record_id) REFERENCES authority_record(authority_record_id)
);

CREATE TABLE work_subject (
  work_subject_id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL,
  authority_record_id INTEGER,
  subject_object_ref TEXT,
  subject_role TEXT,
  source_note TEXT,
  review_state TEXT NOT NULL DEFAULT 'proposed',
  confidence_score REAL,
  provenance_event_ref TEXT,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(work_id) REFERENCES work(work_id),
  FOREIGN KEY(authority_record_id) REFERENCES authority_record(authority_record_id)
);

CREATE TABLE source_relationship (
  source_relationship_id INTEGER PRIMARY KEY,
  from_object_ref TEXT NOT NULL,
  to_object_ref TEXT,
  predicate TEXT NOT NULL,
  target_label TEXT,
  evidence_note TEXT,
  review_state TEXT NOT NULL DEFAULT 'proposed',
  publication_state TEXT,
  authority_level TEXT,
  public_blocker TEXT,
  workspace_id TEXT,
  confidence_score REAL,
  provenance_event_ref TEXT,
  evidence_locator_ref TEXT,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL
);

CREATE TABLE source_claim (
  source_claim_id INTEGER PRIMARY KEY,
  source_claim_key_v1 TEXT UNIQUE,
  about_object_ref TEXT,
  claim_text TEXT NOT NULL,
  public_summary TEXT,
  claim_type TEXT,
  review_state TEXT NOT NULL DEFAULT 'proposed',
  publication_state TEXT,
  authority_level TEXT,
  public_blocker TEXT,
  workspace_id TEXT,
  confidence_score REAL,
  provenance_event_ref TEXT,
  evidence_locator_ref TEXT,
  capture_event_id INTEGER,
  extraction_id INTEGER,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(capture_event_id) REFERENCES capture_event(capture_event_id),
  FOREIGN KEY(extraction_id) REFERENCES extraction_record(extraction_id)
);

CREATE TABLE review_state_history (
  review_state_history_key_v1 TEXT PRIMARY KEY,
  target_namespace TEXT NOT NULL,
  target_id TEXT NOT NULL,
  previous_state TEXT,
  new_state TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  reason TEXT,
  note TEXT,
  source_namespace TEXT,
  source_id TEXT,
  source_tool TEXT,
  source_run_id TEXT,
  record_last_updated TEXT NOT NULL
);

CREATE TABLE authority_reconciliation (
  authority_reconciliation_id INTEGER PRIMARY KEY,
  reconciliation_key_v1 TEXT NOT NULL UNIQUE,
  target_namespace TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detected_entity_id INTEGER,
  raw_label TEXT NOT NULL,
  entity_type TEXT,
  candidate_label TEXT,
  candidate_authority_record_id INTEGER,
  candidate_authority_id INTEGER,
  external_scheme TEXT,
  external_uri TEXT,
  candidate_scheme TEXT,
  candidate_uri TEXT,
  method TEXT,
  match_method TEXT,
  match_score REAL,
  evidence_context TEXT,
  confidence_score REAL,
  review_state TEXT NOT NULL DEFAULT 'proposed',
  reviewer_note TEXT,
  rejected_candidate_ids_json TEXT,
  accepted_authority_id INTEGER,
  decided_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(detected_entity_id) REFERENCES extraction_detected_entity(detected_entity_id),
  FOREIGN KEY(candidate_authority_record_id) REFERENCES authority_record(authority_record_id),
  FOREIGN KEY(accepted_authority_id) REFERENCES authority_record(authority_record_id)
);

CREATE TABLE topic_extension (
  topic_extension_id INTEGER PRIMARY KEY,
  topic_id TEXT NOT NULL,
  extension_type TEXT NOT NULL,
  summary_short TEXT,
  note_text TEXT,
  review_state TEXT NOT NULL DEFAULT 'proposed',
  publication_state TEXT,
  authority_level TEXT,
  public_blocker TEXT,
  workspace_id TEXT,
  confidence_score REAL,
  provenance_event_ref TEXT,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL
);

CREATE TABLE authority_identifier (
  authority_identifier_id INTEGER PRIMARY KEY,
  authority_record_id INTEGER NOT NULL,
  scheme TEXT NOT NULL,
  value TEXT NOT NULL,
  raw_value TEXT,
  normalized_value TEXT,
  uri TEXT,
  normalized_uri TEXT,
  validity_status TEXT,
  validation_warning TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0,
  confidence_score REAL,
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  last_verified_at TEXT,
  record_last_updated TEXT NOT NULL,
  UNIQUE(scheme, value),
  FOREIGN KEY(authority_record_id) REFERENCES authority_record(authority_record_id)
);

CREATE TABLE work_identifier (
  work_identifier_id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL,
  scheme TEXT NOT NULL,
  value TEXT NOT NULL,
  raw_value TEXT,
  normalized_value TEXT,
  normalized_uri TEXT,
  validity_status TEXT,
  validation_warning TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0,
  confidence_score REAL,
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  record_last_updated TEXT NOT NULL,
  UNIQUE(work_id, scheme, value),
  FOREIGN KEY(work_id) REFERENCES work(work_id)
);

CREATE TABLE source_access (
  source_access_id INTEGER PRIMARY KEY,
  work_id INTEGER,
  source_locus_id TEXT,
  source_lead_id TEXT,
  original_locator TEXT NOT NULL,
  canonical_url TEXT,
  access_class TEXT,
  refetchability_status TEXT,
  rights_posture TEXT,
  citation_hint TEXT,
  review_state TEXT NOT NULL DEFAULT 'needs_review',
  publication_state TEXT,
  authority_level TEXT,
  public_blocker TEXT,
  workspace_id TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  record_last_updated TEXT NOT NULL,
  UNIQUE(work_id, original_locator),
  FOREIGN KEY(work_id) REFERENCES work(work_id)
);

CREATE TABLE work_metadata (
  work_metadata_id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL,
  meta_key TEXT NOT NULL,
  meta_value TEXT NOT NULL,
  meta_type TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  record_last_updated TEXT NOT NULL,
  UNIQUE(work_id, meta_key, meta_value),
  FOREIGN KEY(work_id) REFERENCES work(work_id)
);

CREATE TABLE work_url (
  work_url_id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  url_role TEXT,
  url_status TEXT,
  refetchability_status TEXT,
  preferred_refetch_method TEXT,
  record_last_updated TEXT NOT NULL,
  UNIQUE(work_id, url),
  FOREIGN KEY(work_id) REFERENCES work(work_id)
);

CREATE TABLE authority_merge_event (
  authority_merge_event_id INTEGER PRIMARY KEY,
  from_authority_record_id INTEGER NOT NULL,
  into_authority_record_id INTEGER NOT NULL,
  merge_reason TEXT,
  evidence_note TEXT,
  merged_at TEXT NOT NULL,
  merged_by TEXT,
  review_state TEXT NOT NULL DEFAULT 'reviewed',
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(from_authority_record_id) REFERENCES authority_record(authority_record_id),
  FOREIGN KEY(into_authority_record_id) REFERENCES authority_record(authority_record_id)
);

CREATE INDEX ix_provenance_event_object
  ON provenance_event(object_namespace, object_id, event_timestamp);
CREATE INDEX ix_provenance_event_run
  ON provenance_event(run_id, event_type);

CREATE INDEX ix_authority_record_label
  ON authority_record(preferred_label, authority_type);
CREATE INDEX ix_authority_record_merge
  ON authority_record(merged_into_authority_record_id);

CREATE INDEX ix_work_review
  ON work(review_state, publication_state, workspace_id);
CREATE INDEX ix_work_title
  ON work(title, work_type);

CREATE INDEX ix_capture_event_work
  ON capture_event(work_id, captured_at);
CREATE INDEX ix_capture_event_hash
  ON capture_event(content_hash);

CREATE INDEX ix_extraction_record_capture
  ON extraction_record(capture_event_id, extraction_status);

CREATE INDEX ix_detected_entity_extraction
  ON extraction_detected_entity(extraction_id, entity_type);
CREATE INDEX ix_detected_entity_authority
  ON extraction_detected_entity(authority_record_id, review_state);

CREATE INDEX ix_work_subject_work
  ON work_subject(work_id, authority_record_id);

CREATE INDEX ix_source_relationship_refs
  ON source_relationship(from_object_ref, to_object_ref, predicate);
CREATE INDEX ix_source_relationship_review
  ON source_relationship(review_state, workspace_id);

CREATE INDEX ix_source_claim_about
  ON source_claim(about_object_ref, claim_type);
CREATE INDEX ix_source_claim_review
  ON source_claim(review_state, workspace_id);

CREATE INDEX ix_review_state_history_target
  ON review_state_history(target_namespace, target_id, changed_at);

CREATE INDEX ix_authority_reconciliation_target
  ON authority_reconciliation(target_namespace, target_id, review_state);
CREATE INDEX ix_authority_reconciliation_detected
  ON authority_reconciliation(detected_entity_id, candidate_authority_record_id);

CREATE INDEX ix_topic_extension_topic
  ON topic_extension(topic_id, extension_type, review_state);

CREATE INDEX ix_authority_identifier_record
  ON authority_identifier(authority_record_id, is_primary);

CREATE INDEX ix_work_identifier_work
  ON work_identifier(work_id, is_primary);
CREATE UNIQUE INDEX ux_work_identifier_scheme_value
  ON work_identifier(scheme, value);

CREATE INDEX ix_source_access_work
  ON source_access(work_id, canonical_url);
CREATE INDEX ix_source_access_workspace
  ON source_access(workspace_id, review_state);

CREATE INDEX ix_work_metadata_work
  ON work_metadata(work_id, meta_key);

CREATE INDEX ix_work_url_work
  ON work_url(work_id, url_role);

CREATE INDEX ix_authority_merge_event_from_into
  ON authority_merge_event(from_authority_record_id, into_authority_record_id, merged_at);
