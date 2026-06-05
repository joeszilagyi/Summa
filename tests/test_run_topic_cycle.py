from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "run_topic_cycle.py"
WRAPPER = REPO_ROOT / "tools" / "scripts" / "Index_Run_Topic_Cycle.sh"
CANDIDATE_BATCH = (
    REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
)
EXECUTION_RUN = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"


def run_cycle(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def init_db(path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "source_db_tools" / "init_canonical_store.py"),
            "--db",
            str(path),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def write_workspace(tmp_path: Path, *, subject_id: str = "fixture_subject") -> Path:
    workspace = tmp_path / "workspace"
    manifest_path = workspace / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": subject_id,
                "display_name": "Fixture Subject",
                "domain_pack": "general.v1",
                "scope_statement": "Fixture topic cycle subject.",
                "languages": ["en"],
                "aliases": ["Fixture Subject"],
                "disambiguation_terms": ["fixture"],
                "excluded_senses": ["non-fixture"],
                "enabled_facets": [
                    "sources",
                    "timeline",
                    "people",
                    "places",
                    "works",
                    "open_questions",
                ],
                "query_families": ["general_research"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return workspace


def table_count(db_path: Path, table_name: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
    finally:
        conn.close()


def insert_orphan_source_claim(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO source_claim (
              source_claim_key_v1,
              claim_text,
              public_summary,
              claim_type,
              review_state,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "claim:topic-cycle:orphan",
                "orphan claim",
                "orphan claim",
                "factual",
                "accepted",
                "2026-06-03T12:00:00Z",
                "2026-06-03T12:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "topic-cycle-run.json").read_text(encoding="utf-8"))


def stages_by_name(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    return {stage["name"]: stage for stage in manifest["stages"]}  # type: ignore[index]


def test_topic_cycle_python_and_wrapper_help() -> None:
    proc = run_cycle(["--help"])
    assert proc.returncode == 0, proc.stderr
    assert "topic-cycle-run.v1" in proc.stdout

    wrapper_proc = subprocess.run(
        ["bash", str(WRAPPER), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert wrapper_proc.returncode == 0, wrapper_proc.stderr
    assert "--workspace" in wrapper_proc.stdout


def test_topic_cycle_pure_dry_run_writes_manifest_without_db_mutation(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    before_work = table_count(db_path, "work")
    run_dir = tmp_path / "cycle-dry-run"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-dry-run",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_manifest(run_dir)
    assert manifest["schema_version"] == "topic-cycle-run.v1"
    assert manifest["status"] == "dry_run"
    assert manifest["canonical_db"]["mutated"] is False  # type: ignore[index]
    assert table_count(db_path, "work") == before_work
    assert table_count(db_path, "cycle_event") == 0
    stages = stages_by_name(manifest)
    assert stages["run_gather"]["status"] == "passed"
    assert stages["ingest_candidate_batch"]["status"] == "dry_run"
    assert stages["execute_source_adapter"]["status"] == "skipped"


def test_topic_cycle_rejects_resume_on_fresh_run_dir(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-resume-fresh"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-resume-fresh",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
            "--resume",
        ]
    )

    assert proc.returncode != 0
    assert "--resume is reserved" in proc.stderr
    assert not (run_dir / "topic-cycle-run.json").exists()


def test_topic_cycle_rejects_unknown_existing_manifest_status(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-unknown-status"
    run_dir.mkdir()
    (run_dir / "topic-cycle-run.json").write_text(
        json.dumps({"schema_version": "topic-cycle-run.v1", "status": "mystery"}) + "\n",
        encoding="utf-8",
    )

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-unknown-status",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
        ]
    )

    assert proc.returncode != 0
    assert "unknown status" in proc.stderr


def test_topic_cycle_local_fixture_cycle_populates_canonical_store_and_feedback(
    tmp_path: Path,
) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-local"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-local",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--mode",
            "local",
            "--candidate-batch-fixture",
            str(CANDIDATE_BATCH),
            "--execution-run-fixture",
            str(EXECUTION_RUN),
            "--build-next-feedback-plan",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "completed"
    assert isinstance(manifest["cycle_event_id"], str)
    assert manifest["cycle_event_id"].startswith("cycle:")
    assert manifest["canonical_db"]["mutated"] is True  # type: ignore[index]
    assert manifest["graph_closure"]["status"] in {"pass", "pass_with_unresolved"}  # type: ignore[index]
    assert Path(manifest["graph_closure"]["report_path"]).is_file()  # type: ignore[index]
    assert table_count(db_path, "work") >= 1
    assert table_count(db_path, "source_claim") >= 1
    assert table_count(db_path, "capture_event") >= 1
    assert table_count(db_path, "extraction_record") >= 1
    assert table_count(db_path, "cycle_event") == 1
    assert table_count(db_path, "cycle_stage_event") >= len(manifest["stages"])  # type: ignore[arg-type]
    assert table_count(db_path, "cycle_artifact_ref") >= 4
    assert table_count(db_path, "cycle_candidate_considered") >= 1
    assert table_count(db_path, "cycle_candidate_excluded") >= 1
    assert table_count(db_path, "cycle_operator_override") >= 2
    conn = sqlite3.connect(db_path)
    try:
        artifact_types = {
            row[0]
            for row in conn.execute(
                "SELECT artifact_type FROM cycle_artifact_ref ORDER BY artifact_type"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"topic_cycle_manifest", "candidate_batch", "feedback_plan"} <= artifact_types
    stages = stages_by_name(manifest)
    assert stages["ingest_candidate_batch"]["status"] == "passed"
    assert stages["ingest_execution_artifacts"]["status"] == "passed"
    assert stages["build_feedback_plan_post"]["status"] == "passed"
    assert stages["graph_closure_audit"]["status"] in {"passed", "warning"}
    assert manifest["next_action"]["selected_facet"]  # type: ignore[index]
    assert manifest["selection_explanations"]
    assert manifest["selection_explanations"][0]["selection_kind"] == "feedback_next_action"
    assert manifest["selection_explanations"][0]["path"] == manifest["feedback_plan"]["path"]  # type: ignore[index]


def test_topic_cycle_prior_state_and_feedback_plan_auto(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    seed_dir = tmp_path / "cycle-seed"
    seed = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(seed_dir),
            "--run-id",
            "cycle-seed",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--mode",
            "local",
            "--candidate-batch-fixture",
            str(CANDIDATE_BATCH),
        ]
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
    run_dir = tmp_path / "cycle-two"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-two",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--cycle-depth",
            "2",
            "--use-prior-state",
            "--previous-run-id",
            "cycle-seed.gather",
            "--feedback-plan",
            "auto",
            "--dry-run",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "dry_run"
    assert manifest["cycle_depth"] == 2
    assert manifest["prior_state"]["context_hash"]  # type: ignore[index]
    assert manifest["feedback_plan"]["path"]  # type: ignore[index]
    assert manifest["selection_explanations"][0]["selection_kind"] == "feedback_next_action"


def test_topic_cycle_degraded_spool_records_pending_canonical_write(
    tmp_path: Path,
) -> None:
    workspace = write_workspace(tmp_path)
    missing_db = tmp_path / "missing.sqlite"
    run_dir = tmp_path / "cycle-spooled"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(missing_db),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-spooled",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--mode",
            "local",
            "--candidate-batch-fixture",
            str(CANDIDATE_BATCH),
            "--degraded-spool",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "degraded"
    stages = stages_by_name(manifest)
    assert stages["validate_canonical_store"]["status"] == "degraded"
    assert stages["ingest_candidate_batch"]["status"] == "spooled"
    assert manifest["canonical_db"]["mutated"] is False  # type: ignore[index]
    assert manifest["spool_records"]
    assert manifest["spool_records"][0]["operation_kind"] == "candidate_batch_ingest"  # type: ignore[index]
    assert Path(manifest["spool_records"][0]["path"]).is_file()  # type: ignore[index]
    assert manifest["graph_closure"]["status"] == "unavailable"  # type: ignore[index]


def test_topic_cycle_graph_closure_strict_fails_on_orphan_row(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    insert_orphan_source_claim(db_path)
    run_dir = tmp_path / "cycle-closure-fail"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-closure-fail",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
            "--graph-closure-strict",
        ]
    )

    assert proc.returncode == 1
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["failure_stage"] == "graph_closure_audit"
    assert manifest["graph_closure"]["status"] == "fail"  # type: ignore[index]
    report_path = Path(manifest["graph_closure"]["report_path"])  # type: ignore[index]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["true_orphan_error_count"] >= 1


def test_topic_cycle_failure_stops_before_ingestion_and_records_manifest(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    bad_batch = tmp_path / "bad-batch.json"
    bad_batch.write_text('{"schema_version": "gather-candidate-batch.v1"}\n', encoding="utf-8")
    run_dir = tmp_path / "cycle-fail"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-fail",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--mode",
            "local",
            "--candidate-batch-fixture",
            str(bad_batch),
        ]
    )

    assert proc.returncode == 1
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["failure_stage"] == "ingest_candidate_batch"
    assert table_count(db_path, "work") == 0
    assert table_count(db_path, "cycle_event") == 1
    assert table_count(db_path, "cycle_tool_failure") >= 1


def test_topic_cycle_failure_stage_reflects_subject_resolution_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-subject-fail"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-subject-fail",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )

    assert proc.returncode == 1
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "failed"
    assert manifest["failure_stage"] == "resolve_subject_runtime"


def test_topic_cycle_refuses_completed_run_without_force(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-repeat"
    args = [
        "--workspace",
        str(workspace),
        "--db",
        str(db_path),
        "--run-dir",
        str(run_dir),
        "--run-id",
        "cycle-repeat",
        "--timestamp",
        "2026-06-03T12:00:00Z",
        "--dry-run",
    ]
    first = run_cycle(args)
    assert first.returncode == 0, first.stdout + first.stderr

    second = run_cycle(args)
    assert second.returncode == 1
    assert "already completed" in second.stderr


def test_topic_cycle_runner_has_no_direct_canonical_family_inserts() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    forbidden = [
        "INSERT INTO work",
        "INSERT INTO source_claim",
        "INSERT INTO capture_event",
        "INSERT INTO extraction_record",
        "INSERT INTO authority_reconciliation",
        "INSERT INTO source_relationship",
    ]
    for needle in forbidden:
        assert needle not in body
