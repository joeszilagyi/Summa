#!/usr/bin/env python3
"""Validate source acquisition execution artifacts."""

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


VALIDATOR_NAME = "source_acquisition_execution"
CONTRACT_VERSION = "1"
EXECUTION_SCHEMA_VERSION = "source-acquisition-execution.v1"
CAPTURE_SCHEMA_VERSION = "source-capture-event.v1"
EXTRACTION_SCHEMA_VERSION = "source-extraction-record.v1"
SUPPORTED_EXECUTION_SCHEMA_VERSIONS = {EXECUTION_SCHEMA_VERSION, "source-acquisition-execution.v0"}
SUPPORTED_CAPTURE_SCHEMA_VERSIONS = {CAPTURE_SCHEMA_VERSION, "source-capture-event.v0"}
SUPPORTED_EXTRACTION_SCHEMA_VERSIONS = {EXTRACTION_SCHEMA_VERSION, "source-extraction-record.v0"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one checked-in source acquisition execution artifact family rooted at "
            "execution-record.json."
        )
    )
    parser.add_argument(
        "target",
        help="Path to execution-record.json or the run directory that contains it.",
    )
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
    errors.append({"code": code, "line": line, "message": message, "path": path})


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def _load_text(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise OSError(f"{label} could not be read: {path}") from exc
    except UnicodeDecodeError as exc:
        raise UnicodeDecodeError(exc.encoding, exc.object, exc.start, exc.end, f"{label} is not UTF-8") from exc


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    raw_text = _load_text(path, label=label)
    payload = json.loads(
        raw_text,
        object_pairs_hook=no_duplicate_object_pairs,
        parse_constant=reject_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"{label} top-level JSON value must be an object")
    return payload


def _load_jsonl_records(path: Path, *, label: str) -> list[dict[str, Any]]:
    lines = _load_text(path, label=label).splitlines()
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        value = json.loads(
            raw_line,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {line_number} must contain a JSON object")
        records.append(value)
    return records


def resolve_execution_artifact_paths(target: Path) -> dict[str, Path]:
    resolved = target.expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    run_dir = resolved if resolved.is_dir() else resolved.parent
    execution_path = run_dir / "execution-record.json" if resolved.is_dir() else resolved
    return {
        "run_dir": run_dir,
        "execution_record": execution_path,
        "capture_events": run_dir / "capture-events.jsonl",
        "extraction_records": run_dir / "extraction-records.jsonl",
    }


def load_execution_artifacts(target: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Path]]:
    paths = resolve_execution_artifact_paths(target)
    execution_record = _load_json_object(paths["execution_record"], label="execution record")
    capture_events = _load_jsonl_records(paths["capture_events"], label="capture events")
    extraction_records = _load_jsonl_records(paths["extraction_records"], label="extraction records")
    return execution_record, capture_events, extraction_records, paths


def _require_string(
    value: Any,
    *,
    errors: list[dict[str, Any]],
    path: str,
    code: str,
    message: str,
) -> None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=message, path=path)


def _require_bool(
    value: Any,
    *,
    errors: list[dict[str, Any]],
    path: str,
    code: str,
    message: str,
) -> None:
    if not isinstance(value, bool):
        add_error(errors, code=code, message=message, path=path)


