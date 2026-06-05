DROP INDEX IF EXISTS ux_source_access_lead_identity_workspace;
DROP INDEX IF EXISTS ux_source_access_lead_identity_global;

CREATE UNIQUE INDEX ux_source_access_lead_identity_workspace
  ON source_access(workspace_id, source_lead_id, original_locator)
  WHERE source_lead_id IS NOT NULL AND workspace_id IS NOT NULL;

CREATE UNIQUE INDEX ux_source_access_lead_identity_global
  ON source_access(source_lead_id, original_locator)
  WHERE source_lead_id IS NOT NULL AND workspace_id IS NULL;
