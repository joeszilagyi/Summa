from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.publication_fixture_store import (
    FIXED_TIMESTAMP,
    PRIVATE_SENTINEL,
    UNREVIEWED_SENTINEL,
    create_populated_canonical_store,
)
from tools.validators.validate_knowledge_tree_export import EXIT_PASS as EXIT_EXPORT_PASS
from tools.validators.validate_knowledge_tree_export import validate_knowledge_tree_export
from tools.validators.validate_public_knowledge_tree_presentation import (
    EXIT_PASS as EXIT_PRESENTATION_PASS,
)
from tools.validators.validate_public_knowledge_tree_presentation import (
    validate_public_knowledge_tree_presentation,
)

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


def insert_orphan_source_claim(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO source_claim (
              source_claim_key_v1,
              claim_text,
              public_summary,
              claim_type,
              review_state,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "claim:publication:orphan",
                "orphan claim",
                "orphan claim",
                "factual",
                "accepted",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.commit()
    finally:
        conn.close()


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
    graph_report_path = output_dir / "graph-closure-report.json"
    assert export_path.is_file()
    assert presentation_path.is_file()
    assert leak_report_path.is_file()
    assert graph_report_path.is_file()
    assert (publish_root / "index.html").is_file()
    assert (publish_root / "search" / "results.html").is_file()
    assert (output_dir / "search" / "local_search_projection.json").is_file()
    assert (output_dir / "search" / "local_search_results.json").is_file()
    assert (output_dir / "search" / "local_search.sqlite").is_file()
    assert report["output_dir"] == "."
    assert report["export_path"] == "knowledge_tree_export.json"
    assert report["presentation_path"] == "public_presentation.json"
    assert report["publish_root"] == "static"
    assert report["search_projection_path"] == "search/local_search_projection.json"
    assert report["search_results_path"] == "search/local_search_results.json"
    assert report["search_index_db"] == "search/local_search.sqlite"
    assert report["leak_report_path"] == "leak-scan-report.json"
    assert report["static_build"]["export_path"] == "knowledge_tree_export.json"
    assert report["static_build"]["presentation_path"] == "public_presentation.json"
    assert report["static_build"]["publish_root"] == "static"
    assert report["static_build"]["manifest_path"] == "static/build-manifest.json"

    export_report, export_exit = validate_knowledge_tree_export(export_path)
    presentation_report, presentation_exit = validate_public_knowledge_tree_presentation(
        presentation_path
    )
    assert export_exit == EXIT_EXPORT_PASS, export_report
    assert presentation_exit == EXIT_PRESENTATION_PASS, presentation_report

    leak_report = json.loads(leak_report_path.read_text(encoding="utf-8"))
    assert leak_report["status"] == "pass"
    assert report["leak_scan"]["status"] == "pass"
    graph_report = json.loads(graph_report_path.read_text(encoding="utf-8"))
    assert graph_report["status"] in {"pass", "pass_with_unresolved"}
    assert report["graph_closure"]["status"] in {"pass", "pass_with_unresolved"}

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
    search_projection_path = output_dir / "search" / "local_search_projection.json"
    search_results_path = output_dir / "search" / "local_search_results.json"
    assert search_projection_path.is_file()
    assert search_results_path.is_file()
    for body in (
        search_projection_path.read_text(encoding="utf-8"),
        search_results_path.read_text(encoding="utf-8"),
    ):
        assert PRIVATE_SENTINEL not in body
        assert UNREVIEWED_SENTINEL not in body


def test_publication_strict_graph_closure_preflight_fails_on_orphan(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    insert_orphan_source_claim(db_path)
    output_dir = tmp_path / "site-build"

    result = run_builder(
        "--db",
        str(db_path),
        "--output-dir",
        str(output_dir),
        "--generated-at",
        FIXED_TIMESTAMP,
        "--graph-closure-strict",
    )

    assert result.returncode == 1
    assert "graph closure preflight found true orphan errors" in result.stderr
    graph_report_path = output_dir / "graph-closure-report.json"
    assert graph_report_path.is_file()
    graph_report = json.loads(graph_report_path.read_text(encoding="utf-8"))
    assert graph_report["status"] == "fail"


def test_publication_and_search_artifacts_are_stable_across_vacuum(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    before_dir = tmp_path / "site-build-before"
    after_dir = tmp_path / "site-build-after"

    first = run_builder(
        "--db",
        str(db_path),
        "--output-dir",
        str(before_dir),
        "--generated-at",
        FIXED_TIMESTAMP,
        "--build-id",
        "build-20260603T090000Z",
        "--built-at",
        FIXED_TIMESTAMP,
    )
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    second = run_builder(
        "--db",
        str(db_path),
        "--output-dir",
        str(after_dir),
        "--generated-at",
        FIXED_TIMESTAMP,
        "--build-id",
        "build-20260603T090000Z",
        "--built-at",
        FIXED_TIMESTAMP,
    )

    assert second.returncode == 0, second.stdout + second.stderr

    before_artifacts = sorted(
        path.relative_to(before_dir).as_posix()
        for path in before_dir.rglob("*")
        if path.is_file() and path.relative_to(before_dir).as_posix() != "static/build-manifest.json"
    )
    after_artifacts = sorted(
        path.relative_to(after_dir).as_posix()
        for path in after_dir.rglob("*")
        if path.is_file() and path.relative_to(after_dir).as_posix() != "static/build-manifest.json"
    )
    assert before_artifacts == after_artifacts
    for relative_path in before_artifacts:
        before_path = before_dir / relative_path
        after_path = after_dir / relative_path
        assert before_path.read_bytes() == after_path.read_bytes()


def test_index_build_knowledge_tree_wrapper_help_and_dry_run() -> None:
    help_result = run_wrapper("--help")
    dry_run_result = run_wrapper("--dry-run", "--", "--help")

    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "Index_Build_Knowledge_Tree.sh" in help_result.stdout
    assert dry_run_result.returncode == 0, dry_run_result.stdout + dry_run_result.stderr
    assert "build_publication_artifacts.py" in dry_run_result.stdout


def test_index_build_knowledge_tree_docs_example_executes_in_dry_run(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    output_dir = tmp_path / "site-build"

    proc = run_wrapper(
        "--dry-run",
        "--",
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

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DRY-RUN:" in proc.stdout
    assert "build_publication_artifacts.py" in proc.stdout
