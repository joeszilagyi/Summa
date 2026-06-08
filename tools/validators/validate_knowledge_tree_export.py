#!/usr/bin/env python3
"""Validate knowledge-tree export JSON artifacts and public authority gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
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
        resolve_report_root,
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
        resolve_report_root,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.authority_ladder import (  # noqa: E402
    ALLOWED_FIELD_REVIEW_STATES,
    AUTHORITY_CONTENT_CLASSES,
    BLOCKING_FIELD_REVIEW_STATES,
    is_public_export_profile,
    is_visible_publication_state,
)

VALIDATOR_NAME = "knowledge_tree_export"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = "knowledge-tree-export.v1"
FIXTURE_PATH = "tests/fixtures/validators/knowledge_tree_export/valid_minimal/inputs/knowledge_tree_export.json"

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REVIEW_QUEUE_REF_PATTERN = re.compile(r"^(?:[a-z_]+:\d+|frs:[a-z0-9][a-z0-9._:-]*)$")

REQUIRED_KEYS = {
    "schema_version",
    "export_id",
    "display_name",
    "workspace_id",
    "export_profile",
    "generated_at",
    "landing_page_id",
    "page_families",
    "input_sources",
    "pages",
}
OPTIONAL_KEYS = {"notes"}

REQUIRED_INPUT_SOURCE_KEYS = {
    "source_id",
    "source_kind",
    "logical_name",
    "locator_path",
    "fingerprint",
    "storage_policy_class",
    "rights_posture",
    "required_for_freshness",
}

REQUIRED_PAGE_KEYS = {
    "page_id",
    "page_family",
    "route",
    "title",
    "lede",
    "review_posture",
    "publication_state",
    "source_ids",
    "related_page_ids",
    "summary_cards",
    "sections",
}
OPTIONAL_PAGE_KEYS = {"authority_basis"}

REQUIRED_SUMMARY_CARD_KEYS = {"label", "value"}

OPTIONAL_SECTION_KEYS = {"paragraphs", "bullet_items", "link_page_ids", "authority_basis"}
REQUIRED_SECTION_KEYS = {"heading"}

REQUIRED_AUTHORITY_KEYS = {"content_class", "review_queue_refs", "field_review_entries", "metadata_exception_reason"}
REQUIRED_FIELD_REVIEW_ENTRY_KEYS = {"entry_id", "field_path", "state", "review_queue_ref"}

ALLOWED_EXPORT_PROFILES = {"local_preview", "public_preview", "public_release"}
ALLOWED_REVIEW_POSTURES = {"reviewed", "needs_review", "unreviewed", "not_applicable"}
ALLOWED_PUBLICATION_STATES = {"public_safe", "draft", "blocked", "previewable", "published"}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a knowledge-tree export JSON artifact.",
        epilog=(
            "Reads one export JSON file and writes the validation report to stdout.\n"
            "Optional --report-json/--report-text paths are written atomically.\n\n"
            f"Example:\n  python3 tools/validators/validate_knowledge_tree_export.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the knowledge-tree export JSON file.")
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
        add_error(errors, code="JSON_PARSE_ERROR", line=exc.lineno, message="invalid JSON syntax")
        return None, errors, EXIT_VALIDATION_FAILED

    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED
    return payload, errors, EXIT_PASS


def validate_required_and_unknown_fields(
    payload: dict[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
    errors: list[dict[str, Any]],
    missing_code: str,
    unknown_code: str,
) -> None:
    for key in sorted(required - set(payload)):
        add_error(errors, code=missing_code, message=f"missing required {label} key: {key}")
    for key in sorted(set(payload) - allowed):
        add_error(errors, code=unknown_code, message=f"unexpected {label} field: {key}")


def validate_nonblank_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")
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
    value = payload.get(field)
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field} must be an array of non-blank strings")
        return []
    if not allow_empty and not value:
        add_error(errors, code=code, message=f"{field} must not be empty")
        return []
    accepted: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field}[{index}] must be a non-blank string")
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{field} contains duplicate value: {item}")
            continue
        seen.add(item)
        accepted.append(item)
    return accepted


def validate_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.fullmatch(value))


def validate_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_PATTERN.fullmatch(value))


def validate_route(route: str, errors: list[dict[str, Any]], *, field: str = "route") -> None:
    path = PurePosixPath(route)
    if route.startswith("/") or "\\" in route or ".." in path.parts or route.endswith("/"):
        add_error(errors, code="INVALID_ROUTE", message=f"{field} must be a relative public route inside the output root")
    if not route.endswith(".html"):
        add_error(errors, code="INVALID_ROUTE", message=f"{field} must end with .html")


def validate_summary_cards(page: dict[str, Any], page_id: str, errors: list[dict[str, Any]]) -> None:
    cards = page.get("summary_cards")
    if not isinstance(cards, list) or not cards:
        add_error(errors, code="INVALID_SUMMARY_CARDS", message=f"page {page_id} summary_cards must be a non-empty array")
        return
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            add_error(errors, code="INVALID_SUMMARY_CARD", message=f"page {page_id} summary_cards[{index}] must be an object")
            continue
        validate_required_and_unknown_fields(
            card,
            required=REQUIRED_SUMMARY_CARD_KEYS,
            allowed=REQUIRED_SUMMARY_CARD_KEYS,
            label=f"summary_cards[{index}]",
            errors=errors,
            missing_code="MISSING_SUMMARY_CARD_KEY",
            unknown_code="UNKNOWN_SUMMARY_CARD_FIELD",
        )
        validate_nonblank_string(card, "label", errors, code="INVALID_SUMMARY_CARD")
        validate_nonblank_string(card, "value", errors, code="INVALID_SUMMARY_CARD")


def validate_field_review_entries(authority_basis: dict[str, Any], context_label: str, errors: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    value = authority_basis.get("field_review_entries")
    if not isinstance(value, list):
        add_error(errors, code="INVALID_FIELD_REVIEW_ENTRIES", message=f"{context_label} authority_basis field_review_entries must be an array")
        return []

    accepted: list[dict[str, str | None]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            add_error(errors, code="INVALID_FIELD_REVIEW_ENTRY", message=f"{context_label} field_review_entries[{index}] must be an object")
            continue
        validate_required_and_unknown_fields(
            item,
            required=REQUIRED_FIELD_REVIEW_ENTRY_KEYS,
            allowed=REQUIRED_FIELD_REVIEW_ENTRY_KEYS,
            label=f"{context_label} field_review_entries[{index}]",
            errors=errors,
            missing_code="MISSING_FIELD_REVIEW_ENTRY_KEY",
            unknown_code="UNKNOWN_FIELD_REVIEW_ENTRY_FIELD",
        )
        entry_id = validate_nonblank_string(item, "entry_id", errors, code="INVALID_FIELD_REVIEW_ENTRY")
        field_path = validate_nonblank_string(item, "field_path", errors, code="INVALID_FIELD_REVIEW_ENTRY")
        state = validate_nonblank_string(item, "state", errors, code="INVALID_FIELD_REVIEW_STATE")
        review_queue_ref = item.get("review_queue_ref")
        if review_queue_ref is not None and (
            not isinstance(review_queue_ref, str) or not REVIEW_QUEUE_REF_PATTERN.fullmatch(review_queue_ref)
        ):
            add_error(
                errors,
                code="INVALID_REVIEW_QUEUE_REF",
                message=f"{context_label} field_review_entries[{index}].review_queue_ref must be a review queue object ref",
            )
            review_queue_ref = None
        if isinstance(state, str) and state not in ALLOWED_FIELD_REVIEW_STATES:
            add_error(
                errors,
                code="INVALID_FIELD_REVIEW_STATE",
                message=(
                    f"{context_label} field_review_entries[{index}].state must be one of: "
                    f"{', '.join(sorted(ALLOWED_FIELD_REVIEW_STATES))}"
                ),
            )
        if entry_id and field_path and isinstance(state, str) and state in ALLOWED_FIELD_REVIEW_STATES:
            accepted.append(
                {
                    "entry_id": entry_id,
                    "field_path": field_path,
                    "state": state,
                    "review_queue_ref": review_queue_ref if isinstance(review_queue_ref, str) else None,
                }
            )
    return accepted


def validate_authority_basis(
    authority_basis: Any,
    *,
    page_id: str,
    context_label: str,
    page_review_posture: str | None,
    export_profile: str | None,
    publication_state: str | None,
    errors: list[dict[str, Any]],
) -> None:
    if authority_basis is None:
        if (
            is_public_export_profile(export_profile)
            and is_visible_publication_state(publication_state)
            and page_review_posture != "reviewed"
        ):
            add_error(
                errors,
                code="AUTHORITY_REVIEW_REQUIRED",
                message=(
                    f"{context_label} exports authoritative content with review_posture "
                    f"{page_review_posture or '(missing)'}; add an explicit metadata-only exception "
                    f"or review the linked queue objects"
                ),
            )
        return

    if not isinstance(authority_basis, dict):
        add_error(errors, code="INVALID_AUTHORITY_BASIS", message=f"{context_label} authority_basis must be an object")
        return

    validate_required_and_unknown_fields(
        authority_basis,
        required=REQUIRED_AUTHORITY_KEYS,
        allowed=REQUIRED_AUTHORITY_KEYS,
        label=f"{context_label} authority_basis",
        errors=errors,
        missing_code="MISSING_AUTHORITY_BASIS_KEY",
        unknown_code="UNKNOWN_AUTHORITY_BASIS_FIELD",
    )
    content_class = validate_nonblank_string(authority_basis, "content_class", errors, code="INVALID_AUTHORITY_CONTENT_CLASS")
    if isinstance(content_class, str) and content_class not in AUTHORITY_CONTENT_CLASSES:
        add_error(
            errors,
            code="INVALID_AUTHORITY_CONTENT_CLASS",
            message=f"{context_label} authority_basis content_class must be one of: {', '.join(sorted(AUTHORITY_CONTENT_CLASSES))}",
        )
    review_queue_refs = validate_string_array(
        authority_basis,
        "review_queue_refs",
        errors,
        code="INVALID_REVIEW_QUEUE_REFS",
    )
    for ref in review_queue_refs:
        if not REVIEW_QUEUE_REF_PATTERN.fullmatch(ref):
            add_error(
                errors,
                code="INVALID_REVIEW_QUEUE_REF",
                message=f"{context_label} review_queue_refs contains invalid object ref: {ref}",
            )
    field_review_entries = validate_field_review_entries(authority_basis, context_label, errors)

    metadata_exception_reason = authority_basis.get("metadata_exception_reason")
    if metadata_exception_reason is not None and (
        not isinstance(metadata_exception_reason, str) or not metadata_exception_reason.strip()
    ):
        add_error(
            errors,
            code="INVALID_METADATA_EXCEPTION_REASON",
            message=f"{context_label} metadata_exception_reason must be null or a non-blank string",
        )
        metadata_exception_reason = None

    if not (
        is_public_export_profile(export_profile)
        and is_visible_publication_state(publication_state)
        and isinstance(content_class, str)
        and content_class in AUTHORITY_CONTENT_CLASSES
    ):
        return

    if content_class == "metadata_only":
        if page_review_posture != "reviewed" and not metadata_exception_reason:
            add_error(
                errors,
                code="METADATA_EXCEPTION_REQUIRED",
                message=(
                    f"{context_label} is metadata_only but review_posture is "
                    f"{page_review_posture or '(missing)'}; metadata_exception_reason is required"
                ),
            )
        return

    if not review_queue_refs:
        add_error(
            errors,
            code="AUTHORITY_REVIEW_QUEUE_REF_REQUIRED",
            message=f"{context_label} authoritative export must include review_queue_refs for operator follow-up",
        )
    if page_review_posture != "reviewed":
        refs_text = ", ".join(review_queue_refs) if review_queue_refs else "(missing review_queue_refs)"
        add_error(
            errors,
            code="AUTHORITY_REVIEW_REQUIRED",
            message=(
                f"{context_label} exports authoritative content with review_posture "
                f"{page_review_posture or '(missing)'}; review queue refs: {refs_text}"
            ),
        )
    for entry in field_review_entries:
        state = entry["state"]
        if state in BLOCKING_FIELD_REVIEW_STATES:
            ref = entry.get("review_queue_ref") or (review_queue_refs[0] if review_queue_refs else "(missing review_queue_ref)")
            add_error(
                errors,
                code="FIELD_AUTHORITY_BLOCKED",
                message=(
                    f"{context_label} field {entry['field_path']} is {state} and blocks public export; "
                    f"review queue ref: {ref}"
                ),
            )


def validate_sections(
    page: dict[str, Any],
    page_id: str,
    export_profile: str | None,
    publication_state: str | None,
    review_posture: str | None,
    page_authority_basis: Any,
    errors: list[dict[str, Any]],
) -> list[str]:
    sections = page.get("sections")
    if not isinstance(sections, list) or not sections:
        add_error(errors, code="INVALID_SECTIONS", message=f"page {page_id} sections must be a non-empty array")
        return []

    linked_page_ids: list[str] = []
    for index, section in enumerate(sections):
        context_label = f"page {page_id} section[{index}]"
        if not isinstance(section, dict):
            add_error(errors, code="INVALID_SECTION", message=f"{context_label} must be an object")
            continue
        validate_required_and_unknown_fields(
            section,
            required=REQUIRED_SECTION_KEYS,
            allowed=REQUIRED_SECTION_KEYS | OPTIONAL_SECTION_KEYS,
            label=context_label,
            errors=errors,
            missing_code="MISSING_SECTION_KEY",
            unknown_code="UNKNOWN_SECTION_FIELD",
        )
        validate_nonblank_string(section, "heading", errors, code="INVALID_SECTION")
        paragraph_count = 0
        if "paragraphs" in section:
            paragraphs = validate_string_array(section, "paragraphs", errors, code="INVALID_SECTION_PARAGRAPHS")
            paragraph_count += len(paragraphs)
        if "bullet_items" in section:
            bullet_items = validate_string_array(section, "bullet_items", errors, code="INVALID_SECTION_BULLETS")
            paragraph_count += len(bullet_items)
        if paragraph_count == 0:
            add_error(
                errors,
                code="EMPTY_SECTION_CONTENT",
                message=f"{context_label} must include paragraphs or bullet_items",
            )
        if "link_page_ids" in section:
            linked_page_ids.extend(validate_string_array(section, "link_page_ids", errors, code="INVALID_SECTION_LINKS"))
        section_authority_basis = section.get("authority_basis")
        if section_authority_basis is not None or page_authority_basis is None:
            validate_authority_basis(
                section_authority_basis,
                page_id=page_id,
                context_label=context_label,
                page_review_posture=review_posture,
                export_profile=export_profile,
                publication_state=publication_state,
                errors=errors,
            )
    return linked_page_ids


def validate_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    validate_required_and_unknown_fields(
        payload,
        required=REQUIRED_KEYS,
        allowed=REQUIRED_KEYS | OPTIONAL_KEYS,
        label="top-level",
        errors=errors,
        missing_code="MISSING_REQUIRED_KEY",
        unknown_code="UNKNOWN_FIELD",
    )
    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")

    for field in ("export_id", "workspace_id", "landing_page_id"):
        value = payload.get(field)
        if field in payload and not validate_id(value):
            add_error(errors, code="INVALID_IDENTIFIER", message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$")

    validate_nonblank_string(payload, "display_name", errors, code="INVALID_DISPLAY_NAME")
    export_profile = validate_nonblank_string(payload, "export_profile", errors, code="INVALID_EXPORT_PROFILE")
    if isinstance(export_profile, str) and export_profile not in ALLOWED_EXPORT_PROFILES:
        add_error(
            errors,
            code="INVALID_EXPORT_PROFILE",
            message=f"export_profile must be one of: {', '.join(sorted(ALLOWED_EXPORT_PROFILES))}",
        )
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not is_rfc3339_datetime(generated_at):
        add_error(errors, code="INVALID_GENERATED_AT", message="generated_at must be an RFC3339 timestamp")

    page_families = validate_string_array(payload, "page_families", errors, code="INVALID_PAGE_FAMILIES", allow_empty=False)

    input_sources = payload.get("input_sources")
    seen_source_ids: set[str] = set()
    if not isinstance(input_sources, list) or not input_sources:
        add_error(errors, code="INVALID_INPUT_SOURCES", message="input_sources must be a non-empty array")
    else:
        for index, source in enumerate(input_sources):
            if not isinstance(source, dict):
                add_error(errors, code="INPUT_SOURCE_OBJECT_REQUIRED", message=f"input_sources[{index}] must be an object")
                continue
            validate_required_and_unknown_fields(
                source,
                required=REQUIRED_INPUT_SOURCE_KEYS,
                allowed=REQUIRED_INPUT_SOURCE_KEYS,
                label=f"input_sources[{index}]",
                errors=errors,
                missing_code="MISSING_INPUT_SOURCE_KEY",
                unknown_code="UNKNOWN_INPUT_SOURCE_FIELD",
            )
            source_id = source.get("source_id")
            if not validate_id(source_id):
                add_error(errors, code="INVALID_SOURCE_ID", message="input_sources source_id must match ^[a-z0-9][a-z0-9._-]*$")
            elif source_id in seen_source_ids:
                add_error(errors, code="DUPLICATE_SOURCE_ID", message=f"duplicate source_id: {source_id}")
            else:
                seen_source_ids.add(source_id)
            for field, code in (
                ("source_kind", "INVALID_SOURCE_KIND"),
                ("logical_name", "INVALID_LOGICAL_NAME"),
                ("locator_path", "INVALID_LOCATOR_PATH"),
                ("storage_policy_class", "INVALID_STORAGE_POLICY_CLASS"),
                ("rights_posture", "INVALID_RIGHTS_POSTURE"),
            ):
                validate_nonblank_string(source, field, errors, code=code)
            if not validate_sha256(source.get("fingerprint")):
                add_error(errors, code="INVALID_FINGERPRINT", message="input_sources fingerprint must use sha256:<64-hex>")
            if "required_for_freshness" in source and not isinstance(source.get("required_for_freshness"), bool):
                add_error(
                    errors,
                    code="INVALID_REQUIRED_FOR_FRESHNESS",
                    message="input_sources required_for_freshness must be a boolean",
                )

    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        add_error(errors, code="INVALID_PAGES", message="pages must be a non-empty array")
        return errors

    seen_page_ids: set[str] = set()
    seen_routes: set[str] = set()
    page_ids_in_order: list[str] = []
    page_related_refs: dict[str, list[str]] = {}
    section_link_refs: dict[str, list[str]] = {}

    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            add_error(errors, code="PAGE_OBJECT_REQUIRED", message=f"pages[{index}] must be an object")
            continue
        validate_required_and_unknown_fields(
            page,
            required=REQUIRED_PAGE_KEYS,
            allowed=REQUIRED_PAGE_KEYS | OPTIONAL_PAGE_KEYS,
            label=f"pages[{index}]",
            errors=errors,
            missing_code="MISSING_PAGE_KEY",
            unknown_code="UNKNOWN_PAGE_FIELD",
        )
        page_id = page.get("page_id")
        if not validate_id(page_id):
            add_error(errors, code="INVALID_PAGE_IDENTIFIER", message="page_id must match ^[a-z0-9][a-z0-9._-]*$")
            page_id = f"page[{index}]"
        else:
            if page_id in seen_page_ids:
                add_error(errors, code="DUPLICATE_PAGE_ID", message=f"duplicate page_id: {page_id}")
            else:
                seen_page_ids.add(page_id)
                page_ids_in_order.append(page_id)

        page_family = page.get("page_family")
        if not validate_id(page_family):
            add_error(errors, code="INVALID_PAGE_IDENTIFIER", message="page_family must match ^[a-z0-9][a-z0-9._-]*$")
        validate_nonblank_string(page, "title", errors, code="INVALID_TITLE")
        validate_nonblank_string(page, "lede", errors, code="INVALID_LEDE")

        review_posture = validate_nonblank_string(page, "review_posture", errors, code="INVALID_REVIEW_POSTURE")
        if isinstance(review_posture, str) and review_posture not in ALLOWED_REVIEW_POSTURES:
            add_error(
                errors,
                code="INVALID_REVIEW_POSTURE",
                message=f"page {page_id} review_posture must be one of: {', '.join(sorted(ALLOWED_REVIEW_POSTURES))}",
            )

        publication_state = validate_nonblank_string(page, "publication_state", errors, code="INVALID_PUBLICATION_STATE")
        if isinstance(publication_state, str) and publication_state not in ALLOWED_PUBLICATION_STATES:
            add_error(
                errors,
                code="INVALID_PUBLICATION_STATE",
                message=f"page {page_id} publication_state must be one of: {', '.join(sorted(ALLOWED_PUBLICATION_STATES))}",
            )

        route = validate_nonblank_string(page, "route", errors, code="INVALID_ROUTE")
        if route is not None:
            validate_route(route, errors)
            if route in seen_routes:
                add_error(errors, code="DUPLICATE_ROUTE", message=f"duplicate route: {route}")
            else:
                seen_routes.add(route)

        source_ids = validate_string_array(page, "source_ids", errors, code="INVALID_PAGE_SOURCE_IDS", allow_empty=False)
        for source_id in source_ids:
            if source_id not in seen_source_ids:
                add_error(errors, code="UNKNOWN_SOURCE_REFERENCE", message=f"page references unknown source_id: {source_id}")
        related_page_ids = validate_string_array(page, "related_page_ids", errors, code="INVALID_RELATED_PAGE_IDS")
        page_related_refs[str(page_id)] = related_page_ids

        validate_summary_cards(page, str(page_id), errors)
        section_link_refs[str(page_id)] = validate_sections(
            page,
            str(page_id),
            export_profile if isinstance(export_profile, str) else None,
            publication_state if isinstance(publication_state, str) else None,
            review_posture if isinstance(review_posture, str) else None,
            page.get("authority_basis"),
            errors,
        )
        validate_authority_basis(
            page.get("authority_basis"),
            page_id=str(page_id),
            context_label=f"page {page_id}",
            page_review_posture=review_posture if isinstance(review_posture, str) else None,
            export_profile=export_profile if isinstance(export_profile, str) else None,
            publication_state=publication_state if isinstance(publication_state, str) else None,
            errors=errors,
        )

    if isinstance(payload.get("landing_page_id"), str) and payload["landing_page_id"] not in seen_page_ids:
        add_error(errors, code="UNKNOWN_LANDING_PAGE", message=f"landing_page_id references unknown page_id: {payload['landing_page_id']}")

    present_page_families = {
        page.get("page_family") for page in pages if isinstance(page, dict)
    }
    for page_family in page_families:
        if page_family not in present_page_families:
            add_error(errors, code="MISSING_PAGE_FAMILY", message=f"page_families references page_family not present in pages: {page_family}")

    for page_id, related_refs in page_related_refs.items():
        for related_page_id in related_refs:
            if related_page_id not in seen_page_ids:
                add_error(errors, code="BROKEN_PAGE_LINK", message=f"page {page_id} references unknown related_page_id: {related_page_id}")
    for page_id, link_refs in section_link_refs.items():
        for linked_page_id in link_refs:
            if linked_page_id not in seen_page_ids:
                add_error(errors, code="BROKEN_SECTION_LINK", message=f"page {page_id} references unknown section link_page_id: {linked_page_id}")

    return errors


def validate_knowledge_tree_export(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, load_exit = load_json_object(target)
    if payload is not None and load_exit == EXIT_PASS:
        errors.extend(validate_payload(payload))

    if load_exit == EXIT_INPUT_UNAVAILABLE:
        exit_code = EXIT_INPUT_UNAVAILABLE
    elif errors:
        exit_code = EXIT_VALIDATION_FAILED
    else:
        exit_code = EXIT_PASS

    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "target": display_path(str(target)),
        "status": "pass" if exit_code == EXIT_PASS else "fail",
        "counts": {
            "inspected": 0 if load_exit == EXIT_INPUT_UNAVAILABLE else 1,
            "accepted": 1 if exit_code == EXIT_PASS else 0,
            "rejected": 1 if exit_code == EXIT_VALIDATION_FAILED else 0,
            "deferred": 0,
        },
        "errors": errors,
        "warnings": [],
        "output_artifacts": {},
        "scenario": None,
        "payload": payload if exit_code == EXIT_PASS else None,
        "payload_sha256": hash_file(target) if exit_code == EXIT_PASS else None,
    }
    return report, exit_code


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    report, exit_code = validate_knowledge_tree_export(target)
    report["scenario"] = args.scenario
    if args.target_id:
        report["target"] = args.target_id
    report_root = resolve_report_root(target, report_root=args.report_root)
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=report["counts"],
        errors=report["errors"],
        output_artifacts=report["output_artifacts"],
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=report["scenario"],
        status=report["status"],
        target=report["target"],
        validator=VALIDATOR_NAME,
        warnings=report["warnings"],
        report_root=report_root,
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
