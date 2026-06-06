#!/usr/bin/env python3
"""Build a deterministic local-search projection JSON artifact and FTS5 index."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
    REPO_ROOT / "tools" / "source_db_tools",
    REPO_ROOT,
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.local_search_contract import (  # noqa: E402
    PROJECTION_SCHEMA_VERSION,
    PUBLIC_SEARCHABLE_PUBLICATION_STATES,
    VISIBILITY_PROFILES,
    is_public_profile,
    is_searchable_review_state,
    normalize_publication_state,
)
from tools.validators.validate_correction_ledger import EXIT_PASS as EXIT_LEDGER_PASS  # noqa: E402
from tools.validators.validate_correction_ledger import validate_correction_ledger  # noqa: E402
from tools.validators.validate_local_search_projection import validate_local_search_projection_payload  # noqa: E402


SCRIPT_PATH = "tools/scripts/build_local_search_projection.py"
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SearchField:
    field_name: str
    candidates: tuple[str, ...]
    display_policy: str
    title_hint: bool = False
    subtitle_hint: bool = False


@dataclass(frozen=True)
class SearchTarget:
    object_type: str
    table: str
    pk_column: str
    review_state_column: str = "review_state"
    field_specs: tuple[SearchField, ...] = ()


TARGETS: tuple[SearchTarget, ...] = (
    SearchTarget(
        object_type="work",
        table="work",
        pk_column="work_id",
        field_specs=(
            SearchField("title", ("title",), "public", title_hint=True),
            SearchField("work_type", ("work_type",), "public", subtitle_hint=True),
        ),
    ),
    SearchTarget(
        object_type="authority",
        table="authority_record",
        pk_column="authority_record_id",
        field_specs=(
            SearchField("preferred_label", ("preferred_label",), "public", title_hint=True),
            SearchField("authority_type", ("authority_type",), "public", subtitle_hint=True),
        ),
    ),
    SearchTarget(
        object_type="claim",
        table="source_claim",
        pk_column="source_claim_id",
        field_specs=(
            SearchField("public_summary", ("public_summary",), "public", title_hint=True),
            SearchField("claim_text", ("claim_text",), "local_only", title_hint=True),
            SearchField("claim_type", ("claim_type",), "public", subtitle_hint=True),
        ),
    ),
    SearchTarget(
        object_type="source_access",
        table="source_access",
        pk_column="source_access_id",
        field_specs=(
            SearchField("canonical_url", ("canonical_url",), "public", title_hint=True),
            SearchField("original_locator", ("original_locator",), "local_only", title_hint=True),
            SearchField("access_class", ("access_class",), "public", subtitle_hint=True),
        ),
    ),
    SearchTarget(
        object_type="relationship",
        table="source_relationship",
        pk_column="source_relationship_id",
        field_specs=(
            SearchField("predicate", ("predicate",), "public", title_hint=True),
            SearchField("target_label", ("target_label",), "public"),
            SearchField("evidence_note", ("evidence_note",), "local_only"),
        ),
    ),
    SearchTarget(
        object_type="provenance_event",
        table="provenance_event",
        pk_column="provenance_event_id",
        field_specs=(
            SearchField("event_type", ("event_type",), "public", title_hint=True),
            SearchField("actor_label", ("actor_label",), "public", subtitle_hint=True),
            SearchField("note_text", ("note_text",), "local_only"),
        ),
    ),
    SearchTarget(
        object_type="topic_extension",
        table="topic_extension",
        pk_column="topic_extension_id",
        field_specs=(
            SearchField("summary_short", ("summary_short",), "public", title_hint=True),
            SearchField("extension_type", ("extension_type",), "public", title_hint=True),
            SearchField("topic_id", ("topic_id",), "public", subtitle_hint=True),
            SearchField("note_text", ("note_text",), "local_only"),
        ),
    ),
)


class SearchProjectionError(RuntimeError):
    """Raised when projection inputs or outputs cannot be processed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic local-search projection JSON artifact and FTS5 index."
    )
    parser.add_argument("--db", required=True, help="Path to the source SQLite database.")
    parser.add_argument(
        "--profile",
        choices=tuple(sorted(VISIBILITY_PROFILES)),
        required=True,
        help="Visibility profile to build.",
    )
    parser.add_argument(
        "--index-db",
        required=True,
        help="Path to the output SQLite database that will receive the projection table and FTS5 index.",
    )
    parser.add_argument(
        "--output-json",
        help="Optional JSON path for the emitted local-search projection artifact. Omit to write JSON to stdout only.",
    )
    parser.add_argument(
        "--correction-ledger",
        help="Optional validated correction-ledger JSON path used to mark current vs superseded object refs.",
    )
    parser.add_argument(
        "--generated-at",
        help="Optional RFC3339 timestamp override for deterministic tests.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Stdout format for the emitted projection artifact.",
    )
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_existing_file(raw_path: str) -> Path:
    path = resolve_path(raw_path)
    if not path.exists():
        raise SearchProjectionError(f"input path does not exist: {path}")
    if not path.is_file():
        raise SearchProjectionError(f"input path is not a file: {path}")
    return path


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def row_columns(row: sqlite3.Row) -> set[str]:
    return set(row.keys())


