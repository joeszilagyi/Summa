#!/usr/bin/env python3
"""Execute validated source-adapter handoffs into workspace-local acquisition artifacts.

Documentation: `docs/scripts/index_execute_source_adapter.md`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
for candidate in (REPO_ROOT, VALIDATORS_DIR):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_path,
    atomic_write_text,
)
from tools.common.network_safety_gate import (  # noqa: E402
    NetworkSafetyGateError,
    allowlisted,
    evaluate_request,
    load_request,
)
from tools.common.source_adapter_handoff import infer_handoff_variant, utc_now  # noqa: E402
from tools.scripts.plan_local_git_repo_adapter import git as git_command  # noqa: E402
from tools.scripts.plan_structured_data_source_adapter import (  # noqa: E402
    build_xml_path_map,
    resolve_json_record_path,
)
from tools.validators import validate_source_adapter, validate_source_adapter_handoff  # noqa: E402
from tools.validators.common import EXIT_PASS, EXIT_STATE_UNSAFE, is_rfc3339_datetime  # noqa: E402

EXECUTION_SCHEMA_VERSION = "source-acquisition-execution.v1"
CAPTURE_SCHEMA_VERSION = "source-capture-event.v1"
EXTRACTION_SCHEMA_VERSION = "source-extraction-record.v1"
EXECUTOR_NAME = "tools/scripts/execute_source_adapter.py"
MAX_EXTRACT_TEXT_BYTES = 64 * 1024
DEFAULT_REMOTE_TIMEOUT_SECONDS = 10.0
DEFAULT_REMOTE_MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REMOTE_REDIRECTS = 3
SAFE_TEXT_STATUS = {"completed", "failed", "skipped", "denied"}
SAFE_RUN_STATUS = {"completed", "denied", "failed", "dry_run"}
LOCAL_VARIANTS = {"local_source", "structured_data", "local_git_repo"}
REMOTE_VARIANTS = {"remote_url_manifest"}


class SourceAcquisitionError(RuntimeError):
    """Raised when execution inputs are invalid or unsupported."""


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


def reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute one validated source-adapter handoff artifact into workspace-local "
            "execution, capture, and extraction records."
        ),
        epilog=(
            "Examples:\n"
            "  python3 tools/scripts/execute_source_adapter.py \\\n"
            "    --handoff runs/plans/local_handoff.jsonl \\\n"
            "    --output runs/acquisition/local-file-run \\\n"
            "    --run-id local-file-run \\\n"
            "    --created-at 2026-06-03T12:34:56Z\n\n"
            "  python3 tools/scripts/execute_source_adapter.py \\\n"
            "    --handoff runs/plans/remote_handoff.jsonl \\\n"
            "    --network-safety-request gate-request.json \\\n"
            "    --output runs/acquisition/remote-gate-check \\\n"
            "    --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--handoff",
        required=True,
        help="Path to a validated source-adapter handoff JSON or JSONL file.",
    )
    parser.add_argument(
        "--adapter",
        required=True,
        help="Trusted source-adapter manifest path used to validate and execute the handoff.",
    )
    parser.add_argument(
        "--output", required=True, help="Workspace-local run directory for execution artifacts."
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Trusted workspace root that the output directory must remain under. "
            "Defaults to the current working directory."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "local", "remote"),
        default="auto",
        help="Force local or remote execution mode. Defaults to auto inference from the handoff variant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and plan intended actions without reading payload content.",
    )
    parser.add_argument(
        "--network-safety-request",
        help="Path to a network safety gate request JSON file. Required for remote URL-manifest handoffs.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "Explicit opt-in for remote URL retrieval after a network safety gate allow decision. "
            "Remote fetch remains disabled by default."
        ),
    )
    parser.add_argument(
        "--suppress-execution-record-stdout",
        action="store_true",
        help=(
            "Skip printing the final execution record to stdout; the run directory still "
            "contains the canonical artifacts."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_REMOTE_TIMEOUT_SECONDS,
        help=f"Remote HTTP timeout in seconds. Defaults to {DEFAULT_REMOTE_TIMEOUT_SECONDS:g}.",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=DEFAULT_REMOTE_MAX_RESPONSE_BYTES,
        help=f"Maximum remote response body bytes to read per URL. Defaults to {DEFAULT_REMOTE_MAX_RESPONSE_BYTES}.",
    )
    parser.add_argument(
        "--run-id", help="Stable run identifier. Defaults to the output directory name."
    )
    parser.add_argument(
        "--created-at",
        help="RFC3339 timestamp override for deterministic tests. Defaults to current UTC time.",
    )
    return parser.parse_args()


def resolve_cli_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def resolve_workspace_root(raw_path: str | None) -> Path:
    if raw_path is None:
        return Path.cwd().resolve()
    return resolve_cli_path(raw_path, base_dir=Path.cwd())


def resolve_output_dir(raw_path: str, *, workspace_root: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace_root / path).resolve()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(payload: str) -> str:
    return sha256_bytes(payload.encode("utf-8"))


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def load_validated_adapter(adapter_path: Path) -> dict[str, Any]:
    result, exit_code = validate_source_adapter.validate_source_adapter(adapter_path)
    if exit_code != validate_source_adapter.EXIT_PASS:
        message = "source adapter validation failed"
        errors = result.get("errors", [])
        if errors:
            message = errors[0].get("message", message)
        raise SourceAcquisitionError(message)
    return json.loads(adapter_path.read_text(encoding="utf-8"))


def load_validated_handoff_records(
    handoff_path: Path, *, adapter_path: Path
) -> tuple[list[dict[str, Any]], str]:
    report, exit_code = validate_source_adapter_handoff.validate_source_adapter_handoff(
        handoff_path, adapter_path=adapter_path
    )
    if exit_code != validate_source_adapter_handoff.EXIT_PASS:
        errors = report.get("errors", [])
        message = errors[0]["message"] if errors else "source-adapter handoff validation failed"
        raise SourceAcquisitionError(message)
    loaded_records, errors, load_exit = validate_source_adapter_handoff.load_records(handoff_path)
    if load_exit != validate_source_adapter_handoff.EXIT_PASS:
        message = errors[0]["message"] if errors else "source-adapter handoff could not be loaded"
        raise SourceAcquisitionError(message)
    records = [record for _, record in loaded_records]
    if not records:
        raise SourceAcquisitionError("handoff artifact does not contain any records")
    return records, sha256_bytes(handoff_path.read_bytes())


def ensure_single_adapter_context(
    records: list[dict[str, Any]], *, expected_adapter_path: Path
) -> None:
    adapter_paths = {str(record.get("adapter_path", "")).strip() for record in records}
    if len(adapter_paths) != 1:
        raise SourceAcquisitionError(
            "handoff artifact must contain records from exactly one adapter_path"
        )
    adapter_path_value = next(iter(adapter_paths))
    if not adapter_path_value:
        raise SourceAcquisitionError("handoff records must include a non-blank adapter_path")
    adapter_path = Path(adapter_path_value).expanduser().resolve()
    if adapter_path != expected_adapter_path.resolve():
        raise SourceAcquisitionError(
            f"handoff adapter_path does not match trusted adapter manifest: {adapter_path}"
        )


def determine_variant(records: list[dict[str, Any]], *, adapter_payload: dict[str, Any]) -> str:
    variants = {
        infer_handoff_variant(record, adapter_payload=adapter_payload) for record in records
    }
    if len(variants) != 1:
        raise SourceAcquisitionError(
            "handoff artifact must not mix source-adapter handoff variants"
        )
    variant = next(iter(variants))
    if variant not in LOCAL_VARIANTS | REMOTE_VARIANTS:
        raise SourceAcquisitionError(f"unsupported source-adapter handoff variant: {variant}")
    return variant


def validate_handoff_sequence(records: list[dict[str, Any]]) -> None:
    if not records:
        raise SourceAcquisitionError("handoff artifact does not contain any records")

    sequences = [record.get("sequence") for record in records]
    if any(
        not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1
        for sequence in sequences
    ):
        raise SourceAcquisitionError("handoff artifact sequence values must be positive integers")
    if len(set(sequences)) != len(sequences):
        raise SourceAcquisitionError("handoff artifact must not repeat sequence values")
    expected_sequences = list(range(1, len(records) + 1))
    if sorted(sequences) != expected_sequences:
        raise SourceAcquisitionError(
            "handoff artifact sequence values must be contiguous starting at 1"
        )


def determine_executor_mode(requested_mode: str, *, variant: str) -> str:
    inferred_mode = "remote" if variant in REMOTE_VARIANTS else "local"
    if requested_mode == "auto":
        return inferred_mode
    if requested_mode != inferred_mode:
        raise SourceAcquisitionError(
            f"mode={requested_mode} does not match handoff variant {variant}"
        )
    return requested_mode


def normalize_created_at(created_at: str | None) -> str:
    if created_at is None:
        return utc_now()
    if not is_rfc3339_datetime(created_at):
        raise SourceAcquisitionError(f"created_at must be an RFC3339 date-time: {created_at}")
    return created_at


def resolve_run_id(output_dir: Path, *, run_id: str | None) -> str:
    if run_id:
        return run_id
    if output_dir.name:
        return output_dir.name
    raise SourceAcquisitionError(
        "--run-id is required when the output path does not have a final path segment"
    )


def ensure_output_dir_within_workspace_root(output_dir: Path, *, workspace_root: Path) -> None:
    try:
        output_dir.resolve().relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise SourceAcquisitionError(
            f"output path escapes the allowed workspace root: {output_dir}"
        ) from exc


def prepare_output_dir(output_dir: Path, *, run_id: str, workspace_root: Path) -> None:
    ensure_output_dir_within_workspace_root(output_dir, workspace_root=workspace_root)
    if output_dir.exists() and not output_dir.is_dir():
        raise SourceAcquisitionError(f"output path exists and is not a directory: {output_dir}")
    if output_dir.exists():
        manifest_path = output_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SourceAcquisitionError(
                    f"existing output manifest could not be read: {manifest_path}"
                ) from exc
            if manifest_payload.get("run_id") != run_id:
                raise SourceAcquisitionError(
                    f"output path already contains artifacts for run_id={manifest_payload.get('run_id')!r}, expected {run_id!r}"
                )
    output_dir.parent.mkdir(parents=True, exist_ok=True)


def make_capture_id(index: int) -> str:
    return f"capture-{index:04d}"


def make_extraction_id(index: int) -> str:
    return f"extraction-{index:04d}"


def is_probably_binary(payload: bytes) -> bool:
    if not payload:
        return False
    return b"\x00" in payload


def is_probably_text(decoded_text: str) -> bool:
    if not decoded_text:
        return True
    sample = decoded_text[:4096]
    text_like = 0
    for char in sample:
        if char.isprintable() or char.isspace():
            text_like += 1
    return text_like / len(sample) >= 0.75


def safe_decode_text(payload: bytes) -> tuple[str | None, str, str | None]:
    if len(payload) > MAX_EXTRACT_TEXT_BYTES:
        return None, "not_attempted", "oversized_payload"
    if is_probably_binary(payload):
        return None, "binary_unsupported", "binaryish_payload"
    try:
        decoded_text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_utf8", "invalid_utf8"
    if not is_probably_text(decoded_text):
        return None, "binary_unsupported", "binaryish_payload"
    return decoded_text, "utf8", None


def stream_local_file_payload(
    path: Path,
) -> tuple[str, int, str | None, str, str | None]:
    digest = hashlib.sha256()
    byte_count = 0
    preview = bytearray()
    saw_binary_byte = False
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
            if not saw_binary_byte and b"\x00" in chunk:
                saw_binary_byte = True
            if len(preview) < MAX_EXTRACT_TEXT_BYTES:
                preview.extend(chunk[: MAX_EXTRACT_TEXT_BYTES - len(preview)])

    content_hash = digest.hexdigest()
    if byte_count > MAX_EXTRACT_TEXT_BYTES:
        return content_hash, byte_count, None, "not_attempted", "oversized_payload"
    if saw_binary_byte:
        return content_hash, byte_count, None, "binary_unsupported", "binaryish_payload"
    try:
        decoded_text = preview.decode("utf-8")
    except UnicodeDecodeError:
        return content_hash, byte_count, None, "invalid_utf8", "invalid_utf8"
    if not is_probably_text(decoded_text):
        return content_hash, byte_count, None, "binary_unsupported", "binaryish_payload"
    return content_hash, byte_count, decoded_text, "utf8", None


def guess_content_type(path: Path, *, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or fallback


def ensure_path_is_local_file(path: Path) -> None:
    if not path.exists():
        raise SourceAcquisitionError(f"local path not found: {path}")
    if path.is_symlink():
        raise SourceAcquisitionError(f"symlink inputs are not supported: {path}")
    if not path.is_file():
        raise SourceAcquisitionError(f"local path is not a file: {path}")


def ensure_path_within_root(path: Path, *, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SourceAcquisitionError(
            f"resolved source path escapes the allowed root: {path}"
        ) from exc


def expected_local_root(adapter_payload: dict[str, Any], *, adapter_path: Path) -> Path:
    locator = adapter_payload.get("locator")
    if not isinstance(locator, dict):
        raise SourceAcquisitionError("source adapter manifest locator must be an object")
    adapter_local_path = locator.get("local_path")
    if not isinstance(adapter_local_path, str) or not adapter_local_path.strip():
        raise SourceAcquisitionError(
            "source adapter manifest local_path must be a non-blank string"
        )
    return resolve_cli_path(adapter_local_path, base_dir=adapter_path.parent)


def load_csv_row_map(payload: bytes) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    row_map: dict[str, dict[str, str]] = {}
    errors: list[dict[str, str]] = []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {}, [{"context": "file", "reason": "file is not valid UTF-8"}]
    try:
        with io.StringIO(text) as handle:
            reader = csv.DictReader(handle, restkey="__EXTRA_FIELDS__")
            if reader.fieldnames is None:
                return {}, [{"context": "line:1", "reason": "csv header row is missing"}]
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                return {}, [{"context": "line:1", "reason": "duplicate CSV header"}]
            for row_index, row in enumerate(reader, start=1):
                if row.get("__EXTRA_FIELDS__"):
                    errors.append(
                        {
                            "context": f"line:{reader.line_num}",
                            "reason": "csv row has extra columns",
                        }
                    )
                    continue
                if any(value is None for key, value in row.items() if key != "__EXTRA_FIELDS__"):
                    errors.append(
                        {
                            "context": f"line:{reader.line_num}",
                            "reason": "csv row is missing one or more required fields",
                        }
                    )
                    continue
                row_map[f"row:{row_index}"] = dict(row)
    except csv.Error as exc:
        errors.append({"context": "file", "reason": str(exc)})
    return row_map, errors


def load_json_record_map(
    payload: bytes, *, record_path: str | None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        decoded_payload = payload.decode("utf-8")
        parsed_payload = json.loads(
            decoded_payload,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except UnicodeDecodeError:
        return {}, [{"context": "file", "reason": "file is not valid UTF-8"}]
    except DuplicateJsonKeyError as exc:
        return {}, [{"context": "line:1", "reason": str(exc)}]
    except json.JSONDecodeError as exc:
        return {}, [{"context": f"line:{exc.lineno},column:{exc.colno}", "reason": exc.msg}]
    except ValueError as exc:
        return {}, [{"context": "line:1", "reason": str(exc)}]

    selected, record_path_error = resolve_json_record_path(parsed_payload, record_path)
    if record_path_error is not None:
        return {}, [{"context": f"record_path:{record_path}", "reason": record_path_error}]
    if isinstance(selected, list):
        return {f"index:{index}": entry for index, entry in enumerate(selected, start=1)}, []
    return {"object:1": selected}, []


def load_jsonl_record_map(payload: bytes) -> tuple[dict[str, Any], list[dict[str, str]]]:
    record_map: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return {}, [{"context": "file", "reason": "file is not valid UTF-8"}]
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(
                raw_line,
                object_pairs_hook=no_duplicate_object_pairs,
                parse_constant=reject_json_constant,
            )
        except DuplicateJsonKeyError as exc:
            errors.append({"context": f"line:{line_number}", "reason": str(exc)})
            continue
        except json.JSONDecodeError as exc:
            errors.append({"context": f"line:{line_number},column:{exc.colno}", "reason": exc.msg})
            continue
        except ValueError as exc:
            errors.append({"context": f"line:{line_number}", "reason": str(exc)})
            continue
        record_map[f"line:{line_number}"] = value
    return record_map, errors


def load_xml_record_map(
    payload: bytes, *, record_path: str | None
) -> tuple[dict[str, ET.Element], list[dict[str, str]]]:
    try:
        root = ET.fromstring(payload)
    except UnicodeDecodeError:
        return {}, [{"context": "file", "reason": "file is not valid UTF-8"}]
    except ET.ParseError as exc:
        line_number, column = getattr(exc, "position", (1, 1))
        return {}, [{"context": f"line:{line_number},column:{column}", "reason": str(exc)}]

    path_map = build_xml_path_map(root)
    if record_path:
        matches = root.findall(record_path)
        if not matches:
            return {}, [
                {
                    "context": f"record_path:{record_path}",
                    "reason": "record_path matched no XML elements",
                }
            ]
    else:
        matches = list(root) or [root]
    return {
        path_map.get(id(element), f"element:{index}"): element
        for index, element in enumerate(matches, start=1)
    }, []


def load_structured_record_map(
    payload: bytes, *, structured_format: str, record_path: str | None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if structured_format == "csv":
        return load_csv_row_map(payload)
    if structured_format == "json":
        return load_json_record_map(payload, record_path=record_path)
    if structured_format == "jsonl":
        return load_jsonl_record_map(payload)
    if structured_format == "xml":
        return load_xml_record_map(payload, record_path=record_path)
    return {}, [
        {"context": "file", "reason": f"unsupported structured format: {structured_format}"}
    ]


def structured_record_map_cache_key(
    source_path_value: str, structured_format: str, record_path: str | None
) -> tuple[str, str, str]:
    return source_path_value, structured_format, record_path or ""


def serialize_structured_value(value: Any) -> str:
    if isinstance(value, ET.Element):
        body = ET.tostring(value, encoding="unicode")
        return body if body.endswith("\n") else body + "\n"
    body = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return body if body.endswith("\n") else body + "\n"


def build_extraction_record(
    *,
    extraction_id: str,
    run_id: str,
    capture_id: str,
    adapter_payload: dict[str, Any],
    adapter_type: str,
    handoff_sequence: int,
    relative_path: str,
    input_hash: str | None,
    byte_count_in: int,
    extraction_method: str,
    hazard_flags: list[str],
    content_text: str | None,
    encoding_result: str,
    failure_reason: str | None,
    extracted_text_path: str | None,
    status_override: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content_hash = sha256_text(content_text) if content_text is not None else None
    byte_count_out = len(content_text.encode("utf-8")) if content_text is not None else 0
    status = status_override or (
        "completed"
        if content_text is not None and failure_reason is None
        else "skipped"
        if failure_reason == "oversized_payload"
        else "failed"
    )
    if status not in SAFE_TEXT_STATUS:
        raise SourceAcquisitionError(f"unsupported extraction status: {status}")
    payload = {
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "extraction_id": extraction_id,
        "run_id": run_id,
        "capture_id": capture_id,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "adapter_type": adapter_type,
        "handoff_sequence": handoff_sequence,
        "relative_path": relative_path,
        "extraction_method": extraction_method,
        "input_hash": input_hash,
        "content_hash": content_hash,
        "byte_count_in": byte_count_in,
        "byte_count_out": byte_count_out,
        "encoding_result": encoding_result,
        "truncation_status": "refused_oversize"
        if failure_reason == "oversized_payload"
        else "not_truncated",
        "hostile_replay_flags": hazard_flags,
        "failure_reason": failure_reason,
        "extracted_text_path": extracted_text_path,
        "status": status,
        "canonical_persistence_attempted": False,
        "verification_status": "unverified",
    }
    if extra_fields:
        payload.update(extra_fields)
    return payload


def build_remote_denied_capture_event(
    *,
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
    run_id: str,
    handoff_hash: str,
    created_at: str,
    capture_id: str,
    url: str,
    method: str,
    failure_reason: str,
    user_agent: str,
) -> dict[str, Any]:
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": capture_id,
        "run_id": run_id,
        "handoff_hash": handoff_hash,
        "handoff_sequences": [record["sequence"]],
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "adapter_type": "remote_url_manifest",
        "source_reference": {
            "relative_path": record["relative_path"],
            "remote_url": url,
            "manifest_url": record["source_specific"].get("manifest_url"),
        },
        "original_locator": record["preserved"]["original_locator"],
        "normalized_url": urlparse(url).geturl(),
        "final_url": url,
        "redirect_count": 0,
        "http_status_code": None,
        "request_method": method,
        "user_agent": user_agent,
        "content_hash": None,
        "byte_count": 0,
        "content_length_header": None,
        "content_type": "application/octet-stream",
        "captured_at": created_at,
        "capture_method": "remote_url_fetch",
        "transient_payload_path": None,
        "payload_retention_policy": "transient_run_artifact",
        "network_access_attempted": True,
        "rights_posture": record["preserved"].get("rights_posture"),
        "status": "denied",
        "failure_reason": failure_reason,
        "canonical_persistence_attempted": False,
        "verification_status": "unverified",
    }


def build_remote_denied_extraction_record(
    *,
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
    extraction_id: str,
    run_id: str,
    capture_id: str,
    url: str,
    method: str,
    failure_reason: str,
) -> dict[str, Any]:
    return build_extraction_record(
        extraction_id=extraction_id,
        run_id=run_id,
        capture_id=capture_id,
        adapter_payload=adapter_payload,
        adapter_type="remote_url_manifest",
        handoff_sequence=record["sequence"],
        relative_path=record["relative_path"],
        input_hash=None,
        byte_count_in=0,
        extraction_method="remote_text_extract",
        hazard_flags=list(record["preserved"].get("source_metadata", {}).get("hazard_flags", [])),
        content_text=None,
        encoding_result="not_attempted",
        failure_reason=failure_reason,
        extracted_text_path=None,
        status_override="denied",
        extra_fields={
            "content_type": "application/octet-stream",
            "remote_url": url,
            "final_url": url,
            "network_access_attempted": True,
            "request_method": method,
        },
    )


def dry_run_execution_record(
    *,
    run_id: str,
    created_at: str,
    handoff_path: Path,
    handoff_hash: str,
    adapter_payload: dict[str, Any],
    adapter_type: str,
    executor_mode: str,
    local_input_paths: list[str],
    gate_report: dict[str, Any] | None,
    planned_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    output_artifacts = {
        "execution_record": "execution-record.json",
        "capture_events": "capture-events.jsonl",
        "extraction_records": "extraction-records.jsonl",
        "manifest": "manifest.json",
        "denial_record": None,
        "network_safety_report": "network-safety-report.json" if gate_report is not None else None,
    }
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "executor_name": EXECUTOR_NAME,
        "executor_mode": executor_mode,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "adapter_type": adapter_type,
        "handoff_path": str(handoff_path),
        "input_handoff_hash": handoff_hash,
        "dry_run": True,
        "status": "dry_run",
        "network_access_attempted": False,
        "network_access_allowed": gate_report["execution_allowed"]
        if gate_report is not None
        else False,
        "network_access_denied_reason": None,
        "network_safety_gate": summarize_gate_report(gate_report)
        if gate_report is not None
        else None,
        "local_input_paths_processed": local_input_paths,
        "planned_actions": planned_actions,
        "capture_event_count": 0,
        "extraction_record_count": 0,
        "output_artifacts": output_artifacts,
        "canonical_persistence_attempted": False,
        "verification_status": "unverified",
    }


def summarize_gate_report(gate_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": gate_report["schema_version"],
        "decision": gate_report["decision"],
        "execution_allowed": gate_report["execution_allowed"],
        "error_count": gate_report["counts"]["errors"],
        "warning_count": gate_report["counts"]["warnings"],
        "report_path": "network-safety-report.json",
    }


def execution_record_payload(
    *,
    run_id: str,
    created_at: str,
    handoff_path: Path,
    handoff_hash: str,
    adapter_payload: dict[str, Any],
    adapter_type: str,
    executor_mode: str,
    dry_run: bool,
    status: str,
    network_access_attempted: bool,
    network_access_allowed: bool,
    network_access_denied_reason: str | None,
    gate_report: dict[str, Any] | None,
    local_input_paths: list[str],
    planned_actions: list[dict[str, Any]],
    capture_events: list[dict[str, Any]],
    extraction_records: list[dict[str, Any]],
    denial_record_written: bool,
) -> dict[str, Any]:
    if status not in SAFE_RUN_STATUS:
        raise SourceAcquisitionError(f"unsupported execution status: {status}")
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "executor_name": EXECUTOR_NAME,
        "executor_mode": executor_mode,
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "adapter_type": adapter_type,
        "handoff_path": str(handoff_path),
        "input_handoff_hash": handoff_hash,
        "dry_run": dry_run,
        "status": status,
        "network_access_attempted": network_access_attempted,
        "network_access_allowed": network_access_allowed,
        "network_access_denied_reason": network_access_denied_reason,
        "network_safety_gate": summarize_gate_report(gate_report)
        if gate_report is not None
        else None,
        "local_input_paths_processed": local_input_paths,
        "planned_actions": planned_actions,
        "capture_event_count": len(capture_events),
        "extraction_record_count": len(extraction_records),
        "output_artifacts": {
            "execution_record": "execution-record.json",
            "capture_events": "capture-events.jsonl",
            "extraction_records": "extraction-records.jsonl",
            "manifest": "manifest.json",
            "denial_record": "denial-record.json" if denial_record_written else None,
            "network_safety_report": "network-safety-report.json"
            if gate_report is not None
            else None,
        },
        "canonical_persistence_attempted": False,
        "verification_status": "unverified",
    }


def build_manifest(
    run_id: str, *, created_at: str, status: str, output_artifacts: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": "source-acquisition-run-manifest.v1",
        "run_id": run_id,
        "created_at": created_at,
        "status": status,
        "artifacts": output_artifacts,
        "canonical_persistence_attempted": False,
    }


def write_text_artifacts(output_dir: Path, text_artifacts: dict[str, str]) -> None:
    for relative_path, body in text_artifacts.items():
        atomic_write_text(output_dir / relative_path, body)


def write_binary_artifacts(output_dir: Path, binary_artifacts: dict[str, bytes | Path]) -> None:
    for relative_path, payload in binary_artifacts.items():
        target = (output_dir / relative_path).resolve()
        try:
            target.relative_to(output_dir.resolve())
        except ValueError as exc:
            raise SourceAcquisitionError(
                f"binary artifact path escapes output directory: {relative_path}"
            ) from exc
        if isinstance(payload, Path):
            atomic_write_path(target, payload)
        else:
            atomic_write_bytes(target, payload)


def clear_directory_contents(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def publish_output_dir(staging_dir: Path, output_dir: Path) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise SourceAcquisitionError(f"output path exists and is not a directory: {output_dir}")
    if output_dir.exists():
        backup_dir = Path(
            tempfile.mkdtemp(prefix=f".{output_dir.name}.previous.", dir=output_dir.parent)
        )
        shutil.rmtree(backup_dir)
        output_dir.replace(backup_dir)
        try:
            staging_dir.replace(output_dir)
        except Exception:
            backup_dir.replace(output_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
        return
    staging_dir.replace(output_dir)


def write_execution_artifacts(
    *,
    output_dir: Path,
    execution_record: dict[str, Any],
    capture_events: list[dict[str, Any]],
    extraction_records: list[dict[str, Any]],
    denial_record: dict[str, Any] | None,
    gate_report: dict[str, Any] | None,
    text_artifacts: dict[str, str],
    binary_artifacts: dict[str, bytes | Path] | None = None,
) -> None:
    clear_directory_contents(output_dir)
    atomic_write_json(output_dir / "execution-record.json", execution_record)
    atomic_write_jsonl(output_dir / "capture-events.jsonl", capture_events)
    atomic_write_jsonl(output_dir / "extraction-records.jsonl", extraction_records)
    if denial_record is not None:
        atomic_write_json(output_dir / "denial-record.json", denial_record)
    if gate_report is not None:
        atomic_write_json(output_dir / "network-safety-report.json", gate_report)
    write_binary_artifacts(output_dir, binary_artifacts or {})
    write_text_artifacts(output_dir, text_artifacts)
    manifest = build_manifest(
        execution_record["run_id"],
        created_at=execution_record["created_at"],
        status=execution_record["status"],
        output_artifacts=execution_record["output_artifacts"],
    )
    atomic_write_json(output_dir / "manifest.json", manifest)


def validate_emitted_artifacts(output_dir: Path) -> None:
    validator_proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "scripts" / "validate_source_acquisition_execution.py"),
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if validator_proc.returncode != 0:
        raise SourceAcquisitionError(
            validator_proc.stdout.strip()
            or validator_proc.stderr.strip()
            or "execution artifact validation failed"
        )


def planned_actions_for_records(
    records: list[dict[str, Any]], *, variant: str
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if variant == "remote_url_manifest":
        for record in records:
            original_locator = record["preserved"]["original_locator"]
            actions.append(
                {
                    "action_id": f"fetch-{record['sequence']}",
                    "action_kind": "fetch_payload",
                    "method": "GET",
                    "sequence": record["sequence"],
                    "url": original_locator["entry_url"],
                    "manifest_url": record["source_specific"]["manifest_url"],
                }
            )
        return actions
    for record in records:
        actions.append(
            {
                "action_kind": "read_local_source",
                "sequence": record["sequence"],
                "resolved_source_path": record["resolved_source_path"],
                "relative_path": record["relative_path"],
            }
        )
    return actions


def execute_local_source(
    *,
    records: list[dict[str, Any]],
    adapter_payload: dict[str, Any],
    adapter_path: Path,
    run_id: str,
    created_at: str,
    handoff_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], list[str], bool]:
    capture_events: list[dict[str, Any]] = []
    extraction_records: list[dict[str, Any]] = []
    text_artifacts: dict[str, str] = {}
    local_paths: list[str] = []
    failed = False
    root = expected_local_root(adapter_payload, adapter_path=adapter_path)
    capture_method = (
        "local_file_copy"
        if adapter_payload["input_family"] == "local_file"
        else "local_directory_walk"
    )

    for index, record in enumerate(
        sorted(records, key=lambda item: int(item["sequence"])), start=1
    ):
        source_path = Path(record["resolved_source_path"]).expanduser().resolve()
        if adapter_payload["input_family"] == "local_file":
            if source_path != root:
                raise SourceAcquisitionError(
                    f"local file handoff does not match adapter root: {source_path}"
                )
        else:
            ensure_path_within_root(source_path, root=root)
        capture_id = make_capture_id(index)
        local_paths.append(str(source_path))
        original_locator = record["preserved"]["original_locator"]
        try:
            ensure_path_is_local_file(source_path)
            if adapter_payload["input_family"] == "local_file":
                content_hash, byte_count, extracted_text, encoding_result, failure_reason = (
                    stream_local_file_payload(source_path)
                )
            else:
                payload = source_path.read_bytes()
                content_hash = sha256_bytes(payload)
                byte_count = len(payload)
                extracted_text, encoding_result, failure_reason = safe_decode_text(payload)

            capture_event = {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "capture_id": capture_id,
                "run_id": run_id,
                "handoff_hash": handoff_hash,
                "handoff_sequences": [record["sequence"]],
                "adapter_id": adapter_payload["adapter_id"],
                "workspace_id": adapter_payload["workspace_id"],
                "adapter_type": "local_source",
                "source_reference": {
                    "relative_path": record["relative_path"],
                    "resolved_source_path": str(source_path),
                },
                "original_locator": original_locator,
                "normalized_local_path": str(source_path),
                "content_hash": content_hash,
                "byte_count": byte_count,
                "content_type": guess_content_type(source_path),
                "captured_at": created_at,
                "capture_method": capture_method,
                "transient_payload_path": None,
                "rights_posture": record["preserved"].get("rights_posture"),
                "status": "completed",
                "canonical_persistence_attempted": False,
                "verification_status": "unverified",
            }
            capture_events.append(capture_event)
            extraction_id = make_extraction_id(len(extraction_records) + 1)
            extracted_text_path = None
            if extracted_text is not None:
                extracted_text_path = f"extracted-text/{extraction_id}.txt"
                text_artifacts[extracted_text_path] = extracted_text
            else:
                failed = True
            extraction_records.append(
                build_extraction_record(
                    extraction_id=extraction_id,
                    run_id=run_id,
                    capture_id=capture_id,
                    adapter_payload=adapter_payload,
                    adapter_type="local_source",
                    handoff_sequence=record["sequence"],
                    relative_path=record["relative_path"],
                    input_hash=capture_event["content_hash"],
                    byte_count_in=capture_event["byte_count"],
                    extraction_method="utf8_text_extract",
                    hazard_flags=list(
                        record["preserved"].get("source_metadata", {}).get("hazard_flags", [])
                    ),
                    content_text=extracted_text,
                    encoding_result=encoding_result,
                    failure_reason=failure_reason,
                    extracted_text_path=extracted_text_path,
                )
            )
        except SourceAcquisitionError as exc:
            failed = True
            capture_events.append(
                {
                    "schema_version": CAPTURE_SCHEMA_VERSION,
                    "capture_id": capture_id,
                    "run_id": run_id,
                    "handoff_hash": handoff_hash,
                    "handoff_sequences": [record["sequence"]],
                    "adapter_id": adapter_payload["adapter_id"],
                    "workspace_id": adapter_payload["workspace_id"],
                    "adapter_type": "local_source",
                    "source_reference": {
                        "relative_path": record["relative_path"],
                        "resolved_source_path": str(source_path),
                    },
                    "original_locator": original_locator,
                    "normalized_local_path": str(source_path),
                    "content_hash": None,
                    "byte_count": 0,
                    "content_type": guess_content_type(source_path),
                    "captured_at": created_at,
                    "capture_method": capture_method,
                    "transient_payload_path": None,
                    "rights_posture": record["preserved"].get("rights_posture"),
                    "status": "failed",
                    "failure_reason": str(exc),
                    "canonical_persistence_attempted": False,
                    "verification_status": "unverified",
                }
            )
    return capture_events, extraction_records, text_artifacts, local_paths, failed


def execute_structured_data(
    *,
    records: list[dict[str, Any]],
    adapter_payload: dict[str, Any],
    adapter_path: Path,
    run_id: str,
    created_at: str,
    handoff_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], list[str], bool]:
    capture_events: list[dict[str, Any]] = []
    extraction_records: list[dict[str, Any]] = []
    text_artifacts: dict[str, str] = {}
    local_paths: list[str] = []
    failed = False
    record_path = adapter_payload["locator"].get("record_path")
    capture_index_by_path: dict[str, str] = {}
    capture_hash_by_path: dict[str, str] = {}
    capture_size_by_path: dict[str, int] = {}
    record_map_cache: dict[tuple[str, str], tuple[dict[str, Any], list[dict[str, str]]]] = {}
    records_by_source_path: dict[str, list[dict[str, Any]]] = {}
    root = expected_local_root(adapter_payload, adapter_path=adapter_path)

    for record in sorted(records, key=lambda item: int(item["sequence"])):
        records_by_source_path.setdefault(record["resolved_source_path"], []).append(record)

    source_items = sorted(records_by_source_path.items(), key=lambda item: item[0])
    for capture_index, (source_path_value, grouped_records) in enumerate(source_items, start=1):
        if adapter_payload["input_family"] == "local_file":
            expected_path = root
            source_path = Path(source_path_value).expanduser().resolve()
            if source_path != expected_path:
                raise SourceAcquisitionError(
                    f"structured local file handoff does not match adapter root: {source_path}"
                )
        else:
            source_path = Path(source_path_value).expanduser().resolve()
            ensure_path_within_root(source_path, root=root)
        ensure_path_is_local_file(source_path)
        payload = source_path.read_bytes()
        capture_id = make_capture_id(capture_index)
        capture_index_by_path[source_path_value] = capture_id
        capture_hash_by_path[source_path_value] = sha256_bytes(payload)
        capture_size_by_path[source_path_value] = len(payload)
        local_paths.append(str(source_path))
        capture_events.append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "capture_id": capture_id,
                "run_id": run_id,
                "handoff_hash": handoff_hash,
                "handoff_sequences": [record["sequence"] for record in grouped_records],
                "adapter_id": adapter_payload["adapter_id"],
                "workspace_id": adapter_payload["workspace_id"],
                "adapter_type": "structured_data",
                "source_reference": {
                    "relative_path": grouped_records[0]["relative_path"],
                    "resolved_source_path": str(source_path),
                },
                "original_locator": grouped_records[0]["preserved"]["original_locator"],
                "normalized_local_path": str(source_path),
                "content_hash": capture_hash_by_path[source_path_value],
                "byte_count": capture_size_by_path[source_path_value],
                "content_type": guess_content_type(source_path),
                "captured_at": created_at,
                "capture_method": "structured_data_load",
                "transient_payload_path": None,
                "rights_posture": grouped_records[0]["preserved"].get("rights_posture"),
                "status": "completed",
                "canonical_persistence_attempted": False,
                "verification_status": "unverified",
            }
        )
        structured_format = grouped_records[0]["source_specific"]["structured_format"]
        cache_key = structured_record_map_cache_key(
            source_path_value, structured_format, record_path
        )
        record_map, parse_errors = load_structured_record_map(
            payload, structured_format=structured_format, record_path=record_path
        )
        record_map_cache[cache_key] = (record_map, parse_errors)

    for record in sorted(records, key=lambda item: int(item["sequence"])):
        capture_id = capture_index_by_path[record["resolved_source_path"]]
        capture_hash = capture_hash_by_path[record["resolved_source_path"]]
        capture_size = capture_size_by_path[record["resolved_source_path"]]
        structured_format = record["source_specific"]["structured_format"]
        record_locator = record["source_specific"]["record_locator"]
        record_map, parse_errors = record_map_cache[
            structured_record_map_cache_key(
                record["resolved_source_path"], structured_format, record_path
            )
        ]
        extraction_id = make_extraction_id(len(extraction_records) + 1)
        value = record_map.get(record_locator)
        extracted_text = None
        encoding_result = "structured_data"
        failure_reason = None
        if value is None:
            failure_reason = "record_locator_not_found"
            failed = True
        else:
            serialized = serialize_structured_value(value)
            if len(serialized.encode("utf-8")) > MAX_EXTRACT_TEXT_BYTES:
                failure_reason = "oversized_payload"
                failed = True
            else:
                extracted_text = serialized
        extracted_text_path = None
        if extracted_text is not None:
            extracted_text_path = f"extracted-text/{extraction_id}.txt"
            text_artifacts[extracted_text_path] = extracted_text
        elif parse_errors:
            encoding_result = "structured_data_with_parse_errors"
        extraction_records.append(
            build_extraction_record(
                extraction_id=extraction_id,
                run_id=run_id,
                capture_id=capture_id,
                adapter_payload=adapter_payload,
                adapter_type="structured_data",
                handoff_sequence=record["sequence"],
                relative_path=record["relative_path"],
                input_hash=capture_hash,
                byte_count_in=capture_size,
                extraction_method="structured_record_extract",
                hazard_flags=list(
                    record["preserved"].get("source_metadata", {}).get("hazard_flags", [])
                ),
                content_text=extracted_text,
                encoding_result=encoding_result,
                failure_reason=failure_reason,
                extracted_text_path=extracted_text_path,
                extra_fields={
                    "structured_format": structured_format,
                    "record_locator": record_locator,
                    "record_kind": record["source_specific"]["record_kind"],
                    "parse_error_count": len(parse_errors),
                },
            )
        )
    return capture_events, extraction_records, text_artifacts, sorted(local_paths), failed


def compute_git_snapshot_hash(
    file_entries: list[dict[str, Any]], *, git_ref: str, git_commit: str
) -> str:
    ordered_file_entries = sorted(file_entries, key=lambda item: item["relative_path"])
    payload = {
        "git_ref": git_ref,
        "git_commit": git_commit,
        "files": ordered_file_entries,
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def inspect_git_repo_for_execution(
    repo_path: Path, *, git_ref: str, git_commit: str
) -> tuple[str, str]:
    rev_parse = git_command(repo_path, "rev-parse", "--verify", f"{git_ref}^{{commit}}")
    if rev_parse.returncode != 0:
        raise SourceAcquisitionError(
            f"configured git ref could not be resolved during execution: {git_ref}"
        )
    resolved_commit = rev_parse.stdout.strip()
    if resolved_commit != git_commit:
        raise SourceAcquisitionError(
            f"local git checkout no longer matches planned commit {git_commit}; resolved {resolved_commit}"
        )
    status_proc = git_command(repo_path, "status", "--porcelain")
    if status_proc.returncode != 0:
        raise SourceAcquisitionError(f"git status failed for local checkout: {repo_path}")
    repo_state = "dirty" if status_proc.stdout.strip() else "clean"
    if repo_state != "clean":
        raise SourceAcquisitionError("git working tree has local modifications or untracked files")
    return resolved_commit, repo_state


def execute_local_git_repo(
    *,
    records: list[dict[str, Any]],
    adapter_payload: dict[str, Any],
    run_id: str,
    created_at: str,
    handoff_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], list[str], bool]:
    if len(records) != 1:
        raise SourceAcquisitionError(
            "local_git_repo execution expects exactly one snapshot handoff record"
        )
    record = records[0]
    repo_path = Path(record["resolved_source_path"]).expanduser().resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        raise SourceAcquisitionError(f"local git repository path not found: {repo_path}")
    if repo_path.is_symlink():
        raise SourceAcquisitionError(f"symlink git repositories are not supported: {repo_path}")
    git_ref = record["source_specific"]["git_ref"]
    git_commit = record["source_specific"]["git_commit"]
    resolved_commit, repo_state = inspect_git_repo_for_execution(
        repo_path, git_ref=git_ref, git_commit=git_commit
    )
    candidate_paths = list(
        record["preserved"].get("source_metadata", {}).get("candidate_paths", [])
    )
    file_entries: list[dict[str, Any]] = []
    file_payloads_by_path: dict[str, tuple[str, int, str | None, str, str | None]] = {}
    extraction_records: list[dict[str, Any]] = []
    text_artifacts: dict[str, str] = {}
    local_paths: list[str] = []
    failed = False
    for relative_path in candidate_paths:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise SourceAcquisitionError(
                "local_git_repo candidate_paths must contain non-blank strings"
            )
        file_path = (repo_path / relative_path).resolve()
        ensure_path_within_root(file_path, root=repo_path)
        ensure_path_is_local_file(file_path)
        payload = file_path.read_bytes()
        content_hash = sha256_bytes(payload)
        byte_count = len(payload)
        extracted_text, encoding_result, failure_reason = safe_decode_text(payload)
        file_entries.append(
            {
                "relative_path": relative_path,
                "content_hash": content_hash,
                "byte_count": byte_count,
            }
        )
        file_payloads_by_path[relative_path] = (
            content_hash,
            byte_count,
            extracted_text,
            encoding_result,
            failure_reason,
        )
        local_paths.append(str(file_path))
    snapshot_hash = compute_git_snapshot_hash(
        file_entries, git_ref=git_ref, git_commit=resolved_commit
    )
    snapshot_byte_count = sum(entry["byte_count"] for entry in file_entries)
    capture_event = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": make_capture_id(1),
        "run_id": run_id,
        "handoff_hash": handoff_hash,
        "handoff_sequences": [record["sequence"]],
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "adapter_type": "local_git_repo",
        "source_reference": {
            "relative_path": record["relative_path"],
            "resolved_source_path": str(repo_path),
            "candidate_paths": candidate_paths,
        },
        "original_locator": record["preserved"]["original_locator"],
        "normalized_local_path": str(repo_path),
        "content_hash": snapshot_hash,
        "byte_count": snapshot_byte_count,
        "content_type": "application/x-git-local-checkout",
        "captured_at": created_at,
        "capture_method": "local_git_snapshot",
        "transient_payload_path": None,
        "rights_posture": record["preserved"].get("rights_posture"),
        "repo_state": repo_state,
        "git_ref": git_ref,
        "git_commit": resolved_commit,
        "status": "completed",
        "canonical_persistence_attempted": False,
        "verification_status": "unverified",
    }
    for file_entry in file_entries:
        (
            _content_hash,
            _byte_count,
            extracted_text,
            encoding_result,
            failure_reason,
        ) = file_payloads_by_path[file_entry["relative_path"]]
        extraction_id = make_extraction_id(len(extraction_records) + 1)
        extracted_text_path = None
        if extracted_text is not None:
            extracted_text_path = f"extracted-text/{extraction_id}.txt"
            text_artifacts[extracted_text_path] = extracted_text
        else:
            failed = True
        extraction_records.append(
            build_extraction_record(
                extraction_id=extraction_id,
                run_id=run_id,
                capture_id=capture_event["capture_id"],
                adapter_payload=adapter_payload,
                adapter_type="local_git_repo",
                handoff_sequence=record["sequence"],
                relative_path=file_entry["relative_path"],
                input_hash=capture_event["content_hash"],
                byte_count_in=snapshot_byte_count,
                extraction_method="git_file_text_extract",
                hazard_flags=list(
                    record["preserved"].get("source_metadata", {}).get("hazard_flags", [])
                ),
                content_text=extracted_text,
                encoding_result=encoding_result,
                failure_reason=failure_reason,
                extracted_text_path=extracted_text_path,
                extra_fields={
                    "git_ref": git_ref,
                    "git_commit": resolved_commit,
                },
            )
        )
    return [capture_event], extraction_records, text_artifacts, sorted(local_paths), failed


def extract_remote_urls(records: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for record in records:
        original_locator = record.get("preserved", {}).get("original_locator", {})
        entry_url = original_locator.get("entry_url")
        if isinstance(entry_url, str) and entry_url.strip():
            urls.append(entry_url)
    if not urls:
        raise SourceAcquisitionError(
            "remote_url_manifest handoffs must include preserved.original_locator.entry_url"
        )
    return list(dict.fromkeys(urls))


def gate_action_by_url(gate_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}
    for action in gate_report.get("planned_actions", []):
        if isinstance(action, dict) and isinstance(action.get("url"), str):
            actions[action["url"]] = action
    return actions


def ensure_gate_request_matches_handoff(
    gate_report: dict[str, Any], *, expected_urls: list[str]
) -> None:
    expected_actions = [
        {
            "action_id": f"fetch-{index}",
            "action_kind": "fetch_payload",
            "method": "GET",
            "url": url,
        }
        for index, url in enumerate(dict.fromkeys(expected_urls), start=1)
    ]
    planned_actions = [
        {
            "action_id": str(action.get("action_id")),
            "action_kind": str(action.get("action_kind")),
            "method": str(action.get("method")).upper(),
            "url": str(action.get("url")),
        }
        for action in gate_report.get("planned_actions", [])
        if isinstance(action, dict)
        and isinstance(action.get("action_id"), str)
        and isinstance(action.get("action_kind"), str)
        and isinstance(action.get("method"), str)
        and isinstance(action.get("url"), str)
    ]
    expected_set = {
        (action["action_id"], action["action_kind"], action["method"], action["url"])
        for action in expected_actions
    }
    planned_set = {
        (action["action_id"], action["action_kind"], action["method"], action["url"])
        for action in planned_actions
    }
    missing = sorted(expected_set - planned_set)
    extra = sorted(planned_set - expected_set)
    if missing or extra or len(planned_actions) != len(expected_actions):
        details: list[str] = []
        if missing:
            details.append(f"missing planned actions for: {missing[0][3]}")
        if extra:
            details.append(f"includes unexpected planned actions for: {extra[0][3]}")
        if not details:
            details.append("planned action set does not exactly match handoff order")
        raise SourceAcquisitionError("network safety gate request " + "; ".join(details))


def build_denial_record(
    execution_record: dict[str, Any], *, considered_urls: list[str]
) -> dict[str, Any]:
    payload = dict(execution_record)
    payload["considered_urls"] = considered_urls
    return payload


class NoAutoRedirectHandler(HTTPRedirectHandler):
    """Expose redirects to the executor so every hop can be allowlist checked."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def extract_content_type(headers: Any) -> str:
    response_headers = normalize_response_headers(headers)
    value = response_headers.get("content-type")
    if not isinstance(value, str) or not value.strip():
        return "application/octet-stream"
    return value.split(";", 1)[0].strip().casefold() or "application/octet-stream"


def normalize_response_headers(headers: Any) -> dict[str, Any]:
    if headers is None:
        return {}
    if hasattr(headers, "items"):
        try:
            items = headers.items()
        except Exception:
            return {}
        return {str(key).casefold(): value for key, value in items if key is not None}
    if isinstance(headers, dict):
        return {str(key).casefold(): value for key, value in headers.items() if key is not None}
    return {}


def is_extractable_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", 1)[0].strip().casefold()
    return (
        normalized.startswith("text/")
        or normalized
        in {"application/json", "application/xml", "application/xhtml+xml", "application/csv"}
        or normalized.endswith("+json")
        or normalized.endswith("+xml")
    )


def read_limited_response(response: Any, *, max_response_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    limit = max_response_bytes + 1
    while total <= max_response_bytes:
        chunk = response.read(min(64 * 1024, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_response_bytes:
            break
    payload = b"".join(chunks)
    if len(payload) > max_response_bytes:
        return payload[:max_response_bytes], True
    return payload, False


def spool_limited_response(
    response: Any, *, max_response_bytes: int, spool_dir: Path
) -> tuple[Path | None, str | None, int, bool]:
    spool_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    limit = max_response_bytes + 1
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            delete=False,
            dir=spool_dir,
            prefix=".remote-payload-",
            suffix=".bin.tmp",
        ) as handle:
            temp_path = Path(handle.name)
            while total <= max_response_bytes:
                chunk = response.read(min(64 * 1024, limit - total))
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                if total > max_response_bytes:
                    break
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    if temp_path is None:
        raise SourceAcquisitionError("failed to spool remote payload")
    if total > max_response_bytes:
        temp_path.unlink(missing_ok=True)
        return None, None, 0, True
    return temp_path, digest.hexdigest(), total, False


def gate_allowlist(gate_report: dict[str, Any]) -> tuple[list[str], list[str]]:
    checks = gate_report.get("checks", {})
    request_allowlist = checks.get("allowlist")
    if isinstance(request_allowlist, dict):
        hosts = [item for item in request_allowlist.get("hosts", []) if isinstance(item, str)]
        prefixes = [
            item for item in request_allowlist.get("url_prefixes", []) if isinstance(item, str)
        ]
        return hosts, prefixes
    # Older gate reports do not echo the allowlist. In that case every planned
    # URL has already been checked, but redirects must be refused.
    return [], []


def remote_fetch_one(
    *,
    url: str,
    method: str,
    user_agent: str,
    allowlist_hosts: list[str],
    allowlist_prefixes: list[str],
    timeout_seconds: float,
    max_response_bytes: int,
    payload_spool_dir: Path,
    opener: Any | None = None,
) -> dict[str, Any]:
    opener = opener if opener is not None else build_opener(NoAutoRedirectHandler)
    current_url = url
    redirect_count = 0
    attempted_urls: list[str] = []

    while True:
        attempted_urls.append(current_url)
        request = Request(current_url, method=method, headers={"User-Agent": user_agent})
        payload_path: Path | None = None
        payload_sha256: str | None = None
        payload_byte_count = 0
        truncated = False
        try:
            response = opener.open(request, timeout=timeout_seconds)
            status_code = int(getattr(response, "status", response.getcode()))
            headers = response.headers
            if method != "HEAD":
                payload_path, payload_sha256, payload_byte_count, truncated = (
                    spool_limited_response(
                        response,
                        max_response_bytes=max_response_bytes,
                        spool_dir=payload_spool_dir,
                    )
                )
        except HTTPError as exc:
            status_code = int(exc.code)
            headers = exc.headers
            normalized_headers = normalize_response_headers(headers)
            location = normalized_headers.get("location")
            if 300 <= status_code < 400 and location:
                redirect_count += 1
                redirected_url = urljoin(current_url, str(location))
                if redirect_count > MAX_REMOTE_REDIRECTS:
                    return {
                        "status": "failed",
                        "failure_reason": "too_many_redirects",
                        "http_status_code": status_code,
                        "final_url": current_url,
                        "redirect_target": redirected_url,
                        "redirect_count": redirect_count,
                        "attempted_urls": attempted_urls,
                        "payload_path": None,
                        "payload_sha256": None,
                        "payload_byte_count": 0,
                        "truncated": False,
                        "headers": headers,
                    }
                if not allowlist_hosts and not allowlist_prefixes:
                    return {
                        "status": "failed",
                        "failure_reason": "redirects_refused_without_echoed_allowlist",
                        "http_status_code": status_code,
                        "final_url": current_url,
                        "redirect_target": redirected_url,
                        "redirect_count": redirect_count,
                        "attempted_urls": attempted_urls,
                        "payload_path": None,
                        "payload_sha256": None,
                        "payload_byte_count": 0,
                        "truncated": False,
                        "headers": headers,
                    }
                if not allowlisted(redirected_url, allowlist_hosts, allowlist_prefixes):
                    return {
                        "status": "failed",
                        "failure_reason": "redirect_target_not_allowlisted",
                        "http_status_code": status_code,
                        "final_url": current_url,
                        "redirect_target": redirected_url,
                        "redirect_count": redirect_count,
                        "attempted_urls": attempted_urls,
                        "payload_path": None,
                        "payload_sha256": None,
                        "payload_byte_count": 0,
                        "truncated": False,
                        "headers": headers,
                    }
                current_url = redirected_url
                continue
            if method != "HEAD":
                payload_path, payload_sha256, payload_byte_count, truncated = (
                    spool_limited_response(
                        exc,
                        max_response_bytes=max_response_bytes,
                        spool_dir=payload_spool_dir,
                    )
                )
        except (TimeoutError, URLError, OSError) as exc:
            return {
                "status": "failed",
                "failure_reason": f"network_error:{exc.__class__.__name__}",
                "error_detail": str(exc),
                "http_status_code": None,
                "final_url": current_url,
                "redirect_count": redirect_count,
                "attempted_urls": attempted_urls,
                "payload_path": None,
                "payload_sha256": None,
                "payload_byte_count": 0,
                "truncated": False,
                "headers": None,
            }

        failure_reason = None
        status = "captured"
        if status_code >= 400:
            status = "failed"
            failure_reason = f"http_status_{status_code}"
            if payload_path is not None:
                payload_path.unlink(missing_ok=True)
            payload_path = None
            payload_sha256 = None
            payload_byte_count = 0
            truncated = False
        elif truncated:
            status = "failed"
            failure_reason = "response_exceeds_max_bytes"
            if payload_path is not None:
                payload_path.unlink(missing_ok=True)
            payload_path = None
            payload_sha256 = None
            payload_byte_count = 0

        if method == "HEAD":
            payload_path = None
            payload_sha256 = None
            payload_byte_count = 0

        return {
            "status": status,
            "failure_reason": failure_reason,
            "http_status_code": status_code,
            "final_url": current_url,
            "redirect_count": redirect_count,
            "attempted_urls": attempted_urls,
            "payload_path": str(payload_path) if payload_path is not None else None,
            "payload_sha256": payload_sha256,
            "payload_byte_count": payload_byte_count,
            "truncated": truncated,
            "headers": headers,
        }


def _remote_fetch_host_key(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower()


def _remote_fetch_host_queue(
    *,
    tasks: list[dict[str, Any]],
    user_agent: str,
    allowlist_hosts: list[str],
    allowlist_prefixes: list[str],
    timeout_seconds: float,
    max_response_bytes: int,
    min_interval_seconds: float,
    payload_spool_dir: Path,
) -> dict[int, dict[str, Any]]:
    results: dict[int, dict[str, Any]] = {}
    opener = build_opener(NoAutoRedirectHandler)
    for index, task in enumerate(tasks):
        if index > 0 and min_interval_seconds > 0:
            time.sleep(min_interval_seconds)
        results[int(task["sequence"])] = remote_fetch_one(
            url=task["url"],
            method=task["method"],
            user_agent=user_agent,
            allowlist_hosts=allowlist_hosts,
            allowlist_prefixes=allowlist_prefixes,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            payload_spool_dir=payload_spool_dir,
            opener=opener,
        )
    return results


def _build_remote_fetch_artifacts(
    *,
    record: dict[str, Any],
    adapter_payload: dict[str, Any],
    run_id: str,
    handoff_hash: str,
    created_at: str,
    capture_id: str,
    extraction_id: str,
    url: str,
    method: str,
    user_agent: str,
    fetch_result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | None, bool]:
    payload_source_path_value = fetch_result.get("payload_path")
    payload_source_path = (
        Path(str(payload_source_path_value)).expanduser().resolve()
        if isinstance(payload_source_path_value, str) and payload_source_path_value.strip()
        else None
    )
    payload_bytes = fetch_result.get("payload")
    payload_sha256 = fetch_result.get("payload_sha256")
    payload_byte_count = int(fetch_result.get("payload_byte_count") or 0)
    if isinstance(payload_bytes, bytes):
        payload_sha256 = (
            sha256_bytes(payload_bytes) if fetch_result["status"] == "captured" else None
        )
        payload_byte_count = len(payload_bytes) if fetch_result["status"] == "captured" else 0
    content_type = extract_content_type(fetch_result.get("headers"))
    response_headers = normalize_response_headers(fetch_result.get("headers"))
    capture_event = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": capture_id,
        "run_id": run_id,
        "handoff_hash": handoff_hash,
        "handoff_sequences": [record["sequence"]],
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "adapter_type": "remote_url_manifest",
        "source_reference": {
            "relative_path": record["relative_path"],
            "remote_url": url,
            "manifest_url": record["source_specific"].get("manifest_url"),
        },
        "original_locator": record["preserved"]["original_locator"],
        "normalized_url": urlparse(url).geturl(),
        "final_url": fetch_result["final_url"],
        "redirect_count": fetch_result["redirect_count"],
        "http_status_code": fetch_result["http_status_code"],
        "request_method": method,
        "user_agent": user_agent,
        "content_hash": payload_sha256 if fetch_result["status"] == "captured" else None,
        "byte_count": payload_byte_count if fetch_result["status"] == "captured" else 0,
        "content_length_header": response_headers.get("content-length"),
        "content_type": content_type,
        "captured_at": created_at,
        "capture_method": "remote_url_fetch",
        "transient_payload_path": None,
        "payload_retention_policy": "hash_only",
        "network_access_attempted": True,
        "rights_posture": record["preserved"].get("rights_posture"),
        "status": "completed" if fetch_result["status"] == "captured" else "failed",
        "failure_reason": fetch_result["failure_reason"],
        "canonical_persistence_attempted": False,
        "verification_status": "unverified",
    }
    if fetch_result.get("redirect_target"):
        capture_event["redirect_target"] = fetch_result["redirect_target"]
    if fetch_result.get("error_detail"):
        capture_event["error_detail"] = fetch_result["error_detail"]

    extracted_text = None
    encoding_result = "not_attempted"
    failure_reason = fetch_result["failure_reason"]
    extracted_text_path = None
    decode_failed = False
    unsupported_content_type = False
    retain_payload = False
    if fetch_result["status"] == "captured":
        if method == "HEAD":
            failure_reason = "head_request_no_body"
            unsupported_content_type = True
        elif not is_extractable_content_type(content_type):
            failure_reason = "unsupported_content_type"
            unsupported_content_type = True
        else:
            if payload_bytes is None and payload_source_path is not None:
                payload_bytes = payload_source_path.read_bytes()
            extracted_text, encoding_result, failure_reason = safe_decode_text(
                payload_bytes if isinstance(payload_bytes, bytes) else b""
            )
            if extracted_text is not None:
                extracted_text_path = f"extracted-text/{extraction_id}.txt"
            else:
                decode_failed = True
        retain_payload = bool(
            payload_source_path is not None
            and payload_byte_count > 0
            and (unsupported_content_type or decode_failed)
        )
        if retain_payload:
            capture_event["transient_payload_path"] = f"payloads/{capture_id}.bin"
            capture_event["payload_retention_policy"] = "transient_run_artifact"
        elif payload_source_path is not None:
            payload_source_path.unlink(missing_ok=True)

    extraction_record = build_extraction_record(
        extraction_id=extraction_id,
        run_id=run_id,
        capture_id=capture_id,
        adapter_payload=adapter_payload,
        adapter_type="remote_url_manifest",
        handoff_sequence=record["sequence"],
        relative_path=record["relative_path"],
        input_hash=payload_sha256 if fetch_result["status"] == "captured" else None,
        byte_count_in=payload_byte_count if fetch_result["status"] == "captured" else 0,
        extraction_method="remote_text_extract",
        hazard_flags=list(record["preserved"].get("source_metadata", {}).get("hazard_flags", [])),
        content_text=extracted_text,
        encoding_result=encoding_result,
        failure_reason=failure_reason,
        extracted_text_path=extracted_text_path,
        extra_fields={
            "content_type": content_type,
            "remote_url": url,
            "final_url": fetch_result["final_url"],
            "network_access_attempted": True,
        },
    )
    return (
        capture_event,
        extraction_record,
        extracted_text,
        fetch_result["status"] != "captured" or decode_failed or unsupported_content_type,
    )


def execute_remote_fetches(
    *,
    records: list[dict[str, Any]],
    adapter_payload: dict[str, Any],
    run_id: str,
    created_at: str,
    handoff_hash: str,
    gate_report: dict[str, Any],
    timeout_seconds: float,
    max_response_bytes: int,
    payload_spool_dir: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, Path],
    bool,
    dict[str, Any],
]:
    if timeout_seconds <= 0:
        raise SourceAcquisitionError("--timeout-seconds must be greater than zero")
    if max_response_bytes < 1:
        raise SourceAcquisitionError("--max-response-bytes must be at least 1")

    capture_events: list[dict[str, Any]] = []
    extraction_records: list[dict[str, Any]] = []
    text_artifacts: dict[str, str] = {}
    binary_artifacts: dict[str, Path] = {}
    failed = False
    summary = {
        "urls_planned": len(records),
        "urls_attempted": 0,
        "urls_succeeded": 0,
        "urls_failed": 0,
        "urls_denied": 0,
        "bytes_captured": 0,
    }
    action_map = gate_action_by_url(gate_report)
    network_policy = gate_report.get("checks", {}).get("network_policy", {})
    user_agent = network_policy.get("user_agent") if isinstance(network_policy, dict) else None
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise SourceAcquisitionError(
            "network safety gate report does not include a usable user agent"
        )
    rate_limits = gate_report.get("checks", {}).get("rate_limits", {})
    min_interval = (
        rate_limits.get("min_interval_seconds", 0) if isinstance(rate_limits, dict) else 0
    )
    min_interval_seconds = float(min_interval) if isinstance(min_interval, (int, float)) else 0.0
    allowlist_hosts, allowlist_prefixes = gate_allowlist(gate_report)
    record_plans: list[dict[str, Any]] = []
    host_tasks: dict[str, list[dict[str, Any]]] = {}
    for index, record in enumerate(
        sorted(records, key=lambda item: int(item["sequence"])), start=1
    ):
        original_locator = record["preserved"]["original_locator"]
        url = original_locator["entry_url"]
        capture_id = make_capture_id(index)
        extraction_id = make_extraction_id(index)
        gate_action = action_map.get(url)
        record_plan: dict[str, Any] = {
            "sequence": int(record["sequence"]),
            "record": record,
            "capture_id": capture_id,
            "extraction_id": extraction_id,
            "url": url,
        }
        if gate_action is None or gate_action.get("status") != "planned":
            failed = True
            summary["urls_denied"] += 1
            denied_reason = (
                "network_gate_action_missing"
                if gate_action is None
                else "network_gate_action_not_planned"
            )
            request_method = (
                str(gate_action.get("method") or "GET").upper() if gate_action else "GET"
            )
            record_plan["kind"] = "denied"
            record_plan["capture_event"] = build_remote_denied_capture_event(
                record=record,
                adapter_payload=adapter_payload,
                run_id=run_id,
                handoff_hash=handoff_hash,
                created_at=created_at,
                capture_id=capture_id,
                url=url,
                method=request_method,
                failure_reason=denied_reason,
                user_agent=user_agent,
            )
            record_plan["extraction_record"] = build_remote_denied_extraction_record(
                record=record,
                adapter_payload=adapter_payload,
                extraction_id=extraction_id,
                run_id=run_id,
                capture_id=capture_id,
                url=url,
                method=request_method,
                failure_reason=denied_reason,
            )
            record_plans.append(record_plan)
            continue
        method = str(gate_action.get("method") or "GET").upper()
        if method not in {"GET", "HEAD"}:
            failed = True
            summary["urls_denied"] += 1
            record_plan["kind"] = "denied"
            record_plan["capture_event"] = build_remote_denied_capture_event(
                record=record,
                adapter_payload=adapter_payload,
                run_id=run_id,
                handoff_hash=handoff_hash,
                created_at=created_at,
                capture_id=capture_id,
                url=url,
                method=method,
                failure_reason="unsupported_request_method",
                user_agent=user_agent,
            )
            record_plan["extraction_record"] = build_remote_denied_extraction_record(
                record=record,
                adapter_payload=adapter_payload,
                extraction_id=extraction_id,
                run_id=run_id,
                capture_id=capture_id,
                url=url,
                method=method,
                failure_reason="unsupported_request_method",
            )
            record_plans.append(record_plan)
            continue
        host_key = _remote_fetch_host_key(url)
        host_tasks.setdefault(host_key, []).append(
            {
                "sequence": int(record["sequence"]),
                "url": url,
                "method": method,
            }
        )
        record_plan["kind"] = "fetch"
        record_plan["host"] = host_key
        record_plan["method"] = method
        record_plans.append(record_plan)

    fetch_results_by_sequence: dict[int, dict[str, Any]] = {}
    if host_tasks:
        max_workers = min(4, len(host_tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _remote_fetch_host_queue,
                    tasks=tasks,
                    user_agent=user_agent,
                    allowlist_hosts=allowlist_hosts,
                    allowlist_prefixes=allowlist_prefixes,
                    timeout_seconds=timeout_seconds,
                    max_response_bytes=max_response_bytes,
                    min_interval_seconds=min_interval_seconds,
                    payload_spool_dir=payload_spool_dir,
                ): host
                for host, tasks in host_tasks.items()
            }
            for future in as_completed(futures):
                fetch_results_by_sequence.update(future.result())

    for record_plan in record_plans:
        if record_plan["kind"] == "denied":
            capture_events.append(record_plan["capture_event"])
            extraction_records.append(record_plan["extraction_record"])
            continue

        fetch_result = fetch_results_by_sequence[int(record_plan["sequence"])]
        capture_event, extraction_record, extracted_text, record_failed = (
            _build_remote_fetch_artifacts(
                record=record_plan["record"],
                adapter_payload=adapter_payload,
                run_id=run_id,
                handoff_hash=handoff_hash,
                created_at=created_at,
                capture_id=record_plan["capture_id"],
                extraction_id=record_plan["extraction_id"],
                url=record_plan["url"],
                method=record_plan["method"],
                user_agent=user_agent,
                fetch_result=fetch_result,
            )
        )
        summary["urls_attempted"] += 1
        if fetch_result["status"] == "captured":
            summary["urls_succeeded"] += 1
            summary["bytes_captured"] += int(capture_event["byte_count"])
        else:
            failed = True
            summary["urls_failed"] += 1
        if record_failed:
            failed = True
        capture_events.append(capture_event)
        extraction_records.append(extraction_record)
        if extracted_text is not None and extraction_record["extracted_text_path"] is not None:
            text_artifacts[extraction_record["extracted_text_path"]] = extracted_text
        if (
            capture_event.get("transient_payload_path") is not None
            and isinstance(fetch_result.get("payload_path"), str)
            and fetch_result["status"] == "captured"
        ):
            binary_artifacts[str(capture_event["transient_payload_path"])] = Path(
                str(fetch_result["payload_path"])
            )
    return capture_events, extraction_records, text_artifacts, binary_artifacts, failed, summary


def execute_remote_url_manifest(
    *,
    records: list[dict[str, Any]],
    run_id: str,
    created_at: str,
    handoff_path: Path,
    handoff_hash: str,
    adapter_payload: dict[str, Any],
    gate_request_path: Path | None,
    dry_run: bool,
    allow_network: bool,
    timeout_seconds: float,
    max_response_bytes: int,
    payload_spool_dir: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    list[str],
    dict[str, str],
    dict[str, Path],
]:
    if gate_request_path is None:
        raise SourceAcquisitionError(
            "remote_url_manifest execution requires --network-safety-request"
        )
    try:
        gate_request = load_request(gate_request_path)
    except NetworkSafetyGateError as exc:
        raise SourceAcquisitionError(str(exc)) from exc
    gate_report = evaluate_request(gate_request)
    expected_urls = extract_remote_urls(records)
    ensure_gate_request_matches_handoff(gate_report, expected_urls=expected_urls)
    planned_actions = planned_actions_for_records(records, variant="remote_url_manifest")

    if gate_report["decision"] == "refuse":
        execution_record = execution_record_payload(
            run_id=run_id,
            created_at=created_at,
            handoff_path=handoff_path,
            handoff_hash=handoff_hash,
            adapter_payload=adapter_payload,
            adapter_type="remote_url_manifest",
            executor_mode="remote",
            dry_run=dry_run,
            status="denied",
            network_access_attempted=False,
            network_access_allowed=False,
            network_access_denied_reason="network safety gate denied execution",
            gate_report=gate_report,
            local_input_paths=[],
            planned_actions=planned_actions,
            capture_events=[],
            extraction_records=[],
            denial_record_written=True,
        )
        denial_record = build_denial_record(execution_record, considered_urls=expected_urls)
        return execution_record, denial_record, [], [], gate_report, expected_urls, {}, {}

    if gate_report["decision"] == "dry_run" or dry_run:
        execution_record = dry_run_execution_record(
            run_id=run_id,
            created_at=created_at,
            handoff_path=handoff_path,
            handoff_hash=handoff_hash,
            adapter_payload=adapter_payload,
            adapter_type="remote_url_manifest",
            executor_mode="remote",
            local_input_paths=[],
            gate_report=gate_report,
            planned_actions=planned_actions,
        )
        return execution_record, None, [], [], gate_report, expected_urls, {}, {}

    if not gate_report["execution_allowed"]:
        execution_record = execution_record_payload(
            run_id=run_id,
            created_at=created_at,
            handoff_path=handoff_path,
            handoff_hash=handoff_hash,
            adapter_payload=adapter_payload,
            adapter_type="remote_url_manifest",
            executor_mode="remote",
            dry_run=False,
            status="denied",
            network_access_attempted=False,
            network_access_allowed=False,
            network_access_denied_reason="network safety gate did not allow execution",
            gate_report=gate_report,
            local_input_paths=[],
            planned_actions=planned_actions,
            capture_events=[],
            extraction_records=[],
            denial_record_written=True,
        )
        denial_record = build_denial_record(execution_record, considered_urls=expected_urls)
        return execution_record, denial_record, [], [], gate_report, expected_urls, {}, {}

    reason = "explicit --allow-network is required for remote execution"
    if not allow_network:
        execution_record = execution_record_payload(
            run_id=run_id,
            created_at=created_at,
            handoff_path=handoff_path,
            handoff_hash=handoff_hash,
            adapter_payload=adapter_payload,
            adapter_type="remote_url_manifest",
            executor_mode="remote",
            dry_run=False,
            status="denied",
            network_access_attempted=False,
            network_access_allowed=bool(gate_report["execution_allowed"]),
            network_access_denied_reason=reason,
            gate_report=gate_report,
            local_input_paths=[],
            planned_actions=planned_actions,
            capture_events=[],
            extraction_records=[],
            denial_record_written=True,
        )
        denial_record = build_denial_record(execution_record, considered_urls=expected_urls)
        return execution_record, denial_record, [], [], gate_report, expected_urls, {}, {}

    capture_events, extraction_records, text_artifacts, binary_artifacts, failed, remote_summary = (
        execute_remote_fetches(
            records=records,
            adapter_payload=adapter_payload,
            run_id=run_id,
            created_at=created_at,
            handoff_hash=handoff_hash,
            gate_report=gate_report,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            payload_spool_dir=payload_spool_dir,
        )
    )
    execution_record = execution_record_payload(
        run_id=run_id,
        created_at=created_at,
        handoff_path=handoff_path,
        handoff_hash=handoff_hash,
        adapter_payload=adapter_payload,
        adapter_type="remote_url_manifest",
        executor_mode="remote",
        dry_run=False,
        status="failed" if failed else "completed",
        network_access_attempted=remote_summary["urls_attempted"] > 0
        or remote_summary["urls_denied"] > 0,
        network_access_allowed=bool(gate_report["execution_allowed"]),
        network_access_denied_reason=None,
        gate_report=gate_report,
        local_input_paths=[],
        planned_actions=planned_actions,
        capture_events=capture_events,
        extraction_records=extraction_records,
        denial_record_written=False,
    )
    execution_record.update(
        {
            "network_gate_request_hash": sha256_bytes(gate_request_path.read_bytes()),
            "remote_live_fetch_enabled": True,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            **remote_summary,
        }
    )
    return (
        execution_record,
        None,
        capture_events,
        extraction_records,
        gate_report,
        expected_urls,
        text_artifacts,
        binary_artifacts,
    )


def main() -> int:
    args = parse_args()
    handoff_path = resolve_cli_path(args.handoff, base_dir=Path.cwd())
    adapter_path = resolve_cli_path(args.adapter, base_dir=Path.cwd())
    workspace_root = resolve_workspace_root(args.workspace_root)
    output_dir = resolve_output_dir(args.output, workspace_root=workspace_root)
    created_at = normalize_created_at(args.created_at)
    run_id = resolve_run_id(output_dir, run_id=args.run_id)
    remote_payload_spool_dir: Path | None = None

    try:
        adapter_payload = load_validated_adapter(adapter_path)
        raw_records, load_errors, load_exit = validate_source_adapter_handoff.load_records(
            handoff_path
        )
        if load_exit != validate_source_adapter_handoff.EXIT_PASS or not raw_records:
            message = (
                load_errors[0]["message"]
                if load_errors
                else "source-adapter handoff could not be loaded"
            )
            raise SourceAcquisitionError(message)
        ensure_single_adapter_context(
            [record for _, record in raw_records], expected_adapter_path=adapter_path
        )
        records, handoff_hash = load_validated_handoff_records(
            handoff_path, adapter_path=adapter_path
        )
        validate_handoff_sequence(records)
        variant = determine_variant(records, adapter_payload=adapter_payload)
        executor_mode = determine_executor_mode(args.mode, variant=variant)
        planned_actions = planned_actions_for_records(records, variant=variant)

        if variant == "remote_url_manifest" and not args.network_safety_request:
            raise SourceAcquisitionError(
                "remote_url_manifest execution requires --network-safety-request"
            )

        if variant == "remote_url_manifest":
            remote_payload_spool_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{output_dir.name}.remote-payloads.",
                    dir=output_dir.parent,
                )
            )

        if args.dry_run:
            gate_report = None
            if variant == "remote_url_manifest":
                dry_run_gate_request_path = resolve_cli_path(
                    args.network_safety_request, base_dir=Path.cwd()
                )
                (
                    remote_execution_record,
                    remote_denial_record,
                    remote_capture_events,
                    remote_extraction_records,
                    gate_report,
                    _,
                    _remote_text_artifacts,
                    _remote_binary_artifacts,
                ) = execute_remote_url_manifest(
                    records=records,
                    run_id=run_id,
                    created_at=created_at,
                    handoff_path=handoff_path,
                    handoff_hash=handoff_hash,
                    adapter_payload=adapter_payload,
                    gate_request_path=dry_run_gate_request_path,
                    dry_run=True,
                    allow_network=args.allow_network,
                    timeout_seconds=args.timeout_seconds,
                    max_response_bytes=args.max_response_bytes,
                    payload_spool_dir=remote_payload_spool_dir,
                )
                if not args.suppress_execution_record_stdout:
                    sys.stdout.write(
                        json.dumps(
                            remote_execution_record,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                return 0 if remote_execution_record["status"] == "dry_run" else EXIT_STATE_UNSAFE

            dry_run_local_input_paths = sorted(
                {str(record["resolved_source_path"]) for record in records}
            )
            execution_record = dry_run_execution_record(
                run_id=run_id,
                created_at=created_at,
                handoff_path=handoff_path,
                handoff_hash=handoff_hash,
                adapter_payload=adapter_payload,
                adapter_type=variant,
                executor_mode=executor_mode,
                local_input_paths=dry_run_local_input_paths,
                gate_report=None,
                planned_actions=planned_actions,
            )
            if not args.suppress_execution_record_stdout:
                sys.stdout.write(
                    json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                )
            return 0

        prepare_output_dir(output_dir, run_id=run_id, workspace_root=workspace_root)

        denial_record = None
        gate_report = None
        capture_events: list[dict[str, Any]] = []
        extraction_records: list[dict[str, Any]] = []
        text_artifacts: dict[str, str] = {}
        binary_artifacts: dict[str, bytes] = {}
        local_input_paths: list[str] = []
        failed = False

        if variant == "local_source":
            capture_events, extraction_records, text_artifacts, local_input_paths, failed = (
                execute_local_source(
                    records=records,
                    adapter_payload=adapter_payload,
                    adapter_path=adapter_path,
                    run_id=run_id,
                    created_at=created_at,
                    handoff_hash=handoff_hash,
                )
            )
            execution_record = execution_record_payload(
                run_id=run_id,
                created_at=created_at,
                handoff_path=handoff_path,
                handoff_hash=handoff_hash,
                adapter_payload=adapter_payload,
                adapter_type=variant,
                executor_mode=executor_mode,
                dry_run=False,
                status="failed" if failed else "completed",
                network_access_attempted=False,
                network_access_allowed=False,
                network_access_denied_reason=None,
                gate_report=None,
                local_input_paths=local_input_paths,
                planned_actions=planned_actions,
                capture_events=capture_events,
                extraction_records=extraction_records,
                denial_record_written=False,
            )
        elif variant == "structured_data":
            capture_events, extraction_records, text_artifacts, local_input_paths, failed = (
                execute_structured_data(
                    records=records,
                    adapter_payload=adapter_payload,
                    adapter_path=adapter_path,
                    run_id=run_id,
                    created_at=created_at,
                    handoff_hash=handoff_hash,
                )
            )
            execution_record = execution_record_payload(
                run_id=run_id,
                created_at=created_at,
                handoff_path=handoff_path,
                handoff_hash=handoff_hash,
                adapter_payload=adapter_payload,
                adapter_type=variant,
                executor_mode=executor_mode,
                dry_run=False,
                status="failed" if failed else "completed",
                network_access_attempted=False,
                network_access_allowed=False,
                network_access_denied_reason=None,
                gate_report=None,
                local_input_paths=local_input_paths,
                planned_actions=planned_actions,
                capture_events=capture_events,
                extraction_records=extraction_records,
                denial_record_written=False,
            )
        elif variant == "local_git_repo":
            capture_events, extraction_records, text_artifacts, local_input_paths, failed = (
                execute_local_git_repo(
                    records=records,
                    adapter_payload=adapter_payload,
                    run_id=run_id,
                    created_at=created_at,
                    handoff_hash=handoff_hash,
                )
            )
            execution_record = execution_record_payload(
                run_id=run_id,
                created_at=created_at,
                handoff_path=handoff_path,
                handoff_hash=handoff_hash,
                adapter_payload=adapter_payload,
                adapter_type=variant,
                executor_mode=executor_mode,
                dry_run=False,
                status="failed" if failed else "completed",
                network_access_attempted=False,
                network_access_allowed=False,
                network_access_denied_reason=None,
                gate_report=None,
                local_input_paths=local_input_paths,
                planned_actions=planned_actions,
                capture_events=capture_events,
                extraction_records=extraction_records,
                denial_record_written=False,
            )
        elif variant == "remote_url_manifest":
            remote_gate_request_path = (
                resolve_cli_path(args.network_safety_request, base_dir=Path.cwd())
                if args.network_safety_request
                else None
            )
            (
                execution_record,
                denial_record,
                capture_events,
                extraction_records,
                gate_report,
                _,
                text_artifacts,
                binary_artifacts,
            ) = execute_remote_url_manifest(
                records=records,
                run_id=run_id,
                created_at=created_at,
                handoff_path=handoff_path,
                handoff_hash=handoff_hash,
                adapter_payload=adapter_payload,
                gate_request_path=remote_gate_request_path,
                dry_run=False,
                allow_network=args.allow_network,
                timeout_seconds=args.timeout_seconds,
                max_response_bytes=args.max_response_bytes,
                payload_spool_dir=remote_payload_spool_dir,
            )
        else:
            raise SourceAcquisitionError(f"unsupported source-adapter handoff variant: {variant}")

        with tempfile.TemporaryDirectory(
            prefix=f".{output_dir.name}.staging.",
            dir=output_dir.parent,
        ) as staging_root:
            staging_dir = Path(staging_root)
            write_execution_artifacts(
                output_dir=staging_dir,
                execution_record=execution_record,
                capture_events=capture_events,
                extraction_records=extraction_records,
                denial_record=denial_record,
                gate_report=gate_report,
                text_artifacts=text_artifacts,
                binary_artifacts=binary_artifacts,
            )
            validate_emitted_artifacts(staging_dir)
            publish_output_dir(staging_dir, output_dir)

        if not args.suppress_execution_record_stdout:
            sys.stdout.write(
                json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
        return EXIT_PASS if execution_record["status"] == "completed" else EXIT_STATE_UNSAFE
    except SourceAcquisitionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if remote_payload_spool_dir is not None:
            shutil.rmtree(remote_payload_spool_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
