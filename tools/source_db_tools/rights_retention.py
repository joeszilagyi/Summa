"""Shared rights, retention, refetchability, and export-policy helpers.

The registry lives in a ``.yml`` path for contract stability, but the payload is
JSON-compatible YAML so the toolchain stays stdlib-only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


POLICY_PATH = Path(__file__).resolve().with_name("rights_retention_policies.yml")
SCHEMA_VERSION = "rights-retention-policy.v1"
PUBLIC_EXPORT_ELIGIBILITY_VALUES = {"blocked", "metadata_only", "eligible"}
QUOTE_ELIGIBILITY_VALUES = {"blocked", "review_required", "limited_excerpt", "eligible"}


def _require_string_set(values: Any, *, field: str) -> set[str]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"{field} must be a non-empty array")
    normalized: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be non-blank strings")
        normalized.add(item)
    return normalized


def _validate_storage_policy_classes(payload: Any, *, field: str, expect_byte_status: bool) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"{field} must be a non-empty object")
    normalized: dict[str, dict[str, Any]] = {}
    for class_name, class_policy in payload.items():
        if not isinstance(class_name, str) or not class_name.strip():
            raise ValueError(f"{field} keys must be non-blank strings")
        if not isinstance(class_policy, dict):
            raise ValueError(f"{field}.{class_name} must be an object")
        public_export_blocked = class_policy.get("public_export_blocked")
        if not isinstance(public_export_blocked, bool):
            raise ValueError(f"{field}.{class_name}.public_export_blocked must be a boolean")
        normalized_policy: dict[str, Any] = {"public_export_blocked": public_export_blocked}
        if expect_byte_status:
            byte_retention_status = class_policy.get("byte_retention_status")
            if not isinstance(byte_retention_status, str) or not byte_retention_status.strip():
                raise ValueError(f"{field}.{class_name}.byte_retention_status must be a non-blank string")
            normalized_policy["byte_retention_status"] = byte_retention_status
        normalized[class_name] = normalized_policy
    return normalized


def _validate_registry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("rights/retention policy payload must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must equal {SCHEMA_VERSION}")

    rights_postures = payload.get("rights_postures")
    if not isinstance(rights_postures, dict) or not rights_postures:
        raise ValueError("rights_postures must be a non-empty object")

    storage_policy_classes = payload.get("storage_policy_classes")
    if not isinstance(storage_policy_classes, dict):
        raise ValueError("storage_policy_classes must be an object")
    payload_storage = _validate_storage_policy_classes(
        storage_policy_classes.get("payload"),
        field="storage_policy_classes.payload",
        expect_byte_status=True,
    )
    metadata_storage = _validate_storage_policy_classes(
        storage_policy_classes.get("metadata"),
        field="storage_policy_classes.metadata",
        expect_byte_status=False,
    )

    normalized_postures: dict[str, dict[str, Any]] = {}
    for posture_name, posture_policy in rights_postures.items():
        if not isinstance(posture_name, str) or not posture_name.strip():
            raise ValueError("rights_postures keys must be non-blank strings")
        if not isinstance(posture_policy, dict):
            raise ValueError(f"rights_postures.{posture_name} must be an object")
        review_required = posture_policy.get("review_required")
        if not isinstance(review_required, bool):
            raise ValueError(f"rights_postures.{posture_name}.review_required must be a boolean")
        public_export_eligibility = posture_policy.get("public_export_eligibility")
        if public_export_eligibility not in PUBLIC_EXPORT_ELIGIBILITY_VALUES:
            raise ValueError(
                f"rights_postures.{posture_name}.public_export_eligibility must be one of: "
                f"{', '.join(sorted(PUBLIC_EXPORT_ELIGIBILITY_VALUES))}"
            )
        quote_eligibility = posture_policy.get("quote_eligibility")
        if quote_eligibility not in QUOTE_ELIGIBILITY_VALUES:
            raise ValueError(
                f"rights_postures.{posture_name}.quote_eligibility must be one of: "
                f"{', '.join(sorted(QUOTE_ELIGIBILITY_VALUES))}"
            )
        allowed_payload = _require_string_set(
            posture_policy.get("allowed_payload_storage_policy_classes"),
            field=f"rights_postures.{posture_name}.allowed_payload_storage_policy_classes",
        )
        allowed_metadata = _require_string_set(
            posture_policy.get("allowed_metadata_storage_policy_classes"),
            field=f"rights_postures.{posture_name}.allowed_metadata_storage_policy_classes",
        )
        unknown_payload = sorted(allowed_payload - set(payload_storage))
        unknown_metadata = sorted(allowed_metadata - set(metadata_storage))
        if unknown_payload:
            raise ValueError(
                f"rights_postures.{posture_name} references unknown payload storage class: {unknown_payload[0]}"
            )
        if unknown_metadata:
            raise ValueError(
                f"rights_postures.{posture_name} references unknown metadata storage class: {unknown_metadata[0]}"
            )
        normalized_postures[posture_name] = {
            "review_required": review_required,
            "public_export_eligibility": public_export_eligibility,
            "quote_eligibility": quote_eligibility,
            "allowed_payload_storage_policy_classes": sorted(allowed_payload),
            "allowed_metadata_storage_policy_classes": sorted(allowed_metadata),
        }

    refetchability = payload.get("refetchability_by_input_family")
    if not isinstance(refetchability, dict) or not refetchability:
        raise ValueError("refetchability_by_input_family must be a non-empty object")
    normalized_refetchability: dict[str, str] = {}
    for input_family, status in refetchability.items():
        if not isinstance(input_family, str) or not input_family.strip():
            raise ValueError("refetchability_by_input_family keys must be non-blank strings")
        if not isinstance(status, str) or not status.strip():
            raise ValueError(f"refetchability_by_input_family.{input_family} must be a non-blank string")
        normalized_refetchability[input_family] = status

    record_policy = payload.get("record_policy")
    if not isinstance(record_policy, dict):
        raise ValueError("record_policy must be an object")
    normalized_record_policy = {
        "rights_postures": sorted(_require_string_set(record_policy.get("rights_postures"), field="record_policy.rights_postures")),
        "byte_retention_statuses": sorted(
            _require_string_set(record_policy.get("byte_retention_statuses"), field="record_policy.byte_retention_statuses")
        ),
        "full_text_retention_statuses": sorted(
            _require_string_set(
                record_policy.get("full_text_retention_statuses"),
                field="record_policy.full_text_retention_statuses",
            )
        ),
        "refetchability_statuses": sorted(
            _require_string_set(record_policy.get("refetchability_statuses"), field="record_policy.refetchability_statuses")
        ),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "rights_postures": normalized_postures,
        "storage_policy_classes": {
            "payload": payload_storage,
            "metadata": metadata_storage,
        },
        "refetchability_by_input_family": normalized_refetchability,
        "record_policy": normalized_record_policy,
    }


@lru_cache(maxsize=1)
def load_policy_registry(path: Path = POLICY_PATH) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read rights/retention policy registry: {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON-compatible YAML in rights/retention policy registry: {path}") from exc
    return _validate_registry(payload)


def rights_postures() -> set[str]:
    return set(load_policy_registry()["rights_postures"])


def storage_policy_classes(kind: str) -> set[str]:
    return set(load_policy_registry()["storage_policy_classes"].get(kind, {}))


def review_required_rights_postures() -> set[str]:
    return {
        posture_name
        for posture_name, posture_policy in load_policy_registry()["rights_postures"].items()
        if posture_policy["review_required"] is True
    }


def public_blocking_rights_postures() -> set[str]:
    return {
        posture_name
        for posture_name, posture_policy in load_policy_registry()["rights_postures"].items()
        if posture_policy["public_export_eligibility"] == "blocked"
    }


def public_blocking_storage_classes(kind: str) -> set[str]:
    policies = load_policy_registry()["storage_policy_classes"].get(kind, {})
    return {
        class_name
        for class_name, class_policy in policies.items()
        if class_policy["public_export_blocked"] is True
    }


def derive_byte_retention_status(payload_storage_policy_class: str | None) -> str:
    if not isinstance(payload_storage_policy_class, str):
        return "retention_unknown"
    class_policy = load_policy_registry()["storage_policy_classes"]["payload"].get(payload_storage_policy_class)
    if not isinstance(class_policy, dict):
        return "retention_unknown"
    return str(class_policy["byte_retention_status"])


def derive_refetchability_status(input_family: str | None) -> str:
    if not isinstance(input_family, str):
        return "unknown"
    return load_policy_registry()["refetchability_by_input_family"].get(input_family, "unknown")


def derive_adapter_policy_facts(
    rights_and_storage: dict[str, Any] | None,
    *,
    input_family: str | None = None,
) -> dict[str, Any]:
    registry = load_policy_registry()
    rights = rights_and_storage if isinstance(rights_and_storage, dict) else {}
    rights_posture = rights.get("rights_posture") if isinstance(rights.get("rights_posture"), str) else None
    payload_policy_class = (
        rights.get("payload_storage_policy_class")
        if isinstance(rights.get("payload_storage_policy_class"), str)
        else None
    )
    metadata_policy_class = (
        rights.get("metadata_storage_policy_class")
        if isinstance(rights.get("metadata_storage_policy_class"), str)
        else None
    )
    contains_personal_data = rights.get("contains_personal_data") is True

    posture_policy = registry["rights_postures"].get(rights_posture) if rights_posture else None
    payload_policy = (
        registry["storage_policy_classes"]["payload"].get(payload_policy_class) if payload_policy_class else None
    )
    metadata_policy = (
        registry["storage_policy_classes"]["metadata"].get(metadata_policy_class) if metadata_policy_class else None
    )

    review_reasons: list[str] = []
    if posture_policy and posture_policy["review_required"] is True:
        review_reasons.append(f"rights_posture:{rights_posture}")
    if contains_personal_data:
        review_reasons.append("contains_personal_data:true")

    public_export_blockers: list[str] = []
    if posture_policy and posture_policy["public_export_eligibility"] == "blocked":
        public_export_blockers.append(f"rights_posture:{rights_posture}")
    if payload_policy_class and payload_policy and payload_policy["public_export_blocked"] is True:
        public_export_blockers.append(f"payload_storage_policy_class:{payload_policy_class}")
    if metadata_policy_class and metadata_policy and metadata_policy["public_export_blocked"] is True:
        public_export_blockers.append(f"metadata_storage_policy_class:{metadata_policy_class}")
    if contains_personal_data:
        public_export_blockers.append("contains_personal_data:true")

    public_export_eligibility = "blocked"
    quote_eligibility = "blocked"
    if posture_policy:
        public_export_eligibility = str(posture_policy["public_export_eligibility"])
        quote_eligibility = str(posture_policy["quote_eligibility"])
    if public_export_blockers:
        public_export_eligibility = "blocked"

    return {
        "rights_posture": rights_posture,
        "payload_storage_policy_class": payload_policy_class,
        "metadata_storage_policy_class": metadata_policy_class,
        "byte_retention_status": derive_byte_retention_status(payload_policy_class),
        "refetchability_status": derive_refetchability_status(input_family),
        "public_export_eligibility": public_export_eligibility,
        "quote_eligibility": quote_eligibility,
        "review_required": bool(review_reasons),
        "review_reasons": review_reasons,
        "public_export_blockers": public_export_blockers,
    }


def validate_adapter_policy(
    rights_and_storage: dict[str, Any] | None,
    *,
    input_family: str | None = None,
) -> dict[str, Any]:
    rights = rights_and_storage if isinstance(rights_and_storage, dict) else {}
    facts = derive_adapter_policy_facts(rights, input_family=input_family)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    rights_posture = facts["rights_posture"]
    payload_policy_class = facts["payload_storage_policy_class"]
    metadata_policy_class = facts["metadata_storage_policy_class"]
    registry = load_policy_registry()
    posture_policy = registry["rights_postures"].get(rights_posture) if rights_posture else None

    if posture_policy and payload_policy_class and payload_policy_class not in posture_policy["allowed_payload_storage_policy_classes"]:
        errors.append(
            {
                "code": "INVALID_RIGHTS_RETENTION_COMBINATION",
                "message": (
                    f"rights_posture {rights_posture} is incompatible with "
                    f"payload_storage_policy_class {payload_policy_class}"
                ),
            }
        )
    if posture_policy and metadata_policy_class and metadata_policy_class not in posture_policy["allowed_metadata_storage_policy_classes"]:
        errors.append(
            {
                "code": "INVALID_METADATA_RETENTION_COMBINATION",
                "message": (
                    f"rights_posture {rights_posture} is incompatible with "
                    f"metadata_storage_policy_class {metadata_policy_class}"
                ),
            }
        )

    return {"errors": errors, "warnings": warnings, "derived": facts}


def _iter_key_values(obj: Any, target_key: str) -> list[Any]:
    matches: list[Any] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == target_key:
                matches.append(value)
            matches.extend(_iter_key_values(value, target_key))
    elif isinstance(obj, list):
        for item in obj:
            matches.extend(_iter_key_values(item, target_key))
    return matches


def validate_record_policy(record: dict[str, Any]) -> dict[str, Any]:
    registry = load_policy_registry()
    record_policy = registry["record_policy"]
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    for value in _iter_key_values(record, "rights_posture"):
        if isinstance(value, str) and value not in record_policy["rights_postures"]:
            errors.append(
                {
                    "code": "INVALID_RECORD_RIGHTS_POSTURE",
                    "message": f"rights_posture must be one of: {', '.join(record_policy['rights_postures'])}",
                }
            )
            break

    for value in _iter_key_values(record, "byte_retention_status"):
        if isinstance(value, str) and value not in record_policy["byte_retention_statuses"]:
            errors.append(
                {
                    "code": "INVALID_BYTE_RETENTION_STATUS",
                    "message": (
                        "byte_retention_status must be one of: "
                        + ", ".join(record_policy["byte_retention_statuses"])
                    ),
                }
            )
            break

    for value in _iter_key_values(record, "full_text_retention_status"):
        if isinstance(value, str) and value not in record_policy["full_text_retention_statuses"]:
            errors.append(
                {
                    "code": "INVALID_FULL_TEXT_RETENTION_STATUS",
                    "message": (
                        "full_text_retention_status must be one of: "
                        + ", ".join(record_policy["full_text_retention_statuses"])
                    ),
                }
            )
            break

    for value in _iter_key_values(record, "refetchability_status"):
        if isinstance(value, str) and value not in record_policy["refetchability_statuses"]:
            warnings.append(
                {
                    "code": "UNKNOWN_REFETCHABILITY_STATUS",
                    "message": (
                        "refetchability_status is outside the known policy set: "
                        + ", ".join(record_policy["refetchability_statuses"])
                    ),
                }
            )
            break

    return {"errors": errors, "warnings": warnings}
