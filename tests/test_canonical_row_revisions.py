"""Immutable revision history for stable-ID canonical projections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.source_db_tools import canonical_store

STAMP = "2026-10-08T12:00:00Z"


def _connect(tmp_path: Path, *, target_version: int | None = None) -> sqlite3.Connection:
    conn = canonical_store.connect_canonical_store(tmp_path / "canonical.sqlite")
    canonical_store.apply_migrations(
        conn,
        target_version=target_version,
        applied_at=STAMP,
        applied_by="pytest",
    )
    return conn


def _revisions(conn: sqlite3.Connection, table: str, row_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT revision_id, change_kind, supersedes_revision_id, snapshot_json
        FROM canonical_row_revision
        WHERE table_name=? AND row_id=?
        ORDER BY revision_id
        """,
        (table, row_id),
    ).fetchall()


def test_migration_backfills_existing_rows_once(tmp_path: Path) -> None:
    conn = _connect(tmp_path, target_version=9)
    try:
        conn.execute(
            """
            INSERT INTO work (work_key_v1, title, record_last_updated)
            VALUES ('work:legacy', 'Original title', ?)
            """,
            (STAMP,),
        )
        conn.commit()

        result = canonical_store.apply_migrations(conn, applied_at=STAMP, applied_by="pytest")
        assert result.applied_migration_ids == (
            "0010_canonical_row_revisions",
            "0011_detected_entity_span_bounds",
            "0012_cycle_event_attempts",
            "0013_cycle_ledger_status_constraints",
            "0014_ingested_gather_candidate_selection",
            "0015_authority_reconciliation_evidence_history",
            "0016_cycle_error_counts",
            "0017_authority_merge_self_guard",
            "0018_authority_merge_cycle_guard",
        )
        history = _revisions(conn, "work", 1)
        assert len(history) == 1
        assert history[0]["change_kind"] == "baseline"
        assert history[0]["supersedes_revision_id"] is None
        assert json.loads(history[0]["snapshot_json"])["title"] == "Original title"
        assert canonical_store.apply_migrations(conn).noop is True
        assert len(_revisions(conn, "work", 1)) == 1
    finally:
        conn.close()


