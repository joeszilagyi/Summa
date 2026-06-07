"""Deterministic canonical curation helpers for dedup and contradiction review.

These helpers are intentionally narrow:

- exact or strongly normalized work matching
- exact or reviewable authority reconciliation
- structured contradiction detection over JSON-backed source_claim rows

They do not perform fuzzy NLP, broad authority merging, or truth adjudication.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from tools.source_db_tools import canonical_store, identifier_normalization

RECONCILIATION_TOOL = "tools/source_db_tools/canonical_reconciliation.py"
WORK_DUPLICATE_EVENT_TYPE = "work_duplicate_encountered"
AUTHORITY_MATCH_EXACT_IDENTIFIER = 1.0
AUTHORITY_MATCH_LABEL_AND_TYPE = 0.75
AUTHORITY_MATCH_LABEL_AND_TYPE_AMBIGUOUS = 0.5
AUTO_MERGE_REVIEW_STATES = ("accepted", "approved", "curated", "reviewed")
AUTO_MERGE_IDENTIFIER_CONFIDENCE_THRESHOLD = 0.0
WORK_MATCH_REVIEW_STATES = tuple(
    sorted(set(AUTO_MERGE_REVIEW_STATES) | set(canonical_store.PRIOR_STATE_LEAD_REVIEW_STATES))
)
WORK_MATCH_EXACT_IDENTIFIER = 1.0
WORK_MATCH_TITLE_TYPE_AND_SOURCE = 0.95
STRUCTURED_CONTRADICTION_CONFIDENCE = 1.0
RECONCILIATION_REVIEW_STATE = "needs_review"
MERGE_EVENT_REVIEW_STATE = "reviewed"
CONTRADICTION_PREDICATE = "contradicts"
QUANTITY_CONFLICT_RULE = "structured_quantity_conflict"
TAUGHT_BY_IMPOSSIBLE_RULE = "structured_taught_by_impossible_life_overlap"
RELATIONAL_TEMPORAL_RULE = "relational_temporal_lifespan_overlap"
RELATIONAL_EVENT_YEAR_RULE = "relational_temporal_event_year_outside_lifespan"
CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES = tuple(
    sorted(canonical_store.PRIOR_STATE_EXCLUDED_REVIEW_STATES)
)
STRUCTURED_CLAIM_TYPES = {
    "birth_year",
    "death_year",
    "taught_by",
    "relationship_taught_by",
    "publication_year",
    "quantity",
}
BIRTH_CLAIM_TYPES = {"birth_year", "birth_date", "date_of_birth", "born"}
DEATH_CLAIM_TYPES = {"death_year", "death_date", "date_of_death", "died"}
RELATIONAL_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "taught_by": {
        "rule_id": RELATIONAL_TEMPORAL_RULE,
        "required_facts": ["subject.birth_year", "object.death_year"],
        "review_state": RECONCILIATION_REVIEW_STATE,
    },
    "teacher_of": {
        "rule_id": RELATIONAL_TEMPORAL_RULE,
        "required_facts": ["object.birth_year", "subject.death_year"],
        "review_state": RECONCILIATION_REVIEW_STATE,
    },
    "met": {
        "rule_id": RELATIONAL_TEMPORAL_RULE,
        "required_facts": ["lifespan_overlap"],
        "review_state": RECONCILIATION_REVIEW_STATE,
    },
    "influenced": {
        "rule_id": "relational_temporal_posthumous_influence_not_constrained",
        "required_facts": [],
        "review_state": None,
        "skip_reason": "posthumous influence can be valid; no direct-personal constraint is encoded",
    },
}
TRIVIAL_PUNCTUATION_RE = re.compile(r"[\s\-_.,;:!?()\\[\\]{}]+")


class CanonicalReconciliationError(RuntimeError):
    """Raised when deterministic reconciliation cannot proceed safely."""


@dataclass(frozen=True)
class WorkMatch:
    work_id: int
    work_key_v1: str
    method: str
    confidence_score: float
    identity_key: str


@dataclass(frozen=True)
class AuthorityMatch:
    authority_record_id: int
    method: str
    confidence_score: float
    automatic_merge: bool


@dataclass(frozen=True)
class TemporalFact:
    fact_type: str
    year: int
    source_claim_id: int
    about_object_ref: str


@dataclass(frozen=True)
class EndpointFacts:
    object_ref: str
    birth_years: tuple[TemporalFact, ...]
    death_years: tuple[TemporalFact, ...]


@dataclass(frozen=True)
class RelationalContradiction:
    relationship_id: int
    rule_id: str
    target_object_ref: str
    rationale: str


def _normalize_timestamp(value: str | None, *, field_name: str, default: str | None = None) -> str:
    return canonical_store._normalize_timestamp(
        value,
        field_name=field_name,
        default=default,
    )


def _normalize_review_state(value: str, *, field_name: str = "review_state") -> str:
    return canonical_store._normalize_review_state(
        value,
        default=value,
        field_name=field_name,
    )


def _normalize_confidence_score(value: float | int | None) -> float | None:
    return canonical_store._normalize_confidence_score(value)


def normalize_authority_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split()).strip().casefold()
    return text


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split()).strip().casefold()
    return text


def normalize_external_identifier(scheme: Any, value: Any) -> dict[str, Any]:
    normalized = identifier_normalization.identifier_storage_values(scheme, value)
    if normalized["validity_status"] != "valid":
        raise CanonicalReconciliationError(
            f"identifier is not valid for deterministic matching: {normalized['scheme']} {normalized['raw_value']!r}"
        )
    return normalized


def normalize_locator(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        normalized = identifier_normalization.identifier_storage_values("url", text)
        return str(normalized["value"]) if normalized["validity_status"] == "valid" else text
    return unicodedata.normalize("NFKC", text)


def normalize_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CanonicalReconciliationError("year values must be numeric")
    if isinstance(value, int):
        if value < 1:
            raise CanonicalReconciliationError("year value must be a positive integer")
        return value
    text = str(value).strip()
    if not text:
        return None
    iso_match = re.fullmatch(r"(-?\d{1,6})(?:-\d{2}-\d{2})?", text)
    if iso_match is not None:
        year = int(iso_match.group(1))
        if year < 1:
            raise CanonicalReconciliationError("year value must be a positive integer")
        return year
    if not re.fullmatch(r"-?\d{1,6}", text):
        raise CanonicalReconciliationError(f"year value must be an integer: {value!r}")
    year = int(text)
    if year < 1:
        raise CanonicalReconciliationError("year value must be a positive integer")
    return year


def normalize_quantity(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CanonicalReconciliationError("quantity values must be numeric")
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    try:
        return float(text)
    except ValueError as exc:  # pragma: no cover - defensive
        raise CanonicalReconciliationError(f"quantity value must be numeric: {value!r}") from exc


def normalize_work_key(
    *,
    title: str | None,
    work_type: str | None,
    identifier_scheme: str | None = None,
    identifier_value: str | None = None,
    source_identifier: str | None = None,
    publication_year: int | None = None,
) -> str | None:
    if identifier_scheme and identifier_value:
        normalized = normalize_external_identifier(identifier_scheme, identifier_value)
        return f"identifier:{normalized['scheme']}:{normalized['value']}"
    normalized_title = normalize_title(title)
    normalized_type = normalize_authority_label(work_type)
    if normalized_title and normalized_type and source_identifier:
        return f"title-source:{normalized_type}:{normalized_title}:{source_identifier}"
    if normalized_title and normalized_type and publication_year is not None:
        return f"title-year:{normalized_type}:{normalized_title}:{publication_year}"
    return None


def candidate_identifiers(structured: dict[str, Any] | None) -> list[dict[str, str]]:
    if structured is None:
        return []
    identifiers: list[dict[str, str]] = []
    raw_identifiers = structured.get("identifiers")
    if isinstance(raw_identifiers, list):
        for item in raw_identifiers:
            if not isinstance(item, dict):
                continue
            scheme = item.get("scheme")
            value = item.get("value")
            if scheme and value:
                identifiers.append({"scheme": str(scheme), "value": str(value)})
    for scheme_field, value_field in (
        ("identifier_scheme", "identifier_value"),
        ("authority_identifier_scheme", "authority_identifier_value"),
    ):
        scheme = structured.get(scheme_field)
        value = structured.get(value_field)
        if scheme and value:
            identifiers.append({"scheme": str(scheme), "value": str(value)})
    for field_name, scheme in (
        ("doi", "doi"),
        ("isbn", "isbn"),
        ("issn", "issn"),
        ("orcid", "orcid"),
        ("wikidata_id", "wikidata"),
    ):
        value = structured.get(field_name)
        if isinstance(value, str) and value.strip():
            identifiers.append({"scheme": scheme, "value": value})
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in identifiers:
        normalized = identifier_normalization.identifier_storage_values(
            item["scheme"], item["value"]
        )
        key = (str(normalized["scheme"]), str(normalized["value"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"scheme": item["scheme"], "value": item["value"]})
    return deduped


def _structured_claim_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    claim_text = row["claim_text"]
    if not isinstance(claim_text, str):
        return None
    try:
        parsed = json.loads(claim_text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _source_claim_ref(source_claim_id: int) -> str:
    return f"source_claim:{source_claim_id}"


def _source_relationship_ref(source_relationship_id: int) -> str:
    return f"source_relationship:{source_relationship_id}"


def _review_target_row(
    conn: sqlite3.Connection,
    *,
    target_namespace: str,
    target_id: int,
) -> tuple[str, str, sqlite3.Row]:
    mapping = {
        "authority_record": ("authority_record", "authority_record_id"),
        "authority_reconciliation": ("authority_reconciliation", "authority_reconciliation_id"),
        "source_claim": ("source_claim", "source_claim_id"),
        "source_relationship": ("source_relationship", "source_relationship_id"),
        "extraction_detected_entity": ("extraction_detected_entity", "detected_entity_id"),
    }
    if target_namespace not in mapping:
        raise CanonicalReconciliationError(
            f"unsupported review-state target namespace: {target_namespace}"
        )
    table_name, pk_column = mapping[target_namespace]
    row = conn.execute(
        f"SELECT * FROM {table_name} WHERE {pk_column}=?",
        (target_id,),
    ).fetchone()
    if row is None:
        raise CanonicalReconciliationError(f"missing {target_namespace} target id: {target_id}")
    return table_name, pk_column, row


def update_review_state(
    conn: sqlite3.Connection,
    *,
    target_namespace: str,
    target_id: int,
    new_state: str,
    changed_at: str,
    reason: str,
    note: str,
    source_namespace: str,
    source_id: str,
    source_run_id: str | None = None,
) -> bool:
    new_state_value = _normalize_review_state(new_state, field_name="new_state")
    table_name, pk_column, row = _review_target_row(
        conn,
        target_namespace=target_namespace,
        target_id=target_id,
    )
    previous_state = None if row["review_state"] is None else str(row["review_state"])
    previous_state_value = str(previous_state or "").strip().lower()
    if previous_state_value in canonical_store.PRIOR_STATE_ESTABLISHED_REVIEW_STATES:
        return False
    if previous_state == new_state_value:
        return False
    conn.execute(
        f"UPDATE {table_name} SET review_state=?, record_last_updated=? WHERE {pk_column}=?",
        (new_state_value, changed_at, target_id),
    )
    canonical_store.record_review_state_history(
        conn,
        target_namespace=target_namespace,
        target_id=str(target_id),
        previous_state=previous_state,
        new_state=new_state_value,
        changed_by="canonical_reconciliation",
        changed_at=changed_at,
        reason=reason,
        note=note,
        source_namespace=source_namespace,
        source_id=source_id,
        source_tool=RECONCILIATION_TOOL,
        source_run_id=source_run_id,
    )
    return True


def record_work_identifier(
    conn: sqlite3.Connection,
    *,
    work_id: int,
    scheme: str,
    value: str,
    confidence_score: float | int | None = None,
    review_state: str | None = None,
    record_last_updated: str | None = None,
) -> canonical_store.CanonicalWriteResult:
    normalized = identifier_normalization.identifier_storage_values(scheme, value)
    timestamp = _normalize_timestamp(
        record_last_updated,
        field_name="record_last_updated",
        default=canonical_store.now_rfc3339(),
    )
    score = _normalize_confidence_score(confidence_score)
    review_state_value = (
        _normalize_review_state(review_state, field_name="review_state")
        if review_state is not None
        else None
    )
    conflicting = conn.execute(
        """
        SELECT work_identifier_id, work_id
        FROM work_identifier
        WHERE scheme=? AND value=?
        """,
        (normalized["scheme"], normalized["value"]),
    ).fetchone()
    if conflicting is not None and int(conflicting["work_id"]) != work_id:
        raise CanonicalReconciliationError(
            f"identifier {normalized['scheme']}:{normalized['value']} already belongs to work:{int(conflicting['work_id'])}"
        )
    existing = conn.execute(
        """
        SELECT work_identifier_id, review_state
        FROM work_identifier
        WHERE work_id=? AND scheme=? AND value=?
        """,
        (work_id, normalized["scheme"], normalized["value"]),
    ).fetchone()
    if existing is None:
        review_state_value = (
            review_state_value
            if review_state_value is not None
            else canonical_store.DEFAULT_WORK_REVIEW_STATE
        )
        cursor = conn.execute(
            """
            INSERT INTO work_identifier (
              work_id, scheme, value, raw_value, normalized_value, normalized_uri,
              validity_status, validation_warning, is_primary, confidence_score,
              review_state, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                work_id,
                normalized["scheme"],
                normalized["value"],
                normalized["raw_value"],
                normalized["normalized_value"],
                normalized["normalized_uri"],
                normalized["validity_status"],
                normalized["validation_warning"],
                0,
                score,
                review_state_value,
                timestamp,
            ),
        )
        return canonical_store.CanonicalWriteResult(
            "work_identifier",
            canonical_store._inserted_rowid(cursor),
            f"{normalized['scheme']}:{normalized['value']}",
            True,
        )
    existing_state_text = (
        None if existing["review_state"] is None else str(existing["review_state"]).strip().lower()
    )
    preserve_established = False
    merged_review_state = existing["review_state"]
    if review_state_value is not None:
        proposed_state_text = str(review_state_value).strip().lower()
        preserve_established = (
            existing_state_text in canonical_store.PRIOR_STATE_ESTABLISHED_REVIEW_STATES
            and (
                proposed_state_text in canonical_store.PRIOR_STATE_ESTABLISHED_REVIEW_STATES
                or canonical_store._pending_review_state(review_state_value)
            )
        )
        merged_review_state = (
            existing["review_state"]
            if preserve_established
            else canonical_store._merged_review_state(
                existing["review_state"],
                review_state_value,
            )
        )
    conn.execute(
        """
        UPDATE work_identifier
        SET raw_value=?, normalized_value=?, normalized_uri=?, validity_status=?,
            validation_warning=?, confidence_score=COALESCE(?, confidence_score),
            review_state=?, record_last_updated=?
        WHERE work_identifier_id=?
        """,
        (
            normalized["raw_value"],
            normalized["normalized_value"],
            normalized["normalized_uri"],
            normalized["validity_status"],
            normalized["validation_warning"],
            score,
            merged_review_state,
            timestamp,
            int(existing["work_identifier_id"]),
        ),
    )
    return canonical_store.CanonicalWriteResult(
        "work_identifier",
        int(existing["work_identifier_id"]),
        f"{normalized['scheme']}:{normalized['value']}",
        False,
    )


