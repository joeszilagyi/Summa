#!/usr/bin/env python3
"""Build a dry-run candidate feedback plan from later reviewed discoveries."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
    REPO_ROOT / "tools" / "source_db_tools",
    REPO_ROOT,
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.candidate_feedback_contract import (  # noqa: E402
    ACCEPTED_REVIEW_STATES,
    APPEND_ONLY_TARGETS,
    PENDING_REVIEW_STATES,
    PROPOSAL_KINDS,
    SCHEMA_VERSION,
)
from tools.validators.validate_candidate_feedback_plan import EXIT_PASS  # noqa: E402
from tools.validators.validate_candidate_feedback_plan import validate_candidate_feedback_plan  # noqa: E402
from tools.validators.validate_correction_ledger import EXIT_PASS as EXIT_CORRECTION_PASS  # noqa: E402
from tools.validators.validate_correction_ledger import validate_correction_ledger  # noqa: E402
from tools.validators.validate_evidence_locator import EXIT_PASS as EXIT_EVIDENCE_PASS  # noqa: E402
from tools.validators.validate_evidence_locator import validate_evidence_locator  # noqa: E402
from tools.validators.validate_field_review_state import EXIT_PASS as EXIT_FIELD_REVIEW_PASS  # noqa: E402
from tools.validators.validate_field_review_state import validate_field_review_state  # noqa: E402


SCRIPT_PATH = "tools/scripts/build_candidate_feedback_plan.py"


class CandidateFeedbackError(RuntimeError):
    """Raised when planner inputs are missing or malformed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the source SQLite database.")
    parser.add_argument("--correction-ledger", help="Optional correction-ledger JSON path.")
    parser.add_argument(
        "--field-review-state",
        action="append",
        default=[],
        help="Optional field-review-state JSON path. Repeat to include multiple files.",
    )
    parser.add_argument(
        "--evidence-locator",
        action="append",
        default=[],
        help="Optional evidence-locator JSON path. Repeat to include multiple files.",
    )
    parser.add_argument("--output-json", help="Optional JSON path for the emitted candidate-feedback plan.")
    parser.add_argument("--generated-at", help="Optional RFC3339 timestamp override for deterministic tests.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_existing_file(raw_path: str) -> Path:
    path = resolve_path(raw_path)
    if not path.exists():
        raise CandidateFeedbackError(f"input path does not exist: {path}")
    if not path.is_file():
        raise CandidateFeedbackError(f"input path is not a file: {path}")
    return path


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def now_rfc3339() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_label(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().casefold().split())


def text_fingerprint(value: str | None) -> str:
    text = normalize_label(value)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_object_ref(value: str | None) -> tuple[str | None, int | None]:
    if not isinstance(value, str) or ":" not in value:
        return None, None
    namespace, object_id = value.rsplit(":", 1)
    try:
        return namespace, int(object_id)
    except ValueError:
        return None, None


def read_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("PRAGMA user_version").fetchone()
    return None if row is None else int(row[0])


def load_correction_state(path: str | None) -> set[str]:
    if path is None:
        return set()
    result, exit_code = validate_correction_ledger(resolve_existing_file(path))
    if exit_code != EXIT_CORRECTION_PASS:
        first = result["errors"][0]["message"] if result.get("errors") else "unknown error"
        raise CandidateFeedbackError(f"correction ledger failed validation: {first}")
    resolution = result.get("resolution", {})
    superseded = resolution.get("superseded_object_refs", [])
    return set(superseded if isinstance(superseded, list) else [])


def load_field_review_state(paths: list[str]) -> set[tuple[str, str, str]]:
    disputed: set[tuple[str, str, str]] = set()
    for raw_path in paths:
        path = resolve_existing_file(raw_path)
        result, exit_code = validate_field_review_state(path)
        if exit_code != EXIT_FIELD_REVIEW_PASS:
            first = result["errors"][0]["message"] if result.get("errors") else "unknown error"
            raise CandidateFeedbackError(f"field review state failed validation: {first}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        record_locator = payload.get("record_locator", {})
        record_family = record_locator.get("record_family")
        if not isinstance(record_family, str):
            continue
        latest_by_field: dict[str, dict[str, Any]] = {}
        for entry in payload.get("field_reviews", []):
            if not isinstance(entry, dict):
                continue
            field_path = entry.get("field_path")
            if isinstance(field_path, str):
                latest_by_field[field_path] = entry
        for field_path, entry in latest_by_field.items():
            state = entry.get("state")
            fingerprint = entry.get("value_fingerprint")
            if state in {"disputed", "demoted"} and isinstance(fingerprint, str):
                disputed.add((record_family, field_path, fingerprint))
    return disputed


def load_evidence_locators(paths: list[str]) -> dict[str, dict[str, Any]]:
    evidence_map: dict[str, dict[str, Any]] = {}
    for raw_path in paths:
        path = resolve_existing_file(raw_path)
        result, exit_code = validate_evidence_locator(path)
        if exit_code != EXIT_EVIDENCE_PASS:
            first = result["errors"][0]["message"] if result.get("errors") else "unknown error"
            raise CandidateFeedbackError(f"evidence locator failed validation: {first}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence_id = payload.get("evidence_locator_id")
        if isinstance(evidence_id, str):
            evidence_map[evidence_id] = payload
    return evidence_map


def optional_expr(columns: set[str], *names: str, fallback: str = "NULL") -> str:
    for name in names:
        if name in columns:
            return name
    return fallback


def fetch_earlier_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "extraction_detected_entity"):
        return []
    columns = table_columns(conn, "extraction_detected_entity")
    query = f"""
        SELECT
          detected_entity_id,
          {optional_expr(columns, 'entity_label', fallback='NULL')} AS entity_label,
          {optional_expr(columns, 'entity_type', fallback='NULL')} AS entity_type,
          {optional_expr(columns, 'review_state', fallback='NULL')} AS review_state,
          {optional_expr(columns, 'authority_record_id', fallback='NULL')} AS authority_record_id,
          {optional_expr(columns, 'confidence_score', fallback='NULL')} AS confidence_score,
          {optional_expr(columns, 'record_last_updated', fallback='NULL')} AS record_last_updated,
          {optional_expr(columns, 'provenance_event_ref', 'provenance_event_key_v1', fallback='NULL')} AS provenance_event_ref
        FROM extraction_detected_entity
        ORDER BY detected_entity_id
    """
    rows = []
    for row in conn.execute(query).fetchall():
        review_state = row["review_state"] if isinstance(row["review_state"], str) else ""
        if review_state not in PENDING_REVIEW_STATES:
            continue
        if row["authority_record_id"] is not None:
            continue
        rows.append(row)
    return rows


def fetch_later_authorities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "authority_record"):
        return []
    columns = table_columns(conn, "authority_record")
    query = f"""
        SELECT
          authority_record_id,
          {optional_expr(columns, 'preferred_label', fallback='NULL')} AS preferred_label,
          {optional_expr(columns, 'authority_type', fallback='NULL')} AS authority_type,
          {optional_expr(columns, 'review_state', fallback='NULL')} AS review_state,
          {optional_expr(columns, 'confidence_score', fallback='NULL')} AS confidence_score,
          {optional_expr(columns, 'record_last_updated', fallback='NULL')} AS record_last_updated,
          {optional_expr(columns, 'provenance_event_ref', 'provenance_event_key_v1', fallback='NULL')} AS provenance_event_ref
        FROM authority_record
        ORDER BY authority_record_id
    """
    rows = []
    for row in conn.execute(query).fetchall():
        review_state = row["review_state"] if isinstance(row["review_state"], str) else ""
        if review_state in ACCEPTED_REVIEW_STATES:
            rows.append(row)
    return rows


def fetch_later_relationships(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "source_relationship"):
        return []
    columns = table_columns(conn, "source_relationship")
    query = f"""
        SELECT
          source_relationship_id,
          {optional_expr(columns, 'from_object_ref', fallback='NULL')} AS from_object_ref,
          {optional_expr(columns, 'to_object_ref', fallback='NULL')} AS to_object_ref,
          {optional_expr(columns, 'predicate', fallback='NULL')} AS predicate,
          {optional_expr(columns, 'review_state', fallback='NULL')} AS review_state,
          {optional_expr(columns, 'confidence_score', fallback='NULL')} AS confidence_score,
          {optional_expr(columns, 'record_last_updated', fallback='NULL')} AS record_last_updated,
          {optional_expr(columns, 'provenance_event_ref', 'provenance_event_key_v1', fallback='NULL')} AS provenance_event_ref,
          {optional_expr(columns, 'evidence_locator_ref', fallback='NULL')} AS evidence_locator_ref
        FROM source_relationship
        ORDER BY source_relationship_id
    """
    rows = []
    for row in conn.execute(query).fetchall():
        review_state = row["review_state"] if isinstance(row["review_state"], str) else ""
        if review_state in ACCEPTED_REVIEW_STATES:
            rows.append(row)
    return rows


def fetch_later_claims(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "source_claim"):
        return []
    columns = table_columns(conn, "source_claim")
    query = f"""
        SELECT
          source_claim_id,
          {optional_expr(columns, 'about_object_ref', fallback='NULL')} AS about_object_ref,
          {optional_expr(columns, 'claim_text', fallback='NULL')} AS claim_text,
          {optional_expr(columns, 'claim_type', fallback='NULL')} AS claim_type,
          {optional_expr(columns, 'review_state', fallback='NULL')} AS review_state,
          {optional_expr(columns, 'confidence_score', fallback='NULL')} AS confidence_score,
          {optional_expr(columns, 'record_last_updated', fallback='NULL')} AS record_last_updated,
          {optional_expr(columns, 'provenance_event_ref', 'provenance_event_key_v1', fallback='NULL')} AS provenance_event_ref,
          {optional_expr(columns, 'evidence_locator_ref', fallback='NULL')} AS evidence_locator_ref
        FROM source_claim
        ORDER BY source_claim_id
    """
    rows = []
    for row in conn.execute(query).fetchall():
        review_state = row["review_state"] if isinstance(row["review_state"], str) else ""
        if review_state in ACCEPTED_REVIEW_STATES:
            rows.append(row)
    return rows


def provenance_list(*values: Any) -> list[str]:
    refs = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str) and value.startswith("prov:") and value not in seen:
            seen.add(value)
            refs.append(value)
    return refs


