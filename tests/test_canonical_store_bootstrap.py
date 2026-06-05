from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.source_db_tools import (
    authority_reconciliation,
    canonical_store,
    export_bibliography,
    provenance_events,
    review_queue,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "tools" / "source_db_tools" / "init_canonical_store.py"
FIXED_TIMESTAMP = "2026-06-03T08:00:00Z"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def expected_bootstrap_tables() -> set[str]:
    outline = canonical_store.load_canonical_outline()
    return canonical_store.expected_bootstrap_tables_from_outline(outline)


def expected_migration_ids() -> tuple[str, ...]:
    return tuple(migration.migration_id for migration in canonical_store.MIGRATIONS)


def bootstrap_db(tmp_path: Path, *, name: str = "canonical.sqlite") -> Path:
    db_path = tmp_path / name
    result = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest",
    )
    assert result.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    return db_path


def test_init_cli_help_exits_zero() -> None:
    result = run_cli("--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Initialize, migrate, or check the canonical SQLite store." in result.stdout


def test_init_cli_can_bootstrap_and_check_store(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"

    init_result = run_cli("--db", str(db_path))
    check_result = run_cli("--db", str(db_path), "--check")

    assert init_result.returncode == 0, init_result.stdout + init_result.stderr
    assert "status=ok" in init_result.stdout
    assert f"schema_version={canonical_store.CURRENT_SCHEMA_VERSION}" in init_result.stdout
    assert check_result.returncode == 0, check_result.stdout + check_result.stderr
    assert "action=check" in check_result.stdout


def test_empty_db_bootstrap_creates_required_tables_and_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"

    result = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest",
    )

    assert db_path.exists()
    assert result.created is True
    assert result.changed is True
    assert result.applied_migration_ids == expected_migration_ids()

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert canonical_store.actual_tables(conn) == expected_bootstrap_tables()
        version_row = canonical_store.get_schema_version(conn)
        assert version_row is not None
        assert version_row.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
        assert version_row.current_migration_id == canonical_store.CURRENT_MIGRATION_ID
        history_rows = canonical_store.load_applied_migrations(conn)
        assert len(history_rows) == canonical_store.CURRENT_SCHEMA_VERSION
        assert tuple(row["migration_id"] for row in history_rows) == expected_migration_ids()
        assert (
            int(conn.execute("PRAGMA user_version").fetchone()[0])
            == canonical_store.CURRENT_SCHEMA_VERSION
        )
    finally:
        conn.close()


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"

    first = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest:first",
    )
    second = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest:second",
    )

    assert first.changed is True
    assert second.changed is False
    assert second.applied_migration_ids == ()

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        history_count = conn.execute(
            f"SELECT COUNT(*) FROM {canonical_store.MIGRATION_HISTORY_TABLE}"
        ).fetchone()[0]
        version_count = conn.execute(
            f"SELECT COUNT(*) FROM {canonical_store.SCHEMA_VERSION_TABLE} WHERE schema_namespace=?",
            (canonical_store.SCHEMA_NAMESPACE,),
        ).fetchone()[0]
        assert int(history_count) == canonical_store.CURRENT_SCHEMA_VERSION
        assert int(version_count) == 1
        assert canonical_store.actual_tables(conn) == set(first.tables) == set(second.tables)
    finally:
        conn.close()


