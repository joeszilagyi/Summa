#!/usr/bin/env python3
"""Manual source-locus seeding and local reporting for Phase 3A.

This tool only writes source-locus metadata supplied by the operator or tests.
It does not call external services, crawl pages, download payloads, or perform
automated acquisition.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any


REPORT_SCHEMA_VERSION = "source-locus-seed-report.v1"
EXPORT_SCHEMA_VERSION = "source-locus-export.v1"

ALLOWED_LOCUS_TYPES = {
    "government_agency",
    "archive",
    "library",
    "museum",
    "university_repository",
    "journal",
    "magazine",
    "newspaper",
    "publisher_catalog",
    "database",
    "forum",
    "podcast",
    "broadcaster",
    "video_platform",
    "map_repository",
    "bibliography",
    "search_engine",
    "aggregator",
    "local_collection",
    "unknown",
}

ALLOWED_QUERY_FAMILIES = {
    "government_records",
    "academic_literature",
    "newspapers",
    "magazines",
    "books",
    "archives",
    "libraries",
    "maps",
    "film_tv_documentary",
    "radio_podcast",
    "forums_community",
    "web_general",
    "bibliography_chaining",
    "local_document_ingest",
    "unknown",
}

ALLOWED_REVIEW_STATES = {"accepted", "needs_review", "demoted", "deprecated", "rejected"}
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SOURCE_LOCUS_FIELDS = [
    "locus_id",
    "topic_id",
    "display_name",
    "locus_type",
    "query_family",
    "parent_locus_id",
    "parent_org_id",
    "jurisdiction_place_id",
    "languages",
    "time_coverage_start",
    "time_coverage_end",
    "access_class",
    "access_url",
    "catalog_url",
    "archive_url",
    "access_notes",
    "rights_posture",
    "refetchability_status",
    "discovery_method",
    "discovery_source",
    "discovered_at",
    "discovered_by",
    "confidence_score",
    "review_state",
    "productivity_queries_run",
    "productivity_leads_returned",
    "productivity_unique_leads",
    "productivity_captures_made",
    "productivity_works_promoted",
    "productivity_score",
    "last_queried_at",
    "last_productive_at",
    "cooldown_until",
    "is_deprecated",
    "deprecation_reason",
    "notes",
]


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise RuntimeError(f"{field_name} must be a boolean")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug or "unnamed"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not SQL_IDENTIFIER_RE.fullmatch(table):
        raise RuntimeError(f"invalid SQL identifier: {table}")
    if not table_exists(conn, table):
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(conn: sqlite3.Connection, table: str, column_name: str, column_definition: str) -> None:
    if not SQL_IDENTIFIER_RE.fullmatch(table):
        raise RuntimeError(f"invalid SQL identifier: {table}")
    if not SQL_IDENTIFIER_RE.fullmatch(column_name):
        raise RuntimeError(f"invalid SQL identifier: {column_name}")
    if not column_definition.startswith(f"{column_name} "):
        raise RuntimeError(f"invalid column definition: {column_definition}")
    if any(token in column_definition for token in (";", "--", "/*", "*/")):
        raise RuntimeError(f"invalid column definition: {column_definition}")
    if table_exists(conn, table) and column_name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")


def iter_seed_records(seed_path: Path):
    if not seed_path.is_file():
        raise RuntimeError(f"seed file not found: {seed_path}")
    if seed_path.suffix == ".jsonl":
        with seed_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{seed_path}:{line_number}: invalid JSON") from exc
                if not isinstance(value, dict):
                    raise RuntimeError(f"{seed_path}:{line_number}: seed row must be an object")
                yield value
        return

    try:
        value = json.loads(seed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{seed_path}: invalid JSON") from exc
    if isinstance(value, dict):
        records = value.get("source_loci")
    else:
        records = value
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise RuntimeError("seed JSON must be a list of objects or an object with source_loci list")
    yield from records


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_locus (
          source_locus_pk INTEGER PRIMARY KEY,
          locus_id TEXT NOT NULL UNIQUE,
          topic_id TEXT NOT NULL,
          display_name TEXT NOT NULL,
          locus_type TEXT NOT NULL,
          query_family TEXT NOT NULL,
          parent_locus_id TEXT,
          parent_org_id TEXT,
          jurisdiction_place_id TEXT,
          languages_json TEXT NOT NULL DEFAULT '[]',
          time_coverage_start TEXT,
          time_coverage_end TEXT,
          access_class TEXT NOT NULL,
          access_url TEXT,
          catalog_url TEXT,
          archive_url TEXT,
          access_notes TEXT,
          rights_posture TEXT NOT NULL,
          refetchability_status TEXT NOT NULL,
          discovery_method TEXT NOT NULL,
          discovery_source TEXT NOT NULL,
          discovered_at TEXT NOT NULL,
          discovered_by TEXT NOT NULL,
          confidence_score REAL NOT NULL,
          review_state TEXT NOT NULL,
          productivity_queries_run INTEGER NOT NULL DEFAULT 0,
          productivity_leads_returned INTEGER NOT NULL DEFAULT 0,
          productivity_unique_leads INTEGER NOT NULL DEFAULT 0,
          productivity_captures_made INTEGER NOT NULL DEFAULT 0,
          productivity_works_promoted INTEGER NOT NULL DEFAULT 0,
          productivity_score REAL NOT NULL DEFAULT 0.0,
          last_queried_at TEXT,
          last_productive_at TEXT,
          cooldown_until TEXT,
          is_deprecated INTEGER NOT NULL DEFAULT 0,
          deprecation_reason TEXT,
          notes TEXT,
          record_last_updated TEXT NOT NULL,
          FOREIGN KEY(parent_locus_id) REFERENCES source_locus(locus_id)
        );
        CREATE INDEX IF NOT EXISTS ix_source_locus_topic ON source_locus(topic_id, review_state, locus_type);
        CREATE INDEX IF NOT EXISTS ix_source_locus_parent ON source_locus(parent_locus_id);
        CREATE INDEX IF NOT EXISTS ix_source_locus_productivity ON source_locus(topic_id, productivity_score, last_productive_at);
        """
    )
    add_column_if_missing(conn, "lead", "source_locus_id", "source_locus_id TEXT")
    add_column_if_missing(conn, "source_access", "source_locus_id", "source_locus_id TEXT")
    add_column_if_missing(conn, "source_access", "source_lead_id", "source_lead_id TEXT")
    if table_exists(conn, "lead"):
        conn.execute("CREATE INDEX IF NOT EXISTS ix_lead_source_locus ON lead(source_locus_id)")