def find_existing_work_match(
    conn: sqlite3.Connection,
    *,
    structured: dict[str, Any] | None,
    work_type: str | None,
    title: str | None,
    workspace_id: str | None,
) -> WorkMatch | None:
    identifiers = candidate_identifiers(structured)
    for identifier in identifiers:
        normalized = normalize_external_identifier(identifier["scheme"], identifier["value"])
        review_state_placeholders = ", ".join("?" for _ in WORK_MATCH_REVIEW_STATES)
        rows = conn.execute(
            f"""
            SELECT work.work_id, work.work_key_v1
            FROM work_identifier
            INNER JOIN work ON work.work_id = work_identifier.work_id
            WHERE work_identifier.scheme=? AND work_identifier.value=?
              AND work_identifier.validity_status='valid'
              AND work.review_state IN ({review_state_placeholders})
              AND work_identifier.review_state IN ({review_state_placeholders})
              AND work_identifier.confidence_score IS NOT NULL
              AND work_identifier.confidence_score >= ?
            ORDER BY work.work_id
            """,
            (
                normalized["scheme"],
                normalized["value"],
                *WORK_MATCH_REVIEW_STATES,
                *WORK_MATCH_REVIEW_STATES,
                AUTO_MERGE_IDENTIFIER_CONFIDENCE_THRESHOLD,
            ),
        ).fetchall()
        distinct_ids = {int(row["work_id"]) for row in rows}
        if len(distinct_ids) > 1:
            raise CanonicalReconciliationError(
                f"identifier {normalized['scheme']}:{normalized['value']} maps to multiple works"
            )
        if rows:
            return WorkMatch(
                work_id=int(rows[0]["work_id"]),
                work_key_v1=str(rows[0]["work_key_v1"]),
                method="exact_work_identifier",
                confidence_score=WORK_MATCH_EXACT_IDENTIFIER,
                identity_key=normalize_work_key(
                    title=title,
                    work_type=work_type,
                    identifier_scheme=normalized["scheme"],
                    identifier_value=normalized["value"],
                )
                or f"identifier:{normalized['scheme']}:{normalized['value']}",
            )

    source_identifier = normalize_locator(
        structured.get("canonical_url") if structured is not None else None
    ) or normalize_locator(structured.get("original_locator") if structured is not None else None)
    normalized_title = normalize_title(title)
    normalized_type = normalize_authority_label(work_type)
    if source_identifier and normalized_title and normalized_type:
        rows = conn.execute(
            """
            SELECT work.work_id, work.work_key_v1, work.title, work.work_type,
                   access.original_locator, access.canonical_url
            FROM source_access AS access
            INNER JOIN work ON work.work_id = access.work_id
            WHERE (? IS NULL OR work.workspace_id=?)
              AND COALESCE(work.work_type, '') = ?
              AND (access.canonical_url = ? OR access.original_locator = ?)
            ORDER BY work.work_id
            """,
            (
                workspace_id,
                workspace_id,
                work_type or "",
                source_identifier,
                source_identifier,
            ),
        ).fetchall()
        matches: list[sqlite3.Row] = []
        for row in rows:
            row_title = normalize_title(row["title"])
            row_locator = normalize_locator(row["canonical_url"]) or normalize_locator(
                row["original_locator"]
            )
            if row_title == normalized_title and row_locator == source_identifier:
                matches.append(row)
        distinct_ids = {int(row["work_id"]) for row in matches}
        if len(distinct_ids) > 1:
            raise CanonicalReconciliationError(
                f"title/source identity maps to multiple works for {title!r}"
            )
        if matches:
            return WorkMatch(
                work_id=int(matches[0]["work_id"]),
                work_key_v1=str(matches[0]["work_key_v1"]),
                method="normalized_title_type_source",
                confidence_score=WORK_MATCH_TITLE_TYPE_AND_SOURCE,
                identity_key=normalize_work_key(
                    title=title,
                    work_type=work_type,
                    source_identifier=source_identifier,
                )
                or f"title-source:{normalized_type}:{normalized_title}:{source_identifier}",
            )
    return None


