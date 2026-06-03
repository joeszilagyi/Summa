"""Bootstrap, migrate, and validate the canonical SQLite store."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.canonical_graph_model_contract import (  # noqa: E402
    DOCUMENTED_EXPECTED_SQLITE_TABLES,
    REQUIRED_NONCANONICAL_STAGING_TABLES,
    REQUIRED_SCHEMA_METADATA_TABLES,
    REQUIRED_SUPPORTING_SQLITE_TABLES,
)

SCHEMA_NAMESPACE = "canonical_store"
CURRENT_SCHEMA_VERSION = 1
CURRENT_MIGRATION_ID = "0001_canonical_store"
SCHEMA_VERSION_TABLE = "schema_version"
MIGRATION_HISTORY_TABLE = "schema_migration_history"
MODULE_PATH = "tools/source_db_tools/canonical_store.py"
CLI_PATH = "tools/source_db_tools/init_canonical_store.py"
OUTLINE_PATH = REPO_ROOT / "config" / "canonical_graph_model_outline.json"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "schema" / "migrations"

REQUIRED_INDEXES = {
    "ix_authority_identifier_record",
    "ix_authority_merge_event_from_into",
    "ix_authority_record_label",
    "ix_authority_record_merge",
    "ix_capture_event_hash",
    "ix_capture_event_work",
    "ix_detected_entity_authority",
    "ix_detected_entity_extraction",
    "ix_extraction_record_capture",
    "ix_provenance_event_object",
    "ix_provenance_event_run",
    "ix_review_state_history_target",
    "ix_source_access_work",
    "ix_source_access_workspace",
    "ix_source_claim_about",
    "ix_source_claim_review",
    "ix_source_relationship_refs",
    "ix_source_relationship_review",
    "ix_topic_extension_topic",
    "ix_work_identifier_work",
    "ix_work_metadata_work",
    "ix_work_review",
    "ix_work_subject_work",
    "ix_work_title",
    "ix_work_url_work",
}

OPTIONAL_COMPATIBILITY_TABLES = {
    "entity_work",
    "extraction_highlight",
    "lead",
    "source_query_plan",
}


class CanonicalStoreError(RuntimeError):
    """Raised when the canonical store cannot be initialized or checked."""


@dataclass(frozen=True)
class MigrationSpec:
    version: int
    migration_id: str
    sql_path: Path
    notes: str


@dataclass(frozen=True)
class SchemaVersionRecord:
    schema_namespace: str
    schema_version: int
    current_migration_id: str
    applied_at: str
    applied_by: str
    ddl_hash: str
    notes: str | None


@dataclass(frozen=True)
class MigrationResult:
    start_version: int
    end_version: int
    applied_migration_ids: tuple[str, ...]
    noop: bool


@dataclass(frozen=True)
class InitResult:
    db_path: Path
    schema_version: int
    current_migration_id: str
    applied_migration_ids: tuple[str, ...]
    created: bool
    changed: bool
    tables: tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    db_path: Path
    schema_version: int
    current_migration_id: str
    tables: tuple[str, ...]
    extra_tables: tuple[str, ...]


MIGRATIONS: tuple[MigrationSpec, ...] = (
    MigrationSpec(
        version=1,
        migration_id=CURRENT_MIGRATION_ID,
        sql_path=MIGRATIONS_DIR / "0001_canonical_store.sql",
        notes="Initial canonical store bootstrap.",
    ),
)


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_db_path(db_path: Path | str) -> Path:
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def connect_canonical_store(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_existing_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise CanonicalStoreError(f"database not found: {db_path}")
    if not db_path.is_file():
        raise CanonicalStoreError(f"database path is not a file: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def actual_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def actual_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def load_canonical_outline(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    path = repo_root / "config" / "canonical_graph_model_outline.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalStoreError(f"failed to load canonical graph model outline: {path}") from exc
    if not isinstance(payload, dict):
        raise CanonicalStoreError(f"canonical graph model outline must be a JSON object: {path}")
    return payload


def family_table_mapping(outline: dict[str, Any]) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for item in outline.get("canonical_record_families", []):
        if not isinstance(item, dict):
            continue
        family = item.get("record_family")
        tables = item.get("current_sqlite_tables")
        if not isinstance(family, str) or not family.strip() or not isinstance(tables, list):
            continue
        mapping[family] = {
            table_name
            for table_name in tables
            if isinstance(table_name, str) and table_name.strip()
        }
    return mapping


def supporting_tables_from_outline(outline: dict[str, Any]) -> set[str]:
    return {
        table_name
        for table_name in outline.get("supporting_sqlite_tables", [])
        if isinstance(table_name, str) and table_name.strip()
    }


def schema_metadata_tables_from_outline(outline: dict[str, Any]) -> set[str]:
    return {
        table_name
        for table_name in outline.get("schema_metadata_tables", [])
        if isinstance(table_name, str) and table_name.strip()
    }


def staging_tables_from_outline(outline: dict[str, Any]) -> set[str]:
    return {
        table_name
        for table_name in outline.get("noncanonical_staging_tables", [])
        if isinstance(table_name, str) and table_name.strip()
    }


def expected_tables_from_outline(outline: dict[str, Any]) -> set[str]:
    tables: set[str] = set()
    for mapped_tables in family_table_mapping(outline).values():
        tables.update(mapped_tables)
    return tables


def expected_bootstrap_tables_from_outline(outline: dict[str, Any]) -> set[str]:
    return (
        expected_tables_from_outline(outline)
        | supporting_tables_from_outline(outline)
        | schema_metadata_tables_from_outline(outline)
    )


def classified_outline_tables(outline: dict[str, Any]) -> dict[str, str]:
    classified: dict[str, str] = {}
    for family, tables in family_table_mapping(outline).items():
        for table_name in tables:
            classified[table_name] = f"family:{family}"
    for table_name in supporting_tables_from_outline(outline):
        classified[table_name] = "supporting"
    for table_name in schema_metadata_tables_from_outline(outline):
        classified[table_name] = "schema_metadata"
    for table_name in staging_tables_from_outline(outline):
        classified[table_name] = "noncanonical_staging"
    return classified


def migration_ddl_hash(migration: MigrationSpec) -> str:
    digest = hashlib.sha256()
    with migration.sql_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def sql_quote(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def load_sql_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CanonicalStoreError(f"failed to read migration SQL: {path}") from exc


def build_metadata_sql(
    migration: MigrationSpec,
    *,
    applied_at: str,
    applied_by: str,
    ddl_hash: str,
) -> str:
    migration_values = (
        sql_quote(migration.migration_id),
        sql_quote(SCHEMA_NAMESPACE),
        str(migration.version),
        sql_quote(applied_at),
        sql_quote(applied_by),
        sql_quote(ddl_hash),
        sql_quote(migration.notes),
    )
    version_values = (
        sql_quote(SCHEMA_NAMESPACE),
        str(migration.version),
        sql_quote(migration.migration_id),
        sql_quote(applied_at),
        sql_quote(applied_by),
        sql_quote(ddl_hash),
        sql_quote(migration.notes),
    )
    return f"""
