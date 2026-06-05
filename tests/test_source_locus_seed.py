from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.source_db_tools import source_locus_seed


FIXED_TIMESTAMP = "2026-06-05T10:00:00Z"
UPDATED_TIMESTAMP = "2026-06-05T10:30:00Z"


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
        "discovered_at": FIXED_TIMESTAMP,
        "discovered_by": "pytest",
        "confidence_score": 0.8,
        "review_state": "accepted",
        "productivity_queries_run": 2,
        "productivity_leads_returned": 4,
        "productivity_unique_leads": 2,
        "productivity_captures_made": 1,
        "productivity_works_promoted": 0,
        "productivity_score": 0.5,
        "last_queried_at": None,
        "last_productive_at": None,
        "cooldown_until": None,
        "is_deprecated": False,
        "deprecation_reason": None,
        "notes": "seeded",
    }
    record.update(overrides)
    return record


def test_upsert_source_locus_replay_preserves_curation_and_productivity_by_default(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    source_locus_seed.ensure_schema(conn)
    try:
        source_locus_seed.upsert_source_locus(
            conn,
            locus_record(),
            updated_at=FIXED_TIMESTAMP,
        )
        source_locus_seed.upsert_source_locus(
            conn,
            locus_record(
                display_name="New Archive Name",
                review_state="rejected",
                productivity_queries_run=99,
                productivity_leads_returned=99,
                notes="mutated in seed replay",
                is_deprecated=True,
                deprecation_reason="temporary",
            ),
            updated_at=UPDATED_TIMESTAMP,
        )
        row = conn.execute(
            "SELECT display_name, review_state, productivity_queries_run, productivity_leads_returned, notes, is_deprecated, deprecation_reason, record_last_updated"
            " FROM source_locus WHERE locus_id=?",
            ("locus:test_topic:archive:example",),
        ).fetchone()
        if row is None:
            raise AssertionError("expected source_locus row")
        assert row["display_name"] == "Example Archive"
        assert row["review_state"] == "accepted"
        assert row["productivity_queries_run"] == 2
        assert row["productivity_leads_returned"] == 4
        assert row["notes"] == "seeded"
        assert bool(row["is_deprecated"]) is False
        assert row["deprecation_reason"] is None
        assert row["record_last_updated"] == UPDATED_TIMESTAMP
    finally:
        conn.close()


def test_upsert_source_locus_replay_overwrites_curation_with_flag(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    source_locus_seed.ensure_schema(conn)
    try:
        source_locus_seed.upsert_source_locus(
            conn,
            locus_record(),
            updated_at=FIXED_TIMESTAMP,
        )
        source_locus_seed.upsert_source_locus(
            conn,
            locus_record(
                display_name="New Archive Name",
                review_state="rejected",
                is_deprecated=True,
                deprecation_reason="temporary",
                notes="mutated in seed replay",
            ),
            updated_at=UPDATED_TIMESTAMP,
            overwrite_curation=True,
        )
        row = conn.execute(
            "SELECT display_name, review_state, is_deprecated, deprecation_reason, notes, record_last_updated"
            " FROM source_locus WHERE locus_id=?",
            ("locus:test_topic:archive:example",),
        ).fetchone()
        if row is None:
            raise AssertionError("expected source_locus row")
        assert row["display_name"] == "New Archive Name"
        assert row["review_state"] == "rejected"
        assert bool(row["is_deprecated"]) is True
        assert row["deprecation_reason"] == "temporary"
        assert row["notes"] == "mutated in seed replay"
        assert row["record_last_updated"] == UPDATED_TIMESTAMP
    finally:
        conn.close()
