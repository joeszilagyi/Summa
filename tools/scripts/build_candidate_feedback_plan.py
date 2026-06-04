#!/usr/bin/env python3
"""Build a deterministic next-action feedback plan from canonical subject state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT, REPO_ROOT / "tools" / "scripts"):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.candidate_feedback_contract import (  # noqa: E402
    DEFAULT_MAX_DEFERRED_CANDIDATES,
    DEFAULT_MAX_FACET_CANDIDATES,
    DEFAULT_MAX_LEAD_CANDIDATES,
    DEFAULT_SCORING_WEIGHTS,
    LEAD_REVIEW_STATES,
    SCHEMA_VERSION,
    SCORING_POLICY_ID,
)
from tools.scripts import resolve_gather_domain_pack, resolve_subject_runtime  # noqa: E402
from tools.source_db_tools import canonical_store  # noqa: E402
from tools.validators.validate_candidate_feedback_plan import (  # noqa: E402
    EXIT_PASS,
    validate_candidate_feedback_plan,
)

SUCCESS_EXTRACTION_STATUSES = frozenset({"completed", "ok", "recorded", "success"})
FAILED_EXTRACTION_STATUSES = frozenset({"bad_utf8", "error", "failed", "hostile_replay", "invalid"})


class CandidateFeedbackError(RuntimeError):
    """Raised when planner inputs are missing or malformed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subject",
        required=True,
        help="Subject manifest path, or a subject_id resolved from <workspace>/.indexer/subject_manifest.json.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Workspace root used to resolve the subject runtime.",
    )
    parser.add_argument("--db", required=True, help="Path to the canonical SQLite database.")
    parser.add_argument(
        "--output-json", help="Optional JSON path for the emitted candidate-feedback plan."
    )
    parser.add_argument(
        "--generated-at", help="Optional RFC3339 timestamp override for deterministic tests."
    )
    parser.add_argument(
        "--max-facet-candidates",
        type=int,
        default=DEFAULT_MAX_FACET_CANDIDATES,
        help="Maximum ranked facet candidates to retain (default: 12).",
    )
    parser.add_argument(
        "--max-lead-candidates",
        type=int,
        default=DEFAULT_MAX_LEAD_CANDIDATES,
        help="Maximum ranked lead candidates to retain (default: 12).",
    )
    parser.add_argument(
        "--max-deferred-candidates",
        type=int,
        default=DEFAULT_MAX_DEFERRED_CANDIDATES,
        help="Maximum deferred candidates to retain (default: 24).",
    )
    parser.add_argument(
        "--scoring-policy",
        choices=(SCORING_POLICY_ID,),
        default=SCORING_POLICY_ID,
        help="Deterministic scoring policy identifier.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def now_rfc3339() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_args(args: argparse.Namespace) -> None:
    for field_name in ("max_facet_candidates", "max_lead_candidates"):
        value = getattr(args, field_name)
        if value < 1:
            raise CandidateFeedbackError(f"--{field_name.replace('_', '-')} must be at least 1")
    if args.max_deferred_candidates < 0:
        raise CandidateFeedbackError("--max-deferred-candidates must be non-negative")


def load_runtime(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        runtime = resolve_subject_runtime.resolve_subject_runtime(args.subject, args.workspace)
    except resolve_subject_runtime.ResolutionError as exc:
        raise CandidateFeedbackError(str(exc)) from exc
    subject = runtime["subject"]
    try:
        pack = resolve_gather_domain_pack.load_domain_pack(subject["domain_pack"])
        bundles = resolve_subject_runtime.resolve_prompt_bundles(
            pack, list(subject["enabled_facets"])
        )
    except (
        resolve_gather_domain_pack.GatherDomainPackError,
        resolve_subject_runtime.ResolutionError,
    ) as exc:
        raise CandidateFeedbackError(str(exc)) from exc
    return runtime, pack, bundles


def load_checked_connection(
    raw_db_path: str,
) -> tuple[sqlite3.Connection, canonical_store.CheckResult]:
    db_path = canonical_store.resolve_db_path(raw_db_path)
    try:
        check_result = canonical_store.check_canonical_store(db_path)
        conn = canonical_store.connect_existing_read_only(db_path)
    except canonical_store.CanonicalStoreError as exc:
        raise CandidateFeedbackError(f"canonical store is not usable: {exc}") from exc
    return conn, check_result


def parse_note_text(note_text: Any) -> dict[str, Any]:
    if not isinstance(note_text, str) or not note_text.strip():
        return {}
    try:
        payload = json.loads(note_text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def scope_work_ids(conn: sqlite3.Connection, subject_id: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT work_id
        FROM work
        WHERE workspace_id=?
        UNION
        SELECT DISTINCT work_id
        FROM work_subject
        WHERE subject_object_ref=?
        ORDER BY work_id
        """,
        (subject_id, subject_id),
    ).fetchall()
    return [int(row["work_id"]) for row in rows]


def work_ref(work_id: int) -> str:
    return f"work:{work_id}"


def claim_is_open_question(row: sqlite3.Row, provenance_payload: dict[str, Any]) -> bool:
    claim_type = str(row["claim_type"] or "")
    if "question" in claim_type.casefold():
        return True
    if provenance_payload.get("facet") == "open_questions":
        return True
    claim_text = str(row["claim_text"] or "")
    return "?" in claim_text


def truncate_text(value: str, *, max_length: int = 160) -> str:
    text = " ".join(value.split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def safe_source_access_label(row: sqlite3.Row) -> str:
    for field in ("canonical_url", "citation_hint"):
        value = row[field]
        if isinstance(value, str) and value.strip():
            return value.strip()
    original = row["original_locator"]
    if isinstance(original, str) and original.startswith(("http://", "https://")):
        return original
    return "[internal locator withheld]"


def canonical_family_yields_for_event(conn: sqlite3.Connection, event_key: str) -> dict[str, int]:
    counts = {
        "work": int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM work WHERE provenance_event_ref=?",
                (event_key,),
            ).fetchone()["count"]
        ),
        "source_claim": int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM source_claim WHERE provenance_event_ref=?",
                (event_key,),
            ).fetchone()["count"]
        ),
        "extraction_detected_entity": int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM extraction_detected_entity WHERE provenance_event_ref=?",
                (event_key,),
            ).fetchone()["count"]
        ),
        "source_relationship": int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM source_relationship WHERE provenance_event_ref=?",
                (event_key,),
            ).fetchone()["count"]
        ),
    }

    note_row = conn.execute(
        "SELECT note_text FROM provenance_event WHERE provenance_event_key_v1=?",
        (event_key,),
    ).fetchone()
    note_payload = parse_note_text(note_row["note_text"]) if note_row is not None else {}
    artifact_hash = note_payload.get("artifact_hash")

    source_access_ids: set[int] = set()
    for row in conn.execute(
        """
        SELECT access.source_access_id
        FROM source_access AS access
        INNER JOIN work ON work.work_id = access.work_id
        WHERE work.provenance_event_ref=?
        """,
        (event_key,),
    ).fetchall():
        source_access_ids.add(int(row["source_access_id"]))
    if isinstance(artifact_hash, str) and artifact_hash:
        like_pattern = f"source-lead:{artifact_hash}:%"
        for row in conn.execute(
            """
            SELECT source_access_id
            FROM source_access
            WHERE source_lead_id LIKE ?
            """,
            (like_pattern,),
        ).fetchall():
            source_access_ids.add(int(row["source_access_id"]))
    counts["source_access"] = len(source_access_ids)
    return counts


def load_gather_history(conn: sqlite3.Connection, subject_id: str) -> list[dict[str, Any]]:
    pattern = f'%"subject_id": "{subject_id}"%'
    rows = conn.execute(
        """
        SELECT provenance_event_id, provenance_event_key_v1, run_id, event_timestamp, note_text
        FROM provenance_event
        WHERE event_type='gather_candidate_batch_ingest'
          AND note_text LIKE ?
        ORDER BY event_timestamp DESC, provenance_event_id DESC
        """,
        (pattern,),
    ).fetchall()
    history: list[dict[str, Any]] = []
    for row in rows:
        note_payload = parse_note_text(row["note_text"])
        if note_payload.get("subject_id") != subject_id:
            continue
        facet = note_payload.get("facet")
        if not isinstance(facet, str) or not facet:
            continue
        event_key = str(row["provenance_event_key_v1"])
        yields = canonical_family_yields_for_event(conn, event_key)
        total_yield = sum(yields.values())
        history.append(
            {
                "event_id": int(row["provenance_event_id"]),
                "event_key": event_key,
                "run_id": None if row["run_id"] is None else str(row["run_id"]),
                "event_timestamp": str(row["event_timestamp"]),
                "facet": facet,
                "prompt_bundle_id": note_payload.get("prompt_bundle_id"),
                "cycle_depth": note_payload.get("cycle_depth"),
                "yields": yields,
                "total_yield": total_yield,
            }
        )
    return history


def provenance_map_by_key(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["event_key"]: entry for entry in history}


def lead_scope_sql(subject_id: str, work_ids: list[int]) -> tuple[str, tuple[Any, ...]]:
    if work_ids:
        placeholders = ", ".join("?" for _ in work_ids)
        return (
            f"(workspace_id=? OR work_id IN ({placeholders}))",
            (subject_id, *work_ids),
        )
    return "workspace_id=?", (subject_id,)


def claim_scope_sql(subject_id: str, work_ids: list[int]) -> tuple[str, tuple[Any, ...]]:
    work_refs = [work_ref(work_id) for work_id in work_ids]
    if work_refs:
        placeholders = ", ".join("?" for _ in work_refs)
        return (
            f"(workspace_id=? OR about_object_ref IN ({placeholders}))",
            (subject_id, *work_refs),
        )
    return "workspace_id=?", (subject_id,)


def run_id_for_event(conn: sqlite3.Connection, event_key: str) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM provenance_event WHERE provenance_event_key_v1=?",
        (event_key,),
    ).fetchone()
    if row is None or row["run_id"] is None:
        return None
    return str(row["run_id"])


def extraction_outcome_counts(
    conn: sqlite3.Connection, *, source_locus_id: str | None, locators: list[str], subject_id: str
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = [subject_id]
    if source_locus_id:
        clauses.append("source_locus_ref=?")
        params.append(source_locus_id)
    for locator in locators:
        clauses.append("original_locator=?")
        params.append(locator)
    if not clauses:
        return {
            "successful_extractions": 0,
            "failed_extractions": 0,
            "capture_count": 0,
            "capture_ids": [],
            "extraction_ids": [],
            "related_run_ids": [],
        }
    query = (
        "SELECT capture_event_id, provenance_event_ref FROM capture_event "
        "WHERE workspace_id=? AND (" + " OR ".join(clauses) + ") ORDER BY capture_event_id"
    )
    capture_rows = conn.execute(query, tuple(params)).fetchall()
    capture_ids = [int(row["capture_event_id"]) for row in capture_rows]
    run_ids = {
        run_id_for_event(conn, str(row["provenance_event_ref"]))
        for row in capture_rows
        if row["provenance_event_ref"] is not None
    }
    run_ids.discard(None)
    if not capture_ids:
        return {
            "successful_extractions": 0,
            "failed_extractions": 0,
            "capture_count": 0,
            "capture_ids": [],
            "extraction_ids": [],
            "related_run_ids": sorted(run_ids),
        }
    placeholders = ", ".join("?" for _ in capture_ids)
    extraction_rows = conn.execute(
        f"""
        SELECT extraction_id, extraction_status, provenance_event_ref
        FROM extraction_record
        WHERE capture_event_id IN ({placeholders})
        ORDER BY extraction_id
        """,
        tuple(capture_ids),
    ).fetchall()
    extraction_ids = [int(row["extraction_id"]) for row in extraction_rows]
    for row in extraction_rows:
        if row["provenance_event_ref"] is None:
            continue
        run_id = run_id_for_event(conn, str(row["provenance_event_ref"]))
        if run_id is not None:
            run_ids.add(run_id)
    successful = 0
    failed = 0
    for row in extraction_rows:
        status = str(row["extraction_status"] or "").casefold()
        if status in FAILED_EXTRACTION_STATUSES:
            failed += 1
        else:
            successful += 1
    return {
        "successful_extractions": successful,
        "failed_extractions": failed,
        "capture_count": len(capture_ids),
        "capture_ids": capture_ids,
        "extraction_ids": extraction_ids,
        "related_run_ids": sorted(run_ids),
    }


def load_source_access_leads(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    work_ids: list[int],
    history_by_event_key: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    scope_sql, scope_params = lead_scope_sql(subject_id, work_ids)
    placeholders = ", ".join("?" for _ in sorted(LEAD_REVIEW_STATES))
    rows = conn.execute(
        f"""
        SELECT access.source_access_id, access.work_id, access.source_locus_id,
               access.source_lead_id, access.original_locator, access.canonical_url,
               access.citation_hint, access.review_state, access.first_seen_at,
               access.last_seen_at, access.record_last_updated,
               work.provenance_event_ref AS work_provenance_event_ref
        FROM source_access AS access
        LEFT JOIN work
          ON work.work_id = access.work_id
        WHERE {scope_sql.replace("workspace_id", "access.workspace_id").replace("work_id", "access.work_id")}
          AND access.review_state IN ({placeholders})
        ORDER BY COALESCE(access.last_seen_at, access.first_seen_at, access.record_last_updated) DESC, access.source_access_id ASC
        """,
        scope_params + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()

    leads: list[dict[str, Any]] = []
    for row in rows:
        provenance = history_by_event_key.get(str(row["work_provenance_event_ref"] or ""))
        facet = provenance["facet"] if provenance is not None else "sources"
        metrics = extraction_outcome_counts(
            conn,
            source_locus_id=None if row["source_locus_id"] is None else str(row["source_locus_id"]),
            locators=[
                locator
                for locator in {
                    row["original_locator"] if isinstance(row["original_locator"], str) else None,
                    row["canonical_url"] if isinstance(row["canonical_url"], str) else None,
                }
                if isinstance(locator, str) and locator
            ],
            subject_id=subject_id,
        )
        related_claims = 0
        related_works = 0
        if row["work_id"] is not None:
            related_works = 1
            related_claims = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM source_claim WHERE about_object_ref=?",
                    (work_ref(int(row["work_id"])),),
                ).fetchone()["count"]
            )
        related_entities = 0
        if metrics["capture_ids"]:
            placeholders = ", ".join("?" for _ in metrics["capture_ids"])
            related_entities = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM extraction_detected_entity WHERE capture_event_id IN ({placeholders})",
                    tuple(metrics["capture_ids"]),
                ).fetchone()["count"]
            )
        related_yield = related_works + related_claims + related_entities
        zero_yield_attempts = metrics["capture_count"] if related_yield == 0 else 0
        signals = {
            "open_lead": 1,
            "related_works": related_works,
            "related_claims": related_claims,
            "related_entities": related_entities,
            "successful_extractions": metrics["successful_extractions"],
            "failed_extractions": metrics["failed_extractions"],
            "zero_yield_attempts": zero_yield_attempts,
        }
        score = (
            weights["open_lead"] * signals["open_lead"]
            + weights["work_yield"] * signals["related_works"]
            + weights["claim_yield"] * signals["related_claims"]
            + weights["entity_yield"] * signals["related_entities"]
            + weights["successful_extraction"] * signals["successful_extractions"]
            - weights["failed_extraction_penalty"] * signals["failed_extractions"]
            - weights["zero_yield_penalty"] * signals["zero_yield_attempts"]
        )
        reason_codes = ["open_lead_yield"]
        if signals["related_works"] or signals["related_claims"] or signals["related_entities"]:
            reason_codes.append("related_canonical_yield")
        if signals["successful_extractions"]:
            reason_codes.append("productive_extractions")
        if signals["failed_extractions"]:
            reason_codes.append("failed_extractions")
        if signals["zero_yield_attempts"]:
            reason_codes.append("repeated_low_yield")
        rationale_bits = [
            f"open lead {row['source_access_id']}",
            f"{signals['related_works']} related works",
            f"{signals['related_claims']} related claims",
            f"{signals['related_entities']} related entities",
            f"{signals['successful_extractions']} successful extractions",
            f"{signals['failed_extractions']} failed extractions",
        ]
        related_run_ids = sorted(
            {
                *(metrics["related_run_ids"]),
                provenance["run_id"] if provenance and provenance.get("run_id") else None,
            }
            - {None}
        )
        leads.append(
            {
                "candidate_id": f"source_access:{int(row['source_access_id'])}",
                "lead_kind": "source_access",
                "object_ref": f"source_access:{int(row['source_access_id'])}",
                "facet": facet,
                "review_state": str(row["review_state"]),
                "label": safe_source_access_label(row),
                "score": round(score, 4),
                "selected": False,
                "reason_codes": reason_codes,
                "rationale": "; ".join(rationale_bits),
                "signals": signals,
                "source_locus_id": None
                if row["source_locus_id"] is None
                else str(row["source_locus_id"]),
                "source_lead_id": None
                if row["source_lead_id"] is None
                else str(row["source_lead_id"]),
                "related_run_ids": related_run_ids,
            }
        )
    return leads


def load_open_question_leads(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    work_ids: list[int],
    history_by_event_key: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    scope_sql, scope_params = claim_scope_sql(subject_id, work_ids)
    placeholders = ", ".join("?" for _ in sorted(LEAD_REVIEW_STATES))
    rows = conn.execute(
        f"""
        SELECT source_claim_id, about_object_ref, claim_text, claim_type, review_state,
               provenance_event_ref, created_at
        FROM source_claim
        WHERE {scope_sql}
          AND review_state IN ({placeholders})
        ORDER BY created_at DESC, source_claim_id ASC
        """,
        scope_params + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()
    leads: list[dict[str, Any]] = []
    for row in rows:
        provenance = history_by_event_key.get(str(row["provenance_event_ref"] or ""))
        if not claim_is_open_question(row, provenance or {}):
            continue
        score = weights["open_lead"] + weights["claim_yield"]
        leads.append(
            {
                "candidate_id": f"source_claim:{int(row['source_claim_id'])}",
                "lead_kind": "source_claim",
                "object_ref": f"source_claim:{int(row['source_claim_id'])}",
                "facet": "open_questions",
                "review_state": str(row["review_state"]),
                "label": truncate_text(str(row["claim_text"]), max_length=160),
                "score": round(score, 4),
                "selected": False,
                "reason_codes": ["open_lead_yield", "open_question_candidate"],
                "rationale": "Open-question claim remains unresolved and can guide the next gather cycle.",
                "signals": {
                    "open_lead": 1,
                    "related_works": 0,
                    "related_claims": 1,
                    "related_entities": 0,
                    "successful_extractions": 0,
                    "failed_extractions": 0,
                    "zero_yield_attempts": 0,
                },
                "source_locus_id": None,
                "source_lead_id": None,
                "related_run_ids": sorted(
                    {
                        provenance["run_id"] if provenance and provenance.get("run_id") else None,
                    }
                    - {None}
                ),
            }
        )
    return leads


def load_entity_leads(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    history_by_event_key: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in sorted(LEAD_REVIEW_STATES))
    rows = conn.execute(
        f"""
        SELECT entity.detected_entity_id, entity.entity_label, entity.entity_type, entity.review_state,
               entity.provenance_event_ref, extraction.extraction_status
        FROM extraction_detected_entity entity
        LEFT JOIN extraction_record extraction
          ON extraction.extraction_id = entity.extraction_id
        LEFT JOIN capture_event capture
          ON capture.capture_event_id = entity.capture_event_id
        WHERE COALESCE(extraction.workspace_id, capture.workspace_id)=?
          AND entity.review_state IN ({placeholders})
        ORDER BY entity.detected_entity_id
        """,
        (subject_id,) + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()
    leads: list[dict[str, Any]] = []
    for row in rows:
        entity_type = str(row["entity_type"] or "").casefold()
        if entity_type == "person":
            facet = "people"
        elif entity_type == "place":
            facet = "places"
        else:
            continue
        extraction_status = str(row["extraction_status"] or "").casefold()
        success = (
            1 if extraction_status and extraction_status not in FAILED_EXTRACTION_STATUSES else 0
        )
        failed = 1 if extraction_status in FAILED_EXTRACTION_STATUSES else 0
        score = (
            weights["open_lead"]
            + weights["entity_yield"]
            + weights["successful_extraction"] * success
            - weights["failed_extraction_penalty"] * failed
        )
        provenance = history_by_event_key.get(str(row["provenance_event_ref"] or ""))
        leads.append(
            {
                "candidate_id": f"detected_entity:{int(row['detected_entity_id'])}",
                "lead_kind": "detected_entity",
                "object_ref": f"detected_entity:{int(row['detected_entity_id'])}",
                "facet": facet,
                "review_state": str(row["review_state"]),
                "label": truncate_text(str(row["entity_label"]), max_length=120),
                "score": round(score, 4),
                "selected": False,
                "reason_codes": ["open_lead_yield", f"{facet}_candidate"],
                "rationale": f"{facet} facet still has an unresolved detected entity lead.",
                "signals": {
                    "open_lead": 1,
                    "related_works": 0,
                    "related_claims": 0,
                    "related_entities": 1,
                    "successful_extractions": success,
                    "failed_extractions": failed,
                    "zero_yield_attempts": 0,
                },
                "source_locus_id": None,
                "source_lead_id": None,
                "related_run_ids": sorted(
                    {
                        provenance["run_id"] if provenance and provenance.get("run_id") else None,
                    }
                    - {None}
                ),
            }
        )
    return leads


def load_work_leads(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    work_ids = scope_work_ids(conn, subject_id)
    if not work_ids:
        return []
    placeholders = ", ".join("?" for _ in work_ids)
    review_placeholders = ", ".join("?" for _ in sorted(LEAD_REVIEW_STATES))
    rows = conn.execute(
        f"""
        SELECT work_id, title, review_state
        FROM work
        WHERE work_id IN ({placeholders})
          AND review_state IN ({review_placeholders})
        ORDER BY work_id
        """,
        tuple(work_ids) + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()
    leads: list[dict[str, Any]] = []
    for row in rows:
        score = weights["open_lead"] + weights["work_yield"]
        leads.append(
            {
                "candidate_id": f"work:{int(row['work_id'])}",
                "lead_kind": "work",
                "object_ref": f"work:{int(row['work_id'])}",
                "facet": "works",
                "review_state": str(row["review_state"]),
                "label": truncate_text(str(row["title"] or f"work:{int(row['work_id'])}")),
                "score": round(score, 4),
                "selected": False,
                "reason_codes": ["open_lead_yield", "unreviewed_work_candidate"],
                "rationale": "Unreviewed work records can be expanded with a focused works gather pass.",
                "signals": {
                    "open_lead": 1,
                    "related_works": 1,
                    "related_claims": 0,
                    "related_entities": 0,
                    "successful_extractions": 0,
                    "failed_extractions": 0,
                    "zero_yield_attempts": 0,
                },
                "source_locus_id": None,
                "source_lead_id": None,
                "related_run_ids": [],
            }
        )
    return leads


def aggregate_facet_scores(
    *,
    enabled_facets: list[str],
    bundles: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    lead_candidates: list[dict[str, Any]],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    facet_order = {facet: index for index, facet in enumerate(enabled_facets)}
    metrics: dict[str, dict[str, int]] = {
        facet: {
            "productive_runs": 0,
            "zero_yield_runs": 0,
            "open_leads": 0,
            "works": 0,
            "claims": 0,
            "entities": 0,
            "relationships": 0,
            "successful_extractions": 0,
            "failed_extractions": 0,
        }
        for facet in enabled_facets
    }
    last_run_zero_yield: dict[str, bool] = {facet: False for facet in enabled_facets}
    history_by_facet: dict[str, list[dict[str, Any]]] = {facet: [] for facet in enabled_facets}
    for event in history:
        facet = event["facet"]
        if facet not in metrics:
            continue
        history_by_facet[facet].append(event)
        if event["total_yield"] > 0:
            metrics[facet]["productive_runs"] += 1
        else:
            metrics[facet]["zero_yield_runs"] += 1
        metrics[facet]["works"] += int(event["yields"]["work"])
        metrics[facet]["claims"] += int(event["yields"]["source_claim"])
        metrics[facet]["entities"] += int(event["yields"]["extraction_detected_entity"])
        metrics[facet]["relationships"] += int(event["yields"]["source_relationship"])
    for facet, events in history_by_facet.items():
        if events:
            last_run_zero_yield[facet] = events[0]["total_yield"] == 0
    for lead in lead_candidates:
        facet = lead["facet"]
        if facet not in metrics:
            continue
        metrics[facet]["open_leads"] += 1
        metrics[facet]["successful_extractions"] += int(lead["signals"]["successful_extractions"])
        metrics[facet]["failed_extractions"] += int(lead["signals"]["failed_extractions"])

    no_prior_history = not history
    facet_scores: list[dict[str, Any]] = []
    for facet in enabled_facets:
        signal_bucket = metrics[facet]
        apply_recent_low_yield_penalty = bool(
            last_run_zero_yield[facet]
            and signal_bucket["productive_runs"] == 0
            and signal_bucket["open_leads"] == 0
        )
        score = (
            weights["productive_run"] * signal_bucket["productive_runs"]
            + weights["open_lead"] * signal_bucket["open_leads"]
            + weights["work_yield"] * signal_bucket["works"]
            + weights["claim_yield"] * signal_bucket["claims"]
            + weights["entity_yield"] * signal_bucket["entities"]
            + weights["relationship_yield"] * signal_bucket["relationships"]
            + weights["successful_extraction"] * signal_bucket["successful_extractions"]
            - weights["failed_extraction_penalty"] * signal_bucket["failed_extractions"]
            - weights["zero_yield_penalty"] * signal_bucket["zero_yield_runs"]
            - (weights["recent_low_yield_penalty"] if apply_recent_low_yield_penalty else 0.0)
        )
        if no_prior_history:
            score += weights["bootstrap_bias"] * (len(enabled_facets) - facet_order[facet])
        reason_codes: list[str] = []
        if no_prior_history:
            reason_codes.append("bootstrap_no_prior_productivity")
        if signal_bucket["productive_runs"]:
            reason_codes.append("productive_history")
        if signal_bucket["open_leads"]:
            reason_codes.append("open_lead_yield")
        if signal_bucket["zero_yield_runs"]:
            reason_codes.append("repeated_zero_yield")
        if apply_recent_low_yield_penalty:
            reason_codes.append("recent_low_yield_penalty")
        if not reason_codes:
            reason_codes.append("fallback_facet")
        rationale = (
            f"{signal_bucket['productive_runs']} productive runs, "
            f"{signal_bucket['open_leads']} open leads, "
            f"{signal_bucket['zero_yield_runs']} zero-yield runs."
        )
        facet_scores.append(
            {
                "candidate_id": f"facet:{facet}",
                "facet": facet,
                "prompt_bundle_id": bundles[facet]["bundle_id"],
                "score": round(score, 4),
                "selected": False,
                "reason_codes": reason_codes,
                "rationale": rationale,
                "signals": signal_bucket,
            }
        )
    facet_scores.sort(
        key=lambda item: (-float(item["score"]), facet_order[item["facet"]], item["candidate_id"])
    )
    for rank, item in enumerate(facet_scores, start=1):
        item["rank"] = rank
    return facet_scores


def aggregate_lead_scores(
    *,
    enabled_facets: list[str],
    lead_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    facet_order = {facet: index for index, facet in enumerate(enabled_facets)}
    scored = sorted(
        lead_candidates,
        key=lambda item: (
            -float(item["score"]),
            facet_order.get(item["facet"], len(enabled_facets)),
            item["candidate_id"],
        ),
    )
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank
    return scored


def sorted_previous_run_ids(history: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for event in history:
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id and run_id not in seen:
            seen.add(run_id)
            ordered.append(run_id)
    return ordered


def next_cycle_depth(history: list[dict[str, Any]]) -> int:
    max_depth = 0
    for event in history:
        depth = event.get("cycle_depth")
        if isinstance(depth, int) and depth > max_depth:
            max_depth = depth
    return max_depth + 1 if max_depth else 1


def select_next_action(
    *,
    subject: dict[str, Any],
    facet_scores: list[dict[str, Any]],
    lead_scores: list[dict[str, Any]],
    previous_run_ids: list[str],
    cycle_depth: int,
) -> dict[str, Any]:
    if not facet_scores:
        raise CandidateFeedbackError("feedback planner requires at least one enabled facet")
    top_facet = facet_scores[0]
    productive_leads = [item for item in lead_scores if float(item["score"]) > 0.0]
    if productive_leads:
        selected = productive_leads[0]
        selected["selected"] = True
        action_kind = "facet_lead"
        selected_object_ref = selected["object_ref"]
        selected_lead_kind = selected["lead_kind"]
        selected_source_locus_id = selected["source_locus_id"]
        selected_source_lead_id = selected["source_lead_id"]
        selected_label = selected["label"]
        selected_review_state = selected["review_state"]
        rationale = selected["rationale"]
        reason_codes = list(selected["reason_codes"])
        score = float(selected["score"])
        input_record_refs = [selected_object_ref]
        use_prior_state = bool(previous_run_ids)
    else:
        action_kind = "facet_bootstrap" if not previous_run_ids else "facet_only"
        selected_object_ref = None
        selected_lead_kind = None
        selected_source_locus_id = None
        selected_source_lead_id = None
        selected_label = None
        selected_review_state = None
        rationale = top_facet["rationale"]
        reason_codes = list(top_facet["reason_codes"])
        score = float(top_facet["score"])
        input_record_refs = []
        use_prior_state = bool(previous_run_ids)

    selected_facet = top_facet["facet"] if not productive_leads else productive_leads[0]["facet"]
    selected_prompt_bundle_id = (
        top_facet["prompt_bundle_id"]
        if not productive_leads
        else next(
            item["prompt_bundle_id"]
            for item in facet_scores
            if item["facet"] == productive_leads[0]["facet"]
        )
    )
    cli_args = ["--facet", selected_facet]
    if use_prior_state:
        cli_args.extend(["--use-prior-state", "--cycle-depth", str(cycle_depth)])

    return {
        "action_id": f"next-action:{subject['subject_id']}:{selected_facet}:{selected_object_ref or 'facet'}:{cycle_depth}",
        "action_kind": action_kind,
        "subject_id": subject["subject_id"],
        "selected_facet": selected_facet,
        "selected_prompt_bundle_id": selected_prompt_bundle_id,
        "selected_object_ref": selected_object_ref,
        "selected_lead_kind": selected_lead_kind,
        "selected_source_locus_id": selected_source_locus_id,
        "selected_source_lead_id": selected_source_lead_id,
        "selected_label": selected_label,
        "selected_review_state": selected_review_state,
        "selection_score": round(score, 4),
        "scoring_policy_id": SCORING_POLICY_ID,
        "rationale": rationale,
        "reason_codes": reason_codes,
        "cycle_depth": cycle_depth,
        "use_prior_state": use_prior_state,
        "previous_run_ids_considered": previous_run_ids,
        "input_record_refs": input_record_refs,
        "suggested_cli_args": cli_args,
    }


def build_deferred_candidates(
    *,
    selected_next_action: dict[str, Any],
    facet_scores: list[dict[str, Any]],
    lead_scores: list[dict[str, Any]],
    max_deferred: int,
) -> list[dict[str, Any]]:
    deferred: list[dict[str, Any]] = []
    for item in facet_scores[1:]:
        deferred.append(
            {
                "candidate_id": item["candidate_id"],
                "candidate_kind": "facet",
                "score": item["score"],
                "reason": "lower_score_than_selected",
            }
        )
    selected_object_ref = selected_next_action.get("selected_object_ref")
    for item in lead_scores:
        if item["object_ref"] == selected_object_ref:
            continue
        reason = "lower_score_than_selected"
        if "repeated_low_yield" in item["reason_codes"]:
            reason = "repeated_low_yield"
        deferred.append(
            {
                "candidate_id": item["candidate_id"],
                "candidate_kind": "lead",
                "score": item["score"],
                "reason": reason,
            }
        )
    return deferred[:max_deferred]


def build_plan(
    *,
    args: argparse.Namespace,
    runtime: dict[str, Any],
    bundles: dict[str, dict[str, Any]],
    check_result: canonical_store.CheckResult,
    conn: sqlite3.Connection,
    generated_at: str,
) -> dict[str, Any]:
    subject = runtime["subject"]
    work_ids = scope_work_ids(conn, subject["subject_id"])
    history = load_gather_history(conn, subject["subject_id"])
    history_by_event_key = provenance_map_by_key(history)
    source_access_leads = load_source_access_leads(
        conn,
        subject_id=subject["subject_id"],
        work_ids=work_ids,
        history_by_event_key=history_by_event_key,
        weights=DEFAULT_SCORING_WEIGHTS,
    )
    open_question_leads = load_open_question_leads(
        conn,
        subject_id=subject["subject_id"],
        work_ids=work_ids,
        history_by_event_key=history_by_event_key,
        weights=DEFAULT_SCORING_WEIGHTS,
    )
    entity_leads = load_entity_leads(
        conn,
        subject_id=subject["subject_id"],
        history_by_event_key=history_by_event_key,
        weights=DEFAULT_SCORING_WEIGHTS,
    )
    work_leads = load_work_leads(
        conn,
        subject_id=subject["subject_id"],
        weights=DEFAULT_SCORING_WEIGHTS,
    )
    lead_candidates = source_access_leads + open_question_leads + entity_leads + work_leads
    facet_scores = aggregate_facet_scores(
        enabled_facets=list(subject["enabled_facets"]),
        bundles=bundles,
        history=history,
        lead_candidates=lead_candidates,
        weights=DEFAULT_SCORING_WEIGHTS,
    )[: args.max_facet_candidates]
    lead_scores = aggregate_lead_scores(
        enabled_facets=list(subject["enabled_facets"]),
        lead_candidates=lead_candidates,
    )[: args.max_lead_candidates]
    previous_run_ids = sorted_previous_run_ids(history)
    cycle_depth = next_cycle_depth(history)
    next_action = select_next_action(
        subject=subject,
        facet_scores=facet_scores,
        lead_scores=lead_scores,
        previous_run_ids=previous_run_ids,
        cycle_depth=cycle_depth,
    )
    for item in facet_scores:
        item["selected"] = item["facet"] == next_action["selected_facet"]
    deferred = build_deferred_candidates(
        selected_next_action=next_action,
        facet_scores=facet_scores,
        lead_scores=lead_scores,
        max_deferred=args.max_deferred_candidates,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "subject": {
            "subject_id": subject["subject_id"],
            "display_name": subject["display_name"],
            "domain_pack": subject["domain_pack"],
            "enabled_facets": list(subject["enabled_facets"]),
            "query_families": list(subject["query_families"]),
        },
        "canonical_store": {
            "database_name": check_result.db_path.name,
            "schema_version": check_result.schema_version,
            "current_migration_id": check_result.current_migration_id,
            "dry_run": True,
        },
        "scoring_policy": {
            "policy_id": args.scoring_policy,
            "cycle_depth_considered": cycle_depth,
            "previous_run_ids_considered": previous_run_ids,
            "use_prior_state": bool(previous_run_ids),
            "weights": dict(DEFAULT_SCORING_WEIGHTS),
            "limits": {
                "max_facet_candidates": args.max_facet_candidates,
                "max_lead_candidates": args.max_lead_candidates,
                "max_deferred_candidates": args.max_deferred_candidates,
            },
        },
        "counts": {
            "gather_runs_considered": len(history),
            "facet_candidates": len(facet_scores),
            "lead_candidates": len(lead_scores),
            "productive_leads": sum(1 for item in lead_scores if float(item["score"]) > 0.0),
            "deferred_candidates": len(deferred),
        },
        "facet_scores": facet_scores,
        "lead_scores": lead_scores,
        "next_action": next_action,
        "deferred": deferred,
        "warnings": [],
        "errors": [],
    }


def render_text_plan(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"subject_id={payload['subject']['subject_id']}",
        f"domain_pack={payload['subject']['domain_pack']}",
        f"selected_facet={payload['next_action']['selected_facet']}",
        f"selected_prompt_bundle_id={payload['next_action']['selected_prompt_bundle_id']}",
        f"action_kind={payload['next_action']['action_kind']}",
        f"selection_score={payload['next_action']['selection_score']}",
        f"cycle_depth={payload['next_action']['cycle_depth']}",
        f"use_prior_state={str(payload['next_action']['use_prior_state']).lower()}",
    ]
    if payload["next_action"]["selected_object_ref"] is not None:
        lines.append(f"selected_object_ref={payload['next_action']['selected_object_ref']}")
    lines.append(f"reason_codes={','.join(payload['next_action']['reason_codes'])}")
    lines.append(f"rationale={payload['next_action']['rationale']}")
    for index, item in enumerate(payload["facet_scores"][:3]):
        lines.append(f"facet[{index}]={item['facet']} score={item['score']}")
    for index, item in enumerate(payload["lead_scores"][:3]):
        lines.append(
            f"lead[{index}]={item['object_ref']} facet={item['facet']} score={item['score']}"
        )
    return "\n".join(lines) + "\n"


def validate_emitted_plan(path: Path) -> None:
    report, exit_code = validate_candidate_feedback_plan(path)
    if exit_code != EXIT_PASS:
        first = report["errors"][0]["message"] if report.get("errors") else "unknown error"
        raise CandidateFeedbackError(
            f"generated candidate feedback plan failed validation: {first}"
        )


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        generated_at = (
            canonical_store._normalize_timestamp(args.generated_at, field_name="--generated-at")
            if args.generated_at
            else now_rfc3339()
        )
        runtime, _pack, bundles = load_runtime(args)
        conn, check_result = load_checked_connection(args.db)
        try:
            payload = build_plan(
                args=args,
                runtime=runtime,
                bundles=bundles,
                check_result=check_result,
                conn=conn,
                generated_at=generated_at,
            )
        finally:
            conn.close()

        if args.output_json:
            output_path = resolve_path(args.output_json)
            atomic_write_json(output_path, payload)
            validate_emitted_plan(output_path)
        else:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".json", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                )
            try:
                validate_emitted_plan(temp_path)
            finally:
                temp_path.unlink(missing_ok=True)

        if args.format == "text":
            sys.stdout.write(render_text_plan(payload))
        else:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            sys.stdout.write("\n")
        return 0
    except CandidateFeedbackError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
