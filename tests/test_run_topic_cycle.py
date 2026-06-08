from __future__ import annotations

import contextlib
import hashlib
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


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            parts.append(f"{rel}:{file_sha256(path)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


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


def test_validate_store_stage_reuses_validated_store_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()
    manifest: dict[str, object] = {"canonical_db": {}, "stages": []}
    connect_calls: list[Path] = []
    validate_calls: list[tuple[object, dict[str, object]]] = []
    summary_calls: list[tuple[Path, object | None, object | None, bool]] = []
    dummy_conn = SimpleNamespace(close=lambda: None)

    def fake_connect_existing_read_only(db_path: Path) -> object:
        connect_calls.append(db_path)
        return dummy_conn

    def fake_validate_existing_store(
        conn: object, *, outline: dict[str, object]
    ) -> tuple[SimpleNamespace, set[str], set[str]]:
        validate_calls.append((conn, outline))
        return (
            SimpleNamespace(schema_version=8, current_migration_id="m8"),
            {"schema_version", "schema_migration_history"},
            set(),
        )

    def fake_summary(
        db_path: Path,
        *,
        include_counts: bool = True,
        conn: object | None = None,
        validation: object | None = None,
    ) -> dict[str, object]:
        summary_calls.append((db_path, conn, validation, include_counts))
        assert conn is dummy_conn
        assert validation is not None
        assert validation.schema_version == 8
        assert validation.current_migration_id == "m8"
        return {
            "path": str(db_path),
            "exists": True,
            "initialized": True,
            "valid": True,
            "schema_version": 8,
            "current_migration_id": "m8",
            "status": "initialized_empty",
            "family_counts": {},
            "table_counts": {},
            "total_rows": None,
            "last_provenance_event_at": None,
            "last_provenance_event_type": None,
            "last_provenance_event_id": None,
            "last_ingest_at": None,
            "last_ingest_event_type": None,
            "last_ingest_provenance_event_id": None,
            "warnings": [],
            "errors": [],
            "recommended_interpretation": "Canonical store is initialized and valid, but contains no canonical records yet.",
        }

    monkeypatch.setattr(
        module.canonical_store, "connect_existing_read_only", fake_connect_existing_read_only
    )
    monkeypatch.setattr(
        module.canonical_store, "validate_existing_store", fake_validate_existing_store
    )
    monkeypatch.setattr(
        module.canonical_store, "summarize_canonical_store_population", fake_summary
    )
    monkeypatch.setattr(
        module.canonical_store,
        "check_canonical_store",
        lambda *_: pytest.fail("unexpected check_canonical_store"),
    )

    module.validate_store_stage(
        args=SimpleNamespace(degraded_spool=False),
        manifest=manifest,
        db_path=tmp_path / "canonical.sqlite",
    )

    assert connect_calls == [tmp_path / "canonical.sqlite"]
    assert len(validate_calls) == 1
    assert len(summary_calls) == 1
    assert summary_calls[0][3] is False
    assert manifest["canonical_db"]["initial_summary"]["status"] == "initialized_empty"
    assert manifest["canonical_db"]["initial_summary"]["table_counts"] == {}


def test_topic_cycle_pure_dry_run_writes_manifest_without_db_mutation(tmp_path: Path) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    before_work = table_count(db_path, "work")
    before_db_hash = file_sha256(db_path)
    before_workspace_hash = tree_sha256(workspace / ".indexer")
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
            "--command-timeout-seconds",
            "45",
            "--dry-run",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_manifest(run_dir)
    assert manifest["schema_version"] == "topic-cycle-run.v1"
    assert manifest["status"] == "dry_run"
    assert manifest["canonical_db"]["mutated"] is False  # type: ignore[index]
    assert manifest["budget"]["command_timeout_seconds"] == 45.0  # type: ignore[index]
    assert table_count(db_path, "work") == before_work
    assert file_sha256(db_path) == before_db_hash
    assert tree_sha256(workspace / ".indexer") == before_workspace_hash
    assert table_count(db_path, "cycle_event") == 0
    stages = stages_by_name(manifest)
    assert stages["run_gather"]["status"] == "passed"
    assert stages["ingest_candidate_batch"]["status"] == "dry_run"
    assert stages["execute_source_adapter"]["status"] == "skipped"
    assert stages["ingest_candidate_batch"]["artifacts"]["mutated"] is False  # type: ignore[index]
    assert Path(stages["ingest_candidate_batch"]["artifacts"]["ingest_report"]).is_file()  # type: ignore[index]
    assert isinstance(stages["ingest_candidate_batch"]["artifacts"]["ingest_report_sha256"], str)  # type: ignore[index]
    gather_run_dir = workspace / "runs" / "gather" / "cycle-dry-run.gather"
    assert sorted(
        path.relative_to(gather_run_dir).as_posix()
        for path in gather_run_dir.rglob("*")
        if path.is_file()
    ) == [
        "gather-candidate-batch.json",
        "rendered-prompt.txt",
    ]
    assert not (run_dir / "spool").exists()
    assert not any(path.suffix == ".lock" for path in workspace.rglob("*"))


def test_topic_cycle_acquires_workspace_lock_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_run_topic_cycle_module()

    workspace = write_workspace(tmp_path, subject_id="locked_subject")
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-lock-default"
    lock_calls: list[tuple[str, str, Path, bool]] = []

    @contextlib.contextmanager
    def fake_lock(
        workspace_id: str,
        *,
        command: str,
        lock_root: Path,
        wait: bool,
    ):
        lock_calls.append((workspace_id, command, lock_root, wait))
        yield tmp_path / "workspace.lock"

    monkeypatch.setattr(module, "acquire_workspace_lock", fake_lock)

    args = module.parse_args(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-lock-default",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
        ]
    )

    manifest, exit_code = module.run_topic_cycle(args)

    assert exit_code == 0
    assert lock_calls == [
        (
            "locked_subject",
            "run_topic_cycle:cycle-lock-default",
            module.DEFAULT_LOCK_ROOT,
            False,
        )
    ]
    assert manifest["status"] == "dry_run"  # type: ignore[index]


