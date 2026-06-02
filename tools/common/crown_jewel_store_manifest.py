"""Crown-jewel store manifest loading and mutation-refusal helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "crown-jewel-store-manifest.v1"
ASSET_CLASSES = {
    "sqlite_db",
    "jsonl_ledger",
    "source_record",
    "review_state",
    "manifest",
    "metadata_sidecar",
    "other",
}


class CrownJewelStoreManifestError(RuntimeError):
    """Raised when crown-jewel store manifest validation fails."""


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrownJewelStoreManifestError(f"could not read crown-jewel store manifest: {path}") from exc
    validate_manifest(payload)
    return payload


def validate_manifest(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise CrownJewelStoreManifestError("manifest must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CrownJewelStoreManifestError("manifest schema_version must be crown-jewel-store-manifest.v1")
    if not isinstance(payload.get("workspace_id"), str) or not payload["workspace_id"].strip():
        raise CrownJewelStoreManifestError("manifest workspace_id is required")
    posture = payload.get("backup_posture")
    if not isinstance(posture, dict) or posture.get("status") not in {"fresh", "stale", "unknown"}:
        raise CrownJewelStoreManifestError("manifest backup_posture.status must be fresh, stale, or unknown")
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise CrownJewelStoreManifestError("manifest assets must be a non-empty array")
    seen = set()
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, dict):
            raise CrownJewelStoreManifestError(f"asset {index} must be an object")
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise CrownJewelStoreManifestError(f"asset {index} missing asset_id")
        if asset_id in seen:
            raise CrownJewelStoreManifestError(f"duplicate asset_id: {asset_id}")
        seen.add(asset_id)
        if asset.get("asset_class") not in ASSET_CLASSES:
            raise CrownJewelStoreManifestError(f"asset {asset_id} has invalid asset_class")
        if asset.get("rebuildability") not in {"non_rebuildable", "rebuildable_from_crown_jewels"}:
            raise CrownJewelStoreManifestError(f"asset {asset_id} has invalid rebuildability")
        if asset.get("mutation_policy") not in {"requires_fresh_backup", "allows_without_backup"}:
            raise CrownJewelStoreManifestError(f"asset {asset_id} has invalid mutation_policy")
        if not isinstance(asset.get("path_glob"), str) or not asset["path_glob"].strip():
            raise CrownJewelStoreManifestError(f"asset {asset_id} missing path_glob")


def risky_mutation_refusal(payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
    validate_manifest(payload)
    backup_status = payload["backup_posture"]["status"]
    protected_assets = [
        asset
        for asset in payload["assets"]
        if asset["rebuildability"] == "non_rebuildable"
        and asset["mutation_policy"] == "requires_fresh_backup"
    ]
    allowed = backup_status == "fresh" or not protected_assets
    return {
        "operation": operation,
        "allowed": allowed,
        "backup_posture": backup_status,
        "protected_asset_ids": [asset["asset_id"] for asset in protected_assets],
        "reason": None if allowed else "fresh backup is required before mutating non-rebuildable crown-jewel assets",
    }
