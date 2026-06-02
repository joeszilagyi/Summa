"""Claim type registry helpers.

This module loads and validates ``source_claim.claim_type`` registry metadata
used by schema-profile validation and full-fidelity export output.

Documentation: ``docs/tools/source_db_tools/claim_types.md``. Keep that file, this
helper, the registry, and exporter/profile tests in sync when claim semantics
change.

Assumptions:
 - The registry file is local to this module and named ``claim_types.yml`` for
   historical compatibility while containing JSON content.
 - Registry rows are dictionaries keyed by ``claim_type`` and include a
   top-level ``claim_types`` array and ``schema_version``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().with_name("claim_types.yml")
CLAIM_TYPES_KEY = "claim_types"
SCHEMA_VERSION_KEY = "schema_version"
CLAIM_TYPE_FIELD = "claim_type"
SOURCE_CLAIMS_FIELD = "source_claims"
SCHEMA_VERSION_UNKNOWN = "unknown"
APPROVED_REVIEW_STATES = {"accepted", "approved", "curated", "reviewed", "human_approved"}
_REGISTRY_CACHE: dict[Path, dict[str, Any]] = {}


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Load and validate the claim type registry JSON payload."""
    cached = _REGISTRY_CACHE.get(path)
    if cached is not None:
        return cached

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read claim type registry: {path}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in claim type registry: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"claim type registry must be a JSON object: {path}")

    claim_types_value = payload.get(CLAIM_TYPES_KEY)
    if not isinstance(claim_types_value, list):
        raise ValueError(f"claim type registry '{CLAIM_TYPES_KEY}' must be a list: {path}")

    _REGISTRY_CACHE[path] = payload
    return payload


def _registry_rows(path: Path = REGISTRY_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    loaded = load_registry(path)
    rows = loaded[CLAIM_TYPES_KEY]
    claim_types_seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"claim type registry row {index} must be an object: {path}")
        claim_type = normalize_claim_type(row.get(CLAIM_TYPE_FIELD))
        if not claim_type:
            raise ValueError(f"claim type registry row {index} missing non-empty '{CLAIM_TYPE_FIELD}': {path}")
        if claim_type in claim_types_seen:
            raise ValueError(f"duplicate claim type {claim_type!r}: {path}")
        claim_types_seen.add(claim_type)
        validated.append(row)
    return loaded, validated