def test_topic_cycle_graph_closure_is_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_run_topic_cycle_module()

    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-graph-closure-default-off"

    def fail_live_graph_closure(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("graph closure audit should not run by default")

    monkeypatch.setattr(
        module.canonical_graph_closure,
        "audit_canonical_graph_closure",
        fail_live_graph_closure,
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
            "cycle-graph-closure-default-off",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
        ]
    )

    manifest, exit_code = module.run_topic_cycle(args)

    assert exit_code == 0
    assert manifest["graph_closure"]["status"] == "disabled"  # type: ignore[index]
    assert manifest["graph_closure"]["disabled_reason"] == "disabled_by_operator_flag"  # type: ignore[index]
    persisted = load_manifest(run_dir)
    assert persisted["graph_closure"]["status"] == "disabled"  # type: ignore[index]
    assert persisted["graph_closure"]["disabled_reason"] == "disabled_by_operator_flag"  # type: ignore[index]


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


def test_topic_cycle_degraded_spool_reports_retry_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_run_topic_cycle_module()
    calls = {"count": 0}

    def fake_load_validated_execution_artifacts(execution_run_dir: Path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("outer load failure")
        raise RuntimeError("retry load failure")

    monkeypatch.setattr(
        module.canonical_ingest,
        "load_validated_execution_artifacts",
        fake_load_validated_execution_artifacts,
    )

    args = SimpleNamespace(mode="live", degraded_spool=True, candidate_batch_fixture=None)
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


def test_topic_cycle_final_status_is_final_before_cycle_evidence_recording(
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
    monkeypatch.setattr(
        module,
        "gather_stage",
        lambda **kwargs: (
            tmp_path / "candidate-batch.json",
            {},
            "candidate-hash",
            {
                "artifact_path": str(tmp_path / "candidate-batch.json"),
                "artifact_hash": "candidate-hash",
                "validator_name": "gather_candidate_batch",
                "validator_version": "1",
                "result": {"counts": {}, "errors": [], "warnings": []},
            },
        ),
    )
    monkeypatch.setattr(module, "candidate_ingest_stage", lambda **kwargs: None)
    monkeypatch.setattr(module, "acquisition_stage", lambda **kwargs: tmp_path / "execution-run")

    def fake_execution_ingest_stage(**kwargs):
        module.add_spool_record_to_manifest(
            kwargs["manifest"],
            spool_path=tmp_path / "preexisting-spool.json",
            record={
                "spool_record_id": "spool:preexisting",
                "operation_kind": "cycle_evidence_write",
                "failure_kind": "synthetic",
                "replay_status": "spooled",
            },
        )

    monkeypatch.setattr(module, "execution_ingest_stage", fake_execution_ingest_stage)
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
        assert manifest["status"] == "degraded"
        assert manifest["cycle_evidence_ledger"]["status"] == "recorded"  # type: ignore[index]
        assert manifest_path.is_file()
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert persisted["status"] == "degraded"
        assert persisted["cycle_evidence_ledger"]["status"] == "recorded"  # type: ignore[index]

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
            "--skip-workspace-lock",
        ]
    )

    manifest, exit_code = module.run_topic_cycle(args)
    persisted = load_manifest(run_dir)

    assert exit_code == 0
    assert manifest["status"] == "degraded"
    assert persisted["status"] == "degraded"
    assert manifest["cycle_evidence_ledger"]["status"] == "recorded"  # type: ignore[index]
    assert persisted["cycle_evidence_ledger"]["status"] == "recorded"  # type: ignore[index]
    assert persisted["spool_records"]


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
    assert (
        stages["ingest_candidate_batch"]["evidence"]["candidate_batch"]["schema_version"]
        == "gather-candidate-batch.v1"
    )  # type: ignore[index]
    assert (
        stages["build_feedback_plan_post"]["evidence"]["feedback_plan"]["schema_version"]
        == "candidate-feedback-plan.v1"
    )  # type: ignore[index]
    assert (
        stages["ingest_execution_artifacts"]["evidence"]["artifact_schema_ids"]["ingest_report"]
        == "canonical-ingest-report.v1"
    )  # type: ignore[index]
    assert manifest["next_action"]["selected_facet"]  # type: ignore[index]
    assert manifest["feedback_plan_pre"] is None
    assert manifest["feedback_plan"] is None
    assert manifest["active_feedback_plan_for_gather"] is None
    assert manifest["feedback_plan_post"]["path"]  # type: ignore[index]
    assert manifest["selection_explanations"]
    assert len(manifest["selection_explanations"]) == 1
    assert manifest["selection_explanations"][0]["selection_kind"] == "feedback_next_action"
    assert manifest["selection_explanations"][0]["when"] == "post"
    assert manifest["selection_explanations"][0]["path"] == manifest["feedback_plan_post"]["path"]  # type: ignore[index]


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
    assert proc.stdout.startswith("schema_version=topic-cycle-run.v1\n")
    assert not proc.stdout.lstrip().startswith("{")
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "dry_run"
    assert manifest["cycle_depth"] == 2
    assert manifest["prior_state"]["context_hash"]  # type: ignore[index]
    assert manifest["feedback_plan"]["path"]  # type: ignore[index]
    assert manifest["feedback_plan_pre"]["path"] == manifest["feedback_plan"]["path"]  # type: ignore[index]
    assert manifest["active_feedback_plan_for_gather"]["path"] == manifest["feedback_plan"]["path"]  # type: ignore[index]
    assert manifest["selection_explanations"][0]["selection_kind"] == "feedback_next_action"


