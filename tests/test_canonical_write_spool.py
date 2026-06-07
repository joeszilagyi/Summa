from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.source_db_tools import canonical_store, canonical_write_spool
from tools.scripts import ingest_gather_candidate_batch as ingest_batch_script
from tools.scripts import replay_canonical_write_spool as replay_script

REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_BATCH = (
    REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
)
EXECUTION_RUN = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"
INGEST_BATCH = REPO_ROOT / "tools" / "scripts" / "ingest_gather_candidate_batch.py"
INGEST_EXECUTION = REPO_ROOT / "tools" / "scripts" / "ingest_execution_artifacts.py"
APPLY_REVIEW = REPO_ROOT / "tools" / "scripts" / "apply_review_decision.py"
REPLAY = REPO_ROOT / "tools" / "scripts" / "replay_canonical_write_spool.py"
VALIDATE = REPO_ROOT / "tools" / "scripts" / "validate_canonical_write_spool.py"
FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.canonical_write_spool",
    )
    return db_path


def run_script(script: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def first_spool_record(spool_dir: Path) -> tuple[Path, dict[str, object]]:
    records = list(canonical_write_spool.iter_spool_records(spool_dir))
    assert len(records) == 1
    return records[0]


def test_spool_record_validation() -> None:
    batch_hash = canonical_write_spool.hash_file(CANDIDATE_BATCH)
    record = canonical_write_spool.build_spool_record(
        operation_kind="candidate_batch_ingest",
        operation_input={
            "artifact_refs": [
                {
                    "artifact_type": "gather_candidate_batch",
                    "artifact_path": str(CANDIDATE_BATCH),
                    "artifact_hash": batch_hash,
                }
            ]
        },
        replay_recipe={"batch_path": str(CANDIDATE_BATCH), "batch_hash": batch_hash},
        failure="database is locked",
        canonical_db_path=Path("canonical.sqlite"),
        spool_dir=Path("runs/spool"),
        originating_tool="pytest",
        created_at=FIXED_TIMESTAMP,
    )

    canonical_write_spool.validate_spool_record(record)
    missing_kind = dict(record)
    missing_kind.pop("operation_kind")
    with pytest.raises(canonical_write_spool.CanonicalWriteSpoolError, match="operation_kind"):
        canonical_write_spool.validate_spool_record(missing_kind)
    missing_hash = json.loads(json.dumps(record))
    missing_hash["operation_input"]["artifact_refs"][0].pop("artifact_hash")
    missing_hash["spool_record_checksum"] = canonical_write_spool.record_checksum(missing_hash)
    with pytest.raises(canonical_write_spool.CanonicalWriteSpoolError, match="artifact_hash"):
        canonical_write_spool.validate_spool_record(missing_hash)
    invalid_status = dict(record)
    invalid_status["replay_status"] = "unknown"
    invalid_status["spool_record_checksum"] = canonical_write_spool.record_checksum(invalid_status)
    with pytest.raises(
        canonical_write_spool.CanonicalWriteSpoolError, match="invalid replay status"
    ):
        canonical_write_spool.validate_spool_record(invalid_status)


def test_candidate_batch_ingest_spools_on_db_unavailable(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    missing_db = tmp_path / "missing.sqlite"

    proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(missing_db),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "spooled"
    _path, record = first_spool_record(spool_dir)
    assert record["operation_kind"] == "candidate_batch_ingest"
    assert record["replay_status"] == "pending"
    assert record["operation_input"]["artifact_refs"][0]["artifact_hash"]


def test_execution_artifact_ingest_spools_on_db_unavailable(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    run_dir = tmp_path / "execution_run"
    shutil.copytree(EXECUTION_RUN, run_dir)

    proc = run_script(
        INGEST_EXECUTION,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--run-dir",
            str(run_dir),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "spooled"
    _path, record = first_spool_record(spool_dir)
    assert record["operation_kind"] == "execution_artifact_ingest"
    assert len(record["operation_input"]["artifact_refs"]) == 3


def test_review_decision_apply_spools_on_db_unavailable(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    proc = run_script(
        APPLY_REVIEW,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--target",
            "source_claim:1",
            "--decision",
            "reject_claim",
            "--reviewer",
            "pytest",
            "--reason",
            "spool fixture",
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "spooled"
    _path, record = first_spool_record(spool_dir)
    assert record["operation_kind"] == "review_decision_apply"
    assert record["replay_recipe"]["decision"] == "reject_claim"


def test_invalid_candidate_batch_does_not_create_replayable_spool(tmp_path: Path) -> None:
    invalid_batch = tmp_path / "invalid.json"
    payload = json.loads(CANDIDATE_BATCH.read_text(encoding="utf-8"))
    payload["schema_version"] = "invalid"
    invalid_batch.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    spool_dir = tmp_path / "spool"

    proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--batch",
            str(invalid_batch),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )

    assert proc.returncode == 1
    assert "validation failed" in proc.stderr
    assert not spool_dir.exists()


def test_ingest_validation_failure_does_not_spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = bootstrap_db(tmp_path)
    spool_dir = tmp_path / "spool"
    args = SimpleNamespace(
        db=str(db_path),
        batch=str(CANDIDATE_BATCH),
        dry_run=False,
        no_strict=False,
        degraded_spool=True,
        spool_dir=str(spool_dir),
        format="json",
    )

    def fail_ingest(*_args: object, **_kwargs: object) -> None:
        raise ingest_batch_script.canonical_ingest.CanonicalIngestError(
            "validation failed: synthetic ingest check"
        )

    monkeypatch.setattr(ingest_batch_script, "parse_args", lambda: args)
    monkeypatch.setattr(ingest_batch_script.canonical_ingest, "ingest_candidate_batch", fail_ingest)

    exit_code = ingest_batch_script.main()

    assert exit_code == 1
    assert not spool_dir.exists()


def test_replay_candidate_batch_and_idempotence(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    missing_db = tmp_path / "missing.sqlite"
    spool_proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(missing_db),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )
    assert spool_proc.returncode == 0, spool_proc.stdout + spool_proc.stderr
    db_path = bootstrap_db(tmp_path)

    replay_proc = run_script(
        REPLAY,
        ["--db", str(db_path), "--spool-path", str(spool_dir), "--started-at", FIXED_TIMESTAMP],
    )

    assert replay_proc.returncode == 0, replay_proc.stdout + replay_proc.stderr
    report = json.loads(replay_proc.stdout)
    assert report["records_replayed"] == 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] >= 1
    finally:
        conn.close()
    record_path, record = first_spool_record(spool_dir)
    assert record["replay_status"] == "replayed"

    second = run_script(
        REPLAY,
        ["--db", str(db_path), "--spool-path", str(record_path), "--started-at", FIXED_TIMESTAMP],
    )
    assert second.returncode == 0, second.stdout + second.stderr
    second_report = json.loads(second.stdout)
    assert second_report["records_skipped"] == 1


def test_replay_reports_sqlite_connect_failure_as_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    args = type(
        "Args",
        (),
        {
            "db": str(db_path),
            "spool_path": str(spool_dir),
            "output": None,
            "dry_run": False,
            "strict": False,
            "limit": None,
            "format": "json",
            "replay_run_id": "pytest-replay",
            "started_at": FIXED_TIMESTAMP,
        },
    )()

    def fail_connect(_db_path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(canonical_store, "connect_canonical_store", fail_connect)

    report, exit_code = replay_script.replay(args)

    assert exit_code == 1
    assert report["status"] == "failed"
    assert any("unable to open database file" in warning for warning in report["warnings"])
    assert report["ended_at"] is not None


def test_replay_dry_run_does_not_mutate_db_or_spool(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    spool_proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )
    assert spool_proc.returncode == 0, spool_proc.stdout + spool_proc.stderr
    record_path, _record = first_spool_record(spool_dir)
    before = record_path.read_text(encoding="utf-8")
    db_path = bootstrap_db(tmp_path)

    replay_proc = run_script(
        REPLAY,
        [
            "--db",
            str(db_path),
            "--spool-path",
            str(record_path),
            "--dry-run",
            "--started-at",
            FIXED_TIMESTAMP,
        ],
    )

    assert replay_proc.returncode == 0, replay_proc.stdout + replay_proc.stderr
    assert record_path.read_text(encoding="utf-8") == before
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 0
    finally:
        conn.close()


def test_validate_spool_ignores_unrelated_files_but_rejects_partial_json(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    nested = spool_dir / "canonical-unavailable" / "unknown-run"
    nested.mkdir(parents=True, exist_ok=True)
    unrelated = nested / "notes.txt"
    unrelated.write_text("ignore me\n", encoding="utf-8")

    batch_hash = canonical_write_spool.hash_file(CANDIDATE_BATCH)
    record = canonical_write_spool.build_spool_record(
        operation_kind="candidate_batch_ingest",
        operation_input={
            "artifact_refs": [
                {
                    "artifact_type": "gather_candidate_batch",
                    "artifact_path": str(CANDIDATE_BATCH),
                    "artifact_hash": batch_hash,
                }
            ]
        },
        replay_recipe={"batch_path": str(CANDIDATE_BATCH), "batch_hash": batch_hash},
        failure="database is locked",
        canonical_db_path=Path("canonical.sqlite"),
        spool_dir=spool_dir,
        originating_tool="pytest",
        created_at=FIXED_TIMESTAMP,
    )
    valid_path = canonical_write_spool.write_spool_record(spool_dir, record)
    partial_path = nested / "partial.json"
    partial_path.write_text('{"schema_version": "canonical-write-spool-record.v1"', encoding="utf-8")

    validate_proc = run_script(VALIDATE, ["--spool-path", str(spool_dir)])
    assert validate_proc.returncode == 1
    assert "unreadable" in validate_proc.stdout or "JSONDecodeError" in validate_proc.stdout

    partial_path.unlink()
    validate_again = run_script(VALIDATE, ["--spool-path", str(spool_dir)])
    assert validate_again.returncode == 0, validate_again.stdout + validate_again.stderr
    report = json.loads(validate_again.stdout)
    assert report["record_count"] == 1
    assert unrelated.exists()


def test_replay_schema_mismatch_fails_clearly(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    batch_hash = canonical_write_spool.hash_file(CANDIDATE_BATCH)
    record = canonical_write_spool.build_spool_record(
        operation_kind="candidate_batch_ingest",
        operation_input={
            "artifact_refs": [
                {
                    "artifact_type": "gather_candidate_batch",
                    "artifact_path": str(CANDIDATE_BATCH),
                    "artifact_hash": batch_hash,
                }
            ]
        },
        replay_recipe={"batch_path": str(CANDIDATE_BATCH), "batch_hash": batch_hash},
        failure="schema mismatch fixture",
        canonical_db_path=tmp_path / "canonical.sqlite",
        spool_dir=spool_dir,
        originating_tool="pytest",
        expected_schema_version=999,
        created_at=FIXED_TIMESTAMP,
    )
    canonical_write_spool.write_spool_record(spool_dir, record)
    db_path = bootstrap_db(tmp_path)

    replay_proc = run_script(
        REPLAY,
        ["--db", str(db_path), "--spool-path", str(spool_dir), "--started-at", FIXED_TIMESTAMP],
    )

    assert replay_proc.returncode == 1
    report = json.loads(replay_proc.stdout)
    assert report["records_failed"] == 1
    assert "schema_version" in report["results"][0]["error"]


def test_spool_validator_cli_help_and_validation(tmp_path: Path) -> None:
    help_proc = run_script(VALIDATE, ["--help"])
    assert help_proc.returncode == 0
    spool_dir = tmp_path / "spool"
    spool_proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )
    assert spool_proc.returncode == 0, spool_proc.stdout + spool_proc.stderr

    validate_proc = run_script(VALIDATE, ["--spool-path", str(spool_dir)])

    assert validate_proc.returncode == 0, validate_proc.stdout + validate_proc.stderr
    report = json.loads(validate_proc.stdout)
    assert report["valid"] is True
    assert report["record_count"] == 1


def test_spool_record_does_not_embed_raw_payload_text(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    record_path, record = first_spool_record(spool_dir)
    text = record_path.read_text(encoding="utf-8")
    batch = json.loads(CANDIDATE_BATCH.read_text(encoding="utf-8"))
    candidate_text = batch["candidates"][0]["text"]
    assert candidate_text not in text
    assert record["raw_payload_policy"] == "artifact_references_only"


def test_moved_spool_record_remains_loadable(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    record_path, _record = first_spool_record(spool_dir)
    moved_path = tmp_path / "moved.json"
    record_path.replace(moved_path)

    loaded = canonical_write_spool.load_spool_record(moved_path)
    assert loaded["spool_path"] == str(record_path)


def test_replay_main_writes_report_with_atomic_json_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool_dir = tmp_path / "spool"
    spool_proc = run_script(
        INGEST_BATCH,
        [
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--batch",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ],
    )
    assert spool_proc.returncode == 0, spool_proc.stdout + spool_proc.stderr
    record_path, _ = first_spool_record(spool_dir)
    db_path = bootstrap_db(tmp_path)
    output = tmp_path / "replay-report.json"
    writes: list[Path] = []
    original_write_text = replay_script.Path.write_text

    def fake_atomic_write(path: Path, payload: object) -> None:
        writes.append(path)
        original_write_text(
            output,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def reject_direct_write(_self: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("direct write_text should not be used")

    monkeypatch.setattr(replay_script, "atomic_write_json", fake_atomic_write)
    monkeypatch.setattr(replay_script.Path, "write_text", reject_direct_write)

    exit_code = replay_script.main(
        [
            "--db",
            str(db_path),
            "--spool-path",
            str(record_path),
            "--output",
            str(output),
            "--started-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert exit_code == 0
    assert writes == [output.resolve()]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["records_replayed"] == 1