def _require_nonnegative_int(
    value: Any,
    *,
    errors: list[dict[str, Any]],
    path: str,
    code: str,
    message: str,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        add_error(errors, code=code, message=message, path=path)


def _require_sha256_or_null(
    value: Any,
    *,
    errors: list[dict[str, Any]],
    path: str,
    code: str,
    message: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        add_error(errors, code=code, message=message, path=path)


def _require_rfc3339(
    value: Any,
    *,
    errors: list[dict[str, Any]],
    path: str,
    code: str,
    message: str,
) -> None:
    if not isinstance(value, str) or not is_rfc3339_datetime(value):
        add_error(errors, code=code, message=message, path=path)


def validate_execution_record(
    payload: dict[str, Any],
    *,
    capture_events: list[dict[str, Any]],
    extraction_records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    if payload.get("schema_version") not in SUPPORTED_EXECUTION_SCHEMA_VERSIONS:
        add_error(
            errors,
            code="INVALID_EXECUTION_SCHEMA_VERSION",
            message=f"schema_version must be one of {sorted(SUPPORTED_EXECUTION_SCHEMA_VERSIONS)!r}",
            path="$.schema_version",
        )
    for key in (
        "run_id",
        "executor_name",
        "executor_mode",
        "adapter_id",
        "workspace_id",
        "adapter_type",
        "handoff_path",
        "input_handoff_hash",
        "status",
        "verification_status",
    ):
        _require_string(
            payload.get(key),
            errors=errors,
            path=f"$.{key}",
            code="STRING_REQUIRED",
            message=f"{key} must be a non-blank string",
        )
    _require_rfc3339(
        payload.get("created_at"),
        errors=errors,
        path="$.created_at",
        code="INVALID_CREATED_AT",
        message="created_at must be an RFC3339 date-time",
    )
    _require_bool(
        payload.get("dry_run"),
        errors=errors,
        path="$.dry_run",
        code="BOOL_REQUIRED",
        message="dry_run must be boolean",
    )
    _require_bool(
        payload.get("network_access_attempted"),
        errors=errors,
        path="$.network_access_attempted",
        code="BOOL_REQUIRED",
        message="network_access_attempted must be boolean",
    )
    _require_bool(
        payload.get("network_access_allowed"),
        errors=errors,
        path="$.network_access_allowed",
        code="BOOL_REQUIRED",
        message="network_access_allowed must be boolean",
    )
    _require_bool(
        payload.get("canonical_persistence_attempted"),
        errors=errors,
        path="$.canonical_persistence_attempted",
        code="BOOL_REQUIRED",
        message="canonical_persistence_attempted must be boolean",
    )
    _require_nonnegative_int(
        payload.get("capture_event_count"),
        errors=errors,
        path="$.capture_event_count",
        code="COUNT_REQUIRED",
        message="capture_event_count must be a non-negative integer",
    )
    _require_nonnegative_int(
        payload.get("extraction_record_count"),
        errors=errors,
        path="$.extraction_record_count",
        code="COUNT_REQUIRED",
        message="extraction_record_count must be a non-negative integer",
    )
    if payload.get("capture_event_count") != len(capture_events):
        add_error(
            errors,
            code="CAPTURE_COUNT_MISMATCH",
            message="capture_event_count does not match capture-events.jsonl row count",
            path="$.capture_event_count",
        )
    if payload.get("extraction_record_count") != len(extraction_records):
        add_error(
            errors,
            code="EXTRACTION_COUNT_MISMATCH",
            message="extraction_record_count does not match extraction-records.jsonl row count",
            path="$.extraction_record_count",
        )
    _require_sha256_or_null(
        payload.get("input_handoff_hash"),
        errors=errors,
        path="$.input_handoff_hash",
        code="INVALID_HANDOFF_HASH",
        message="input_handoff_hash must be a 64-character lowercase SHA-256 hex digest",
    )
    if payload.get("adapter_type") == "remote_url_manifest":
        if payload.get("status") in {"denied", "dry_run"} and payload.get("network_access_attempted") is not False:
            add_error(
                errors,
                code="REMOTE_DENIAL_ATTEMPTED_NETWORK",
                message="remote denied and dry-run execution records must set network_access_attempted false",
                path="$.network_access_attempted",
            )
        if capture_events and payload.get("network_access_attempted") is not True:
            add_error(
                errors,
                code="REMOTE_CAPTURE_WITHOUT_NETWORK_ATTEMPT",
                message="remote capture events require execution network_access_attempted true",
                path="$.network_access_attempted",
            )


def validate_capture_events(
    records: list[dict[str, Any]],
    *,
    expected_run_id: str,
    expected_input_handoff_hash: str,
    errors: list[dict[str, Any]],
) -> None:
    seen_capture_ids: set[str] = set()
    for index, record in enumerate(records):
        base = f"$[{index}]"
        if record.get("schema_version") not in SUPPORTED_CAPTURE_SCHEMA_VERSIONS:
            add_error(
                errors,
                code="INVALID_CAPTURE_SCHEMA_VERSION",
                message=f"schema_version must be one of {sorted(SUPPORTED_CAPTURE_SCHEMA_VERSIONS)!r}",
                path=f"{base}.schema_version",
            )
        for key in (
            "capture_id",
            "run_id",
            "handoff_hash",
            "adapter_id",
            "workspace_id",
            "adapter_type",
            "capture_method",
            "status",
            "verification_status",
        ):
            _require_string(
                record.get(key),
                errors=errors,
                path=f"{base}.{key}",
                code="STRING_REQUIRED",
                message=f"{key} must be a non-blank string",
            )
        if record.get("run_id") != expected_run_id:
            add_error(
                errors,
                code="CAPTURE_RUN_ID_MISMATCH",
                message="capture event run_id does not match execution-record.json",
                path=f"{base}.run_id",
            )
        capture_id = record.get("capture_id")
        if isinstance(capture_id, str):
            if capture_id in seen_capture_ids:
                add_error(
                    errors,
                    code="DUPLICATE_CAPTURE_ID",
                    message=f"duplicate capture_id encountered: {capture_id}",
                    path=f"{base}.capture_id",
                )
            else:
                seen_capture_ids.add(capture_id)
        if record.get("handoff_hash") != expected_input_handoff_hash:
            add_error(
                errors,
                code="CAPTURE_HANDOFF_HASH_MISMATCH",
                message="capture handoff_hash does not match execution input_handoff_hash",
                path=f"{base}.handoff_hash",
            )
        _require_rfc3339(
            record.get("captured_at"),
            errors=errors,
            path=f"{base}.captured_at",
            code="INVALID_CAPTURED_AT",
            message="captured_at must be an RFC3339 date-time",
        )
        _require_nonnegative_int(
            record.get("byte_count"),
            errors=errors,
            path=f"{base}.byte_count",
            code="INVALID_BYTE_COUNT",
            message="byte_count must be a non-negative integer",
        )
        _require_bool(
            record.get("canonical_persistence_attempted"),
            errors=errors,
            path=f"{base}.canonical_persistence_attempted",
            code="BOOL_REQUIRED",
            message="canonical_persistence_attempted must be boolean",
        )
        _require_sha256_or_null(
            record.get("handoff_hash"),
            errors=errors,
            path=f"{base}.handoff_hash",
            code="INVALID_HANDOFF_HASH",
            message="handoff_hash must be a 64-character lowercase SHA-256 hex digest",
        )
        _require_sha256_or_null(
            record.get("content_hash"),
            errors=errors,
            path=f"{base}.content_hash",
            code="INVALID_CONTENT_HASH",
            message="content_hash must be null or a 64-character lowercase SHA-256 hex digest",
        )
        if record.get("adapter_type") == "remote_url_manifest" or record.get("capture_method") == "remote_url_fetch":
            for key in ("normalized_url", "final_url", "request_method", "user_agent"):
                _require_string(
                    record.get(key),
                    errors=errors,
                    path=f"{base}.{key}",
                    code="REMOTE_FIELD_REQUIRED",
                    message=f"remote capture event must include {key}",
                )
            _require_bool(
                record.get("network_access_attempted"),
                errors=errors,
                path=f"{base}.network_access_attempted",
                code="BOOL_REQUIRED",
                message="remote capture event network_access_attempted must be boolean",
            )
            if record.get("network_access_attempted") is not True:
                add_error(
                    errors,
                    code="REMOTE_CAPTURE_NOT_ATTEMPTED",
                    message="remote capture event must set network_access_attempted true once an HTTP request is attempted",
                    path=f"{base}.network_access_attempted",
                )
            if record.get("status") == "completed":
                _require_nonnegative_int(
                    record.get("http_status_code"),
                    errors=errors,
                    path=f"{base}.http_status_code",
                    code="REMOTE_STATUS_REQUIRED",
                    message="completed remote capture event must include HTTP status code",
                )
                _require_sha256_or_null(
                    record.get("content_hash"),
                    errors=errors,
                    path=f"{base}.content_hash",
                    code="INVALID_CONTENT_HASH",
                    message="completed remote capture event must include content_hash",
                )
                if record.get("content_hash") is None:
                    add_error(
                        errors,
                        code="REMOTE_CAPTURE_HASH_REQUIRED",
                        message="completed remote capture event must include content_hash",
                        path=f"{base}.content_hash",
                    )


def validate_extraction_records(
    records: list[dict[str, Any]],
    *,
    expected_run_id: str,
    capture_ids: set[str],
    capture_records: dict[str, dict[str, Any]],
    artifact_root: Path,
    errors: list[dict[str, Any]],
) -> None:
    artifact_root_resolved = artifact_root.resolve()
    seen_extraction_ids: set[str] = set()
    for index, record in enumerate(records):
        base = f"$[{index}]"
        if record.get("schema_version") not in SUPPORTED_EXTRACTION_SCHEMA_VERSIONS:
            add_error(
                errors,
                code="INVALID_EXTRACTION_SCHEMA_VERSION",
                message=f"schema_version must be one of {sorted(SUPPORTED_EXTRACTION_SCHEMA_VERSIONS)!r}",
                path=f"{base}.schema_version",
            )
        for key in (
            "extraction_id",
            "run_id",
            "capture_id",
            "adapter_id",
            "workspace_id",
            "adapter_type",
            "extraction_method",
            "encoding_result",
            "status",
            "verification_status",
            "truncation_status",
        ):
            _require_string(
                record.get(key),
                errors=errors,
                path=f"{base}.{key}",
                code="STRING_REQUIRED",
                message=f"{key} must be a non-blank string",
            )
        if record.get("run_id") != expected_run_id:
            add_error(
                errors,
                code="EXTRACTION_RUN_ID_MISMATCH",
                message="extraction record run_id does not match execution-record.json",
                path=f"{base}.run_id",
            )
        extraction_id = record.get("extraction_id")
        if isinstance(extraction_id, str):
            if extraction_id in seen_extraction_ids:
                add_error(
                    errors,
                    code="DUPLICATE_EXTRACTION_ID",
                    message=f"duplicate extraction_id encountered: {extraction_id}",
                    path=f"{base}.extraction_id",
                )
            else:
                seen_extraction_ids.add(extraction_id)
        capture_id = record.get("capture_id")
        if isinstance(capture_id, str) and capture_id not in capture_ids:
            add_error(
                errors,
                code="UNKNOWN_CAPTURE_REFERENCE",
                message=f"capture_id does not exist in capture-events.jsonl: {capture_id}",
                path=f"{base}.capture_id",
            )
        capture_record = capture_records.get(capture_id) if isinstance(capture_id, str) else None
        if capture_record is not None:
            if record.get("input_hash") != capture_record.get("content_hash"):
                add_error(
                    errors,
                    code="EXTRACTION_INPUT_HASH_MISMATCH",
                    message="extraction input_hash does not match referenced capture content_hash",
                    path=f"{base}.input_hash",
                )
            if record.get("byte_count_in") != capture_record.get("byte_count"):
                add_error(
                    errors,
                    code="EXTRACTION_BYTE_COUNT_MISMATCH",
                    message="extraction byte_count_in does not match referenced capture byte_count",
                    path=f"{base}.byte_count_in",
                )
        _require_nonnegative_int(
            record.get("byte_count_in"),
            errors=errors,
            path=f"{base}.byte_count_in",
            code="INVALID_BYTE_COUNT",
            message="byte_count_in must be a non-negative integer",
        )
        _require_nonnegative_int(
            record.get("byte_count_out"),
            errors=errors,
            path=f"{base}.byte_count_out",
            code="INVALID_BYTE_COUNT",
            message="byte_count_out must be a non-negative integer",
        )
        _require_bool(
            record.get("canonical_persistence_attempted"),
            errors=errors,
            path=f"{base}.canonical_persistence_attempted",
            code="BOOL_REQUIRED",
            message="canonical_persistence_attempted must be boolean",
        )
        _require_sha256_or_null(
            record.get("input_hash"),
            errors=errors,
            path=f"{base}.input_hash",
            code="INVALID_INPUT_HASH",
            message="input_hash must be null or a 64-character lowercase SHA-256 hex digest",
        )
        _require_sha256_or_null(
            record.get("content_hash"),
            errors=errors,
            path=f"{base}.content_hash",
            code="INVALID_CONTENT_HASH",
            message="content_hash must be null or a 64-character lowercase SHA-256 hex digest",
        )
        if record.get("status") == "completed":
            extracted_text_path = record.get("extracted_text_path")
            if not isinstance(extracted_text_path, str) or not extracted_text_path.strip():
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_PATH_REQUIRED",
                    message="completed extraction records must include extracted_text_path",
                    path=f"{base}.extracted_text_path",
                )
                continue
            artifact_path = (artifact_root_resolved / extracted_text_path).resolve()
            try:
                artifact_path.relative_to(artifact_root_resolved)
            except ValueError:
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_PATH_INVALID",
                    message="extracted_text_path escapes the artifact root",
                    path=f"{base}.extracted_text_path",
                )
                continue
            try:
                artifact_bytes = artifact_path.read_bytes()
            except FileNotFoundError:
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_ARTIFACT_MISSING",
                    message=f"extracted text artifact does not exist: {artifact_path}",
                    path=f"{base}.extracted_text_path",
                )
                continue
            except OSError as exc:
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_ARTIFACT_UNREADABLE",
                    message=f"extracted text artifact could not be read: {exc}",
                    path=f"{base}.extracted_text_path",
                )
                continue
            actual_hash = hashlib.sha256(artifact_bytes).hexdigest()
            if record.get("content_hash") != actual_hash:
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_HASH_MISMATCH",
                    message="extracted text artifact hash does not match content_hash",
                    path=f"{base}.content_hash",
                )
            if record.get("byte_count_out") != len(artifact_bytes):
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_BYTE_COUNT_MISMATCH",
                    message="extracted text artifact byte_count does not match byte_count_out",
                    path=f"{base}.byte_count_out",
                )


