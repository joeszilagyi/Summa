#!/usr/bin/env python3
"""Relationship predicate registry helpers.

This module provides registry loading and validation helpers for
``source_relationship`` predicate metadata used by schema-profile validation and
full-fidelity export output.

Documentation: ``docs/tools/source_db_tools/relationship_predicates.md``. Keep that
file, this helper, the registry, and exporter/profile tests in sync when
predicate semantics change.

Assumptions:
 - The registry file is local to this module and named
   ``relationship_predicates.yml`` for historical compatibility while containing
   JSON content.
 - Registry rows are dictionaries keyed by ``predicate`` and include a top-level
   ``predicates`` array and ``schema_version``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().with_name("relationship_predicates.yml")
PREDICATES_KEY = "predicates"
SCHEMA_VERSION_KEY = "schema_version"
PREDICATE_FIELD = "predicate"
SCHEMA_VERSION_UNKNOWN = "unknown"


def normalize_predicate(value: Any) -> str:
    return str(value or "").strip().lower()


def _registry_rows(path: Path = REGISTRY_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    loaded = load_registry(path)
    rows = loaded.get(PREDICATES_KEY)
    if not isinstance(rows, list):
        raise ValueError(f"Registry key '{PREDICATES_KEY}' must be a list: {path}")

    predicates_seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Registry row {index} must be an object: {path}")
        predicate = normalize_predicate(row.get(PREDICATE_FIELD))
        if not predicate:
            raise ValueError(f"Registry row {index} missing non-empty '{PREDICATE_FIELD}': {path}")
        if predicate in predicates_seen:
            raise ValueError(f"Duplicate relationship predicate {predicate!r}: {path}")
        predicates_seen.add(predicate)
        validated.append(row)
    return loaded, validated


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON payload in relationship predicate registry: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Relationship predicate registry must be an object: {path}")
    return payload


def predicates(path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    _, rows = _registry_rows(path)
    return list(rows)


def predicate_map(path: Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    return {normalize_predicate(row[PREDICATE_FIELD]): row for row in predicates(path)}


def get(predicate: str, path: Path = REGISTRY_PATH) -> dict[str, Any] | None:
    return predicate_map(path).get(normalize_predicate(predicate))


def has_evidence(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    return any(row.get(field) for field in ("evidence_locator", "evidence_highlight_id"))


def validate_relationships(
    rows: Any,
    *,
    unknown_severity: str = "warning",
    evidence_missing_severity: str = "warning",
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return [
            {
                "severity": unknown_severity,
                "code": "INVALID_SOURCE_RELATIONSHIPS",
                "field": "source_relationships",
                "message": "source_relationships must be a list",
            }
        ]

    registry = predicate_map()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append(
                {
                    "severity": unknown_severity,
                    "code": "INVALID_SOURCE_RELATIONSHIP",
                    "field": f"source_relationships[{index}]",
                    "message": "source_relationship row must be an object",
                }
            )
            continue
        predicate = normalize_predicate(row.get("predicate"))
        definition = registry.get(predicate)
        if definition is None:
            issues.append(
                {
                    "severity": unknown_severity,
                    "code": "UNKNOWN_RELATIONSHIP_PREDICATE",
                    "field": f"source_relationships[{index}].predicate",
                    "message": f"unknown relationship predicate: {predicate or '<missing>'}",
                }
            )
            continue
        if definition.get("evidence_required") and not has_evidence(row):
            issues.append(
                {
                    "severity": evidence_missing_severity,
                    "code": "RELATIONSHIP_EVIDENCE_REQUIRED",
                    "field": f"source_relationships[{index}].evidence_locator",
                    "message": f"{predicate} requires evidence_locator or evidence_highlight_id",
                }
            )
    return issues


def derive_inverse(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    definition = get(str(row.get("predicate") or ""))
    if not definition or not definition.get("inverse_predicate"):
        return None
    inverse = dict(row)
    inverse["from_namespace"] = row.get("to_namespace")
    inverse["from_id"] = row.get("to_id")
    inverse["to_namespace"] = row.get("from_namespace")
    inverse["to_id"] = row.get("from_id")
    inverse["predicate"] = definition["inverse_predicate"]
    return inverse


def definitions_for_rows(
    rows: list[dict[str, Any]],
    path: Path = REGISTRY_PATH,
) -> list[dict[str, Any]]:
    registry, registry_rows = _registry_rows(path)
    mapped = {normalize_predicate(row[PREDICATE_FIELD]): row for row in registry_rows}
    definitions: list[dict[str, Any]] = []
    seen_predicates: set[str] = set()
    row_predicates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        predicate = normalize_predicate(row.get(PREDICATE_FIELD))
        if predicate and predicate not in seen_predicates:
            seen_predicates.add(predicate)
            row_predicates.append(predicate)
    for predicate in sorted(row_predicates):
        definition = mapped.get(predicate)
        if definition:
            out = dict(definition)
            out["registry_schema_version"] = registry.get(SCHEMA_VERSION_KEY, SCHEMA_VERSION_UNKNOWN)
            out["registered"] = True
        else:
            out = {
                "predicate": predicate,
                "label": predicate,
                "registry_schema_version": registry.get(SCHEMA_VERSION_KEY, SCHEMA_VERSION_UNKNOWN),
                "registered": False,
                "export_behavior": "full_fidelity_local_only_until_registered",
                "review_requirement": "review_required",
            }
        definitions.append(out)
    return definitions
