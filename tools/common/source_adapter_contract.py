"""Shared source-adapter manifest contract constants."""

from __future__ import annotations

from tools.source_db_tools import rights_retention


SCHEMA_VERSION = "source-adapter.v1"

LOCAL_INPUT_FAMILIES = {"local_file", "local_directory", "local_git_repo"}
REMOTE_INPUT_FAMILIES = {"remote_git_repo", "remote_url_manifest", "remote_archive_collection"}
INPUT_FAMILIES = LOCAL_INPUT_FAMILIES | REMOTE_INPUT_FAMILIES

INPUT_FAMILY_LOCATOR_KEYS = {
    "local_file": "local_path",
    "local_directory": "local_path",
    "local_git_repo": "local_path",
    "remote_git_repo": "repo_url",
    "remote_url_manifest": "manifest_url",
    "remote_archive_collection": "base_url",
}
LOCATOR_GLOB_KEYS = {"include_globs", "exclude_globs"}
STRUCTURED_DATA_FORMATS = {"csv", "json", "jsonl", "xml"}

AUTOMATION_POSTURES = {"operator_review_required", "unattended_safe"}
RIGHTS_POSTURES = rights_retention.rights_postures()
PAYLOAD_STORAGE_POLICY_CLASSES = rights_retention.storage_policy_classes("payload")
METADATA_STORAGE_POLICY_CLASSES = rights_retention.storage_policy_classes("metadata")

ALLOWED_PRESERVE_FIELDS = {
    "original_locator",
    "discovery_provenance",
    "rights_posture",
    "byte_retention_status",
    "discard_metadata",
    "refetchability_status",
    "extraction_metadata",
    "durable_source_record",
    "controlled_subjects",
    "authority_records",
    "transform_lineage",
    "source_metadata",
}

REVIEW_RIGHTS_POSTURES = rights_retention.review_required_rights_postures()
PUBLIC_BLOCKING_RIGHTS = rights_retention.public_blocking_rights_postures()
PUBLIC_BLOCKING_STORAGE = rights_retention.public_blocking_storage_classes("payload") | rights_retention.public_blocking_storage_classes("metadata")

EMIT_HANDOFF_STEP_KIND = "emit_handoff"

LOCAL_ADAPTER_INPUT_FAMILIES = {"local_file", "local_directory"}
LOCAL_SOURCE_SPECIFIC_FIELDS = {"relative_path", "source_filename"}
STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS = {
    "relative_path",
    "source_filename",
    "structured_format",
    "record_locator",
    "record_kind",
}
LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS = {"git_ref", "git_commit"}
REMOTE_URL_MANIFEST_SOURCE_SPECIFIC_FIELDS = {"manifest_url"}
HANDOFF_SCHEMA_VERSION = "source-adapter-handoff.v1"
