from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.validators.validate_public_knowledge_tree_presentation import EXIT_PASS as EXIT_PRESENTATION_PASS
from tools.validators.validate_public_knowledge_tree_presentation import validate_public_knowledge_tree_presentation

from tests.publication_fixture_store import FIXED_TIMESTAMP, PRIVATE_SENTINEL, UNREVIEWED_SENTINEL, create_populated_canonical_store, create_sparse_canonical_store


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORT_SCRIPT = REPO_ROOT / "tools" / "scripts" / "build_knowledge_tree_export.py"
PRESENTATION_SCRIPT = REPO_ROOT / "tools" / "scripts" / "build_public_knowledge_tree_presentation.py"


def run_export(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_presentation(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PRESENTATION_SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def build_export(tmp_path: Path, *, sparse: bool) -> Path:
    db_path = create_sparse_canonical_store(tmp_path) if sparse else create_populated_canonical_store(tmp_path)
    export_path = tmp_path / "knowledge_tree_export.json"
    result = run_export("--db", str(db_path), "--output", str(export_path), "--generated-at", FIXED_TIMESTAMP)
    assert result.returncode == 0, result.stdout + result.stderr
    return export_path


def load_presentation(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_presentation_builder_help_exits_zero() -> None:
    result = run_presentation("--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Build a validated public-presentation JSON artifact" in result.stdout


def test_populated_export_builds_valid_presentation(tmp_path: Path) -> None:
    export_path = build_export(tmp_path, sparse=False)
    output_path = tmp_path / "public_presentation.json"

    result = run_presentation("--export", str(export_path), "--output", str(output_path))

    assert result.returncode == 0, result.stdout + result.stderr
    stdout_report = json.loads(result.stdout)
    payload = load_presentation(output_path)
    report, exit_code = validate_public_knowledge_tree_presentation(output_path)
    assert exit_code == EXIT_PRESENTATION_PASS, report
    assert stdout_report["export_path"] == export_path.name
    assert stdout_report["output_path"] == output_path.name
    families = [page["page_family"] for page in payload["page_inventory"]]
    assert families == [
        "home",
        "facet",
        "entity",
        "source",
        "collection",
        "timeline",
        "validation",
        "search_results",
    ]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert PRIVATE_SENTINEL not in rendered
    assert UNREVIEWED_SENTINEL not in rendered
    search_page = next(page for page in payload["page_inventory"] if page["page_family"] == "search_results")
    assert search_page["route"] == "search/results.html"
    assert search_page["reader_state"] == "ready"


def test_sparse_export_builds_valid_sparse_presentation(tmp_path: Path) -> None:
    export_path = build_export(tmp_path, sparse=True)
    output_path = tmp_path / "public_presentation.json"

    result = run_presentation("--export", str(export_path), "--output", str(output_path))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = load_presentation(output_path)
    report, exit_code = validate_public_knowledge_tree_presentation(output_path)
    assert exit_code == EXIT_PRESENTATION_PASS, report
    by_family = {page["page_family"]: page for page in payload["page_inventory"]}
    assert by_family["home"]["reader_state"] == "sparse"
    assert by_family["entity"]["reader_state"] == "empty"
    assert by_family["source"]["reader_state"] == "empty"
    assert by_family["search_results"]["reader_state"] == "empty"
    assert by_family["validation"]["reader_state"] == "ready"


def test_presentation_builder_rejects_invalid_export_before_rendering(tmp_path: Path) -> None:
    export_path = build_export(tmp_path, sparse=False)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "broken"
    export_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    output_path = tmp_path / "public_presentation.json"
    result = run_presentation("--export", str(export_path), "--output", str(output_path))

    assert result.returncode != 0
    assert "schema_version must equal knowledge-tree-export.v1" in result.stderr
    assert not output_path.exists()
