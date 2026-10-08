-- Keep every authority-reconciliation evidence state, including the state
-- present before this migration. The reconciliation row remains the current
-- projection; this table is its immutable evidence history.
CREATE TABLE authority_reconciliation_evidence_history (
  history_id INTEGER PRIMARY KEY,
  authority_reconciliation_id INTEGER NOT NULL
    REFERENCES authority_reconciliation(authority_reconciliation_id),
  change_kind TEXT NOT NULL CHECK (change_kind IN ('baseline', 'insert', 'update')),
  reconciliation_key_v1 TEXT NOT NULL,
  detected_entity_id INTEGER,
  candidate_authority_record_id INTEGER,
  raw_label TEXT NOT NULL,
  entity_type TEXT,
  candidate_label TEXT,
  method TEXT,
  match_method TEXT,
  match_score REAL,
  evidence_context TEXT,
  confidence_score REAL,
  review_state TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX ix_authority_reconciliation_evidence_history_row
  ON authority_reconciliation_evidence_history(authority_reconciliation_id, history_id);

INSERT INTO authority_reconciliation_evidence_history (
  authority_reconciliation_id, change_kind, reconciliation_key_v1,
  detected_entity_id, candidate_authority_record_id, raw_label, entity_type,
  candidate_label, method, match_method, match_score, evidence_context,
  confidence_score, review_state, updated_at
)
SELECT authority_reconciliation_id, 'baseline', reconciliation_key_v1,
       detected_entity_id, candidate_authority_record_id, raw_label, entity_type,
       candidate_label, method, match_method, match_score, evidence_context,
       confidence_score, review_state, updated_at
FROM authority_reconciliation ORDER BY authority_reconciliation_id;

CREATE TRIGGER authority_reconciliation_evidence_history_insert
AFTER INSERT ON authority_reconciliation
BEGIN
  INSERT INTO authority_reconciliation_evidence_history (
    authority_reconciliation_id, change_kind, reconciliation_key_v1,
    detected_entity_id, candidate_authority_record_id, raw_label, entity_type,
    candidate_label, method, match_method, match_score, evidence_context,
    confidence_score, review_state, updated_at
  ) VALUES (
    NEW.authority_reconciliation_id, 'insert', NEW.reconciliation_key_v1,
    NEW.detected_entity_id, NEW.candidate_authority_record_id, NEW.raw_label,
    NEW.entity_type, NEW.candidate_label, NEW.method, NEW.match_method,
    NEW.match_score, NEW.evidence_context, NEW.confidence_score,
    NEW.review_state, NEW.updated_at
  );
END;

CREATE TRIGGER authority_reconciliation_evidence_history_update
AFTER UPDATE ON authority_reconciliation
BEGIN
  INSERT INTO authority_reconciliation_evidence_history (
    authority_reconciliation_id, change_kind, reconciliation_key_v1,
    detected_entity_id, candidate_authority_record_id, raw_label, entity_type,
    candidate_label, method, match_method, match_score, evidence_context,
    confidence_score, review_state, updated_at
  ) VALUES (
    NEW.authority_reconciliation_id, 'update', NEW.reconciliation_key_v1,
    NEW.detected_entity_id, NEW.candidate_authority_record_id, NEW.raw_label,
    NEW.entity_type, NEW.candidate_label, NEW.method, NEW.match_method,
    NEW.match_score, NEW.evidence_context, NEW.confidence_score,
    NEW.review_state, NEW.updated_at
  );
END;

CREATE TRIGGER authority_reconciliation_evidence_history_no_update
BEFORE UPDATE ON authority_reconciliation_evidence_history
BEGIN
  SELECT RAISE(ABORT, 'authority reconciliation evidence history is append-only');
END;

CREATE TRIGGER authority_reconciliation_evidence_history_no_delete
BEFORE DELETE ON authority_reconciliation_evidence_history
BEGIN
  SELECT RAISE(ABORT, 'authority reconciliation evidence history is append-only');
END;

CREATE TRIGGER authority_reconciliation_evidence_history_parent_no_delete
BEFORE DELETE ON authority_reconciliation
BEGIN
  SELECT RAISE(ABORT, 'authority reconciliation evidence rows cannot be deleted');
END;
