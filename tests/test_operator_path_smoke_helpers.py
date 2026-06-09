from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scripts import operator_path_smoke

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_smoke_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    scripts_dir = repo / "tools" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "bootstrap_topic_workspace.py",
        "build_workspace_overview_view.py",
        "build_subject_detail_view.py",
        "build_source_intake_status_view.py",
        "build_review_queue_view.py",
        "resolve_gather_domain_pack.py",
        "local_doctor.py",
        "build_operator_dashboard.py",
    ):
        (scripts_dir / name).write_text("# stub\n", encoding="utf-8")
    return repo


def _make_canonical_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT
            );
            CREATE TABLE source_claim (
              source_claim_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT
            );
            CREATE TABLE extraction_detected_entity (
              detected_entity_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT
            );
            CREATE TABLE source_relationship (
              source_relationship_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT
            );
            CREATE TABLE source_access (
              source_access_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT,
              source_lead_id TEXT,
              work_id INTEGER
            );
            CREATE TABLE provenance_event (
              provenance_event_id INTEGER PRIMARY KEY,
              provenance_event_key_v1 TEXT,
              note_text TEXT
            );
            INSERT INTO work VALUES (1, 'event-1');
            INSERT INTO source_claim VALUES (1, 'event-1');
            INSERT INTO source_claim VALUES (2, 'event-1');
            INSERT INTO source_claim VALUES (3, 'event-1');
            INSERT INTO source_relationship VALUES (1, 'event-1');
            INSERT INTO source_access VALUES (1, 'event-1', 'source-lead:hash:1', 1);
            INSERT INTO source_access VALUES (2, 'event-1', 'source-lead:hash:2', 1);
            INSERT INTO provenance_event VALUES (1, 'event-1', '{"artifact_hash":"hash"}');
            CREATE TABLE capture_event (
              capture_event_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT
            );
            CREATE TABLE extraction_record (
              extraction_id INTEGER PRIMARY KEY,
              provenance_event_ref TEXT
            );
            INSERT INTO capture_event VALUES (1, 'event-1');
            INSERT INTO extraction_record VALUES (1, 'event-1');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_operator_path_smoke_core_helpers_cover_parsing_and_command_wrappers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_smoke_repo(tmp_path)
    parsed = operator_path_smoke.parse_args(
        [
            "--repo-root",
            str(repo),
            "--dry-run",
            "--json",
            "--run-id",
            "fixture-smoke",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )
    assert parsed.json is True
    assert parsed.format == "text"
    assert operator_path_smoke.normalize_timestamp(" 2026-06-03T12:00:00Z ") == "2026-06-03T12:00:00Z"
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="blank string"):
        operator_path_smoke.normalize_timestamp("   ")
    assert operator_path_smoke.resolve_repo_root(str(repo)) == repo.resolve()

    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="repo root not found"):
        operator_path_smoke.resolve_repo_root(str(tmp_path / "missing"))

    not_dir = tmp_path / "not-a-dir"
    not_dir.write_text("x", encoding="utf-8")
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="not a directory"):
        operator_path_smoke.resolve_repo_root(str(not_dir))

    with operator_path_smoke.managed_workspace(None, run_id="fixture", keep=False) as temp_ws:
        assert temp_ws.exists()
    assert not temp_ws.exists()

    with operator_path_smoke.managed_workspace(None, run_id="fixture", keep=True) as kept_ws:
        assert kept_ws.exists()
    assert kept_ws.exists()

    explicit_ws = tmp_path / "explicit-workspace"
    with operator_path_smoke.managed_workspace(str(explicit_ws), run_id="fixture", keep=False) as ws:
        assert ws == explicit_ws.resolve()
        assert ws.exists()
    assert explicit_ws.exists()

    dynamic = tmp_path / "dynamic_module.py"
    dynamic.write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Payload:\n"
        "    value: int\n",
        encoding="utf-8",
    )
    module = operator_path_smoke.load_module(dynamic, "operator_path_smoke_dynamic")
    assert module.Payload(7).value == 7
    assert sys.modules["operator_path_smoke_dynamic"] is module

    monkeypatch.setattr(operator_path_smoke.importlib.util, "spec_from_file_location", lambda *a, **k: None)
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="could not load module spec"):
        operator_path_smoke.load_module(dynamic, "operator_path_smoke_missing")

    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="not found"):
        operator_path_smoke.require_file(tmp_path / "missing.txt", label="missing file")
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="not a file"):
        operator_path_smoke.require_file(tmp_path, label="directory")

    completed = subprocess.CompletedProcess(["python3"], 0, stdout='{"alpha": 1}\n', stderr="")
    monkeypatch.setattr(operator_path_smoke.subprocess, "run", lambda *a, **k: completed)
    assert operator_path_smoke.run_command(["python3", "--help"], cwd=repo).stdout == '{"alpha": 1}\n'
    assert operator_path_smoke.checked_command(["python3", "--help"], cwd=repo, label="help").returncode == 0

    failing = subprocess.CompletedProcess(["python3"], 1, stdout="", stderr="boom")
    monkeypatch.setattr(operator_path_smoke.subprocess, "run", lambda *a, **k: failing)
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="help failed"):
        operator_path_smoke.checked_command(["python3", "--help"], cwd=repo, label="help")

    assert operator_path_smoke.command_text(["python3", "tool.py", "--flag", "value with space"])
    good = subprocess.CompletedProcess(["python3"], 0, stdout='{"alpha": 1}\n', stderr="")
    assert operator_path_smoke.parse_json_stdout(good, label="good") == {"alpha": 1}
    bad = subprocess.CompletedProcess(["python3"], 0, stdout="[]\n", stderr="")
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="JSON object"):
        operator_path_smoke.parse_json_stdout(bad, label="bad")
    invalid = subprocess.CompletedProcess(["python3"], 0, stdout="{", stderr="")
    with pytest.raises(operator_path_smoke.OperatorPathSmokeError, match="valid JSON"):
        operator_path_smoke.parse_json_stdout(invalid, label="bad-json")

    ctx = operator_path_smoke.SmokeContext(
        repo_root=repo.resolve(),
        workspace_path=tmp_path / "workspace",
        dry_run=True,
        run_id="fixture",
        timestamp="2026-06-03T00:00:00Z",
        registry_path=tmp_path / "workspace" / "topic_workspaces.local.json",
        topic_workspace_root=tmp_path / "workspace" / "topic-workspace",
    )
    report = operator_path_smoke.build_report(
        ctx,
        [
            operator_path_smoke.SmokeCheck(
                name="a",
                status="passed",
                surface="surface.a",
                command="cmd-a",
                message="ok",
            ),
            operator_path_smoke.SmokeCheck(
                name="b",
                status="failed",
                surface="surface.b",
                error_message="boom",
            ),
        ],
    )
    assert report["status"] == "failed"
    rendered = operator_path_smoke.render_text(report)
    assert "summary.passed=1" in rendered
    assert "summary.failed=1" in rendered
    assert "check[1].error_message=boom" in rendered

    out_path = tmp_path / "body.txt"
    operator_path_smoke.write_body(out_path, rendered)
    assert out_path.read_text(encoding="utf-8") == rendered

    output = capsys.readouterr()
    assert output.out == ""


