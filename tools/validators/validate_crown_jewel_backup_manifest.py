#!/usr/bin/env python3
"""Validate crown-jewel-backup-manifest.v1 JSON artifacts."""

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
        emit_report,
        is_rfc3339_datetime,
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
        is_rfc3339_datetime,
        render_text_report,
    )


VALIDATOR_NAME = "crown_jewel_backup_manifest"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = "crown-jewel-backup-manifest.v1"
SCHEMA_PATH = "config/crown_jewel_backup_manifest.schema.json"
ALLOWED_STATUSES = {"present", "missing_allowed", "missing_required"}

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a crown-jewel backup manifest JSON file.")
    parser.add_argument("target", help="Path to the crown-jewel backup manifest JSON file.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(errors: list[dict[str, Any]], *, code: str, message: str, line: int | None = None) -> None:
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
    except OSError:
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


def is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value.strip()) and not path.is_absolute() and ".." not in path.parts


def validate_string_array(
    value: Any,
    *,
    field_name: str,
    errors: list[dict[str, Any]],
    min_items: int = 0,
    require_relative_path: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) < min_items:
        add_error(errors, code="INVALID_ARRAY", message=f"{field_name} must be an array with at least {min_items} item(s)")
        return []
    accepted: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not is_nonblank_string(item):
            add_error(errors, code="INVALID_ARRAY_ITEM", message=f"{field_name}[{index}] must be a non-blank string")
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{field_name} contains duplicate value: {item}")
            continue
        if require_relative_path and not is_relative_path(item):
            add_error(errors, code="INVALID_RELATIVE_PATH", message=f"{field_name}[{index}] must be a relative repo path or glob")
            continue
        seen.add(item)
        accepted.append(item)
    return accepted