def first_nonblank(row: sqlite3.Row, *candidates: str) -> str | None:
    columns = row_columns(row)
    for field_name in candidates:
        if field_name not in columns:
            continue
        value = row[field_name]
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (list, dict, bytes)):
            return str(value)
    return None


def first_nonblank_float(row: sqlite3.Row, *candidates: str) -> float | None:
    columns = row_columns(row)
    for field_name in candidates:
        if field_name not in columns:
            continue
        raw_value = row[field_name]
        try:
            score = float(raw_value)
        except (TypeError, ValueError):
            continue
        if score != score:
            continue
        return score
    return None


def read_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("PRAGMA user_version").fetchone()
    return None if row is None else int(row[0])


def database_fingerprint(payload: dict[str, Any]) -> str:
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def has_projection_index_marker(conn: sqlite3.Connection) -> bool:
    if not table_exists(conn, "projection_metadata"):
        return False
    if not table_exists(conn, "search_projection"):
        return False
    if not table_exists(conn, "search_projection_fts"):
        return False
    row = conn.execute("SELECT 1 FROM projection_metadata LIMIT 1").fetchone()
    return row is not None


def ensure_safe_index_target(source_db_path: Path, index_path: Path) -> None:
    source_resolved = source_db_path.resolve()
    index_resolved = index_path.resolve()
    if source_resolved == index_resolved:
        raise SearchProjectionError(
            f"index output path must differ from source database path: {index_resolved}"
        )
    if not index_resolved.exists():
        return
    if not index_resolved.is_file():
        raise SearchProjectionError(f"index output path is not a file: {index_resolved}")
    try:
        conn = connect_read_only(index_resolved)
        try:
            if not has_projection_index_marker(conn):
                raise SearchProjectionError(
                    f"refusing to overwrite existing SQLite file without projection marker: {index_resolved}"
                )
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SearchProjectionError(
            f"refusing to overwrite existing SQLite file without projection marker: {index_resolved}"
        ) from exc