def validate_source_acquisition_execution(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        execution_record, capture_events, extraction_records, paths = load_execution_artifacts(target)
    except FileNotFoundError as exc:
        add_error(errors, code="INPUT_NOT_FOUND", message=str(exc))
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE
    except (OSError, UnicodeDecodeError) as exc:
        add_error(errors, code="INPUT_UNREADABLE", message=str(exc))
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE
    except DuplicateJsonKeyError as exc:
        add_error(errors, code="DUPLICATE_JSON_KEY", message=str(exc))
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(errors, code="NON_STANDARD_JSON_CONSTANT", message=str(exc))
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED
    except (json.JSONDecodeError, ValueError) as exc:
        add_error(errors, code="JSON_PARSE_ERROR", message=str(exc))
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["inspected"] = 1
    validate_execution_record(
        execution_record,
        capture_events=capture_events,
        extraction_records=extraction_records,
        errors=errors,
    )
    expected_run_id = execution_record.get("run_id") if isinstance(execution_record.get("run_id"), str) else ""
    expected_input_handoff_hash = execution_record.get("input_handoff_hash")
    if not isinstance(expected_input_handoff_hash, str):
        expected_input_handoff_hash = ""
    validate_capture_events(
        capture_events,
        expected_run_id=expected_run_id,
        expected_input_handoff_hash=expected_input_handoff_hash,
        errors=errors,
    )
    capture_ids = {
        record["capture_id"]
        for record in capture_events
        if isinstance(record.get("capture_id"), str)
    }
    validate_extraction_records(
        extraction_records,
        expected_run_id=expected_run_id,
        capture_ids=capture_ids,
        capture_records={
            str(record["capture_id"]): record
            for record in capture_events
            if isinstance(record.get("capture_id"), str)
        },
        artifact_root=paths["run_dir"],
        errors=errors,
    )

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_source_acquisition_execution(target)
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
