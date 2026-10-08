CREATE TRIGGER authority_reconciliation_candidate_authority_insert
BEFORE INSERT ON authority_reconciliation
WHEN NEW.candidate_authority_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM authority_record
    WHERE authority_record_id = NEW.candidate_authority_id
  )
BEGIN
  SELECT RAISE(ABORT, 'candidate_authority_id does not resolve');
END;

CREATE TRIGGER authority_reconciliation_candidate_authority_update
BEFORE UPDATE OF candidate_authority_id ON authority_reconciliation
WHEN NEW.candidate_authority_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM authority_record
    WHERE authority_record_id = NEW.candidate_authority_id
  )
BEGIN
  SELECT RAISE(ABORT, 'candidate_authority_id does not resolve');
END;

CREATE TRIGGER authority_record_candidate_authority_delete
BEFORE DELETE ON authority_record
WHEN EXISTS (
  SELECT 1 FROM authority_reconciliation
  WHERE candidate_authority_id = OLD.authority_record_id
)
BEGIN
  SELECT RAISE(ABORT, 'authority record is referenced by candidate_authority_id');
END;

CREATE TRIGGER authority_record_candidate_authority_id_update
BEFORE UPDATE OF authority_record_id ON authority_record
WHEN NEW.authority_record_id <> OLD.authority_record_id
  AND EXISTS (
    SELECT 1 FROM authority_reconciliation
    WHERE candidate_authority_id = OLD.authority_record_id
  )
BEGIN
  SELECT RAISE(ABORT, 'authority record is referenced by candidate_authority_id');
END;
