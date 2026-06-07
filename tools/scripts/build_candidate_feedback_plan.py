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
    ACCEPTED_REVIEW_STATES,
    CANDIDATE_FEEDBACK_SCORE_MAX,
    CANDIDATE_FEEDBACK_SCORE_MIN,
    DEFAULT_MAX_DEFERRED_CANDIDATES,
    DEFAULT_MAX_FACET_CANDIDATES,
    DEFAULT_MAX_LEAD_CANDIDATES,
    DEFAULT_SCORING_WEIGHTS,
    LEAD_REVIEW_STATES,
    SCHEMA_VERSION,
    SCORING_POLICY_ID,
)
from tools.common.selection_explanation import (  # noqa: E402
    build_feedback_selection_explanation,
)
from tools.scripts import resolve_gather_domain_pack, resolve_subject_runtime  # noqa: E402
from tools.source_db_tools import canonical_store, cycle_evidence_ledger  # noqa: E402
from tools.validators.validate_candidate_feedback_plan import (  # noqa: E402
    EXIT_PASS,
    validate_candidate_feedback_plan,
)

SUCCESS_EXTRACTION_STATUSES = frozenset({"completed", "ok", "recorded", "success"})
FAILED_EXTRACTION_STATUSES = frozenset({"bad_utf8", "error", "failed", "hostile_replay", "invalid"})
ENTITY_FACET_PREFIXES = {
    "person": "people",
    "person_or_group": "people",
    "place": "places",
    "organization": "organizations",
    "org": "organizations",
    "work": "works",
    "event": "events",
    "concept": "concepts",
    "topic": "topics",
    "product": "products",
}
FACET_SCORE_SIGNAL_CAP = 5
LEAD_SCORE_SIGNAL_CAP = 3
RELATED_SCORE_SIGNAL_CAP = 3
SOURCE_DIVERSITY_BONUS_STEP = 0.25
QUESTION_TERMS = frozenset(
    {"what", "which", "who", "whom", "whose", "where", "when", "why", "how", "whether"}
)


def classify_extraction_status(raw_status: Any) -> tuple[int, int, str | None]:
    status = str(raw_status or "").strip().casefold()
    if status in SUCCESS_EXTRACTION_STATUSES:
        return 1, 0, None
    if status in FAILED_EXTRACTION_STATUSES:
        return 0, 1, None
    if not status:
        return 0, 0, None
    return 0, 1, status