INSERT INTO {MIGRATION_HISTORY_TABLE} (
  migration_id,
  schema_namespace,
  schema_version,
  applied_at,
  applied_by,
  ddl_hash,
  notes
) VALUES ({", ".join(migration_values)});
INSERT INTO {SCHEMA_VERSION_TABLE} (
  schema_namespace,
  schema_version,
  current_migration_id,
  applied_at,
  applied_by,
  ddl_hash,
  notes
) VALUES ({", ".join(version_values)})
ON CONFLICT(schema_namespace) DO UPDATE SET
  schema_version=excluded.schema_version,
  current_migration_id=excluded.current_migration_id,
  applied_at=excluded.applied_at,
  applied_by=excluded.applied_by,
  ddl_hash=excluded.ddl_hash,
  notes=excluded.notes;
PRAGMA user_version = {migration.version};
"""


def validate_outline_contract(outline: dict[str, Any]) -> None:
    family_tables = expected_tables_from_outline(outline)
    if not DOCUMENTED_EXPECTED_SQLITE_TABLES.issubset(family_tables):
        missing = sorted(DOCUMENTED_EXPECTED_SQLITE_TABLES - family_tables)
        raise CanonicalStoreError(
            "canonical graph model outline is missing documented family table mappings: "
            + ", ".join(missing)
        )
    supporting = supporting_tables_from_outline(outline)
    if not REQUIRED_SUPPORTING_SQLITE_TABLES.issubset(supporting):
        missing = sorted(REQUIRED_SUPPORTING_SQLITE_TABLES - supporting)
        raise CanonicalStoreError(
            "canonical graph model outline is missing required supporting SQLite tables: "
            + ", ".join(missing)
        )
    metadata = schema_metadata_tables_from_outline(outline)
    if metadata != REQUIRED_SCHEMA_METADATA_TABLES:
        missing = sorted(REQUIRED_SCHEMA_METADATA_TABLES - metadata)
        extra = sorted(metadata - REQUIRED_SCHEMA_METADATA_TABLES)
        message_parts = []
        if missing:
            message_parts.append("missing " + ", ".join(missing))
        if extra:
            message_parts.append("unexpected " + ", ".join(extra))
        raise CanonicalStoreError(
            "canonical graph model outline schema metadata tables drifted: "
            + "; ".join(message_parts)
        )
    staging = staging_tables_from_outline(outline)
    if not REQUIRED_NONCANONICAL_STAGING_TABLES.issubset(staging):
        missing = sorted(REQUIRED_NONCANONICAL_STAGING_TABLES - staging)
        raise CanonicalStoreError(
            "canonical graph model outline is missing required noncanonical staging tables: "
            + ", ".join(missing)
        )


def get_schema_version(conn: sqlite3.Connection) -> SchemaVersionRecord | None:
    if not table_exists(conn, SCHEMA_VERSION_TABLE):
        return None
    row = conn.execute(
        f"""
        SELECT schema_namespace, schema_version, current_migration_id, applied_at,
               applied_by, ddl_hash, notes
        FROM {SCHEMA_VERSION_TABLE}
        WHERE schema_namespace=?
        """,
        (SCHEMA_NAMESPACE,),
    ).fetchone()
    if row is None:
        return None
    return SchemaVersionRecord(
        schema_namespace=str(row["schema_namespace"]),
        schema_version=int(row["schema_version"]),
        current_migration_id=str(row["current_migration_id"]),
        applied_at=str(row["applied_at"]),
        applied_by=str(row["applied_by"]),
        ddl_hash=str(row["ddl_hash"]),
        notes=None if row["notes"] is None else str(row["notes"]),
    )


def load_applied_migrations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, MIGRATION_HISTORY_TABLE):
        return []
    rows = conn.execute(
        f"""
        SELECT migration_id, schema_namespace, schema_version, applied_at, applied_by, ddl_hash, notes
        FROM {MIGRATION_HISTORY_TABLE}
        WHERE schema_namespace=?
        ORDER BY schema_version, migration_id
        """,
        (SCHEMA_NAMESPACE,),
    ).fetchall()
    return list(rows)


def current_migration_version(migrations: Iterable[MigrationSpec] = MIGRATIONS) -> int:
    versions = [migration.version for migration in migrations]
    if not versions:
        raise CanonicalStoreError("migration registry is empty")
    return max(versions)


def validate_existing_store(
    conn: sqlite3.Connection,
    *,
    outline: dict[str, Any] | None = None,
) -> tuple[SchemaVersionRecord, set[str], set[str]]:
    outline_payload = load_canonical_outline() if outline is None else outline
    validate_outline_contract(outline_payload)

    table_set = actual_tables(conn)
    metadata_tables = schema_metadata_tables_from_outline(outline_payload)
    missing_metadata_tables = metadata_tables - table_set
    if missing_metadata_tables:
        missing = ", ".join(sorted(missing_metadata_tables))
        raise CanonicalStoreError(f"canonical store is missing schema metadata tables: {missing}")

    version_row = get_schema_version(conn)
    if version_row is None:
        raise CanonicalStoreError(
            "canonical store metadata is present but schema_version row for canonical_store is missing"
        )
    latest_version = current_migration_version()
    if version_row.schema_version > latest_version:
        raise CanonicalStoreError(
            f"canonical store schema_version {version_row.schema_version} is newer than supported version {latest_version}"
        )

    history_rows = load_applied_migrations(conn)
    if len(history_rows) != version_row.schema_version:
        raise CanonicalStoreError(
            "canonical store migration history count does not match schema_version"
        )
    expected_versions = list(range(1, version_row.schema_version + 1))
    actual_versions = [int(row["schema_version"]) for row in history_rows]
    if actual_versions != expected_versions:
        raise CanonicalStoreError(
            "canonical store migration history is not contiguous from version 1"
        )
    if history_rows:
        latest_history = history_rows[-1]
        if str(latest_history["migration_id"]) != version_row.current_migration_id:
            raise CanonicalStoreError(
                "canonical store schema_version current_migration_id does not match migration history"
            )

    expected_tables = expected_bootstrap_tables_from_outline(outline_payload)
    missing_tables = expected_tables - table_set
    if missing_tables:
        raise CanonicalStoreError(
            "canonical store is missing required tables: " + ", ".join(sorted(missing_tables))
        )
    missing_indexes = REQUIRED_INDEXES - actual_indexes(conn)
    if missing_indexes:
        raise CanonicalStoreError(
            "canonical store is missing required indexes: " + ", ".join(sorted(missing_indexes))
        )
    foreign_keys_row = conn.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys_row is None or int(foreign_keys_row[0]) != 1:
        raise CanonicalStoreError("canonical store connection does not have PRAGMA foreign_keys=ON")

    allowed_extra = staging_tables_from_outline(outline_payload) | OPTIONAL_COMPATIBILITY_TABLES
    extra_tables = table_set - expected_tables
    unexpected_extras = sorted(extra_tables - allowed_extra)
    if unexpected_extras:
        # Unknown extra tables are tolerated so long as the canonical substrate is valid,
        # but we still surface them to the caller through CheckResult.extra_tables.
        pass

    return version_row, table_set, extra_tables


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    target_version: int | None = None,
    applied_at: str | None = None,
    applied_by: str = CLI_PATH,
    migrations: Iterable[MigrationSpec] = MIGRATIONS,
    outline: dict[str, Any] | None = None,
) -> MigrationResult:
    outline_payload = load_canonical_outline() if outline is None else outline
    validate_outline_contract(outline_payload)

    migration_list = tuple(sorted(migrations, key=lambda item: item.version))
    latest_version = current_migration_version(migration_list)
    target = latest_version if target_version is None else int(target_version)
    if target < 1:
        raise CanonicalStoreError(f"refusing canonical store downgrade target: {target}")
    if target > latest_version:
        raise CanonicalStoreError(
            f"requested canonical store target version {target} is newer than supported version {latest_version}"
        )

    table_set = actual_tables(conn)
    metadata_tables_present = REQUIRED_SCHEMA_METADATA_TABLES.issubset(table_set)
    version_row = get_schema_version(conn) if metadata_tables_present else None
    if not metadata_tables_present:
        if table_set:
            raise CanonicalStoreError(
                "database already contains tables but lacks canonical schema metadata; refusing bootstrap over unknown state"
            )
        current_version = 0
    else:
        if version_row is None:
            raise CanonicalStoreError(
                "database has canonical schema metadata tables but no schema_version row for canonical_store"
            )
        if version_row.schema_version > latest_version:
            raise CanonicalStoreError(
                f"database schema_version {version_row.schema_version} is newer than supported version {latest_version}"
            )
        if target < version_row.schema_version:
            raise CanonicalStoreError(
                f"refusing canonical store downgrade from {version_row.schema_version} to {target}"
            )
        current_version = version_row.schema_version

    pending = [
        migration for migration in migration_list if current_version < migration.version <= target
    ]
    if not pending:
        return MigrationResult(
            start_version=current_version,
            end_version=current_version,
            applied_migration_ids=(),
            noop=True,
        )

    timestamp = now_rfc3339() if applied_at is None else applied_at
    script_parts = ["PRAGMA foreign_keys=ON;", "BEGIN IMMEDIATE;"]
    for migration in pending:
        sql_text = load_sql_text(migration.sql_path).strip()
        if not sql_text:
            raise CanonicalStoreError(f"migration SQL is empty: {migration.sql_path}")
        ddl_hash = migration_ddl_hash(migration)
        script_parts.append(f"-- migration {migration.migration_id}")
        script_parts.append(sql_text)
        script_parts.append(
            build_metadata_sql(
                migration,
                applied_at=timestamp,
                applied_by=applied_by,
                ddl_hash=ddl_hash,
            ).strip()
        )
    script_parts.append("COMMIT;")
    script = "\n".join(script_parts) + "\n"

    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        conn.rollback()
        raise CanonicalStoreError(f"failed to apply canonical store migrations: {exc}") from exc

    final_version = get_schema_version(conn)
    if final_version is None:
        raise CanonicalStoreError(
            "canonical store migrations completed without recording schema_version"
        )
    return MigrationResult(
        start_version=current_version,
        end_version=final_version.schema_version,
        applied_migration_ids=tuple(migration.migration_id for migration in pending),
        noop=False,
    )


def init_canonical_store(
    db_path: Path,
    *,
    target_version: int | None = None,
    applied_at: str | None = None,
    applied_by: str = CLI_PATH,
) -> InitResult:
    path = resolve_db_path(db_path)
    if path.exists() and not path.is_file():
        raise CanonicalStoreError(f"database path is not a file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    outline = load_canonical_outline()
    conn = connect_canonical_store(path)
    try:
        migration_result = apply_migrations(
            conn,
            target_version=target_version,
            applied_at=applied_at,
            applied_by=applied_by,
            outline=outline,
        )
        version_row, table_set, _extra = validate_existing_store(conn, outline=outline)
    finally:
        conn.close()
    return InitResult(
        db_path=path,
        schema_version=version_row.schema_version,
        current_migration_id=version_row.current_migration_id,
        applied_migration_ids=migration_result.applied_migration_ids,
        created=created,
        changed=not migration_result.noop,
        tables=tuple(sorted(table_set)),
    )


def check_canonical_store(db_path: Path) -> CheckResult:
    path = resolve_db_path(db_path)
    outline = load_canonical_outline()
    conn = connect_existing_read_only(path)
    try:
        version_row, table_set, extra_tables = validate_existing_store(conn, outline=outline)
    finally:
        conn.close()
    return CheckResult(
        db_path=path,
        schema_version=version_row.schema_version,
        current_migration_id=version_row.current_migration_id,
        tables=tuple(sorted(table_set)),
        extra_tables=tuple(sorted(extra_tables)),
    )
