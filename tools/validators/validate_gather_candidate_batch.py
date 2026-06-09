#!/usr/bin/env python3
"""Validate gather candidate batch JSON artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from common import (  # type: ignore
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
    from tools.validators.common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        is_rfc3339_datetime,
        render_text_report,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.candidate_feedback_contract import (  # noqa: E402
    compact_next_action_prompt_payload,
    compact_prior_state_prompt_payload,
)
from tools.common.llm_source_text_wrapper import load_template, parse_wrapped_blocks  # noqa: E402
from tools.common.source_text_profile import (  # noqa: E402
    build_source_text_profile,
)
from tools.scripts import resolve_subject_runtime  # noqa: E402

VALIDATOR_NAME = "gather_candidate_batch"
CONTRACT_VERSION = "1"
SCHEMA_PATH = REPO_ROOT / "config" / "gather_candidate_batch.schema.json"
SCHEMA_VERSION = "gather-candidate-batch.v1"
SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION, "gather-candidate-batch.v0"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STAMP_RUN_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one gather candidate batch JSON artifact against the checked-in "
            "gather-candidate-batch.v1 contract."
        ),
        epilog=(
            "Example:\n"
            "  python3 tools/validators/validate_gather_candidate_batch.py "
            "runs/gather/<run-id>/gather-candidate-batch.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the gather candidate batch JSON file.")
    add_report_args(parser)
    return parser.parse_args()


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


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def resolve_prompt_path(raw_path: str, *, batch_path: Path) -> Path:
    candidate_path = Path(raw_path)
    batch_dir = batch_path.parent.resolve()
    if not candidate_path.is_absolute():
        candidate_path = batch_dir / candidate_path
    candidate_path = candidate_path.expanduser().resolve()
    try:
        candidate_path.relative_to(batch_dir)
    except ValueError as exc:
        raise ValueError("rendered_prompt_path must remain inside the batch directory") from exc
    return candidate_path


def load_json_object(
    target: Path, *, label: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []
    if not target.exists():
        add_error(errors, code="INPUT_NOT_FOUND", message=f"{label} path does not exist")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    if not target.is_file():
        add_error(errors, code="INPUT_NOT_FILE", message=f"{label} path is not a file")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        add_error(errors, code="INPUT_UNREADABLE", message=f"{label} file could not be read")
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
        add_error(
            errors, code="JSON_PARSE_ERROR", line=exc.lineno, message=f"{label} is not valid JSON"
        )
        return None, errors, EXIT_VALIDATION_FAILED
    if not isinstance(payload, dict):
        add_error(
            errors,
            code="OBJECT_REQUIRED",
            message=f"{label} top-level JSON value must be an object",
        )
        return None, errors, EXIT_VALIDATION_FAILED
    return payload, errors, EXIT_PASS


def load_validated_gather_candidate_batch(
    target: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any], int]:
    payload, errors, exit_code = load_json_object(target, label="gather candidate batch")
    if payload is None:
        return (
            None,
            {
                "counts": {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0},
                "errors": errors,
                "warnings": [],
            },
            exit_code,
        )

    report, report_exit_code = validate_gather_candidate_batch_payload(payload, target=target)
    return payload, report, report_exit_code


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
    if isinstance(value, int) and not isinstance(value, bool):
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_candidate_extraction_record(
    candidate: dict[str, Any],
    *,
    expected_candidate_type: str | None,
    errors: list[dict[str, Any]],
    path: str,
) -> None:
    text = candidate.get("text")
    if not isinstance(text, str):
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_REQUIRED",
            message="raw candidate text must be a JSON object encoded as a string",
            path=f"{path}.text",
        )
        return
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except DuplicateJsonKeyError as exc:
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_DUPLICATE_KEY",
            message=str(exc),
            path=f"{path}.text",
        )
        return
    except NonStandardJsonConstantError as exc:
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_NON_STANDARD_CONSTANT",
            message=str(exc),
            path=f"{path}.text",
        )
        return
    except json.JSONDecodeError:
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_NOT_JSON",
            message="raw candidate text must be a JSON object",
            path=f"{path}.text",
        )
        return
    if not isinstance(parsed, dict):
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_OBJECT_REQUIRED",
            message="raw candidate text must be a JSON object",
            path=f"{path}.text",
        )
        return
    required_keys = ("candidate_type", "locator", "claim", "confidence", "reason", "source_span")
    for key in required_keys:
        if key not in parsed:
            add_error(
                errors,
                code="RAW_CANDIDATE_TEXT_MISSING_FIELD",
                message=f"raw candidate text JSON object must include {key}",
                path=f"{path}.text.{key}",
            )
    candidate_type = parsed.get("candidate_type")
    if not isinstance(candidate_type, str) or not candidate_type.strip():
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_CANDIDATE_TYPE_INVALID",
            message="raw candidate text JSON object must include a non-blank candidate_type",
            path=f"{path}.text.candidate_type",
        )
    elif expected_candidate_type is not None and candidate_type != expected_candidate_type:
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_CANDIDATE_TYPE_MISMATCH",
            message="raw candidate text candidate_type must match the facet candidate type hint",
            path=f"{path}.text.candidate_type",
        )
    claim = parsed.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_CLAIM_INVALID",
            message="raw candidate text JSON object must include a non-blank claim",
            path=f"{path}.text.claim",
        )
    reason = parsed.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        add_error(
            errors,
            code="RAW_CANDIDATE_TEXT_REASON_INVALID",
            message="raw candidate text JSON object must include a non-blank reason",
            path=f"{path}.text.reason",
        )


def resolve_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported schema ref: {ref}")
    current: Any = root_schema
    for segment in ref[2:].split("/"):
        if not isinstance(current, dict) or segment not in current:
            raise ValueError(f"schema ref not found: {ref}")
        current = current[segment]
    if not isinstance(current, dict):
        raise ValueError(f"schema ref does not point to an object schema: {ref}")
    return current


def validate_against_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
) -> None:
    if "$ref" in schema:
        validate_against_schema(
            value,
            resolve_ref(schema["$ref"], root_schema),
            root_schema=root_schema,
            path=path,
            errors=errors,
        )
        return

    expected_type = schema.get("type")
    expected_types: list[str] = []
    if isinstance(expected_type, str):
        expected_types = [expected_type]
    elif isinstance(expected_type, list) and all(isinstance(item, str) for item in expected_type):
        expected_types = list(expected_type)
    if expected_types and not any(matches_json_type(value, item) for item in expected_types):
        add_error(
            errors,
            code="TYPE_MISMATCH",
            message=f"value must be type {' or '.join(expected_types)}, got {json_type_name(value)}",
            path=path,
        )
        return

    if "const" in schema and value != schema["const"]:
        add_error(
            errors,
            code="CONST_MISMATCH",
            message=f"value must equal {schema['const']!r}",
            path=path,
        )
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        add_error(
            errors,
            code="ENUM_MISMATCH",
            message=f"value must be one of {enum_values!r}",
            path=path,
        )

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            add_error(
                errors,
                code="STRING_TOO_SHORT",
                message=f"string length must be at least {min_length}",
                path=path,
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            add_error(
                errors,
                code="PATTERN_MISMATCH",
                message=f"value does not match pattern {pattern}",
                path=path,
            )

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            add_error(
                errors,
                code="ARRAY_TOO_SHORT",
                message=f"array must contain at least {min_items} item(s)",
                path=path,
            )
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            add_error(
                errors,
                code="ARRAY_TOO_LONG",
                message=f"array must contain at most {max_items} item(s)",
                path=path,
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_against_schema(
                    item,
                    item_schema,
                    root_schema=root_schema,
                    path=f"{path}[{index}]",
                    errors=errors,
                )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            add_error(
                errors, code="NUMBER_TOO_SMALL", message=f"value must be >= {minimum}", path=path
            )
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            add_error(
                errors, code="NUMBER_TOO_LARGE", message=f"value must be <= {maximum}", path=path
            )

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    add_error(
                        errors,
                        code="MISSING_REQUIRED_KEY",
                        message=f"missing required key: {key}",
                        path=f"{path}.{key}",
                    )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            additional_properties = schema.get("additionalProperties")
            if additional_properties is False:
                for key in sorted(value):
                    if key not in properties:
                        add_error(
                            errors,
                            code="UNKNOWN_FIELD",
                            message=f"unexpected field: {key}",
                            path=f"{path}.{key}",
                        )
            for key, property_schema in properties.items():
                if key not in value or not isinstance(property_schema, dict):
                    continue
                validate_against_schema(
                    value[key],
                    property_schema,
                    root_schema=root_schema,
                    path=f"{path}.{key}",
                    errors=errors,
                )


def _read_text_if_present(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def compact_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_json_payload(payload: dict[str, Any]) -> str:
    return compact_json_text(payload)


def parse_stamp_footer(text: str) -> dict[str, str] | None:
    footer_delimiter = "\n---\n"
    footer_prefix = f"{footer_delimiter}RUN_META_VERSION: "
    start = text.rfind(footer_prefix)
    if start < 0:
        return None
    footer_text = text[start + len(footer_delimiter) :]
    values: dict[str, str] = {}
    for line in footer_text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            return None
        key, raw_value = line.split(":", 1)
        values[key.strip()] = raw_value.strip()
    required = ("RUN_META_VERSION", "GENERATED_BY", "MODEL", "PLACE", "FACET", "PHASE", "RUN_TS")
    if any(key not in values for key in required):
        return None
    return {
        "run_meta_version": values["RUN_META_VERSION"],
        "generated_by": values["GENERATED_BY"],
        "model": values["MODEL"],
        "place": values["PLACE"],
        "facet": values["FACET"],
        "phase": values["PHASE"],
        "run_ts": values["RUN_TS"],
    }


def validate_invariants(
    payload: dict[str, Any], target: Path, errors: list[dict[str, Any]]
) -> None:
    created_at = payload.get("created_at")
    if isinstance(created_at, str) and not is_rfc3339_datetime(created_at):
        add_error(
            errors,
            code="INVALID_CREATED_AT",
            message="created_at must be an RFC3339 date-time",
            path="$.created_at",
        )

    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        timestamp = provenance.get("timestamp")
        if isinstance(timestamp, str) and not is_rfc3339_datetime(timestamp):
            add_error(
                errors,
                code="INVALID_PROVENANCE_TIMESTAMP",
                message="provenance.timestamp must be an RFC3339 date-time",
                path="$.provenance.timestamp",
            )

    mode = payload.get("mode")
    engine = payload.get("engine")
    raw_engine_output = payload.get("raw_engine_output")
    raw_engine_output_hash = payload.get("raw_engine_output_hash")
    engine_output_ref = payload.get("engine_output_ref")
    feedback_plan = payload.get("feedback_plan")
    if isinstance(mode, str) and isinstance(engine, dict) and isinstance(provenance, dict):
        if raw_engine_output_hash is not None and (
            not isinstance(raw_engine_output_hash, str)
            or SHA256_PATTERN.fullmatch(raw_engine_output_hash) is None
        ):
            add_error(
                errors,
                code="INVALID_RAW_ENGINE_OUTPUT_HASH",
                message="raw_engine_output_hash must be a 64-character lowercase SHA-256 hex digest",
                path="$.raw_engine_output_hash",
            )
        if mode == "dry_run":
            if engine.get("invoked") is True or provenance.get("engine_invoked") is True:
                add_error(
                    errors,
                    code="DRY_RUN_ENGINE_INVOCATION_FORBIDDEN",
                    message="mode=dry_run must not claim an engine invocation",
                    path="$.mode",
                )
            if raw_engine_output not in (None, ""):
                add_error(
                    errors,
                    code="DRY_RUN_RAW_OUTPUT_FORBIDDEN",
                    message="mode=dry_run must not include raw_engine_output",
                    path="$.raw_engine_output",
                )
            if engine_output_ref is not None:
                add_error(
                    errors,
                    code="DRY_RUN_ENGINE_OUTPUT_REF_FORBIDDEN",
                    message="mode=dry_run must not include engine_output_ref",
                    path="$.engine_output_ref",
                )
            if (
                provenance.get("stamped_output_path") is not None
                or provenance.get("stamped_output_footer") is not None
            ):
                add_error(
                    errors,
                    code="DRY_RUN_STAMP_FORBIDDEN",
                    message="mode=dry_run must not claim stamped engine output",
                    path="$.provenance",
                )
        elif mode == "live":
            should_call_llm = None
            if isinstance(feedback_plan, dict):
                next_action = feedback_plan.get("next_action")
                if isinstance(next_action, dict):
                    should_call_llm = next_action.get("should_call_llm")
            if should_call_llm is False:
                if engine.get("invoked") is True or provenance.get("engine_invoked") is True:
                    add_error(
                        errors,
                        code="LIVE_ENGINE_INVOCATION_FORBIDDEN",
                        message="mode=live must not claim engine invocation when the feedback plan skips LLM execution",
                        path="$.mode",
                    )
                if engine.get("engine_present") is True or provenance.get("engine_present") is True:
                    add_error(
                        errors,
                        code="LIVE_ENGINE_PRESENCE_FORBIDDEN",
                        message="mode=live must not claim engine presence when the feedback plan skips LLM execution",
                        path="$.engine",
                    )
                if raw_engine_output not in (None, ""):
                    add_error(
                        errors,
                        code="LIVE_RAW_OUTPUT_FORBIDDEN",
                        message="mode=live must not include raw_engine_output when the feedback plan skips LLM execution",
                        path="$.raw_engine_output",
                    )
                if raw_engine_output_hash not in (None, ""):
                    add_error(
                        errors,
                        code="LIVE_RAW_OUTPUT_HASH_FORBIDDEN",
                        message="mode=live must not include raw_engine_output_hash when the feedback plan skips LLM execution",
                        path="$.raw_engine_output_hash",
                    )
                if engine_output_ref is not None:
                    add_error(
                        errors,
                        code="LIVE_ENGINE_OUTPUT_REF_FORBIDDEN",
                        message="mode=live must not include engine_output_ref when the feedback plan skips LLM execution",
                        path="$.engine_output_ref",
                    )
                if (
                    provenance.get("stamped_output_path") is not None
                    or provenance.get("stamped_output_hash") is not None
                    or provenance.get("stamped_output_footer_hash") is not None
                    or provenance.get("stamped_output_footer") is not None
                ):
                    add_error(
                        errors,
                        code="LIVE_STAMP_FORBIDDEN",
                        message="mode=live must not claim stamped engine output when the feedback plan skips LLM execution",
                        path="$.provenance",
                    )
            else:
                cache_hit = engine.get("cache_hit") is True or provenance.get("engine_cache_hit") is True
                if cache_hit:
                    if engine.get("invoked") is not False or provenance.get("engine_invoked") is not False:
                        add_error(
                            errors,
                            code="LIVE_ENGINE_CACHE_HIT_INVOCATION_MISMATCH",
                            message="mode=live cache hits must record engine_invoked=false",
                            path="$.mode",
                        )
                else:
                    if (
                        engine.get("invoked") is not True
                        or provenance.get("engine_invoked") is not True
                    ):
                        add_error(
                            errors,
                            code="LIVE_ENGINE_INVOCATION_REQUIRED",
                            message="mode=live must include engine invocation provenance",
                            path="$.mode",
                        )
                if (
                    engine.get("engine_present") is not True
                    or provenance.get("engine_present") is not True
                ):
                    add_error(
                        errors,
                        code="LIVE_ENGINE_PRESENCE_REQUIRED",
                        message="mode=live must include engine_present=true",
                        path="$.engine",
                    )
                if not isinstance(raw_engine_output, str) or not raw_engine_output:
                    add_error(
                        errors,
                        code="LIVE_RAW_OUTPUT_REQUIRED",
                        message="mode=live must include raw_engine_output",
                        path="$.raw_engine_output",
                    )
                if (
                    not isinstance(raw_engine_output_hash, str)
                    or SHA256_PATTERN.fullmatch(raw_engine_output_hash) is None
                ):
                    add_error(
                        errors,
                        code="LIVE_RAW_OUTPUT_HASH_REQUIRED",
                        message="mode=live must include raw_engine_output_hash",
                        path="$.raw_engine_output_hash",
                    )
                if not isinstance(engine_output_ref, str) or not engine_output_ref:
                    add_error(
                        errors,
                        code="LIVE_ENGINE_OUTPUT_REF_REQUIRED",
                        message="mode=live must include engine_output_ref",
                        path="$.engine_output_ref",
                    )
                if not isinstance(provenance.get("stamped_output_path"), str) or not provenance.get(
                    "stamped_output_path"
                ):
                    add_error(
                        errors,
                        code="LIVE_STAMPED_OUTPUT_PATH_REQUIRED",
                        message="mode=live must include provenance.stamped_output_path",
                        path="$.provenance.stamped_output_path",
                    )
                if (
                    not isinstance(provenance.get("stamped_output_hash"), str)
                    or SHA256_PATTERN.fullmatch(provenance.get("stamped_output_hash") or "") is None
                ):
                    add_error(
                        errors,
                        code="LIVE_STAMPED_OUTPUT_HASH_REQUIRED",
                        message="mode=live must include provenance.stamped_output_hash",
                        path="$.provenance.stamped_output_hash",
                    )
                if (
                    not isinstance(provenance.get("stamped_output_footer_hash"), str)
                    or SHA256_PATTERN.fullmatch(provenance.get("stamped_output_footer_hash") or "")
                    is None
                ):
                    add_error(
                        errors,
                        code="LIVE_STAMPED_OUTPUT_FOOTER_HASH_REQUIRED",
                        message="mode=live must include provenance.stamped_output_footer_hash",
                        path="$.provenance.stamped_output_footer_hash",
                    )
                if not isinstance(provenance.get("stamped_output_footer"), dict):
                    add_error(
                        errors,
                        code="LIVE_STAMPED_OUTPUT_FOOTER_REQUIRED",
                        message="mode=live must include provenance.stamped_output_footer",
                        path="$.provenance.stamped_output_footer",
                    )

    prompt = payload.get("prompt")
    prompt_text: str | None = None
    if isinstance(prompt, dict):
        rendered_prompt_hash = prompt.get("rendered_prompt_hash")
        rendered_prompt_path = prompt.get("rendered_prompt_path")
        prompt_budget = prompt.get("budget")
        path: Path | None = None
        if isinstance(rendered_prompt_path, str):
            try:
                path = resolve_prompt_path(rendered_prompt_path, batch_path=target)
            except ValueError as exc:
                add_error(
                    errors,
                    code="PROMPT_PATH_OUTSIDE_BATCH",
                    message=str(exc),
                    path="$.prompt.rendered_prompt_path",
                )
        if path is not None:
            if not path.is_file():
                add_error(
                    errors,
                    code="PROMPT_PATH_MISSING",
                    message="rendered_prompt_path does not point to a readable file",
                    path="$.prompt.rendered_prompt_path",
                )
            else:
                try:
                    file_text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    add_error(
                        errors,
                        code="PROMPT_PATH_UNREADABLE",
                        message="rendered_prompt_path file could not be read",
                        path="$.prompt.rendered_prompt_path",
                    )
                else:
                    prompt_text = file_text
                    file_bytes = file_text.encode("utf-8")
                    actual_hash = hashlib.sha256(file_bytes).hexdigest()
                    if rendered_prompt_hash != actual_hash:
                        add_error(
                            errors,
                            code="PROMPT_PATH_CONTENT_MISMATCH",
                            message="rendered_prompt_hash does not match rendered_prompt_path content",
                            path="$.prompt.rendered_prompt_path",
                        )
        if isinstance(prompt_budget, dict):
            section_byte_counts = prompt_budget.get("section_byte_counts")
            if not isinstance(section_byte_counts, dict):
                add_error(
                    errors,
                    code="PROMPT_BUDGET_SECTION_COUNTS_REQUIRED",
                    message="prompt.budget.section_byte_counts must be an object",
                    path="$.prompt.budget.section_byte_counts",
                )
            else:
                counted_total = 0
                for section_name, byte_count in section_byte_counts.items():
                    if not isinstance(section_name, str) or not section_name:
                        add_error(
                            errors,
                            code="PROMPT_BUDGET_SECTION_NAME_INVALID",
                            message="prompt.budget.section_byte_counts keys must be non-empty strings",
                            path="$.prompt.budget.section_byte_counts",
                        )
                        continue
                    if not isinstance(byte_count, int) or byte_count < 0:
                        add_error(
                            errors,
                            code="PROMPT_BUDGET_SECTION_BYTE_COUNT_INVALID",
                            message="prompt.budget.section_byte_counts values must be integers >= 0",
                            path=f"$.prompt.budget.section_byte_counts.{section_name}",
                        )
                        continue
                    counted_total += byte_count
                prompt_total_byte_count = prompt_budget.get("prompt_total_byte_count")
                if not isinstance(prompt_total_byte_count, int) or prompt_total_byte_count < 0:
                    add_error(
                        errors,
                        code="PROMPT_BUDGET_TOTAL_REQUIRED",
                        message="prompt.budget.prompt_total_byte_count must be an integer >= 0",
                        path="$.prompt.budget.prompt_total_byte_count",
                    )
                else:
                    if counted_total != prompt_total_byte_count:
                        add_error(
                            errors,
                            code="PROMPT_BUDGET_TOTAL_MISMATCH",
                            message="prompt.budget.prompt_total_byte_count must equal the sum of prompt section byte counts",
                            path="$.prompt.budget.prompt_total_byte_count",
                        )
                    if prompt_text is not None and len(prompt_text.encode("utf-8")) != prompt_total_byte_count:
                        add_error(
                            errors,
                            code="PROMPT_BUDGET_PROMPT_MISMATCH",
                            message="prompt.budget.prompt_total_byte_count must match the rendered prompt size",
                            path="$.prompt.budget.prompt_total_byte_count",
                        )
            section_order = prompt_budget.get("section_order")
            if not isinstance(section_order, list) or any(
                not isinstance(item, str) or not item for item in section_order
            ):
                add_error(
                    errors,
                    code="PROMPT_BUDGET_SECTION_ORDER_REQUIRED",
                    message="prompt.budget.section_order must be an array of non-empty strings",
                    path="$.prompt.budget.section_order",
                )
            source_block_count = prompt_budget.get("source_block_count")
            if not isinstance(source_block_count, int) or source_block_count < 0:
                add_error(
                    errors,
                    code="PROMPT_BUDGET_SOURCE_BLOCK_COUNT_REQUIRED",
                    message="prompt.budget.source_block_count must be an integer >= 0",
                    path="$.prompt.budget.source_block_count",
                )

    iteration_mode = payload.get("iteration_mode")
    cycle_depth = payload.get("cycle_depth")
    previous_run_ids = payload.get("previous_run_ids")
    prior_state = payload.get("prior_state")
    facet = payload.get("facet")
    prompt_bundle = payload.get("prompt_bundle")
    cycle_depth_value = cycle_depth if isinstance(cycle_depth, int) else 1
    if cycle_depth is not None and not isinstance(cycle_depth, int):
        add_error(
            errors,
            code="INVALID_CYCLE_DEPTH",
            message="cycle_depth must be an integer when present",
            path="$.cycle_depth",
        )
    if isinstance(cycle_depth, int) and cycle_depth < 1:
        add_error(
            errors,
            code="INVALID_CYCLE_DEPTH",
            message="cycle_depth must be at least 1",
            path="$.cycle_depth",
        )
    if previous_run_ids is not None and not isinstance(previous_run_ids, list):
        add_error(
            errors,
            code="INVALID_PREVIOUS_RUN_IDS",
            message="previous_run_ids must be an array when present",
            path="$.previous_run_ids",
        )
    if isinstance(previous_run_ids, list):
        for index, run_id in enumerate(previous_run_ids):
            if not isinstance(run_id, str) or not run_id:
                add_error(
                    errors,
                    code="INVALID_PREVIOUS_RUN_ID",
                    message="previous_run_ids entries must be non-empty strings",
                    path=f"$.previous_run_ids[{index}]",
                )
    if iteration_mode == "prior_state" and not isinstance(prior_state, dict):
        add_error(
            errors,
            code="PRIOR_STATE_REQUIRED",
            message="iteration_mode=prior_state requires a prior_state object",
            path="$.prior_state",
        )
    if iteration_mode == "prior_state" and not isinstance(cycle_depth, int):
        add_error(
            errors,
            code="CYCLE_DEPTH_REQUIRED",
            message="iteration_mode=prior_state requires cycle_depth",
            path="$.cycle_depth",
        )
    if iteration_mode == "one_shot" and prior_state is not None:
        add_error(
            errors,
            code="ONE_SHOT_PRIOR_STATE_FORBIDDEN",
            message="iteration_mode=one_shot must not include prior_state",
            path="$.prior_state",
        )
    if isinstance(prior_state, dict):
        context_text = prior_state.get("context_text")
        context_hash = prior_state.get("context_hash")
        if isinstance(context_text, str):
            actual_hash = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
            if context_hash != actual_hash:
                add_error(
                    errors,
                    code="PRIOR_STATE_HASH_MISMATCH",
                    message="prior_state.context_hash does not match prior_state.context_text",
                    path="$.prior_state.context_hash",
                )
        record_counts = prior_state.get("record_counts")
        if isinstance(record_counts, dict):
            for family_key, counts in record_counts.items():
                if not isinstance(counts, dict):
                    continue
                total = counts.get("total")
                selected = counts.get("selected")
                rendered = counts.get("rendered")
                if (
                    isinstance(total, int)
                    and isinstance(selected, int)
                    and isinstance(rendered, int)
                    and (selected > total or rendered > selected)
                ):
                    add_error(
                        errors,
                        code="PRIOR_STATE_COUNT_ORDER_INVALID",
                        message="prior_state counts must satisfy rendered <= selected <= total",
                        path=f"$.prior_state.record_counts.{family_key}",
                    )
        if isinstance(provenance, dict):
            if provenance.get("prior_state_enabled") is not True:
                add_error(
                    errors,
                    code="PRIOR_STATE_PROVENANCE_FLAG_REQUIRED",
                    message="provenance.prior_state_enabled must be true when prior_state is present",
                    path="$.provenance.prior_state_enabled",
                )
            if provenance.get("prior_state_hash") != prior_state.get("context_hash"):
                add_error(
                    errors,
                    code="PRIOR_STATE_PROVENANCE_HASH_MISMATCH",
                    message="provenance.prior_state_hash must match prior_state.context_hash",
                    path="$.provenance.prior_state_hash",
                )
            if provenance.get("cycle_depth") != cycle_depth:
                add_error(
                    errors,
                    code="PRIOR_STATE_CYCLE_DEPTH_MISMATCH",
                    message="provenance.cycle_depth must match cycle_depth",
                    path="$.provenance.cycle_depth",
                )
    elif isinstance(provenance, dict):
        if provenance.get("prior_state_enabled") not in (False, None):
            add_error(
                errors,
                code="ONE_SHOT_PRIOR_STATE_FLAG_INVALID",
                message="one-shot batches must not claim prior-state provenance",
                path="$.provenance.prior_state_enabled",
            )
        if provenance.get("prior_state_hash") is not None:
            add_error(
                errors,
                code="ONE_SHOT_PRIOR_STATE_HASH_FORBIDDEN",
                message="one-shot batches must not include provenance.prior_state_hash",
                path="$.provenance.prior_state_hash",
            )

    if isinstance(feedback_plan, dict):
        if isinstance(feedback_plan.get("plan_hash"), str) and not SHA256_PATTERN.fullmatch(
            feedback_plan["plan_hash"]
        ):
            add_error(
                errors,
                code="INVALID_FEEDBACK_PLAN_HASH",
                message="feedback_plan.plan_hash must be a lowercase hex sha256",
                path="$.feedback_plan.plan_hash",
            )
        if isinstance(facet, dict) and feedback_plan.get("applied_facet") != facet.get("name"):
            add_error(
                errors,
                code="FEEDBACK_PLAN_FACET_MISMATCH",
                message="feedback_plan.applied_facet must match facet.name",
                path="$.feedback_plan.applied_facet",
            )
        if isinstance(prompt_bundle, dict) and feedback_plan.get(
            "applied_prompt_bundle_id"
        ) != prompt_bundle.get("bundle_id"):
            add_error(
                errors,
                code="FEEDBACK_PLAN_BUNDLE_MISMATCH",
                message="feedback_plan.applied_prompt_bundle_id must match prompt_bundle.bundle_id",
                path="$.feedback_plan.applied_prompt_bundle_id",
            )
        if isinstance(provenance, dict):
            if provenance.get("feedback_plan_enabled") is not True:
                add_error(
                    errors,
                    code="FEEDBACK_PLAN_PROVENANCE_FLAG_REQUIRED",
                    message="provenance.feedback_plan_enabled must be true when feedback_plan is present",
                    path="$.provenance.feedback_plan_enabled",
                )
            if provenance.get("feedback_plan_hash") != feedback_plan.get("plan_hash"):
                add_error(
                    errors,
                    code="FEEDBACK_PLAN_PROVENANCE_HASH_MISMATCH",
                    message="provenance.feedback_plan_hash must match feedback_plan.plan_hash",
                    path="$.provenance.feedback_plan_hash",
                )
            if provenance.get("next_action_id") != feedback_plan.get("next_action_id"):
                add_error(
                    errors,
                    code="FEEDBACK_PLAN_NEXT_ACTION_MISMATCH",
                    message="provenance.next_action_id must match feedback_plan.next_action_id",
                    path="$.provenance.next_action_id",
                )
            if provenance.get("scoring_policy_id") != feedback_plan.get("scoring_policy_id"):
                add_error(
                    errors,
                    code="FEEDBACK_PLAN_POLICY_MISMATCH",
                    message="provenance.scoring_policy_id must match feedback_plan.scoring_policy_id",
                    path="$.provenance.scoring_policy_id",
                )
    elif isinstance(provenance, dict):
        if provenance.get("feedback_plan_enabled") not in (False, None):
            add_error(
                errors,
                code="ONE_SHOT_FEEDBACK_PLAN_FLAG_INVALID",
                message="batches without feedback_plan must not claim feedback-plan provenance",
                path="$.provenance.feedback_plan_enabled",
            )
        for field_name in ("feedback_plan_hash", "next_action_id", "scoring_policy_id"):
            if provenance.get(field_name) is not None:
                add_error(
                    errors,
                    code="ONE_SHOT_FEEDBACK_PLAN_METADATA_FORBIDDEN",
                    message=f"batches without feedback_plan must not include provenance.{field_name}",
                    path=f"$.provenance.{field_name}",
                )

    wrapping = payload.get("source_text_wrapping")
    if isinstance(wrapping, dict) and isinstance(prompt_text, str):
        try:
            template = load_template()
        except RuntimeError as exc:
            add_error(
                errors,
                code="WRAPPER_TEMPLATE_LOAD_FAILED",
                message=str(exc),
                path="$.source_text_wrapping",
            )
        else:
            if wrapping.get("wrapper_template_id") != template.template_id:
                add_error(
                    errors,
                    code="WRAPPER_TEMPLATE_ID_MISMATCH",
                    message="source_text_wrapping.wrapper_template_id must match the checked-in template",
                    path="$.source_text_wrapping.wrapper_template_id",
                )
            wrapper_template_path = wrapping.get("wrapper_template_path")
            if isinstance(wrapper_template_path, str):
                template_path = Path(wrapper_template_path)
                if not template_path.is_absolute():
                    template_path = (REPO_ROOT / template_path).resolve()
                if not template_path.is_file():
                    add_error(
                        errors,
                        code="WRAPPER_TEMPLATE_PATH_MISSING",
                        message="source_text_wrapping.wrapper_template_path does not point to a readable file",
                        path="$.source_text_wrapping.wrapper_template_path",
                    )
                else:
                    actual_wrapper_hash = sha256_file(template_path)
                    if wrapping.get("wrapper_template_hash") != actual_wrapper_hash:
                        add_error(
                            errors,
                            code="WRAPPER_TEMPLATE_HASH_MISMATCH",
                            message="source_text_wrapping.wrapper_template_hash does not match the wrapper template file",
                            path="$.source_text_wrapping.wrapper_template_hash",
                        )
            if wrapping.get("begin_delimiter") != template.begin_delimiter:
                add_error(
                    errors,
                    code="WRAPPER_BEGIN_DELIMITER_MISMATCH",
                    message="source_text_wrapping.begin_delimiter must match the checked-in template",
                    path="$.source_text_wrapping.begin_delimiter",
                )
            if wrapping.get("end_delimiter") != template.end_delimiter:
                add_error(
                    errors,
                    code="WRAPPER_END_DELIMITER_MISMATCH",
                    message="source_text_wrapping.end_delimiter must match the checked-in template",
                    path="$.source_text_wrapping.end_delimiter",
                )
            source_section_marker = "\nWrapped source text blocks:\n"
            source_section_start = prompt_text.find(source_section_marker)
            source_section_text_start = (
                source_section_start + len(source_section_marker)
                if source_section_start != -1
                else 0
            )
            source_section_text = prompt_text[source_section_text_start:]
            if source_section_text.endswith("\n"):
                source_section_text = source_section_text[:-1]
            if source_section_start == -1:
                add_error(
                    errors,
                    code="PROMPT_SOURCE_SECTION_MISSING",
                    message="rendered prompt must include wrapped source text blocks",
                    path="$.prompt.rendered_prompt",
                )
            parsed_blocks = parse_wrapped_blocks(prompt_text, template=template)
            recorded_blocks = wrapping.get("blocks")
            if not isinstance(recorded_blocks, list):
                return
            if wrapping.get("source_block_count") != len(recorded_blocks):
                add_error(
                    errors,
                    code="WRAPPER_BLOCK_COUNT_MISMATCH",
                    message="source_block_count must equal the number of recorded blocks",
                    path="$.source_text_wrapping.source_block_count",
                )

            parsed_source_blocks = [
                block for block in parsed_blocks if block.source_ref.startswith("source:")
            ]
            if len(parsed_source_blocks) != len(recorded_blocks):
                add_error(
                    errors,
                    code="WRAPPED_SOURCE_BLOCK_COUNT_MISMATCH",
                    message="recorded source block count does not match parsed source blocks",
                    path="$.source_text_wrapping.source_block_count",
                )

            for index, expected in enumerate(recorded_blocks):
                if not isinstance(expected, dict):
                    continue
                start_offset = expected.get("start_offset")
                end_offset = expected.get("end_offset")
                if not isinstance(start_offset, int) or not isinstance(end_offset, int):
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_OFFSET_MISSING",
                        message="recorded block offsets are required",
                        path=f"$.source_text_wrapping.blocks[{index}]",
                    )
                    continue
                if (
                    start_offset < 0
                    or end_offset < start_offset
                    or end_offset > len(source_section_text)
                ):
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_OFFSET_MISMATCH",
                        message="recorded block offsets do not match the rendered prompt",
                        path=f"$.source_text_wrapping.blocks[{index}]",
                    )
                    continue
                expected_start = source_section_text_start + start_offset
                expected_end = source_section_text_start + end_offset
                if index >= len(parsed_source_blocks):
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_PARSE_FAILED",
                        message="recorded block offsets do not delimit a wrapped source block",
                        path=f"$.source_text_wrapping.blocks[{index}]",
                    )
                    continue
                actual = parsed_source_blocks[index]
                if actual.start_offset != expected_start or actual.end_offset != expected_end:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_OFFSET_MISMATCH",
                        message="recorded block offsets do not match the rendered prompt source block",
                        path=f"$.source_text_wrapping.blocks[{index}]",
                    )
                if expected.get("source_ref") != actual.source_ref:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_SOURCE_REF_MISMATCH",
                        message="recorded source_ref does not match the rendered prompt block",
                        path=f"$.source_text_wrapping.blocks[{index}].source_ref",
                    )
                if expected.get("provenance") != actual.provenance:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_PROVENANCE_MISMATCH",
                        message="recorded provenance does not match the rendered prompt block",
                        path=f"$.source_text_wrapping.blocks[{index}].provenance",
                    )
                if expected.get("hazard_flags") != list(actual.hazard_flags):
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_HAZARD_FLAGS_MISMATCH",
                        message="recorded hazard_flags do not match the rendered prompt block",
                        path=f"$.source_text_wrapping.blocks[{index}].hazard_flags",
                    )
                actual_source_bytes = actual.source_text.encode("utf-8")
                expected_profile = expected.get("source_profile")
                if not isinstance(expected_profile, dict):
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_SOURCE_PROFILE_REQUIRED",
                        message="recorded source_profile is required",
                        path=f"$.source_text_wrapping.blocks[{index}].source_profile",
                    )
                    continue
                actual_profile = build_source_text_profile(
                    actual.source_text,
                    byte_count=len(actual_source_bytes),
                )
                if expected_profile != actual_profile:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_SOURCE_PROFILE_MISMATCH",
                        message="recorded source_profile does not match the rendered prompt block",
                        path=f"$.source_text_wrapping.blocks[{index}].source_profile",
                    )
                actual_hash = hashlib.sha256(actual_source_bytes).hexdigest()
                if expected.get("sha256") != actual_hash:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_HASH_MISMATCH",
                        message="recorded sha256 does not match the wrapped source text",
                        path=f"$.source_text_wrapping.blocks[{index}].sha256",
                    )
                actual_bytes = len(actual_source_bytes)
                if expected.get("byte_count") != actual_bytes:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_BYTE_COUNT_MISMATCH",
                        message="recorded byte_count does not match the wrapped source text",
                        path=f"$.source_text_wrapping.blocks[{index}].byte_count",
                    )

            parsed_metadata_blocks = {
                block.source_ref: block
                for block in parsed_blocks
                if block.source_ref.startswith("metadata:")
            }
            subject_payload = payload.get("subject")
            if isinstance(subject_payload, dict):
                manifest_path = subject_payload.get("manifest_path")
                if isinstance(manifest_path, str):
                    subject_path = Path(manifest_path)
                    if not subject_path.is_absolute():
                        subject_path = (REPO_ROOT / subject_path).resolve()
                    if not subject_path.is_file():
                        add_error(
                            errors,
                            code="SUBJECT_MANIFEST_PATH_MISSING",
                            message="subject.manifest_path does not point to a readable file",
                            path="$.subject.manifest_path",
                        )
                    else:
                        manifest_hash = subject_payload.get("manifest_hash")
                        actual_manifest_hash = sha256_file(subject_path)
                        if manifest_hash != actual_manifest_hash:
                            add_error(
                                errors,
                                code="SUBJECT_MANIFEST_HASH_MISMATCH",
                                message="subject.manifest_hash does not match subject.manifest_path content",
                                path="$.subject.manifest_hash",
                            )
                        try:
                            manifest = resolve_subject_runtime.load_subject_manifest(subject_path)
                        except resolve_subject_runtime.ResolutionError as exc:
                            add_error(
                                errors,
                                code="SUBJECT_MANIFEST_INVALID",
                                message=str(exc),
                                path="$.subject.manifest_path",
                            )
                        else:
                            if subject_payload.get("subject_id") != manifest["subject_id"]:
                                add_error(
                                    errors,
                                    code="SUBJECT_ID_MISMATCH",
                                    message="subject.subject_id must match the loaded subject manifest",
                                    path="$.subject.subject_id",
                                )
                            expected_subject = {
                                "subject_id": manifest["subject_id"],
                                "display_name": manifest["display_name"],
                                "domain_pack": manifest["domain_pack"],
                                "scope_statement": manifest["scope_statement"],
                            }
                            expected_subject_text = render_json_payload(expected_subject)
                            actual_subject_block = parsed_metadata_blocks.get("metadata:subject")
                            if actual_subject_block is None:
                                add_error(
                                    errors,
                                    code="UNTRUSTED_SUBJECT_METADATA_MISSING",
                                    message="rendered prompt must include an untrusted subject metadata block",
                                    path="$.prompt.rendered_prompt",
                                )
                            else:
                                if actual_subject_block.provenance != "subject manifest metadata":
                                    add_error(
                                        errors,
                                        code="UNTRUSTED_SUBJECT_METADATA_PROVENANCE_MISMATCH",
                                        message="subject metadata provenance must match the wrapped prompt contract",
                                        path="$.prompt.rendered_prompt",
                                    )
                                if actual_subject_block.source_text != expected_subject_text:
                                    add_error(
                                        errors,
                                        code="UNTRUSTED_SUBJECT_METADATA_MISMATCH",
                                        message="subject metadata block must serialize the rendered subject payload as inert JSON",
                                        path="$.prompt.rendered_prompt",
                                    )
                else:
                    add_error(
                        errors,
                        code="SUBJECT_MANIFEST_PATH_REQUIRED",
                        message="subject.manifest_path must be present",
                        path="$.subject.manifest_path",
                    )

            domain_pack_payload = payload.get("domain_pack")
            if isinstance(domain_pack_payload, dict):
                domain_pack_path = domain_pack_payload.get("path")
                if isinstance(domain_pack_path, str):
                    pack_path = Path(domain_pack_path)
                    if not pack_path.is_absolute():
                        pack_path = (REPO_ROOT / pack_path).resolve()
                    if not pack_path.is_file():
                        add_error(
                            errors,
                            code="DOMAIN_PACK_PATH_MISSING",
                            message="domain_pack.path does not point to a readable file",
                            path="$.domain_pack.path",
                        )
                    else:
                        actual_hash = sha256_file(pack_path)
                        if domain_pack_payload.get("sha256") != actual_hash:
                            add_error(
                                errors,
                                code="DOMAIN_PACK_HASH_MISMATCH",
                                message="domain_pack.sha256 does not match domain_pack.path content",
                                path="$.domain_pack.sha256",
                            )
                    if isinstance(facet, dict) and domain_pack_payload.get("selected_facet") != facet.get(
                        "name"
                    ):
                        add_error(
                            errors,
                            code="DOMAIN_PACK_SELECTED_FACET_MISMATCH",
                            message="domain_pack.selected_facet must match facet.name",
                            path="$.domain_pack.selected_facet",
                        )
                else:
                    add_error(
                        errors,
                        code="DOMAIN_PACK_PATH_REQUIRED",
                        message="domain_pack.path must be present",
                        path="$.domain_pack.path",
                    )

            if isinstance(prompt_bundle, dict):
                selected_template_file = prompt_bundle.get("selected_template_file")
                if isinstance(selected_template_file, str):
                    template_path = Path(selected_template_file)
                    if not template_path.is_absolute():
                        template_path = (REPO_ROOT / template_path).resolve()
                    if not template_path.is_file():
                        add_error(
                            errors,
                            code="PROMPT_BUNDLE_TEMPLATE_PATH_MISSING",
                            message="prompt_bundle.selected_template_file does not point to a readable file",
                            path="$.prompt_bundle.selected_template_file",
                        )
                    else:
                        actual_template_hash = sha256_file(template_path)
                        if prompt_bundle.get("selected_template_hash") != actual_template_hash:
                            add_error(
                                errors,
                                code="PROMPT_BUNDLE_TEMPLATE_HASH_MISMATCH",
                                message="prompt_bundle.selected_template_hash does not match the selected template file",
                                path="$.prompt_bundle.selected_template_hash",
                            )
                else:
                    add_error(
                        errors,
                        code="PROMPT_BUNDLE_TEMPLATE_PATH_REQUIRED",
                        message="prompt_bundle.selected_template_file must be present",
                        path="$.prompt_bundle.selected_template_file",
                    )
                if isinstance(domain_pack_payload, dict):
                    if domain_pack_payload.get("prompt_bundle_id") != prompt_bundle.get("bundle_id"):
                        add_error(
                            errors,
                            code="DOMAIN_PACK_PROMPT_BUNDLE_ID_MISMATCH",
                            message="domain_pack.prompt_bundle_id must match prompt_bundle.bundle_id",
                            path="$.domain_pack.prompt_bundle_id",
                        )
                    if domain_pack_payload.get("prompt_bundle_key") != prompt_bundle.get("bundle_key"):
                        add_error(
                            errors,
                            code="DOMAIN_PACK_PROMPT_BUNDLE_KEY_MISMATCH",
                            message="domain_pack.prompt_bundle_key must match prompt_bundle.bundle_key",
                            path="$.domain_pack.prompt_bundle_key",
                        )

            if isinstance(feedback_plan, dict):
                next_action = feedback_plan.get("next_action")
                if isinstance(next_action, dict):
                    expected_next_action_text = render_json_payload(
                        compact_next_action_prompt_payload(next_action)
                    )
                    expected_next_action_hash = hashlib.sha256(
                        expected_next_action_text.encode("utf-8")
                    ).hexdigest()
                    expected_next_action_byte_count = len(expected_next_action_text.encode("utf-8"))
                    if (
                        feedback_plan.get("next_action_rendered_source_ref")
                        != "metadata:feedback-plan"
                    ):
                        add_error(
                            errors,
                            code="UNTRUSTED_FEEDBACK_PLAN_METADATA_SOURCE_REF_MISMATCH",
                            message="feedback-plan metadata source_ref must use the wrapped prompt contract",
                            path="$.feedback_plan.next_action_rendered_source_ref",
                        )
                    if (
                        feedback_plan.get("next_action_rendered_provenance")
                        != "candidate feedback plan next action"
                    ):
                        add_error(
                            errors,
                            code="UNTRUSTED_FEEDBACK_PLAN_METADATA_PROVENANCE_MISMATCH",
                            message="feedback-plan metadata provenance must match the wrapped prompt contract",
                            path="$.feedback_plan.next_action_rendered_provenance",
                        )
                    if feedback_plan.get("next_action_rendered_hash") != expected_next_action_hash:
                        add_error(
                            errors,
                            code="UNTRUSTED_FEEDBACK_PLAN_METADATA_HASH_MISMATCH",
                            message="feedback-plan metadata hash must match the rendered next_action payload",
                            path="$.feedback_plan.next_action_rendered_hash",
                        )
                    if (
                        feedback_plan.get("next_action_rendered_byte_count")
                        != expected_next_action_byte_count
                    ):
                        add_error(
                            errors,
                            code="UNTRUSTED_FEEDBACK_PLAN_METADATA_BYTE_COUNT_MISMATCH",
                            message="feedback-plan metadata byte_count must match the rendered next_action payload",
                            path="$.feedback_plan.next_action_rendered_byte_count",
                        )

            if isinstance(prior_state, dict):
                prior_state_payload = {
                    key: value
                    for key, value in prior_state.items()
                    if not key.startswith("prior_state_rendered_")
                }
                expected_prior_state_text = render_json_payload(
                    compact_prior_state_prompt_payload(
                        prior_state_payload,
                        cycle_depth=cycle_depth_value,
                    )
                )
                expected_prior_state_hash = hashlib.sha256(
                    expected_prior_state_text.encode("utf-8")
                ).hexdigest()
                expected_prior_state_byte_count = len(expected_prior_state_text.encode("utf-8"))
                if prior_state.get("prior_state_rendered_source_ref") != "metadata:prior-state":
                    add_error(
                        errors,
                        code="UNTRUSTED_PRIOR_STATE_METADATA_SOURCE_REF_MISMATCH",
                        message="prior-state metadata source_ref must use the wrapped prompt contract",
                        path="$.prior_state.prior_state_rendered_source_ref",
                    )
                if (
                    prior_state.get("prior_state_rendered_provenance")
                    != "prior canonical state context"
                ):
                    add_error(
                        errors,
                        code="UNTRUSTED_PRIOR_STATE_METADATA_PROVENANCE_MISMATCH",
                        message="prior-state metadata provenance must match the wrapped prompt contract",
                        path="$.prior_state.prior_state_rendered_provenance",
                    )
                if prior_state.get("prior_state_rendered_hash") != expected_prior_state_hash:
                    add_error(
                        errors,
                        code="UNTRUSTED_PRIOR_STATE_METADATA_HASH_MISMATCH",
                        message="prior-state metadata hash must match the rendered prior_state payload",
                        path="$.prior_state.prior_state_rendered_hash",
                    )
                if (
                    prior_state.get("prior_state_rendered_byte_count")
                    != expected_prior_state_byte_count
                ):
                    add_error(
                        errors,
                        code="UNTRUSTED_PRIOR_STATE_METADATA_BYTE_COUNT_MISMATCH",
                        message="prior-state metadata byte_count must match the rendered prior_state payload",
                        path="$.prior_state.prior_state_rendered_byte_count",
                    )

    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        banned_values = {"accepted", "canonical", "source", "verified", "persisted"}
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            if candidate.get("candidate_type") == "raw_candidate_text" and isinstance(facet, dict):
                validate_candidate_extraction_record(
                    candidate,
                    expected_candidate_type=facet.get("candidate_type_hint")
                    if isinstance(facet.get("candidate_type_hint"), str)
                    else None,
                    errors=errors,
                    path=f"$.candidates[{index}]",
                )
            for key in ("review_status", "persistence_status", "origin"):
                value = candidate.get(key)
                if isinstance(value, str) and value.lower() in banned_values:
                    add_error(
                        errors,
                        code="CANDIDATE_STATE_FORBIDDEN",
                        message=f"candidate {key} uses a forbidden final-state term: {value}",
                        path=f"$.candidates[{index}].{key}",
                    )

    if isinstance(mode, str) and mode == "live":
        if isinstance(engine_output_ref, str):
            engine_output_path = Path(engine_output_ref)
            if not engine_output_path.is_file():
                add_error(
                    errors,
                    code="ENGINE_OUTPUT_PATH_MISSING",
                    message="engine_output_ref does not point to a readable file",
                    path="$.engine_output_ref",
                )
        if isinstance(provenance, dict):
            stamped_output_path = provenance.get("stamped_output_path")
            stamped_output_footer = provenance.get("stamped_output_footer")
            if isinstance(stamped_output_path, str):
                stamped_path = Path(stamped_output_path)
                if not stamped_path.is_file():
                    add_error(
                        errors,
                        code="STAMPED_OUTPUT_PATH_MISSING",
                        message="stamped_output_path does not point to a readable file",
                        path="$.provenance.stamped_output_path",
                    )
            if isinstance(stamped_output_footer, dict):
                parsed_footer_hash = hashlib.sha256(
                    json.dumps(stamped_output_footer, ensure_ascii=False, sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if provenance.get("stamped_output_footer_hash") != parsed_footer_hash:
                    add_error(
                        errors,
                        code="STAMP_FOOTER_HASH_MISMATCH",
                        message="stamped_output_footer_hash does not match the stamped output footer metadata",
                        path="$.provenance.stamped_output_footer_hash",
                    )
                run_ts = stamped_output_footer.get("run_ts")
                if run_ts is not None and STAMP_RUN_TS_PATTERN.fullmatch(str(run_ts)) is None:
                    add_error(
                        errors,
                        code="STAMP_RUN_TS_INVALID",
                        message="stamped output RUN_TS must match YYYY-MM-DDTHHMMSSZ",
                        path="$.provenance.stamped_output_footer.run_ts",
                    )

    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        add_error(
            errors,
            code="INVALID_SCHEMA_VERSION",
            message=f"schema_version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}",
            path="$.schema_version",
        )

    runner_path = (
        payload.get("engine", {}).get("runner_path")
        if isinstance(payload.get("engine"), dict)
        else None
    )
    if isinstance(runner_path, str) and not Path(runner_path).is_file():
        add_error(
            errors,
            code="RUNNER_PATH_MISSING",
            message="engine.runner_path does not point to a readable file",
            path="$.engine.runner_path",
        )
    bridge_path = (
        payload.get("engine", {}).get("bridge_path")
        if isinstance(payload.get("engine"), dict)
        else None
    )
    if isinstance(bridge_path, str) and not Path(bridge_path).is_file():
        add_error(
            errors,
            code="BRIDGE_PATH_MISSING",
            message="engine.bridge_path does not point to a readable file",
            path="$.engine.bridge_path",
        )


def validate_gather_candidate_batch_payload(
    payload: dict[str, Any],
    *,
    target: Path,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 1, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    schema, schema_errors, schema_exit = load_json_object(
        SCHEMA_PATH, label="gather candidate batch schema"
    )
    errors.extend(schema_errors)
    if schema is None:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, schema_exit

    if payload.get("schema_version") != SCHEMA_VERSION:
        schema = json.loads(json.dumps(schema))
        schema_version_schema = schema.get("properties", {}).get("schema_version", {})
        if (
            isinstance(schema_version_schema, dict)
            and schema_version_schema.get("const") == SCHEMA_VERSION
        ):
            schema_version_schema["const"] = payload.get("schema_version")
    validate_against_schema(payload, schema, root_schema=schema, path="$", errors=errors)
    if not errors:
        validate_invariants(payload, target, errors)

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def validate_gather_candidate_batch(target: Path) -> tuple[dict[str, Any], int]:
    _, report, exit_code = load_validated_gather_candidate_batch(target)
    return report, exit_code


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_gather_candidate_batch(target)
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