def load_seed_records(seed_path: Path) -> list[dict[str, Any]]:
    return list(iter_seed_records(seed_path))


def unknown_locus_id(topic_id: str) -> str:
    return f"locus:unknown_locus:{slugify(topic_id)}"


def unknown_locus_record(
    topic_id: str,
    *,
    discovered_at: str,
    discovered_by: str,
    discovery_source: str = "unknown_locus_fallback",
) -> dict[str, Any]:
    return {
        "locus_id": unknown_locus_id(topic_id),
        "topic_id": topic_id,
        "display_name": f"Unknown locus fallback for {topic_id}",
        "locus_type": "unknown",
        "query_family": "unknown",
        "parent_locus_id": None,
        "parent_org_id": None,
        "jurisdiction_place_id": None,
        "languages": ["unknown"],
        "time_coverage_start": None,
        "time_coverage_end": None,
        "access_class": "unknown",
        "access_url": None,
        "catalog_url": None,
        "archive_url": None,
        "access_notes": "Fallback locus for serendipitous or legacy leads with no known reviewed source locus.",
        "rights_posture": "unknown",
        "refetchability_status": "unknown",
        "discovery_method": "system_fallback",
        "discovery_source": discovery_source,
        "discovered_at": discovered_at,
        "discovered_by": discovered_by,
        "confidence_score": 0.0,
        "review_state": "needs_review",
        "productivity_queries_run": 0,
        "productivity_leads_returned": 0,
        "productivity_unique_leads": 0,
        "productivity_captures_made": 0,
        "productivity_works_promoted": 0,
        "productivity_score": 0.0,
        "last_queried_at": None,
        "last_productive_at": None,
        "cooldown_until": None,
        "is_deprecated": False,
        "deprecation_reason": None,
        "notes": "Created by Phase 3A unknown_locus fallback; not a reviewed source-locus candidate.",
    }


