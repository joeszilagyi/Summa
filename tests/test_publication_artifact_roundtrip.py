from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.validators.validate_knowledge_tree_export import EXIT_PASS as EXIT_EXPORT_PASS
from tools.validators.validate_knowledge_tree_export import validate_knowledge_tree_export
from tools.validators.validate_public_knowledge_tree_presentation import EXIT_PASS as EXIT_PRESENTATION_PASS
from tools.validators.validate_public_knowledge_tree_presentation import validate_public_knowledge_tree_presentation

from tests.publication_fixture_store import FIXED_TIMESTAMP, PRIVATE_SENTINEL, UNREVIEWED_SENTINEL, create_populated_canonical_store


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "tools" / "scripts" / "build_publication_artifacts.py"
WRAPPER_SCRIPT = REPO_ROOT / "tools" / "scripts" / "Index_Build_Knowledge_Tree.sh"


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_wrapper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(WRAPPER_SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_publication_artifact_roundtrip_builds_valid_outputs(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    output_dir = tmp_path / "site-build"

    result = run_builder(
        "--db",
        str(db_path),
        "--output-dir",
        str(output_dir),
        "--generated-at",
        FIXED_TIMESTAMP,
        "--build-id",
        "build-20260603T090000Z",
        "--built-at",
        FIXED_TIMESTAMP,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)

    export_path = output_dir / "knowledge_tree_export.json"
    presentation_path = output_dir / "public_presentation.json"
    publish_root = output_dir / "static"
    leak_report_path = output_dir / "leak-scan-report.json"
    assert export_path.is_file()
    assert presentation_path.is_file()
    assert leak_report_path.is_file()
    assert (publish_root / "index.html").is_file()
    assert (publish_root / "search" / "results.html").is_file()
    assert (output_dir / "search" / "local_search_projection.json").is_file()
    assert (output_dir / "search" / "local_search_results.json").is_file()
    assert (output_dir / "search" / "local_search.sqlite").is_file()

    export_report, export_exit = validate_knowledge_tree_export(export_path)
    presentation_report, presentation_exit = validate_public_knowledge_tree_presentation(presentation_path)
    assert export_exit == EXIT_EXPORT_PASS, export_report
    assert presentation_exit == EXIT_PRESENTATION_PASS, presentation_report

    leak_report = json.loads(leak_report_path.read_text(encoding="utf-8"))
    assert leak_report["status"] == "pass"
    assert report["leak_scan"]["status"] == "pass"

    for path in sorted(publish_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "build-manifest.json":
            continue
        body = path.read_text(encoding="utf-8")
        assert PRIVATE_SENTINEL not in body
        assert UNREVIEWED_SENTINEL not in body
    assert PRIVATE_SENTINEL not in export_path.read_text(encoding="utf-8")
    assert PRIVATE_SENTINEL not in presentation_path.read_text(encoding="utf-8")


def test_index_build_knowledge_tree_wrapper_help_and_dry_run() -> None:
    help_result = run_wrapper("--help")
    dry_run_result = run_wrapper("--dry-run", "--", "--help")

    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "Index_Build_Knowledge_Tree.sh" in help_result.stdout
    assert dry_run_result.returncode == 0, dry_run_result.stdout + dry_run_result.stderr
    assert "build_publication_artifacts.py" in dry_run_result.stdout
