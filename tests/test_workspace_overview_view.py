import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "Index_Workspace_Overview.sh"
PY_TOOL = REPO_ROOT / "tools" / "scripts" / "build_workspace_overview_view.py"


def run_script(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_manifest(workspace_root: Path, *, subject_id: str, display_name: str) -> Path:
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": subject_id,
                "display_name": display_name,
                "domain_pack": "subject.v1",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def workspace_record(
    *,
    workspace_id: str,
    workspace_root: Path,
    lifecycle_state: str = "active",
    schedule_posture: str = "manual",
    workspace_policy_class: str = "private_local",
    manifest_path: Path | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "workspace_id": workspace_id,
        "topic_label": workspace_id.replace("_", " ").title(),
        "workspace_root": str(workspace_root),
        "domain_pack": "subject.v1",
        "lifecycle_state": lifecycle_state,
        "schedule_posture": schedule_posture,
        "workspace_policy_class": workspace_policy_class,
    }
    if manifest_path is not None:
        record["default_subject_manifest"] = str(manifest_path)
    return record


def write_registry(tmp_path: Path, workspaces: list[dict[str, object]]) -> Path:
    registry_path = tmp_path / "topic_workspaces.local.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "topic-workspace-registry.v1",
                "workspaces": workspaces,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return registry_path


def test_workspace_overview_surfaces_status_and_publish_blockers(tmp_path: Path) -> None:
    public_root = tmp_path / "workspaces" / "public_topic"
    private_root = tmp_path / "workspaces" / "private_topic"
    missing_root = tmp_path / "workspaces" / "missing_topic"
    public_root.mkdir(parents=True)
    private_root.mkdir(parents=True)

    public_manifest = write_manifest(
        public_root,
        subject_id="subject.public_topic",
        display_name="Public Topic",
    )
    missing_manifest = missing_root / ".indexer" / "subject_manifest.json"

    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="public_topic",
                workspace_root=public_root,
                schedule_posture="scheduled",
                workspace_policy_class="public_safe_release",
                manifest_path=public_manifest,
            ),
            workspace_record(
                workspace_id="private_topic",
                workspace_root=private_root,
            ),
            workspace_record(
                workspace_id="missing_topic",
                workspace_root=missing_root,
                schedule_posture="scheduled",
                workspace_policy_class="public_safe_release",
                manifest_path=missing_manifest,
            ),
        ],
    )

    result = run_script(["--registry", str(registry_path), "--format", "json"])

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "workspace-overview.v1"
    assert payload["counts"] == {
        "active_workspaces": 3,
        "default_subject_manifest_ok": 1,
        "publish_blocked": 2,
        "scheduled_workspaces": 2,
        "total_workspaces": 3,
        "workspace_root_ok": 2,
    }

    workspaces = {entry["workspace_id"]: entry for entry in payload["workspaces"]}
    assert workspaces["public_topic"]["workspace_root_status"] == "ok"
    assert workspaces["public_topic"]["default_subject_manifest_status"] == "ok"
    assert workspaces["public_topic"]["manifest_subject_id"] == "subject.public_topic"
    assert workspaces["public_topic"]["publish_readiness"] == {
        "blockers": [],
        "state": "needs_validation_review",
    }

    assert workspaces["private_topic"]["default_subject_manifest_status"] == "not_declared"
    assert workspaces["private_topic"]["publish_readiness"]["state"] == "blocked"
    assert "workspace_policy_class:private_local" in workspaces["private_topic"]["publish_readiness"]["blockers"]

    assert workspaces["missing_topic"]["workspace_root_status"] == "missing"
    assert workspaces["missing_topic"]["default_subject_manifest_status"] == "missing"
    assert "workspace_root:missing" in workspaces["missing_topic"]["publish_readiness"]["blockers"]


def test_workspace_overview_filters_and_renders_text(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "manual_topic"
    workspace_root.mkdir(parents=True)
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="manual_topic",
                workspace_root=workspace_root,
            )
        ],
    )

    result = run_script(
        [
            "--registry",
            str(registry_path),
            "--workspace-id",
            "manual_topic",
            "--format",
            "text",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_version=workspace-overview.v1" in result.stdout
    assert "workspace_count=1" in result.stdout
    assert "workspace[0].workspace_id=manual_topic" in result.stdout
    assert "workspace[0].publish_readiness=blocked" in result.stdout


def test_workspace_overview_rejects_unknown_workspace_id(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "known_topic"
    workspace_root.mkdir(parents=True)
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="known_topic",
                workspace_root=workspace_root,
            )
        ],
    )

    result = run_script(["--registry", str(registry_path), "--workspace-id", "missing_topic"])

    assert result.returncode == 1
    assert "workspace_id not found in topic workspace registry: missing_topic" in result.stderr


def test_workspace_overview_python_tool_compiles() -> None:
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(PY_TOOL)],
        cwd=REPO_ROOT,
        check=True,
    )
