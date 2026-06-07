from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "tools" / "scripts" / "build_local_search_projection.py"
QUERY_TOOL = REPO_ROOT / "tools" / "scripts" / "query_local_search.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_local_search_results.py"

spec = importlib.util.spec_from_file_location("local_search_results_validator_for_tests", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)

query_spec = importlib.util.spec_from_file_location("query_local_search_for_tests", QUERY_TOOL)
assert query_spec is not None
query_tool = importlib.util.module_from_spec(query_spec)
assert query_spec.loader is not None
query_spec.loader.exec_module(query_tool)


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
              (2, 'book', 'Supplemental Work', 'reviewed', 'public_release_allowed', 'primary', NULL, 'alpha_subject');
            INSERT INTO source_claim (
              source_claim_id, claim_text, public_summary, claim_type, review_state,
              publication_state, authority_level, public_blocker, workspace_id
            ) VALUES
              (1, 'localclaimmarker internal review text', 'Public claim summary', 'factual', 'reviewed', 'public_preview', 'primary', NULL, 'alpha_subject'),
              (2, 'second internal claim text', 'Supplemental public summary', 'interpretive', 'reviewed', 'public_preview', 'primary', NULL, 'alpha_subject');
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


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_query(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(QUERY_TOOL), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def validate_results(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_local_search_results(path)


def build_local_index(tmp_path: Path) -> Path:
    db = create_search_db(tmp_path)
    index_db = tmp_path / "local_projection.sqlite"
    output_json = tmp_path / "local_projection.json"
    result = run_builder(
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
    assert result.returncode == 0, result.stdout + result.stderr
    return index_db


def create_ranked_search_db(
    tmp_path: Path,
    records: list[tuple[int, str, str, float]],
) -> Path:
    db = tmp_path / "ranked_search.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              publication_state TEXT,
              authority_level TEXT,
              confidence_score REAL,
              public_blocker TEXT,
              workspace_id TEXT
            )
            """
        )
        insert_sql = """
            INSERT INTO work (
              work_id, work_type, title, review_state, publication_state,
              authority_level, confidence_score, public_blocker, workspace_id
            ) VALUES (?, 'book', ?, 'reviewed', 'public_release_allowed', ?, ?, NULL, 'alpha_subject')
            """
        conn.executemany(insert_sql, records)
        conn.commit()
    finally:
        conn.close()
    return db


def build_ranked_index(
    tmp_path: Path,
    records: list[tuple[int, str, str, float]],
) -> Path:
    db = create_ranked_search_db(tmp_path, records)
    index_db = tmp_path / "ranked_projection.sqlite"
    output_json = tmp_path / "ranked_projection.json"
    result = run_builder(
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
    assert result.returncode == 0, result.stdout + result.stderr
    return index_db


def test_query_cli_normalizes_plain_text_and_validates_json(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)
    results_json = tmp_path / "results.json"

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        ' Public!!! +claim? "summary" ',
        "--output-json",
        str(results_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["query"]["normalized_query"] == 'Public!!! +claim? "summary"'
    assert payload["query"]["terms"] == ["public", "claim", "summary"]
    assert payload["counts"]["returned"] == 2
    assert payload["results"][0]["object_id"] == "claim:1"
    assert payload["results"][0]["result_class"] == "claim"

    report, exit_code = validate_results(results_json)
    assert exit_code == validator.EXIT_PASS, report


def test_query_cli_paginates_and_scopes_results(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "work",
        "--scope",
        "source_work",
        "--limit",
        "1",
        "--offset",
        "1",
        "--format",
        "text",
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scope=source_work" in result.stdout
    assert "returned=1" in result.stdout
    assert "truncated=false" in result.stdout
    assert "result[2].object_id=work:2" in result.stdout


def test_load_matching_rows_pushes_pagination_into_sqlite(tmp_path: Path) -> None:
    index_db = build_ranked_index(
        tmp_path,
        [
            (1, "alpha ranking sample", "secondary", 0.55),
            (2, "alpha ranking sample", "primary", 0.55),
            (3, "alpha ranking sample", "trusted", 0.10),
        ],
    )

    conn = query_tool.connect_read_only(index_db)
    traces: list[str] = []
    try:
        conn.set_trace_callback(traces.append)
        rows, total = query_tool.load_matching_rows(
            conn,
            fts_query='"alpha"',
            scope="all",
            limit=1,
            offset=1,
        )
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert total == 3
    assert [int(row["object_pk"]) for row in rows] == [1]
    assert any("ORDER BY" in statement and "LIMIT 1 OFFSET 1" in statement for statement in traces)


def test_query_cli_suppresses_private_path_snippets(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)
    results_json = tmp_path / "private_path_results.json"

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "cacheprivatemarker",
        "--output-json",
        str(results_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["counts"]["returned"] == 1
    snippet = payload["results"][0]["snippets"][0]
    assert snippet["display_policy"] == "suppressed"
    assert snippet["text"] == "[suppressed private path]"
    assert payload["policy"]["private_paths_exposed"] is False


def test_build_result_caches_indexed_fields_json_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    indexed_fields_json = json.dumps(
        [
            {"field": "indexed_text", "text": "public snippet"},
            {"field": "title", "text": "secondary snippet"},
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    row = {
        "projection_id": 1,
        "object_type": "work",
        "object_ref": "work:1",
        "title": "Public Work",
        "subtitle": "Supplemental Work",
        "indexed_fields_json": indexed_fields_json,
        "match_snippet": "public snippet",
        "suppressed_fields_json": "[]",
        "review_state": "reviewed",
        "publication_state": "public_release_allowed",
        "profile": "local",
        "score": 1.0,
    }
    load_calls = 0
    real_json_loads = query_tool.json.loads

    def counting_json_loads(raw: object, *args: object, **kwargs: object) -> object:
        nonlocal load_calls
        if raw == indexed_fields_json:
            load_calls += 1
        return real_json_loads(raw, *args, **kwargs)

    monkeypatch.setattr(query_tool.json, "loads", counting_json_loads)
    query_tool.parse_indexed_fields_json.cache_clear()

    first = query_tool.build_result(row, terms=["public"], rank=1)
    second = query_tool.build_result(row, terms=["public"], rank=2)

    assert load_calls == 1
    assert first["snippets"][0]["text"] == "public snippet"
    assert second["snippets"][0]["text"] == "public snippet"


def test_query_cli_treats_sql_injection_like_input_as_plain_text(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "work; DROP TABLE search_projection; --",
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["query"]["normalized_query"] == "work; DROP TABLE search_projection; --"
    assert payload["query"]["terms"] == ["work", "drop", "table", "search", "projection"]
    assert payload["counts"]["returned"] == 0
    assert payload["counts"]["total_estimate"] == 0


def test_results_validator_rejects_secret_and_private_note_findings(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)
    results_json = tmp_path / "results.json"

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "Supplemental",
        "--output-json",
        str(results_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(results_json.read_text(encoding="utf-8"))
    payload["query"]["visibility_profile"] = "public_preview"
    for row in payload["results"]:
        row["visibility"]["profile"] = "public_preview"
    payload["results"][0]["snippets"][0]["field"] = "private_note"
    payload["results"][0]["snippets"][0]["text"] = "authorization: Bearer leaked-token"
    results_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report, exit_code = validate_results(results_json)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert [error["code"] for error in report["errors"]] == [
        "PRIVATE_NOTE_FIELD_EXPOSED",
        "SECRET_MARKER_EXPOSED",
    ]
    assert report["errors"][0]["path"] == "results[0].snippets[0].field"
    assert report["errors"][1]["path"] == "results[0].snippets[0].text"


def test_results_validator_rejects_private_path_and_public_visibility_contradiction(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)
    results_json = tmp_path / "results.json"

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "Supplemental",
        "--output-json",
        str(results_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(results_json.read_text(encoding="utf-8"))
    payload["query"]["visibility_profile"] = "public_preview"
    for row in payload["results"]:
        row["visibility"]["profile"] = "public_preview"
    payload["results"][0]["publication_state"] = "local_only"
    payload["results"][0]["snippets"][0]["text"] = "/Users/joe/private.txt"
    results_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report, exit_code = validate_results(results_json)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert [error["code"] for error in report["errors"]] == [
        "PRIVATE_PATH_EXPOSED",
        "PUBLIC_VISIBILITY_CONTRADICTION",
    ]
    assert report["errors"][0]["path"] == "results[0].snippets[0].text"
    assert report["errors"][1]["path"] == "results[0].publication_state"


def test_query_ranking_prefers_authority_level_for_equal_confidence_matches(tmp_path: Path) -> None:
    index_db = build_ranked_index(
        tmp_path,
        [
            (1, "alpha ranking sample", "secondary", 0.55),
            (2, "alpha ranking sample", "primary", 0.55),
        ],
    )

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "alpha",
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["counts"]["returned"] == 2
    assert payload["results"][0]["object_id"] == "work:2"


def test_query_ranking_prefers_higher_confidence_for_equal_authority(tmp_path: Path) -> None:
    index_db = build_ranked_index(
        tmp_path,
        [
            (3, "beta confidence sample", "secondary", 0.20),
            (4, "beta confidence sample", "secondary", 0.95),
        ],
    )

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "beta",
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["counts"]["returned"] == 2
    assert payload["results"][0]["object_id"] == "work:4"


def test_results_validator_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    index_db = build_local_index(tmp_path)
    results_json = tmp_path / "unknown-fields-results.json"

    result = run_query(
        "--index-db",
        str(index_db),
        "--query",
        "Supplemental",
        "--output-json",
        str(results_json),
        "--generated-at",
        "2026-06-02T00:00:00Z",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    payload = json.loads(results_json.read_text(encoding="utf-8"))
    payload["source"]["unknown_source"] = "bad"
    payload["query"]["unexpected_query_field"] = "bad"
    payload["counts"]["unexpected_count"] = 7
    payload["policy"]["unexpected_policy_field"] = "bad"
    payload["results"][0]["unexpected_row_field"] = "bad"
    payload["results"][0]["snippets"][0]["unknown_snippet_field"] = "bad"
    payload["results"][0]["visibility"]["unknown_visibility_field"] = "bad"
    results_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report, exit_code = validate_results(results_json)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert any(item.get("code") == "UNKNOWN_FIELD" for item in report["errors"])
