from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.source_db_tools import canonical_ingest, canonical_reconciliation, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"
STRUCTURED_HOSTILE_ADAPTER = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "source_adapter_runtime"
    / "hostile_replay"
    / "structured_data"
    / "source_adapter.json"
)
PLAN_STRUCTURED_DATA = REPO_ROOT / "tools" / "scripts" / "plan_structured_data_source_adapter.py"
EXECUTE_SOURCE_ADAPTER = REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"
FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.execution_ingest",
    )
    return db_path


def copy_execution_fixture(tmp_path: Path) -> Path:
    run_dir = tmp_path / "execution_run"
    shutil.copytree(EXECUTION_FIXTURE_DIR, run_dir)
    return run_dir


def build_hostile_execution_run(tmp_path: Path) -> Path:
    handoff_path = tmp_path / "handoff.jsonl"
    run_dir = tmp_path / "hostile_run"
    plan_proc = subprocess.run(
        [
            sys.executable,
            str(PLAN_STRUCTURED_DATA),
            "--adapter",
            str(STRUCTURED_HOSTILE_ADAPTER),
            "--handoff-jsonl",
            str(handoff_path),
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert handoff_path.is_file(), plan_proc.stdout + plan_proc.stderr
    exec_proc = subprocess.run(
        [
            sys.executable,
            str(EXECUTE_SOURCE_ADAPTER),
            "--handoff",
            str(handoff_path),
            "--workspace-root",
            str(tmp_path),
            "--adapter",
            str(STRUCTURED_HOSTILE_ADAPTER),
            "--output",
            str(run_dir),
            "--run-id",
            "hostile-exec",
            "--created-at",
            FIXED_TIMESTAMP,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run_dir.is_dir(), exec_proc.stdout + exec_proc.stderr
    execution_record_path = run_dir / "execution-record.json"
    capture_events_path = run_dir / "capture-events.jsonl"
    extraction_records_path = run_dir / "extraction-records.jsonl"
    assert execution_record_path.is_file(), exec_proc.stdout + exec_proc.stderr
    assert capture_events_path.is_file(), exec_proc.stdout + exec_proc.stderr
    assert extraction_records_path.is_file(), exec_proc.stdout + exec_proc.stderr

    execution_record = json.loads(execution_record_path.read_text(encoding="utf-8"))
    assert execution_record["status"] in {"completed", "failed"}
    return run_dir


def test_execution_artifact_ingest_writes_capture_and_extraction_rows(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                paths=paths,
                input_hashes=input_hashes,
                capture_events=None,
                extraction_records=None,
                db_path=db_path,
            )
        counts = canonical_store.canonical_family_counts(conn)
        capture_row = conn.execute(
            "SELECT capture_event_id, provenance_event_ref FROM capture_event"
        ).fetchone()
        extraction_row = conn.execute(
            "SELECT capture_event_id, provenance_event_ref FROM extraction_record"
        ).fetchone()
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert counts["provenance_event"] == 1
    assert counts["capture_event"] == 1
    assert counts["extraction_record"] == 1
    assert extraction_row["capture_event_id"] == capture_row["capture_event_id"]
    assert capture_row["provenance_event_ref"] == report["provenance_event"]["event_key"]
    assert extraction_row["provenance_event_ref"] == report["provenance_event"]["event_key"]


def test_execution_artifact_ingest_passes_empty_reconciliation_work_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    captured: dict[str, object] = {}

    def fake_run_reconciliation_pass_for_ingest(
        conn: sqlite3.Connection, **kwargs: object
    ) -> dict[str, int]:
        assert conn is not None
        captured.update(kwargs)
        return {
            "work_deduped": 0,
            "authority_reconciled": 0,
            "authority_merged": 0,
            "claims_contradicted": 0,
            "relationships_contradicted": 0,
            "relational_constraints_checked": 0,
            "relational_constraints_skipped": 0,
        }

    monkeypatch.setattr(
        canonical_reconciliation,
        "run_reconciliation_pass_for_ingest",
        fake_run_reconciliation_pass_for_ingest,
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                paths=paths,
                input_hashes=input_hashes,
                capture_events=None,
                extraction_records=None,
                db_path=db_path,
            )
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert captured["provenance_event_ref"] == report["provenance_event"]["event_key"]
    assert captured["source_run_id"] == str(execution_record.get("run_id") or "")
    assert captured["claim_work_items"] == []
    assert captured["relationship_work_items"] == []


def test_execution_artifact_validation_happens_before_write(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record_path = run_dir / "execution-record.json"
    payload = json.loads(execution_record_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "invalid-execution-schema"
    execution_record_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(canonical_ingest.CanonicalIngestError, match="validation failed"):
        canonical_ingest.load_validated_execution_artifacts(run_dir)

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        assert canonical_store.canonical_family_counts(conn) == {
            "provenance_event": 0,
            "work": 0,
            "source_access": 0,
            "source_claim": 0,
            "capture_event": 0,
            "extraction_record": 0,
            "extraction_detected_entity": 0,
            "source_relationship": 0,
        }
    finally:
        conn.close()


def test_load_validated_execution_artifacts_uses_single_execution_receipt_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_receipt = SimpleNamespace(
        execution_record={"run_id": "receipt-run"},
        paths={
            "run_dir": tmp_path / "execution_run",
            "execution_record": tmp_path / "execution_run" / "execution-record.json",
            "capture_events": tmp_path / "execution_run" / "capture-events.jsonl",
            "extraction_records": tmp_path / "execution_run" / "extraction-records.jsonl",
        },
        input_hashes={
            "execution_record": "record-hash",
            "capture_events": "capture-hash",
            "extraction_records": "extraction-hash",
        },
    )
    load_calls = {"count": 0}
    validate_calls = {"count": 0}

    def fake_load_execution_artifacts(target: Path):
        load_calls["count"] += 1
        assert target == tmp_path / "execution_run"
        return fake_receipt

    def fake_validate_execution_artifact_receipt(receipt: object):
        validate_calls["count"] += 1
        assert receipt is fake_receipt
        return (
            {
                "counts": {"inspected": 1, "accepted": 1, "rejected": 0, "deferred": 0},
                "errors": [],
                "warnings": [],
            },
            canonical_ingest.EXIT_EXECUTION_PASS,
        )

    monkeypatch.setattr(canonical_ingest, "load_execution_artifacts", fake_load_execution_artifacts)
    monkeypatch.setattr(
        canonical_ingest,
        "validate_execution_artifact_receipt",
        fake_validate_execution_artifact_receipt,
    )

    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        tmp_path / "execution_run"
    )

    assert load_calls["count"] == 1
    assert validate_calls["count"] == 1
    assert execution_record == fake_receipt.execution_record
    assert paths == fake_receipt.paths
    assert input_hashes == fake_receipt.input_hashes


def test_execution_artifact_ingest_is_idempotent(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            first = canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                paths=paths,
                input_hashes=input_hashes,
                capture_events=None,
                extraction_records=None,
                db_path=db_path,
            )
        counts_after_first = canonical_store.canonical_family_counts(conn)
        with conn:
            second = canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                paths=paths,
                input_hashes=input_hashes,
                capture_events=None,
                extraction_records=None,
                db_path=db_path,
            )
        counts_after_second = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert counts_after_first == counts_after_second
    assert first["counts"]["inserted"]["capture_event"] == 1
    assert second["counts"]["updated"]["capture_event"] == 1
    assert second["counts"]["updated"]["extraction_record"] == 1


def test_execution_artifact_missing_capture_reference_rolls_back_in_strict_mode(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records[0]["capture_id"] = "capture-9999"
    (run_dir / "extraction-records.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in extraction_records
        ),
        encoding="utf-8",
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(canonical_ingest.CanonicalIngestError, match="unknown capture_id"), conn:
            canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                paths=paths,
                input_hashes=input_hashes,
                capture_events=None,
                extraction_records=None,
                db_path=db_path,
            )
        counts = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert all(count == 0 for count in counts.values())


def test_execution_artifact_dry_run_reports_intended_writes_without_mutation(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        report = canonical_ingest.ingest_execution_artifacts(
            conn,
            execution_record,
            paths=paths,
            input_hashes=input_hashes,
            capture_events=None,
            extraction_records=None,
            dry_run=True,
            db_path=db_path,
        )
        counts = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert report["status"] == "dry_run"
    assert report["counts"]["intended"]["capture_event"] == 1
    assert report["counts"]["intended"]["extraction_record"] == 1
    assert all(count == 0 for count in counts.values())


def test_execution_artifact_dry_run_reuses_capture_ids_for_all_extractions(
    tmp_path: Path,
) -> None:
    class CountingCaptureEvents(list[dict[str, object]]):
        def __init__(self, values: list[dict[str, object]]) -> None:
            super().__init__(values)
            self.iteration_count = 0

        def __iter__(self):  # type: ignore[override]
            self.iteration_count += 1
            return super().__iter__()

    db_path = bootstrap_db(tmp_path)
    run_dir = copy_execution_fixture(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    capture_events = CountingCaptureEvents(
        [
            json.loads(line)
            for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    )
    extraction_records = [
        json.loads(line)
        for line in (run_dir / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    extraction_records = extraction_records + extraction_records
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        report = canonical_ingest.ingest_execution_artifacts(
            conn,
            execution_record,
            paths=paths,
            input_hashes=input_hashes,
            capture_events=capture_events,
            extraction_records=extraction_records,
            dry_run=True,
            db_path=db_path,
        )
        counts = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert report["status"] == "dry_run"
    assert report["counts"]["intended"]["capture_event"] == 1
    assert report["counts"]["intended"]["extraction_record"] == 2
    assert capture_events.iteration_count == 1
    assert all(count == 0 for count in counts.values())


def test_execution_artifact_hostile_fixture_preserves_status_and_flags(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    run_dir = build_hostile_execution_run(tmp_path)
    execution_record, paths, input_hashes = canonical_ingest.load_validated_execution_artifacts(
        run_dir
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                paths=paths,
                input_hashes=input_hashes,
                capture_events=None,
                extraction_records=None,
                db_path=db_path,
            )
        rows = conn.execute(
            "SELECT extraction_status, hostile_replay_flags_json, bad_utf8_handling FROM extraction_record ORDER BY extraction_id"
        ).fetchall()
    finally:
        conn.close()

    statuses = {row["extraction_status"] for row in rows}
    assert "completed" in statuses or "failed" in statuses or "skipped" in statuses
    assert any(row["hostile_replay_flags_json"] for row in rows)
    assert any(
        row["bad_utf8_handling"] is not None or row["extraction_status"] in {"failed", "skipped"}
        for row in rows
    )
