from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import validators

from tools.source_db_tools import canonical_ingest, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "audit_rebuildability.py"
WRAPPER = REPO_ROOT / "tools" / "scripts" / "Index_Audit_Rebuildability.sh"
SCHEMA = REPO_ROOT / "config" / "canonical-rebuildability-report.v1.schema.json"
CANDIDATE_BATCH = (
    REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
)
EXECUTION_RUN = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"
FIXED_TIMESTAMP = "2026-06-04T12:00:00Z"

spec = importlib.util.spec_from_file_location("audit_rebuildability_for_tests", SCRIPT)
assert spec is not None
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def run_audit(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def stage_runs_dir(tmp_path: Path) -> Path:
    runs_dir = tmp_path / "runs"
    gather_dir = runs_dir / "gather" / "run-001"
    execution_dir = runs_dir / "acquisition" / "exec-001"
    cycle_dir = runs_dir / "topic-cycle" / "cycle-001"
    gather_dir.mkdir(parents=True)
    execution_dir.parent.mkdir(parents=True)
    cycle_dir.mkdir(parents=True)
    shutil.copy2(CANDIDATE_BATCH, gather_dir / "gather-candidate-batch.json")
    shutil.copytree(EXECUTION_RUN, execution_dir)
    (cycle_dir / "topic-cycle-run.json").write_text(
        json.dumps(
            {
                "schema_version": "topic-cycle-run.v1",
                "run_id": "cycle-001",
                "status": "completed",
                "stages": [
                    {
                        "name": "ingest_candidate_batch",
                        "status": "passed",
                        "artifacts": {
                            "candidate_batch": "../../gather/run-001/gather-candidate-batch.json"
                        },
                    },
                    {
                        "name": "ingest_execution_artifacts",
                        "status": "passed",
                        "artifacts": {
                            "execution_record": "../../acquisition/exec-001/execution-record.json"
                        },
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return runs_dir


def bootstrap_db(path: Path) -> None:
    canonical_store.init_canonical_store(
        path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.rebuildability",
    )


def ingest_fixture_artifacts(db_path: Path, runs_dir: Path) -> None:
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        batch_path = runs_dir / "gather" / "run-001" / "gather-candidate-batch.json"
        batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=batch_path,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        execution_dir = runs_dir / "acquisition" / "exec-001"
        execution_record, captures, extractions, paths, hashes = (
            canonical_ingest.load_validated_execution_artifacts(execution_dir)
        )
        with conn:
            canonical_ingest.ingest_execution_artifacts(
                conn,
                execution_record,
                captures,
                extractions,
                paths=paths,
                input_hashes=hashes,
                db_path=db_path,
            )
    finally:
        conn.close()


def table_count(db_path: Path, table: str) -> int:
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
    finally:
        conn.close()


def test_cli_and_wrapper_help_exit_zero() -> None:
    for command in ([sys.executable, str(SCRIPT), "--help"], [str(WRAPPER), "--help"]):
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "usage:" in (proc.stdout + proc.stderr).lower()


def test_validation_only_discovers_and_validates_artifacts(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    report_path = tmp_path / "report.json"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = load_json(report_path)
    schema = load_json(SCHEMA)
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator_cls(schema).validate(report)
    assert report["final_status"] == "validation_only"
    types = {item["artifact_type"] for item in report["artifacts_discovered"]}
    assert {
        "gather_candidate_batch",
        "source_acquisition_execution",
        "topic_cycle_manifest",
    } <= types
    assert report["replay_plan"]["replayable_artifact_count"] == 2
    assert report["temp_rebuild_db"] is None


def test_rebuild_temp_replays_artifacts_and_runs_graph_closure(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    report_path = tmp_path / "report.json"
    rebuilt_db = tmp_path / "rebuilt.sqlite"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "rebuild_temp",
            "--temp-rebuild-db",
            str(rebuilt_db),
            "--keep-temp-db",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert report["final_status"] in {"rebuildable", "rebuildable_with_warnings"}
    assert rebuilt_db.is_file()
    assert table_count(rebuilt_db, "work") >= 1
    assert table_count(rebuilt_db, "capture_event") >= 1
    assert report["canonical_validation_result"]["status"] == "pass"
    assert report["graph_closure_result"]["status"] in {"pass", "pass_with_unresolved"}
    assert {item["status"] for item in report["replay_results"]} == {"replayed"}


def test_compare_existing_reports_matching_meaningful_state(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    existing_db = tmp_path / "existing.sqlite"
    rebuilt_db = tmp_path / "rebuilt.sqlite"
    bootstrap_db(existing_db)
    ingest_fixture_artifacts(existing_db, runs_dir)
    before = {
        "work": table_count(existing_db, "work"),
        "capture_event": table_count(existing_db, "capture_event"),
    }
    report_path = tmp_path / "report.json"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--canonical-db",
            str(existing_db),
            "--output",
            str(report_path),
            "--replay-mode",
            "compare_existing",
            "--temp-rebuild-db",
            str(rebuilt_db),
            "--keep-temp-db",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert report["final_status"] in {"rebuildable", "rebuildable_with_warnings"}
    assert report["row_count_comparison"]["status"] == "match"
    assert report["key_hash_comparison"]["status"] == "match"
    assert table_count(existing_db, "work") == before["work"]
    assert table_count(existing_db, "capture_event") == before["capture_event"]


def test_find_missing_artifacts_resolves_absolute_path_aliases(tmp_path: Path, monkeypatch) -> None:
    runs_dir = tmp_path / "runs"
    cycle_dir = runs_dir / "topic-cycle" / "cycle-001"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    actual = data_dir / "candidate-batch.json"
    actual.write_text("{\"schema_version\":\"demo\"}\n", encoding="utf-8")
    alias = data_dir / ".." / "data" / "candidate-batch.json"
    cycle_dir.mkdir(parents=True)
    (cycle_dir / "topic-cycle-run.json").write_text(
        json.dumps(
            {
                "schema_version": "topic-cycle-run.v1",
                "run_id": "cycle-001",
                "status": "completed",
                "stages": [
                    {
                        "name": "ingest_candidate_batch",
                        "status": "passed",
                        "artifacts": {"candidate_batch": str(alias)},
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    artifacts = [
        audit.Artifact(
            artifact_type="topic_cycle_manifest",
            path=cycle_dir / "topic-cycle-run.json",
            hash=None,
            schema_id="topic-cycle-run.v1",
            run_id="cycle-001",
            stage="cycle",
            validation_status="valid",
            replay_status="pending",
        )
    ]

    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if self == alias:
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    missing = audit.find_missing_artifacts(artifacts, runs_dir)

    assert missing == []


def test_missing_manifest_artifact_reports_not_rebuildable(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    (runs_dir / "gather" / "run-001" / "gather-candidate-batch.json").unlink()
    report_path = tmp_path / "report.json"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 1
    report = load_json(report_path)
    assert report["artifacts_missing"]
    assert report["final_status"] == "validation_only"


def test_invalid_artifact_reports_failure(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    batch_path = runs_dir / "gather" / "run-001" / "gather-candidate-batch.json"
    batch_path.write_text("{not json}\n", encoding="utf-8")
    report_path = tmp_path / "report.json"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 1
    report = load_json(report_path)
    invalid = [
        item for item in report["artifacts_discovered"] if item["validation_status"] == "invalid"
    ]
    assert invalid
    assert report["errors"]


def test_missing_replay_support_is_incomplete_support_not_false_success(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    review_dir = runs_dir / "review"
    review_dir.mkdir()
    (review_dir / "review-decision-apply-result.json").write_text(
        json.dumps(
            {
                "schema_version": "review-decision-apply-result.v1",
                "target": "source_claim:1",
                "decision_action": "reject_claim",
                "status": "completed",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "rebuild_temp",
            "--temp-rebuild-db",
            str(tmp_path / "rebuilt.sqlite"),
            "--keep-temp-db",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert report["final_status"] == "incomplete_support"
    assert report["missing_replay_support"]
    assert report["missing_replay_support"][0]["artifact_type"] == "review_decision_apply_result"


def test_refuses_to_overwrite_existing_temp_db_without_force(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    rebuild_db = tmp_path / "rebuilt.sqlite"
    rebuild_db.write_text("existing", encoding="utf-8")
    report_path = tmp_path / "report.json"

    proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "rebuild_temp",
            "--temp-rebuild-db",
            str(rebuild_db),
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 1
    assert "already exists" in proc.stderr


def test_deterministic_with_fixed_timestamp_and_kept_temp_db(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(first),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )
    second_proc = run_audit(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(second),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert first_proc.returncode == 0, first_proc.stdout + first_proc.stderr
    assert second_proc.returncode == 0, second_proc.stdout + second_proc.stderr
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
