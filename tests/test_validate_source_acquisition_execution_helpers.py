from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.validators import validate_source_acquisition_execution as validator

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"


def copy_execution_fixture(tmp_path: Path) -> Path:
    run_dir = tmp_path / "execution_run"
    shutil.copytree(EXECUTION_FIXTURE_DIR, run_dir)
    return run_dir


def load_fixture_records(run_dir: Path) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return execution_record, capture_events, extraction_records


def test_load_json_object_reports_duplicate_keys_and_top_level_type_errors(
    tmp_path: Path,
) -> None:
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text('{"alpha": 1, "alpha": 2}', encoding="utf-8")
    with pytest.raises(validator.DuplicateJsonKeyError):
        validator._load_json_object(duplicate_path, label="duplicate payload")

    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level JSON value must be an object"):
        validator._load_json_object(array_path, label="array payload")


def test_load_json_object_reports_missing_and_non_utf8_paths(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError, match="missing payload path does not exist"):
        validator._load_json_object(missing_path, label="missing payload")

    invalid_utf8_path = tmp_path / "invalid.json"
    invalid_utf8_path.write_bytes(b'{"alpha": "\xff"}')
    with pytest.raises(UnicodeDecodeError, match="invalid payload is not UTF-8"):
        validator._load_json_object(invalid_utf8_path, label="invalid payload")