def validate_projection_index_file(index_path: Path, payload: dict[str, Any]) -> None:
    try:
        conn = connect_read_only(index_path)
        try:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity_row is None or str(integrity_row[0]).strip().lower() != "ok":
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: integrity check failed"
                )
            if not has_projection_index_marker(conn):
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: missing marker"
                )
            metadata = conn.execute(
                """
                SELECT projection_schema_version, profile, source_database_fingerprint, projection_records_digest
                FROM projection_metadata
                LIMIT 1
                """
            ).fetchone()
            if metadata is None:
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: missing metadata"
                )
            if str(metadata["projection_schema_version"]) != str(payload["schema_version"]):
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: schema version mismatch"
                )
            if str(metadata["profile"]) != str(payload["profile"]):
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: profile mismatch"
                )
            if str(metadata["source_database_fingerprint"]) != str(
                payload["source"]["database_fingerprint"]
            ):
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: source fingerprint mismatch"
                )
            expected_records_digest = projection_records_digest(payload["records"])
            if str(metadata["projection_records_digest"]) != expected_records_digest:
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: projection records digest mismatch"
                )
            indexed_records = conn.execute(
                """
                SELECT projection_id, object_ref, object_type, object_pk, title, subtitle,
                       review_state, publication_state, confidence_score, authority_level,
                       public_blocker, lineage_state, visible_profiles_json, suppressed_fields_json,
                       indexed_fields_json
                FROM search_projection
                ORDER BY object_type ASC, object_pk ASC, projection_id ASC
                """
            ).fetchall()
            actual_records = [
                {
                    "projection_id": row["projection_id"],
                    "object_ref": row["object_ref"],
                    "object_type": row["object_type"],
                    "object_pk": int(row["object_pk"]),
                    "title": row["title"],
                    "subtitle": row["subtitle"],
                    "review_state": row["review_state"],
                    "publication_state": row["publication_state"],
                    "confidence_score": row["confidence_score"],
                    "authority_level": row["authority_level"],
                    "public_blocker": row["public_blocker"],
                    "lineage_state": row["lineage_state"],
                    "visible_profiles": json.loads(row["visible_profiles_json"]),
                    "suppressed_fields": json.loads(row["suppressed_fields_json"]),
                    "indexed_fields": json.loads(row["indexed_fields_json"]),
                }
                for row in indexed_records
            ]
            if projection_records_digest(actual_records) != expected_records_digest:
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: projection records digest mismatch"
                )
            projection_count = conn.execute("SELECT COUNT(*) FROM search_projection").fetchone()
            fts_count = conn.execute("SELECT COUNT(*) FROM search_projection_fts").fetchone()
            expected_count = int(payload["counts"]["indexed_rows"])
            if projection_count is None or int(projection_count[0]) != expected_count:
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: search_projection row count mismatch"
                )
            if fts_count is None or int(fts_count[0]) != expected_count:
                raise SearchProjectionError(
                    f"projection index validation failed for {index_path}: search_projection_fts row count mismatch"
                )
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SearchProjectionError(f"projection index validation failed for {index_path}") from exc