def _pick_facet_for_entity_type(entity_type: str, enabled_facets: list[str]) -> str:
    normalized = str(entity_type or "").strip().casefold().replace(" ", "_")
    if normalized in ENTITY_FACET_PREFIXES:
        candidate = ENTITY_FACET_PREFIXES[normalized]
    elif normalized.endswith("s"):
        candidate = normalized[:-1]
        if candidate in ENTITY_FACET_PREFIXES:
            candidate = ENTITY_FACET_PREFIXES[candidate]
    else:
        candidate = normalized
    if candidate in {"", None}:
        candidate = normalized
    if candidate in enabled_facets:
        return candidate
    if candidate and candidate + "s" in enabled_facets:
        return candidate + "s"
    if candidate.endswith("s") and candidate[:-1] in enabled_facets:
        return candidate[:-1]
    if "sources" in enabled_facets:
        return "sources"
    return enabled_facets[0] if enabled_facets else candidate


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
    parser.add_argument(
        "--feedback-plan-stage",
        default="build_candidate_feedback_plan",
        help="Stage label used in selection-explanation IDs and manifest evidence.",
    )
    parser.add_argument(
        "--record-selection-ledger",
        action="store_true",
        help=(
            "Record feedback candidates into the local cycle evidence ledger. "
            "This mutates only operational ledger tables."
        ),
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
    if not isinstance(args.feedback_plan_stage, str) or not args.feedback_plan_stage.strip():
        raise CandidateFeedbackError("--feedback-plan-stage must be a non-empty string")


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


def bounded_candidate_score(value: float) -> float:
    return round(
        max(CANDIDATE_FEEDBACK_SCORE_MIN, min(CANDIDATE_FEEDBACK_SCORE_MAX, float(value))),
        4,
    )


def capped_count(value: int, cap: int) -> int:
    return min(max(int(value), 0), cap)


def accepted_review_placeholder_sql() -> tuple[str, tuple[str, ...]]:
    placeholders = ", ".join("?" for _ in sorted(ACCEPTED_REVIEW_STATES))
    return placeholders, tuple(sorted(ACCEPTED_REVIEW_STATES))


def open_question_quality_bonus(
    *, claim_text: str, claim_type: str, provenance_payload: dict[str, Any]
) -> float:
    text = " ".join(str(claim_text or "").split())
    if not text:
        return -0.75
    words = [word.strip(".,;:!?()[]{}\"'").casefold() for word in text.split()]
    words = [word for word in words if word]
    word_count = len(words)
    if word_count == 0:
        return -0.75
    question_terms = sum(1 for word in words[:4] if word in QUESTION_TERMS)
    bonus = min(max(word_count - 4, 0), 10) * 0.08
    bonus += min(sum(1 for word in words if len(word) > 4 and word not in QUESTION_TERMS), 4) * 0.05
    if "?" in text:
        bonus += 0.25
    if question_terms:
        bonus += 0.25 * min(question_terms, 2)
    if word_count >= 8:
        bonus += 0.15
    if word_count <= 3:
        bonus -= 0.4
    if "open_question" in claim_type.casefold():
        bonus += 0.15
    if provenance_payload.get("facet") == "open_questions":
        bonus += 0.1
    return bonus


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
    note_row = conn.execute(
        "SELECT note_text FROM provenance_event WHERE provenance_event_key_v1=?",
        (event_key,),
    ).fetchone()
    note_payload = parse_note_text(note_row["note_text"]) if note_row is not None else {}
    artifact_hash = note_payload.get("artifact_hash")
    if not isinstance(artifact_hash, str):
        artifact_hash = None
    yield_summaries = _family_yields_for_events(conn, [(event_key, artifact_hash)])
    return yield_summaries.get(
        event_key,
        {
            "work": 0,
            "source_claim": 0,
            "extraction_detected_entity": 0,
            "source_relationship": 0,
            "source_access": 0,
        },
    )


def _family_yields_for_events(
    conn: sqlite3.Connection,
    event_artifacts: list[tuple[str, str | None]],
) -> dict[str, dict[str, int]]:
    event_artifacts_by_key: dict[str, str | None] = {}
    for event_key, artifact_hash in event_artifacts:
        if event_key not in event_artifacts_by_key:
            event_artifacts_by_key[event_key] = artifact_hash
    if not event_artifacts_by_key:
        return {}
    values_clause = ", ".join("(?, ?)" for _ in event_artifacts_by_key)
    params: list[Any] = []
    for event_key, artifact_hash in event_artifacts_by_key.items():
        params.extend([event_key, artifact_hash])
    rows = conn.execute(
        f"""
        WITH requested_events(event_key, artifact_hash) AS (
            VALUES {values_clause}
        ),
        work_counts AS (
            SELECT requested_events.event_key AS event_key, COUNT(*) AS count
            FROM requested_events
            JOIN work ON work.provenance_event_ref = requested_events.event_key
            GROUP BY requested_events.event_key
        ),
        source_claim_counts AS (
            SELECT requested_events.event_key AS event_key, COUNT(*) AS count
            FROM requested_events
            JOIN source_claim ON source_claim.provenance_event_ref = requested_events.event_key
            GROUP BY requested_events.event_key
        ),
        extraction_detected_entity_counts AS (
            SELECT requested_events.event_key AS event_key, COUNT(*) AS count
            FROM requested_events
            JOIN extraction_detected_entity ON extraction_detected_entity.provenance_event_ref = requested_events.event_key
            GROUP BY requested_events.event_key
        ),
        source_relationship_counts AS (
            SELECT requested_events.event_key AS event_key, COUNT(*) AS count
            FROM requested_events
            JOIN source_relationship ON source_relationship.provenance_event_ref = requested_events.event_key
            GROUP BY requested_events.event_key
        ),
        source_access_matches AS (
            SELECT requested_events.event_key AS event_key, access.source_access_id AS source_access_id
            FROM requested_events
            JOIN work ON work.provenance_event_ref = requested_events.event_key
            JOIN source_access AS access ON access.work_id = work.work_id
            UNION
            SELECT requested_events.event_key AS event_key, access.source_access_id AS source_access_id
            FROM requested_events
            JOIN source_access AS access
              ON requested_events.artifact_hash IS NOT NULL
             AND requested_events.artifact_hash <> ''
             AND access.source_lead_id LIKE ('source-lead:' || requested_events.artifact_hash || ':%')
        ),
        source_access_counts AS (
            SELECT event_key, COUNT(*) AS count
            FROM source_access_matches
            GROUP BY event_key
        )
        SELECT requested_events.event_key,
               COALESCE(work_counts.count, 0) AS work_count,
               COALESCE(source_claim_counts.count, 0) AS source_claim_count,
               COALESCE(extraction_detected_entity_counts.count, 0) AS extraction_detected_entity_count,
               COALESCE(source_relationship_counts.count, 0) AS source_relationship_count,
               COALESCE(source_access_counts.count, 0) AS source_access_count
        FROM requested_events
        LEFT JOIN work_counts USING (event_key)
        LEFT JOIN source_claim_counts USING (event_key)
        LEFT JOIN extraction_detected_entity_counts USING (event_key)
        LEFT JOIN source_relationship_counts USING (event_key)
        LEFT JOIN source_access_counts USING (event_key)
        ORDER BY requested_events.event_key
        """,
        tuple(params),
    ).fetchall()
    summaries: dict[str, dict[str, int]] = {
        event_key: {
            "work": 0,
            "source_claim": 0,
            "extraction_detected_entity": 0,
            "source_relationship": 0,
            "source_access": 0,
        }
        for event_key in event_artifacts_by_key
    }
    for row in rows:
        event_key = str(row["event_key"])
        summaries[event_key] = {
            "work": int(row["work_count"]),
            "source_claim": int(row["source_claim_count"]),
            "extraction_detected_entity": int(row["extraction_detected_entity_count"]),
            "source_relationship": int(row["source_relationship_count"]),
            "source_access": int(row["source_access_count"]),
        }
    return summaries


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
    event_artifacts: list[tuple[str, str | None]] = []
    for row in rows:
        note_payload = parse_note_text(row["note_text"])
        if note_payload.get("subject_id") != subject_id:
            continue
        facet = note_payload.get("facet")
        if not isinstance(facet, str) or not facet:
            continue
        event_key = str(row["provenance_event_key_v1"])
        artifact_hash = note_payload.get("artifact_hash")
        if not isinstance(artifact_hash, str):
            artifact_hash = None
        history.append(
            {
                "event_id": int(row["provenance_event_id"]),
                "event_key": event_key,
                "run_id": None if row["run_id"] is None else str(row["run_id"]),
                "event_timestamp": str(row["event_timestamp"]),
                "facet": facet,
                "prompt_bundle_id": note_payload.get("prompt_bundle_id"),
                "cycle_depth": note_payload.get("cycle_depth"),
            }
        )
        event_artifacts.append((event_key, artifact_hash))
    yields_by_event = _family_yields_for_events(conn, event_artifacts)
    for entry in history:
        yields = yields_by_event.get(
            entry["event_key"],
            {
                "work": 0,
                "source_claim": 0,
                "extraction_detected_entity": 0,
                "source_relationship": 0,
                "source_access": 0,
            },
        )
        entry["yields"] = yields
        entry["total_yield"] = sum(yields.values())
    return history


def provenance_map_by_key(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["event_key"]: entry for entry in history}


def lead_scope_sql(
    subject_id: str,
    work_ids: list[int],
    *,
    table_alias: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    workspace_column = "workspace_id" if table_alias is None else f"{table_alias}.workspace_id"
    work_id_column = "work_id" if table_alias is None else f"{table_alias}.work_id"
    if work_ids:
        placeholders = ", ".join("?" for _ in work_ids)
        return (
            f"({workspace_column}=? OR {work_id_column} IN ({placeholders}))",
            (subject_id, *work_ids),
        )
    return f"{workspace_column}=?", (subject_id,)


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


def run_ids_for_events(conn: sqlite3.Connection, event_keys: set[str]) -> dict[str, str]:
    if not event_keys:
        return {}
    placeholders = ", ".join("?" for _ in sorted(event_keys))
    rows = conn.execute(
        f"""
        SELECT provenance_event_key_v1, run_id
        FROM provenance_event
        WHERE provenance_event_key_v1 IN ({placeholders})
        """,
        tuple(sorted(event_keys)),
    ).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        if row["run_id"] is None:
            continue
        result[str(row["provenance_event_key_v1"])] = str(row["run_id"])
    return result


def extraction_outcome_counts(
    conn: sqlite3.Connection,
    *,
    source_locus_id: str | None,
    locators: list[str],
    subject_id: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    requested_locators: list[str] = []
    seen_locators: set[str] = set()
    if source_locus_id:
        requested_locators.append(source_locus_id)
        seen_locators.add(source_locus_id)
    for locator in locators:
        if locator not in seen_locators:
            requested_locators.append(locator)
            seen_locators.add(locator)
    if not requested_locators:
        return {
            "successful_extractions": 0,
            "failed_extractions": 0,
            "capture_count": 0,
            "capture_ids": [],
            "extraction_ids": [],
            "related_run_ids": [],
        }
    lookup_values = ", ".join("(?)" for _ in requested_locators)
    query = f"""
    WITH requested_locators(locator) AS (
        VALUES {lookup_values}
    ),
    matching_captures AS (
        SELECT capture.capture_event_id, capture.provenance_event_ref
        FROM capture_event AS capture
        JOIN requested_locators
          ON requested_locators.locator = capture.source_locus_ref
        WHERE capture.workspace_id=?
        UNION
        SELECT capture.capture_event_id, capture.provenance_event_ref
        FROM capture_event AS capture
        JOIN requested_locators
          ON requested_locators.locator = capture.original_locator
        WHERE capture.workspace_id=?
    )
    SELECT capture_event_id, provenance_event_ref
    FROM matching_captures
    ORDER BY capture_event_id
    """
    run_ids: set[str] = set()
    capture_rows = conn.execute(
        query, tuple(requested_locators) + (subject_id, subject_id)
    ).fetchall()
    capture_ids = [int(row["capture_event_id"]) for row in capture_rows]
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
    provenance_event_keys = {
        str(row["provenance_event_ref"])
        for row in capture_rows + extraction_rows
        if row["provenance_event_ref"] is not None
    }
    run_ids = set(run_ids_for_events(conn, provenance_event_keys).values())
    successful = 0
    failed = 0
    unknown_statuses: set[str] = set()
    for row in extraction_rows:
        success, failed_delta, unknown_status = classify_extraction_status(row["extraction_status"])
        successful += success
        failed += failed_delta
        if unknown_status is not None:
            unknown_statuses.add(unknown_status)
    if warnings is not None and unknown_statuses:
        warnings.extend(
            f"unknown extraction_status treated as failure for source-access lead scoring: {status!r}"
            for status in sorted(unknown_statuses)
        )
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
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    scope_sql, scope_params = lead_scope_sql(subject_id, work_ids, table_alias="access")
    accepted_placeholders, accepted_states = accepted_review_placeholder_sql()
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
        WHERE {scope_sql}
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
            warnings=warnings,
        )
        related_claims = 0
        related_works = 0
        if row["work_id"] is not None:
            related_works = 1
            related_claims = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM source_claim
                    WHERE about_object_ref=?
                      AND review_state IN ({accepted_placeholders})
                    """,
                    (work_ref(int(row["work_id"])), *accepted_states),
                ).fetchone()["count"]
            )
        related_entities = 0
        if metrics["capture_ids"]:
            capture_placeholders = ", ".join("?" for _ in metrics["capture_ids"])
            related_entities = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM extraction_detected_entity
                    WHERE capture_event_id IN ({capture_placeholders})
                      AND review_state IN ({accepted_placeholders})
                    """,
                    tuple(metrics["capture_ids"]) + accepted_states,
                ).fetchone()["count"]
            )
        related_claims_score = capped_count(related_claims, RELATED_SCORE_SIGNAL_CAP)
        related_entities_score = capped_count(related_entities, RELATED_SCORE_SIGNAL_CAP)
        related_yield = related_works + related_claims_score + related_entities_score
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
            + weights["claim_yield"] * related_claims_score
            + weights["entity_yield"] * related_entities_score
            + weights["successful_extraction"]
            * capped_count(signals["successful_extractions"], LEAD_SCORE_SIGNAL_CAP)
            - weights["failed_extraction_penalty"]
            * capped_count(signals["failed_extractions"], LEAD_SCORE_SIGNAL_CAP)
            - weights["zero_yield_penalty"]
            * capped_count(signals["zero_yield_attempts"], LEAD_SCORE_SIGNAL_CAP)
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
        if related_run_ids:
            score += SOURCE_DIVERSITY_BONUS_STEP * min(max(len(related_run_ids) - 1, 0), 4)
        leads.append(
            {
                "candidate_id": f"source_access:{int(row['source_access_id'])}",
                "lead_kind": "source_access",
                "object_ref": f"source_access:{int(row['source_access_id'])}",
                "facet": facet,
                "review_state": str(row["review_state"]),
                "label": safe_source_access_label(row),
                "score": bounded_candidate_score(score),
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
          AND is_open_question=1
          AND review_state IN ({placeholders})
        ORDER BY created_at DESC, source_claim_id ASC
        """,
        scope_params + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()
    leads: list[dict[str, Any]] = []
    for row in rows:
        provenance = history_by_event_key.get(str(row["provenance_event_ref"] or ""))
        score = (
            weights["open_lead"]
            + weights["claim_yield"]
            + open_question_quality_bonus(
                claim_text=str(row["claim_text"] or ""),
                claim_type=str(row["claim_type"] or ""),
                provenance_payload=provenance or {},
            )
        )
        leads.append(
            {
                "candidate_id": f"source_claim:{int(row['source_claim_id'])}",
                "lead_kind": "source_claim",
                "object_ref": f"source_claim:{int(row['source_claim_id'])}",
                "facet": "open_questions",
                "review_state": str(row["review_state"]),
                "label": truncate_text(str(row["claim_text"]), max_length=160),
                "score": bounded_candidate_score(score),
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
    enabled_facets: list[str],
    history_by_event_key: dict[str, dict[str, Any]],
    weights: dict[str, float],
    warnings: list[str] | None = None,
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
        WHERE entity.workspace_id=?
          AND entity.review_state IN ({placeholders})
        ORDER BY entity.detected_entity_id
        """,
        (subject_id,) + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()
    leads: list[dict[str, Any]] = []
    for row in rows:
        entity_type = str(row["entity_type"] or "")
        facet = _pick_facet_for_entity_type(entity_type, enabled_facets)
        success, failed, unknown_status = classify_extraction_status(row["extraction_status"])
        if unknown_status is not None and warnings is not None:
            warnings.append(
                f"unknown extraction_status treated as failure for detected entity lead scoring: {unknown_status!r}"
            )
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
                "score": bounded_candidate_score(score),
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
    history_by_event_key: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    review_placeholders = ", ".join("?" for _ in sorted(LEAD_REVIEW_STATES))
    rows = conn.execute(
        f"""
        WITH scoped_work_ids(work_id) AS (
            SELECT work_id
            FROM work
            WHERE workspace_id=?
            UNION
            SELECT work_id
            FROM work_subject
            WHERE subject_object_ref=?
        )
        SELECT work.work_id, work.title, work.review_state, work.provenance_event_ref
        FROM work
        JOIN scoped_work_ids USING (work_id)
        WHERE work.review_state IN ({review_placeholders})
        ORDER BY work.work_id
        """,
        (subject_id, subject_id) + tuple(sorted(LEAD_REVIEW_STATES)),
    ).fetchall()
    leads: list[dict[str, Any]] = []
    for row in rows:
        provenance = history_by_event_key.get(str(row["provenance_event_ref"] or ""))
        score = weights["open_lead"] + weights["work_yield"]
        leads.append(
            {
                "candidate_id": f"work:{int(row['work_id'])}",
                "lead_kind": "work",
                "object_ref": f"work:{int(row['work_id'])}",
                "facet": "works",
                "review_state": str(row["review_state"]),
                "label": truncate_text(str(row["title"] or f"work:{int(row['work_id'])}")),
                "score": bounded_candidate_score(score),
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
                "related_run_ids": sorted(
                    {
                        provenance["run_id"] if provenance and provenance.get("run_id") else None,
                    }
                    - {None}
                ),
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
        capped_productive_runs = capped_count(
            signal_bucket["productive_runs"], FACET_SCORE_SIGNAL_CAP
        )
        capped_open_leads = capped_count(signal_bucket["open_leads"], FACET_SCORE_SIGNAL_CAP)
        capped_works = capped_count(signal_bucket["works"], FACET_SCORE_SIGNAL_CAP)
        capped_claims = capped_count(signal_bucket["claims"], FACET_SCORE_SIGNAL_CAP)
        capped_entities = capped_count(signal_bucket["entities"], FACET_SCORE_SIGNAL_CAP)
        capped_relationships = capped_count(signal_bucket["relationships"], FACET_SCORE_SIGNAL_CAP)
        capped_successful_extractions = capped_count(
            signal_bucket["successful_extractions"], FACET_SCORE_SIGNAL_CAP
        )
        capped_failed_extractions = capped_count(
            signal_bucket["failed_extractions"], FACET_SCORE_SIGNAL_CAP
        )
        distinct_run_ids = {
            str(event["run_id"])
            for event in history_by_facet[facet]
            if isinstance(event.get("run_id"), str) and event.get("run_id")
        }
        apply_recent_low_yield_penalty = bool(
            last_run_zero_yield[facet] and capped_productive_runs == 0 and capped_open_leads == 0
        )
        score = (
            weights["productive_run"] * capped_productive_runs
            + weights["open_lead"] * capped_open_leads
            + weights["work_yield"] * capped_works
            + weights["claim_yield"] * capped_claims
            + weights["entity_yield"] * capped_entities
            + weights["relationship_yield"] * capped_relationships
            + weights["successful_extraction"] * capped_successful_extractions
            - weights["failed_extraction_penalty"] * capped_failed_extractions
            - weights["zero_yield_penalty"]
            * capped_count(signal_bucket["zero_yield_runs"], FACET_SCORE_SIGNAL_CAP)
            - (weights["recent_low_yield_penalty"] if apply_recent_low_yield_penalty else 0.0)
            + SOURCE_DIVERSITY_BONUS_STEP * min(max(len(distinct_run_ids) - 1, 0), 4)
        )
        if no_prior_history:
            score += weights["bootstrap_bias"] * (len(enabled_facets) - facet_order[facet])
        reason_codes: list[str] = []
        if no_prior_history:
            reason_codes.append("bootstrap_no_prior_productivity")
        if capped_productive_runs:
            reason_codes.append("productive_history")
        if capped_open_leads:
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
                "score": bounded_candidate_score(score),
                "selected": False,
                "supporting_facet": False,
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
    lead_candidates = [item for item in lead_candidates if item["facet"] in facet_order]
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
    facet_score_by_name = {item["facet"]: item for item in facet_scores}
    productive_leads = [
        item for item in lead_scores if float(item["score"]) > 0.0 and item["facet"] in facet_score_by_name
    ]
    if productive_leads:
        selected = productive_leads[0]
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
    should_call_llm = selected_object_ref is None

    selected_facet = top_facet["facet"] if not productive_leads else productive_leads[0]["facet"]
    selected_prompt_bundle_id = (
        top_facet["prompt_bundle_id"]
        if not productive_leads
        else facet_score_by_name[productive_leads[0]["facet"]]["prompt_bundle_id"]
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
        "should_call_llm": should_call_llm,
        "selected_object_ref": selected_object_ref,
        "selected_lead_kind": selected_lead_kind,
        "selected_source_locus_id": selected_source_locus_id,
        "selected_source_lead_id": selected_source_lead_id,
        "selected_label": selected_label,
        "selected_review_state": selected_review_state,
        "selection_score": bounded_candidate_score(score),
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
    selected_facet = selected_next_action.get("selected_facet")
    selected_facet_id = f"facet:{selected_facet}" if isinstance(selected_facet, str) else None
    for item in facet_scores:
        if selected_facet_id is not None and item.get("candidate_id") == selected_facet_id:
            continue
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
    warnings: list[str] = []
    work_ids = scope_work_ids(conn, subject["subject_id"])
    history = load_gather_history(conn, subject["subject_id"])
    history_by_event_key = provenance_map_by_key(history)
    source_access_leads = load_source_access_leads(
        conn,
        subject_id=subject["subject_id"],
        work_ids=work_ids,
        history_by_event_key=history_by_event_key,
        weights=DEFAULT_SCORING_WEIGHTS,
        warnings=warnings,
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
        enabled_facets=list(subject["enabled_facets"]),
        history_by_event_key=history_by_event_key,
        weights=DEFAULT_SCORING_WEIGHTS,
        warnings=warnings,
    )
    work_leads = load_work_leads(
        conn,
        subject_id=subject["subject_id"],
        history_by_event_key=history_by_event_key,
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
    selected_object_ref = next_action.get("selected_object_ref")
    selected_facet = next_action.get("selected_facet")
    for item in facet_scores:
        is_selected_facet = item["facet"] == selected_facet
        item["selected"] = selected_object_ref is None and is_selected_facet
        item["supporting_facet"] = selected_object_ref is not None and is_selected_facet
    for item in lead_scores:
        item["selected"] = False
    deferred = build_deferred_candidates(
        selected_next_action=next_action,
        facet_scores=facet_scores,
        lead_scores=lead_scores,
        max_deferred=args.max_deferred_candidates,
    )

    scoring_policy = {
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
    }
    selection_explanation = build_feedback_selection_explanation(
        subject_id=subject["subject_id"],
        workspace_id=subject["subject_id"],
        generated_at=generated_at,
        scoring_policy=scoring_policy,
        facet_scores=facet_scores,
        lead_scores=lead_scores,
        next_action=next_action,
        deferred=deferred,
        stage_name=args.feedback_plan_stage,
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
        "scoring_policy": scoring_policy,
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
        "selection_explanation": selection_explanation,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": [],
    }


def record_selection_explanation_ledger(db_path: Path, payload: dict[str, Any]) -> None:
    explanation = payload.get("selection_explanation")
    if not isinstance(explanation, dict):
        raise CandidateFeedbackError("selection_explanation is missing")
    warning_count = len(payload["warnings"]) if isinstance(payload.get("warnings"), list) else 0
    error_count = len(payload["errors"]) if isinstance(payload.get("errors"), list) else 0
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            cycle_event_id = cycle_evidence_ledger.build_cycle_event_id(
                run_id=str(explanation["explanation_id"]),
                started_at=str(payload["generated_at"]),
                workspace_ref=str(payload["subject"]["subject_id"]),
            )
            event_id = cycle_evidence_ledger.record_cycle_event_start(
                conn,
                cycle_event_id=cycle_event_id,
                run_id=str(explanation["explanation_id"]),
                workspace_id=str(payload["subject"]["subject_id"]),
                workspace_ref=str(payload["subject"]["subject_id"]),
                subject_key=str(payload["subject"]["subject_id"]),
                domain_pack_id=str(payload["subject"]["domain_pack"]),
                cycle_depth=payload["next_action"].get("cycle_depth")
                if isinstance(payload.get("next_action"), dict)
                else None,
                mode="selection_explanation",
                started_at=str(payload["generated_at"]),
                status="completed",
                final_feedback_plan_ref=None,
                warning_count=warning_count,
                error_count=error_count,
                metadata={
                    "selection_explanation_id": explanation["explanation_id"],
                    "selection_kind": explanation["selection_kind"],
                    "artifact_schema_version": payload["schema_version"],
                },
            )
            stage_id = cycle_evidence_ledger.record_cycle_stage_start(
                conn,
                cycle_event_id=event_id,
                run_id=str(explanation["explanation_id"]),
                stage_name=str(explanation.get("stage_name") or "build_candidate_feedback_plan"),
                stage_order=1,
                started_at=str(payload["generated_at"]),
                status="completed",
                required_stage=False,
                command_name="build_candidate_feedback_plan.py",
                validation_status="pass",
                metadata={"selection_explanation_id": explanation["explanation_id"]},
            )
            cycle_evidence_ledger.record_cycle_stage_finish(
                conn,
                stage_event_id=stage_id,
                status="completed",
                ended_at=str(payload["generated_at"]),
            )
            policy_id = str(payload["scoring_policy"]["policy_id"])
            for candidate in explanation.get("considered_candidates", []):
                if not isinstance(candidate, dict):
                    continue
                cycle_evidence_ledger.record_cycle_candidate_considered(
                    conn,
                    cycle_event_id=event_id,
                    stage_event_id=stage_id,
                    candidate_kind=str(candidate.get("candidate_type") or "feedback_candidate"),
                    candidate_ref_type="selection_explanation",
                    candidate_ref_id=str(candidate.get("candidate_id") or ""),
                    candidate_label=None
                    if candidate.get("label") is None
                    else str(candidate.get("label")),
                    score=candidate.get("score")
                    if isinstance(candidate.get("score"), (int, float))
                    else None,
                    score_policy_id=policy_id,
                    rationale=None
                    if candidate.get("rationale") is None
                    else str(candidate.get("rationale")),
                    reason={
                        "selection_explanation_id": explanation["explanation_id"],
                        "reason_codes": candidate.get("reason_codes", []),
                        "eligibility_status": candidate.get("eligibility_status"),
                    },
                    selected=bool(candidate.get("selected")),
                )
            for candidate in explanation.get("excluded_candidates", []):
                if not isinstance(candidate, dict):
                    continue
                cycle_evidence_ledger.record_cycle_candidate_excluded(
                    conn,
                    cycle_event_id=event_id,
                    stage_event_id=stage_id,
                    candidate_kind=str(candidate.get("candidate_type") or "feedback_candidate"),
                    candidate_ref_type="selection_explanation",
                    candidate_ref_id=str(candidate.get("candidate_id") or ""),
                    candidate_label=None
                    if candidate.get("label") is None
                    else str(candidate.get("label")),
                    exclusion_reason=str(candidate.get("reason") or "deferred_by_feedback_plan"),
                    policy_id=policy_id,
                    retryable=bool(candidate.get("retryable", True)),
                )
            cycle_evidence_ledger.record_cycle_event_finish(
                conn,
                cycle_event_id=event_id,
                status="completed",
                ended_at=str(payload["generated_at"]),
            )
    finally:
        conn.close()


def render_text_plan(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"subject_id={payload['subject']['subject_id']}",
        f"domain_pack={payload['subject']['domain_pack']}",
        f"selected_facet={payload['next_action']['selected_facet']}",
        f"selected_prompt_bundle_id={payload['next_action']['selected_prompt_bundle_id']}",
        f"action_kind={payload['next_action']['action_kind']}",
        f"should_call_llm={str(payload['next_action']['should_call_llm']).lower()}",
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

        if args.record_selection_ledger:
            record_selection_explanation_ledger(
                canonical_store.resolve_db_path(args.db),
                payload,
            )

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
