from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.scripts import plan_local_git_repo_adapter  # noqa: E402, I001
from tools.scripts import execute_source_adapter as source_executor  # noqa: E402, I001

SCRIPT = REPO_ROOT / "tools" / "scripts" / "plan_local_git_repo_adapter.py"
EXECUTOR = REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "local_git_repo"


def run_planner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_executor(*, handoff: Path, output: Path, adapter_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(EXECUTOR),
            "--handoff",
            str(handoff),
            "--adapter",
            str(adapter_path),
            "--output",
            str(output),
            "--mode",
            "local",
            "--run-id",
            output.name,
            "--created-at",
            "2026-06-03T12:34:56Z",
        ],
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


def snapshot_paths(paths: list[Path]) -> dict[Path, tuple[int, str | None, str | None]]:
    snapshot: dict[Path, tuple[int, str | None, str | None]] = {}
    for path in paths:
        if path.is_symlink():
            snapshot[path] = (path.stat().st_size, None, path.readlink().as_posix())
        elif path.is_file():
            snapshot[path] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest(), None)
        else:
            snapshot[path] = (path.stat().st_size, None, None)
    return snapshot


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


def test_git_environment_isolation_removes_polluting_runtime_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
        "PYTHONPATH",
        "NO_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "HOME",
        "TMPDIR",
        "TZ",
    ]:
        monkeypatch.setenv(key, "/tmp/polluted-env")

    env = plan_local_git_repo_adapter.git_environment(Path("/tmp/repo-root"))

    for key in [
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
        "PYTHONPATH",
        "NO_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "HOME",
        "TZ",
    ]:
        assert key not in env
    assert env["TMPDIR"] == str(Path("/tmp/repo-root") / ".tmp")
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_git_helper_times_out_with_structured_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(plan_local_git_repo_adapter.subprocess, "run", fake_run)

    result = plan_local_git_repo_adapter.git(repo_root, "status", "--porcelain")

    assert result.returncode == 124
    assert result.stderr == "git status --porcelain timed out after 10 seconds"
    run_kwargs = captured["kwargs"]
    assert isinstance(run_kwargs, dict)
    assert run_kwargs["timeout"] == plan_local_git_repo_adapter.GIT_COMMAND_TIMEOUT_SECONDS
    env = run_kwargs["env"]
    assert isinstance(env, dict)
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_local_git_repo_reports_status_timeout_as_blocker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, include_remote_url=False)
    repo_dir = scenario_dir / "repo"

    def fake_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        command = " ".join(args)
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(
                ["git", "-C", str(repo_path), *args],
                returncode=0,
                stdout=f"{repo_path.resolve()}\n",
                stderr="",
            )
        if args[:3] == ("rev-parse", "--verify", "main^{commit}"):
            return subprocess.CompletedProcess(
                ["git", "-C", str(repo_path), *args],
                returncode=0,
                stdout="abc123\n",
                stderr="",
            )
        if args[:3] == ("symbolic-ref", "--short", "HEAD"):
            return subprocess.CompletedProcess(
                ["git", "-C", str(repo_path), *args],
                returncode=0,
                stdout="main\n",
                stderr="",
            )
        if args[:2] == ("status", "--porcelain"):
            return subprocess.CompletedProcess(
                ["git", "-C", str(repo_path), *args],
                returncode=124,
                stdout="",
                stderr=f"git {command} timed out after 10 seconds",
            )
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(plan_local_git_repo_adapter, "git", fake_git)

    repo_details, blockers = plan_local_git_repo_adapter.inspect_repo(
        repo_dir,
        locator={},
        configured_ref="main",
        include_globs=["**/*.md"],
        exclude_globs=[],
    )

    assert repo_details is None
    assert blockers == [
        f"git status failed for repository: {repo_dir.resolve()}: git status --porcelain timed out after 10 seconds"
    ]


def test_local_git_repo_plans_clean_checkout_with_commit_metadata(tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, include_remote_url=True)
    adapter_path = write_adapter(scenario_dir)
    repo_dir = scenario_dir / "repo"
    handoff_jsonl = tmp_path / "handoff.jsonl"

    input_paths = sorted(path for path in repo_dir.rglob("*") if ".git" not in path.parts)
    input_paths.append(adapter_path)
    tree_before = sorted(path.relative_to(scenario_dir).as_posix() for path in input_paths)
    snapshot_before = snapshot_paths(input_paths)
    git_status_before = git(repo_dir, "status", "--porcelain").stdout

    proc = run_planner(["--adapter", str(adapter_path), "--handoff-jsonl", str(handoff_jsonl), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(path.relative_to(scenario_dir).as_posix() for path in input_paths) == tree_before
    assert snapshot_paths(input_paths) == snapshot_before
    assert git(repo_dir, "status", "--porcelain").stdout == git_status_before

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

    assert proc.returncode == 1, proc.stdout + proc.stderr
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

    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["repo_state"] == "invalid"
    assert payload["blockers"] == [
        f"local git repo path is not a git repository: {(scenario_dir / 'plain').resolve()}: "
        "fatal: not a git repository (or any parent up to mount point /)\n"
        "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set)."
    ]
    assert payload["handoff_record_count"] == 0


def test_local_git_repo_refuses_remote_clone_behavior(tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, include_remote_url=True)
    adapter_path = write_adapter(scenario_dir, repo_url="https://example.invalid/clone-target.git")

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 1
    assert "locator field repo_url is not allowed for input_family local_git_repo" in proc.stderr


def test_local_git_repo_execution_smokes_a_clean_checkout(tmp_path: Path) -> None:
    scenario_dir = init_fixture_repo(tmp_path, include_remote_url=True)
    adapter_path = write_adapter(scenario_dir)
    repo_dir = scenario_dir / "repo"
    handoff_jsonl = tmp_path / "handoff.jsonl"

    proc = run_planner(["--adapter", str(adapter_path), "--handoff-jsonl", str(handoff_jsonl), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr

    output = tmp_path / "local-git-execution"
    exec_proc = run_executor(handoff=handoff_jsonl, output=output, adapter_path=adapter_path)

    assert exec_proc.returncode == source_executor.EXIT_PASS, exec_proc.stdout + exec_proc.stderr

    execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
    captures = [json.loads(line) for line in (output / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()]
    extractions = [
        json.loads(line) for line in (output / "extraction-records.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    expected_commit = git(repo_dir, "rev-parse", "--verify", "main^{commit}").stdout.strip()
    assert execution["adapter_type"] == "local_git_repo"
    assert execution["status"] == "completed"
    assert execution["network_access_attempted"] is False
    assert execution["capture_event_count"] == 1
    assert execution["extraction_record_count"] == 2
    assert set(execution["local_input_paths_processed"]) == {
        str(repo_dir / "nested" / "data.json"),
        str(repo_dir / "tracked.md"),
    }
    assert captures[0]["capture_method"] == "local_git_snapshot"
    assert captures[0]["repo_state"] == "clean"
    assert captures[0]["git_commit"] == expected_commit
    assert [entry["extraction_method"] for entry in extractions] == ["git_file_text_extract", "git_file_text_extract"]
    assert [entry["status"] for entry in extractions] == ["completed", "completed"]
    assert {entry["input_hash"] for entry in extractions} == {captures[0]["content_hash"]}
    assert {entry["byte_count_in"] for entry in extractions} == {captures[0]["byte_count"]}
    assert (output / "execution-record.json").read_bytes() == (
        json.dumps(execution, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
