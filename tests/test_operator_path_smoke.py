from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tools.scripts import operator_path_smoke


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_TOOL = REPO_ROOT / "tools" / "scripts" / "operator_path_smoke.py"
WRAPPER = REPO_ROOT / "tools" / "scripts" / "Index_Operator_Path_Smoke.sh"


def run_python(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    if cwd is None:
        cwd = REPO_ROOT
    return subprocess.run(
        [sys.executable, str(PY_TOOL), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def run_wrapper(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    if cwd is None:
        cwd = REPO_ROOT
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def git_status(cwd: Path | None = None) -> str:
    if cwd is None:
        cwd = REPO_ROOT
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_operator_path_smoke_python_help() -> None:
    proc = run_python(["--help"])

    assert proc.returncode == 0, proc.stderr
    assert "operator-path smoke" in proc.stdout
    assert "--dry-run" in proc.stdout


def test_operator_path_smoke_wrapper_help() -> None:
    proc = run_wrapper(["--help"])

    assert proc.returncode == 0, proc.stderr
    assert "operator-path smoke" in proc.stdout
    assert "--workspace" in proc.stdout


def test_operator_path_smoke_wrapper_dry_run_json_passes_without_repo_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "smoke-workspace"
    before = git_status()

    proc = run_wrapper(
        [
            "--dry-run",
            "--json",
            "--workspace",
            str(workspace),
            "--run-id",
            "fixture-smoke",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )

    after = git_status()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert before == after

    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "operator-path-smoke.v1"
    assert payload["status"] == "passed"
    assert payload["dry_run"] is True
    assert payload["network_access_attempted"] is False
    assert payload["llm_invoked"] is False
    assert payload["timestamp"] == "2026-06-03T12:00:00Z"
    assert payload["summary"]["failed"] == 0
    assert payload["summary"]["passed"] >= 5

    checks = {check["name"]: check for check in payload["checks"]}
    assert "operator_script_help" in checks
    assert "bootstrap_workspace_apply" in checks
    assert "build_workspace_overview" in checks
    assert "run_topic_cycle" in checks
    assert "canonical_family_counts" in checks
    assert "build_operator_dashboard" in checks
    assert checks["bootstrap_workspace_apply"]["status"] == "passed"
    assert checks["build_workspace_overview"]["status"] == "passed"
    assert checks["run_topic_cycle"]["status"] == "passed"
    assert checks["bootstrap_workspace_apply"]["artifact_path"] == str(
        workspace / "topic-workspace" / ".indexer" / "subject_manifest.json"
    )
    assert checks["run_topic_cycle"]["artifact_path"] == str(
        workspace / "topic-cycle" / "fixture-smoke" / "topic-cycle-run.json"
    )
    assert checks["canonical_family_counts"]["artifact_path"] == str(workspace / "canonical.sqlite")
    assert checks["run_local_doctor"]["artifact_path"] == str(workspace / "doctor-report.json")
    assert checks["build_operator_dashboard"]["artifact_path"] == str(
        workspace / "operator-dashboard.html"
    )

    expected_paths = [
        workspace / "topic_workspaces.local.json",
        workspace / "topic-workspace" / ".indexer" / "subject_manifest.json",
        workspace / "canonical.sqlite",
        workspace / "doctor-report.json",
        workspace / "operator-dashboard.html",
    ]
    for path in expected_paths:
        assert path.exists(), path

    subject_manifest = json.loads(
        (workspace / "topic-workspace" / ".indexer" / "subject_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert subject_manifest["schema_version"] == "subject-manifest.v1"
    assert subject_manifest["subject_id"]

    topic_cycle_path = workspace / "topic-cycle" / "fixture-smoke" / "topic-cycle-run.json"
    topic_cycle = json.loads(topic_cycle_path.read_text(encoding="utf-8"))
    assert topic_cycle["schema_version"] == "topic-cycle-run.v1"
    assert topic_cycle["status"] == "completed"
    assert topic_cycle["canonical_db"]["mutated"] is True
    assert topic_cycle["canonical_db"]["final_summary"]["status"] == "populated"
    assert topic_cycle["canonical_db"]["final_summary"]["total_rows"] > 0
    assert topic_cycle["canonical_db"]["final_summary"]["family_counts"]
    stage_statuses = {stage["name"]: stage["status"] for stage in topic_cycle["stages"]}
    assert stage_statuses["ingest_candidate_batch"] == "passed"
    assert stage_statuses["ingest_execution_artifacts"] == "passed"

    conn = sqlite3.connect(workspace / "canonical.sqlite")
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
            "cycle_event": int(conn.execute("SELECT COUNT(*) FROM cycle_event").fetchone()[0]),
            "cycle_stage_event": int(
                conn.execute("SELECT COUNT(*) FROM cycle_stage_event").fetchone()[0]
            ),
        }
    finally:
        conn.close()

    assert all(count > 0 for count in db_counts.values())
    assert db_counts == {
        "work": 1,
        "source_claim": 3,
        "source_access": 2,
        "source_relationship": 1,
        "capture_event": 1,
        "extraction_record": 1,
        "provenance_event": 2,
        "cycle_event": 1,
        "cycle_stage_event": 12,
    }

    final_family_counts = topic_cycle["canonical_db"]["final_summary"]["family_counts"]

    doctor_report = json.loads((workspace / "doctor-report.json").read_text(encoding="utf-8"))
    assert doctor_report["canonical_store"]["status"] == "populated"
    assert doctor_report["canonical_store"]["total_rows"] > 0
    assert doctor_report["canonical_store"]["family_counts"] == final_family_counts
    for table_name, count in db_counts.items():
        assert doctor_report["canonical_store"]["table_counts"][table_name] == count

    dashboard_body = (workspace / "operator-dashboard.html").read_text(encoding="utf-8")
    assert "Summa Operator Health" in dashboard_body
    assert "canonical store" in dashboard_body.lower()


def test_operator_path_smoke_wrapper_handles_workspace_paths_with_spaces(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"

    proc = run_wrapper(
        [
            "--dry-run",
            "--json",
            "--workspace",
            str(workspace),
            "--run-id",
            "fixture-smoke-spaces",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "passed"
    assert payload["dry_run"] is True
    assert workspace.exists()
    assert workspace.is_dir()
    checks = {check["name"] for check in payload["checks"]}
    assert "bootstrap_workspace_apply" in checks


def test_operator_path_smoke_json_failure_for_invalid_workspace_path(tmp_path: Path) -> None:
    invalid_workspace = tmp_path / "not-a-directory"
    invalid_workspace.write_text("fixture", encoding="utf-8")

    proc = run_python(
        [
            "--dry-run",
            "--json",
            "--workspace",
            str(invalid_workspace),
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["status"] == "failed"
    assert payload["dry_run"] is True
    assert payload["summary"] == {"passed": 0, "failed": 1, "skipped": 0}
    assert payload["checks"][0]["name"] == "smoke_setup"
    assert "workspace path is not a directory" in payload["checks"][0]["error_message"]


def test_operator_path_smoke_keeps_repo_root_intact_when_workspace_is_tmp(tmp_path: Path) -> None:
    workspace = tmp_path / "smoke-workspace"
    root_drift_path = REPO_ROOT / "._operator-path-smoke-root-drift.txt"
    root_drift_path.write_text("root probe\n", encoding="utf-8")
    try:
        before = git_status()
        proc = run_wrapper(
            [
                "--dry-run",
                "--json",
                "--workspace",
                str(workspace),
                "--run-id",
                "fixture-smoke",
                "--timestamp",
                "2026-06-03T12:00:00Z",
            ],
            cwd=tmp_path,
        )
        after = git_status()

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout
        assert before == after

        payload = json.loads(proc.stdout)
        assert payload["schema_version"] == "operator-path-smoke.v1"
        assert payload["status"] == "passed"
        assert payload["summary"]["failed"] == 0
    finally:
        root_drift_path.unlink(missing_ok=True)


def test_operator_path_smoke_wrapper_invokes_the_python_target(tmp_path: Path) -> None:
    capture_path = tmp_path / "python-argv.txt"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim_path = shim_dir / "python3"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$SUMMA_WRAPPER_CAPTURE"\n'
        'exec "$REAL_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    shim_path.chmod(0o755)

    env = {
        **os.environ,
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
        "REAL_PYTHON": sys.executable,
        "SUMMA_WRAPPER_CAPTURE": str(capture_path),
    }
    proc = subprocess.run(
        ["bash", str(WRAPPER), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    captured_args = capture_path.read_text(encoding="utf-8").splitlines()
    assert Path(captured_args[0]) == PY_TOOL
    assert captured_args[1:] == ["--help"]


def test_operator_path_smoke_run_topic_cycle_uses_the_real_script_path(
    tmp_path: Path, monkeypatch: object
) -> None:
    ctx = operator_path_smoke.SmokeContext(
        repo_root=REPO_ROOT,
        workspace_path=tmp_path / "workspace",
        dry_run=True,
        run_id="fixture-smoke",
        timestamp="2026-06-03T12:00:00Z",
        registry_path=tmp_path / "workspace" / "topic_workspaces.local.json",
        topic_workspace_root=tmp_path / "workspace" / "topic-workspace",
        subject_manifest_path=(
            tmp_path / "workspace" / "topic-workspace" / ".indexer" / "subject_manifest.json"
        ),
        subject_id="alpha_subject",
        canonical_db_path=tmp_path / "workspace" / "canonical.sqlite",
    )
    ctx.subject_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.subject_manifest_path.write_text(
        json.dumps({"schema_version": "subject-manifest.v1", "subject_id": "alpha_subject"}) + "\n",
        encoding="utf-8",
    )

    captured: dict[str, list[str]] = {}

    def fake_checked_command(command: list[str], *, cwd: Path, label: str) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
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

    monkeypatch.setattr(operator_path_smoke, "checked_command", fake_checked_command)

    artifact_path, summary = operator_path_smoke.smoke_run_topic_cycle(ctx)

    assert artifact_path == str(ctx.workspace_path / "topic-cycle" / ctx.run_id / "topic-cycle-run.json")
    assert "real topic-cycle path" in summary
    assert captured["command"][1] == str(REPO_ROOT / "tools" / "scripts" / "run_topic_cycle.py")
    assert captured["command"][captured["command"].index("--run-id") + 1] == "fixture-smoke"