def record_work_duplicate_encounter(
    conn: sqlite3.Connection,
    *,
    work_id: int,
    matched_by: str,
    identity_key: str,
    provenance_event_ref: str,
    incoming_work_key: str | None,
    workspace_id: str | None,
    encountered_at: str,
) -> canonical_store.CanonicalWriteResult:
    note_payload = {
        "matched_by": matched_by,
        "identity_key": identity_key,
        "incoming_work_key": incoming_work_key,
        "workspace_id": workspace_id,
        "provenance_event_ref": provenance_event_ref,
    }
    event_key = canonical_store.stable_write_key(
        "prov",
        WORK_DUPLICATE_EVENT_TYPE,
        work_id,
        provenance_event_ref,
        identity_key,
    )
    existing = conn.execute(
        """
        SELECT provenance_event_id
        FROM provenance_event
        WHERE provenance_event_key_v1=?
        """,
        (event_key,),
    ).fetchone()
    if existing is not None:
        return canonical_store.CanonicalWriteResult(
            "provenance_event",
            int(existing["provenance_event_id"]),
            event_key,
            False,
        )
    event = canonical_store.record_provenance_event(
        conn,
        object_namespace="work",
        object_id=str(work_id),
        event_type=WORK_DUPLICATE_EVENT_TYPE,
        tool_name=RECONCILIATION_TOOL,
        source_object_namespace="provenance_event",
        source_object_id=provenance_event_ref,
        event_timestamp=encountered_at,
        note_text=json.dumps(note_payload, ensure_ascii=False, sort_keys=True),
        provenance_event_key_v1=event_key,
    )
    return canonical_store.CanonicalWriteResult(
        "provenance_event",
        event.event_id,
        event.event_key,
        True,
    )


def find_existing_authority_match(
    conn: sqlite3.Connection,
    *,
    entity_label: str,
    entity_type: str | None,
    structured: dict[str, Any] | None = None,
) -> list[AuthorityMatch]:
    matches: list[AuthorityMatch] = []
    identifiers = candidate_identifiers(structured)
    for identifier in identifiers:
        normalized = normalize_external_identifier(identifier["scheme"], identifier["value"])
        review_state_placeholders = ", ".join("?" for _ in AUTO_MERGE_REVIEW_STATES)
        row = conn.execute(
            f"""
            SELECT authority_record.authority_record_id
            FROM authority_identifier
            INNER JOIN authority_record
              ON authority_record.authority_record_id = authority_identifier.authority_record_id
            WHERE authority_identifier.scheme=? AND authority_identifier.value=?
              AND authority_identifier.validity_status='valid'
              AND authority_record.review_state IN ({review_state_placeholders})
              AND authority_identifier.review_state IN ({review_state_placeholders})
              AND authority_identifier.confidence_score IS NOT NULL
              AND authority_identifier.confidence_score >= ?
              AND authority_record.merged_into_authority_record_id IS NULL
            """,
            (
                normalized["scheme"],
                normalized["value"],
                *AUTO_MERGE_REVIEW_STATES,
                *AUTO_MERGE_REVIEW_STATES,
                AUTO_MERGE_IDENTIFIER_CONFIDENCE_THRESHOLD,
            ),
        ).fetchone()
        if row is not None:
            return [
                AuthorityMatch(
                    authority_record_id=int(row["authority_record_id"]),
                    method="exact_authority_identifier",
                    confidence_score=AUTHORITY_MATCH_EXACT_IDENTIFIER,
                    automatic_merge=True,
                )
            ]

    normalized_label = normalize_authority_label(entity_label)
    normalized_type = normalize_authority_label(entity_type)
    if not normalized_label or not normalized_type:
        return matches
    rows = conn.execute(
        """
        SELECT authority_record_id
        FROM authority_record
        WHERE label_norm=? AND authority_type=? AND merged_into_authority_record_id IS NULL
        ORDER BY authority_record_id
        """,
        (normalized_label, entity_type),
    ).fetchall()
    if len(rows) == 1:
        return [
            AuthorityMatch(
                authority_record_id=int(rows[0]["authority_record_id"]),
                method="normalized_label_and_type",
                confidence_score=AUTHORITY_MATCH_LABEL_AND_TYPE,
                automatic_merge=False,
            )
        ]
    for row in rows:
        matches.append(
            AuthorityMatch(
                authority_record_id=int(row["authority_record_id"]),
                method="ambiguous_normalized_label_and_type",
                confidence_score=AUTHORITY_MATCH_LABEL_AND_TYPE_AMBIGUOUS,
                automatic_merge=False,
            )
        )
    return matches


