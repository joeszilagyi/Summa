from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.validators import validate_source_acquisition_execution as validator

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_acquisition_execution.py"


def copy_execution_fixture(tmp_path: Path) -> Path:
    run_dir = tmp_path / "execution_run"
    shutil.copytree(EXECUTION_FIXTURE_DIR, run_dir)
    return run_dir


def write_json_lines(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records), encoding="utf-8")


def run_validator(run_dir: Path, *, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(run_dir / "execution-record.json"),
            "--report-root",
            str(tmp_path),
            "--report-json",
            str(tmp_path / "actual" / "report.json"),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    report = json.loads((tmp_path / "actual" / "report.json").read_text(encoding="utf-8"))
    return proc, report


def test_execution_artifact_loader_streams_jsonl_and_hashes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    capture_events_path = run_dir / "capture-events.jsonl"
    extraction_records_path = run_dir / "extraction-records.jsonl"
    original_read_text = validator.Path.read_text

    def read_text_side_effect(self: Path, *args: object, **kwargs: object) -> str:
        if self in {capture_events_path, extraction_records_path}:
            raise AssertionError("JSONL inputs should be streamed with Path.open(), not read_text()")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(validator.Path, "read_text", read_text_side_effect)

    receipt = validator.load_execution_artifacts(run_dir)

    assert receipt.input_hashes["capture_events"] == hashlib.sha256(
        capture_events_path.read_bytes()
    ).hexdigest()
    assert receipt.input_hashes["extraction_records"] == hashlib.sha256(
        extraction_records_path.read_bytes()
    ).hexdigest()


def test_execution_validation_rejects_capture_handoff_hash_mismatch(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    execution_record["capture_event_count"] = 1
    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_events[0]["handoff_hash"] = "0" * 64

    (run_dir / "execution-record.json").write_text(
        json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_json_lines(run_dir / "capture-events.jsonl", capture_events)

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(
        error["code"] == "CAPTURE_HANDOFF_HASH_MISMATCH" for error in report.get("errors", [])
    )


def test_execution_validation_rejects_duplicate_capture_ids(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_events.append(dict(capture_events[0]))
    execution_record["capture_event_count"] = 2

    (run_dir / "execution-record.json").write_text(
        json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_json_lines(run_dir / "capture-events.jsonl", capture_events)

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "DUPLICATE_CAPTURE_ID" for error in report.get("errors", []))


def test_execution_validation_rejects_duplicate_extraction_ids(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    execution_record["extraction_record_count"] = 2
    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records.append(dict(extraction_records[0]))

    (run_dir / "execution-record.json").write_text(
        json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_json_lines(run_dir / "extraction-records.jsonl", extraction_records)

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "DUPLICATE_EXTRACTION_ID" for error in report.get("errors", []))


def test_execution_validation_rejects_extraction_hash_and_count_mismatches(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records[0]["input_hash"] = "0" * 64
    extraction_records[0]["byte_count_in"] = 999

    write_json_lines(run_dir / "extraction-records.jsonl", extraction_records)

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    codes = {error["code"] for error in report.get("errors", [])}
    assert "EXTRACTION_INPUT_HASH_MISMATCH" in codes
    assert "EXTRACTION_BYTE_COUNT_MISMATCH" in codes


def test_execution_validation_rejects_invalid_manifest_json(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    (run_dir / "manifest.json").write_text(
        '{"schema_version":"source-acquisition-run-manifest.v1","run_id":"fixture-exec","run_id":"duplicate"}',
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "DUPLICATE_JSON_KEY" for error in report.get("errors", []))


def test_execution_validation_rejects_missing_transient_payload_artifact(
    tmp_path: Path,
) -> None:
    run_dir = copy_execution_fixture(tmp_path)
    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_events[0]["transient_payload_path"] = "payloads/capture-0001.bin"
    write_json_lines(run_dir / "capture-events.jsonl", capture_events)

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(
        error["code"] == "TRANSIENT_PAYLOAD_ARTIFACT_MISSING" for error in report.get("errors", [])
    )


def test_execution_validation_rejects_invalid_execution_status(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    execution_record["status"] = "banana"
    (run_dir / "execution-record.json").write_text(
        json.dumps(execution_record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "INVALID_EXECUTION_STATUS" for error in report.get("errors", []))


def test_execution_validation_rejects_unknown_execution_fields(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    execution_record["unexpected_secret"] = "hidden"
    (run_dir / "execution-record.json").write_text(
        json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "UNKNOWN_EXECUTION_FIELD" for error in report.get("errors", []))


def test_execution_validation_rejects_invalid_capture_status(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_events[0]["status"] = "banana"
    run_dir.joinpath("capture-events.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in capture_events),
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "INVALID_CAPTURE_STATUS" for error in report.get("errors", []))


def test_execution_validation_rejects_unknown_capture_fields(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_events[0]["private_note"] = "hidden"
    run_dir.joinpath("capture-events.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in capture_events),
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "UNKNOWN_CAPTURE_FIELD" for error in report.get("errors", []))


def test_execution_validation_rejects_invalid_extraction_status(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records[0]["status"] = "banana"
    run_dir.joinpath("extraction-records.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in extraction_records),
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "INVALID_EXTRACTION_STATUS" for error in report.get("errors", []))


def test_execution_validation_rejects_unknown_extraction_fields(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records[0]["hidden"] = "secret"
    run_dir.joinpath("extraction-records.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in extraction_records),
        encoding="utf-8",
    )

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "UNKNOWN_EXTRACTION_FIELD" for error in report.get("errors", []))
