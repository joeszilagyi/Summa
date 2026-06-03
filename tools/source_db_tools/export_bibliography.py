"""Read-only canonical source/work export helpers for local schema validation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists() or not db_path.is_file():
        raise RuntimeError(f"db not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _decode_metadata_value(row: sqlite3.Row) -> Any:
    value = row["meta_value"]
    meta_type = row["meta_type"] if "meta_type" in row.keys() else None
    if meta_type == "json" and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _rows_for_work(conn: sqlite3.Connection, table: str, work_id: int) -> list[dict[str, Any]]:
    if not table_exists(conn, table):
        return []
    columns = _table_columns(conn, table)
    if "work_id" in columns:
        rows = conn.execute(f"SELECT * FROM {table} WHERE work_id=? ORDER BY 1", (work_id,)).fetchall()
    elif "about_object_ref" in columns:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE about_object_ref=? ORDER BY 1",
            (f"work:{work_id}",),
        ).fetchall()
    elif "from_object_ref" in columns or "to_object_ref" in columns:
        predicates: list[str] = []
        params: list[str] = []
        if "from_object_ref" in columns:
            predicates.append("from_object_ref=?")
            params.append(f"work:{work_id}")
        if "to_object_ref" in columns:
            predicates.append("to_object_ref=?")
            params.append(f"work:{work_id}")
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {' OR '.join(predicates)} ORDER BY 1",
            tuple(params),
        ).fetchall()
    else:
        return []
    return [dict(row) for row in rows]


def load_records(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not table_exists(conn, "work"):
        return []

    sql = "SELECT * FROM work ORDER BY work_id"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    work_rows = conn.execute(sql, params).fetchall()

    records: list[dict[str, Any]] = []
    for work_row in work_rows:
        work_id = int(work_row["work_id"])
        # Build work_metadata as both rows and a convenience object when available.
        metadata_rows = _rows_for_work(conn, "work_metadata", work_id)
        metadata_object = {}
        for row in metadata_rows:
            meta_key = row.get("meta_key")
            if isinstance(meta_key, str) and meta_key:
                meta_type = row.get("meta_type")
                value = row.get("meta_value")
                if meta_type == "json" and isinstance(value, str):
                    try:
                        metadata_object[meta_key] = json.loads(value)
                    except json.JSONDecodeError:
                        metadata_object[meta_key] = value
                else:
                    metadata_object[meta_key] = value

        record = {
            "work": dict(work_row),
            "work_identifiers": _rows_for_work(conn, "work_identifier", work_id),
            "authority_identifiers": _rows_for_work(conn, "authority_identifier", work_id),
            "source_access": _rows_for_work(conn, "source_access", work_id),
            "source_claims": _rows_for_work(conn, "source_claim", work_id),
            "source_relationships": _rows_for_work(conn, "source_relationship", work_id),
            "work_metadata_rows": metadata_rows,
        }
        if metadata_object:
            record["work_metadata"] = metadata_object
        records.append(record)
    return records
