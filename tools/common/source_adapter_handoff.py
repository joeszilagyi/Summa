"""Helpers for building and validating local source-adapter handoff records."""

from __future__ import annotations

import re
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.common.source_adapter_contract import (
    HANDOFF_SCHEMA_VERSION,
    HANDOFF_ALLOWED_TOP_LEVEL_KEYS,
    HANDOFF_RECORD_VARIANTS,
    HANDOFF_REMOTE_STATES,
    LOCAL_SOURCE_SPECIFIC_FIELDS,
    LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS,
    REMOTE_URL_MANIFEST_SOURCE_SPECIFIC_FIELDS,
    RIGHTS_POSTURES,
    STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS,
    STRUCTURED_DATA_FORMATS,
)
from tools.source_db_tools import rights_retention


GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_http_url(value: Any) -> bool:
    if not _is_nonblank_string(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_timestamp(value: Any) -> bool:
    if not _is_nonblank_string(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def infer_handoff_variant(record: dict[str, Any], adapter_payload: dict[str, Any] | None = None) -> str:
    if isinstance(adapter_payload, dict):
        input_family = adapter_payload.get("input_family")
        if input_family == "local_git_repo":
            return "local_git_repo"
        if input_family == "remote_url_manifest":
            return "remote_url_manifest"
        handoff = adapter_payload.get("normalized_handoff")
        if isinstance(handoff, dict):
            requested_specific = set(handoff.get("source_specific_fields", []))
            if requested_specific & (STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS - LOCAL_SOURCE_SPECIFIC_FIELDS):
                return "structured_data"
        if input_family in {"local_file", "local_directory"}:
            return "local_source"

    source_specific = record.get("source_specific")
    if isinstance(source_specific, dict):
        keys = set(source_specific)
        if keys & (STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS - LOCAL_SOURCE_SPECIFIC_FIELDS):
            return "structured_data"
        if keys & LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS:
            return "local_git_repo"
        if keys & REMOTE_URL_MANIFEST_SOURCE_SPECIFIC_FIELDS:
            return "remote_url_manifest"

    remote_state = record.get("remote_state")
    if remote_state == "local_checkout":
        return "local_git_repo"
    if remote_state == "configured_remote":
        return "remote_url_manifest"
    return "local_source"


def allowed_source_specific_fields_for_variant(variant: str) -> set[str]:
    return set(HANDOFF_RECORD_VARIANTS.get(variant, set()))


def validate_source_adapter_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    variant = infer_handoff_variant(record, adapter_payload)
    record_variant = infer_handoff_variant(record)
    expected_specific = allowed_source_specific_fields_for_variant(variant)
    handoff = adapter_payload.get("normalized_handoff") if isinstance(adapter_payload, dict) else None
    adapter_input_family = adapter_payload.get("input_family") if isinstance(adapter_payload, dict) else None
    adapter_id = adapter_payload.get("adapter_id") if isinstance(adapter_payload, dict) else None
    workspace_id = adapter_payload.get("workspace_id") if isinstance(adapter_payload, dict) else None

    if record.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        errors.append(f"schema_version must equal {HANDOFF_SCHEMA_VERSION}")

    unknown_top_level = sorted(set(record) - HANDOFF_ALLOWED_TOP_LEVEL_KEYS)
    if unknown_top_level:
        errors.append(f"unexpected handoff field: {unknown_top_level[0]}")

    for key in ("adapter_id", "workspace_id", "record_family", "batch_unit", "adapter_path", "resolved_source_path", "relative_path"):
        if not _is_nonblank_string(record.get(key)):
            errors.append(f"{key} must be a non-blank string")
    if not _is_timestamp(record.get("emitted_at")):
        errors.append("emitted_at must be an RFC3339 timestamp")
    if not isinstance(record.get("sequence"), int) or isinstance(record.get("sequence"), bool) or record["sequence"] < 1:
        errors.append("sequence must be an integer >= 1")

    if adapter_id is not None and record.get("adapter_id") != adapter_id:
        errors.append("adapter_id must match the source adapter manifest")
    if workspace_id is not None and record.get("workspace_id") != workspace_id:
        errors.append("workspace_id must match the source adapter manifest")
    if adapter_payload is not None and record_variant != variant:
        errors.append(
            f"handoff record variant {record_variant} does not match adapter-declared variant {variant}"
        )
    if isinstance(handoff, dict):
        if record.get("record_family") != handoff.get("record_family"):
            errors.append("record_family must match normalized_handoff.record_family")
        if record.get("batch_unit") != handoff.get("batch_unit"):
            errors.append("batch_unit must match normalized_handoff.batch_unit")

    preserved = record.get("preserved")
    source_specific = record.get("source_specific")
    if not isinstance(preserved, dict):
        errors.append("preserved must be an object")
    if not isinstance(source_specific, dict):
        errors.append("source_specific must be an object")

    if isinstance(preserved, dict):
        validate_preserved_fields(preserved, variant=variant, errors=errors)
        if isinstance(handoff, dict):
            requested_preserve = set(handoff.get("preserve_fields", []))
            unknown_preserved = sorted(set(preserved) - requested_preserve)
            if unknown_preserved:
                errors.append(f"preserved contains undeclared field: {unknown_preserved[0]}")

    if isinstance(source_specific, dict):
        validate_source_specific_fields(source_specific, variant=variant, errors=errors)
        unknown_specific = sorted(set(source_specific) - expected_specific)
        if unknown_specific:
            errors.append(f"source_specific contains unsupported {variant} field: {unknown_specific[0]}")
        if isinstance(handoff, dict):
            requested_specific = set(handoff.get("source_specific_fields", []))
            missing_requested = sorted(requested_specific - set(source_specific))
            if missing_requested:
                errors.append(f"source_specific is missing required field: {missing_requested[0]}")
            undeclared_requested = sorted(set(source_specific) - requested_specific)
            if undeclared_requested:
                errors.append(f"source_specific contains undeclared field: {undeclared_requested[0]}")
            unsupported_requested = sorted(requested_specific - expected_specific)
            if unsupported_requested:
                errors.append(
                    f"source_specific_fields requests unsupported {variant} field: {unsupported_requested[0]}"
                )

    remote_state = record.get("remote_state")
    if remote_state is not None and remote_state not in HANDOFF_REMOTE_STATES:
        errors.append(f"remote_state must be one of: {', '.join(sorted(HANDOFF_REMOTE_STATES))}")
    network_access_attempted = record.get("network_access_attempted")
    if network_access_attempted is not None and not isinstance(network_access_attempted, bool):
        errors.append("network_access_attempted must be a boolean when present")

    source_identity = record.get("source_identity")
    if source_identity is not None and not isinstance(source_identity, dict):
        errors.append("source_identity must be an object when present")

    if variant in {"local_source", "structured_data"}:
        if remote_state is not None:
            errors.append(f"remote_state is not allowed for {variant} handoff records")
        if network_access_attempted is not None:
            errors.append(f"network_access_attempted is not allowed for {variant} handoff records")
        if source_identity is not None:
            errors.append(f"source_identity is not allowed for {variant} handoff records")
    elif variant == "remote_url_manifest":
        if remote_state != "configured_remote":
            errors.append("remote_state must equal configured_remote")
        if network_access_attempted is not False:
            errors.append("network_access_attempted must be false")
        validate_remote_url_manifest_identity(record, source_identity, errors)
    elif variant == "local_git_repo":
        if remote_state != "local_checkout":
            errors.append("remote_state must equal local_checkout")
        if network_access_attempted is not False:
            errors.append("network_access_attempted must be false")
        if record.get("sequence") != 1:
            errors.append("sequence must equal 1 for per_snapshot git handoff records")
        if source_identity is not None:
            errors.append("source_identity is not allowed for local_git_repo handoff records")

    if adapter_input_family == "local_git_repo" and variant != "local_git_repo":
        errors.append("source_specific fields do not match local_git_repo handoff shape")
    if adapter_input_family == "remote_url_manifest" and variant != "remote_url_manifest":
        errors.append("source_specific fields do not match remote_url_manifest handoff shape")

    return errors


def validate_remote_url_manifest_identity(
    record: dict[str, Any],
    source_identity: dict[str, Any] | None,
    errors: list[str],
) -> None:
    if source_identity is None:
        errors.append("source_identity is required for remote_url_manifest handoff records")
        return

    allowed_fields = {"manifest_url", "manifest_snapshot", "manifest_line", "entry_url"}
    unknown_fields = sorted(set(source_identity) - allowed_fields)
    if unknown_fields:
        errors.append(f"source_identity contains unknown field: {unknown_fields[0]}")

    manifest_url = source_identity.get("manifest_url")
    if not _is_http_url(manifest_url):
        errors.append("source_identity.manifest_url must be an absolute http or https URL")

    entry_url = source_identity.get("entry_url")
    if not _is_http_url(entry_url):
        errors.append("source_identity.entry_url must be an absolute http or https URL")

    manifest_line = source_identity.get("manifest_line")
    if not isinstance(manifest_line, int) or isinstance(manifest_line, bool) or manifest_line < 1:
        errors.append("source_identity.manifest_line must be an integer >= 1")

    manifest_snapshot = source_identity.get("manifest_snapshot")
    if not isinstance(manifest_snapshot, dict):
        errors.append("source_identity.manifest_snapshot must be an object")
    else:
        snapshot_allowed = {"path", "sha256"}
        snapshot_unknown = sorted(set(manifest_snapshot) - snapshot_allowed)
        if snapshot_unknown:
            errors.append(f"source_identity.manifest_snapshot contains unknown field: {snapshot_unknown[0]}")
        snapshot_path = manifest_snapshot.get("path")
        if not _is_nonblank_string(snapshot_path):
            errors.append("source_identity.manifest_snapshot.path must be a non-blank string")
        snapshot_sha256 = manifest_snapshot.get("sha256")
        if not _is_nonblank_string(snapshot_sha256) or len(snapshot_sha256) != 64:
            errors.append("source_identity.manifest_snapshot.sha256 must be a 64-character hex digest")

    original_locator = record.get("preserved", {}).get("original_locator")
    if not isinstance(original_locator, dict):
        return

    expected_locator_fields = {
        "manifest_url": original_locator.get("manifest_url"),
        "entry_url": original_locator.get("entry_url"),
        "manifest_input_path": original_locator.get("manifest_input_path"),
        "line_number": original_locator.get("line_number"),
    }
    for field, value in expected_locator_fields.items():
        if field == "line_number":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append("preserved.original_locator.line_number must be an integer >= 1")
        elif not _is_nonblank_string(value):
            errors.append(f"preserved.original_locator.{field} must be a non-blank string")

    if _is_http_url(manifest_url) and original_locator.get("manifest_url") != manifest_url:
        errors.append("source_identity.manifest_url must match preserved.original_locator.manifest_url")
    if _is_http_url(entry_url) and original_locator.get("entry_url") != entry_url:
        errors.append("source_identity.entry_url must match preserved.original_locator.entry_url")
    snapshot_path = manifest_snapshot.get("path") if isinstance(manifest_snapshot, dict) else None
    if _is_nonblank_string(snapshot_path) and original_locator.get("manifest_input_path") != snapshot_path:
        errors.append("source_identity.manifest_snapshot.path must match preserved.original_locator.manifest_input_path")
    if isinstance(manifest_line, int) and not isinstance(manifest_line, bool) and original_locator.get("line_number") != manifest_line:
        errors.append("source_identity.manifest_line must match preserved.original_locator.line_number")


def validate_preserved_fields(
    preserved: dict[str, Any],
    *,
    variant: str,
    errors: list[str],
) -> None:
    allowed_preserve = {
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
    unknown_preserved = sorted(set(preserved) - allowed_preserve)
    if unknown_preserved:
        errors.append(f"preserved contains unknown field: {unknown_preserved[0]}")
    if "original_locator" in preserved and not isinstance(preserved.get("original_locator"), dict):
        errors.append("preserved.original_locator must be an object")
        return
    if "original_locator" in preserved:
        validate_original_locator_fields(
            original_locator=preserved.get("original_locator"),
            variant=variant,
            errors=errors,
        )
    if "discovery_provenance" in preserved and not _is_nonblank_string(preserved.get("discovery_provenance")):
        errors.append("preserved.discovery_provenance must be a non-blank string")
    if "rights_posture" in preserved:
        rights_posture = preserved.get("rights_posture")
        if not _is_nonblank_string(rights_posture) or rights_posture not in RIGHTS_POSTURES:
            errors.append("preserved.rights_posture must be a known rights posture")
    if "byte_retention_status" in preserved:
        known_statuses = rights_retention.load_policy_registry()["record_policy"]["byte_retention_statuses"]
        if preserved.get("byte_retention_status") not in known_statuses:
            errors.append("preserved.byte_retention_status must be a known byte retention status")
    if "discard_metadata" in preserved:
        discard_metadata = preserved.get("discard_metadata")
        if not isinstance(discard_metadata, dict):
            errors.append("preserved.discard_metadata must be an object")
        else:
            if not isinstance(discard_metadata.get("discard_required"), bool):
                errors.append("preserved.discard_metadata.discard_required must be a boolean")
            discard_reason = discard_metadata.get("discard_reason")
            if discard_reason is not None and not _is_nonblank_string(discard_reason):
                errors.append("preserved.discard_metadata.discard_reason must be null or a non-blank string")
    if "refetchability_status" in preserved:
        known_statuses = rights_retention.load_policy_registry()["record_policy"]["refetchability_statuses"]
        if preserved.get("refetchability_status") not in known_statuses:
            errors.append("preserved.refetchability_status must be a known refetchability status")
    if "extraction_metadata" in preserved and not isinstance(preserved.get("extraction_metadata"), dict):
        errors.append("preserved.extraction_metadata must be an object")
    if "durable_source_record" in preserved:
        durable_source_record = preserved.get("durable_source_record")
        if durable_source_record is not None and not isinstance(durable_source_record, dict):
            errors.append("preserved.durable_source_record must be null or an object")
    if "controlled_subjects" in preserved and not isinstance(preserved.get("controlled_subjects"), list):
        errors.append("preserved.controlled_subjects must be an array")
    if "authority_records" in preserved and not isinstance(preserved.get("authority_records"), list):
        errors.append("preserved.authority_records must be an array")
    if "transform_lineage" in preserved and not isinstance(preserved.get("transform_lineage"), list):
        errors.append("preserved.transform_lineage must be an array")
    if "source_metadata" in preserved and not isinstance(preserved.get("source_metadata"), dict):
        errors.append("preserved.source_metadata must be an object")


def validate_original_locator_fields(
    *,
    original_locator: dict[str, Any] | None,
    variant: str,
    errors: list[str],
) -> None:
    if variant == "remote_url_manifest":
        expected = {"manifest_url", "entry_url", "manifest_input_path", "line_number"}
    elif variant == "local_git_repo":
        expected = {"adapter_local_path", "configured_ref", "inspected_ref", "resolved_repo_path"}
    else:
        expected = {"adapter_local_path", "resolved_source_path", "relative_path"}
    for field in sorted(expected):
        value = original_locator.get(field) if isinstance(original_locator, dict) else None
        if field == "line_number":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append("preserved.original_locator.line_number must be an integer >= 1")
                continue
            if value < 1:
                errors.append("preserved.original_locator.line_number must be an integer >= 1")
            continue
        if not _is_nonblank_string(value):
            errors.append(f"preserved.original_locator.{field} must be a non-blank string")


def validate_source_specific_fields(
    source_specific: dict[str, Any],
    *,
    variant: str,
    errors: list[str],
) -> None:
    for field in sorted(set(source_specific) & {"relative_path", "source_filename", "record_locator", "record_kind", "git_ref"}):
        if not _is_nonblank_string(source_specific.get(field)):
            errors.append(f"source_specific.{field} must be a non-blank string")

    if "structured_format" in source_specific:
        structured_format = source_specific.get("structured_format")
        if not _is_nonblank_string(structured_format) or structured_format not in STRUCTURED_DATA_FORMATS:
            errors.append(
                f"source_specific.structured_format must be one of: {', '.join(sorted(STRUCTURED_DATA_FORMATS))}"
            )
    if "git_commit" in source_specific:
        git_commit = source_specific.get("git_commit")
        if not _is_nonblank_string(git_commit) or not GIT_COMMIT_PATTERN.fullmatch(git_commit):
            errors.append("source_specific.git_commit must be a lowercase hexadecimal commit id")
    if "manifest_url" in source_specific and not _is_http_url(source_specific.get("manifest_url")):
        errors.append("source_specific.manifest_url must be an absolute http or https URL")


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
    return validate_source_adapter_handoff_record(record, adapter_payload)


def validate_structured_data_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
) -> list[str]:
    return validate_source_adapter_handoff_record(record, adapter_payload)


def build_remote_url_manifest_handoff_record(
    adapter_payload: dict[str, Any],
    *,
    adapter_path: Path,
    manifest_input_path: Path,
    entry: dict[str, Any],
    sequence: int,
    line_number: int,
    manifest_url: str | None = None,
) -> dict[str, Any]:
    handoff = adapter_payload["normalized_handoff"]
    provenance = adapter_payload["provenance"]
    rights = adapter_payload["rights_and_storage"]
    locator = adapter_payload["locator"]
    content_profile = adapter_payload["content_profile"]

    preserved_candidates: dict[str, Any] = {
        "original_locator": {
            "manifest_url": manifest_url or locator.get("manifest_url"),
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
    manifest_snapshot_hash = _sha256_file(manifest_input_path)
    source_identity = {
        "manifest_url": manifest_url or locator.get("manifest_url"),
        "manifest_snapshot": {
            "path": str(manifest_input_path),
            "sha256": manifest_snapshot_hash,
        },
        "manifest_line": line_number,
        "entry_url": entry.get("url"),
    }
    source_specific_candidates = {
        "manifest_url": manifest_url or locator.get("manifest_url"),
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
        "source_identity": source_identity,
        "preserved": preserved,
        "source_specific": source_specific,
    }


def validate_remote_url_manifest_handoff_record(
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
) -> list[str]:
    return validate_source_adapter_handoff_record(record, adapter_payload)


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
    return validate_source_adapter_handoff_record(record, adapter_payload)
