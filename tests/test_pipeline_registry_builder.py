from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "tools" / "pipeline_registry" / "build_pipeline_registry.py"


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_pipeline_registry_builder_smoke(tmp_path: Path) -> None:
    output_db = tmp_path / "pipeline_registry.sqlite"

    result = run_builder("--output-db", str(output_db))

    assert result.returncode == 0, result.stdout + result.stderr
    assert output_db.exists()

    conn = sqlite3.connect(output_db)
    try:
        surface_count = conn.execute("SELECT COUNT(*) FROM surface").fetchone()[0]
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifact_class").fetchone()[0]
        current_surface_count = conn.execute(
            "SELECT COUNT(*) FROM repo_file WHERE tracking_status='current' AND path_kind='surface'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert surface_count > 0
    assert artifact_count > 0
    assert current_surface_count > 0