def evidence_details(evidence_map: dict[str, dict[str, Any]], evidence_id: str | None) -> tuple[list[str], list[str]]:
    if not isinstance(evidence_id, str) or evidence_id not in evidence_map:
        return [], []
    payload = evidence_map[evidence_id]
    highlight = payload.get("highlight", {})
    summaries = []
    for key in ("public_summary", "highlight_note"):
        value = highlight.get(key)
        if isinstance(value, str) and value.strip():
            summaries.append(value.strip())
    return [evidence_id], summaries


def proposal_id(kind: str, target_object_ref: str, *source_refs: str) -> str:
    seed = "|".join([kind, target_object_ref, *sorted(source_refs)])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"cfp:{kind}.{digest}"


def build_plan(
    *,
    conn: sqlite3.Connection,
    db_path: Path,
    correction_ledger_path: str | None,
    field_review_paths: list[str],
    evidence_locator_paths: list[str],
    generated_at: str | None,
) -> dict[str, Any]:
    superseded_refs = load_correction_state(correction_ledger_path)
    disputed_fields = load_field_review_state(field_review_paths)
    evidence_map = load_evidence_locators(evidence_locator_paths)

    earlier_candidates = fetch_earlier_candidates(conn)
    later_authorities = fetch_later_authorities(conn)
    later_relationships = fetch_later_relationships(conn)
    later_claims = fetch_later_claims(conn)

    proposals: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for row in earlier_candidates:
        target_ref = f"detected_entity:{row['detected_entity_id']}"
        if target_ref in superseded_refs:
            skipped.append({"target_object_ref": target_ref, "reason": "superseded_by_correction_ledger"})
            continue

        entity_label = row["entity_label"] if isinstance(row["entity_label"], str) else None
        entity_type = row["entity_type"] if isinstance(row["entity_type"], str) else None
        earlier_ts = row["record_last_updated"] if isinstance(row["record_last_updated"], str) else ""

        matching_authorities = [
            candidate
            for candidate in later_authorities
            if normalize_label(candidate["preferred_label"]) == normalize_label(entity_label)
            and (
                entity_type is None
                or candidate["authority_type"] is None
                or normalize_label(candidate["authority_type"]) == normalize_label(entity_type)
            )
            and (not earlier_ts or not isinstance(candidate["record_last_updated"], str) or candidate["record_last_updated"] >= earlier_ts)
        ]
        if len(matching_authorities) == 1:
            authority = matching_authorities[0]
            score = float(authority["confidence_score"]) if isinstance(authority["confidence_score"], (int, float)) else 0.9
            proposals.append(
                {
                    "proposal_id": proposal_id("update_candidate", target_ref, f"authority:{authority['authority_record_id']}"),
                    "proposal_kind": "update_candidate",
                    "target_record_family": "entity",
                    "target_object_ref": target_ref,
                    "source_object_refs": [f"authority:{authority['authority_record_id']}"],
                    "append_only_target": "field_review_state",
                    "score": round(score, 3),
                    "rationale": "unique later reviewed authority match can improve the earlier unresolved detected entity",
                    "preserved_target_provenance_refs": provenance_list(row["provenance_event_ref"]),
                    "preserved_source_provenance_refs": provenance_list(authority["provenance_event_ref"]),
                    "evidence_locator_refs": [],
                    "evidence_summaries": [],
                    "proposed_changes": {
                        "field_path": "authority_record_ref",
                        "proposed_value": f"authority:{authority['authority_record_id']}",
                        "source_preference": authority["preferred_label"],
                    },
                }
            )
        elif len(matching_authorities) > 1:
            best_score = max(
                float(candidate["confidence_score"]) if isinstance(candidate["confidence_score"], (int, float)) else 0.75
                for candidate in matching_authorities
            )
            proposals.append(
                {
                    "proposal_id": proposal_id(
                        "review_task",
                        target_ref,
                        *[f"authority:{candidate['authority_record_id']}" for candidate in matching_authorities],
                    ),
                    "proposal_kind": "review_task",
                    "target_record_family": "entity",
                    "target_object_ref": target_ref,
                    "source_object_refs": [f"authority:{candidate['authority_record_id']}" for candidate in matching_authorities],
                    "append_only_target": "review_queue",
                    "score": round(best_score * 0.85, 3),
                    "rationale": "multiple later reviewed authorities match the same earlier candidate and need operator resolution",
                    "preserved_target_provenance_refs": provenance_list(row["provenance_event_ref"]),
                    "preserved_source_provenance_refs": provenance_list(*(candidate["provenance_event_ref"] for candidate in matching_authorities)),
                    "evidence_locator_refs": [],
                    "evidence_summaries": [],
                    "proposed_changes": {
                        "review_task_reason": "ambiguous_authority_match",
                        "candidate_object_refs": [f"authority:{candidate['authority_record_id']}" for candidate in matching_authorities],
                    },
                }
            )
        else:
            skipped.append({"target_object_ref": target_ref, "reason": "no_later_reviewed_authority_match"})

        for relationship in later_relationships:
            related = relationship["from_object_ref"] == target_ref or relationship["to_object_ref"] == target_ref
            if not related:
                continue
            evidence_refs, summaries = evidence_details(evidence_map, relationship["evidence_locator_ref"])
            score = float(relationship["confidence_score"]) if isinstance(relationship["confidence_score"], (int, float)) else 0.8
            proposals.append(
                {
                    "proposal_id": proposal_id("relationship_candidate", target_ref, f"relationship:{relationship['source_relationship_id']}"),
                    "proposal_kind": "relationship_candidate",
                    "target_record_family": "relationship",
                    "target_object_ref": target_ref,
                    "source_object_refs": [f"relationship:{relationship['source_relationship_id']}"],
                    "append_only_target": "review_queue",
                    "score": round(score, 3),
                    "rationale": "later reviewed relationship discovery can extend earlier candidate map coverage without mutating the earlier record",
                    "preserved_target_provenance_refs": provenance_list(row["provenance_event_ref"]),
                    "preserved_source_provenance_refs": provenance_list(relationship["provenance_event_ref"]),
                    "evidence_locator_refs": evidence_refs,
                    "evidence_summaries": summaries,
                    "proposed_changes": {
                        "predicate": relationship["predicate"],
                        "related_object_ref": relationship["to_object_ref"] if relationship["from_object_ref"] == target_ref else relationship["from_object_ref"],
                    },
                }
            )

        for claim in later_claims:
            if claim["about_object_ref"] != target_ref:
                continue
            claim_text = claim["claim_text"] if isinstance(claim["claim_text"], str) else None
            claim_type = claim["claim_type"] if isinstance(claim["claim_type"], str) else "claim_text"
            fingerprint = text_fingerprint(claim_text)
            evidence_refs, summaries = evidence_details(evidence_map, claim["evidence_locator_ref"])
            score = float(claim["confidence_score"]) if isinstance(claim["confidence_score"], (int, float)) else 0.75
            disputed = (
                ("detected_entity", claim_type, fingerprint) in disputed_fields
                or ("detected_entity", "preferred_label", fingerprint) in disputed_fields
                or ("entity", claim_type, fingerprint) in disputed_fields
            )
            if disputed:
                proposals.append(
                    {
                        "proposal_id": proposal_id("review_task", target_ref, f"claim:{claim['source_claim_id']}"),
                        "proposal_kind": "review_task",
                        "target_record_family": "assertion",
                        "target_object_ref": target_ref,
                        "source_object_refs": [f"claim:{claim['source_claim_id']}"],
                        "append_only_target": "review_queue",
                        "score": round(score, 3),
                        "rationale": "later reviewed claim matches a disputed field-review fingerprint and should route back to operator review",
                        "preserved_target_provenance_refs": provenance_list(row["provenance_event_ref"]),
                        "preserved_source_provenance_refs": provenance_list(claim["provenance_event_ref"]),
                        "evidence_locator_refs": evidence_refs,
                        "evidence_summaries": summaries,
                        "proposed_changes": {
                            "review_task_reason": "disputed_field_value",
                            "field_path": claim_type,
                            "proposed_value": claim_text,
                        },
                    }
                )
            else:
                proposals.append(
                    {
                        "proposal_id": proposal_id("update_candidate", target_ref, f"claim:{claim['source_claim_id']}"),
                        "proposal_kind": "update_candidate",
                        "target_record_family": "assertion",
                        "target_object_ref": target_ref,
                        "source_object_refs": [f"claim:{claim['source_claim_id']}"],
                        "append_only_target": "field_review_state",
                        "score": round(score, 3),
                        "rationale": "later reviewed claim can contribute a non-destructive field-level candidate update",
                        "preserved_target_provenance_refs": provenance_list(row["provenance_event_ref"]),
                        "preserved_source_provenance_refs": provenance_list(claim["provenance_event_ref"]),
                        "evidence_locator_refs": evidence_refs,
                        "evidence_summaries": summaries,
                        "proposed_changes": {
                            "field_path": claim_type,
                            "proposed_value": claim_text,
                        },
                    }
                )

    proposals.sort(key=lambda item: (-float(item["score"]), item["proposal_kind"], item["target_object_ref"], item["proposal_id"]))
    for index, proposal in enumerate(proposals, start=1):
        proposal["rank"] = index

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or now_rfc3339(),
        "source": {
            "database_name": db_path.name,
            "schema_version": read_schema_version(conn),
            "correction_ledger_applied": correction_ledger_path is not None,
            "field_review_state_count": len(field_review_paths),
            "evidence_locator_count": len(evidence_locator_paths),
            "dry_run": True,
        },
        "counts": {
            "earlier_candidates_considered": len(earlier_candidates),
            "later_discoveries_considered": len(later_authorities) + len(later_relationships) + len(later_claims),
            "proposals_emitted": len(proposals),
            "skipped_targets": len(skipped),
        },
        "proposals": proposals,
        "skipped": skipped,
        "warnings": [],
        "errors": [],
    }
    return payload


