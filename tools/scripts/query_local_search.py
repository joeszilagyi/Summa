#!/usr/bin/env python3
"""Run a safe plain-text query over a local search projection index."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from functools import lru_cache
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
    REPO_ROOT,
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.local_search_contract import (  # noqa: E402
    RESULTS_SCHEMA_VERSION,
    RESULT_CLASS_BY_OBJECT_TYPE,
    SEARCH_SCOPE_TO_OBJECT_TYPES,
)
from tools.scripts.build_local_search_projection import looks_like_private_path  # noqa: E402
from tools.validators.validate_local_search_results import EXIT_PASS as EXIT_VALIDATOR_PASS  # noqa: E402
from tools.validators.validate_local_search_results import validate_local_search_results  # noqa: E402


SCRIPT_PATH = "tools/scripts/query_local_search.py"
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+", re.ASCII)
COMPOSITE_RANK_PENALTY_SCALE = 0.95
CONFIDENCE_BONUS_SCALE = 0.75

AUTHORITY_SCORE_BY_LEVEL = {
    "primary": -0.35,
    "trusted": -0.20,
    "approved": -0.15,
    "secondary": -0.10,
}

REVIEW_STATE_BONUS = {
    "accepted": 0.0,
    "approved": -0.10,
    "curated": -0.05,
    "reviewed": -0.03,
}

NEGATIVE_REVIEW_STATE_PENALTY = {
    "rejected": 2.0,
    "deprecated": 2.0,
    "demoted": 2.0,
}


class SearchQueryError(RuntimeError):
    """Raised when search query inputs or outputs cannot be processed."""


@lru_cache(maxsize=1024)
def parse_indexed_fields_json(indexed_fields_json: str) -> tuple[dict[str, Any], ...]:
    try:
        parsed = json.loads(indexed_fields_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    indexed_fields: list[dict[str, Any]] = []
    for field in parsed:
        if isinstance(field, dict):
            indexed_fields.append(field)
    return tuple(indexed_fields)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a safe plain-text query over a local search projection index.")
    parser.add_argument("--index-db", required=True, help="Path to the local search projection SQLite index.")
    parser.add_argument("--query", required=True, help="Plain-text search query.")
    parser.add_argument(
        "--scope",
        choices=tuple(SEARCH_SCOPE_TO_OBJECT_TYPES),
        default="all",
        help="Optional result scope to constrain object families.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum number of results to return, capped at 100.")
    parser.add_argument("--offset", type=int, default=0, help="Offset into the ordered result set.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Stdout format for the emitted results payload.")
    parser.add_argument("--output-json", help="Optional JSON path for the emitted local-search-results payload.")
    parser.add_argument("--generated-at", help="Optional RFC3339 timestamp override for deterministic tests.")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_existing_file(raw_path: str) -> Path:
    path = resolve_path(raw_path)
    if not path.exists():
        raise SearchQueryError(f"input path does not exist: {path}")
    if not path.is_file():
        raise SearchQueryError(f"input path is not a file: {path}")
    return path


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_metadata(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
          projection_schema_version,
          source_database_name,
          source_database_fingerprint,
          source_schema_version,
          profile
        FROM projection_metadata
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise SearchQueryError("projection_metadata row missing; rebuild the local search index with build_local_search_projection.py")
    return row


def normalize_plain_query(raw_query: str) -> tuple[str, list[str], str]:
    normalized_query = " ".join(raw_query.split())
    if not normalized_query:
        raise SearchQueryError("query must contain at least one non-whitespace character")
    terms = [match.group(0).lower() for match in TOKEN_PATTERN.finditer(normalized_query)]
    if not terms:
        raise SearchQueryError("query does not contain any searchable alphanumeric terms")
    fts_query = " AND ".join(f'"{term}"' for term in terms)
    return normalized_query, terms, fts_query


def build_scope_clause(scope: str) -> tuple[str, list[str]]:
    if scope == "all":
        return "", []
    object_types = sorted(SEARCH_SCOPE_TO_OBJECT_TYPES[scope])
    placeholders = ", ".join("?" for _ in object_types)
    return f" AND sp.object_type IN ({placeholders})", object_types


def sql_score_expression() -> str:
    return (
        "(bm25(search_projection_fts) * 0.95"
        " + CASE lower(COALESCE(sp.authority_level, ''))"
        "     WHEN 'primary' THEN -0.35"
        "     WHEN 'trusted' THEN -0.20"
        "     WHEN 'approved' THEN -0.15"
        "     WHEN 'secondary' THEN -0.10"
        "     ELSE 0.0"
        "   END"
        " + CASE lower(COALESCE(sp.review_state, ''))"
        "     WHEN 'approved' THEN -0.10"
        "     WHEN 'curated' THEN -0.05"
        "     WHEN 'reviewed' THEN -0.03"
        "     ELSE 0.0"
        "   END"
        " + CASE lower(COALESCE(sp.review_state, ''))"
        "     WHEN 'rejected' THEN 2.0"
        "     WHEN 'deprecated' THEN 2.0"
        "     WHEN 'demoted' THEN 2.0"
        "     ELSE 0.0"
        "   END"
        " - ("
        "     CASE"
        "       WHEN sp.confidence_score IS NULL THEN 0.0"
        "       WHEN CAST(sp.confidence_score AS REAL) != CAST(sp.confidence_score AS REAL) THEN 0.0"
        "       WHEN CAST(sp.confidence_score AS REAL) < 0 THEN 0.0"
        "       WHEN CAST(sp.confidence_score AS REAL) > 1.0 THEN 1.0"
        "       ELSE CAST(sp.confidence_score AS REAL)"
        "     END"
        "     * 0.75"
        "   )"
        ")"
    )


def load_matching_rows(
    conn: sqlite3.Connection,
    *,
    fts_query: str,
    scope: str,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    scope_clause, scope_params = build_scope_clause(scope)
    fts_scope_clause = scope_clause.replace("sp.", "")
    score_expression = sql_score_expression().replace(
        "bm25(search_projection_fts)",
        "matches.score",
    )
    rows: list[sqlite3.Row] = conn.execute(
        f"""
        SELECT
          sp.*,
          matches.score,
          matches.match_snippet,
            (
            SELECT COUNT(*)
            FROM search_projection_fts
            JOIN search_projection AS sf_match USING (projection_id)
            WHERE search_projection_fts MATCH ?{scope_clause}
          ) AS total_matches
        FROM (
          SELECT
            projection_id,
            bm25(search_projection_fts) AS score,
            snippet(search_projection_fts, 4, '', '', '...', 120) AS match_snippet
          FROM search_projection_fts
          WHERE search_projection_fts MATCH ?{fts_scope_clause}
        ) AS matches
        JOIN search_projection AS sp USING (projection_id)
        ORDER BY {score_expression} ASC, sp.object_type ASC, sp.object_pk ASC
        LIMIT ? OFFSET ?
        """,
        [fts_query, *scope_params, fts_query, *scope_params, limit, offset],
    ).fetchall()
    total = 0 if not rows else int(rows[0]["total_matches"])
    return rows, total


def render_snippet_text(text: str, terms: list[str], *, max_length: int = 120) -> str:
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if term in lowered]
    if not positions:
        return text[:max_length]
    start = max(min(positions) - 24, 0)
    end = min(start + max_length, len(text))
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def render_snippets(row: sqlite3.Row, terms: list[str]) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    private_field_snippets: list[dict[str, Any]] = []
    indexed_fields_json = row["indexed_fields_json"]
    if isinstance(indexed_fields_json, str):
        indexed_fields = parse_indexed_fields_json(indexed_fields_json)
    else:
        indexed_fields = ()
    for field in indexed_fields:
        field_name = field.get("field")
        field_text = field.get("text")
        if not isinstance(field_name, str) or not isinstance(field_text, str):
            continue
        if looks_like_private_path(field_text):
            private_field_snippets.append(
                {
                    "field": field_name,
                    "text": "[suppressed private path]",
                    "locator": None,
                    "display_policy": "suppressed",
                }
            )
    match_snippet = row["match_snippet"]
    if isinstance(match_snippet, str) and match_snippet.strip():
        snippets.append(
            {
                "field": "indexed_text",
                "text": render_snippet_text(match_snippet, terms),
                "locator": None,
                "display_policy": "public",
            }
        )
    title = row["title"]
    if isinstance(title, str) and title.strip():
        snippets.append(
            {
                "field": "title",
                "text": render_snippet_text(title, terms),
                "locator": None,
                "display_policy": "public",
            }
        )

    subtitle = row["subtitle"]
    if isinstance(subtitle, str) and subtitle.strip() and subtitle != title:
        snippets.append(
            {
                "field": "subtitle",
                "text": render_snippet_text(subtitle, terms),
                "locator": None,
                "display_policy": "public",
            }
        )
    if not snippets:
        title_text = "" if not isinstance(title, str) else title
        snippets.append(
            {
                "field": "title",
                "text": title_text[:120],
                "locator": None,
                "display_policy": "public",
            }
        )
    snippets = private_field_snippets + snippets
    for snippet in snippets:
        if looks_like_private_path(snippet["text"]):
            snippet["text"] = "[suppressed private path]"
            snippet["locator"] = None
            snippet["display_policy"] = "suppressed"
    return snippets


def build_result(row: sqlite3.Row, *, terms: list[str], rank: int) -> dict[str, Any]:
    matched_fields: list[str] = []
    snippets = render_snippets(row, terms)

    object_type = row["object_type"]
    return {
        "rank": rank,
        "result_class": RESULT_CLASS_BY_OBJECT_TYPE.get(object_type, "facet"),
        "result_id": row["projection_id"],
        "object_type": object_type,
        "object_id": row["object_ref"],
        "title": row["title"],
        "subtitle": row["subtitle"],
        "matched_fields": matched_fields,
        "snippets": snippets,
        "review_state": row["review_state"],
        "publication_state": row["publication_state"],
        "visibility": {
            "profile": row["profile"],
            "suppressed_fields": json.loads(row["suppressed_fields_json"]),
        },
        "score": None if row["score"] is None else float(row["score"]),
        "links": {
            "object_ref": row["object_ref"],
            "projection_id": row["projection_id"],
            "writer_surface": SCRIPT_PATH,
        },
    }


def build_results_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.limit < 1:
        raise SearchQueryError("limit must be at least 1")
    if args.limit > MAX_LIMIT:
        raise SearchQueryError(f"limit must be <= {MAX_LIMIT}")
    if args.offset < 0:
        raise SearchQueryError("offset must be >= 0")

    index_path = resolve_existing_file(args.index_db)
    normalized_query, terms, fts_query = normalize_plain_query(args.query)
    conn = connect_read_only(index_path)
    try:
        metadata = load_metadata(conn)
        rows, total = load_matching_rows(conn, fts_query=fts_query, scope=args.scope, limit=args.limit, offset=args.offset)
    finally:
        conn.close()

    results = [build_result(row, terms=terms, rank=args.offset + index + 1) for index, row in enumerate(rows)]
    return {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "generated_at": args.generated_at or now_rfc3339(),
        "source": {
            "database_name": metadata["source_database_name"],
            "database_fingerprint": metadata["source_database_fingerprint"],
            "projection_version": metadata["projection_schema_version"],
            "schema_version": metadata["source_schema_version"],
        },
        "query": {
            "raw_query": args.query,
            "normalized_query": normalized_query,
            "terms": terms,
            "scope": args.scope,
            "limit": args.limit,
            "offset": args.offset,
            "visibility_profile": metadata["profile"],
        },
        "counts": {
            "returned": len(results),
            "total_estimate": total,
            "truncated": args.offset + len(results) < total,
        },
        "policy": {
            "raw_payload_indexed": False,
            "full_text_indexed": False,
            "private_paths_exposed": False,
            "excluded_families": [],
        },
        "results": results,
        "warnings": [],
        "errors": [],
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"query={payload['query']['normalized_query']}",
        f"scope={payload['query']['scope']}",
        f"visibility_profile={payload['query']['visibility_profile']}",
        f"returned={payload['counts']['returned']}",
        f"total_estimate={payload['counts']['total_estimate']}",
        f"truncated={str(payload['counts']['truncated']).lower()}",
        f"writer_surface={SCRIPT_PATH}",
    ]
    for result in payload["results"]:
        lines.append(f"result[{result['rank']}].object_id={result['object_id']}")
        lines.append(f"result[{result['rank']}].result_class={result['result_class']}")
        lines.append(f"result[{result['rank']}].title={result['title']}")
        if result["matched_fields"]:
            lines.append(f"result[{result['rank']}].matched_fields={','.join(result['matched_fields'])}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = build_results_payload(args)
        if args.output_json:
            output_path = resolve_path(args.output_json)
            atomic_write_json(output_path, payload)
            report, exit_code = validate_local_search_results(output_path)
            if exit_code != EXIT_VALIDATOR_PASS:
                message = "; ".join(error["message"] for error in report["errors"]) or "local search results validation failed"
                raise SearchQueryError(message)
    except (OSError, SearchQueryError, sqlite3.DatabaseError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        )
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
