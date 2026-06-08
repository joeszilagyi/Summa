#!/usr/bin/env python3
"""Validate static knowledge-tree build manifests and optional export freshness.

Documentation: tools/validators/README.md
Schema: config/knowledge_tree_build_manifest.schema.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

import tools.validators.validate_knowledge_tree_export as validate_knowledge_tree_export  # noqa: E402
import tools.validators.validate_public_knowledge_tree_presentation as validate_public_knowledge_tree_presentation  # noqa: E402

VALIDATOR_NAME = "knowledge_tree_build_manifest"
CONTRACT_VERSION = "1"
HASH_CHUNK_SIZE = 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

REQUIRED_KEYS = {
    "schema_version",
    "build_id",
    "export_id",
    "landing_page_id",
    "export_path",
    "export_sha256",
    "presentation_path",
    "presentation_sha256",
    "built_at",
    "output_root",
    "page_count",
    "asset_count",
    "input_sources",
    "assets",
    "pages",
}
OPTIONAL_KEYS = {"notes"}
ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS

INPUT_SOURCE_REQUIRED_KEYS = {
    "source_id",
    "fingerprint",
    "required_for_freshness",
}
INPUT_SOURCE_ALLOWED_KEYS = INPUT_SOURCE_REQUIRED_KEYS

ASSET_REQUIRED_KEYS = {
    "path",
    "sha256",
}
ASSET_ALLOWED_KEYS = ASSET_REQUIRED_KEYS

PAGE_REQUIRED_KEYS = {
    "page_id",
    "page_family",
    "route",
    "title",
    "sha256",
    "source_ids",
    "related_page_ids",
}
PAGE_ALLOWED_KEYS = PAGE_REQUIRED_KEYS


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


@dataclass(frozen=True)
class BuildManifestReceipt:
    manifest: dict[str, Any]
    export_payload: dict[str, Any]
    presentation_payload: dict[str, Any]
    export_sha256: str
    presentation_sha256: str
    asset_records: list[dict[str, Any]]
    page_records: list[dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a knowledge-tree build manifest, including optional export-hash "
            "and output-root checks."
        )
    )
    parser.add_argument("target", help="Path to the build manifest JSON file.")
    parser.add_argument(
        "--export",
        dest="export_path",
        help="Optional export JSON path to compare against the build manifest.",
    )
    add_report_args(parser)
    return parser.parse_args()


def add_error(errors: list[dict[str, Any]], *, code: str, message: str, line: int | None = None) -> None:
    errors.append({"code": code, "line": line, "message": message})


def validate_object_keys(
    payload: dict[str, Any],
    *,
    allowed_keys: set[str],
    required_keys: set[str],
    errors: list[dict[str, Any]],
    field_label: str,
    unknown_code: str,
    missing_code: str,
) -> None:
    for key in sorted(set(payload) - allowed_keys):
        add_error(errors, code=unknown_code, message=f"unexpected {field_label} field: {key}")
    for key in sorted(required_keys):
        if key not in payload:
            add_error(errors, code=missing_code, message=f"missing required {field_label} key: {key}")


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


def validate_positive_int(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str, minimum: int = 1) -> int | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        add_error(errors, code=code, message=f"{field} must be an integer >= {minimum}")
        return None
    return value


def validate_nonblank_string(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str) -> None:
    if field not in payload:
        return
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")


def is_manifest_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value.strip()) and "\\" not in value and not path.is_absolute()


def validate_manifest_relative_path(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")
        return None
    if not is_manifest_relative_path(value):
        add_error(errors, code=code, message=f"{field} must be a relative path")
        return None
    return value


def validate_string_array(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
    allow_empty: bool = True,
) -> list[str]:
    if field not in payload:
        return []
    value = payload[field]
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field} must be a string array")
        return []
    if not allow_empty and not value:
        add_error(errors, code=code, message=f"{field} must not be empty")
        return []
    seen: set[str] = set()
    accepted: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field}[{index}] must be a non-blank string")
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{field} contains a duplicate value: {item}")
            continue
        seen.add(item)
        accepted.append(item)
    return accepted


def validate_timestamp(payload: dict[str, Any], field: str, errors: list[dict[str, Any]]) -> None:
    if field not in payload:
        return
    value = payload[field]
    if not isinstance(value, str) or not is_rfc3339_datetime(value):
        add_error(errors, code="INVALID_TIMESTAMP", message=f"{field} must be an RFC3339 timestamp")


def is_output_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value.strip()) and "\\" not in value and not path.is_absolute() and ".." not in path.parts


def validate_output_relative_path(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")
        return None
    if not is_output_relative_path(value):
        add_error(errors, code=code, message=f"{field} must be a relative path inside output_root")
        return None
    return value


def validate_public_route(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")
        return None
    validate_knowledge_tree_export.validate_route(value, errors, field=field)
    return value


def resolve_path(raw_value: str, anchor: Path) -> Path:
    raw = Path(raw_value).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (anchor / raw).resolve()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def validate_build_manifest_receipt(
    receipt: BuildManifestReceipt,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    payload = receipt.manifest
    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["inspected"] = 1

    for key in sorted(set(payload) - ALLOWED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != "knowledge-tree-build-manifest.v1":
        add_error(errors, code="INVALID_SCHEMA_VERSION", message="schema_version must equal knowledge-tree-build-manifest.v1")

    build_id = payload.get("build_id")
    if "build_id" in payload and (not isinstance(build_id, str) or not BUILD_ID_PATTERN.fullmatch(build_id)):
        add_error(errors, code="INVALID_BUILD_ID", message="build_id contains invalid characters")

    for field in ("export_id", "landing_page_id"):
        value = payload.get(field)
        if field in payload and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
            add_error(errors, code="INVALID_IDENTIFIER", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")

    validate_manifest_relative_path(
        payload,
        "export_path",
        errors,
        code="INVALID_INPUT_PATH",
    )
    validate_manifest_relative_path(
        payload,
        "presentation_path",
        errors,
        code="INVALID_INPUT_PATH",
    )
    validate_manifest_relative_path(
        payload,
        "output_root",
        errors,
        code="INVALID_OUTPUT_ROOT",
    )
    validate_timestamp(payload, "built_at", errors)
    validate_string_array(payload, "notes", errors, code="INVALID_NOTES")

    export_sha256 = payload.get("export_sha256")
    if "export_sha256" in payload and (not isinstance(export_sha256, str) or not SHA256_PATTERN.fullmatch(export_sha256)):
        add_error(errors, code="INVALID_EXPORT_SHA256", message="export_sha256 must use sha256:<64-hex>")
    elif payload.get("export_sha256") != receipt.export_sha256:
        add_error(errors, code="EXPORT_HASH_MISMATCH", message="export_sha256 does not match the export artifact hash")

    presentation_sha256 = payload.get("presentation_sha256")
    if "presentation_sha256" in payload and (
        not isinstance(presentation_sha256, str) or not SHA256_PATTERN.fullmatch(presentation_sha256)
    ):
        add_error(
            errors,
            code="INVALID_PRESENTATION_SHA256",
            message="presentation_sha256 must use sha256:<64-hex>",
        )
    elif payload.get("presentation_sha256") != receipt.presentation_sha256:
        add_error(
            errors,
            code="PRESENTATION_HASH_MISMATCH",
            message="presentation_sha256 does not match the presentation artifact hash",
        )

    input_sources = payload.get("input_sources")
    seen_source_ids: set[str] = set()
    manifest_source_ids: list[str] = []
    if not isinstance(input_sources, list) or not input_sources:
        add_error(errors, code="INVALID_INPUT_SOURCES", message="input_sources must be a non-empty array")
    else:
        for entry in input_sources:
            if not isinstance(entry, dict):
                add_error(errors, code="INPUT_SOURCE_OBJECT_REQUIRED", message="input_sources entries must be JSON objects")
                continue
            validate_object_keys(
                entry,
                allowed_keys=INPUT_SOURCE_ALLOWED_KEYS,
                required_keys=INPUT_SOURCE_REQUIRED_KEYS,
                errors=errors,
                field_label="input_sources",
                unknown_code="UNKNOWN_INPUT_SOURCE_FIELD",
                missing_code="MISSING_INPUT_SOURCE_KEY",
            )
            for field in ("source_id",):
                value = entry.get(field)
                if field in entry and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
                    add_error(errors, code="INVALID_SOURCE_ID", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")
            fingerprint = entry.get("fingerprint")
            if "fingerprint" in entry and (not isinstance(fingerprint, str) or not SHA256_PATTERN.fullmatch(fingerprint)):
                add_error(errors, code="INVALID_FINGERPRINT", message="input_sources fingerprint must use sha256:<64-hex>")
            if "required_for_freshness" in entry and not isinstance(entry.get("required_for_freshness"), bool):
                add_error(errors, code="INVALID_REQUIRED_FOR_FRESHNESS", message="input_sources required_for_freshness must be a boolean")
            source_id = entry.get("source_id")
            if isinstance(source_id, str) and ID_PATTERN.fullmatch(source_id):
                if source_id in seen_source_ids:
                    add_error(errors, code="DUPLICATE_SOURCE_ID", message=f"duplicate source_id: {source_id}")
                else:
                    seen_source_ids.add(source_id)
                    manifest_source_ids.append(source_id)

    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        add_error(errors, code="INVALID_ASSETS", message="assets must be a non-empty array")
        assets = []
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        add_error(errors, code="INVALID_PAGES", message="pages must be a non-empty array")
        pages = []

    asset_count = validate_positive_int(payload, "asset_count", errors, code="INVALID_ASSET_COUNT")
    if asset_count is not None and asset_count != len(assets):
        add_error(errors, code="ASSET_COUNT_MISMATCH", message="asset_count does not match assets length")

    page_count = validate_positive_int(payload, "page_count", errors, code="INVALID_PAGE_COUNT")
    if page_count is not None and page_count != len(pages):
        add_error(errors, code="PAGE_COUNT_MISMATCH", message="page_count does not match pages length")

    receipt_assets = receipt.asset_records
    if not isinstance(receipt_assets, list):
        add_error(errors, code="ASSET_RECORDS_MISSING", message="receipt asset records must be a list")
    elif assets != receipt_assets:
        add_error(errors, code="ASSET_RECORD_MISMATCH", message="receipt asset records do not match manifest assets")

    receipt_pages = receipt.page_records
    if not isinstance(receipt_pages, list):
        add_error(errors, code="PAGE_RECORDS_MISSING", message="receipt page records must be a list")
    elif pages != receipt_pages:
        add_error(errors, code="PAGE_RECORD_MISMATCH", message="receipt page records do not match manifest pages")

    seen_asset_paths: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            add_error(errors, code="ASSET_OBJECT_REQUIRED", message="assets entries must be JSON objects")
            continue
        validate_object_keys(
            asset,
            allowed_keys=ASSET_ALLOWED_KEYS,
            required_keys=ASSET_REQUIRED_KEYS,
            errors=errors,
            field_label="assets",
            unknown_code="UNKNOWN_ASSET_FIELD",
            missing_code="MISSING_ASSET_KEY",
        )
        path_value = validate_output_relative_path(asset, "path", errors, code="INVALID_ASSET_PATH")
        if path_value is not None and path_value in seen_asset_paths:
            add_error(errors, code="DUPLICATE_ASSET_PATH", message=f"duplicate asset path: {path_value}")
        elif path_value is not None:
            seen_asset_paths.add(path_value)
        sha_value = asset.get("sha256")
        if "sha256" in asset and (not isinstance(sha_value, str) or not SHA256_PATTERN.fullmatch(sha_value)):
            add_error(errors, code="INVALID_ASSET_SHA256", message="assets sha256 must use sha256:<64-hex>")

    seen_page_ids: set[str] = set()
    seen_routes: set[str] = set()
    page_related_refs: dict[str, list[str]] = {}
    page_source_refs: dict[str, list[str]] = {}
    for page in pages:
        if not isinstance(page, dict):
            add_error(errors, code="PAGE_OBJECT_REQUIRED", message="pages entries must be JSON objects")
            continue
        validate_object_keys(
            page,
            allowed_keys=PAGE_ALLOWED_KEYS,
            required_keys=PAGE_REQUIRED_KEYS,
            errors=errors,
            field_label="pages",
            unknown_code="UNKNOWN_PAGE_FIELD",
            missing_code="MISSING_PAGE_KEY",
        )
        for field in ("page_id", "page_family"):
            value = page.get(field)
            if field in page and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
                add_error(errors, code="INVALID_PAGE_IDENTIFIER", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")
        validate_public_route(page, "route", errors, code="INVALID_ROUTE")
        validate_nonblank_string(page, "title", errors, code="INVALID_TITLE")
        sha_value = page.get("sha256")
        if "sha256" in page and (not isinstance(sha_value, str) or not SHA256_PATTERN.fullmatch(sha_value)):
            add_error(errors, code="INVALID_PAGE_SHA256", message="page sha256 must use sha256:<64-hex>")
        source_ids = validate_string_array(page, "source_ids", errors, code="INVALID_PAGE_SOURCE_IDS", allow_empty=False)
        related_page_ids = validate_string_array(page, "related_page_ids", errors, code="INVALID_RELATED_PAGE_IDS")
        for source_id in source_ids:
            if source_id not in seen_source_ids:
                add_error(errors, code="UNKNOWN_SOURCE_REFERENCE", message=f"page references unknown source_id: {source_id}")
        page_id = page.get("page_id")
        if isinstance(page_id, str) and ID_PATTERN.fullmatch(page_id):
            if page_id in seen_page_ids:
                add_error(errors, code="DUPLICATE_PAGE_ID", message=f"duplicate page_id: {page_id}")
            else:
                seen_page_ids.add(page_id)
                page_related_refs[page_id] = related_page_ids
                page_source_refs[page_id] = source_ids
        route = page.get("route")
        if isinstance(route, str) and route.strip():
            if route in seen_routes:
                add_error(errors, code="DUPLICATE_ROUTE", message=f"duplicate route: {route}")
            seen_routes.add(route)

    for page_id, related_refs in page_related_refs.items():
        for related_page_id in related_refs:
            if related_page_id not in seen_page_ids:
                add_error(errors, code="BROKEN_PAGE_LINK", message=f"page {page_id} references unknown related_page_id: {related_page_id}")

    export_pages: list[Any] = []
    export_page_map: dict[str, dict[str, Any]] = {}
    export_payload = receipt.export_payload
    if not isinstance(export_payload, dict):
        add_error(errors, code="EXPORT_INVALID", message="export payload must be a JSON object")
        export_payload = {}
    else:
        if payload.get("export_id") != export_payload.get("export_id"):
            add_error(
                errors,
                code="EXPORT_ID_MISMATCH",
                message="build manifest export_id does not match export payload",
            )
        if payload.get("landing_page_id") != export_payload.get("landing_page_id"):
            add_error(
                errors,
                code="LANDING_PAGE_MISMATCH",
                message="build manifest landing_page_id does not match export payload",
            )

        export_source_list = export_payload.get("input_sources")
        if not isinstance(export_source_list, list):
            add_error(
                errors,
                code="EXPORT_SOURCE_LIST_MISSING",
                message="referenced export is missing a valid input_sources array",
            )
            export_source_map = {}
        else:
            export_source_map = {
                source["source_id"]: {
                    "fingerprint": source["fingerprint"],
                    "required_for_freshness": source["required_for_freshness"],
                }
                for source in export_source_list
                if isinstance(source, dict)
                and "source_id" in source
                and "fingerprint" in source
                and "required_for_freshness" in source
            }
        for source_id in manifest_source_ids:
            if source_id not in export_source_map:
                add_error(
                    errors,
                    code="SOURCE_NOT_IN_EXPORT",
                    message=f"build manifest source_id missing from export: {source_id}",
                )

        export_pages = export_payload.get("pages")
        if not isinstance(export_pages, list):
            add_error(
                errors,
                code="EXPORT_PAGE_LIST_MISSING",
                message="referenced export is missing a valid pages array",
            )
            export_page_map = {}
        else:
            export_page_map = {
                page["page_id"]: page
                for page in export_pages
                if isinstance(page, dict) and "page_id" in page
            }

        for page in pages:
            if not isinstance(page, dict):
                continue
            page_id = page.get("page_id")
            if not isinstance(page_id, str) or page_id not in export_page_map:
                add_error(
                    errors,
                    code="PAGE_NOT_IN_EXPORT",
                    message=f"build manifest page_id missing from export: {page_id}",
                )
                continue
            export_page = export_page_map[page_id]
            if page.get("route") != export_page.get("route"):
                add_error(errors, code="PAGE_ROUTE_MISMATCH", message=f"route mismatch for page_id: {page_id}")
            if page.get("title") != export_page.get("title"):
                add_error(errors, code="PAGE_TITLE_MISMATCH", message=f"title mismatch for page_id: {page_id}")
            if page.get("page_family") != export_page.get("page_family"):
                add_error(errors, code="PAGE_FAMILY_MISMATCH", message=f"page_family mismatch for page_id: {page_id}")
            if page_source_refs.get(page_id, []) != export_page.get("source_ids", []):
                add_error(errors, code="PAGE_SOURCE_IDS_MISMATCH", message=f"source_ids mismatch for page_id: {page_id}")
            if page_related_refs.get(page_id, []) != export_page.get("related_page_ids", []):
                add_error(errors, code="PAGE_RELATED_IDS_MISMATCH", message=f"related_page_ids mismatch for page_id: {page_id}")

        presentation_payload = receipt.presentation_payload
        if not isinstance(presentation_payload, dict):
            add_error(errors, code="PRESENTATION_INVALID", message="presentation payload must be a JSON object")
        else:
            presentation_pages = presentation_payload.get("page_inventory")
            if not isinstance(presentation_pages, list):
                add_error(
                    errors,
                    code="PRESENTATION_PAGE_LIST_MISSING",
                    message="presentation payload is missing a valid page_inventory array",
                )
            else:
                route_index: dict[str, dict[str, Any]] = {}
                family_index: dict[str, dict[str, Any]] = {}
                for item in presentation_pages:
                    if not isinstance(item, dict):
                        continue
                    route = item.get("route")
                    family = item.get("page_family")
                    if isinstance(route, str):
                        route_index[route] = item
                    if isinstance(family, str):
                        family_index[family] = item
                if isinstance(export_pages, list):
                    if len(export_pages) != len(route_index):
                        add_error(
                            errors,
                            code="PRESENTATION_PAGE_COUNT_MISMATCH",
                            message="presentation page_inventory count does not match export page count",
                        )
                    export_page_ids: set[str] = set()
                    export_page_routes: set[str] = set()
                    for page in export_pages:
                        if not isinstance(page, dict):
                            continue
                        page_id = page.get("page_id")
                        page_family = page.get("page_family")
                        route = page.get("route")
                        if not isinstance(page_id, str) or not isinstance(page_family, str) or not isinstance(route, str):
                            add_error(
                                errors,
                                code="EXPORT_PAGE_IDENTIFIER_MISSING",
                                message="validated export pages are missing required identifiers",
                            )
                            continue
                        presentation_page = route_index.get(route)
                        if presentation_page is None:
                            add_error(errors, code="PRESENTATION_ROUTE_MISSING", message=f"presentation missing route from export: {route}")
                            continue
                        if presentation_page.get("page_family") != page_family:
                            add_error(
                                errors,
                                code="PRESENTATION_PAGE_FAMILY_MISMATCH",
                                message=(
                                    f"presentation page_family mismatch for route {route}: "
                                    f"{presentation_page.get('page_family')} != {page_family}"
                                ),
                            )
                        export_page_ids.add(page_id)
                        export_page_routes.add(route)
                        family_page = family_index.get(page_family)
                        if family_page is None or family_page.get("route") != route:
                            add_error(
                                errors,
                                code="PRESENTATION_FAMILY_ROUTING_MISMATCH",
                                message=f"presentation family routing mismatch for page_family {page_family}",
                            )

                    for route in route_index:
                        if route not in export_page_routes:
                            add_error(errors, code="PRESENTATION_ROUTE_ORPHAN", message=f"presentation route missing from export: {route}")

                    landing_page_id = export_payload.get("landing_page_id")
                    if landing_page_id not in export_page_ids:
                        add_error(errors, code="LANDING_PAGE_ID_MISSING", message="landing_page_id is not present in export pages")

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def validate_build_manifest(
    target: Path,
    *,
    export_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code

    counts["inspected"] = 1

    for key in sorted(set(payload) - ALLOWED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != "knowledge-tree-build-manifest.v1":
        add_error(errors, code="INVALID_SCHEMA_VERSION", message="schema_version must equal knowledge-tree-build-manifest.v1")

    build_id = payload.get("build_id")
    if "build_id" in payload and (not isinstance(build_id, str) or not BUILD_ID_PATTERN.fullmatch(build_id)):
        add_error(errors, code="INVALID_BUILD_ID", message="build_id contains invalid characters")

    for field in ("export_id", "landing_page_id"):
        value = payload.get(field)
        if field in payload and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
            add_error(errors, code="INVALID_IDENTIFIER", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")

    export_path_value = validate_manifest_relative_path(
        payload,
        "export_path",
        errors,
        code="INVALID_INPUT_PATH",
    )
    presentation_path_value = validate_manifest_relative_path(
        payload,
        "presentation_path",
        errors,
        code="INVALID_INPUT_PATH",
    )
    output_root_value = validate_manifest_relative_path(
        payload,
        "output_root",
        errors,
        code="INVALID_OUTPUT_ROOT",
    )
    validate_timestamp(payload, "built_at", errors)
    validate_string_array(payload, "notes", errors, code="INVALID_NOTES")

    export_sha256 = payload.get("export_sha256")
    if "export_sha256" in payload and (not isinstance(export_sha256, str) or not SHA256_PATTERN.fullmatch(export_sha256)):
        add_error(errors, code="INVALID_EXPORT_SHA256", message="export_sha256 must use sha256:<64-hex>")
    presentation_sha256 = payload.get("presentation_sha256")
    if "presentation_sha256" in payload and (
        not isinstance(presentation_sha256, str) or not SHA256_PATTERN.fullmatch(presentation_sha256)
    ):
        add_error(
            errors,
            code="INVALID_PRESENTATION_SHA256",
            message="presentation_sha256 must use sha256:<64-hex>",
        )

    input_sources = payload.get("input_sources")
    seen_source_ids: set[str] = set()
    manifest_source_ids: list[str] = []
    if not isinstance(input_sources, list) or not input_sources:
        add_error(errors, code="INVALID_INPUT_SOURCES", message="input_sources must be a non-empty array")
    else:
        for entry in input_sources:
            if not isinstance(entry, dict):
                add_error(errors, code="INPUT_SOURCE_OBJECT_REQUIRED", message="input_sources entries must be JSON objects")
                continue
            validate_object_keys(
                entry,
                allowed_keys=INPUT_SOURCE_ALLOWED_KEYS,
                required_keys=INPUT_SOURCE_REQUIRED_KEYS,
                errors=errors,
                field_label="input_sources",
                unknown_code="UNKNOWN_INPUT_SOURCE_FIELD",
                missing_code="MISSING_INPUT_SOURCE_KEY",
            )
            for field in ("source_id",):
                value = entry.get(field)
                if field in entry and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
                    add_error(errors, code="INVALID_SOURCE_ID", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")
            fingerprint = entry.get("fingerprint")
            if "fingerprint" in entry and (not isinstance(fingerprint, str) or not SHA256_PATTERN.fullmatch(fingerprint)):
                add_error(errors, code="INVALID_FINGERPRINT", message="input_sources fingerprint must use sha256:<64-hex>")
            if "required_for_freshness" in entry and not isinstance(entry.get("required_for_freshness"), bool):
                add_error(errors, code="INVALID_REQUIRED_FOR_FRESHNESS", message="input_sources required_for_freshness must be a boolean")
            source_id = entry.get("source_id")
            if isinstance(source_id, str) and ID_PATTERN.fullmatch(source_id):
                if source_id in seen_source_ids:
                    add_error(errors, code="DUPLICATE_SOURCE_ID", message=f"duplicate source_id: {source_id}")
                else:
                    seen_source_ids.add(source_id)
                    manifest_source_ids.append(source_id)

    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        add_error(errors, code="INVALID_ASSETS", message="assets must be a non-empty array")
        assets = []
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        add_error(errors, code="INVALID_PAGES", message="pages must be a non-empty array")
        pages = []

    asset_count = validate_positive_int(payload, "asset_count", errors, code="INVALID_ASSET_COUNT")
    if asset_count is not None and asset_count != len(assets):
        add_error(errors, code="ASSET_COUNT_MISMATCH", message="asset_count does not match assets length")

    page_count = validate_positive_int(payload, "page_count", errors, code="INVALID_PAGE_COUNT")
    if page_count is not None and page_count != len(pages):
        add_error(errors, code="PAGE_COUNT_MISMATCH", message="page_count does not match pages length")

    seen_asset_paths: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict):
            add_error(errors, code="ASSET_OBJECT_REQUIRED", message="assets entries must be JSON objects")
            continue
        validate_object_keys(
            asset,
            allowed_keys=ASSET_ALLOWED_KEYS,
            required_keys=ASSET_REQUIRED_KEYS,
            errors=errors,
            field_label="assets",
            unknown_code="UNKNOWN_ASSET_FIELD",
            missing_code="MISSING_ASSET_KEY",
        )
        path_value = validate_output_relative_path(asset, "path", errors, code="INVALID_ASSET_PATH")
        if path_value is not None and path_value in seen_asset_paths:
            add_error(errors, code="DUPLICATE_ASSET_PATH", message=f"duplicate asset path: {path_value}")
        elif path_value is not None:
            seen_asset_paths.add(path_value)
        sha_value = asset.get("sha256")
        if "sha256" in asset and (not isinstance(sha_value, str) or not SHA256_PATTERN.fullmatch(sha_value)):
            add_error(errors, code="INVALID_ASSET_SHA256", message="assets sha256 must use sha256:<64-hex>")

    seen_page_ids: set[str] = set()
    seen_routes: set[str] = set()
    page_related_refs: dict[str, list[str]] = {}
    page_source_refs: dict[str, list[str]] = {}
    for page in pages:
        if not isinstance(page, dict):
            add_error(errors, code="PAGE_OBJECT_REQUIRED", message="pages entries must be JSON objects")
            continue
        validate_object_keys(
            page,
            allowed_keys=PAGE_ALLOWED_KEYS,
            required_keys=PAGE_REQUIRED_KEYS,
            errors=errors,
            field_label="pages",
            unknown_code="UNKNOWN_PAGE_FIELD",
            missing_code="MISSING_PAGE_KEY",
        )
        for field in ("page_id", "page_family"):
            value = page.get(field)
            if field in page and (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)):
                add_error(errors, code="INVALID_PAGE_IDENTIFIER", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")
        validate_public_route(page, "route", errors, code="INVALID_ROUTE")
        validate_nonblank_string(page, "title", errors, code="INVALID_TITLE")
        sha_value = page.get("sha256")
        if "sha256" in page and (not isinstance(sha_value, str) or not SHA256_PATTERN.fullmatch(sha_value)):
            add_error(errors, code="INVALID_PAGE_SHA256", message="page sha256 must use sha256:<64-hex>")
        source_ids = validate_string_array(page, "source_ids", errors, code="INVALID_PAGE_SOURCE_IDS", allow_empty=False)
        related_page_ids = validate_string_array(page, "related_page_ids", errors, code="INVALID_RELATED_PAGE_IDS")
        for source_id in source_ids:
            if source_id not in seen_source_ids:
                add_error(errors, code="UNKNOWN_SOURCE_REFERENCE", message=f"page references unknown source_id: {source_id}")
        page_id = page.get("page_id")
        if isinstance(page_id, str) and ID_PATTERN.fullmatch(page_id):
            if page_id in seen_page_ids:
                add_error(errors, code="DUPLICATE_PAGE_ID", message=f"duplicate page_id: {page_id}")
            else:
                seen_page_ids.add(page_id)
                page_related_refs[page_id] = related_page_ids
                page_source_refs[page_id] = source_ids
        route = page.get("route")
        if isinstance(route, str) and route.strip():
            if route in seen_routes:
                add_error(errors, code="DUPLICATE_ROUTE", message=f"duplicate route: {route}")
            seen_routes.add(route)

    for page_id, related_refs in page_related_refs.items():
        for related_page_id in related_refs:
            if related_page_id not in seen_page_ids:
                add_error(errors, code="BROKEN_PAGE_LINK", message=f"page {page_id} references unknown related_page_id: {related_page_id}")

    output_root_path: Path | None = None
    if output_root_value is not None:
        output_root_path = resolve_path(output_root_value, target.parent)
        if output_root_path.exists() and not output_root_path.is_dir():
            add_error(errors, code="OUTPUT_ROOT_NOT_DIRECTORY", message="output_root path is not a directory")

    if output_root_path is not None and output_root_path.is_dir():
        for asset in assets:
            if isinstance(asset, dict) and isinstance(asset.get("path"), str) and is_output_relative_path(asset["path"]):
                asset_path = output_root_path / asset["path"]
                if not asset_path.is_file():
                    add_error(errors, code="ASSET_FILE_NOT_FOUND", message=f"asset file not found: {asset['path']}")
                elif asset.get("sha256") != hash_file(asset_path):
                    add_error(errors, code="ASSET_HASH_MISMATCH", message=f"asset hash mismatch: {asset['path']}")
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("route"), str) and is_output_relative_path(page["route"]):
                page_path = output_root_path / page["route"]
                if not page_path.is_file():
                    add_error(errors, code="PAGE_FILE_NOT_FOUND", message=f"page file not found: {page['route']}")
                elif page.get("sha256") != hash_file(page_path):
                    add_error(errors, code="PAGE_HASH_MISMATCH", message=f"page hash mismatch: {page['route']}")

    resolved_export_path: Path | None = None
    if export_path is not None:
        resolved_export_path = export_path.resolve()
    elif export_path_value is not None:
        resolved_export_path = resolve_path(export_path_value, target.parent)

    if resolved_export_path is not None:
        export_result, export_exit = validate_knowledge_tree_export.validate_knowledge_tree_export(resolved_export_path)
        if export_exit != validate_knowledge_tree_export.EXIT_PASS:
            first_error = export_result["errors"][0]["message"] if export_result["errors"] else "validation failed"
            add_error(errors, code="EXPORT_INVALID", message=f"referenced export failed validation: {first_error}")
        else:
            export_payload = export_result.get("payload")
            export_sha256 = export_result.get("payload_sha256")
            if payload.get("export_sha256") != export_sha256:
                add_error(errors, code="EXPORT_HASH_MISMATCH", message="export_sha256 does not match the export file on disk")
            if isinstance(export_payload, dict):
                if payload.get("export_id") != export_payload.get("export_id"):
                    add_error(
                        errors,
                        code="EXPORT_ID_MISMATCH",
                        message="build manifest export_id does not match export file",
                    )
                if payload.get("landing_page_id") != export_payload.get("landing_page_id"):
                    add_error(
                        errors,
                        code="LANDING_PAGE_MISMATCH",
                        message="build manifest landing_page_id does not match export file",
                    )

                export_source_list = export_payload.get("input_sources")
                if not isinstance(export_source_list, list):
                    add_error(
                        errors,
                        code="EXPORT_SOURCE_LIST_MISSING",
                        message="referenced export is missing a valid input_sources array",
                    )
                    export_source_map = {}
                else:
                    export_source_map = {
                        source["source_id"]: {
                            "fingerprint": source["fingerprint"],
                            "required_for_freshness": source["required_for_freshness"],
                        }
                        for source in export_source_list
                        if isinstance(source, dict)
                        and "source_id" in source
                        and "fingerprint" in source
                        and "required_for_freshness" in source
                    }
                for source_id in manifest_source_ids:
                    if source_id not in export_source_map:
                        add_error(
                            errors,
                            code="SOURCE_NOT_IN_EXPORT",
                            message=f"build manifest source_id missing from export: {source_id}",
                        )

                export_pages = export_payload.get("pages")
                if not isinstance(export_pages, list):
                    add_error(
                        errors,
                        code="EXPORT_PAGE_LIST_MISSING",
                        message="referenced export is missing a valid pages array",
                    )
                    export_page_map = {}
                else:
                    export_page_map = {
                        page["page_id"]: page
                        for page in export_pages
                        if isinstance(page, dict) and "page_id" in page
                    }

                for page in pages:
                    if not isinstance(page, dict):
                        continue
                    page_id = page.get("page_id")
                    if not isinstance(page_id, str) or page_id not in export_page_map:
                        add_error(
                            errors,
                            code="PAGE_NOT_IN_EXPORT",
                            message=f"build manifest page_id missing from export: {page_id}",
                        )
                        continue
                    export_page = export_page_map[page_id]
                    if page.get("route") != export_page.get("route"):
                        add_error(errors, code="PAGE_ROUTE_MISMATCH", message=f"route mismatch for page_id: {page_id}")
                    if page.get("title") != export_page.get("title"):
                        add_error(errors, code="PAGE_TITLE_MISMATCH", message=f"title mismatch for page_id: {page_id}")
                    if page.get("page_family") != export_page.get("page_family"):
                        add_error(errors, code="PAGE_FAMILY_MISMATCH", message=f"page_family mismatch for page_id: {page_id}")
                    if page_source_refs.get(page_id, []) != export_page.get("source_ids", []):
                        add_error(errors, code="PAGE_SOURCE_IDS_MISMATCH", message=f"source_ids mismatch for page_id: {page_id}")
                    if page_related_refs.get(page_id, []) != export_page.get("related_page_ids", []):
                        add_error(errors, code="PAGE_RELATED_IDS_MISMATCH", message=f"related_page_ids mismatch for page_id: {page_id}")
            else:
                add_error(
                    errors,
                    code="EXPORT_PAYLOAD_MISSING",
                    message="referenced export validation did not return a parsed payload",
                )

    resolved_presentation_path: Path | None = None
    if presentation_path_value is not None:
        resolved_presentation_path = resolve_path(presentation_path_value, target.parent)

    if resolved_presentation_path is not None:
        presentation_result, presentation_exit = (
            validate_public_knowledge_tree_presentation.validate_public_knowledge_tree_presentation(
                resolved_presentation_path
            )
        )
        if presentation_exit != validate_public_knowledge_tree_presentation.EXIT_PASS:
            first_error = (
                presentation_result["errors"][0]["message"]
                if presentation_result["errors"]
                else "validation failed"
            )
            add_error(
                errors,
                code="PRESENTATION_INVALID",
                message=f"referenced presentation failed validation: {first_error}",
            )
        elif resolved_presentation_path.is_file():
            actual_presentation_sha = hash_file(resolved_presentation_path)
            if payload.get("presentation_sha256") != actual_presentation_sha:
                add_error(
                    errors,
                    code="PRESENTATION_HASH_MISMATCH",
                    message="presentation_sha256 does not match the presentation file on disk",
                )

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    result, exit_code = validate_build_manifest(Path(args.target), export_path=Path(args.export_path) if args.export_path else None)
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
