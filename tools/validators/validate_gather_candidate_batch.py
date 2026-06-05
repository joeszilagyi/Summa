#!/usr/bin/env python3
"""Validate gather candidate batch JSON artifacts."""

from __future__ import annotations

import argparse
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

from tools.common.llm_source_text_wrapper import load_template, parse_wrapped_blocks  # noqa: E402


VALIDATOR_NAME = "gather_candidate_batch"
CONTRACT_VERSION = "1"
SCHEMA_PATH = REPO_ROOT / "config" / "gather_candidate_batch.schema.json"
SCHEMA_VERSION = "gather-candidate-batch.v1"
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


def load_json_object(target: Path, *, label: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
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
        add_error(errors, code="JSON_PARSE_ERROR", line=exc.lineno, message=f"{label} is not valid JSON")
        return None, errors, EXIT_VALIDATION_FAILED
    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message=f"{label} top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED
    return payload, errors, EXIT_PASS


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
            add_error(errors, code="PATTERN_MISMATCH", message=f"value does not match pattern {pattern}", path=path)

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
            add_error(errors, code="NUMBER_TOO_SMALL", message=f"value must be >= {minimum}", path=path)
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            add_error(errors, code="NUMBER_TOO_LARGE", message=f"value must be <= {maximum}", path=path)

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


def validate_invariants(payload: dict[str, Any], target: Path, errors: list[dict[str, Any]]) -> None:
    created_at = payload.get("created_at")
    if isinstance(created_at, str) and not is_rfc3339_datetime(created_at):
        add_error(errors, code="INVALID_CREATED_AT", message="created_at must be an RFC3339 date-time", path="$.created_at")

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
    engine_output_ref = payload.get("engine_output_ref")
    if isinstance(mode, str) and isinstance(engine, dict) and isinstance(provenance, dict):
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
            if provenance.get("stamped_output_path") is not None or provenance.get("stamped_output_footer") is not None:
                add_error(
                    errors,
                    code="DRY_RUN_STAMP_FORBIDDEN",
                    message="mode=dry_run must not claim stamped engine output",
                    path="$.provenance",
                )
        elif mode == "live":
            if engine.get("invoked") is not True or provenance.get("engine_invoked") is not True:
                add_error(
                    errors,
                    code="LIVE_ENGINE_INVOCATION_REQUIRED",
                    message="mode=live must include engine invocation provenance",
                    path="$.mode",
                )
            if engine.get("engine_present") is not True or provenance.get("engine_present") is not True:
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
            if not isinstance(engine_output_ref, str) or not engine_output_ref:
                add_error(
                    errors,
                    code="LIVE_ENGINE_OUTPUT_REF_REQUIRED",
                    message="mode=live must include engine_output_ref",
                    path="$.engine_output_ref",
                )
            if not isinstance(provenance.get("stamped_output_path"), str) or not provenance.get("stamped_output_path"):
                add_error(
                    errors,
                    code="LIVE_STAMPED_OUTPUT_PATH_REQUIRED",
                    message="mode=live must include provenance.stamped_output_path",
                    path="$.provenance.stamped_output_path",
                )
            if not isinstance(provenance.get("stamped_output_footer"), dict):
                add_error(
                    errors,
                    code="LIVE_STAMPED_OUTPUT_FOOTER_REQUIRED",
                    message="mode=live must include provenance.stamped_output_footer",
                    path="$.provenance.stamped_output_footer",
                )

    prompt = payload.get("prompt")
    if isinstance(prompt, dict):
        rendered_prompt = prompt.get("rendered_prompt")
        rendered_prompt_hash = prompt.get("rendered_prompt_hash")
        rendered_prompt_path = prompt.get("rendered_prompt_path")
        if isinstance(rendered_prompt, str):
            import hashlib

            actual_hash = hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()
            if rendered_prompt_hash != actual_hash:
                add_error(
                    errors,
                    code="PROMPT_HASH_MISMATCH",
                    message="rendered_prompt_hash does not match rendered_prompt",
                    path="$.prompt.rendered_prompt_hash",
                )
        if isinstance(rendered_prompt_path, str):
            path = Path(rendered_prompt_path)
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
                    if isinstance(rendered_prompt, str) and file_text != rendered_prompt:
                        add_error(
                            errors,
                            code="PROMPT_PATH_CONTENT_MISMATCH",
                            message="rendered_prompt_path content does not match inline rendered_prompt",
                            path="$.prompt.rendered_prompt_path",
                        )

    iteration_mode = payload.get("iteration_mode")
    cycle_depth = payload.get("cycle_depth")
    previous_run_ids = payload.get("previous_run_ids")
    prior_state = payload.get("prior_state")
    feedback_plan = payload.get("feedback_plan")
    facet = payload.get("facet")
    prompt_bundle = payload.get("prompt_bundle")
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
            import hashlib

            actual_hash = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
            if context_hash != actual_hash:
                add_error(
                    errors,
                    code="PRIOR_STATE_HASH_MISMATCH",
                    message="prior_state.context_hash does not match prior_state.context_text",
                    path="$.prior_state.context_hash",
                )
            if isinstance(prompt, dict) and isinstance(prompt.get("rendered_prompt"), str):
                if context_text not in prompt["rendered_prompt"]:
                    add_error(
                        errors,
                        code="PRIOR_STATE_PROMPT_MISMATCH",
                        message="prior_state.context_text does not appear in rendered_prompt",
                        path="$.prior_state.context_text",
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
        if isinstance(prompt, dict) and isinstance(prompt.get("rendered_prompt"), str):
            rendered_prompt = prompt["rendered_prompt"]
            if "NEXT ACTION SELECTION" not in rendered_prompt:
                add_error(
                    errors,
                    code="FEEDBACK_PLAN_PROMPT_BLOCK_REQUIRED",
                    message="feedback-guided batches must include a NEXT ACTION SELECTION block in rendered_prompt",
                    path="$.prompt.rendered_prompt",
                )
            next_action_id = feedback_plan.get("next_action_id")
            if isinstance(next_action_id, str) and next_action_id not in rendered_prompt:
                add_error(
                    errors,
                    code="FEEDBACK_PLAN_PROMPT_MISMATCH",
                    message="feedback_plan.next_action_id does not appear in rendered_prompt",
                    path="$.feedback_plan.next_action_id",
                )
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
        if isinstance(prompt_bundle, dict) and feedback_plan.get("applied_prompt_bundle_id") != prompt_bundle.get("bundle_id"):
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
    if isinstance(wrapping, dict) and isinstance(prompt, dict) and isinstance(prompt.get("rendered_prompt"), str):
        try:
            template = load_template()
        except RuntimeError as exc:
            add_error(errors, code="WRAPPER_TEMPLATE_LOAD_FAILED", message=str(exc), path="$.source_text_wrapping")
        else:
            if wrapping.get("wrapper_template_id") != template.template_id:
                add_error(
                    errors,
                    code="WRAPPER_TEMPLATE_ID_MISMATCH",
                    message="source_text_wrapping.wrapper_template_id must match the checked-in template",
                    path="$.source_text_wrapping.wrapper_template_id",
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

            parsed_blocks = parse_wrapped_blocks(prompt["rendered_prompt"], template=template)
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
            if len(parsed_blocks) != len(recorded_blocks):
                add_error(
                    errors,
                    code="PROMPT_WRAPPER_BLOCK_COUNT_MISMATCH",
                    message="rendered prompt wrapped block count must equal source_text_wrapping.blocks length",
                    path="$.source_text_wrapping.blocks",
                )
            import hashlib

            for index, actual in enumerate(parsed_blocks):
                if index >= len(recorded_blocks):
                    break
                expected = recorded_blocks[index]
                if not isinstance(expected, dict):
                    continue
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
                actual_hash = hashlib.sha256(actual.source_text.encode("utf-8")).hexdigest()
                if expected.get("sha256") != actual_hash:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_HASH_MISMATCH",
                        message="recorded sha256 does not match the wrapped source text",
                        path=f"$.source_text_wrapping.blocks[{index}].sha256",
                    )
                actual_bytes = len(actual.source_text.encode("utf-8"))
                if expected.get("byte_count") != actual_bytes:
                    add_error(
                        errors,
                        code="WRAPPED_BLOCK_BYTE_COUNT_MISMATCH",
                        message="recorded byte_count does not match the wrapped source text",
                        path=f"$.source_text_wrapping.blocks[{index}].byte_count",
                    )

    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        banned_values = {"accepted", "canonical", "source", "verified", "persisted"}
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
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
            else:
                try:
                    engine_output_text = engine_output_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    add_error(
                        errors,
                        code="ENGINE_OUTPUT_PATH_UNREADABLE",
                        message="engine_output_ref file could not be read",
                        path="$.engine_output_ref",
                    )
                else:
                    if isinstance(raw_engine_output, str) and engine_output_text != raw_engine_output:
                        add_error(
                            errors,
                            code="ENGINE_OUTPUT_CONTENT_MISMATCH",
                            message="engine_output_ref content does not match raw_engine_output",
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
                else:
                    try:
                        stamped_text = stamped_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        add_error(
                            errors,
                            code="STAMPED_OUTPUT_PATH_UNREADABLE",
                            message="stamped_output_path file could not be read",
                            path="$.provenance.stamped_output_path",
                        )
                    else:
                        parsed_footer = parse_stamp_footer(stamped_text)
                        if parsed_footer is None:
                            add_error(
                                errors,
                                code="STAMP_FOOTER_MISSING",
                                message="stamped_output_path file is missing the llm_runner footer",
                                path="$.provenance.stamped_output_path",
                            )
                        else:
                            if stamped_output_footer != parsed_footer:
                                add_error(
                                    errors,
                                    code="STAMP_FOOTER_MISMATCH",
                                    message="stamped_output_footer does not match the stamped output file footer",
                                    path="$.provenance.stamped_output_footer",
                                )
                            run_ts = parsed_footer.get("run_ts")
                            if run_ts is not None and STAMP_RUN_TS_PATTERN.fullmatch(run_ts) is None:
                                add_error(
                                    errors,
                                    code="STAMP_RUN_TS_INVALID",
                                    message="stamped output RUN_TS must match YYYY-MM-DDTHHMMSSZ",
                                    path="$.provenance.stamped_output_footer.run_ts",
                                )

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        add_error(
            errors,
            code="INVALID_SCHEMA_VERSION",
            message=f"schema_version must equal {SCHEMA_VERSION}",
            path="$.schema_version",
        )

    runner_path = payload.get("engine", {}).get("runner_path") if isinstance(payload.get("engine"), dict) else None
    if isinstance(runner_path, str) and not Path(runner_path).is_file():
        add_error(
            errors,
            code="RUNNER_PATH_MISSING",
            message="engine.runner_path does not point to a readable file",
            path="$.engine.runner_path",
        )
    bridge_path = payload.get("engine", {}).get("bridge_path") if isinstance(payload.get("engine"), dict) else None
    if isinstance(bridge_path, str) and not Path(bridge_path).is_file():
        add_error(
            errors,
            code="BRIDGE_PATH_MISSING",
            message="engine.bridge_path does not point to a readable file",
            path="$.engine.bridge_path",
        )


def validate_gather_candidate_batch(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, exit_code = load_json_object(target, label="gather candidate batch")
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code

    counts["inspected"] = 1
    schema, schema_errors, schema_exit = load_json_object(SCHEMA_PATH, label="gather candidate batch schema")
    errors.extend(schema_errors)
    if schema is None:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, schema_exit

    validate_against_schema(payload, schema, root_schema=schema, path="$", errors=errors)
    if not errors:
        validate_invariants(payload, target, errors)

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


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