def normalize_seed_record(
    raw: dict[str, Any],
    *,
    topic_id: str,
    seed_path: Path,
    discovered_at: str,
    discovered_by: str,
) -> dict[str, Any]:
    display_name = str(raw.get("display_name") or raw.get("name") or "").strip()
    if not display_name:
        raise RuntimeError("seed row missing display_name")
    locus_type = str(raw.get("locus_type") or "unknown").strip()
    query_family = str(raw.get("query_family") or "unknown").strip()
    if locus_type not in ALLOWED_LOCUS_TYPES:
        raise RuntimeError(f"{display_name}: unsupported locus_type {locus_type!r}")
    if query_family not in ALLOWED_QUERY_FAMILIES:
        raise RuntimeError(f"{display_name}: unsupported query_family {query_family!r}")
    review_state = str(raw.get("review_state") or "needs_review").strip()
    if review_state not in ALLOWED_REVIEW_STATES:
        raise RuntimeError(f"{display_name}: unsupported review_state {review_state!r}")

    locus_id = str(raw.get("locus_id") or f"locus:{slugify(topic_id)}:{locus_type}:{slugify(display_name)}").strip()
    if not locus_id.startswith("locus:"):
        raise RuntimeError(f"{display_name}: locus_id must use the locus: namespace")

    languages = raw.get("languages", ["unknown"])
    if not isinstance(languages, list) or any(not isinstance(item, str) or not item.strip() for item in languages):
        raise RuntimeError(f"{display_name}: languages must be a list of nonblank strings")

    is_deprecated = coerce_bool(
        raw.get("is_deprecated", False),
        field_name=f"{display_name}: is_deprecated",
    )
    deprecation_reason = raw.get("deprecation_reason")
    if is_deprecated and not str(deprecation_reason or "").strip():
        raise RuntimeError(f"{display_name}: deprecated loci require deprecation_reason")

    record = {
        "locus_id": locus_id,
        "topic_id": str(raw.get("topic_id") or topic_id),
        "display_name": display_name,
        "locus_type": locus_type,
        "query_family": query_family,
        "parent_locus_id": raw.get("parent_locus_id"),
        "parent_org_id": raw.get("parent_org_id"),
        "jurisdiction_place_id": raw.get("jurisdiction_place_id"),
        "languages": [item.strip() for item in languages],
        "time_coverage_start": raw.get("time_coverage_start"),
        "time_coverage_end": raw.get("time_coverage_end"),
        "access_class": str(raw.get("access_class") or "unknown"),
        "access_url": raw.get("access_url"),
        "catalog_url": raw.get("catalog_url"),
        "archive_url": raw.get("archive_url"),
        "access_notes": raw.get("access_notes"),
        "rights_posture": str(raw.get("rights_posture") or "unknown"),
        "refetchability_status": str(raw.get("refetchability_status") or "unknown"),
        "discovery_method": str(raw.get("discovery_method") or "manual_seed"),
        "discovery_source": str(raw.get("discovery_source") or seed_path.as_posix()),
        "discovered_at": str(raw.get("discovered_at") or discovered_at),
        "discovered_by": str(raw.get("discovered_by") or discovered_by),
        "confidence_score": float(raw.get("confidence_score", 0.5)),
        "review_state": review_state,
        "productivity_queries_run": int(raw.get("productivity_queries_run", 0)),
        "productivity_leads_returned": int(raw.get("productivity_leads_returned", 0)),
        "productivity_unique_leads": int(raw.get("productivity_unique_leads", 0)),
        "productivity_captures_made": int(raw.get("productivity_captures_made", 0)),
        "productivity_works_promoted": int(raw.get("productivity_works_promoted", 0)),
        "productivity_score": float(raw.get("productivity_score", 0.0)),
        "last_queried_at": raw.get("last_queried_at"),
        "last_productive_at": raw.get("last_productive_at"),
        "cooldown_until": raw.get("cooldown_until"),
        "is_deprecated": is_deprecated,
        "deprecation_reason": deprecation_reason,
        "notes": raw.get("notes"),
    }
    return record