def _ensure_local_authority_candidate(
    conn: sqlite3.Connection,
    *,
    detected_entity_id: int,
    entity_label: str,
    entity_type: str | None,
    workspace_id: str | None,
    provenance_event_ref: str,
    confidence_score: float | int | None,
    created_at: str,
) -> int:
    existing = conn.execute(
        """
        SELECT authority_record_id
        FROM authority_record
        WHERE source_namespace='extraction_detected_entity' AND source_id=?
        LIMIT 1
        """,
        (str(detected_entity_id),),
    ).fetchone()
    if existing is not None:
        return int(existing["authority_record_id"])
    authority_key = canonical_store.stable_write_key(
        "auth",
        "detected_entity_candidate",
        detected_entity_id,
        entity_type,
        entity_label,
    )
    cursor = conn.execute(
        """
        INSERT INTO authority_record (
          authority_key_v1, authority_type, preferred_label, label_norm, sort_label,
          source_namespace, source_id, reconciliation_status, review_state,
          confidence_score, workspace_id, provenance_event_ref, created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            authority_key,
            entity_type or "unknown",
            entity_label,
            normalize_authority_label(entity_label),
            entity_label,
            "extraction_detected_entity",
            str(detected_entity_id),
            "local",
            "proposed",
            _normalize_confidence_score(confidence_score),
            workspace_id,
            provenance_event_ref,
            created_at,
            created_at,
        ),
    )
    return canonical_store._inserted_rowid(cursor)


def record_authority_reconciliation(
    conn: sqlite3.Connection,
    *,
    detected_entity_id: int,
    raw_label: str,
    entity_type: str | None,
    candidate_authority_record_id: int,
    method: str,
    match_method: str,
    confidence_score: float,
    evidence_context: str | None,
    review_state: str,
    created_at: str,
) -> canonical_store.CanonicalWriteResult:
    review_state_value = _normalize_review_state(review_state, field_name="review_state")
    score = _normalize_confidence_score(confidence_score)
    reconciliation_key = canonical_store.stable_write_key(
        "authrec",
        detected_entity_id,
        candidate_authority_record_id,
        method,
    )
    existing = conn.execute(
        """
        SELECT authority_reconciliation_id, review_state, updated_at, record_last_updated
        FROM authority_reconciliation
        WHERE reconciliation_key_v1=?
        """,
        (reconciliation_key,),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO authority_reconciliation (
              reconciliation_key_v1, target_namespace, target_id, detected_entity_id,
              raw_label, entity_type, candidate_label, candidate_authority_record_id,
              candidate_authority_id, method, match_method, match_score, evidence_context,
              confidence_score, review_state, created_at, updated_at, record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reconciliation_key,
                "extraction_detected_entity",
                str(detected_entity_id),
                detected_entity_id,
                raw_label,
                entity_type,
                raw_label,
                candidate_authority_record_id,
                candidate_authority_record_id,
                method,
                match_method,
                score,
                evidence_context,
                score,
                review_state_value,
                created_at,
                created_at,
                created_at,
            ),
        )
        return canonical_store.CanonicalWriteResult(
            "authority_reconciliation",
            canonical_store._inserted_rowid(cursor),
            reconciliation_key,
            True,
        )
    conn.execute(
        """
        UPDATE authority_reconciliation
        SET confidence_score=?, match_score=?, evidence_context=?, review_state=?,
            updated_at=?, record_last_updated=?
        WHERE authority_reconciliation_id=?
        """,
        (
            score,
            score,
            evidence_context,
            (
                existing["review_state"]
                if (
                    existing["review_state"] is not None
                    and str(existing["review_state"]).strip().lower()
                    in canonical_store.PRIOR_STATE_ESTABLISHED_REVIEW_STATES
                    and (
                        str(review_state_value).strip().lower()
                        in canonical_store.PRIOR_STATE_ESTABLISHED_REVIEW_STATES
                        or canonical_store._pending_review_state(review_state_value)
                    )
                )
                else canonical_store._merged_review_state(
                    existing["review_state"],
                    review_state_value,
                )
            ),
            canonical_store._max_nonnull_iso(existing["updated_at"], created_at),
            canonical_store._max_nonnull_iso(existing["record_last_updated"], created_at),
            int(existing["authority_reconciliation_id"]),
        ),
    )
    return canonical_store.CanonicalWriteResult(
        "authority_reconciliation",
        int(existing["authority_reconciliation_id"]),
        reconciliation_key,
        False,
    )


def record_authority_merge_event(
    conn: sqlite3.Connection,
    *,
    from_authority_record_id: int,
    into_authority_record_id: int,
    merge_reason: str,
    evidence_note: str,
    merged_by: str,
    merged_at: str,
) -> canonical_store.CanonicalWriteResult:
    if from_authority_record_id == into_authority_record_id:
        raise CanonicalReconciliationError("cannot merge an authority record into itself")
    row = conn.execute(
        """
        SELECT merged_into_authority_record_id
        FROM authority_record
        WHERE authority_record_id=?
        """,
        (from_authority_record_id,),
    ).fetchone()
    if row is None:
        raise CanonicalReconciliationError(
            f"missing authority merge source: {from_authority_record_id}"
        )
    current_target = row["merged_into_authority_record_id"]
    if current_target is not None and int(current_target) not in {into_authority_record_id}:
        raise CanonicalReconciliationError(
            f"authority {from_authority_record_id} is already merged into {current_target}"
        )
    existing = conn.execute(
        """
        SELECT authority_merge_event_id
        FROM authority_merge_event
        WHERE from_authority_record_id=? AND into_authority_record_id=? AND merge_reason=?
        """,
        (from_authority_record_id, into_authority_record_id, merge_reason),
    ).fetchone()
    if existing is not None:
        return canonical_store.CanonicalWriteResult(
            "authority_merge_event",
            int(existing["authority_merge_event_id"]),
            canonical_store.stable_write_key(
                "authority-merge",
                from_authority_record_id,
                into_authority_record_id,
                merge_reason,
            ),
            False,
        )
    conn.execute(
        """
        UPDATE authority_record
        SET merged_into_authority_record_id=?, reconciliation_status='merged', record_last_updated=?
        WHERE authority_record_id=?
        """,
        (into_authority_record_id, merged_at, from_authority_record_id),
    )
    cursor = conn.execute(
        """
        INSERT INTO authority_merge_event (
          from_authority_record_id, into_authority_record_id, merge_reason,
          evidence_note, merged_at, merged_by, review_state, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            from_authority_record_id,
            into_authority_record_id,
            merge_reason,
            evidence_note,
            merged_at,
            merged_by,
            MERGE_EVENT_REVIEW_STATE,
            merged_at,
        ),
    )
    return canonical_store.CanonicalWriteResult(
        "authority_merge_event",
        canonical_store._inserted_rowid(cursor),
        canonical_store.stable_write_key(
            "authority-merge",
            from_authority_record_id,
            into_authority_record_id,
            merge_reason,
        ),
        True,
    )


def _maybe_link_detected_entity_authority(
    conn: sqlite3.Connection,
    *,
    detected_entity_id: int,
    authority_record_id: int,
    changed_at: str,
) -> bool:
    row = conn.execute(
        """
        SELECT authority_record_id
        FROM extraction_detected_entity
        WHERE detected_entity_id=?
        """,
        (detected_entity_id,),
    ).fetchone()
    if row is None:
        raise CanonicalReconciliationError(f"missing detected entity: {detected_entity_id}")
    if (
        row["authority_record_id"] is not None
        and int(row["authority_record_id"]) == authority_record_id
    ):
        return False
    conn.execute(
        """
        UPDATE extraction_detected_entity
        SET authority_record_id=?, record_last_updated=?
        WHERE detected_entity_id=?
        """,
        (authority_record_id, changed_at, detected_entity_id),
    )
    return True


