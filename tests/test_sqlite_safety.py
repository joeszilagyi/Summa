from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "source_db_tools" / "sqlite_safety.py"

spec = importlib.util.spec_from_file_location("sqlite_safety_for_tests", SCRIPT_PATH)
assert spec is not None
sqlite_safety = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sqlite_safety)


def make_database(path: Path, *, marker: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS marker(value TEXT)")
        conn.execute("DELETE FROM marker")
        conn.execute("INSERT INTO marker(value) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def read_marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM marker").fetchone()
        return "" if row is None else str(row[0])
    finally:
        conn.close()


def test_backup_database_rejects_existing_destination_by_default(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    destination_db = tmp_path / "destination.sqlite"

    make_database(source_db, marker="source")
    make_database(destination_db, marker="original_destination")

    try:
        sqlite_safety.backup_database(source_db, destination_db)
    except sqlite_safety.SQLiteSafetyError as exc:
        assert "destination already exists" in str(exc)
    else:
        raise AssertionError("expected destination overwrite protection")

    assert read_marker(destination_db) == "original_destination"


def test_backup_database_allows_explicit_overwrite(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    destination_db = tmp_path / "destination.sqlite"

    make_database(source_db, marker="source")
    make_database(destination_db, marker="original_destination")

    report = sqlite_safety.backup_database(source_db, destination_db, overwrite=True)

    assert report["status"] == "pass"
    assert report["backup_path"] == str(destination_db)
    assert read_marker(destination_db) == "source"


def test_backup_database_rejects_same_path(tmp_path: Path) -> None:
    source_db = tmp_path / "db.sqlite"
    make_database(source_db, marker="source")

    try:
        sqlite_safety.backup_database(source_db, source_db)
    except sqlite_safety.SQLiteSafetyError as exc:
        assert "must not be the same database path" in str(exc)
    else:
        raise AssertionError("expected same-path safety guard")