def test_operator_path_smoke_smoke_checks_cover_happy_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_smoke_repo(tmp_path)
    workspace = tmp_path / "workspace"
    ctx = operator_path_smoke.SmokeContext(
        repo_root=repo.resolve(),
        workspace_path=workspace,
        dry_run=True,
        run_id="fixture-smoke",
        timestamp="2026-06-03T12:00:00Z",
        registry_path=workspace / "topic_workspaces.local.json",
        topic_workspace_root=workspace / "topic-workspace",
    )

    def fake_load_module(path: Path, module_name: str) -> object:
        class _ResolutionError(Exception):
            pass

        def _resolve_subject_runtime(subject_id: str, workspace: str) -> dict[str, object]:
            return {
                "schema_version": "subject-runtime-resolution.v1",
                "subject": {
                    "subject_id": subject_id,
                    "display_name": "Smoke Subject",
                    "domain_pack": "general.v1",
                    "enabled_facets": ["sources", "people"],
                    "query_families": ["web_search"],
                },
            }

        return type(
            "FakeRuntimeModule",
            (),
            {
                "ResolutionError": _ResolutionError,
                "resolve_subject_runtime": staticmethod(_resolve_subject_runtime),
            },
        )()

    def fake_checked_command(command: list[str], *, cwd: Path, label: str) -> subprocess.CompletedProcess[str]:
        if label.endswith("--help"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if label == "bootstrap dry-run":
            payload = {"planned_created_paths": [str(ctx.topic_workspace_root / "placeholder")]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if label == "bootstrap apply":
            manifest_path = ctx.topic_workspace_root / ".indexer" / "subject_manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "subject-manifest.v1",
                        "subject_id": "alpha_subject",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"subject_manifest_path": str(manifest_path)}),
                "",
            )
        if label == "domain pack resolution":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "schema_version": "gather-domain-pack-resolution.v1",
                        "selected_facets": ["sources", "people"],
                    }
                ),
                "",
            )
        if label == "workspace overview":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "schema_version": "workspace-overview.v1",
                        "counts": {"total_workspaces": 1, "workspace_root_ok": 1},
                    }
                ),
                "",
            )
        if label == "subject detail build":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "schema_version": "subject-detail.v1",
                        "status": {"domain_pack_status": "ok"},
                    }
                ),
                "",
            )
        if label == "source intake status build":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "schema_version": "source-intake-status.v1",
                        "counts": {"total_adapters": 1, "contract_fail": 0},
                    }
                ),
                "",
            )
        if label == "canonical store init":
            _make_canonical_db(workspace / "canonical.sqlite")
            ctx.canonical_db_path = workspace / "canonical.sqlite"
            return subprocess.CompletedProcess(command, 0, "", "")
        if label == "topic cycle":
            payload = {
                "schema_version": "topic-cycle-run.v1",
                "status": "completed",
                "canonical_db": {"mutated": True},
                "stages": [
                    {"name": "ingest_candidate_batch", "status": "passed"},
                    {"name": "ingest_execution_artifacts", "status": "passed"},
                ],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if label == "review queue build":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "schema_version": "review-queue.v1",
                        "counts": {"total_items": 1},
                    }
                ),
                "",
            )
        if label == "local doctor":
            report_path = ctx.doctor_report_path
            assert report_path is not None
            report_path.write_text(
                json.dumps(
                    {
                        "schema_version": "local-doctor-report.v1",
                        "read_only": True,
                        "auto_fix_performed": False,
                        "summary": {"status": "warn"},
                        "canonical_store": {"status": "populated"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True}), "")
        if label == "operator dashboard build":
            dashboard_path = ctx.dashboard_output_path
            assert dashboard_path is not None
            dashboard_path.write_text("<html><title>Summa Operator Health</title></html>", encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"schema_version": "operator-dashboard-build-report.v1"}),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(operator_path_smoke, "load_module", fake_load_module)
    monkeypatch.setattr(operator_path_smoke, "checked_command", fake_checked_command)

    help_msg = operator_path_smoke.smoke_help_surfaces(ctx)
    assert "verified --help" in help_msg[1]

    dry_manifest, dry_msg = operator_path_smoke.smoke_bootstrap_dry_run(ctx)
    assert dry_manifest is None
    assert "planned" in dry_msg
    assert not ctx.topic_workspace_root.exists()

    manifest_path, apply_msg = operator_path_smoke.smoke_bootstrap_apply(ctx)
    assert manifest_path == str(ctx.topic_workspace_root / ".indexer" / "subject_manifest.json")
    assert "bootstrapped workspace" in apply_msg

    runtime_manifest, runtime_msg = operator_path_smoke.smoke_resolve_subject_runtime(ctx)
    assert runtime_manifest == manifest_path
    assert "resolved runtime" in runtime_msg

    domain_pack_manifest, domain_msg = operator_path_smoke.smoke_resolve_domain_pack(ctx)
    assert domain_pack_manifest is None
    assert "resolved" in domain_msg

    overview_artifact, overview_msg = operator_path_smoke.smoke_workspace_overview(ctx)
    assert overview_artifact == str(ctx.registry_path)
    assert "workspace overview" in overview_msg

    detail_manifest, detail_msg = operator_path_smoke.smoke_subject_detail(ctx)
    assert detail_manifest == manifest_path
    assert "subject detail view" in detail_msg

    intake_artifact, intake_msg = operator_path_smoke.smoke_source_intake(ctx)
    assert intake_artifact == str(operator_path_smoke.SOURCE_ADAPTER_FIXTURE)
    assert "source intake status" in intake_msg

    canonical_artifact, canonical_msg = operator_path_smoke.smoke_init_canonical_store(ctx)
    assert canonical_artifact == str(workspace / "canonical.sqlite")
    assert "initialized canonical store" in canonical_msg

    cycle_artifact, cycle_msg = operator_path_smoke.smoke_run_topic_cycle(ctx)
    assert cycle_artifact == str(workspace / "topic-cycle" / ctx.run_id / "topic-cycle-run.json")
    assert "real topic-cycle path" in cycle_msg

    family_artifact, family_msg = operator_path_smoke.smoke_canonical_family_counts(ctx)
    assert family_artifact == str(workspace / "canonical.sqlite")
    assert "canonical family counts" in family_msg

    review_artifact, review_msg = operator_path_smoke.smoke_review_queue(ctx)
    assert review_artifact == str(workspace / "canonical.sqlite")
    assert "review queue view" in review_msg

    doctor_artifact, doctor_msg = operator_path_smoke.smoke_local_doctor(ctx)
    assert doctor_artifact == str(workspace / "doctor-report.json")
    assert "local doctor completed" in doctor_msg

    dashboard_artifact, dashboard_msg = operator_path_smoke.smoke_operator_dashboard(ctx)
    assert dashboard_artifact == str(workspace / "operator-dashboard.html")
    assert "operator dashboard HTML" in dashboard_msg

    ctx.canonical_db_path = workspace / "canonical.sqlite"
    conn = sqlite3.connect(ctx.canonical_db_path)
    try:
        db_counts = {
            "work": int(conn.execute("SELECT COUNT(*) FROM work").fetchone()[0]),
            "source_claim": int(conn.execute("SELECT COUNT(*) FROM source_claim").fetchone()[0]),
            "source_access": int(conn.execute("SELECT COUNT(*) FROM source_access").fetchone()[0]),
            "source_relationship": int(
                conn.execute("SELECT COUNT(*) FROM source_relationship").fetchone()[0]
            ),
            "capture_event": int(conn.execute("SELECT COUNT(*) FROM capture_event").fetchone()[0]),
            "extraction_record": int(
                conn.execute("SELECT COUNT(*) FROM extraction_record").fetchone()[0]
            ),
            "provenance_event": int(conn.execute("SELECT COUNT(*) FROM provenance_event").fetchone()[0]),
        }
    finally:
        conn.close()
    assert all(count > 0 for count in db_counts.values())


