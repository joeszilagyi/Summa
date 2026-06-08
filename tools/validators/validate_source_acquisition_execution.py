#!/usr/bin/env python3
"""Validate source acquisition execution artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
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
        resolve_report_root,
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
        resolve_report_root,
    )


VALIDATOR_NAME = "source_acquisition_execution"
CONTRACT_VERSION = "1"
EXECUTION_SCHEMA_VERSION = "source-acquisition-execution.v1"
RUN_MANIFEST_SCHEMA_VERSION = "source-acquisition-run-manifest.v1"
CAPTURE_SCHEMA_VERSION = "source-capture-event.v1"
EXTRACTION_SCHEMA_VERSION = "source-extraction-record.v1"
SUPPORTED_EXECUTION_SCHEMA_VERSIONS = {EXECUTION_SCHEMA_VERSION, "source-acquisition-execution.v0"}
SUPPORTED_CAPTURE_SCHEMA_VERSIONS = {CAPTURE_SCHEMA_VERSION, "source-capture-event.v0"}
SUPPORTED_EXTRACTION_SCHEMA_VERSIONS = {EXTRACTION_SCHEMA_VERSION, "source-extraction-record.v0"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_EXECUTION_STATUSES = {"completed", "denied", "dry_run", "failed"}
SUPPORTED_CAPTURE_STATUSES = {"completed", "failed", "skipped", "denied"}
SUPPORTED_EXTRACTION_STATUSES = {"completed", "failed", "skipped", "denied"}
EXECUTION_RECORD_ALLOWED_KEYS = {
    "schema_version",
    "run_id",
    "created_at",
    "executor_name",
    "executor_mode",
    "adapter_id",
    "workspace_id",
    "adapter_type",
    "handoff_path",
    "input_handoff_hash",
    "dry_run",
    "status",
    "network_access_attempted",
    "network_access_allowed",
    "network_access_denied_reason",
    "network_safety_gate",
    "local_input_paths_processed",
    "planned_actions",
    "capture_event_count",
    "extraction_record_count",
    "output_artifacts",
    "canonical_persistence_attempted",
    "verification_status",
    "network_gate_request_hash",
    "remote_live_fetch_enabled",
    "timeout_seconds",
    "max_response_bytes",
    "urls_planned",
    "urls_attempted",
    "urls_succeeded",
    "urls_failed",
    "urls_denied",
    "bytes_captured",
}
OUTPUT_ARTIFACT_ALLOWED_KEYS = {
    "execution_record",
    "capture_events",
    "extraction_records",
    "manifest",
    "denial_record",
    "network_safety_report",
}
RUN_MANIFEST_ALLOWED_KEYS = {
    "schema_version",
    "run_id",
    "created_at",
    "status",
    "artifacts",
    "canonical_persistence_attempted",
}
CAPTURE_RECORD_ALLOWED_KEYS = {
    "schema_version",
    "capture_id",
    "run_id",
    "handoff_hash",
    "handoff_sequences",
    "adapter_id",
    "workspace_id",
    "adapter_type",
    "source_reference",
    "original_locator",
    "normalized_local_path",
    "normalized_url",
    "final_url",
    "redirect_count",
    "redirect_target",
    "http_status_code",
    "request_method",
    "user_agent",
    "content_hash",
    "byte_count",
    "content_length_header",
    "content_type",
    "captured_at",
    "capture_method",
    "transient_payload_path",
    "payload_retention_policy",
    "network_access_attempted",
    "rights_posture",
    "repo_state",
    "git_ref",
    "git_commit",
    "status",
    "failure_reason",
    "error_detail",
    "canonical_persistence_attempted",
    "verification_status",
}
EXTRACTION_RECORD_ALLOWED_KEYS = {
    "schema_version",
    "extraction_id",
    "run_id",
    "capture_id",
    "adapter_id",
    "workspace_id",
    "adapter_type",
    "handoff_sequence",
    "relative_path",
    "extraction_method",
    "input_hash",
    "content_hash",
    "byte_count_in",
    "byte_count_out",
    "encoding_result",
    "truncation_status",
    "hostile_replay_flags",
    "failure_reason",
    "extracted_text_path",
    "status",
    "canonical_persistence_attempted",
    "verification_status",
    "structured_format",
    "record_locator",
    "record_kind",
    "parse_error_count",
    "git_ref",
    "git_commit",
    "content_type",
    "remote_url",
    "final_url",
    "network_access_attempted",
}


@dataclass(frozen=True)
class ExecutionArtifactReceipt:
    execution_record: dict[str, Any]
    paths: dict[str, Path]
    input_hashes: dict[str, str]
    manifest: dict[str, Any]
    denial_record: dict[str, Any] | None
    network_safety_report: dict[str, Any] | None


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


def _validation_failure_result(
    code: str, message: str, exit_code: int
) -> tuple[dict[str, Any], int]:
    result = {
        "counts": {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0},
        "errors": [],
        "warnings": [],
    }
    add_error(result["errors"], code=code, message=message)
    return result, exit_code


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def _load_json_object(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise OSError(f"{label} could not be read: {path}") from exc
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload = json.loads(
            raw_bytes,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise UnicodeDecodeError(
            exc.encoding, exc.object, exc.start, exc.end, f"{label} is not UTF-8"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} top-level JSON value must be an object")
    return payload, raw_hash


def hash_file(path: Path, *, label: str | None = None) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        if label is None:
            raise FileNotFoundError(f"path does not exist: {path}") from exc
        raise FileNotFoundError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        if label is None:
            raise OSError(f"could not be read: {path}") from exc
        raise OSError(f"{label} could not be read: {path}") from exc
    return digest.hexdigest()


def _iter_jsonl_records(path: Path, *, label: str):
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(
                        raw_line,
                        object_pairs_hook=no_duplicate_object_pairs,
                        parse_constant=reject_json_constant,
                    )
                except UnicodeDecodeError as exc:
                    raise UnicodeDecodeError(
                        exc.encoding,
                        exc.object,
                        exc.start,
                        exc.end,
                        f"{label} line {line_number} is not UTF-8",
                    ) from exc
                if not isinstance(value, dict):
                    raise ValueError(f"{label} line {line_number} must contain a JSON object")
                yield value
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise OSError(f"{label} could not be read: {path}") from exc


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
        "manifest": run_dir / "manifest.json",
        "denial_record": run_dir / "denial-record.json",
        "network_safety_report": run_dir / "network-safety-report.json",
    }


def load_execution_artifacts(target: Path) -> ExecutionArtifactReceipt:
    paths = resolve_execution_artifact_paths(target)
    execution_record, execution_record_hash = _load_json_object(
        paths["execution_record"], label="execution record"
    )
    capture_events_hash = hash_file(paths["capture_events"], label="capture events")
    extraction_records_hash = hash_file(paths["extraction_records"], label="extraction records")
    manifest, manifest_hash = _load_json_object(paths["manifest"], label="manifest")
    output_artifacts = execution_record.get("output_artifacts")
    if not isinstance(output_artifacts, dict):
        output_artifacts = {}
    denial_record = None
    denial_record_hash = None
    if output_artifacts.get("denial_record") is not None:
        denial_record, denial_record_hash = _load_json_object(
            paths["denial_record"], label="denial record"
        )
    network_safety_report = None
    network_safety_report_hash = None
    if output_artifacts.get("network_safety_report") is not None:
        network_safety_report, network_safety_report_hash = _load_json_object(
            paths["network_safety_report"], label="network safety report"
        )
    return ExecutionArtifactReceipt(
        execution_record=execution_record,
        paths=paths,
        input_hashes={
            "execution_record": execution_record_hash,
            "capture_events": capture_events_hash,
            "extraction_records": extraction_records_hash,
            "manifest": manifest_hash,
            **({"denial_record": denial_record_hash} if denial_record_hash is not None else {}),
            **(
                {"network_safety_report": network_safety_report_hash}
                if network_safety_report_hash is not None
                else {}
            ),
        },
        manifest=manifest,
        denial_record=denial_record,
        network_safety_report=network_safety_report,
    )


def validate_execution_artifact_receipt(
    receipt: ExecutionArtifactReceipt,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    counts["inspected"] = 1
    expected_run_id = (
        receipt.execution_record.get("run_id")
        if isinstance(receipt.execution_record.get("run_id"), str)
        else ""
    )
    expected_input_handoff_hash = receipt.execution_record.get("input_handoff_hash")
    if not isinstance(expected_input_handoff_hash, str):
        expected_input_handoff_hash = ""
    capture_event_count, capture_ids, capture_records = validate_capture_events(
        _iter_jsonl_records(receipt.paths["capture_events"], label="capture events"),
        expected_run_id=expected_run_id,
        expected_input_handoff_hash=expected_input_handoff_hash,
        artifact_root=receipt.paths["run_dir"],
        errors=errors,
    )
    extraction_record_count = validate_extraction_records(
        _iter_jsonl_records(receipt.paths["extraction_records"], label="extraction records"),
        expected_run_id=expected_run_id,
        capture_ids=capture_ids,
        capture_records=capture_records,
        artifact_root=receipt.paths["run_dir"],
        errors=errors,
    )
    validate_execution_record(
        receipt.execution_record,
        capture_event_count=capture_event_count,
        extraction_record_count=extraction_record_count,
        errors=errors,
    )
    validate_run_manifest(
        receipt.manifest,
        execution_record=receipt.execution_record,
        errors=errors,
    )
    if receipt.denial_record is not None:
        validate_denial_record(
            receipt.denial_record,
            expected_run_id=expected_run_id,
            expected_input_handoff_hash=expected_input_handoff_hash,
            errors=errors,
        )
    if receipt.network_safety_report is not None:
        validate_network_safety_report(
            receipt.network_safety_report,
            execution_record=receipt.execution_record,
            errors=errors,
        )

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


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


def _require_enum(
    value: Any,
    *,
    errors: list[dict[str, Any]],
    path: str,
    code: str,
    message: str,
    allowed: set[str],
) -> None:
    if value not in allowed:
        add_error(errors, code=code, message=message, path=path)


def _reject_unknown_fields(
    payload: dict[str, Any],
    *,
    allowed: set[str],
    errors: list[dict[str, Any]],
    path_prefix: str,
    code: str,
    label: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        add_error(
            errors,
            code=code,
            message=f"unexpected {label} field: {unknown[0]}",
            path=f"{path_prefix}.{unknown[0]}",
        )


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


def _resolve_artifact_path(
    raw_path: str,
    *,
    artifact_root_resolved: Path,
) -> Path | None:
    artifact_path = (artifact_root_resolved / raw_path).resolve()
    try:
        artifact_path.relative_to(artifact_root_resolved)
    except ValueError:
        return None
    return artifact_path


def _require_artifact_path(
    value: Any,
    *,
    artifact_root_resolved: Path,
    errors: list[dict[str, Any]],
    path: str,
    code_missing: str,
    code_invalid: str,
    code_missing_file: str,
    message_missing: str,
    message_invalid: str,
    message_missing_file: str,
) -> Path | None:
    if value is None:
        add_error(errors, code=code_missing, message=message_missing, path=path)
        return None
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code_invalid, message=message_invalid, path=path)
        return None
    artifact_path = _resolve_artifact_path(value, artifact_root_resolved=artifact_root_resolved)
    if artifact_path is None:
        add_error(errors, code=code_invalid, message=message_invalid, path=path)
        return None
    if not artifact_path.is_file():
        add_error(errors, code=code_missing_file, message=message_missing_file, path=path)
        return None
    return artifact_path


def validate_execution_record(
    payload: dict[str, Any],
    *,
    capture_event_count: int,
    extraction_record_count: int,
    errors: list[dict[str, Any]],
) -> None:
    _reject_unknown_fields(
        payload,
        allowed=EXECUTION_RECORD_ALLOWED_KEYS,
        errors=errors,
        path_prefix="$",
        code="UNKNOWN_EXECUTION_FIELD",
        label="execution record",
    )
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
    _require_enum(
        payload.get("status"),
        errors=errors,
        path="$.status",
        code="INVALID_EXECUTION_STATUS",
        message=f"status must be one of {sorted(SUPPORTED_EXECUTION_STATUSES)!r}",
        allowed=SUPPORTED_EXECUTION_STATUSES,
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
    if payload.get("capture_event_count") != capture_event_count:
        add_error(
            errors,
            code="CAPTURE_COUNT_MISMATCH",
            message="capture_event_count does not match capture-events.jsonl row count",
            path="$.capture_event_count",
        )
    if payload.get("extraction_record_count") != extraction_record_count:
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
    output_artifacts = payload.get("output_artifacts")
    if not isinstance(output_artifacts, dict):
        add_error(
            errors,
            code="INVALID_OUTPUT_ARTIFACTS",
            message="output_artifacts must be an object",
            path="$.output_artifacts",
        )
    else:
        _reject_unknown_fields(
            output_artifacts,
            allowed=OUTPUT_ARTIFACT_ALLOWED_KEYS,
            errors=errors,
            path_prefix="$.output_artifacts",
            code="UNKNOWN_OUTPUT_ARTIFACT_FIELD",
            label="output_artifacts",
        )
        for key in ("execution_record", "capture_events", "extraction_records", "manifest"):
            _require_string(
                output_artifacts.get(key),
                errors=errors,
                path=f"$.output_artifacts.{key}",
                code="STRING_REQUIRED",
                message=f"output_artifacts.{key} must be a non-blank string",
            )
        for key in ("denial_record", "network_safety_report"):
            if output_artifacts.get(key) is not None:
                _require_string(
                    output_artifacts.get(key),
                    errors=errors,
                    path=f"$.output_artifacts.{key}",
                    code="STRING_REQUIRED",
                    message=f"output_artifacts.{key} must be null or a non-blank string",
                )
    if payload.get("network_gate_request_hash") is not None:
        _require_sha256_or_null(
            payload.get("network_gate_request_hash"),
            errors=errors,
            path="$.network_gate_request_hash",
            code="INVALID_NETWORK_GATE_REQUEST_HASH",
            message="network_gate_request_hash must be a 64-character lowercase SHA-256 hex digest",
        )
    if payload.get("remote_live_fetch_enabled") is not None:
        _require_bool(
            payload.get("remote_live_fetch_enabled"),
            errors=errors,
            path="$.remote_live_fetch_enabled",
            code="BOOL_REQUIRED",
            message="remote_live_fetch_enabled must be boolean",
        )
    for key in ("timeout_seconds",):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
        ):
            add_error(
                errors,
                code="INVALID_TIMEOUT_SECONDS",
                message="timeout_seconds must be a positive number",
                path=f"$.{key}",
            )
    for key in (
        "max_response_bytes",
        "urls_planned",
        "urls_attempted",
        "urls_succeeded",
        "urls_failed",
        "urls_denied",
        "bytes_captured",
    ):
        value = payload.get(key)
        if value is not None:
            _require_nonnegative_int(
                value,
                errors=errors,
                path=f"$.{key}",
                code="COUNT_REQUIRED",
                message=f"{key} must be a non-negative integer",
            )
    if payload.get("adapter_type") == "remote_url_manifest":
        if (
            payload.get("status") in {"denied", "dry_run"}
            and payload.get("network_access_attempted") is not False
        ):
            add_error(
                errors,
                code="REMOTE_DENIAL_ATTEMPTED_NETWORK",
                message="remote denied and dry-run execution records must set network_access_attempted false",
                path="$.network_access_attempted",
            )
        if capture_event_count > 0 and payload.get("network_access_attempted") is not True:
            add_error(
                errors,
                code="REMOTE_CAPTURE_WITHOUT_NETWORK_ATTEMPT",
                message="remote capture events require execution network_access_attempted true",
                path="$.network_access_attempted",
            )


def validate_capture_events(
    records: Any,
    *,
    expected_run_id: str,
    expected_input_handoff_hash: str,
    artifact_root: Path,
    errors: list[dict[str, Any]],
) -> tuple[int, set[str], dict[str, dict[str, Any]]]:
    artifact_root_resolved = artifact_root.resolve()
    seen_capture_ids: set[str] = set()
    capture_records: dict[str, dict[str, Any]] = {}
    record_count = 0
    for index, record in enumerate(records):
        record_count += 1
        base = f"$[{index}]"
        _reject_unknown_fields(
            record,
            allowed=CAPTURE_RECORD_ALLOWED_KEYS,
            errors=errors,
            path_prefix=base,
            code="UNKNOWN_CAPTURE_FIELD",
            label="capture record",
        )
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
        _require_enum(
            record.get("status"),
            errors=errors,
            path=f"{base}.status",
            code="INVALID_CAPTURE_STATUS",
            message=f"status must be one of {sorted(SUPPORTED_CAPTURE_STATUSES)!r}",
            allowed=SUPPORTED_CAPTURE_STATUSES,
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
                capture_records[capture_id] = {
                    "content_hash": record.get("content_hash"),
                    "byte_count": record.get("byte_count"),
                }
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
        if (
            record.get("adapter_type") == "remote_url_manifest"
            or record.get("capture_method") == "remote_url_fetch"
        ):
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
        transient_payload_path = record.get("transient_payload_path")
        if transient_payload_path is not None:
            if not isinstance(transient_payload_path, str) or not transient_payload_path.strip():
                add_error(
                    errors,
                    code="INVALID_TRANSIENT_PAYLOAD_PATH",
                    message="transient_payload_path must be null or a non-blank string",
                    path=f"{base}.transient_payload_path",
                )
            else:
                artifact_path = _resolve_artifact_path(
                    transient_payload_path, artifact_root_resolved=artifact_root_resolved
                )
                if artifact_path is None:
                    add_error(
                        errors,
                        code="TRANSIENT_PAYLOAD_PATH_INVALID",
                        message="transient_payload_path escapes the artifact root",
                        path=f"{base}.transient_payload_path",
                    )
                elif not artifact_path.is_file():
                    add_error(
                        errors,
                        code="TRANSIENT_PAYLOAD_ARTIFACT_MISSING",
                        message="transient_payload_path points to a missing artifact",
                        path=f"{base}.transient_payload_path",
                    )
    return record_count, seen_capture_ids, capture_records


def validate_extraction_records(
    records: Any,
    *,
    expected_run_id: str,
    capture_ids: set[str],
    capture_records: dict[str, dict[str, Any]],
    artifact_root: Path,
    errors: list[dict[str, Any]],
) -> int:
    artifact_root_resolved = artifact_root.resolve()
    seen_extraction_ids: set[str] = set()
    record_count = 0
    for index, record in enumerate(records):
        record_count += 1
        base = f"$[{index}]"
        _reject_unknown_fields(
            record,
            allowed=EXTRACTION_RECORD_ALLOWED_KEYS,
            errors=errors,
            path_prefix=base,
            code="UNKNOWN_EXTRACTION_FIELD",
            label="extraction record",
        )
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
        _require_enum(
            record.get("status"),
            errors=errors,
            path=f"{base}.status",
            code="INVALID_EXTRACTION_STATUS",
            message=f"status must be one of {sorted(SUPPORTED_EXTRACTION_STATUSES)!r}",
            allowed=SUPPORTED_EXTRACTION_STATUSES,
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
                actual_hash = hash_file(artifact_path, label="extracted text artifact")
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
            if record.get("content_hash") != actual_hash:
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_HASH_MISMATCH",
                    message="extracted text artifact hash does not match content_hash",
                    path=f"{base}.content_hash",
                )
            if record.get("byte_count_out") != artifact_path.stat().st_size:
                add_error(
                    errors,
                    code="EXTRACTED_TEXT_BYTE_COUNT_MISMATCH",
                    message="extracted text artifact byte_count does not match byte_count_out",
                    path=f"{base}.byte_count_out",
                )
    return record_count


def validate_run_manifest(
    manifest: dict[str, Any],
    *,
    execution_record: dict[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    _reject_unknown_fields(
        manifest,
        allowed=RUN_MANIFEST_ALLOWED_KEYS,
        errors=errors,
        path_prefix="$",
        code="UNKNOWN_MANIFEST_FIELD",
        label="run manifest",
    )
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
        add_error(
            errors,
            code="INVALID_MANIFEST_SCHEMA_VERSION",
            message=f"schema_version must be {RUN_MANIFEST_SCHEMA_VERSION!r}",
            path="$.schema_version",
        )
    _require_string(
        manifest.get("run_id"),
        errors=errors,
        path="$.run_id",
        code="STRING_REQUIRED",
        message="run_id must be a non-blank string",
    )
    _require_rfc3339(
        manifest.get("created_at"),
        errors=errors,
        path="$.created_at",
        code="INVALID_CREATED_AT",
        message="created_at must be an RFC3339 date-time",
    )
    _require_enum(
        manifest.get("status"),
        errors=errors,
        path="$.status",
        code="INVALID_MANIFEST_STATUS",
        message=f"status must be one of {sorted(SUPPORTED_EXECUTION_STATUSES)!r}",
        allowed=SUPPORTED_EXECUTION_STATUSES,
    )
    _require_bool(
        manifest.get("canonical_persistence_attempted"),
        errors=errors,
        path="$.canonical_persistence_attempted",
        code="BOOL_REQUIRED",
        message="canonical_persistence_attempted must be boolean",
    )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        add_error(
            errors,
            code="INVALID_MANIFEST_ARTIFACTS",
            message="manifest.artifacts must be an object",
            path="$.artifacts",
        )
    output_artifacts = execution_record.get("output_artifacts")
    if not isinstance(output_artifacts, dict):
        output_artifacts = {}
    if manifest.get("run_id") != execution_record.get("run_id"):
        add_error(
            errors,
            code="MANIFEST_RUN_ID_MISMATCH",
            message="manifest run_id does not match execution-record.json",
            path="$.run_id",
        )
    if manifest.get("status") != execution_record.get("status"):
        add_error(
            errors,
            code="MANIFEST_STATUS_MISMATCH",
            message="manifest status does not match execution-record.json",
            path="$.status",
        )
    if isinstance(artifacts, dict) and artifacts != output_artifacts:
        add_error(
            errors,
            code="MANIFEST_ARTIFACTS_MISMATCH",
            message="manifest.artifacts does not match execution-record.json output_artifacts",
            path="$.artifacts",
        )


def validate_denial_record(
    record: dict[str, Any],
    *,
    expected_run_id: str,
    expected_input_handoff_hash: str,
    errors: list[dict[str, Any]],
) -> None:
    payload = dict(record)
    considered_urls = payload.pop("considered_urls", None)
    validate_execution_record(
        payload,
        capture_event_count=0,
        extraction_record_count=0,
        errors=errors,
    )
    if not isinstance(considered_urls, list) or not all(
        isinstance(url, str) and url for url in considered_urls
    ):
        add_error(
            errors,
            code="INVALID_CONSIDERED_URLS",
            message="considered_urls must be an array of non-blank strings",
            path="$.considered_urls",
        )
    elif expected_run_id and payload.get("run_id") != expected_run_id:
        add_error(
            errors,
            code="DENIAL_RUN_ID_MISMATCH",
            message="denial record run_id does not match execution-record.json",
            path="$.run_id",
        )
    if payload.get("input_handoff_hash") != expected_input_handoff_hash:
        add_error(
            errors,
            code="DENIAL_HANDOFF_HASH_MISMATCH",
            message="denial record input_handoff_hash does not match execution-record.json",
            path="$.input_handoff_hash",
        )


def validate_network_safety_report(
    report: dict[str, Any],
    *,
    execution_record: dict[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    if not isinstance(report, dict):
        add_error(
            errors,
            code="INVALID_NETWORK_SAFETY_REPORT",
            message="network-safety-report.json must contain a JSON object",
            path="$",
        )
        return
    summary = execution_record.get("network_safety_gate")
    if not isinstance(summary, dict):
        add_error(
            errors,
            code="MISSING_NETWORK_SAFETY_GATE_SUMMARY",
            message="execution-record.json is missing network_safety_gate for a network safety report",
            path="$.network_safety_gate",
        )
        return
    if report.get("schema_version") != summary.get("schema_version"):
        add_error(
            errors,
            code="NETWORK_SAFETY_SCHEMA_MISMATCH",
            message="network safety report schema_version does not match execution summary",
            path="$.schema_version",
        )
    if report.get("decision") != summary.get("decision"):
        add_error(
            errors,
            code="NETWORK_SAFETY_DECISION_MISMATCH",
            message="network safety report decision does not match execution summary",
            path="$.decision",
        )
    if report.get("execution_allowed") != summary.get("execution_allowed"):
        add_error(
            errors,
            code="NETWORK_SAFETY_ALLOWANCE_MISMATCH",
            message="network safety report execution_allowed does not match execution summary",
            path="$.execution_allowed",
        )
    counts = report.get("counts")
    if not isinstance(counts, dict):
        add_error(
            errors,
            code="INVALID_NETWORK_SAFETY_COUNTS",
            message="network safety report counts must be an object",
            path="$.counts",
        )
        return
    if counts.get("errors") != summary.get("error_count"):
        add_error(
            errors,
            code="NETWORK_SAFETY_ERROR_COUNT_MISMATCH",
            message="network safety report error count does not match execution summary",
            path="$.counts.errors",
        )
    if counts.get("warnings") != summary.get("warning_count"):
        add_error(
            errors,
            code="NETWORK_SAFETY_WARNING_COUNT_MISMATCH",
            message="network safety report warning count does not match execution summary",
            path="$.counts.warnings",
        )


def validate_source_acquisition_execution(target: Path) -> tuple[dict[str, Any], int]:
    try:
        receipt = load_execution_artifacts(target)
    except FileNotFoundError as exc:
        return _validation_failure_result("INPUT_NOT_FOUND", str(exc), EXIT_INPUT_UNAVAILABLE)
    except (OSError, UnicodeDecodeError) as exc:
        return _validation_failure_result("INPUT_UNREADABLE", str(exc), EXIT_INPUT_UNAVAILABLE)
    except DuplicateJsonKeyError as exc:
        return _validation_failure_result("DUPLICATE_JSON_KEY", str(exc), EXIT_VALIDATION_FAILED)
    except NonStandardJsonConstantError as exc:
        return _validation_failure_result(
            "NON_STANDARD_JSON_CONSTANT", str(exc), EXIT_VALIDATION_FAILED
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return _validation_failure_result("JSON_PARSE_ERROR", str(exc), EXIT_VALIDATION_FAILED)

    return validate_execution_artifact_receipt(receipt)


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_source_acquisition_execution(target)
    status = "pass" if exit_code == EXIT_PASS else "fail"
    report_root = resolve_report_root(target, report_root=args.report_root)
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
        report_root=report_root,
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
