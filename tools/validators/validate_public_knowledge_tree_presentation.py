#!/usr/bin/env python3
"""Validate public-presentation fixture metadata and navigation gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import PurePosixPath, Path
from typing import Any

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
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
        render_text_report,
    )


VALIDATOR_NAME = "public_knowledge_tree_presentation"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = "public-presentation.v1"
CONTRACT_DOC = "docs/project/PUBLIC_KNOWLEDGE_TREE_PRESENTATION_CONTRACT.md"

REQUIRED_TOP_LEVEL_KEYS = {"schema_version", "contract_doc", "page_inventory", "never_publish"}
ALLOWED_TOP_LEVEL_KEYS = REQUIRED_TOP_LEVEL_KEYS

REQUIRED_PAGE_KEYS = {
    "page_family",
    "route",
    "navigation_parent",
    "reader_state",
    "review_state",
    "validation_state",
    "publication_state",
    "source_transparency",
    "summary_cards",
    "empty_state",
    "redaction_gate_refs",
    "navigation_children",
    "related_routes",
    "breadcrumbs",
}
ALLOWED_PAGE_KEYS = REQUIRED_PAGE_KEYS

ALLOWED_PAGE_FAMILIES = {"home", "facet", "entity", "source", "collection", "timeline", "validation"}
ALLOWED_READER_STATES = {"ready", "sparse", "empty", "blocked"}
ALLOWED_REVIEW_STATES = {"reviewed", "needs_review", "unreviewed", "not_applicable"}
ALLOWED_VALIDATION_STATES = {"passing", "warning", "blocked", "unknown"}
ALLOWED_PUBLICATION_STATES = {"public_safe", "draft", "blocked", "previewable", "published"}

REQUIRED_PAGE_FAMILIES = {"home", "facet", "entity", "source", "collection", "timeline", "validation"}
REQUIRED_NEVER_PUBLISH_FAMILIES = {
    "private local payload paths",
    "raw prompt output",
    "runtime logs",
    "private operator notes",
    "unreviewed source text",
    "restricted files",
    "credentials",
}
REQUIRED_REDACTION_GATES = {
    "public_private_export_boundary",
    "knowledge_tree_export_validator",
    "review_gate",
}
PRIVATE_RELEASE_GATES = {"public_private_export_boundary", "review_gate"}


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate public knowledge-tree presentation metadata, redaction gates, and navigation links."
    )
    parser.add_argument("target", help="Path to a public-presentation JSON fixture.")
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
    except UnicodeDecodeError:
        add_error(errors, code="INPUT_DECODE_ERROR", message="input file is not valid UTF-8")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    except OSError:
        add_error(errors, code="INPUT_UNREADABLE", message="input file could not be read")
        return None, errors, EXIT_INPUT_UNAVAILABLE

    try:
        payload = json.loads(raw_text, object_pairs_hook=no_duplicate_object_pairs, parse_constant=reject_json_constant)
    except DuplicateJsonKeyError as exc:
        add_error(errors, code="DUPLICATE_JSON_KEY", message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(errors, code="NON_STANDARD_JSON_CONSTANT", message=str(exc))
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
        add_error(errors, code=missing_code, message=f"{label} missing required field: {key}")
    for key in sorted(set(payload) - allowed):
        add_error(errors, code=unknown_code, message=f"{label} has unexpected field: {key}")


def validate_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
    allow_empty: bool = False,
) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, str):
        add_error(errors, code=code, message=f"{field} must be a string")
        return None
    if not allow_empty and not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")
        return None
    return value


def validate_enum(
    payload: dict[str, Any],
    field: str,
    allowed: set[str],
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> str | None:
    value = validate_string(payload, field, errors, code=code)
    if value is None:
        return None
    if value not in allowed:
        add_error(errors, code=code, message=f"{field} must be one of: {', '.join(sorted(allowed))}")
    return value


def validate_string_array(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str,
    min_items: int = 0,
    unique: bool = False,
) -> list[str]:
    if field not in payload:
        return []
    value = payload[field]
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field} must be an array")
        return []
    if len(value) < min_items:
        add_error(errors, code=code, message=f"{field} must contain at least {min_items} item(s)")
    strings: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field}[{index}] must be a non-blank string")
            continue
        strings.append(item)
        if unique:
            if item in seen:
                add_error(errors, code=code, message=f"{field} contains duplicate item: {item}")
            seen.add(item)
    return strings


def validate_route(route: str, errors: list[dict[str, Any]], *, field: str = "route") -> None:
    path = PurePosixPath(route)
    if route.startswith("/") or "\\" in route or ".." in path.parts or route.endswith("/"):
        add_error(errors, code="INVALID_ROUTE", message=f"{field} must be a relative public route inside the output root")
    if not route.endswith(".html"):
        add_error(errors, code="INVALID_ROUTE", message=f"{field} must end with .html")


def validate_page(page: Any, index: int, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(page, dict):
        add_error(errors, code="INVALID_PAGE", message=f"page_inventory[{index}] must be an object")
        return None

    validate_required_and_unknown_fields(
        page,
        required=REQUIRED_PAGE_KEYS,
        allowed=ALLOWED_PAGE_KEYS,
        label=f"page_inventory[{index}]",
        errors=errors,
        missing_code="MISSING_PAGE_FIELD",
        unknown_code="UNKNOWN_PAGE_FIELD",
    )
    validate_enum(page, "page_family", ALLOWED_PAGE_FAMILIES, errors, code="INVALID_PAGE_FAMILY")
    route = validate_string(page, "route", errors, code="INVALID_ROUTE")
    validate_string(page, "navigation_parent", errors, code="INVALID_NAVIGATION_PARENT", allow_empty=True)
    validate_enum(page, "reader_state", ALLOWED_READER_STATES, errors, code="INVALID_READER_STATE")
    validate_enum(page, "review_state", ALLOWED_REVIEW_STATES, errors, code="INVALID_REVIEW_STATE")
    validate_enum(page, "validation_state", ALLOWED_VALIDATION_STATES, errors, code="INVALID_VALIDATION_STATE")
    validate_enum(page, "publication_state", ALLOWED_PUBLICATION_STATES, errors, code="INVALID_PUBLICATION_STATE")
    validate_string(page, "source_transparency", errors, code="MISSING_SOURCE_TRANSPARENCY")
    validate_string(page, "empty_state", errors, code="INVALID_EMPTY_STATE", allow_empty=True)
    validate_string_array(page, "summary_cards", errors, code="MISSING_SUMMARY_CARDS", min_items=1)
    validate_string_array(page, "redaction_gate_refs", errors, code="MISSING_REDACTION_GATE_REFS", min_items=1, unique=True)
    validate_string_array(page, "navigation_children", errors, code="INVALID_NAVIGATION_CHILDREN", unique=True)
    validate_string_array(page, "related_routes", errors, code="INVALID_RELATED_ROUTES", unique=True)
    validate_string_array(page, "breadcrumbs", errors, code="INVALID_BREADCRUMBS", min_items=1)
    if route is not None:
        validate_route(route, errors)
    return page


def validate_navigation(pages: list[dict[str, Any]], errors: list[dict[str, Any]]) -> None:
    route_to_page: dict[str, dict[str, Any]] = {}
    family_to_route: dict[str, str] = {}
    for index, page in enumerate(pages):
        route = page.get("route")
        family = page.get("page_family")
        if isinstance(route, str):
            if route in route_to_page:
                add_error(errors, code="DUPLICATE_ROUTE", message=f"duplicate route: {route}")
            route_to_page[route] = page
        if isinstance(family, str) and isinstance(route, str):
            family_to_route.setdefault(family, route)

    missing_families = sorted(REQUIRED_PAGE_FAMILIES - set(family_to_route))
    for family in missing_families:
        add_error(errors, code="MISSING_PAGE_FAMILY", message=f"page_inventory must include page_family: {family}")

    for page in pages:
        route = page.get("route")
        parent = page.get("navigation_parent")
        if isinstance(parent, str) and parent and parent not in route_to_page:
            add_error(errors, code="BROKEN_NAVIGATION_PARENT", message=f"{route} parent route does not exist: {parent}")
        if page.get("page_family") == "home" and parent:
            add_error(errors, code="INVALID_NAVIGATION_PARENT", message="home page navigation_parent must be empty")
        if page.get("page_family") != "home" and not parent:
            add_error(errors, code="INVALID_NAVIGATION_PARENT", message=f"{route} navigation_parent must reference a public route")

        for field, code in (
            ("navigation_children", "BROKEN_NAVIGATION_CHILD"),
            ("related_routes", "BROKEN_RELATED_ROUTE"),
            ("breadcrumbs", "BROKEN_BREADCRUMB"),
        ):
            values = page.get(field)
            if not isinstance(values, list):
                continue
            for linked_route in values:
                if isinstance(linked_route, str) and linked_route not in route_to_page:
                    add_error(errors, code=code, message=f"{route} {field} route does not exist: {linked_route}")

        breadcrumbs = page.get("breadcrumbs")
        if isinstance(route, str) and isinstance(breadcrumbs, list) and breadcrumbs:
            if breadcrumbs[-1] != route:
                add_error(errors, code="INVALID_BREADCRUMBS", message=f"{route} breadcrumbs must end at the current route")
            if parent and (len(breadcrumbs) < 2 or breadcrumbs[-2] != parent):
                add_error(errors, code="INVALID_BREADCRUMBS", message=f"{route} breadcrumbs must include its parent route")


def validate_redaction_gates(payload: dict[str, Any], pages: list[dict[str, Any]], errors: list[dict[str, Any]]) -> None:
    never_publish = validate_string_array(
        payload,
        "never_publish",
        errors,
        code="INVALID_NEVER_PUBLISH",
        min_items=8,
        unique=True,
    )
    lower_never_publish = {item.lower() for item in never_publish}
    for family in REQUIRED_NEVER_PUBLISH_FAMILIES:
        if family.lower() not in lower_never_publish:
            add_error(errors, code="MISSING_NEVER_PUBLISH_FAMILY", message=f"never_publish must include: {family}")

    all_gate_refs: set[str] = set()
    for page in pages:
        refs = page.get("redaction_gate_refs")
        if isinstance(refs, list):
            all_gate_refs.update(ref for ref in refs if isinstance(ref, str))
    for gate in REQUIRED_REDACTION_GATES:
        if gate not in all_gate_refs:
            add_error(errors, code="MISSING_REQUIRED_GATE_REF", message=f"page redaction_gate_refs must include: {gate}")
    if lower_never_publish and all_gate_refs.isdisjoint(PRIVATE_RELEASE_GATES):
        add_error(
            errors,
            code="MISSING_PRIVATE_RELEASE_GATE",
            message="never_publish families require at least one public/private or release-readiness gate reference",
        )


def validate_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    validate_required_and_unknown_fields(
        payload,
        required=REQUIRED_TOP_LEVEL_KEYS,
        allowed=ALLOWED_TOP_LEVEL_KEYS,
        label="top-level object",
        errors=errors,
        missing_code="MISSING_TOP_LEVEL_FIELD",
        unknown_code="UNKNOWN_TOP_LEVEL_FIELD",
    )
    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    if payload.get("contract_doc") != CONTRACT_DOC:
        add_error(errors, code="INVALID_CONTRACT_DOC", message=f"contract_doc must equal {CONTRACT_DOC}")

    page_inventory = payload.get("page_inventory")
    pages: list[dict[str, Any]] = []
    if not isinstance(page_inventory, list):
        add_error(errors, code="INVALID_PAGE_INVENTORY", message="page_inventory must be an array")
    else:
        if len(page_inventory) < 7:
            add_error(errors, code="INVALID_PAGE_INVENTORY", message="page_inventory must contain at least 7 pages")
        for index, page in enumerate(page_inventory):
            validated = validate_page(page, index, errors)
            if validated is not None:
                pages.append(validated)
        validate_navigation(pages, errors)

    validate_redaction_gates(payload, pages, errors)
    return errors


def validate_public_knowledge_tree_presentation(target: Path) -> tuple[dict[str, Any], int]:
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
    }
    return report, exit_code


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    report, exit_code = validate_public_knowledge_tree_presentation(target)
    report["scenario"] = args.scenario
    if args.target_id:
        report["target"] = args.target_id
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
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