def record_source_contradiction(
    conn: sqlite3.Connection,
    *,
    offending_namespace: str,
    offending_id: int,
    target_object_ref: str,
    provenance_event_ref: str,
    workspace_id: str | None,
    rule_id: str,
    rationale: str,
    changed_at: str,
    source_run_id: str | None,
) -> canonical_store.CanonicalWriteResult:
    result = canonical_store.record_source_relationship(
        conn,
        provenance_event_ref=provenance_event_ref,
        from_object_ref=f"{offending_namespace}:{offending_id}",
        to_object_ref=target_object_ref,
        predicate=CONTRADICTION_PREDICATE,
        target_label=rule_id,
        evidence_note=rationale,
        review_state=RECONCILIATION_REVIEW_STATE,
        workspace_id=workspace_id,
        confidence_score=STRUCTURED_CONTRADICTION_CONFIDENCE,
        created_at=changed_at,
        record_last_updated=changed_at,
    )
    update_review_state(
        conn,
        target_namespace=offending_namespace,
        target_id=offending_id,
        new_state=RECONCILIATION_REVIEW_STATE,
        changed_at=changed_at,
        reason=rule_id,
        note=rationale,
        source_namespace="source_relationship",
        source_id=str(result.row_id),
        source_run_id=source_run_id,
    )
    return result


def _load_claim_row(conn: sqlite3.Connection, source_claim_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM source_claim WHERE source_claim_id=?",
        (source_claim_id,),
    ).fetchone()
    if row is None:
        raise CanonicalReconciliationError(f"missing source_claim id: {source_claim_id}")
    return row


def _structured_numeric_value(payload: dict[str, Any]) -> float | int | None:
    for key in ("value", "numeric_value", "year"):
        if key in payload:
            return normalize_quantity(payload.get(key))
    return None


def _claim_is_excluded_from_structured_contradictions(row: sqlite3.Row | None) -> bool:
    if row is None:
        return False
    review_state = (
        canonical_store.DEFAULT_SOURCE_CLAIM_REVIEW_STATE
        if row["review_state"] is None
        else str(row["review_state"]).strip().lower()
    )
    return review_state in CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES


