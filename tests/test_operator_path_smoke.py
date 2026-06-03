from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_TOOL = REPO_ROOT / "tools" / "scripts" / "operator_path_smoke.py"
WRAPPER = REPO_ROOT / "tools" / "scripts" / "Index_Operator_Path_Smoke.sh"


def run_python(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PY_TOOL), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_wrapper(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def git_status() -> str:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
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
    assert "ingest_candidate_batch" in checks
    assert "ingest_execution_artifacts" in checks
    assert "canonical_family_counts" in checks
    assert "build_operator_dashboard" in checks
    assert checks["bootstrap_workspace_apply"]["status"] == "passed"
    assert checks["build_workspace_overview"]["status"] == "passed"
    assert checks["ingest_candidate_batch"]["status"] == "passed"
    assert checks["ingest_execution_artifacts"]["status"] == "passed"

    expected_paths = [
        workspace / "topic_workspaces.local.json",
        workspace / "topic-workspace" / ".indexer" / "subject_manifest.json",
        workspace / "canonical.sqlite",
        workspace / "doctor-report.json",
        workspace / "operator-dashboard.html",
    ]
    for path in expected_paths:
        assert path.exists(), path


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


def test_operator_path_smoke_wrapper_points_to_existing_python_target() -> None:
    body = WRAPPER.read_text(encoding="utf-8")

    assert 'operator_path_smoke.py' in body
    assert PY_TOOL.exists()