def test_hash_file_reports_missing_path_with_label(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.bin"
    with pytest.raises(FileNotFoundError, match="missing artifact path does not exist"):
        validator.hash_file(missing_path, label="missing artifact")


def test_iter_jsonl_records_streams_and_rejects_invalid_records(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "records.jsonl"
    jsonl_path.write_text("\n" + json.dumps({"alpha": 1}, ensure_ascii=False) + "\n", encoding="utf-8")

    records = list(validator._iter_jsonl_records(jsonl_path, label="records"))
    assert records == [{"alpha": 1}]

    invalid_value_path = tmp_path / "invalid-value.jsonl"
    invalid_value_path.write_text(
        json.dumps({"alpha": 1}, ensure_ascii=False)
        + "\n"
        + json.dumps(["not", "an", "object"], ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid-value line 2 must contain a JSON object"):
        list(validator._iter_jsonl_records(invalid_value_path, label="invalid-value"))

    invalid_path = tmp_path / "invalid.jsonl"
    invalid_path.write_bytes(b'{"alpha": 1}\n{"beta": "\xff"}\n')
    with pytest.raises(UnicodeDecodeError, match="invalid records line 2 is not UTF-8"):
        list(validator._iter_jsonl_records(invalid_path, label="invalid records"))


def test_resolve_execution_artifact_paths_accepts_directory_and_relative_file_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    execution_path = run_dir / "execution-record.json"
    execution_path.write_text("{}", encoding="utf-8")

    resolved_from_dir = validator.resolve_execution_artifact_paths(run_dir)
    assert resolved_from_dir["run_dir"] == run_dir
    assert resolved_from_dir["execution_record"] == execution_path

    monkeypatch.chdir(tmp_path)
    relative_resolved = validator.resolve_execution_artifact_paths(Path("run/execution-record.json"))
    assert relative_resolved["run_dir"] == run_dir
    assert relative_resolved["execution_record"] == execution_path


def test_validate_execution_record_reports_remote_specific_failures(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, capture_events, extraction_records = load_fixture_records(run_dir)
    capture_count = len(capture_events)
    extraction_count = len(extraction_records)

    denied_payload = dict(execution_record)
    denied_payload["adapter_type"] = "remote_url_manifest"
    denied_payload["status"] = "denied"
    denied_payload["network_access_attempted"] = True
    denied_errors: list[dict[str, object]] = []
    validator.validate_execution_record(
        denied_payload,
        capture_event_count=capture_count,
        extraction_record_count=extraction_count,
        errors=denied_errors,
    )
    assert any(error["code"] == "REMOTE_DENIAL_ATTEMPTED_NETWORK" for error in denied_errors)

    attempted_payload = dict(execution_record)
    attempted_payload["adapter_type"] = "remote_url_manifest"
    attempted_payload["status"] = "completed"
    attempted_payload["network_access_attempted"] = False
    attempted_errors: list[dict[str, object]] = []
    validator.validate_execution_record(
        attempted_payload,
        capture_event_count=max(capture_count, 1),
        extraction_record_count=extraction_count,
        errors=attempted_errors,
    )
    assert any(
        error["code"] == "REMOTE_CAPTURE_WITHOUT_NETWORK_ATTEMPT"
        for error in attempted_errors
    )


def test_validate_capture_events_reports_remote_constraints_and_transient_payload_errors(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, capture_events, _ = load_fixture_records(run_dir)
    capture_event = dict(capture_events[0])
    capture_event["adapter_type"] = "remote_url_manifest"
    capture_event["status"] = "completed"
    capture_event["normalized_url"] = "https://example.com/source.pdf"
    capture_event["final_url"] = "https://example.com/source.pdf"
    capture_event["request_method"] = "GET"
    capture_event["user_agent"] = "pytest"
    capture_event["http_status_code"] = 200
    capture_event["network_access_attempted"] = False
    capture_event["content_hash"] = None
    capture_event["transient_payload_path"] = "../escape.bin"

    errors: list[dict[str, object]] = []
    record_count, capture_ids, capture_records = validator.validate_capture_events(
        [capture_event],
        expected_run_id=str(execution_record["run_id"]),
        expected_input_handoff_hash=str(execution_record["input_handoff_hash"]),
        artifact_root=run_dir,
        errors=errors,
    )

    assert record_count == 1
    assert capture_ids == {str(capture_event["capture_id"])}
    codes = {error["code"] for error in errors}
    assert "REMOTE_CAPTURE_NOT_ATTEMPTED" in codes
    assert "REMOTE_CAPTURE_HASH_REQUIRED" in codes
    assert "TRANSIENT_PAYLOAD_PATH_INVALID" in codes


def test_validate_extraction_records_reports_path_escape_mismatch(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, capture_events, extraction_records = load_fixture_records(run_dir)
    capture_record = dict(capture_events[0])
    extraction_record = dict(extraction_records[0])
    capture_ids = {str(capture_record["capture_id"])}
    capture_records = {str(capture_record["capture_id"]): capture_record}

    extraction_record["status"] = "completed"
    extraction_record["input_hash"] = "0" * 64
    extraction_record["byte_count_in"] = 0
    extraction_record["content_hash"] = "1" * 64
    extraction_record["byte_count_out"] = 0
    extraction_record["extracted_text_path"] = "../escape.txt"

    errors: list[dict[str, object]] = []
    record_count = validator.validate_extraction_records(
        [extraction_record],
        expected_run_id=str(execution_record["run_id"]),
        capture_ids=capture_ids,
        capture_records=capture_records,
        artifact_root=run_dir,
        errors=errors,
    )

    assert record_count == 1
    codes = {error["code"] for error in errors}
    assert "EXTRACTED_TEXT_PATH_INVALID" in codes


def test_validate_extraction_records_reports_hash_and_size_mismatches(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, capture_events, extraction_records = load_fixture_records(run_dir)
    capture_record = dict(capture_events[0])
    extraction_record = dict(extraction_records[0])
    capture_ids = {str(capture_record["capture_id"])}
    capture_records = {str(capture_record["capture_id"]): capture_record}

    extracted_text_path = run_dir / str(extraction_record["extracted_text_path"])
    extracted_text_path.write_text("mismatched body", encoding="utf-8")
    extraction_record["status"] = "completed"
    extraction_record["input_hash"] = "0" * 64
    extraction_record["byte_count_in"] = 0
    extraction_record["content_hash"] = "1" * 64
    extraction_record["byte_count_out"] = 0

    errors: list[dict[str, object]] = []
    record_count = validator.validate_extraction_records(
        [extraction_record],
        expected_run_id=str(execution_record["run_id"]),
        capture_ids=capture_ids,
        capture_records=capture_records,
        artifact_root=run_dir,
        errors=errors,
    )

    assert record_count == 1
    codes = {error["code"] for error in errors}
    assert "EXTRACTION_INPUT_HASH_MISMATCH" in codes
    assert "EXTRACTION_BYTE_COUNT_MISMATCH" in codes
    assert "EXTRACTED_TEXT_HASH_MISMATCH" in codes
    assert "EXTRACTED_TEXT_BYTE_COUNT_MISMATCH" in codes


def test_validate_denial_record_reports_considered_urls_and_handoff_mismatch(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, _, _ = load_fixture_records(run_dir)
    denial_record = dict(execution_record)
    denial_record["considered_urls"] = ["", "https://example.com"]
    denial_record["input_handoff_hash"] = "0" * 64

    errors: list[dict[str, object]] = []
    validator.validate_denial_record(
        denial_record,
        expected_run_id=str(execution_record["run_id"]),
        expected_input_handoff_hash="1" * 64,
        errors=errors,
    )

    codes = {error["code"] for error in errors}
    assert "INVALID_CONSIDERED_URLS" in codes
    assert "DENIAL_HANDOFF_HASH_MISMATCH" in codes


def test_validate_network_safety_report_reports_summary_mismatches(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, _, _ = load_fixture_records(run_dir)
    execution_record = dict(execution_record)
    execution_record["network_safety_gate"] = {
        "schema_version": "network-safety-gate.v1",
        "decision": "blocked",
        "execution_allowed": False,
        "error_count": 2,
        "warning_count": 1,
    }
    report = {
        "schema_version": "network-safety-gate.v0",
        "decision": "allowed",
        "execution_allowed": True,
        "counts": {"errors": 0, "warnings": 0},
    }

    errors: list[dict[str, object]] = []
    validator.validate_network_safety_report(
        report,
        execution_record=execution_record,
        errors=errors,
    )

    codes = {error["code"] for error in errors}
    assert "NETWORK_SAFETY_SCHEMA_MISMATCH" in codes
    assert "NETWORK_SAFETY_DECISION_MISMATCH" in codes
    assert "NETWORK_SAFETY_ALLOWANCE_MISMATCH" in codes
    assert "NETWORK_SAFETY_ERROR_COUNT_MISMATCH" in codes
    assert "NETWORK_SAFETY_WARNING_COUNT_MISMATCH" in codes
