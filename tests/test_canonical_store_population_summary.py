from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tools.source_db_tools import canonical_ingest, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_BATCH = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path, *, name: str = "canonical.sqlite") -> Path:
    db_path = tmp_path / name
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.population_summary",
    )
    return db_path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_population_summary_reports_absent_store(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"

    summary = canonical_store.summarize_canonical_store_population(missing)

    assert summary["status"] == "absent"
    assert summary["exists"] is False
    assert summary["initialized"] is False
    assert summary["valid"] is False
    assert summary["total_rows"] == 0
    assert summary["last_ingest_at"] is None


def test_population_summary_reports_uninitialized_sqlite_file(tmp_path: Path) -> None:
    db_path = tmp_path / "uninitialized.sqlite"
    sqlite3.connect(db_path).close()

    summary = canonical_store.summarize_canonical_store_population(db_path)

    assert summary["status"] == "uninitialized"
    assert summary["exists"] is True
    assert summary["initialized"] is False
    assert summary["valid"] is False
    assert summary["errors"]


def test_population_summary_reports_initialized_empty_store(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)

    summary = canonical_store.summarize_canonical_store_population(db_path)

    assert summary["status"] == "initialized_empty"
    assert summary["exists"] is True
    assert summary["initialized"] is True
    assert summary["valid"] is True
    assert summary["total_rows"] == 0
    assert summary["family_counts"]["entity"] == 0
    assert summary["family_counts"]["relationship"] == 0
    assert summary["family_counts"]["assertion"] == 0
    assert summary["family_counts"]["provenance_event"] == 0
    assert summary["last_ingest_at"] is None


def test_population_summary_reports_populated_store_and_last_ingest_without_mutation(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(FIXTURE_BATCH)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
    finally:
        conn.close()

    before = file_hash(db_path)
    summary = canonical_store.summarize_canonical_store_population(db_path)
    after = file_hash(db_path)

    assert before == after
    assert summary["status"] == "populated"
    assert summary["exists"] is True
    assert summary["initialized"] is True
    assert summary["valid"] is True
    assert summary["total_rows"] > 0
    assert summary["table_counts"]["work"] >= 1
    assert summary["table_counts"]["source_claim"] >= 1
    assert summary["last_ingest_event_type"] == "gather_candidate_batch_ingest"
    assert summary["last_ingest_at"] is not None
    assert summary["last_provenance_event_at"] == summary["last_ingest_at"]


def test_population_summary_fast_path_skips_full_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(FIXTURE_BATCH)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
    finally:
        conn.close()

    monkeypatch.setattr(
        canonical_store,
        "_count_known_tables",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full counts were requested")),
    )

    summary = canonical_store.summarize_canonical_store_population(
        db_path,
        include_counts=False,
    )

    assert summary["status"] == "populated"
    assert summary["valid"] is True
    assert summary["table_counts"] == {}
    assert summary["family_counts"] == {}
    assert summary["total_rows"] is None
    assert summary["last_ingest_event_type"] == "gather_candidate_batch_ingest"


def test_population_summary_reuses_prevalidated_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        outline = canonical_store.load_canonical_outline()
        version_row, table_set, extra_tables = canonical_store.validate_existing_store(
            conn,
            outline=outline,
        )
        validation = canonical_store.CheckResult(
            db_path=db_path,
            schema_version=version_row.schema_version,
            current_migration_id=version_row.current_migration_id,
            tables=tuple(sorted(table_set)),
            extra_tables=tuple(sorted(extra_tables)),
        )
        monkeypatch.setattr(
            canonical_store,
            "validate_existing_store",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("unexpected second validation")
            ),
        )

        summary = canonical_store.summarize_canonical_store_population(
            db_path,
            include_counts=False,
            conn=conn,
            validation=validation,
        )
    finally:
        conn.close()

    assert summary["status"] == "initialized_empty"
    assert summary["valid"] is True
    assert summary["schema_version"] == version_row.schema_version
    assert summary["current_migration_id"] == version_row.current_migration_id


def test_population_summary_warns_when_only_provenance_events_exist(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_store.record_provenance_event(
                conn,
                object_namespace="pytest",
                object_id="provenance-only",
                event_type="pytest_event",
                event_timestamp=FIXED_TIMESTAMP,
                provenance_event_key_v1="prov:pytest:provenance-only",
            )
    finally:
        conn.close()

    summary = canonical_store.summarize_canonical_store_population(db_path)

    assert summary["status"] == "populated"
    assert summary["table_counts"]["provenance_event"] == 1
    assert "provenance events exist, but no substantive canonical family rows were found" in summary[
        "warnings"
    ]