def render_text(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    lines = [
        f"schema_version={payload['schema_version']}",
        f"generated_at={payload['generated_at']}",
        "earlier_candidates_considered={earlier_candidates_considered} later_discoveries_considered={later_discoveries_considered} proposals_emitted={proposals_emitted} skipped_targets={skipped_targets}".format(
            **counts
        ),
    ]
    for index, proposal in enumerate(payload["proposals"][:20]):
        lines.append(
            f"proposal[{index}]={proposal['proposal_kind']} target={proposal['target_object_ref']} score={proposal['score']} append_only_target={proposal['append_only_target']}"
        )
    for index, skipped in enumerate(payload["skipped"][:20]):
        lines.append(f"skipped[{index}]={skipped['target_object_ref']} reason={skipped['reason']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    db_path = resolve_existing_file(args.db)
    try:
        conn = connect_read_only(db_path)
        try:
            payload = build_plan(
                conn=conn,
                db_path=db_path,
                correction_ledger_path=args.correction_ledger,
                field_review_paths=args.field_review_state,
                evidence_locator_paths=args.evidence_locator,
                generated_at=args.generated_at,
            )
        finally:
            conn.close()
    except CandidateFeedbackError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output_json:
        atomic_write_json(resolve_path(args.output_json), payload)
    if args.output_json:
        report, exit_code = validate_candidate_feedback_plan(resolve_path(args.output_json))
    else:
        temp_path = db_path.parent / ".candidate_feedback_validation_tmp.json"
        atomic_write_json(temp_path, payload)
        try:
            report, exit_code = validate_candidate_feedback_plan(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)
    if exit_code != EXIT_PASS:
        print("Error: generated candidate feedback plan failed validation", file=sys.stderr)
        if report["errors"]:
            print(report["errors"][0]["code"], file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