def validate_normalized_record(record: dict[str, Any]) -> None:
    if record["parent_locus_id"] is not None and not str(record["parent_locus_id"]).startswith("locus:"):
        raise RuntimeError(f"{record['locus_id']}: parent_locus_id must use locus: namespace")
    if record["locus_type"] == "unknown" and "unknown_locus" not in record["locus_id"]:
        raise RuntimeError(f"{record['locus_id']}: unknown loci must include unknown_locus in locus_id")
    for key in (
        "productivity_queries_run",
        "productivity_leads_returned",
        "productivity_unique_leads",
        "productivity_captures_made",
        "productivity_works_promoted",
    ):
        if record[key] < 0:
            raise RuntimeError(f"{record['locus_id']}: {key} must be nonnegative")
    for key in ("confidence_score", "productivity_score"):
        if record[key] < 0 or record[key] > 1:
            raise RuntimeError(f"{record['locus_id']}: {key} must be between 0 and 1")


def upsert_source_locus(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    *,
    updated_at: str,
    overwrite_curation: bool = False,
) -> None:
    validate_normalized_record(record)
    columns = [
        "locus_id",
        "topic_id",
        "display_name",
        "locus_type",
        "query_family",
        "parent_locus_id",
        "parent_org_id",
        "jurisdiction_place_id",
        "languages_json",
        "time_coverage_start",
        "time_coverage_end",
        "access_class",
        "access_url",
        "catalog_url",
        "archive_url",
        "access_notes",
        "rights_posture",
        "refetchability_status",
        "discovery_method",
        "discovery_source",
        "discovered_at",
        "discovered_by",
        "confidence_score",
        "review_state",
        "productivity_queries_run",
        "productivity_leads_returned",
        "productivity_unique_leads",
        "productivity_captures_made",
        "productivity_works_promoted",
        "productivity_score",
        "last_queried_at",
        "last_productive_at",
        "cooldown_until",
        "is_deprecated",
        "deprecation_reason",
        "notes",
        "record_last_updated",
    ]
    values = {
        **{key: record[key] for key in SOURCE_LOCUS_FIELDS if key not in {"languages", "is_deprecated"}},
        "languages_json": json.dumps(record["languages"], ensure_ascii=False, sort_keys=True),
        "is_deprecated": 1 if record["is_deprecated"] else 0,
        "record_last_updated": updated_at,
    }
    if overwrite_curation:
        update_columns = [column for column in columns if column != "locus_id"]
    else:
        # Preserve existing curation and operational fields unless explicit override requested.
        update_columns = ["record_last_updated"]
    sql = f"""
    INSERT INTO source_locus ({', '.join(columns)})
    VALUES ({', '.join('?' for _ in columns)})
    ON CONFLICT(locus_id) DO UPDATE SET
      {', '.join(f'{column}=excluded.{column}' for column in update_columns)}
    """
    conn.execute(sql, [values[column] for column in columns])


def ensure_unknown_locus(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    discovered_at: str,
    discovered_by: str,
) -> str:
    locus = unknown_locus_record(topic_id, discovered_at=discovered_at, discovered_by=discovered_by)
    upsert_source_locus(conn, locus, updated_at=discovered_at)
    return locus["locus_id"]


def assign_unknown_locus_to_unlinked_leads(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    country_slug: str | None,
    discovered_at: str,
    discovered_by: str,
) -> int:
    ensure_schema(conn)
    locus_id = ensure_unknown_locus(conn, topic_id, discovered_at=discovered_at, discovered_by=discovered_by)
    if not table_exists(conn, "lead") or "source_locus_id" not in table_columns(conn, "lead"):
        return 0
    lead_scope = country_slug or topic_id
    cursor = conn.execute(
        """
        UPDATE lead
        SET source_locus_id=?
        WHERE source_locus_id IS NULL
          AND (country_slug=? OR country_slug_canonical=?)
        """,
        (locus_id, lead_scope, lead_scope),
    )
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def productivity_score(
    *,
    queries_run: int,
    unique_leads: int,
    captures_made: int,
    works_promoted: int,
) -> float:
    denominator = max(1, queries_run * 10)
    weighted = unique_leads + (captures_made * 2) + (works_promoted * 4)
    return round(min(1.0, weighted / denominator), 4)