def test_check_mode_succeeds_for_valid_store_and_fails_for_missing_table(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    result = canonical_store.check_canonical_store(db_path)
    assert result.schema_version == canonical_store.CURRENT_SCHEMA_VERSION

    broken_path = tmp_path / "broken.sqlite"
    shutil.copy2(db_path, broken_path)
    conn = sqlite3.connect(broken_path)
    try:
        conn.execute("DROP TABLE source_claim")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(canonical_store.CanonicalStoreError, match="missing required tables"):
        canonical_store.check_canonical_store(broken_path)


def test_outline_and_bootstrap_tables_do_not_drift(tmp_path: Path) -> None:
    outline = canonical_store.load_canonical_outline()
    classified = canonical_store.classified_outline_tables(outline)

    assert (
        canonical_store.expected_tables_from_outline(outline)
        >= canonical_store.DOCUMENTED_EXPECTED_SQLITE_TABLES
    )
    assert (
        canonical_store.supporting_tables_from_outline(outline)
        == canonical_store.REQUIRED_SUPPORTING_SQLITE_TABLES
    )
    assert (
        canonical_store.schema_metadata_tables_from_outline(outline)
        == canonical_store.REQUIRED_SCHEMA_METADATA_TABLES
    )

    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        table_set = canonical_store.actual_tables(conn)
    finally:
        conn.close()

    assert table_set == canonical_store.expected_bootstrap_tables_from_outline(outline)
    assert canonical_store.family_table_mapping(outline)["confidence_assessment"]
    assert canonical_store.family_table_mapping(outline)["review_annotation"]
    assert not (table_set - set(classified))


def test_migration_runner_refuses_downgrade_and_unknown_future_version(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(canonical_store.CanonicalStoreError, match="downgrade"):
            canonical_store.apply_migrations(
                conn,
                target_version=0,
                applied_at=FIXED_TIMESTAMP,
                applied_by="pytest",
            )
    finally:
        conn.close()

    future_path = tmp_path / "future.sqlite"
    shutil.copy2(db_path, future_path)
    conn = sqlite3.connect(future_path)
    try:
        conn.execute(
            f"""
            UPDATE {canonical_store.SCHEMA_VERSION_TABLE}
            SET schema_version=?, current_migration_id=?, applied_by=?
            WHERE schema_namespace=?
            """,
            (99, "0099_future", "pytest:future", canonical_store.SCHEMA_NAMESPACE),
        )
        conn.execute("PRAGMA user_version=99")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(canonical_store.CanonicalStoreError, match="newer than supported"):
        canonical_store.check_canonical_store(future_path)


def test_init_canonical_store_upgrades_v2_db_with_source_access_provenance_event_ref(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical-v2.sqlite"
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        canonical_store.apply_migrations(
            conn,
            target_version=2,
            applied_at=FIXED_TIMESTAMP,
            applied_by="pytest",
            migrations=canonical_store.MIGRATIONS[:2],
        )
    finally:
        conn.close()

    result = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest",
    )

    assert result.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    assert result.applied_migration_ids == (
        "0003_source_access_provenance_event_ref",
        "0004_source_access_lead_identity",
        "0005_extraction_detected_entity_workspace",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(source_access)").fetchall()
        }
        indexes = canonical_store.actual_indexes(conn)
        version_row = canonical_store.get_schema_version(conn)
    finally:
        conn.close()

    assert "provenance_event_ref" in columns
    assert "ix_source_access_provenance_event_ref" in indexes
    assert version_row is not None
    assert version_row.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    assert version_row.current_migration_id == canonical_store.CURRENT_MIGRATION_ID


def test_init_canonical_store_upgrades_v3_db_with_source_access_lead_identity_indexes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "canonical-v3.sqlite"
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        canonical_store.apply_migrations(
            conn,
            target_version=3,
            applied_at=FIXED_TIMESTAMP,
            applied_by="pytest",
            migrations=canonical_store.MIGRATIONS[:3],
        )
    finally:
        conn.close()

    result = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest",
    )

    assert result.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    assert result.applied_migration_ids == (
        "0004_source_access_lead_identity",
        "0005_extraction_detected_entity_workspace",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        indexes = canonical_store.actual_indexes(conn)
        version_row = canonical_store.get_schema_version(conn)
    finally:
        conn.close()

    assert "ux_source_access_lead_identity_workspace" in indexes
    assert "ux_source_access_lead_identity_global" in indexes
    assert version_row is not None
    assert version_row.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    assert version_row.current_migration_id == canonical_store.CURRENT_MIGRATION_ID


def test_init_canonical_store_upgrades_v4_db_with_detected_entity_workspace_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "canonical-v4.sqlite"
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        canonical_store.apply_migrations(
            conn,
            target_version=4,
            applied_at=FIXED_TIMESTAMP,
            applied_by="pytest",
            migrations=canonical_store.MIGRATIONS[:4],
        )
        conn.execute(
            """
            INSERT INTO provenance_event (
              provenance_event_key_v1,
              object_namespace,
              object_id,
              event_type,
              event_timestamp,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "prov:fixture-detected-entity",
                "fixture",
                "fixture-detected-entity",
                "fixture_ingest",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO capture_event (
              capture_event_id,
              original_locator,
              captured_at,
              capture_method,
              review_state,
              workspace_id,
              provenance_event_ref,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "https://example.test/capture",
                FIXED_TIMESTAMP,
                "pytest",
                "needs_review",
                "fixture_subject",
                "prov:fixture-detected-entity",
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO extraction_detected_entity (
              detected_entity_id,
              capture_event_id,
              entity_label,
              entity_type,
              review_state,
              confidence_score,
              provenance_event_ref,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                "Fixture Entity",
                "person",
                "proposed",
                0.91,
                "prov:fixture-detected-entity",
                FIXED_TIMESTAMP,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest",
    )

    assert result.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    assert result.applied_migration_ids == ("0005_extraction_detected_entity_workspace",)

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(extraction_detected_entity)").fetchall()
        }
        indexes = canonical_store.actual_indexes(conn)
        row = conn.execute(
            """
            SELECT workspace_id
            FROM extraction_detected_entity
            WHERE detected_entity_id=1
            """
        ).fetchone()
        version_row = canonical_store.get_schema_version(conn)
    finally:
        conn.close()

    assert "workspace_id" in columns
    assert "ix_detected_entity_workspace" in indexes
    assert row["workspace_id"] == "fixture_subject"
    assert version_row is not None
    assert version_row.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    assert version_row.current_migration_id == canonical_store.CURRENT_MIGRATION_ID


def test_work_identifier_scheme_value_is_database_unique_across_works(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO work (
                  work_id, work_key_v1, work_type, title, review_state,
                  created_at, record_last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (1, "work:one", "article", "Work One", "needs_review", FIXED_TIMESTAMP, FIXED_TIMESTAMP),
            )
            conn.execute(
                """
                INSERT INTO work (
                  work_id, work_key_v1, work_type, title, review_state,
                  created_at, record_last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (2, "work:two", "article", "Work Two", "needs_review", FIXED_TIMESTAMP, FIXED_TIMESTAMP),
            )
            conn.execute(
                """
                INSERT INTO work_identifier (
                  work_id, scheme, value, raw_value, normalized_value,
                  normalized_uri, validity_status, is_primary, review_state,
                  record_last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "doi",
                    "10.1234/example",
                    "10.1234/example",
                    "10.1234/example",
                    None,
                    "valid",
                    1,
                    "accepted",
                    FIXED_TIMESTAMP,
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO work_identifier (
                      work_id, scheme, value, raw_value, normalized_value,
                      normalized_uri, validity_status, is_primary, review_state,
                      record_last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        "doi",
                        "10.1234/example",
                        "10.1234/example",
                        "10.1234/example",
                        None,
                        "valid",
                        1,
                        "accepted",
                        FIXED_TIMESTAMP,
                    ),
                )
    finally:
        conn.close()


def test_migration_runner_rolls_back_on_bad_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "broken_migration.sqlite"
    conn = canonical_store.connect_canonical_store(db_path)
    bad_sql = tmp_path / "0002_bad.sql"
    bad_sql.write_text("CREATE TABLE broken (\n", encoding="utf-8")
    migrations = (
        canonical_store.MIGRATIONS[0],
        canonical_store.MigrationSpec(
            version=2,
            migration_id="0002_bad",
            sql_path=bad_sql,
            notes="Intentional fixture failure.",
        ),
    )
    try:
        with pytest.raises(
            canonical_store.CanonicalStoreError, match="failed to apply canonical store migrations"
        ):
            canonical_store.apply_migrations(
                conn,
                target_version=2,
                applied_at=FIXED_TIMESTAMP,
                applied_by="pytest",
                migrations=migrations,
            )
        assert canonical_store.actual_tables(conn) == set()
    finally:
        conn.close()


def test_migration_runner_preserves_original_error_when_rollback_fails(tmp_path: Path) -> None:
    class FailingRollbackConnection(sqlite3.Connection):
        def rollback(self) -> None:  # type: ignore[override]
            raise sqlite3.OperationalError("rollback failed")

    db_path = tmp_path / "broken_migration_with_bad_rollback.sqlite"
    conn = sqlite3.connect(db_path, factory=FailingRollbackConnection)
    conn.row_factory = sqlite3.Row
    bad_sql = tmp_path / "0002_bad.sql"
    bad_sql.write_text("CREATE TABLE broken (\n", encoding="utf-8")
    migrations = (
        canonical_store.MIGRATIONS[0],
        canonical_store.MigrationSpec(
            version=2,
            migration_id="0002_bad",
            sql_path=bad_sql,
            notes="Intentional fixture failure.",
        ),
    )
    try:
        with pytest.raises(
            canonical_store.CanonicalStoreError, match="failed to apply canonical store migrations"
        ) as excinfo:
            canonical_store.apply_migrations(
                conn,
                target_version=2,
                applied_at=FIXED_TIMESTAMP,
                applied_by="pytest",
                migrations=migrations,
            )
        assert isinstance(excinfo.value.__cause__, sqlite3.Error)
        assert "rollback failed" not in str(excinfo.value)
    finally:
        conn.close()


def test_existing_source_db_helpers_work_against_bootstrapped_store(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        conn.execute(
            """
            INSERT INTO work (
              work_id, work_key_v1, work_type, title, rights_posture,
              refetchability_status, review_state, confidence_score,
              workspace_id, authority_level, first_seen_at, last_seen_at,
              created_at, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "work:test-alpha",
                "webpage",
                "Alpha Work",
                "unknown",
                "unknown",
                "needs_review",
                0.5,
                "alpha_subject",
                "primary",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO work_identifier (
              work_id, scheme, value, raw_value, normalized_value,
              normalized_uri, validity_status, validation_warning,
              is_primary, confidence_score, review_state, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "local",
                "alpha-1",
                "alpha-1",
                "alpha-1",
                None,
                "valid",
                None,
                1,
                1.0,
                "accepted",
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_access (
              work_id, original_locator, canonical_url, access_class,
              refetchability_status, rights_posture, citation_hint,
              review_state, workspace_id, first_seen_at, last_seen_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "fixtures/alpha.txt",
                "https://example.test/alpha",
                "local_fixture",
                "unknown",
                "unknown",
                "fixture",
                "needs_review",
                "alpha_subject",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        authority_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Alpha Subject",
            source_id="fixture-alpha",
            confidence_score=0.82,
            created_at=FIXED_TIMESTAMP,
        )
        identifier_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=authority_id,
            scheme="local",
            value="alpha-subject",
            is_primary=1,
            confidence_score=0.9,
            review_state="accepted",
            verified_at=FIXED_TIMESTAMP,
        )
        conn.execute(
            """
            INSERT INTO extraction_detected_entity (
              detected_entity_id, entity_label, entity_type, authority_record_id,
              review_state, confidence_score, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "Alpha Subject",
                "person",
                None,
                "proposed",
                0.42,
                FIXED_TIMESTAMP,
            ),
        )
        reconciliation_id = authority_reconciliation.propose_candidate(
            conn,
            detected_entity_id=1,
            raw_label="Alpha Subject",
            entity_type="person",
            candidate_authority_id=authority_id,
            match_method="label",
            match_score=0.91,
            created_at=FIXED_TIMESTAMP,
        )
        provenance_id = provenance_events.record_event(
            conn,
            object_namespace="work",
            object_id=1,
            event_type="created",
            tool_name="pytest",
            run_id="run:test",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:test-created-work",
        )
        conn.commit()

        records = export_bibliography.load_records(conn)
        queue_rows = review_queue.list_review_items(conn, object_type="work", state="all")
    finally:
        conn.close()

    assert authority_id > 0
    assert identifier_id > 0
    assert reconciliation_id > 0
    assert provenance_id > 0
    assert len(records) == 1
    assert records[0]["work"]["title"] == "Alpha Work"
    assert records[0]["work_identifiers"][0]["value"] == "alpha-1"
    assert records[0]["source_access"][0]["canonical_url"] == "https://example.test/alpha"
    assert queue_rows[0]["object_ref"] == "work:1"


def test_migration_sql_contains_no_destructive_statements() -> None:
    sql_text = "\n".join(
        line
        for migration in canonical_store.MIGRATIONS
        for line in migration.sql_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    ).upper()
    assert "DROP TABLE" not in sql_text
    assert "CREATE TABLE AS SELECT" not in sql_text
