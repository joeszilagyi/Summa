from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest

from tools.source_db_tools import source_locus_seed, source_query_plan

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


def temp_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    source_query_plan.ensure_schema(conn)
    return conn


def test_source_query_plan_module_imports_and_creates_schema(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('source_locus', 'source_query_plan')"
        )
    }

    assert tables == {"source_locus", "source_query_plan"}


def test_query_plan_ids_are_deterministic() -> None:
    kwargs = {
        "topic_id": "test_topic",
        "locus_id": "locus:test_topic:archive:example",
        "query_family": "archives",
        "query_mode": "archive_search",
        "query_target": "Example Archive",
    }

    first = source_query_plan.deterministic_query_plan_id(**kwargs)
    second = source_query_plan.deterministic_query_plan_id(**kwargs)

    assert first == second
    assert first.startswith("qplan:test-topic:")


def test_query_normalization_is_deterministic() -> None:
    assert (
        source_query_plan.normalize_query_text("  Trout\tFly  Fishing\nMontana  ")
        == "trout fly fishing montana"
    )


def test_invalid_plan_inputs_fail_clearly() -> None:
    plan = source_query_plan.plan_from_locus(
        locus_record(), generated_at=GENERATED_AT, generated_by="pytest"
    )
    plan["simulation_only"] = False

    with pytest.raises(RuntimeError, match="simulation_only must be true"):
        source_query_plan.validate_plan(plan)


def test_plan_records_are_planning_only_not_acquired(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    source_locus_seed.upsert_source_locus(conn, locus_record(), updated_at=GENERATED_AT)

    report = source_query_plan.create_plans_from_loci(
        conn,
        topic_id="test_topic",
        generated_at=GENERATED_AT,
        generated_by="pytest",
    )

    plan = report["query_plans"][0]
    assert report["planning_only"] is True
    assert report["network_access_attempted"] is False
    assert plan["simulation_only"] is True
    assert plan["network_access_attempted"] is False
    assert "No query was executed" in str(plan["notes"])


def test_source_query_plan_does_not_attempt_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_socket(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    conn = temp_conn(tmp_path)
    source_locus_seed.upsert_source_locus(conn, locus_record(), updated_at=GENERATED_AT)

    report = source_query_plan.create_plans_from_loci(
        conn,
        topic_id="test_topic",
        generated_at=GENERATED_AT,
        generated_by="pytest",
    )

    assert report["counts"]["total_plans"] == 1
