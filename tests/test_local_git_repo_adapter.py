from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "plan_local_git_repo_adapter.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "local_git_repo"


def run_planner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def init_fixture_repo(tmp_path: Path, *, dirty: bool = False, include_remote_url: bool = False) -> Path:
    source_dir = FIXTURE_ROOT
    scenario_dir = tmp_path / "local_git_repo"
    shutil.copytree(source_dir, scenario_dir)
    repo_dir = scenario_dir / "repo"
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Fixture User"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixture commit"], check=True)
    if include_remote_url:
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", "https://example.invalid/repo.git"], check=True)
    if dirty:
        (repo_dir / "tracked.md").write_text("dirty change\n", encoding="utf-8")
        (repo_dir / "untracked.tmp").write_text("untracked\n", encoding="utf-8")
    return scenario_dir


def write_adapter(path: Path, *, local_path: str = "repo", ref: str = "main", repo_url: str | None = None) -> Path:
    adapter_path = path / "source_adapter.json"
    locator = {
        "local_path": local_path,
        "ref": ref,
        "include_globs": ["**/*.md", "**/*.json"],
        "exclude_globs": ["ignored/**"],
    }
    if repo_url is not None:
        locator["repo_url"] = repo_url
    adapter_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_local_git_repo",
                "display_name": "Runtime local git repo",
                "workspace_id": "alpha_subject",
                "description": "Runtime fixture for local git planning.",
                "input_family": "local_git_repo",
                "locator": locator,
                "content_profile": {
                    "content_kinds": ["json", "markdown", "git_history"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "runtime fixture repo",
                    "acquisition_method": "local_clone",
                    "source_description": "Local git repository used for dry-run planning tests.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "tracked_source",
                    "metadata_storage_policy_class": "tracked_source",
                    "rights_posture": "redistributable",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "source_lead",
                    "batch_unit": "per_snapshot",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                        "byte_retention_status",
                        "discard_metadata",
                        "refetchability_status",
                        "transform_lineage",
                        "source_metadata",
                    ],
                    "source_specific_fields": ["git_ref", "git_commit"],
                },
                "transform_lineage": [
                    {
                        "step_id": "inspect",
                        "step_kind": "inspect_local_repo",
                        "description": "Inspect a local git checkout without mutating it.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit one source-lead handoff record for the local checkout.",
                        "deterministic": True,
                        "review_required": True,
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return adapter_path


def test_local_git_repo_plans_clean_checkout_with_commit_metadata(tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, include_remote_url=True)
    adapter_path = write_adapter(scenario_dir)
    repo_dir = scenario_dir / "repo"
    handoff_jsonl = tmp_path / "handoff.jsonl"

    input_paths = sorted(path for path in repo_dir.rglob("*") if ".git" not in path.parts)
    input_paths.append(adapter_path)
    mtimes_before = {path: path.stat().st_mtime_ns for path in input_paths}

    proc = run_planner(["--adapter", str(adapter_path), "--handoff-jsonl", str(handoff_jsonl), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {path: path.stat().st_mtime_ns for path in input_paths} == mtimes_before

    payload = json.loads(proc.stdout)
    expected_commit = git(repo_dir, "rev-parse", "--verify", "main^{commit}").stdout.strip()

    assert payload["schema_version"] == "local-git-repo-plan.v1"
    assert payload["repo_state"] == "clean"
    assert payload["network_access_attempted"] is False
    assert payload["remote_operations_attempted"] is False
    assert payload["resolved_commit"] == expected_commit
    assert payload["inspected_ref"] == "main"
    assert payload["current_branch"] == "main"
    assert payload["candidate_count"] == 2
    assert set(payload["candidate_paths"]) == {"tracked.md", "nested/data.json"}
    assert payload["blocker_count"] == 0
    assert payload["handoff_record_count"] == 1

    record = json.loads(handoff_jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert record["schema_version"] == "source-adapter-handoff.v1"
    assert record["remote_state"] == "local_checkout"
    assert record["preserved"]["refetchability_status"] == "local_replayable"
    assert record["source_specific"] == {"git_commit": expected_commit, "git_ref": "main"}


def test_local_git_repo_reports_dirty_state_clearly(tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, dirty=True)
    adapter_path = write_adapter(scenario_dir)

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["repo_state"] == "dirty"
    assert payload["blockers"] == ["git working tree has local modifications or untracked files"]
    assert payload["handoff_record_count"] == 1


def test_local_git_repo_reports_non_repo_path_clearly(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "scenario"
    scenario_dir.mkdir()
    (scenario_dir / "plain").mkdir()
    adapter_path = write_adapter(scenario_dir, local_path="plain")

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["repo_state"] == "invalid"
    assert payload["blockers"] == [f"local git repo path is not a git repository: {(scenario_dir / 'plain').resolve()}"]
    assert payload["handoff_record_count"] == 0


def test_local_git_repo_refuses_remote_clone_behavior(tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, include_remote_url=True)
    adapter_path = write_adapter(scenario_dir, repo_url="https://example.invalid/clone-target.git")

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert "remote clone behavior is not allowed; planner inspects already-local checkouts only" in payload["blockers"]
