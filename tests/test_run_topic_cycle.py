from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def load_run_topic_cycle_module():
    spec = importlib.util.spec_from_file_location("run_topic_cycle_for_tests", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_stage_plan_matches_feedback_plan_mode() -> None:
    module = load_run_topic_cycle_module()

    assert module.build_stage_plan(feedback_plan_mode=None, build_next_feedback_plan=False) == [
        "resolve_subject_runtime",
        "resolve_domain_pack",
        "validate_canonical_store",
        "feedback_plan_pre",
        "run_gather",
        "ingest_candidate_batch",
        "execute_source_adapter",
        "ingest_execution_artifacts",
        "feedback_plan_post",
        "build_publication",
        "final_canonical_store_summary",
        "graph_closure_audit",
    ]
    assert module.build_stage_plan(feedback_plan_mode="auto", build_next_feedback_plan=True) == [
        "resolve_subject_runtime",
        "resolve_domain_pack",
        "validate_canonical_store",
        "build_feedback_plan_pre",
        "run_gather",
        "ingest_candidate_batch",
        "execute_source_adapter",
        "ingest_execution_artifacts",
        "build_feedback_plan_post",
        "build_publication",
        "final_canonical_store_summary",
        "graph_closure_audit",
    ]
    assert module.build_stage_plan(
        feedback_plan_mode="fixtures/feedback-plan.json",
        build_next_feedback_plan=False,
    ) == [
        "resolve_subject_runtime",
        "resolve_domain_pack",
        "validate_canonical_store",
        "load_feedback_plan",
        "run_gather",
        "ingest_candidate_batch",
        "execute_source_adapter",
        "ingest_execution_artifacts",
        "feedback_plan_post",
        "build_publication",
        "final_canonical_store_summary",
        "graph_closure_audit",
    ]


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
    assert stages["ingest_candidate_batch"]["artifacts"]["mutated"] is False  # type: ignore[index]
    assert Path(stages["ingest_candidate_batch"]["artifacts"]["ingest_report"]).is_file()  # type: ignore[index]
    assert isinstance(stages["ingest_candidate_batch"]["artifacts"]["ingest_report_sha256"], str)  # type: ignore[index]


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


def test_topic_cycle_degraded_spool_reports_retry_exception(tmp_path: Path) -> None:
    module = load_run_topic_cycle_module()
    calls = {"count": 0}

    def fake_load_validated_execution_artifacts(execution_run_dir: Path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("outer load failure")
        raise RuntimeError("retry load failure")

    module.canonical_ingest.load_validated_execution_artifacts = (  # type: ignore[attr-defined]
        fake_load_validated_execution_artifacts
    )

    args = SimpleNamespace(mode="live", degraded_spool=True)
    manifest = {"stages": []}

    with pytest.raises(module.TopicCycleError) as excinfo:
        module.execution_ingest_stage(
            args=args,
            manifest=manifest,
            db_path=tmp_path / "canonical.sqlite",
            execution_run_dir=EXECUTION_RUN,
            run_dir=tmp_path / "cycle-degraded-spool",
        )

    assert calls["count"] == 2
    assert "retry load failure" in str(excinfo.value)
    assert "outer load failure" not in str(excinfo.value)


def test_topic_cycle_unexpected_exception_writes_failed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-unexpected-exception"

    def boom(**_kwargs):
        raise ValueError("boom from runtime stage")

    monkeypatch.setattr(module, "resolve_runtime_stage", boom)

    args = module.parse_args(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-unexpected-exception",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--mode",
            "local",
        ]
    )

    manifest, exit_code = module.run_topic_cycle(args)
    persisted = load_manifest(run_dir)

    assert exit_code == 1
    assert manifest["status"] == "failed"
    assert persisted["status"] == "failed"
    assert manifest["error_summary"] == "boom from runtime stage"
    assert persisted["error_summary"] == "boom from runtime stage"
    assert manifest["failure_stage"] == "cycle_setup"
    assert persisted["failure_stage"] == "cycle_setup"


def test_topic_cycle_final_status_recomputes_after_evidence_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()
    run_dir = tmp_path / "cycle-final-status"
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)

    monkeypatch.setattr(
        module,
        "resolve_runtime_stage",
        lambda **kwargs: {
            "subject": {"subject_id": "fixture_subject"},
            "domain_pack": {"name": "general.v1"},
        },
    )
    monkeypatch.setattr(module, "resolve_domain_pack_stage", lambda **kwargs: None)
    monkeypatch.setattr(module, "validate_store_stage", lambda **kwargs: None)
    monkeypatch.setattr(
        module,
        "resolve_feedback_plan",
        lambda **kwargs: {"path": str(tmp_path / "feedback-plan.json")},
    )
    monkeypatch.setattr(module, "gather_stage", lambda **kwargs: tmp_path / "candidate-batch.json")
    monkeypatch.setattr(module, "candidate_ingest_stage", lambda **kwargs: None)
    monkeypatch.setattr(module, "acquisition_stage", lambda **kwargs: tmp_path / "execution-run")
    monkeypatch.setattr(module, "execution_ingest_stage", lambda **kwargs: None)
    monkeypatch.setattr(module, "publication_stage", lambda **kwargs: None)
    monkeypatch.setattr(module, "final_store_stage", lambda **kwargs: None)
    monkeypatch.setattr(module, "graph_closure_stage", lambda **kwargs: None)

    def fake_record_cycle_evidence_from_manifest(
        *,
        args: object,
        manifest: dict[str, object],
        manifest_path: Path,
        db_path: Path,
    ) -> None:
        module.add_spool_record_to_manifest(
            manifest,
            spool_path=manifest_path,
            record={
                "spool_record_id": "spool:cycle-evidence",
                "operation_kind": "cycle_evidence_write",
                "failure_kind": "synthetic",
                "replay_status": "spooled",
            },
        )

    monkeypatch.setattr(
        module, "record_cycle_evidence_from_manifest", fake_record_cycle_evidence_from_manifest
    )

    args = module.parse_args(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-final-status",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--mode",
            "local",
        ]
    )

    manifest, exit_code = module.run_topic_cycle(args)
    persisted = load_manifest(run_dir)

    assert exit_code == 0
    assert manifest["status"] == "degraded"
    assert persisted["status"] == "degraded"
    assert persisted["spool_records"]
    assert persisted["spool_records"][0]["operation_kind"] == "cycle_evidence_write"  # type: ignore[index]


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
    assert manifest["graph_closure"]["status"] == "disabled"  # type: ignore[index]
    assert manifest["graph_closure"].get("report_path") is None
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
    assert stages["graph_closure_audit"]["status"] in {"passed", "warning", "skipped"}
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


