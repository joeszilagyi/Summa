ALTER TABLE source_claim
  ADD COLUMN is_open_question INTEGER NOT NULL DEFAULT 0;

UPDATE source_claim
SET is_open_question = CASE
  WHEN LOWER(COALESCE(claim_type, '')) LIKE '%question%' THEN 1
  WHEN INSTR(COALESCE(claim_text, ''), '?') > 0 THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM provenance_event
    WHERE provenance_event.provenance_event_key_v1 = source_claim.provenance_event_ref
      AND INSTR(COALESCE(provenance_event.note_text, ''), '"facet": "open_questions"') > 0
  ) THEN 1
  ELSE 0
END;

CREATE INDEX ix_source_claim_workspace_question_review
  ON source_claim(workspace_id, is_open_question, review_state);
