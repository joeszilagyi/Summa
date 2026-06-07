"""Queryable operational evidence ledger for bounded topic cycles."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.common.candidate_feedback_contract import deferred_candidate_retryable

SCHEMA_VERSION = "cycle-evidence-ledger.v1"
DEFAULT_PRIVACY_CLASSIFICATION = "local_operator"
_ID_PREFIXES = {
    "cycle": "cycle",
    "stage": "cycle-stage",
    "artifact": "cycle-artifact",
    "considered": "cycle-considered",
    "excluded": "cycle-excluded",
    "failure": "cycle-failure",
    "override": "cycle-override",
}


class CycleEvidenceLedgerError(RuntimeError):
    """Raised when cycle evidence cannot be recorded or loaded safely."""


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(kind: str, *parts: object) -> str:
    """Return a deterministic local ledger id for append-friendly idempotence."""

    prefix = _ID_PREFIXES.get(kind, kind)
    seed = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def build_cycle_event_id(
    *,
    run_id: str,
    started_at: str | None = None,
    workspace_ref: str | None = None,
) -> str:
    del started_at
    del workspace_ref
    return stable_id("cycle", run_id)


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_mapping(value: Mapping[str, object] | None) -> str:
    return json_dumps(dict(value or {}))


def _json_sequence(value: Sequence[object] | None) -> str:
    return json_dumps(list(value or []))


def _require_nonblank(value: object, field_name: str) -> str:
    if value is None:
        raise CycleEvidenceLedgerError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise CycleEvidenceLedgerError(f"{field_name} is required")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_int(value: bool) -> int:
    return 1 if value else 0


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _assert_append_only_replay_compatible(
    table: str,
    key: str,
    row: sqlite3.Row | None,
    expected: dict[str, Any],
    *,
    ignore: frozenset[str] = frozenset(),
) -> None:
    if row is None:
        raise CycleEvidenceLedgerError(f"ledger replay conflict for {table} {key}: existing row not found")
    mismatches = [
        field
        for field, value in expected.items()
        if field not in ignore and row[field] != value
    ]
    if mismatches:
        field_list = ", ".join(mismatches)
        raise CycleEvidenceLedgerError(
            f"ledger replay mismatch for {table} {key}: {field_list}"
        )


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _file_hash(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def record_cycle_event_start(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    workspace_id: str | None = None,
    workspace_ref: str | None = None,
    subject_key: str | None = None,
    domain_pack_id: str | None = None,
    cycle_depth: int | None = None,
    previous_run_ids: Sequence[object] | None = None,
    mode: str | None = None,
    started_at: str | None = None,
    status: str = "running",
    topic_cycle_manifest_path: str | None = None,
    topic_cycle_manifest_hash: str | None = None,
    canonical_db_ref: str | None = None,
    final_feedback_plan_ref: str | None = None,
    row_count_delta: Mapping[str, object] | None = None,
    warning_count: int = 0,
    error_count: int = 0,
    metadata: Mapping[str, object] | None = None,
    cycle_event_id: str | None = None,
) -> str:
    run_id_text = _require_nonblank(run_id, "run_id")
    started = started_at or now_rfc3339()
    event_id = cycle_event_id or build_cycle_event_id(
        run_id=run_id_text, started_at=started, workspace_ref=workspace_ref
    )
    now = now_rfc3339()
    expected = {
        "cycle_event_id": event_id,
        "run_id": run_id_text,
        "workspace_id": workspace_id,
        "workspace_ref": workspace_ref,
        "subject_key": subject_key,
        "domain_pack_id": domain_pack_id,
        "cycle_depth": cycle_depth,
        "previous_run_ids_json": _json_sequence(previous_run_ids),
        "mode": mode,
        "started_at": started,
        "status": _require_nonblank(status, "status"),
        "topic_cycle_manifest_path": topic_cycle_manifest_path,
        "topic_cycle_manifest_hash": topic_cycle_manifest_hash,
        "canonical_db_ref": canonical_db_ref,
        "final_feedback_plan_ref": final_feedback_plan_ref,
        "row_count_delta_json": _json_mapping(row_count_delta),
        "warning_count": int(warning_count),
        "error_count": int(error_count),
        "metadata_json": _json_mapping(metadata),
        "record_last_updated": now,
    }
    cursor = conn.execute(
        """
        INSERT INTO cycle_event (
          cycle_event_id, run_id, workspace_id, workspace_ref, subject_key, domain_pack_id,
          cycle_depth, previous_run_ids_json, mode, started_at, status,
          topic_cycle_manifest_path, topic_cycle_manifest_hash, canonical_db_ref,
          final_feedback_plan_ref, row_count_delta_json, warning_count, error_count,
          metadata_json, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO NOTHING
        RETURNING cycle_event_id
        """,
        tuple(expected.values()),
    )
    row = cursor.fetchone()
    if row is None:
        existing_row = conn.execute(
            "SELECT * FROM cycle_event WHERE run_id=?",
            (run_id_text,),
        ).fetchone()
        _assert_append_only_replay_compatible(
            "cycle_event",
            f"run_id={run_id_text}",
            existing_row,
            expected,
            ignore=frozenset({"record_last_updated", "status"}),
        )
        return str(existing_row["cycle_event_id"])
    return str(row[0])


def record_cycle_event_finish(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    status: str,
    ended_at: str | None = None,
    topic_cycle_manifest_path: str | None = None,
    topic_cycle_manifest_hash: str | None = None,
    final_feedback_plan_ref: str | None = None,
    row_count_delta: Mapping[str, object] | None = None,
    warning_count: int | None = None,
    error_count: int | None = None,
) -> None:
    now = now_rfc3339()
    cursor = conn.execute(
        """
        UPDATE cycle_event
        SET status=?,
            ended_at=?,
            topic_cycle_manifest_path=COALESCE(?, topic_cycle_manifest_path),
            topic_cycle_manifest_hash=COALESCE(?, topic_cycle_manifest_hash),
            final_feedback_plan_ref=COALESCE(?, final_feedback_plan_ref),
            row_count_delta_json=COALESCE(?, row_count_delta_json),
            warning_count=COALESCE(?, warning_count),
            error_count=COALESCE(?, error_count),
            record_last_updated=?
        WHERE cycle_event_id=?
        """,
        (
            _require_nonblank(status, "status"),
            ended_at or now,
            topic_cycle_manifest_path,
            topic_cycle_manifest_hash,
            final_feedback_plan_ref,
            None if row_count_delta is None else _json_mapping(row_count_delta),
            warning_count,
            error_count,
            now,
            _require_nonblank(cycle_event_id, "cycle_event_id"),
        ),
    )
    if cursor.rowcount != 1:
        raise CycleEvidenceLedgerError(
            f"cycle_event finish target not found: cycle_event_id={cycle_event_id}"
        )


def record_cycle_stage_start(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    run_id: str,
    stage_name: str,
    stage_order: int,
    started_at: str | None = None,
    status: str = "running",
    required_stage: bool = True,
    skipped_reason: str | None = None,
    command_name: str | None = None,
    helper_name: str | None = None,
    input_artifact_ref_id: str | None = None,
    output_artifact_ref_id: str | None = None,
    validation_status: str | None = None,
    error_summary: str | None = None,
    metadata: Mapping[str, object] | None = None,
    stage_event_id: str | None = None,
) -> str:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    stage = _require_nonblank(stage_name, "stage_name")
    run_id_text = _require_nonblank(run_id, "run_id")
    stage_id = stage_event_id or stable_id("stage", event_id, stage_order, stage)
    created = started_at or now_rfc3339()
    now = now_rfc3339()
    expected = {
        "stage_event_id": stage_id,
        "cycle_event_id": event_id,
        "run_id": run_id_text,
        "stage_name": stage,
        "stage_order": int(stage_order),
        "started_at": created,
        "status": _require_nonblank(status, "status"),
        "required_stage": _bool_int(required_stage),
        "skipped_reason": skipped_reason,
        "command_name": command_name,
        "helper_name": helper_name,
        "input_artifact_ref_id": input_artifact_ref_id,
        "output_artifact_ref_id": output_artifact_ref_id,
        "validation_status": validation_status,
        "error_summary": error_summary,
        "metadata_json": _json_mapping(metadata),
        "created_at": created,
        "record_last_updated": now,
    }
    cursor = conn.execute(
        """
        INSERT INTO cycle_stage_event (
          stage_event_id, cycle_event_id, run_id, stage_name, stage_order, started_at,
          status, required_stage, skipped_reason, command_name, helper_name,
          input_artifact_ref_id, output_artifact_ref_id, validation_status,
          error_summary, metadata_json, created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stage_event_id) DO NOTHING
        """,
        tuple(expected.values()),
    )
    if cursor.rowcount == 0:
        existing_row = conn.execute(
            "SELECT * FROM cycle_stage_event WHERE stage_event_id=?",
            (stage_id,),
        ).fetchone()
        _assert_append_only_replay_compatible(
            "cycle_stage_event",
            f"stage_event_id={stage_id}",
            existing_row,
            expected,
            ignore=frozenset({"created_at", "record_last_updated"}),
        )
    return stage_id


def record_cycle_stage_finish(
    conn: sqlite3.Connection,
    *,
    stage_event_id: str,
    status: str,
    ended_at: str | None = None,
    validation_status: str | None = None,
    error_summary: str | None = None,
) -> None:
    now = now_rfc3339()
    cursor = conn.execute(
        """
        UPDATE cycle_stage_event
        SET status=?,
            ended_at=?,
            validation_status=COALESCE(?, validation_status),
            error_summary=COALESCE(?, error_summary),
            record_last_updated=?
        WHERE stage_event_id=?
        """,
        (
            _require_nonblank(status, "status"),
            ended_at or now,
            validation_status,
            error_summary,
            now,
            _require_nonblank(stage_event_id, "stage_event_id"),
        ),
    )
    if cursor.rowcount != 1:
        raise CycleEvidenceLedgerError(
            f"cycle_stage_event finish target not found: stage_event_id={stage_event_id}"
        )


def record_cycle_artifact_ref(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    artifact_type: str,
    artifact_path: str,
    stage_event_id: str | None = None,
    artifact_hash: str | None = None,
    byte_count: int | None = None,
    privacy_classification: str = DEFAULT_PRIVACY_CLASSIFICATION,
    public_safe: bool = False,
    schema_id: str | None = None,
    validation_status: str | None = None,
    created_at: str | None = None,
    metadata: Mapping[str, object] | None = None,
    artifact_ref_id: str | None = None,
) -> str:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    artifact_type_text = _require_nonblank(artifact_type, "artifact_type")
    artifact_path_text = _require_nonblank(artifact_path, "artifact_path")
    artifact_id = artifact_ref_id or stable_id(
        "artifact", event_id, stage_event_id, artifact_type_text, artifact_path_text
    )
    now = now_rfc3339()
    created = created_at or now
    expected = {
        "artifact_ref_id": artifact_id,
        "cycle_event_id": event_id,
        "stage_event_id": stage_event_id,
        "artifact_type": artifact_type_text,
        "artifact_path": artifact_path_text,
        "artifact_hash": artifact_hash,
        "byte_count": byte_count,
        "privacy_classification": _require_nonblank(privacy_classification, "privacy_classification"),
        "public_safe": _bool_int(public_safe),
        "schema_id": schema_id,
        "validation_status": validation_status,
        "created_at": created,
        "metadata_json": _json_mapping(metadata),
        "record_last_updated": now,
    }
    conn.execute(
        """
        INSERT INTO cycle_artifact_ref (
          artifact_ref_id, cycle_event_id, stage_event_id, artifact_type, artifact_path,
          artifact_hash, byte_count, privacy_classification, public_safe, schema_id,
          validation_status, created_at, metadata_json, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_ref_id) DO NOTHING
        """,
        tuple(expected.values()),
    )
    cursor = conn.execute(
        "SELECT * FROM cycle_artifact_ref WHERE artifact_ref_id=?",
        (artifact_id,),
    )
    existing_row = cursor.fetchone()
    if existing_row is not None:
        _assert_append_only_replay_compatible(
            "cycle_artifact_ref",
            f"artifact_ref_id={artifact_id}",
            existing_row,
            expected,
            ignore=frozenset({"created_at", "record_last_updated"}),
        )
    return artifact_id


def record_cycle_candidate_considered(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    candidate_kind: str,
    stage_event_id: str | None = None,
    candidate_ref_type: str | None = None,
    candidate_ref_id: str | None = None,
    candidate_label: str | None = None,
    score: float | int | None = None,
    score_policy_id: str | None = None,
    rationale: str | None = None,
    reason: Mapping[str, object] | None = None,
    selected: bool = False,
    created_at: str | None = None,
    candidate_considered_id: str | None = None,
) -> str:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    kind = _require_nonblank(candidate_kind, "candidate_kind")
    candidate_id = candidate_considered_id or stable_id(
        "considered", event_id, stage_event_id, kind, candidate_ref_type, candidate_ref_id
    )
    now = now_rfc3339()
    created = created_at or now
    normalized_score = None if score is None else float(score)
    expected = {
        "candidate_considered_id": candidate_id,
        "cycle_event_id": event_id,
        "stage_event_id": stage_event_id,
        "candidate_kind": kind,
        "candidate_ref_type": candidate_ref_type,
        "candidate_ref_id": candidate_ref_id,
        "candidate_label": candidate_label,
        "score": normalized_score,
        "score_policy_id": score_policy_id,
        "rationale": rationale,
        "reason_json": _json_mapping(reason),
        "selected": _bool_int(selected),
        "created_at": created,
        "record_last_updated": now,
    }
    conn.execute(
        """
        INSERT INTO cycle_candidate_considered (
          candidate_considered_id, cycle_event_id, stage_event_id, candidate_kind,
          candidate_ref_type, candidate_ref_id, candidate_label, score, score_policy_id,
          rationale, reason_json, selected, created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_considered_id) DO NOTHING
        """,
        tuple(expected.values()),
    )
    existing_row = conn.execute(
        "SELECT * FROM cycle_candidate_considered WHERE candidate_considered_id=?",
        (candidate_id,),
    ).fetchone()
    _assert_append_only_replay_compatible(
        "cycle_candidate_considered",
        f"candidate_considered_id={candidate_id}",
        existing_row,
        expected,
        ignore=frozenset({"created_at", "record_last_updated"}),
    )
    return candidate_id


def record_cycle_candidate_excluded(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    candidate_kind: str,
    exclusion_reason: str,
    stage_event_id: str | None = None,
    candidate_ref_type: str | None = None,
    candidate_ref_id: str | None = None,
    candidate_label: str | None = None,
    policy_id: str | None = None,
    retryable: bool = False,
    created_at: str | None = None,
    candidate_excluded_id: str | None = None,
) -> str:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    kind = _require_nonblank(candidate_kind, "candidate_kind")
    reason_text = _require_nonblank(exclusion_reason, "exclusion_reason")
    excluded_id = candidate_excluded_id or stable_id(
        "excluded",
        event_id,
        stage_event_id,
        kind,
        candidate_ref_type,
        candidate_ref_id,
        reason_text,
    )
    now = now_rfc3339()
    created = created_at or now
    expected = {
        "candidate_excluded_id": excluded_id,
        "cycle_event_id": event_id,
        "stage_event_id": stage_event_id,
        "candidate_kind": kind,
        "candidate_ref_type": candidate_ref_type,
        "candidate_ref_id": candidate_ref_id,
        "candidate_label": candidate_label,
        "exclusion_reason": reason_text,
        "policy_id": policy_id,
        "retryable": _bool_int(retryable),
        "created_at": created,
        "record_last_updated": now,
    }
    conn.execute(
        """
        INSERT INTO cycle_candidate_excluded (
          candidate_excluded_id, cycle_event_id, stage_event_id, candidate_kind,
          candidate_ref_type, candidate_ref_id, candidate_label, exclusion_reason,
          policy_id, retryable, created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_excluded_id) DO NOTHING
        """,
        tuple(expected.values()),
    )
    existing_row = conn.execute(
        "SELECT * FROM cycle_candidate_excluded WHERE candidate_excluded_id=?",
        (excluded_id,),
    ).fetchone()
    _assert_append_only_replay_compatible(
        "cycle_candidate_excluded",
        f"candidate_excluded_id={excluded_id}",
        existing_row,
        expected,
        ignore=frozenset({"created_at", "record_last_updated"}),
    )
    return excluded_id


def record_cycle_tool_failure(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    tool_name: str,
    failure_kind: str,
    error_summary: str,
    stage_event_id: str | None = None,
    command_name: str | None = None,
    exit_code: int | None = None,
    artifact_ref_id: str | None = None,
    retryable: bool = False,
    created_at: str | None = None,
    tool_failure_id: str | None = None,
) -> str:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    tool = _require_nonblank(tool_name, "tool_name")
    kind = _require_nonblank(failure_kind, "failure_kind")
    summary = _require_nonblank(error_summary, "error_summary")
    failure_id = tool_failure_id or stable_id(
        "failure", event_id, stage_event_id, tool, kind, summary
    )
    now = now_rfc3339()
    created = created_at or now
    expected = {
        "tool_failure_id": failure_id,
        "cycle_event_id": event_id,
        "stage_event_id": stage_event_id,
        "tool_name": tool,
        "command_name": command_name,
        "exit_code": exit_code,
        "failure_kind": kind,
        "error_summary": summary,
        "artifact_ref_id": artifact_ref_id,
        "retryable": _bool_int(retryable),
        "created_at": created,
        "record_last_updated": now,
    }
    conn.execute(
        """
        INSERT INTO cycle_tool_failure (
          tool_failure_id, cycle_event_id, stage_event_id, tool_name, command_name,
          exit_code, failure_kind, error_summary, artifact_ref_id, retryable,
          created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tool_failure_id) DO NOTHING
        """,
        tuple(expected.values()),
    )
    existing_row = conn.execute(
        "SELECT * FROM cycle_tool_failure WHERE tool_failure_id=?",
        (failure_id,),
    ).fetchone()
    _assert_append_only_replay_compatible(
        "cycle_tool_failure",
        f"tool_failure_id={failure_id}",
        existing_row,
        expected,
        ignore=frozenset({"created_at", "record_last_updated"}),
    )
    return failure_id


def record_cycle_operator_override(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    override_kind: str,
    override_value: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
    stage_event_id: str | None = None,
    created_at: str | None = None,
    operator_override_id: str | None = None,
) -> str:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    kind = _require_nonblank(override_kind, "override_kind")
    override_id = operator_override_id or stable_id(
        "override", event_id, stage_event_id, kind, override_value
    )
    now = now_rfc3339()
    created = created_at or now
    expected = {
        "operator_override_id": override_id,
        "cycle_event_id": event_id,
        "stage_event_id": stage_event_id,
        "override_kind": kind,
        "override_value": override_value,
        "reason": reason,
        "actor": actor,
        "created_at": created,
        "record_last_updated": now,
    }
    conn.execute(
        """
        INSERT INTO cycle_operator_override (
          operator_override_id, cycle_event_id, stage_event_id, override_kind,
          override_value, reason, actor, created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(operator_override_id) DO NOTHING
        """,
        tuple(expected.values()),
    )
    existing_row = conn.execute(
        "SELECT * FROM cycle_operator_override WHERE operator_override_id=?",
        (override_id,),
    ).fetchone()
    _assert_append_only_replay_compatible(
        "cycle_operator_override",
        f"operator_override_id={override_id}",
        existing_row,
        expected,
        ignore=frozenset({"created_at", "record_last_updated"}),
    )
    return override_id


def load_cycle_event(conn: sqlite3.Connection, cycle_event_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM cycle_event WHERE cycle_event_id=?",
        (_require_nonblank(cycle_event_id, "cycle_event_id"),),
    ).fetchone()
    return None if row is None else _row_to_dict(row)


def list_cycle_events_for_subject(
    conn: sqlite3.Connection, subject_key: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    sql = """
        SELECT * FROM cycle_event
        WHERE subject_key=?
        ORDER BY started_at DESC, run_id DESC, cycle_event_id DESC
    """
    params: tuple[object, ...] = (_require_nonblank(subject_key, "subject_key"),)
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, int(limit))
    rows = conn.execute(sql, params).fetchall()
    events = [_row_to_dict(row) for row in rows]
    if limit is None:
        events.reverse()
    return events


def list_cycle_stage_events(conn: sqlite3.Connection, cycle_event_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM cycle_stage_event
        WHERE cycle_event_id=?
        ORDER BY stage_order, stage_name, stage_event_id
        """,
        (_require_nonblank(cycle_event_id, "cycle_event_id"),),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_cycle_artifacts(conn: sqlite3.Connection, cycle_event_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM cycle_artifact_ref
        WHERE cycle_event_id=?
        ORDER BY artifact_type, artifact_path, artifact_ref_id
        """,
        (_require_nonblank(cycle_event_id, "cycle_event_id"),),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _load_cycle_evidence_details(
    conn: sqlite3.Connection, cycle_event_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    rows = conn.execute(
        """
        WITH combined AS (
          SELECT
            0 AS sort_group,
            stage_order AS sort_primary,
            stage_name AS sort_secondary,
            stage_event_id AS sort_tertiary,
            'stage' AS row_kind,
            stage_event_id,
            cycle_event_id,
            run_id,
            stage_name,
            stage_order,
            started_at,
            ended_at,
            status,
            required_stage,
            skipped_reason,
            command_name,
            helper_name,
            input_artifact_ref_id,
            output_artifact_ref_id,
            validation_status,
            error_summary,
            metadata_json,
            created_at,
            record_last_updated,
            NULL AS artifact_ref_id,
            NULL AS artifact_type,
            NULL AS artifact_path,
            NULL AS artifact_hash,
            NULL AS byte_count,
            NULL AS privacy_classification,
            NULL AS public_safe,
            NULL AS schema_id
          FROM cycle_stage_event
          WHERE cycle_event_id=?
          UNION ALL
          SELECT
            1 AS sort_group,
            0 AS sort_primary,
            artifact_type AS sort_secondary,
            artifact_ref_id AS sort_tertiary,
            'artifact' AS row_kind,
            NULL AS stage_event_id,
            cycle_event_id,
            NULL AS run_id,
            NULL AS stage_name,
            NULL AS stage_order,
            NULL AS started_at,
            NULL AS ended_at,
            NULL AS status,
            NULL AS required_stage,
            NULL AS skipped_reason,
            NULL AS command_name,
            NULL AS helper_name,
            NULL AS input_artifact_ref_id,
            NULL AS output_artifact_ref_id,
            validation_status,
            NULL AS error_summary,
            metadata_json,
            created_at,
            record_last_updated,
            artifact_ref_id,
            artifact_type,
            artifact_path,
            artifact_hash,
            byte_count,
            privacy_classification,
            public_safe,
            schema_id
          FROM cycle_artifact_ref
          WHERE cycle_event_id=?
        )
        SELECT *
        FROM combined
        ORDER BY sort_group, sort_primary, sort_secondary, sort_tertiary
        """,
        (event_id, event_id),
    ).fetchall()
    stages: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    for row in rows:
        if row["row_kind"] == "stage":
            stages.append(
                {
                    "stage_event_id": row["stage_event_id"],
                    "cycle_event_id": row["cycle_event_id"],
                    "run_id": row["run_id"],
                    "stage_name": row["stage_name"],
                    "stage_order": row["stage_order"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "status": row["status"],
                    "required_stage": row["required_stage"],
                    "skipped_reason": row["skipped_reason"],
                    "command_name": row["command_name"],
                    "helper_name": row["helper_name"],
                    "input_artifact_ref_id": row["input_artifact_ref_id"],
                    "output_artifact_ref_id": row["output_artifact_ref_id"],
                    "validation_status": row["validation_status"],
                    "error_summary": row["error_summary"],
                    "metadata_json": row["metadata_json"],
                    "created_at": row["created_at"],
                    "record_last_updated": row["record_last_updated"],
                }
            )
        elif row["row_kind"] == "artifact":
            artifacts.append(
                {
                    "artifact_ref_id": row["artifact_ref_id"],
                    "cycle_event_id": row["cycle_event_id"],
                    "stage_event_id": row["stage_event_id"],
                    "artifact_type": row["artifact_type"],
                    "artifact_path": row["artifact_path"],
                    "artifact_hash": row["artifact_hash"],
                    "byte_count": row["byte_count"],
                    "privacy_classification": row["privacy_classification"],
                    "public_safe": row["public_safe"],
                    "schema_id": row["schema_id"],
                    "validation_status": row["validation_status"],
                    "created_at": row["created_at"],
                    "metadata_json": row["metadata_json"],
                    "record_last_updated": row["record_last_updated"],
                }
            )
    return stages, artifacts


def summarize_cycle_evidence(conn: sqlite3.Connection, cycle_event_id: str) -> dict[str, Any]:
    event_id = _require_nonblank(cycle_event_id, "cycle_event_id")
    event = load_cycle_event(conn, event_id)
    if event is None:
        raise CycleEvidenceLedgerError(f"cycle event not found: {event_id}")
    counts_row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM cycle_stage_event WHERE cycle_event_id=?) AS stages,
            (SELECT COUNT(*) FROM cycle_artifact_ref WHERE cycle_event_id=?) AS artifacts,
            (SELECT COUNT(*) FROM cycle_candidate_considered WHERE cycle_event_id=?) AS candidates_considered,
            (SELECT COUNT(*) FROM cycle_candidate_excluded WHERE cycle_event_id=?) AS candidates_excluded,
            (SELECT COUNT(*) FROM cycle_tool_failure WHERE cycle_event_id=?) AS tool_failures,
            (SELECT COUNT(*) FROM cycle_operator_override WHERE cycle_event_id=?) AS operator_overrides
        """,
        (event_id, event_id, event_id, event_id, event_id, event_id),
    ).fetchone()
    counts = {
        "stages": int(counts_row["stages"]),
        "artifacts": int(counts_row["artifacts"]),
        "candidates_considered": int(counts_row["candidates_considered"]),
        "candidates_excluded": int(counts_row["candidates_excluded"]),
        "tool_failures": int(counts_row["tool_failures"]),
        "operator_overrides": int(counts_row["operator_overrides"]),
    }
    stages, artifacts = _load_cycle_evidence_details(conn, event_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_event": event,
        "counts": counts,
        "stages": stages,
        "artifacts": artifacts,
    }


def _command_name(command: object) -> str | None:
    if not isinstance(command, list) or not command:
        return None
    first = command[0]
    return Path(str(first)).name if first is not None else None


def _stage_artifact_schema_id(stage: Mapping[str, Any], artifact_key: str) -> str | None:
    evidence = stage.get("evidence")
    if not isinstance(evidence, dict):
        return None
    artifact_schema_ids = evidence.get("artifact_schema_ids")
    if not isinstance(artifact_schema_ids, dict):
        return None
    value = artifact_schema_ids.get(artifact_key)
    return value if isinstance(value, str) and value else None


def _collect_artifact_schema_ids(stages: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    artifact_schema_ids: dict[str, str] = {}
    for stage in stages:
        evidence = stage.get("evidence")
        if not isinstance(evidence, dict):
            continue
        stage_schema_ids = evidence.get("artifact_schema_ids")
        if not isinstance(stage_schema_ids, dict):
            continue
        for artifact_key, schema_id in stage_schema_ids.items():
            if isinstance(artifact_key, str) and isinstance(schema_id, str) and schema_id:
                artifact_schema_ids.setdefault(artifact_key, schema_id)
    return artifact_schema_ids


def _stage_evidence_payload(stage: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    evidence = stage.get("evidence")
    if not isinstance(evidence, dict):
        return None
    payload = evidence.get(key)
    return payload if isinstance(payload, dict) else None


def _record_stage_artifacts(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    stage_event_id: str,
    stage: Mapping[str, Any],
    artifact_schema_ids: Mapping[str, str] | None = None,
) -> None:
    artifacts = stage.get("artifacts")
    if not isinstance(artifacts, dict):
        return
    for key, value in sorted(artifacts.items()):
        if key.endswith("_sha256") or key == "mutated":
            continue
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        hash_value = artifacts.get(f"{key}_sha256")
        artifact_hash = str(hash_value) if isinstance(hash_value, str) else None
        if artifact_hash is None:
            artifact_hash = _file_hash(path)
        schema_id = _stage_artifact_schema_id(stage, key)
        if schema_id is None and artifact_schema_ids is not None:
            schema_id = artifact_schema_ids.get(key)
        record_cycle_artifact_ref(
            conn,
            cycle_event_id=cycle_event_id,
            stage_event_id=stage_event_id,
            artifact_type=key,
            artifact_path=value,
            artifact_hash=artifact_hash,
            byte_count=_file_size(path),
            public_safe=False,
            schema_id=schema_id,
            validation_status=_optional_text((stage.get("validation") or {}).get("status"))
            if isinstance(stage.get("validation"), dict)
            else None,
        )


def _record_candidate_batch_payload(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    stage_event_id: str | None,
    batch: Mapping[str, Any],
    source_artifact_path: str | None = None,
) -> None:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        return
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        candidate_id = _optional_text(candidate.get("candidate_id")) or f"candidate:{index}"
        candidate_kind = _optional_text(candidate.get("candidate_type")) or "gather_candidate"
        origin = candidate.get("origin") if isinstance(candidate.get("origin"), dict) else {}
        label_parts = [
            _optional_text(candidate.get("candidate_type")),
            _optional_text(candidate.get("review_status")),
            _optional_text(candidate.get("persistence_status")),
        ]
        label = " / ".join(part for part in label_parts if part) or candidate_id
        record_cycle_candidate_considered(
            conn,
            cycle_event_id=cycle_event_id,
            stage_event_id=stage_event_id,
            candidate_kind=candidate_kind,
            candidate_ref_type="gather_candidate",
            candidate_ref_id=candidate_id,
            candidate_label=label,
            reason={
                "origin": origin,
                "facet": batch.get("facet"),
                "source_artifact": source_artifact_path,
            },
            selected=False,
        )


def _record_feedback_candidates_payload(
    conn: sqlite3.Connection,
    *,
    cycle_event_id: str,
    stage_event_id: str | None,
    payload: Mapping[str, Any],
    source_artifact_path: str | None = None,
) -> None:
    explanation = payload.get("selection_explanation")
    if isinstance(explanation, dict):
        policy = explanation.get("policy") if isinstance(explanation.get("policy"), dict) else {}
        policy_id = _optional_text(policy.get("policy_id"))
        for candidate in explanation.get("considered_candidates", []):
            if not isinstance(candidate, dict):
                continue
            candidate_id = _optional_text(candidate.get("candidate_id"))
            if candidate_id is None:
                continue
            record_cycle_candidate_considered(
                conn,
                cycle_event_id=cycle_event_id,
                stage_event_id=stage_event_id,
                candidate_kind=_optional_text(candidate.get("candidate_type"))
                or "feedback_candidate",
                candidate_ref_type="selection_explanation",
                candidate_ref_id=candidate_id,
                candidate_label=_optional_text(candidate.get("label")),
                score=candidate.get("score")
                if isinstance(candidate.get("score"), (int, float))
                else None,
                score_policy_id=policy_id,
                rationale=_optional_text(candidate.get("rationale")),
                reason={
                    "selection_explanation_id": explanation.get("explanation_id"),
                    "reason_codes": candidate.get("reason_codes", []),
                    "eligibility_status": candidate.get("eligibility_status"),
                    "source_artifact": source_artifact_path,
                },
                selected=bool(candidate.get("selected")),
            )
        for candidate in explanation.get("excluded_candidates", []):
            if not isinstance(candidate, dict):
                continue
            candidate_id = _optional_text(candidate.get("candidate_id"))
            if candidate_id is None:
                continue
            record_cycle_candidate_excluded(
                conn,
                cycle_event_id=cycle_event_id,
                stage_event_id=stage_event_id,
                candidate_kind=_optional_text(candidate.get("candidate_type"))
                or "feedback_candidate",
                candidate_ref_type="selection_explanation",
                candidate_ref_id=candidate_id,
                candidate_label=_optional_text(candidate.get("label")),
                exclusion_reason=_optional_text(candidate.get("reason"))
                or "deferred_by_feedback_plan",
                policy_id=policy_id,
                retryable=bool(candidate.get("retryable", True)),
            )
        return
    next_action = payload.get("next_action")
    if isinstance(next_action, dict):
        record_cycle_candidate_considered(
            conn,
            cycle_event_id=cycle_event_id,
            stage_event_id=stage_event_id,
            candidate_kind="feedback_next_action",
            candidate_ref_type=_optional_text(next_action.get("selected_lead_kind")) or "facet",
            candidate_ref_id=_optional_text(next_action.get("selected_object_ref"))
            or _optional_text(next_action.get("selected_facet")),
            candidate_label=_optional_text(next_action.get("selected_label"))
            or _optional_text(next_action.get("selected_facet")),
            score=next_action.get("selection_score")
            if isinstance(next_action.get("selection_score"), (int, float))
            else None,
            score_policy_id=_optional_text(next_action.get("scoring_policy_id")),
            rationale=_optional_text(next_action.get("rationale")),
            reason={"reason_codes": next_action.get("reason_codes", [])},
            selected=True,
        )
    deferred = payload.get("deferred")
    if isinstance(deferred, list):
        for index, item in enumerate(deferred, start=1):
            if not isinstance(item, dict):
                continue
            candidate_id = _optional_text(item.get("candidate_id")) or f"deferred:{index}"
            candidate_kind = _optional_text(item.get("candidate_kind")) or _optional_text(
                item.get("proposal_kind")
            ) or "feedback_candidate"
            reason = _optional_text(item.get("reason")) or "deferred_by_feedback_plan"
            record_cycle_candidate_excluded(
                conn,
                cycle_event_id=cycle_event_id,
                stage_event_id=stage_event_id,
                candidate_kind=candidate_kind,
                candidate_ref_type="candidate_feedback_plan",
                candidate_ref_id=candidate_id,
                candidate_label=_optional_text(item.get("label")) or candidate_id,
                exclusion_reason=reason,
                policy_id=_optional_text(item.get("policy_id")),
                retryable=bool(item.get("retryable"))
                if isinstance(item.get("retryable"), bool)
                else deferred_candidate_retryable(reason),
            )


def _stage_by_name(stages: Iterable[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    for stage in stages:
        if stage.get("name") == name:
            return stage
    return None


def record_topic_cycle_manifest(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    manifest_hash: str | None = None,
    canonical_db_ref: str | None = None,
) -> str:
    """Record operational evidence from a topic-cycle manifest.

    The function is idempotent for the same run/stage/artifact ids and never
    writes canonical source facts, claims, captures, or review decisions.
    """

    run_id = _require_nonblank(manifest.get("run_id"), "manifest.run_id")
    workspace = manifest.get("workspace") if isinstance(manifest.get("workspace"), dict) else {}
    subject = manifest.get("subject") if isinstance(manifest.get("subject"), dict) else {}
    domain_pack = (
        manifest.get("domain_pack") if isinstance(manifest.get("domain_pack"), dict) else {}
    )
    ledger = (
        manifest.get("cycle_evidence_ledger")
        if isinstance(manifest.get("cycle_evidence_ledger"), dict)
        else {}
    )
    cycle_event_id = _optional_text(ledger.get("cycle_event_id"))
    final_feedback_plan: Mapping[str, Any] = {}
    if isinstance(manifest.get("active_feedback_plan_for_gather"), dict):
        final_feedback_plan = manifest["active_feedback_plan_for_gather"]
    elif isinstance(manifest.get("feedback_plan_pre"), dict):
        final_feedback_plan = manifest["feedback_plan_pre"]
    elif isinstance(manifest.get("feedback_plan"), dict):
        final_feedback_plan = manifest["feedback_plan"]
    final_feedback_plan_ref = _optional_text(final_feedback_plan.get("path"))
    stages = manifest.get("stages") if isinstance(manifest.get("stages"), list) else []
    status = _require_nonblank(manifest.get("status"), "manifest.status")
    warning_count = (
        len(manifest.get("warnings", [])) if isinstance(manifest.get("warnings"), list) else 0
    )
    error_count = 1 if status == "failed" else 0
    event_id = record_cycle_event_start(
        conn,
        run_id=run_id,
        workspace_id=_optional_text(workspace.get("workspace_id")),
        workspace_ref=_optional_text(workspace.get("path")),
        subject_key=_optional_text(subject.get("subject_id")),
        domain_pack_id=_optional_text(domain_pack.get("domain_pack_id")),
        cycle_depth=manifest.get("cycle_depth")
        if isinstance(manifest.get("cycle_depth"), int)
        else None,
        previous_run_ids=manifest.get("previous_run_ids")
        if isinstance(manifest.get("previous_run_ids"), list)
        else None,
        mode=_optional_text(manifest.get("mode")),
        started_at=_optional_text(manifest.get("started_at")),
        status=status,
        topic_cycle_manifest_path=str(manifest_path),
        topic_cycle_manifest_hash=manifest_hash,
        canonical_db_ref=canonical_db_ref,
        final_feedback_plan_ref=final_feedback_plan_ref,
        warning_count=warning_count,
        error_count=error_count,
        metadata={"schema_version": manifest.get("schema_version")},
        cycle_event_id=cycle_event_id,
    )
    artifact_schema_ids = _collect_artifact_schema_ids(stages)
    stage_ids: dict[str, str] = {}
    for index, raw_stage in enumerate(stages, start=1):
        if not isinstance(raw_stage, dict):
            continue
        name = _require_nonblank(raw_stage.get("name"), f"stages[{index}].name")
        stage_id = record_cycle_stage_start(
            conn,
            cycle_event_id=event_id,
            run_id=run_id,
            stage_name=name,
            stage_order=index,
            started_at=_optional_text(raw_stage.get("started_at")),
            status=_optional_text(raw_stage.get("status")) or "recorded",
            required_stage=bool(raw_stage.get("required", True)),
            skipped_reason=_optional_text(raw_stage.get("skipped_reason")),
            command_name=_command_name(raw_stage.get("command")),
            validation_status=_optional_text((raw_stage.get("validation") or {}).get("status"))
            if isinstance(raw_stage.get("validation"), dict)
            else None,
            error_summary=_optional_text(raw_stage.get("error_message")),
            metadata={"counts": raw_stage.get("counts"), "inputs": raw_stage.get("inputs")},
        )
        record_cycle_stage_finish(
            conn,
            stage_event_id=stage_id,
            status=_optional_text(raw_stage.get("status")) or "recorded",
            ended_at=_optional_text(raw_stage.get("ended_at")),
            error_summary=_optional_text(raw_stage.get("error_message")),
        )
        stage_ids[name] = stage_id
        _record_stage_artifacts(
            conn,
            cycle_event_id=event_id,
            stage_event_id=stage_id,
            stage=raw_stage,
            artifact_schema_ids=artifact_schema_ids,
        )
        candidate_batch_payload = _stage_evidence_payload(raw_stage, "candidate_batch")
        if candidate_batch_payload is not None:
            _record_candidate_batch_payload(
                conn,
                cycle_event_id=event_id,
                stage_event_id=stage_id,
                batch=candidate_batch_payload,
                source_artifact_path=_optional_text(candidate_batch_payload.get("artifact_path")),
            )
        feedback_plan_payload = _stage_evidence_payload(raw_stage, "feedback_plan")
        if feedback_plan_payload is not None:
            _record_feedback_candidates_payload(
                conn,
                cycle_event_id=event_id,
                stage_event_id=stage_id,
                payload=feedback_plan_payload,
                source_artifact_path=_optional_text(feedback_plan_payload.get("artifact_path")),
            )
        if raw_stage.get("status") == "failed":
            record_cycle_tool_failure(
                conn,
                cycle_event_id=event_id,
                stage_event_id=stage_id,
                tool_name=name,
                command_name=_command_name(raw_stage.get("command")),
                failure_kind="stage_failure",
                error_summary=_optional_text(raw_stage.get("error_message"))
                or _optional_text(manifest.get("error_summary"))
                or "stage failed",
                retryable=True,
            )
        if raw_stage.get("status") == "skipped" and raw_stage.get("skipped_reason"):
            record_cycle_candidate_excluded(
                conn,
                cycle_event_id=event_id,
                stage_event_id=stage_id,
                candidate_kind="cycle_stage",
                candidate_ref_type="stage",
                candidate_ref_id=name,
                candidate_label=name,
                exclusion_reason=str(raw_stage["skipped_reason"]),
                retryable=True,
            )

    manifest_artifact_id = record_cycle_artifact_ref(
        conn,
        cycle_event_id=event_id,
        artifact_type="topic_cycle_manifest",
        artifact_path=str(manifest_path),
        artifact_hash=manifest_hash,
        byte_count=_file_size(manifest_path),
        schema_id=_optional_text(manifest.get("schema_version")),
        validation_status=status,
        public_safe=False,
    )
    record_cycle_event_finish(
        conn,
        cycle_event_id=event_id,
        status=status,
        ended_at=_optional_text(manifest.get("ended_at")),
        topic_cycle_manifest_path=str(manifest_path),
        topic_cycle_manifest_hash=manifest_hash,
        final_feedback_plan_ref=final_feedback_plan_ref,
        warning_count=warning_count,
        error_count=error_count,
    )

    for item in manifest.get("operator_overrides", []):
        if not isinstance(item, dict):
            continue
        record_cycle_operator_override(
            conn,
            cycle_event_id=event_id,
            override_kind=_optional_text(item.get("override_kind")) or "operator_override",
            override_value=_optional_text(item.get("override_value")),
            reason=_optional_text(item.get("reason")),
            actor=_optional_text(item.get("actor")),
        )

    # Keep the manifest artifact id visibly linked by leaving it as metadata on
    # failure rows created later by callers if needed. The local variable is
    # intentionally retained to make this side effect auditable in reviews.
    _ = manifest_artifact_id
    return event_id