def _claim_about_ref(row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    about_object_ref = row["about_object_ref"]
    if isinstance(about_object_ref, str) and about_object_ref.strip():
        return about_object_ref
    for key in ("about_object_ref", "from_object_ref", "subject_object_ref"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _claims_for_ref_and_type(
    conn: sqlite3.Connection,
    *,
    about_object_ref: str,
    claim_type: str,
    workspace_id: str | None,
    excluded_review_states: tuple[str, ...] = CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES,
) -> list[sqlite3.Row]:
    exclusion_clause = ""
    params: list[Any] = [about_object_ref, claim_type]
    if excluded_review_states:
        placeholders = ", ".join("?" for _ in excluded_review_states)
        exclusion_clause = f" AND COALESCE(review_state, ?) NOT IN ({placeholders})"
        params.extend([canonical_store.DEFAULT_SOURCE_CLAIM_REVIEW_STATE, *excluded_review_states])
    if workspace_id is None:
        rows = conn.execute(
            f"""
            SELECT *
            FROM source_claim
            WHERE about_object_ref=? AND claim_type=?{exclusion_clause}
            ORDER BY source_claim_id
            """,
            tuple(params),
        ).fetchall()
    else:
        params.append(workspace_id)
        rows = conn.execute(
            f"""
            SELECT *
            FROM source_claim
            WHERE about_object_ref=? AND claim_type=?{exclusion_clause} AND workspace_id=?
            ORDER BY source_claim_id
            """,
            tuple(params),
        ).fetchall()
    return list(rows)


def _claims_for_ref_and_types(
    conn: sqlite3.Connection,
    *,
    about_object_ref: str,
    claim_types: set[str],
    workspace_id: str | None,
    excluded_review_states: tuple[str, ...] = CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES,
) -> list[sqlite3.Row]:
    if not claim_types:
        return []
    placeholders = ", ".join("?" for _ in claim_types)
    params: list[Any] = [about_object_ref, *sorted(claim_types)]
    exclusion_clause = ""
    if excluded_review_states:
        exclusion_placeholders = ", ".join("?" for _ in excluded_review_states)
        exclusion_clause = f" AND COALESCE(review_state, ?) NOT IN ({exclusion_placeholders})"
        params.append(canonical_store.DEFAULT_SOURCE_CLAIM_REVIEW_STATE)
        params.extend(excluded_review_states)
    workspace_clause = ""
    if workspace_id is not None:
        workspace_clause = " AND workspace_id=?"
        params.append(workspace_id)
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM source_claim
            WHERE about_object_ref=? AND claim_type IN ({placeholders}){exclusion_clause}{workspace_clause}
            ORDER BY source_claim_id
            """,
            tuple(params),
        ).fetchall()
    )


def _year_from_structured_fact(payload: dict[str, Any]) -> int | None:
    for key in ("year", "value", "date", "birth_year", "death_year", "event_year", "event_date"):
        if key in payload:
            return normalize_year(payload.get(key))
    return None


def load_relationship_endpoint_facts(
    conn: sqlite3.Connection,
    *,
    object_ref: str,
    workspace_id: str | None,
) -> EndpointFacts:
    birth_facts: list[TemporalFact] = []
    death_facts: list[TemporalFact] = []
    for row in _claims_for_ref_and_types(
        conn,
        about_object_ref=object_ref,
        claim_types=BIRTH_CLAIM_TYPES | DEATH_CLAIM_TYPES,
        workspace_id=workspace_id,
    ):
        payload = _structured_claim_payload(row)
        if payload is None:
            continue
        year = _year_from_structured_fact(payload)
        if year is None:
            continue
        fact_type = str(row["claim_type"] or payload.get("claim_type") or "").strip().casefold()
        fact = TemporalFact(
            fact_type=fact_type,
            year=year,
            source_claim_id=int(row["source_claim_id"]),
            about_object_ref=object_ref,
        )
        if fact_type in BIRTH_CLAIM_TYPES:
            birth_facts.append(fact)
        elif fact_type in DEATH_CLAIM_TYPES:
            death_facts.append(fact)
    return EndpointFacts(
        object_ref=object_ref,
        birth_years=tuple(birth_facts),
        death_years=tuple(death_facts),
    )


def _cached_relationship_endpoint_facts(
    conn: sqlite3.Connection,
    *,
    object_ref: str,
    workspace_id: str | None,
    endpoint_facts_cache: dict[tuple[str | None, str], EndpointFacts] | None = None,
) -> EndpointFacts:
    if endpoint_facts_cache is None:
        return load_relationship_endpoint_facts(
            conn,
            object_ref=object_ref,
            workspace_id=workspace_id,
        )
    key = (workspace_id, object_ref)
    if key not in endpoint_facts_cache:
        endpoint_facts_cache[key] = load_relationship_endpoint_facts(
            conn,
            object_ref=object_ref,
            workspace_id=workspace_id,
        )
    return endpoint_facts_cache[key]


def _relationship_structured_payload(row: sqlite3.Row) -> dict[str, Any]:
    evidence_note = row["evidence_note"]
    if not isinstance(evidence_note, str) or not evidence_note.strip():
        return {}
    try:
        payload = json.loads(evidence_note)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _relationship_event_year(row: sqlite3.Row) -> int | None:
    payload = _relationship_structured_payload(row)
    for key in ("event_year", "event_date", "year", "date"):
        if key in payload:
            return normalize_year(payload.get(key))
    return None


def lifespan_interval(facts: EndpointFacts) -> tuple[int | None, int | None]:
    birth_year = min((fact.year for fact in facts.birth_years), default=None)
    death_year = max((fact.year for fact in facts.death_years), default=None)
    return birth_year, death_year


def intervals_overlap(
    left_start: int | None,
    left_end: int | None,
    right_start: int | None,
    right_end: int | None,
) -> bool | None:
    if left_start is None and left_end is None:
        return None
    if right_start is None and right_end is None:
        return None
    if left_start is not None and right_end is not None and left_start > right_end:
        return False
    return not (right_start is not None and left_end is not None and right_start > left_end)


def _event_outside_lifespan(event_year: int, facts: EndpointFacts) -> TemporalFact | None:
    for birth in facts.birth_years:
        if event_year < birth.year:
            return birth
    for death in facts.death_years:
        if event_year > death.year:
            return death
    return None


def _load_relationship_row(conn: sqlite3.Connection, source_relationship_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM source_relationship WHERE source_relationship_id=?",
        (source_relationship_id,),
    ).fetchone()
    if row is None:
        raise CanonicalReconciliationError(
            f"missing source_relationship id: {source_relationship_id}"
        )
    return row


def _relationship_has_structured_claim_counterpart(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> bool:
    rows = conn.execute(
        """
        SELECT *
        FROM source_claim
        WHERE provenance_event_ref=? AND claim_type IN ('taught_by', 'relationship_taught_by')
        ORDER BY source_claim_id
        """,
        (row["provenance_event_ref"],),
    ).fetchall()
    from_ref = str(row["from_object_ref"])
    to_ref = None if row["to_object_ref"] is None else str(row["to_object_ref"])
    predicate = str(row["predicate"]).strip().casefold()
    for claim_row in rows:
        payload = _structured_claim_payload(claim_row)
        if payload is None:
            continue
        claim_from = payload.get("from_object_ref") or payload.get("about_object_ref")
        claim_to = payload.get("to_object_ref") or payload.get("object_object_ref")
        claim_predicate = (
            str(payload.get("predicate") or claim_row["claim_type"] or "").strip().casefold()
        )
        if claim_from == from_ref and claim_to == to_ref and claim_predicate == predicate:
            return True
    return False


def evaluate_temporal_relation_constraint(
    *,
    row: sqlite3.Row,
    subject_facts: EndpointFacts,
    object_facts: EndpointFacts,
) -> tuple[list[RelationalContradiction], list[str]]:
    predicate = str(row["predicate"]).strip().casefold()
    relationship_id = int(row["source_relationship_id"])
    if predicate not in RELATIONAL_CONSTRAINTS:
        return [], [f"no relational constraint registered for predicate {predicate!r}"]
    constraint = RELATIONAL_CONSTRAINTS[predicate]
    skip_reason = constraint.get("skip_reason")
    if isinstance(skip_reason, str):
        return [], [skip_reason]

    contradictions: list[RelationalContradiction] = []
    skipped: list[str] = []

    def add_lifespan_contradiction(
        *,
        left_birth: TemporalFact,
        right_death: TemporalFact,
        subject_role: str,
        object_role: str,
    ) -> None:
        rationale = (
            f"relationship {relationship_id} predicate {predicate} is impossible: "
            f"{subject_role} birth year {left_birth.year} is after {object_role} death year {right_death.year}"
        )
        contradictions.append(
            RelationalContradiction(
                relationship_id=relationship_id,
                rule_id=RELATIONAL_TEMPORAL_RULE,
                target_object_ref=_source_claim_ref(right_death.source_claim_id),
                rationale=rationale,
            )
        )

    if predicate == "taught_by":
        if not subject_facts.birth_years or not object_facts.death_years:
            skipped.append("taught_by requires subject birth year and object death year")
        for birth in subject_facts.birth_years:
            for death in object_facts.death_years:
                if birth.year > death.year:
                    add_lifespan_contradiction(
                        left_birth=birth,
                        right_death=death,
                        subject_role="subject",
                        object_role="object",
                    )
    elif predicate == "teacher_of":
        if not object_facts.birth_years or not subject_facts.death_years:
            skipped.append("teacher_of requires object birth year and subject death year")
        for birth in object_facts.birth_years:
            for death in subject_facts.death_years:
                if birth.year > death.year:
                    add_lifespan_contradiction(
                        left_birth=birth,
                        right_death=death,
                        subject_role="object",
                        object_role="subject",
                    )
    elif predicate == "met":
        overlap = intervals_overlap(
            *lifespan_interval(subject_facts), *lifespan_interval(object_facts)
        )
        if overlap is False:
            subject_birth, subject_death = lifespan_interval(subject_facts)
            object_birth, object_death = lifespan_interval(object_facts)
            if (
                subject_birth is not None
                and object_death is not None
                and subject_birth > object_death
            ):
                target = object_facts.death_years[0]
                rationale = (
                    f"relationship {relationship_id} predicate met is impossible: "
                    f"subject birth year {subject_birth} is after object death year {object_death}"
                )
                contradictions.append(
                    RelationalContradiction(
                        relationship_id=relationship_id,
                        rule_id=RELATIONAL_TEMPORAL_RULE,
                        target_object_ref=_source_claim_ref(target.source_claim_id),
                        rationale=rationale,
                    )
                )
            elif (
                object_birth is not None
                and subject_death is not None
                and object_birth > subject_death
            ):
                target = subject_facts.death_years[0]
                rationale = (
                    f"relationship {relationship_id} predicate met is impossible: "
                    f"object birth year {object_birth} is after subject death year {subject_death}"
                )
                contradictions.append(
                    RelationalContradiction(
                        relationship_id=relationship_id,
                        rule_id=RELATIONAL_TEMPORAL_RULE,
                        target_object_ref=_source_claim_ref(target.source_claim_id),
                        rationale=rationale,
                    )
                )
        elif overlap is None:
            skipped.append("met requires enough endpoint lifespan facts to prove non-overlap")

    event_year = _relationship_event_year(row)
    if event_year is not None and predicate in {"taught_by", "teacher_of", "met"}:
        for role, facts in (("subject", subject_facts), ("object", object_facts)):
            boundary_fact = _event_outside_lifespan(event_year, facts)
            if boundary_fact is None:
                continue
            direction = (
                "before birth year"
                if boundary_fact.fact_type in BIRTH_CLAIM_TYPES
                else "after death year"
            )
            rationale = (
                f"relationship {relationship_id} predicate {predicate} has event year {event_year} "
                f"{direction} {boundary_fact.year} for {role} {facts.object_ref}"
            )
            contradictions.append(
                RelationalContradiction(
                    relationship_id=relationship_id,
                    rule_id=RELATIONAL_EVENT_YEAR_RULE,
                    target_object_ref=_source_claim_ref(boundary_fact.source_claim_id),
                    rationale=rationale,
                )
            )
    return contradictions, skipped


def record_relational_contradiction(
    conn: sqlite3.Connection,
    *,
    contradiction: RelationalContradiction,
    relationship_row: sqlite3.Row,
    changed_at: str,
    source_run_id: str | None,
) -> canonical_store.CanonicalWriteResult:
    return record_source_contradiction(
        conn,
        offending_namespace="source_relationship",
        offending_id=contradiction.relationship_id,
        target_object_ref=contradiction.target_object_ref,
        provenance_event_ref=str(relationship_row["provenance_event_ref"]),
        workspace_id=None
        if relationship_row["workspace_id"] is None
        else str(relationship_row["workspace_id"]),
        rule_id=contradiction.rule_id,
        rationale=contradiction.rationale,
        changed_at=changed_at,
        source_run_id=source_run_id,
    )


def detect_relational_contradictions_for_relationship(
    conn: sqlite3.Connection,
    *,
    source_relationship_id: int,
    changed_at: str,
    source_run_id: str | None = None,
    relationship_row: sqlite3.Row | None = None,
    subject_facts: EndpointFacts | None = None,
    object_facts: EndpointFacts | None = None,
    endpoint_facts_cache: dict[tuple[str | None, str], EndpointFacts] | None = None,
) -> dict[str, Any]:
    if relationship_row is None:
        relationship_row = _load_relationship_row(conn, source_relationship_id)
    else:
        loaded_row_id = int(relationship_row["source_relationship_id"])
        if loaded_row_id != source_relationship_id:
            raise CanonicalReconciliationError(
                "relationship_row source_relationship_id does not match requested row"
            )
    row = relationship_row
    if row is None:
        return {"results": [], "skipped": ["relationship row is missing"]}

    predicate = str(row["predicate"]).strip().casefold()
    if predicate == CONTRADICTION_PREDICATE:
        return {"results": [], "skipped": ["contradiction relationship rows are not checked"]}
    if predicate not in RELATIONAL_CONSTRAINTS:
        return {
            "results": [],
            "skipped": [f"no relational constraint registered for predicate {predicate!r}"],
        }
    to_object_ref = row["to_object_ref"]
    if not isinstance(to_object_ref, str) or not to_object_ref.strip():
        return {"results": [], "skipped": ["relationship has no to_object_ref"]}
    workspace_id = None if row["workspace_id"] is None else str(row["workspace_id"])
    if subject_facts is None:
        subject_facts = _cached_relationship_endpoint_facts(
            conn,
            object_ref=str(row["from_object_ref"]),
            workspace_id=workspace_id,
            endpoint_facts_cache=endpoint_facts_cache,
        )
    if object_facts is None:
        object_facts = _cached_relationship_endpoint_facts(
            conn,
            object_ref=to_object_ref,
            workspace_id=workspace_id,
            endpoint_facts_cache=endpoint_facts_cache,
        )
    contradictions, skipped = evaluate_temporal_relation_constraint(
        row=row,
        subject_facts=subject_facts,
        object_facts=object_facts,
    )
    results = [
        record_relational_contradiction(
            conn,
            contradiction=contradiction,
            relationship_row=row,
            changed_at=changed_at,
            source_run_id=source_run_id,
        )
        for contradiction in contradictions
    ]
    return {"results": results, "skipped": skipped}


def run_relational_constraint_pass(
    conn: sqlite3.Connection,
    *,
    provenance_event_ref: str | None = None,
    workspace_id: str | None = None,
    changed_at: str,
    source_run_id: str | None = None,
    skip_structured_claim_counterparts: bool = False,
) -> dict[str, int]:
    params: list[Any] = [CONTRADICTION_PREDICATE]
    clauses = ["predicate<>?"]
    if provenance_event_ref is not None:
        clauses.append("provenance_event_ref=?")
        params.append(provenance_event_ref)
    if workspace_id is not None:
        clauses.append("workspace_id=?")
        params.append(workspace_id)
    rows = conn.execute(
        f"""
        SELECT *
        FROM source_relationship
        WHERE {" AND ".join(clauses)}
        ORDER BY source_relationship_id
        """,
        tuple(params),
    ).fetchall()
    counts = {
        "relational_constraints_checked": 0,
        "relational_constraints_skipped": 0,
        "relational_contradictions_detected": 0,
    }
    endpoint_facts_cache: dict[tuple[str | None, str], EndpointFacts] = {}

    for row in rows:
        predicate = str(row["predicate"]).strip().casefold()
        if predicate not in RELATIONAL_CONSTRAINTS:
            continue
        workspace_id = None if row["workspace_id"] is None else str(row["workspace_id"])
        to_object_ref = row["to_object_ref"]
        if not isinstance(to_object_ref, str):
            continue
        if skip_structured_claim_counterparts and _relationship_has_structured_claim_counterpart(
            conn, row
        ):
            counts["relational_constraints_skipped"] += 1
            continue
        counts["relational_constraints_checked"] += 1
        result = detect_relational_contradictions_for_relationship(
            conn,
            source_relationship_id=int(row["source_relationship_id"]),
            relationship_row=row,
            endpoint_facts_cache=endpoint_facts_cache,
            changed_at=changed_at,
            source_run_id=source_run_id,
        )
        counts["relational_contradictions_detected"] += len(result["results"])
        counts["relational_constraints_skipped"] += len(result["skipped"])
    return counts


def _structured_contradictions_for_claim_group(
    conn: sqlite3.Connection,
    *,
    claim_rows: list[sqlite3.Row],
    provenance_event_ref: str,
    changed_at: str,
    source_run_id: str | None = None,
    include_excluded_claim_states: bool = False,
) -> list[canonical_store.CanonicalWriteResult]:
    results: list[canonical_store.CanonicalWriteResult] = []
    payloads: dict[int, dict[str, Any] | None] = {}
    for row in claim_rows:
        payloads[int(row["source_claim_id"])] = _structured_claim_payload(row)

    claims_for_key: dict[tuple[str | None, str, str], list[sqlite3.Row]] = {}

    for left_row in claim_rows:
        left_claim_id = int(left_row["source_claim_id"])
        left_payload = payloads[left_claim_id]
        if left_payload is None:
            continue

        left_about_ref = _claim_about_ref(left_row, left_payload)
        left_claim_type = str(
            left_row["claim_type"] or left_payload.get("claim_type") or ""
        ).strip()
        left_workspace_id = (
            None if left_row["workspace_id"] is None else str(left_row["workspace_id"])
        )

        left_numeric = _structured_numeric_value(left_payload)
        if left_numeric is not None and left_claim_type and left_about_ref is not None:
            claim_key = (left_workspace_id, left_about_ref, left_claim_type)
            peer_rows = claims_for_key.get(claim_key)
            if peer_rows is None:
                peer_rows = _claims_for_ref_and_type(
                    conn,
                    about_object_ref=left_about_ref,
                    claim_type=left_claim_type,
                    workspace_id=left_workspace_id,
                    excluded_review_states=()
                    if include_excluded_claim_states
                    else CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES,
                )
                claims_for_key[claim_key] = peer_rows
            for right_row in peer_rows:
                right_claim_id = int(right_row["source_claim_id"])
                right_payload = payloads.get(right_claim_id)
                if right_payload is None:
                    right_payload = _structured_claim_payload(right_row)
                    payloads[right_claim_id] = right_payload
                if right_claim_id == left_claim_id:
                    continue
                right_numeric = _structured_numeric_value(right_payload)
                if right_numeric is None or right_numeric == left_numeric:
                    continue
                right_about_ref = _claim_about_ref(right_row, right_payload)
                if right_about_ref != left_about_ref:
                    continue
                left_id = min(left_claim_id, right_claim_id)
                right_id = max(left_claim_id, right_claim_id)
                left_value = left_numeric if left_id == left_claim_id else right_numeric
                right_value = right_numeric if left_id == left_claim_id else left_numeric
                rationale = (
                    f"structured claim conflict for {left_about_ref} {left_claim_type}: "
                    f"{left_value} versus {right_value}"
                )
                should_persist = not (
                    _claim_is_excluded_from_structured_contradictions(left_row)
                    or _claim_is_excluded_from_structured_contradictions(right_row)
                )
                if should_persist:
                    result = record_source_contradiction(
                        conn,
                        offending_namespace="source_claim",
                        offending_id=left_id,
                        target_object_ref=_source_claim_ref(right_id),
                        provenance_event_ref=provenance_event_ref,
                        workspace_id=left_workspace_id,
                        rule_id=QUANTITY_CONFLICT_RULE,
                        rationale=rationale,
                        changed_at=changed_at,
                        source_run_id=source_run_id,
                    )
                    update_review_state(
                        conn,
                        target_namespace="source_claim",
                        target_id=right_id,
                        new_state=RECONCILIATION_REVIEW_STATE,
                        changed_at=changed_at,
                        reason=QUANTITY_CONFLICT_RULE,
                        note=rationale,
                        source_namespace="source_relationship",
                        source_id=str(result.row_id),
                        source_run_id=source_run_id,
                    )
                else:
                    result = canonical_store.CanonicalWriteResult(
                        table="source_relationship",
                        row_id=-1,
                        key=None,
                        created=False,
                    )
                results.append(result)

        relation_type = left_claim_type.casefold()
        left_predicate = str(left_payload.get("predicate") or "").strip().casefold()
        if (
            relation_type in {"taught_by", "relationship_taught_by"}
            or left_predicate == "taught_by"
        ):
            subject_ref = left_about_ref
            object_ref = left_payload.get("to_object_ref") or left_payload.get("object_object_ref")
            if (
                isinstance(subject_ref, str)
                and subject_ref
                and isinstance(object_ref, str)
                and object_ref
            ):
                birth_rows = _claims_for_ref_and_type(
                    conn,
                    about_object_ref=subject_ref,
                    claim_type="birth_year",
                    workspace_id=left_workspace_id,
                    excluded_review_states=()
                    if include_excluded_claim_states
                    else CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES,
                )
                death_rows = _claims_for_ref_and_type(
                    conn,
                    about_object_ref=object_ref,
                    claim_type="death_year",
                    workspace_id=left_workspace_id,
                    excluded_review_states=()
                    if include_excluded_claim_states
                    else CONTRADICTION_EXCLUDED_CLAIM_REVIEW_STATES,
                )
                for birth_row in birth_rows:
                    birth_payload = _structured_claim_payload(birth_row)
                    birth_year = (
                        None
                        if birth_payload is None
                        else normalize_year(birth_payload.get("year", birth_payload.get("value")))
                    )
                    if birth_year is None:
                        continue
                    for death_row in death_rows:
                        death_payload = _structured_claim_payload(death_row)
                        death_year = (
                            None
                            if death_payload is None
                            else normalize_year(
                                death_payload.get("year", death_payload.get("value"))
                            )
                        )
                        if death_year is None or birth_year <= death_year:
                            continue
                        rationale = (
                            f"claim says {subject_ref} was taught_by {object_ref}, "
                            f"but subject birth year {birth_year} is after object death year {death_year}"
                        )
                        should_persist = not (
                            _claim_is_excluded_from_structured_contradictions(left_row)
                            or _claim_is_excluded_from_structured_contradictions(birth_row)
                            or _claim_is_excluded_from_structured_contradictions(death_row)
                        )
                        if should_persist:
                            result = record_source_contradiction(
                                conn,
                                offending_namespace="source_claim",
                                offending_id=left_claim_id,
                                target_object_ref=_source_claim_ref(
                                    int(death_row["source_claim_id"])
                                ),
                                provenance_event_ref=provenance_event_ref,
                                workspace_id=left_workspace_id,
                                rule_id=TAUGHT_BY_IMPOSSIBLE_RULE,
                                rationale=rationale,
                                changed_at=changed_at,
                                source_run_id=source_run_id,
                            )
                        else:
                            result = canonical_store.CanonicalWriteResult(
                                table="source_relationship",
                                row_id=-1,
                                key=None,
                                created=False,
                            )
                        relation_row = conn.execute(
                            """
                            SELECT source_relationship_id
                            FROM source_relationship
                            WHERE provenance_event_ref=? AND from_object_ref=? AND to_object_ref=? AND predicate='taught_by'
                            ORDER BY source_relationship_id
                            LIMIT 1
                            """,
                            (provenance_event_ref, subject_ref, object_ref),
                        ).fetchone()
                        if relation_row is not None and should_persist:
                            update_review_state(
                                conn,
                                target_namespace="source_relationship",
                                target_id=int(relation_row["source_relationship_id"]),
                                new_state=RECONCILIATION_REVIEW_STATE,
                                changed_at=changed_at,
                                reason=TAUGHT_BY_IMPOSSIBLE_RULE,
                                note=rationale,
                                source_namespace="source_relationship",
                                source_id=str(result.row_id),
                                source_run_id=source_run_id,
                            )
                        results.append(result)
                        return results
    return results


def detect_structured_contradictions_for_claim(
    conn: sqlite3.Connection,
    *,
    source_claim_id: int,
    provenance_event_ref: str,
    changed_at: str,
    source_run_id: str | None = None,
) -> list[canonical_store.CanonicalWriteResult]:
    row = _load_claim_row(conn, source_claim_id)
    return _structured_contradictions_for_claim_group(
        conn,
        claim_rows=[row],
        provenance_event_ref=provenance_event_ref,
        changed_at=changed_at,
        source_run_id=source_run_id,
        include_excluded_claim_states=True,
    )


def run_reconciliation_pass_for_ingest(
    conn: sqlite3.Connection,
    *,
    provenance_event_ref: str,
    workspace_id: str | None,
    changed_at: str,
    entity_candidates: list[dict[str, Any]] | None = None,
    source_run_id: str | None = None,
) -> dict[str, int]:
    counts = {
        "work_deduped": 0,
        "authority_reconciled": 0,
        "authority_merged": 0,
        "claims_contradicted": 0,
        "relationships_contradicted": 0,
        "relational_constraints_checked": 0,
        "relational_constraints_skipped": 0,
    }
    for entity in entity_candidates or []:
        entity_id = int(entity["detected_entity_id"])
        matches = find_existing_authority_match(
            conn,
            entity_label=str(entity["entity_label"]),
            entity_type=None if entity.get("entity_type") is None else str(entity["entity_type"]),
            structured=entity.get("structured"),
        )
        if not matches:
            continue
        first_match = matches[0]
        if first_match.automatic_merge:
            local_authority_id = _ensure_local_authority_candidate(
                conn,
                detected_entity_id=entity_id,
                entity_label=str(entity["entity_label"]),
                entity_type=None
                if entity.get("entity_type") is None
                else str(entity["entity_type"]),
                workspace_id=workspace_id,
                provenance_event_ref=provenance_event_ref,
                confidence_score=entity.get("confidence_score"),
                created_at=changed_at,
            )
            merge_result = record_authority_merge_event(
                conn,
                from_authority_record_id=local_authority_id,
                into_authority_record_id=first_match.authority_record_id,
                merge_reason="exact_authority_identifier_duplicate",
                evidence_note=(
                    f"detected entity {entity_id} matched authority:{first_match.authority_record_id} "
                    f"by exact normalized identifier"
                ),
                merged_by="canonical_reconciliation",
                merged_at=changed_at,
            )
            linked = _maybe_link_detected_entity_authority(
                conn,
                detected_entity_id=entity_id,
                authority_record_id=first_match.authority_record_id,
                changed_at=changed_at,
            )
            if merge_result.created or linked:
                counts["authority_merged"] += 1
            continue

        review_state = "ambiguous" if len(matches) > 1 else RECONCILIATION_REVIEW_STATE
        for match in matches:
            result = record_authority_reconciliation(
                conn,
                detected_entity_id=entity_id,
                raw_label=str(entity["entity_label"]),
                entity_type=None
                if entity.get("entity_type") is None
                else str(entity["entity_type"]),
                candidate_authority_record_id=match.authority_record_id,
                method=match.method,
                match_method=match.method,
                confidence_score=match.confidence_score,
                evidence_context=(
                    "normalized label and entity type match"
                    if match.method == "normalized_label_and_type"
                    else "ambiguous normalized label and entity type match"
                ),
                review_state=review_state,
                created_at=changed_at,
            )
            if result.created:
                counts["authority_reconciled"] += 1

    claim_rows = conn.execute(
        """
        SELECT *
        FROM source_claim
        WHERE provenance_event_ref=?
        ORDER BY source_claim_id
        """,
        (provenance_event_ref,),
    ).fetchall()
    grouped_claim_rows: dict[tuple[str | None, str | None, str], list[sqlite3.Row]] = {}
    for claim_row in claim_rows:
        about_object_ref = (
            None if claim_row["about_object_ref"] is None else str(claim_row["about_object_ref"])
        )
        claim_type = "" if claim_row["claim_type"] is None else str(claim_row["claim_type"])
        workspace_key = (
            None if claim_row["workspace_id"] is None else str(claim_row["workspace_id"])
        )
        key = (workspace_key, about_object_ref, claim_type)
        grouped_claim_rows.setdefault(key, []).append(claim_row)

    seen_relationship_rows: set[int] = set()
    for claim_group in grouped_claim_rows.values():
        results = _structured_contradictions_for_claim_group(
            conn,
            claim_rows=claim_group,
            provenance_event_ref=provenance_event_ref,
            changed_at=changed_at,
            source_run_id=source_run_id,
        )
        for result in results:
            if result.created:
                counts["claims_contradicted"] += 1
            relationship_row = conn.execute(
                """
                SELECT source_relationship_id
                FROM source_relationship
                WHERE source_relationship_id=?
                """,
                (result.row_id,),
            ).fetchone()
            if relationship_row is not None:
                seen_relationship_rows.add(int(relationship_row["source_relationship_id"]))
    counts["relationships_contradicted"] = len(seen_relationship_rows)
    relational_counts = run_relational_constraint_pass(
        conn,
        provenance_event_ref=provenance_event_ref,
        workspace_id=workspace_id,
        changed_at=changed_at,
        source_run_id=source_run_id,
        skip_structured_claim_counterparts=True,
    )
    counts["relational_constraints_checked"] = relational_counts["relational_constraints_checked"]
    counts["relational_constraints_skipped"] = relational_counts["relational_constraints_skipped"]
    counts["relationships_contradicted"] += relational_counts["relational_contradictions_detected"]
    return counts