def claim_types(path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    """Return registry claim type rows."""
    _, rows = _registry_rows(path)
    return list(rows)


def claim_type_map(path: Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    """Map normalized claim types to their registry definitions."""
    return {normalize_claim_type(row[CLAIM_TYPE_FIELD]): row for row in claim_types(path)}


def normalize_claim_type(value: Any) -> str:
    """Normalize a claim type value to the registry matching form."""
    if value is None:
        return ""
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def get(claim_type: Any, path: Path = REGISTRY_PATH) -> dict[str, Any] | None:
    """Return a claim type definition when registered, else ``None``."""
    return claim_type_map(path).get(normalize_claim_type(claim_type))


def claim_has_locator_or_highlight(row: dict[str, Any]) -> bool:
    """Return true when locator/highlight-style evidence exists on a claim row."""
    return bool(
        row.get("evidence_locator")
        or row.get("evidence_highlight_id")
        or row.get("evidence_text")
        or row.get("highlight_id")
    )


def record_has_metadata_support(record: dict[str, Any]) -> bool:
    """Check if the source record carries any metadata that can satisfy metadata evidence."""
    if not isinstance(record, dict):
        return False

    if record.get("work_metadata") or record.get("metadata"):
        return True
    if record.get("work_identifiers") or record.get("identifiers") or record.get("work_urls") or record.get("urls"):
        return True
    work = record.get("work", {})
    if not isinstance(work, dict):
        return False
    return any(work.get(field) for field in ("title", "work_type", "publication_date", "publisher", "container_title"))


def claim_has_supported_evidence(
    row: dict[str, Any],
    record: dict[str, Any],
    definition: dict[str, Any],
) -> bool:
    """Evaluate whether a row has evidence via allowed evidence channels."""
    if not isinstance(row, dict) or not isinstance(record, dict) or not isinstance(definition, dict):
        return False
    if claim_has_locator_or_highlight(row):
        return True
    allowed = set(definition.get("allowed_evidence_requirements", []))
    if "metadata" in allowed and record_has_metadata_support(record):
        return True
    if any(row.get(field) for field in ("evidence_source", "evidence_type", "evidence_provenance_id")):
        return True
    return False


def validate_claims(
    record: dict[str, Any],
    *,
    unknown_severity: str = "error",
    evidence_missing_severity: str = "error",
    review_required_severity: str = "warning",
    path: Path = REGISTRY_PATH,
) -> list[dict[str, Any]]:
    """Validate claim rows in a source record."""
    issues: list[dict[str, Any]] = []
    definitions = claim_type_map(path)

    rows = record.get(SOURCE_CLAIMS_FIELD, [])
    if not isinstance(rows, list):
        return [
            {
                "severity": unknown_severity,
                "code": "INVALID_SOURCE_CLAIMS",
                "field": SOURCE_CLAIMS_FIELD,
                "message": "source_claims must be a list",
            }
        ]

    for index, row in enumerate(rows):
        field_prefix = f"{SOURCE_CLAIMS_FIELD}[{index}]"
        if not isinstance(row, dict):
            issues.append(
                {
                    "severity": unknown_severity,
                    "code": "INVALID_SOURCE_CLAIM",
                    "field": field_prefix,
                    "message": "source_claim row must be an object",
                }
            )
            continue
        claim_type = normalize_claim_type(row.get("claim_type"))
        definition = definitions.get(claim_type)
        if not definition:
            issues.append(
                {
                    "severity": unknown_severity,
                    "code": "UNKNOWN_CLAIM_TYPE",
                    "field": f"{field_prefix}.claim_type",
                    "message": f"unknown source_claim claim_type: {row.get('claim_type')!r}",
                }
            )
            continue
        if definition.get("evidence_mandatory") and not claim_has_supported_evidence(row, record, definition):
            issues.append(
                {
                    "severity": evidence_missing_severity,
                    "code": "CLAIM_EVIDENCE_REQUIRED",
                    "field": f"{field_prefix}.evidence_locator",
                    "message": (
                        f"claim_type {claim_type!r} requires evidence from "
                        f"{definition.get('allowed_evidence_requirements', [])}"
                    ),
                }
            )
        review_requirement = str(definition.get("default_review_requirement") or "")
        review_state = normalize_claim_type(row.get("review_state"))
        if review_requirement == "human_review_required" and review_state not in APPROVED_REVIEW_STATES:
            issues.append(
                {
                    "severity": review_required_severity,
                    "code": "CLAIM_REQUIRES_HUMAN_REVIEW",
                    "field": f"{field_prefix}.review_state",
                    "message": f"claim_type {claim_type!r} remains human-review-required until approved",
                }
            )
    return issues


def definitions_for_rows(
    rows: list[dict[str, Any]],
    *,
    path: Path = REGISTRY_PATH,
) -> list[dict[str, Any]]:
    """Build claim type definitions for rows, including unknown ones as provisional."""
    registry, registry_rows = _registry_rows(path)
    known = {}
    for row in registry_rows:
        claim_type = normalize_claim_type(row.get(CLAIM_TYPE_FIELD))
        known[claim_type] = row

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        claim_type = normalize_claim_type(row.get("claim_type"))
        if not claim_type or claim_type in seen:
            continue
        seen.add(claim_type)
        definition = known.get(claim_type)
        if definition:
            emitted = dict(definition)
            emitted["registered"] = True
            emitted["registry_schema_version"] = registry.get(SCHEMA_VERSION_KEY, SCHEMA_VERSION_UNKNOWN)
            out.append(emitted)
        else:
            out.append(
                {
                    "claim_type": claim_type,
                    "label": str(row.get("claim_type") or claim_type),
                    "description": "Unregistered local/provisional claim type.",
                    "allowed_evidence_requirements": [],
                    "direct_quote_recommended": False,
                    "evidence_mandatory": False,
                    "default_review_requirement": "review_required",
                    "export_visibility": "full_fidelity_only",
                    "examples": [],
                    "confidence_guidance": "Review and either map to a registered claim type or keep as a local extension.",
                    "registered": False,
                    "registry_schema_version": registry.get(SCHEMA_VERSION_KEY, SCHEMA_VERSION_UNKNOWN),
                }
            )
    return out
