from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.source_db_tools import source_locus_seed, source_query_plan


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


def test_deprecated_source_locus_is_inspectable_and_reversible(tmp_path: Path) -> None:
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
                review_state="deprecated",
                is_deprecated=True,
                deprecation_reason="temporary outage",
                notes="deprecated for maintenance",
            ),
            updated_at=UPDATED_TIMESTAMP,
            overwrite_curation=True,
        )

        default_loci = source_query_plan.load_source_loci(conn, "test_topic")
        deprecated_loci = source_query_plan.load_source_loci(
            conn, "test_topic", include_deprecated=True
        )
        deprecated_plan = source_query_plan.plan_from_locus(
            deprecated_loci[0],
            generated_at=UPDATED_TIMESTAMP,
            generated_by="pytest",
        )

        source_locus_seed.upsert_source_locus(
            conn,
            locus_record(),
            updated_at=UPDATED_TIMESTAMP,
            overwrite_curation=True,
        )

        restored_loci = source_query_plan.load_source_loci(conn, "test_topic")
        restored_plan = source_query_plan.plan_from_locus(
            restored_loci[0],
            generated_at=UPDATED_TIMESTAMP,
            generated_by="pytest",
        )
    finally:
        conn.close()

    assert default_loci == []
    assert len(deprecated_loci) == 1
    assert deprecated_loci[0]["deprecation_reason"] == "temporary outage"
    assert deprecated_plan["plan_status"] == "deprecated"
    assert deprecated_plan["review_state"] == "deprecated"
    assert len(restored_loci) == 1
    assert restored_loci[0]["is_deprecated"] is False
    assert restored_loci[0]["deprecation_reason"] is None
    assert restored_plan["plan_status"] == "accepted"


def test_seed_database_streams_jsonl_records_without_materializing_list(
    tmp_path: Path, monkeypatch
) -> None:
    conn = sqlite3.connect(tmp_path / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    source_locus_seed.ensure_schema(conn)

    class StreamingRecords:
        def __init__(self, records: list[dict[str, object]]) -> None:
            self._records = records
            self._index = 0

        def __iter__(self) -> "StreamingRecords":
            return self

        def __next__(self) -> dict[str, object]:
            if self._index >= len(self._records):
                raise StopIteration
            value = self._records[self._index]
            self._index += 1
            return value

        def __len__(self) -> int:
            raise AssertionError("seed_database should stream JSONL records instead of materializing them")

    records = [
        {
            "locus_id": "locus:stream_topic:archive:one",
            "display_name": "Streamed Archive One",
            "locus_type": "archive",
            "query_family": "archives",
            "languages": ["en"],
            "access_class": "public_catalog_or_web",
            "rights_posture": "metadata_only",
            "refetchability_status": "not_checked",
        },
        {
            "locus_id": "locus:stream_topic:archive:two",
            "display_name": "Streamed Archive Two",
            "locus_type": "archive",
            "query_family": "archives",
            "languages": ["en"],
            "access_class": "public_catalog_or_web",
            "rights_posture": "metadata_only",
            "refetchability_status": "not_checked",
        },
    ]

    monkeypatch.setattr(
        source_locus_seed,
        "iter_seed_records",
        lambda _path: StreamingRecords(records),
    )
    monkeypatch.setattr(
        source_locus_seed,
        "export_source_loci",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full export should not run unless explicitly requested")
        ),
    )

    try:
        report = source_locus_seed.seed_database(
            conn,
            seed_path=tmp_path / "seed.jsonl",
            topic_id="stream_topic",
            discovered_at=FIXED_TIMESTAMP,
            discovered_by="pytest",
        )
    finally:
        conn.close()

    assert report["manual_seed_records"] == 2
    assert report["inserted_or_updated"] == 3
    assert report["changed_locus_ids"] == [
        "locus:unknown_locus:stream-topic",
        "locus:stream_topic:archive:one",
        "locus:stream_topic:archive:two",
    ]
    assert report["source_locus_export"] is None
