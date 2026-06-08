"""Shared canonical-store ingestion helpers for gather and acquisition artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from tools.source_db_tools import canonical_reconciliation, canonical_store
from tools.validators import (
    validate_gather_candidate_batch as validate_gather_candidate_batch_validator,
)
from tools.validators.validate_gather_candidate_batch import (
    EXIT_PASS as EXIT_GATHER_BATCH_PASS,
)
from tools.validators.validate_source_acquisition_execution import (
    EXIT_PASS as EXIT_EXECUTION_PASS,
)
from tools.validators.validate_source_acquisition_execution import (
    load_execution_artifacts,
    validate_execution_artifact_receipt,
)

INGEST_REPORT_SCHEMA_VERSION = "canonical-ingest-report.v1"
INGEST_TOOL_VERSION = "canonical-ingest.v1"
GATHER_INGEST_TOOL = "tools/scripts/ingest_gather_candidate_batch.py"
EXECUTION_INGEST_TOOL = "tools/scripts/ingest_execution_artifacts.py"


class CanonicalIngestError(RuntimeError):
    """Raised when canonical ingestion cannot continue safely."""


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def _reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def _no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(
                handle,
                object_pairs_hook=_no_duplicate_object_pairs,
                parse_constant=_reject_json_constant,
            )
    except FileNotFoundError as exc:
        raise CanonicalIngestError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise CanonicalIngestError(f"{label} could not be read: {path}") from exc
    except DuplicateJsonKeyError as exc:
        raise CanonicalIngestError(f"{label} is not valid JSON: {exc}") from exc
    except NonStandardJsonConstantError as exc:
        raise CanonicalIngestError(f"{label} is not valid JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalIngestError(
            f"{label} is not valid JSON: {path} (line {exc.lineno})"
        ) from exc
    if not isinstance(payload, dict):
        raise CanonicalIngestError(f"{label} must be a JSON object: {path}")
    return payload


def _iter_jsonl_records(path: Path, *, label: str) -> Iterable[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(
                        raw_line,
                        object_pairs_hook=_no_duplicate_object_pairs,
                        parse_constant=_reject_json_constant,
                    )
                except UnicodeDecodeError as exc:
                    raise CanonicalIngestError(f"{label} line {line_number} is not UTF-8") from exc
                if not isinstance(payload, dict):
                    raise CanonicalIngestError(
                        f"{label} line {line_number} must contain a JSON object"
                    )
                yield payload
    except FileNotFoundError as exc:
        raise CanonicalIngestError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise CanonicalIngestError(f"{label} could not be read: {path}") from exc


def _batch_insert_rows_if_fresh(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    provenance_event_ref: str,
    rows: list[dict[str, Any]],
    insert_sql: str,
    insert_params: Callable[[dict[str, Any]], tuple[Any, ...]],
    lookup_sql: str,
    lookup_params: Callable[[dict[str, Any]], tuple[Any, ...]],
    label: str,
) -> list[sqlite3.Row] | None:
    if not rows:
        return []
    existing = conn.execute(
        f"SELECT 1 FROM {table_name} WHERE provenance_event_ref=? LIMIT 1",
        (provenance_event_ref,),
    ).fetchone()
    if existing is not None:
        return None
    lookup_param_rows = [lookup_params(row) for row in rows]
    if len(set(lookup_param_rows)) != len(lookup_param_rows):
        return None
    savepoint_name = f"batch_insert_{table_name}"
    conn.execute(f"SAVEPOINT {savepoint_name}")
    try:
        conn.executemany(insert_sql, [insert_params(row) for row in rows])
    except sqlite3.IntegrityError:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        return None
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
    fetched_rows: list[sqlite3.Row] = []
    for lookup_params_row in lookup_param_rows:
        row = conn.execute(lookup_sql, lookup_params_row).fetchone()
        if row is None:
            raise CanonicalIngestError(f"{label} batch lookup failed after insert")
        fetched_rows.append(row)
    return fetched_rows


def load_validated_candidate_batch(batch_path: Path) -> tuple[dict[str, Any], str]:
    payload = _load_json_object(batch_path, label="candidate batch")
    result, exit_code = (
        validate_gather_candidate_batch_validator.validate_gather_candidate_batch_payload(
            payload,
            target=batch_path,
        )
    )
    if exit_code != EXIT_GATHER_BATCH_PASS:
        message = "; ".join(
            f"{error['code']}: {error['message']}" for error in result.get("errors", [])
        )
        raise CanonicalIngestError(
            f"gather candidate batch validation failed: {message or batch_path}"
        )
    return payload, hash_file(batch_path)


def load_validated_execution_artifacts(
    target: Path,
) -> tuple[dict[str, Any], dict[str, Path], dict[str, str]]:
    try:
        receipt = load_execution_artifacts(target)
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        DuplicateJsonKeyError,
        NonStandardJsonConstantError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise CanonicalIngestError(
            f"source acquisition execution validation failed: {exc}"
        ) from exc
    result, exit_code = validate_execution_artifact_receipt(receipt)
    if exit_code != EXIT_EXECUTION_PASS:
        message = "; ".join(
            f"{error['code']}: {error['message']}" for error in result.get("errors", [])
        )
        raise CanonicalIngestError(
            f"source acquisition execution validation failed: {message or target}"
        )
    return (
        receipt.execution_record,
        receipt.paths,
        receipt.input_hashes,
    )


def _new_report(
    *,
    ingest_kind: str,
    status: str,
    timestamp: str,
    input_paths: dict[str, str],
    input_hashes: dict[str, str],
    db_path: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": INGEST_REPORT_SCHEMA_VERSION,
        "ingest_kind": ingest_kind,
        "status": status,
        "timestamp": timestamp,
        "db_path": db_path,
        "input_paths": input_paths,
        "input_hashes": input_hashes,
        "provenance_event": None,
        "counts": {
            "inserted": {},
            "updated": {},
            "intended": {},
            "skipped": {},
            "deduped": {},
            "reconciled": {},
            "contradicted": {},
        },
        "warnings": [],
        "review_state_defaults": {},
        "transaction_status": "not_started",
    }


def _bump(report: dict[str, Any], bucket: str, key: str, amount: int = 1) -> None:
    target = report["counts"][bucket]
    target[key] = int(target.get(key, 0)) + amount


def _append_warning(
    report: dict[str, Any], message: str, *, candidate_id: str | None = None
) -> None:
    payload: dict[str, Any] = {"message": message}
    if candidate_id is not None:
        payload["candidate_id"] = candidate_id
    report["warnings"].append(payload)


def _safe_json_text(value: dict[str, Any] | list[Any] | str) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


RAW_TEXT_CANDIDATE_TYPES = {
    "open_question",
    "raw_candidate_text",
    "timeline_item",
}


def _candidate_structured_payload(candidate: dict[str, Any]) -> dict[str, Any] | None:
    candidate_type = candidate.get("candidate_type")
    if candidate_type in RAW_TEXT_CANDIDATE_TYPES:
        return None
    raw_text = candidate.get("text")
    if not isinstance(raw_text, str):
        return None
    try:
        parsed = json.loads(
            raw_text,
            object_pairs_hook=_no_duplicate_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (DuplicateJsonKeyError, NonStandardJsonConstantError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _structured_claim_text(candidate: dict[str, Any], structured: dict[str, Any] | None) -> str:
    if structured is not None:
        return _safe_json_text(structured)
    text = str(candidate.get("text") or "").strip()
    first_line = text.splitlines()[0] if text else ""
    collapsed = " ".join(first_line.split())
    return collapsed[:240] or "claim-fallback-empty"


def _structured_claim_type(candidate_type: str, structured: dict[str, Any] | None) -> str:
    if structured is not None:
        claim_type = structured.get("claim_type")
        if isinstance(claim_type, str) and claim_type.strip():
            return claim_type.strip()
    return f"candidate_{candidate_type}"


def _normalize_key_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = _safe_json_text(value)
    text = "" if value is None else str(value)
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.casefold().split())


def _key_scope(workspace_id: str | None) -> str:
    return (
        workspace_id.strip() if isinstance(workspace_id, str) and workspace_id.strip() else "global"
    )


def _structured_about_object_ref(
    structured: dict[str, Any] | None,
) -> str | None:
    if structured is None:
        return None
    for key in ("about_object_ref", "subject_object_ref", "from_object_ref"):
        value = structured.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _candidate_workspace_id(batch: dict[str, Any]) -> str | None:
    subject = batch.get("subject")
    if not isinstance(subject, dict):
        return None
    subject_id = subject.get("subject_id")
    return subject_id if isinstance(subject_id, str) and subject_id.strip() else None


def _batch_provenance_note(batch: dict[str, Any], *, batch_hash: str, batch_path: Path) -> str:
    subject = batch.get("subject", {}) if isinstance(batch.get("subject"), dict) else {}
    pack = batch.get("domain_pack", {}) if isinstance(batch.get("domain_pack"), dict) else {}
    prompt_bundle = (
        batch.get("prompt_bundle", {}) if isinstance(batch.get("prompt_bundle"), dict) else {}
    )
    facet = batch.get("facet", {}) if isinstance(batch.get("facet"), dict) else {}
    prior_state = batch.get("prior_state", {}) if isinstance(batch.get("prior_state"), dict) else {}
    feedback_plan = (
        batch.get("feedback_plan", {}) if isinstance(batch.get("feedback_plan"), dict) else {}
    )
    payload: dict[str, Any] = {
        "artifact_path": str(batch_path),
        "artifact_hash": batch_hash,
        "schema_version": batch.get("schema_version"),
        "run_id": batch.get("run_id"),
        "mode": batch.get("mode"),
        "iteration_mode": batch.get("iteration_mode"),
        "cycle_depth": batch.get("cycle_depth"),
        "previous_run_ids": batch.get("previous_run_ids")
        if isinstance(batch.get("previous_run_ids"), list)
        else [],
        "prior_state_hash": prior_state.get("context_hash"),
        "feedback_plan_hash": feedback_plan.get("plan_hash"),
        "next_action_id": feedback_plan.get("next_action_id"),
        "scoring_policy_id": feedback_plan.get("scoring_policy_id"),
        "applied_facet": feedback_plan.get("applied_facet"),
        "selected_object_ref": feedback_plan.get("selected_object_ref"),
        "engine": batch.get("engine", {}).get("engine_name")
        if isinstance(batch.get("engine"), dict)
        else None,
        "subject_id": subject.get("subject_id"),
        "domain_pack": pack.get("pack_id"),
        "facet": facet.get("name"),
        "prompt_bundle_id": prompt_bundle.get("bundle_id"),
        "candidate_count": len(batch.get("candidates", []))
        if isinstance(batch.get("candidates"), list)
        else 0,
    }
    lines = ["gather_candidate_batch_ingest"]
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value if str(item).strip())
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


def _execution_provenance_note(
    execution_record: dict[str, Any],
    *,
    paths: dict[str, Path],
    input_hashes: dict[str, str],
) -> str:
    payload = {
        "execution_record_path": str(paths["execution_record"]),
        "capture_events_path": str(paths["capture_events"]),
        "extraction_records_path": str(paths["extraction_records"]),
        "input_hashes": input_hashes,
        "schema_version": execution_record.get("schema_version"),
        "run_id": execution_record.get("run_id"),
        "adapter_id": execution_record.get("adapter_id"),
        "adapter_type": execution_record.get("adapter_type"),
        "workspace_id": execution_record.get("workspace_id"),
        "handoff_hash": execution_record.get("input_handoff_hash"),
        "status": execution_record.get("status"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _work_key_for_candidate(
    candidate: dict[str, Any],
    structured: dict[str, Any] | None,
    *,
    workspace_id: str | None,
) -> str:
    if structured is not None:
        for field in ("work_key", "work_key_v1", "source_work_key"):
            value = structured.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    normalized_title = canonical_reconciliation.normalize_title(
        structured.get("title") if structured is not None else None
    )
    normalized_type = canonical_reconciliation.normalize_authority_label(
        structured.get("work_type") if structured is not None else None
    )
    normalized_type = normalized_type or "work"
    identifiers = canonical_reconciliation.candidate_identifiers(structured)
    for identifier in identifiers:
        try:
            normalized = canonical_reconciliation.normalize_external_identifier(
                identifier["scheme"],
                identifier["value"],
            )
            return canonical_store.stable_write_key(
                "work",
                _key_scope(workspace_id),
                canonical_reconciliation.normalize_work_key(
                    title=normalized_title,
                    work_type=normalized_type,
                    identifier_scheme=normalized["scheme"],
                    identifier_value=normalized["value"],
                )
                or f"identifier:{normalized['scheme']}:{normalized['value']}",
            )
        except canonical_reconciliation.CanonicalReconciliationError:
            continue
    source_identifier = canonical_reconciliation.normalize_locator(
        structured.get("canonical_url") if structured is not None else None,
    )
    if source_identifier is None and structured is not None:
        source_identifier = canonical_reconciliation.normalize_locator(
            structured.get("original_locator")
        )
    normalized_title_key = canonical_reconciliation.normalize_work_key(
        title=normalized_title,
        work_type=normalized_type,
        source_identifier=source_identifier,
        publication_year=None,
    )
    if normalized_title_key:
        return canonical_store.stable_write_key(
            "work",
            _key_scope(workspace_id),
            normalized_title_key,
        )
    if normalized_title:
        return canonical_store.stable_write_key(
            "work",
            _key_scope(workspace_id),
            "title",
            normalized_type,
            normalized_title,
        )
    fallback_work_title = _default_work_title(candidate)
    return canonical_store.stable_write_key(
        "work",
        _key_scope(workspace_id),
        "candidate",
        _normalize_key_text(fallback_work_title) or "work-candidate-fallback",
    )


def _claim_key_for_candidate(
    candidate: dict[str, Any],
    *,
    workspace_id: str | None,
    claim_text: str,
    claim_type: str,
    about_object_ref: str | None,
) -> str:
    normalized_claim = _normalize_key_text(claim_text)
    normalized_type = _normalize_key_text(claim_type)
    if normalized_claim:
        return canonical_store.stable_write_key(
            "claim",
            _key_scope(workspace_id),
            normalized_type,
            about_object_ref or "about:unknown",
            normalized_claim,
        )
    return canonical_store.stable_write_key(
        "claim",
        _key_scope(workspace_id),
        normalized_type,
        about_object_ref or "about:unknown",
        "claim-fallback-empty",
    )


def _source_lead_key_for_candidate(
    candidate: dict[str, Any],
    *,
    workspace_id: str | None,
    structured: dict[str, Any] | None,
) -> str:
    locator = None
    if structured is not None:
        locator = canonical_reconciliation.normalize_locator(structured.get("canonical_url"))
        if locator is None:
            locator = canonical_reconciliation.normalize_locator(structured.get("original_locator"))
    if locator is None:
        locator = (
            _normalize_key_text(candidate.get("candidate_id"))
            or "source-lead-candidate-fallback"
        )
    return canonical_store.stable_write_key(
        "source-lead",
        _key_scope(workspace_id),
        locator,
    )


def _default_work_title(candidate: dict[str, Any]) -> str:
    text = str(candidate.get("text") or "").strip()
    first_line = text.splitlines()[0] if text else ""
    return first_line[:240] or "work-candidate-fallback"


def _apply_curation_counts(report: dict[str, Any], counts: dict[str, int]) -> None:
    mapping = {
        "work_deduped": ("deduped", "work"),
        "authority_reconciled": ("reconciled", "authority_reconciliation"),
        "authority_merged": ("deduped", "authority"),
        "claims_contradicted": ("contradicted", "source_claim"),
        "relationships_contradicted": ("contradicted", "source_relationship"),
    }
    for key, amount in counts.items():
        if amount <= 0:
            continue
        bucket_key = mapping.get(key)
        if bucket_key is None:
            continue
        _bump(report, bucket_key[0], bucket_key[1], amount)


def ingest_candidate_batch(
    conn: sqlite3.Connection,
    batch: dict[str, Any],
    *,
    batch_path: Path,
    batch_hash: str,
    dry_run: bool = False,
    strict: bool = True,
    db_path: Path | None = None,
) -> dict[str, Any]:
    created_at = canonical_store._normalize_timestamp(
        batch.get("created_at"),
        field_name="candidate_batch.created_at",
    )
    report = _new_report(
        ingest_kind="candidate_batch",
        status="dry_run" if dry_run else "completed",
        timestamp=created_at,
        input_paths={"candidate_batch": str(batch_path)},
        input_hashes={"candidate_batch": batch_hash},
        db_path=None if db_path is None else str(db_path),
    )
    report["review_state_defaults"] = {
        "work": canonical_store.DEFAULT_WORK_REVIEW_STATE,
        "source_access": canonical_store.DEFAULT_SOURCE_ACCESS_REVIEW_STATE,
        "source_claim": canonical_store.DEFAULT_SOURCE_CLAIM_REVIEW_STATE,
        "extraction_detected_entity": canonical_store.DEFAULT_DETECTED_ENTITY_REVIEW_STATE,
        "source_relationship": canonical_store.DEFAULT_SOURCE_RELATIONSHIP_REVIEW_STATE,
    }
    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        raise CanonicalIngestError("candidate batch candidates must be a list")
    workspace_id = _candidate_workspace_id(batch)
    provenance_event: canonical_store.ProvenanceEventRef | None = None
    provenance_event_key = ""
    if not dry_run:
        provenance_kwargs: dict[str, Any] = {
            "object_namespace": "gather_candidate_batch",
            "object_id": str(batch.get("run_id") or batch_hash),
            "event_type": "gather_candidate_batch_ingest",
            "tool_name": GATHER_INGEST_TOOL,
            "tool_version": INGEST_TOOL_VERSION,
            "run_id": str(batch.get("run_id") or ""),
            "event_timestamp": created_at,
            "note_text": _batch_provenance_note(
                batch, batch_hash=batch_hash, batch_path=batch_path
            ),
            "provenance_event_key_v1": f"prov:gather-ingest:{batch_hash}",
        }
        if workspace_id is not None:
            provenance_kwargs["source_object_namespace"] = "topic_subject"
            provenance_kwargs["source_object_id"] = workspace_id
        provenance_event = canonical_store.record_provenance_event(conn, **provenance_kwargs)
        provenance_event_key = provenance_event.event_key
        provenance_event_id = provenance_event.event_id
        report["provenance_event"] = {
            "event_id": provenance_event.event_id,
            "event_key": provenance_event.event_key,
        }
        report["transaction_status"] = "in_progress"
    else:
        report["transaction_status"] = "dry_run"

    work_refs: dict[str, str] = {}
    entity_candidates: list[dict[str, Any]] = []
    claim_work_items: set[tuple[str | None, str | None, str]] = set()
    relationship_work_items: set[tuple[str | None, str, str, str | None]] = set()
    use_batch_writes = (
        not dry_run
        and conn.execute(
            "SELECT 1 FROM work WHERE provenance_event_ref=? LIMIT 1",
            (provenance_event_key,),
        ).fetchone()
        is None
    )
    pending_source_access_work_records: list[dict[str, Any]] = []
    pending_source_access_lead_records: list[dict[str, Any]] = []
    pending_source_claim_records: list[dict[str, Any]] = []
    pending_source_relationship_records: list[dict[str, Any]] = []
    pending_entity_records: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise CanonicalIngestError("candidate rows must be JSON objects")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise CanonicalIngestError("candidate_id is required")
        candidate_type = candidate.get("candidate_type")
        if not isinstance(candidate_type, str) or not candidate_type:
            raise CanonicalIngestError(f"{candidate_id}: candidate_type is required")
        structured = _candidate_structured_payload(candidate)

        handled = False
        relationship_written = False
        if structured is not None and {"from_object_ref", "predicate"} <= set(structured):
            if dry_run:
                _bump(report, "intended", "source_relationship")
            else:
                pending_source_relationship_records.append(
                    {
                        "from_object_ref": str(structured["from_object_ref"]),
                        "predicate": str(structured["predicate"]),
                        "to_object_ref": structured.get("to_object_ref"),
                        "target_label": structured.get("target_label"),
                        "evidence_note": structured.get("evidence_note"),
                        "review_state": structured.get("review_state"),
                        "confidence_score": structured.get("confidence_score"),
                        "workspace_id": workspace_id,
                    }
                )
            relationship_work_items.add(
                (
                    workspace_id,
                    str(structured["from_object_ref"]),
                    str(structured["predicate"]).strip(),
                    None if structured.get("to_object_ref") is None else str(structured.get("to_object_ref")),
                )
            )
            relationship_written = True

        if candidate_type == "work":
            incoming_work_key = _work_key_for_candidate(
                candidate,
                structured,
                workspace_id=workspace_id,
            )
            work_key = incoming_work_key
            title = (
                structured.get("title")
                if structured is not None and isinstance(structured.get("title"), str)
                else _default_work_title(candidate)
            )
            work_type = (
                structured.get("work_type")
                if structured is not None and isinstance(structured.get("work_type"), str)
                else "work"
            )
            work_match: canonical_reconciliation.WorkMatch | None = None
            if structured is not None and not dry_run:
                try:
                    work_match = canonical_reconciliation.find_existing_work_match(
                        conn,
                        structured=structured,
                        work_type=work_type,
                        title=title,
                        workspace_id=workspace_id,
                    )
                except canonical_reconciliation.CanonicalReconciliationError as exc:
                    raise CanonicalIngestError(
                        f"{candidate_id}: deterministic work reconciliation failed: {exc}"
                    ) from exc
                if work_match is not None:
                    work_key = work_match.work_key_v1
            if dry_run:
                _bump(report, "intended", "work")
            else:
                work_result = canonical_store.upsert_work(
                    conn,
                    work_key_v1=work_key,
                    provenance_event_ref=provenance_event_key,
                    provenance_event_id=provenance_event_id,
                    work_type=work_type,
                    title=title,
                    rights_posture=structured.get("rights_posture") if structured else None,
                    refetchability_status=structured.get("refetchability_status")
                    if structured
                    else None,
                    raw_cite_text=structured.get("raw_cite_text") if structured else None,
                    workspace_id=workspace_id,
                    confidence_score=structured.get("confidence_score") if structured else None,
                    first_seen_at=created_at,
                    last_seen_at=created_at,
                    created_at=created_at,
                    record_last_updated=created_at,
                )
                _bump(report, "inserted" if work_result.created else "updated", "work")
                work_refs[candidate_id] = f"work:{work_result.row_id}"
                if structured is not None:
                    try:
                        for identifier in canonical_reconciliation.candidate_identifiers(
                            structured
                        ):
                            identifier_confidence = structured.get("confidence_score")
                            if work_match is not None and identifier_confidence is None:
                                identifier_confidence = work_match.confidence_score
                            identifier_result = canonical_reconciliation.record_work_identifier(
                                conn,
                                work_id=work_result.row_id,
                                scheme=identifier["scheme"],
                                value=identifier["value"],
                                confidence_score=identifier_confidence,
                                record_last_updated=created_at,
                            )
                            _bump(
                                report,
                                "inserted" if identifier_result.created else "updated",
                                "work_identifier",
                            )
                    except canonical_reconciliation.CanonicalReconciliationError as exc:
                        raise CanonicalIngestError(
                            f"{candidate_id}: deterministic work identifier handling failed: {exc}"
                        ) from exc
                if work_match is not None and incoming_work_key != work_key:
                    duplicate_result = canonical_reconciliation.record_work_duplicate_encounter(
                        conn,
                        work_id=work_result.row_id,
                        matched_by=work_match.method,
                        identity_key=work_match.identity_key,
                        provenance_event_ref=provenance_event_key,
                        incoming_work_key=incoming_work_key,
                        workspace_id=workspace_id,
                        encountered_at=created_at,
                    )
                    _bump(report, "deduped", "work")
                    _bump(
                        report,
                        "inserted" if duplicate_result.created else "updated",
                        "provenance_event",
                    )
                if structured is not None and (
                    isinstance(structured.get("canonical_url"), str)
                    or isinstance(structured.get("original_locator"), str)
                ):
                    pending_source_access_work_records.append(
                        {
                            "work_id": work_result.row_id,
                            "source_lead_id": _source_lead_key_for_candidate(
                                candidate,
                                workspace_id=workspace_id,
                                structured=structured,
                            ),
                            "original_locator": str(
                                structured.get("original_locator")
                                or structured.get("canonical_url")
                                or title
                            ),
                            "canonical_url": structured.get("canonical_url"),
                            "access_class": structured.get("access_class"),
                            "refetchability_status": structured.get("refetchability_status"),
                            "rights_posture": structured.get("rights_posture"),
                            "citation_hint": structured.get("citation_hint") or title,
                            "workspace_id": workspace_id,
                            "first_seen_at": created_at,
                            "last_seen_at": created_at,
                            "record_last_updated": created_at,
                        }
                    )
                claim_text = structured.get("claim_text") if structured else None
                if isinstance(claim_text, str) and claim_text.strip():
                    work_claim_type = _structured_claim_type(
                        candidate_type="work",
                        structured=structured,
                    )
                    if work_claim_type == "candidate_work":
                        work_claim_type = "candidate_work_claim"
                    pending_source_claim_records.append(
                        {
                            "source_claim_key_v1": _claim_key_for_candidate(
                                candidate,
                                workspace_id=workspace_id,
                                claim_text=claim_text,
                                claim_type=work_claim_type,
                                about_object_ref=work_refs[candidate_id],
                            ),
                            "about_object_ref": work_refs[candidate_id],
                            "claim_text": claim_text,
                            "public_summary": structured.get("public_summary")
                            if structured is not None
                            else None,
                            "claim_type": work_claim_type,
                            "workspace_id": workspace_id,
                            "confidence_score": (
                                structured.get("confidence_score") if structured is not None else None
                            ),
                            "created_at": created_at,
                            "record_last_updated": created_at,
                        }
                    )
                    claim_work_items.add((workspace_id, work_refs[candidate_id], work_claim_type))
            handled = True

        elif candidate_type == "source_lead":
            if dry_run:
                _bump(report, "intended", "source_access")
            else:
                source_lead_id = (
                    structured.get("source_lead_id")
                    if structured is not None and isinstance(structured.get("source_lead_id"), str)
                    else _source_lead_key_for_candidate(
                        candidate,
                        workspace_id=workspace_id,
                        structured=structured,
                    )
                )
                pending_source_access_lead_records.append(
                    {
                        "source_lead_id": source_lead_id,
                        "original_locator": str(
                            structured.get("original_locator")
                            if structured is not None and structured.get("original_locator")
                            else candidate["text"]
                        ),
                        "canonical_url": structured.get("canonical_url") if structured else None,
                        "access_class": structured.get("access_class") if structured else None,
                        "refetchability_status": structured.get("refetchability_status")
                        if structured
                        else None,
                        "rights_posture": structured.get("rights_posture") if structured else None,
                        "citation_hint": structured.get("citation_hint") if structured else None,
                        "workspace_id": workspace_id,
                        "first_seen_at": created_at,
                        "last_seen_at": created_at,
                        "record_last_updated": created_at,
                    }
                )
            handled = True

        elif candidate_type in {"person", "place"}:
            if dry_run:
                _bump(report, "intended", "extraction_detected_entity")
            else:
                entity_label = str(
                    structured.get("entity_label")
                    if structured is not None and structured.get("entity_label")
                    else structured.get("label")
                    if structured is not None and structured.get("label")
                    else candidate["text"]
                )
                pending_entity_records.append(
                    {
                        "entity_label": entity_label,
                        "normalized_label": structured.get("normalized_label") if structured else None,
                        "entity_type": structured.get("entity_type") if structured else candidate_type,
                        "confidence_score": structured.get("confidence_score") if structured else None,
                        "workspace_id": workspace_id,
                        "record_last_updated": created_at,
                        "structured": structured,
                    }
                )
            handled = True

        if candidate_type in {"open_question", "raw_candidate_text", "unknown", "timeline_item"}:
            if dry_run:
                _bump(report, "intended", "source_claim")
            else:
                claim_text = _structured_claim_text(candidate, structured)
                claim_type = _structured_claim_type(candidate_type, structured)
                pending_source_claim_records.append(
                    {
                        "source_claim_key_v1": _claim_key_for_candidate(
                            candidate,
                            workspace_id=workspace_id,
                            claim_text=claim_text,
                            claim_type=claim_type,
                            about_object_ref=_structured_about_object_ref(structured),
                        ),
                        "about_object_ref": _structured_about_object_ref(structured),
                        "claim_text": claim_text,
                        "public_summary": (
                            structured.get("public_summary") if structured is not None else None
                        ),
                        "claim_type": claim_type,
                        "workspace_id": workspace_id,
                        "confidence_score": (
                            structured.get("confidence_score") if structured is not None else None
                        ),
                        "created_at": created_at,
                        "record_last_updated": created_at,
                    }
                )
                claim_work_items.add(
                    (
                        workspace_id,
                        _structured_about_object_ref(structured),
                        claim_type,
                    )
                )
            handled = True
            if candidate_type == "unknown":
                _append_warning(
                    report,
                    "unknown candidate type preserved as a source claim",
                    candidate_id=candidate_id,
                )

        if not handled and not relationship_written:
            if strict:
                raise CanonicalIngestError(
                    f"candidate {candidate_id} of type {candidate_type} could not be mapped safely"
                )
            _bump(report, "skipped", "unmapped_candidate")
            _append_warning(
                report,
                f"candidate type {candidate_type} could not be mapped and was skipped",
                candidate_id=candidate_id,
            )

    if not dry_run:
        if use_batch_writes and pending_source_relationship_records:
            relationship_rows = _batch_insert_rows_if_fresh(
                conn,
                table_name="source_relationship",
                provenance_event_ref=provenance_event_key,
                rows=pending_source_relationship_records,
                insert_sql=(
                    """
                    INSERT INTO source_relationship (
                      from_object_ref,
                      to_object_ref,
                      predicate,
                      target_label,
                      evidence_note,
                      review_state,
                      publication_state,
                      authority_level,
                      public_blocker,
                      workspace_id,
                      confidence_score,
                      provenance_event_ref,
                      evidence_locator_ref,
                      created_at,
                      record_last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                insert_params=lambda row: (
                    row["from_object_ref"],
                    row["to_object_ref"],
                    row["predicate"],
                    row["target_label"],
                    row["evidence_note"],
                    canonical_store._normalize_review_state(
                        row.get("review_state"),
                        default=canonical_store.DEFAULT_SOURCE_RELATIONSHIP_REVIEW_STATE,
                    ),
                    None,
                    None,
                    None,
                    row["workspace_id"],
                    canonical_store._normalize_confidence_score(row.get("confidence_score")),
                    provenance_event_key,
                    None,
                    created_at,
                    created_at,
                ),
                lookup_sql=(
                    """
                    SELECT source_relationship_id
                    FROM source_relationship
                    WHERE from_object_ref=? AND to_object_ref IS ? AND predicate=? AND target_label IS ?
                      AND evidence_note IS ? AND workspace_id IS ?
                    """
                ),
                lookup_params=lambda row: (
                    row["from_object_ref"],
                    row["to_object_ref"],
                    row["predicate"],
                    row["target_label"],
                    row["evidence_note"],
                    row["workspace_id"],
                ),
                label="source_relationship",
            )
            if relationship_rows is not None:
                for _record in pending_source_relationship_records:
                    _bump(report, "inserted", "source_relationship")
            else:
                for record in pending_source_relationship_records:
                    result = canonical_store.record_source_relationship(
                        conn,
                        provenance_event_ref=provenance_event_key,
                        provenance_event_id=provenance_event_id,
                        from_object_ref=record["from_object_ref"],
                        predicate=record["predicate"],
                        to_object_ref=record["to_object_ref"],
                        target_label=record["target_label"],
                        evidence_note=record["evidence_note"],
                        review_state=record["review_state"],
                        confidence_score=record["confidence_score"],
                        workspace_id=record["workspace_id"],
                    )
                    _bump(report, "inserted" if result.created else "updated", "source_relationship")
        elif pending_source_relationship_records:
            for record in pending_source_relationship_records:
                result = canonical_store.record_source_relationship(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    provenance_event_id=provenance_event_id,
                    from_object_ref=record["from_object_ref"],
                    predicate=record["predicate"],
                    to_object_ref=record["to_object_ref"],
                    target_label=record["target_label"],
                    evidence_note=record["evidence_note"],
                    review_state=record["review_state"],
                    confidence_score=record["confidence_score"],
                    workspace_id=record["workspace_id"],
                )
                _bump(report, "inserted" if result.created else "updated", "source_relationship")

        if pending_source_access_work_records or pending_source_access_lead_records:
            source_access_records = [
                *pending_source_access_work_records,
                *pending_source_access_lead_records,
            ]
            if use_batch_writes:
                source_access_rows = _batch_insert_rows_if_fresh(
                    conn,
                    table_name="source_access",
                    provenance_event_ref=provenance_event_key,
                    rows=source_access_records,
                    insert_sql=(
                        """
                        INSERT INTO source_access (
                          work_id,
                          source_locus_id,
                          source_lead_id,
                          original_locator,
                          canonical_url,
                          access_class,
                          refetchability_status,
                          rights_posture,
                          citation_hint,
                          review_state,
                          publication_state,
                          authority_level,
                          public_blocker,
                          workspace_id,
                          provenance_event_ref,
                          first_seen_at,
                          last_seen_at,
                          record_last_updated
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    insert_params=lambda record: (
                        record.get("work_id"),
                        None,
                        record.get("source_lead_id"),
                        record["original_locator"],
                        record["canonical_url"],
                        record["access_class"],
                        record["refetchability_status"],
                        record["rights_posture"],
                        record["citation_hint"],
                        canonical_store.DEFAULT_SOURCE_ACCESS_REVIEW_STATE,
                        None,
                        None,
                        None,
                        record["workspace_id"],
                        provenance_event_key,
                        record["first_seen_at"],
                        record["last_seen_at"],
                        record["record_last_updated"],
                    ),
                    lookup_sql=(
                        """
                        SELECT source_access_id
                        FROM source_access
                        WHERE provenance_event_ref=? AND original_locator=? AND (
                          (? IS NOT NULL AND work_id IS ?)
                          OR (
                            ? IS NULL AND source_lead_id IS ? AND workspace_id IS ?
                          )
                        )
                        """
                    ),
                    lookup_params=lambda record: (
                        provenance_event_key,
                        record["original_locator"],
                        record.get("work_id"),
                        record.get("work_id"),
                        record.get("work_id"),
                        record.get("source_lead_id"),
                        record.get("workspace_id"),
                    ),
                    label="source_access",
                )
                if source_access_rows is not None:
                    for _record in source_access_records:
                        _bump(report, "inserted", "source_access")
                else:
                    for record in source_access_records:
                        if record.get("work_id") is not None:
                            result = canonical_store.record_source_access(
                                conn,
                                provenance_event_ref=provenance_event_key,
                                provenance_event_id=provenance_event_id,
                                work_id=record["work_id"],
                                source_lead_id=record["source_lead_id"],
                                original_locator=record["original_locator"],
                                canonical_url=record["canonical_url"],
                                access_class=record["access_class"],
                                refetchability_status=record["refetchability_status"],
                                rights_posture=record["rights_posture"],
                                citation_hint=record["citation_hint"],
                                workspace_id=record["workspace_id"],
                                first_seen_at=record["first_seen_at"],
                                last_seen_at=record["last_seen_at"],
                                record_last_updated=record["record_last_updated"],
                            )
                        else:
                            result = canonical_store.record_source_access(
                                conn,
                                provenance_event_ref=provenance_event_key,
                                provenance_event_id=provenance_event_id,
                                source_lead_id=record["source_lead_id"],
                                original_locator=record["original_locator"],
                                canonical_url=record["canonical_url"],
                                access_class=record["access_class"],
                                refetchability_status=record["refetchability_status"],
                                rights_posture=record["rights_posture"],
                                citation_hint=record["citation_hint"],
                                workspace_id=record["workspace_id"],
                                first_seen_at=record["first_seen_at"],
                                last_seen_at=record["last_seen_at"],
                                record_last_updated=record["record_last_updated"],
                            )
                        _bump(report, "inserted" if result.created else "updated", "source_access")
            else:
                for record in source_access_records:
                    if record.get("work_id") is not None:
                        result = canonical_store.record_source_access(
                            conn,
                            provenance_event_ref=provenance_event_key,
                            provenance_event_id=provenance_event_id,
                            work_id=record["work_id"],
                            source_lead_id=record["source_lead_id"],
                            original_locator=record["original_locator"],
                            canonical_url=record["canonical_url"],
                            access_class=record["access_class"],
                            refetchability_status=record["refetchability_status"],
                            rights_posture=record["rights_posture"],
                            citation_hint=record["citation_hint"],
                            workspace_id=record["workspace_id"],
                            first_seen_at=record["first_seen_at"],
                            last_seen_at=record["last_seen_at"],
                            record_last_updated=record["record_last_updated"],
                        )
                    else:
                        result = canonical_store.record_source_access(
                            conn,
                            provenance_event_ref=provenance_event_key,
                            provenance_event_id=provenance_event_id,
                            source_lead_id=record["source_lead_id"],
                            original_locator=record["original_locator"],
                            canonical_url=record["canonical_url"],
                            access_class=record["access_class"],
                            refetchability_status=record["refetchability_status"],
                            rights_posture=record["rights_posture"],
                            citation_hint=record["citation_hint"],
                            workspace_id=record["workspace_id"],
                            first_seen_at=record["first_seen_at"],
                            last_seen_at=record["last_seen_at"],
                            record_last_updated=record["record_last_updated"],
                        )
                    _bump(report, "inserted" if result.created else "updated", "source_access")

        if use_batch_writes and pending_source_claim_records:
            source_claim_rows = _batch_insert_rows_if_fresh(
                conn,
                table_name="source_claim",
                provenance_event_ref=provenance_event_key,
                rows=pending_source_claim_records,
                insert_sql=(
                    """
                    INSERT INTO source_claim (
                      source_claim_key_v1,
                      about_object_ref,
                      claim_text,
                      public_summary,
                      claim_type,
                      review_state,
                      publication_state,
                      authority_level,
                      public_blocker,
                      workspace_id,
                      is_open_question,
                      confidence_score,
                      provenance_event_ref,
                      evidence_locator_ref,
                      capture_event_id,
                      extraction_id,
                      created_at,
                      record_last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                insert_params=lambda record: (
                    record["source_claim_key_v1"],
                    record["about_object_ref"],
                    record["claim_text"],
                    record["public_summary"],
                    record["claim_type"],
                    canonical_store.DEFAULT_SOURCE_CLAIM_REVIEW_STATE,
                    None,
                    None,
                    None,
                    record["workspace_id"],
                    1
                    if canonical_store._is_open_question_claim(
                        record["claim_text"], record["claim_type"]
                    )
                    else 0,
                    canonical_store._normalize_confidence_score(record.get("confidence_score")),
                    provenance_event_key,
                    None,
                    None,
                    None,
                    record["created_at"],
                    record["record_last_updated"],
                ),
                lookup_sql=(
                    """
                    SELECT source_claim_id
                    FROM source_claim
                    WHERE source_claim_key_v1=?
                    """
                ),
                lookup_params=lambda record: (record["source_claim_key_v1"],),
                label="source_claim",
            )
            if source_claim_rows is not None:
                for _record in pending_source_claim_records:
                    _bump(report, "inserted", "source_claim")
            else:
                for record in pending_source_claim_records:
                    result = canonical_store.record_source_claim(
                        conn,
                        provenance_event_ref=provenance_event_key,
                        provenance_event_id=provenance_event_id,
                        source_claim_key_v1=record["source_claim_key_v1"],
                        about_object_ref=record["about_object_ref"],
                        claim_text=record["claim_text"],
                        public_summary=record["public_summary"],
                        claim_type=record["claim_type"],
                        workspace_id=record["workspace_id"],
                        confidence_score=record["confidence_score"],
                        created_at=record["created_at"],
                        record_last_updated=record["record_last_updated"],
                    )
                    _bump(report, "inserted" if result.created else "updated", "source_claim")
        elif pending_source_claim_records:
            for record in pending_source_claim_records:
                result = canonical_store.record_source_claim(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    provenance_event_id=provenance_event_id,
                    source_claim_key_v1=record["source_claim_key_v1"],
                    about_object_ref=record["about_object_ref"],
                    claim_text=record["claim_text"],
                    public_summary=record["public_summary"],
                    claim_type=record["claim_type"],
                    workspace_id=record["workspace_id"],
                    confidence_score=record["confidence_score"],
                    created_at=record["created_at"],
                    record_last_updated=record["record_last_updated"],
                )
                _bump(report, "inserted" if result.created else "updated", "source_claim")

        if use_batch_writes and pending_entity_records:
            entity_rows = _batch_insert_rows_if_fresh(
                conn,
                table_name="extraction_detected_entity",
                provenance_event_ref=provenance_event_key,
                rows=pending_entity_records,
                insert_sql=(
                    """
                    INSERT INTO extraction_detected_entity (
                      extraction_id,
                      capture_event_id,
                      entity_label,
                      normalized_label,
                      entity_type,
                      source_span_start,
                      source_span_end,
                      authority_record_id,
                      review_state,
                      confidence_score,
                      workspace_id,
                      provenance_event_ref,
                      record_last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                insert_params=lambda record: (
                    None,
                    None,
                    record["entity_label"],
                    record["normalized_label"],
                    record["entity_type"],
                    None,
                    None,
                    None,
                    canonical_store.DEFAULT_DETECTED_ENTITY_REVIEW_STATE,
                    canonical_store._normalize_confidence_score(record.get("confidence_score")),
                    record["workspace_id"],
                    provenance_event_key,
                    record["record_last_updated"],
                ),
                lookup_sql=(
                    """
                    SELECT detected_entity_id
                    FROM extraction_detected_entity
                    WHERE provenance_event_ref=? AND extraction_id IS ? AND capture_event_id IS ?
                      AND entity_label=? AND entity_type IS ? AND normalized_label IS ?
                      AND source_span_start IS NULL AND source_span_end IS NULL
                    """
                ),
                lookup_params=lambda record: (
                    provenance_event_key,
                    None,
                    None,
                    record["entity_label"],
                    record["entity_type"],
                    record["normalized_label"],
                ),
                label="extraction_detected_entity",
            )
            if entity_rows is not None:
                for record, entity_row in zip(pending_entity_records, entity_rows, strict=True):
                    _bump(report, "inserted", "extraction_detected_entity")
                    entity_candidates.append(
                        {
                            "detected_entity_id": int(entity_row["detected_entity_id"]),
                            "entity_label": record["entity_label"],
                            "entity_type": record["entity_type"],
                            "confidence_score": record["confidence_score"],
                            "structured": record["structured"],
                        }
                    )
            else:
                for record in pending_entity_records:
                    result = canonical_store.record_extraction_detected_entity(
                        conn,
                        provenance_event_ref=provenance_event_key,
                        provenance_event_id=provenance_event_id,
                        entity_label=record["entity_label"],
                        normalized_label=record["normalized_label"],
                        entity_type=record["entity_type"],
                        confidence_score=record["confidence_score"],
                        workspace_id=record["workspace_id"],
                        record_last_updated=record["record_last_updated"],
                    )
                    _bump(
                        report,
                        "inserted" if result.created else "updated",
                        "extraction_detected_entity",
                    )
                    entity_candidates.append(
                        {
                            "detected_entity_id": result.row_id,
                            "entity_label": record["entity_label"],
                            "entity_type": record["entity_type"],
                            "confidence_score": record["confidence_score"],
                            "structured": record["structured"],
                        }
                    )
        elif pending_entity_records:
            for record in pending_entity_records:
                result = canonical_store.record_extraction_detected_entity(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    provenance_event_id=provenance_event_id,
                    entity_label=record["entity_label"],
                    normalized_label=record["normalized_label"],
                    entity_type=record["entity_type"],
                    confidence_score=record["confidence_score"],
                    workspace_id=record["workspace_id"],
                    record_last_updated=record["record_last_updated"],
                )
                _bump(
                    report,
                    "inserted" if result.created else "updated",
                    "extraction_detected_entity",
                )
                entity_candidates.append(
                    {
                        "detected_entity_id": result.row_id,
                        "entity_label": record["entity_label"],
                        "entity_type": record["entity_type"],
                        "confidence_score": record["confidence_score"],
                        "structured": record["structured"],
                    }
                )

        if entity_candidates or claim_work_items or relationship_work_items:
            try:
                curation_counts = canonical_reconciliation.run_reconciliation_pass_for_ingest(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    workspace_id=workspace_id,
                    changed_at=created_at,
                    entity_candidates=entity_candidates,
                    source_run_id=str(batch.get("run_id") or ""),
                    claim_work_items=sorted(
                        claim_work_items,
                        key=lambda item: tuple("" if part is None else str(part) for part in item),
                    ),
                    relationship_work_items=sorted(
                        relationship_work_items,
                        key=lambda item: tuple("" if part is None else str(part) for part in item),
                    ),
                )
            except canonical_reconciliation.CanonicalReconciliationError as exc:
                raise CanonicalIngestError(f"candidate-batch reconciliation failed: {exc}") from exc
            _apply_curation_counts(report, curation_counts)
        report["transaction_status"] = "committed"
    return report


def ingest_execution_artifacts(
    conn: sqlite3.Connection,
    execution_record: dict[str, Any],
    *,
    paths: dict[str, Path],
    input_hashes: dict[str, str],
    capture_events: Iterable[dict[str, Any]] | None = None,
    extraction_records: Iterable[dict[str, Any]] | None = None,
    dry_run: bool = False,
    strict: bool = True,
    db_path: Path | None = None,
) -> dict[str, Any]:
    created_at = canonical_store._normalize_timestamp(
        execution_record.get("created_at"),
        field_name="execution_record.created_at",
    )
    report = _new_report(
        ingest_kind="execution_artifacts",
        status="dry_run" if dry_run else "completed",
        timestamp=created_at,
        input_paths={key: str(path) for key, path in paths.items() if key != "run_dir"},
        input_hashes=input_hashes,
        db_path=None if db_path is None else str(db_path),
    )
    report["review_state_defaults"] = {
        "capture_event": canonical_store.DEFAULT_CAPTURE_EVENT_REVIEW_STATE,
        "extraction_record": canonical_store.DEFAULT_EXTRACTION_RECORD_REVIEW_STATE,
        "extraction_detected_entity": canonical_store.DEFAULT_DETECTED_ENTITY_REVIEW_STATE,
        "source_relationship": canonical_store.DEFAULT_SOURCE_RELATIONSHIP_REVIEW_STATE,
    }
    provenance_event: canonical_store.ProvenanceEventRef | None = None
    provenance_event_key = ""
    if not dry_run:
        provenance_event = canonical_store.record_provenance_event(
            conn,
            object_namespace="source_acquisition_execution",
            object_id=str(execution_record.get("run_id") or input_hashes["execution_record"]),
            event_type="execution_artifact_ingest",
            tool_name=EXECUTION_INGEST_TOOL,
            tool_version=INGEST_TOOL_VERSION,
            run_id=str(execution_record.get("run_id") or ""),
            event_timestamp=created_at,
            note_text=_execution_provenance_note(
                execution_record, paths=paths, input_hashes=input_hashes
            ),
            provenance_event_key_v1=f"prov:execution-ingest:{input_hashes['execution_record']}",
        )
        provenance_event_key = provenance_event.event_key
        provenance_event_id = provenance_event.event_id
        report["provenance_event"] = {
            "event_id": provenance_event.event_id,
            "event_key": provenance_event.event_key,
        }
        report["transaction_status"] = "in_progress"
    else:
        report["transaction_status"] = "dry_run"

    capture_id_map: dict[str, int] = {}
    entity_candidates: list[dict[str, Any]] = []
    claim_work_items: list[tuple[str | None, str | None, str]] = []
    relationship_work_items: list[tuple[str | None, str, str, str | None]] = []
    capture_ids: set[str] = set()
    use_batch_writes = (
        not dry_run
        and conn.execute(
            "SELECT 1 FROM capture_event WHERE provenance_event_ref=? LIMIT 1",
            (provenance_event_key,),
        ).fetchone()
        is None
    )
    pending_capture_records: list[dict[str, Any]] = []
    capture_event_iter = (
        capture_events
        if capture_events is not None
        else _iter_jsonl_records(paths["capture_events"], label="capture events")
    )
    for record in capture_event_iter:
        original_locator = record.get("original_locator")
        locator_text = (
            _safe_json_text(original_locator)
            if original_locator is not None
            else str(
                record.get("normalized_local_path")
                or record.get("source_reference")
                or record.get("capture_id")
            )
        )
        capture_key = str(record.get("capture_id") or "")
        capture_ids.add(capture_key)
        if dry_run:
            _bump(report, "intended", "capture_event")
            continue
        if use_batch_writes:
            pending_capture_records.append(
                {
                    "capture_id": capture_key,
                    "original_locator": locator_text,
                    "captured_at": str(record.get("captured_at") or created_at),
                    "capture_method": str(record.get("capture_method") or "captured"),
                    "content_hash": record.get("content_hash"),
                    "byte_count": record.get("byte_count"),
                    "mime_type": record.get("content_type"),
                    "refetchability_status": record.get("refetchability_status"),
                    "workspace_id": record.get("workspace_id"),
                }
            )
        else:
            result = canonical_store.record_capture_event(
                conn,
                provenance_event_ref=provenance_event_key,
                provenance_event_id=provenance_event_id,
                original_locator=locator_text,
                captured_at=str(record.get("captured_at") or created_at),
                capture_method=str(record.get("capture_method") or "captured"),
                content_hash=record.get("content_hash"),
                byte_count=record.get("byte_count"),
                mime_type=record.get("content_type"),
                refetchability_status=record.get("refetchability_status"),
                review_state=canonical_store.DEFAULT_CAPTURE_EVENT_REVIEW_STATE,
                workspace_id=record.get("workspace_id"),
                record_last_updated=created_at,
            )
            capture_id_map[capture_key] = result.row_id
            _bump(report, "inserted" if result.created else "updated", "capture_event")

    if use_batch_writes:
        capture_rows = _batch_insert_rows_if_fresh(
            conn,
            table_name="capture_event",
            provenance_event_ref=provenance_event_key,
            rows=pending_capture_records,
            insert_sql=(
                """
                INSERT INTO capture_event (
                  work_id,
                  source_locus_ref,
                  original_locator,
                  captured_at,
                  capture_method,
                  content_hash,
                  byte_count,
                  mime_type,
                  byte_retention_status,
                  full_text_retention_status,
                  refetchability_status,
                  payload_storage_policy_class,
                  quality_warnings_json,
                  transient_payload_note,
                  review_state,
                  workspace_id,
                  public_blocker,
                  provenance_event_ref,
                  record_last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            insert_params=lambda row: (
                None,
                None,
                row["original_locator"],
                row["captured_at"],
                row["capture_method"],
                row.get("content_hash"),
                row.get("byte_count"),
                row.get("mime_type"),
                None,
                None,
                row.get("refetchability_status"),
                None,
                None,
                None,
                canonical_store.DEFAULT_CAPTURE_EVENT_REVIEW_STATE,
                row.get("workspace_id"),
                None,
                provenance_event_key,
                created_at,
            ),
            lookup_sql=(
                """
                SELECT capture_event_id
                FROM capture_event
                WHERE provenance_event_ref=? AND original_locator=? AND captured_at=? AND capture_method=?
                  AND content_hash IS ? AND workspace_id IS ?
                """
            ),
            lookup_params=lambda row: (
                provenance_event_key,
                row["original_locator"],
                row["captured_at"],
                row["capture_method"],
                row.get("content_hash"),
                row.get("workspace_id"),
            ),
            label="capture_event",
        )
        if capture_rows is not None:
            for record, row in zip(pending_capture_records, capture_rows, strict=True):
                capture_id_map[str(record["capture_id"])] = int(row["capture_event_id"])
                _bump(report, "inserted", "capture_event")
        else:
            for record in pending_capture_records:
                capture_key = str(record["capture_id"])
                result = canonical_store.record_capture_event(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    provenance_event_id=provenance_event_id,
                    original_locator=record["original_locator"],
                    captured_at=record["captured_at"],
                    capture_method=record["capture_method"],
                    content_hash=record.get("content_hash"),
                    byte_count=record.get("byte_count"),
                    mime_type=record.get("mime_type"),
                    refetchability_status=record.get("refetchability_status"),
                    review_state=canonical_store.DEFAULT_CAPTURE_EVENT_REVIEW_STATE,
                    workspace_id=record.get("workspace_id"),
                    record_last_updated=created_at,
                )
                capture_id_map[capture_key] = result.row_id
                _bump(report, "inserted" if result.created else "updated", "capture_event")

    extraction_record_iter = (
        extraction_records
        if extraction_records is not None
        else _iter_jsonl_records(paths["extraction_records"], label="extraction records")
    )
    pending_extraction_records: list[dict[str, Any]] = []
    for record in extraction_record_iter:
        capture_key = str(record.get("capture_id") or "")
        if dry_run:
            if capture_key not in capture_ids:
                if strict:
                    raise CanonicalIngestError(
                        f"extraction record references unknown capture_id: {capture_key}"
                    )
                _bump(report, "skipped", "missing_capture_reference")
                continue
            _bump(report, "intended", "extraction_record")
            continue

        canonical_capture_id = capture_id_map.get(capture_key)
        if canonical_capture_id is None:
            if strict:
                raise CanonicalIngestError(
                    f"extraction record references unknown capture_id: {capture_key}"
                )
            _bump(report, "skipped", "missing_capture_reference")
            continue
        if use_batch_writes:
            pending_extraction_records.append(
                {
                    "capture_event_id": canonical_capture_id,
                    "record": record,
                }
            )
        else:
            result = canonical_store.record_extraction_record(
                conn,
                provenance_event_ref=provenance_event_key,
                provenance_event_id=provenance_event_id,
                capture_event_id=canonical_capture_id,
                extractor_name=str(
                    execution_record.get("executor_name") or "execute_source_adapter.py"
                ),
                extractor_version=INGEST_TOOL_VERSION,
                extraction_method=str(record.get("extraction_method") or "extract"),
                summary_short=str(record.get("relative_path") or record.get("extraction_id") or ""),
                input_hash=record.get("input_hash"),
                output_hash=record.get("content_hash"),
                byte_count_in=record.get("byte_count_in"),
                byte_count_out=record.get("byte_count_out"),
                encoding_handling=record.get("encoding_result"),
                extraction_status=str(record.get("status") or "completed"),
                bad_utf8_handling=record.get("failure_reason")
                if record.get("encoding_result") == "invalid_utf8"
                else None,
                truncation_status=record.get("truncation_status"),
                hostile_replay_flags_json=canonical_store._normalize_json_text(
                    record.get("hostile_replay_flags"),
                    "hostile_replay_flags_json",
                ),
                review_state=canonical_store.DEFAULT_EXTRACTION_RECORD_REVIEW_STATE,
                workspace_id=record.get("workspace_id"),
                created_at=created_at,
                record_last_updated=created_at,
            )
            _bump(report, "inserted" if result.created else "updated", "extraction_record")

            detected_entities = record.get("detected_entities")
            if isinstance(detected_entities, list):
                for entity in detected_entities:
                    if not isinstance(entity, dict):
                        raise CanonicalIngestError("detected_entities entries must be objects")
                    entity_result = canonical_store.record_extraction_detected_entity(
                        conn,
                        provenance_event_ref=provenance_event_key,
                        provenance_event_id=provenance_event_id,
                        extraction_id=result.row_id,
                        capture_event_id=canonical_capture_id,
                        entity_label=str(
                            entity.get("entity_label") or entity.get("label") or ""
                        ),
                        normalized_label=entity.get("normalized_label"),
                        entity_type=entity.get("entity_type"),
                        confidence_score=entity.get("confidence_score"),
                        workspace_id=record.get("workspace_id"),
                        record_last_updated=created_at,
                    )
                    _bump(
                        report,
                        "inserted" if entity_result.created else "updated",
                        "extraction_detected_entity",
                    )
                    entity_candidates.append(
                        {
                            "detected_entity_id": entity_result.row_id,
                            "entity_label": str(
                                entity.get("entity_label") or entity.get("label") or ""
                            ),
                            "entity_type": entity.get("entity_type"),
                            "confidence_score": entity.get("confidence_score"),
                            "structured": entity,
                        }
                    )

    if use_batch_writes:
        extraction_rows = _batch_insert_rows_if_fresh(
            conn,
            table_name="extraction_record",
            provenance_event_ref=provenance_event_key,
            rows=pending_extraction_records,
            insert_sql=(
                """
                INSERT INTO extraction_record (
                  capture_event_id,
                  extractor_name,
                  extractor_version,
                  extraction_method,
                  summary_short,
                  input_hash,
                  output_hash,
                  byte_count_in,
                  byte_count_out,
                  encoding_handling,
                  extraction_status,
                  bad_utf8_handling,
                  truncation_status,
                  hostile_replay_flags_json,
                  review_state,
                  workspace_id,
                  public_blocker,
                  provenance_event_ref,
                  created_at,
                  record_last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            insert_params=lambda pending: (
                pending["capture_event_id"],
                str(execution_record.get("executor_name") or "execute_source_adapter.py"),
                INGEST_TOOL_VERSION,
                str(pending["record"].get("extraction_method") or "extract"),
                str(
                    pending["record"].get("relative_path")
                    or pending["record"].get("extraction_id")
                    or ""
                ),
                pending["record"].get("input_hash"),
                pending["record"].get("content_hash"),
                pending["record"].get("byte_count_in"),
                pending["record"].get("byte_count_out"),
                pending["record"].get("encoding_result"),
                str(pending["record"].get("status") or "completed"),
                pending["record"].get("failure_reason")
                if pending["record"].get("encoding_result") == "invalid_utf8"
                else None,
                pending["record"].get("truncation_status"),
                canonical_store._normalize_json_text(
                    pending["record"].get("hostile_replay_flags"),
                    "hostile_replay_flags_json",
                ),
                canonical_store.DEFAULT_EXTRACTION_RECORD_REVIEW_STATE,
                pending["record"].get("workspace_id"),
                None,
                provenance_event_key,
                created_at,
                created_at,
            ),
            lookup_sql=(
                """
                SELECT extraction_id
                FROM extraction_record
                WHERE provenance_event_ref=? AND capture_event_id=? AND extraction_method=? AND input_hash IS ?
                  AND output_hash IS ? AND created_at=?
                """
            ),
            lookup_params=lambda pending: (
                provenance_event_key,
                pending["capture_event_id"],
                str(pending["record"].get("extraction_method") or "extract"),
                pending["record"].get("input_hash"),
                pending["record"].get("content_hash"),
                created_at,
            ),
            label="extraction_record",
        )
        if extraction_rows is None:
            raise CanonicalIngestError("execution-artifact batch write failed for extraction records")
        pending_entity_records: list[dict[str, Any]] = []
        for pending, row in zip(pending_extraction_records, extraction_rows, strict=True):
            record = pending["record"]
            canonical_capture_id = pending["capture_event_id"]
            detected_entities = record.get("detected_entities")
            if isinstance(detected_entities, list):
                for entity in detected_entities:
                    if not isinstance(entity, dict):
                        raise CanonicalIngestError("detected_entities entries must be objects")
                    pending_entity_records.append(
                        {
                            "extraction_id": int(row["extraction_id"]),
                            "capture_event_id": canonical_capture_id,
                            "entity_label": str(
                                entity.get("entity_label") or entity.get("label") or ""
                            ),
                            "normalized_label": entity.get("normalized_label"),
                            "entity_type": entity.get("entity_type"),
                            "confidence_score": entity.get("confidence_score"),
                            "workspace_id": record.get("workspace_id"),
                            "structured": entity,
                        }
                    )

        if pending_entity_records:
            entity_rows = _batch_insert_rows_if_fresh(
                conn,
                table_name="extraction_detected_entity",
                provenance_event_ref=provenance_event_key,
                rows=pending_entity_records,
                insert_sql=(
                    """
                    INSERT INTO extraction_detected_entity (
                      extraction_id,
                      capture_event_id,
                      entity_label,
                      normalized_label,
                      entity_type,
                      source_span_start,
                      source_span_end,
                      authority_record_id,
                      review_state,
                      confidence_score,
                      workspace_id,
                      provenance_event_ref,
                      record_last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                insert_params=lambda row: (
                    row["extraction_id"],
                    row["capture_event_id"],
                    row["entity_label"],
                    row["normalized_label"],
                    row["entity_type"],
                    None,
                    None,
                    None,
                    canonical_store.DEFAULT_DETECTED_ENTITY_REVIEW_STATE,
                    row["confidence_score"],
                    row["workspace_id"],
                    provenance_event_key,
                    created_at,
                ),
                lookup_sql=(
                    """
                    SELECT detected_entity_id
                    FROM extraction_detected_entity
                    WHERE provenance_event_ref=? AND extraction_id=? AND capture_event_id=? AND entity_label=?
                      AND entity_type IS ? AND normalized_label IS ? AND source_span_start IS NULL
                      AND source_span_end IS NULL
                    """
                ),
                lookup_params=lambda row: (
                    provenance_event_key,
                    row["extraction_id"],
                    row["capture_event_id"],
                    row["entity_label"],
                    row["entity_type"],
                    row["normalized_label"],
                ),
                label="extraction_detected_entity",
            )
            if entity_rows is None:
                raise CanonicalIngestError(
                    "execution-artifact batch write failed for detected entities"
                )
            for row, entity_row in zip(pending_entity_records, entity_rows, strict=True):
                _bump(report, "inserted", "extraction_detected_entity")
                entity_candidates.append(
                    {
                        "detected_entity_id": int(entity_row["detected_entity_id"]),
                        "entity_label": row["entity_label"],
                        "entity_type": row["entity_type"],
                        "confidence_score": row["confidence_score"],
                        "structured": row["structured"],
                    }
                )

    if not dry_run:
        if entity_candidates or claim_work_items or relationship_work_items:
            try:
                curation_counts = canonical_reconciliation.run_reconciliation_pass_for_ingest(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    workspace_id=str(execution_record.get("workspace_id") or "") or None,
                    changed_at=created_at,
                    entity_candidates=entity_candidates,
                    source_run_id=str(execution_record.get("run_id") or ""),
                    claim_work_items=claim_work_items,
                    relationship_work_items=relationship_work_items,
                )
            except canonical_reconciliation.CanonicalReconciliationError as exc:
                raise CanonicalIngestError(
                    f"execution-artifact reconciliation failed: {exc}"
                ) from exc
            _apply_curation_counts(report, curation_counts)
        report["transaction_status"] = "committed"
    return report


def render_report_text(report: dict[str, Any]) -> str:
    lines = [
        f"schema_version={report['schema_version']}",
        f"ingest_kind={report['ingest_kind']}",
        f"status={report['status']}",
        f"timestamp={report['timestamp']}",
        f"transaction_status={report['transaction_status']}",
    ]
    if report.get("db_path") is not None:
        lines.append(f"db_path={report['db_path']}")
    if report.get("provenance_event") is not None:
        lines.append(f"provenance_event_key={report['provenance_event']['event_key']}")
    for bucket in (
        "inserted",
        "updated",
        "intended",
        "skipped",
        "deduped",
        "reconciled",
        "contradicted",
    ):
        entries = report["counts"][bucket]
        for key in sorted(entries):
            lines.append(f"{bucket}.{key}={entries[key]}")
    for warning in report["warnings"]:
        lines.append(
            "warning=" + str(warning.get("candidate_id") or warning.get("message") or "warning")
        )
    return "\n".join(lines) + "\n"
