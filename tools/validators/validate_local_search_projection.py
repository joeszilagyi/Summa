#!/usr/bin/env python3
"""Validate local-search projection JSON artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        is_rfc3339_datetime,
        render_text_report,
        write_json,
        write_text,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        is_rfc3339_datetime,
        render_text_report,
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.local_search_contract import (  # noqa: E402
    INDEXED_FIELD_POLICIES,
    LINEAGE_STATES,
    PROJECTION_SCHEMA_VERSION,
    PUBLICATION_STATES,
    SEARCH_OBJECT_TYPES,
    VISIBILITY_PROFILES,
    is_public_profile,
)
from tools.common.search_leak_policy import (  # noqa: E402
    contains_private_path,
    contains_secret_marker,
    is_private_note_field,
    is_raw_payload_field,
    is_restricted_public_field,
)


VALIDATOR_NAME = "local_search_projection"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/local_search_projection.schema.json"
OBJECT_REF_PATTERN = re.compile(r"^[a-z_]+:[0-9]+$")

REQUIRED_KEYS = {
    "schema_version",
    "generated_at",
    "source",
    "profile",
    "policy",
    "counts",
    "excluded_records",
    "records",
    "warnings",
    "errors",
}
SOURCE_REQUIRED_KEYS = {"database_name", "schema_version", "correction_ledger_applied"}
POLICY_REQUIRED_KEYS = {
    "raw_payload_indexed",
    "full_text_indexed",
    "private_paths_exposed",
    "superseded_records_included",
    "blocked_records_included",
}
COUNT_REQUIRED_KEYS = {"candidate_records", "projected_records", "excluded_records", "indexed_rows"}
RECORD_REQUIRED_KEYS = {
    "projection_id",
    "object_ref",
    "object_type",
    "object_pk",
    "title",
    "subtitle",
    "review_state",
    "publication_state",
    "authority_level",
    "public_blocker",
    "lineage_state",
    "visible_profiles",
    "suppressed_fields",
    "indexed_fields",
}
RECORD_ALLOWED_KEYS = set(RECORD_REQUIRED_KEYS) | {"confidence_score"}
FIELD_REQUIRED_KEYS = {"field", "text", "display_policy"}
SOURCE_ALLOWED_KEYS = {
    "database_name",
    "schema_version",
    "database_fingerprint",
    "correction_ledger_applied",
}
POLICY_ALLOWED_KEYS = {
    "raw_payload_indexed",
    "full_text_indexed",
    "private_paths_exposed",
    "superseded_records_included",
    "blocked_records_included",
}
COUNT_ALLOWED_KEYS = {
    "candidate_records",
    "projected_records",
    "excluded_records",
    "indexed_rows",
}
TOP_LEVEL_ALLOWED_KEYS = {
    "schema_version",
    "generated_at",
    "source",
    "profile",
    "policy",
    "counts",
    "excluded_records",
    "records",
    "warnings",
    "errors",
    "_projection_records_digest",
}
INDEXED_FIELD_ALLOWED_KEYS = set(FIELD_REQUIRED_KEYS)


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object contains duplicate keys."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one local-search projection JSON artifact.",
        epilog=(
            "Reads the target file and writes validation output to stdout.\n"
            "Optional --report-json/--report-text paths are created atomically.\n\n"
            f"Schema: {SCHEMA_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the local-search projection JSON file.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    line: int | None = None,
    path: str | None = None,
) -> None:
    payload = {"code": code, "line": line, "message": message}
    if path is not None:
        payload["path"] = path
    errors.append(payload)


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
        payload = json.loads(raw_text, object_pairs_hook=no_duplicate_object_pairs, parse_constant=reject_json_constant)
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


def validate_nonblank_string(value: Any, field_name: str, errors: list[dict[str, Any]], *, code: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field_name} must be a non-blank string")
        return None
    return value


def validate_string_list(value: Any, field_name: str, errors: list[dict[str, Any]], *, code: str) -> list[str]:
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field_name} must be an array")
        return []
    accepted: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field_name}[{index}] must be a non-blank string")
            continue
        accepted.append(item)
    return accepted


def validate_indexed_fields(
    fields: Any,
    *,
    profile: str,
    errors: list[dict[str, Any]],
    record_label: str,
) -> tuple[list[dict[str, str]], bool]:
    if not isinstance(fields, list):
        add_error(errors, code="INDEXED_FIELDS_NOT_ARRAY", message=f"{record_label}.indexed_fields must be an array")
        return [], False
    validated: list[dict[str, str]] = []
    private_path_found = False
    for index, item in enumerate(fields):
        field_label = f"{record_label}.indexed_fields[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="INDEXED_FIELD_NOT_OBJECT", message=f"{field_label} must be an object")
            continue
        for key in sorted(set(item) - INDEXED_FIELD_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"{field_label} contains unexpected key: {key}")
        for key in sorted(FIELD_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_INDEXED_FIELD_KEY", message=f"missing required {field_label} key: {key}")
        field_name = validate_nonblank_string(item.get("field"), f"{field_label}.field", errors, code="INVALID_INDEXED_FIELD")
        text = validate_nonblank_string(item.get("text"), f"{field_label}.text", errors, code="INVALID_INDEXED_FIELD")
        policy = validate_nonblank_string(
            item.get("display_policy"),
            f"{field_label}.display_policy",
            errors,
            code="INVALID_DISPLAY_POLICY",
        )
        if policy is not None and policy not in INDEXED_FIELD_POLICIES:
            add_error(
                errors,
                code="INVALID_DISPLAY_POLICY",
                message=f"{field_label}.display_policy must be one of: {', '.join(sorted(INDEXED_FIELD_POLICIES))}",
            )
        if policy == "local_only" and is_public_profile(profile):
            add_error(
                errors,
                code="LOCAL_ONLY_FIELD_IN_PUBLIC_PROFILE",
                message=f"{field_label} cannot use local_only display_policy in profile {profile}",
                path=f"{field_label}.display_policy",
            )
        if field_name is not None and is_raw_payload_field(field_name):
            add_error(
                errors,
                code="RAW_PAYLOAD_FIELD_INDEXED",
                message=f"{field_label}.field must not expose raw payload or full-text fields in search artifacts",
                path=f"{field_label}.field",
            )
        if field_name is not None and is_private_note_field(field_name):
            add_error(
                errors,
                code="PRIVATE_NOTE_FIELD_INDEXED",
                message=f"{field_label}.field must not expose private note fields in search artifacts",
                path=f"{field_label}.field",
            )
        if field_name is not None and is_restricted_public_field(field_name) and is_public_profile(profile):
            add_error(
                errors,
                code="RESTRICTED_EVIDENCE_FIELD_IN_PUBLIC_PROFILE",
                message=f"{field_label}.field must not expose restricted evidence fields in public search artifacts",
                path=f"{field_label}.field",
            )
        if text is not None and contains_secret_marker(text):
            add_error(
                errors,
                code="SECRET_MARKER_EXPOSED",
                message=f"{field_label}.text contains a secret-looking value",
                path=f"{field_label}.text",
            )
        if text is not None and contains_private_path(text):
            private_path_found = True
            if is_public_profile(profile) or policy == "public":
                add_error(
                    errors,
                    code="PRIVATE_PATH_EXPOSED",
                    message=f"{field_label}.text must not expose a private path in public-visible search artifacts",
                    path=f"{field_label}.text",
                )
        if field_name is not None and text is not None and policy in INDEXED_FIELD_POLICIES:
            validated.append({"field": field_name, "text": text, "display_policy": policy})
    return validated, private_path_found


def validate_record(record: Any, *, profile: str, errors: list[dict[str, Any]], index: int) -> tuple[dict[str, Any] | None, bool]:
    label = f"records[{index}]"
    if not isinstance(record, dict):
        add_error(errors, code="RECORD_NOT_OBJECT", message=f"{label} must be an object")
        return None, False
    for key in sorted(set(record) - RECORD_ALLOWED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"{label} contains unexpected key: {key}")
    for key in sorted(RECORD_REQUIRED_KEYS - set(record)):
        add_error(errors, code="MISSING_RECORD_KEY", message=f"missing required {label} key: {key}")

    projection_id = validate_nonblank_string(record.get("projection_id"), f"{label}.projection_id", errors, code="INVALID_PROJECTION_ID")
    object_ref = validate_nonblank_string(record.get("object_ref"), f"{label}.object_ref", errors, code="INVALID_OBJECT_REF")
    if object_ref is not None and not OBJECT_REF_PATTERN.fullmatch(object_ref):
        add_error(errors, code="INVALID_OBJECT_REF", message=f"{label}.object_ref must match ^[a-z_]+:[0-9]+$")
    object_type = validate_nonblank_string(record.get("object_type"), f"{label}.object_type", errors, code="INVALID_OBJECT_TYPE")
    if object_type is not None and object_type not in SEARCH_OBJECT_TYPES:
        add_error(errors, code="INVALID_OBJECT_TYPE", message=f"{label}.object_type must be a supported search object type")
    object_pk = record.get("object_pk")
    if not isinstance(object_pk, int) or object_pk < 1:
        add_error(errors, code="INVALID_OBJECT_PK", message=f"{label}.object_pk must be an integer >= 1")
    title = validate_nonblank_string(record.get("title"), f"{label}.title", errors, code="INVALID_TITLE")
    subtitle = record.get("subtitle")
    confidence_score = record.get("confidence_score")
    if confidence_score is not None and not isinstance(confidence_score, (int, float)):
        add_error(errors, code="INVALID_CONFIDENCE_SCORE", message=f"{label}.confidence_score must be a number")
    if subtitle is not None and (not isinstance(subtitle, str) or not subtitle.strip()):
        add_error(errors, code="INVALID_SUBTITLE", message=f"{label}.subtitle must be null or a non-blank string")
    if title is not None and contains_secret_marker(title):
        add_error(errors, code="SECRET_MARKER_EXPOSED", message=f"{label}.title contains a secret-looking value", path=f"{label}.title")
    if title is not None and contains_private_path(title):
        add_error(errors, code="PRIVATE_PATH_EXPOSED", message=f"{label}.title must not expose a private path", path=f"{label}.title")
    if isinstance(subtitle, str):
        if contains_secret_marker(subtitle):
            add_error(errors, code="SECRET_MARKER_EXPOSED", message=f"{label}.subtitle contains a secret-looking value", path=f"{label}.subtitle")
        if contains_private_path(subtitle):
            add_error(errors, code="PRIVATE_PATH_EXPOSED", message=f"{label}.subtitle must not expose a private path", path=f"{label}.subtitle")
    review_state = validate_nonblank_string(record.get("review_state"), f"{label}.review_state", errors, code="INVALID_REVIEW_STATE")
    publication_state = validate_nonblank_string(
        record.get("publication_state"),
        f"{label}.publication_state",
        errors,
        code="INVALID_PUBLICATION_STATE",
    )
    if publication_state is not None and publication_state not in PUBLICATION_STATES:
        add_error(
            errors,
            code="INVALID_PUBLICATION_STATE",
            message=f"{label}.publication_state must be one of: {', '.join(sorted(PUBLICATION_STATES))}",
        )
    lineage_state = validate_nonblank_string(record.get("lineage_state"), f"{label}.lineage_state", errors, code="INVALID_LINEAGE_STATE")
    if lineage_state is not None and lineage_state not in LINEAGE_STATES:
        add_error(errors, code="INVALID_LINEAGE_STATE", message=f"{label}.lineage_state must be current or superseded")
    visible_profiles = validate_string_list(record.get("visible_profiles"), f"{label}.visible_profiles", errors, code="INVALID_VISIBLE_PROFILE")
    for visible_profile in visible_profiles:
        if visible_profile not in VISIBILITY_PROFILES:
            add_error(errors, code="INVALID_VISIBLE_PROFILE", message=f"{label}.visible_profiles contains unsupported profile: {visible_profile}")
    suppressed_fields = validate_string_list(record.get("suppressed_fields"), f"{label}.suppressed_fields", errors, code="INVALID_SUPPRESSED_FIELD")
    indexed_fields, private_path_found = validate_indexed_fields(
        record.get("indexed_fields"),
        profile=profile,
        errors=errors,
        record_label=label,
    )
    if not indexed_fields:
        add_error(errors, code="INDEXED_FIELDS_EMPTY", message=f"{label}.indexed_fields must contain at least one field")
    if profile in visible_profiles:
        if is_public_profile(profile) and publication_state in {"private_working", "local_only", "blocked"}:
            add_error(
                errors,
                code="PUBLIC_VISIBILITY_CONTRADICTION",
                message=f"{label} cannot be visible in {profile} with publication_state {publication_state}",
                path=f"{label}.publication_state",
            )
        if is_public_profile(profile) and record.get("public_blocker") not in {None, ""}:
            add_error(
                errors,
                code="PUBLIC_BLOCKER_VISIBLE",
                message=f"{label} cannot remain visible in {profile} while public_blocker is set",
                path=f"{label}.public_blocker",
            )
        if is_public_profile(profile) and lineage_state == "superseded":
            add_error(
                errors,
                code="SUPERSEDED_PUBLIC_RECORD",
                message=f"{label} cannot remain visible in {profile} while lineage_state is superseded",
                path=f"{label}.lineage_state",
            )
    if title is not None and projection_id is not None and object_ref is not None and object_type is not None and review_state is not None and publication_state is not None and lineage_state is not None and isinstance(object_pk, int) and object_pk >= 1:
        return (
            {
                "projection_id": projection_id,
                "object_ref": object_ref,
                "object_type": object_type,
                "object_pk": object_pk,
                "title": title,
                "subtitle": subtitle,
                "review_state": review_state,
                "publication_state": publication_state,
                "authority_level": record.get("authority_level"),
                "public_blocker": record.get("public_blocker"),
                "lineage_state": lineage_state,
                "visible_profiles": visible_profiles,
                "suppressed_fields": suppressed_fields,
                "indexed_fields": indexed_fields,
            },
            private_path_found,
        )
    return None, private_path_found


def validate_local_search_projection_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for key in sorted(set(payload) - TOP_LEVEL_ALLOWED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"top-level contains unexpected key: {key}")
    for key in sorted(REQUIRED_KEYS - set(payload)):
        add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {PROJECTION_SCHEMA_VERSION}")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not is_rfc3339_datetime(generated_at):
        add_error(errors, code="INVALID_GENERATED_AT", message="generated_at must be an RFC3339 datetime")

    profile = payload.get("profile")
    if not isinstance(profile, str) or profile not in VISIBILITY_PROFILES:
        add_error(errors, code="INVALID_PROFILE", message=f"profile must be one of: {', '.join(sorted(VISIBILITY_PROFILES))}")
        profile = "local"

    source = payload.get("source")
    if not isinstance(source, dict):
        add_error(errors, code="SOURCE_NOT_OBJECT", message="source must be an object")
    else:
        for key in sorted(set(source) - SOURCE_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"source contains unexpected key: {key}")
        for key in sorted(SOURCE_REQUIRED_KEYS - set(source)):
            add_error(errors, code="MISSING_SOURCE_KEY", message=f"missing required source key: {key}")
        validate_nonblank_string(source.get("database_name"), "source.database_name", errors, code="INVALID_SOURCE_FIELD")
        if "correction_ledger_applied" in source and not isinstance(source.get("correction_ledger_applied"), bool):
            add_error(errors, code="INVALID_SOURCE_FIELD", message="source.correction_ledger_applied must be a boolean")

    policy = payload.get("policy")
    private_path_found = False
    if not isinstance(policy, dict):
        add_error(errors, code="POLICY_NOT_OBJECT", message="policy must be an object")
    else:
        for key in sorted(set(policy) - POLICY_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"policy contains unexpected key: {key}")
        for key in sorted(POLICY_REQUIRED_KEYS - set(policy)):
            add_error(errors, code="MISSING_POLICY_KEY", message=f"missing required policy key: {key}")
        for field_name in sorted(POLICY_REQUIRED_KEYS):
            if field_name in policy and not isinstance(policy.get(field_name), bool):
                add_error(errors, code="INVALID_POLICY_FIELD", message=f"policy.{field_name} must be a boolean")
        if policy.get("raw_payload_indexed") is not False:
            add_error(errors, code="RAW_PAYLOAD_INDEXED", message="policy.raw_payload_indexed must be false")
        if policy.get("full_text_indexed") is not False:
            add_error(errors, code="FULL_TEXT_INDEXED", message="policy.full_text_indexed must be false")

    counts = payload.get("counts")
    if not isinstance(counts, dict):
        add_error(errors, code="COUNTS_NOT_OBJECT", message="counts must be an object")
    else:
        for key in sorted(set(counts) - COUNT_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"counts contains unexpected key: {key}")
        for key in sorted(COUNT_REQUIRED_KEYS - set(counts)):
            add_error(errors, code="MISSING_COUNT_KEY", message=f"missing required counts key: {key}")
        for key in sorted(COUNT_REQUIRED_KEYS):
            value = counts.get(key)
            if not isinstance(value, int) or value < 0:
                add_error(errors, code="INVALID_COUNT_VALUE", message=f"counts.{key} must be an integer >= 0")

    excluded_records = payload.get("excluded_records")
    if not isinstance(excluded_records, list):
        add_error(errors, code="EXCLUDED_NOT_ARRAY", message="excluded_records must be an array")
        excluded_count = 0
    else:
        excluded_count = len(excluded_records)
        for index, record in enumerate(excluded_records):
            label = f"excluded_records[{index}]"
            if not isinstance(record, dict):
                add_error(errors, code="EXCLUDED_NOT_OBJECT", message=f"{label} must be an object")
                continue
            for key in sorted(set(record) - {"object_ref", "reason"}):
                add_error(errors, code="UNKNOWN_FIELD", message=f"{label} contains unexpected key: {key}")
            validate_nonblank_string(record.get("object_ref"), f"{label}.object_ref", errors, code="INVALID_EXCLUDED_RECORD")
            validate_nonblank_string(record.get("reason"), f"{label}.reason", errors, code="INVALID_EXCLUDED_RECORD")

    records_value = payload.get("records")
    validated_records: list[dict[str, Any]] = []
    if not isinstance(records_value, list):
        add_error(errors, code="RECORDS_NOT_ARRAY", message="records must be an array")
    else:
        for index, record in enumerate(records_value):
            validated, found_private_path = validate_record(record, profile=profile, errors=errors, index=index)
            private_path_found = private_path_found or found_private_path
            if validated is not None:
                validated_records.append(validated)

    if isinstance(counts, dict):
        if counts.get("projected_records") != len(validated_records):
            add_error(errors, code="COUNT_MISMATCH", message="counts.projected_records must equal len(records)")
        if counts.get("excluded_records") != excluded_count:
            add_error(errors, code="COUNT_MISMATCH", message="counts.excluded_records must equal len(excluded_records)")
        if counts.get("indexed_rows") != len(validated_records):
            add_error(errors, code="COUNT_MISMATCH", message="counts.indexed_rows must equal len(records)")
        if isinstance(counts.get("candidate_records"), int) and counts["candidate_records"] < len(validated_records) + excluded_count:
            add_error(
                errors,
                code="COUNT_MISMATCH",
                message="counts.candidate_records must be at least projected_records + excluded_records",
            )
    if isinstance(policy, dict):
        if bool(policy.get("private_paths_exposed")) != private_path_found:
            add_error(
                errors,
                code="PRIVATE_PATH_POLICY_MISMATCH",
                message="policy.private_paths_exposed must match whether indexed_fields contain private-looking paths",
            )
    return errors


def validate_local_search_projection(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, load_exit = load_json_object(target)
    if payload is not None and load_exit == EXIT_PASS:
        errors.extend(validate_local_search_projection_payload(payload))

    if load_exit == EXIT_INPUT_UNAVAILABLE:
        exit_code = EXIT_INPUT_UNAVAILABLE
    elif errors:
        exit_code = EXIT_VALIDATION_FAILED
    else:
        exit_code = EXIT_PASS

    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "target": display_path(str(target)),
        "status": "pass" if exit_code == EXIT_PASS else "fail",
        "counts": {
            "inspected": 0 if load_exit == EXIT_INPUT_UNAVAILABLE else 1,
            "accepted": 1 if exit_code == EXIT_PASS else 0,
            "rejected": 1 if exit_code == EXIT_VALIDATION_FAILED else 0,
            "deferred": 0,
        },
        "errors": errors,
        "warnings": [],
        "output_artifacts": {},
        "scenario": None,
    }
    return report, exit_code


def main() -> int:
    args = parse_args()
    report, exit_code = validate_local_search_projection(Path(args.target))
    report["scenario"] = args.scenario
    if args.target_id:
        report["target"] = args.target_id
    report["output_artifacts"] = {
        "report_json": display_path(args.report_json) if args.report_json else None,
        "report_text": display_path(args.report_text) if args.report_text else None,
    }
    text_report = render_text_report(report)
    write_json(args.report_json, report)
    write_text(args.report_text, text_report)
    sys.stdout.write(text_report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