def projection_records_digest(records: list[dict[str, Any]]) -> str:
    canonical_records = [
        {
            "authority_level": record["authority_level"],
            "confidence_score": record["confidence_score"],
            "indexed_fields": record["indexed_fields"],
            "lineage_state": record["lineage_state"],
            "object_pk": record["object_pk"],
            "object_ref": record["object_ref"],
            "object_type": record["object_type"],
            "projection_id": record["projection_id"],
            "public_blocker": record["public_blocker"],
            "publication_state": record["publication_state"],
            "review_state": record["review_state"],
            "subtitle": record["subtitle"],
            "suppressed_fields": record["suppressed_fields"],
            "title": record["title"],
            "visible_profiles": record["visible_profiles"],
        }
        for record in records
    ]
    canonical_json = json.dumps(
        canonical_records,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_visible_profiles(publication_state: str, *, public_blocker: str | None, lineage_state: str) -> list[str]:
    profiles = ["local"]
    if public_blocker or lineage_state == "superseded":
        return profiles
    if publication_state == "public_release_allowed":
        profiles.extend(["public_preview", "public_release"])
    elif publication_state in {"public_preview", "public_release_candidate"}:
        profiles.append("public_preview")
    return profiles


def looks_like_private_path(value: str) -> bool:
    return value.startswith("/") or value.startswith("~") or value.startswith("file://") or (len(value) > 2 and value[1:3] == ":\\")


def filter_fields(target: SearchTarget, row: sqlite3.Row, *, profile: str) -> tuple[list[dict[str, str]], list[str]]:
    present_fields: list[dict[str, str]] = []
    suppressed_fields: list[str] = []
    for field_spec in target.field_specs:
        value = first_nonblank(row, *field_spec.candidates)
        if value is None:
            continue
        if is_public_profile(profile) and field_spec.display_policy == "local_only":
            suppressed_fields.append(field_spec.field_name)
            continue
        present_fields.append(
            {
                "field": field_spec.field_name,
                "text": value,
                "display_policy": field_spec.display_policy,
            }
        )
    return present_fields, suppressed_fields


def choose_title(target: SearchTarget, fields: list[dict[str, str]], object_pk: int) -> tuple[str, str | None]:
    title_candidates = {field_spec.field_name for field_spec in target.field_specs if field_spec.title_hint}
    subtitle_candidates = {field_spec.field_name for field_spec in target.field_specs if field_spec.subtitle_hint}
    title: str | None = None
    for field in fields:
        if field["field"] in title_candidates and field["text"].strip():
            title = field["text"].strip()
            break
    if title is None:
        title = f"{target.object_type.replace('_', ' ').title()} {object_pk}"

    subtitle: str | None = None
    for field in fields:
        if field["field"] in subtitle_candidates and field["text"].strip() and field["text"].strip() != title:
            subtitle = field["text"].strip()
            break
    return title, subtitle


def load_correction_resolution(raw_path: str | None) -> tuple[set[str], set[str], bool]:
    if raw_path is None:
        return set(), set(), False
    ledger_path = resolve_existing_file(raw_path)
    report, exit_code = validate_correction_ledger(ledger_path)
    if exit_code != EXIT_LEDGER_PASS:
        message = "; ".join(error["message"] for error in report["errors"]) or "correction ledger validation failed"
        raise SearchProjectionError(message)
    resolution = report.get("resolution", {})
    current_refs = set(resolution.get("current_object_refs", []))
    superseded_refs = set(resolution.get("superseded_object_refs", []))
    return current_refs, superseded_refs, True


def projection_record(
    target: SearchTarget,
    row: sqlite3.Row,
    *,
    profile: str,
    superseded_refs: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    columns = row_columns(row)
    if target.pk_column not in columns:
        return None, "missing_primary_key"
    object_pk = int(row[target.pk_column])
    object_ref = f"{target.object_type}:{object_pk}"
    review_state = first_nonblank(row, target.review_state_column) or ""
    if is_public_profile(profile) and not is_searchable_review_state(review_state):
        return None, "review_state_not_searchable"

    publication_state = normalize_publication_state(row["publication_state"] if "publication_state" in columns else None)
    authority_level = first_nonblank(row, "authority_level", "authority_tier", "authority_status")
    confidence_score = first_nonblank_float(row, "confidence_score", "confidence")
    public_blocker = first_nonblank(row, "public_blocker")
    if "public_blocked" in columns and public_blocker is None and row["public_blocked"]:
        public_blocker = "blocked"

    lineage_state = "superseded" if object_ref in superseded_refs else "current"
    if is_public_profile(profile):
        if lineage_state == "superseded":
            return None, "superseded_in_public_profile"
        if public_blocker:
            return None, "public_blocker"
        if publication_state not in PUBLIC_SEARCHABLE_PUBLICATION_STATES:
            return None, "publication_state_not_public"

    indexed_fields, suppressed_fields = filter_fields(target, row, profile=profile)
    if not indexed_fields:
        return None, "no_indexable_fields"

    title, subtitle = choose_title(target, indexed_fields, object_pk)
    visible_profiles = build_visible_profiles(publication_state, public_blocker=public_blocker, lineage_state=lineage_state)
    return (
        {
            "projection_id": f"{profile}:{object_ref}",
            "object_ref": object_ref,
            "object_type": target.object_type,
            "object_pk": object_pk,
            "title": title,
            "subtitle": subtitle,
            "review_state": review_state,
            "publication_state": publication_state,
            "authority_level": authority_level,
            "confidence_score": confidence_score,
            "public_blocker": public_blocker,
            "lineage_state": lineage_state,
            "visible_profiles": visible_profiles,
            "suppressed_fields": suppressed_fields,
            "indexed_fields": indexed_fields,
        },
        None,
    )


def build_projection_payload(args: argparse.Namespace) -> dict[str, Any]:
    db_path = resolve_existing_file(args.db)
    _, superseded_refs, ledger_applied = load_correction_resolution(args.correction_ledger)
    conn = connect_read_only(db_path)
    try:
        candidate_records = 0
        projected_records: list[dict[str, Any]] = []
        excluded_records: list[dict[str, str]] = []
        for target in TARGETS:
            if not SQL_IDENTIFIER_RE.fullmatch(target.table):
                raise RuntimeError(f"invalid projection target table: {target.table}")
            if not SQL_IDENTIFIER_RE.fullmatch(target.pk_column):
                raise RuntimeError(f"invalid projection target primary key column: {target.pk_column}")
            if not table_exists(conn, target.table):
                continue
            rows = conn.execute(f"SELECT * FROM {target.table} ORDER BY {target.pk_column}").fetchall()
            for row in rows:
                candidate_records += 1
                record, excluded_reason = projection_record(
                    target,
                    row,
                    profile=args.profile,
                    superseded_refs=superseded_refs,
                )
                object_ref = f"{target.object_type}:{row[target.pk_column]}"
                if record is None:
                    excluded_records.append({"object_ref": object_ref, "reason": excluded_reason or "excluded"})
                    continue
                projected_records.append(record)
        projected_records.sort(key=lambda item: (item["object_type"], item["object_pk"]))
        schema_version = read_schema_version(conn)
    finally:
        conn.close()

    logical_fingerprint_source = {
        "candidate_records": candidate_records,
        "excluded_records": excluded_records,
        "profile": args.profile,
        "projected_records": projected_records,
        "schema_version": schema_version,
        "source_database_name": db_path.name,
        "source_ledger_applied": ledger_applied,
    }

    private_paths_exposed = any(
        looks_like_private_path(field["text"])
        for record in projected_records
        for field in record["indexed_fields"]
    )
    blocked_records_included = any(
        record["public_blocker"] is not None or record["publication_state"] in {"blocked", "local_only", "private_working"}
        for record in projected_records
    )
    superseded_records_included = any(record["lineage_state"] == "superseded" for record in projected_records)

    generated_at = args.generated_at or now_rfc3339()
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": {
            "database_name": db_path.name,
            "database_fingerprint": database_fingerprint(logical_fingerprint_source),
            "schema_version": schema_version,
            "correction_ledger_applied": ledger_applied,
        },
        "profile": args.profile,
        "policy": {
            "raw_payload_indexed": False,
            "full_text_indexed": False,
            "private_paths_exposed": private_paths_exposed,
            "superseded_records_included": superseded_records_included,
            "blocked_records_included": blocked_records_included,
        },
        "counts": {
            "candidate_records": candidate_records,
            "projected_records": len(projected_records),
            "excluded_records": len(excluded_records),
            "indexed_rows": len(projected_records),
        },
        "excluded_records": excluded_records,
        "records": projected_records,
        "warnings": [],
        "errors": [],
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"profile={payload['profile']}",
        f"database_name={payload['source']['database_name']}",
        f"projected_records={payload['counts']['projected_records']}",
        f"excluded_records={payload['counts']['excluded_records']}",
        f"indexed_rows={payload['counts']['indexed_rows']}",
        f"private_paths_exposed={str(payload['policy']['private_paths_exposed']).lower()}",
        f"blocked_records_included={str(payload['policy']['blocked_records_included']).lower()}",
        f"superseded_records_included={str(payload['policy']['superseded_records_included']).lower()}",
        f"writer_surface={SCRIPT_PATH}",
    ]
    for index, record in enumerate(payload["records"]):
        lines.append(f"record[{index}].object_ref={record['object_ref']}")
        lines.append(f"record[{index}].title={record['title']}")
        lines.append(f"record[{index}].publication_state={record['publication_state']}")
        lines.append(f"record[{index}].lineage_state={record['lineage_state']}")
    return "\n".join(lines) + "\n"


