#!/usr/bin/env python3
"""Validate typed subject manifest JSON files against the current contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from common import (
    EXIT_INPUT_UNAVAILABLE,
    EXIT_PASS,
    EXIT_VALIDATION_FAILED,
    add_report_args,
    display_path,
    emit_report,
    render_text_report,
)

VALIDATOR_NAME = "subject_manifest"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = "subject-manifest.v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

REQUIRED_KEYS = {
    "schema_version",
    "subject_id",
    "display_name",
    "domain_pack",
    "scope_statement",
    "languages",
    "aliases",
    "disambiguation_terms",
    "excluded_senses",
    "enabled_facets",
    "query_families",
}

OPTIONAL_KEYS = {
    "notes",
    "legacy_substrate_paths",
    "public_export_default",
}

ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a typed subject manifest JSON file, including current "
            "domain-pack consistency checks."
        )
    )
    parser.add_argument("target", help="Path to the subject manifest JSON file.")
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


def repo_display(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return display_path(str(path)) or str(path)


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
        add_error(
            errors,
            code="DUPLICATE_JSON_KEY",
            line=1,
            message=str(exc),
        )
        return None, errors, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(
            errors,
            code="NON_STANDARD_JSON_CONSTANT",
            line=1,
            message=str(exc),
        )
        return None, errors, EXIT_VALIDATION_FAILED
    except json.JSONDecodeError as exc:
        add_error(
            errors,
            code="JSON_PARSE_ERROR",
            line=exc.lineno,
            message="invalid JSON syntax",
        )
        return None, errors, EXIT_VALIDATION_FAILED

    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED

    return payload, errors, EXIT_PASS


def validate_identifier(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
) -> None:
    if field not in payload:
        return
    value = payload[field]
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        add_error(
            errors,
            code="INVALID_IDENTIFIER",
            message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$",
        )


def validate_nonblank_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
) -> None:
    if field not in payload:
        return
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code="INVALID_STRING", message=f"{field} must be a non-blank string")


def validate_string_array(
    payload: dict[str, Any],
    field: str,
    *,
    errors: list[dict[str, Any]],
    min_items: int = 0,
) -> None:
    if field not in payload:
        return
    value = payload[field]
    if not isinstance(value, list):
        add_error(errors, code="FIELD_NOT_ARRAY", message=f"{field} must be an array")
        return
    if len(value) < min_items:
        add_error(
            errors,
            code="ARRAY_TOO_SHORT",
            message=f"{field} must contain at least {min_items} item(s)",
        )
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(
                errors,
                code="INVALID_ARRAY_ITEM",
                message=f"{field}[{index}] must be a non-blank string",
            )
            continue
        if item in seen:
            add_error(
                errors,
                code="DUPLICATE_ARRAY_ITEM",
                message=f"{field} contains a duplicate value: {item}",
            )
            continue
        seen.add(item)


def validate_domain_pack_consistency(
    payload: dict[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    pack_id = payload.get("domain_pack")
    if not isinstance(pack_id, str) or not ID_PATTERN.fullmatch(pack_id):
        return

    pack_path = REPO_ROOT / "config" / "domain_packs" / f"{pack_id}.json"
    if not pack_path.is_file():
        add_error(
            errors,
            code="DOMAIN_PACK_NOT_FOUND",
            message=f"domain pack file not found: {repo_display(pack_path)}",
        )
        return

    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        add_error(
            errors,
            code="DOMAIN_PACK_INVALID",
            message=f"domain pack file could not be parsed: {repo_display(pack_path)}",
        )
        return

    if not isinstance(pack, dict):
        add_error(
            errors,
            code="DOMAIN_PACK_INVALID",
            message=f"domain pack file must contain a JSON object: {repo_display(pack_path)}",
        )
        return

    validate_domain_pack_subset(
        payload,
        pack,
        manifest_field="enabled_facets",
        error_code="FACET_NOT_IN_DOMAIN_PACK",
        item_label="facet",
        errors=errors,
    )
    validate_domain_pack_subset(
        payload,
        pack,
        manifest_field="query_families",
        error_code="QUERY_FAMILY_NOT_IN_DOMAIN_PACK",
        item_label="query family",
        errors=errors,
    )


def validate_domain_pack_subset(
    payload: dict[str, Any],
    pack: dict[str, Any],
    *,
    manifest_field: str,
    error_code: str,
    item_label: str,
    errors: list[dict[str, Any]],
) -> None:
    manifest_values = payload.get(manifest_field)
    pack_values = pack.get(manifest_field)
    if not isinstance(manifest_values, list) or not isinstance(pack_values, list):
        return

    allowed_values = {item for item in pack_values if isinstance(item, str)}
    for value in manifest_values:
        if isinstance(value, str) and value not in allowed_values:
            add_error(
                errors,
                code=error_code,
                message=f"{item_label} not enabled in domain pack: {value}",
            )


def validate_manifest(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code

    counts["inspected"] = 1

    unknown_keys = sorted(set(payload) - ALLOWED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")

    for key in sorted(REQUIRED_KEYS):
        if key not in payload or payload[key] is None:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    schema_version = payload.get("schema_version")
    if "schema_version" in payload:
        if not isinstance(schema_version, str):
            add_error(
                errors,
                code="INVALID_SCHEMA_VERSION_TYPE",
                message="schema_version must be a string",
            )
        elif schema_version != SCHEMA_VERSION:
            add_error(
                errors,
                code="INVALID_SCHEMA_VERSION",
                message=f"schema_version must equal {SCHEMA_VERSION}",
            )

    validate_identifier(payload, "subject_id", errors)
    validate_identifier(payload, "domain_pack", errors)
    validate_nonblank_string(payload, "display_name", errors)
    validate_nonblank_string(payload, "scope_statement", errors)
    validate_string_array(payload, "languages", errors=errors, min_items=1)
    validate_string_array(payload, "aliases", errors=errors)
    validate_string_array(payload, "disambiguation_terms", errors=errors)
    validate_string_array(payload, "excluded_senses", errors=errors)
    validate_string_array(payload, "enabled_facets", errors=errors, min_items=1)
    validate_string_array(payload, "query_families", errors=errors, min_items=1)
    validate_string_array(payload, "notes", errors=errors)
    validate_string_array(payload, "legacy_substrate_paths", errors=errors)

    if "public_export_default" in payload and not isinstance(payload["public_export_default"], bool):
        add_error(
            errors,
            code="INVALID_BOOLEAN",
            message="public_export_default must be a boolean",
        )

    validate_domain_pack_consistency(payload, errors)

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_manifest(target)
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
