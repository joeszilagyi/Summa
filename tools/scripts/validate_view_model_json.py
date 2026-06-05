#!/usr/bin/env python3
"""Validate a product/API view model against the local schema catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_DIR = REPO_ROOT / "config" / "view_models"
REPORT_SCHEMA_VERSION = "view-model-validation-report.v1"
VALIDATOR_NAME = "view_model_json"
CONTRACT_VERSION = "1"
SCHEMA_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one read-only product/API view-model JSON file against "
            "config/view_models/<schema_version>.schema.json."
        )
    )
    parser.add_argument("view_model", help="Path to the view-model JSON file.")
    parser.add_argument(
        "--schema-dir",
        default=str(DEFAULT_SCHEMA_DIR),
        help="Directory containing <schema_version>.schema.json files.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the validation report.",
    )
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def add_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    path: str = "$",
    line: int | None = None,
) -> None:
    errors.append(
        {
            "code": code,
            "line": line,
            "message": message,
            "path": path,
        }
    )


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def matches_json_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def expected_types(rule: dict[str, Any]) -> list[str]:
    raw_type = rule.get("type")
    if isinstance(raw_type, str):
        return [raw_type]
    if isinstance(raw_type, list) and all(isinstance(item, str) for item in raw_type):
        return raw_type
    return []


def reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def load_json_file(path: Path, *, label: str) -> tuple[Any | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    if not path.exists():
        add_error(errors, code="INPUT_NOT_FOUND", message=f"{label} path does not exist")
        return None, errors
    if not path.is_file():
        add_error(errors, code="INPUT_NOT_FILE", message=f"{label} path is not a file")
        return None, errors
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        add_error(errors, code="INPUT_UNREADABLE", message=f"{label} file could not be read")
        return None, errors
    try:
        return json.loads(raw_text, parse_constant=reject_json_constant), errors
    except json.JSONDecodeError as exc:
        add_error(
            errors,
            code="JSON_PARSE_ERROR",
            line=exc.lineno,
            message=f"{label} is not valid JSON",
        )
        return None, errors
    except ValueError as exc:
        add_error(
            errors,
            code="JSON_PARSE_ERROR",
            line=1,
            message=f"{label} is not valid JSON: {exc}",
        )
        return None, errors


def schema_path_for(schema_dir: Path, schema_version: Any, errors: list[dict[str, Any]]) -> Path | None:
    if not isinstance(schema_version, str) or not schema_version.strip():
        add_error(
            errors,
            code="MISSING_SCHEMA_VERSION",
            message="schema_version must be a non-blank string",
            path="$.schema_version",
        )
        return None
    if not SCHEMA_VERSION_PATTERN.fullmatch(schema_version):
        add_error(
            errors,
            code="INVALID_SCHEMA_VERSION",
            message="schema_version is not a safe schema file stem",
            path="$.schema_version",
        )
        return None
    schema_path = schema_dir / f"{schema_version}.schema.json"
    if not schema_path.is_file():
        add_error(
            errors,
            code="UNKNOWN_SCHEMA_VERSION",
            message=f"schema catalog has no entry for {schema_version}",
            path="$.schema_version",
        )
        return None
    return schema_path


def validate_object_against_schema(
    payload: dict[str, Any],
    schema: dict[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    schema_type = schema.get("type")
    if schema_type == "object":
        pass
    elif isinstance(schema_type, str):
        add_error(
            errors,
            code="UNSUPPORTED_SCHEMA_TYPE",
            message=f"view-model validator only supports object schemas, got {schema_type}",
        )

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in payload:
                add_error(
                    errors,
                    code="MISSING_REQUIRED_KEY",
                    message=f"missing required key: {key}",
                    path=f"$.{key}",
                )

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return

    for key, rule in properties.items():
        if not isinstance(key, str) or not isinstance(rule, dict) or key not in payload:
            continue
        value = payload[key]
        property_path = f"$.{key}"

        const_value = rule.get("const")
        if "const" in rule and value != const_value:
            add_error(
                errors,
                code="CONST_MISMATCH",
                message=f"{key} must equal {const_value!r}",
                path=property_path,
            )

        allowed_types = expected_types(rule)
        if allowed_types and not any(matches_json_type(value, type_name) for type_name in allowed_types):
            add_error(
                errors,
                code="TYPE_MISMATCH",
                message=(
                    f"{key} must be type {' or '.join(allowed_types)}, "
                    f"got {json_type_name(value)}"
                ),
                path=property_path,
            )

        enum_values = rule.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            add_error(
                errors,
                code="ENUM_MISMATCH",
                message=f"{key} must be one of {enum_values!r}",
                path=property_path,
            )


def validate_view_model(target: Path, schema_dir: Path) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    schema_path: Path | None = None
    model_schema_version: str | None = None
    inspected = False

    payload, load_errors = load_json_file(target, label="view model")
    errors.extend(load_errors)
    if errors:
        return build_report(
            errors=errors,
            inspected=inspected,
            model_schema_version=model_schema_version,
            schema_path=schema_path,
            target=target,
        )

    inspected = True
    if not isinstance(payload, dict):
        add_error(
            errors,
            code="OBJECT_REQUIRED",
            message="view model top-level JSON value must be an object",
        )
        return build_report(
            errors=errors,
            inspected=inspected,
            model_schema_version=model_schema_version,
            schema_path=schema_path,
            target=target,
        )

    raw_schema_version = payload.get("schema_version")
    model_schema_version = raw_schema_version if isinstance(raw_schema_version, str) else None
    schema_path = schema_path_for(schema_dir, raw_schema_version, errors)
    if schema_path is None:
        return build_report(
            errors=errors,
            inspected=inspected,
            model_schema_version=model_schema_version,
            schema_path=schema_path,
            target=target,
        )

    schema_payload, schema_errors = load_json_file(schema_path, label="schema")
    errors.extend(schema_errors)
    if not errors and not isinstance(schema_payload, dict):
        add_error(errors, code="SCHEMA_INVALID", message="schema top-level JSON value must be an object")

    if not errors and isinstance(schema_payload, dict):
        validate_object_against_schema(payload, schema_payload, errors)

    return build_report(
        errors=errors,
        inspected=inspected,
        model_schema_version=model_schema_version,
        schema_path=schema_path,
        target=target,
    )


def build_report(
    *,
    errors: list[dict[str, Any]],
    inspected: bool,
    model_schema_version: str | None,
    schema_path: Path | None,
    target: Path,
) -> dict[str, Any]:
    ok = not errors
    counts = {
        "inspected": 1 if inspected else 0,
        "accepted": 1 if ok and inspected else 0,
        "rejected": 1 if errors and inspected else 0,
        "deferred": 0,
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "validator": VALIDATOR_NAME,
        "status": "pass" if ok else "fail",
        "ok": ok,
        "target": str(target),
        "model_schema_version": model_schema_version,
        "schema_path": str(schema_path) if schema_path is not None else None,
        "counts": counts,
        "errors": errors,
        "warnings": [],
    }


def text_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\n", " ").replace("\t", " ")


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"schema_version={report['schema_version']}",
        f"validator={report['validator']}",
        f"status={report['status']}",
        f"ok={str(report['ok']).lower()}",
        f"target={report['target']}",
        f"model_schema_version={text_value(report['model_schema_version'])}",
        f"schema_path={text_value(report['schema_path'])}",
        (
            "inspected={inspected} accepted={accepted} "
            "rejected={rejected} deferred={deferred}".format(**report["counts"])
        ),
        f"errors={len(report['errors'])} warnings={len(report['warnings'])}",
    ]
    for index, error in enumerate(report["errors"]):
        line_suffix = f" line={error.get('line')}" if error.get("line") is not None else ""
        lines.append(
            "error[{index}]={code}{line_suffix} path={path} message={message}".format(
                index=index,
                code=error["code"],
                line_suffix=line_suffix,
                path=text_value(error.get("path")),
                message=text_value(error.get("message")),
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    target = resolve_path(args.view_model)
    schema_dir = resolve_path(args.schema_dir)
    report = validate_view_model(target, schema_dir)
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
