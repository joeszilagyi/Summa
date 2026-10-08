-- Immutable full-row revisions for stable-ID canonical projections.
-- Each row starts with one baseline/insert revision. A changed projection row
-- appends a complete snapshot linked to the immediately preceding revision.
CREATE TABLE canonical_row_revision (
  revision_id INTEGER PRIMARY KEY,
  table_name TEXT NOT NULL CHECK (table_name IN ('provenance_event', 'work', 'source_access', 'source_claim', 'capture_event', 'extraction_record', 'extraction_detected_entity', 'source_relationship')),
  row_id INTEGER NOT NULL,
  change_kind TEXT NOT NULL CHECK (change_kind IN ('baseline', 'insert', 'supersede')),
  supersedes_revision_id INTEGER REFERENCES canonical_row_revision(revision_id),
  snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'),
  recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  CHECK ((change_kind = 'supersede') = (supersedes_revision_id IS NOT NULL))
);
CREATE INDEX ix_canonical_row_revision_target
  ON canonical_row_revision(table_name, row_id, revision_id);
CREATE UNIQUE INDEX ux_canonical_row_revision_predecessor
  ON canonical_row_revision(supersedes_revision_id)
  WHERE supersedes_revision_id IS NOT NULL;
CREATE TRIGGER canonical_row_revision_no_update
BEFORE UPDATE ON canonical_row_revision
BEGIN
  SELECT RAISE(ABORT, 'canonical row revisions are append-only');
END;
CREATE TRIGGER canonical_row_revision_no_delete
BEFORE DELETE ON canonical_row_revision
BEGIN
  SELECT RAISE(ABORT, 'canonical row revisions are append-only');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'provenance_event', "provenance_event_id", 'baseline', json_object(
    'provenance_event_id', "provenance_event_id",
    'provenance_event_key_v1', "provenance_event_key_v1",
    'object_namespace', "object_namespace",
    'object_id', "object_id",
    'event_type', "event_type",
    'actor_type', "actor_type",
    'actor_id', "actor_id",
    'actor_label', "actor_label",
    'tool_name', "tool_name",
    'tool_version', "tool_version",
    'model_name', "model_name",
    'prompt_id', "prompt_id",
    'run_id', "run_id",
    'source_object_namespace', "source_object_namespace",
    'source_object_id', "source_object_id",
    'event_timestamp', "event_timestamp",
    'confidence_score', "confidence_score",
    'note_text', "note_text",
    'record_last_updated', "record_last_updated"
  ) FROM "provenance_event" ORDER BY "provenance_event_id";
