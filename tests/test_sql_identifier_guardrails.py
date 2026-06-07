from __future__ import annotations

import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from tools.scripts import build_local_search_projection
from tools.source_db_tools import (
    canonical_graph_closure,
    export_bibliography,
    review_queue,
    source_locus_seed,
    source_query_plan,
)


def test_source_query_plan_rejects_unsafe_sql_identifiers() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="invalid SQL identifier"):
            source_query_plan.add_column_if_missing(conn, "work;drop", "new_col", "new_col TEXT")
    finally:
        conn.close()


def test_source_locus_seed_rejects_unsafe_column_definition() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="invalid column definition"):
            source_locus_seed.add_column_if_missing(
                conn,
                "source_access",
                "source_lead_id",
                "source_lead_id TEXT; DROP TABLE work;--",
            )
    finally:
        conn.close()


def test_canonical_graph_closure_rejects_unsafe_sql_identifiers() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(canonical_graph_closure.GraphClosureError, match="invalid SQL identifier"):
            canonical_graph_closure.audit_simple_fk_table(
                conn,
                table="source_access;drop",
                pk_column="source_access_id",
                fk_column="source_locus_id",
                target_namespace="source_locus",
            )
    finally:
        conn.close()


def test_export_bibliography_rejects_unsafe_sql_identifiers() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="invalid SQL identifier"):
            export_bibliography._rows_for_work(conn, "source_access;drop", 1)
    finally:
        conn.close()


def test_review_queue_rejects_unsafe_target_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe_target = review_queue.ReviewTarget(
        namespace="work",
        table="work;drop",
        pk_column="work_id",
        state_column="review_state",
    )
    monkeypatch.setitem(review_queue.TARGETS, "work", unsafe_target)
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="invalid review target table"):
            review_queue.fetch_review_object(conn, "work:1")
    finally:
        conn.close()


def test_local_search_projection_rejects_unsafe_projection_target(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe_target = build_local_search_projection.SearchTarget(
        object_type="work",
        table="work;drop",
        pk_column="work_id",
        field_specs=(),
    )
    monkeypatch.setattr(build_local_search_projection, "TARGETS", (unsafe_target,))
    monkeypatch.setattr(
        build_local_search_projection,
        "load_correction_resolution",
        lambda _path: (None, set(), False),
    )
    monkeypatch.setattr(
        build_local_search_projection,
        "connect_read_only",
        lambda _path: sqlite3.connect(":memory:"),
    )
    monkeypatch.setattr(
        build_local_search_projection,
        "read_schema_version",
        lambda _conn: "schema.v1",
    )
    monkeypatch.setattr(
        build_local_search_projection,
        "resolve_existing_file",
        lambda _raw_path: Path("/tmp/fake-projection.sqlite"),
    )
    args = Namespace(db=Path("/tmp/fake-projection.sqlite"), profile="public", correction_ledger=Path("ledger.json"))

    with pytest.raises(RuntimeError, match="invalid projection target table"):
        build_local_search_projection.build_projection_payload(args)


def test_review_queue_fetch_object_uses_lightweight_projection_by_default() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    executed_sql: list[str] = []
    conn.set_trace_callback(executed_sql.append)
    try:
        conn.executescript(
            """
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              confidence_score REAL,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            INSERT INTO work (
              work_id, work_type, title, review_state, confidence_score,
              workspace_id, authority_level, public_blocker, record_last_updated
            ) VALUES (
              1, 'book', 'Projected Work', 'needs_review', 0.5,
              'alpha_subject', 'primary', '', '2026-06-02T00:00:00Z'
            );
            """
        )

        row = review_queue.fetch_review_object(conn, "work:1")
        projected_sql = list(executed_sql)
        executed_sql.clear()
        raw_row = review_queue.fetch_review_object(conn, "work:1", full_row=True)
    finally:
        conn.close()

    assert row["object_ref"] == "work:1"
    assert row["label"] == "Projected Work"
    assert row["review_state"] == "needs_review"
    assert raw_row["work_type"] == "book"
    assert any("AS object_type" in sql and "AS label" in sql for sql in projected_sql)
    assert not any("SELECT * FROM work WHERE work_id=1" in sql for sql in projected_sql)
    assert any("SELECT * FROM work WHERE work_id=1" in sql for sql in executed_sql)
