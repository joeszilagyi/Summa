#!/usr/bin/env python3
"""Operational review-state queue and transition helper for source.sqlite.

This script mutates review state in ``source.sqlite`` and records corresponding
append-only history and provenance rows for auditability.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import provenance_events
except ImportError:  # pragma: no cover - package import fallback
    from tools.source_db_tools import provenance_events  # type: ignore

REVIEW_NAMESPACE = uuid.UUID("2d2d4f0a-6b0c-443a-9e69-47fd4a830243")
SCRIPT_PATH = "tools/source_db_tools/review_queue.py"
DEFAULT_PENDING_STATES = {
    "",
    "unreviewed",
    "machine_extracted",
    "proposed",
    "needs_review",
    "ambiguous",
    "demoted",
}
ACCEPTED_STATES = {"accepted", "approved", "curated", "reviewed"}
TRANSITION_STATES = {"accepted", "rejected", "demoted", "ambiguous"}
PUBLICATION_BLOCKING_STATES = {"blocked", "draft", "local_only", "private_working"}
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ReviewTarget:
    namespace: str
    table: str
    pk_column: str
    state_column: str = "review_state"
    confidence_column: str | None = "confidence_score"
    label_sql: str = "NULL"
    source_type_sql: str = "NULL"
    extra_where: str | None = None


TARGETS: dict[str, ReviewTarget] = {
    "lead": ReviewTarget(
        "lead",
        "lead",
        "lead_id",
        label_sql="label_text",
        source_type_sql="lead_kind",
        confidence_column=None,
    ),
    "work": ReviewTarget("work", "work", "work_id", label_sql="title", source_type_sql="work_type"),
    "work_identifier": ReviewTarget(
        "work_identifier",
        "work_identifier",
        "work_identifier_id",
        label_sql="scheme || ':' || value",
    ),
    "authority_identifier": ReviewTarget(
        "authority_identifier",
        "authority_identifier",
        "authority_identifier_id",
        label_sql="scheme || ':' || value",
    ),
    "authority": ReviewTarget(
        "authority",
        "authority_record",
        "authority_record_id",
        label_sql="preferred_label",
        source_type_sql="authority_type",
    ),
    "work_subject": ReviewTarget(
        "work_subject",
        "work_subject",
        "work_subject_id",
        label_sql="COALESCE(source_note, subject_role)",
    ),
    "highlight": ReviewTarget(
        "highlight",
        "extraction_highlight",
        "highlight_id",
        label_sql="substr(text_excerpt, 1, 120)",
    ),
    "detected_entity": ReviewTarget(
        "detected_entity",
        "extraction_detected_entity",
        "detected_entity_id",
        label_sql="entity_label",
        source_type_sql="entity_type",
    ),
    "relationship": ReviewTarget(
        "relationship",
        "source_relationship",
        "source_relationship_id",
        label_sql="predicate || ' -> ' || COALESCE(target_label, to_object_ref, '')",
    ),
    "claim": ReviewTarget(
        "claim",
        "source_claim",
        "source_claim_id",
        label_sql="substr(claim_text, 1, 120)",
        source_type_sql="claim_type",
    ),
    "topic_extension": ReviewTarget(
        "topic_extension",
        "topic_extension",
        "topic_extension_id",
        label_sql="topic_id || ':' || extension_type",
    ),
    "source_access": ReviewTarget(
        "source_access",
        "source_access",
        "source_access_id",
        label_sql="original_locator",
        confidence_column=None,
    ),
    "capture_event": ReviewTarget(
        "capture_event",
        "capture_event",
        "capture_event_id",
        label_sql="capture_method",
        confidence_column=None,
    ),
    "extraction_record": ReviewTarget(
        "extraction_record", "extraction_record", "extraction_id", label_sql="summary_short"
    ),
}

TYPE_ALIASES: dict[str, list[str]] = {
    "leads": ["lead"],
    "source_lead": ["lead"],
    "source_candidates": ["lead"],
    "works": ["work"],
    "identifiers": ["work_identifier", "authority_identifier"],
    "identifier": ["work_identifier", "authority_identifier"],
    "authorities": ["authority"],
    "authority_record": ["authority"],
    "subject": ["work_subject"],
    "subject_assignment": ["work_subject"],
    "subject_assignments": ["work_subject"],
    "subjects": ["work_subject"],
    "highlights": ["highlight"],
    "detected_entities": ["detected_entity"],
    "entity": ["detected_entity"],
    "entities": ["detected_entity"],
    "relationships": ["relationship"],
    "source_relationship": ["relationship"],
    "claims": ["claim"],
    "source_claim": ["claim"],
    "topic_extensions": ["topic_extension"],
    "source_access_records": ["source_access"],
    "retention_override": ["work", "source_access", "capture_event", "extraction_record"],
    "retention_overrides": ["work", "source_access", "capture_event", "extraction_record"],
}


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def review_key(*parts: Any) -> str:
    seed = "|".join("" if part is None else str(part) for part in parts)
    return "review:" + str(uuid.uuid5(REVIEW_NAMESPACE, seed))


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"review database does not exist: {db_path}")
    if not db_path.is_file():
        raise ValueError(f"review database path is not a file: {db_path}")
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
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def canonical_type(type_name: str) -> str:
    normalized = type_name.strip().lower().replace("-", "_")
    if normalized in TARGETS:
        return normalized
    matches = TYPE_ALIASES.get(normalized)
    if matches and len(matches) == 1:
        return matches[0]
    raise ValueError(f"unsupported review object type: {type_name}")


def expand_object_type(type_name: str | None) -> list[str]:
    if not type_name:
        return list(TARGETS)
    normalized = type_name.strip().lower().replace("-", "_")
    if normalized in TARGETS:
        return [normalized]
    if normalized in TYPE_ALIASES:
        return TYPE_ALIASES[normalized]
    raise ValueError(f"unsupported review object type: {type_name}")


def parse_object_ref(object_ref: str) -> tuple[str, int]:
    if ":" not in object_ref:
        raise ValueError("OBJECT_ID must be '<object_type>:<numeric_id>', for example work:1")
    type_part, id_part = object_ref.rsplit(":", 1)
    target_type = canonical_type(type_part)
    try:
        object_pk = int(id_part)
    except ValueError as exc:
        raise ValueError(f"OBJECT_ID has non-numeric id: {object_ref}") from exc
    return target_type, object_pk


def pending_filter_sql(state: str | None) -> tuple[str, list[Any]]:
    if state is None:
        placeholders = ",".join("?" for _ in ACCEPTED_STATES)
        return f"COALESCE(review_state, '') NOT IN ({placeholders})", list(ACCEPTED_STATES)
    if state == "all":
        return "1=1", []
    return "COALESCE(review_state, '') = ?", [state]


def optional_column_expr(columns: set[str], *candidate_names: str) -> str:
    for name in candidate_names:
        if name in columns:
            return name
    return "NULL"


def public_blocker_expr(columns: set[str]) -> str:
    if "public_blocker" in columns:
        return "NULLIF(public_blocker, '')"
    if "public_blocked" in columns:
        return "CASE WHEN COALESCE(public_blocked, 0) THEN 'blocked' ELSE NULL END"
    if "publication_state" in columns:
        states = ", ".join(f"'{value}'" for value in sorted(PUBLICATION_BLOCKING_STATES))
        return f"CASE WHEN publication_state IN ({states}) THEN publication_state ELSE NULL END"
    return "NULL"


def apply_optional_filter(
    where: str, params: list[Any], expr: str, value: str | None
) -> tuple[str, list[Any]]:
    if value is None:
        return where, params
    if expr == "NULL":
        return "0=1", params
    if value == "any":
        return f"({where}) AND {expr} IS NOT NULL", params
    if value == "none":
        return f"({where}) AND {expr} IS NULL", params
    params.append(value)
    return f"({where}) AND {expr} = ?", params


def list_review_items(
    conn: sqlite3.Connection,
    *,
    object_type: str | None = None,
    state: str | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    source_type: str | None = None,
    workspace_id: str | None = None,
    authority_level: str | None = None,
    public_blocker: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    rows: list[dict[str, Any]] = []
    normalized_object_type = (
        "" if object_type is None else object_type.strip().lower().replace("-", "_")
    )
    retention_only = normalized_object_type in {"retention_override", "retention_overrides"}
    for key in expand_object_type(object_type):
        target = TARGETS[key]
        if not table_exists(conn, target.table):
            continue
        columns = table_columns(conn, target.table)
        if target.state_column not in columns:
            continue
        if retention_only and "retention_policy_id" not in columns:
            continue
        confidence_expr = (
            target.confidence_column if target.confidence_column in columns else "NULL"
        )
        source_type_expr = target.source_type_sql
        workspace_expr = optional_column_expr(columns, "workspace_id")
        authority_level_expr = optional_column_expr(
            columns, "authority_level", "authority_tier", "authority_status"
        )
        public_blocker_value_expr = public_blocker_expr(columns)
        where, params = pending_filter_sql(state)
        if target.extra_where:
            where = f"({where}) AND ({target.extra_where})"
        if retention_only:
            where = f"({where}) AND retention_policy_id IS NOT NULL"
        if min_confidence is not None and confidence_expr != "NULL":
            where = f"({where}) AND {confidence_expr} >= ?"
            params.append(min_confidence)
        if max_confidence is not None and confidence_expr != "NULL":
            where = f"({where}) AND {confidence_expr} <= ?"
            params.append(max_confidence)
        if source_type is not None and source_type_expr != "NULL":
            where = f"({where}) AND {source_type_expr} = ?"
            params.append(source_type)
        where, params = apply_optional_filter(where, params, workspace_expr, workspace_id)
        where, params = apply_optional_filter(where, params, authority_level_expr, authority_level)
        where, params = apply_optional_filter(
            where, params, public_blocker_value_expr, public_blocker
        )
        query = f"""
            SELECT
              ? AS object_type,
              ? AS object_namespace,
              {target.pk_column} AS object_pk,
              COALESCE({target.state_column}, '') AS review_state,
              {confidence_expr} AS confidence_score,
              {target.label_sql} AS label,
              {source_type_expr} AS source_type,
              {workspace_expr} AS workspace_id,
              {authority_level_expr} AS authority_level,
              {public_blocker_value_expr} AS public_blocker
            FROM {target.table}
            WHERE {where}
            ORDER BY COALESCE({confidence_expr}, 2.0), {target.pk_column}
        """
        for row in conn.execute(query, (key, target.namespace, *params)).fetchall():
            item = dict(row)
            item["object_ref"] = f"{item['object_type']}:{item['object_pk']}"
            rows.append(item)
    rows.sort(
        key=lambda row: (
            row.get("confidence_score") is None,
            row.get("confidence_score") or 2.0,
            row["object_type"],
            row["object_pk"],
        )
    )
    return rows[:limit] if limit is not None else rows


def fetch_review_object(
    conn: sqlite3.Connection,
    object_ref: str,
    *,
    full_row: bool = False,
) -> dict[str, Any]:
    target_type, object_pk = parse_object_ref(object_ref)
    target = TARGETS[target_type]
    if not SQL_IDENTIFIER_RE.fullmatch(target.table):
        raise ValueError(f"invalid review target table: {target.table}")
    if not SQL_IDENTIFIER_RE.fullmatch(target.pk_column):
        raise ValueError(f"invalid review target primary key column: {target.pk_column}")
    if not table_exists(conn, target.table):
        raise ValueError(f"review target table does not exist: {target.table}")
    if full_row:
        row = conn.execute(
            f"SELECT * FROM {target.table} WHERE {target.pk_column}=?",
            (object_pk,),
        ).fetchone()
    else:
        columns = table_columns(conn, target.table)
        confidence_expr = (
            target.confidence_column if target.confidence_column in columns else "NULL"
        )
        source_type_expr = target.source_type_sql
        workspace_expr = optional_column_expr(columns, "workspace_id")
        authority_level_expr = optional_column_expr(
            columns, "authority_level", "authority_tier", "authority_status"
        )
        public_blocker_value_expr = public_blocker_expr(columns)
        row = conn.execute(
            f"""
            SELECT
              ? AS object_type,
              ? AS object_namespace,
              {target.pk_column} AS object_pk,
              COALESCE({target.state_column}, '') AS review_state,
              {confidence_expr} AS confidence_score,
              {target.label_sql} AS label,
              {source_type_expr} AS source_type,
              {workspace_expr} AS workspace_id,
              {authority_level_expr} AS authority_level,
              {public_blocker_value_expr} AS public_blocker
            FROM {target.table}
            WHERE {target.pk_column}=?
            """,
            (target_type, target.namespace, object_pk),
        ).fetchone()
    if row is None:
        raise ValueError(f"review object not found: {object_ref}")
    result = dict(row)
    result["object_type"] = target_type
    result["object_ref"] = f"{target_type}:{object_pk}"
    return result


def record_review_history(
    conn: sqlite3.Connection,
    *,
    target: ReviewTarget,
    object_pk: int,
    previous_state: str | None,
    new_state: str,
    changed_by: str,
    changed_at: str,
    reason: str | None,
    note: str | None,
    source_run_id: str | None,
) -> None:
    key = review_key(target.namespace, object_pk, previous_state, new_state, changed_by, changed_at)
    conn.execute(
        """
        INSERT INTO review_state_history (
          review_state_history_key_v1, target_namespace, target_id,
          previous_state, new_state, changed_by, changed_at, reason, note,
          source_namespace, source_id, source_tool, source_run_id,
          record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            target.namespace,
            str(object_pk),
            previous_state,
            new_state,
            changed_by,
            changed_at,
            reason,
            note,
            "review_queue",
            f"{target.namespace}:{object_pk}",
            SCRIPT_PATH,
            source_run_id,
            changed_at,
        ),
    )


