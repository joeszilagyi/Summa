CREATE TRIGGER authority_record_no_self_merge_insert
BEFORE INSERT ON authority_record
WHEN NEW.merged_into_authority_record_id IS NOT NULL
  AND NEW.merged_into_authority_record_id = NEW.authority_record_id
BEGIN
  SELECT RAISE(ABORT, 'authority record cannot merge into itself');
END;

CREATE TRIGGER authority_record_no_self_merge_update
BEFORE UPDATE ON authority_record
WHEN NEW.merged_into_authority_record_id IS NOT NULL
  AND NEW.merged_into_authority_record_id = NEW.authority_record_id
BEGIN
  SELECT RAISE(ABORT, 'authority record cannot merge into itself');
END;
