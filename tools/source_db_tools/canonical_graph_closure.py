"""Read-only graph-closure audit for the canonical SQLite store.

Graph closure checks attachment, provenance, and reviewability. It does not
adjudicate whether source claims are true.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from tools.source_db_tools import canonical_store

REPORT_SCHEMA_VERSION = "canonical-graph-closure-report.v1"
AUDIT_TOOL = "tools/source_db_tools/canonical_graph_closure.py"
REVIEWABLE_STATES = {
    "ambiguous",
    "machine_extracted",
    "needs_review",
    "proposed",
    "recorded",
    "unreviewed",
}
RESOLVED_STATES = {
    "accepted",
    "approved",
    "curated",
    "demoted",
    "deprecated",
    "rejected",
    "reviewed",
}
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

OBJECT_REF_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "authority_record": ("authority_record", ("authority_record_id", "authority_key_v1")),
    "authority": ("authority_record", ("authority_record_id", "authority_key_v1")),
    "authority_reconciliation": (
        "authority_reconciliation",
        ("authority_reconciliation_id", "reconciliation_key_v1"),
    ),
    "capture_event": ("capture_event", ("capture_event_id",)),
    "extraction_detected_entity": ("extraction_detected_entity", ("detected_entity_id",)),
    "extraction_record": ("extraction_record", ("extraction_id",)),
    "source_access": ("source_access", ("source_access_id",)),
    "source_claim": ("source_claim", ("source_claim_id", "source_claim_key_v1")),
    "source_relationship": ("source_relationship", ("source_relationship_id",)),
    "topic_extension": ("topic_extension", ("topic_extension_id",)),
    "work": ("work", ("work_id", "work_key_v1")),
    "work_subject": ("work_subject", ("work_subject_id",)),
}

AUDITED_TABLES = (
    "provenance_event",
    "work",
    "source_access",
    "capture_event",
    "extraction_record",
    "extraction_detected_entity",
    "source_claim",
    "source_relationship",
    "authority_reconciliation",
    "authority_merge_event",
    "review_state_history",
    "work_subject",
    "authority_identifier",
    "work_identifier",
    "work_metadata",
    "work_url",
    "topic_extension",
)


class GraphClosureError(RuntimeError):
    """Raised when a graph-closure audit cannot be produced."""


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reviewable(state: Any) -> bool:
    return (_text(state) or "") in REVIEWABLE_STATES


class GraphClosureLookup:
    """Read-only cache of provenance and object-reference existence checks."""

    __slots__ = ("conn", "_object_ref_cache", "_provenance_keys")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._object_ref_cache: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        self._provenance_keys: set[str] | None = None

    def provenance_exists(self, key: Any) -> bool:
        text = _text(key)
        if text is None:
            return False
        if self._provenance_keys is None:
            self._provenance_keys = {
                text
                for row in self.conn.execute("SELECT provenance_event_key_v1 FROM provenance_event")
                if (text := _text(row[0])) is not None
            }
        return text in self._provenance_keys

    def object_ref_exists(self, object_ref: str | None) -> bool:
        text = _text(object_ref)
        if text is None or ":" not in text:
            return False
        namespace, raw_id = text.split(":", 1)
        if namespace not in OBJECT_REF_TABLES or not raw_id.strip():
            return False
        table, columns = OBJECT_REF_TABLES[namespace]
        if not SQL_IDENTIFIER_RE.fullmatch(table):
            raise GraphClosureError(f"invalid SQL identifier: {table}")
        for column in columns:
            if not SQL_IDENTIFIER_RE.fullmatch(column):
                raise GraphClosureError(f"invalid SQL identifier: {column}")
        cache_key = (table, columns)
        refs = self._object_ref_cache.get(cache_key)
        if refs is None:
            select_columns = ", ".join(f"CAST({column} AS TEXT)" for column in columns)
            where_clause = " OR ".join(f"{column} IS NOT NULL" for column in columns)
            refs = set()
            for row in self.conn.execute(
                f"SELECT {select_columns} FROM {table} WHERE {where_clause}"
            ):
                for value in row:
                    text_value = _text(value)
                    if text_value is not None:
                        refs.add(text_value)
            self._object_ref_cache[cache_key] = refs
        return raw_id.strip() in refs


def _provenance_resolution(
    lookup: GraphClosureLookup, provenance_ref: Any
) -> tuple[str | None, bool]:
    provenance_key = _text(provenance_ref)
    return provenance_key, provenance_key is not None and lookup.provenance_exists(provenance_key)


def _provenance_exists(conn: sqlite3.Connection, key: Any) -> bool:
    return GraphClosureLookup(conn).provenance_exists(key)


def object_ref_exists(conn: sqlite3.Connection, object_ref: str | None) -> bool:
    return GraphClosureLookup(conn).object_ref_exists(object_ref)


def issue(
    *,
    table: str,
    primary_key: Any,
    status: str,
    severity: str,
    code: str,
    message: str,
    attachment_policy: str,
) -> dict[str, Any]:
    return {
        "table": table,
        "primary_key": str(primary_key),
        "status": status,
        "severity": severity,
        "code": code,
        "message": message,
        "attachment_policy": attachment_policy,
    }


def unresolved_issue(
    table: str,
    primary_key: Any,
    message: str,
    *,
    policy: str = "tracked_unresolved_rows_are_reviewable",
) -> dict[str, Any]:
    return issue(
        table=table,
        primary_key=primary_key,
        status="unresolved_tracked",
        severity="warning",
        code=f"{table.upper()}_UNRESOLVED_TRACKED",
        message=message,
        attachment_policy=policy,
    )


def orphan_issue(
    table: str,
    primary_key: Any,
    message: str,
    *,
    policy: str = "canonical_rows_must_have_graph_attachment_or_tracked_provenance",
) -> dict[str, Any]:
    return issue(
        table=table,
        primary_key=primary_key,
        status="true_orphan_error",
        severity="fail",
        code=f"{table.upper()}_TRUE_ORPHAN",
        message=message,
        attachment_policy=policy,
    )


def exempt_issue(table: str, primary_key: Any, message: str) -> dict[str, Any]:
    return issue(
        table=table,
        primary_key=primary_key,
        status="intentionally_exempt",
        severity="info",
        code=f"{table.upper()}_INTENTIONALLY_EXEMPT",
        message=message,
        attachment_policy="supporting_or_operational_evidence_table",
    )


def _provenance_issue(
    lookup: GraphClosureLookup, row: sqlite3.Row, table: str, pk: str
) -> tuple[str | None, dict[str, Any] | None]:
    provenance_key, has_provenance = _provenance_resolution(lookup, row["provenance_event_ref"])
    if provenance_key is None:
        return None, orphan_issue(
            table,
            row[pk],
            f"{table} row lacks resolvable provenance_event_ref",
            policy="provenance_event_ref_must_resolve",
        )
    if has_provenance:
        return provenance_key, None
    return provenance_key, orphan_issue(
        table,
        row[pk],
        f"{table}.{pk} references missing provenance_event_ref {provenance_key!r}",
        policy="provenance_event_ref_must_resolve",
    )


def audit_work(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM work ORDER BY work_id"):
        _provenance_key, invalid = _provenance_issue(lookup, row, "work", "work_id")
        if invalid is not None:
            issues.append(invalid)
    return issues


def audit_source_access(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM source_access ORDER BY source_access_id"):
        if row["work_id"] is not None and not lookup.object_ref_exists(f"work:{row['work_id']}"):
            issues.append(
                orphan_issue(
                    "source_access",
                    row["source_access_id"],
                    "source_access.work_id does not resolve",
                    policy="source_access_must_link_to_work",
                )
            )
            continue
        linked_work = row["work_id"] is not None
        tracked_locus = any(
            _text(row[key]) is not None
            for key in ("source_locus_id", "source_lead_id", "workspace_id")
        )
        if linked_work or tracked_locus:
            continue
        issues.append(
            orphan_issue(
                "source_access",
                row["source_access_id"],
                "source_access row has no work_id, source locus/lead, or workspace context",
            )
        )
    return issues


def audit_capture_event(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM capture_event ORDER BY capture_event_id"):
        provenance_key, invalid = _provenance_issue(
            lookup, row, "capture_event", "capture_event_id"
        )
        if invalid is not None:
            issues.append(invalid)
            continue
        if row["work_id"] is not None and not lookup.object_ref_exists(f"work:{row['work_id']}"):
            issues.append(
                orphan_issue(
                    "capture_event",
                    row["capture_event_id"],
                    "capture_event.work_id does not resolve",
                    policy="capture_event_must_link_to_work",
                )
            )
            continue
        if row["work_id"] is not None or provenance_key is not None:
            continue
        if _reviewable(row["review_state"]) and _text(row["source_locus_ref"]) is not None:
            issues.append(
                unresolved_issue(
                    "capture_event",
                    row["capture_event_id"],
                    "capture_event is reviewable and tracked by source_locus_ref but not linked to a work",
                )
            )
        else:
            issues.append(
                orphan_issue(
                    "capture_event",
                    row["capture_event_id"],
                    "capture_event has no work_id and no resolvable provenance",
                )
            )
    return issues


def audit_extraction_record(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT extraction_record.*, capture_event.capture_event_id AS linked_capture_id
        FROM extraction_record
        LEFT JOIN capture_event USING (capture_event_id)
        ORDER BY extraction_record.extraction_id
        """
    ):
        _provenance_key, invalid = _provenance_issue(
            lookup, row, "extraction_record", "extraction_id"
        )
        if invalid is not None:
            issues.append(invalid)
        elif row["linked_capture_id"] is None:
            issues.append(
                orphan_issue(
                    "extraction_record",
                    row["extraction_id"],
                    "extraction_record.capture_event_id does not resolve",
                    policy="extraction_record_must_link_to_capture_event",
                )
            )
    return issues


