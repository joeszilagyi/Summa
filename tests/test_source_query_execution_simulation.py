from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.source_db_tools import source_locus_seed, source_query_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "source_db_tools" / "source_query_execution_simulation.py"
GENERATED_AT = "2026-04-28T00:00:00+00:00"


def locus_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "locus_id": "locus:test_topic:archive:example",
        "topic_id": "test_topic",
        "display_name": "Example Archive",
        "locus_type": "archive",
        "query_family": "archives",
        "parent_locus_id": None,
        "parent_org_id": None,
        "jurisdiction_place_id": None,
        "languages": ["en"],
        "time_coverage_start": None,
        "time_coverage_end": None,
        "access_class": "public_catalog_or_web",
        "access_url": "https://example.test/archive",
        "catalog_url": None,
        "archive_url": None,
        "access_notes": "Fixture only.",
        "rights_posture": "metadata_only",
        "refetchability_status": "not_checked",
        "discovery_method": "manual_seed",
        "discovery_source": "unit_test",
        "discovered_at": GENERATED_AT,
        "discovered_by": "pytest",
        "confidence_score": 0.8,
        "review_state": "accepted",
        "productivity_queries_run": 0,
        "productivity_leads_returned": 0,
        "productivity_unique_leads": 0,
        "productivity_captures_made": 0,
        "productivity_works_promoted": 0,
        "productivity_score": 0.0,
        "last_queried_at": None,
        "last_productive_at": None,
        "cooldown_until": None,
        "is_deprecated": False,
        "deprecation_reason": None,
        "notes": None,
    }
    record.update(overrides)
    return record


def prepared_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "source.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    source_query_plan.ensure_schema(conn)
    source_locus_seed.upsert_source_locus(conn, locus_record(), updated_at=GENERATED_AT)
    source_query_plan.create_plans_from_loci(
        conn,
        topic_id="test_topic",
        generated_at=GENERATED_AT,
        generated_by="pytest",
    )
    conn.close()
    return db_path


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def table_count(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_source_query_execution_simulation_help_exits_zero() -> None:
    result = run_script("--help")

    assert result.returncode == 0
    assert "Simulate source-query-plan execution without network access" in result.stdout


def test_simulation_runs_against_temp_db_and_creates_simulated_rows(tmp_path: Path) -> None:
    db_path = prepared_db(tmp_path)
    report_path = tmp_path / "simulation-report.json"

    result = run_script(
        "run",
        "--db",
        str(db_path),
        "--topic-id",
        "test_topic",
        "--started-at",
        GENERATED_AT,
        "--completed-at",
        GENERATED_AT,
        "--simulated-by",
        "pytest",
        "--report-json",
        str(report_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["network_access"] is False
    assert payload["external_calls_attempted"] is False
    assert payload["source_access_rows_created"] == 0
    assert payload["captures_created"] == 0
    assert payload["per_topic_simulation_report"]["total_simulations"] == 1
    assert payload["simulated_lead_candidates"]
    assert all(candidate["is_simulated"] for candidate in payload["simulated_lead_candidates"])
    assert all(
        candidate["acquisition_status"] == "not_acquired"
        for candidate in payload["simulated_lead_candidates"]
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert table_count(conn, "source_query_execution_simulation") == 1
    assert table_count(conn, "simulated_source_lead_candidate") == 2
    assert table_count(conn, "capture_event") == 0
    assert table_count(conn, "extraction_record") == 0
    assert table_count(conn, "source_access") == 0
    conn.close()


def test_simulation_rerun_is_idempotent(tmp_path: Path) -> None:
    db_path = prepared_db(tmp_path)
    args = [
        "run",
        "--db",
        str(db_path),
        "--topic-id",
        "test_topic",
        "--started-at",
        GENERATED_AT,
        "--completed-at",
        GENERATED_AT,
        "--simulated-by",
        "pytest",
    ]

    first = run_script(*args)
    second = run_script(*args)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert table_count(conn, "source_query_execution_simulation") == 1
    assert table_count(conn, "simulated_source_lead_candidate") == 2
    conn.close()


def test_simulation_dry_run_does_not_write(tmp_path: Path) -> None:
    db_path = prepared_db(tmp_path)

    result = run_script(
        "run",
        "--db",
        str(db_path),
        "--topic-id",
        "test_topic",
        "--started-at",
        GENERATED_AT,
        "--completed-at",
        GENERATED_AT,
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert table_count(conn, "source_query_execution_simulation") == 0
    assert table_count(conn, "simulated_source_lead_candidate") == 0
    conn.close()


def test_simulation_does_not_attempt_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_socket(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    db_path = prepared_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Exercise the importable path under the socket guard. Subprocess tests above
    # already cover the live CLI import and execution path.
    from tools.source_db_tools import source_query_execution_simulation

    payload = source_query_execution_simulation.run_simulations(
        conn,
        topic_id="test_topic",
        started_at=GENERATED_AT,
        completed_at=GENERATED_AT,
        simulated_by="pytest",
    )

    assert payload["network_access"] is False
    assert payload["external_calls_attempted"] is False
    conn.close()
