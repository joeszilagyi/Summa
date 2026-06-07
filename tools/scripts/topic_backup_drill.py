#!/usr/bin/env python3
"""Create and verify a per-topic crown-jewel backup snapshot."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import sys
import tempfile
from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.crown_jewel_store_manifest import load_manifest  # noqa: E402
from tools.common.runtime_ledger import append_event, build_event, default_ledger_path, new_run_id  # noqa: E402
from tools.source_db_tools.sqlite_safety import backup_database, run_check  # noqa: E402


SNAPSHOT_SCHEMA_VERSION = "topic-backup-snapshot.v1"


class TopicBackupDrillError(RuntimeError):
    """Raised when a topic backup drill cannot complete."""


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1024)
def cached_asset_matches(workspace_root_value: str, pattern: str) -> tuple[str, ...]:
    workspace_root = Path(workspace_root_value)
    matches = sorted(workspace_root.glob(pattern))
    if not matches and pattern.startswith("dbs/"):
        matches = sorted(REPO_ROOT.glob(pattern))
    return tuple(str(path.resolve()) for path in matches if path.is_file())


def resolve_asset_paths(workspace_root: Path, manifest: dict[str, Any]) -> list[tuple[dict[str, Any], Path]]:
    resolved = []
    workspace_root_value = str(workspace_root.resolve())
    for asset in manifest["assets"]:
        pattern = asset["path_glob"]
        for raw_path in cached_asset_matches(workspace_root_value, pattern):
            resolved.append((asset, Path(raw_path)))
    return resolved


def relative_snapshot_path(workspace_root: Path, source: Path) -> Path:
    try:
        return source.relative_to(workspace_root.resolve())
    except ValueError:
        try:
            return Path("repo") / source.relative_to(REPO_ROOT.resolve())
        except ValueError:
            return Path("external") / source.name


def backup_asset(source: Path, destination: Path, *, asset_class: str, workspace_id: str) -> dict[str, Any]:
    if asset_class == "sqlite_db" or source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        report = backup_database(source, destination, workspace_id=workspace_id)
        status = report["status"]
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        status = "pass"
    return {
        "source_path": str(source),
        "snapshot_path": str(destination),
        "asset_class": asset_class,
        "sha256": sha256_file(destination),
        "size_bytes": destination.stat().st_size,
        "status": status,
    }


def check_asset(source: Path, *, asset_class: str) -> dict[str, Any]:
    if asset_class == "sqlite_db" or source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        check = run_check(source, quick=True)
        return {
            "source_path": str(source),
            "asset_class": asset_class,
            "status": check["status"],
            "integrity": check,
        }
    try:
        return {
            "source_path": str(source),
            "asset_class": asset_class,
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
            "status": "pass",
        }
    except OSError as exc:
        return {
            "source_path": str(source),
            "asset_class": asset_class,
            "status": "fail",
            "messages": [str(exc)],
        }


def verify_restored_snapshot(snapshot_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    verifications = []
    for artifact in snapshot_manifest["artifacts"]:
        source = Path(artifact["snapshot_path"])
        try:
            sha_ok = sha256_file(source) == artifact["sha256"]
            sqlite_ok = True
            sqlite_messages: Any = []
            if artifact["asset_class"] == "sqlite_db" or source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                check = run_check(source)
                sqlite_ok = check["status"] == "pass"
                sqlite_messages = check["messages"]
        except OSError as exc:
            sha_ok = False
            sqlite_ok = False
            sqlite_messages = [str(exc)]
        verifications.append(
            {
                "source_snapshot_path": artifact["snapshot_path"],
                "sha256_status": "pass" if sha_ok else "fail",
                "sqlite_integrity_status": "pass" if sqlite_ok else "fail",
                "sqlite_messages": sqlite_messages,
                "status": "pass" if sha_ok and sqlite_ok else "fail",
            }
        )
    return verifications


def backup_asset_task(
    asset: dict[str, Any],
    source: Path,
    snapshot_root: Path,
    *,
    workspace_root: Path,
    workspace_id: str,
) -> dict[str, Any]:
    destination = snapshot_root / "files" / relative_snapshot_path(workspace_root, source)
    artifact = backup_asset(source, destination, asset_class=asset["asset_class"], workspace_id=workspace_id)
    artifact["asset_id"] = asset["asset_id"]
    return artifact


def build_snapshot(
    *,
    workspace_root: Path,
    manifest_path: Path,
    output_root: Path,
    ledger_path: Path | None,
    dry_run: bool,
    check_only: bool,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    workspace_id = manifest["workspace_id"]
    run_id = new_run_id("backup-drill")
    assets = resolve_asset_paths(workspace_root, manifest)
    if not assets:
        raise TopicBackupDrillError("crown-jewel store manifest matched no files")
    snapshot_root = output_root / workspace_id / utc_stamp()
    planned = [
        {
            "asset_id": asset["asset_id"],
            "source_path": str(path),
            "snapshot_path": str(snapshot_root / "files" / relative_snapshot_path(workspace_root, path)),
        }
        for asset, path in assets
    ]
    if dry_run:
        return {
            "schema_version": "topic-backup-drill-report.v1",
            "status": "dry_run",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "planned_artifacts": planned,
        }
    if check_only:
        source_checks = [
            check_asset(path, asset_class=asset["asset_class"])
            for asset, path in assets
        ]
        return {
            "schema_version": "topic-backup-drill-report.v1",
            "status": "pass" if all(item["status"] == "pass" for item in source_checks) else "fail",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "planned_artifacts": planned,
            "source_checks": source_checks,
        }

    ledger = ledger_path or default_ledger_path(REPO_ROOT, workspace_id)
    append_event(
        ledger,
        build_event(
            workspace_id=workspace_id,
            run_id=run_id,
            event_type="command_start",
            command="topic_backup_drill",
            status="started",
        ),
    )
    artifacts = []
    try:
        sqlite_assets = [(asset, source) for asset, source in assets if asset["asset_class"] == "sqlite_db" or source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}]
        other_assets = [(asset, source) for asset, source in assets if (asset, source) not in sqlite_assets]
        for asset, source in sqlite_assets:
            artifacts.append(
                backup_asset_task(
                    asset,
                    source,
                    snapshot_root,
                    workspace_root=workspace_root,
                    workspace_id=workspace_id,
                )
            )
        if other_assets:
            max_workers = min(4, len(other_assets))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        backup_asset_task,
                        asset,
                        source,
                        snapshot_root,
                        workspace_root=workspace_root,
                        workspace_id=workspace_id,
                    )
                    for asset, source in other_assets
                ]
                for future in concurrent.futures.as_completed(futures):
                    artifacts.append(future.result())
        artifacts.sort(key=lambda artifact: artifact["asset_id"])
        snapshot_manifest = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "source_manifest_path": str(manifest_path),
            "workspace_root": str(workspace_root),
            "artifacts": artifacts,
        }
        verifications = verify_restored_snapshot(snapshot_manifest)
        snapshot_manifest["restore_verifications"] = verifications
        snapshot_manifest["status"] = "pass" if all(item["status"] == "pass" for item in verifications) else "fail"
        atomic_write_json(snapshot_root / "manifest.json", snapshot_manifest)
        append_event(
            ledger,
            build_event(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type="restore_verified",
                command="topic_backup_drill",
                status=snapshot_manifest["status"],
                artifact_refs=[{"path": str(snapshot_root / "manifest.json"), "role": "snapshot_manifest"}],
                validation_posture={"restore_drill": snapshot_manifest["status"]},
            ),
        )
        if snapshot_manifest["status"] != "pass":
            raise TopicBackupDrillError("restore drill failed")
        append_event(
            ledger,
            build_event(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type="command_end",
                command="topic_backup_drill",
                status="pass",
            ),
        )
        return {
            "schema_version": "topic-backup-drill-report.v1",
            "status": "pass",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "snapshot_manifest": str(snapshot_root / "manifest.json"),
            "ledger_path": str(ledger),
            "artifact_count": len(artifacts),
        }
    except Exception as exc:
        append_event(
            ledger,
            build_event(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type="command_failure",
                command="topic_backup_drill",
                status="fail",
                failure={"message": str(exc)},
            ),
        )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("runtime/backups/topics"))
    parser.add_argument("--ledger", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true", help="Validate matched sources without writing a snapshot or ledger event.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def render_text(report: dict[str, Any]) -> str:
    return "\n".join(f"{key}={value}" for key, value in report.items()) + "\n"


def main() -> int:
    args = parse_args()
    try:
        report = build_snapshot(
            workspace_root=args.workspace_root.resolve(),
            manifest_path=args.manifest.resolve(),
            output_root=args.output_root.resolve(),
            ledger_path=args.ledger.resolve() if args.ledger else None,
            dry_run=args.dry_run,
            check_only=args.check,
        )
    except TopicBackupDrillError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report), end="")
    return 0 if report.get("status") in {"pass", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
