#!/usr/bin/env python3
"""Export a redacted structural diagnostic bundle for a canonical store.

Documentation: docs/scripts/index_export_redacted_diagnostics.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.leak_scanner import scan_directory  # noqa: E402
from tools.common.operator_text import format_operator_text_value, strip_terminal_escapes  # noqa: E402
from tools.source_db_tools import canonical_graph_closure, canonical_store  # noqa: E402

try:  # noqa: SIM105 - optional helper; diagnostics still work without doctor import.
    from tools.scripts import local_doctor  # noqa: E402
except Exception:  # pragma: no cover - defensive import guard for standalone usage.
    local_doctor = None  # type: ignore[assignment]


MANIFEST_SCHEMA_VERSION = "redacted-diagnostic-manifest.v1"
MANIFEST_FILENAME = "diagnostic-manifest.json"
DIAGNOSTIC_OUTPUT_PROFILE = "public_bundle"
REDACTION_REPORT_SCHEMA_VERSION = "redacted-diagnostic-redaction-report.v1"
EXPORT_REPORT_SCHEMA_VERSION = "redacted-diagnostic-export-report.v1"
DEFAULT_GENERATED_AT = "runtime"
TEXT_SENTINEL_RE = re.compile(r"(?i)[A-Za-z0-9_.:-]*sentinel[A-Za-z0-9_.:-]*")
URL_RE = re.compile(r"https?://[^\s'\"<>),}]+")
PATH_RE = re.compile(
    r"(?i)(?:^|[\s'\"(])(?:/home/|/Users/|/tmp/|file://|~/|[A-Za-z]:\\\\)[^\s'\"()]+"
)
SECRET_RE = re.compile(
    r"(?i)(authorization:\s*bearer|api[_-]?key\s*=|secret\s*=|token\s*=|private key)"
)
GRAPH_NODE_TABLES = (
    "work",
    "authority_record",
    "source_access",
    "capture_event",
    "extraction_record",
    "extraction_detected_entity",
    "source_claim",
    "source_relationship",
)
REVIEW_TABLES = (
    "work",
    "authority_record",
    "source_access",
    "capture_event",
    "extraction_record",
    "extraction_detected_entity",
    "source_claim",
    "source_relationship",
    "authority_reconciliation",
    "review_state_history",
)
ARTIFACT_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".sqlite", ".db", ".csv", ".html"}
MAX_WORKSPACE_ARTIFACTS = 500


class DiagnosticExportError(RuntimeError):
    """Raised when a diagnostic export cannot be safely produced."""


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def count_by(conn: sqlite3.Connection, table_name: str, column: str) -> dict[str, int]:
    if not table_exists(conn, table_name) or column not in table_columns(conn, table_name):
        return {}
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM({column}), ''), '[blank]') AS value, COUNT(*) AS count
        FROM {table_name}
        GROUP BY value
        ORDER BY value
        """
    ).fetchall()
    return {str(row["value"]): int(row["count"]) for row in rows}


def count_table(conn: sqlite3.Connection, table_name: str) -> int:
    if not table_exists(conn, table_name):
        return 0
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    return int(row["count"])


