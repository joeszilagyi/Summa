"""Source type registry helpers for canonical source/work records.

This module loads and validates ``work.work_type`` registry metadata used by
legacy backfill and schema-profile validation.

Assumptions:
 - The registry file is local to this module and named ``source_types.yml`` for
   historical compatibility while containing JSON content.
 - Registry rows are dictionaries keyed by ``work_type`` and include a
   top-level ``source_types`` array and ``schema_version``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REGISTRY_PATH = Path(__file__).resolve().with_name("source_types.yml")
SOURCE_TYPES_KEY = "source_types"
WORK_TYPE_FIELD = "work_type"
_REGISTRY_CACHE: dict[Path, dict[str, Any]] = {}


def normalize_work_type(value: Any) -> str:
    """Normalize a work type value to the registry lookup form."""
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "_")


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Load and validate the source type registry JSON payload."""
    cached = _REGISTRY_CACHE.get(path)
    if cached is not None:
        return cached

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read source type registry: {path}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in source type registry: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"source type registry must be a JSON object: {path}")
    rows = payload.get(SOURCE_TYPES_KEY)
    if not isinstance(rows, list):
        raise ValueError(f"source type registry '{SOURCE_TYPES_KEY}' must be a list: {path}")

    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"source type registry row {index} must be an object: {path}")
        work_type = normalize_work_type(row.get(WORK_TYPE_FIELD))
        if not work_type:
            raise ValueError(f"source type registry row {index} missing non-empty '{WORK_TYPE_FIELD}': {path}")
        if work_type in seen:
            raise ValueError(f"duplicate work_type {work_type!r}: {path}")
        seen.add(work_type)

    _REGISTRY_CACHE[path] = payload
    return payload


def source_types(path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    """Return registered source type rows."""
    return list(load_registry(path)[SOURCE_TYPES_KEY])


def source_type_map(path: Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    """Map normalized work types to registry rows."""
    return {normalize_work_type(row[WORK_TYPE_FIELD]): row for row in source_types(path)}


def get(work_type: Any, path: Path = REGISTRY_PATH) -> dict[str, Any] | None:
    """Return the registry definition for one work type when present."""
    return source_type_map(path).get(normalize_work_type(work_type))


def validation_issue(
    work_type: Any,
    *,
    required_mappings: list[str] | tuple[str, ...] | None = None,
    path: Path = REGISTRY_PATH,
) -> tuple[str, str] | None:
    """Return one validation issue for a work type, else ``None``."""
    normalized = normalize_work_type(work_type)
    if not normalized:
        return ("MISSING_SOURCE_TYPE", "work_type is missing")

    definition = get(normalized, path)
    if definition is None:
        return ("UNKNOWN_SOURCE_TYPE", f"work_type {normalized!r} is not registered")

    mappings = definition.get("mappings", {})
    if not isinstance(mappings, dict):
        mappings = {}
    missing_mappings = [key for key in (required_mappings or []) if not mappings.get(key)]
    if missing_mappings:
        return (
            "MISSING_SOURCE_TYPE_MAPPING",
            f"work_type {normalized!r} is missing required mappings: {', '.join(sorted(missing_mappings))}",
        )

    if definition.get("provisional"):
        return ("PROVISIONAL_SOURCE_TYPE", f"work_type {normalized!r} remains provisional")
    return None
