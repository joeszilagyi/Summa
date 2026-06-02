-- Static SQLite schema consumed by tools/pipeline_registry/build_pipeline_registry.py.
-- Documentation: docs/tools/pipeline_registry/README.md
-- Keep logical-key indexes aligned with KEY_COLUMNS in build_pipeline_registry.py.
PRAGMA foreign_keys = ON;

CREATE TABLE registry_meta (
  meta_key TEXT PRIMARY KEY,
  meta_value TEXT NOT NULL
);

CREATE TABLE surface (
  surface_key TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  surface_type TEXT NOT NULL CHECK (
    surface_type IN (
      'shell_script',
      'shell_library',
      'python_script',
      'python_module',
      'prompt',
      'config'
    )
  ),
  lifecycle TEXT NOT NULL CHECK (
    lifecycle IN ('live', 'manual', 'legacy', 'archived', 'experimental')
  ),
  language TEXT NOT NULL,
  entrypoint_kind TEXT NOT NULL CHECK (
    entrypoint_kind IN ('entrypoint', 'helper', 'tool', 'prompt', 'manual_helper', 'contract')
  ),
  description TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE artifact_class (
  artifact_key TEXT PRIMARY KEY,
  format TEXT NOT NULL,
  lineage_role TEXT NOT NULL CHECK (
    lineage_role IN ('dataflow', 'operational', 'environment')
  ),
  path_scope TEXT NOT NULL,
  path_pattern TEXT NOT NULL,
  cardinality TEXT NOT NULL,
  description TEXT NOT NULL
);

CREATE TABLE repo_path_rule (
  rule_key TEXT PRIMARY KEY,
  priority INTEGER NOT NULL CHECK (priority >= 0),
  glob TEXT NOT NULL,
  path_kind TEXT NOT NULL CHECK (
    path_kind IN (
      'surface',
      'config',
      'contract',
      'documentation',
      'source_data',
      'generated_data',
      'database',
      'operational',
      'environment',
      'test',
      'legacy',
      'unknown'
    )
  ),
  artifact_key TEXT,
  description TEXT NOT NULL,
  FOREIGN KEY (artifact_key) REFERENCES artifact_class(artifact_key) ON DELETE SET NULL
);

CREATE TABLE repo_file (
  repo_path TEXT PRIMARY KEY,
  tracking_status TEXT NOT NULL CHECK (tracking_status IN ('current', 'departed')),
  path_kind TEXT NOT NULL CHECK (
    path_kind IN (
      'surface',
      'config',
      'contract',
      'documentation',
      'source_data',
      'generated_data',
      'database',
      'operational',
      'environment',
      'test',
      'legacy',
      'unknown'
    )
  ),
  rule_key TEXT,
  artifact_key TEXT,
  FOREIGN KEY (rule_key) REFERENCES repo_path_rule(rule_key) ON DELETE SET NULL,
  FOREIGN KEY (artifact_key) REFERENCES artifact_class(artifact_key) ON DELETE SET NULL
);

CREATE TABLE surface_option (
  surface_key TEXT NOT NULL,
  option_name TEXT NOT NULL,
  option_kind TEXT NOT NULL CHECK (
    option_kind IN ('positional', 'flag', 'env')
  ),
  value_shape TEXT NOT NULL,
  default_value TEXT,
  required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
  description TEXT NOT NULL,
  effect_summary TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (surface_key, option_name, option_kind),
  FOREIGN KEY (surface_key) REFERENCES surface(surface_key) ON DELETE CASCADE
);

CREATE TABLE surface_option_effect (
  surface_option_effect_id INTEGER PRIMARY KEY,
  surface_key TEXT NOT NULL,
  option_name TEXT NOT NULL,
  option_kind TEXT NOT NULL,
  artifact_key TEXT,
  effect_kind TEXT NOT NULL CHECK (
    effect_kind IN (
      'suppresses_write',
      'changes_provenance',
      'multiplies_outputs',
      'selects_input',
      'redirects_output',
      'narrows_output_scope',
      'overwrites_output',
      'changes_metadata',
      'adds_side_effect'
    )
  ),
  description TEXT NOT NULL,
  FOREIGN KEY (surface_key, option_name, option_kind)
    REFERENCES surface_option(surface_key, option_name, option_kind)
    ON DELETE CASCADE,
  FOREIGN KEY (artifact_key) REFERENCES artifact_class(artifact_key) ON DELETE SET NULL
);

CREATE UNIQUE INDEX ux_surface_option_effect_logical_key
ON surface_option_effect (
  surface_key,
  option_name,
  option_kind,
  effect_kind,
  COALESCE(artifact_key, '')
);

CREATE TABLE surface_io (
  surface_io_id INTEGER PRIMARY KEY,
  surface_key TEXT NOT NULL,
  artifact_key TEXT NOT NULL,
  io_direction TEXT NOT NULL CHECK (
    io_direction IN ('reads', 'writes', 'updates', 'appends', 'requires')
  ),
  path_template TEXT NOT NULL DEFAULT '',
  condition_text TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (surface_key) REFERENCES surface(surface_key) ON DELETE CASCADE,
  FOREIGN KEY (artifact_key) REFERENCES artifact_class(artifact_key) ON DELETE CASCADE
);

CREATE UNIQUE INDEX ux_surface_io_logical_key
ON surface_io (
  surface_key,
  artifact_key,
  io_direction,
  path_template
);

CREATE TABLE surface_dependency (
  surface_dependency_id INTEGER PRIMARY KEY,
  surface_key TEXT NOT NULL,
  dependency_surface_key TEXT NOT NULL,
  dependency_kind TEXT NOT NULL CHECK (
    dependency_kind IN ('loads_helper', 'uses_prompt', 'calls', 'executes', 'reads_artifact_contract')
  ),
  condition_text TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (surface_key) REFERENCES surface(surface_key) ON DELETE CASCADE,
  FOREIGN KEY (dependency_surface_key) REFERENCES surface(surface_key) ON DELETE CASCADE
);

CREATE UNIQUE INDEX ux_surface_dependency_logical_key
ON surface_dependency (
  surface_key,
  dependency_surface_key,
  dependency_kind,
  condition_text
);

CREATE VIEW v_surface_contract_summary AS
SELECT
  s.surface_key,
  s.path,
  s.surface_type,
  s.lifecycle,
  s.entrypoint_kind,
  COUNT(DISTINCT (so.surface_key || '|' || so.option_name || '|' || so.option_kind)) AS option_count,
  COUNT(DISTINCT sio.surface_io_id) AS io_count,
  COUNT(DISTINCT sd.surface_dependency_id) AS dependency_count
FROM surface s
LEFT JOIN surface_option so ON so.surface_key = s.surface_key
LEFT JOIN surface_io sio ON sio.surface_key = s.surface_key
LEFT JOIN surface_dependency sd ON sd.surface_key = s.surface_key
GROUP BY
  s.surface_key,
  s.path,
  s.surface_type,
  s.lifecycle,
  s.entrypoint_kind;

CREATE VIEW v_causal_edges AS
SELECT
  prod.surface_key AS producer_surface_key,
  prod.path AS producer_path,
  art.artifact_key,
  art.path_pattern AS artifact_path_pattern,
  art.description AS artifact_description,
  con.surface_key AS consumer_surface_key,
  con.path AS consumer_path,
  prod_io.io_direction AS producer_direction,
  con_io.io_direction AS consumer_direction,
  prod_io.condition_text AS producer_condition,
  con_io.condition_text AS consumer_condition
FROM surface_io prod_io
JOIN surface prod ON prod.surface_key = prod_io.surface_key
JOIN artifact_class art ON art.artifact_key = prod_io.artifact_key
JOIN surface_io con_io ON con_io.artifact_key = prod_io.artifact_key
JOIN surface con ON con.surface_key = con_io.surface_key
WHERE art.lineage_role = 'dataflow'
  AND prod_io.io_direction IN ('writes', 'updates', 'appends')
  AND con_io.io_direction IN ('reads', 'updates', 'appends', 'requires')
  AND prod.surface_key <> con.surface_key;

CREATE VIEW v_artifact_producers AS
SELECT
  art.artifact_key,
  art.path_pattern AS artifact_path_pattern,
  art.description AS artifact_description,
  s.surface_key,
  s.path AS surface_path,
  s.surface_type,
  s.entrypoint_kind,
  sio.io_direction,
  sio.path_template,
  sio.condition_text,
  sio.notes
FROM surface_io sio
JOIN surface s ON s.surface_key = sio.surface_key
JOIN artifact_class art ON art.artifact_key = sio.artifact_key
WHERE sio.io_direction IN ('writes', 'updates', 'appends');

CREATE VIEW v_artifact_consumers AS
SELECT
  art.artifact_key,
  art.path_pattern AS artifact_path_pattern,
  art.description AS artifact_description,
  s.surface_key,
  s.path AS surface_path,
  s.surface_type,
  s.entrypoint_kind,
  sio.io_direction,
  sio.path_template,
  sio.condition_text,
  sio.notes
FROM surface_io sio
JOIN surface s ON s.surface_key = sio.surface_key
JOIN artifact_class art ON art.artifact_key = sio.artifact_key
WHERE sio.io_direction IN ('reads', 'updates', 'appends', 'requires');

CREATE VIEW v_artifact_blast_radius AS
SELECT
  art.artifact_key,
  art.path_pattern AS artifact_path_pattern,
  art.description AS artifact_description,
  con.surface_key AS impacted_surface_key,
  con.path AS impacted_surface_path,
  con.surface_type AS impacted_surface_type,
  con.entrypoint_kind AS impacted_entrypoint_kind,
  con_io.io_direction AS impacted_direction,
  con_io.condition_text AS impacted_condition,
  con_io.notes AS impacted_notes
FROM surface_io con_io
JOIN surface con ON con.surface_key = con_io.surface_key
JOIN artifact_class art ON art.artifact_key = con_io.artifact_key
WHERE con_io.io_direction IN ('reads', 'updates', 'appends', 'requires');

CREATE VIEW v_prompt_bindings AS
SELECT
  s.surface_key,
  s.path,
  d.dependency_surface_key AS prompt_surface_key,
  p.path AS prompt_path,
  d.condition_text,
  d.notes
FROM surface_dependency d
JOIN surface s ON s.surface_key = d.surface_key
JOIN surface p ON p.surface_key = d.dependency_surface_key
WHERE d.dependency_kind = 'uses_prompt';

CREATE VIEW v_full_lineage_chain AS
WITH RECURSIVE lineage (
  start_surface_key,
  start_surface_path,
  depth,
  producer_surface_key,
  producer_path,
  artifact_key,
  artifact_path_pattern,
  artifact_description,
  consumer_surface_key,
  consumer_path,
  chain_text,
  visited_surface_keys
) AS (
  SELECT
    e.producer_surface_key,
    e.producer_path,
    1,
    e.producer_surface_key,
    e.producer_path,
    e.artifact_key,
    e.artifact_path_pattern,
    e.artifact_description,
    e.consumer_surface_key,
    e.consumer_path,
    e.producer_surface_key || ' --' || e.artifact_key || '--> ' || e.consumer_surface_key,
    '|' || e.producer_surface_key || '|' || e.consumer_surface_key || '|'
  FROM v_causal_edges e

  UNION ALL

  SELECT
    pb.prompt_surface_key,
    pb.prompt_path,
    1,
    e.producer_surface_key,
    e.producer_path,
    e.artifact_key,
    e.artifact_path_pattern,
    e.artifact_description,
    e.consumer_surface_key,
    e.consumer_path,
    pb.prompt_surface_key || ' --uses_prompt--> ' || e.producer_surface_key || ' --' || e.artifact_key || '--> ' || e.consumer_surface_key,
    '|' || pb.prompt_surface_key || '|' || e.producer_surface_key || '|' || e.consumer_surface_key || '|'
  FROM v_prompt_bindings pb
  JOIN v_causal_edges e ON e.producer_surface_key = pb.surface_key

  UNION ALL

  SELECT
    l.start_surface_key,
    l.start_surface_path,
    l.depth + 1,
    e.producer_surface_key,
    e.producer_path,
    e.artifact_key,
    e.artifact_path_pattern,
    e.artifact_description,
    e.consumer_surface_key,
    e.consumer_path,
    l.chain_text || ' --' || e.artifact_key || '--> ' || e.consumer_surface_key,
    l.visited_surface_keys || e.consumer_surface_key || '|'
  FROM lineage l
  JOIN v_causal_edges e ON e.producer_surface_key = l.consumer_surface_key
  WHERE instr(l.visited_surface_keys, '|' || e.consumer_surface_key || '|') = 0
)
SELECT
  start_surface_key,
  start_surface_path,
  depth,
  producer_surface_key,
  producer_path,
  artifact_key,
  artifact_path_pattern,
  artifact_description,
  consumer_surface_key,
  consumer_path,
  chain_text
FROM lineage;

CREATE VIEW v_option_impacts AS
SELECT
  so.surface_key,
  s.path AS surface_path,
  so.option_name,
  so.option_kind,
  so.value_shape,
  so.default_value,
  so.required,
  so.description AS option_description,
  so.effect_summary,
  soe.artifact_key,
  soe.effect_kind,
  soe.description AS effect_description
FROM surface_option so
JOIN surface s ON s.surface_key = so.surface_key
LEFT JOIN surface_option_effect soe
  ON soe.surface_key = so.surface_key
 AND soe.option_name = so.option_name
 AND soe.option_kind = so.option_kind;

CREATE VIEW v_repo_file_touch_map AS
SELECT
  rf.repo_path,
  rf.tracking_status,
  rf.path_kind,
  rf.rule_key,
  rf.artifact_key,
  art.description AS artifact_description,
  s.surface_key,
  s.surface_type,
  s.entrypoint_kind,
  COALESCE(prod.producer_count, 0) AS producer_count,
  COALESCE(con.consumer_count, 0) AS consumer_count
FROM repo_file rf
LEFT JOIN artifact_class art ON art.artifact_key = rf.artifact_key
LEFT JOIN surface s ON s.path = rf.repo_path
LEFT JOIN (
  SELECT artifact_key, COUNT(*) AS producer_count
  FROM surface_io
  WHERE io_direction IN ('writes', 'updates', 'appends')
  GROUP BY artifact_key
) prod ON prod.artifact_key = rf.artifact_key
LEFT JOIN (
  SELECT artifact_key, COUNT(*) AS consumer_count
  FROM surface_io
  WHERE io_direction IN ('reads', 'updates', 'appends', 'requires')
  GROUP BY artifact_key
) con ON con.artifact_key = rf.artifact_key;

CREATE VIEW v_repo_unmapped_files AS
SELECT repo_path, path_kind, tracking_status
FROM repo_file
WHERE tracking_status = 'current'
  AND path_kind = 'unknown';

CREATE VIEW v_repo_departed_files AS
SELECT repo_path, path_kind, rule_key, artifact_key
FROM repo_file
WHERE tracking_status = 'departed';

CREATE VIEW v_repo_touch_summary AS
SELECT
  tracking_status,
  path_kind,
  COALESCE(artifact_key, '<none>') AS artifact_key,
  COALESCE(rule_key, '<none>') AS rule_key,
  COUNT(*) AS file_count
FROM repo_file
GROUP BY tracking_status, path_kind, artifact_key, rule_key
ORDER BY tracking_status, path_kind, artifact_key, rule_key;
