-- Rebuild operational ledger parents with checked lifecycle statuses.
-- The migration runner validates all foreign keys before committing.
CREATE TABLE cycle_event_checked (
  cycle_event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  workspace_id TEXT,
  workspace_ref TEXT,
  subject_key TEXT,
  domain_pack_id TEXT,
  cycle_depth INTEGER,
  previous_run_ids_json TEXT NOT NULL DEFAULT '[]',
  mode TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL CHECK (status IN (
    'planned', 'running', 'completed', 'dry_run', 'failed', 'partial', 'degraded'
  )),
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

CREATE TABLE cycle_stage_event_checked (
  stage_event_id TEXT PRIMARY KEY,
  cycle_event_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  stage_name TEXT NOT NULL,
  stage_order INTEGER NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  status TEXT NOT NULL CHECK (status IN (
    'planned', 'running', 'completed', 'passed', 'failed', 'partial',
    'dry_run', 'degraded', 'spooled', 'skipped', 'not_reached', 'warning', 'recorded'
  )),
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

INSERT INTO cycle_event_checked SELECT * FROM cycle_event;
INSERT INTO cycle_stage_event_checked SELECT * FROM cycle_stage_event;
DROP TABLE cycle_stage_event;
DROP TABLE cycle_event;
ALTER TABLE cycle_event_checked RENAME TO cycle_event;
ALTER TABLE cycle_stage_event_checked RENAME TO cycle_stage_event;

CREATE INDEX ix_cycle_event_subject
  ON cycle_event(subject_key, started_at);
CREATE INDEX ix_cycle_event_run
  ON cycle_event(run_id, status);
CREATE INDEX ix_cycle_stage_event_cycle
  ON cycle_stage_event(cycle_event_id, stage_order, stage_name);
