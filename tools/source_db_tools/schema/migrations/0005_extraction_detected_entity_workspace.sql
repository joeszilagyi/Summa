ALTER TABLE extraction_detected_entity
  ADD COLUMN workspace_id TEXT;

UPDATE extraction_detected_entity
SET workspace_id = COALESCE(
  (
    SELECT extraction.workspace_id
    FROM extraction_record extraction
    WHERE extraction.extraction_id = extraction_detected_entity.extraction_id
  ),
  (
    SELECT capture.workspace_id
    FROM capture_event capture
    WHERE capture.capture_event_id = extraction_detected_entity.capture_event_id
  )
)
WHERE workspace_id IS NULL;

CREATE INDEX ix_detected_entity_workspace
  ON extraction_detected_entity(workspace_id, review_state);
