from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "tools" / "scripts" / "build_local_search_projection.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_local_search_projection.py"

spec = importlib.util.spec_from_file_location("local_search_projection_validator_for_tests", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)

builder_spec = importlib.util.spec_from_file_location(
    "local_search_projection_builder_for_tests",
    BUILDER,
)
builder = importlib.util.module_from_spec(builder_spec)
assert builder_spec.loader is not None
sys.modules[builder_spec.name] = builder
builder_spec.loader.exec_module(builder)


def create_search_db(tmp_path: Path) -> Path:
    db = tmp_path / "search.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              publication_state TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              workspace_id TEXT
            );
            CREATE TABLE source_claim (
              source_claim_id INTEGER PRIMARY KEY,
              claim_text TEXT NOT NULL,
              public_summary TEXT,
              claim_type TEXT,
              review_state TEXT,
              publication_state TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              workspace_id TEXT
            );
            CREATE TABLE source_access (
              source_access_id INTEGER PRIMARY KEY,
              original_locator TEXT,
              canonical_url TEXT,
              access_class TEXT,
              review_state TEXT,
              publication_state TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              workspace_id TEXT
            );
            INSERT INTO work (
              work_id, work_type, title, review_state, publication_state,
              authority_level, public_blocker, workspace_id
            ) VALUES
              (1, 'book', 'Public Work', 'reviewed', 'public_release_allowed', 'primary', NULL, 'alpha_subject'),
              (2, 'book', 'Superseded Work', 'reviewed', 'public_release_allowed', 'primary', NULL, 'alpha_subject'),
              (4, 'book', 'Replacement Work', 'reviewed', 'public_release_allowed', 'primary', NULL, 'alpha_subject'),
              (9, 'book', 'Pending Work', 'needs_review', 'public_release_allowed', 'primary', NULL, 'alpha_subject');
            INSERT INTO source_claim (
              source_claim_id, claim_text, public_summary, claim_type, review_state,
              publication_state, authority_level, public_blocker, workspace_id
            ) VALUES
              (1, 'localclaimmarker internal review text', 'Public claim summary', 'factual', 'reviewed', 'public_preview', 'primary', NULL, 'alpha_subject'),
              (2, 'blockedlocalclaimmarker internal only', 'Blocked claim summary', 'factual', 'reviewed', 'public_preview', 'primary', 'authority_gap', 'alpha_subject');
            INSERT INTO source_access (
              source_access_id, original_locator, canonical_url, access_class, review_state,
              publication_state, authority_level, public_blocker, workspace_id
            ) VALUES
              (1, '/Users/joe/cacheprivatemarker/source.pdf', 'https://example.org/source.pdf', 'web_capture', 'reviewed', 'public_release_allowed', 'primary', NULL, 'alpha_subject');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def create_correction_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "correction_ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "correction-ledger.v1",
                "workspace_id": "alpha_subject",
                "events": [
                    {
                        "event_id": "cle:work-supersede-1",
                        "action": "supersede",
                        "changed_at": "2026-06-02T00:00:00Z",
                        "changed_by": "pytest",
                        "rationale": "Fixture supersession",
                        "source_object_refs": ["work:2"],
                        "result_object_refs": ["work:4"],
                        "review_queue_refs": ["work:2"],
                        "provenance_event_refs": ["prov:11111111-1111-1111-1111-111111111111"],
                        "evidence_locator_refs": [],
                        "field_review_entry_refs": [],
                        "note": None
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return ledger


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def validate_projection(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_local_search_projection(path)


def fts_matches(index_db: Path, query: str) -> list[str]:
    conn = sqlite3.connect(index_db)
    try:
        rows = conn.execute(
            "SELECT object_ref FROM search_projection_fts WHERE search_projection_fts MATCH ? ORDER BY object_ref",
            (query,),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def test_public_projection_excludes_superseded_blocked_and_local_only_fields(tmp_path: Path) -> None:
    db = create_search_db(tmp_path)
    ledger = create_correction_ledger(tmp_path)
    output_json = tmp_path / "public_projection.json"
    index_db = tmp_path / "public_projection.sqlite"

    result = run_builder(
        "--db",
        str(db),
        "--profile",
        "public_preview",
        "--correction-ledger",
        str(ledger),
        "--index-db",
        str(index_db),
        "--output-json",
        str(output_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report, exit_code = validate_projection(output_json)
    assert exit_code == validator.EXIT_PASS, report

    payload = json.loads(output_json.read_text(encoding="utf-8"))
    refs = {record["object_ref"] for record in payload["records"]}
    assert refs == {"claim:1", "source_access:1", "work:1", "work:4"}
    assert {"object_ref": "work:2", "reason": "superseded_in_public_profile"} in payload["excluded_records"]
    assert {"object_ref": "claim:2", "reason": "public_blocker"} in payload["excluded_records"]
    claim_record = next(record for record in payload["records"] if record["object_ref"] == "claim:1")
    assert claim_record["suppressed_fields"] == ["claim_text"]
    assert [field["field"] for field in claim_record["indexed_fields"]] == ["public_summary", "claim_type"]
    access_record = next(record for record in payload["records"] if record["object_ref"] == "source_access:1")
    assert access_record["suppressed_fields"] == ["original_locator"]
    assert [field["field"] for field in access_record["indexed_fields"]] == ["canonical_url", "access_class"]
    assert payload["policy"]["private_paths_exposed"] is False
    assert fts_matches(index_db, "cacheprivatemarker") == []
    assert fts_matches(index_db, "localclaimmarker") == []
    assert fts_matches(index_db, "Public") == ["claim:1", "work:1"]


def test_local_projection_includes_superseded_and_local_only_fields(tmp_path: Path) -> None:
    db = create_search_db(tmp_path)
    ledger = create_correction_ledger(tmp_path)
    output_json = tmp_path / "local_projection.json"
    index_db = tmp_path / "local_projection.sqlite"

    result = run_builder(
        "--db",
        str(db),
        "--profile",
        "local",
        "--correction-ledger",
        str(ledger),
        "--index-db",
        str(index_db),
        "--output-json",
        str(output_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
        "--format",
        "text",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_version=local-search-projection.v1" in result.stdout
    report, exit_code = validate_projection(output_json)
    assert exit_code == validator.EXIT_PASS, report

    payload = json.loads(output_json.read_text(encoding="utf-8"))
    refs = {record["object_ref"] for record in payload["records"]}
    assert refs == {"claim:1", "claim:2", "source_access:1", "work:1", "work:2", "work:4"}
    superseded = next(record for record in payload["records"] if record["object_ref"] == "work:2")
    assert superseded["lineage_state"] == "superseded"
    claim_record = next(record for record in payload["records"] if record["object_ref"] == "claim:1")
    assert [field["field"] for field in claim_record["indexed_fields"]] == ["public_summary", "claim_text", "claim_type"]
    access_record = next(record for record in payload["records"] if record["object_ref"] == "source_access:1")
    assert [field["field"] for field in access_record["indexed_fields"]] == ["canonical_url", "original_locator", "access_class"]
    assert payload["policy"]["private_paths_exposed"] is True
    assert payload["policy"]["superseded_records_included"] is True
    assert payload["policy"]["blocked_records_included"] is True
    assert fts_matches(index_db, "cacheprivatemarker") == ["source_access:1"]
    assert fts_matches(index_db, "localclaimmarker") == ["claim:1"]
    assert fts_matches(index_db, "Superseded") == ["work:2"]


def test_public_projection_builder_blocks_secret_like_leaks(tmp_path: Path) -> None:
    db = tmp_path / "search.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              publication_state TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              workspace_id TEXT
            );
            INSERT INTO work (
              work_id, work_type, title, review_state, publication_state,
              authority_level, public_blocker, workspace_id
            ) VALUES
              (1, 'book', 'api_key=leaked Public Work', 'reviewed', 'public_release_allowed', 'primary', NULL, 'alpha_subject');
            """
        )
        conn.commit()
    finally:
        conn.close()

    output_json = tmp_path / "public_projection.json"
    index_db = tmp_path / "public_projection.sqlite"

    result = run_builder(
        "--db",
        str(db),
        "--profile",
        "public_preview",
        "--index-db",
        str(index_db),
        "--output-json",
        str(output_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "public search leak validation failed" in combined
    assert "SECRET_MARKER_EXPOSED" in combined


def test_builder_refuses_same_source_and_index_path(tmp_path: Path) -> None:
    db = create_search_db(tmp_path)

    result = run_builder(
        "--db",
        str(db),
        "--profile",
        "local",
        "--index-db",
        str(db),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "index output path must differ from source database path" in combined
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT COUNT(*) FROM work").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert int(row[0]) == 4


def test_builder_refuses_existing_non_projection_index_db(tmp_path: Path) -> None:
    db = create_search_db(tmp_path)
    index_db = tmp_path / "not_a_projection.sqlite"
    conn = sqlite3.connect(index_db)
    try:
        conn.execute("CREATE TABLE keep_me (value TEXT NOT NULL)")
        conn.execute("INSERT INTO keep_me(value) VALUES ('sentinel')")
        conn.commit()
    finally:
        conn.close()

    result = run_builder(
        "--db",
        str(db),
        "--profile",
        "local",
        "--index-db",
        str(index_db),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "refusing to overwrite existing SQLite file without projection marker" in combined
    conn = sqlite3.connect(index_db)
    try:
        row = conn.execute("SELECT value FROM keep_me").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "sentinel"


def test_builder_replaces_existing_projection_index(tmp_path: Path) -> None:
    db = create_search_db(tmp_path)
    output_json = tmp_path / "local_projection.json"
    index_db = tmp_path / "local_projection.sqlite"

    first = run_builder(
        "--db",
        str(db),
        "--profile",
        "local",
        "--index-db",
        str(index_db),
        "--output-json",
        str(output_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE work SET title='Updated Public Work' WHERE work_id=1")
        conn.commit()
    finally:
        conn.close()

    second = run_builder(
        "--db",
        str(db),
        "--profile",
        "local",
        "--index-db",
        str(index_db),
        "--output-json",
        str(output_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert second.returncode == 0, second.stdout + second.stderr
    conn = sqlite3.connect(index_db)
    try:
        row = conn.execute(
            """
            SELECT title
            FROM search_projection
            WHERE object_ref='work:1'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "Updated Public Work"


def test_builder_keeps_previous_projection_index_if_validation_fails(tmp_path: Path, monkeypatch) -> None:
    db = create_search_db(tmp_path)
    index_db = tmp_path / "local_projection.sqlite"

    first = run_builder(
        "--db",
        str(db),
        "--profile",
        "local",
        "--index-db",
        str(index_db),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE work SET title='Updated Public Work' WHERE work_id=1")
        conn.commit()
    finally:
        conn.close()

    def fail_validation(index_path: Path, payload: dict[str, object]) -> None:
        raise builder.SearchProjectionError("forced validation failure")

    monkeypatch.setattr(builder, "validate_projection_index_file", fail_validation)

    args = SimpleNamespace(
        db=str(db),
        profile="local",
        correction_ledger=None,
        generated_at="2026-06-02T00:00:00Z",
    )
    with pytest.raises(builder.SearchProjectionError):
        builder.write_index(index_db, builder.build_projection_payload(args))

    conn = sqlite3.connect(index_db)
    try:
        row = conn.execute(
            """
            SELECT title
            FROM search_projection
            WHERE object_ref='work:1'
            """
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == "Public Work"
