from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "scripts" / "topic_backup_drill.py"


spec = importlib.util.spec_from_file_location("topic_backup_drill_for_tests", SCRIPT_PATH)
assert spec is not None
topic_backup_drill = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(topic_backup_drill)


def test_verify_restored_snapshot_checks_snapshots_in_place(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "snapshot.txt"
    snapshot.write_text("fixture snapshot\n", encoding="utf-8")

    manifest = {
        "artifacts": [
            {
                "snapshot_path": str(snapshot),
                "sha256": topic_backup_drill.sha256_file(snapshot),
                "asset_class": "other",
            }
        ]
    }

    def fail_copy2(*args: object, **kwargs: object) -> object:
        raise AssertionError("verify_restored_snapshot should not copy snapshot files")

    monkeypatch.setattr(topic_backup_drill.shutil, "copy2", fail_copy2)

    verifications = topic_backup_drill.verify_restored_snapshot(manifest)

    assert verifications == [
        {
            "source_snapshot_path": str(snapshot),
            "sha256_status": "pass",
            "sqlite_integrity_status": "pass",
            "sqlite_messages": [],
            "status": "pass",
        }
    ]


def test_resolve_asset_paths_caches_repeated_glob_matches(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    asset_dir = workspace_root / "dbs" / "shared"
    asset_dir.mkdir(parents=True)
    (asset_dir / "one.txt").write_text("one\n", encoding="utf-8")
    manifest = {
        "assets": [
            {"asset_id": "asset-1", "path_glob": "dbs/shared/*.txt", "asset_class": "other"},
            {"asset_id": "asset-2", "path_glob": "dbs/shared/*.txt", "asset_class": "other"},
        ]
    }

    topic_backup_drill.cached_asset_matches.cache_clear()
    glob_calls: list[str] = []
    original_glob = Path.glob

    def fake_glob(self: Path, pattern: str):
        if self == workspace_root.resolve():
            glob_calls.append(pattern)
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", fake_glob)

    resolved = topic_backup_drill.resolve_asset_paths(workspace_root, manifest)

    assert [path.name for _, path in resolved] == ["one.txt", "one.txt"]
    assert glob_calls == ["dbs/shared/*.txt"]


def test_build_snapshot_parallelizes_non_sqlite_assets(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    manifest_path = tmp_path / "manifest.json"
    output_root = tmp_path / "output"
    ledger_path = tmp_path / "runtime" / "ledgers" / "workspace.runtime-ledger.jsonl"
    asset_dir = workspace_root / "assets"
    asset_dir.mkdir(parents=True)
    source_files = []
    for index in range(3):
        source = asset_dir / f"asset-{index}.txt"
        source.write_text(f"asset-{index}\n", encoding="utf-8")
        source_files.append(source)

    manifest_path.write_text(
        json.dumps(
                {
                    "schema_version": "crown-jewel-store-manifest.v1",
                    "workspace_id": "workspace",
                    "backup_posture": {"status": "fresh"},
                    "assets": [
                        {
                            "asset_id": f"asset-{index}",
                            "asset_class": "other",
                            "path_glob": f"assets/asset-{index}.txt",
                            "rebuildability": "rebuildable_from_crown_jewels",
                            "mutation_policy": "allows_without_backup",
                        }
                        for index in range(3)
                    ],
                },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    active = 0
    max_active = 0
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def fake_backup_asset(source: Path, destination: Path, *, asset_class: str, workspace_id: str) -> dict[str, object]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            started.set()
        release.wait(timeout=2)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        with lock:
            active -= 1
        return {
            "source_path": str(source),
            "snapshot_path": str(destination),
            "asset_class": asset_class,
            "sha256": topic_backup_drill.sha256_file(destination),
            "size_bytes": destination.stat().st_size,
            "status": "pass",
        }

    monkeypatch.setattr(topic_backup_drill, "backup_asset", fake_backup_asset)
    monkeypatch.setattr(topic_backup_drill, "verify_restored_snapshot", lambda snapshot_manifest: [
        {
            "source_snapshot_path": artifact["snapshot_path"],
            "sha256_status": "pass",
            "sqlite_integrity_status": "pass",
            "sqlite_messages": [],
            "status": "pass",
        }
        for artifact in snapshot_manifest["artifacts"]
    ])
    monkeypatch.setattr(topic_backup_drill, "atomic_write_json", lambda *args, **kwargs: None)

    def run_snapshot() -> dict[str, object]:
        return topic_backup_drill.build_snapshot(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            output_root=output_root,
            ledger_path=ledger_path,
            dry_run=False,
            check_only=False,
        )

    thread = threading.Thread(target=run_snapshot)
    thread.start()
    assert started.wait(timeout=2)
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert max_active >= 2
