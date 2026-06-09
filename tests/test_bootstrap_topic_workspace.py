from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scripts import bootstrap_topic_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "bootstrap_topic_workspace.py"
GENERAL_PACK = "general.v1"


def run_tool(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT if cwd is None else cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def make_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "registry": str(tmp_path / "topic-workspaces.local.json"),
        "allow_tracked_registry": False,
        "topic_label": "Monarch butterflies",
        "workspace_id": None,
        "workspace_root": str(tmp_path / "topic-workspace"),
        "domain_pack": GENERAL_PACK,
        "subject_id": None,
        "display_name": None,
        "scope_statement": None,
        "languages": None,
        "aliases": None,
        "disambiguation_terms": None,
        "excluded_senses": None,
        "enabled_facets": None,
        "query_families": None,
        "schedule_posture": "manual",
        "workspace_policy_class": "private_local",
        "lifecycle_state": "bootstrap",
        "set_default": False,
        "non_interactive": True,
        "dry_run": False,
        "format": "json",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_bootstrap_helpers_cover_identifiers_and_rendering(tmp_path: Path) -> None:
    assert (
        bootstrap_topic_workspace.slugify_identifier("Monarch butterflies", label="workspace_id")
        == "monarch_butterflies"
    )
    with pytest.raises(bootstrap_topic_workspace.BootstrapError):
        bootstrap_topic_workspace.slugify_identifier("!!!", label="workspace_id")

    assert bootstrap_topic_workspace.parse_csv("en, fr, en, ") == ["en", "fr"]
    assert bootstrap_topic_workspace.parse_csv(None) == []

    assert bootstrap_topic_workspace.unique_subset([], ["a", "b"], label="enabled_facets") == [
        "a",
        "b",
    ]
    assert bootstrap_topic_workspace.unique_subset(
        ["b", "a", "b"], ["a", "b", "c"], label="query_families"
    ) == ["b", "a"]
    with pytest.raises(bootstrap_topic_workspace.BootstrapError):
        bootstrap_topic_workspace.unique_subset(["c"], ["a", "b"], label="enabled_facets")
    with pytest.raises(bootstrap_topic_workspace.BootstrapError):
        bootstrap_topic_workspace.unique_subset([], [], label="query_families")

    scope = bootstrap_topic_workspace.build_scope_statement("Monarch butterflies", GENERAL_PACK)
    assert "Monarch butterflies" in scope
    assert GENERAL_PACK in scope

    brief = bootstrap_topic_workspace.build_source_brief(
        topic_label="Monarch butterflies",
        display_name="Monarch butterflies",
        scope_statement=scope,
    )
    assert "Bootstrap-generated subject brief." in brief
    assert "Topic label: Monarch butterflies" in brief

    result = bootstrap_topic_workspace.build_result_payload(
        registry_path=tmp_path / "registry.json",
        workspace_id="monarch_butterflies",
        workspace_root=tmp_path / "topic-workspace",
        manifest_path=tmp_path / "topic-workspace" / ".indexer" / "subject_manifest.json",
        source_brief_path=tmp_path / "topic-workspace" / "source.txt",
        default_workspace_id="monarch_butterflies",
        created_paths=[tmp_path / "topic-workspace"],
        dry_run=True,
        registry_action="create",
    )
    rendered = bootstrap_topic_workspace.render_text(result)
    assert "dry_run=true" in rendered
    assert "registry_action=create" in rendered
    assert "planned_created_path[0]=" in rendered


def test_bootstrap_rejects_tracked_registry_paths() -> None:
    with pytest.raises(bootstrap_topic_workspace.BootstrapError):
        bootstrap_topic_workspace.ensure_bootstrap_safe_registry_path(
            REPO_ROOT / "config" / "topic_workspaces.local.json",
            allow_tracked_registry=False,
        )


def test_load_domain_pack_reports_missing_and_invalid_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap_topic_workspace, "REPO_ROOT", tmp_path)

    with pytest.raises(bootstrap_topic_workspace.BootstrapError, match="not found"):
        bootstrap_topic_workspace.load_domain_pack("missing")

    pack_path = tmp_path / "config" / "domain_packs" / "broken.json"
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(bootstrap_topic_workspace.BootstrapError, match="must contain a JSON object"):
        bootstrap_topic_workspace.load_domain_pack("broken")

    pack_path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(bootstrap_topic_workspace.BootstrapError, match="could not be parsed"):
        bootstrap_topic_workspace.load_domain_pack("broken")


