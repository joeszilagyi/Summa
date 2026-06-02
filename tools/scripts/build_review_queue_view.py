#!/usr/bin/env python3
"""Emit a read-only review queue view model from source.sqlite."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SQLITE_TOOL_DIR = REPO_ROOT / "tools" / "source_db_tools"
if str(SQLITE_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(SQLITE_TOOL_DIR))

import review_queue  # type: ignore  # noqa: E402


SCHEMA_VERSION = "review-queue.v1"
DEFAULT_LIMIT = 50


class ReviewQueueViewError(RuntimeError):
    """Raised when review queue view inputs cannot be read."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only review queue view model from source.sqlite."
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the source source.sqlite database.",
    )
    parser.add_argument(
        "--state",
        help=(
            "Review state to filter. Omit for all non-accepted pending objects, "
            "or use 'all' to include accepted/reviewed records."
        ),
    )
    parser.add_argument(
        "--object-type",
        help="Object type such as work, authority, claim, source_access, or retention_override.",
    )
    parser.add_argument("--min-confidence", type=float)
    parser.add_argument("--max-confidence", type=float)
    parser.add_argument("--source-type")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum number of queue items to return. Counts still cover the full filtered queue.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the generated review queue view.",
    )
    return parser.parse_args()


def resolve_db_path(raw_db: str) -> Path:
    db_path = Path(raw_db).expanduser()
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()
    if not db_path.exists():
        raise ReviewQueueViewError(f"review database not found: {db_path}")
    if not db_path.is_file():
        raise ReviewQueueViewError(f"review database is not a file: {db_path}")
    return db_path


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise ReviewQueueViewError(f"cannot open review database read-only: {db_path}: {exc}") from exc
    return conn


def validate_args(args: argparse.Namespace) -> None:
    if args.limit is not None and args.limit < 0:
        raise ReviewQueueViewError("limit must be non-negative")
    if (
        args.min_confidence is not None
        and args.max_confidence is not None
        and args.min_confidence > args.max_confidence
    ):
        raise ReviewQueueViewError("min-confidence cannot exceed max-confidence")


def count_key(value: Any) -> str:
    if value is None or value == "":
        return "(empty)"
    return str(value)


def count_by(rows: list[dict[str, Any]], field_name: str) -> dict[str, int]:
    counts = Counter(count_key(row.get(field_name)) for row in rows)
    return dict(sorted(counts.items()))


def normalize_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_ref": row.get("object_ref"),
        "object_type": row.get("object_type"),
        "object_namespace": row.get("object_namespace"),
        "object_pk": row.get("object_pk"),
        "review_state": row.get("review_state"),
        "confidence_score": row.get("confidence_score"),
        "source_type": row.get("source_type"),
        "label": row.get("label"),
    }


def build_review_queue_payload(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    db_path = resolve_db_path(args.db)
    conn = connect_read_only(db_path)
    try:
        all_rows = review_queue.list_review_items(
            conn,
            object_type=args.object_type,
            state=args.state,
            min_confidence=args.min_confidence,
            max_confidence=args.max_confidence,
            source_type=args.source_type,
            limit=None,
        )
    finally:
        conn.close()

    returned_rows = all_rows[: args.limit] if args.limit is not None else all_rows
    items = [normalize_item(row) for row in returned_rows]
    filters = {
        "object_type": args.object_type,
        "state": args.state if args.state is not None else "pending_non_accepted",
        "min_confidence": args.min_confidence,
        "max_confidence": args.max_confidence,
        "source_type": args.source_type,
        "limit": args.limit,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "database_path": str(db_path),
        "filters": filters,
        "counts": {
            "total_items": len(all_rows),
            "returned_items": len(items),
            "by_review_state": count_by(all_rows, "review_state"),
            "by_object_type": count_by(all_rows, "object_type"),
        },
        "truncated": len(items) < len(all_rows),
        "items": items,
    }


def text_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\n", " ").replace("\t", " ")


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"database_path={payload['database_path']}",
        f"state_filter={payload['filters']['state']}",
        f"object_type_filter={text_value(payload['filters']['object_type'])}",
        f"total_items={payload['counts']['total_items']}",
        f"returned_items={payload['counts']['returned_items']}",
        f"truncated={str(payload['truncated']).lower()}",
    ]
    for index, item in enumerate(payload["items"]):
        confidence = item.get("confidence_score")
        confidence_text = "-" if confidence is None else f"{float(confidence):.2f}"
        lines.append(f"item[{index}].object_ref={text_value(item['object_ref'])}")
        lines.append(f"item[{index}].review_state={text_value(item['review_state'])}")
        lines.append(f"item[{index}].confidence_score={confidence_text}")
        lines.append(f"item[{index}].source_type={text_value(item['source_type'])}")
        lines.append(f"item[{index}].label={text_value(item['label'])}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = build_review_queue_payload(args)
    except (ReviewQueueViewError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