def test_topic_cycle_explicit_json_format_still_prints_full_manifest(
    tmp_path: Path,
) -> None:
    workspace = write_workspace(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    run_dir = tmp_path / "cycle-json-format"

    proc = run_cycle(
        [
            "--workspace",
            str(workspace),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "cycle-json-format",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--dry-run",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "topic-cycle-run.v1"
    assert payload["status"] == "dry_run"
    assert payload["run_id"] == "cycle-json-format"


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

    def fake_run_command(
        command: list[str], *, cwd: Path, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 111.0
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(fake_payload),
            stderr="",
        )

    def fake_hash_file(path: Path) -> str:
        if path == batch_path:
            raise AssertionError("candidate batch was rehashed")
        if path.name == "gather-candidate-batch-validation.json":
            return "receipt-hash"
        raise AssertionError(f"unexpected hash_file call: {path}")

    def fake_load_validated_candidate_batch(
        path: Path,
    ) -> tuple[dict[str, object], dict[str, object], int]:
        if path == batch_path:
            return {}, {"valid": True}, module.EXIT_GATHER_PASS
        raise AssertionError(f"unexpected candidate batch reload: {path}")

    original_read_json = module.read_json

    def fake_read_json(path: Path, *, label: str) -> dict[str, object]:
        if path == batch_path:
            raise AssertionError("candidate batch should be reused after validation")
        return original_read_json(path, label=label)

    monkeypatch.setattr(module, "run_command", fake_run_command)
    monkeypatch.setattr(
        module.gather_candidate_batch_validator,
        "load_validated_gather_candidate_batch",
        fake_load_validated_candidate_batch,
    )
    monkeypatch.setattr(module, "hash_file", fake_hash_file)
    monkeypatch.setattr(module, "read_json", fake_read_json)

    args = SimpleNamespace(
        mode="dry-run",
        facet="sources",
        phase="01a",
        use_prior_state=False,
        cycle_depth=1,
        previous_run_id=[],
        dry_run=True,
        candidate_batch_fixture=None,
        command_timeout_seconds=111.0,
    )
    manifest = {
        "run_id": "cycle-827",
        "started_at": "2026-06-03T12:00:00Z",
        "stages": [],
        "prior_state": {},
    }
    runtime = {"subject_manifest_path": str(prompt_path)}

    batch_path_result, batch_payload, batch_hash, validation_receipt = module.gather_stage(
        args=args,
        manifest=manifest,
        workspace=tmp_path,
        db_path=tmp_path / "canonical.sqlite",
        run_dir=run_dir,
        runtime=runtime,
        feedback_plan=None,
    )

    assert batch_path_result == batch_path
    assert batch_payload == {}
    assert batch_hash == "candidate-hash"
    assert validation_receipt["artifact_path"] == str(batch_path)
    assert validation_receipt["artifact_hash"] == "candidate-hash"
    assert validation_receipt["validator_name"] == "gather_candidate_batch"
    assert validation_receipt["validator_version"] == "1"
    assert validation_receipt["result"] == {"valid": True}
    stages = stages_by_name(manifest)
    assert stages["run_gather"]["artifacts"]["candidate_batch_sha256"] == "candidate-hash"  # type: ignore[index]
    assert stages["run_gather"]["artifacts"]["rendered_prompt_sha256"] == "prompt-hash"  # type: ignore[index]
    receipt_path = Path(stages["run_gather"]["artifacts"]["candidate_batch_validation_receipt"])  # type: ignore[index]
    assert receipt_path.is_file()
    assert (
        stages["run_gather"]["artifacts"]["candidate_batch_validation_receipt_sha256"]
        == "receipt-hash"
    )  # type: ignore[index]


def test_feedback_plan_stage_hashes_output_once_without_rehashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()

    run_dir = tmp_path / "feedback-plan-run"
    run_dir.mkdir()
    output_path = run_dir / "feedback" / "candidate-feedback-plan.pre.json"
    payload = {
        "schema_version": "candidate-feedback-plan.v1",
        "selection_explanation": {
            "explanation_id": "feedback-plan-explanation",
            "selection_kind": "feedback_next_action",
        },
        "next_action": {"selected_facet": "sources"},
        "deferred": [],
        "counts": {"selected": 1},
    }

    def fake_run_command(
        command: list[str], *, cwd: Path, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 123.0
        output_index = command.index("--output-json") + 1
        path = Path(command[output_index])
        assert path == output_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    def fake_validate_candidate_feedback_plan(path: Path):
        assert path == output_path
        return ({"errors": [], "warnings": [], "counts": {}}, module.EXIT_FEEDBACK_PASS)

    hashed_paths: list[Path] = []

    def fake_hash_file(path: Path) -> str:
        if path != output_path:
            raise AssertionError(f"unexpected hash_file call: {path}")
        hashed_paths.append(path)
        return "feedback-hash"

    monkeypatch.setattr(module, "run_command", fake_run_command)
    monkeypatch.setattr(
        module, "validate_candidate_feedback_plan", fake_validate_candidate_feedback_plan
    )
    monkeypatch.setattr(module, "hash_file", fake_hash_file)

    args = SimpleNamespace(degraded_spool=False)
    args.command_timeout_seconds = 123.0
    manifest = {
        "run_id": "cycle-feedback-plan",
        "started_at": "2026-06-03T12:34:56Z",
        "stages": [],
        "selection_explanations": [],
        "next_action": None,
    }
    runtime = {"subject_manifest_path": str(tmp_path / "subject-manifest.json")}

    result = module.build_feedback_plan_stage(
        args=args,
        manifest=manifest,
        workspace=tmp_path,
        db_path=tmp_path / "canonical.sqlite",
        run_dir=run_dir,
        runtime=runtime,
        when="pre",
    )

    assert result == output_path
    assert hashed_paths == [output_path]
    assert manifest["feedback_plan"]["sha256"] == "feedback-hash"  # type: ignore[index]
    assert manifest["selection_explanations"][0]["sha256"] == "feedback-hash"  # type: ignore[index]


def test_graph_closure_stage_hashes_report_once_without_rehashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()

    run_dir = tmp_path / "graph-closure-run"
    run_dir.mkdir()
    report_path = run_dir / "graph-closure-report.json"
    report_payload = {
        "schema_version": "canonical-graph-closure-report.v1",
        "status": "pass",
        "summary": {
            "true_orphan_error_count": 0,
            "unresolved_tracked_count": 0,
            "repairable_count": 0,
            "quarantined_count": 0,
        },
    }

    def fake_audit_canonical_graph_closure(
        db_path: Path,
        *,
        generated_at: str,
        strict: bool,
        report_path: Path,
    ) -> dict[str, object]:
        assert db_path == tmp_path / "canonical.sqlite"
        assert generated_at == "2026-06-03T12:34:56Z"
        assert strict is True
        assert report_path == run_dir / "graph-closure-report.json"
        report_path.write_text(
            json.dumps(report_payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return report_payload

    hashed_paths: list[Path] = []

    def fake_hash_file(path: Path) -> str:
        if path != report_path:
            raise AssertionError(f"unexpected hash_file call: {path}")
        hashed_paths.append(path)
        return "graph-hash"

    monkeypatch.setattr(
        module.canonical_graph_closure,
        "audit_canonical_graph_closure",
        fake_audit_canonical_graph_closure,
    )
    monkeypatch.setattr(module, "hash_file", fake_hash_file)

    args = SimpleNamespace(graph_closure=True, graph_closure_strict=True, graph_closure_report=None)
    manifest = {
        "run_id": "cycle-graph-closure",
        "started_at": "2026-06-03T12:34:56Z",
        "stages": [],
        "warnings": [],
        "graph_closure": {},
    }

    module.graph_closure_stage(
        args=args,
        manifest=manifest,
        db_path=tmp_path / "canonical.sqlite",
        run_dir=run_dir,
    )

    assert hashed_paths == [report_path]
    assert manifest["graph_closure"]["report_sha256"] == "graph-hash"  # type: ignore[index]
    stages = stages_by_name(manifest)
    assert stages["graph_closure_audit"]["artifacts"]["graph_closure_report_sha256"] == "graph-hash"  # type: ignore[index]


def test_candidate_ingest_spool_reuses_batch_hash_without_rehashing(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_run_topic_cycle_module()

    batch_path = tmp_path / "gather-candidate-batch.json"
    batch_path.write_text(Path(CANDIDATE_BATCH).read_text(encoding="utf-8"), encoding="utf-8")

    db_path = tmp_path / "canonical.sqlite"
    run_dir = tmp_path / "candidate-ingest-run"

    batch_payload = json.loads(batch_path.read_text(encoding="utf-8"))
    validation_receipt = {
        "artifact_path": str(batch_path),
        "artifact_hash": "batch-hash",
        "validator_name": "gather_candidate_batch",
        "validator_version": "1",
        "result": {"counts": {}, "errors": [], "warnings": []},
    }

    def fake_load_validated(path: Path):
        raise AssertionError(f"unexpected candidate batch reload: {path}")

    def fake_ingest(*args, **kwargs):
        raise RuntimeError("forced ingest failure")

    def fake_connect(_: Path):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def close(self) -> None:
                return None

        return FakeConn()

    hashed_batch_path: list[Path] = []

    def fake_hash_file(path: Path) -> str:
        if path == batch_path:
            hashed_batch_path.append(path)
            raise AssertionError("candidate batch was rehashed")
        return "0" * 64

    monkeypatch.setattr(
        module.canonical_ingest, "load_validated_candidate_batch", fake_load_validated
    )
    monkeypatch.setattr(module.canonical_ingest, "ingest_candidate_batch", fake_ingest)
    monkeypatch.setattr(module.canonical_store, "connect_canonical_store", fake_connect)
    monkeypatch.setattr(module, "hash_file", fake_hash_file)

    args = SimpleNamespace(
        mode="live",
        degraded_spool=True,
        candidate_batch_fixture=None,
        spool_dir=None,
    )
    manifest = {
        "run_id": "cycle-830",
        "stages": [],
        "workspace": {"workspace_id": "fixture_workspace"},
        "cycle_event_id": "cycle:fixture",
        "canonical_db": {"mutated": False},
        "subject": {"subject_id": "fixture_subject"},
    }

    result = module.candidate_ingest_stage(
        args=args,
        manifest=manifest,
        db_path=db_path,
        batch_path=batch_path,
        run_dir=run_dir,
        candidate_batch=batch_payload,
        candidate_batch_hash="batch-hash",
        validation_receipt=validation_receipt,
    )

    assert hashed_batch_path == []
    assert result["status"] == "spooled"
    spool_path = Path(result["spool_record_path"])  # type: ignore[index]
    spool_payload = json.loads(spool_path.read_text(encoding="utf-8"))
    refs = spool_payload["operation_input"]["artifact_refs"][0]
    assert refs["artifact_hash"] == "batch-hash"
    assert spool_payload["replay_recipe"]["batch_hash"] == "batch-hash"


def test_execution_ingest_spool_reuses_loaded_artifacts_without_reload(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_run_topic_cycle_module()

    run_dir = tmp_path / "execution-ingest-run"
    execution_run_dir = tmp_path / "execution-run"
    execution_run_dir.mkdir()
    db_path = tmp_path / "canonical.sqlite"
    execution_records = {
        "execution_record": {"schema_version": "source-execution-record.v1"},
        "capture_events": [{"schema_version": "source-capture-event.v1"}],
        "extraction_records": [{"schema_version": "source-extraction-record.v1"}],
        "paths": {
            "execution_record": tmp_path / "execution-record.json",
            "capture_events": tmp_path / "capture-events.jsonl",
            "extraction_records": tmp_path / "extraction-records.jsonl",
        },
    }
    for path in execution_records["paths"].values():
        path.write_text("{}", encoding="utf-8")

    def fake_load_execution_artifacts(_: Path):
        fake_load_execution_artifacts.calls += 1
        return (
            execution_records["execution_record"],
            execution_records["paths"],
            {
                "execution_record": "record-hash",
                "capture_events": "capture-hash",
                "extraction_records": "extraction-hash",
            },
        )

    fake_load_execution_artifacts.calls = 0

    def fake_ingest(*args, **kwargs):
        raise RuntimeError("forced execution ingest failure")

    def fake_connect(_: Path):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def close(self) -> None:
                return None

        return FakeConn()

    monkeypatch.setattr(
        module.canonical_ingest, "load_validated_execution_artifacts", fake_load_execution_artifacts
    )
    monkeypatch.setattr(module.canonical_ingest, "ingest_execution_artifacts", fake_ingest)
    monkeypatch.setattr(module.canonical_store, "connect_canonical_store", fake_connect)

    args = SimpleNamespace(mode="live", degraded_spool=True, spool_dir=None)
    manifest = {
        "run_id": "cycle-831",
        "stages": [],
        "workspace": {"workspace_id": "fixture_workspace"},
        "cycle_event_id": "cycle:fixture",
        "canonical_db": {"mutated": False},
        "subject": {"subject_id": "fixture_subject"},
    }

    result = module.execution_ingest_stage(
        args=args,
        manifest=manifest,
        db_path=db_path,
        execution_run_dir=execution_run_dir,
        run_dir=run_dir,
    )

    assert fake_load_execution_artifacts.calls == 1
    assert result["status"] == "spooled"


def test_topic_cycle_execution_artifact_receipt_reused_between_acquisition_and_ingest(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_run_topic_cycle_module()

    run_dir = tmp_path / "topic-cycle-run"
    run_dir.mkdir()
    execution_run_dir = run_dir / "execution"
    db_path = tmp_path / "canonical.sqlite"

    fake_receipt = module.ExecutionArtifactReceipt(
        execution_record={"run_id": "cycle-832"},
        paths={
            "run_dir": execution_run_dir,
            "execution_record": execution_run_dir / "execution-record.json",
            "capture_events": execution_run_dir / "capture-events.jsonl",
            "extraction_records": execution_run_dir / "extraction-records.jsonl",
        },
        input_hashes={
            "execution_record": "record-hash",
            "capture_events": "capture-hash",
            "extraction_records": "extraction-hash",
        },
        manifest={
            "schema_version": "source-acquisition-run-manifest.v1",
            "run_id": "cycle-832",
            "created_at": "2026-06-03T12:34:56Z",
            "status": "completed",
            "artifacts": {
                "execution_record": "execution-record.json",
                "capture_events": "capture-events.jsonl",
                "extraction_records": "extraction-records.jsonl",
                "manifest": "manifest.json",
                "denial_record": None,
                "network_safety_report": None,
            },
            "canonical_persistence_attempted": False,
        },
        denial_record=None,
        network_safety_report=None,
    )
    load_calls = {"count": 0}
    validate_calls = {"count": 0}
    ingest_calls = {"count": 0}

    def fake_load_execution_artifacts(target: Path):
        load_calls["count"] += 1
        assert target == execution_run_dir
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
            module.EXIT_EXECUTION_PASS,
        )

    def fake_run_command(*args, **kwargs):
        assert kwargs.get("timeout") == 222.0
        command = args[0]
        assert "--suppress-execution-record-stdout" in command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_load_handoff_adapter_path(_: Path) -> Path:
        return tmp_path / "adapter.json"

    def fake_resolve_path(raw_path: str | Path, *, base: Path | None = None) -> Path:
        return Path(raw_path)

    def fake_ingest(*args, **kwargs):
        ingest_calls["count"] += 1
        assert args[1] == fake_receipt.execution_record
        assert kwargs["paths"] == fake_receipt.paths
        assert kwargs["input_hashes"] == fake_receipt.input_hashes
        assert kwargs["capture_events"] is None
        assert kwargs["extraction_records"] is None
        return {"status": "completed", "counts": {}}

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "run_command", fake_run_command)
    monkeypatch.setattr(module, "load_handoff_adapter_path", fake_load_handoff_adapter_path)
    monkeypatch.setattr(module, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(module, "load_execution_artifacts", fake_load_execution_artifacts)
    monkeypatch.setattr(
        module, "validate_execution_artifact_receipt", fake_validate_execution_artifact_receipt
    )
    monkeypatch.setattr(
        module.canonical_ingest,
        "load_validated_execution_artifacts",
        lambda *_: pytest.fail("unexpected execution reload"),
    )
    monkeypatch.setattr(module.canonical_ingest, "ingest_execution_artifacts", fake_ingest)
    monkeypatch.setattr(module.canonical_store, "connect_canonical_store", lambda _: FakeConn())

    args = SimpleNamespace(
        allow_network=False,
        execution_run_fixture=None,
        source_handoff="handoff.json",
        mode="live",
        degraded_spool=False,
        spool_dir=None,
        command_timeout_seconds=222.0,
    )
    manifest = {
        "run_id": "cycle-832",
        "started_at": "2026-06-03T12:34:56Z",
        "warnings": [],
        "stages": [],
        "canonical_db": {"mutated": False},
    }

    acquired_run_dir, receipt = module.acquisition_stage(
        args=args, manifest=manifest, run_dir=run_dir
    )
    assert acquired_run_dir == execution_run_dir
    assert receipt is fake_receipt
    assert load_calls["count"] == 1
    assert validate_calls["count"] == 1

    result = module.execution_ingest_stage(
        args=args,
        manifest=manifest,
        db_path=db_path,
        execution_run_dir=acquired_run_dir,
        execution_artifacts=receipt,
        run_dir=run_dir,
    )

    assert ingest_calls["count"] == 1
    assert result["status"] == "completed"
    assert load_calls["count"] == 1


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


def test_topic_cycle_manifest_write_is_atomic_and_hashes_written_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()

    manifest_path = tmp_path / "topic-cycle-run.json"
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("placeholder", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "topic-cycle-run.v1",
                "run_id": "cycle-atomic",
                "status": "completed",
                "mode": "local",
                "workspace": {"workspace_id": "workspace:atomic"},
                "canonical_db": {"path": str(tmp_path / "canonical.sqlite"), "mutated": False},
                "stages": [],
                "cycle_evidence_ledger": {"status": "pending"},
                "run_dir": str(tmp_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    atomic_calls: list[tuple[Path, dict[str, object]]] = []

    def fake_atomic_write_json(path: Path, payload: dict[str, object]) -> None:
        atomic_calls.append(
            (
                path,
                json.loads(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                ),
            )
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    hash_calls: list[Path] = []

    def fake_hash_file(path: Path) -> str:
        hash_calls.append(path)
        return "manifest-hash"

    class FakeConn:
        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def close(self) -> None:
            return None

    def fake_connect_canonical_store(db_path: Path) -> FakeConn:
        return FakeConn()

    def fake_record_topic_cycle_manifest(
        conn: object,
        *,
        manifest: dict[str, object],
        manifest_path: Path,
        manifest_hash: str,
        canonical_db_ref: str,
    ) -> str:
        assert conn is not None
        assert manifest_path == tmp_path / "topic-cycle-run.json"
        assert manifest_hash == "manifest-hash"
        assert canonical_db_ref == str(tmp_path / "canonical.sqlite")
        assert manifest["run_id"] == "cycle-atomic"
        return "cycle:recorded"

    monkeypatch.setattr(module, "atomic_write_json", fake_atomic_write_json)
    monkeypatch.setattr(module, "hash_file", fake_hash_file)
    monkeypatch.setattr(
        module.canonical_store, "connect_canonical_store", fake_connect_canonical_store
    )
    monkeypatch.setattr(
        module.cycle_evidence_ledger,
        "record_topic_cycle_manifest",
        fake_record_topic_cycle_manifest,
    )

    manifest = {
        "schema_version": "topic-cycle-run.v1",
        "run_id": "cycle-atomic",
        "status": "completed",
        "mode": "local",
        "workspace": {"workspace_id": "workspace:atomic"},
        "canonical_db": {"path": str(tmp_path / "canonical.sqlite"), "mutated": False},
        "stages": [],
        "cycle_evidence_ledger": {"status": "recorded"},
        "run_dir": str(tmp_path),
    }
    args = SimpleNamespace(mode="local", degraded_spool=False, spool_dir=None)

    module.write_json(manifest_path, manifest)
    assert len(atomic_calls) == 1
    assert atomic_calls[0][0] == manifest_path
    assert atomic_calls[0][1]["cycle_evidence_ledger"]["status"] == "recorded"  # type: ignore[index]

    module.record_cycle_evidence_from_manifest(
        args=args,
        manifest=manifest,
        manifest_path=manifest_path,
        db_path=db_path,
    )

    assert hash_calls == [manifest_path]
    assert len(atomic_calls) == 1
    assert manifest["cycle_evidence_ledger"]["status"] == "recorded"  # type: ignore[index]
    assert manifest["cycle_evidence_ledger"]["cycle_event_id"] == "cycle:recorded"  # type: ignore[index]
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["cycle_evidence_ledger"]["status"] == "recorded"
    assert "cycle_event_id" not in persisted["cycle_evidence_ledger"]  # type: ignore[operator]


def test_topic_cycle_missing_db_spool_hashes_manifest_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load_run_topic_cycle_module()

    manifest_path = tmp_path / "topic-cycle-run.json"
    db_path = tmp_path / "missing.sqlite"
    manifest_path.write_text(
        json.dumps(
                {
                    "schema_version": "topic-cycle-run.v1",
                    "run_id": "cycle-spool",
                    "status": "completed",
                    "mode": "local",
                    "cycle_event_id": "cycle:spool",
                    "workspace": {"workspace_id": "workspace:spool"},
                    "canonical_db": {"path": str(db_path), "mutated": False},
                    "stages": [],
                    "cycle_evidence_ledger": {"status": "recorded"},
                    "run_dir": str(tmp_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    hash_calls: list[Path] = []

    def fake_hash_file(path: Path) -> str:
        hash_calls.append(path)
        return "manifest-hash"

    class FakeConn:
        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "hash_file", fake_hash_file)
    monkeypatch.setattr(module.canonical_store, "connect_canonical_store", lambda _: FakeConn())

    written_records: list[dict[str, object]] = []

    def fake_build_spool_record(**kwargs: object) -> dict[str, object]:
        written_records.append(dict(kwargs))
        return {"spool_record_id": "spool:manifest", "operation_kind": kwargs["operation_kind"]}

    monkeypatch.setattr(
        module.canonical_write_spool,
        "build_spool_record",
        fake_build_spool_record,
    )
    monkeypatch.setattr(
        module.canonical_write_spool,
        "write_spool_record",
        lambda spool_dir, record: spool_dir / "cycle-evidence-write.json",
    )

    manifest = {
        "schema_version": "topic-cycle-run.v1",
        "run_id": "cycle-spool",
        "status": "completed",
        "mode": "local",
        "cycle_event_id": "cycle:spool",
        "workspace": {"workspace_id": "workspace:spool"},
        "canonical_db": {"path": str(db_path), "mutated": False},
        "stages": [],
        "cycle_evidence_ledger": {"status": "recorded"},
        "run_dir": str(tmp_path),
    }
    args = SimpleNamespace(mode="local", degraded_spool=True, spool_dir=None)

    module.record_cycle_evidence_from_manifest(
        args=args,
        manifest=manifest,
        manifest_path=manifest_path,
        db_path=db_path,
    )

    assert hash_calls == [manifest_path]
    assert len(written_records) == 1
    record = written_records[0]
    assert record["operation_kind"] == "cycle_evidence_write"
    assert record["operation_input"]["artifact_refs"][0]["artifact_hash"] == "manifest-hash"  # type: ignore[index]
    assert record["replay_recipe"]["manifest_hash"] == "manifest-hash"  # type: ignore[index]


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
