-- Preserve existing cycle evidence while allowing more than one attempt per run id.
-- Child tables continue to reference the immutable cycle_event_id primary key.
CREATE TABLE cycle_event_next (
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

INSERT INTO cycle_event_next SELECT * FROM cycle_event;
DROP TABLE cycle_event;
ALTER TABLE cycle_event_next RENAME TO cycle_event;

CREATE INDEX ix_cycle_event_subject
  ON cycle_event(subject_key, started_at);
CREATE INDEX ix_cycle_event_run
  ON cycle_event(run_id, status);