def test_topic_cycle_dry_run_records_execution_ingest_report_by_reference(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-exec-dry-run"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-exec-dry-run",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
            "--execution-run-fixture",
            str(EXECUTION_RUN),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_manifest(run_dir)
    stages = stages_by_name(manifest)
    assert stages["execute_source_adapter"]["status"] == "skipped"
    artifacts = stages["ingest_execution_artifacts"]["artifacts"]
    assert artifacts["mutated"] is False  # type: ignore[index]
    assert Path(artifacts["ingest_report"]).is_file()  # type: ignore[index]
    assert isinstance(artifacts["ingest_report_sha256"], str)  # type: ignore[index]


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
    assert manifest["graph_closure"]["status"] == "disabled"  # type: ignore[index]


def test_gather_stage_uses_payload_hashes_from_child(monkeypatch, tmp_path: Path) -> None:
    module = load_run_topic_cycle_module()

    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    batch_path = tmp_path / "gather-candidate-batch.json"
    prompt_path = tmp_path / "rendered-prompt.txt"
    batch_path.write_text("{}", encoding="utf-8")
    prompt_path.write_text("prompt", encoding="utf-8")

    fake_payload = {
        "candidate_batch_path": str(batch_path),
        "rendered_prompt_path": str(prompt_path),
        "candidate_batch_sha256": "candidate-hash",
        "rendered_prompt_sha256": "prompt-hash",
    }

    def fake_run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(fake_payload),
            stderr="",
        )

    def deny_rehash(path: Path) -> str:
        raise AssertionError(f"unexpected hash_file call: {path}")

    monkeypatch.setattr(module, "run_command", fake_run_command)
    monkeypatch.setattr(
        module,
        "validate_gather_candidate_batch",
        lambda path: ({"valid": True}, module.EXIT_GATHER_PASS),
    )
    monkeypatch.setattr(module, "hash_file", deny_rehash)

    args = SimpleNamespace(
        mode="dry-run",
        facet="sources",
        phase="01a",
        use_prior_state=False,
        cycle_depth=1,
        previous_run_id=[],
        dry_run=True,
        candidate_batch_fixture=None,
    )
    manifest = {
        "run_id": "cycle-827",
        "started_at": "2026-06-03T12:00:00Z",
        "stages": [],
        "prior_state": {},
    }
    runtime = {"subject_manifest_path": str(prompt_path)}

    result = module.gather_stage(
        args=args,
        manifest=manifest,
        workspace=tmp_path,
        db_path=tmp_path / "canonical.sqlite",
        run_dir=run_dir,
        runtime=runtime,
        feedback_plan=None,
    )

    assert result == batch_path
    stages = stages_by_name(manifest)
    assert stages["run_gather"]["artifacts"]["candidate_batch_sha256"] == "candidate-hash"  # type: ignore[index]
    assert stages["run_gather"]["artifacts"]["rendered_prompt_sha256"] == "prompt-hash"  # type: ignore[index]


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
            "--graph-closure",
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
