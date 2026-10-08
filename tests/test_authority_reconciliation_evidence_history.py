"""Append-only reconciliation evidence survives direct and writer-driven updates."""

from __future__ import annotations

import sqlite3

import pytest

from tools.source_db_tools import canonical_store

STAMP = "2026-10-08T12:00:00Z"


def _history(conn: sqlite3.Connection, row_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM authority_reconciliation_evidence_history
        WHERE authority_reconciliation_id=? ORDER BY history_id""",
        (row_id,),
    ).fetchall()


def test_migration_backfills_and_versions_reconciliation_evidence(tmp_path) -> None:
    conn = canonical_store.connect_canonical_store(tmp_path / "canonical.sqlite")
    try:
        canonical_store.apply_migrations(conn, target_version=13, applied_by="pytest")
        old = conn.execute(
            """INSERT INTO authority_reconciliation (
                reconciliation_key_v1, target_namespace, target_id, raw_label,
                method, match_method, match_score, evidence_context,
                confidence_score, review_state, created_at, updated_at,
                record_last_updated
            ) VALUES ('authrec:old', 'test', '1', 'Jane', 'import', 'label',
                      0.5, 'original evidence', 0.5, 'proposed', ?, ?, ?)""",
            (STAMP, STAMP, STAMP),
        ).lastrowid
        conn.commit()

        canonical_store.apply_migrations(conn, applied_by="pytest")
        assert canonical_store.check_canonical_store(tmp_path / "canonical.sqlite").schema_version == 15
        rows = _history(conn, old)
        assert [(row["change_kind"], row["evidence_context"]) for row in rows] == [
            ("baseline", "original evidence")
        ]

        conn.execute(
            """UPDATE authority_reconciliation
            SET match_score=0.8, evidence_context='new evidence', updated_at=?
            WHERE authority_reconciliation_id=?""",
            (STAMP, old),
        )
        rows = _history(conn, old)
        assert [(row["change_kind"], row["match_score"], row["evidence_context"]) for row in rows] == [
            ("baseline", 0.5, "original evidence"),
            ("update", 0.8, "new evidence"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE authority_reconciliation_evidence_history SET evidence_context='lost' WHERE history_id=?",
                (rows[0]["history_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM authority_reconciliation_evidence_history WHERE history_id=?",
                (rows[0]["history_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute(
                "DELETE FROM authority_reconciliation WHERE authority_reconciliation_id=?",
                (old,),
            )
        assert canonical_store.apply_migrations(conn).noop is True
        assert len(_history(conn, old)) == 2

        new = conn.execute(
            """INSERT INTO authority_reconciliation (
                reconciliation_key_v1, target_namespace, target_id, raw_label,
                evidence_context, created_at, updated_at, record_last_updated
            ) VALUES ('authrec:new', 'test', '2', 'John', 'fresh evidence', ?, ?, ?)""",
            (STAMP, STAMP, STAMP),
        ).lastrowid
        assert [(row["change_kind"], row["evidence_context"]) for row in _history(conn, new)] == [
            ("insert", "fresh evidence")
        ]
    finally:
        conn.close()
