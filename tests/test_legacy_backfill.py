from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.source_db_tools import canonical_store, legacy_backfill


FIXED_TIMESTAMP_1 = "2026-06-01T10:00:00Z"
FIXED_TIMESTAMP_2 = "2026-06-03T10:00:00Z"


def init_db(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP_1,
        applied_by="pytest",
    )
    conn = canonical_store.connect_canonical_store(db_path)
    provenance = canonical_store.record_provenance_event(
        conn,
        object_namespace="test",
        object_id="legacy_backfill_metadata",
        event_type="legacy_backfill",
        run_id="legacy-backfill-test",
        event_timestamp=FIXED_TIMESTAMP_1,
        tool_name="legacy_backfill",
    ).event_key
    return conn, provenance


def test_legacy_backfill_metadata_seen_timestamps_update(tmp_path: Path) -> None:
    conn, provenance = init_db(tmp_path)
    try:
        work_id, _created = legacy_backfill.insert_or_get_work(
            conn,
            provenance_event_ref=provenance,
            work_key="fixture-work-1",
            work_type="local:legacy_record",
            title="Fixture Work",
            raw_cite_text="fixture cite",
            review_state="needs_review",
            confidence_score=0.5,
            timestamp=FIXED_TIMESTAMP_1,
        )
        legacy_backfill.insert_metadata(
            conn,
            work_id=work_id,
            values={"doi": "10.1000/1"},
            timestamp=FIXED_TIMESTAMP_1,
        )
        legacy_backfill.insert_metadata(
            conn,
            work_id=work_id,
            values={"doi": "10.1000/1"},
            timestamp=FIXED_TIMESTAMP_2,
        )
        row = conn.execute(
            "SELECT first_seen_at, last_seen_at, record_last_updated FROM work_metadata WHERE work_id=? AND meta_key='doi'",
            (work_id,),
        ).fetchone()
        assert row is not None
        assert row["first_seen_at"] == FIXED_TIMESTAMP_1
        assert row["last_seen_at"] == FIXED_TIMESTAMP_2
        assert row["record_last_updated"] == FIXED_TIMESTAMP_2
    finally:
        conn.close()


def test_legacy_backfill_work_url_record_last_updated_updates_on_repeat(tmp_path: Path) -> None:
    conn, provenance = init_db(tmp_path)
    try:
        work_id, _created = legacy_backfill.insert_or_get_work(
            conn,
            provenance_event_ref=provenance,
            work_key="fixture-work-2",
            work_type="local:legacy_record",
            title="Fixture Work",
            raw_cite_text="fixture cite",
            review_state="needs_review",
            confidence_score=0.5,
            timestamp=FIXED_TIMESTAMP_1,
        )
        legacy_backfill.insert_source_access(
            conn,
            provenance_event_ref=provenance,
            work_id=work_id,
            locator="https://example.org/fixture",
            url="https://example.org/resource",
            timestamp=FIXED_TIMESTAMP_1,
        )
        legacy_backfill.insert_source_access(
            conn,
            provenance_event_ref=provenance,
            work_id=work_id,
            locator="https://example.org/fixture",
            url="https://example.org/resource",
            timestamp=FIXED_TIMESTAMP_2,
        )
        row = conn.execute(
            "SELECT record_last_updated FROM work_url WHERE work_id=? AND url='https://example.org/resource'",
            (work_id,),
        ).fetchone()
        assert row is not None
        assert row["record_last_updated"] == FIXED_TIMESTAMP_2
        assert conn.execute(
            "SELECT COUNT(*) FROM work_url WHERE work_id=? AND url='https://example.org/resource'",
            (work_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
