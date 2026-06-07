from __future__ import annotations

import importlib.util
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
