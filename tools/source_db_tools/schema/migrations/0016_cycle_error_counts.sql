WITH stage_error_counts AS (
  SELECT
    e.cycle_event_id,
    MAX(
      CASE WHEN e.status IN ('failed', 'degraded', 'partial') THEN 1 ELSE 0 END,
      (
        SELECT COUNT(*)
        FROM cycle_stage_event AS s
        WHERE s.cycle_event_id = e.cycle_event_id
          AND (
            s.status IN ('failed', 'degraded', 'spooled', 'partial')
            OR (s.status = 'not_reached' AND (s.required_stage = 1 OR s.stage_name = 'graph_closure_audit'))
            OR s.validation_status IN ('fail', 'failed', 'error', 'invalid')
          )
      )
    ) AS computed_count
  FROM cycle_event AS e
)
UPDATE cycle_event
SET error_count = (
      SELECT computed_count
      FROM stage_error_counts
      WHERE stage_error_counts.cycle_event_id = cycle_event.cycle_event_id
    ),
    record_last_updated = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE error_count < (
  SELECT computed_count
  FROM stage_error_counts
  WHERE stage_error_counts.cycle_event_id = cycle_event.cycle_event_id
);