def promotion_state_for_review(new_state: str) -> str:
    if new_state == "accepted":
        return "accepted_for_citation"
    return new_state


def review_outcome_update_sql(
    columns: set[str],
    *,
    new_state: str,
    changed_by: str,
    changed_at: str,
) -> tuple[list[str], list[Any]]:
    assignments = ["review_state=?", "record_last_updated=?"]
    params: list[Any] = [new_state, changed_at]
    if "reviewed_by" in columns:
        assignments.append("reviewed_by=?")
        params.append(changed_by)
    if "reviewed_at" in columns:
        assignments.append("reviewed_at=?")
        params.append(changed_at)
    if "accepted_for_citation" in columns:
        assignments.append("accepted_for_citation=MAX(COALESCE(accepted_for_citation, 0), ?)")
        params.append(1 if new_state == "accepted" else 0)
    if "promotion_state" in columns:
        assignments.append("promotion_state=?")
        params.append(promotion_state_for_review(new_state))
    return assignments, params


def change_review_state(
    conn: sqlite3.Connection,
    object_ref: str,
    *,
    new_state: str,
    changed_by: str = "operator",
    reason: str | None = None,
    note: str | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    target_type, object_pk = parse_object_ref(object_ref)
    target = TARGETS[target_type]
    if not SQL_IDENTIFIER_RE.fullmatch(target.table):
        raise ValueError(f"invalid review target table: {target.table}")
    if not SQL_IDENTIFIER_RE.fullmatch(target.pk_column):
        raise ValueError(f"invalid review target primary key column: {target.pk_column}")
    if not SQL_IDENTIFIER_RE.fullmatch(target.state_column):
        raise ValueError(f"invalid review target state column: {target.state_column}")
    if not table_exists(conn, target.table):
        raise ValueError(f"review target table does not exist: {target.table}")
    if new_state not in TRANSITION_STATES:
        raise ValueError(f"unsupported review state transition: {new_state}")
    row = conn.execute(
        f"SELECT {target.state_column} FROM {target.table} WHERE {target.pk_column}=?",
        (object_pk,),
    ).fetchone()
    if row is None:
        raise ValueError(f"review object not found: {object_ref}")
    previous_state = row[target.state_column]
    changed_at = now_iso()
    if dry_run:
        return {
            "object_ref": f"{target_type}:{object_pk}",
            "object_namespace": target.namespace,
            "object_id": str(object_pk),
            "previous_state": previous_state,
            "new_state": new_state,
            "changed_by": changed_by,
            "changed_at": changed_at,
            "dry_run": True,
        }

    columns = table_columns(conn, target.table)
    assignments, params = review_outcome_update_sql(
        columns,
        new_state=new_state,
        changed_by=changed_by,
        changed_at=changed_at,
    )
    params.append(object_pk)
    conn.execute(
        f"UPDATE {target.table} SET {', '.join(assignments)} WHERE {target.pk_column}=?",
        params,
    )
    try:
        record_review_history(
            conn,
            target=target,
            object_pk=object_pk,
            previous_state=previous_state,
            new_state=new_state,
            changed_by=changed_by,
            changed_at=changed_at,
            reason=reason,
            note=note,
            source_run_id=run_id,
        )
        event_type = "demoted" if new_state == "demoted" else "reviewed"
        provenance_events.record_event(
            conn,
            object_namespace=target.namespace,
            object_id=object_pk,
            event_type=event_type,
            actor_type="human",
            actor_id=changed_by,
            tool_name=SCRIPT_PATH,
            run_id=run_id,
            event_timestamp=changed_at,
            note_text=note or reason or f"review state changed to {new_state}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "object_ref": f"{target_type}:{object_pk}",
        "object_namespace": target.namespace,
        "object_id": str(object_pk),
        "previous_state": previous_state,
        "new_state": new_state,
        "changed_by": changed_by,
        "changed_at": changed_at,
        "dry_run": False,
    }


def render_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def render_list_text(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        confidence = (
            "" if row.get("confidence_score") is None else f"{float(row['confidence_score']):.2f}"
        )
        source_type = row.get("source_type") or ""
        label = row.get("label") or ""
        workspace_id = row.get("workspace_id") or ""
        authority_level = row.get("authority_level") or ""
        public_blocker = row.get("public_blocker") or ""
        print(
            f"{row['object_ref']}\t{row['review_state']}\t{confidence}\t{source_type}\t"
            f"{workspace_id}\t{authority_level}\t{public_blocker}\t{label}"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List and update review_state objects in source.sqlite."
    )
    parser.add_argument("db", type=Path, help="Path to source.sqlite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned state changes without writing to the database",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List reviewable objects")
    list_p.add_argument(
        "--state",
        help="Review state to filter; omit for non-accepted pending objects, or use 'all'",
    )
    list_p.add_argument(
        "--object-type",
        help="Object type such as work, authority, claim, source_access, retention_override",
    )
    list_p.add_argument("--min-confidence", type=float)
    list_p.add_argument("--max-confidence", type=float)
    list_p.add_argument("--source-type")
    list_p.add_argument(
        "--workspace-id", help="Exact workspace_id match for targets that carry workspace metadata."
    )
    list_p.add_argument(
        "--authority-level",
        help="Exact authority level/tier match for targets that carry authority metadata.",
    )
    list_p.add_argument(
        "--public-blocker",
        help="Filter by public blocker reason, or use 'any' / 'none'.",
    )
    list_p.add_argument("--limit", type=int)
    list_p.add_argument("--format", choices=["text", "json"], default="text")

    show_p = sub.add_parser("show", help="Show one review object")
    show_p.add_argument("object_id")
    show_p.add_argument(
        "--full",
        action="store_true",
        help="Show the full raw row instead of the lightweight projection.",
    )
    show_p.add_argument("--format", choices=["text", "json"], default="json")

    for command, state in (
        ("accept", "accepted"),
        ("reject", "rejected"),
        ("demote", "demoted"),
        ("mark-ambiguous", "ambiguous"),
    ):
        action_p = sub.add_parser(command, help=f"Mark one review object {state}")
        action_p.add_argument("object_id")
        action_p.add_argument("--changed-by", default="operator")
        action_p.add_argument("--reason")
        action_p.add_argument("--note")
        action_p.add_argument("--run-id")
        action_p.add_argument("--format", choices=["text", "json"], default="json")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        conn = connect(args.db)
        try:
            if args.command == "list":
                rows = list_review_items(
                    conn,
                    object_type=args.object_type,
                    state=args.state,
                    min_confidence=args.min_confidence,
                    max_confidence=args.max_confidence,
                    source_type=args.source_type,
                    workspace_id=args.workspace_id,
                    authority_level=args.authority_level,
                    public_blocker=args.public_blocker,
                    limit=args.limit,
                )
                if args.format == "json":
                    render_json({"items": rows, "count": len(rows)})
                else:
                    render_list_text(rows)
                return 0
            if args.command == "show":
                row = fetch_review_object(conn, args.object_id, full_row=args.full)
                if args.format == "json":
                    render_json(row)
                else:
                    print(json.dumps(row, sort_keys=True))
                return 0
            state_by_command = {
                "accept": "accepted",
                "reject": "rejected",
                "demote": "demoted",
                "mark-ambiguous": "ambiguous",
            }
            result = change_review_state(
                conn,
                args.object_id,
                new_state=state_by_command[args.command],
                changed_by=args.changed_by,
                reason=args.reason,
                note=args.note,
                run_id=args.run_id,
                dry_run=args.dry_run,
            )
            if args.format == "json":
                render_json(result)
            else:
                suffix = " (dry-run)" if args.dry_run else ""
                print(
                    f"{result['object_ref']}: {result['previous_state']} -> {result['new_state']}{suffix}"
                )
            return 0
        finally:
            conn.close()
    except ValueError as exc:
        print(f"review error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"review file error: {exc}", file=sys.stderr)
        return 3
    except sqlite3.DatabaseError as exc:
        print(f"review database error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"review unexpected error: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