def test_bootstrap_workspace_dry_run_cli_reports_planned_paths(tmp_path: Path) -> None:
    registry = tmp_path / "topic-workspaces.local.json"
    workspace_root = tmp_path / "workspace with spaces"

    proc = run_tool(
        [
            "--registry",
            str(registry),
            "--non-interactive",
            "--dry-run",
            "--format",
            "text",
            "--topic-label",
            "Monarch butterflies",
            "--workspace-root",
            str(workspace_root),
            "--domain-pack",
            GENERAL_PACK,
            "--set-default",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "dry_run=true" in proc.stdout
    assert "registry_action=create" in proc.stdout
    assert "planned_created_path[0]=" in proc.stdout
    assert not registry.exists()
    assert not workspace_root.exists()


def test_bootstrap_workspace_creates_manifest_and_registry_cli_json(tmp_path: Path) -> None:
    registry = tmp_path / "topic-workspaces.local.json"
    workspace_root = tmp_path / "topic-workspace"

    proc = run_tool(
        [
            "--registry",
            str(registry),
            "--non-interactive",
            "--format",
            "json",
            "--topic-label",
            "Monarch butterflies",
            "--workspace-root",
            str(workspace_root),
            "--domain-pack",
            GENERAL_PACK,
            "--set-default",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)

    assert payload["registry_path"] == str(registry)
    assert payload["workspace_root"] == str(workspace_root)
    assert payload["workspace_id"] == "monarch_butterflies"
    assert payload["default_workspace_id"] == "monarch_butterflies"
    assert payload["created_paths"] == [
        str(workspace_root),
        str(workspace_root / ".indexer"),
        str(workspace_root / "state"),
        str(workspace_root / "runs"),
        str(workspace_root / "source.txt"),
        str(workspace_root / ".indexer" / "subject_manifest.json"),
    ]

    subject_manifest = json.loads(
        (workspace_root / ".indexer" / "subject_manifest.json").read_text(encoding="utf-8")
    )
    assert subject_manifest["schema_version"] == "subject-manifest.v1"
    assert subject_manifest["subject_id"] == "general.monarch_butterflies"
    assert subject_manifest["display_name"] == "Monarch butterflies"
    assert subject_manifest["domain_pack"] == GENERAL_PACK
    assert subject_manifest["languages"] == ["en"]
    assert subject_manifest["aliases"] == ["Monarch butterflies"]
    assert subject_manifest["public_export_default"] is False
    assert subject_manifest["legacy_substrate_paths"] == [str(workspace_root.resolve())]

    source_brief = (workspace_root / "source.txt").read_text(encoding="utf-8")
    assert "Monarch butterflies" in source_brief
    assert "Bootstrap-generated subject brief." in source_brief

    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    assert registry_payload["schema_version"] == "topic-workspace-registry.v1"
    assert registry_payload["default_workspace_id"] == "monarch_butterflies"
    assert registry_payload["workspaces"][0]["workspace_id"] == "monarch_butterflies"
    assert registry_payload["workspaces"][0]["topic_label"] == "Monarch butterflies"


def test_bootstrap_workspace_rolls_back_registry_on_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "topic-workspaces.local.json"
    existing_root = tmp_path / "existing-workspace"
    existing_root.mkdir(parents=True, exist_ok=True)
    existing_manifest = existing_root / ".indexer" / "subject_manifest.json"
    existing_manifest.parent.mkdir(parents=True, exist_ok=True)
    existing_manifest.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": "general.existing",
                "display_name": "Existing workspace",
                "domain_pack": GENERAL_PACK,
                "scope_statement": "Existing workspace scope.",
                "languages": ["en"],
                    "aliases": ["Existing workspace"],
                    "disambiguation_terms": [],
                    "excluded_senses": [],
                    "enabled_facets": ["sources"],
                    "query_families": ["web_search"],
                    "notes": [],
                    "legacy_substrate_paths": [str(existing_root.resolve())],
                    "public_export_default": False,
                }
        )
        + "\n",
        encoding="utf-8",
    )
    original_registry = {
        "schema_version": "topic-workspace-registry.v1",
        "default_workspace_id": "existing",
        "workspaces": [
            {
                "workspace_id": "existing",
                "topic_label": "Existing workspace",
                "workspace_root": str(existing_root),
                "domain_pack": GENERAL_PACK,
                "default_subject_manifest": str(existing_manifest),
                "lifecycle_state": "active",
                "schedule_posture": "manual",
                "workspace_policy_class": "private_local",
                "notes": [],
            }
        ],
    }
    registry.write_text(json.dumps(original_registry) + "\n", encoding="utf-8")
    workspace_root = tmp_path / "topic-workspace"

    args = make_args(
        tmp_path,
        registry=str(registry),
        workspace_root=str(workspace_root),
    )

    def fail_validation(_: Path) -> None:
        raise bootstrap_topic_workspace.BootstrapError("boom")

    monkeypatch.setattr(bootstrap_topic_workspace, "validate_manifest_or_raise", fail_validation)

    with pytest.raises(bootstrap_topic_workspace.BootstrapError, match="boom"):
        bootstrap_topic_workspace.bootstrap_workspace(args)

    assert registry.read_text(encoding="utf-8") == json.dumps(original_registry) + "\n"
    assert not workspace_root.exists()
