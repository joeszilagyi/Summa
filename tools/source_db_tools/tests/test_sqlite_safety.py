from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = REPO_ROOT / "tools" / "source_db_tools" / "sqlite_safety.py"


def create_wal_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO demo (value) VALUES ('fixture')")
        conn.commit()
    finally:
        conn.close()


def run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_sqlite_safety_backup_and_restore_verify(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    backup = tmp_path / "snapshot.sqlite"
    create_wal_db(db)

    result = run_tool("backup", str(db), "--output", str(backup))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert backup.is_file()

    verify = run_tool("restore-verify", str(backup))
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert json.loads(verify.stdout)["status"] == "pass"


def test_sqlite_safety_backup_uses_workspace_lock_when_requested(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    backup = tmp_path / "snapshot.sqlite"
    lock_root = tmp_path / "locks"
    create_wal_db(db)

    result = run_tool(
        "backup",
        str(db),
        "--output",
        str(backup),
        "--workspace-id",
        "fixture_workspace",
        "--lock-root",
        str(lock_root),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "pass"
    assert backup.is_file()
    assert not list(lock_root.glob("*.lock"))


def test_sqlite_safety_integrity_failure_returns_nonzero(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a sqlite database")

    result = run_tool("integrity-check", str(bad))

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"


def test_sqlite_safety_reserved_path_characters_survive_readonly_access(tmp_path: Path) -> None:
    db = tmp_path / "a?b#c.sqlite"
    output = tmp_path / "q?x#y.sqlite"
    create_wal_db(db)

    result = run_tool("integrity-check", str(db))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["database"] == str(db)

    backup_result = run_tool("backup", str(db), "--output", str(output))
    assert backup_result.returncode == 0, backup_result.stdout + backup_result.stderr
    assert output.is_file()
    verify = run_tool("restore-verify", str(output))
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert json.loads(verify.stdout)["status"] == "pass"


def test_sqlite_safety_profile_marks_wal_sidecars_runtime(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    create_wal_db(db)

    result = run_tool("profile", str(db))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["sidecar_posture"] == "runtime_artifacts"
    assert payload["wal_sidecar_path"].endswith("source.sqlite-wal")
