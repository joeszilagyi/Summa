-- Require every source claim to retain an object or source-artifact anchor.
-- Triggers preserve existing legacy rows while preventing new invalid rows.

CREATE TRIGGER source_claim_anchor_required_insert
BEFORE INSERT ON source_claim
FOR EACH ROW
WHEN NULLIF(TRIM(COALESCE(NEW.about_object_ref, '')), '') IS NULL
 AND NEW.capture_event_id IS NULL
 AND NEW.extraction_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'source_claim requires an about_object_ref, capture_event_id, or extraction_id anchor');
END;

CREATE TRIGGER source_claim_anchor_required_update
BEFORE UPDATE OF about_object_ref, capture_event_id, extraction_id ON source_claim
FOR EACH ROW
WHEN NULLIF(TRIM(COALESCE(NEW.about_object_ref, '')), '') IS NULL
 AND NEW.capture_event_id IS NULL
 AND NEW.extraction_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'source_claim requires an about_object_ref, capture_event_id, or extraction_id anchor');
END;
