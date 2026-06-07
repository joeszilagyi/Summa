from __future__ import annotations

import pytest

from tools.common.source_adapter_handoff import validate_source_adapter_handoff_record
from tools.scripts import execute_source_adapter as source_executor
from tools.scripts.execute_source_adapter import (
    SourceAcquisitionError,
    determine_executor_mode,
    determine_variant,
    validate_handoff_sequence,
)


def local_adapter_payload(*, variant_fields: list[str] | None = None) -> dict[str, object]:
    return {
        "adapter_id": "fixture-local-adapter",
        "workspace_id": "alpha_subject",
        "input_family": "local_directory",
        "normalized_handoff": {
            "record_family": "source_lead",
            "batch_unit": "per_file",
            "preserve_fields": ["original_locator", "source_metadata"],
            "source_specific_fields": variant_fields or ["relative_path", "source_filename"],
        },
    }


def remote_adapter_payload() -> dict[str, object]:
    return {
        "adapter_id": "fixture-remote-adapter",
        "workspace_id": "alpha_subject",
        "input_family": "remote_url_manifest",
        "normalized_handoff": {
            "record_family": "source_lead",
            "batch_unit": "per_reference",
            "preserve_fields": ["original_locator", "source_metadata"],
            "source_specific_fields": ["manifest_url"],
        },
    }


def local_record(sequence: int = 1) -> dict[str, object]:
    return {
        "schema_version": "source-adapter-handoff.v1",
        "adapter_id": "fixture-local-adapter",
        "workspace_id": "alpha_subject",
        "record_family": "source_lead",
        "batch_unit": "per_file",
        "adapter_path": "/tmp/fixture-local-adapter.json",
        "emitted_at": "2026-06-03T12:34:56Z",
        "sequence": sequence,
        "resolved_source_path": "/tmp/fixture-local-adapter/root/file.txt",
        "relative_path": "file.txt",
        "preserved": {
            "original_locator": {"adapter_local_path": "root"},
            "discovery_provenance": "fixture",
            "rights_posture": "private_local_only",
            "byte_retention_status": "not_retained_local",
            "discard_metadata": {"discard_required": False, "discard_reason": None},
            "refetchability_status": "local_replayable",
            "transform_lineage": [],
            "source_metadata": {"content_kinds": ["text"], "hazard_flags": []},
        },
        "source_specific": {
            "relative_path": "file.txt",
            "source_filename": "file.txt",
        },
    }


def remote_record(sequence: int = 1) -> dict[str, object]:
    record = local_record(sequence)
    record["adapter_id"] = "fixture-remote-adapter"
    record["record_family"] = "source_lead"
    record["batch_unit"] = "per_reference"
    record["remote_state"] = "configured_remote"
    record["network_access_attempted"] = False
    record["source_specific"] = {"manifest_url": "https://example.test/manifest.jsonl"}
    return record


def structured_record(sequence: int = 1) -> dict[str, object]:
    record = local_record(sequence)
    record["source_specific"] = {
        "relative_path": "row-1",
        "source_filename": "data.jsonl",
        "structured_format": "jsonl",
        "record_locator": "row:1",
        "record_kind": "record",
    }
    return record


def test_handoff_validator_rejects_adapter_record_variant_mismatches() -> None:
    local_remote_errors = validate_source_adapter_handoff_record(remote_record(), local_adapter_payload())
    structured_local_errors = validate_source_adapter_handoff_record(
        local_record(),
        local_adapter_payload(variant_fields=["relative_path", "source_filename", "structured_format", "record_locator", "record_kind"]),
    )

    assert any("variant remote_url_manifest does not match adapter-declared variant local_source" in error for error in local_remote_errors)
    assert any("variant local_source does not match adapter-declared variant structured_data" in error for error in structured_local_errors)


def test_determine_variant_rejects_mixed_handoff_variants() -> None:
    with pytest.raises(SourceAcquisitionError, match="must not mix source-adapter handoff variants"):
        determine_variant([local_record(), structured_record()], adapter_payload={})


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([], "does not contain any records"),
        ([local_record(1), local_record(1)], "must not repeat sequence values"),
        ([local_record(1), local_record(3)], "must be contiguous starting at 1"),
    ],
)
def test_validate_handoff_sequence_rejects_bad_sequences(records: list[dict[str, object]], message: str) -> None:
    with pytest.raises(SourceAcquisitionError, match=message):
        validate_handoff_sequence(records)


def test_structured_json_loaders_reject_nonstandard_constants(tmp_path: Path) -> None:
    json_path = tmp_path / "bad.json"
    json_path.write_text('{"value": NaN}\n', encoding="utf-8")
    jsonl_path = tmp_path / "bad.jsonl"
    jsonl_path.write_text('{"value": Infinity}\n{"value": -Infinity}\n', encoding="utf-8")

    json_records, json_errors = source_executor.load_json_record_map(json_path, record_path=None)
    jsonl_records, jsonl_errors = source_executor.load_jsonl_record_map(jsonl_path)

    assert json_records == {}
    assert json_errors == [{"context": "line:1", "reason": "unsupported JSON constant: NaN"}]
    assert jsonl_records == {}
    assert jsonl_errors == [{"context": "line:1", "reason": "unsupported JSON constant: Infinity"}, {"context": "line:2", "reason": "unsupported JSON constant: -Infinity"}]


def test_serialize_structured_value_rejects_nonstandard_floats() -> None:
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        source_executor.serialize_structured_value({"value": float("nan")})


@pytest.mark.parametrize(
    ("mode", "variant", "message"),
    [
        ("remote", "local_source", "mode=remote does not match handoff variant local_source"),
        ("local", "remote_url_manifest", "mode=local does not match handoff variant remote_url_manifest"),
    ],
)
def test_determine_executor_mode_rejects_mode_variant_mismatch(mode: str, variant: str, message: str) -> None:
    with pytest.raises(SourceAcquisitionError, match=message):
        determine_executor_mode(mode, variant=variant)