def test_operator_path_smoke_execute_check_run_smoke_and_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_smoke_repo(tmp_path)
    workspace = tmp_path / "workspace"

    checks: list[operator_path_smoke.SmokeCheck] = []

    assert operator_path_smoke.execute_check(
        checks,
        name="success",
        surface="surface.success",
        command="cmd-success",
        action=lambda: (str(tmp_path / "artifact.txt"), "done"),
    )
    assert checks[-1].status == "passed"

    assert not operator_path_smoke.execute_check(
        checks,
        name="failure",
        surface="surface.failure",
        command="cmd-failure",
        action=lambda: (_ for _ in ()).throw(operator_path_smoke.OperatorPathSmokeError("boom")),
    )
    assert checks[-1].status == "failed"
    assert checks[-1].error_message == "boom"

    def fake_run_smoke(args: argparse.Namespace) -> tuple[dict[str, object], int]:
        return (
            {
                "schema_version": operator_path_smoke.SMOKE_SCHEMA_VERSION,
                "status": "passed",
                "repo_root": str(repo.resolve()),
                "workspace_path": str(workspace),
                "dry_run": True,
                "run_id": "fixture-smoke",
                "timestamp": "2026-06-03T12:00:00Z",
                "network_access_attempted": False,
                "llm_invoked": False,
                "checks": [],
                "summary": {"passed": 1, "failed": 0, "skipped": 0},
            },
            0,
        )

    monkeypatch.setattr(operator_path_smoke, "run_smoke", fake_run_smoke)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "operator_path_smoke.py",
            "--repo-root",
            str(repo),
            "--workspace",
            str(workspace),
            "--dry-run",
            "--json",
            "--run-id",
            "fixture-smoke",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ],
    )
    assert operator_path_smoke.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"

    def boom_run_smoke(args: argparse.Namespace) -> tuple[dict[str, object], int]:
        raise operator_path_smoke.OperatorPathSmokeError("boom")

    monkeypatch.setattr(operator_path_smoke, "run_smoke", boom_run_smoke)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "operator_path_smoke.py",
            "--repo-root",
            str(repo),
            "--workspace",
            str(workspace),
            "--dry-run",
            "--json",
            "--run-id",
            "fixture-smoke",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ],
    )
    assert operator_path_smoke.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