CREATE TRIGGER canonical_row_revision_provenance_event_insert
AFTER INSERT ON "provenance_event"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('provenance_event', NEW."provenance_event_id", 'insert', json_object(
    'provenance_event_id', NEW."provenance_event_id",
    'provenance_event_key_v1', NEW."provenance_event_key_v1",
    'object_namespace', NEW."object_namespace",
    'object_id', NEW."object_id",
    'event_type', NEW."event_type",
    'actor_type', NEW."actor_type",
    'actor_id', NEW."actor_id",
    'actor_label', NEW."actor_label",
    'tool_name', NEW."tool_name",
    'tool_version', NEW."tool_version",
    'model_name', NEW."model_name",
    'prompt_id', NEW."prompt_id",
    'run_id', NEW."run_id",
    'source_object_namespace', NEW."source_object_namespace",
    'source_object_id', NEW."source_object_id",
    'event_timestamp', NEW."event_timestamp",
    'confidence_score', NEW."confidence_score",
    'note_text', NEW."note_text",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_provenance_event_immutable
BEFORE UPDATE ON "provenance_event"
BEGIN
  SELECT RAISE(ABORT, 'provenance events are append-only');
END;
CREATE TRIGGER canonical_row_revision_provenance_event_no_delete
BEFORE DELETE ON "provenance_event"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'work', "work_id", 'baseline', json_object(
    'work_id', "work_id",
    'work_key_v1', "work_key_v1",
    'work_type', "work_type",
    'title', "title",
    'rights_posture', "rights_posture",
    'refetchability_status', "refetchability_status",
    'review_state', "review_state",
    'publication_state', "publication_state",
    'confidence_score', "confidence_score",
    'raw_cite_text', "raw_cite_text",
    'workspace_id', "workspace_id",
    'authority_level', "authority_level",
    'public_blocker', "public_blocker",
    'accepted_for_citation', "accepted_for_citation",
    'provenance_event_ref', "provenance_event_ref",
    'first_seen_at', "first_seen_at",
    'last_seen_at', "last_seen_at",
    'created_at', "created_at",
    'record_last_updated', "record_last_updated"
  ) FROM "work" ORDER BY "work_id";
CREATE TRIGGER canonical_row_revision_work_insert
AFTER INSERT ON "work"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('work', NEW."work_id", 'insert', json_object(
    'work_id', NEW."work_id",
    'work_key_v1', NEW."work_key_v1",
    'work_type', NEW."work_type",
    'title', NEW."title",
    'rights_posture', NEW."rights_posture",
    'refetchability_status', NEW."refetchability_status",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'confidence_score', NEW."confidence_score",
    'raw_cite_text', NEW."raw_cite_text",
    'workspace_id', NEW."workspace_id",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'accepted_for_citation', NEW."accepted_for_citation",
    'provenance_event_ref', NEW."provenance_event_ref",
    'first_seen_at', NEW."first_seen_at",
    'last_seen_at', NEW."last_seen_at",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_work_update
AFTER UPDATE ON "work"
WHEN json_object(
    'work_id', OLD."work_id",
    'work_key_v1', OLD."work_key_v1",
    'work_type', OLD."work_type",
    'title', OLD."title",
    'rights_posture', OLD."rights_posture",
    'refetchability_status', OLD."refetchability_status",
    'review_state', OLD."review_state",
    'publication_state', OLD."publication_state",
    'confidence_score', OLD."confidence_score",
    'raw_cite_text', OLD."raw_cite_text",
    'workspace_id', OLD."workspace_id",
    'authority_level', OLD."authority_level",
    'public_blocker', OLD."public_blocker",
    'accepted_for_citation', OLD."accepted_for_citation",
    'provenance_event_ref', OLD."provenance_event_ref",
    'first_seen_at', OLD."first_seen_at",
    'last_seen_at', OLD."last_seen_at",
    'created_at', OLD."created_at",
    'record_last_updated', OLD."record_last_updated"
  ) IS NOT json_object(
    'work_id', NEW."work_id",
    'work_key_v1', NEW."work_key_v1",
    'work_type', NEW."work_type",
    'title', NEW."title",
    'rights_posture', NEW."rights_posture",
    'refetchability_status', NEW."refetchability_status",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'confidence_score', NEW."confidence_score",
    'raw_cite_text', NEW."raw_cite_text",
    'workspace_id', NEW."workspace_id",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'accepted_for_citation', NEW."accepted_for_citation",
    'provenance_event_ref', NEW."provenance_event_ref",
    'first_seen_at', NEW."first_seen_at",
    'last_seen_at', NEW."last_seen_at",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('work', NEW."work_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='work' AND row_id=NEW."work_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'work_id', NEW."work_id",
    'work_key_v1', NEW."work_key_v1",
    'work_type', NEW."work_type",
    'title', NEW."title",
    'rights_posture', NEW."rights_posture",
    'refetchability_status', NEW."refetchability_status",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'confidence_score', NEW."confidence_score",
    'raw_cite_text', NEW."raw_cite_text",
    'workspace_id', NEW."workspace_id",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'accepted_for_citation', NEW."accepted_for_citation",
    'provenance_event_ref', NEW."provenance_event_ref",
    'first_seen_at', NEW."first_seen_at",
    'last_seen_at', NEW."last_seen_at",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_work_no_delete
BEFORE DELETE ON "work"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'source_access', "source_access_id", 'baseline', json_object(
    'source_access_id', "source_access_id",
    'work_id', "work_id",
    'source_locus_id', "source_locus_id",
    'source_lead_id', "source_lead_id",
    'original_locator', "original_locator",
    'canonical_url', "canonical_url",
    'access_class', "access_class",
    'refetchability_status', "refetchability_status",
    'rights_posture', "rights_posture",
    'citation_hint', "citation_hint",
    'review_state', "review_state",
    'publication_state', "publication_state",
    'authority_level', "authority_level",
    'public_blocker', "public_blocker",
    'workspace_id', "workspace_id",
    'first_seen_at', "first_seen_at",
    'last_seen_at', "last_seen_at",
    'record_last_updated', "record_last_updated",
    'provenance_event_ref', "provenance_event_ref"
  ) FROM "source_access" ORDER BY "source_access_id";
CREATE TRIGGER canonical_row_revision_source_access_insert
AFTER INSERT ON "source_access"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('source_access', NEW."source_access_id", 'insert', json_object(
    'source_access_id', NEW."source_access_id",
    'work_id', NEW."work_id",
    'source_locus_id', NEW."source_locus_id",
    'source_lead_id', NEW."source_lead_id",
    'original_locator', NEW."original_locator",
    'canonical_url', NEW."canonical_url",
    'access_class', NEW."access_class",
    'refetchability_status', NEW."refetchability_status",
    'rights_posture', NEW."rights_posture",
    'citation_hint', NEW."citation_hint",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'first_seen_at', NEW."first_seen_at",
    'last_seen_at', NEW."last_seen_at",
    'record_last_updated', NEW."record_last_updated",
    'provenance_event_ref', NEW."provenance_event_ref"
  ));
END;
CREATE TRIGGER canonical_row_revision_source_access_update
AFTER UPDATE ON "source_access"
WHEN json_object(
    'source_access_id', OLD."source_access_id",
    'work_id', OLD."work_id",
    'source_locus_id', OLD."source_locus_id",
    'source_lead_id', OLD."source_lead_id",
    'original_locator', OLD."original_locator",
    'canonical_url', OLD."canonical_url",
    'access_class', OLD."access_class",
    'refetchability_status', OLD."refetchability_status",
    'rights_posture', OLD."rights_posture",
    'citation_hint', OLD."citation_hint",
    'review_state', OLD."review_state",
    'publication_state', OLD."publication_state",
    'authority_level', OLD."authority_level",
    'public_blocker', OLD."public_blocker",
    'workspace_id', OLD."workspace_id",
    'first_seen_at', OLD."first_seen_at",
    'last_seen_at', OLD."last_seen_at",
    'record_last_updated', OLD."record_last_updated",
    'provenance_event_ref', OLD."provenance_event_ref"
  ) IS NOT json_object(
    'source_access_id', NEW."source_access_id",
    'work_id', NEW."work_id",
    'source_locus_id', NEW."source_locus_id",
    'source_lead_id', NEW."source_lead_id",
    'original_locator', NEW."original_locator",
    'canonical_url', NEW."canonical_url",
    'access_class', NEW."access_class",
    'refetchability_status', NEW."refetchability_status",
    'rights_posture', NEW."rights_posture",
    'citation_hint', NEW."citation_hint",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'first_seen_at', NEW."first_seen_at",
    'last_seen_at', NEW."last_seen_at",
    'record_last_updated', NEW."record_last_updated",
    'provenance_event_ref', NEW."provenance_event_ref"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('source_access', NEW."source_access_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='source_access' AND row_id=NEW."source_access_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'source_access_id', NEW."source_access_id",
    'work_id', NEW."work_id",
    'source_locus_id', NEW."source_locus_id",
    'source_lead_id', NEW."source_lead_id",
    'original_locator', NEW."original_locator",
    'canonical_url', NEW."canonical_url",
    'access_class', NEW."access_class",
    'refetchability_status', NEW."refetchability_status",
    'rights_posture', NEW."rights_posture",
    'citation_hint', NEW."citation_hint",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'first_seen_at', NEW."first_seen_at",
    'last_seen_at', NEW."last_seen_at",
    'record_last_updated', NEW."record_last_updated",
    'provenance_event_ref', NEW."provenance_event_ref"
  ));
END;
CREATE TRIGGER canonical_row_revision_source_access_no_delete
BEFORE DELETE ON "source_access"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'source_claim', "source_claim_id", 'baseline', json_object(
    'source_claim_id', "source_claim_id",
    'source_claim_key_v1', "source_claim_key_v1",
    'about_object_ref', "about_object_ref",
    'claim_text', "claim_text",
    'public_summary', "public_summary",
    'claim_type', "claim_type",
    'review_state', "review_state",
    'publication_state', "publication_state",
    'authority_level', "authority_level",
    'public_blocker', "public_blocker",
    'workspace_id', "workspace_id",
    'confidence_score', "confidence_score",
    'provenance_event_ref', "provenance_event_ref",
    'evidence_locator_ref', "evidence_locator_ref",
    'capture_event_id', "capture_event_id",
    'extraction_id', "extraction_id",
    'created_at', "created_at",
    'record_last_updated', "record_last_updated",
    'is_open_question', "is_open_question"
  ) FROM "source_claim" ORDER BY "source_claim_id";
CREATE TRIGGER canonical_row_revision_source_claim_insert
AFTER INSERT ON "source_claim"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('source_claim', NEW."source_claim_id", 'insert', json_object(
    'source_claim_id', NEW."source_claim_id",
    'source_claim_key_v1', NEW."source_claim_key_v1",
    'about_object_ref', NEW."about_object_ref",
    'claim_text', NEW."claim_text",
    'public_summary', NEW."public_summary",
    'claim_type', NEW."claim_type",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'evidence_locator_ref', NEW."evidence_locator_ref",
    'capture_event_id', NEW."capture_event_id",
    'extraction_id', NEW."extraction_id",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated",
    'is_open_question', NEW."is_open_question"
  ));
END;
CREATE TRIGGER canonical_row_revision_source_claim_update
AFTER UPDATE ON "source_claim"
WHEN json_object(
    'source_claim_id', OLD."source_claim_id",
    'source_claim_key_v1', OLD."source_claim_key_v1",
    'about_object_ref', OLD."about_object_ref",
    'claim_text', OLD."claim_text",
    'public_summary', OLD."public_summary",
    'claim_type', OLD."claim_type",
    'review_state', OLD."review_state",
    'publication_state', OLD."publication_state",
    'authority_level', OLD."authority_level",
    'public_blocker', OLD."public_blocker",
    'workspace_id', OLD."workspace_id",
    'confidence_score', OLD."confidence_score",
    'provenance_event_ref', OLD."provenance_event_ref",
    'evidence_locator_ref', OLD."evidence_locator_ref",
    'capture_event_id', OLD."capture_event_id",
    'extraction_id', OLD."extraction_id",
    'created_at', OLD."created_at",
    'record_last_updated', OLD."record_last_updated",
    'is_open_question', OLD."is_open_question"
  ) IS NOT json_object(
    'source_claim_id', NEW."source_claim_id",
    'source_claim_key_v1', NEW."source_claim_key_v1",
    'about_object_ref', NEW."about_object_ref",
    'claim_text', NEW."claim_text",
    'public_summary', NEW."public_summary",
    'claim_type', NEW."claim_type",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'evidence_locator_ref', NEW."evidence_locator_ref",
    'capture_event_id', NEW."capture_event_id",
    'extraction_id', NEW."extraction_id",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated",
    'is_open_question', NEW."is_open_question"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('source_claim', NEW."source_claim_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='source_claim' AND row_id=NEW."source_claim_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'source_claim_id', NEW."source_claim_id",
    'source_claim_key_v1', NEW."source_claim_key_v1",
    'about_object_ref', NEW."about_object_ref",
    'claim_text', NEW."claim_text",
    'public_summary', NEW."public_summary",
    'claim_type', NEW."claim_type",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'evidence_locator_ref', NEW."evidence_locator_ref",
    'capture_event_id', NEW."capture_event_id",
    'extraction_id', NEW."extraction_id",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated",
    'is_open_question', NEW."is_open_question"
  ));
END;
CREATE TRIGGER canonical_row_revision_source_claim_no_delete
BEFORE DELETE ON "source_claim"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'capture_event', "capture_event_id", 'baseline', json_object(
    'capture_event_id', "capture_event_id",
    'work_id', "work_id",
    'source_locus_ref', "source_locus_ref",
    'original_locator', "original_locator",
    'captured_at', "captured_at",
    'capture_method', "capture_method",
    'content_hash', "content_hash",
    'byte_count', "byte_count",
    'mime_type', "mime_type",
    'byte_retention_status', "byte_retention_status",
    'full_text_retention_status', "full_text_retention_status",
    'refetchability_status', "refetchability_status",
    'payload_storage_policy_class', "payload_storage_policy_class",
    'quality_warnings_json', "quality_warnings_json",
    'transient_payload_note', "transient_payload_note",
    'review_state', "review_state",
    'workspace_id', "workspace_id",
    'public_blocker', "public_blocker",
    'provenance_event_ref', "provenance_event_ref",
    'record_last_updated', "record_last_updated"
  ) FROM "capture_event" ORDER BY "capture_event_id";
CREATE TRIGGER canonical_row_revision_capture_event_insert
AFTER INSERT ON "capture_event"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('capture_event', NEW."capture_event_id", 'insert', json_object(
    'capture_event_id', NEW."capture_event_id",
    'work_id', NEW."work_id",
    'source_locus_ref', NEW."source_locus_ref",
    'original_locator', NEW."original_locator",
    'captured_at', NEW."captured_at",
    'capture_method', NEW."capture_method",
    'content_hash', NEW."content_hash",
    'byte_count', NEW."byte_count",
    'mime_type', NEW."mime_type",
    'byte_retention_status', NEW."byte_retention_status",
    'full_text_retention_status', NEW."full_text_retention_status",
    'refetchability_status', NEW."refetchability_status",
    'payload_storage_policy_class', NEW."payload_storage_policy_class",
    'quality_warnings_json', NEW."quality_warnings_json",
    'transient_payload_note', NEW."transient_payload_note",
    'review_state', NEW."review_state",
    'workspace_id', NEW."workspace_id",
    'public_blocker', NEW."public_blocker",
    'provenance_event_ref', NEW."provenance_event_ref",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_capture_event_update
AFTER UPDATE ON "capture_event"
WHEN json_object(
    'capture_event_id', OLD."capture_event_id",
    'work_id', OLD."work_id",
    'source_locus_ref', OLD."source_locus_ref",
    'original_locator', OLD."original_locator",
    'captured_at', OLD."captured_at",
    'capture_method', OLD."capture_method",
    'content_hash', OLD."content_hash",
    'byte_count', OLD."byte_count",
    'mime_type', OLD."mime_type",
    'byte_retention_status', OLD."byte_retention_status",
    'full_text_retention_status', OLD."full_text_retention_status",
    'refetchability_status', OLD."refetchability_status",
    'payload_storage_policy_class', OLD."payload_storage_policy_class",
    'quality_warnings_json', OLD."quality_warnings_json",
    'transient_payload_note', OLD."transient_payload_note",
    'review_state', OLD."review_state",
    'workspace_id', OLD."workspace_id",
    'public_blocker', OLD."public_blocker",
    'provenance_event_ref', OLD."provenance_event_ref",
    'record_last_updated', OLD."record_last_updated"
  ) IS NOT json_object(
    'capture_event_id', NEW."capture_event_id",
    'work_id', NEW."work_id",
    'source_locus_ref', NEW."source_locus_ref",
    'original_locator', NEW."original_locator",
    'captured_at', NEW."captured_at",
    'capture_method', NEW."capture_method",
    'content_hash', NEW."content_hash",
    'byte_count', NEW."byte_count",
    'mime_type', NEW."mime_type",
    'byte_retention_status', NEW."byte_retention_status",
    'full_text_retention_status', NEW."full_text_retention_status",
    'refetchability_status', NEW."refetchability_status",
    'payload_storage_policy_class', NEW."payload_storage_policy_class",
    'quality_warnings_json', NEW."quality_warnings_json",
    'transient_payload_note', NEW."transient_payload_note",
    'review_state', NEW."review_state",
    'workspace_id', NEW."workspace_id",
    'public_blocker', NEW."public_blocker",
    'provenance_event_ref', NEW."provenance_event_ref",
    'record_last_updated', NEW."record_last_updated"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('capture_event', NEW."capture_event_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='capture_event' AND row_id=NEW."capture_event_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'capture_event_id', NEW."capture_event_id",
    'work_id', NEW."work_id",
    'source_locus_ref', NEW."source_locus_ref",
    'original_locator', NEW."original_locator",
    'captured_at', NEW."captured_at",
    'capture_method', NEW."capture_method",
    'content_hash', NEW."content_hash",
    'byte_count', NEW."byte_count",
    'mime_type', NEW."mime_type",
    'byte_retention_status', NEW."byte_retention_status",
    'full_text_retention_status', NEW."full_text_retention_status",
    'refetchability_status', NEW."refetchability_status",
    'payload_storage_policy_class', NEW."payload_storage_policy_class",
    'quality_warnings_json', NEW."quality_warnings_json",
    'transient_payload_note', NEW."transient_payload_note",
    'review_state', NEW."review_state",
    'workspace_id', NEW."workspace_id",
    'public_blocker', NEW."public_blocker",
    'provenance_event_ref', NEW."provenance_event_ref",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_capture_event_no_delete
BEFORE DELETE ON "capture_event"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'extraction_record', "extraction_id", 'baseline', json_object(
    'extraction_id', "extraction_id",
    'capture_event_id', "capture_event_id",
    'extractor_name', "extractor_name",
    'extractor_version', "extractor_version",
    'extraction_method', "extraction_method",
    'summary_short', "summary_short",
    'input_hash', "input_hash",
    'output_hash', "output_hash",
    'byte_count_in', "byte_count_in",
    'byte_count_out', "byte_count_out",
    'encoding_handling', "encoding_handling",
    'extraction_status', "extraction_status",
    'bad_utf8_handling', "bad_utf8_handling",
    'truncation_status', "truncation_status",
    'hostile_replay_flags_json', "hostile_replay_flags_json",
    'review_state', "review_state",
    'workspace_id', "workspace_id",
    'public_blocker', "public_blocker",
    'provenance_event_ref', "provenance_event_ref",
    'created_at', "created_at",
    'record_last_updated', "record_last_updated"
  ) FROM "extraction_record" ORDER BY "extraction_id";
CREATE TRIGGER canonical_row_revision_extraction_record_insert
AFTER INSERT ON "extraction_record"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('extraction_record', NEW."extraction_id", 'insert', json_object(
    'extraction_id', NEW."extraction_id",
    'capture_event_id', NEW."capture_event_id",
    'extractor_name', NEW."extractor_name",
    'extractor_version', NEW."extractor_version",
    'extraction_method', NEW."extraction_method",
    'summary_short', NEW."summary_short",
    'input_hash', NEW."input_hash",
    'output_hash', NEW."output_hash",
    'byte_count_in', NEW."byte_count_in",
    'byte_count_out', NEW."byte_count_out",
    'encoding_handling', NEW."encoding_handling",
    'extraction_status', NEW."extraction_status",
    'bad_utf8_handling', NEW."bad_utf8_handling",
    'truncation_status', NEW."truncation_status",
    'hostile_replay_flags_json', NEW."hostile_replay_flags_json",
    'review_state', NEW."review_state",
    'workspace_id', NEW."workspace_id",
    'public_blocker', NEW."public_blocker",
    'provenance_event_ref', NEW."provenance_event_ref",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_extraction_record_update
AFTER UPDATE ON "extraction_record"
WHEN json_object(
    'extraction_id', OLD."extraction_id",
    'capture_event_id', OLD."capture_event_id",
    'extractor_name', OLD."extractor_name",
    'extractor_version', OLD."extractor_version",
    'extraction_method', OLD."extraction_method",
    'summary_short', OLD."summary_short",
    'input_hash', OLD."input_hash",
    'output_hash', OLD."output_hash",
    'byte_count_in', OLD."byte_count_in",
    'byte_count_out', OLD."byte_count_out",
    'encoding_handling', OLD."encoding_handling",
    'extraction_status', OLD."extraction_status",
    'bad_utf8_handling', OLD."bad_utf8_handling",
    'truncation_status', OLD."truncation_status",
    'hostile_replay_flags_json', OLD."hostile_replay_flags_json",
    'review_state', OLD."review_state",
    'workspace_id', OLD."workspace_id",
    'public_blocker', OLD."public_blocker",
    'provenance_event_ref', OLD."provenance_event_ref",
    'created_at', OLD."created_at",
    'record_last_updated', OLD."record_last_updated"
  ) IS NOT json_object(
    'extraction_id', NEW."extraction_id",
    'capture_event_id', NEW."capture_event_id",
    'extractor_name', NEW."extractor_name",
    'extractor_version', NEW."extractor_version",
    'extraction_method', NEW."extraction_method",
    'summary_short', NEW."summary_short",
    'input_hash', NEW."input_hash",
    'output_hash', NEW."output_hash",
    'byte_count_in', NEW."byte_count_in",
    'byte_count_out', NEW."byte_count_out",
    'encoding_handling', NEW."encoding_handling",
    'extraction_status', NEW."extraction_status",
    'bad_utf8_handling', NEW."bad_utf8_handling",
    'truncation_status', NEW."truncation_status",
    'hostile_replay_flags_json', NEW."hostile_replay_flags_json",
    'review_state', NEW."review_state",
    'workspace_id', NEW."workspace_id",
    'public_blocker', NEW."public_blocker",
    'provenance_event_ref', NEW."provenance_event_ref",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('extraction_record', NEW."extraction_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='extraction_record' AND row_id=NEW."extraction_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'extraction_id', NEW."extraction_id",
    'capture_event_id', NEW."capture_event_id",
    'extractor_name', NEW."extractor_name",
    'extractor_version', NEW."extractor_version",
    'extraction_method', NEW."extraction_method",
    'summary_short', NEW."summary_short",
    'input_hash', NEW."input_hash",
    'output_hash', NEW."output_hash",
    'byte_count_in', NEW."byte_count_in",
    'byte_count_out', NEW."byte_count_out",
    'encoding_handling', NEW."encoding_handling",
    'extraction_status', NEW."extraction_status",
    'bad_utf8_handling', NEW."bad_utf8_handling",
    'truncation_status', NEW."truncation_status",
    'hostile_replay_flags_json', NEW."hostile_replay_flags_json",
    'review_state', NEW."review_state",
    'workspace_id', NEW."workspace_id",
    'public_blocker', NEW."public_blocker",
    'provenance_event_ref', NEW."provenance_event_ref",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_extraction_record_no_delete
BEFORE DELETE ON "extraction_record"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'extraction_detected_entity', "detected_entity_id", 'baseline', json_object(
    'detected_entity_id', "detected_entity_id",
    'extraction_id', "extraction_id",
    'capture_event_id', "capture_event_id",
    'entity_label', "entity_label",
    'normalized_label', "normalized_label",
    'entity_type', "entity_type",
    'source_span_start', "source_span_start",
    'source_span_end', "source_span_end",
    'authority_record_id', "authority_record_id",
    'review_state', "review_state",
    'confidence_score', "confidence_score",
    'provenance_event_ref', "provenance_event_ref",
    'record_last_updated', "record_last_updated",
    'workspace_id', "workspace_id"
  ) FROM "extraction_detected_entity" ORDER BY "detected_entity_id";
CREATE TRIGGER canonical_row_revision_extraction_detected_entity_insert
AFTER INSERT ON "extraction_detected_entity"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('extraction_detected_entity', NEW."detected_entity_id", 'insert', json_object(
    'detected_entity_id', NEW."detected_entity_id",
    'extraction_id', NEW."extraction_id",
    'capture_event_id', NEW."capture_event_id",
    'entity_label', NEW."entity_label",
    'normalized_label', NEW."normalized_label",
    'entity_type', NEW."entity_type",
    'source_span_start', NEW."source_span_start",
    'source_span_end', NEW."source_span_end",
    'authority_record_id', NEW."authority_record_id",
    'review_state', NEW."review_state",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'record_last_updated', NEW."record_last_updated",
    'workspace_id', NEW."workspace_id"
  ));
END;
CREATE TRIGGER canonical_row_revision_extraction_detected_entity_update
AFTER UPDATE ON "extraction_detected_entity"
WHEN json_object(
    'detected_entity_id', OLD."detected_entity_id",
    'extraction_id', OLD."extraction_id",
    'capture_event_id', OLD."capture_event_id",
    'entity_label', OLD."entity_label",
    'normalized_label', OLD."normalized_label",
    'entity_type', OLD."entity_type",
    'source_span_start', OLD."source_span_start",
    'source_span_end', OLD."source_span_end",
    'authority_record_id', OLD."authority_record_id",
    'review_state', OLD."review_state",
    'confidence_score', OLD."confidence_score",
    'provenance_event_ref', OLD."provenance_event_ref",
    'record_last_updated', OLD."record_last_updated",
    'workspace_id', OLD."workspace_id"
  ) IS NOT json_object(
    'detected_entity_id', NEW."detected_entity_id",
    'extraction_id', NEW."extraction_id",
    'capture_event_id', NEW."capture_event_id",
    'entity_label', NEW."entity_label",
    'normalized_label', NEW."normalized_label",
    'entity_type', NEW."entity_type",
    'source_span_start', NEW."source_span_start",
    'source_span_end', NEW."source_span_end",
    'authority_record_id', NEW."authority_record_id",
    'review_state', NEW."review_state",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'record_last_updated', NEW."record_last_updated",
    'workspace_id', NEW."workspace_id"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('extraction_detected_entity', NEW."detected_entity_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='extraction_detected_entity' AND row_id=NEW."detected_entity_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'detected_entity_id', NEW."detected_entity_id",
    'extraction_id', NEW."extraction_id",
    'capture_event_id', NEW."capture_event_id",
    'entity_label', NEW."entity_label",
    'normalized_label', NEW."normalized_label",
    'entity_type', NEW."entity_type",
    'source_span_start', NEW."source_span_start",
    'source_span_end', NEW."source_span_end",
    'authority_record_id', NEW."authority_record_id",
    'review_state', NEW."review_state",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'record_last_updated', NEW."record_last_updated",
    'workspace_id', NEW."workspace_id"
  ));
END;
CREATE TRIGGER canonical_row_revision_extraction_detected_entity_no_delete
BEFORE DELETE ON "extraction_detected_entity"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;
-- Seed one baseline for every row predating this migration.
INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
SELECT 'source_relationship', "source_relationship_id", 'baseline', json_object(
    'source_relationship_id', "source_relationship_id",
    'from_object_ref', "from_object_ref",
    'to_object_ref', "to_object_ref",
    'predicate', "predicate",
    'target_label', "target_label",
    'evidence_note', "evidence_note",
    'review_state', "review_state",
    'publication_state', "publication_state",
    'authority_level', "authority_level",
    'public_blocker', "public_blocker",
    'workspace_id', "workspace_id",
    'confidence_score', "confidence_score",
    'provenance_event_ref', "provenance_event_ref",
    'evidence_locator_ref', "evidence_locator_ref",
    'created_at', "created_at",
    'record_last_updated', "record_last_updated"
  ) FROM "source_relationship" ORDER BY "source_relationship_id";
CREATE TRIGGER canonical_row_revision_source_relationship_insert
AFTER INSERT ON "source_relationship"
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
  VALUES ('source_relationship', NEW."source_relationship_id", 'insert', json_object(
    'source_relationship_id', NEW."source_relationship_id",
    'from_object_ref', NEW."from_object_ref",
    'to_object_ref', NEW."to_object_ref",
    'predicate', NEW."predicate",
    'target_label', NEW."target_label",
    'evidence_note', NEW."evidence_note",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'evidence_locator_ref', NEW."evidence_locator_ref",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_source_relationship_update
AFTER UPDATE ON "source_relationship"
WHEN json_object(
    'source_relationship_id', OLD."source_relationship_id",
    'from_object_ref', OLD."from_object_ref",
    'to_object_ref', OLD."to_object_ref",
    'predicate', OLD."predicate",
    'target_label', OLD."target_label",
    'evidence_note', OLD."evidence_note",
    'review_state', OLD."review_state",
    'publication_state', OLD."publication_state",
    'authority_level', OLD."authority_level",
    'public_blocker', OLD."public_blocker",
    'workspace_id', OLD."workspace_id",
    'confidence_score', OLD."confidence_score",
    'provenance_event_ref', OLD."provenance_event_ref",
    'evidence_locator_ref', OLD."evidence_locator_ref",
    'created_at', OLD."created_at",
    'record_last_updated', OLD."record_last_updated"
  ) IS NOT json_object(
    'source_relationship_id', NEW."source_relationship_id",
    'from_object_ref', NEW."from_object_ref",
    'to_object_ref', NEW."to_object_ref",
    'predicate', NEW."predicate",
    'target_label', NEW."target_label",
    'evidence_note', NEW."evidence_note",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'evidence_locator_ref', NEW."evidence_locator_ref",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  )
BEGIN
  INSERT INTO canonical_row_revision (table_name, row_id, change_kind, supersedes_revision_id, snapshot_json)
  VALUES ('source_relationship', NEW."source_relationship_id", 'supersede',
    (SELECT revision_id FROM canonical_row_revision
     WHERE table_name='source_relationship' AND row_id=NEW."source_relationship_id"
     ORDER BY revision_id DESC LIMIT 1),
    json_object(
    'source_relationship_id', NEW."source_relationship_id",
    'from_object_ref', NEW."from_object_ref",
    'to_object_ref', NEW."to_object_ref",
    'predicate', NEW."predicate",
    'target_label', NEW."target_label",
    'evidence_note', NEW."evidence_note",
    'review_state', NEW."review_state",
    'publication_state', NEW."publication_state",
    'authority_level', NEW."authority_level",
    'public_blocker', NEW."public_blocker",
    'workspace_id', NEW."workspace_id",
    'confidence_score', NEW."confidence_score",
    'provenance_event_ref', NEW."provenance_event_ref",
    'evidence_locator_ref', NEW."evidence_locator_ref",
    'created_at', NEW."created_at",
    'record_last_updated', NEW."record_last_updated"
  ));
END;
CREATE TRIGGER canonical_row_revision_source_relationship_no_delete
BEFORE DELETE ON "source_relationship"
BEGIN
  SELECT RAISE(ABORT, 'canonical projection rows cannot be deleted');
END;

-- Every row has one root and a linear chain of superseding snapshots.
CREATE TRIGGER canonical_row_revision_linear_chain
BEFORE INSERT ON canonical_row_revision
WHEN (NEW.change_kind = 'supersede' AND NEW.supersedes_revision_id IS NOT (
        SELECT revision_id FROM canonical_row_revision
        WHERE table_name=NEW.table_name AND row_id=NEW.row_id
        ORDER BY revision_id DESC LIMIT 1
     ))
  OR (NEW.change_kind != 'supersede' AND EXISTS (
        SELECT 1 FROM canonical_row_revision
        WHERE table_name=NEW.table_name AND row_id=NEW.row_id
     ))
BEGIN
  SELECT RAISE(ABORT, 'canonical row revisions must form a linear supersession chain');
END;