def validate_crown_jewel_backup_manifest(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, load_exit = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, load_exit

    counts["inspected"] = 1

    required_keys = {
        "schema_version",
        "policy_id",
        "policy_path",
        "created_at",
        "repo_root",
        "backup_root",
        "requested_store_keys",
        "store_entries",
    }
    for key in sorted(set(payload) - required_keys):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(required_keys):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")

    policy_id = payload.get("policy_id")
    if not is_nonblank_string(policy_id):
        add_error(errors, code="INVALID_POLICY_ID", message="policy_id must be a non-blank string")
    elif not ID_PATTERN.fullmatch(policy_id):
        add_error(errors, code="INVALID_POLICY_ID", message="policy_id must use lowercase token characters only")

    for field_name in ("policy_path", "backup_root"):
        value = payload.get(field_name)
        if not is_nonblank_string(value):
            add_error(errors, code="INVALID_RELATIVE_PATH", message=f"{field_name} must be a non-blank string")
        elif not is_relative_path(value):
            add_error(errors, code="INVALID_RELATIVE_PATH", message=f"{field_name} must be a relative repo path")

    repo_root = payload.get("repo_root")
    if not is_nonblank_string(repo_root):
        add_error(errors, code="INVALID_REPO_ROOT", message="repo_root must be a non-blank string")
    elif not Path(repo_root).is_absolute():
        add_error(errors, code="INVALID_REPO_ROOT", message="repo_root must be an absolute path")

    created_at = payload.get("created_at")
    if not isinstance(created_at, str) or not is_rfc3339_datetime(created_at):
        add_error(errors, code="INVALID_TIMESTAMP", message="created_at must be an RFC3339 timestamp")

    requested_store_keys = validate_string_array(
        payload.get("requested_store_keys"),
        field_name="requested_store_keys",
        errors=errors,
        min_items=0,
    )

    store_entries = payload.get("store_entries")
    if not isinstance(store_entries, list) or not store_entries:
        add_error(errors, code="INVALID_STORE_ENTRIES", message="store_entries must be a non-empty array")
    else:
        seen_store_keys: set[str] = set()
        entry_store_keys: list[str] = []
        required_store_keys = {
            "store_key",
            "display_name",
            "path_globs",
            "durability_class",
            "storage_policy_class",
            "backup_frequency_expectation",
            "restore_expectation",
            "integrity_check_method",
            "silent_replace_forbidden",
            "missing_ok",
            "status",
            "match_count",
            "matched_paths",
            "notes",
        }
        for index, store in enumerate(store_entries):
            if not isinstance(store, dict):
                add_error(errors, code="STORE_OBJECT_REQUIRED", message=f"store_entries[{index}] must be an object")
                continue
            for key in sorted(set(store) - required_store_keys):
                add_error(errors, code="UNKNOWN_STORE_FIELD", message=f"store_entries[{index}] has unexpected field: {key}")
            for key in sorted(required_store_keys):
                if key not in store:
                    add_error(errors, code="MISSING_STORE_FIELD", message=f"store_entries[{index}] missing required key: {key}")

            store_key = store.get("store_key")
            if not is_nonblank_string(store_key):
                add_error(errors, code="INVALID_STORE_KEY", message=f"store_entries[{index}].store_key must be a non-blank string")
            elif not ID_PATTERN.fullmatch(store_key):
                add_error(errors, code="INVALID_STORE_KEY", message=f"store_entries[{index}].store_key must use lowercase token characters only")
            elif store_key in seen_store_keys:
                add_error(errors, code="DUPLICATE_STORE_KEY", message=f"store_entries contains duplicate store_key: {store_key}")
            else:
                seen_store_keys.add(store_key)
                entry_store_keys.append(store_key)

            if not is_nonblank_string(store.get("display_name")):
                add_error(errors, code="INVALID_DISPLAY_NAME", message=f"store_entries[{index}].display_name must be a non-blank string")

            validate_string_array(
                store.get("path_globs"),
                field_name=f"store_entries[{index}].path_globs",
                errors=errors,
                min_items=1,
                require_relative_path=True,
            )
            for field_name in (
                "durability_class",
                "storage_policy_class",
                "integrity_check_method",
            ):
                value = store.get(field_name)
                if not is_nonblank_string(value):
                    add_error(errors, code="INVALID_TOKEN", message=f"store_entries[{index}].{field_name} must be a non-blank string")
                elif not TOKEN_PATTERN.fullmatch(value):
                    add_error(errors, code="INVALID_TOKEN", message=f"store_entries[{index}].{field_name} must use lowercase token characters only")
            for field_name in ("backup_frequency_expectation", "restore_expectation"):
                if not is_nonblank_string(store.get(field_name)):
                    add_error(errors, code="INVALID_STRING", message=f"store_entries[{index}].{field_name} must be a non-blank string")
            if not isinstance(store.get("silent_replace_forbidden"), bool):
                add_error(errors, code="INVALID_BOOLEAN", message=f"store_entries[{index}].silent_replace_forbidden must be a boolean")
            missing_ok = store.get("missing_ok")
            if not isinstance(missing_ok, bool):
                add_error(errors, code="INVALID_BOOLEAN", message=f"store_entries[{index}].missing_ok must be a boolean")
            status = store.get("status")
            if status not in ALLOWED_STATUSES:
                add_error(errors, code="INVALID_STATUS", message=f"store_entries[{index}].status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}")
            match_count = store.get("match_count")
            if not isinstance(match_count, int) or isinstance(match_count, bool) or match_count < 0:
                add_error(errors, code="INVALID_MATCH_COUNT", message=f"store_entries[{index}].match_count must be a non-negative integer")
                match_count = None
            matched_paths = validate_string_array(
                store.get("matched_paths"),
                field_name=f"store_entries[{index}].matched_paths",
                errors=errors,
                min_items=0,
                require_relative_path=True,
            )
            validate_string_array(
                store.get("notes"),
                field_name=f"store_entries[{index}].notes",
                errors=errors,
                min_items=0,
            )

            if match_count is not None and match_count != len(matched_paths):
                add_error(errors, code="MATCH_COUNT_MISMATCH", message=f"store_entries[{index}].match_count must equal len(matched_paths)")
            if status == "present":
                if match_count == 0 or not matched_paths:
                    add_error(errors, code="STATUS_PATH_MISMATCH", message=f"store_entries[{index}] marked present must include matched_paths")
            elif status in {"missing_allowed", "missing_required"}:
                if match_count not in {None, 0} or matched_paths:
                    add_error(errors, code="STATUS_PATH_MISMATCH", message=f"store_entries[{index}] marked missing must not include matched paths")
            if isinstance(missing_ok, bool) and status == "missing_allowed" and not missing_ok:
                add_error(errors, code="MISSING_OK_CONTRADICTION", message=f"store_entries[{index}] cannot be missing_allowed when missing_ok is false")
            if isinstance(missing_ok, bool) and status == "missing_required" and missing_ok:
                add_error(errors, code="MISSING_OK_CONTRADICTION", message=f"store_entries[{index}] cannot be missing_required when missing_ok is true")

        if requested_store_keys:
            missing_requested = sorted(set(requested_store_keys) - set(entry_store_keys))
            if missing_requested:
                add_error(errors, code="REQUESTED_STORE_KEY_MISSING", message="requested_store_keys missing from store_entries: " + ", ".join(missing_requested))

    exit_code = EXIT_PASS if not errors else EXIT_VALIDATION_FAILED
    counts["accepted" if exit_code == EXIT_PASS else "rejected"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_crown_jewel_backup_manifest(target)
    status = "pass" if exit_code == EXIT_PASS else "fail"
    output_artifacts = {
        "report_json": display_path(args.report_json),
        "report_text": display_path(args.report_text),
    }
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts=output_artifacts,
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or display_path(args.target) or str(target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
