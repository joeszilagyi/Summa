from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import exceptions, validators


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = REPO_ROOT / "config"

EXPLICIT_SCHEMA_PAIRS = {
    Path("canonical_graph_model_outline.json"): Path("canonical_graph_model_outline.schema.json"),
    Path("domain_packs/general.v1.json"): Path("domain_packs/domain_pack.schema.json"),
    Path("domain_packs/organism.v1.json"): Path("domain_packs/domain_pack.schema.json"),
    Path("durability_policies/local_first_crown_jewels.v1.json"): Path(
        "crown_jewel_store_policy.schema.json"
    ),
    Path("standards_profiles/dcmi.v1.json"): Path("standards_profiles/standards_profile.schema.json"),
    Path("standards_profiles/premis.v1.json"): Path("standards_profiles/standards_profile.schema.json"),
    Path("standards_profiles/rico.v1.json"): Path("standards_profiles/standards_profile.schema.json"),
    Path("standards_profiles/nara_preservation_readiness.v1.json"): Path(
        "standards_profiles/standards_profile.schema.json"
    ),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_schema_paths(config_root: Path) -> list[Path]:
    return sorted(config_root.rglob("*.schema.json"))


def iter_instance_paths(config_root: Path) -> list[Path]:
    return sorted(
        path for path in config_root.rglob("*.json") if not path.name.endswith(".schema.json")
    )


def validator_class_for_schema(schema: dict[str, Any], *, schema_path: Path) -> type[Any]:
    if "$schema" not in schema:
        raise AssertionError(f"{schema_path}: missing required $schema declaration")
    return validators.validator_for(schema)


def check_schema_payload(schema: dict[str, Any], *, schema_path: Path) -> type[Any]:
    validator_cls = validator_class_for_schema(schema, schema_path=schema_path)
    try:
        validator_cls.check_schema(schema)
    except exceptions.SchemaError as exc:
        schema_error_path = "/".join(str(part) for part in exc.absolute_schema_path) or "$"
        raise AssertionError(
            f"{schema_path}: invalid JSON Schema for {validator_cls.META_SCHEMA.get('$id', 'unknown draft')} "
            f"at schema path {schema_error_path}: {exc.message}"
        ) from exc
    return validator_cls


def format_instance_path(error: exceptions.ValidationError) -> str:
    if not error.absolute_path:
        return "$"
    return "$." + ".".join(str(part) for part in error.absolute_path)


def validate_instance_payload(
    instance: Any,
    schema: dict[str, Any],
    *,
    instance_path: Path,
    schema_path: Path,
) -> None:
    validator_cls = check_schema_payload(schema, schema_path=schema_path)
    validator = validator_cls(schema)
    try:
        validator.validate(instance)
    except exceptions.ValidationError as exc:
        raise AssertionError(
            f"{instance_path}: does not satisfy {schema_path} at {format_instance_path(exc)}: {exc.message}"
        ) from exc


def discover_schema_pairs(config_root: Path) -> tuple[dict[Path, Path], list[Path]]:
    schema_paths = {path.relative_to(config_root) for path in iter_schema_paths(config_root)}
    pairs: dict[Path, Path] = {}
    unpaired: list[Path] = []

    for instance_path in iter_instance_paths(config_root):
        relative_instance = instance_path.relative_to(config_root)
        explicit = EXPLICIT_SCHEMA_PAIRS.get(relative_instance)
        if explicit is not None:
            if explicit not in schema_paths:
                raise AssertionError(
                    f"{relative_instance}: explicit schema mapping points to missing schema {explicit}"
                )
            pairs[relative_instance] = explicit
            continue

        direct_candidate = relative_instance.with_name(
            relative_instance.name[:-5] + ".schema.json"
        )
        if direct_candidate in schema_paths:
            pairs[relative_instance] = direct_candidate
            continue

        unpaired.append(relative_instance)

    return pairs, unpaired


def test_all_checked_in_config_schemas_are_valid_json_schema() -> None:
    for schema_path in iter_schema_paths(CONFIG_ROOT):
        schema = load_json(schema_path)
        check_schema_payload(schema, schema_path=schema_path)


def test_all_checked_in_config_instances_have_schema_coverage() -> None:
    pairs, unpaired = discover_schema_pairs(CONFIG_ROOT)
    assert unpaired == [], (
        "Every checked-in config JSON instance must have schema coverage or an explicit reason. "
        f"Unpaired instances: {[str(path) for path in unpaired]}"
    )
    assert len(pairs) == len(iter_instance_paths(CONFIG_ROOT))


def test_checked_in_config_instances_validate_against_paired_schemas() -> None:
    pairs, unpaired = discover_schema_pairs(CONFIG_ROOT)
    assert unpaired == []

    for relative_instance, relative_schema in sorted(pairs.items()):
        instance_path = CONFIG_ROOT / relative_instance
        schema_path = CONFIG_ROOT / relative_schema
        instance = load_json(instance_path)
        schema = load_json(schema_path)
        validate_instance_payload(
            instance,
            schema,
            instance_path=instance_path,
            schema_path=schema_path,
        )


def test_invalid_schema_payload_is_rejected() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": "not-an-array",
    }

    with pytest.raises(AssertionError, match="invalid JSON Schema"):
        check_schema_payload(schema, schema_path=Path("inline-invalid.schema.json"))


def test_invalid_instance_payload_is_rejected() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }

    with pytest.raises(AssertionError, match=r"\$: 'name' is a required property"):
        validate_instance_payload(
            {},
            schema,
            instance_path=Path("inline-instance.json"),
            schema_path=Path("inline.schema.json"),
        )


def test_schema_rejects_unknown_saturation_state_fields() -> None:
    registry_path = CONFIG_ROOT / "topic_workspace_registry.schema.json"
    schema = load_json(registry_path)

    invalid_instance = {
        "schema_version": "topic-workspace-registry.v1",
        "workspaces": [
            {
                "workspace_id": "synthetic-workspace",
                "topic_label": "Synthetic Workspace",
                "workspace_root": "/tmp/synthetic-workspace",
                "domain_pack": "general.v1",
                "lifecycle_state": "active",
                "schedule_posture": "scheduled",
                "workspace_policy_class": "private_local",
                "scheduler_policy": {
                    "saturation_state": {
                        "state": "active",
                        "reason_codes": [],
                        "scheduler_action": "run",
                        "unexpected_field": "should-fail",
                    }
                },
            }
        ],
    }

    with pytest.raises(AssertionError, match="Additional properties are not allowed"):
        validate_instance_payload(
            invalid_instance,
            schema,
            instance_path=Path("synthetic-topic-workspace-registry.json"),
            schema_path=registry_path.relative_to(REPO_ROOT),
        )
