#!/usr/bin/env python3
"""Validate prompt fixtures that embed untrusted source text for LLM use."""

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

from tools.common.llm_source_text_wrapper import (  # noqa: E402
    DEFAULT_TEMPLATE_ID,
    TEMPLATE_PATH,
    WrapperContractError,
    load_template,
    parse_wrapped_blocks,
)


VALIDATOR_NAME = "llm_prompt_fixture"
CONTRACT_VERSION = "1"
FIXTURE_SCHEMA_VERSION = "llm-prompt-fixture.v1"
FIXTURE_PATH = "tests/fixtures/validators/llm_prompt_fixture/valid_wrapped_hostile_prompt/inputs/prompt_fixture.json"
PHASES = {"01a", "01r"}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one LLM prompt fixture JSON document.",
        epilog=(
            "Checks that hostile source text stays inside the checked-in untrusted wrapper.\n\n"
            f"Wrapper template: {TEMPLATE_PATH.relative_to(REPO_ROOT).as_posix()}\n"
            f"Example:\n  python3 tools/validators/validate_llm_prompt_fixture.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the prompt fixture JSON document to validate.")
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


def validate_nonblank_string(value: object, *, field_name: str, errors: list[dict[str, Any]], code: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field_name} must be a non-blank string")
        return None
    return value


def validate_prompt_fixture(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, exit_code = load_json_object(target)
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code

    counts["inspected"] = 1
    required_keys = {"schema_version", "prompt_id", "phase", "wrapper_template_id", "prompt_text", "source_blocks"}
    optional_keys = {"notes"}
    for key in sorted(set(payload) - (required_keys | optional_keys)):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(required_keys):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {FIXTURE_SCHEMA_VERSION}")
    validate_nonblank_string(payload.get("prompt_id"), field_name="prompt_id", errors=errors, code="INVALID_PROMPT_ID")
    phase = validate_nonblank_string(payload.get("phase"), field_name="phase", errors=errors, code="INVALID_PHASE")
    if phase is not None and phase not in PHASES:
        add_error(errors, code="INVALID_PHASE", message=f"phase must be one of: {', '.join(sorted(PHASES))}")

    wrapper_template_id = validate_nonblank_string(
        payload.get("wrapper_template_id"),
        field_name="wrapper_template_id",
        errors=errors,
        code="INVALID_TEMPLATE_ID",
    )
    prompt_text = validate_nonblank_string(
        payload.get("prompt_text"),
        field_name="prompt_text",
        errors=errors,
        code="INVALID_PROMPT_TEXT",
    )

    try:
        template = load_template()
    except WrapperContractError as exc:
        add_error(errors, code="TEMPLATE_LOAD_FAILED", message=str(exc))
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED
    if wrapper_template_id is not None and wrapper_template_id != template.template_id:
        add_error(errors, code="UNKNOWN_TEMPLATE_ID", message=f"wrapper_template_id must equal {template.template_id}")

    source_blocks = payload.get("source_blocks")
    normalized_source_blocks: list[dict[str, Any]] = []
    if not isinstance(source_blocks, list) or not source_blocks:
        add_error(errors, code="INVALID_SOURCE_BLOCKS", message="source_blocks must be a non-empty array")
    else:
        for index, item in enumerate(source_blocks):
            if not isinstance(item, dict):
                add_error(errors, code="INVALID_SOURCE_BLOCK", message=f"source_blocks[{index}] must be an object")
                continue
            for field in ("source_ref", "provenance", "source_text"):
                validate_nonblank_string(
                    item.get(field),
                    field_name=f"source_blocks[{index}].{field}",
                    errors=errors,
                    code="INVALID_SOURCE_BLOCK_FIELD",
                )
            hazard_flags = item.get("hazard_flags")
            if not isinstance(hazard_flags, list) or any(not isinstance(flag, str) or not flag.strip() for flag in hazard_flags):
                add_error(errors, code="INVALID_SOURCE_BLOCK_FIELD", message=f"source_blocks[{index}].hazard_flags must be an array of non-blank strings")
                continue
            normalized_source_blocks.append(
                {
                    "source_ref": item.get("source_ref"),
                    "provenance": item.get("provenance"),
                    "hazard_flags": [flag.strip() for flag in hazard_flags],
                    "source_text": item.get("source_text"),
                }
            )

    if prompt_text is not None:
        parsed_blocks = parse_wrapped_blocks(prompt_text, template=template)
        if not parsed_blocks:
            add_error(errors, code="WRAPPED_SOURCE_BLOCK_REQUIRED", message="prompt_text must contain at least one wrapped source block")
        if len(parsed_blocks) != len(normalized_source_blocks):
            add_error(
                errors,
                code="WRAPPED_SOURCE_BLOCK_COUNT_MISMATCH",
                message="wrapped source block count must match source_blocks length",
            )

        outside_text = prompt_text
        for block in parsed_blocks:
            outside_text = outside_text.replace(block.raw_text, "\n")

        if not parsed_blocks:
            for index, expected in enumerate(normalized_source_blocks):
                if expected["source_text"] in outside_text:
                    add_error(errors, code="UNWRAPPED_SOURCE_TEXT", message=f"source_blocks[{index}] source_text appears outside the wrapper")

        for index, expected in enumerate(normalized_source_blocks):
            if index >= len(parsed_blocks):
                break
            actual = parsed_blocks[index]
            if actual.source_ref != expected["source_ref"]:
                add_error(errors, code="SOURCE_REF_MISMATCH", message=f"wrapped block {index} source_ref does not match source_blocks[{index}]")
            if actual.provenance != expected["provenance"]:
                add_error(errors, code="PROVENANCE_MISMATCH", message=f"wrapped block {index} provenance does not match source_blocks[{index}]")
            if list(actual.hazard_flags) != expected["hazard_flags"]:
                add_error(errors, code="HAZARD_FLAGS_MISMATCH", message=f"wrapped block {index} hazard_flags do not match source_blocks[{index}]")
            if actual.instruction_negation != template.instruction_negation_guidance:
                add_error(errors, code="INSTRUCTION_NEGATION_MISSING", message=f"wrapped block {index} must use the checked-in instruction-negation guidance")
            if actual.source_text != expected["source_text"]:
                add_error(errors, code="SOURCE_TEXT_MISMATCH", message=f"wrapped block {index} source_text does not match source_blocks[{index}]")
            if expected["source_text"] in outside_text:
                add_error(errors, code="UNWRAPPED_SOURCE_TEXT", message=f"source_blocks[{index}] source_text appears outside the wrapper")

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_prompt_fixture(target)
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
