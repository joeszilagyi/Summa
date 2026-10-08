UPDATE cycle_candidate_considered
SET selected = 1,
    record_last_updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE candidate_ref_type = 'gather_candidate'
  AND selected = 0
  AND EXISTS (
    SELECT 1
    FROM cycle_stage_event AS stage
    WHERE stage.stage_event_id = cycle_candidate_considered.stage_event_id
      AND stage.stage_name = 'ingest_candidate_batch'
      AND stage.status IN ('passed', 'completed')
  );
