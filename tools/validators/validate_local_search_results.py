#!/usr/bin/env python3
"""Validate local-search-results JSON artifacts."""

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
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.local_search_contract import (  # noqa: E402
    RESULT_CLASS_BY_OBJECT_TYPE,
    RESULTS_SCHEMA_VERSION,
    SEARCH_SCOPE_TO_OBJECT_TYPES,
    VISIBILITY_PROFILES,
)
from tools.common.search_leak_policy import (  # noqa: E402
    contains_private_path,
    contains_secret_marker,
    is_private_note_field,
    is_raw_payload_field,
    is_restricted_public_field,
)

VALIDATOR_NAME = "local_search_results"
CONTRACT_VERSION = "1"
REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "generated_at",
    "source",
    "query",
    "counts",
    "policy",
    "results",
    "warnings",
    "errors",
}
TOP_LEVEL_ALLOWED_KEYS = set(REQUIRED_TOP_LEVEL_KEYS) | {"projection_version"}
SOURCE_ALLOWED_KEYS = {"database_name", "database_fingerprint", "projection_version", "schema_version"}
QUERY_ALLOWED_KEYS = {
    "raw_query",
    "normalized_query",
    "terms",
    "scope",
    "limit",
    "offset",
    "visibility_profile",
}
COUNT_ALLOWED_KEYS = {"returned", "total_estimate", "truncated"}
POLICY_ALLOWED_KEYS = {
    "raw_payload_indexed",
    "full_text_indexed",
    "private_paths_exposed",
    "excluded_families",
}
RESULT_ALLOWED_KEYS = {
    "rank",
    "result_class",
    "result_id",
    "object_type",
    "object_id",
    "title",
    "subtitle",
    "matched_fields",
    "snippets",
    "review_state",
    "publication_state",
    "visibility",
    "score",
    "confidence_score",
    "links",
}
VISIBILITY_ALLOWED_KEYS = {"profile", "suppressed_fields"}
SNIPPET_ALLOWED_KEYS = {"field", "text", "locator", "display_policy"}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a local-search-results JSON file.")
    parser.add_argument("target", help="Path to the local-search-results JSON file.")
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


def validate_nonblank_string(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field_name} must be a non-blank string")
        return None
    return value


