-- Existing legacy rows are preserved. Reject new or revised invalid spans at the database boundary.
CREATE TRIGGER extraction_detected_entity_span_insert
BEFORE INSERT ON extraction_detected_entity
WHEN (NEW.source_span_start IS NOT NULL AND
      (typeof(NEW.source_span_start) != 'integer' OR NEW.source_span_start < 0))
  OR (NEW.source_span_end IS NOT NULL AND
      (typeof(NEW.source_span_end) != 'integer' OR NEW.source_span_end < 0))
  OR (NEW.source_span_start IS NOT NULL AND NEW.source_span_end IS NOT NULL AND
      NEW.source_span_start > NEW.source_span_end)
BEGIN
  SELECT RAISE(ABORT, 'invalid detected-entity source span');
END;

CREATE TRIGGER extraction_detected_entity_span_update
BEFORE UPDATE OF source_span_start, source_span_end ON extraction_detected_entity
WHEN (NEW.source_span_start IS NOT NULL AND
      (typeof(NEW.source_span_start) != 'integer' OR NEW.source_span_start < 0))
  OR (NEW.source_span_end IS NOT NULL AND
      (typeof(NEW.source_span_end) != 'integer' OR NEW.source_span_end < 0))
  OR (NEW.source_span_start IS NOT NULL AND NEW.source_span_end IS NOT NULL AND
      NEW.source_span_start > NEW.source_span_end)
BEGIN
  SELECT RAISE(ABORT, 'invalid detected-entity source span');
END;
