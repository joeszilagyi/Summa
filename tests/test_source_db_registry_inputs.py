from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB_DIR = REPO_ROOT / "tools" / "source_db_tools"


def load_module(module_name: str, relative_path: str):
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


source_types = load_module("source_types_for_tests", "tools/source_db_tools/source_types.py")
claim_types = load_module("claim_types_for_tests", "tools/source_db_tools/claim_types.py")
relationship_predicates = load_module("relationship_predicates_for_tests", "tools/source_db_tools/relationship_predicates.py")
identifier_normalization = load_module("identifier_normalization_for_tests", "tools/source_db_tools/identifier_normalization.py")
legacy_backfill = load_module("legacy_backfill_for_tests", "tools/source_db_tools/legacy_backfill.py")
authority_reconciliation = load_module("authority_reconciliation_for_tests", "tools/source_db_tools/authority_reconciliation.py")
schema_profile_validation = load_module("schema_profile_validation_for_tests", "tools/source_db_tools/schema_profile_validation.py")


def create_schema_profile_db(tmp_path: Path) -> Path:
    db = tmp_path / "source.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_key_v1 TEXT,
              work_type TEXT,
              title TEXT
            );
            CREATE TABLE work_identifier (
              work_identifier_id INTEGER PRIMARY KEY,
              work_id INTEGER,
              scheme TEXT,
              value TEXT
            );
            CREATE TABLE source_access (
              source_access_id INTEGER PRIMARY KEY,
              work_id INTEGER,
              original_locator TEXT
            );
            INSERT INTO work (work_id, work_key_v1, work_type, title)
            VALUES (1, 'work:fixture:1', 'webpage', 'Fixture source');
            INSERT INTO work_identifier (work_identifier_id, work_id, scheme, value)
            VALUES (1, 1, 'doi', '10.1000/fixture');
            INSERT INTO source_access (source_access_id, work_id, original_locator)
            VALUES (1, 1, 'https://example.invalid/fixture');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_source_db_registries_load() -> None:
    assert source_types.load_registry()["schema_version"] == "source-type-registry.v1"
    assert claim_types.load_registry()["schema_version"] == "claim-type-registry.v1"
    assert relationship_predicates.load_registry()["schema_version"] == "relationship-predicate-registry.v1"
    assert "canonical_minimal" in json.loads((SOURCE_DB_DIR / "schema_profiles.json").read_text(encoding="utf-8"))["profiles"]
    assert callable(legacy_backfill.infer_work_type)
    assert callable(authority_reconciliation.identifier_normalization.identifier_storage_values)
    assert "canonical_minimal" in schema_profile_validation.profile_names()


def test_source_type_helper_reports_provisional_type() -> None:
    issue = source_types.validation_issue("local:legacy_record")

    assert issue is not None
    assert issue[0] == "PROVISIONAL_SOURCE_TYPE"


def test_identifier_normalization_helpers_are_loadable() -> None:
    normalized = identifier_normalization.normalize_identifier_row(
        {"scheme": "doi", "value": "https://doi.org/10.1000/FIXTURE"}
    )

    assert normalized["scheme"] == "doi"
    assert normalized["normalized_value"] == "10.1000/fixture"
    assert normalized["validity_status"] == "valid"


def test_validate_schema_profile_cli_uses_restored_helpers_and_registries(tmp_path: Path) -> None:
    db = create_schema_profile_db(tmp_path)
    tool = REPO_ROOT / "tools" / "source_db_tools" / "validate_schema_profile.py"

    proc = subprocess.run(
        [
            sys.executable,
            str(tool),
            str(db),
            "--profile",
            "canonical_minimal",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "schema-profile-validation-report.v1"
    assert payload["profile"] == "canonical_minimal"
    assert payload["ok"] is True


def test_source_db_helper_documentation_paths_exist() -> None:
    doc_path_pattern = re.compile(r"docs/tools/source_db_tools/[A-Za-z0-9_./-]+\.md")
    referenced_paths: set[str] = set()

    for module_path in SOURCE_DB_DIR.glob("*.py"):
        matches = doc_path_pattern.findall(module_path.read_text(encoding="utf-8"))
        referenced_paths.update(matches)

    assert referenced_paths
    missing = [relative_path for relative_path in sorted(referenced_paths) if not (REPO_ROOT / relative_path).exists()]
    assert missing == []
