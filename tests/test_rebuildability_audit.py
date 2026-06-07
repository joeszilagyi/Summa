from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

try:
    from jsonschema import validators
except ModuleNotFoundError:  # pragma: no cover - optional test dependency in this environment
    validators = None

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
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_direct_file_import_registers_modules_before_exec(tmp_path: Path) -> None:
    module_path = tmp_path / "dataclass_module.py"
    module_path.write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Payload:\n"
        "    value: int\n",
        encoding="utf-8",
    )

    spec = importlib.util.spec_from_file_location("dataclass_module_for_tests", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert sys.modules[spec.name] is module
    assert module.Payload(7).value == 7


def run_audit(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_audit_with_timeout(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def assert_rebuildability_report_schema(report: dict[str, Any]) -> None:
    if validators is None:
        assert report["schema_version"] == "canonical-rebuildability-report.v1"
        assert "final_status" in report
        assert "artifacts_discovered" in report
        assert "replay_plan" in report
        return
    schema = load_json(SCHEMA)
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator_cls(schema).validate(report)


def stage_runs_dir(tmp_path: Path) -> Path:
    runs_dir = tmp_path / "runs"
    gather_dir = runs_dir / "gather" / "run-001"
    execution_dir = runs_dir / "acquisition" / "exec-001"
    cycle_dir = runs_dir / "topic-cycle" / "cycle-001"
    gather_dir.mkdir(parents=True)
    execution_dir.parent.mkdir(parents=True)
    cycle_dir.mkdir(parents=True)
    shutil.copy2(CANDIDATE_BATCH, gather_dir / "gather-candidate-batch.json")
    shutil.copy2(CANDIDATE_BATCH.parent / "rendered-prompt.txt", gather_dir / "rendered-prompt.txt")
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


def stage_noisy_runs_dir(tmp_path: Path) -> Path:
    runs_dir = stage_runs_dir(tmp_path)
    noise_root = runs_dir / "noise"
    for branch_index in range(12):
        branch_dir = noise_root / f"branch-{branch_index:02d}" / "deep" / "layer"
        branch_dir.mkdir(parents=True, exist_ok=True)
        for file_index in range(150):
            (branch_dir / f"artifact-{branch_index:02d}-{file_index:04d}.txt").write_text(
                f"noise file {branch_index}:{file_index}\n",
                encoding="utf-8",
            )
    huge_text_dir = runs_dir / "extracted-text"
    huge_text_dir.mkdir(parents=True, exist_ok=True)
    (huge_text_dir / "bulk-transcript.txt").write_text(
        "lorem ipsum dolor sit amet\n" * 5000,
        encoding="utf-8",
    )
    return runs_dir


def rewrite_schema_version(path: Path, schema_version: str) -> None:
    payload = load_json(path)
    payload["schema_version"] = schema_version
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rewrite_jsonl_schema_version(path: Path, schema_version: str) -> None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for record in records:
        record["schema_version"] = schema_version
    path.write_text(
        "\n".join(json.dumps(record, indent=None, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def stage_legacy_runs_dir(tmp_path: Path) -> Path:
    runs_dir = stage_runs_dir(tmp_path)
    rewrite_schema_version(runs_dir / "gather" / "run-001" / "gather-candidate-batch.json", "gather-candidate-batch.v0")
    rewrite_schema_version(runs_dir / "acquisition" / "exec-001" / "execution-record.json", "source-acquisition-execution.v0")
    rewrite_jsonl_schema_version(runs_dir / "acquisition" / "exec-001" / "capture-events.jsonl", "source-capture-event.v0")
    rewrite_jsonl_schema_version(runs_dir / "acquisition" / "exec-001" / "extraction-records.jsonl", "source-extraction-record.v0")
    rewrite_schema_version(runs_dir / "topic-cycle" / "cycle-001" / "topic-cycle-run.json", "topic-cycle-run.v0")

    review_dir = runs_dir / "review"
    review_dir.mkdir(exist_ok=True)
    (review_dir / "review-decision-apply-result.json").write_text(
        json.dumps(
            {
                "schema_version": "review-decision-apply-result.v0",
                "target": "source_claim:1",
                "decision_action": "reject_claim",
                "status": "completed",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    publication_dir = runs_dir / "publication"
    publication_dir.mkdir(exist_ok=True)
    (publication_dir / "publication-artifacts-report.json").write_text(
        json.dumps(
            {
                "schema_version": "publication-artifacts-report.v0",
                "status": "pass",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    gate_dir = runs_dir / "network"
    gate_dir.mkdir(exist_ok=True)
    (gate_dir / "network-safety-gate-report.json").write_text(
        json.dumps(
            {
                "schema_version": "network-safety-gate-report.v0",
                "status": "allow",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    audit_dir = runs_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    (audit_dir / "canonical-rebuildability-report.json").write_text(
        json.dumps(
            {
                "schema_version": "canonical-rebuildability-report.v0",
                "final_status": "validation_only",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return runs_dir


def stage_hostile_runs_dir(tmp_path: Path) -> Path:
    runs_dir = stage_runs_dir(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_batch = outside_dir / "escaped-gather-candidate-batch.json"
    outside_batch.write_text(
        json.dumps(
            {
                "schema_version": "gather-candidate-batch.v1",
                "run_id": "escaped",
                "subject": {"subject_id": "escaped"},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    symlink_path = runs_dir / "gather" / "escaped" / "gather-candidate-batch.json"
    symlink_path.parent.mkdir(parents=True, exist_ok=True)
    symlink_path.symlink_to(outside_batch)

    malformed_review = runs_dir / "review" / "review-decision-apply-result.json"
    malformed_review.parent.mkdir(parents=True, exist_ok=True)
    malformed_review.write_text("{\"schema_version\": \"review-decision-apply-result.v1\",\n", encoding="utf-8")

    topic_cycle_dir = runs_dir / "topic-cycle" / "cycle-002"
    topic_cycle_dir.mkdir(parents=True, exist_ok=True)
    topic_cycle_dir.joinpath("topic-cycle-run.json").write_text(
        json.dumps(
            {
                "schema_version": "topic-cycle-run.v1",
                "run_id": "cycle-002",
                "status": "completed",
                "stages": [
                    {
                        "name": "missing_reference",
                        "status": "passed",
                        "artifacts": {"candidate_batch": "../../gather/run-999/gather-candidate-batch.json"},
                    }
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


def test_audit_main_uses_stable_json_text_for_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    report_path = tmp_path / "report.json"
    calls: list[dict[str, Any]] = []

    def fake_stable_json_text(payload: Any) -> str:
        calls.append(dict(payload))
        return "SAFE JSON\n"

    monkeypatch.setattr(audit, "stable_json_text", fake_stable_json_text)

    exit_code = audit.main(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert calls
    assert captured.out == "SAFE JSON\n"
    report = load_json(report_path)
    assert_rebuildability_report_schema(report)


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

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert_rebuildability_report_schema(report)
    assert report["final_status"] == "incomplete_support"
    types = {item["artifact_type"] for item in report["artifacts_discovered"]}
    assert {
        "gather_candidate_batch",
        "source_acquisition_execution",
        "topic_cycle_manifest",
    } <= types
    assert report["replay_plan"]["replayable_artifact_count"] == 2
    assert {item["artifact_type"] for item in report["missing_replay_support"]} == {
        "topic_cycle_manifest"
    }
    assert report["temp_rebuild_db"] is None


def test_validation_only_completes_on_large_noisy_run_tree_within_timeout(tmp_path: Path) -> None:
    runs_dir = stage_noisy_runs_dir(tmp_path)
    report_path = tmp_path / "report.json"

    proc = run_audit_with_timeout(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(report_path),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ],
        timeout=30,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert_rebuildability_report_schema(report)
    assert report["final_status"] == "incomplete_support"
    assert len(report["artifacts_discovered"]) == 3
    assert report["replay_plan"]["replayable_artifact_count"] == 2
    assert report["artifacts_missing"] == []


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
    assert report["final_status"] == "incomplete_support"
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
    assert report["final_status"] == "incomplete_support"
    assert report["row_count_comparison"]["status"] == "match"
    assert report["key_hash_comparison"]["status"] == "match"
    assert table_count(existing_db, "work") == before["work"]
    assert table_count(existing_db, "capture_event") == before["capture_event"]


def test_compare_existing_detects_mutated_row_content(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    existing_db = tmp_path / "existing.sqlite"
    rebuilt_db = tmp_path / "rebuilt.sqlite"
    bootstrap_db(existing_db)
    ingest_fixture_artifacts(existing_db, runs_dir)
    conn = canonical_store.connect_canonical_store(existing_db)
    try:
        with conn:
            conn.execute(
                "UPDATE work SET title = title || ' (mutated)' "
                "WHERE work_id = (SELECT work_id FROM work ORDER BY work_id LIMIT 1)"
            )
    finally:
        conn.close()
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
    assert report["final_status"] == "incomplete_support"
    assert report["row_count_comparison"]["differences"] == {}
    assert report["key_hash_comparison"]["status"] == "different"
    assert "work" in report["key_hash_comparison"]["differences"]


def test_db_summary_reuses_prevalidated_store_summary_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "rebuilt.sqlite"
    store_summary = {
        "schema_version": canonical_store.CURRENT_SCHEMA_VERSION,
        "current_migration_id": canonical_store.CURRENT_MIGRATION_ID,
        "table_counts": {"work": 2, "capture_event": 1},
    }

    def fail_check(_path: Path) -> object:
        raise AssertionError("db_summary should reuse the provided store summary")

    def fail_row_count_summary(_path: Path) -> dict[str, int]:
        raise AssertionError("row_count_summary should not be called when table_counts are provided")

    monkeypatch.setattr(audit.canonical_store, "check_canonical_store", fail_check)
    monkeypatch.setattr(audit, "row_count_summary", fail_row_count_summary)
    monkeypatch.setattr(audit, "table_content_hash_summary", lambda _path: {"work": "abc"})

    summary = audit.db_summary(db_path, store_summary=store_summary)

    assert summary["path"] == str(db_path)
    assert summary["schema_version"] == canonical_store.CURRENT_SCHEMA_VERSION
    assert summary["current_migration_id"] == canonical_store.CURRENT_MIGRATION_ID
    assert summary["row_counts"] == store_summary["table_counts"]
    assert summary["key_hashes"] == {"work": "abc"}


def test_find_missing_artifacts_resolves_absolute_path_aliases(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    cycle_dir = runs_dir / "topic-cycle" / "cycle-001"
    data_dir = runs_dir / "data"
    data_dir.mkdir(parents=True)
    actual = data_dir / "candidate-batch.json"
    actual.write_text("{\"schema_version\":\"demo\"}\n", encoding="utf-8")
    alias = runs_dir / "topic-cycle" / "cycle-001" / ".." / ".." / "data" / "candidate-batch.json"
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

    missing = audit.find_missing_artifacts(artifacts, runs_dir)

    assert missing == []


def test_find_missing_artifacts_rejects_absolute_refs_outside_runs_root(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    cycle_dir = runs_dir / "topic-cycle" / "cycle-001"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True)
    outside_ref = outside_dir / "escaped-candidate-batch.json"
    outside_ref.write_text("{\"schema_version\":\"demo\"}\n", encoding="utf-8")
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
                        "artifacts": {"candidate_batch": str(outside_ref)},
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

    missing = audit.find_missing_artifacts(artifacts, runs_dir)

    assert missing == [
        {
            "referenced_by": "topic-cycle/cycle-001/topic-cycle-run.json",
            "stage": "ingest_candidate_batch",
            "artifact_key": "candidate_batch",
            "missing_path": str(outside_ref.resolve()),
        }
    ]


def test_find_missing_artifacts_includes_non_json_references(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    cycle_dir = runs_dir / "topic-cycle" / "cycle-001"
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
                        "artifacts": {"transcript": "../../documents/transcript.txt"},
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

    missing = audit.find_missing_artifacts(artifacts, runs_dir)

    assert missing == [
        {
            "referenced_by": "topic-cycle/cycle-001/topic-cycle-run.json",
            "stage": "ingest_candidate_batch",
            "artifact_key": "transcript",
            "missing_path": str((runs_dir / "documents" / "transcript.txt").resolve()),
        }
    ]


def test_find_missing_artifacts_reuses_discovered_manifest_payload_without_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    artifacts = audit.discover_artifacts(runs_dir)

    def fail_read_json(_path: Path) -> dict[str, Any] | None:
        raise AssertionError("topic-cycle payload should be reused from discovery")

    monkeypatch.setattr(audit, "read_json", fail_read_json)

    missing = audit.find_missing_artifacts(artifacts, runs_dir)

    assert missing == []


def test_discover_artifacts_validates_replayable_artifacts_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    barrier = threading.Barrier(2)
    active = 0
    max_active = 0
    lock = threading.Lock()
    original_candidate = audit.validate_candidate_batch
    original_execution = audit.validate_execution_dir

    def wrap_candidate(path: Path) -> audit.Artifact:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=5)
            return original_candidate(path)
        finally:
            with lock:
                active -= 1

    def wrap_execution(path: Path) -> audit.Artifact:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=5)
            return original_execution(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(audit, "validate_candidate_batch", wrap_candidate)
    monkeypatch.setattr(audit, "validate_execution_dir", wrap_execution)

    artifacts = audit.discover_artifacts(runs_dir)

    assert max_active >= 2
    assert {
        artifact.artifact_type
        for artifact in artifacts
        if artifact.artifact_type in {"gather_candidate_batch", "source_acquisition_execution"}
    } == {"gather_candidate_batch", "source_acquisition_execution"}


def test_inventory_discovered_artifacts_streams_without_replay_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = stage_runs_dir(tmp_path)

    def fail_candidate_batch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inventory should not validate candidate batches")

    def fail_execution_dir(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inventory should not validate execution artifacts")

    monkeypatch.setattr(audit, "validate_candidate_batch", fail_candidate_batch)
    monkeypatch.setattr(audit, "validate_execution_dir", fail_execution_dir)

    candidates = audit._inventory_discovered_artifacts(runs_dir)

    assert {
        candidate.kind
        for candidate in candidates
        if candidate.artifact_type in {"gather_candidate_batch", "source_acquisition_execution"}
    } == {"gather_candidate_batch", "source_acquisition_execution"}


def test_replay_artifacts_reuses_discovery_validation_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    db_path = tmp_path / "rebuilt.sqlite"
    bootstrap_db(db_path)
    artifacts = audit.discover_artifacts(runs_dir)
    replayable = [
        artifact for artifact in artifacts if artifact.artifact_type in audit.REPLAYABLE_TYPES
    ]

    def fail_candidate_batch(*_args: object, **_kwargs: object) -> tuple[dict[str, Any], str]:
        raise AssertionError("candidate batch should reuse discovery inputs")

    def fail_execution_artifacts(
        *_args: object, **_kwargs: object
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Path], dict[str, str]]:
        raise AssertionError("execution artifacts should reuse discovery inputs")

    def fail_spool_record(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("spool records should reuse discovery inputs")

    monkeypatch.setattr(audit.canonical_ingest, "load_validated_candidate_batch", fail_candidate_batch)
    monkeypatch.setattr(
        audit.canonical_ingest,
        "load_validated_execution_artifacts",
        fail_execution_artifacts,
    )
    monkeypatch.setattr(audit.canonical_write_spool, "load_spool_record", fail_spool_record)

    results, errors = audit.replay_artifacts(db_path=db_path, artifacts=replayable, strict=True)

    assert errors == []
    assert {item["status"] for item in results} == {"replayed"}


def test_replay_artifacts_prepares_validations_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    db_path = tmp_path / "rebuilt.sqlite"
    bootstrap_db(db_path)
    artifacts = [
        replace(artifact, replay_inputs=None)
        for artifact in audit.discover_artifacts(runs_dir)
        if artifact.artifact_type in audit.REPLAYABLE_TYPES
    ]

    barrier = threading.Barrier(len(artifacts))
    active = 0
    max_active = 0
    lock = threading.Lock()

    def wrap_candidate(path: Path) -> tuple[dict[str, Any], str]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=5)
            return {"schema_version": "gather-candidate-batch.v1"}, "batch-hash"
        finally:
            with lock:
                active -= 1

    def wrap_execution(path: Path) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Path],
        dict[str, str],
    ]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            barrier.wait(timeout=5)
            execution_record = {"schema_version": "source-acquisition-execution.v1"}
            empty_paths = {
                "execution_record": path / "execution-record.json",
                "capture_events": path / "capture-events.jsonl",
                "extraction_records": path / "extraction-records.jsonl",
            }
            return execution_record, [], [], empty_paths, {
                "execution_record": "record-hash",
                "capture_events": "capture-hash",
                "extraction_records": "extraction-hash",
            }
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(audit.canonical_ingest, "load_validated_candidate_batch", wrap_candidate)
    monkeypatch.setattr(audit.canonical_ingest, "load_validated_execution_artifacts", wrap_execution)
    monkeypatch.setattr(
        audit,
        "replay_candidate_batch",
        lambda *_args, **_kwargs: {"status": "completed", "counts": {}},
    )
    monkeypatch.setattr(
        audit,
        "replay_execution_artifacts",
        lambda *_args, **_kwargs: {"status": "completed", "counts": {}},
    )

    results, errors = audit.replay_artifacts(db_path=db_path, artifacts=artifacts, strict=True)

    assert max_active >= 2
    assert errors == []
    assert {item["status"] for item in results} == {"replayed"}


def test_table_content_hash_summary_streams_key_rows_without_fetchall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCursor:
        def __init__(
            self,
            *,
            rows: list[dict[str, Any]] | None = None,
            fetchall_rows: list[dict[str, Any]] | None = None,
            allow_fetchall: bool = True,
        ) -> None:
            self._rows = rows or []
            self._fetchall_rows = fetchall_rows or []
            self._allow_fetchall = allow_fetchall

        def fetchall(self) -> list[dict[str, Any]]:
            if not self._allow_fetchall:
                raise AssertionError("streaming key hash code should not fetchall rows")
            return self._fetchall_rows

        def __iter__(self):
            return iter(self._rows)

    class FakeConnection:
        def execute(self, sql: str) -> FakeCursor:
            if sql == "PRAGMA table_info(authority_record)":
                return FakeCursor(fetchall_rows=[{"name": "authority_key_v1"}])
            if sql.startswith("SELECT authority_key_v1 AS value FROM authority_record"):
                return FakeCursor(
                    rows=[{"value": "alpha"}, {"value": "beta"}],
                    allow_fetchall=False,
                )
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self) -> None:
            return None

    monkeypatch.setattr(audit.canonical_store, "connect_existing_read_only", lambda _path: FakeConnection())
    monkeypatch.setattr(audit.canonical_store, "actual_tables", lambda _conn: ["authority_record"])

    summary = audit.table_content_hash_summary(tmp_path / "fake.sqlite")

    assert summary == {
        "authority_record": hashlib.sha256("alpha\nbeta".encode()).hexdigest()
    }


def test_table_content_hash_summary_forces_allow_nan_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "canonical.sqlite"
    bootstrap_db(db_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            provenance = canonical_store.record_provenance_event(
                conn,
                object_namespace="rebuildability",
                object_id="row-hash",
                event_type="fixture",
                actor_type="tool",
                actor_id="pytest",
                tool_name="tests/test_rebuildability_audit.py",
                run_id="run-row-hash",
                event_timestamp=FIXED_TIMESTAMP,
                note_text="row hash fixture",
                provenance_event_key_v1="prov:rebuildability:row-hash",
            )
            canonical_store.upsert_work(
                conn,
                work_key_v1="work:row-hash",
                provenance_event_ref=provenance.event_key,
                work_type="web_page",
                title="Row hash fixture",
                review_state="accepted",
                publication_state="public_safe",
                workspace_id="rebuildability",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
    finally:
        conn.close()

    calls: list[dict[str, Any]] = []
    original_dumps = audit.json.dumps

    def fake_dumps(payload: Any, **kwargs: Any) -> str:
        calls.append(dict(kwargs))
        assert kwargs.get("allow_nan") is False
        return original_dumps(payload, **kwargs)

    monkeypatch.setattr(audit.json, "dumps", fake_dumps)

    summary = audit.table_content_hash_summary(db_path)

    assert calls
    assert any(call.get("allow_nan") is False for call in calls)
    assert "work" in summary


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
    assert report["final_status"] == "not_rebuildable"


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


def test_reference_artifacts_reject_future_schema_versions(tmp_path: Path) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    cycle_path = runs_dir / "topic-cycle" / "cycle-001" / "topic-cycle-run.json"
    cycle_payload = load_json(cycle_path)
    cycle_payload["schema_version"] = "topic-cycle-run.v999"
    cycle_path.write_text(json.dumps(cycle_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    review_dir = runs_dir / "review"
    review_dir.mkdir()
    (review_dir / "review-decision-apply-result.json").write_text(
        json.dumps(
            {
                "schema_version": "review-decision-apply-result.v999",
                "target": "source_claim:1",
                "decision_action": "reject_claim",
                "status": "completed",
            },
            indent=2,
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
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = load_json(report_path)
    invalid = [
        item for item in report["artifacts_discovered"] if item["validation_status"] == "invalid"
    ]
    assert {item["artifact_type"] for item in invalid} >= {
        "topic_cycle_manifest",
        "review_decision_apply_result",
    }
    assert any("schema_version" in (item.get("failure_reason") or "") for item in invalid)


def test_legacy_artifacts_remain_readable_after_schema_changes(tmp_path: Path) -> None:
    runs_dir = stage_legacy_runs_dir(tmp_path)
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

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert report["final_status"] == "incomplete_support"
    discovered = {item["artifact_type"]: item for item in report["artifacts_discovered"]}
    assert discovered["gather_candidate_batch"]["schema_id"] == "gather-candidate-batch.v0"
    assert discovered["source_acquisition_execution"]["schema_id"] == "source-acquisition-execution.v0"
    assert discovered["topic_cycle_manifest"]["schema_id"] == "topic-cycle-run.v0"
    assert discovered["review_decision_apply_result"]["schema_id"] == "review-decision-apply-result.v0"
    assert discovered["publication_artifact"]["schema_id"] == "publication-artifacts-report.v0"
    assert discovered["network_safety_gate_report"]["schema_id"] == "network-safety-gate-report.v0"
    assert discovered["rebuildability_report"]["schema_id"] == "canonical-rebuildability-report.v0"
    assert report["artifacts_validated"] == len(report["artifacts_discovered"])
    expected_missing = {
        artifact_type
        for artifact_type, item in discovered.items()
        if item["replay_status"] == "reference_only"
    }
    assert {item["artifact_type"] for item in report["missing_replay_support"]} == expected_missing


def test_rebuildable_report_can_reconstruct_row_provenance_chain(tmp_path: Path) -> None:
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
    artifact_by_type = {item["artifact_type"]: item for item in report["artifacts_discovered"]}
    assert artifact_by_type["gather_candidate_batch"]["originating_run_id"] == "fixture-gather-ingest"
    assert artifact_by_type["source_acquisition_execution"]["originating_run_id"] == "fixture-exec"
    assert report["canonical_validation_result"]["status"] == "pass"
    assert report["graph_closure_result"]["status"] in {"pass", "pass_with_unresolved"}
    assert {item["status"] for item in report["replay_results"]} == {"replayed"}

    conn = canonical_store.connect_existing_read_only(rebuilt_db)
    try:
        source_claim = conn.execute(
            "SELECT provenance_event_ref, claim_text FROM source_claim ORDER BY source_claim_id LIMIT 1"
        ).fetchone()
        capture_event = conn.execute(
            "SELECT provenance_event_ref, original_locator FROM capture_event ORDER BY capture_event_id LIMIT 1"
        ).fetchone()
        extraction_record = conn.execute(
            "SELECT capture_event_id, provenance_event_ref, extraction_status FROM extraction_record ORDER BY extraction_id LIMIT 1"
        ).fetchone()
        source_claim_prov = conn.execute(
            "SELECT run_id, tool_name, note_text FROM provenance_event WHERE provenance_event_key_v1=?",
            (source_claim["provenance_event_ref"],),
        ).fetchone()
        capture_prov = conn.execute(
            "SELECT run_id, tool_name, note_text FROM provenance_event WHERE provenance_event_key_v1=?",
            (capture_event["provenance_event_ref"],),
        ).fetchone()
        extraction_prov = conn.execute(
            "SELECT run_id, tool_name, note_text FROM provenance_event WHERE provenance_event_key_v1=?",
            (extraction_record["provenance_event_ref"],),
        ).fetchone()
    finally:
        conn.close()

    assert "gather-candidate-batch.json" in source_claim_prov["note_text"]
    assert source_claim_prov["run_id"] == "fixture-gather-ingest"
    assert "capture-events.jsonl" in capture_prov["note_text"]
    assert "extraction-records.jsonl" in capture_prov["note_text"]
    assert capture_prov["run_id"] == "fixture-exec"
    assert extraction_record["capture_event_id"] is not None
    assert extraction_prov["run_id"] == "fixture-exec"
    assert extraction_prov["tool_name"] == "tools/scripts/ingest_execution_artifacts.py"


def test_rebuildability_audit_survives_hostile_run_directories(tmp_path: Path) -> None:
    runs_dir = stage_hostile_runs_dir(tmp_path)
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

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = load_json(report_path)
    assert report["final_status"] == "not_rebuildable"
    assert report["artifacts_missing"]
    assert any(item["validation_status"] == "invalid" for item in report["artifacts_discovered"])
    assert any(
        item["artifact_type"] == "review_decision_apply_result" and item["validation_status"] == "invalid"
        for item in report["artifacts_discovered"]
    )
    outside_path = (tmp_path / "outside" / "escaped-gather-candidate-batch.json").resolve()
    assert not any(item["path"] == str(outside_path) for item in report["artifacts_discovered"])


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

    assert first_proc.returncode == 1, first_proc.stdout + first_proc.stderr
    assert second_proc.returncode == 1, second_proc.stdout + second_proc.stderr
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_main_writes_report_with_atomic_json_writer(tmp_path: Path, monkeypatch) -> None:
    runs_dir = stage_runs_dir(tmp_path)
    output = tmp_path / "audit-report.json"
    writes: list[Path] = []
    original_write_text = audit.Path.write_text

    def fake_atomic_write(path: Path, payload: object) -> None:
        writes.append(path)
        # Preserve a real artifact for downstream checks.
        output.parent.mkdir(parents=True, exist_ok=True)
        original_write_text(
            output,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def reject_direct_write(_self: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("direct write_text should not be used")

    monkeypatch.setattr(audit, "atomic_write_json", fake_atomic_write)
    monkeypatch.setattr(audit.Path, "write_text", reject_direct_write)

    exit_code = audit.main(
        [
            "--runs-dir",
            str(runs_dir),
            "--output",
            str(output),
            "--replay-mode",
            "validate_only",
            "--generated-at",
            FIXED_TIMESTAMP,
        ]
    )

    assert exit_code == 1
    assert writes == [output.resolve()]
    assert output.is_file()
    report = load_json(output)
    assert_rebuildability_report_schema(report)
