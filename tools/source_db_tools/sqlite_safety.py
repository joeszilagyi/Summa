#!/usr/bin/env python3
"""SQLite safety operations for local crown-jewel databases."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.workspace_lock import acquire_workspace_lock  # noqa: E402


class SQLiteSafetyError(RuntimeError):
    """Raised when a SQLite safety operation fails."""


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists() or not path.is_file():
        raise SQLiteSafetyError(f"database not found: {path}")
    return sqlite3.connect(_readonly_uri(path), uri=True)


def run_check(path: Path, *, quick: bool = False) -> dict[str, Any]:
    pragma = "quick_check" if quick else "integrity_check"
    try:
        conn = connect_readonly(path)
    except (sqlite3.DatabaseError, SQLiteSafetyError) as exc:
        return {"database": str(path), "operation": pragma, "status": "fail", "messages": [str(exc)]}
    try:
        rows = [row[0] for row in conn.execute(f"PRAGMA {pragma}").fetchall()]
    except sqlite3.DatabaseError as exc:
        return {"database": str(path), "operation": pragma, "status": "fail", "messages": [str(exc)]}
    finally:
        conn.close()
    return {"database": str(path), "operation": pragma, "status": "pass" if rows == ["ok"] else "fail", "messages": rows}


def checkpoint(path: Path, *, mode: str = "PASSIVE") -> dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    finally:
        conn.close()
    return {"database": str(path), "operation": "checkpoint", "mode": mode, "status": "pass", "result": list(row) if row else []}


def backup_database(
    source: Path,
    destination: Path,
    *,
    workspace_id: str | None = None,
    lock_root: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path = source.resolve()
    destination_path = destination.resolve()
    if source_path == destination_path:
        raise SQLiteSafetyError("source and destination must not be the same database path")
    if destination.exists() and not overwrite:
        raise SQLiteSafetyError(f"destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    lock_context = (
        acquire_workspace_lock(
            workspace_id,
            **(
                {"command": "sqlite_safety.backup"}
                | ({"lock_root": lock_root} if lock_root is not None else {})
            ),
        )
        if workspace_id
        else nullcontext()
    )
    try:
        with lock_context:
            source_conn = sqlite3.connect(_readonly_uri(source_path), uri=True)
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{destination.name}.tmp.",
                    suffix=".db",
                    dir=destination.parent,
                    delete=False,
                ) as temp_file:
                    backup_path = Path(temp_file.name)
                temp_conn = sqlite3.connect(backup_path)
                try:
                    source_conn.backup(temp_conn)
                finally:
                    temp_conn.close()
            finally:
                source_conn.close()
            if backup_path is None:
                raise SQLiteSafetyError(f"backup temporary path was not created: {destination}")
            check = run_check(backup_path)
            if check["status"] != "pass":
                raise SQLiteSafetyError(f"backup integrity check failed: {check['messages']}")
            backup_path.replace(destination)
    except Exception:
        if backup_path is not None and backup_path.exists():
            backup_path.unlink()
        raise
    if destination.exists():
        check = run_check(destination)
    else:
        raise SQLiteSafetyError(f"backup failed to produce destination database: {destination}")
    return {
        "database": str(source),
        "backup_path": str(destination),
        "operation": "backup",
        "status": check["status"],
        "integrity": check,
    }


def restore_verify(backup_path: Path) -> dict[str, Any]:
    if not backup_path.exists():
        return {"backup_path": str(backup_path), "operation": "restore-verify", "status": "fail", "messages": ["backup not found"]}
    with tempfile.TemporaryDirectory(prefix="sqlite-restore-verify-") as temp_dir:
        restored = Path(temp_dir) / backup_path.name
        shutil.copy2(backup_path, restored)
        check = run_check(restored)
        return {
            "backup_path": str(backup_path),
            "restored_path": str(restored),
            "operation": "restore-verify",
            "status": check["status"],
            "integrity": check,
        }


def profile(path: Path) -> dict[str, Any]:
    conn = connect_readonly(path)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return {
        "database": str(path),
        "operation": "profile",
        "status": "pass",
        "user_version": user_version,
        "page_count": page_count,
        "page_size": page_size,
        "journal_mode": journal_mode,
        "wal_sidecar_path": str(path) + "-wal",
        "shm_sidecar_path": str(path) + "-shm",
        "sidecar_posture": "runtime_artifacts",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("integrity-check", "quick-check", "checkpoint", "profile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("database", type=Path)
    sub.choices["checkpoint"].add_argument("--mode", default="PASSIVE", choices=("PASSIVE", "FULL", "RESTART", "TRUNCATE"))

    backup = sub.add_parser("backup")
    backup.add_argument("database", type=Path)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing destination database.",
    )
    backup.add_argument("--workspace-id")
    backup.add_argument("--lock-root", type=Path)

    verify = sub.add_parser("restore-verify")
    verify.add_argument("backup", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "integrity-check":
            report = run_check(args.database)
        elif args.command == "quick-check":
            report = run_check(args.database, quick=True)
        elif args.command == "checkpoint":
            report = checkpoint(args.database, mode=args.mode)
        elif args.command == "backup":
            report = backup_database(
                args.database,
                args.output,
                workspace_id=args.workspace_id,
                lock_root=args.lock_root,
                overwrite=args.overwrite,
            )
        elif args.command == "restore-verify":
            report = restore_verify(args.backup)
        elif args.command == "profile":
            report = profile(args.database)
        else:  # pragma: no cover
            raise SQLiteSafetyError(f"unsupported command: {args.command}")
    except (SQLiteSafetyError, sqlite3.DatabaseError) as exc:
        report = {"operation": args.command, "status": "fail", "messages": [str(exc)]}
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
