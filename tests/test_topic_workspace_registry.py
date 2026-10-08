from __future__ import annotations

from pathlib import Path

import pytest

from tools.common.topic_workspace_registry import (
    DEFAULT_REGISTRY_ENV,
    TopicWorkspaceRegistryError,
    discover_registry_path,
    resolve_workspace,
)


def workspace_registry_payload(
    *, workspace_id: str, workspace_root: Path, subject_manifest: Path
) -> dict[str, object]:
    return {
        "schema_version": "topic-workspace-registry.v1",
        "workspaces": [
            {
                "workspace_id": workspace_id,
                "workspace_root": str(workspace_root),
                "default_subject_manifest": str(subject_manifest),
            }
        ],
    }


def test_discover_registry_path_rejects_untrusted_environment_path(tmp_path: Path) -> None:
    with pytest.raises(TopicWorkspaceRegistryError, match="trusted registry root"):
        discover_registry_path(
            env={DEFAULT_REGISTRY_ENV: str(tmp_path / "attacker-controlled.json")},
            cwd=tmp_path,
        )


@pytest.mark.parametrize("workspace_id", ["../escape", "team/one", "/absolute"])
def test_resolve_workspace_rejects_path_unsafe_requested_ids(
    tmp_path: Path, workspace_id: str
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_manifest = workspace_root / "subject_manifest.json"
    subject_manifest.write_text("{}\n", encoding="utf-8")

    with pytest.raises(TopicWorkspaceRegistryError, match="workspace identifier pattern"):
        resolve_workspace(
            registry_path=tmp_path / "registry.json",
            workspace_id=workspace_id,
            registry_payload=workspace_registry_payload(
                workspace_id="topic_01",
                workspace_root=workspace_root,
                subject_manifest=subject_manifest,
            ),
        )


def test_resolve_workspace_accepts_schema_compliant_id(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_manifest = workspace_root / "subject_manifest.json"
    subject_manifest.write_text("{}\n", encoding="utf-8")

    resolved = resolve_workspace(
        registry_path=tmp_path / "registry.json",
        workspace_id="topic_01",
        registry_payload=workspace_registry_payload(
            workspace_id="topic_01",
            workspace_root=workspace_root,
            subject_manifest=subject_manifest,
        ),
    )

    assert resolved["workspace_id"] == "topic_01"
    assert resolved["resolved_workspace_root"] == workspace_root
