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
    create_sparse_canonical_store,
)
from tools.common.publication_builder import table_fingerprint
from tools.validators.validate_knowledge_tree_export import EXIT_PASS as EXIT_EXPORT_PASS
from tools.validators.validate_knowledge_tree_export import validate_knowledge_tree_export

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "scripts" / "build_knowledge_tree_export.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def load_export(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_export_builder_help_exits_zero() -> None:
    result = run_cli("--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Build a validated knowledge-tree export JSON artifact" in result.stdout


def test_populated_fixture_store_builds_valid_export(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    output_path = tmp_path / "knowledge_tree_export.json"
    search_dir = tmp_path / "search"

    result = run_cli(
        "--db",
        str(db_path),
        "--output",
        str(output_path),
        "--generated-at",
        FIXED_TIMESTAMP,
        "--search-output-dir",
        str(search_dir),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    stdout_report = json.loads(result.stdout)
    payload = load_export(output_path)
    validator_report, exit_code = validate_knowledge_tree_export(output_path)
    assert exit_code == EXIT_EXPORT_PASS, validator_report
    assert stdout_report["db_path"] == db_path.name or not Path(stdout_report["db_path"]).is_absolute()
    assert stdout_report["output_path"] == output_path.name
    assert stdout_report["search_projection_path"] == "search/local_search_projection.json"
    assert stdout_report["search_results_path"] == "search/local_search_results.json"
    assert payload["page_families"] == [
        "home",
        "facet",
        "entity",
        "source",
        "collection",
        "timeline",
        "validation",
        "search_results",
    ]
    assert len(payload["pages"]) == 8
    page_families = {page["page_family"] for page in payload["pages"]}
    assert page_families == set(payload["page_families"])

    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert PRIVATE_SENTINEL not in rendered
    assert UNREVIEWED_SENTINEL not in rendered
    assert "Alpha Chronicle documents Alpha Example." in rendered
    assert "Unreviewed summary should not publish." not in rendered

    search_page = next(page for page in payload["pages"] if page["page_family"] == "search_results")
    assert search_page["route"] == "search/results.html"
    assert (search_dir / "local_search_projection.json").is_file()
    assert (search_dir / "local_search_results.json").is_file()
    assert (search_dir / "local_search.sqlite").is_file()


def test_sparse_fixture_store_builds_valid_export(tmp_path: Path) -> None:
    db_path = create_sparse_canonical_store(tmp_path)
    output_path = tmp_path / "knowledge_tree_export.json"

    result = run_cli(
        "--db",
        str(db_path),
        "--output",
        str(output_path),
        "--generated-at",
        FIXED_TIMESTAMP,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = load_export(output_path)
    report, exit_code = validate_knowledge_tree_export(output_path)
    assert exit_code == EXIT_EXPORT_PASS, report
    assert len(payload["pages"]) == 8
    notes = payload["notes"]
    assert isinstance(notes, dict)
    hints = {item["page_id"]: item for item in notes["page_inventory_hints"]}
    assert hints["home"]["reader_state"] == "sparse"
    assert hints["entity_index"]["reader_state"] == "empty"
    assert hints["search_results"]["reader_state"] == "empty"


def test_export_builder_fails_clearly_when_required_table_is_missing(tmp_path: Path) -> None:
    db_path = create_sparse_canonical_store(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE source_claim")
        conn.commit()
    finally:
        conn.close()

    output_path = tmp_path / "knowledge_tree_export.json"
    result = run_cli("--db", str(db_path), "--output", str(output_path), "--generated-at", FIXED_TIMESTAMP)

    assert result.returncode != 0
    assert "missing required publication tables" in result.stderr
    assert not output_path.exists()


def test_export_builder_is_deterministic_with_fixed_timestamp(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    output_a = tmp_path / "export-a.json"
    output_b = tmp_path / "export-b.json"

    first = run_cli("--db", str(db_path), "--output", str(output_a), "--generated-at", FIXED_TIMESTAMP)
    second = run_cli("--db", str(db_path), "--output", str(output_b), "--generated-at", FIXED_TIMESTAMP)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert output_a.read_text(encoding="utf-8") == output_b.read_text(encoding="utf-8")


def test_export_builder_records_logical_and_storage_fingerprints(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    output_path = tmp_path / "knowledge_tree_export.json"

    result = run_cli(
        "--db",
        str(db_path),
        "--output",
        str(output_path),
        "--generated-at",
        FIXED_TIMESTAMP,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = load_export(output_path)

    builder_notes = payload["notes"]["builder"]
    source = payload["input_sources"][0]
    assert builder_notes["canonical_store_fingerprint"] == source["fingerprint"]
    assert builder_notes["canonical_store_fingerprint"].startswith("sha256:")
    assert builder_notes["canonical_store_storage_fingerprint"].startswith("sha256:")
    assert builder_notes["canonical_store_storage_fingerprint"] != builder_notes["canonical_store_fingerprint"]
    table_fingerprints = builder_notes["canonical_store_table_fingerprints"]
    assert isinstance(table_fingerprints, dict)
    assert "authority_record" in table_fingerprints
    assert table_fingerprints["authority_record"].startswith("sha256:")
    query_fingerprints = builder_notes["canonical_store_query_fingerprints"]
    assert isinstance(query_fingerprints, dict)
    assert query_fingerprints["public_works"].startswith("sha256:")
    assert query_fingerprints["public_works"] != builder_notes["canonical_store_storage_fingerprint"]

    # Rebuild once more from the same DB to ensure all logical/source fingerprints remain stable.
    output_path_alt = tmp_path / "knowledge_tree_export_alt.json"
    rerun = run_cli(
        "--db",
        str(db_path),
        "--output",
        str(output_path_alt),
        "--generated-at",
        FIXED_TIMESTAMP,
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    payload_alt = load_export(output_path_alt)
    assert payload_alt["notes"]["builder"]["canonical_store_fingerprint"] == source["fingerprint"]
    assert payload_alt["notes"]["builder"]["canonical_store_storage_fingerprint"] == builder_notes["canonical_store_storage_fingerprint"]
    assert payload_alt["notes"]["builder"]["canonical_store_table_fingerprints"] == table_fingerprints
    assert payload_alt["notes"]["builder"]["canonical_store_query_fingerprints"] == query_fingerprints


def test_table_fingerprint_handles_sqlite_row_mapping(tmp_path: Path) -> None:
    db_path = tmp_path / "fingerprint.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE demo (demo_id INTEGER PRIMARY KEY, label TEXT, is_public INTEGER)")
        conn.execute("INSERT INTO demo (label, is_public) VALUES (?, ?)", ("alpha", 1))
        conn.commit()
        fingerprint = table_fingerprint(conn, "demo")
    finally:
        conn.close()

    assert fingerprint.startswith("sha256:")


def test_export_builder_uses_logical_fingerprint_for_publication_inputs(tmp_path: Path) -> None:
    db_path = create_populated_canonical_store(tmp_path)
    output_path = tmp_path / "knowledge_tree_export.json"
    vacuumed_output_path = tmp_path / "knowledge_tree_export_vacuum.json"

    first = run_cli(
        "--db",
        str(db_path),
        "--output",
        str(output_path),
        "--generated-at",
        FIXED_TIMESTAMP,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    payload = load_export(output_path)
    source = payload["input_sources"][0]
    builder_notes = payload["notes"]["builder"]

    conn = sqlite3.connect(db_path)
    conn.execute("VACUUM")
    conn.close()

    second = run_cli(
        "--db",
        str(db_path),
        "--output",
        str(vacuumed_output_path),
        "--generated-at",
        FIXED_TIMESTAMP,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    vacuumed_payload = load_export(vacuumed_output_path)
    vacuumed_source = vacuumed_payload["input_sources"][0]
    vacuumed_notes = vacuumed_payload["notes"]["builder"]

    assert vacuumed_source["fingerprint"] == source["fingerprint"]
    assert vacuumed_notes["canonical_store_fingerprint"] == builder_notes["canonical_store_fingerprint"]
    assert vacuumed_notes["canonical_store_storage_fingerprint"] != source["fingerprint"]
