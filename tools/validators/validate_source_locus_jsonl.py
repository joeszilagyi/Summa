#!/usr/bin/env python3
"""Validate Phase 3A source-locus JSONL exports.

The validator is read-only for its target JSONL file. It checks each row
against the closed field set in config/source_locus.schema.json and may write
optional JSON/text reports when those paths are provided.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common import (
    EXIT_INPUT_UNAVAILABLE,
    EXIT_PASS,
    EXIT_VALIDATION_FAILED,
    add_report_args,
    display_path,
    emit_report,
    render_text_report,
)


VALIDATOR_NAME = "source_locus_jsonl"
CONTRACT_VERSION = "1"
SOURCE_LOCUS_SCHEMA_PATH = "config/source_locus.schema.json"
VALID_FIXTURE_PATH = (
    "tests/fixtures/validators/source_locus_jsonl/valid_minimal/inputs/"
    "DemoTopic_source_loci.jsonl"
)

ALLOWED_LOCUS_TYPES = {
    "government_agency",
    "archive",
    "library",
    "museum",
    "university_repository",
    "journal",
    "magazine",
    "newspaper",
    "publisher_catalog",
    "database",
    "forum",
    "podcast",
    "broadcaster",
    "video_platform",
    "map_repository",
    "bibliography",
    "search_engine",
    "aggregator",
    "local_collection",
    "unknown",
}

ALLOWED_QUERY_FAMILIES = {
    "government_records",
    "academic_literature",
    "newspapers",
    "magazines",
    "books",
    "archives",
    "libraries",
    "maps",
    "film_tv_documentary",
    "radio_podcast",
    "forums_community",
    "web_general",
    "bibliography_chaining",
    "local_document_ingest",
    "unknown",
}

ALLOWED_REVIEW_STATES = {"accepted", "needs_review", "demoted", "deprecated", "rejected"}

REQUIRED_KEYS = {
    "locus_id",
    "topic_id",
    "display_name",
    "locus_type",
    "query_family",
    "parent_locus_id",
    "parent_org_id",
    "jurisdiction_place_id",
    "languages",
    "time_coverage_start",
    "time_coverage_end",
    "access_class",
    "access_url",
    "catalog_url",
    "archive_url",
    "access_notes",
    "rights_posture",
    "refetchability_status",
    "discovery_method",
    "discovery_source",
    "discovered_at",
    "discovered_by",
    "confidence_score",
    "review_state",
    "productivity_queries_run",
    "productivity_leads_returned",
    "productivity_unique_leads",
    "productivity_captures_made",
    "productivity_works_promoted",
    "productivity_score",
    "last_queried_at",
    "last_productive_at",
    "cooldown_until",
    "is_deprecated",
    "deprecation_reason",
    "notes",
}

NONBLANK_STRING_FIELDS = {
    "locus_id",
    "topic_id",
    "display_name",
    "locus_type",
    "query_family",
    "access_class",
    "rights_posture",
    "refetchability_status",
    "discovery_method",
    "discovery_source",
    "discovered_at",
    "discovered_by",
    "review_state",
}

NULLABLE_STRING_FIELDS = {
    "parent_locus_id",
    "parent_org_id",
    "jurisdiction_place_id",
    "time_coverage_start",
    "time_coverage_end",
    "access_url",
    "catalog_url",
    "archive_url",
    "access_notes",
    "last_queried_at",
    "last_productive_at",
    "cooldown_until",
    "deprecation_reason",
    "notes",
}

COUNTER_FIELDS = {
    "productivity_queries_run",
    "productivity_leads_returned",
    "productivity_unique_leads",
    "productivity_captures_made",
    "productivity_works_promoted",
}

SCORE_FIELDS = {"confidence_score", "productivity_score"}
URL_FIELDS = {"access_url", "catalog_url", "archive_url"}
LOCUS_ID_PATTERN = re.compile(r"^locus:[a-z0-9][a-z0-9:_-]*$")


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one source-locus JSONL file for the Phase 3A local discovery contract.",
        epilog=(
            "Reads the target file and writes validation output to stdout.\n"
            "Optional --report-json/--report-text paths are created atomically.\n\n"
            f"Schema: {SOURCE_LOCUS_SCHEMA_PATH}\n"
            "Example:\n"
            f"  python3 tools/validators/validate_source_locus_jsonl.py {VALID_FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the source-locus JSONL file to validate.")
    add_report_args(parser)
    return parser.parse_args()


def fail(
    counts: dict[str, int],
    *,
    code: str,
    line: int | None,
    message: str,
) -> tuple[dict[str, Any], int]:
    counts["rejected"] += 1
    return (
        {
            "counts": counts,
            "errors": [{"code": code, "line": line, "message": message}],
            "warnings": [],
        },
        EXIT_VALIDATION_FAILED,
    )


def is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_nullable_string(value: Any) -> bool:
    return value is None or isinstance(value, str)


def is_locus_id(value: Any) -> bool:
    return isinstance(value, str) and LOCUS_ID_PATTERN.fullmatch(value) is not None


def is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_unit_interval_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def absolute_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def validate_source_locus_jsonl(target: Path) -> tuple[dict[str, Any], int]:
    counts = {
        "inspected": 0,
        "accepted": 0,
        "rejected": 0,
        "deferred": 0,
    }
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_locus_ids: set[str] = set()

    if not target.exists():
        errors.append({"code": "INPUT_NOT_FOUND", "line": None, "message": "input path does not exist"})
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    if not target.is_file():
        errors.append({"code": "INPUT_NOT_FILE", "line": None, "message": "input path is not a file"})
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    if "_source_loci" not in target.name or not target.name.endswith(".jsonl"):
        return fail(
            counts,
            code="SOURCE_LOCUS_FILE_NAME_INVALID",
            line=None,
            message="target file name must contain _source_loci and end with .jsonl",
        )

    try:
        with target.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                counts["inspected"] += 1
                try:
                    row = json.loads(
                        line,
                        object_pairs_hook=no_duplicate_object_pairs,
                        parse_constant=reject_json_constant,
                    )
                except json.JSONDecodeError as exc:
                    return fail(
                        counts,
                        code="JSONL_PARSE_ERROR",
                        line=line_number,
                        message=f"invalid JSON syntax at column {exc.colno}: {exc.msg}",
                    )
                except DuplicateJsonKeyError as exc:
                    return fail(
                        counts,
                        code="DUPLICATE_JSON_KEY",
                        line=line_number,
                        message=str(exc),
                    )
                except ValueError as exc:
                    return fail(
                        counts,
                        code="JSONL_PARSE_ERROR",
                        line=line_number,
                        message=f"invalid JSON syntax: {exc}",
                    )

                if not isinstance(row, dict):
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_RECORD_NOT_OBJECT",
                        line=line_number,
                        message="source-locus row must be a JSON object",
                    )

                missing_keys = [key for key in sorted(REQUIRED_KEYS) if key not in row]
                if missing_keys:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_REQUIRED_KEY_MISSING",
                        line=line_number,
                        message=f"missing required keys: {', '.join(missing_keys)}",
                    )

                unexpected_keys = [key for key in sorted(row) if key not in REQUIRED_KEYS]
                if unexpected_keys:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_UNEXPECTED_KEY",
                        line=line_number,
                        message=f"unexpected keys: {', '.join(unexpected_keys)}",
                    )

                for key in sorted(NONBLANK_STRING_FIELDS):
                    if not is_nonblank_string(row[key]):
                        return fail(
                            counts,
                            code="SOURCE_LOCUS_REQUIRED_FIELD_BLANK",
                            line=line_number,
                            message=f"{key} must be a nonblank string",
                        )

                for key in sorted(NULLABLE_STRING_FIELDS):
                    if not is_nullable_string(row[key]):
                        return fail(
                            counts,
                            code="SOURCE_LOCUS_FIELD_TYPE_INVALID",
                            line=line_number,
                            message=f"{key} must be a string or null",
                        )

                if not is_locus_id(row["locus_id"]):
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_ID_INVALID",
                        line=line_number,
                        message=f"locus_id must match {LOCUS_ID_PATTERN.pattern}",
                    )

                if row["locus_id"] in seen_locus_ids:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_ID_DUPLICATE",
                        line=line_number,
                        message="locus_id appears more than once in the file",
                    )
                seen_locus_ids.add(row["locus_id"])

                if row["parent_locus_id"] is not None and not is_locus_id(row["parent_locus_id"]):
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_PARENT_ID_INVALID",
                        line=line_number,
                        message=f"parent_locus_id must be null or match {LOCUS_ID_PATTERN.pattern}",
                    )

                if row["locus_type"] not in ALLOWED_LOCUS_TYPES:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_TYPE_INVALID",
                        line=line_number,
                        message="locus_type is not in the allowed vocabulary",
                    )

                if row["query_family"] not in ALLOWED_QUERY_FAMILIES:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_QUERY_FAMILY_INVALID",
                        line=line_number,
                        message="query_family is not in the allowed vocabulary",
                    )

                if row["review_state"] not in ALLOWED_REVIEW_STATES:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_REVIEW_STATE_INVALID",
                        line=line_number,
                        message="review_state is not in the allowed vocabulary",
                    )

                if not isinstance(row["languages"], list) or any(not is_nonblank_string(item) for item in row["languages"]):
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_LANGUAGES_INVALID",
                        line=line_number,
                        message="languages must be a list of nonblank strings",
                    )

                for key in sorted(COUNTER_FIELDS):
                    if not is_nonnegative_integer(row[key]):
                        return fail(
                            counts,
                            code="SOURCE_LOCUS_COUNTER_INVALID",
                            line=line_number,
                            message=f"{key} must be a nonnegative integer",
                        )

                for key in sorted(SCORE_FIELDS):
                    if not is_unit_interval_number(row[key]):
                        return fail(
                            counts,
                            code="SOURCE_LOCUS_SCORE_INVALID",
                            line=line_number,
                            message=f"{key} must be a number from 0 to 1",
                        )

                if not isinstance(row["is_deprecated"], bool):
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_DEPRECATED_FLAG_INVALID",
                        line=line_number,
                        message="is_deprecated must be a boolean",
                    )

                for key in sorted(URL_FIELDS):
                    value = row[key]
                    if value is not None and value.strip() and not absolute_http_url(value):
                        return fail(
                            counts,
                            code="SOURCE_LOCUS_URL_INVALID",
                            line=line_number,
                            message=f"{key} must be null, blank, or an absolute http or https URL",
                        )

                if row["locus_type"] == "unknown" and "unknown_locus" not in row["locus_id"]:
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_UNKNOWN_ID_INVALID",
                        line=line_number,
                        message="unknown locus rows must include unknown_locus in locus_id",
                    )

                if (row["is_deprecated"] or row["review_state"] == "deprecated") and not is_nonblank_string(
                    row["deprecation_reason"]
                ):
                    return fail(
                        counts,
                        code="SOURCE_LOCUS_DEPRECATION_REASON_MISSING",
                        line=line_number,
                        message="deprecated source-locus rows must include deprecation_reason",
                    )

                if row["review_state"] in {"needs_review", "demoted"}:
                    counts["deferred"] += 1
                    warnings.append(
                        {
                            "code": "SOURCE_LOCUS_REVIEW_NEEDED",
                            "line": line_number,
                            "message": f"{row['locus_id']} requires review before automated planning",
                        }
                    )

                if row["is_deprecated"] or row["review_state"] == "deprecated":
                    warnings.append(
                        {
                            "code": "SOURCE_LOCUS_DEPRECATED",
                            "line": line_number,
                            "message": f"{row['locus_id']} is preserved as deprecated",
                        }
                    )

                counts["accepted"] += 1
    except OSError as exc:
        errors.append(
            {"code": "INPUT_UNREADABLE", "line": None, "message": f"input file could not be read: {exc}"}
        )
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_source_locus_jsonl(target)
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
