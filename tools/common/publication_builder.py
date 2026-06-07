"""Shared helpers for canonical-store publication artifact builders."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.common.atomic_write import atomic_write_json
from tools.common.canonical_graph_model_contract import DOCUMENTED_EXPECTED_SQLITE_TABLES
from tools.common.local_search_contract import PUBLIC_SEARCHABLE_PUBLICATION_STATES, is_searchable_review_state, normalize_publication_state
from tools.scripts.build_local_search_projection import build_projection_payload, write_index
from tools.scripts.query_local_search import build_results_payload
from tools.validators.validate_knowledge_tree_export import EXIT_PASS as EXIT_EXPORT_PASS
from tools.validators.validate_knowledge_tree_export import validate_knowledge_tree_export
from tools.validators.validate_local_search_projection import validate_local_search_projection_payload
from tools.validators.validate_local_search_results import validate_local_search_results_payload
from tools.validators.validate_public_knowledge_tree_presentation import EXIT_PASS as EXIT_PRESENTATION_PASS
from tools.validators.validate_public_knowledge_tree_presentation import validate_public_knowledge_tree_presentation


EXPORT_SCHEMA_VERSION = "knowledge-tree-export.v1"
PRESENTATION_SCHEMA_VERSION = "public-presentation.v1"
PRESENTATION_CONTRACT_DOC = "docs/project/PUBLIC_KNOWLEDGE_TREE_PRESENTATION_CONTRACT.md"
DEFAULT_EXPORT_PROFILE = "public_release"
DEFAULT_PAGE_PUBLICATION_STATE = "public_safe"
DEFAULT_PAGE_REVIEW_POSTURE = "reviewed"
CANONICAL_SOURCE_ID = "canonical_store"
SEARCH_RESULTS_QUERY_FALLBACK = "search"
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+", re.ASCII)

PAGE_FAMILIES: tuple[str, ...] = (
    "home",
    "facet",
    "entity",
    "source",
    "collection",
    "timeline",
    "validation",
    "search_results",
)

PAGE_ID_BY_FAMILY = {
    "home": "home",
    "facet": "facet_overview",
    "entity": "entity_index",
    "source": "source_index",
    "collection": "collection_index",
    "timeline": "timeline_index",
    "validation": "validation_status",
    "search_results": "search_results",
}

PAGE_ROUTE_BY_FAMILY = {
    "home": "index.html",
    "facet": "facets/index.html",
    "entity": "entities/index.html",
    "source": "sources/index.html",
    "collection": "collections/index.html",
    "timeline": "timeline/index.html",
    "validation": "validation/index.html",
    "search_results": "search/results.html",
}

PAGE_TITLE_BY_FAMILY = {
    "home": "Knowledge Tree",
    "facet": "Facets",
    "entity": "Entities",
    "source": "Sources",
    "collection": "Collections",
    "timeline": "Timeline",
    "validation": "Validation",
    "search_results": "Search Results",
}

RELATED_FAMILIES_BY_FAMILY = {
    "home": ("facet", "entity", "source", "collection", "timeline", "validation", "search_results"),
    "facet": ("home", "collection", "entity"),
    "entity": ("home", "source", "search_results"),
    "source": ("home", "collection", "validation"),
    "collection": ("home", "facet", "timeline"),
    "timeline": ("home", "validation"),
    "validation": ("home", "search_results"),
    "search_results": ("home", "entity", "source"),
}

NEVER_PUBLISH = [
    "private local payload paths",
    "raw prompt output",
    "runtime logs",
    "private operator notes",
    "unreviewed source text",
    "restricted files",
    "credentials",
    "direct database snapshots",
]

REDACTION_GATE_REFS = [
    "public_private_export_boundary",
    "knowledge_tree_export_validator",
    "review_gate",
]

REQUIRED_PUBLICATION_TABLES = DOCUMENTED_EXPECTED_SQLITE_TABLES | {"schema_version", "source_access", "topic_extension"}


class PublicationBuildError(RuntimeError):
    """Raised when a publication producer cannot complete safely."""


@dataclass(frozen=True)
class SearchArtifacts:
    projection_payload: dict[str, Any]
    results_payload: dict[str, Any]
    index_db_path: Path | None
    projection_json_path: Path | None
    results_json_path: Path | None
    query_text: str


@dataclass(frozen=True)
class KnowledgeTreeExportBuildResult:
    payload: dict[str, Any]
    search_artifacts: SearchArtifacts
    validation_state: str


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_existing_file(raw_path: str | Path, *, label: str) -> Path:
    path = resolve_path(raw_path)
    if not path.exists():
        raise PublicationBuildError(f"{label} path does not exist: {path}")
    if not path.is_file():
        raise PublicationBuildError(f"{label} path is not a file: {path}")
    return path


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _normalize_fingerprint_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (str, int, bool)):
        return value
    if value is None:
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise PublicationBuildError("non-finite numeric value cannot be included in publication fingerprint")
        return value
    return str(value)


def _row_to_fingerprint_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _normalize_fingerprint_value(row[key]) for key in row.keys()}


def actual_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def table_fingerprint(conn: sqlite3.Connection, table: str) -> str:
    table_schema_rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not table_schema_rows:
        raise PublicationBuildError(f"unknown table for fingerprinting: {table}")
    columns = [str(item["name"]) for item in table_schema_rows]
    primary_key_columns = [
        column for column in columns if any(item["name"] == column and item["pk"] for item in table_schema_rows)
    ]
    order_by_columns = primary_key_columns or columns
    row_query = f"SELECT * FROM {table}"
    if order_by_columns:
        row_query += " ORDER BY " + ", ".join(f'"{column}"' for column in order_by_columns)
    rows = conn.execute(row_query).fetchall()
    payload = {
        "table": table,
        "columns": columns,
        "rows": [_row_to_fingerprint_payload(row) for row in rows],
    }
    return database_fingerprint(payload)


def query_fingerprint(label: str, rows: list[dict[str, Any]]) -> str:
    return database_fingerprint(
        {
            "label": label,
            "rows": rows,
        }
    )


def database_fingerprint(payload: dict[str, Any]) -> str:
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationBuildError(f"failed to load {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise PublicationBuildError(f"{label} must be a JSON object: {path}")
    return payload


def nonblank(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def slugify_identifier(value: str | None, *, fallback: str) -> str:
    if not value:
        return fallback
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9._-]+", "_", lowered)
    lowered = lowered.strip("._-")
    return lowered or fallback


def display_name_from_workspace_id(workspace_id: str, *, fallback: str) -> str:
    source = workspace_id if workspace_id else fallback
    words = [token for token in re.split(r"[_\-.]+", source) if token]
    if not words:
        return fallback
    return " ".join(word.capitalize() for word in words)


def ensure_required_publication_tables(conn: sqlite3.Connection) -> set[str]:
    tables = actual_tables(conn)
    missing = sorted(REQUIRED_PUBLICATION_TABLES - tables)
    if missing:
        raise PublicationBuildError(
            "canonical store missing required publication tables: " + ", ".join(missing)
        )
    return tables


def row_is_public(row: sqlite3.Row, *, review_field: str = "review_state", publication_field: str = "publication_state", blocker_field: str = "public_blocker") -> bool:
    review_state = nonblank(row[review_field]) if review_field in row.keys() else None
    publication_state = normalize_publication_state(row[publication_field]) if publication_field in row.keys() else "local_only"
    public_blocker = nonblank(row[blocker_field]) if blocker_field in row.keys() else None
    return (
        is_searchable_review_state(review_state)
        and public_blocker is None
        and publication_state in PUBLIC_SEARCHABLE_PUBLICATION_STATES
    )


def first_workspace_id(conn: sqlite3.Connection) -> str | None:
    candidate_queries = (
        ("SELECT workspace_id FROM authority_record WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) <> '' ORDER BY authority_record_id LIMIT 1", ()),
        ("SELECT workspace_id FROM work WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) <> '' ORDER BY work_id LIMIT 1", ()),
        ("SELECT workspace_id FROM source_access WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) <> '' ORDER BY source_access_id LIMIT 1", ()),
        ("SELECT workspace_id FROM topic_extension WHERE workspace_id IS NOT NULL AND TRIM(workspace_id) <> '' ORDER BY topic_extension_id LIMIT 1", ()),
    )
    for query, params in candidate_queries:
        row = conn.execute(query, params).fetchone()
        if row is None:
            continue
        value = nonblank(row[0])
        if value:
            return value
    return None


def schema_version_from_store(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("PRAGMA user_version").fetchone()
    return None if row is None else as_int(row[0], 0)


def validate_export_file(path: Path) -> None:
    report, exit_code = validate_knowledge_tree_export(path)
    if exit_code != EXIT_EXPORT_PASS:
        message = "; ".join(error["message"] for error in report["errors"]) or "knowledge tree export validation failed"
        raise PublicationBuildError(message)


def validate_presentation_file(path: Path) -> None:
    report, exit_code = validate_public_knowledge_tree_presentation(path)
    if exit_code != EXIT_PRESENTATION_PASS:
        message = "; ".join(error["message"] for error in report["errors"]) or "public presentation validation failed"
        raise PublicationBuildError(message)


def choose_search_query(projection_payload: dict[str, Any]) -> str:
    records = projection_payload.get("records", [])
    if not isinstance(records, list):
        return SEARCH_RESULTS_QUERY_FALLBACK
    for record in records:
        if not isinstance(record, dict):
            continue
        for candidate in (record.get("title"), record.get("subtitle")):
            if not isinstance(candidate, str):
                continue
            for token in TOKEN_PATTERN.findall(candidate):
                if token.strip():
                    return token
    return SEARCH_RESULTS_QUERY_FALLBACK


def build_search_artifacts(
    db_path: Path,
    *,
    generated_at: str,
    search_artifacts_dir: Path | None = None,
) -> SearchArtifacts:
    managed_tmp = search_artifacts_dir is None
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if managed_tmp:
        temp_dir = tempfile.TemporaryDirectory(prefix=".publication-search.")
        root = Path(temp_dir.name)
    else:
        root = search_artifacts_dir
        assert root is not None
        root.mkdir(parents=True, exist_ok=True)

    index_db_path = root / "local_search.sqlite"
    projection_json_path = root / "local_search_projection.json"
    results_json_path = root / "local_search_results.json"

    try:
        projection_args = argparse.Namespace(
            db=str(db_path),
            profile="public_release",
            index_db=str(index_db_path),
            output_json=str(projection_json_path) if not managed_tmp else None,
            correction_ledger=None,
            generated_at=generated_at,
            format="json",
        )
        projection_payload = build_projection_payload(projection_args)
        projection_errors = validate_local_search_projection_payload(projection_payload)
        if projection_errors:
            message = "; ".join(error["message"] for error in projection_errors) or "local search projection validation failed"
            raise PublicationBuildError(message)
        if not managed_tmp:
            atomic_write_json(projection_json_path, projection_payload)
        write_index(index_db_path, projection_payload)

        query_text = choose_search_query(projection_payload)
        results_args = argparse.Namespace(
            index_db=str(index_db_path),
            query=query_text,
            scope="all",
            limit=10,
            offset=0,
            format="json",
            output_json=str(results_json_path) if not managed_tmp else None,
            generated_at=generated_at,
        )
        results_payload = build_results_payload(results_args)
        results_errors = validate_local_search_results_payload(results_payload)
        if results_errors:
            message = "; ".join(error["message"] for error in results_errors) or "local search results validation failed"
            raise PublicationBuildError(message)
        if not managed_tmp:
            atomic_write_json(results_json_path, results_payload)

        return SearchArtifacts(
            projection_payload=projection_payload,
            results_payload=results_payload,
            index_db_path=None if managed_tmp else index_db_path,
            projection_json_path=None if managed_tmp else projection_json_path,
            results_json_path=None if managed_tmp else results_json_path,
            query_text=query_text,
        )
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        raise PublicationBuildError(f"failed to build public search artifacts: {exc}") from exc
    finally:
        if managed_tmp and temp_dir is not None:
            temp_dir.cleanup()


def read_publication_snapshot(
    db_path: Path,
    *,
    generated_at: str,
    search_artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    db_path = resolve_existing_file(db_path, label="database")
    try:
        conn = connect_read_only(db_path)
    except sqlite3.DatabaseError as exc:
        raise PublicationBuildError(f"database could not be opened read-only as SQLite: {db_path}") from exc
    try:
        required_tables = ensure_required_publication_tables(conn)
        workspace_id = first_workspace_id(conn) or slugify_identifier(db_path.stem, fallback="canonical_workspace")
        schema_version = schema_version_from_store(conn)
        publication_table_fingerprints = {
            table_name: table_fingerprint(conn, table_name) for table_name in sorted(required_tables)
        }

        authority_rows = conn.execute(
            """
            SELECT *
            FROM authority_record
            WHERE merged_into_authority_record_id IS NULL
            ORDER BY COALESCE(sort_label, preferred_label, authority_key_v1), authority_record_id
            """
        ).fetchall()
        public_authorities = [
            {
                "authority_record_id": as_int(row["authority_record_id"]),
                "preferred_label": nonblank(row["preferred_label"]) or f"Authority {row['authority_record_id']}",
                "authority_type": nonblank(row["authority_type"]) or "entity",
                "confidence_score": row["confidence_score"],
            }
            for row in authority_rows
            if row_is_public(row)
        ]
        public_authority_ids = {item["authority_record_id"] for item in public_authorities}

        work_rows = conn.execute(
            """
            SELECT *
            FROM work
            ORDER BY COALESCE(title, work_key_v1), work_id
            """
        ).fetchall()
        public_works = [
            {
                "work_id": as_int(row["work_id"]),
                "title": nonblank(row["title"]) or f"Work {row['work_id']}",
                "work_type": nonblank(row["work_type"]) or "work",
                "first_seen_at": nonblank(row["first_seen_at"]),
                "last_seen_at": nonblank(row["last_seen_at"]),
            }
            for row in work_rows
            if row_is_public(row)
        ]
        public_work_ids = {item["work_id"] for item in public_works}

        source_rows = conn.execute(
            """
            SELECT
              sa.*,
              w.title AS work_title
            FROM source_access AS sa
            LEFT JOIN work AS w ON w.work_id = sa.work_id
            ORDER BY COALESCE(sa.canonical_url, sa.citation_hint, sa.source_access_id), sa.source_access_id
            """
        ).fetchall()
        public_sources = []
        for row in source_rows:
            if not row_is_public(row):
                continue
            public_sources.append(
                {
                    "source_access_id": as_int(row["source_access_id"]),
                    "canonical_url": nonblank(row["canonical_url"]),
                    "citation_hint": nonblank(row["citation_hint"]) or nonblank(row["work_title"]) or f"Source {row['source_access_id']}",
                    "access_class": nonblank(row["access_class"]) or "source",
                    "first_seen_at": nonblank(row["first_seen_at"]),
                    "last_seen_at": nonblank(row["last_seen_at"]),
                }
            )

        claim_rows = conn.execute(
            """
            SELECT *
            FROM source_claim
            ORDER BY COALESCE(public_summary, claim_type, source_claim_id), source_claim_id
            """
        ).fetchall()
        public_claims = []
        for row in claim_rows:
            public_summary = nonblank(row["public_summary"])
            if not row_is_public(row) or public_summary is None:
                continue
            public_claims.append(
                {
                    "source_claim_id": as_int(row["source_claim_id"]),
                    "public_summary": public_summary,
                    "claim_type": nonblank(row["claim_type"]) or "claim",
                    "about_object_ref": nonblank(row["about_object_ref"]),
                }
            )

        relationship_rows = conn.execute(
            """
            SELECT *
            FROM source_relationship
            ORDER BY predicate, COALESCE(target_label, to_object_ref, from_object_ref), source_relationship_id
            """
        ).fetchall()
        public_relationships = [
            {
                "source_relationship_id": as_int(row["source_relationship_id"]),
                "predicate": nonblank(row["predicate"]) or "related_to",
                "target_label": nonblank(row["target_label"]) or nonblank(row["to_object_ref"]) or "related record",
            }
            for row in relationship_rows
            if row_is_public(row)
        ]

        topic_rows = conn.execute(
            """
            SELECT *
            FROM topic_extension
            ORDER BY extension_type, COALESCE(summary_short, topic_id), topic_extension_id
            """
        ).fetchall()
        public_topics = []
        for row in topic_rows:
            summary_short = nonblank(row["summary_short"])
            if not row_is_public(row) or summary_short is None:
                continue
            public_topics.append(
                {
                    "topic_extension_id": as_int(row["topic_extension_id"]),
                    "topic_id": nonblank(row["topic_id"]) or f"topic-{row['topic_extension_id']}",
                    "extension_type": nonblank(row["extension_type"]) or "topic",
                    "summary_short": summary_short,
                    "created_at": nonblank(row["created_at"]),
                }
            )

        subject_rows = conn.execute(
            """
            SELECT *
            FROM work_subject
            ORDER BY COALESCE(subject_role, subject_object_ref, authority_record_id), work_subject_id
            """
        ).fetchall()
        public_work_subjects = []
        for row in subject_rows:
            if not is_searchable_review_state(nonblank(row["review_state"])):
                continue
            work_id = as_int(row["work_id"])
            authority_id = as_int(row["authority_record_id"], 0) if row["authority_record_id"] is not None else None
            if work_id not in public_work_ids and authority_id not in public_authority_ids:
                continue
            public_work_subjects.append(
                {
                    "work_subject_id": as_int(row["work_subject_id"]),
                    "work_id": work_id,
                    "authority_record_id": authority_id,
                    "subject_role": nonblank(row["subject_role"]) or "subject",
                    "subject_object_ref": nonblank(row["subject_object_ref"]) or "",
                }
            )

        detected_rows = conn.execute(
            """
            SELECT *
            FROM extraction_detected_entity
            ORDER BY COALESCE(normalized_label, entity_label), detected_entity_id
            """
        ).fetchall()
        public_detected_entities = []
        for row in detected_rows:
            authority_id = as_int(row["authority_record_id"], 0) if row["authority_record_id"] is not None else None
            if authority_id not in public_authority_ids:
                continue
            if not is_searchable_review_state(nonblank(row["review_state"])):
                continue
            public_detected_entities.append(
                {
                    "detected_entity_id": as_int(row["detected_entity_id"]),
                    "entity_label": nonblank(row["entity_label"]) or f"Detected entity {row['detected_entity_id']}",
                    "entity_type": nonblank(row["entity_type"]) or "entity",
                }
            )

        provenance_rows = conn.execute(
            """
            SELECT provenance_event_id, event_type, actor_label, event_timestamp
            FROM provenance_event
            ORDER BY event_timestamp, provenance_event_id
            """
        ).fetchall()
        public_provenance_events = [
            {
                "provenance_event_id": as_int(row["provenance_event_id"]),
                "event_type": nonblank(row["event_type"]) or "event",
                "actor_label": nonblank(row["actor_label"]) or "operator",
                "event_timestamp": nonblank(row["event_timestamp"]) or "",
            }
            for row in provenance_rows
            if nonblank(row["event_timestamp"])
        ]

        capture_rows = conn.execute(
            "SELECT capture_event_id FROM capture_event ORDER BY capture_event_id"
        ).fetchall()
        extraction_rows = conn.execute(
            "SELECT extraction_id, extraction_status FROM extraction_record ORDER BY extraction_id"
        ).fetchall()
        review_history_rows = conn.execute(
            "SELECT review_state_history_key_v1, new_state FROM review_state_history ORDER BY changed_at, review_state_history_key_v1"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise PublicationBuildError(f"database query failed while building publication snapshot: {db_path}") from exc
    finally:
        conn.close()

    search_artifacts = build_search_artifacts(
        db_path,
        generated_at=generated_at,
        search_artifacts_dir=search_artifacts_dir,
    )

    timeline_events: list[dict[str, str]] = []
    for work in public_works:
        if work["first_seen_at"]:
            timeline_events.append(
                {
                    "timestamp": work["first_seen_at"],
                    "text": f"{work['first_seen_at'][:10]}: work observed - {work['title']}",
                }
            )
        if work["last_seen_at"] and work["last_seen_at"] != work["first_seen_at"]:
            timeline_events.append(
                {
                    "timestamp": work["last_seen_at"],
                    "text": f"{work['last_seen_at'][:10]}: work refreshed - {work['title']}",
                }
            )
    for source in public_sources:
        if source["first_seen_at"]:
            timeline_events.append(
                {
                    "timestamp": source["first_seen_at"],
                    "text": f"{source['first_seen_at'][:10]}: source captured - {source['citation_hint']}",
                }
            )
    for topic in public_topics:
        if topic["created_at"]:
            timeline_events.append(
                {
                    "timestamp": topic["created_at"],
                    "text": f"{topic['created_at'][:10]}: topic extended - {topic['summary_short']}",
                }
            )
    timeline_events.sort(key=lambda item: (item["timestamp"], item["text"]))
    publication_query_fingerprints = {
        "public_authorities": query_fingerprint("public_authorities", public_authorities),
        "public_claims": query_fingerprint("public_claims", public_claims),
        "public_detected_entities": query_fingerprint(
            "public_detected_entities", public_detected_entities
        ),
        "public_provenance_events": query_fingerprint(
            "public_provenance_events", public_provenance_events
        ),
        "public_relationships": query_fingerprint("public_relationships", public_relationships),
        "public_sources": query_fingerprint("public_sources", public_sources),
        "public_topics": query_fingerprint("public_topics", public_topics),
        "public_work_subjects": query_fingerprint("public_work_subjects", public_work_subjects),
        "public_works": query_fingerprint("public_works", public_works),
        "timeline_events": query_fingerprint("timeline_events", timeline_events),
    }

    validation_summary = {
        "public_authorities": len(public_authorities),
        "public_works": len(public_works),
        "public_sources": len(public_sources),
        "public_claims": len(public_claims),
        "public_relationships": len(public_relationships),
        "public_topics": len(public_topics),
        "withheld_authorities": max(len(authority_rows) - len(public_authorities), 0),
        "withheld_works": max(len(work_rows) - len(public_works), 0),
        "withheld_sources": max(len(source_rows) - len(public_sources), 0),
        "withheld_claims": max(len(claim_rows) - len(public_claims), 0),
        "withheld_relationships": max(len(relationship_rows) - len(public_relationships), 0),
        "captures": len(capture_rows),
        "extractions": len(extraction_rows),
        "review_events": len(review_history_rows),
        "search_indexed": as_int(search_artifacts.projection_payload["counts"]["projected_records"]),
        "search_returned": as_int(search_artifacts.results_payload["counts"]["returned"]),
    }
    validation_summary["withheld_total"] = sum(
        validation_summary[key]
        for key in (
            "withheld_authorities",
            "withheld_works",
            "withheld_sources",
            "withheld_claims",
            "withheld_relationships",
        )
    )
    validation_summary["public_total"] = sum(
        validation_summary[key]
        for key in (
            "public_authorities",
            "public_works",
            "public_sources",
            "public_claims",
            "public_relationships",
            "public_topics",
        )
    )
    validation_summary["status"] = "warning" if validation_summary["withheld_total"] else "passing"

    fingerprint_source = {
        "display_name": display_name_from_workspace_id(workspace_id, fallback="Knowledge Tree"),
        "public_authorities": public_authorities,
        "public_claims": public_claims,
        "public_detected_entities": public_detected_entities,
        "public_provenance_events": public_provenance_events,
        "public_relationships": public_relationships,
        "public_sources": public_sources,
        "public_topics": public_topics,
        "public_work_subjects": public_work_subjects,
        "public_works": public_works,
        "schema_version": schema_version,
        "search_artifacts": {
            "projection_records_digest": search_artifacts.projection_payload["source"].get(
                "database_fingerprint"
            ),
            "results_counts": search_artifacts.results_payload["counts"],
        },
        "timeline_events": timeline_events,
        "validation_summary": validation_summary,
        "workspace_id": slugify_identifier(workspace_id, fallback="canonical_workspace"),
    }

    return {
        "db_path": db_path,
        "db_fingerprint": database_fingerprint(fingerprint_source),
        "db_storage_fingerprint": hash_file(db_path),
        "publication_table_fingerprints": publication_table_fingerprints,
        "schema_version": schema_version,
        "workspace_id": slugify_identifier(workspace_id, fallback="canonical_workspace"),
        "display_name": display_name_from_workspace_id(workspace_id, fallback="Knowledge Tree"),
        "public_authorities": public_authorities,
        "public_works": public_works,
        "public_sources": public_sources,
        "public_claims": public_claims,
        "public_relationships": public_relationships,
        "public_topics": public_topics,
        "public_work_subjects": public_work_subjects,
        "public_detected_entities": public_detected_entities,
        "public_provenance_events": public_provenance_events,
        "timeline_events": timeline_events,
        "validation_summary": validation_summary,
        "search_artifacts": search_artifacts,
        "publication_query_fingerprints": publication_query_fingerprints,
    }


def summary_cards(entries: list[tuple[str, Any]]) -> list[dict[str, str]]:
    return [{"label": label, "value": str(value)} for label, value in entries]


def build_section(
    heading: str,
    *,
    paragraphs: list[str] | None = None,
    bullet_items: list[str] | None = None,
    link_page_ids: list[str] | None = None,
) -> dict[str, Any]:
    section: dict[str, Any] = {"heading": heading}
    if paragraphs:
        section["paragraphs"] = paragraphs
    if bullet_items:
        section["bullet_items"] = bullet_items
    if link_page_ids:
        section["link_page_ids"] = link_page_ids
    return section


def page_reference(family: str) -> tuple[str, str]:
    return PAGE_ID_BY_FAMILY[family], PAGE_ROUTE_BY_FAMILY[family]


def build_home_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    related_ids = [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["home"]]
    reader_state = "ready" if snapshot["validation_summary"]["public_total"] else "sparse"
    empty_state = "" if reader_state == "ready" else "The canonical store is initialized, but no public-safe records are ready for publication yet."
    sections = [
        build_section(
            "Overview",
            paragraphs=[
                f"{snapshot['display_name']} is published from reviewed, public-safe canonical records.",
                "Private locators, raw payloads, and internal notes remain excluded from this export and its rendered pages.",
            ],
            link_page_ids=related_ids,
        )
    ]
    if reader_state != "ready":
        sections.append(
            build_section(
                "Sparse State",
                paragraphs=[empty_state],
                link_page_ids=[PAGE_ID_BY_FAMILY["validation"]],
            )
        )
    page = {
        "page_id": PAGE_ID_BY_FAMILY["home"],
        "page_family": "home",
        "route": PAGE_ROUTE_BY_FAMILY["home"],
        "title": PAGE_TITLE_BY_FAMILY["home"],
        "lede": f"Public summary for {snapshot['display_name']}.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": related_ids,
        "summary_cards": summary_cards(
            [
                ("Pages", len(PAGE_FAMILIES)),
                ("Public records", snapshot["validation_summary"]["public_total"]),
                ("Withheld records", snapshot["validation_summary"]["withheld_total"]),
            ]
        ),
        "sections": sections,
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Built from reviewed public-safe canonical rows and derived search projections.",
        "empty_state": empty_state,
    }
    return page, hint


def build_facet_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    public_topics = snapshot["public_topics"]
    public_subjects = snapshot["public_work_subjects"]
    items = [f"{row['summary_short']} ({row['extension_type']})" for row in public_topics[:5]]
    items.extend(
        f"{row['subject_role']} mapping for work {row['work_id']}"
        for row in public_subjects[: max(0, 5 - len(items))]
    )
    reader_state = "ready" if items else "empty"
    empty_state = "" if items else "No public facets are currently available."
    sections = [
        build_section(
            "Facet Overview",
            bullet_items=items if items else None,
            paragraphs=None if items else [empty_state],
            link_page_ids=[PAGE_ID_BY_FAMILY["collection"], PAGE_ID_BY_FAMILY["entity"]],
        )
    ]
    page = {
        "page_id": PAGE_ID_BY_FAMILY["facet"],
        "page_family": "facet",
        "route": PAGE_ROUTE_BY_FAMILY["facet"],
        "title": PAGE_TITLE_BY_FAMILY["facet"],
        "lede": "Facet overview and topical extensions.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["facet"]],
        "summary_cards": summary_cards(
            [
                ("Topics", len(public_topics)),
                ("Mappings", len(public_subjects)),
                ("Claims", len(snapshot["public_claims"])),
            ]
        ),
        "sections": sections,
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Facet summaries use reviewed topic extensions and subject mappings only.",
        "empty_state": empty_state,
    }
    return page, hint


def build_entity_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    authority_items = [
        f"{row['preferred_label']} ({row['authority_type']})"
        for row in snapshot["public_authorities"][:5]
    ]
    detected_items = [
        f"{row['entity_label']} ({row['entity_type']})"
        for row in snapshot["public_detected_entities"][: max(0, 5 - len(authority_items))]
    ]
    items = authority_items + detected_items
    reader_state = "ready" if items else "empty"
    empty_state = "" if items else "No public entities are currently available."
    sections = [
        build_section(
            "Entity Summary",
            bullet_items=items if items else None,
            paragraphs=None if items else [empty_state],
            link_page_ids=[PAGE_ID_BY_FAMILY["source"], PAGE_ID_BY_FAMILY["search_results"]],
        )
    ]
    page = {
        "page_id": PAGE_ID_BY_FAMILY["entity"],
        "page_family": "entity",
        "route": PAGE_ROUTE_BY_FAMILY["entity"],
        "title": PAGE_TITLE_BY_FAMILY["entity"],
        "lede": "Reviewed entities and resolved labels.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["entity"]],
        "summary_cards": summary_cards(
            [
                ("Authorities", len(snapshot["public_authorities"])),
                ("Detected mentions", len(snapshot["public_detected_entities"])),
                ("Relationships", len(snapshot["public_relationships"])),
            ]
        ),
        "sections": sections,
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Entity labels publish only from reviewed authority rows and reviewed linked detections.",
        "empty_state": empty_state,
    }
    return page, hint


def build_source_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source_items = []
    for row in snapshot["public_sources"][:5]:
        label = row["citation_hint"]
        if row["canonical_url"]:
            label = f"{label} - {row['canonical_url']}"
        source_items.append(label)
    claim_items = [row["public_summary"] for row in snapshot["public_claims"][: max(0, 5 - len(source_items))]]
    items = source_items + claim_items
    reader_state = "ready" if items else "empty"
    empty_state = "" if items else "No public sources are currently available."
    sections = [
        build_section(
            "Source Summary",
            bullet_items=items if items else None,
            paragraphs=None if items else [empty_state],
            link_page_ids=[PAGE_ID_BY_FAMILY["collection"], PAGE_ID_BY_FAMILY["validation"]],
        )
    ]
    page = {
        "page_id": PAGE_ID_BY_FAMILY["source"],
        "page_family": "source",
        "route": PAGE_ROUTE_BY_FAMILY["source"],
        "title": PAGE_TITLE_BY_FAMILY["source"],
        "lede": "Reviewed public sources and claim summaries.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["source"]],
        "summary_cards": summary_cards(
            [
                ("Sources", len(snapshot["public_sources"])),
                ("Claims", len(snapshot["public_claims"])),
                ("Captures", snapshot["validation_summary"]["captures"]),
            ]
        ),
        "sections": sections,
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Source pages publish canonical URLs and public summaries only; local locators stay private.",
        "empty_state": empty_state,
    }
    return page, hint


def build_collection_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    collection_rows = [row for row in snapshot["public_topics"] if row["extension_type"] == "collection"]
    items = [row["summary_short"] for row in collection_rows[:5]]
    if not items:
        items = [
            f"Work {row['work_id']} linked as {row['subject_role']}"
            for row in snapshot["public_work_subjects"][:5]
        ]
    reader_state = "ready" if items else "empty"
    empty_state = "" if items else "No public collections are currently available."
    sections = [
        build_section(
            "Collection Summary",
            bullet_items=items if items else None,
            paragraphs=None if items else [empty_state],
            link_page_ids=[PAGE_ID_BY_FAMILY["facet"], PAGE_ID_BY_FAMILY["timeline"]],
        )
    ]
    page = {
        "page_id": PAGE_ID_BY_FAMILY["collection"],
        "page_family": "collection",
        "route": PAGE_ROUTE_BY_FAMILY["collection"],
        "title": PAGE_TITLE_BY_FAMILY["collection"],
        "lede": "Curated collection and mapping overview.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["collection"]],
        "summary_cards": summary_cards(
            [
                ("Collections", len(collection_rows)),
                ("Works", len(snapshot["public_works"])),
                ("Mappings", len(snapshot["public_work_subjects"])),
            ]
        ),
        "sections": sections,
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Collections publish reviewed summaries and subject mappings only.",
        "empty_state": empty_state,
    }
    return page, hint


def build_timeline_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    events = [row["text"] for row in snapshot["timeline_events"][:8]]
    latest = snapshot["timeline_events"][-1]["timestamp"][:10] if snapshot["timeline_events"] else "none"
    reader_state = "ready" if events else "empty"
    empty_state = "" if events else "No public timeline events are currently available."
    sections = [
        build_section(
            "Timeline",
            bullet_items=events if events else None,
            paragraphs=None if events else [empty_state],
            link_page_ids=[PAGE_ID_BY_FAMILY["validation"]],
        )
    ]
    page = {
        "page_id": PAGE_ID_BY_FAMILY["timeline"],
        "page_family": "timeline",
        "route": PAGE_ROUTE_BY_FAMILY["timeline"],
        "title": PAGE_TITLE_BY_FAMILY["timeline"],
        "lede": "Chronology derived from reviewed public records.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["timeline"]],
        "summary_cards": summary_cards(
            [
                ("Events", len(snapshot["timeline_events"])),
                ("Latest", latest),
                ("Provenance events", len(snapshot["public_provenance_events"])),
            ]
        ),
        "sections": sections,
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Timeline entries are derived from public-safe timestamps and omit private event notes.",
        "empty_state": empty_state,
    }
    return page, hint


def build_validation_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = snapshot["validation_summary"]
    bullet_items = [
        f"Public entities: {validation['public_authorities']}",
        f"Public works: {validation['public_works']}",
        f"Public sources: {validation['public_sources']}",
        f"Withheld records: {validation['withheld_total']}",
        f"Indexed search rows: {validation['search_indexed']}",
    ]
    if validation["withheld_total"]:
        bullet_items.append("Some records remain withheld until review or public blockers are cleared.")
    else:
        bullet_items.append("All currently eligible records publish cleanly into the public projection.")
    page = {
        "page_id": PAGE_ID_BY_FAMILY["validation"],
        "page_family": "validation",
        "route": PAGE_ROUTE_BY_FAMILY["validation"],
        "title": PAGE_TITLE_BY_FAMILY["validation"],
        "lede": "Public release gates and review posture summary.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["validation"]],
        "summary_cards": summary_cards(
            [
                ("Status", validation["status"]),
                ("Public records", validation["public_total"]),
                ("Withheld records", validation["withheld_total"]),
            ]
        ),
        "sections": [
            build_section(
                "Validation Summary",
                bullet_items=bullet_items,
                link_page_ids=[PAGE_ID_BY_FAMILY["home"], PAGE_ID_BY_FAMILY["search_results"]],
            )
        ],
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": "ready",
        "validation_state": validation["status"],
        "source_transparency": "Validation summarizes public-safe counts and withholds private review details.",
        "empty_state": "",
    }
    return page, hint


def build_search_results_page(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    results_payload = snapshot["search_artifacts"].results_payload
    projection_payload = snapshot["search_artifacts"].projection_payload
    results = results_payload.get("results", [])
    items = []
    if isinstance(results, list):
        for row in results[:5]:
            if not isinstance(row, dict):
                continue
            title = nonblank(row.get("title"))
            object_type = nonblank(row.get("object_type")) or "result"
            if title:
                items.append(f"{title} ({object_type})")
    projected_records = as_int(projection_payload["counts"]["projected_records"])
    reader_state = "ready" if items else ("sparse" if projected_records else "empty")
    empty_state = ""
    if reader_state != "ready":
        empty_state = "No public search results are currently available."
    page = {
        "page_id": PAGE_ID_BY_FAMILY["search_results"],
        "page_family": "search_results",
        "route": PAGE_ROUTE_BY_FAMILY["search_results"],
        "title": PAGE_TITLE_BY_FAMILY["search_results"],
        "lede": "Public search projection overview.",
        "review_posture": DEFAULT_PAGE_REVIEW_POSTURE,
        "publication_state": DEFAULT_PAGE_PUBLICATION_STATE,
        "source_ids": [CANONICAL_SOURCE_ID],
        "related_page_ids": [PAGE_ID_BY_FAMILY[family] for family in RELATED_FAMILIES_BY_FAMILY["search_results"]],
        "summary_cards": summary_cards(
            [
                ("Indexed rows", projected_records),
                ("Returned results", as_int(results_payload["counts"]["returned"])),
                ("Query", snapshot["search_artifacts"].query_text),
            ]
        ),
        "sections": [
            build_section(
                "Search Results",
                bullet_items=items if items else None,
                paragraphs=None if items else [empty_state],
                link_page_ids=[PAGE_ID_BY_FAMILY["entity"], PAGE_ID_BY_FAMILY["source"]],
            )
        ],
    }
    hint = {
        "page_id": page["page_id"],
        "reader_state": reader_state,
        "validation_state": snapshot["validation_summary"]["status"],
        "source_transparency": "Search results reuse the public local-search projection and suppress private fields by profile.",
        "empty_state": empty_state,
    }
    return page, hint


PAGE_BUILDERS = {
    "home": build_home_page,
    "facet": build_facet_page,
    "entity": build_entity_page,
    "source": build_source_page,
    "collection": build_collection_page,
    "timeline": build_timeline_page,
    "validation": build_validation_page,
    "search_results": build_search_results_page,
}


def build_page_inventory_hints(pages: list[dict[str, Any]], page_hints: list[dict[str, Any]], *, validation_state: str) -> list[dict[str, Any]]:
    page_by_id = {page["page_id"]: page for page in pages}
    route_by_id = {page["page_id"]: page["route"] for page in pages}
    hints_by_id = {hint["page_id"]: dict(hint) for hint in page_hints}
    for family in PAGE_FAMILIES:
        page_id = PAGE_ID_BY_FAMILY[family]
        page = page_by_id[page_id]
        hint = hints_by_id[page_id]
        related_routes = [route_by_id[item] for item in page["related_page_ids"] if item in route_by_id]
        navigation_children = related_routes if family == "home" else []
        breadcrumbs = [PAGE_ROUTE_BY_FAMILY["home"]] if family == "home" else [PAGE_ROUTE_BY_FAMILY["home"], page["route"]]
        hint["page_family"] = family
        hint["route"] = page["route"]
        hint["navigation_parent"] = "" if family == "home" else PAGE_ROUTE_BY_FAMILY["home"]
        hint["navigation_children"] = navigation_children
        hint["related_routes"] = related_routes
        hint["breadcrumbs"] = breadcrumbs
        hint["validation_state"] = validation_state if hint.get("validation_state") is None else hint["validation_state"]
    return [hints_by_id[PAGE_ID_BY_FAMILY[family]] for family in PAGE_FAMILIES]


def build_knowledge_tree_export_payload(
    db_path: Path,
    *,
    generated_at: str | None = None,
    export_id: str | None = None,
    display_name: str | None = None,
    workspace_id: str | None = None,
    search_artifacts_dir: Path | None = None,
) -> KnowledgeTreeExportBuildResult:
    effective_generated_at = generated_at or now_rfc3339()
    snapshot = read_publication_snapshot(
        db_path,
        generated_at=effective_generated_at,
        search_artifacts_dir=search_artifacts_dir,
    )
    effective_workspace_id = slugify_identifier(workspace_id or snapshot["workspace_id"], fallback="canonical_workspace")
    effective_display_name = display_name or snapshot["display_name"]
    effective_export_id = slugify_identifier(export_id or f"{effective_workspace_id}_knowledge_tree", fallback="knowledge_tree")

    pages: list[dict[str, Any]] = []
    page_hints: list[dict[str, Any]] = []
    for family in PAGE_FAMILIES:
        page, hint = PAGE_BUILDERS[family](snapshot)
        pages.append(page)
        page_hints.append(hint)

    page_inventory_hints = build_page_inventory_hints(
        pages,
        page_hints,
        validation_state=snapshot["validation_summary"]["status"],
    )

    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "export_id": effective_export_id,
        "display_name": effective_display_name,
        "workspace_id": effective_workspace_id,
        "export_profile": DEFAULT_EXPORT_PROFILE,
        "generated_at": effective_generated_at,
        "landing_page_id": PAGE_ID_BY_FAMILY["home"],
        "page_families": list(PAGE_FAMILIES),
        "input_sources": [
            {
                "source_id": CANONICAL_SOURCE_ID,
                "source_kind": "sqlite_db",
                "logical_name": f"Canonical store for {effective_display_name}",
                "locator_path": f"canonical_store/{db_path.name}",
                "fingerprint": snapshot["db_fingerprint"],
                "storage_policy_class": "tracked_release",
                "rights_posture": "redistributable",
                "required_for_freshness": True,
            }
        ],
        "pages": pages,
        "notes": {
            "builder": {
                "writer_surface": "tools/scripts/build_knowledge_tree_export.py",
                "canonical_store_name": db_path.name,
                "canonical_store_schema_version": snapshot["schema_version"],
                "canonical_store_fingerprint": snapshot["db_fingerprint"],
                "canonical_store_storage_fingerprint": snapshot["db_storage_fingerprint"],
                "canonical_store_table_fingerprints": snapshot["publication_table_fingerprints"],
                "canonical_store_query_fingerprints": snapshot["publication_query_fingerprints"],
            },
            "page_inventory_hints": page_inventory_hints,
            "validation_summary": snapshot["validation_summary"],
            "search": {
                "query": snapshot["search_artifacts"].query_text,
                "projection_schema_version": snapshot["search_artifacts"].projection_payload["schema_version"],
                "results_schema_version": snapshot["search_artifacts"].results_payload["schema_version"],
                "projected_records": snapshot["search_artifacts"].projection_payload["counts"]["projected_records"],
                "returned_results": snapshot["search_artifacts"].results_payload["counts"]["returned"],
            },
        },
    }
    return KnowledgeTreeExportBuildResult(
        payload=payload,
        search_artifacts=snapshot["search_artifacts"],
        validation_state=snapshot["validation_summary"]["status"],
    )


def load_presentation_hints(export_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    notes = export_payload.get("notes")
    if not isinstance(notes, dict):
        return {}
    raw_hints = notes.get("page_inventory_hints")
    if not isinstance(raw_hints, list):
        return {}
    hints: dict[str, dict[str, Any]] = {}
    for item in raw_hints:
        if not isinstance(item, dict):
            continue
        page_id = item.get("page_id")
        if isinstance(page_id, str) and page_id.strip():
            hints[page_id] = item
    return hints


def build_public_presentation_payload(export_payload: dict[str, Any]) -> dict[str, Any]:
    pages = export_payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise PublicationBuildError("knowledge tree export does not contain any pages")
    page_by_id: dict[str, dict[str, Any]] = {}
    route_by_id: dict[str, str] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = page.get("page_id")
        route = page.get("route")
        if isinstance(page_id, str) and isinstance(route, str):
            page_by_id[page_id] = page
            route_by_id[page_id] = route
    hints = load_presentation_hints(export_payload)

    page_inventory: list[dict[str, Any]] = []
    for family in PAGE_FAMILIES:
        page_id = PAGE_ID_BY_FAMILY[family]
        page = page_by_id.get(page_id)
        if page is None:
            raise PublicationBuildError(f"knowledge tree export is missing required page family: {family}")
        hint = hints.get(page_id, {})
        raw_related_page_ids = page.get("related_page_ids")
        related_page_ids = raw_related_page_ids if isinstance(raw_related_page_ids, list) else []
        related_routes = [route_by_id[item] for item in related_page_ids if isinstance(item, str) and item in route_by_id]
        raw_cards = page.get("summary_cards")
        summary_labels: list[str] = []
        if isinstance(raw_cards, list):
            for card in raw_cards:
                if not isinstance(card, dict):
                    continue
                label = nonblank(card.get("label"))
                if label:
                    summary_labels.append(label)
        if not summary_labels:
            summary_labels = [str(page.get("title", "Page"))]

        page_inventory.append(
            {
                "page_family": family,
                "route": page["route"],
                "navigation_parent": hint.get("navigation_parent", "" if family == "home" else PAGE_ROUTE_BY_FAMILY["home"]),
                "reader_state": hint.get("reader_state", "ready" if family == "validation" else "sparse"),
                "review_state": page.get("review_posture", DEFAULT_PAGE_REVIEW_POSTURE),
                "validation_state": hint.get("validation_state", "passing"),
                "publication_state": page.get("publication_state", DEFAULT_PAGE_PUBLICATION_STATE),
                "source_transparency": hint.get(
                    "source_transparency",
                    "Public presentation excludes private locators, raw payloads, and internal notes.",
                ),
                "summary_cards": summary_labels,
                "empty_state": hint.get("empty_state", ""),
                "redaction_gate_refs": list(REDACTION_GATE_REFS),
                "navigation_children": hint.get("navigation_children", []),
                "related_routes": related_routes,
                "breadcrumbs": hint.get(
                    "breadcrumbs",
                    [page["route"]] if family == "home" else [PAGE_ROUTE_BY_FAMILY["home"], page["route"]],
                ),
            }
        )

    return {
        "schema_version": PRESENTATION_SCHEMA_VERSION,
        "contract_doc": PRESENTATION_CONTRACT_DOC,
        "page_inventory": page_inventory,
        "never_publish": list(NEVER_PUBLISH),
    }


def write_and_validate_export(output_path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(output_path, payload)
    validate_export_file(output_path)


def write_and_validate_presentation(output_path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(output_path, payload)
    validate_presentation_file(output_path)