def validate_local_search_results_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for key in sorted(set(payload) - TOP_LEVEL_ALLOWED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"top-level contains unexpected key: {key}")
    for key in sorted(REQUIRED_TOP_LEVEL_KEYS - set(payload)):
        add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required top-level key: {key}")

    if payload.get("schema_version") != RESULTS_SCHEMA_VERSION:
        add_error(errors, code="SCHEMA_VERSION_MISMATCH", message=f"schema_version must equal {RESULTS_SCHEMA_VERSION}")

    source = payload.get("source")
    if not isinstance(source, dict):
        add_error(errors, code="SOURCE_NOT_OBJECT", message="source must be an object")
    else:
        for key in sorted(set(source) - SOURCE_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"source contains unexpected key: {key}")
        validate_nonblank_string(source.get("database_name"), field_name="source.database_name", errors=errors, code="INVALID_SOURCE_FIELD")
        validate_nonblank_string(source.get("projection_version"), field_name="source.projection_version", errors=errors, code="INVALID_SOURCE_FIELD")
        schema_field = source.get("schema_version")
        if schema_field is not None and not isinstance(schema_field, (str, int)):
            add_error(errors, code="INVALID_SOURCE_FIELD", message="source.schema_version must be a string, integer, or null")

    query = payload.get("query")
    query_visibility_profile = None
    if not isinstance(query, dict):
        add_error(errors, code="QUERY_NOT_OBJECT", message="query must be an object")
    else:
        for key in sorted(set(query) - QUERY_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"query contains unexpected key: {key}")
        validate_nonblank_string(query.get("raw_query"), field_name="query.raw_query", errors=errors, code="INVALID_QUERY_FIELD")
        validate_nonblank_string(query.get("normalized_query"), field_name="query.normalized_query", errors=errors, code="INVALID_QUERY_FIELD")
        terms = query.get("terms")
        if not isinstance(terms, list) or not terms or not all(isinstance(term, str) and term.strip() for term in terms):
            add_error(errors, code="INVALID_QUERY_FIELD", message="query.terms must be a non-empty array of non-blank strings")
        if query.get("scope") not in SEARCH_SCOPE_TO_OBJECT_TYPES:
            add_error(errors, code="INVALID_QUERY_FIELD", message=f"query.scope must be one of: {', '.join(sorted(SEARCH_SCOPE_TO_OBJECT_TYPES))}")
        limit = query.get("limit")
        if not isinstance(limit, int) or limit < 1:
            add_error(errors, code="INVALID_QUERY_FIELD", message="query.limit must be an integer >= 1")
        offset = query.get("offset")
        if offset is not None and (not isinstance(offset, int) or offset < 0):
            add_error(errors, code="INVALID_QUERY_FIELD", message="query.offset must be an integer >= 0 when present")
        query_visibility_profile = query.get("visibility_profile")
        if query_visibility_profile not in VISIBILITY_PROFILES:
            add_error(errors, code="INVALID_QUERY_FIELD", message="query.visibility_profile must be a known visibility profile")

    result_rows = payload.get("results")
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        add_error(errors, code="COUNTS_NOT_OBJECT", message="counts must be an object")
    else:
        for key in sorted(set(counts) - COUNT_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"counts contains unexpected key: {key}")
        returned = counts.get("returned")
        total_estimate = counts.get("total_estimate")
        truncated = counts.get("truncated")
        if not isinstance(returned, int) or returned < 0:
            add_error(errors, code="INVALID_COUNTS_FIELD", message="counts.returned must be an integer >= 0")
        if total_estimate is not None and (not isinstance(total_estimate, int) or total_estimate < 0):
            add_error(errors, code="INVALID_COUNTS_FIELD", message="counts.total_estimate must be an integer >= 0 or null")
        if not isinstance(truncated, bool):
            add_error(errors, code="INVALID_COUNTS_FIELD", message="counts.truncated must be a boolean")
        if isinstance(returned, int) and isinstance(result_rows, list) and returned != len(result_rows):
            add_error(errors, code="COUNT_MISMATCH", message="counts.returned must equal len(results)")
        if isinstance(total_estimate, int) and isinstance(returned, int) and total_estimate < returned:
            add_error(errors, code="COUNT_MISMATCH", message="counts.total_estimate must be >= counts.returned")

    policy = payload.get("policy")
    if not isinstance(policy, dict):
        add_error(errors, code="POLICY_NOT_OBJECT", message="policy must be an object")
    else:
        for key in sorted(set(policy) - POLICY_ALLOWED_KEYS):
            add_error(errors, code="UNKNOWN_FIELD", message=f"policy contains unexpected key: {key}")
        for field_name in ("raw_payload_indexed", "full_text_indexed", "private_paths_exposed"):
            if policy.get(field_name) is not False:
                add_error(errors, code="POLICY_VIOLATION", message=f"policy.{field_name} must be false")
        excluded_families = policy.get("excluded_families")
        if not isinstance(excluded_families, list) or not all(isinstance(item, str) for item in excluded_families):
            add_error(errors, code="INVALID_POLICY_FIELD", message="policy.excluded_families must be an array of strings")

    if not isinstance(result_rows, list):
        add_error(errors, code="RESULTS_NOT_ARRAY", message="results must be an array")
    else:
        seen_ids: set[str] = set()
        for index, item in enumerate(result_rows):
            label = f"results[{index}]"
            if not isinstance(item, dict):
                add_error(errors, code="RESULT_NOT_OBJECT", message=f"{label} must be an object")
                continue
            for key in sorted(set(item) - RESULT_ALLOWED_KEYS):
                add_error(errors, code="UNKNOWN_FIELD", message=f"{label} contains unexpected key: {key}")
            result_id = validate_nonblank_string(item.get("result_id"), field_name=f"{label}.result_id", errors=errors, code="INVALID_RESULT_FIELD")
            if result_id is not None:
                if result_id in seen_ids:
                    add_error(errors, code="DUPLICATE_RESULT_ID", message=f"duplicate result_id: {result_id}")
                seen_ids.add(result_id)
            rank = item.get("rank")
            if not isinstance(rank, int) or rank < 1:
                add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.rank must be an integer >= 1")
            object_type = item.get("object_type")
            if object_type not in RESULT_CLASS_BY_OBJECT_TYPE:
                add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.object_type is not a known local-search object type")
            if object_type in RESULT_CLASS_BY_OBJECT_TYPE and item.get("result_class") != RESULT_CLASS_BY_OBJECT_TYPE[object_type]:
                add_error(errors, code="RESULT_CLASS_MISMATCH", message=f"{label}.result_class does not match object_type")
            validate_nonblank_string(item.get("object_id"), field_name=f"{label}.object_id", errors=errors, code="INVALID_RESULT_FIELD")
            confidence = item.get("confidence_score")
            if confidence is not None and not isinstance(confidence, (int, float)):
                add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.confidence_score must be a number")
            title = validate_nonblank_string(item.get("title"), field_name=f"{label}.title", errors=errors, code="INVALID_RESULT_FIELD")
            subtitle = item.get("subtitle")
            if isinstance(title, str):
                if contains_secret_marker(title):
                    add_error(errors, code="SECRET_MARKER_EXPOSED", message=f"{label}.title contains a secret-looking value", path=f"{label}.title")
                if contains_private_path(title):
                    add_error(errors, code="PRIVATE_PATH_EXPOSED", message=f"{label}.title must not expose a private path", path=f"{label}.title")
            if isinstance(subtitle, str):
                if contains_secret_marker(subtitle):
                    add_error(errors, code="SECRET_MARKER_EXPOSED", message=f"{label}.subtitle contains a secret-looking value", path=f"{label}.subtitle")
                if contains_private_path(subtitle):
                    add_error(errors, code="PRIVATE_PATH_EXPOSED", message=f"{label}.subtitle must not expose a private path", path=f"{label}.subtitle")

            matched_fields = item.get("matched_fields")
            if not isinstance(matched_fields, list) or not all(isinstance(field, str) and field.strip() for field in matched_fields):
                add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.matched_fields must be an array of non-blank strings")
            snippets = item.get("snippets")
            if not isinstance(snippets, list) or not snippets:
                add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.snippets must be a non-empty array")
            else:
                for snippet_index, snippet in enumerate(snippets):
                    snippet_label = f"{label}.snippets[{snippet_index}]"
                    if not isinstance(snippet, dict):
                        add_error(errors, code="INVALID_RESULT_FIELD", message=f"{snippet_label} must be an object")
                        continue
                    for key in sorted(set(snippet) - SNIPPET_ALLOWED_KEYS):
                        add_error(errors, code="UNKNOWN_FIELD", message=f"{snippet_label} contains unexpected key: {key}")
                    field_name = validate_nonblank_string(
                        snippet.get("field"),
                        field_name=f"{snippet_label}.field",
                        errors=errors,
                        code="INVALID_RESULT_FIELD",
                    )
                    text = validate_nonblank_string(
                        snippet.get("text"),
                        field_name=f"{snippet_label}.text",
                        errors=errors,
                        code="INVALID_RESULT_FIELD",
                    )
                    if snippet.get("display_policy") not in {"public", "local_only", "suppressed"}:
                        add_error(
                            errors,
                            code="INVALID_RESULT_FIELD",
                            message=f"{snippet_label}.display_policy must be public, local_only, or suppressed",
                        )
                    if field_name is not None and is_raw_payload_field(field_name):
                        add_error(
                            errors,
                            code="RAW_PAYLOAD_FIELD_EXPOSED",
                            message=f"{snippet_label}.field must not expose raw payload or full-text fields in search results",
                            path=f"{snippet_label}.field",
                        )
                    if field_name is not None and is_private_note_field(field_name):
                        add_error(
                            errors,
                            code="PRIVATE_NOTE_FIELD_EXPOSED",
                            message=f"{snippet_label}.field must not expose private note fields in search results",
                            path=f"{snippet_label}.field",
                        )
                    if field_name is not None and is_restricted_public_field(field_name) and query_visibility_profile in {"public_preview", "public_release"}:
                        add_error(
                            errors,
                            code="RESTRICTED_EVIDENCE_FIELD_IN_PUBLIC_RESULTS",
                            message=f"{snippet_label}.field must not expose restricted evidence fields in public search results",
                            path=f"{snippet_label}.field",
                        )
                    if text is not None and contains_secret_marker(text):
                        add_error(
                            errors,
                            code="SECRET_MARKER_EXPOSED",
                            message=f"{snippet_label}.text contains a secret-looking value",
                            path=f"{snippet_label}.text",
                        )
                    if text is not None and contains_private_path(text):
                        add_error(
                            errors,
                            code="PRIVATE_PATH_EXPOSED",
                            message=f"{snippet_label}.text must not expose a private path",
                            path=f"{snippet_label}.text",
                        )

            visibility = item.get("visibility")
            if not isinstance(visibility, dict):
                add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.visibility must be an object")
            else:
                for key in sorted(set(visibility) - VISIBILITY_ALLOWED_KEYS):
                    add_error(errors, code="UNKNOWN_FIELD", message=f"{label}.visibility contains unexpected key: {key}")
                visibility_profile = visibility.get("profile")
                if visibility_profile not in VISIBILITY_PROFILES:
                    add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.visibility.profile must be a known visibility profile")
                if query_visibility_profile in VISIBILITY_PROFILES and visibility_profile != query_visibility_profile:
                    add_error(
                        errors,
                        code="VISIBILITY_PROFILE_MISMATCH",
                        message=f"{label}.visibility.profile must match query.visibility_profile",
                        path=f"{label}.visibility.profile",
                    )
                suppressed = visibility.get("suppressed_fields")
                if not isinstance(suppressed, list) or not all(isinstance(field, str) for field in suppressed):
                    add_error(errors, code="INVALID_RESULT_FIELD", message=f"{label}.visibility.suppressed_fields must be an array of strings")
                publication_state = item.get("publication_state")
                if visibility_profile in {"public_preview", "public_release"} and publication_state in {"private_working", "local_only", "blocked"}:
                    add_error(
                        errors,
                        code="PUBLIC_VISIBILITY_CONTRADICTION",
                        message=f"{label} cannot be visible in {visibility_profile} with publication_state {publication_state}",
                        path=f"{label}.publication_state",
                    )

    if not isinstance(payload.get("warnings"), list):
        add_error(errors, code="WARNINGS_NOT_ARRAY", message="warnings must be an array")
    if not isinstance(payload.get("errors"), list):
        add_error(errors, code="ERRORS_NOT_ARRAY", message="errors must be an array")
    return errors


def validate_local_search_results(target: Path) -> tuple[dict[str, Any], int]:
    payload, load_errors, exit_code = load_json_object(target)
    errors = list(load_errors)
    if payload is not None:
        errors.extend(validate_local_search_results_payload(payload))
    status = "pass" if not errors and exit_code == EXIT_PASS else "fail"
    if errors and exit_code == EXIT_PASS:
        exit_code = EXIT_VALIDATION_FAILED
    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "target": display_path(target),
        "status": status,
        "counts": {
            "inspected": 1,
            "accepted": 1 if status == "pass" else 0,
            "rejected": 0 if status == "pass" else 1,
            "deferred": 0,
        },
        "errors": errors,
        "warnings": [],
    }
    return report, exit_code


def main() -> int:
    args = parse_args()
    report, exit_code = validate_local_search_results(Path(args.target))
    rendered = render_text_report(report)
    if args.report_json:
        write_json(Path(args.report_json), report)
    if args.report_text:
        write_text(Path(args.report_text), rendered)
    sys.stdout.write(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
