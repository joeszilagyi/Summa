#!/usr/bin/env python3
"""Create local source-query plans from source-locus staging records.

This is a planning-only Phase 3B helper. It never calls external services,
crawls, downloads payloads, creates source captures, or turns plans into real
source candidates. The paired execution simulation consumes these plans to test
local source-discovery behavior without acquisition side effects.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools import source_locus_seed  # noqa: E402

REPORT_SCHEMA_VERSION = "source-query-plan-report.v1"
EXPORT_SCHEMA_VERSION = "source-query-plan-export.v1"

ALLOWED_PLAN_STATUSES = {"proposed", "needs_review", "accepted", "rejected", "deprecated"}
ALLOWED_REVIEW_STATES = {"accepted", "needs_review", "demoted", "deprecated", "rejected"}
ALLOWED_QUERY_MODES = {
    "site_search",
    "catalog_search",
    "archive_search",
    "bibliographic_search",
    "newspaper_search",
    "local_search",
    "manual_search",
    "unknown",
}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
ALLOWED_COST_LEVELS = {"none", "low", "medium", "high", "unknown"}
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

QUERY_PLAN_FIELDS = [
    "query_plan_id",
    "topic_id",
    "locus_id",
    "query_family",
    "locus_type",
    "plan_status",
    "query_text",
    "normalized_query",
    "query_language",
    "query_target",
    "query_mode",
    "expected_source_type",
    "expected_access_class",
    "expected_rights_posture",
    "expected_refetchability",
    "risk_level",
    "cost_level",
    "manual_review_required",
    "rationale",
    "generated_from",
    "generated_at",
    "generated_by",
    "confidence_score",
    "review_state",
    "simulation_only",
    "network_access_attempted",
    "notes",
]


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


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
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _require_safe_column_definition(column_name: str, column_definition: str) -> None:
    if not SQL_IDENTIFIER_RE.fullmatch(column_name):
        raise RuntimeError(f"invalid SQL identifier: {column_name}")
    prefix = f"{column_name} "
    if not column_definition.startswith(prefix):
        raise RuntimeError(f"invalid column definition: {column_definition}")
    if any(token in column_definition for token in (";", "--", "/*", "*/")):
        raise RuntimeError(f"invalid column definition: {column_definition}")


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column_name: str, column_definition: str
) -> None:
    if not SQL_IDENTIFIER_RE.fullmatch(table):
        raise RuntimeError(f"invalid SQL identifier: {table}")
    _require_safe_column_definition(column_name, column_definition)
    if table_exists(conn, table) and column_name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    source_locus_seed.ensure_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_query_plan (
          source_query_plan_pk INTEGER PRIMARY KEY,
          query_plan_id TEXT NOT NULL UNIQUE,
          topic_id TEXT NOT NULL,
          locus_id TEXT NOT NULL,
          query_family TEXT NOT NULL,
          locus_type TEXT NOT NULL,
          plan_status TEXT NOT NULL,
          query_text TEXT NOT NULL,
          normalized_query TEXT NOT NULL,
          query_language TEXT NOT NULL,
          query_target TEXT NOT NULL,
          query_mode TEXT NOT NULL,
          expected_source_type TEXT NOT NULL,
          expected_access_class TEXT NOT NULL,
          expected_rights_posture TEXT NOT NULL,
          expected_refetchability TEXT NOT NULL,
          risk_level TEXT NOT NULL,
          cost_level TEXT NOT NULL,
          manual_review_required INTEGER NOT NULL DEFAULT 0,
          rationale TEXT NOT NULL,
          generated_from TEXT NOT NULL,
          generated_at TEXT NOT NULL,
          generated_by TEXT NOT NULL,
          confidence_score REAL NOT NULL,
          review_state TEXT NOT NULL,
          simulation_only INTEGER NOT NULL DEFAULT 1,
          network_access_attempted INTEGER NOT NULL DEFAULT 0,
          notes TEXT,
          record_last_updated TEXT NOT NULL,
          FOREIGN KEY(locus_id) REFERENCES source_locus(locus_id)
        );
        CREATE INDEX IF NOT EXISTS ix_source_query_plan_topic ON source_query_plan(topic_id, plan_status, query_family, review_state);
        CREATE INDEX IF NOT EXISTS ix_source_query_plan_locus ON source_query_plan(locus_id);
        CREATE INDEX IF NOT EXISTS ix_source_query_plan_mode ON source_query_plan(query_mode, expected_access_class, cost_level);
        """
    )
    add_column_if_missing(
        conn, "source_query_plan", "normalized_query", "normalized_query TEXT NOT NULL DEFAULT ''"
    )
    add_column_if_missing(
        conn, "source_query_plan", "simulation_only", "simulation_only INTEGER NOT NULL DEFAULT 1"
    )
    add_column_if_missing(
        conn,
        "source_query_plan",
        "network_access_attempted",
        "network_access_attempted INTEGER NOT NULL DEFAULT 0",
    )


