"""Shared confidence score and dimension validation helpers.

Documentation: docs/tools/source_db_tools/confidence_model.md. Update that paired doc
when changing dimensions, score bands, or profile policy semantics.
"""

from __future__ import annotations

import math
from typing import Any

MIN_CONFIDENCE_SCORE = 0.0
MAX_CONFIDENCE_SCORE = 1.0

CONFIDENCE_DIMENSIONS = {
    "metadata_confidence",
    "identity_confidence",
    "extraction_confidence",
    "authority_confidence",
    "relationship_confidence",
    "claim_confidence",
    "rights_confidence",
    "refetch_confidence",
    "subject_confidence",
    "topic_extension_confidence",
}

CONFIDENCE_BANDS: tuple[tuple[float, float, str], ...] = (
    (MIN_CONFIDENCE_SCORE, 0.24, "very_low"),
    (0.25, 0.49, "low"),
    (0.50, 0.74, "medium"),
    (0.75, 0.89, "high"),
    (0.90, MAX_CONFIDENCE_SCORE, "very_high"),
)
CONFIDENCE_SCORE_KEYS = {"confidence_score", "equivalence_confidence"}

DEFAULT_INVALID_SCORE_SEVERITY = "error"
DEFAULT_INVALID_DIMENSION_SEVERITY = "error"
DEFAULT_BAND_MISMATCH_SEVERITY = "warning"
DEFAULT_MISSING_CONFIDENCE_SEVERITY = "warning"
DEFAULT_INVALID_POLICY_SEVERITY = "error"


