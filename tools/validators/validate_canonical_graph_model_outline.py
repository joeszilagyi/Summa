#!/usr/bin/env python3
"""Validate canonical graph model outline JSON artifacts."""

from __future__ import annotations

import argparse
import json
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
        render_text_report,
        resolve_report_root,
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
        render_text_report,
        resolve_report_root,
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.canonical_graph_model_contract import (  # noqa: E402
    CONTRACT_DOC,
    DOCUMENTED_EXPECTED_SQLITE_TABLES,
    REQUIRED_NONCANONICAL_STAGING_TABLES,
    REQUIRED_RECORD_FAMILIES,
    REQUIRED_SCHEMA_METADATA_TABLES,
    REQUIRED_SIDECARS,
    REQUIRED_SQLITE_TABLE_MAPPINGS,
    REQUIRED_SUPPORTING_SQLITE_TABLES,
    SCHEMA_VERSION,
)

VALIDATOR_NAME = "canonical_graph_model_outline"
CONTRACT_VERSION = "1"
REQUIRED_KEYS = {
    "schema_version",
    "contract_doc",
    "summary",
    "canonical_record_families",
    "append_only_sidecars",
    "supporting_sqlite_tables",
    "schema_metadata_tables",
    "noncanonical_staging_tables",
    "migration_stages",
}
FAMILY_REQUIRED_KEYS = {
    "record_family",
    "description",
    "current_sqlite_tables",
    "provenance_strategy",
    "review_strategy",
}
SIDECAR_REQUIRED_KEYS = {
    "sidecar",
    "schema_version",
    "applies_to_record_families",
    "purpose",
}
MIGRATION_REQUIRED_KEYS = {
    "stage_id",
    "title",
    "current_inputs",
    "target_record_families",
    "notes",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a canonical graph model outline JSON file.")
    parser.add_argument("target", help="Path to the canonical graph model outline JSON file.")
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


def validate_nonblank_string(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field_name} must be a non-blank string")
        return None
    return value


def validate_string_array(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str) -> list[str]:
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field_name} must be an array of non-blank strings")
        return []
    accepted: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field_name}[{index}] must be a non-blank string")
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{field_name} contains duplicate value: {item}")
            continue
        seen.add(item)
        accepted.append(item)
    return accepted