class Redactor:
    def __init__(
        self,
        *,
        path_mode: str,
        url_mode: str,
        key: str | None,
        internal_full_fidelity: bool,
    ) -> None:
        self.path_mode = path_mode
        self.url_mode = url_mode
        self.key = key.encode("utf-8") if key is not None else os.urandom(32)
        self.key_supplied = key is not None
        self.internal_full_fidelity = internal_full_fidelity

    def fingerprint(self) -> str | None:
        if not self.key_supplied:
            return None
        return hashlib.sha256(self.key).hexdigest()[:16]

    def token(self, prefix: str, value: str) -> str:
        digest = hmac.new(self.key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        return f"{prefix}:{digest}"

    def redact_path(self, value: str | Path | None) -> str | None:
        if value is None:
            return None
        text = str(value)
        if self.internal_full_fidelity:
            return text
        if self.path_mode == "omit":
            return None
        if self.path_mode == "basename":
            return Path(text).name
        if self.path_mode == "hashed":
            return "path-sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        if self.path_mode == "hmac":
            return self.token("path-hmac", text)
        raise DiagnosticExportError(f"unsupported path redaction mode: {self.path_mode}")

    def redact_url(self, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        parsed = urlparse(text)
        if self.internal_full_fidelity and self.url_mode == "full":
            return text
        if self.url_mode == "omit":
            return None
        if self.url_mode == "domain_only":
            return parsed.netloc.lower() if parsed.netloc else "[non-url-locator]"
        if self.url_mode == "hmac":
            return self.token("url-hmac", text)
        if self.url_mode == "full":
            raise DiagnosticExportError("--url-redaction full requires --internal-full-fidelity")
        raise DiagnosticExportError(f"unsupported URL redaction mode: {self.url_mode}")

    def redact_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        text = strip_terminal_escapes(value)
        if self.internal_full_fidelity:
            return text
        text = TEXT_SENTINEL_RE.sub("[redacted-sentinel]", text)
        text = SECRET_RE.sub("[redacted-secret]", text)
        text = URL_RE.sub(lambda match: self.redact_url(match.group(0)) or "[redacted-url]", text)
        text = PATH_RE.sub(
            lambda match: " " + (self.redact_path(match.group(0).strip()) or "[redacted-path]"),
            text,
        )
        if local_doctor is not None:
            redacted = local_doctor.redact(text)
            return strip_terminal_escapes(str(redacted)) if redacted is not None else None
        return text

    def redact_json(self, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, nested in sorted(value.items()):
                key_text = str(key)
                normalized = key_text.lower()
                if any(
                    marker in normalized
                    for marker in (
                        "claim_text",
                        "reviewer_note",
                        "note_text",
                        "prompt",
                        "excerpt",
                        "payload",
                    )
                ):
                    result[key_text] = "[omitted]"
                elif normalized.endswith("path") or "path" in normalized:
                    result[key_text] = self.redact_path(str(nested)) if nested is not None else None
                elif "url" in normalized or "locator" in normalized or "uri" in normalized:
                    result[key_text] = self.redact_url(str(nested)) if nested is not None else None
                else:
                    result[key_text] = self.redact_json(nested)
            return result
        if isinstance(value, list):
            return [self.redact_json(item) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value


def db_metadata(db_path: Path, redactor: Redactor) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "db_path": redactor.redact_path(db_path),
        "db_hash_sha256": hash_file(db_path),
        "byte_count": db_path.stat().st_size,
        "schema_version": None,
        "current_migration_id": None,
    }
    try:
        conn = canonical_store.connect_existing_read_only(db_path)
    except canonical_store.CanonicalStoreError:
        return metadata
    try:
        if table_exists(conn, "schema_version"):
            row = conn.execute(
                "SELECT schema_version, current_migration_id FROM schema_version WHERE schema_namespace=?",
                (canonical_store.SCHEMA_NAMESPACE,),
            ).fetchone()
            if row is not None:
                metadata["schema_version"] = int(row["schema_version"])
                metadata["current_migration_id"] = row["current_migration_id"]
    finally:
        conn.close()
    return metadata


def build_canonical_summary(
    conn: sqlite3.Connection, db_path: Path, redactor: Redactor
) -> dict[str, Any]:
    tables = sorted(canonical_store.actual_tables(conn))
    table_counts = {table: count_table(conn, table) for table in tables}
    source_domains = Counter[str]()
    if table_exists(conn, "source_access"):
        for row in conn.execute(
            "SELECT original_locator, canonical_url FROM source_access ORDER BY source_access_id"
        ):
            locator = row["canonical_url"] or row["original_locator"]
            redacted = redactor.redact_url(str(locator)) if locator else None
            source_domains[redacted or "[omitted]"] += 1
    return {
        "schema_version": "redacted-diagnostic-canonical-summary.v1",
        "canonical_db": db_metadata(db_path, redactor),
        "table_count": len(tables),
        "table_row_counts": table_counts,
        "source_access_locator_counts": dict(sorted(source_domains.items())),
        "capture_counts": {
            "by_method": count_by(conn, "capture_event", "capture_method"),
            "by_mime_type": count_by(conn, "capture_event", "mime_type"),
            "by_review_state": count_by(conn, "capture_event", "review_state"),
        },
        "extraction_counts": {
            "by_status": count_by(conn, "extraction_record", "extraction_status"),
            "by_encoding_handling": count_by(conn, "extraction_record", "encoding_handling"),
            "by_truncation_status": count_by(conn, "extraction_record", "truncation_status"),
        },
        "detected_entity_counts": {
            "by_type": count_by(conn, "extraction_detected_entity", "entity_type"),
            "by_review_state": count_by(conn, "extraction_detected_entity", "review_state"),
        },
        "authority_reconciliation_counts": {
            "by_review_state": count_by(conn, "authority_reconciliation", "review_state"),
            "by_method": count_by(conn, "authority_reconciliation", "method"),
        },
        "provenance_counts": {
            "by_event_type": count_by(conn, "provenance_event", "event_type"),
            "by_tool_name": count_by(conn, "provenance_event", "tool_name"),
        },
        "cycle_counts": {
            "by_status": count_by(conn, "cycle_event", "status"),
            "by_mode": count_by(conn, "cycle_event", "mode"),
        },
        "content_policy": {
            "payload_bytes": "excluded",
            "complete_extracted_text": "excluded",
            "operator_notes": "excluded",
            "model_prompt_bodies": "excluded",
        },
    }


def build_review_state_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "schema_version": "redacted-diagnostic-review-state-summary.v1",
        "tables": {
            table: count_by(
                conn, table, "review_state" if table != "review_state_history" else "new_state"
            )
            for table in REVIEW_TABLES
            if table_exists(conn, table)
        },
        "history_count": count_table(conn, "review_state_history"),
    }


def build_relationship_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    contradiction_count = 0
    if table_exists(conn, "source_relationship"):
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM source_relationship WHERE predicate=?",
            ("contradicts",),
        ).fetchone()
        contradiction_count = int(row["count"])
    return {
        "schema_version": "redacted-diagnostic-relationship-summary.v1",
        "predicate_counts": count_by(conn, "source_relationship", "predicate"),
        "review_state_counts": count_by(conn, "source_relationship", "review_state"),
        "contradiction_count": contradiction_count,
    }


def build_source_access_summary(conn: sqlite3.Connection, redactor: Redactor) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if table_exists(conn, "source_access"):
        for row in conn.execute(
            """
            SELECT source_access_id, source_locus_id, source_lead_id, original_locator,
                   canonical_url, access_class, refetchability_status, rights_posture,
                   review_state, publication_state, public_blocker, workspace_id
            FROM source_access
            ORDER BY source_access_id
            """
        ):
            locator = str(row["canonical_url"] or row["original_locator"])
            rows.append(
                {
                    "source_access_id": row["source_access_id"],
                    "source_locus_present": bool(row["source_locus_id"]),
                    "source_lead_present": bool(row["source_lead_id"]),
                    "locator": redactor.redact_url(locator),
                    "access_class": row["access_class"],
                    "refetchability_status": row["refetchability_status"],
                    "rights_posture": row["rights_posture"],
                    "review_state": row["review_state"],
                    "publication_state": row["publication_state"],
                    "has_public_blocker": bool(row["public_blocker"]),
                    "workspace_id": row["workspace_id"],
                }
            )
    return {
        "schema_version": "redacted-diagnostic-source-access-summary.v1",
        "count": len(rows),
        "records": rows,
        "url_redaction": redactor.url_mode
        if not redactor.internal_full_fidelity
        else "internal_full_fidelity",
    }


def parse_ref_family(value: str | None) -> str | None:
    if not value or ":" not in value:
        return None
    return value.split(":", 1)[0]


def bucket_degree(value: int) -> str:
    if value == 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2-3"
    if value <= 10:
        return "4-10"
    return "11+"


def component_sizes(edges: Iterable[tuple[str, str]]) -> list[int]:
    graph: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        graph[left].add(right)
        graph[right].add(left)
    seen: set[str] = set()
    sizes: list[int] = []
    for node in sorted(graph):
        if node in seen:
            continue
        queue: deque[str] = deque([node])
        seen.add(node)
        size = 0
        while queue:
            current = queue.popleft()
            size += 1
            for neighbor in sorted(graph[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        sizes.append(size)
    return sorted(sizes, reverse=True)


def build_graph_shape(
    conn: sqlite3.Connection, graph_closure_report: Mapping[str, Any] | None
) -> dict[str, Any]:
    edges: list[tuple[str, str]] = []
    edge_predicates = Counter[str]()
    node_families = Counter[str]()
    for table in GRAPH_NODE_TABLES:
        if table_exists(conn, table):
            node_families[table] = count_table(conn, table)
    if table_exists(conn, "source_relationship"):
        for row in conn.execute(
            "SELECT from_object_ref, to_object_ref, predicate FROM source_relationship ORDER BY source_relationship_id"
        ):
            from_ref = str(row["from_object_ref"])
            to_ref = row["to_object_ref"]
            edge_predicates[str(row["predicate"])] += 1
            if isinstance(to_ref, str) and to_ref.strip():
                edges.append((from_ref, to_ref))
    degree_counts = Counter[str]()
    raw_degrees = Counter[str]()
    for left, right in edges:
        raw_degrees[left] += 1
        raw_degrees[right] += 1
    for degree in raw_degrees.values():
        degree_counts[bucket_degree(degree)] += 1
    component_summary = component_sizes(edges)
    raw_closure_summary = (
        graph_closure_report.get("summary") if isinstance(graph_closure_report, Mapping) else None
    )
    closure_summary: Mapping[str, Any] = (
        raw_closure_summary if isinstance(raw_closure_summary, Mapping) else {}
    )
    return {
        "schema_version": "redacted-diagnostic-graph-shape.v1",
        "node_counts_by_family": dict(sorted(node_families.items())),
        "edge_counts_by_predicate": dict(sorted(edge_predicates.items())),
        "edge_ref_family_counts": {
            "from": dict(
                sorted(Counter(filter(None, (parse_ref_family(left) for left, _ in edges))).items())
            ),
            "to": dict(
                sorted(
                    Counter(filter(None, (parse_ref_family(right) for _, right in edges))).items()
                )
            ),
        },
        "degree_distribution": dict(sorted(degree_counts.items())),
        "component_summary": {
            "component_count": len(component_summary),
            "largest_component_sizes": component_summary[:10],
        },
        "graph_closure": {
            "status": graph_closure_report.get("status")
            if isinstance(graph_closure_report, Mapping)
            else "not_run",
            "orphan_error_count": closure_summary.get("true_orphan_count", 0),
            "unresolved_tracked_count": closure_summary.get("unresolved_tracked_count", 0),
            "repairable_count": closure_summary.get("repairable_count", 0),
            "quarantined_count": closure_summary.get("quarantined_count", 0),
        },
    }


def build_graph_closure_summary(
    db_path: Path, generated_at: str, redactor: Redactor
) -> dict[str, Any]:
    try:
        report = canonical_graph_closure.audit_canonical_graph_closure(
            db_path,
            generated_at=generated_at,
        )
    except Exception as exc:
        return {
            "schema_version": "redacted-diagnostic-graph-closure-summary.v1",
            "status": "unavailable",
            "error_summary": redactor.redact_text(str(exc)),
        }
    report = redactor.redact_json(report)
    assert isinstance(report, dict)
    return report


def summarize_workspace_artifacts(workspace: Path | None, redactor: Redactor) -> dict[str, Any]:
    if workspace is None:
        return {
            "schema_version": "redacted-diagnostic-artifact-summary.v1",
            "workspace_present": False,
            "artifacts": [],
            "truncated": False,
        }
    artifacts: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*")):
        if len(artifacts) >= MAX_WORKSPACE_ARTIFACTS:
            break
        if not path.is_file() or path.suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
            digest = hash_file(path)
        except OSError:
            continue
        rel = path.relative_to(workspace).as_posix()
        artifacts.append(
            {
                "artifact_ref": redactor.redact_path(str(path)),
                "relative_artifact_ref": redactor.redact_path(rel),
                "basename": path.name,
                "suffix": path.suffix.lower(),
                "byte_count": size,
                "sha256": digest,
            }
        )
    return {
        "schema_version": "redacted-diagnostic-artifact-summary.v1",
        "workspace_present": True,
        "workspace_ref": redactor.redact_path(workspace),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "truncated": len(artifacts) >= MAX_WORKSPACE_ARTIFACTS,
    }


def load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def summarize_cycle_manifests(workspace: Path | None, redactor: Redactor) -> dict[str, Any]:
    if workspace is None:
        return {
            "schema_version": "redacted-diagnostic-cycle-summary.v1",
            "workspace_present": False,
            "cycle_manifests": [],
        }
    manifests: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*.json")):
        if path.name not in {"topic-cycle-manifest.json", "scheduled-topic-cycles-manifest.json"}:
            continue
        payload = load_json_object(path)
        if payload is None:
            continue
        stages = payload.get("stages")
        stage_counts: dict[str, int] = {}
        failed_stages: list[str] = []
        if isinstance(stages, list):
            counts = Counter[str]()
            for item in stages:
                if not isinstance(item, Mapping):
                    continue
                status = str(item.get("status") or "[blank]")
                counts[status] += 1
                if status in {"failed", "degraded"}:
                    failed_stages.append(
                        str(item.get("name") or item.get("stage_name") or "unknown")
                    )
            stage_counts = dict(sorted(counts.items()))
        manifests.append(
            {
                "manifest_ref": redactor.redact_path(str(path)),
                "manifest_hash": hash_file(path),
                "schema_version": payload.get("schema_version"),
                "run_id": payload.get("run_id"),
                "cycle_event_id": payload.get("cycle_event_id"),
                "status": payload.get("status"),
                "stage_status_counts": stage_counts,
                "failed_stages": sorted(failed_stages),
                "graph_closure": redactor.redact_json(payload.get("graph_closure")),
            }
        )
    return {
        "schema_version": "redacted-diagnostic-cycle-summary.v1",
        "workspace_present": True,
        "cycle_manifest_count": len(manifests),
        "cycle_manifests": manifests,
    }


def build_cycle_ledger_summary(conn: sqlite3.Connection, redactor: Redactor) -> dict[str, Any]:
    if not table_exists(conn, "cycle_event"):
        return {
            "schema_version": "redacted-diagnostic-cycle-ledger-summary.v1",
            "ledger_present": False,
        }
    artifacts: list[dict[str, Any]] = []
    if table_exists(conn, "cycle_artifact_ref"):
        for row in conn.execute(
            """
            SELECT artifact_type, artifact_path, artifact_hash, byte_count,
                   privacy_classification, public_safe, validation_status
            FROM cycle_artifact_ref
            ORDER BY artifact_type, artifact_hash, artifact_path
            LIMIT 100
            """
        ):
            artifacts.append(
                {
                    "artifact_type": row["artifact_type"],
                    "artifact_ref": redactor.redact_path(row["artifact_path"]),
                    "artifact_hash": row["artifact_hash"],
                    "byte_count": row["byte_count"],
                    "privacy_classification": row["privacy_classification"],
                    "public_safe": bool(row["public_safe"]),
                    "validation_status": row["validation_status"],
                }
            )
    return {
        "schema_version": "redacted-diagnostic-cycle-ledger-summary.v1",
        "ledger_present": True,
        "cycle_counts": count_by(conn, "cycle_event", "status"),
        "stage_counts": count_by(conn, "cycle_stage_event", "status"),
        "tool_failure_counts": count_by(conn, "cycle_tool_failure", "failure_kind"),
        "operator_override_counts": count_by(conn, "cycle_operator_override", "override_kind"),
        "candidate_considered_count": count_table(conn, "cycle_candidate_considered"),
        "candidate_excluded_count": count_table(conn, "cycle_candidate_excluded"),
        "artifact_refs": artifacts,
    }


def build_spool_summary(workspace: Path | None, redactor: Redactor) -> dict[str, Any]:
    if workspace is None:
        return {
            "schema_version": "redacted-diagnostic-spool-summary.v1",
            "workspace_present": False,
            "spool_records": [],
        }
    records: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*spool*.json")):
        payload = load_json_object(path)
        if payload is None:
            continue
        if payload.get("schema_version") != "canonical-write-spool-record.v1":
            continue
        records.append(
            {
                "spool_ref": redactor.redact_path(path),
                "spool_hash": hash_file(path),
                "operation_kind": payload.get("operation_kind"),
                "failure_kind": payload.get("failure_kind"),
                "retryable": payload.get("retryable"),
                "replay_status": payload.get("replay_status"),
                "privacy_classification": payload.get("privacy_classification"),
            }
        )
    return {
        "schema_version": "redacted-diagnostic-spool-summary.v1",
        "workspace_present": True,
        "spool_record_count": len(records),
        "spool_records": records,
    }


def build_local_doctor_summary(repo_root: Path, redactor: Redactor) -> dict[str, Any]:
    if local_doctor is None:
        return {
            "schema_version": "redacted-diagnostic-local-doctor-summary.v1",
            "status": "unavailable",
            "reason": "local_doctor import unavailable",
        }
    payload = local_doctor.build_report(repo_root)
    return {
        "schema_version": "redacted-diagnostic-local-doctor-summary.v1",
        "status": payload.get("overall_status"),
        "checks": redactor.redact_json(payload.get("checks")),
        "findings": redactor.redact_json(payload.get("findings")),
        "graph_closure": redactor.redact_json(payload.get("graph_closure")),
    }


def build_redaction_report(args: argparse.Namespace, redactor: Redactor) -> dict[str, Any]:
    return {
        "schema_version": REDACTION_REPORT_SCHEMA_VERSION,
        "redaction_mode": "internal_full_fidelity" if args.internal_full_fidelity else "redacted",
        "privacy_classification": "internal_private"
        if args.internal_full_fidelity
        else "local_operator_redacted",
        "path_redaction": redactor.path_mode,
        "url_redaction": redactor.url_mode,
        "redaction_key_fingerprint": redactor.fingerprint(),
        "redaction_key_written": False,
        "omitted_content_families": [
            "source text bodies",
            "complete extracted text",
            "operator notes",
            "model prompt bodies",
            "local filesystem paths",
            "secret-looking values",
        ],
        "content_hashes_included": True,
    }


def section_hashes(output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("*.json")):
        if path.name in {"diagnostic-manifest.json", "leak-scan-report.json"}:
            continue
        rows.append(
            {
                "path": path.name,
                "sha256": hash_file(path),
                "byte_count": path.stat().st_size,
            }
        )
    return rows


def build_manifest(
    *,
    args: argparse.Namespace,
    db_path: Path,
    output_dir: Path,
    generated_at: str,
    export_id: str,
    redactor: Redactor,
    included_sections: list[str],
    omitted_sections: list[dict[str, str]],
    leak_report: Mapping[str, Any] | None,
    warnings: list[str],
    errors: list[str],
) -> dict[str, Any]:
    file_hashes = section_hashes(output_dir)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "export_id": export_id,
        "read_only": True,
        "redaction_mode": "internal_full_fidelity" if args.internal_full_fidelity else "redacted",
        "privacy_classification": "internal_private"
        if args.internal_full_fidelity
        else "local_operator_redacted",
        "canonical_db": db_metadata(db_path, redactor),
        "scope": {
            "subject": args.subject,
            "workspace": redactor.redact_path(args.workspace) if args.workspace else None,
        },
        "redaction_policy": {
            "path_redaction": redactor.path_mode,
            "url_redaction": redactor.url_mode,
            "redaction_key_fingerprint": redactor.fingerprint(),
            "redaction_key_written": False,
        },
        "included_sections": sorted(included_sections),
        "omitted_sections": omitted_sections,
        "files": file_hashes,
        "leak_scan": {
            "status": leak_report.get("status") if leak_report else "not_run",
            "finding_count": leak_report.get("counts", {}).get("findings") if leak_report else None,
            "report_path": "leak-scan-report.json" if leak_report else None,
        },
        "warnings": warnings,
        "errors": errors,
    }


def compute_export_id(db_path: Path, generated_at: str, subject: str | None) -> str:
    db_hash = hash_file(db_path)
    digest = hashlib.sha256(f"{db_hash}\x1f{generated_at}\x1f{subject or ''}".encode()).hexdigest()[
        :24
    ]
    return f"redacted-diagnostics:{digest}"


def _contains_sqlite_file(path: Path) -> bool:
    return any(item.suffix.lower() == ".sqlite" for item in path.glob("**/*") if item.is_file())


def _is_recognized_redacted_diagnostic_bundle(path: Path) -> bool:
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("schema_version") == MANIFEST_SCHEMA_VERSION


def _is_canonical_workspace_root(path: Path) -> bool:
    return (
        (path / ".indexer" / "subject_manifest.json").is_file()
        or (
            (path / "source.txt").is_file()
            and (path / "state").is_dir()
            and (path / "runs").is_dir()
        )
    )


def _assert_diagnostic_output_target_safe(output_dir: Path, overwrite: bool) -> None:
    if output_dir == REPO_ROOT:
        raise DiagnosticExportError(f"refusing to write output to repository root: {output_dir}")
    if output_dir == Path.cwd().resolve():
        raise DiagnosticExportError(f"refusing to write output to current working directory: {output_dir}")
    if output_dir == Path.home().resolve():
        raise DiagnosticExportError(f"refusing to write output to home directory: {output_dir}")
    if ".git" in output_dir.parts:
        raise DiagnosticExportError(f"refusing to write output under .git path: {output_dir}")
    if "runtime" in output_dir.parts or "dbs" in output_dir.parts:
        raise DiagnosticExportError(f"refusing to write output into reserved workspace path: {output_dir}")
    if _is_canonical_workspace_root(output_dir):
        raise DiagnosticExportError(f"refusing to write output to canonical workspace root: {output_dir}")
    if _contains_sqlite_file(output_dir):
        raise DiagnosticExportError(f"refusing to overwrite path with sqlite artifacts: {output_dir}")


def run_leak_scan(output_dir: Path) -> dict[str, Any]:
    return scan_directory(output_dir, profile=DIAGNOSTIC_OUTPUT_PROFILE)


def export_bundle(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        raise DiagnosticExportError(f"canonical DB not found: {db_path}")
    output_dir = Path(args.output_dir).expanduser().resolve()
    _assert_diagnostic_output_target_safe(output_dir, args.overwrite)
    if output_dir.exists():
        if not output_dir.is_dir():
            raise DiagnosticExportError(f"output path is not a directory: {output_dir}")
        if not args.overwrite:
            raise DiagnosticExportError(f"output directory already exists: {output_dir}")
        if not _is_recognized_redacted_diagnostic_bundle(output_dir):
            raise DiagnosticExportError(
                f"output directory exists but is not a recognized redacted diagnostics bundle: {output_dir}"
            )
    if args.url_redaction == "full" and not args.internal_full_fidelity:
        raise DiagnosticExportError("--url-redaction full requires --internal-full-fidelity")

    generated_at = args.generated_at or now_rfc3339()
    export_id = args.export_id or compute_export_id(db_path, generated_at, args.subject)
    redactor = Redactor(
        path_mode=args.path_redaction,
        url_mode=args.url_redaction,
        key=args.redaction_key,
        internal_full_fidelity=bool(args.internal_full_fidelity),
    )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".tmp", dir=output_dir.parent))
    backup_root = None
    included: list[str] = []
    omitted: list[dict[str, str]] = []
    warnings: list[str] = []
    errors: list[str] = []
    leak_report: dict[str, Any] | None = None
    final_manifest: dict[str, Any] | None = None
    try:
        omitted = [
            {
                "section": "payload_bodies",
                "reason": "Source payload bodies are not part of redacted diagnostics.",
            },
            {"section": "complete_text", "reason": "Complete extracted text is omitted by default."},
            {"section": "operator_notes", "reason": "Private operator notes are omitted by default."},
            {"section": "model_prompt_bodies", "reason": "Prompt bodies are omitted by default."},
        ]

        conn = canonical_store.connect_existing_read_only(db_path)
        try:
            graph_closure_summary: dict[str, Any] | None = None
            if args.include_graph_closure:
                graph_closure_summary = build_graph_closure_summary(db_path, generated_at, redactor)
                write_json(stage_root / "graph-closure-summary.json", graph_closure_summary)
                included.append("graph-closure-summary.json")
            else:
                omitted.append({"section": "graph_closure", "reason": "Disabled by operator flag."})

            sections = {
                "canonical-summary.json": build_canonical_summary(conn, db_path, redactor),
                "graph-shape.json": build_graph_shape(conn, graph_closure_summary),
                "review-state-summary.json": build_review_state_summary(conn),
                "relationship-summary.json": build_relationship_summary(conn),
                "source-access-summary.json": build_source_access_summary(conn, redactor),
                "cycle-ledger-summary.json": build_cycle_ledger_summary(conn, redactor),
            }
            for name, payload in sections.items():
                write_json(stage_root / name, payload)
                included.append(name)
        finally:
            conn.close()

        workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
        write_json(
            stage_root / "artifact-summary.json", summarize_workspace_artifacts(workspace, redactor)
        )
        included.append("artifact-summary.json")
        if args.include_cycle_manifests:
            write_json(
                stage_root / "cycle-summary.json", summarize_cycle_manifests(workspace, redactor)
            )
            included.append("cycle-summary.json")
        else:
            omitted.append({"section": "cycle_manifests", "reason": "Disabled by operator flag."})
        write_json(stage_root / "spool-summary.json", build_spool_summary(workspace, redactor))
        included.append("spool-summary.json")

        if args.include_local_doctor_summary:
            write_json(
                stage_root / "local-doctor-summary.json",
                build_local_doctor_summary(REPO_ROOT, redactor),
            )
            included.append("local-doctor-summary.json")
        else:
            omitted.append({"section": "local_doctor", "reason": "Disabled by operator flag."})

        write_json(stage_root / "redaction-report.json", build_redaction_report(args, redactor))
        included.append("redaction-report.json")

        provisional_manifest = build_manifest(
            args=args,
            db_path=db_path,
            output_dir=stage_root,
            generated_at=generated_at,
            export_id=export_id,
            redactor=redactor,
            included_sections=included,
            omitted_sections=omitted,
            leak_report=None,
            warnings=warnings,
            errors=errors,
        )
        write_json(stage_root / "diagnostic-manifest.json", provisional_manifest)

        leak_report = run_leak_scan(stage_root)
        write_json(stage_root / "leak-scan-report.json", leak_report)
        final_manifest = build_manifest(
            args=args,
            db_path=db_path,
            output_dir=stage_root,
            generated_at=generated_at,
            export_id=export_id,
            redactor=redactor,
            included_sections=included,
            omitted_sections=omitted,
            leak_report=leak_report,
            warnings=warnings,
            errors=errors,
        )
        final_manifest["leak_scan"] = {
            "status": leak_report.get("status"),
            "finding_count": leak_report.get("counts", {}).get("findings"),
            "report_path": "leak-scan-report.json",
        }
        write_json(stage_root / "diagnostic-manifest.json", final_manifest)

        if leak_report.get("status") != "pass" and not args.internal_full_fidelity:
            findings = leak_report.get("findings") or []
            first = findings[0] if isinstance(findings, list) and findings else {}
            raise DiagnosticExportError(f"diagnostic leak scan failed: {first}")

        if output_dir.exists():
            backup_root = output_dir.parent / f".{output_dir.name}.backup.{uuid.uuid4().hex[:8]}"
            output_dir.replace(backup_root)
        stage_root.replace(output_dir)
        if backup_root is not None and backup_root.exists():
            shutil.rmtree(backup_root, ignore_errors=True)
    except Exception:
        if backup_root is not None and backup_root.exists() and not output_dir.exists():
            backup_root.replace(output_dir)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    return {
        "schema_version": EXPORT_REPORT_SCHEMA_VERSION,
        "status": "pass",
        "output_dir": str(output_dir),
        "manifest_path": str(output_dir / "diagnostic-manifest.json"),
        "leak_scan_status": "pass" if (leak_report or {}).get("status") == "pass" else "internal_with_leak_warnings",
        "included_section_count": len(included),
        "privacy_classification": final_manifest["privacy_classification"] if final_manifest else "local_operator_redacted",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Canonical SQLite DB path.")
    parser.add_argument(
        "--output-dir", required=True, help="Output directory for the JSON diagnostic bundle."
    )
    parser.add_argument(
        "--workspace", help="Optional workspace or run root to summarize structurally."
    )
    parser.add_argument("--subject", help="Optional subject/workspace scope label.")
    parser.add_argument(
        "--include-cycle-manifests", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--include-graph-closure", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--include-local-doctor-summary", action="store_true")
    parser.add_argument(
        "--internal-full-fidelity",
        action="store_true",
        help="Mark output internal/private and permit full URL/path modes.",
    )
    parser.add_argument(
        "--path-redaction", choices=("omit", "basename", "hmac", "hashed"), default="omit"
    )
    parser.add_argument(
        "--url-redaction", choices=("omit", "domain_only", "hmac", "full"), default="domain_only"
    )
    parser.add_argument("--redaction-key", help="Optional HMAC key; fingerprint only is recorded.")
    parser.add_argument("--generated-at", help="Fixed RFC3339 timestamp for deterministic tests.")
    parser.add_argument("--export-id", help="Optional explicit export id.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def render_text(report: Mapping[str, Any]) -> str:
    return "\n".join(
        f"{key}={format_operator_text_value(value)}" for key, value in report.items()
    ) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = export_bundle(args)
    except DiagnosticExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
