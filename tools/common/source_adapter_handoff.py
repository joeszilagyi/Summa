"""Helpers for building and validating local source-adapter handoff records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.common.source_adapter_contract import (
    HANDOFF_SCHEMA_VERSION,
    LOCAL_SOURCE_SPECIFIC_FIELDS,
    LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS,
    REMOTE_URL_MANIFEST_SOURCE_SPECIFIC_FIELDS,
    STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS,
)
from tools.source_db_tools import rights_retention


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def derive_byte_retention_status(payload_storage_policy_class: str | None) -> str:
    return rights_retention.derive_byte_retention_status(payload_storage_policy_class)


def derive_refetchability_status(input_family: str) -> str:
    return rights_retention.derive_refetchability_status(input_family)


def build_preserved_fields(
    adapter_payload: dict[str, Any],
    *,
    source_path: Path,
    relative_path: str,
) -> dict[str, Any]:
    handoff = adapter_payload["normalized_handoff"]
    provenance = adapter_payload["provenance"]
    rights = adapter_payload["rights_and_storage"]
    locator = adapter_payload["locator"]
    content_profile = adapter_payload["content_profile"]

    candidate_values: dict[str, Any] = {
        "original_locator": {
            "adapter_local_path": locator.get("local_path"),
            "resolved_source_path": str(source_path),
            "relative_path": relative_path,
        },
        "discovery_provenance": provenance.get("discovery_provenance"),
        "rights_posture": rights.get("rights_posture"),
        "byte_retention_status": derive_byte_retention_status(rights.get("payload_storage_policy_class")),
        "discard_metadata": {
            "discard_required": False,
            "discard_reason": None,
        },
        "refetchability_status": derive_refetchability_status(adapter_payload["input_family"]),
        "extraction_metadata": {
            "dry_run": True,
            "transform_step_count": len(adapter_payload.get("transform_lineage", [])),
        },
        "durable_source_record": None,
        "controlled_subjects": [],
        "authority_records": [],
        "transform_lineage": adapter_payload.get("transform_lineage", []),
        "source_metadata": {
            "display_name": adapter_payload.get("display_name"),
            "description": adapter_payload.get("description"),
            "content_kinds": content_profile.get("content_kinds", []),
            "hazard_flags": content_profile.get("hazard_flags", []),
        },
    }
    return {
        field: candidate_values[field]
        for field in handoff.get("preserve_fields", [])
        if field in candidate_values
    }


def build_source_specific_fields(
    adapter_payload: dict[str, Any],
    *,
    source_path: Path,
    relative_path: str,
) -> dict[str, Any]:
    candidate_values = {
        "relative_path": relative_path,
        "source_filename": source_path.name,
    }
    return {
        field: candidate_values[field]
        for field in adapter_payload["normalized_handoff"].get("source_specific_fields", [])
        if field in candidate_values
    }


def build_local_handoff_record(
    adapter_payload: dict[str, Any],
    *,
    adapter_path: Path,
    source_path: Path,
    relative_path: str,
    sequence: int,
) -> dict[str, Any]:
    handoff = adapter_payload["normalized_handoff"]
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "record_family": handoff["record_family"],
        "batch_unit": handoff["batch_unit"],
        "adapter_path": str(adapter_path),
        "emitted_at": utc_now(),
        "sequence": sequence,
        "resolved_source_path": str(source_path),
        "relative_path": relative_path,
        "preserved": build_preserved_fields(
            adapter_payload,
            source_path=source_path,
            relative_path=relative_path,
        ),
        "source_specific": build_source_specific_fields(
            adapter_payload,
            source_path=source_path,
            relative_path=relative_path,
        ),
    }


def build_structured_data_handoff_record(
    adapter_payload: dict[str, Any],
    *,
    adapter_path: Path,
    source_path: Path,
    relative_path: str,
    sequence: int,
    structured_format: str,
    record_locator: str,
    record_kind: str,
) -> dict[str, Any]:
    handoff = adapter_payload["normalized_handoff"]
    source_specific_candidates = {
        "relative_path": relative_path,
        "source_filename": source_path.name,
        "structured_format": structured_format,
        "record_locator": record_locator,
        "record_kind": record_kind,
    }
    source_specific = {
        field: source_specific_candidates[field]
        for field in handoff.get("source_specific_fields", [])
        if field in source_specific_candidates
    }
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "record_family": handoff["record_family"],
        "batch_unit": handoff["batch_unit"],
        "adapter_path": str(adapter_path),
        "emitted_at": utc_now(),
        "sequence": sequence,
        "resolved_source_path": str(source_path),
        "relative_path": relative_path,
        "preserved": build_preserved_fields(
            adapter_payload,
            source_path=source_path,
            relative_path=relative_path,
        ),
        "source_specific": source_specific,
    }


def validate_local_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    handoff = adapter_payload["normalized_handoff"]

    if record.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        errors.append(f"schema_version must equal {HANDOFF_SCHEMA_VERSION}")
    for key in ("adapter_id", "workspace_id", "record_family", "batch_unit", "resolved_source_path", "relative_path"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-blank string")
    if record.get("adapter_id") != adapter_payload.get("adapter_id"):
        errors.append("adapter_id must match the source adapter manifest")
    if record.get("workspace_id") != adapter_payload.get("workspace_id"):
        errors.append("workspace_id must match the source adapter manifest")
    if record.get("record_family") != handoff.get("record_family"):
        errors.append("record_family must match normalized_handoff.record_family")
    if record.get("batch_unit") != handoff.get("batch_unit"):
        errors.append("batch_unit must match normalized_handoff.batch_unit")
    if not isinstance(record.get("sequence"), int) or isinstance(record.get("sequence"), bool) or record["sequence"] < 1:
        errors.append("sequence must be an integer >= 1")
    if not isinstance(record.get("preserved"), dict):
        errors.append("preserved must be an object")
    if not isinstance(record.get("source_specific"), dict):
        errors.append("source_specific must be an object")

    if isinstance(record.get("preserved"), dict):
        expected_preserve = set(handoff.get("preserve_fields", []))
        unknown_preserved = sorted(set(record["preserved"]) - expected_preserve)
        if unknown_preserved:
            errors.append(f"preserved contains undeclared field: {unknown_preserved[0]}")

    if isinstance(record.get("source_specific"), dict):
        expected_specific = set(handoff.get("source_specific_fields", []))
        unknown_specific = sorted(set(record["source_specific"]) - expected_specific)
        if unknown_specific:
            errors.append(f"source_specific contains undeclared field: {unknown_specific[0]}")
        unsupported_requested = sorted(expected_specific - LOCAL_SOURCE_SPECIFIC_FIELDS)
        if unsupported_requested:
            errors.append(f"source_specific_fields requests unsupported local field: {unsupported_requested[0]}")

    return errors


def validate_structured_data_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    handoff = adapter_payload["normalized_handoff"]

    if record.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        errors.append(f"schema_version must equal {HANDOFF_SCHEMA_VERSION}")
    for key in ("adapter_id", "workspace_id", "record_family", "batch_unit", "resolved_source_path", "relative_path"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-blank string")
    if record.get("adapter_id") != adapter_payload.get("adapter_id"):
        errors.append("adapter_id must match the source adapter manifest")
    if record.get("workspace_id") != adapter_payload.get("workspace_id"):
        errors.append("workspace_id must match the source adapter manifest")
    if record.get("record_family") != handoff.get("record_family"):
        errors.append("record_family must match normalized_handoff.record_family")
    if record.get("batch_unit") != handoff.get("batch_unit"):
        errors.append("batch_unit must match normalized_handoff.batch_unit")
    if not isinstance(record.get("sequence"), int) or isinstance(record.get("sequence"), bool) or record["sequence"] < 1:
        errors.append("sequence must be an integer >= 1")
    if not isinstance(record.get("preserved"), dict):
        errors.append("preserved must be an object")
    if not isinstance(record.get("source_specific"), dict):
        errors.append("source_specific must be an object")

    if isinstance(record.get("preserved"), dict):
        expected_preserve = set(handoff.get("preserve_fields", []))
        unknown_preserved = sorted(set(record["preserved"]) - expected_preserve)
        if unknown_preserved:
            errors.append(f"preserved contains undeclared field: {unknown_preserved[0]}")

    if isinstance(record.get("source_specific"), dict):
        expected_specific = set(handoff.get("source_specific_fields", []))
        unknown_specific = sorted(set(record["source_specific"]) - expected_specific)
        if unknown_specific:
            errors.append(f"source_specific contains undeclared field: {unknown_specific[0]}")
        unsupported_requested = sorted(expected_specific - STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS)
        if unsupported_requested:
            errors.append(f"source_specific_fields requests unsupported structured-data field: {unsupported_requested[0]}")

    return errors


def build_remote_url_manifest_handoff_record(
    adapter_payload: dict[str, Any],
    *,
    adapter_path: Path,
    manifest_input_path: Path,
    entry: dict[str, Any],
    sequence: int,
    line_number: int,
) -> dict[str, Any]:
    handoff = adapter_payload["normalized_handoff"]
    provenance = adapter_payload["provenance"]
    rights = adapter_payload["rights_and_storage"]
    locator = adapter_payload["locator"]
    content_profile = adapter_payload["content_profile"]

    preserved_candidates: dict[str, Any] = {
        "original_locator": {
            "manifest_url": locator.get("manifest_url"),
            "entry_url": entry.get("url"),
            "manifest_input_path": str(manifest_input_path),
            "line_number": line_number,
        },
        "discovery_provenance": provenance.get("discovery_provenance"),
        "rights_posture": rights.get("rights_posture"),
        "byte_retention_status": derive_byte_retention_status(rights.get("payload_storage_policy_class")),
        "discard_metadata": {
            "discard_required": False,
            "discard_reason": None,
        },
        "refetchability_status": derive_refetchability_status(adapter_payload["input_family"]),
        "extraction_metadata": {
            "dry_run": True,
            "network_access_attempted": False,
            "line_number": line_number,
        },
        "durable_source_record": None,
        "controlled_subjects": [],
        "authority_records": [],
        "transform_lineage": adapter_payload.get("transform_lineage", []),
        "source_metadata": {
            "display_name": adapter_payload.get("display_name"),
            "description": adapter_payload.get("description"),
            "content_kinds": content_profile.get("content_kinds", []),
            "hazard_flags": content_profile.get("hazard_flags", []),
            "entry_title": entry.get("title"),
            "entry_notes": entry.get("notes"),
        },
    }
    preserved = {
        field: preserved_candidates[field]
        for field in handoff.get("preserve_fields", [])
        if field in preserved_candidates
    }
    source_specific_candidates = {
        "manifest_url": locator.get("manifest_url"),
    }
    source_specific = {
        field: source_specific_candidates[field]
        for field in handoff.get("source_specific_fields", [])
        if field in source_specific_candidates
    }
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "record_family": handoff["record_family"],
        "batch_unit": handoff["batch_unit"],
        "adapter_path": str(adapter_path),
        "emitted_at": utc_now(),
        "sequence": sequence,
        "remote_state": "configured_remote",
        "network_access_attempted": False,
        "resolved_source_path": str(manifest_input_path),
        "relative_path": f"line:{line_number}",
        "preserved": preserved,
        "source_specific": source_specific,
    }


def validate_remote_url_manifest_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    handoff = adapter_payload["normalized_handoff"]

    if record.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        errors.append(f"schema_version must equal {HANDOFF_SCHEMA_VERSION}")
    for key in ("adapter_id", "workspace_id", "record_family", "batch_unit", "resolved_source_path", "relative_path"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-blank string")
    if record.get("adapter_id") != adapter_payload.get("adapter_id"):
        errors.append("adapter_id must match the source adapter manifest")
    if record.get("workspace_id") != adapter_payload.get("workspace_id"):
        errors.append("workspace_id must match the source adapter manifest")
    if record.get("record_family") != handoff.get("record_family"):
        errors.append("record_family must match normalized_handoff.record_family")
    if record.get("batch_unit") != handoff.get("batch_unit"):
        errors.append("batch_unit must match normalized_handoff.batch_unit")
    if record.get("remote_state") != "configured_remote":
        errors.append("remote_state must equal configured_remote")
    if record.get("network_access_attempted") is not False:
        errors.append("network_access_attempted must be false")
    if not isinstance(record.get("sequence"), int) or isinstance(record.get("sequence"), bool) or record["sequence"] < 1:
        errors.append("sequence must be an integer >= 1")
    if not isinstance(record.get("preserved"), dict):
        errors.append("preserved must be an object")
    if not isinstance(record.get("source_specific"), dict):
        errors.append("source_specific must be an object")

    if isinstance(record.get("preserved"), dict):
        expected_preserve = set(handoff.get("preserve_fields", []))
        unknown_preserved = sorted(set(record["preserved"]) - expected_preserve)
        if unknown_preserved:
            errors.append(f"preserved contains undeclared field: {unknown_preserved[0]}")

    if isinstance(record.get("source_specific"), dict):
        expected_specific = set(handoff.get("source_specific_fields", []))
        unknown_specific = sorted(set(record["source_specific"]) - expected_specific)
        if unknown_specific:
            errors.append(f"source_specific contains undeclared field: {unknown_specific[0]}")
        unsupported_requested = sorted(expected_specific - REMOTE_URL_MANIFEST_SOURCE_SPECIFIC_FIELDS)
        if unsupported_requested:
            errors.append(
                f"source_specific_fields requests unsupported remote_url_manifest field: {unsupported_requested[0]}"
            )

    return errors


def build_local_git_repo_handoff_record(
    adapter_payload: dict[str, Any],
    *,
    adapter_path: Path,
    repo_path: Path,
    inspected_ref: str,
    resolved_commit: str,
    current_branch: str | None,
    repo_state: str,
    include_globs: list[str],
    exclude_globs: list[str],
    candidate_paths: list[str],
) -> dict[str, Any]:
    handoff = adapter_payload["normalized_handoff"]
    provenance = adapter_payload["provenance"]
    rights = adapter_payload["rights_and_storage"]
    locator = adapter_payload["locator"]
    content_profile = adapter_payload["content_profile"]

    preserved_candidates: dict[str, Any] = {
        "original_locator": {
            "adapter_local_path": locator.get("local_path"),
            "resolved_repo_path": str(repo_path),
            "configured_ref": locator.get("ref"),
            "inspected_ref": inspected_ref,
        },
        "discovery_provenance": provenance.get("discovery_provenance"),
        "rights_posture": rights.get("rights_posture"),
        "byte_retention_status": derive_byte_retention_status(rights.get("payload_storage_policy_class")),
        "discard_metadata": {
            "discard_required": False,
            "discard_reason": None,
        },
        "refetchability_status": derive_refetchability_status(adapter_payload["input_family"]),
        "extraction_metadata": {
            "dry_run": True,
            "network_access_attempted": False,
            "repo_state": repo_state,
            "candidate_path_count": len(candidate_paths),
        },
        "durable_source_record": None,
        "controlled_subjects": [],
        "authority_records": [],
        "transform_lineage": adapter_payload.get("transform_lineage", []),
        "source_metadata": {
            "display_name": adapter_payload.get("display_name"),
            "description": adapter_payload.get("description"),
            "content_kinds": content_profile.get("content_kinds", []),
            "hazard_flags": content_profile.get("hazard_flags", []),
            "include_globs": include_globs,
            "exclude_globs": exclude_globs,
            "current_branch": current_branch,
            "repo_state": repo_state,
            "candidate_paths": candidate_paths,
        },
    }
    preserved = {
        field: preserved_candidates[field]
        for field in handoff.get("preserve_fields", [])
        if field in preserved_candidates
    }
    source_specific_candidates = {
        "git_ref": inspected_ref,
        "git_commit": resolved_commit,
    }
    source_specific = {
        field: source_specific_candidates[field]
        for field in handoff.get("source_specific_fields", [])
        if field in source_specific_candidates
    }
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "record_family": handoff["record_family"],
        "batch_unit": handoff["batch_unit"],
        "adapter_path": str(adapter_path),
        "emitted_at": utc_now(),
        "sequence": 1,
        "remote_state": "local_checkout",
        "network_access_attempted": False,
        "resolved_source_path": str(repo_path),
        "relative_path": ".",
        "preserved": preserved,
        "source_specific": source_specific,
    }


def validate_local_git_repo_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    handoff = adapter_payload["normalized_handoff"]

    if record.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        errors.append(f"schema_version must equal {HANDOFF_SCHEMA_VERSION}")
    for key in ("adapter_id", "workspace_id", "record_family", "batch_unit", "resolved_source_path", "relative_path"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-blank string")
    if record.get("adapter_id") != adapter_payload.get("adapter_id"):
        errors.append("adapter_id must match the source adapter manifest")
    if record.get("workspace_id") != adapter_payload.get("workspace_id"):
        errors.append("workspace_id must match the source adapter manifest")
    if record.get("record_family") != handoff.get("record_family"):
        errors.append("record_family must match normalized_handoff.record_family")
    if record.get("batch_unit") != handoff.get("batch_unit"):
        errors.append("batch_unit must match normalized_handoff.batch_unit")
    if record.get("remote_state") != "local_checkout":
        errors.append("remote_state must equal local_checkout")
    if record.get("network_access_attempted") is not False:
        errors.append("network_access_attempted must be false")
    if record.get("sequence") != 1:
        errors.append("sequence must equal 1 for per_snapshot git handoff records")
    if not isinstance(record.get("preserved"), dict):
        errors.append("preserved must be an object")
    if not isinstance(record.get("source_specific"), dict):
        errors.append("source_specific must be an object")

    if isinstance(record.get("preserved"), dict):
        expected_preserve = set(handoff.get("preserve_fields", []))
        unknown_preserved = sorted(set(record["preserved"]) - expected_preserve)
        if unknown_preserved:
            errors.append(f"preserved contains undeclared field: {unknown_preserved[0]}")

    if isinstance(record.get("source_specific"), dict):
        expected_specific = set(handoff.get("source_specific_fields", []))
        unknown_specific = sorted(set(record["source_specific"]) - expected_specific)
        if unknown_specific:
            errors.append(f"source_specific contains undeclared field: {unknown_specific[0]}")
        unsupported_requested = sorted(expected_specific - LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS)
        if unsupported_requested:
            errors.append(f"source_specific_fields requests unsupported local_git_repo field: {unsupported_requested[0]}")

    return errors
