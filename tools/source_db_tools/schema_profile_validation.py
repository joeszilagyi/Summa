#!/usr/bin/env python3
"""Profile-boundary validation for canonical SQLite source/work records.

The DB remains permissive. This module validates the nested canonical record
shape produced by export_bibliography.load_records at promotion, review, export,
and handoff boundaries.

Documentation: docs/tools/source_db_tools/schema_profile_validation.md
When modifying profile semantics or report shape, update that guide and the
paired validate_schema_profile.py CLI wrapper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.source_db_tools import (  # noqa: E402
    claim_types,
    confidence_model,
    identifier_normalization,
    relationship_predicates,
    rights_retention,
    source_types,
)

PROFILE_PATH = Path(__file__).resolve().with_name("schema_profiles.json")
REPORT_SCHEMA_VERSION = "schema-profile-validation-report.v1"
LIST_PATH_MARKER = "[]."


def load_profiles(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read schema profiles: {path}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in schema profiles: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"schema profiles payload must be a JSON object: {path}")
    if not isinstance(payload.get("profiles"), dict):
        raise ValueError(f"schema profiles payload missing object key 'profiles': {path}")
    return payload


def profile_names() -> list[str]:
    return sorted(load_profiles()["profiles"])


def first_nonblank(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (str, list, dict)):
            return str(value)
    return None


def meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def values_for_path(obj: Any, path: str) -> list[Any]:
    """Resolve the profile path DSL against a canonical record.

    A [] suffix expands array rows, so `source_claims[].claim_type` returns
    values from every claim row while scalar segments preserve missing values as
    None for callers that need to detect absent fields.
    """
    current = [obj]
    for part in path.split("."):
        next_values: list[Any] = []
        list_part = part.endswith("[]")
        key = part[:-2] if list_part else part
        for value in current:
            child = value.get(key) if isinstance(value, dict) else None
            if list_part:
                if isinstance(child, list):
                    next_values.extend(child)
                elif child is not None:
                    next_values.append(child)
            else:
                next_values.append(child)
        current = next_values
    return current


def path_present(record: dict[str, Any], path: str) -> bool:
    return any(meaningful(value) for value in values_for_path(record, path))


def list_path_parts(path: str) -> tuple[str, str] | None:
    if path.count("[]") != 1:
        return None
    if path.endswith("[]"):
        section = path[:-2]
        return (section, "") if section else None
    if LIST_PATH_MARKER not in path:
        return None
    section, child_path = path.split(LIST_PATH_MARKER, 1)
    if not section or not child_path:
        return None
    return section, child_path


def required_field_missing(record: dict[str, Any], path: str) -> list[str]:
    parts = list_path_parts(path)
    if parts is None:
        return [] if path_present(record, path) else [path]

    section, child_path = parts
    rows = record.get(section)
    if not isinstance(rows, list) or not rows:
        return [path]

    missing: list[str] = []
    for index, row in enumerate(rows):
        field = f"{section}[{index}]"
        if not isinstance(row, dict):
            missing.append(field)
            continue
        if child_path and not path_present(row, child_path):
            missing.append(f"{field}.{child_path}")
        elif not child_path and not meaningful(row):
            missing.append(field)
    return missing


def record_id(record: dict[str, Any]) -> str:
    work = record.get("work", {})
    return first_nonblank(work.get("work_key_v1"), work.get("work_id")) or "unknown-record"


def issue(
    *,
    profile_name: str,
    record: dict[str, Any],
    severity: str,
    code: str,
    field: str | None,
    message: str,
) -> dict[str, Any]:
    return {
        "profile": profile_name,
        "record_id": record_id(record),
        "severity": severity,
        "code": code,
        "field": field,
        "message": message,
    }


def condition_matches(record: dict[str, Any], condition: Any) -> bool:
    if not isinstance(condition, dict):
        raise ValueError("schema profile condition must be an object")
    path = condition.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("schema profile condition must declare a non-empty path")
    values = values_for_path(record, path.strip())
    if condition.get("present") is True:
        return any(meaningful(value) for value in values)
    if "equals" in condition:
        return any(value == condition["equals"] for value in values)
    if "in" in condition:
        allowed = set(condition["in"])
        return any(value in allowed for value in values)
    return False


def scoped_condition_rows(
    record: dict[str, Any],
    condition: Any,
) -> list[tuple[str, int, dict[str, Any]]] | None:
    if not isinstance(condition, dict):
        return None
    path = condition.get("path")
    if not isinstance(path, str):
        return None
    parts = list_path_parts(path)
    if parts is None:
        return None

    section, child_path = parts
    if not child_path:
        return None
    rows = record.get(section)
    if not isinstance(rows, list):
        return []

    row_condition = dict(condition)
    row_condition["path"] = child_path
    matched: list[tuple[str, int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        if isinstance(row, dict) and condition_matches(row, row_condition):
            matched.append((section, index, row))
    return matched


def scoped_child_path(section: str, path: str) -> str | None:
    parts = list_path_parts(path)
    if parts is None:
        return None
    field_section, child_path = parts
    if field_section != section or not child_path:
        return None
    return child_path


def identifier_rows(record: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for index, row in enumerate(record.get("work_identifiers", record.get("identifiers", []))):
        if isinstance(row, dict):
            rows.append(("work_identifiers", f"work_identifiers[{index}]", row))
    for index, row in enumerate(record.get("authority_identifiers", [])):
        if isinstance(row, dict):
            rows.append(("authority_identifiers", f"authority_identifiers[{index}]", row))
    return rows


def validate_identifier_policy(
    record: dict[str, Any],
    profile_name: str,
    profile: dict[str, Any],
    default_policy: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    policy = profile.get("identifier_policy", default_policy)
    if not policy:
        return {"errors": [], "warnings": []}

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], str] = {}
    duplicate_severity = policy.get("duplicate_severity", "warning")
    invalid_severity = policy.get("invalid_severity", "warning")
    unsupported_severity = policy.get("unsupported_scheme_severity", "warning")

    def append(severity: str, code: str, field: str, message: str) -> None:
        target = errors if severity == "error" else warnings
        target.append(
            issue(
                profile_name=profile_name,
                record=record,
                severity=severity,
                code=code,
                field=field,
                message=message,
            )
        )

    for namespace, field, row in identifier_rows(record):
        normalized = identifier_normalization.normalize_identifier_row(row)
        status = normalized["validity_status"]
        scheme = normalized["scheme"] or str(row.get("scheme", "")).strip().lower()
        value = normalized["normalized_value"] or normalized["raw_value"]
        if status == "unsupported_scheme":
            append(
                unsupported_severity,
                "UNSUPPORTED_IDENTIFIER_SCHEME",
                f"{field}.scheme",
                f"{profile_name} identifier scheme is unsupported: {scheme}",
            )
        elif status in {"invalid", "needs_review"}:
            severity = (
                invalid_severity
                if status == "invalid"
                else policy.get("needs_review_severity", "warning")
            )
            append(
                severity,
                "INVALID_IDENTIFIER" if status == "invalid" else "IDENTIFIER_NEEDS_REVIEW",
                f"{field}.value",
                f"{profile_name} identifier {scheme}:{normalized['raw_value']} is {status}: {normalized['validation_warning']}",
            )
        if not scheme or not value:
            continue
        dedupe_key = (namespace, scheme, value)
        if dedupe_key in seen:
            append(
                duplicate_severity,
                "DUPLICATE_IDENTIFIER",
                field,
                f"{profile_name} duplicate normalized identifier {scheme}:{value} also appears at {seen[dedupe_key]}",
            )
        else:
            seen[dedupe_key] = field

    return {"errors": errors, "warnings": warnings}


def validate_record(
    record: dict[str, Any],
    profile_name: str,
    *,
    profiles_payload: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    payload = profiles_payload or load_profiles()
    profiles = payload["profiles"]
    if profile_name not in profiles:
        raise ValueError(f"unknown schema validation profile: {profile_name}")
    profile = profiles[profile_name]
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    allowed_missing = set(profile.get("allowed_missing_fields", []))

    for field in profile.get("required_fields", []):
        for missing_field in required_field_missing(record, field):
            errors.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity="error",
                    code="MISSING_REQUIRED_FIELD",
                    field=missing_field,
                    message=f"{profile_name} missing required field: {missing_field}",
                )
            )

    source_type_policy = profile.get("source_type_policy")
    if source_type_policy:
        work_type = first_nonblank(*values_for_path(record, "work.work_type"))
        validation_issue = source_types.validation_issue(
            work_type,
            required_mappings=source_type_policy.get("require_mappings", []),
        )
        if validation_issue:
            code, message = validation_issue
            severity = source_type_policy.get("unknown_severity", "error")
            target = errors if severity == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=severity,
                    code=code,
                    field="work.work_type",
                    message=f"{profile_name} source type policy failed: {message}",
                )
            )

    relationship_policy = profile.get("relationship_predicate_policy")
    if relationship_policy:
        for row in relationship_predicates.validate_relationships(
            record.get("source_relationships", []),
            unknown_severity=relationship_policy.get("unknown_severity", "error"),
            evidence_missing_severity=relationship_policy.get("evidence_missing_severity", "error"),
        ):
            severity = row["severity"]
            target = errors if severity == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=severity,
                    code=row["code"],
                    field=row["field"],
                    message=f"{profile_name} relationship predicate policy failed: {row['message']}",
                )
            )

    claim_type_policy = profile.get("claim_type_policy")
    if claim_type_policy:
        for row in claim_types.validate_claims(
            record,
            unknown_severity=claim_type_policy.get("unknown_severity", "error"),
            evidence_missing_severity=claim_type_policy.get("evidence_missing_severity", "error"),
            review_required_severity=claim_type_policy.get("review_required_severity", "warning"),
        ):
            severity = row["severity"]
            target = errors if severity == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=severity,
                    code=row["code"],
                    field=row["field"],
                    message=f"{profile_name} claim type policy failed: {row['message']}",
                )
            )

    confidence_policy = profile.get("confidence_policy")
    if confidence_policy:
        for row in confidence_model.validate_record_confidence(record, policy=confidence_policy):
            severity = row["severity"]
            target = errors if severity == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=severity,
                    code=row["code"],
                    field=row["field"],
                    message=f"{profile_name} confidence policy failed: {row['message']}",
                )
            )

    identifier_result = validate_identifier_policy(
        record,
        profile_name,
        profile,
        payload.get("identifier_policy_default"),
    )
    errors.extend(identifier_result["errors"])
    warnings.extend(identifier_result["warnings"])

    retention_result = rights_retention.validate_record_policy(record)
    for row in retention_result["errors"]:
        errors.append(
            issue(
                profile_name=profile_name,
                record=record,
                severity="error",
                code=row["code"],
                field="retention_policy",
                message=f"{profile_name} rights/retention policy failed: {row['message']}",
            )
        )
    for row in retention_result["warnings"]:
        warnings.append(
            issue(
                profile_name=profile_name,
                record=record,
                severity="warning",
                code=row["code"],
                field="retention_policy",
                message=f"{profile_name} rights/retention policy warning: {row['message']}",
            )
        )

    for field in profile.get("recommended_fields", []):
        if field in allowed_missing:
            continue
        if not path_present(record, field):
            warnings.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity="warning",
                    code="MISSING_RECOMMENDED_FIELD",
                    field=field,
                    message=f"{profile_name} missing recommended field: {field}",
                )
            )

    for rule in profile.get("invalid_field_combinations", []):
        condition = rule.get("when", {})
        scoped_rows = scoped_condition_rows(record, condition)
        if scoped_rows is not None:
            for section, index, row in scoped_rows:
                condition_child_path = scoped_child_path(section, condition["path"])
                missing_required: list[str] = []
                for field in rule.get("require_all", []):
                    child_path = scoped_child_path(section, field)
                    if child_path is not None:
                        if not path_present(row, child_path):
                            missing_required.append(f"{section}[{index}].{child_path}")
                    elif not path_present(record, field):
                        missing_required.append(field)
                if missing_required:
                    target = errors if rule.get("severity") == "error" else warnings
                    target.append(
                        issue(
                            profile_name=profile_name,
                            record=record,
                            severity=rule.get("severity", "error"),
                            code=rule.get("code", "INVALID_FIELD_COMBINATION"),
                            field=", ".join(missing_required),
                            message=rule.get("message", "invalid field combination"),
                        )
                    )
                forbidden_present: list[str] = []
                for field in rule.get("forbid_present", []):
                    child_path = scoped_child_path(section, field)
                    if child_path is not None:
                        if path_present(row, child_path):
                            forbidden_present.append(f"{section}[{index}].{child_path}")
                    elif path_present(record, field):
                        forbidden_present.append(field)
                if forbidden_present:
                    target = errors if rule.get("severity") == "error" else warnings
                    target.append(
                        issue(
                            profile_name=profile_name,
                            record=record,
                            severity=rule.get("severity", "error"),
                            code=rule.get("code", "INVALID_FIELD_COMBINATION"),
                            field=", ".join(forbidden_present),
                            message=rule.get("message", "invalid field combination"),
                        )
                    )
                if (
                    not missing_required
                    and not forbidden_present
                    and not rule.get("require_all")
                    and not rule.get("forbid_present")
                ):
                    target = errors if rule.get("severity") == "error" else warnings
                    target.append(
                        issue(
                            profile_name=profile_name,
                            record=record,
                            severity=rule.get("severity", "warning"),
                            code=rule.get("code", "INVALID_FIELD_COMBINATION"),
                            field=f"{section}[{index}].{condition_child_path}",
                            message=rule.get("message", "invalid field combination"),
                        )
                    )
            continue

        if not condition_matches(record, condition):
            continue
        missing_required = [
            field for field in rule.get("require_all", []) if not path_present(record, field)
        ]
        if missing_required:
            target = errors if rule.get("severity") == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=rule.get("severity", "error"),
                    code=rule.get("code", "INVALID_FIELD_COMBINATION"),
                    field=", ".join(missing_required),
                    message=rule.get("message", "invalid field combination"),
                )
            )
        forbidden_present = [
            field for field in rule.get("forbid_present", []) if path_present(record, field)
        ]
        if forbidden_present:
            target = errors if rule.get("severity") == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=rule.get("severity", "error"),
                    code=rule.get("code", "INVALID_FIELD_COMBINATION"),
                    field=", ".join(forbidden_present),
                    message=rule.get("message", "invalid field combination"),
                )
            )
        if (
            not missing_required
            and not forbidden_present
            and not rule.get("require_all")
            and not rule.get("forbid_present")
        ):
            target = errors if rule.get("severity") == "error" else warnings
            target.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity=rule.get("severity", "warning"),
                    code=rule.get("code", "INVALID_FIELD_COMBINATION"),
                    field=rule.get("when", {}).get("path"),
                    message=rule.get("message", "invalid field combination"),
                )
            )

    for field in profile.get("lossy_export_dropped_fields", []):
        if path_present(record, field):
            warnings.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity="warning",
                    code="FIELD_DROPPED_BY_PROFILE",
                    field=field,
                    message=f"{profile_name} will drop or transform field family: {field}",
                )
            )

    for field in profile.get("review_required_fields", []):
        if path_present(record, field):
            warnings.append(
                issue(
                    profile_name=profile_name,
                    record=record,
                    severity="warning",
                    code="FIELD_REQUIRES_REVIEW",
                    field=field,
                    message=f"{profile_name} should review field before boundary handoff: {field}",
                )
            )

    return {"errors": errors, "warnings": warnings}


def validate_records(records: list[dict[str, Any]], profile_name: str) -> dict[str, Any]:
    payload = load_profiles()
    profiles = payload["profiles"]
    if profile_name not in profiles:
        raise ValueError(f"unknown schema validation profile: {profile_name}")

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for record in records:
        result = validate_record(record, profile_name, profiles_payload=payload)
        errors.extend(result["errors"])
        warnings.extend(result["warnings"])

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "profile_schema_version": payload["schema_version"],
        "profile": profile_name,
        "ok": not errors,
        "record_count": len(records),
        "records_validated": len(records),
        "errors": errors,
        "warnings": warnings,
        "profile_declaration": profiles[profile_name],
    }


def render_report(records: list[dict[str, Any]], profile_name: str) -> str:
    return (
        json.dumps(
            validate_records(records, profile_name), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )
