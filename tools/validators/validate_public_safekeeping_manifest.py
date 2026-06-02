#!/usr/bin/env python3
"""Validate public-safekeeping-manifest.v1 payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
        is_rfc3339_datetime,
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
        is_rfc3339_datetime,
        render_text_report,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


VALIDATOR_NAME = "public_safekeeping_manifest"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = "public-safekeeping-manifest.v1"
BUNDLE_SCHEMA_VERSION = "public-sharing-bundle.v1"
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
CHANNELS = {"git_handoff", "archive_export", "manual_copy"}
ARTIFACT_FAMILIES = {
    "site_page",
    "site_asset",
    "bundle_manifest",
    "export_summary",
    "presentation_summary",
}
RIGHTS_POSTURES = {"public_safe", "metadata_only"}


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a public safekeeping manifest JSON file.")
    parser.add_argument("target", help="Path to the public safekeeping manifest JSON file.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(errors: list[dict[str, Any]], *, code: str, message: str, line: int | None = None) -> None:
    errors.append({"code": code, "line": line, "message": message})


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


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
    except OSError:
        add_error(errors, code="INPUT_UNREADABLE", message="input file could not be read")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    try:
        payload = json.loads(
            raw_text,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except DuplicateJsonKeyError as exc:
        add_error(errors, code="DUPLICATE_JSON_KEY", line=1, message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    except json.JSONDecodeError as exc:
        add_error(errors, code="JSON_PARSE_ERROR", line=exc.lineno, message="invalid JSON syntax")
        return None, errors, EXIT_VALIDATION_FAILED
    except ValueError as exc:
        add_error(errors, code="JSON_PARSE_ERROR", line=1, message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED
    return payload, errors, EXIT_PASS


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def is_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(value.strip()) and not path.is_absolute() and ".." not in path.parts


def validate_string_array(value: Any, *, field_name: str, errors: list[dict[str, Any]], allowed: set[str] | None = None, min_items: int = 1) -> list[str]:
    if not isinstance(value, list) or len(value) < min_items:
        add_error(errors, code="INVALID_ARRAY", message=f"{field_name} must be an array with at least {min_items} item(s)")
        return []
    accepted: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code="INVALID_ARRAY_ITEM", message=f"{field_name}[{index}] must be a non-blank string")
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{field_name} contains duplicate value: {item}")
            continue
        if allowed is not None and item not in allowed:
            add_error(errors, code="INVALID_ARRAY_ITEM", message=f"{field_name}[{index}] must be one of: {', '.join(sorted(allowed))}")
            continue
        seen.add(item)
        accepted.append(item)
    return accepted


def validate_public_safekeeping_manifest(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, load_exit = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, load_exit

    counts["inspected"] = 1

    required_keys = {
        "schema_version",
        "generated_at",
        "bundle_root",
        "bundle_manifest_path",
        "bundle_manifest_sha256",
        "bundle_schema_version",
        "upload_attempted",
        "preservation_targets",
        "manual_operator_steps",
        "artifacts",
    }
    optional_keys = {"excluded_families"}
    for key in sorted(set(payload) - (required_keys | optional_keys)):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(required_keys):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    if not isinstance(payload.get("generated_at"), str) or not is_rfc3339_datetime(payload["generated_at"]):
        add_error(errors, code="INVALID_TIMESTAMP", message="generated_at must be an RFC3339 timestamp")
    if payload.get("bundle_schema_version") != BUNDLE_SCHEMA_VERSION:
        add_error(errors, code="INVALID_BUNDLE_SCHEMA_VERSION", message=f"bundle_schema_version must equal {BUNDLE_SCHEMA_VERSION}")
    if payload.get("upload_attempted") is not False:
        add_error(errors, code="UPLOAD_ATTEMPTED_FORBIDDEN", message="upload_attempted must be false")

    bundle_root_value = payload.get("bundle_root")
    bundle_root: Path | None = None
    if not isinstance(bundle_root_value, str) or not bundle_root_value.strip():
        add_error(errors, code="INVALID_BUNDLE_ROOT", message="bundle_root must be a non-blank string")
    elif not is_relative_path(bundle_root_value):
        add_error(errors, code="INVALID_BUNDLE_ROOT", message="bundle_root must be a relative path inside the manifest directory")
    else:
        bundle_root = (target.parent / bundle_root_value).resolve()
        if not bundle_root.exists() or not bundle_root.is_dir():
            add_error(errors, code="BUNDLE_ROOT_NOT_FOUND", message="bundle_root directory does not exist")

    bundle_manifest_path_value = payload.get("bundle_manifest_path")
    resolved_bundle_manifest_path: Path | None = None
    if not isinstance(bundle_manifest_path_value, str) or not bundle_manifest_path_value.strip():
        add_error(errors, code="INVALID_BUNDLE_MANIFEST_PATH", message="bundle_manifest_path must be a non-blank string")
    elif not is_relative_path(bundle_manifest_path_value):
        add_error(errors, code="INVALID_BUNDLE_MANIFEST_PATH", message="bundle_manifest_path must be a relative path inside bundle_root")
    elif bundle_root is not None:
        resolved_bundle_manifest_path = (bundle_root / bundle_manifest_path_value).resolve()
        if not resolved_bundle_manifest_path.exists() or not resolved_bundle_manifest_path.is_file():
            add_error(errors, code="BUNDLE_MANIFEST_NOT_FOUND", message=f"bundle manifest file not found: {bundle_manifest_path_value}")

    bundle_manifest_sha = payload.get("bundle_manifest_sha256")
    if not isinstance(bundle_manifest_sha, str) or not SHA256_PATTERN.fullmatch(bundle_manifest_sha):
        add_error(errors, code="INVALID_BUNDLE_MANIFEST_SHA256", message="bundle_manifest_sha256 must use sha256:<64-hex>")
    elif resolved_bundle_manifest_path is not None and resolved_bundle_manifest_path.is_file():
        actual_bundle_sha = hash_file(resolved_bundle_manifest_path)
        if actual_bundle_sha != bundle_manifest_sha:
            add_error(errors, code="BUNDLE_MANIFEST_HASH_MISMATCH", message="bundle_manifest_sha256 does not match the bundle manifest file")
        try:
            bundle_manifest_payload = json.loads(resolved_bundle_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            add_error(errors, code="BUNDLE_MANIFEST_UNREADABLE", message="bundle manifest could not be parsed")
            bundle_manifest_payload = None
        if isinstance(bundle_manifest_payload, dict):
            if bundle_manifest_payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
                add_error(errors, code="BUNDLE_MANIFEST_SCHEMA_MISMATCH", message="bundle manifest schema_version does not match bundle_schema_version")
            if bundle_manifest_payload.get("upload_attempted") is not False:
                add_error(errors, code="BUNDLE_MANIFEST_UPLOAD_FORBIDDEN", message="bundle manifest upload_attempted must be false")

    validate_string_array(
        payload.get("preservation_targets"),
        field_name="preservation_targets",
        errors=errors,
        allowed=CHANNELS,
        min_items=1,
    )
    validate_string_array(
        payload.get("manual_operator_steps"),
        field_name="manual_operator_steps",
        errors=errors,
        allowed=None,
        min_items=3,
    )

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        add_error(errors, code="INVALID_ARTIFACTS", message="artifacts must be a non-empty array")
    else:
        seen_paths: set[str] = set()
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                add_error(errors, code="ARTIFACT_OBJECT_REQUIRED", message=f"artifacts[{index}] must be an object")
                continue
            path_value = artifact.get("path")
            if not isinstance(path_value, str) or not is_relative_path(path_value):
                add_error(errors, code="INVALID_ARTIFACT_PATH", message=f"artifacts[{index}].path must be a relative path")
                continue
            if path_value in seen_paths:
                add_error(errors, code="DUPLICATE_ARTIFACT_PATH", message=f"duplicate artifact path: {path_value}")
                continue
            seen_paths.add(path_value)
            family = artifact.get("artifact_family")
            if family not in ARTIFACT_FAMILIES:
                add_error(errors, code="INVALID_ARTIFACT_FAMILY", message=f"artifacts[{index}].artifact_family is invalid")
            sha_value = artifact.get("sha256")
            if not isinstance(sha_value, str) or not SHA256_PATTERN.fullmatch(sha_value):
                add_error(errors, code="INVALID_ARTIFACT_SHA256", message=f"artifacts[{index}].sha256 must use sha256:<64-hex>")
            size_bytes = artifact.get("size_bytes")
            if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
                add_error(errors, code="INVALID_ARTIFACT_SIZE", message=f"artifacts[{index}].size_bytes must be an integer >= 0")
            rights_posture = artifact.get("rights_posture")
            if rights_posture not in RIGHTS_POSTURES:
                add_error(errors, code="INVALID_RIGHTS_POSTURE", message=f"artifacts[{index}].rights_posture is invalid")
            validate_string_array(
                artifact.get("preservation_channels"),
                field_name=f"artifacts[{index}].preservation_channels",
                errors=errors,
                allowed=CHANNELS,
                min_items=1,
            )
            if bundle_root is not None:
                artifact_path = (bundle_root / path_value).resolve()
                if not artifact_path.exists() or not artifact_path.is_file():
                    add_error(errors, code="ARTIFACT_FILE_NOT_FOUND", message=f"artifact file not found: {path_value}")
                else:
                    if hash_file(artifact_path) != sha_value:
                        add_error(errors, code="ARTIFACT_HASH_MISMATCH", message=f"artifact hash mismatch: {path_value}")
                    if artifact_path.stat().st_size != size_bytes:
                        add_error(errors, code="ARTIFACT_SIZE_MISMATCH", message=f"artifact size mismatch: {path_value}")

    excluded_families = payload.get("excluded_families")
    if excluded_families is not None:
        if not isinstance(excluded_families, list):
            add_error(errors, code="INVALID_EXCLUDED_FAMILIES", message="excluded_families must be an array")
        else:
            for index, item in enumerate(excluded_families):
                if not isinstance(item, dict):
                    add_error(errors, code="INVALID_EXCLUDED_FAMILY", message=f"excluded_families[{index}] must be an object")
                    continue
                if not isinstance(item.get("family"), str) or not item["family"].strip():
                    add_error(errors, code="INVALID_EXCLUDED_FAMILY", message=f"excluded_families[{index}].family must be a non-blank string")
                if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                    add_error(errors, code="INVALID_EXCLUDED_FAMILY", message=f"excluded_families[{index}].reason must be a non-blank string")

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_public_safekeeping_manifest(target)
    status = "pass" if exit_code == EXIT_PASS else "fail"
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts={
            "report_json": display_path(args.report_json),
            "report_text": display_path(args.report_text),
        },
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or (display_path(args.target) or args.target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
