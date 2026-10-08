from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.common.crown_jewel_store_manifest import (
    CrownJewelStoreManifestError,
    load_manifest,
)


def manifest_payload() -> dict[str, object]:
    return {
        "schema_version": "crown-jewel-store-manifest.v1",
        "workspace_id": "fixture_workspace",
        "backup_posture": {"status": "fresh"},
        "assets": [
            {
                "asset_id": "fixture-db",
                "asset_class": "sqlite_db",
                "rebuildability": "non_rebuildable",
                "mutation_policy": "requires_fresh_backup",
                "path_glob": "dbs/fixture.sqlite",
            }
        ],
    }


def test_load_manifest_accepts_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    payload = manifest_payload()
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_manifest(path) == payload


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"crown-jewel-store-manifest.v1","workspace_id":"first",'
        '"workspace_id":"second","backup_posture":{"status":"fresh"},"assets":[]}',
        '{"schema_version":"crown-jewel-store-manifest.v1","workspace_id":"fixture",'
        '"backup_posture":{"status":"fresh","score":NaN},"assets":[]}',
    ],
)
def test_load_manifest_rejects_non_strict_json(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(CrownJewelStoreManifestError, match="could not read"):
        load_manifest(path)
