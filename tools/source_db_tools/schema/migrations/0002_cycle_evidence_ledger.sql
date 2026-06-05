-- Cycle evidence ledger schema v2.
-- Operational evidence only: these tables do not replace canonical facts,
-- provenance_event rows, review decisions, or topic-cycle manifests.

CREATE TABLE cycle_event (
  cycle_event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT,
  workspace_ref TEXT,
  subject_key TEXT,
  domain_pack_id TEXT,
  cycle_depth INTEGER,
  previous_run_ids_json TEXT NOT NULL DEFAULT '[]',
  mode TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  topic_cycle_manifest_path TEXT,
  topic_cycle_manifest_hash TEXT,
  canonical_db_ref TEXT,
  final_feedback_plan_ref TEXT,
  row_count_delta_json TEXT,
  warning_count INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  record_last_updated TEXT NOT NULL
);

CREATE TABLE cycle_stage_event (
  stage_event_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  stage_order INTEGER NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  status TEXT NOT NULL,
  required_stage INTEGER NOT NULL DEFAULT 1,
  skipped_reason TEXT,
  command_name TEXT,
  helper_name TEXT,
  input_artifact_ref_id TEXT,
  output_artifact_ref_id TEXT,
  validation_status TEXT,
  error_summary TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  UNIQUE(cycle_event_id, stage_order, stage_name),
  FOREIGN KEY(cycle_event_id) REFERENCES cycle_event(cycle_event_id),
  FOREIGN KEY(input_artifact_ref_id) REFERENCES cycle_artifact_ref(artifact_ref_id),
  FOREIGN KEY(output_artifact_ref_id) REFERENCES cycle_artifact_ref(artifact_ref_id)
);

CREATE TABLE cycle_artifact_ref (
  artifact_ref_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  stage_event_id TEXT,
  artifact_type TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  artifact_hash TEXT,
  byte_count INTEGER,
  privacy_classification TEXT NOT NULL DEFAULT 'local_operator',
  public_safe INTEGER NOT NULL DEFAULT 0,
  schema_id TEXT,
  validation_status TEXT,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  record_last_updated TEXT NOT NULL,
  UNIQUE(cycle_event_id, stage_event_id, artifact_type, artifact_path),
  FOREIGN KEY(cycle_event_id) REFERENCES cycle_event(cycle_event_id),
  FOREIGN KEY(stage_event_id) REFERENCES cycle_stage_event(stage_event_id)
);

CREATE TABLE cycle_candidate_considered (
  candidate_considered_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  stage_event_id TEXT,
  candidate_kind TEXT NOT NULL,
  candidate_ref_type TEXT,
  candidate_ref_id TEXT,
  candidate_label TEXT,
  score REAL,
  score_policy_id TEXT,
  rationale TEXT,
  reason_json TEXT NOT NULL DEFAULT '{}',
  selected INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  UNIQUE(cycle_event_id, stage_event_id, candidate_kind, candidate_ref_type, candidate_ref_id),
  FOREIGN KEY(cycle_event_id) REFERENCES cycle_event(cycle_event_id),
  FOREIGN KEY(stage_event_id) REFERENCES cycle_stage_event(stage_event_id)
);

CREATE TABLE cycle_candidate_excluded (
  candidate_excluded_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  stage_event_id TEXT,
  candidate_kind TEXT NOT NULL,
  candidate_ref_type TEXT,
  candidate_ref_id TEXT,
  candidate_label TEXT,
  exclusion_reason TEXT NOT NULL,
  policy_id TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  UNIQUE(cycle_event_id, stage_event_id, candidate_kind, candidate_ref_type, candidate_ref_id, exclusion_reason),
  FOREIGN KEY(cycle_event_id) REFERENCES cycle_event(cycle_event_id),
  FOREIGN KEY(stage_event_id) REFERENCES cycle_stage_event(stage_event_id)
);

CREATE TABLE cycle_tool_failure (
  tool_failure_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  stage_event_id TEXT,
  tool_name TEXT NOT NULL,
  command_name TEXT,
  exit_code INTEGER,
  failure_kind TEXT NOT NULL,
  error_summary TEXT NOT NULL,
  artifact_ref_id TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  FOREIGN KEY(cycle_event_id) REFERENCES cycle_event(cycle_event_id),
  FOREIGN KEY(stage_event_id) REFERENCES cycle_stage_event(stage_event_id),
  FOREIGN KEY(artifact_ref_id) REFERENCES cycle_artifact_ref(artifact_ref_id)
);

CREATE TABLE cycle_operator_override (
  operator_override_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  stage_event_id TEXT,
  override_kind TEXT NOT NULL,
  override_value TEXT,
  reason TEXT,
  actor TEXT,
  created_at TEXT NOT NULL,
  record_last_updated TEXT NOT NULL,
  UNIQUE(cycle_event_id, stage_event_id, override_kind, override_value),
  FOREIGN KEY(cycle_event_id) REFERENCES cycle_event(cycle_event_id),
  FOREIGN KEY(stage_event_id) REFERENCES cycle_stage_event(stage_event_id)
);

CREATE INDEX ix_cycle_event_subject
  ON cycle_event(subject_key, started_at);
CREATE INDEX ix_cycle_event_run
  ON cycle_event(run_id, status);

CREATE INDEX ix_cycle_stage_event_cycle
  ON cycle_stage_event(cycle_event_id, stage_order, stage_name);

CREATE INDEX ix_cycle_artifact_ref_cycle
  ON cycle_artifact_ref(cycle_event_id, artifact_type, validation_status);

CREATE INDEX ix_cycle_candidate_considered_cycle
  ON cycle_candidate_considered(cycle_event_id, candidate_kind, selected);

CREATE INDEX ix_cycle_candidate_excluded_cycle
  ON cycle_candidate_excluded(cycle_event_id, candidate_kind, retryable);

CREATE INDEX ix_cycle_tool_failure_cycle
  ON cycle_tool_failure(cycle_event_id, failure_kind, retryable);

CREATE INDEX ix_cycle_operator_override_cycle
  ON cycle_operator_override(cycle_event_id, override_kind);