def update_productivity(
    conn: sqlite3.Connection,
    locus_id: str,
    *,
    queries_run_delta: int = 0,
    leads_returned_delta: int = 0,
    unique_leads_delta: int = 0,
    captures_made_delta: int = 0,
    works_promoted_delta: int = 0,
    timestamp: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM source_locus WHERE locus_id=?", (locus_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"source_locus not found: {locus_id}")
    for value in (queries_run_delta, leads_returned_delta, unique_leads_delta, captures_made_delta, works_promoted_delta):
        if value < 0:
            raise RuntimeError("productivity deltas must be nonnegative")
    updated_at = timestamp or now_iso()
    queries_run = int(row["productivity_queries_run"]) + queries_run_delta
    leads_returned = int(row["productivity_leads_returned"]) + leads_returned_delta
    unique_leads = int(row["productivity_unique_leads"]) + unique_leads_delta
    captures_made = int(row["productivity_captures_made"]) + captures_made_delta
    works_promoted = int(row["productivity_works_promoted"]) + works_promoted_delta
    score = productivity_score(
        queries_run=queries_run,
        unique_leads=unique_leads,
        captures_made=captures_made,
        works_promoted=works_promoted,
    )
    last_queried_at = updated_at if queries_run_delta else row["last_queried_at"]
    last_productive_at = (
        updated_at
        if (unique_leads_delta or captures_made_delta or works_promoted_delta)
        else row["last_productive_at"]
    )
    conn.execute(
        """
        UPDATE source_locus
        SET productivity_queries_run=?,
            productivity_leads_returned=?,
            productivity_unique_leads=?,
            productivity_captures_made=?,
            productivity_works_promoted=?,
            productivity_score=?,
            last_queried_at=?,
            last_productive_at=?,
            record_last_updated=?
        WHERE locus_id=?
        """,
        (
            queries_run,
            leads_returned,
            unique_leads,
            captures_made,
            works_promoted,
            score,
            last_queried_at,
            last_productive_at,
            updated_at,
            locus_id,
        ),
    )
    return {
        "locus_id": locus_id,
        "productivity_queries_run": queries_run,
        "productivity_leads_returned": leads_returned,
        "productivity_unique_leads": unique_leads,
        "productivity_captures_made": captures_made,
        "productivity_works_promoted": works_promoted,
        "productivity_score": score,
        "last_queried_at": last_queried_at,
        "last_productive_at": last_productive_at,
    }


def db_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = {field: row[field] for field in SOURCE_LOCUS_FIELDS if field not in {"languages", "is_deprecated"}}
    try:
        record["languages"] = json.loads(row["languages_json"] or "[]")
    except json.JSONDecodeError:
        record["languages"] = []
    record["is_deprecated"] = bool(row["is_deprecated"])
    return record


def export_source_loci(conn: sqlite3.Connection, topic_id: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    params: tuple[Any, ...] = ()
    where = ""
    if topic_id:
        where = "WHERE topic_id=?"
        params = (topic_id,)
    rows = conn.execute(
        f"""
        SELECT *
        FROM source_locus
        {where}
        ORDER BY topic_id, is_deprecated, review_state, display_name, locus_id
        """,
        params,
    ).fetchall()
    records = [db_row_to_record(row) for row in rows]
    by_review_state: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for record in records:
        by_review_state[record["review_state"]] = by_review_state.get(record["review_state"], 0) + 1
        by_type[record["locus_type"]] = by_type.get(record["locus_type"], 0) + 1
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "topic_id": topic_id,
        "counts": {
            "total_loci": len(records),
            "review_needed_loci": sum(1 for record in records if record["review_state"] in {"needs_review", "demoted"}),
            "deprecated_loci": sum(1 for record in records if record["is_deprecated"]),
            "unknown_loci": sum(1 for record in records if record["locus_type"] == "unknown"),
            "by_review_state": by_review_state,
            "by_type": by_type,
        },
        "source_loci": records,
    }


def seed_database(
    conn: sqlite3.Connection,
    *,
    seed_path: Path,
    topic_id: str,
    discovered_at: str,
    discovered_by: str,
    assign_unknown_leads: bool = False,
    lead_country_slug: str | None = None,
    overwrite_curation: bool = False,
) -> dict[str, Any]:
    ensure_schema(conn)
    raw_records = iter_seed_records(seed_path)
    unknown_id = ensure_unknown_locus(
        conn, topic_id, discovered_at=discovered_at, discovered_by=discovered_by
    )
    inserted_or_updated = 1
    manual_seed_records = 0
    for raw in raw_records:
        record = normalize_seed_record(
            raw,
            topic_id=topic_id,
            seed_path=seed_path,
            discovered_at=discovered_at,
            discovered_by=discovered_by,
        )
        upsert_source_locus(
            conn,
            record,
            updated_at=discovered_at,
            overwrite_curation=overwrite_curation,
        )
        inserted_or_updated += 1
        manual_seed_records += 1
    assigned_unknown_leads = 0
    if assign_unknown_leads:
        assigned_unknown_leads = assign_unknown_locus_to_unlinked_leads(
            conn,
            topic_id,
            country_slug=lead_country_slug,
            discovered_at=discovered_at,
            discovered_by=discovered_by,
        )
    conn.commit()
    export = export_source_loci(conn, topic_id)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "topic_id": topic_id,
        "seed_path": seed_path.as_posix(),
        "inserted_or_updated": inserted_or_updated,
        "manual_seed_records": manual_seed_records,
        "unknown_locus_id": unknown_id,
        "assigned_unknown_leads": assigned_unknown_leads,
        "source_locus_export": export,
    }