def validate_record_families(value: Any, errors: list[dict[str, Any]]) -> set[str]:
    if not isinstance(value, list):
        add_error(errors, code="FAMILIES_NOT_ARRAY", message="canonical_record_families must be an array")
        return set()
    seen_families: set[str] = set()
    family_to_tables: dict[str, set[str]] = {}
    for index, item in enumerate(value):
        label = f"canonical_record_families[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="FAMILY_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(FAMILY_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_FAMILY_KEY", message=f"missing required {label} key: {key}")
        family = validate_nonblank_string(item.get("record_family"), field_name=f"{label}.record_family", errors=errors, code="INVALID_RECORD_FAMILY")
        if family is None:
            continue
        if family in seen_families:
            add_error(errors, code="DUPLICATE_RECORD_FAMILY", message=f"duplicate record_family: {family}")
            continue
        seen_families.add(family)
        validate_nonblank_string(item.get("description"), field_name=f"{label}.description", errors=errors, code="INVALID_FAMILY_FIELD")
        validate_nonblank_string(item.get("provenance_strategy"), field_name=f"{label}.provenance_strategy", errors=errors, code="INVALID_FAMILY_FIELD")
        validate_nonblank_string(item.get("review_strategy"), field_name=f"{label}.review_strategy", errors=errors, code="INVALID_FAMILY_FIELD")
        family_to_tables[family] = set(
            validate_string_array(item.get("current_sqlite_tables"), field_name=f"{label}.current_sqlite_tables", errors=errors, code="INVALID_FAMILY_TABLES")
        )

    for family in sorted(REQUIRED_RECORD_FAMILIES - seen_families):
        add_error(errors, code="REQUIRED_RECORD_FAMILY_MISSING", message=f"missing required canonical record family: {family}")
    for family, required_tables in REQUIRED_SQLITE_TABLE_MAPPINGS.items():
        if family in family_to_tables and not required_tables.issubset(family_to_tables[family]):
            add_error(
                errors,
                code="SQLITE_TABLE_MAPPING_MISSING",
                message=f"{family} must map required SQLite tables: {', '.join(sorted(required_tables))}",
            )
    return seen_families


def validate_sidecars(value: Any, errors: list[dict[str, Any]], known_families: set[str]) -> None:
    if not isinstance(value, list):
        add_error(errors, code="SIDECARS_NOT_ARRAY", message="append_only_sidecars must be an array")
        return
    seen: set[str] = set()
    for index, item in enumerate(value):
        label = f"append_only_sidecars[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="SIDECAR_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(SIDECAR_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_SIDECAR_KEY", message=f"missing required {label} key: {key}")
        sidecar = validate_nonblank_string(item.get("sidecar"), field_name=f"{label}.sidecar", errors=errors, code="INVALID_SIDECAR")
        if sidecar is None:
            continue
        if sidecar in seen:
            add_error(errors, code="DUPLICATE_SIDECAR", message=f"duplicate sidecar: {sidecar}")
            continue
        seen.add(sidecar)
        validate_nonblank_string(item.get("schema_version"), field_name=f"{label}.schema_version", errors=errors, code="INVALID_SIDECAR")
        validate_nonblank_string(item.get("purpose"), field_name=f"{label}.purpose", errors=errors, code="INVALID_SIDECAR")
        applies = validate_string_array(item.get("applies_to_record_families"), field_name=f"{label}.applies_to_record_families", errors=errors, code="INVALID_SIDECAR")
        for family in applies:
            if family not in known_families:
                add_error(errors, code="UNKNOWN_RECORD_FAMILY_REFERENCE", message=f"{label} references unknown record family: {family}")
    for sidecar in sorted(REQUIRED_SIDECARS - seen):
        add_error(errors, code="REQUIRED_SIDECAR_MISSING", message=f"missing required append-only sidecar: {sidecar}")


def validate_table_inventory(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    supporting = set(
        validate_string_array(
            payload.get("supporting_sqlite_tables"),
            field_name="supporting_sqlite_tables",
            errors=errors,
            code="INVALID_SUPPORTING_SQLITE_TABLES",
        )
    )
    metadata = set(
        validate_string_array(
            payload.get("schema_metadata_tables"),
            field_name="schema_metadata_tables",
            errors=errors,
            code="INVALID_SCHEMA_METADATA_TABLES",
        )
    )
    staging = set(
        validate_string_array(
            payload.get("noncanonical_staging_tables"),
            field_name="noncanonical_staging_tables",
            errors=errors,
            code="INVALID_NONCANONICAL_STAGING_TABLES",
        )
    )

    for table_name in sorted(REQUIRED_SUPPORTING_SQLITE_TABLES - supporting):
        add_error(
            errors,
            code="REQUIRED_SUPPORTING_SQLITE_TABLE_MISSING",
            message=f"missing required supporting SQLite table: {table_name}",
        )
    for table_name in sorted(REQUIRED_SCHEMA_METADATA_TABLES - metadata):
        add_error(
            errors,
            code="REQUIRED_SCHEMA_METADATA_TABLE_MISSING",
            message=f"missing required schema metadata table: {table_name}",
        )
    for table_name in sorted(REQUIRED_NONCANONICAL_STAGING_TABLES - staging):
        add_error(
            errors,
            code="REQUIRED_NONCANONICAL_STAGING_TABLE_MISSING",
            message=f"missing required noncanonical staging table: {table_name}",
        )

    family_table_union: set[str] = set()
    families = payload.get("canonical_record_families")
    if isinstance(families, list):
        for item in families:
            if not isinstance(item, dict):
                continue
            for table_name in item.get("current_sqlite_tables", []):
                if isinstance(table_name, str) and table_name.strip():
                    family_table_union.add(table_name)
    for table_name in sorted(DOCUMENTED_EXPECTED_SQLITE_TABLES - family_table_union):
        add_error(
            errors,
            code="DOCUMENTED_SQLITE_TABLE_MISSING",
            message=f"documented canonical SQLite table is not mapped to any record family: {table_name}",
        )


def validate_migration_stages(value: Any, errors: list[dict[str, Any]], known_families: set[str]) -> None:
    if not isinstance(value, list):
        add_error(errors, code="MIGRATION_STAGES_NOT_ARRAY", message="migration_stages must be an array")
        return
    if not value:
        add_error(errors, code="MIGRATION_STAGES_EMPTY", message="migration_stages must not be empty")
        return
    seen_stage_ids: set[str] = set()
    for index, item in enumerate(value):
        label = f"migration_stages[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="MIGRATION_STAGE_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(MIGRATION_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_MIGRATION_STAGE_KEY", message=f"missing required {label} key: {key}")
        stage_id = validate_nonblank_string(item.get("stage_id"), field_name=f"{label}.stage_id", errors=errors, code="INVALID_MIGRATION_STAGE")
        if stage_id is not None:
            if stage_id in seen_stage_ids:
                add_error(errors, code="DUPLICATE_MIGRATION_STAGE_ID", message=f"duplicate stage_id: {stage_id}")
            seen_stage_ids.add(stage_id)
        validate_nonblank_string(item.get("title"), field_name=f"{label}.title", errors=errors, code="INVALID_MIGRATION_STAGE")
        validate_nonblank_string(item.get("notes"), field_name=f"{label}.notes", errors=errors, code="INVALID_MIGRATION_STAGE")
        validate_string_array(item.get("current_inputs"), field_name=f"{label}.current_inputs", errors=errors, code="INVALID_MIGRATION_STAGE")
        families = validate_string_array(item.get("target_record_families"), field_name=f"{label}.target_record_families", errors=errors, code="INVALID_MIGRATION_STAGE")
        for family in families:
            if family not in known_families:
                add_error(errors, code="UNKNOWN_RECORD_FAMILY_REFERENCE", message=f"{label} references unknown target_record_family: {family}")


def validate_outline(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for key in sorted(REQUIRED_KEYS - set(payload)):
        add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    if payload.get("contract_doc") != CONTRACT_DOC:
        add_error(errors, code="INVALID_CONTRACT_DOC", message=f"contract_doc must equal {CONTRACT_DOC}")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        add_error(errors, code="SUMMARY_NOT_OBJECT", message="summary must be an object")
    else:
        validate_nonblank_string(summary.get("canonical_layer_name"), field_name="summary.canonical_layer_name", errors=errors, code="INVALID_SUMMARY_FIELD")
        if summary.get("presentations_downstream") is not True:
            add_error(errors, code="PRESENTATIONS_NOT_DOWNSTREAM", message="summary.presentations_downstream must be true")
    families = validate_record_families(payload.get("canonical_record_families"), errors)
    validate_sidecars(payload.get("append_only_sidecars"), errors, families)
    validate_table_inventory(payload, errors)
    validate_migration_stages(payload.get("migration_stages"), errors, families)
    return errors


def validate_canonical_graph_model_outline(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, load_exit = load_json_object(target)
    if payload is not None and load_exit == EXIT_PASS:
        errors.extend(validate_outline(payload))
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
    target = Path(args.target)
    report, exit_code = validate_canonical_graph_model_outline(target)
    report_root = resolve_report_root(target, report_root=args.report_root)
    report["scenario"] = args.scenario
    if args.target_id:
        report["target"] = args.target_id
    report["output_artifacts"] = {
        "report_json": display_path(args.report_json) if args.report_json else None,
        "report_text": display_path(args.report_text) if args.report_text else None,
    }
    text_report = render_text_report(report)
    write_json(args.report_json, report, root=report_root)
    write_text(args.report_text, text_report, root=report_root)
    sys.stdout.write(text_report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
