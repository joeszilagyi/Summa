#!/usr/bin/env python3
"""Idempotent legacy entity/lead backfill into durable source/work tables.

This migrates legacy lead/entity rows into the durable schema with conservative
mapping rules and without deleting legacy rows. Use --dry-run for a safe
preview; when enabled, all writes are rolled back.
"""
# Documentation: docs/tools/source_db_tools/legacy_backfill.md
# Keep the paired documentation in sync when changing behavior or CLI options.

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import identifier_normalization
import canonical_store
import source_types

REPORT_SCHEMA_VERSION = "legacy-backfill-report.v1"
SCRIPT_PATH = "tools/source_db_tools/legacy_backfill.py"
DEFAULT_LEGACY_LEAD_CONFIDENCE = 0.45
DEFAULT_LEGACY_ENTITY_CONFIDENCE = 0.35
IDENTIFIER_VALID_SCORE = 1.0
IDENTIFIER_INVALID_SCORE = 0.2
URL_RE = re.compile(r"https?://[^\s\]\)>\"]+")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists() or not db_path.is_file():
        raise RuntimeError(f"db not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def infer_work_type(*texts: str | None) -> tuple[str, list[str]]:
    joined = " ".join(text.lower() for text in texts if text)
    if "http://" in joined or "https://" in joined:
        return "webpage", []
    if "isbn" in joined:
        return "book", []
    if "doi" in joined or "journal" in joined:
        return "journal_article", []
    return "local:legacy_record", ["source_type"]


def review_state_from_legacy(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"rejected", "demoted"}:
        return normalized
    return "needs_review"


def first_url(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = URL_RE.search(text)
        if match:
            return match.group(0).rstrip(".,;")
    return None


def insert_or_get_work(
    conn: sqlite3.Connection,
    *,
    provenance_event_ref: str,
    work_key: str,
    work_type: str,
    title: str,
    raw_cite_text: str | None,
    review_state: str,
    confidence_score: float,
    timestamp: str,
) -> tuple[int, bool]:
    result = canonical_store.upsert_work(
        conn,
        work_key_v1=work_key,
        provenance_event_ref=provenance_event_ref,
        work_type=work_type,
        title=title,
        rights_posture="unknown",
        refetchability_status="unknown",
        review_state=review_state,
        confidence_score=confidence_score,
        raw_cite_text=raw_cite_text,
        first_seen_at=timestamp,
        last_seen_at=timestamp,
        created_at=timestamp,
        record_last_updated=timestamp,
    )
    return result.row_id, result.created


def insert_metadata(
    conn: sqlite3.Connection,
    *,
    work_id: int,
    values: dict[str, Any],
    timestamp: str,
) -> None:
    for key, value in values.items():
        if value is None or value == "":
            continue
        conn.execute(
            """
            INSERT INTO work_metadata (
              work_id, meta_key, meta_value, meta_type, first_seen_at,
              last_seen_at, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id, meta_key, meta_value) DO UPDATE SET
              first_seen_at=MIN(COALESCE(first_seen_at, excluded.first_seen_at), excluded.first_seen_at),
              last_seen_at=MAX(COALESCE(last_seen_at, excluded.last_seen_at), excluded.last_seen_at),
              record_last_updated=MAX(record_last_updated, excluded.record_last_updated)
            """,
            (
                work_id,
                key,
                json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value),
                "json" if isinstance(value, (dict, list)) else "text",
                timestamp,
                timestamp,
                timestamp,
            ),
        )


def insert_local_identifier(conn: sqlite3.Connection, *, work_id: int, value: str, timestamp: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO work_identifier (
          work_id, scheme, value, raw_value, normalized_value, normalized_uri,
          validity_status, validation_warning, is_primary, confidence_score,
          review_state, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (work_id, "local", value, value, value, None, "valid", None, 1, 1.0, "accepted", timestamp),
    )


def insert_identifier(
    conn: sqlite3.Connection,
    *,
    work_id: int,
    scheme: str,
    value: str,
    timestamp: str,
) -> str:
    normalized = identifier_normalization.normalize_identifier_row({"scheme": scheme, "value": value})
    is_valid = normalized["validity_status"] == "valid"
    conn.execute(
        """
        INSERT OR IGNORE INTO work_identifier (
          work_id, scheme, value, raw_value, normalized_value, normalized_uri,
          validity_status, validation_warning, is_primary, confidence_score,
          review_state, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            work_id,
            normalized["scheme"] or scheme,
            value,
            normalized["raw_value"],
            normalized["normalized_value"],
            normalized["normalized_uri"],
            normalized["validity_status"],
            normalized["validation_warning"],
            0,
            IDENTIFIER_VALID_SCORE if is_valid else IDENTIFIER_INVALID_SCORE,
            "accepted" if is_valid else "needs_review",
            timestamp,
        ),
    )
    return str(normalized["validity_status"])


def insert_source_access(
    conn: sqlite3.Connection,
    *,
    provenance_event_ref: str,
    work_id: int,
    locator: str,
    url: str | None,
    timestamp: str,
) -> None:
    canonical_store.record_source_access(
        conn,
        provenance_event_ref=provenance_event_ref,
        work_id=work_id,
        original_locator=locator,
        canonical_url=url,
        access_class="unknown",
        refetchability_status="unknown",
        rights_posture="unknown",
        citation_hint="legacy backfill",
        first_seen_at=timestamp,
        last_seen_at=timestamp,
        record_last_updated=timestamp,
    )
    if url:
        conn.execute(
            """
            INSERT INTO work_url (
              work_id, url, url_role, url_status, refetchability_status,
              preferred_refetch_method, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id, url) DO UPDATE SET
              record_last_updated=MAX(record_last_updated, excluded.record_last_updated)
            """,
            (work_id, url, "legacy_observed", "unknown", "unknown", "http_refetch", timestamp),
        )


def insert_no_capture_event(
    conn: sqlite3.Connection,
    *,
    provenance_event_ref: str,
    work_id: int,
    timestamp: str,
) -> None:
    locator_row = conn.execute(
        "SELECT original_locator FROM source_access WHERE work_id=? ORDER BY source_access_id LIMIT 1",
        (work_id,),
    ).fetchone()
    locator = (
        str(locator_row["original_locator"])
        if locator_row is not None and locator_row["original_locator"]
        else f"legacy backfill placeholder for work:{work_id}"
    )
    canonical_store.record_capture_event(
        conn,
        provenance_event_ref=provenance_event_ref,
        work_id=work_id,
        original_locator=locator,
        captured_at=timestamp,
        capture_method="legacy_backfill_no_capture",
        byte_retention_status="not_applicable",
        refetchability_status="unknown",
        quality_warnings_json=["legacy record had no capture payload"],
        record_last_updated=timestamp,
    )


def lead_identifier_rows(conn: sqlite3.Connection, lead_id: int, *, has_identifier_metadata: bool) -> list[tuple[str, str]]:
    if not has_identifier_metadata:
        return []
    rows = []
    for row in conn.execute(
        "SELECT meta_key, meta_value FROM lead_metadata WHERE lead_id=? AND meta_key LIKE 'identifier:%'",
        (lead_id,),
    ).fetchall():
        scheme = str(row["meta_key"]).split(":", 1)[1]
        if row["meta_value"]:
            rows.append((scheme, str(row["meta_value"])))
    return rows


def backfill_leads(
    conn: sqlite3.Connection,
    report: dict[str, Any],
    timestamp: str,
    *,
    has_identifier_metadata: bool,
    provenance_event_ref: str,
) -> None:
    if not table_exists(conn, "lead"):
        return
    for row in conn.execute("SELECT * FROM lead ORDER BY lead_id"):
        report["records_scanned"] += 1
        label = normalize_text(row["label_text"]) or f"legacy lead {row['lead_id']}"
        url = first_url(row["label_text"], row["note_text"])
        work_type, unresolved = infer_work_type(row["lead_kind"], row["label_text"], row["note_text"])
        review_state = review_state_from_legacy(row["lead_status"])
        work_id, created = insert_or_get_work(
            conn,
            provenance_event_ref=provenance_event_ref,
            work_key=f"work:legacy-lead:{row['lead_id']}",
            work_type=work_type,
            title=label,
            raw_cite_text=row["note_text"],
            review_state=review_state,
            confidence_score=DEFAULT_LEGACY_LEAD_CONFIDENCE,
            timestamp=timestamp,
        )
        insert_local_identifier(conn, work_id=work_id, value=f"lead:{row['lead_id']}", timestamp=timestamp)
        invalid_identifier = False
        for scheme, value in lead_identifier_rows(conn, int(row["lead_id"]), has_identifier_metadata=has_identifier_metadata):
            if insert_identifier(conn, work_id=work_id, scheme=scheme, value=value, timestamp=timestamp) == "invalid":
                invalid_identifier = True
        insert_source_access(
            conn,
            provenance_event_ref=provenance_event_ref,
            work_id=work_id,
            locator=row["note_text"] or row["label_text"] or f"legacy lead {row['lead_id']}",
            url=url,
            timestamp=timestamp,
        )
        insert_no_capture_event(
            conn,
            provenance_event_ref=provenance_event_ref,
            work_id=work_id,
            timestamp=timestamp,
        )
        insert_metadata(
            conn,
            work_id=work_id,
            values={
                "legacy_table": "lead",
                "legacy_lead_id": row["lead_id"],
                "legacy_lead_key_v1": row["lead_key_v1"],
                "legacy_facet": row["facet"],
                "legacy_lead_kind": row["lead_kind"],
                "legacy_lead_status": row["lead_status"],
                "legacy_note_text": row["note_text"],
                "legacy_target_canonical_id": row["target_canonical_id"],
            },
            timestamp=timestamp,
        )
        report["records_migrated" if created else "records_skipped"] += 1
        if not created:
            report["skipped"].append({"record": f"lead:{row['lead_id']}", "reason": "already_backfilled"})
        if unresolved or invalid_identifier or review_state == "needs_review":
            report["records_partially_migrated"] += 1
            report["records_requiring_review"] += 1
        for field in unresolved:
            report["fields_unresolved"][field] = report["fields_unresolved"].get(field, 0) + 1
        validation_issue = source_types.validation_issue(work_type)
        if validation_issue and validation_issue[0] == "PROVISIONAL_SOURCE_TYPE":
            report["records_with_provisional_source_type"] += 1
        if invalid_identifier:
            report["records_with_invalid_identifiers"] += 1
        report["records_with_missing_rights_refetchability"] += 1


def backfill_entities(
    conn: sqlite3.Connection,
    report: dict[str, Any],
    timestamp: str,
    *,
    can_link_entity_work: bool,
    provenance_event_ref: str,
) -> None:
    if not table_exists(conn, "entity"):
        return
    for row in conn.execute("SELECT * FROM entity ORDER BY entity_id"):
        report["records_scanned"] += 1
        label = normalize_text(row["canonical_label"]) or f"legacy entity {row['entity_id']}"
        work_type = "local:legacy_record"
        review_state = review_state_from_legacy(row["current_status"])
        work_id, created = insert_or_get_work(
            conn,
            provenance_event_ref=provenance_event_ref,
            work_key=f"work:legacy-entity:{row['entity_id']}",
            work_type=work_type,
            title=label,
            raw_cite_text=row["canonical_provenance"],
            review_state=review_state,
            confidence_score=DEFAULT_LEGACY_ENTITY_CONFIDENCE,
            timestamp=timestamp,
        )
        insert_local_identifier(conn, work_id=work_id, value=f"entity:{row['entity_id']}", timestamp=timestamp)
        insert_source_access(
            conn,
            provenance_event_ref=provenance_event_ref,
            work_id=work_id,
            locator=row["canonical_provenance"] or f"legacy entity {row['entity_id']}",
            url=first_url(row["canonical_provenance"], row["canonical_grounded_detail"]),
            timestamp=timestamp,
        )
        insert_no_capture_event(
            conn,
            provenance_event_ref=provenance_event_ref,
            work_id=work_id,
            timestamp=timestamp,
        )
        insert_metadata(
            conn,
            work_id=work_id,
            values={
                "legacy_table": "entity",
                "legacy_entity_id": row["entity_id"],
                "legacy_entity_key_v1": row["entity_key_v1"],
                "legacy_canonical_id": row["canonical_id"],
                "legacy_facet": row["facet"],
                "legacy_entity_kind": row["entity_kind"],
                "legacy_current_status": row["current_status"],
                "legacy_grounded_detail": row["canonical_grounded_detail"],
                "legacy_duplicate_check": row["canonical_duplicate_check"],
                "legacy_related_facets_json": row["related_facets_json"],
            },
            timestamp=timestamp,
        )
        if can_link_entity_work:
            conn.execute(
                """
                INSERT OR IGNORE INTO entity_work (
                  entity_id, work_id, link_kind, evidence_note, first_seen_at,
                  last_seen_at, record_last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["entity_id"],
                    work_id,
                    "legacy_backfill",
                    row["canonical_provenance"],
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        report["records_migrated" if created else "records_skipped"] += 1
        if not created:
            report["skipped"].append({"record": f"entity:{row['entity_id']}", "reason": "already_backfilled"})
        report["records_partially_migrated"] += 1
        report["records_requiring_review"] += 1
        report["fields_unresolved"]["source_type"] = report["fields_unresolved"].get("source_type", 0) + 1
        report["records_with_provisional_source_type"] += 1
        report["records_with_missing_rights_refetchability"] += 1


def build_empty_report(db_path: Path, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "db_path": str(db_path),
        "dry_run": dry_run,
        "records_scanned": 0,
        "records_migrated": 0,
        "records_partially_migrated": 0,
        "fields_unresolved": {},
        "records_requiring_review": 0,
        "records_skipped": 0,
        "skipped": [],
        "records_with_provisional_source_type": 0,
        "records_with_invalid_identifiers": 0,
        "records_with_missing_rights_refetchability": 0,
    }


def run_backfill(db_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    timestamp = now_iso()
    conn = connect(db_path)
    report = build_empty_report(db_path, dry_run)
    try:
        has_identifier_metadata = table_exists(conn, "lead_metadata")
        can_link_entity_work = table_exists(conn, "entity_work")
        if dry_run:
            conn.execute("BEGIN")
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="legacy_backfill",
            object_id=str(db_path.resolve()),
            event_type="legacy_backfill",
            tool_name=SCRIPT_PATH,
            run_id=f"legacy-backfill:{timestamp}",
            event_timestamp=timestamp,
            note_text=f"legacy backfill for {db_path.resolve()}",
            provenance_event_key_v1=f"prov:legacy-backfill:{db_path.resolve()}:{timestamp}",
        )
        backfill_leads(
            conn,
            report,
            timestamp,
            has_identifier_metadata=has_identifier_metadata,
            provenance_event_ref=provenance.event_key,
        )
        backfill_entities(
            conn,
            report,
            timestamp,
            can_link_entity_work=can_link_entity_work,
            provenance_event_ref=provenance.event_key,
        )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Legacy backfill report",
        f"db: {report['db_path']}",
        f"dry_run: {report['dry_run']}",
        f"records scanned: {report['records_scanned']}",
        f"records migrated: {report['records_migrated']}",
        f"records partially migrated: {report['records_partially_migrated']}",
        f"records requiring review: {report['records_requiring_review']}",
        f"records skipped: {report['records_skipped']}",
        f"records with provisional source type: {report['records_with_provisional_source_type']}",
        f"records with invalid identifiers: {report['records_with_invalid_identifiers']}",
        f"records with missing rights/refetchability: {report['records_with_missing_rights_refetchability']}",
        "fields unresolved:",
    ]
    for field, count in sorted(report["fields_unresolved"].items()):
        lines.append(f"  {field}: {count}")
    for row in report["skipped"]:
        lines.append(f"skipped {row['record']}: {row['reason']}")
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill legacy entity/lead records into durable source/work tables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Side effects:\n"
            "  Without --dry-run, inserts durable work/source rows into the selected\n"
            "  SQLite database. Legacy lead/entity rows are preserved.\n\n"
            "Examples:\n"
            "  python3 tools/source_db_tools/legacy_backfill.py path/to/source.sqlite --dry-run --format json\n"
            "  python3 tools/source_db_tools/legacy_backfill.py path/to/source.sqlite\n\n"
            "Documentation: docs/tools/source_db_tools/legacy_backfill.md"
        ),
    )
    parser.add_argument("db", type=Path, help="SQLite place database to backfill.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview inserts and report counts, then roll back all writes.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Report output format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report = run_backfill(args.db, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"legacy backfill error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"legacy backfill failed: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
