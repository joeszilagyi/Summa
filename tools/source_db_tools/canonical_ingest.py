"""Shared canonical-store ingestion helpers for gather and acquisition artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from tools.source_db_tools import canonical_reconciliation, canonical_store
from tools.validators.validate_gather_candidate_batch import (
    EXIT_PASS as EXIT_GATHER_BATCH_PASS,
)
from tools.validators.validate_gather_candidate_batch import (
    validate_gather_candidate_batch,
)
from tools.validators.validate_source_acquisition_execution import (
    EXIT_PASS as EXIT_EXECUTION_PASS,
)
from tools.validators.validate_source_acquisition_execution import (
    load_execution_artifacts,
    validate_source_acquisition_execution,
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


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CanonicalIngestError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise CanonicalIngestError(f"{label} could not be read: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CanonicalIngestError(
            f"{label} is not valid JSON: {path} (line {exc.lineno})"
        ) from exc
    if not isinstance(payload, dict):
        raise CanonicalIngestError(f"{label} must be a JSON object: {path}")
    return payload


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CanonicalIngestError(f"{label} path does not exist: {path}") from exc
    except OSError as exc:
        raise CanonicalIngestError(f"{label} could not be read: {path}") from exc
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise CanonicalIngestError(
                f"{label} line {line_number} is not valid JSON (column {exc.colno})"
            ) from exc
        if not isinstance(value, dict):
            raise CanonicalIngestError(f"{label} line {line_number} must be a JSON object")
        records.append(value)
    return records


def load_validated_candidate_batch(batch_path: Path) -> tuple[dict[str, Any], str]:
    result, exit_code = validate_gather_candidate_batch(batch_path)
    if exit_code != EXIT_GATHER_BATCH_PASS:
        message = "; ".join(
            f"{error['code']}: {error['message']}" for error in result.get("errors", [])
        )
        raise CanonicalIngestError(
            f"gather candidate batch validation failed: {message or batch_path}"
        )
    return _load_json_object(batch_path, label="candidate batch"), hash_file(batch_path)


def load_validated_execution_artifacts(
    target: Path,
) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Path], dict[str, str]
]:
    result, exit_code = validate_source_acquisition_execution(target)
    if exit_code != EXIT_EXECUTION_PASS:
        message = "; ".join(
            f"{error['code']}: {error['message']}" for error in result.get("errors", [])
        )
        raise CanonicalIngestError(
            f"source acquisition execution validation failed: {message or target}"
        )
    execution_record, capture_events, extraction_records, paths = load_execution_artifacts(target)
    return (
        execution_record,
        capture_events,
        extraction_records,
        paths,
        {
            "execution_record": hash_file(paths["execution_record"]),
            "capture_events": hash_file(paths["capture_events"]),
            "extraction_records": hash_file(paths["extraction_records"]),
        },
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
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _candidate_structured_payload(candidate: dict[str, Any]) -> dict[str, Any] | None:
    raw_text = candidate.get("text")
    if not isinstance(raw_text, str):
        return None
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _structured_claim_text(candidate: dict[str, Any], structured: dict[str, Any] | None) -> str:
    if structured is not None:
        return _safe_json_text(structured)
    return str(candidate["text"])


def _structured_claim_type(candidate_type: str, structured: dict[str, Any] | None) -> str:
    if structured is not None:
        claim_type = structured.get("claim_type")
        if isinstance(claim_type, str) and claim_type.strip():
            return claim_type.strip()
    return f"candidate_{candidate_type}"


def _normalize_key_text(value: Any) -> str:
    if isinstance(value, dict) or isinstance(value, list):
        value = _safe_json_text(value)
    text = "" if value is None else str(value)
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.casefold().split())


def _key_scope(workspace_id: str | None) -> str:
    return workspace_id.strip() if isinstance(workspace_id, str) and workspace_id.strip() else "global"


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
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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
    normalized_title = canonical_reconciliation.normalize_title(structured.get("title") if structured is not None else None)
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
        structured.get("canonical_url")
        if structured is not None
        else None,
    )
    if source_identifier is None and structured is not None:
        source_identifier = canonical_reconciliation.normalize_locator(structured.get("original_locator"))
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
        _normalize_key_text(candidate.get("text")) or "claim-fallback-empty",
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
        locator = _normalize_key_text(candidate.get("text", ""))
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
        provenance_event = canonical_store.record_provenance_event(
            conn,
            object_namespace="gather_candidate_batch",
            object_id=str(batch.get("run_id") or batch_hash),
            event_type="gather_candidate_batch_ingest",
            tool_name=GATHER_INGEST_TOOL,
            tool_version=INGEST_TOOL_VERSION,
            run_id=str(batch.get("run_id") or ""),
            event_timestamp=created_at,
            note_text=_batch_provenance_note(batch, batch_hash=batch_hash, batch_path=batch_path),
            provenance_event_key_v1=f"prov:gather-ingest:{batch_hash}",
        )
        provenance_event_key = provenance_event.event_key
        report["provenance_event"] = {
            "event_id": provenance_event.event_id,
            "event_key": provenance_event.event_key,
        }
        report["transaction_status"] = "in_progress"
    else:
        report["transaction_status"] = "dry_run"

    work_refs: dict[str, str] = {}
    entity_candidates: list[dict[str, Any]] = []
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
                result = canonical_store.record_source_relationship(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    from_object_ref=str(structured["from_object_ref"]),
                    predicate=str(structured["predicate"]),
                    to_object_ref=structured.get("to_object_ref"),
                    target_label=structured.get("target_label"),
                    evidence_note=structured.get("evidence_note"),
                    review_state=structured.get("review_state"),
                    confidence_score=structured.get("confidence_score"),
                    workspace_id=workspace_id,
                )
                _bump(report, "inserted" if result.created else "updated", "source_relationship")
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
                    access_result = canonical_store.record_source_access(
                        conn,
                        provenance_event_ref=provenance_event_key,
                        work_id=work_result.row_id,
                        source_lead_id=_source_lead_key_for_candidate(
                            candidate,
                            workspace_id=workspace_id,
                            structured=structured,
                        ),
                        original_locator=str(
                            structured.get("original_locator")
                            or structured.get("canonical_url")
                            or title
                        ),
                        canonical_url=structured.get("canonical_url"),
                        access_class=structured.get("access_class"),
                        refetchability_status=structured.get("refetchability_status"),
                        rights_posture=structured.get("rights_posture"),
                        citation_hint=structured.get("citation_hint") or title,
                        workspace_id=workspace_id,
                        first_seen_at=created_at,
                        last_seen_at=created_at,
                        record_last_updated=created_at,
                    )
                    _bump(
                        report,
                        "inserted" if access_result.created else "updated",
                        "source_access",
                    )
                claim_text = structured.get("claim_text") if structured else None
                if isinstance(claim_text, str) and claim_text.strip():
                    work_claim_type = _structured_claim_type(
                        candidate_type="work",
                        structured=structured,
                    )
                    if work_claim_type == "candidate_work":
                        work_claim_type = "candidate_work_claim"
                    claim_result = canonical_store.record_source_claim(
                        conn,
                        provenance_event_ref=provenance_event_key,
                        source_claim_key_v1=_claim_key_for_candidate(
                            candidate,
                            workspace_id=workspace_id,
                            claim_text=claim_text,
                            claim_type=work_claim_type,
                            about_object_ref=work_refs[candidate_id],
                        ),
                        claim_text=claim_text,
                        claim_type=work_claim_type,
                        workspace_id=workspace_id,
                        created_at=created_at,
                        record_last_updated=created_at,
                    )
                    _bump(
                        report,
                        "inserted" if claim_result.created else "updated",
                        "source_claim",
                    )
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
                result = canonical_store.record_source_access(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    source_lead_id=source_lead_id,
                    original_locator=str(
                        structured.get("original_locator")
                        if structured is not None and structured.get("original_locator")
                        else candidate["text"]
                    ),
                    canonical_url=structured.get("canonical_url") if structured else None,
                    access_class=structured.get("access_class") if structured else None,
                    refetchability_status=structured.get("refetchability_status")
                    if structured
                    else None,
                    rights_posture=structured.get("rights_posture") if structured else None,
                    citation_hint=structured.get("citation_hint") if structured else None,
                    workspace_id=workspace_id,
                    first_seen_at=created_at,
                    last_seen_at=created_at,
                    record_last_updated=created_at,
                )
                _bump(report, "inserted" if result.created else "updated", "source_access")
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
                result = canonical_store.record_extraction_detected_entity(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    entity_label=entity_label,
                    normalized_label=structured.get("normalized_label") if structured else None,
                    entity_type=structured.get("entity_type") if structured else candidate_type,
                    confidence_score=structured.get("confidence_score") if structured else None,
                    workspace_id=workspace_id,
                    record_last_updated=created_at,
                )
                _bump(
                    report,
                    "inserted" if result.created else "updated",
                    "extraction_detected_entity",
                )
                entity_candidates.append(
                    {
                        "detected_entity_id": result.row_id,
                        "entity_label": entity_label,
                        "entity_type": (
                            structured.get("entity_type")
                            if structured is not None and structured.get("entity_type")
                            else candidate_type
                        ),
                        "confidence_score": (
                            structured.get("confidence_score") if structured is not None else None
                        ),
                        "structured": structured,
                    }
                )
            handled = True

        if candidate_type in {"open_question", "raw_candidate_text", "unknown", "timeline_item"}:
            if dry_run:
                _bump(report, "intended", "source_claim")
            else:
                result = canonical_store.record_source_claim(
                    conn,
                    provenance_event_ref=provenance_event_key,
                    source_claim_key_v1=_claim_key_for_candidate(
                        candidate,
                        workspace_id=workspace_id,
                        claim_text=_structured_claim_text(candidate, structured),
                        claim_type=_structured_claim_type(candidate_type, structured),
                        about_object_ref=_structured_about_object_ref(structured),
                    ),
                    about_object_ref=_structured_about_object_ref(structured),
                    claim_text=_structured_claim_text(candidate, structured),
                    public_summary=(
                        structured.get("public_summary") if structured is not None else None
                    ),
                    claim_type=_structured_claim_type(candidate_type, structured),
                    workspace_id=workspace_id,
                    confidence_score=(
                        structured.get("confidence_score") if structured is not None else None
                    ),
                    created_at=created_at,
                    record_last_updated=created_at,
                )
                _bump(report, "inserted" if result.created else "updated", "source_claim")
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
        try:
            curation_counts = canonical_reconciliation.run_reconciliation_pass_for_ingest(
                conn,
                provenance_event_ref=provenance_event_key,
                workspace_id=workspace_id,
                changed_at=created_at,
                entity_candidates=entity_candidates,
                source_run_id=str(batch.get("run_id") or ""),
            )
        except canonical_reconciliation.CanonicalReconciliationError as exc:
            raise CanonicalIngestError(f"candidate-batch reconciliation failed: {exc}") from exc
        _apply_curation_counts(report, curation_counts)
        report["transaction_status"] = "committed"
    return report


def ingest_execution_artifacts(
    conn: sqlite3.Connection,
    execution_record: dict[str, Any],
    capture_events: list[dict[str, Any]],
    extraction_records: list[dict[str, Any]],
    *,
    paths: dict[str, Path],
    input_hashes: dict[str, str],
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
        report["provenance_event"] = {
            "event_id": provenance_event.event_id,
            "event_key": provenance_event.event_key,
        }
        report["transaction_status"] = "in_progress"
    else:
        report["transaction_status"] = "dry_run"

    capture_id_map: dict[str, int] = {}
    entity_candidates: list[dict[str, Any]] = []
    for record in capture_events:
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
        if dry_run:
            _bump(report, "intended", "capture_event")
            continue
        result = canonical_store.record_capture_event(
            conn,
            provenance_event_ref=provenance_event_key,
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
        capture_id_map[str(record["capture_id"])] = result.row_id
        _bump(report, "inserted" if result.created else "updated", "capture_event")

    for record in extraction_records:
        capture_key = str(record.get("capture_id") or "")
        if dry_run:
            if capture_key not in {str(item.get("capture_id")) for item in capture_events}:
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
        result = canonical_store.record_extraction_record(
            conn,
            provenance_event_ref=provenance_event_key,
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
            hostile_replay_flags_json=record.get("hostile_replay_flags"),
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
                    extraction_id=result.row_id,
                    capture_event_id=canonical_capture_id,
                    entity_label=str(entity.get("entity_label") or entity.get("label") or ""),
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

    if not dry_run:
        try:
            curation_counts = canonical_reconciliation.run_reconciliation_pass_for_ingest(
                conn,
                provenance_event_ref=provenance_event_key,
                workspace_id=str(execution_record.get("workspace_id") or "") or None,
                changed_at=created_at,
                entity_candidates=entity_candidates,
                source_run_id=str(execution_record.get("run_id") or ""),
            )
        except canonical_reconciliation.CanonicalReconciliationError as exc:
            raise CanonicalIngestError(f"execution-artifact reconciliation failed: {exc}") from exc
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