def write_index(index_path: Path, payload: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    conn: sqlite3.Connection | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            dir=index_path.parent,
            prefix=f".{index_path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
        conn = sqlite3.connect(temp_path)
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE projection_metadata (
              projection_schema_version TEXT NOT NULL,
              source_database_name TEXT NOT NULL,
              source_database_fingerprint TEXT NOT NULL,
              source_schema_version TEXT,
              profile TEXT NOT NULL,
              generated_at TEXT NOT NULL,
              projection_records_digest TEXT NOT NULL,
              raw_payload_indexed INTEGER NOT NULL,
              full_text_indexed INTEGER NOT NULL,
              private_paths_exposed INTEGER NOT NULL
            );
            CREATE TABLE search_projection (
              projection_id TEXT PRIMARY KEY,
              object_ref TEXT NOT NULL,
              object_type TEXT NOT NULL,
              object_pk INTEGER NOT NULL,
              title TEXT NOT NULL,
              subtitle TEXT,
              review_state TEXT NOT NULL,
              publication_state TEXT NOT NULL,
              confidence_score REAL,
              authority_level TEXT,
              public_blocker TEXT,
              lineage_state TEXT NOT NULL,
              profile TEXT NOT NULL,
              visible_profiles_json TEXT NOT NULL,
              suppressed_fields_json TEXT NOT NULL,
              indexed_fields_json TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE search_projection_fts USING fts5(
              projection_id UNINDEXED,
              object_ref,
              object_type,
              title,
              subtitle,
              indexed_text
            );
            """
        )
        conn.execute(
            """
            INSERT INTO projection_metadata (
              projection_schema_version,
              source_database_name,
              source_database_fingerprint,
              source_schema_version,
              profile,
              generated_at,
              projection_records_digest,
              raw_payload_indexed,
              full_text_indexed,
              private_paths_exposed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["schema_version"],
                payload["source"]["database_name"],
                payload["source"]["database_fingerprint"],
                None if payload["source"]["schema_version"] is None else str(payload["source"]["schema_version"]),
                payload["profile"],
                payload["generated_at"],
                projection_records_digest(payload["records"]),
                int(bool(payload["policy"]["raw_payload_indexed"])),
                int(bool(payload["policy"]["full_text_indexed"])),
                int(bool(payload["policy"]["private_paths_exposed"])),
            ),
        )
        for record in payload["records"]:
            indexed_text = "\n".join(field["text"] for field in record["indexed_fields"])
            conn.execute(
                """
                INSERT INTO search_projection (
                  projection_id, object_ref, object_type, object_pk, title, subtitle,
                  review_state, publication_state, confidence_score, authority_level, public_blocker,
                  lineage_state, profile, visible_profiles_json, suppressed_fields_json, indexed_fields_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["projection_id"],
                    record["object_ref"],
                    record["object_type"],
                    record["object_pk"],
                    record["title"],
                    record["subtitle"],
                    record["review_state"],
                    record["publication_state"],
                    record["confidence_score"],
                    record["authority_level"],
                    record["public_blocker"],
                    record["lineage_state"],
                    payload["profile"],
                    json.dumps(record["visible_profiles"], sort_keys=True),
                    json.dumps(record["suppressed_fields"], sort_keys=True),
                    json.dumps(record["indexed_fields"], sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO search_projection_fts (
                  projection_id, object_ref, object_type, title, subtitle, indexed_text
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["projection_id"],
                    record["object_ref"],
                    record["object_type"],
                    record["title"],
                    record["subtitle"] or "",
                    indexed_text,
                ),
            )
        conn.commit()
        conn.close()
        conn = None
        validate_projection_index_file(temp_path, payload)
        temp_path.replace(index_path)
        temp_path = None
    finally:
        if conn is not None:
            conn.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    try:
        source_db_path = resolve_existing_file(args.db)
        index_path = resolve_path(args.index_db)
        ensure_safe_index_target(source_db_path, index_path)
        payload = build_projection_payload(args)
        if is_public_profile(args.profile):
            validation_errors = validate_local_search_projection_payload(payload)
            if validation_errors:
                summary = "; ".join(
                    f"{error['code']} {error.get('path') or error['message']}"
                    for error in validation_errors[:5]
                )
                raise SearchProjectionError(f"public search leak validation failed: {summary}")
        write_index(index_path, payload)
        if args.output_json:
            atomic_write_json(resolve_path(args.output_json), payload)
    except (SearchProjectionError, sqlite3.DatabaseError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
