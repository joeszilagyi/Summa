from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "tools" / "pipeline_registry" / "build_pipeline_registry.py"

spec = importlib.util.spec_from_file_location("pipeline_registry_builder_for_tests", BUILDER)
assert spec is not None
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


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


def test_collect_current_files_uses_inventory_file_when_git_is_missing(tmp_path: Path) -> None:
    repo_root = tmp_path / "archive"
    repo_root.mkdir()
    inventory = tmp_path / "inventory.txt"
    inventory.write_text(
        "\n".join(
            [
                "# archive inventory",
                "",
                "tools/scripts/build_release_readiness_bundle.py",
                "config/release_readiness_report.schema.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    files = builder.collect_current_files(repo_root, inventory_file=inventory)

    assert files == [
        "config/release_readiness_report.schema.json",
        "tools/scripts/build_release_readiness_bundle.py",
    ]


def test_collect_current_files_requires_inventory_file_without_git(tmp_path: Path) -> None:
    repo_root = tmp_path / "archive"
    repo_root.mkdir()

    with pytest.raises(SystemExit, match="inventory-file"):
        builder.collect_current_files(repo_root)


def test_glob_matches_caches_compiled_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    builder.compile_glob_regex.cache_clear()
    compile_calls: list[str] = []
    real_compile = builder.re.compile

    def tracking_compile(pattern: str, flags: int = 0):
        compile_calls.append(pattern)
        return real_compile(pattern, flags)

    monkeypatch.setattr(builder.re, "compile", tracking_compile)

    assert builder.glob_matches("tools/scripts/build_pipeline_registry.py", "tools/scripts/*.py")
    assert builder.glob_matches("tools/scripts/build_release_readiness_bundle.py", "tools/scripts/*.py")
    assert compile_calls == [r"tools/scripts/[^/]*\.py"]
