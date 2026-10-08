CREATE TRIGGER authority_merge_event_unique_pair_insert
BEFORE INSERT ON authority_merge_event
WHEN EXISTS (
  SELECT 1 FROM authority_merge_event
  WHERE from_authority_record_id = NEW.from_authority_record_id
    AND into_authority_record_id = NEW.into_authority_record_id
)
BEGIN
  SELECT RAISE(ABORT, 'authority merge pair already recorded');
END;

CREATE TRIGGER authority_merge_event_unique_pair_update
BEFORE UPDATE OF from_authority_record_id, into_authority_record_id ON authority_merge_event
WHEN EXISTS (
  SELECT 1 FROM authority_merge_event
  WHERE from_authority_record_id = NEW.from_authority_record_id
    AND into_authority_record_id = NEW.into_authority_record_id
    AND authority_merge_event_id <> OLD.authority_merge_event_id
)
BEGIN
  SELECT RAISE(ABORT, 'authority merge pair already recorded');
END;