def audit_extraction_detected_entity(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM extraction_detected_entity ORDER BY detected_entity_id"):
        provenance_key, invalid = _provenance_issue(
            lookup, row, "extraction_detected_entity", "detected_entity_id"
        )
        if invalid is not None:
            issues.append(invalid)
            continue
        extraction_ok = row["extraction_id"] is not None and lookup.object_ref_exists(
            f"extraction_record:{row['extraction_id']}"
        )
        capture_ok = row["capture_event_id"] is not None and lookup.object_ref_exists(
            f"capture_event:{row['capture_event_id']}"
        )
        if extraction_ok or capture_ok:
            continue
        if _reviewable(row["review_state"]) and provenance_key is not None:
            issues.append(
                unresolved_issue(
                    "extraction_detected_entity",
                    row["detected_entity_id"],
                    "detected entity has provenance and review state but no extraction/capture attachment",
                )
            )
        else:
            issues.append(
                orphan_issue(
                    "extraction_detected_entity",
                    row["detected_entity_id"],
                    "detected entity has no extraction/capture attachment",
                )
            )
    return issues


def audit_source_claim(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM source_claim ORDER BY source_claim_id"):
        provenance_key, invalid = _provenance_issue(lookup, row, "source_claim", "source_claim_id")
        if invalid is not None:
            issues.append(invalid)
            continue
        attached = any(
            (
                row["capture_event_id"] is not None
                and lookup.object_ref_exists(f"capture_event:{row['capture_event_id']}"),
                row["extraction_id"] is not None
                and lookup.object_ref_exists(f"extraction_record:{row['extraction_id']}"),
                lookup.object_ref_exists(_text(row["about_object_ref"])),
            )
        )
        if attached:
            continue
        if provenance_key is not None and _reviewable(row["review_state"]):
            issues.append(
                unresolved_issue(
                    "source_claim",
                    row["source_claim_id"],
                    "source_claim is provenance-backed and reviewable but not attached to a resolved object",
                )
            )
        else:
            issues.append(
                orphan_issue(
                    "source_claim",
                    row["source_claim_id"],
                    "source_claim has no resolved about/capture/extraction attachment and no reviewable provenance context",
                )
            )
    return issues


def audit_source_relationship(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM source_relationship ORDER BY source_relationship_id"):
        provenance_key, invalid = _provenance_issue(
            lookup, row, "source_relationship", "source_relationship_id"
        )
        if invalid is not None:
            issues.append(invalid)
            continue
        from_ok = lookup.object_ref_exists(_text(row["from_object_ref"]))
        to_ref = _text(row["to_object_ref"])
        to_ok = to_ref is None or lookup.object_ref_exists(to_ref)
        if from_ok and to_ok:
            continue
        has_target_label = _text(row["target_label"]) is not None
        if (
            provenance_key is not None
            and _reviewable(row["review_state"])
            and (from_ok or has_target_label)
        ):
            issues.append(
                unresolved_issue(
                    "source_relationship",
                    row["source_relationship_id"],
                    "source_relationship is provenance-backed and reviewable but has an unresolved endpoint",
                    policy="unresolved_endpoint_must_be_visible_and_reviewable",
                )
            )
        else:
            issues.append(
                orphan_issue(
                    "source_relationship",
                    row["source_relationship_id"],
                    "source_relationship endpoint refs do not resolve and no reviewable provenance context is present",
                    policy="relationship_endpoints_must_resolve_or_be_tracked_unresolved",
                )
            )
    return issues


def audit_authority_reconciliation(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM authority_reconciliation ORDER BY authority_reconciliation_id"
    ):
        detected_ok = row["detected_entity_id"] is not None and lookup.object_ref_exists(
            f"extraction_detected_entity:{row['detected_entity_id']}"
        )
        target_ok = lookup.object_ref_exists(f"{row['target_namespace']}:{row['target_id']}")
        if detected_ok or target_ok:
            continue
        if _reviewable(row["review_state"]) and _text(row["raw_label"]) is not None:
            issues.append(
                unresolved_issue(
                    "authority_reconciliation",
                    row["authority_reconciliation_id"],
                    "authority reconciliation is reviewable but its target/detected entity is unresolved",
                )
            )
        else:
            issues.append(
                orphan_issue(
                    "authority_reconciliation",
                    row["authority_reconciliation_id"],
                    "authority reconciliation target and detected entity do not resolve",
                )
            )
    return issues


def audit_review_state_history(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT *
        FROM review_state_history
        ORDER BY changed_at, target_namespace, target_id, review_state_history_key_v1
        """
    ):
        if lookup.object_ref_exists(f"{row['target_namespace']}:{row['target_id']}"):
            continue
        issues.append(
            orphan_issue(
                "review_state_history",
                row["review_state_history_key_v1"],
                "review_state_history target does not resolve",
                policy="review_history_must_reference_existing_canonical_target",
            )
        )
    return issues


def audit_provenance_event(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM provenance_event ORDER BY provenance_event_id"):
        namespace = _text(row["object_namespace"])
        object_id = _text(row["object_id"])
        if namespace in OBJECT_REF_TABLES:
            if lookup.object_ref_exists(f"{namespace}:{object_id}"):
                continue
            if namespace in {"source_access"}:
                issues.append(
                    unresolved_issue(
                        "provenance_event",
                        row["provenance_event_id"],
                        "provenance_event points to an ingest-time source_access candidate not retained as a direct FK",
                    )
                )
                continue
            issues.append(
                orphan_issue(
                    "provenance_event",
                    row["provenance_event_id"],
                    f"provenance_event object {namespace}:{object_id} does not resolve",
                    policy="provenance_event_object_must_resolve_or_be_noncanonical_artifact_context",
                )
            )
            continue
        issues.append(
            exempt_issue(
                "provenance_event",
                row["provenance_event_id"],
                f"provenance_event object namespace {namespace!r} is noncanonical operational/artifact context",
            )
        )
    return issues


def audit_simple_fk_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    pk_column: str,
    fk_column: str,
    target_namespace: str,
    lookup: GraphClosureLookup | None = None,
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    if not SQL_IDENTIFIER_RE.fullmatch(table):
        raise GraphClosureError(f"invalid SQL identifier: {table}")
    if not SQL_IDENTIFIER_RE.fullmatch(pk_column):
        raise GraphClosureError(f"invalid SQL identifier: {pk_column}")
    if not SQL_IDENTIFIER_RE.fullmatch(fk_column):
        raise GraphClosureError(f"invalid SQL identifier: {fk_column}")
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY {pk_column}"):
        if lookup.object_ref_exists(f"{target_namespace}:{row[fk_column]}"):
            continue
        issues.append(
            orphan_issue(
                table,
                row[pk_column],
                f"{table}.{fk_column} does not resolve",
                policy=f"{table}_must_link_to_{target_namespace}",
            )
        )
    return issues


def audit_authority_merge_event(
    conn: sqlite3.Connection, *, lookup: GraphClosureLookup | None = None
) -> list[dict[str, Any]]:
    lookup = lookup or GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM authority_merge_event ORDER BY authority_merge_event_id"
    ):
        from_ok = lookup.object_ref_exists(f"authority_record:{row['from_authority_record_id']}")
        into_ok = lookup.object_ref_exists(f"authority_record:{row['into_authority_record_id']}")
        if from_ok and into_ok:
            continue
        issues.append(
            orphan_issue(
                "authority_merge_event",
                row["authority_merge_event_id"],
                "authority merge event references a missing authority record",
                policy="authority_merge_events_must_link_two_authority_records",
            )
        )
    return issues


def collect_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    lookup = GraphClosureLookup(conn)
    issues: list[dict[str, Any]] = []
    issues.extend(audit_provenance_event(conn, lookup=lookup))
    issues.extend(audit_work(conn, lookup=lookup))
    issues.extend(audit_source_access(conn, lookup=lookup))
    issues.extend(audit_capture_event(conn, lookup=lookup))
    issues.extend(audit_extraction_record(conn, lookup=lookup))
    issues.extend(audit_extraction_detected_entity(conn, lookup=lookup))
    issues.extend(audit_source_claim(conn, lookup=lookup))
    issues.extend(audit_source_relationship(conn, lookup=lookup))
    issues.extend(audit_authority_reconciliation(conn, lookup=lookup))
    issues.extend(audit_authority_merge_event(conn, lookup=lookup))
    issues.extend(audit_review_state_history(conn, lookup=lookup))
    issues.extend(
        audit_simple_fk_table(
            conn,
            table="work_subject",
            pk_column="work_subject_id",
            fk_column="work_id",
            target_namespace="work",
            lookup=lookup,
        )
    )
    issues.extend(
        audit_simple_fk_table(
            conn,
            table="authority_identifier",
            pk_column="authority_identifier_id",
            fk_column="authority_record_id",
            target_namespace="authority_record",
            lookup=lookup,
        )
    )
    issues.extend(
        audit_simple_fk_table(
            conn,
            table="work_identifier",
            pk_column="work_identifier_id",
            fk_column="work_id",
            target_namespace="work",
            lookup=lookup,
        )
    )
    issues.extend(
        audit_simple_fk_table(
            conn,
            table="work_metadata",
            pk_column="work_metadata_id",
            fk_column="work_id",
            target_namespace="work",
            lookup=lookup,
        )
    )
    issues.extend(
        audit_simple_fk_table(
            conn,
            table="work_url",
            pk_column="work_url_id",
            fk_column="work_id",
            target_namespace="work",
            lookup=lookup,
        )
    )
    return issues


def status_from_counts(summary: dict[str, int]) -> str:
    if summary["true_orphan_error_count"]:
        return "fail"
    if summary["repairable_count"] or summary["quarantined_count"]:
        return "warning"
    if summary["unresolved_tracked_count"]:
        return "pass_with_unresolved"
    if summary["audited_row_count"] == 0:
        return "no_rows"
    return "pass"


def audit_canonical_graph_closure(
    db_path: Path | str,
    *,
    generated_at: str | None = None,
    strict: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    path = canonical_store.resolve_db_path(db_path)
    generated = generated_at or now_rfc3339()
    try:
        conn = canonical_store.connect_existing_read_only(path)
    except (canonical_store.CanonicalStoreError, sqlite3.Error) as exc:
        raise GraphClosureError(f"canonical store unavailable for graph closure: {exc}") from exc
    try:
        try:
            version_row, table_set, _extra = canonical_store.validate_existing_store(conn)
        except (canonical_store.CanonicalStoreError, sqlite3.Error) as exc:
            raise GraphClosureError(f"canonical store invalid for graph closure: {exc}") from exc
        missing = [table for table in AUDITED_TABLES if table not in table_set]
        if missing:
            raise GraphClosureError(
                "canonical store missing graph-closure tables: " + ", ".join(missing)
            )
        table_counts = {table: _count(conn, table) for table in AUDITED_TABLES}
        issues = collect_issues(conn)
    finally:
        conn.close()

    status_counts = {
        "true_orphan_error_count": sum(
            1 for item in issues if item["status"] == "true_orphan_error"
        ),
        "unresolved_tracked_count": sum(
            1 for item in issues if item["status"] == "unresolved_tracked"
        ),
        "repairable_count": sum(1 for item in issues if item["status"] == "repairable"),
        "quarantined_count": sum(1 for item in issues if item["status"] == "quarantined"),
        "intentionally_exempt_count": sum(
            1 for item in issues if item["status"] == "intentionally_exempt"
        ),
        "audited_row_count": sum(table_counts.values()),
    }
    status = status_from_counts(status_counts)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": generated,
        "tool": AUDIT_TOOL,
        "db_path": str(path),
        "canonical_schema_version": version_row.schema_version,
        "canonical_migration_id": version_row.current_migration_id,
        "strict": strict,
        "read_only": True,
        "repair_performed": False,
        "status": status,
        "severity": "fail"
        if status == "fail"
        else ("warning" if status in {"warning", "pass_with_unresolved"} else "pass"),
        "summary": status_counts | {"issue_count": len(issues)},
        "table_counts": table_counts,
        "issues": issues,
        "notes": [
            "Graph closure checks attachment/reviewability, not factual truth.",
            "Repair is not performed by this audit.",
        ],
    }
    if report_path is not None:
        write_json(report_path, report)
        report["report_path"] = str(report_path)
        report["report_sha256"] = sha256_file(report_path)
    return report


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return (
        f"schema_version={report['schema_version']}\n"
        f"status={report['status']}\n"
        f"strict={report['strict']}\n"
        f"orphan_errors={summary['true_orphan_error_count']}\n"
        f"unresolved_tracked={summary['unresolved_tracked_count']}\n"
        f"repairable={summary['repairable_count']}\n"
        f"quarantined={summary['quarantined_count']}\n"
        f"issues={summary['issue_count']}\n"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit canonical graph closure without mutating the store."
    )
    parser.add_argument("--db", required=True, help="Canonical SQLite store to audit.")
    parser.add_argument("--report-json", type=Path, help="Optional JSON report output path.")
    parser.add_argument("--generated-at", help="Timestamp override for deterministic tests.")
    parser.add_argument(
        "--strict", action="store_true", help="Exit nonzero when true orphan errors exist."
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit_canonical_graph_closure(
            args.db,
            generated_at=args.generated_at,
            strict=args.strict,
            report_path=args.report_json,
        )
    except GraphClosureError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.format == "text":
        sys.stdout.write(render_text(report))
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 1 if args.strict and report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
