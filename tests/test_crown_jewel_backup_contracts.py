from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLANNER_PATH = REPO_ROOT / "tools" / "common" / "crown_jewel_backup.py"
BACKUP_VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_crown_jewel_backup_manifest.py"
STORE_POLICY_VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_crown_jewel_store_policy.py"


def load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


planner = load_module("crown_jewel_backup_planner_for_tests", PLANNER_PATH)
backup_validator = load_module("crown_jewel_backup_manifest_validator_for_tests", BACKUP_VALIDATOR_PATH)
store_policy_validator = load_module("crown_jewel_store_policy_validator_for_tests", STORE_POLICY_VALIDATOR_PATH)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sample_policy_payload() -> dict:
    return {
        "schema_version": "crown-jewel-store-policy.v1",
        "policy_id": "fixture_crown_jewels.v1",
        "backup_root": "runtime/backups/crown_jewels",
        "notes": ["Fixture policy for backup planner validation."],
        "store_families": [
            {
                "store_key": "local_topic_workspace_registry",
                "display_name": "Local topic workspace registry",
                "path_globs": ["runtime/config/topic_workspaces.local.json"],
                "durability_class": "non_rebuildable_local",
                "storage_policy_class": "private_only",
                "backup_frequency_expectation": "after workspace mutations",
                "restore_expectation": "restore last good local registry snapshot",
                "integrity_check_method": "validate_topic_workspace_registry",
                "silent_replace_forbidden": True,
                "missing_ok": False,
                "notes": ["Local-only operator state."],
            },
            {
                "store_key": "global_sqlite_indexes",
                "display_name": "Derived global SQLite indexes",
                "path_globs": ["dbs/rollups/*.sqlite"],
                "durability_class": "expensive_rebuildable",
                "storage_policy_class": "tracked_release",
                "backup_frequency_expectation": "after rollup refreshes",
                "restore_expectation": "restore a clean rollup snapshot",
                "integrity_check_method": "sqlite_integrity_check",
                "silent_replace_forbidden": True,
                "missing_ok": True,
                "notes": ["Derived but still operationally expensive."],
            },
        ],
    }


def sample_manifest_payload() -> dict:
    return {
        "schema_version": "crown-jewel-backup-manifest.v1",
        "policy_id": "fixture_crown_jewels.v1",
        "policy_path": "config/durability_policies/local_first_crown_jewels.v1.json",
        "created_at": "2026-06-02T21:00:00Z",
        "repo_root": str(REPO_ROOT.resolve()),
        "backup_root": "runtime/backups/crown_jewels",
        "requested_store_keys": [],
        "store_entries": [
            {
                "store_key": "local_topic_workspace_registry",
                "display_name": "Local topic workspace registry",
                "path_globs": ["runtime/config/topic_workspaces.local.json"],
                "durability_class": "non_rebuildable_local",
                "storage_policy_class": "private_only",
                "backup_frequency_expectation": "after workspace mutations",
                "restore_expectation": "restore last good local registry snapshot",
                "integrity_check_method": "validate_topic_workspace_registry",
                "silent_replace_forbidden": True,
                "missing_ok": False,
                "status": "present",
                "match_count": 1,
                "matched_paths": ["runtime/config/topic_workspaces.local.json"],
                "notes": ["Local-only operator state."],
            }
        ],
    }


def test_store_policy_validator_accepts_current_repo_policy() -> None:
    policy_path = REPO_ROOT / "config" / "durability_policies" / "local_first_crown_jewels.v1.json"

    result, exit_code = store_policy_validator.validate_crown_jewel_store_policy(policy_path)

    assert exit_code == store_policy_validator.EXIT_PASS, result


def test_store_policy_validator_rejects_duplicate_store_key(tmp_path: Path) -> None:
    payload = sample_policy_payload()
    payload["store_families"][1]["store_key"] = payload["store_families"][0]["store_key"]
    policy_path = tmp_path / "policy.json"
    write_json(policy_path, payload)

    result, exit_code = store_policy_validator.validate_crown_jewel_store_policy(policy_path)

    assert exit_code == store_policy_validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "DUPLICATE_STORE_KEY" for error in result["errors"])


def test_backup_planner_emits_valid_manifest_for_fixture_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    policy_path = repo_root / "config" / "durability_policies" / "local_first_crown_jewels.v1.json"
    write_json(policy_path, sample_policy_payload())
    (repo_root / "runtime" / "config").mkdir(parents=True)
    (repo_root / "runtime" / "config" / "topic_workspaces.local.json").write_text("{}\n", encoding="utf-8")
    (repo_root / "dbs" / "rollups").mkdir(parents=True)
    (repo_root / "dbs" / "rollups" / "global.sqlite").write_text("fixture\n", encoding="utf-8")

    manifest = planner.plan_backup_manifest(policy_path=policy_path, repo_root=repo_root)

    assert manifest["schema_version"] == "crown-jewel-backup-manifest.v1"
    assert manifest["policy_path"] == "config/durability_policies/local_first_crown_jewels.v1.json"
    statuses = {entry["store_key"]: entry["status"] for entry in manifest["store_entries"]}
    assert statuses == {
        "local_topic_workspace_registry": "present",
        "global_sqlite_indexes": "present",
    }
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, manifest)
    result, exit_code = backup_validator.validate_crown_jewel_backup_manifest(manifest_path)
    assert exit_code == backup_validator.EXIT_PASS, result


def test_backup_manifest_validator_rejects_present_without_matches(tmp_path: Path) -> None:
    payload = sample_manifest_payload()
    payload["store_entries"][0]["match_count"] = 0
    payload["store_entries"][0]["matched_paths"] = []
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, payload)

    result, exit_code = backup_validator.validate_crown_jewel_backup_manifest(manifest_path)

    assert exit_code == backup_validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "STATUS_PATH_MISMATCH" for error in result["errors"])
