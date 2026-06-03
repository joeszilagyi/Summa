#!/usr/bin/env python3
"""Validate crown-jewel-store-policy.v1 JSON artifacts."""

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


VALIDATOR_NAME = "crown_jewel_store_policy"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = "crown-jewel-store-policy.v1"
SCHEMA_PATH = "config/crown_jewel_store_policy.schema.json"

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a crown-jewel store policy JSON file.")
    parser.add_argument("target", help="Path to the crown-jewel store policy JSON file.")
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


def is_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value.strip()) and not path.is_absolute() and ".." not in path.parts


def is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def validate_token_field(payload: dict[str, Any], *, field_name: str, errors: list[dict[str, Any]], id_like: bool = False) -> None:
    value = payload.get(field_name)
    if not is_nonblank_string(value):
        add_error(errors, code="INVALID_STRING", message=f"{field_name} must be a non-blank string")
        return
    pattern = ID_PATTERN if id_like else TOKEN_PATTERN
    if not pattern.fullmatch(value):
        add_error(errors, code="INVALID_TOKEN", message=f"{field_name} must use lowercase token characters only")


def validate_crown_jewel_store_policy(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, load_exit = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, load_exit

    counts["inspected"] = 1

    required_keys = {"schema_version", "policy_id", "backup_root", "notes", "store_families"}
    for key in sorted(set(payload) - required_keys):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(required_keys):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")

    validate_token_field(payload, field_name="policy_id", errors=errors, id_like=True)

    backup_root = payload.get("backup_root")
    if not is_nonblank_string(backup_root):
        add_error(errors, code="INVALID_BACKUP_ROOT", message="backup_root must be a non-blank string")
    elif not is_relative_path(backup_root):
        add_error(errors, code="INVALID_BACKUP_ROOT", message="backup_root must be a relative repo path")

    validate_string_array(payload.get("notes"), field_name="notes", errors=errors, min_items=0)

    store_families = payload.get("store_families")
    if not isinstance(store_families, list) or not store_families:
        add_error(errors, code="INVALID_STORE_FAMILIES", message="store_families must be a non-empty array")
    else:
        seen_store_keys: set[str] = set()
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
            "notes",
        }
        for index, store in enumerate(store_families):
            if not isinstance(store, dict):
                add_error(errors, code="STORE_OBJECT_REQUIRED", message=f"store_families[{index}] must be an object")
                continue
            for key in sorted(set(store) - required_store_keys):
                add_error(errors, code="UNKNOWN_STORE_FIELD", message=f"store_families[{index}] has unexpected field: {key}")
            for key in sorted(required_store_keys):
                if key not in store:
                    add_error(errors, code="MISSING_STORE_FIELD", message=f"store_families[{index}] missing required key: {key}")

            store_key = store.get("store_key")
            if not is_nonblank_string(store_key):
                add_error(errors, code="INVALID_STORE_KEY", message=f"store_families[{index}].store_key must be a non-blank string")
            elif not ID_PATTERN.fullmatch(store_key):
                add_error(errors, code="INVALID_STORE_KEY", message=f"store_families[{index}].store_key must use lowercase token characters only")
            elif store_key in seen_store_keys:
                add_error(errors, code="DUPLICATE_STORE_KEY", message=f"store_families contains duplicate store_key: {store_key}")
            else:
                seen_store_keys.add(store_key)

            if not is_nonblank_string(store.get("display_name")):
                add_error(errors, code="INVALID_DISPLAY_NAME", message=f"store_families[{index}].display_name must be a non-blank string")
            validate_string_array(
                store.get("path_globs"),
                field_name=f"store_families[{index}].path_globs",
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
                    add_error(errors, code="INVALID_TOKEN", message=f"store_families[{index}].{field_name} must be a non-blank string")
                elif not TOKEN_PATTERN.fullmatch(value):
                    add_error(errors, code="INVALID_TOKEN", message=f"store_families[{index}].{field_name} must use lowercase token characters only")
            for field_name in ("backup_frequency_expectation", "restore_expectation"):
                if not is_nonblank_string(store.get(field_name)):
                    add_error(errors, code="INVALID_STRING", message=f"store_families[{index}].{field_name} must be a non-blank string")
            if not isinstance(store.get("silent_replace_forbidden"), bool):
                add_error(errors, code="INVALID_BOOLEAN", message=f"store_families[{index}].silent_replace_forbidden must be a boolean")
            if not isinstance(store.get("missing_ok"), bool):
                add_error(errors, code="INVALID_BOOLEAN", message=f"store_families[{index}].missing_ok must be a boolean")
            validate_string_array(
                store.get("notes"),
                field_name=f"store_families[{index}].notes",
                errors=errors,
                min_items=0,
            )

    exit_code = EXIT_PASS if not errors else EXIT_VALIDATION_FAILED
    counts["accepted" if exit_code == EXIT_PASS else "rejected"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_crown_jewel_store_policy(target)
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