def test_every_tracked_family_has_full_linked_revisions_for_direct_sql(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        conn.execute(
            """
            INSERT INTO provenance_event (
              provenance_event_key_v1, object_namespace, object_id, event_type,
              event_timestamp, record_last_updated
            ) VALUES ('prov:one', 'test', 'one', 'test', ?, ?)
            """,
            (STAMP, STAMP),
        )
        conn.execute(
            "INSERT INTO work (work_key_v1, title, record_last_updated) VALUES ('work:one', 'Before', ?)",
            (STAMP,),
        )
        conn.execute(
            "INSERT INTO source_access (original_locator, citation_hint, record_last_updated) VALUES ('https://example.test', 'Before', ?)",
            (STAMP,),
        )
        conn.execute(
            """
            INSERT INTO source_claim (
              source_claim_key_v1, about_object_ref, claim_text, created_at, record_last_updated
            ) VALUES ('claim:one', 'work:1', 'Before', ?, ?)
            """,
            (STAMP, STAMP),
        )
        conn.execute(
            """
            INSERT INTO capture_event (
              original_locator, captured_at, capture_method, byte_count, record_last_updated
            ) VALUES ('https://example.test', ?, 'test', 10, ?)
            """,
            (STAMP, STAMP),
        )
        conn.execute(
            """
            INSERT INTO extraction_record (
              capture_event_id, extraction_method, extraction_status,
              output_hash, created_at, record_last_updated
            ) VALUES (1, 'test', 'success', 'before', ?, ?)
            """,
            (STAMP, STAMP),
        )
        conn.execute(
            "INSERT INTO extraction_detected_entity (entity_label, normalized_label, record_last_updated) VALUES ('Entity', 'before', ?)",
            (STAMP,),
        )
        conn.execute(
            """
            INSERT INTO source_relationship (
              from_object_ref, predicate, evidence_note, created_at, record_last_updated
            ) VALUES ('work:1', 'related_to', 'Before', ?, ?)
            """,
            (STAMP, STAMP),
        )

        changes = {
            "work": ("title", "After"),
            "source_access": ("citation_hint", "After"),
            "source_claim": ("claim_text", "After"),
            "capture_event": ("byte_count", 20),
            "extraction_record": ("output_hash", "after"),
            "extraction_detected_entity": ("normalized_label", "after"),
            "source_relationship": ("evidence_note", "After"),
        }
        primary_keys = {
            "work": "work_id",
            "source_access": "source_access_id",
            "source_claim": "source_claim_id",
            "capture_event": "capture_event_id",
            "extraction_record": "extraction_id",
            "extraction_detected_entity": "detected_entity_id",
            "source_relationship": "source_relationship_id",
        }
        for table, (column, value) in changes.items():
            conn.execute(f"UPDATE {table} SET {column}=? WHERE {primary_keys[table]}=1", (value,))
            revisions = _revisions(conn, table, 1)
            assert len(revisions) == 2, table
            assert [row["change_kind"] for row in revisions] == ["insert", "supersede"]
            assert revisions[1]["supersedes_revision_id"] == revisions[0]["revision_id"]
            first_snapshot = json.loads(revisions[0]["snapshot_json"])
            latest_snapshot = json.loads(revisions[1]["snapshot_json"])
            assert first_snapshot[column] != value
            assert latest_snapshot[column] == value
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert set(latest_snapshot) == columns
            conn.execute(f"UPDATE {table} SET {column}=? WHERE {primary_keys[table]}=1", (value,))
            assert len(_revisions(conn, table, 1)) == 2, table

        provenance = _revisions(conn, "provenance_event", 1)
        assert len(provenance) == 1
        assert set(json.loads(provenance[0]["snapshot_json"])) == {
            row["name"] for row in conn.execute("PRAGMA table_info(provenance_event)")
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_revision_and_provenance_rows_are_immutable(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO work (work_key_v1, record_last_updated) VALUES ('work:one', ?)",
            (STAMP,),
        )
        conn.execute(
            """
            INSERT INTO provenance_event (
              provenance_event_key_v1, object_namespace, object_id, event_type,
              event_timestamp, record_last_updated
            ) VALUES ('prov:one', 'test', 'one', 'test', ?, ?)
            """,
            (STAMP, STAMP),
        )
        revision_id = _revisions(conn, "work", 1)[0]["revision_id"]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE canonical_row_revision SET snapshot_json='{}' WHERE revision_id=?",
                (revision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM canonical_row_revision WHERE revision_id=?", (revision_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE provenance_event SET note_text='changed' WHERE provenance_event_id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM work WHERE work_id=1")
    finally:
        conn.close()


def test_revisions_roll_back_with_projection_changes(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO work (work_key_v1, title, record_last_updated) VALUES ('work:one', 'Before', ?)",
            (STAMP,),
        )
        conn.commit()
        conn.execute("SAVEPOINT attempt")
        conn.execute("UPDATE work SET title='After' WHERE work_id=1")
        assert len(_revisions(conn, "work", 1)) == 2
        conn.execute("ROLLBACK TO attempt")
        conn.execute("RELEASE attempt")
        assert len(_revisions(conn, "work", 1)) == 1
        assert conn.execute("SELECT title FROM work WHERE work_id=1").fetchone()[0] == "Before"
    finally:
        conn.close()


def test_store_check_rejects_missing_revision_trigger(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    conn = _connect(tmp_path)
    try:
        conn.execute("DROP TRIGGER canonical_row_revision_work_update")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(
        canonical_store.CanonicalStoreError, match="missing required revision triggers"
    ):
        canonical_store.check_canonical_store(db_path)


def test_revision_chain_rejects_second_root(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO work (work_key_v1, record_last_updated) VALUES ('work:one', ?)",
            (STAMP,),
        )
        snapshot = _revisions(conn, "work", 1)[0]["snapshot_json"]
        with pytest.raises(sqlite3.IntegrityError, match="linear supersession chain"):
            conn.execute(
                """
                INSERT INTO canonical_row_revision (table_name, row_id, change_kind, snapshot_json)
                VALUES ('work', 1, 'insert', ?)
                """,
                (snapshot,),
            )
    finally:
        conn.close()
