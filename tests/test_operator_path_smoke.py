from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


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

    expected_paths = [
        workspace / "topic_workspaces.local.json",
        workspace / "topic-workspace" / ".indexer" / "subject_manifest.json",
        workspace / "canonical.sqlite",
        workspace / "doctor-report.json",
        workspace / "operator-dashboard.html",
    ]
    for path in expected_paths:
        assert path.exists(), path

    doctor_report = json.loads((workspace / "doctor-report.json").read_text(encoding="utf-8"))
    assert doctor_report["canonical_store"]["status"] == "populated"
    assert doctor_report["canonical_store"]["total_rows"] > 0


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


def test_operator_path_smoke_wrapper_points_to_existing_python_target() -> None:
    body = WRAPPER.read_text(encoding="utf-8")

    assert 'operator_path_smoke.py' in body
    assert PY_TOOL.exists()


def test_operator_path_smoke_uses_real_topic_cycle_path() -> None:
    body = PY_TOOL.read_text(encoding="utf-8")

    assert "run_topic_cycle.py" in body
    assert "smoke_run_topic_cycle" in body
