#!/usr/bin/env python3
"""Validate source-adapter JSON manifests against the current contract."""

from __future__ import annotations

import argparse
import json
import ipaddress
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PROJECT_ROOT = REPO_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from tools.common.network_safety_gate import normalized_allowlist_url  # noqa: E402

from tools.common.source_adapter_contract import (  # noqa: E402
    ALLOWED_PRESERVE_FIELDS,
    AUTOMATION_POSTURES,
    EMIT_HANDOFF_STEP_KIND,
    INPUT_FAMILIES,
    INPUT_FAMILY_ALLOWED_LOCATOR_KEYS,
    INPUT_FAMILY_LOCATOR_KEYS,
    METADATA_STORAGE_POLICY_CLASSES,
    PAYLOAD_STORAGE_POLICY_CLASSES,
    REVIEW_RIGHTS_POSTURES,
    RIGHTS_POSTURES,
    SCHEMA_VERSION,
    STRUCTURED_DATA_FORMATS,
)
from tools.source_db_tools import rights_retention  # noqa: E402


VALIDATOR_NAME = "source_adapter"
CONTRACT_VERSION = "1"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
HOSTNAME_PATTERN = re.compile(r"^(?:(?=[a-z0-9])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.)*(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.?$")

REQUIRED_KEYS = {
    "schema_version",
    "adapter_id",
    "display_name",
    "workspace_id",
    "input_family",
    "locator",
    "content_profile",
    "provenance",
    "rights_and_storage",
    "automation_posture",
    "normalized_handoff",
    "transform_lineage",
}
OPTIONAL_KEYS = {"description"}
ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS

CONTENT_PROFILE_REQUIRED_KEYS = {"content_kinds", "hazard_flags"}
PROVENANCE_REQUIRED_KEYS = {"discovery_provenance", "acquisition_method", "source_description"}
PROVENANCE_OPTIONAL_KEYS = {"upstream_reference"}
RIGHTS_REQUIRED_KEYS = {
    "payload_storage_policy_class",
    "metadata_storage_policy_class",
    "rights_posture",
}
RIGHTS_OPTIONAL_KEYS = {"contains_personal_data"}
HANDOFF_REQUIRED_KEYS = {"record_family", "batch_unit", "preserve_fields", "source_specific_fields"}
TRANSFORM_STEP_REQUIRED_KEYS = {"step_id", "step_kind", "description", "deterministic", "review_required"}
TRANSFORM_STEP_ALLOWED_KEYS = set(TRANSFORM_STEP_REQUIRED_KEYS)


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a source-adapter JSON manifest.")
    parser.add_argument("target", help="Path to the source-adapter JSON manifest.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    line: int | None = None,
) -> None:
    errors.append({"code": code, "line": line, "message": message})


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def load_json_object(target: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []

    if not target.exists():
        add_error(errors, code="INPUT_NOT_FOUND", message="input path does not exist")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    if not target.is_file():
        add_error(errors, code="INPUT_NOT_FILE", message="input path is not a file")
        return None, errors, EXIT_INPUT_UNAVAILABLE

    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        add_error(errors, code="INPUT_UNREADABLE", message="input file could not be read")
        return None, errors, EXIT_INPUT_UNAVAILABLE

    try:
        payload = json.loads(
            raw_text,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except DuplicateJsonKeyError as exc:
        add_error(errors, code="DUPLICATE_JSON_KEY", line=1, message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(errors, code="NON_STANDARD_JSON_CONSTANT", line=1, message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    except json.JSONDecodeError as exc:
        add_error(errors, code="JSON_PARSE_ERROR", line=exc.lineno, message="invalid JSON syntax")
        return None, errors, EXIT_VALIDATION_FAILED

    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED

    return payload, errors, EXIT_PASS


def validate_identifier(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        add_error(errors, code=code, message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")


def validate_nonblank_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str = "INVALID_STRING",
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")


def validate_string_array(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    require_nonempty: bool = False,
    item_code: str = "INVALID_ARRAY_ITEM",
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, list):
        add_error(errors, code="FIELD_NOT_ARRAY", message=f"{field} must be an array")
        return
    if require_nonempty and not value:
        add_error(errors, code="EMPTY_ARRAY", message=f"{field} must not be empty")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=item_code, message=f"{field}[{index}] must be a non-blank string")
            return


def validate_enum(
    payload: dict[str, Any],
    field: str,
    allowed_values: set[str],
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or value not in allowed_values:
        add_error(errors, code=code, message=f"{field} must be one of: {', '.join(sorted(allowed_values))}")


def validate_boolean(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, bool):
        add_error(errors, code=code, message=f"{field} must be a boolean")


def is_http_url(value: str) -> bool:
    return normalize_http_url(value) is not None


def _is_valid_http_host(hostname: str) -> bool:
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if any(ch.isspace() for ch in hostname):
        return False
    if any(ord(ch) < 32 or ord(ch) == 0x7F for ch in hostname):
        return False
    if "[" in hostname or "]" in hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return bool(HOSTNAME_PATTERN.fullmatch(hostname))


def normalize_http_url(value: str) -> str | None:
    if any(ch.isspace() for ch in value):
        return None
    if any(ord(ch) < 32 or ord(ch) == 0x7F for ch in value):
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname
    if not isinstance(host, str) or not _is_valid_http_host(host):
        return None
    if parsed.username or parsed.password:
        return None
    return normalized_allowlist_url(value)


def validate_url_field(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or not is_http_url(value):
        add_error(errors, code="INVALID_REMOTE_URL", message=f"{field} must be an absolute http or https URL")


def validate_locator(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    locator = payload.get("locator")
    if not isinstance(locator, dict):
        add_error(errors, code="LOCATOR_NOT_OBJECT", message="locator must be an object")
        return

    input_family = payload.get("input_family")
    if not isinstance(input_family, str):
        return
    allowed_locator_keys = INPUT_FAMILY_ALLOWED_LOCATOR_KEYS.get(input_family)
    if allowed_locator_keys is None:
        return
    for key in sorted(locator):
        if key not in allowed_locator_keys:
            add_error(
                errors,
                code="LOCATOR_FIELD_NOT_ALLOWED",
                message=f"locator field {key} is not allowed for input_family {input_family}",
            )
    required_locator_key = INPUT_FAMILY_LOCATOR_KEYS.get(input_family)
    if required_locator_key is None:
        return
    if not isinstance(locator.get(required_locator_key), str) or not locator.get(required_locator_key, "").strip():
        add_error(errors, code="MISSING_LOCATOR_KEY", message=f"missing required locator key: {required_locator_key}")
        return
    if required_locator_key in {"repo_url", "manifest_url", "base_url"}:
        validate_url_field(locator, required_locator_key, errors)
    for key in ("include_globs", "exclude_globs"):
        if key in locator:
            validate_string_array(locator, key, errors)
    if "ref" in locator:
        validate_nonblank_string(locator, "ref", errors)
    if "format_hint" in locator:
        validate_enum(locator, "format_hint", STRUCTURED_DATA_FORMATS, errors, code="INVALID_FORMAT_HINT")
    if "record_path" in locator:
        validate_nonblank_string(locator, "record_path", errors)


def validate_content_profile(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    content_profile = payload.get("content_profile")
    if not isinstance(content_profile, dict):
        add_error(errors, code="CONTENT_PROFILE_NOT_OBJECT", message="content_profile must be an object")
        return
    unknown_content_profile_keys = sorted(set(content_profile) - CONTENT_PROFILE_REQUIRED_KEYS)
    for key in unknown_content_profile_keys:
        add_error(
            errors,
            code="UNKNOWN_CONTENT_PROFILE_FIELD",
            message=f"unexpected content_profile field: {key}",
        )
    for key in sorted(CONTENT_PROFILE_REQUIRED_KEYS):
        if key not in content_profile:
            add_error(errors, code="MISSING_CONTENT_PROFILE_KEY", message=f"missing required content_profile key: {key}")
    validate_string_array(content_profile, "content_kinds", errors, require_nonempty=True)
    validate_string_array(content_profile, "hazard_flags", errors)


def validate_provenance(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        add_error(errors, code="PROVENANCE_NOT_OBJECT", message="provenance must be an object")
        return
    unknown_provenance_keys = sorted(set(provenance) - (PROVENANCE_REQUIRED_KEYS | PROVENANCE_OPTIONAL_KEYS))
    for key in unknown_provenance_keys:
        add_error(errors, code="UNKNOWN_PROVENANCE_FIELD", message=f"unexpected provenance field: {key}")
    for key in sorted(PROVENANCE_REQUIRED_KEYS):
        if key not in provenance:
            add_error(errors, code="MISSING_PROVENANCE_KEY", message=f"missing required provenance key: {key}")
    for key in sorted(PROVENANCE_REQUIRED_KEYS | PROVENANCE_OPTIONAL_KEYS):
        if key in provenance:
            validate_nonblank_string(provenance, key, errors)


def validate_rights_and_storage(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    rights = payload.get("rights_and_storage")
    if not isinstance(rights, dict):
        add_error(errors, code="RIGHTS_NOT_OBJECT", message="rights_and_storage must be an object")
        return
    unknown_rights_keys = sorted(set(rights) - (RIGHTS_REQUIRED_KEYS | RIGHTS_OPTIONAL_KEYS))
    for key in unknown_rights_keys:
        add_error(errors, code="UNKNOWN_RIGHTS_FIELD", message=f"unexpected rights_and_storage field: {key}")
    for key in sorted(RIGHTS_REQUIRED_KEYS):
        if key not in rights:
            add_error(errors, code="MISSING_RIGHTS_KEY", message=f"missing required rights_and_storage key: {key}")
    validate_enum(
        rights,
        "payload_storage_policy_class",
        PAYLOAD_STORAGE_POLICY_CLASSES,
        errors,
        code="INVALID_PAYLOAD_STORAGE_POLICY_CLASS",
    )
    validate_enum(
        rights,
        "metadata_storage_policy_class",
        METADATA_STORAGE_POLICY_CLASSES,
        errors,
        code="INVALID_METADATA_STORAGE_POLICY_CLASS",
    )
    validate_enum(
        rights,
        "rights_posture",
        RIGHTS_POSTURES,
        errors,
        code="INVALID_RIGHTS_POSTURE",
    )
    if "contains_personal_data" in rights:
        validate_boolean(rights, "contains_personal_data", errors, code="INVALID_CONTAINS_PERSONAL_DATA")

    policy_result = rights_retention.validate_adapter_policy(rights, input_family=payload.get("input_family"))
    for row in policy_result["errors"]:
        add_error(errors, code=row["code"], message=row["message"])


def validate_normalized_handoff(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    handoff = payload.get("normalized_handoff")
    if not isinstance(handoff, dict):
        add_error(errors, code="HANDOFF_NOT_OBJECT", message="normalized_handoff must be an object")
        return
    allowed_handoff_keys = HANDOFF_REQUIRED_KEYS
    for key in sorted(set(handoff) - allowed_handoff_keys):
        add_error(errors, code="UNKNOWN_HANDOFF_FIELD", message=f"unexpected normalized_handoff field: {key}")
    for key in sorted(HANDOFF_REQUIRED_KEYS):
        if key not in handoff:
            add_error(errors, code="MISSING_HANDOFF_KEY", message=f"missing required normalized_handoff key: {key}")
    validate_nonblank_string(handoff, "record_family", errors)
    validate_nonblank_string(handoff, "batch_unit", errors)
    validate_string_array(handoff, "preserve_fields", errors, require_nonempty=True)
    validate_string_array(handoff, "source_specific_fields", errors)

    preserve_fields = handoff.get("preserve_fields")
    if isinstance(preserve_fields, list):
        for field in preserve_fields:
            if isinstance(field, str) and field not in ALLOWED_PRESERVE_FIELDS:
                add_error(
                    errors,
                    code="INVALID_PRESERVE_FIELD",
                    message=f"preserve_fields contains unsupported value: {field}",
                )
                break


def validate_transform_lineage(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    lineage = payload.get("transform_lineage")
    if not isinstance(lineage, list):
        add_error(errors, code="TRANSFORM_LINEAGE_NOT_ARRAY", message="transform_lineage must be an array")
        return
    if not lineage:
        add_error(errors, code="TRANSFORM_LINEAGE_EMPTY", message="transform_lineage must not be empty")
        return

    seen_step_ids: set[str] = set()
    for step in lineage:
        if not isinstance(step, dict):
            add_error(errors, code="TRANSFORM_STEP_NOT_OBJECT", message="transform_lineage entries must be objects")
            return
        unknown_step_keys = sorted(set(step) - TRANSFORM_STEP_ALLOWED_KEYS)
        if unknown_step_keys:
            add_error(
                errors,
                code="UNKNOWN_TRANSFORM_STEP_FIELD",
                message=f"unexpected transform_lineage field: {unknown_step_keys[0]}",
            )
            return
        for key in sorted(TRANSFORM_STEP_REQUIRED_KEYS):
            if key not in step:
                add_error(errors, code="MISSING_TRANSFORM_STEP_KEY", message=f"missing required transform_lineage key: {key}")
                return
        validate_identifier(step, "step_id", errors, code="INVALID_STEP_ID")
        validate_nonblank_string(step, "step_kind", errors)
        validate_nonblank_string(step, "description", errors)
        validate_boolean(step, "deterministic", errors, code="INVALID_DETERMINISTIC")
        validate_boolean(step, "review_required", errors, code="INVALID_REVIEW_REQUIRED")
        step_id = step.get("step_id")
        if isinstance(step_id, str):
            if step_id in seen_step_ids:
                add_error(errors, code="DUPLICATE_STEP_ID", message=f"duplicate transform step_id: {step_id}")
                return
            seen_step_ids.add(step_id)

    handoff_positions = [index for index, step in enumerate(lineage) if isinstance(step, dict) and step.get("step_kind") == EMIT_HANDOFF_STEP_KIND]
    if not handoff_positions:
        add_error(errors, code="MISSING_HANDOFF_STEP", message="transform_lineage must contain an emit_handoff step")
        return
    if handoff_positions[-1] != len(lineage) - 1:
        add_error(errors, code="EMIT_HANDOFF_NOT_TERMINAL", message="emit_handoff must be the final transform step")


def validate_automation_rules(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    automation_posture = payload.get("automation_posture")
    if automation_posture != "unattended_safe":
        return

    rights = payload.get("rights_and_storage")
    if isinstance(rights, dict) and rights.get("rights_posture") in REVIEW_RIGHTS_POSTURES:
        add_error(
            errors,
            code="RIGHTS_POSTURE_REQUIRES_REVIEW",
            message=f"rights_posture {rights['rights_posture']} is not allowed for unattended_safe adapters",
        )
        return

    lineage = payload.get("transform_lineage")
    if isinstance(lineage, list):
        for step in lineage:
            if isinstance(step, dict) and step.get("review_required") is True:
                add_error(
                    errors,
                    code="REVIEW_REQUIRED_STEP_NOT_ALLOWED",
                    message="unattended_safe adapters must not declare transform steps with review_required=true",
                )
                return


def validate_source_adapter_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    counts["inspected"] = 1

    unknown_keys = sorted(set(payload) - ALLOWED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")

    validate_identifier(payload, "adapter_id", errors, code="INVALID_ADAPTER_ID")
    validate_identifier(payload, "workspace_id", errors, code="INVALID_WORKSPACE_ID")
    validate_nonblank_string(payload, "display_name", errors)
    if "description" in payload:
        validate_nonblank_string(payload, "description", errors)
    validate_enum(payload, "input_family", INPUT_FAMILIES, errors, code="INVALID_INPUT_FAMILY")
    validate_enum(payload, "automation_posture", AUTOMATION_POSTURES, errors, code="INVALID_AUTOMATION_POSTURE")

    validate_locator(payload, errors)
    validate_content_profile(payload, errors)
    validate_provenance(payload, errors)
    validate_rights_and_storage(payload, errors)
    validate_normalized_handoff(payload, errors)
    validate_transform_lineage(payload, errors)
    validate_automation_rules(payload, errors)

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def validate_source_adapter(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        return {"counts": {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}, "errors": errors, "warnings": []}, exit_code

    return validate_source_adapter_payload(payload)


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_source_adapter(target)

    status = "pass" if exit_code == EXIT_PASS else "fail"
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts={
            "report_json": display_path(args.report_json),
            "report_text": display_path(args.report_text),
        },
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or (display_path(args.target) or args.target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
