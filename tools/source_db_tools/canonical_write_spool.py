"""Filesystem spool for failed canonical writes.

Spool records are local recovery artifacts. They are not canonical rows and are
not treated as authoritative until replay validates them and applies the
recorded operation through the normal canonical APIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tools.source_db_tools import (
    canonical_ingest,
    canonical_store,
    cycle_evidence_ledger,
    review_decision_apply,
)

SCHEMA_VERSION = "canonical-write-spool-record.v1"
REPLAY_REPORT_SCHEMA_VERSION = "canonical-write-spool-replay-report.v1"
DEFAULT_PRIVACY_CLASSIFICATION = "local_operator_private"
ALLOWED_OPERATION_KINDS = {
    "candidate_batch_ingest",
    "execution_artifact_ingest",
    "review_decision_apply",
    "cycle_evidence_write",
}
REPLAY_STATUSES = {"pending", "replayed", "failed", "superseded", "skipped"}
FAILURE_KINDS = {
    "db_missing",
    "db_locked",
    "db_invalid",
    "schema_mismatch",
    "write_error",
    "transaction_rollback",
    "permission_denied",
    "unknown",
}


class CanonicalWriteSpoolError(RuntimeError):
    """Raised when a spool record cannot be written, validated, or replayed."""


def now_rfc3339() -> str:
    return canonical_store.now_rfc3339()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_checksum(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("spool_record_checksum", None)
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_spool_record_id(
    *,
    operation_kind: str,
    artifact_hash: str | None,
    run_id: str | None,
    stage_name: str | None,
    created_at: str,
) -> str:
    digest = hashlib.sha256(
        "\x1f".join(
            [operation_kind, artifact_hash or "", run_id or "", stage_name or "", created_at]
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"canonical-write-spool:{operation_kind}:{digest}"


def classify_failure(exc: BaseException) -> str:
    message = str(exc).casefold()
    if isinstance(exc, PermissionError) or "permission denied" in message:
        return "permission_denied"
    if "database is locked" in message or "database locked" in message:
        return "db_locked"
    if "does not exist" in message or "not found" in message:
        return "db_missing"
    if "schema" in message or "migration" in message:
        return "schema_mismatch"
    if "not a sqlite" in message or "file is not a database" in message or "unusable" in message:
        return "db_invalid"
    if isinstance(exc, sqlite3.Error):
        return "write_error"
    return "unknown"


def _artifact_hashes(operation_input: Mapping[str, Any]) -> list[str]:
    refs = operation_input.get("artifact_refs")
    hashes: list[str] = []
    if isinstance(refs, list):
        for item in refs:
            if isinstance(item, Mapping) and isinstance(item.get("artifact_hash"), str):
                hashes.append(str(item["artifact_hash"]))
    artifact_hash = operation_input.get("artifact_hash")
    if isinstance(artifact_hash, str):
        hashes.append(artifact_hash)
    return sorted(set(hashes))


def build_spool_record(
    *,
    operation_kind: str,
    operation_input: Mapping[str, Any],
    replay_recipe: Mapping[str, Any],
    failure: BaseException | str,
    canonical_db_path: Path,
    spool_dir: Path,
    originating_tool: str,
    originating_command: str | None = None,
    originating_run_id: str | None = None,
    topic_cycle_id: str | None = None,
    stage_name: str | None = None,
    workspace_id: str | None = None,
    subject_id: str | None = None,
    expected_schema_version: int | None = None,
    created_at: str | None = None,
    retryable: bool = True,
) -> dict[str, Any]:
    if operation_kind not in ALLOWED_OPERATION_KINDS:
        raise CanonicalWriteSpoolError(f"unsupported spool operation kind: {operation_kind}")
    created = created_at or now_rfc3339()
    hashes = _artifact_hashes(operation_input)
    record_id = build_spool_record_id(
        operation_kind=operation_kind,
        artifact_hash=hashes[0] if hashes else None,
        run_id=originating_run_id,
        stage_name=stage_name,
        created_at=created,
    )
    failure_message = str(failure)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "spool_record_id": record_id,
        "created_at": created,
        "originating_tool": originating_tool,
        "originating_command": originating_command,
        "originating_run_id": originating_run_id,
        "topic_cycle_id": topic_cycle_id,
        "stage_name": stage_name,
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "canonical_db": {
            "path": str(canonical_db_path),
            "expected_schema_version": expected_schema_version,
        },
        "operation_kind": operation_kind,
        "operation_input": dict(operation_input),
        "replay_recipe": dict(replay_recipe),
        "validation_status": "validated",
        "failure_kind": classify_failure(failure)
        if isinstance(failure, BaseException)
        else "unknown",
        "failure_message": failure_message,
        "retryable": bool(retryable),
        "replay_status": "pending",
        "replayed_at": None,
        "replay_result_refs": None,
        "privacy_classification": DEFAULT_PRIVACY_CLASSIFICATION,
        "raw_payload_policy": "artifact_references_only",
        "spool_path": None,
    }
    record["spool_record_checksum"] = record_checksum(record)
    record["spool_path"] = str(spool_record_path(spool_dir, record))
    record["spool_record_checksum"] = record_checksum(record)
    validate_spool_record(record)
    return record


def spool_record_path(spool_dir: Path, record: Mapping[str, Any]) -> Path:
    run_id = record.get("originating_run_id")
    safe_run = (
        str(run_id).replace("/", "_") if isinstance(run_id, str) and run_id else "unknown-run"
    )
    record_id = str(record["spool_record_id"]).replace("/", "_").replace(":", "_")
    return spool_dir / "canonical-unavailable" / safe_run / f"{record_id}.json"


def write_spool_record(spool_dir: Path, record: Mapping[str, Any]) -> Path:
    path = spool_record_path(spool_dir, record)
    payload = dict(record)
    payload["spool_path"] = str(path)
    payload["spool_record_checksum"] = record_checksum(payload)
    validate_spool_record(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    return path


def load_spool_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalWriteSpoolError(f"spool record is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CanonicalWriteSpoolError(f"spool record must be a JSON object: {path}")
    validate_spool_record(payload)
    stored_spool_path = payload.get("spool_path")
    if not isinstance(stored_spool_path, str):
        raise CanonicalWriteSpoolError(f"spool record missing spool_path: {path}")
    if Path(stored_spool_path).resolve() != path.resolve():
        raise CanonicalWriteSpoolError(
            f"spool record path mismatch: stored={stored_spool_path} actual={path}"
        )
    return payload


def iter_spool_records(path: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    if path.is_file():
        yield path, load_spool_record(path)
        return
    if not path.exists():
        raise CanonicalWriteSpoolError(f"spool path does not exist: {path}")
    for record_path in sorted(path.rglob("*.json")):
        yield record_path, load_spool_record(record_path)


def validate_spool_record(record: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "spool_record_id",
        "created_at",
        "originating_tool",
        "canonical_db",
        "operation_kind",
        "operation_input",
        "replay_recipe",
        "validation_status",
        "failure_kind",
        "failure_message",
        "retryable",
        "replay_status",
        "privacy_classification",
        "raw_payload_policy",
        "spool_record_checksum",
    }
    missing = sorted(required - set(record))
    if missing:
        raise CanonicalWriteSpoolError(
            "missing required spool record fields: " + ", ".join(missing)
        )
    if record["schema_version"] != SCHEMA_VERSION:
        raise CanonicalWriteSpoolError(
            f"unsupported spool record schema: {record['schema_version']}"
        )
    if record["operation_kind"] not in ALLOWED_OPERATION_KINDS:
        raise CanonicalWriteSpoolError(f"unsupported operation kind: {record['operation_kind']}")
    if record["replay_status"] not in REPLAY_STATUSES:
        raise CanonicalWriteSpoolError(f"invalid replay status: {record['replay_status']}")
    if record["failure_kind"] not in FAILURE_KINDS:
        raise CanonicalWriteSpoolError(f"invalid failure kind: {record['failure_kind']}")
    if not isinstance(record["retryable"], bool):
        raise CanonicalWriteSpoolError("retryable must be boolean")
    if record.get("raw_payload_policy") != "artifact_references_only":
        raise CanonicalWriteSpoolError("spool records must not embed raw payloads by default")
    operation_input = record["operation_input"]
    if not isinstance(operation_input, Mapping):
        raise CanonicalWriteSpoolError("operation_input must be an object")
    refs = operation_input.get("artifact_refs", [])
    if refs is not None:
        if not isinstance(refs, list):
            raise CanonicalWriteSpoolError("operation_input.artifact_refs must be an array")
        for ref in refs:
            if not isinstance(ref, Mapping):
                raise CanonicalWriteSpoolError("artifact refs must be objects")
            if ref.get("artifact_path") and not ref.get("artifact_hash"):
                raise CanonicalWriteSpoolError("artifact refs with paths require artifact_hash")
    expected = record_checksum(record)
    if record.get("spool_record_checksum") != expected:
        raise CanonicalWriteSpoolError("spool_record_checksum mismatch")


def mark_spool_record_replayed(
    path: Path,
    record: Mapping[str, Any],
    *,
    replayed_at: str,
    replay_result_refs: Mapping[str, Any],
) -> Path:
    payload = dict(record)
    payload["replay_status"] = "replayed"
    payload["replayed_at"] = replayed_at
    payload["replay_result_refs"] = dict(replay_result_refs)
    payload["spool_record_checksum"] = record_checksum(payload)
    return write_spool_record(path.parent.parent.parent, payload)


def mark_spool_record_failed(
    path: Path,
    record: Mapping[str, Any],
    *,
    failure_message: str,
    replayed_at: str,
) -> Path:
    payload = dict(record)
    payload["replay_status"] = "failed"
    payload["replayed_at"] = replayed_at
    payload["replay_result_refs"] = {"failure_message": failure_message}
    payload["spool_record_checksum"] = record_checksum(payload)
    return write_spool_record(path.parent.parent.parent, payload)


def artifact_ref(path: Path, *, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "artifact_path": str(path),
        "artifact_hash": hash_file(path),
    }


def replay_spool_record(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    db_path: Path,
    dry_run: bool,
    strict: bool = True,
) -> dict[str, Any]:
    validate_spool_record(record)
    check = canonical_store.check_canonical_store(db_path)
    expected_schema = record.get("canonical_db", {}).get("expected_schema_version")
    if expected_schema is not None and int(expected_schema) > int(check.schema_version):
        raise CanonicalWriteSpoolError(
            f"spool expects schema_version {expected_schema}, target has {check.schema_version}"
        )
    operation = str(record["operation_kind"])
    recipe = record["replay_recipe"]
    if not isinstance(recipe, Mapping):
        raise CanonicalWriteSpoolError("replay_recipe must be an object")
    if operation == "candidate_batch_ingest":
        batch_path = Path(str(recipe["batch_path"]))
        batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)
        expected_hash = recipe.get("batch_hash")
        if expected_hash and expected_hash != batch_hash:
            raise CanonicalWriteSpoolError("candidate batch hash mismatch")
        return canonical_ingest.ingest_candidate_batch(
            conn,
            batch,
            batch_path=batch_path,
            batch_hash=batch_hash,
            dry_run=dry_run,
            strict=strict,
            db_path=db_path,
        )
    if operation == "execution_artifact_ingest":
        run_dir = Path(str(recipe["run_dir"]))
        execution_record, capture_events, extraction_records, paths, input_hashes = (
            canonical_ingest.load_validated_execution_artifacts(run_dir)
        )
        expected_hashes = recipe.get("input_hashes")
        if isinstance(expected_hashes, Mapping):
            for key, value in expected_hashes.items():
                if input_hashes.get(str(key)) != value:
                    raise CanonicalWriteSpoolError(f"execution artifact hash mismatch: {key}")
        return canonical_ingest.ingest_execution_artifacts(
            conn,
            execution_record,
            capture_events,
            extraction_records,
            paths=paths,
            input_hashes=input_hashes,
            dry_run=dry_run,
            strict=strict,
            db_path=db_path,
        )
    if operation == "review_decision_apply":
        return review_decision_apply.apply_review_decision(
            conn,
            target=str(recipe["target"]),
            decision_action=str(recipe["decision"]),
            reviewer=str(recipe["reviewer"]),
            reason=str(recipe["reason"]),
            expected_state=recipe.get("expected_state")
            if isinstance(recipe.get("expected_state"), str)
            else None,
            dry_run=dry_run,
            decided_at=recipe.get("decided_at")
            if isinstance(recipe.get("decided_at"), str)
            else None,
            run_id=recipe.get("run_id") if isinstance(recipe.get("run_id"), str) else None,
        )
    if operation == "cycle_evidence_write":
        manifest_path = Path(str(recipe["manifest_path"]))
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_payload, dict):
            raise CanonicalWriteSpoolError("cycle manifest must be a JSON object")
        if dry_run:
            return {
                "schema_version": "cycle-evidence-replay-dry-run.v1",
                "status": "dry_run",
                "manifest_path": str(manifest_path),
            }
        cycle_id = cycle_evidence_ledger.record_topic_cycle_manifest(
            conn,
            manifest=manifest_payload,
            manifest_path=manifest_path,
            manifest_hash=recipe.get("manifest_hash")
            if isinstance(recipe.get("manifest_hash"), str)
            else None,
            canonical_db_ref=str(db_path),
        )
        return {"schema_version": "cycle-evidence-replay-result.v1", "cycle_event_id": cycle_id}
    raise CanonicalWriteSpoolError(f"unsupported operation kind: {operation}")


def summarize_spool(path: Path) -> dict[str, Any]:
    counts = {status: 0 for status in sorted(REPLAY_STATUSES)}
    by_kind: dict[str, int] = {}
    total = 0
    for _record_path, record in iter_spool_records(path):
        total += 1
        counts[str(record["replay_status"])] += 1
        kind = str(record["operation_kind"])
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"total": total, "by_status": counts, "by_operation_kind": by_kind}
