#!/usr/bin/env python3
"""Minimal JSONL syntax validator for the local validation harness.

The tool reads one UTF-8 JSONL target, emits the shared validation report to
stdout, and writes optional report artifacts when requested. See
tools/validators/README.md for the report contract and exit-code convention.
"""

from __future__ import annotations

import argparse
import json
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

VALIDATOR_NAME = "jsonl_syntax"
CONTRACT_VERSION = "1"


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that a target file is line-delimited JSON with one valid JSON "
            "value per non-empty line."
        )
    )
    parser.add_argument("target", help="Path to the JSONL file to validate.")
    add_report_args(parser)
    return parser.parse_args()


def reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def validate_jsonl(target: Path) -> tuple[dict[str, Any], int]:
    counts = {
        "inspected": 0,
        "accepted": 0,
        "rejected": 0,
        "deferred": 0,
    }
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not target.exists():
        errors.append(
            {
                "code": "INPUT_NOT_FOUND",
                "line": None,
                "message": "input path does not exist",
            }
        )
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    if not target.is_file():
        errors.append(
            {
                "code": "INPUT_NOT_FILE",
                "line": None,
                "message": "input path is not a file",
            }
        )
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    try:
        with target.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    continue
                counts["inspected"] += 1
                try:
                    json.loads(
                        line,
                        object_pairs_hook=no_duplicate_object_pairs,
                        parse_constant=reject_json_constant,
                    )
                except json.JSONDecodeError:
                    counts["rejected"] += 1
                    errors.append(
                        {
                            "code": "JSONL_PARSE_ERROR",
                            "line": line_number,
                            "message": "invalid JSON syntax",
                        }
                    )
                    return {
                        "counts": counts,
                        "errors": errors,
                        "warnings": warnings,
                    }, EXIT_VALIDATION_FAILED
                except DuplicateJsonKeyError as exc:
                    counts["rejected"] += 1
                    errors.append(
                        {
                            "code": "DUPLICATE_JSON_KEY",
                            "line": line_number,
                            "message": str(exc),
                        }
                    )
                    return {
                        "counts": counts,
                        "errors": errors,
                        "warnings": warnings,
                    }, EXIT_VALIDATION_FAILED
                except ValueError as exc:
                    counts["rejected"] += 1
                    errors.append(
                        {
                            "code": "JSONL_PARSE_ERROR",
                            "line": line_number,
                            "message": f"invalid JSON syntax: {exc}",
                        }
                    )
                    return {
                        "counts": counts,
                        "errors": errors,
                        "warnings": warnings,
                    }, EXIT_VALIDATION_FAILED
                counts["accepted"] += 1
    except UnicodeDecodeError:
        errors.append(
            {
                "code": "INPUT_DECODE_ERROR",
                "line": None,
                "message": "input file is not valid UTF-8",
            }
        )
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE
    except OSError:
        errors.append(
            {
                "code": "INPUT_UNREADABLE",
                "line": None,
                "message": "input file could not be read",
            }
        )
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()

    target = Path(args.target)
    result, exit_code = validate_jsonl(target)
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
    text_report = render_text_report(report)
    sys.stdout.write(text_report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