def normalize_query_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    return normalized


def deterministic_query_plan_id(
    *,
    topic_id: str,
    locus_id: str,
    query_family: str,
    query_mode: str,
    query_target: str,
) -> str:
    fingerprint = hashlib.sha256(
        "|".join(
            [
                normalize_query_text(topic_id),
                normalize_query_text(locus_id),
                normalize_query_text(query_family),
                normalize_query_text(query_mode),
                normalize_query_text(query_target),
            ]
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"qplan:{slugify(topic_id)}:{slugify(locus_id.removeprefix('locus:'))}:{slugify(query_family)}:{fingerprint}"


def query_mode_for_locus(record: dict[str, Any]) -> str:
    family = str(record.get("query_family") or "unknown")
    locus_type = str(record.get("locus_type") or "unknown")
    if family == "newspapers":
        return "newspaper_search"
    if family in {"books", "libraries"} or locus_type in {"library", "publisher_catalog"}:
        return "catalog_search"
    if family in {"archives", "government_records"} or locus_type in {
        "archive",
        "government_agency",
    }:
        return "archive_search" if family == "archives" else "site_search"
    if family in {"academic_literature", "bibliography_chaining"}:
        return "bibliographic_search"
    if family == "local_document_ingest" or locus_type == "local_collection":
        return "local_search"
    if family in {
        "web_general",
        "forums_community",
        "radio_podcast",
        "film_tv_documentary",
        "maps",
        "magazines",
    }:
        return "site_search"
    return "unknown"


def expected_source_type_for_locus(record: dict[str, Any]) -> str:
    family = str(record.get("query_family") or "unknown")
    return {
        "government_records": "government_record",
        "academic_literature": "scholarly_reference",
        "newspapers": "newspaper_article",
        "magazines": "magazine_article",
        "books": "book_or_catalog_record",
        "archives": "archival_finding_aid",
        "libraries": "catalog_record",
        "maps": "map_record",
        "film_tv_documentary": "media_record",
        "radio_podcast": "audio_record",
        "forums_community": "community_discussion",
        "web_general": "web_page",
        "bibliography_chaining": "bibliographic_reference",
        "local_document_ingest": "local_document",
    }.get(family, "unknown")


def cost_level_for_locus(record: dict[str, Any]) -> str:
    access_class = str(record.get("access_class") or "").casefold()
    if any(token in access_class for token in ("subscription", "paywall", "restricted")):
        return "medium"
    if any(token in access_class for token in ("fee", "onsite", "limited")):
        return "low"
    if any(token in access_class for token in ("unknown", "not_checked")):
        return "unknown"
    return "none"


def risk_level_for_locus(record: dict[str, Any]) -> str:
    access_class = str(record.get("access_class") or "").casefold()
    locus_type = str(record.get("locus_type") or "").casefold()
    if any(token in access_class for token in ("restricted", "subscription", "paywall")):
        return "medium"
    if locus_type == "unknown":
        return "high"
    return "low"


def query_target_for_locus(record: dict[str, Any]) -> str:
    for key in ("catalog_url", "archive_url", "access_url", "display_name", "locus_id"):
        value = record.get(key)
        if value:
            return str(value)
    return "unknown"


def query_text_for_locus(record: dict[str, Any], *, topic_id: str) -> str:
    target = query_target_for_locus(record)
    display_name = str(record.get("display_name") or target)
    family = str(record.get("query_family") or "unknown")
    if str(
        record.get("access_url") or record.get("catalog_url") or record.get("archive_url") or ""
    ).startswith(("http://", "https://")):
        return f"{topic_id} {display_name} {family}"
    return f"{topic_id} {target} {family}"


def plan_from_locus(
    record: dict[str, Any], *, generated_at: str, generated_by: str
) -> dict[str, Any]:
    topic_id = str(record["topic_id"])
    query_mode = query_mode_for_locus(record)
    query_target = query_target_for_locus(record)
    query_family = str(record.get("query_family") or "unknown")
    review_state = str(record.get("review_state") or "needs_review")
    is_deprecated = bool(record.get("is_deprecated"))
    locus_type = str(record.get("locus_type") or "unknown")
    manual_review_required = review_state != "accepted" or locus_type == "unknown"
    if is_deprecated or review_state == "deprecated":
        plan_status = "deprecated"
        plan_review_state = "deprecated"
    elif manual_review_required:
        plan_status = "needs_review"
        plan_review_state = "needs_review"
    else:
        plan_status = "accepted"
        plan_review_state = "accepted"
    query_text = query_text_for_locus(record, topic_id=topic_id)
    plan_id = deterministic_query_plan_id(
        topic_id=topic_id,
        locus_id=str(record["locus_id"]),
        query_family=query_family,
        query_mode=query_mode,
        query_target=query_target,
    )
    return {
        "query_plan_id": plan_id,
        "topic_id": topic_id,
        "locus_id": record["locus_id"],
        "query_family": query_family,
        "locus_type": locus_type,
        "plan_status": plan_status,
        "query_text": query_text,
        "normalized_query": normalize_query_text(query_text),
        "query_language": "en",
        "query_target": query_target,
        "query_mode": query_mode,
        "expected_source_type": expected_source_type_for_locus(record),
        "expected_access_class": str(record.get("access_class") or "unknown"),
        "expected_rights_posture": str(record.get("rights_posture") or "unknown"),
        "expected_refetchability": str(record.get("refetchability_status") or "not_checked"),
        "risk_level": risk_level_for_locus(record),
        "cost_level": cost_level_for_locus(record),
        "manual_review_required": manual_review_required,
        "rationale": "Planning-only query derived deterministically from source_locus metadata.",
        "generated_from": f"source_locus:{record['locus_id']}",
        "generated_at": generated_at,
        "generated_by": generated_by,
        "confidence_score": round(float(record.get("confidence_score") or 0.0), 4),
        "review_state": plan_review_state,
        "simulation_only": True,
        "network_access_attempted": False,
        "notes": "PLANNING ONLY. No query was executed and no network access was attempted.",
    }


def validate_plan(plan: dict[str, Any]) -> None:
    required = [field for field in QUERY_PLAN_FIELDS if field not in {"notes"}]
    missing = [field for field in required if field not in plan or plan[field] is None]
    if missing:
        raise RuntimeError(
            f"{plan.get('query_plan_id', '(unknown)')}: missing required fields: {', '.join(missing)}"
        )
    if not re.fullmatch(r"qplan:[a-z0-9][a-z0-9:_-]*", str(plan["query_plan_id"])):
        raise RuntimeError(f"{plan['query_plan_id']}: invalid query_plan_id")
    if not re.fullmatch(r"locus:[a-z0-9][a-z0-9:_-]*", str(plan["locus_id"])):
        raise RuntimeError(f"{plan['query_plan_id']}: invalid locus_id")
    if plan["plan_status"] not in ALLOWED_PLAN_STATUSES:
        raise RuntimeError(f"{plan['query_plan_id']}: invalid plan_status")
    if plan["review_state"] not in ALLOWED_REVIEW_STATES:
        raise RuntimeError(f"{plan['query_plan_id']}: invalid review_state")
    if plan["query_mode"] not in ALLOWED_QUERY_MODES:
        raise RuntimeError(f"{plan['query_plan_id']}: invalid query_mode")
    if plan["risk_level"] not in ALLOWED_RISK_LEVELS:
        raise RuntimeError(f"{plan['query_plan_id']}: invalid risk_level")
    if plan["cost_level"] not in ALLOWED_COST_LEVELS:
        raise RuntimeError(f"{plan['query_plan_id']}: invalid cost_level")
    if not isinstance(plan["manual_review_required"], bool):
        raise RuntimeError(f"{plan['query_plan_id']}: manual_review_required must be boolean")
    if plan["simulation_only"] is not True:
        raise RuntimeError(f"{plan['query_plan_id']}: simulation_only must be true")
    if plan["network_access_attempted"] is not False:
        raise RuntimeError(f"{plan['query_plan_id']}: network_access_attempted must be false")
    confidence = float(plan["confidence_score"])
    if confidence < 0 or confidence > 1:
        raise RuntimeError(f"{plan['query_plan_id']}: confidence_score must be between 0 and 1")


def upsert_query_plan(
    conn: sqlite3.Connection, plan: dict[str, Any], *, updated_at: str | None = None
) -> None:
    validate_plan(plan)
    columns = QUERY_PLAN_FIELDS + ["record_last_updated"]
    values = {
        **plan,
        "manual_review_required": 1 if plan["manual_review_required"] else 0,
        "simulation_only": 1 if plan["simulation_only"] else 0,
        "network_access_attempted": 1 if plan["network_access_attempted"] else 0,
        "record_last_updated": updated_at or str(plan["generated_at"]),
    }
    update_columns = [column for column in columns if column != "query_plan_id"]
    sql = f"""
    INSERT INTO source_query_plan ({", ".join(columns)})
    VALUES ({", ".join("?" for _ in columns)})
    ON CONFLICT(query_plan_id) DO UPDATE SET
      {", ".join(f"{column}=excluded.{column}" for column in update_columns)}
    """
    conn.execute(sql, [values[column] for column in columns])


def row_to_plan(row: sqlite3.Row) -> dict[str, Any]:
    row_keys = set(row.keys())
    plan = {field: row[field] for field in QUERY_PLAN_FIELDS if field in row_keys}
    if "normalized_query" not in plan or not plan["normalized_query"]:
        plan["normalized_query"] = normalize_query_text(str(plan["query_text"]))
    plan["manual_review_required"] = bool(plan["manual_review_required"])
    plan["simulation_only"] = bool(plan.get("simulation_only", 1))
    plan["network_access_attempted"] = bool(plan.get("network_access_attempted", 0))
    return plan


def load_source_loci(
    conn: sqlite3.Connection, topic_id: str, *, include_deprecated: bool = False
) -> list[dict[str, Any]]:
    source_locus_seed.ensure_schema(conn)
    clauses = ["topic_id=?"]
    params: list[Any] = [topic_id]
    if not include_deprecated:
        clauses.append("is_deprecated=0")
    rows = conn.execute(
        f"""
        SELECT *
        FROM source_locus
        WHERE {" AND ".join(clauses)}
        ORDER BY topic_id, is_deprecated, review_state, query_family, display_name, locus_id
        """,
        params,
    ).fetchall()
    return [source_locus_seed.db_row_to_record(row) for row in rows]


def create_plans_from_loci(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    generated_at: str,
    generated_by: str,
    include_deprecated: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    ensure_schema(conn)
    loci = load_source_loci(conn, topic_id, include_deprecated=include_deprecated)
    plans = [
        plan_from_locus(record, generated_at=generated_at, generated_by=generated_by)
        for record in loci
    ]
    for plan in plans:
        validate_plan(plan)
        if write:
            upsert_query_plan(conn, plan, updated_at=generated_at)
    if write:
        conn.commit()
    by_status: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for plan in plans:
        by_status[str(plan["plan_status"])] = by_status.get(str(plan["plan_status"]), 0) + 1
        by_family[str(plan["query_family"])] = by_family.get(str(plan["query_family"]), 0) + 1
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "operation": "create-plans",
        "topic_id": topic_id,
        "is_simulated": True,
        "planning_only": True,
        "network_access_attempted": False,
        "source_loci_considered": len(loci),
        "query_plans_written": len(plans) if write else 0,
        "query_plans": plans,
        "counts": {
            "total_plans": len(plans),
            "by_status": by_status,
            "by_query_family": by_family,
        },
    }


def export_query_plans(conn: sqlite3.Connection, topic_id: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    params: tuple[Any, ...] = ()
    where = ""
    if topic_id:
        where = "WHERE topic_id=?"
        params = (topic_id,)
    rows = conn.execute(
        f"""
        SELECT *
        FROM source_query_plan
        {where}
        ORDER BY topic_id, plan_status, query_family, query_plan_id
        """,
        params,
    ).fetchall()
    plans = [row_to_plan(row) for row in rows]
    by_status: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for plan in plans:
        by_status[str(plan["plan_status"])] = by_status.get(str(plan["plan_status"]), 0) + 1
        by_family[str(plan["query_family"])] = by_family.get(str(plan["query_family"]), 0) + 1
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "topic_id": topic_id,
        "is_simulated": True,
        "planning_only": True,
        "network_access_attempted": False,
        "counts": {
            "total_plans": len(plans),
            "by_status": by_status,
            "by_query_family": by_family,
        },
        "source_query_plans": plans,
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
    parser = argparse.ArgumentParser(
        description="Create planning-only source-query plans from source_locus records."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create", help="Create deterministic source_query_plan rows for a topic."
    )
    create.add_argument("--db", required=True, type=Path)
    create.add_argument("--topic-id", required=True)
    create.add_argument("--generated-at", default="2026-04-28T00:00:00+00:00")
    create.add_argument("--generated-by", default="codex_phase3b")
    create.add_argument("--include-deprecated", action="store_true")
    create.add_argument(
        "--dry-run", action="store_true", help="Build plans without writing source_query_plan rows."
    )
    create.add_argument("--report-json", type=Path)

    export = subparsers.add_parser("export", help="Export source_query_plan rows.")
    export.add_argument("--db", required=True, type=Path)
    export.add_argument("--topic-id")
    export.add_argument("--report-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(args.db)
        if args.command == "create":
            payload = create_plans_from_loci(
                conn,
                topic_id=args.topic_id,
                generated_at=args.generated_at,
                generated_by=args.generated_by,
                include_deprecated=args.include_deprecated,
                write=not args.dry_run,
            )
        elif args.command == "export":
            payload = export_query_plans(conn, args.topic_id)
        else:  # pragma: no cover - argparse prevents this.
            raise RuntimeError(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"source-query-plan error: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    write_json(args.report_json, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
