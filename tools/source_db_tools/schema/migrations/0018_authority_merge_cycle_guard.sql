CREATE TRIGGER authority_record_no_merge_cycle_insert
BEFORE INSERT ON authority_record
WHEN NEW.merged_into_authority_record_id IS NOT NULL
  AND NEW.merged_into_authority_record_id <> NEW.authority_record_id
BEGIN
  SELECT RAISE(ABORT, 'authority merge cycle is not allowed')
  WHERE EXISTS (
    WITH RECURSIVE ancestors(authority_record_id) AS (
      SELECT NEW.merged_into_authority_record_id
      UNION
      SELECT authority_record.merged_into_authority_record_id
      FROM authority_record
      JOIN ancestors USING (authority_record_id)
      WHERE authority_record.merged_into_authority_record_id IS NOT NULL
    )
    SELECT 1 FROM ancestors WHERE authority_record_id = NEW.authority_record_id
  );
END;

CREATE TRIGGER authority_record_no_merge_cycle_update
BEFORE UPDATE ON authority_record
WHEN NEW.merged_into_authority_record_id IS NOT NULL
  AND NEW.merged_into_authority_record_id <> NEW.authority_record_id
BEGIN
  SELECT RAISE(ABORT, 'authority merge cycle is not allowed')
  WHERE EXISTS (
    WITH RECURSIVE ancestors(authority_record_id) AS (
      SELECT NEW.merged_into_authority_record_id
      UNION
      SELECT authority_record.merged_into_authority_record_id
      FROM authority_record
      JOIN ancestors USING (authority_record_id)
      WHERE authority_record.merged_into_authority_record_id IS NOT NULL
    )
    SELECT 1 FROM ancestors WHERE authority_record_id = NEW.authority_record_id
  );
END;