def write_json(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    tmp_path_obj = Path(tmp_path)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path_obj, path)
    finally:
        if tmp_path_obj.exists():
            tmp_path_obj.unlink(missing_ok=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed and report local source-locus candidates without acquisition.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed", help="Load a manually supplied seed list into source_locus.")
    seed.add_argument("--db", required=True, type=Path)
    seed.add_argument("--seed-json", required=True, type=Path, help="JSON or JSONL manual seed list.")
    seed.add_argument("--topic-id", required=True)
    seed.add_argument("--discovered-at", default="2026-04-28T00:00:00+00:00")
    seed.add_argument("--discovered-by", default="codex_phase3a")
    seed.add_argument("--assign-unknown-to-unlinked-leads", action="store_true")
    seed.add_argument(
        "--overwrite-curation",
        action="store_true",
        help="Allow reseeding to replace existing curation fields for matching locus_id.",
    )
    seed.add_argument("--lead-country-slug")
    seed.add_argument("--report-json", type=Path)

    productivity = subparsers.add_parser("update-productivity", help="Increment deterministic source-locus counters.")
    productivity.add_argument("--db", required=True, type=Path)
    productivity.add_argument("--locus-id", required=True)
    productivity.add_argument("--queries-run", type=int, default=0)
    productivity.add_argument("--leads-returned", type=int, default=0)
    productivity.add_argument("--unique-leads", type=int, default=0)
    productivity.add_argument("--captures-made", type=int, default=0)
    productivity.add_argument("--works-promoted", type=int, default=0)
    productivity.add_argument("--timestamp", default="2026-04-28T00:00:00+00:00")
    productivity.add_argument("--report-json", type=Path)

    export = subparsers.add_parser("export", help="Export source-locus records as full-fidelity local JSON.")
    export.add_argument("--db", required=True, type=Path)
    export.add_argument("--topic-id")
    export.add_argument("--report-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(args.db)
        if args.command == "seed":
            payload = seed_database(
                conn,
                seed_path=args.seed_json,
                topic_id=args.topic_id,
                discovered_at=args.discovered_at,
                discovered_by=args.discovered_by,
                assign_unknown_leads=args.assign_unknown_to_unlinked_leads,
                lead_country_slug=args.lead_country_slug,
                overwrite_curation=args.overwrite_curation,
            )
        elif args.command == "update-productivity":
            metrics = update_productivity(
                conn,
                args.locus_id,
                queries_run_delta=args.queries_run,
                leads_returned_delta=args.leads_returned,
                unique_leads_delta=args.unique_leads,
                captures_made_delta=args.captures_made,
                works_promoted_delta=args.works_promoted,
                timestamp=args.timestamp,
            )
            conn.commit()
            payload = {
                "schema_version": REPORT_SCHEMA_VERSION,
                "operation": "update-productivity",
                "metrics": metrics,
            }
        elif args.command == "export":
            payload = export_source_loci(conn, args.topic_id)
        else:  # pragma: no cover - argparse prevents this.
            raise RuntimeError(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"source-locus seed error: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    write_json(args.report_json, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