def parse_score(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def is_valid_score(value: Any) -> bool:
    score = parse_score(value)
    return score is not None and MIN_CONFIDENCE_SCORE <= score <= MAX_CONFIDENCE_SCORE


def band_for_score(value: Any) -> str | None:
    score = parse_score(value)
    if score is None or not MIN_CONFIDENCE_SCORE <= score <= MAX_CONFIDENCE_SCORE:
        return None
    for low, high, label in CONFIDENCE_BANDS:
        if low <= score <= high:
            return label
    return None


def meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def values_for_path(obj: Any, path: str) -> list[Any]:
    current = [obj]
    # Profile paths use dotted keys plus [] fan-out, e.g.
    # confidence_dimensions[].confidence_score.
    for part in path.split("."):
        next_values: list[Any] = []
        list_part = part.endswith("[]")
        key = part[:-2] if list_part else part
        for value in current:
            child = value.get(key) if isinstance(value, dict) else None
            if list_part:
                if isinstance(child, list):
                    next_values.extend(child)
                elif child is not None:
                    next_values.append(child)
            else:
                next_values.append(child)
        current = next_values
    return current


def confidence_value_paths(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    paths: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in CONFIDENCE_SCORE_KEYS:
                paths.append((path, value))
            paths.extend(confidence_value_paths(value, path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            paths.extend(confidence_value_paths(value, f"{prefix}[{index}]"))
    return paths


def validate_record_confidence(
    record: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return [
            {
                "severity": DEFAULT_INVALID_SCORE_SEVERITY,
                "code": "INVALID_RECORD_TYPE",
                "field": "record",
                "message": "confidence validation expects a record dictionary",
            }
        ]

    if policy is None:
        policy = {}
    elif not isinstance(policy, dict):
        return [
            {
                "severity": DEFAULT_INVALID_POLICY_SEVERITY,
                "code": "INVALID_CONFIDENCE_POLICY",
                "field": "confidence_policy",
                "message": "confidence policy must be a dictionary",
            }
        ]

    issues: list[dict[str, Any]] = []
    invalid_score_severity = policy.get("invalid_score_severity", DEFAULT_INVALID_SCORE_SEVERITY)
    invalid_dimension_severity = policy.get(
        "invalid_dimension_severity", DEFAULT_INVALID_DIMENSION_SEVERITY
    )
    band_mismatch_severity = policy.get("band_mismatch_severity", DEFAULT_BAND_MISMATCH_SEVERITY)
    missing_severity = policy.get("missing_confidence_severity", DEFAULT_MISSING_CONFIDENCE_SEVERITY)

    for path, value in confidence_value_paths(record):
        if value is not None and not is_valid_score(value):
            issues.append(
                {
                    "severity": invalid_score_severity,
                    "code": "INVALID_CONFIDENCE_SCORE",
                    "field": path,
                    "message": (
                        f"confidence score must be between {MIN_CONFIDENCE_SCORE:.1f} "
                        f"and {MAX_CONFIDENCE_SCORE:.1f}, got {value!r}"
                    ),
                }
            )

    confidence_dimensions = record.get("confidence_dimensions")
    if confidence_dimensions is None:
        confidence_dimensions = []
    elif not isinstance(confidence_dimensions, list):
        issues.append(
            {
                "severity": invalid_dimension_severity,
                "code": "INVALID_CONFIDENCE_DIMENSIONS_TYPE",
                "field": "confidence_dimensions",
                "message": "confidence_dimensions must be a list",
            }
        )
        confidence_dimensions = []

    for index, row in enumerate(confidence_dimensions):
        if not isinstance(row, dict):
            issues.append(
                {
                    "severity": invalid_dimension_severity,
                    "code": "INVALID_CONFIDENCE_DIMENSIONS_ENTRY",
                    "field": f"confidence_dimensions[{index}]",
                    "message": f"confidence_dimensions[{index}] must be a dict",
                }
            )
            continue

        dimension = str(row.get("dimension") or "").strip()
        score = row.get("confidence_score")
        expected_band = band_for_score(score)
        actual_band = str(row.get("confidence_band") or "").strip()
        if dimension not in CONFIDENCE_DIMENSIONS:
            issues.append(
                {
                    "severity": invalid_dimension_severity,
                    "code": "UNKNOWN_CONFIDENCE_DIMENSION",
                    "field": f"confidence_dimensions[{index}].dimension",
                    "message": f"unknown confidence dimension: {dimension!r}",
                }
            )
        if score is not None and expected_band and actual_band and actual_band != expected_band:
            issues.append(
                {
                    "severity": band_mismatch_severity,
                    "code": "CONFIDENCE_BAND_MISMATCH",
                    "field": f"confidence_dimensions[{index}].confidence_band",
                    "message": f"confidence band {actual_band!r} does not match score band {expected_band!r}",
                }
            )

    missing_warning_paths = policy.get("missing_warning_paths")
    if missing_warning_paths is None:
        missing_warning_paths = []
    elif not isinstance(missing_warning_paths, list):
        issues.append(
            {
                "severity": DEFAULT_INVALID_POLICY_SEVERITY,
                "code": "INVALID_MISSING_WARNING_PATHS_POLICY",
                "field": "confidence_policy.missing_warning_paths",
                "message": "missing_warning_paths must be a list of non-empty path strings",
            }
        )
        missing_warning_paths = []

    for index, raw_path in enumerate(missing_warning_paths):
        if not isinstance(raw_path, str):
            issues.append(
                {
                    "severity": DEFAULT_INVALID_POLICY_SEVERITY,
                    "code": "INVALID_MISSING_WARNING_PATH_POLICY",
                    "field": f"confidence_policy.missing_warning_paths[{index}]",
                    "message": "missing_warning_paths entries must be non-empty strings",
                }
            )
            continue
        path = raw_path.strip()
        if not path:
            issues.append(
                {
                    "severity": DEFAULT_INVALID_POLICY_SEVERITY,
                    "code": "INVALID_MISSING_WARNING_PATH_POLICY",
                    "field": f"confidence_policy.missing_warning_paths[{index}]",
                    "message": "missing_warning_paths entries must be non-empty strings",
                }
            )
            continue
        if not any(meaningful(value) for value in values_for_path(record, path)):
            issues.append(
                {
                    "severity": missing_severity,
                    "code": "MISSING_CONFIDENCE_SCORE",
                    "field": path,
                    "message": f"strict confidence profile is missing confidence at {path}",
                }
            )

    minimum_scores = policy.get("minimum_scores")
    if minimum_scores is None:
        minimum_scores = []
    elif not isinstance(minimum_scores, list):
        issues.append(
            {
                "severity": invalid_score_severity,
                "code": "INVALID_MINIMUM_SCORE_POLICY",
                "field": "confidence_policy.minimum_scores",
                "message": "minimum_scores must be a list",
            }
        )
        minimum_scores = []

    for row in minimum_scores:
        if not isinstance(row, dict):
            issues.append(
                {
                    "severity": invalid_score_severity,
                    "code": "INVALID_MINIMUM_SCORE_POLICY_ROW",
                    "field": "confidence_policy.minimum_scores",
                    "message": "minimum_scores entries must be mapping objects",
                }
            )
            continue

        raw_path = row.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            issues.append(
                {
                    "severity": invalid_score_severity,
                    "code": "INVALID_MINIMUM_SCORE_POLICY_PATH",
                    "field": "confidence_policy.minimum_scores",
                    "message": "minimum_scores rows require a non-empty path",
                }
            )
            continue
        path = raw_path.strip()

        minimum = parse_score(row.get("minimum"))
        if minimum is None or not MIN_CONFIDENCE_SCORE <= minimum <= MAX_CONFIDENCE_SCORE:
            issues.append(
                {
                    "severity": invalid_score_severity,
                    "code": "INVALID_MINIMUM_SCORE_POLICY_VALUE",
                    "field": "confidence_policy.minimum_scores",
                    "message": (
                        f"minimum score for path {path!r} must be between "
                        f"{MIN_CONFIDENCE_SCORE:.1f} and {MAX_CONFIDENCE_SCORE:.1f}"
                    ),
                }
            )
            continue

        severity = row.get("severity", DEFAULT_INVALID_SCORE_SEVERITY)
        values = [value for value in values_for_path(record, path) if meaningful(value)]
        if not values:
            continue
        for value in values:
            score = parse_score(value)
            if score is None or score < minimum:
                issues.append(
                    {
                        "severity": severity,
                        "code": "CONFIDENCE_BELOW_THRESHOLD",
                        "field": path,
                        "message": f"confidence score at {path} must be >= {minimum:.2f}, got {value!r}",
                    }
                )
    return issues
