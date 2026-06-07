#!/usr/bin/env python3
"""Run bounded topic cycles from scheduled workspace selection artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common import runtime_ledger  # noqa: E402
from tools.common.workspace_lock import (  # noqa: E402
    acquire_workspace_lock,
    DEFAULT_LOCK_ROOT,
    WorkspaceLockError,
)
from tools.common.scheduler_failure_reconciliation import (  # noqa: E402
    read_runtime_ledger,
    summarize_run_outcomes,
)

SCHEMA_VERSION = "scheduled-topic-cycles-run.v1"
PLANNED_RUN_SCHEMA_VERSION = "planned-run.v1"
WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 2
EXIT_VALIDATION_FAILED = 3
EXIT_SAFETY_DENIAL = 4
EXIT_TRANSIENT_ACQUISITION_FAILED = 5
EXIT_TRANSIENT_ACQUISITION_FAILURE = EXIT_TRANSIENT_ACQUISITION_FAILED
EXIT_INTEGRITY_FAILURE = 6
EXIT_PARTIAL_OUTPUT = 7
EXIT_INTERNAL_CRASH = 8



def _next_workspace_token() -> str:
    return uuid.uuid4().hex


def _format_run_id(*, run_id: str, workspace_id: str, attempt_number: int | str, token: str) -> str:
    return f"{run_id}.{workspace_id}.{attempt_number}.{token}"


class ScheduledCycleError(RuntimeError):
    """Raised when scheduled topic-cycle execution cannot continue safely."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def offset_timestamp(timestamp: str, *, seconds: int) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    shifted = parsed + timedelta(seconds=seconds)
    return shifted.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: str | None) -> str:
    if value is None:
        return utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduledCycleError(f"timestamp must be RFC3339: {value}") from exc
    if parsed.tzinfo is None:
        raise ScheduledCycleError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_failure_result(
    *,
    result: dict[str, Any],
    reason: str,
    reason_code: str,
    stage: str,
    recoverability: str,
    affected_record_id: str | None = None,
) -> None:
    result["failure_reason"] = reason
    result["failure_reason_code"] = reason_code
    result["error_code"] = reason_code
    result["stage"] = stage
    result["recoverability"] = recoverability
    if affected_record_id is not None:
        result["affected_record_id"] = affected_record_id


def _set_failure(
    result: dict[str, Any],
    *,
    reason_code: str,
    reason: str,
    stage: str,
    recoverability: str,
) -> None:
    _format_failure_result(
        result=result,
        reason=reason,
        reason_code=reason_code,
        stage=stage,
        recoverability=recoverability,
        affected_record_id=result.get("planned_run_id", result.get("workspace_id")),
    )


def resolve_path(raw_path: str | Path, *, base: Path | None = None) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base or Path.cwd()) / path).resolve()


def validate_planned_run_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required_fields = (
        "schema_version",
        "planner_run_id",
        "planned_run_id",
        "planned_at",
        "registry_path",
        "workspace_id",
        "decision",
        "cadence_reason",
        "skipped_reason",
        "skipped_reasons",
        "run_budget",
        "retry_policy",
        "failure_state",
        "workspace_root",
        "resolved_workspace_root",
        "default_subject_manifest",
        "resolved_default_subject_manifest",
    )
    for field in required_fields:
        if field not in record:
            errors.append(f"planned-run record is missing required field: {field}")

    if record.get("schema_version") != PLANNED_RUN_SCHEMA_VERSION:
        errors.append("planned-run record has wrong schema_version")
    if not isinstance(record.get("planner_run_id"), str) or not record["planner_run_id"].strip():
        errors.append("planned-run record planner_run_id must be a non-blank string")
    if not isinstance(record.get("planned_run_id"), str) or not record["planned_run_id"].strip():
        errors.append("planned-run record planned_run_id must be a non-blank string")
    if not isinstance(record.get("planned_at"), str) or not record["planned_at"].strip():
        errors.append("planned-run record planned_at must be a non-blank string")
    if not isinstance(record.get("registry_path"), str) or not record["registry_path"].strip():
        errors.append("planned-run record registry_path must be a non-blank string")
    workspace_id = record.get("workspace_id")
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        errors.append("planned-run record workspace_id must match the workspace identifier pattern")
    if record.get("decision") not in {"selected", "skipped"}:
        errors.append("planned-run record decision must be selected or skipped")
    if not isinstance(record.get("cadence_reason"), str) or not record["cadence_reason"].strip():
        errors.append("planned-run record cadence_reason must be a non-blank string")
    if record.get("skipped_reason") is not None and not isinstance(record.get("skipped_reason"), str):
        errors.append("planned-run record skipped_reason must be null or a string")
    if not isinstance(record.get("skipped_reasons"), list):
        errors.append("planned-run record skipped_reasons must be an array")
    run_budget = record.get("run_budget")
    if not isinstance(run_budget, dict):
        errors.append("planned-run record run_budget must be an object")
    else:
        max_attempts = run_budget.get("max_attempts")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            errors.append("planned-run record run_budget.max_attempts must be an integer >= 1")
        max_runtime = run_budget.get("max_runtime_seconds")
        if max_runtime is not None and (
            not isinstance(max_runtime, int) or isinstance(max_runtime, bool) or max_runtime < 1
        ):
            errors.append("planned-run record run_budget.max_runtime_seconds must be null or an integer >= 1")
    if record.get("retry_policy") is not None and not isinstance(record.get("retry_policy"), dict):
        errors.append("planned-run record retry_policy must be null or an object")
    if record.get("failure_state") is not None and not isinstance(record.get("failure_state"), dict):
        errors.append("planned-run record failure_state must be null or an object")
    for field in ("workspace_root", "resolved_workspace_root", "default_subject_manifest", "resolved_default_subject_manifest"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            errors.append(f"planned-run record {field} must be a non-blank string")

    return errors


def hash_file(path: Path) -> str:
    digest = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def manifest_relative_path(path: Path, *, run_dir: Path) -> str:
    return os.path.relpath(str(path.resolve()), start=str(run_dir)).replace(os.sep, "/")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Consume select_scheduled_workspaces.py planned-run output, enforce recorded "
            "run budgets, run bounded topic cycles, and append runtime-ledger outcomes."
        )
    )
    parser.add_argument(
        "--selection", required=True, help="Selection JSON or planned-run JSONL artifact."
    )
    parser.add_argument(
        "--db", required=True, help="Canonical SQLite store to pass to each topic cycle."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Output directory for the scheduled run manifest and child cycles.",
    )
    parser.add_argument("--run-id", help="Stable scheduled run id. Defaults to run directory name.")
    parser.add_argument("--timestamp", help="RFC3339 timestamp override.")
    parser.add_argument("--mode", choices=("dry-run", "local"), default="dry-run")
    parser.add_argument(
        "--cycle-runner", help="Optional alternate cycle runner script for deterministic tests."
    )
    parser.add_argument(
        "--candidate-batch-fixture", help="Optional fixture passed through to each child cycle."
    )
    parser.add_argument(
        "--execution-run-fixture",
        help="Optional execution fixture passed through to each child cycle.",
    )
    parser.add_argument("--build-next-feedback-plan", action="store_true")
    parser.add_argument(
        "--ledger-root",
        type=Path,
        default=REPO_ROOT / runtime_ledger.DEFAULT_LEDGER_ROOT,
        help="Directory for per-workspace runtime-ledger JSONL files.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument(
        "--workspace-lock-root",
        type=Path,
        default=DEFAULT_LOCK_ROOT,
        help="Directory containing advisory workspace lock files.",
    )
    return parser.parse_args(argv)


def load_selection_records(selection_path: Path) -> list[dict[str, Any]]:
    if not selection_path.is_file():
        raise ScheduledCycleError(f"selection artifact not found: {selection_path}")
    text = selection_path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    records: list[dict[str, Any]]
    if stripped.startswith("{"):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ScheduledCycleError("selection JSON must be an object")
        raw_records = payload.get("planned_run_records")
        if not isinstance(raw_records, list):
            raise ScheduledCycleError("selection JSON must include planned_run_records")
        records = raw_records
    else:
        records = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ScheduledCycleError(f"selection JSONL line {line_number} must be an object")
            records.append(value)
    for record in records:
        errors = validate_planned_run_record(record)
        if errors:
            raise ScheduledCycleError(errors[0])
    return records


def terminal_attempt_count(ledger_path: Path, *, workspace_id: str) -> int:
    events = read_runtime_ledger(ledger_path, workspace_id=workspace_id)
    outcomes = summarize_run_outcomes(events)
    return len(outcomes)


def append_ledger_event(
    *,
    ledger_path: Path,
    workspace_id: str,
    run_id: str,
    event_type: str,
    occurred_at: str,
    status: str | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    failure: dict[str, Any] | None = None,
) -> None:
    event = runtime_ledger.build_event(
        workspace_id=workspace_id,
        run_id=run_id,
        event_type=event_type,
        command="run_topic_cycle",
        status=status,
        artifact_refs=artifact_refs,
        failure=failure,
        occurred_at=occurred_at,
    )
    runtime_ledger.append_event(ledger_path, event)


def planned_record_selected(record: dict[str, Any]) -> bool:
    return record.get("decision") == "selected"


def max_attempts_exceeded(record: dict[str, Any], *, prior_attempts: int) -> str | None:
    run_budget = record.get("run_budget")
    max_attempts = run_budget.get("max_attempts") if isinstance(run_budget, dict) else None
    if isinstance(max_attempts, int) and prior_attempts >= max_attempts:
        return f"attempt_count {prior_attempts} reached run_budget.max_attempts {max_attempts}"
    return None


def get_runtime_budget_seconds(record: dict[str, Any]) -> int | None:
    run_budget = record.get("run_budget")
    max_runtime = run_budget.get("max_runtime_seconds") if isinstance(run_budget, dict) else None
    return max_runtime if isinstance(max_runtime, int) else None


def default_cycle_invoker(
    command: list[str],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def invoke_cycle(
    cycle_invoker: Callable[..., subprocess.CompletedProcess[str]],
    command: list[str],
    *,
    timeout_seconds: float | None,
) -> subprocess.CompletedProcess[str]:
    try:
        return cycle_invoker(command, timeout=timeout_seconds)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
    return cycle_invoker(command)


def run_scheduled_cycles(
    args: argparse.Namespace,
    *,
    cycle_invoker: Callable[[list[str]], subprocess.CompletedProcess[str]] = default_cycle_invoker,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], int]:
    started_at = normalize_timestamp(args.timestamp)
    run_dir = resolve_path(args.run_dir)
    run_id = args.run_id or run_dir.name
    db_path = resolve_path(args.db)
    selection_path = resolve_path(args.selection)
    records = load_selection_records(selection_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scheduled_run_id": run_id,
        "selection_artifact": {
            "path": manifest_relative_path(selection_path, run_dir=run_dir),
            "sha256": hash_file(selection_path),
        },
        "started_at": started_at,
        "ended_at": None,
        "status": "running",
        "mode": args.mode,
        "selected_workspace_count": sum(1 for record in records if planned_record_selected(record)),
        "attempted_workspace_count": 0,
        "completed_workspace_count": 0,
        "failed_workspace_count": 0,
        "deferred_workspace_count": 0,
        "budget_summary": {
            "max_attempts_enforced": True,
            "max_runtime_seconds_enforced": True,
        },
        "workspace_results": [],
        "warnings": [],
        "errors": [],
        "remote_fetch_enabled": False,
    }
    runner = (
        resolve_path(args.cycle_runner)
        if args.cycle_runner
        else REPO_ROOT / "tools" / "scripts" / "run_topic_cycle.py"
    )
    exit_code = EXIT_SUCCESS

    def _update_global_exit_code(code: int) -> None:
        nonlocal exit_code
        if code > exit_code:
            exit_code = code

    workspace_results: list[dict[str, Any] | None] = [None] * len(records)
    workspace_locks: dict[str, threading.Lock] = {}

    def _run_single_cycle(
        *,
        workspace_id: str,
        runtime_budget: int | None,
        workspace_root: str,
        subject_manifest: str,
        cycle_runner: Path,
        db_path: str,
        attempt_number: int,
        mode: str,
        run_id: str,
        candidate_batch_fixture: str | None,
        execution_run_fixture: str | None,
        build_next_feedback_plan: bool,
        child_started_at: str,
        child_ended_at: str,
        ledger_path: Path,
        result: dict[str, Any],
        workspace_lock_root: Path,
        workspace_mutex: threading.Lock,
    ) -> tuple[dict[str, Any], int, int, int, int, int]:
        proc: subprocess.CompletedProcess[str] | None = None
        result["runtime_consumed_seconds"] = 0.0
        attempted_delta = 0
        completed_delta = 0
        failed_delta = 0
        deferred_delta = 0
        code_delta = EXIT_SUCCESS
        with workspace_mutex:
            child_run_id = _format_run_id(
                run_id=run_id,
                workspace_id=workspace_id,
                attempt_number=attempt_number,
                token=_next_workspace_token(),
            )
            child_run_dir = run_dir / workspace_id / child_run_id
            command = [
                sys.executable,
                str(cycle_runner),
                "--workspace",
                workspace_root,
                "--subject",
                subject_manifest,
                "--db",
                str(db_path),
                "--run-dir",
                str(child_run_dir),
                "--run-id",
                child_run_id,
                "--timestamp",
                child_started_at,
                "--mode",
                mode,
                "--format",
                "json",
            ]
            if candidate_batch_fixture:
                command.extend(["--candidate-batch-fixture", candidate_batch_fixture])
            if execution_run_fixture:
                command.extend(["--execution-run-fixture", execution_run_fixture])
            if build_next_feedback_plan:
                command.append("--build-next-feedback-plan")
            try:
                with acquire_workspace_lock(
                    workspace_id=workspace_id,
                    command=f"scheduled-topic-cycle:{child_run_id}",
                    lock_root=workspace_lock_root,
                    wait=False,
                ):
                    append_ledger_event(
                        ledger_path=ledger_path,
                        workspace_id=workspace_id,
                        run_id=child_run_id,
                        event_type="command_start",
                        occurred_at=child_started_at,
                    )
                    attempted_delta += 1
                    start = monotonic()
                    timeout_seconds = float(runtime_budget) if runtime_budget is not None else None
                    try:
                        proc = invoke_cycle(cycle_invoker, command, timeout_seconds=timeout_seconds)
                    except subprocess.TimeoutExpired:
                        elapsed = round(monotonic() - start, 6)
                        result["runtime_consumed_seconds"] = elapsed
                        result["cycle_run_id"] = child_run_id
                        result["cycle_manifest_path"] = manifest_relative_path(
                            child_run_dir / "topic-cycle-run.json", run_dir=run_dir
                        )
                        result["outcome"] = "failed"
                        _set_failure(
                            result=result,
                            reason_code="runtime_budget_timeout",
                            reason=(
                                f"cycle exceeded run_budget.max_runtime_seconds {runtime_budget} before completion"
                            ),
                            stage="child_cycle_exec",
                            recoverability="retryable",
                        )
                        failed_delta += 1
                        code_delta = EXIT_INTEGRITY_FAILURE
                        _update_global_exit_code(code_delta)
                        append_ledger_event(
                            ledger_path=ledger_path,
                            workspace_id=workspace_id,
                            run_id=child_run_id,
                            event_type="command_failure",
                            occurred_at=child_ended_at,
                            artifact_refs=[
                                {
                                    "artifact_type": "topic_cycle_manifest",
                                    "path": result["cycle_manifest_path"],
                                }
                            ],
                            failure={"message": result["failure_reason"]},
                        )
                        result["scheduler_failure_state_record"] = manifest_relative_path(
                            ledger_path, run_dir=run_dir
                        )
                        return result, code_delta, attempted_delta, completed_delta, failed_delta, deferred_delta
                    elapsed = round(monotonic() - start, 6)
            except WorkspaceLockError as exc:
                result["outcome"] = "deferred"
                _set_failure(
                    result=result,
                    reason_code="workspace_lock_unavailable",
                    reason=str(exc),
                    stage="workspace_lock",
                    recoverability="retryable",
                )
                deferred_delta += 1
                code_delta = EXIT_TRANSIENT_ACQUISITION_FAILURE
                _update_global_exit_code(code_delta)
                return result, code_delta, attempted_delta, completed_delta, failed_delta, deferred_delta
            except Exception as exc:
                result["outcome"] = "failed"
                _set_failure(
                    result=result,
                    reason_code="internal_scheduler_error",
                    reason=str(exc),
                    stage="scheduler_execution",
                    recoverability="non_retryable",
                )
                failed_delta += 1
                code_delta = EXIT_INTERNAL_CRASH
                _update_global_exit_code(code_delta)
                return result, code_delta, attempted_delta, completed_delta, failed_delta, deferred_delta
        result["runtime_consumed_seconds"] = elapsed
        result["cycle_run_id"] = child_run_id
        result["cycle_manifest_path"] = manifest_relative_path(
            child_run_dir / "topic-cycle-run.json", run_dir=run_dir
        )
        child_manifest_path = child_run_dir / "topic-cycle-run.json"
        child_status = None
        if child_manifest_path.is_file():
            try:
                child_manifest = json.loads(child_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                child_manifest = None
            if isinstance(child_manifest, dict):
                raw_child_status = child_manifest.get("status")
                if isinstance(raw_child_status, str):
                    child_status = raw_child_status
                cycle_event_id = child_manifest.get("cycle_event_id")
                if isinstance(cycle_event_id, str) and cycle_event_id:
                    result["cycle_event_id"] = cycle_event_id

        artifact_refs = [
            {"artifact_type": "topic_cycle_manifest", "path": result["cycle_manifest_path"]}
        ]
        if runtime_budget is not None and elapsed > runtime_budget:
            result["outcome"] = "failed"
            _set_failure(
                result=result,
                reason_code="runtime_budget_exceeded",
                reason=(
                    f"cycle runtime {elapsed} exceeded run_budget.max_runtime_seconds {runtime_budget}"
                ),
                stage="runtime_budget_check",
                recoverability="retryable",
            )
            failed_delta += 1
            code_delta = EXIT_INTEGRITY_FAILURE
            _update_global_exit_code(code_delta)
            append_ledger_event(
                ledger_path=ledger_path,
                workspace_id=workspace_id,
                run_id=child_run_id,
                event_type="command_failure",
                occurred_at=child_ended_at,
                artifact_refs=artifact_refs,
                failure={"message": result["failure_reason"]},
            )
        elif proc is not None and proc.returncode == 0:
            result["outcome"] = "completed"
            completed_delta += 1
            code_delta = EXIT_SUCCESS
            append_ledger_event(
                ledger_path=ledger_path,
                workspace_id=workspace_id,
                run_id=child_run_id,
                event_type="command_end",
                occurred_at=child_ended_at,
                status="success",
                artifact_refs=artifact_refs,
            )
        else:
            result["outcome"] = "failed"
            is_partial = child_status == "partial"
            assert proc is not None
            _set_failure(
                result=result,
                reason_code="topic_cycle_partial_output" if is_partial else "topic_cycle_failed",
                reason=(proc.stderr or proc.stdout).strip() or "topic cycle failed",
                stage="child_cycle_exec",
                recoverability="retryable" if is_partial else "non_retryable",
            )
            failed_delta += 1
            code_delta = EXIT_PARTIAL_OUTPUT if is_partial else EXIT_INTEGRITY_FAILURE
            _update_global_exit_code(code_delta)
            append_ledger_event(
                ledger_path=ledger_path,
                workspace_id=workspace_id,
                run_id=child_run_id,
                event_type="command_failure",
                occurred_at=child_ended_at,
                artifact_refs=artifact_refs,
                failure={"message": result["failure_reason"]},
            )
        result["scheduler_failure_state_record"] = manifest_relative_path(ledger_path, run_dir=run_dir)
        return result, code_delta, attempted_delta, completed_delta, failed_delta, deferred_delta

    attempted_index = 0
    pending: list[tuple[int, Any]] = []
    max_workers = min(4, len(records)) if len(records) > 0 else 1
    workspace_lock_root = resolve_path(args.workspace_lock_root)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for record_index, record in enumerate(records):
            workspace_id = str(record.get("workspace_id") or "")
            result: dict[str, Any] = {
                "workspace_id": workspace_id,
                "planned_run_id": record.get("planned_run_id"),
                "decision": record.get("decision"),
                "cycle_run_id": None,
                "cycle_event_id": None,
                "cycle_manifest_path": None,
                "attempt_number": None,
                "max_attempts": None,
                "runtime_budget_seconds": None,
                "runtime_consumed_seconds": 0.0,
                "outcome": "skipped",
                "failure_reason": None,
                "failure_reason_code": None,
                "error_code": None,
                "stage": None,
                "recoverability": None,
                "affected_record_id": None,
                "saturation": record.get("saturation")
                if isinstance(record.get("saturation"), dict)
                else None,
                "saturation_override": bool(record.get("saturation_override", False)),
                "scheduler_failure_state_record": None,
                "ledger_path": None,
            }
            raw_run_budget = record.get("run_budget")
            run_budget: dict[str, Any] = raw_run_budget if isinstance(raw_run_budget, dict) else {}
            result["max_attempts"] = run_budget.get("max_attempts")
            result["runtime_budget_seconds"] = run_budget.get("max_runtime_seconds")
            if not planned_record_selected(record):
                result["outcome"] = "deferred"
                _set_failure(
                    result=result,
                    reason_code="selection_not_selected",
                    reason=record.get("skipped_reason") or "planned-run decision was not selected",
                    stage="selection_filter",
                    recoverability="non_retryable",
                )
                manifest["deferred_workspace_count"] += 1
                workspace_results[record_index] = result
                continue
            workspace_root = record.get("resolved_workspace_root")
            subject_manifest = record.get("resolved_default_subject_manifest")
            if not isinstance(workspace_root, str) or not isinstance(subject_manifest, str):
                result["outcome"] = "failed"
                _set_failure(
                    result=result,
                    reason_code="selection_record_invalid",
                    reason="planned-run record is missing resolved workspace or subject manifest",
                    stage="input_validation",
                    recoverability="non_retryable",
                )
                manifest["failed_workspace_count"] += 1
                _update_global_exit_code(EXIT_VALIDATION_FAILED)
                workspace_results[record_index] = result
                continue
            saturation = result.get("saturation")
            if (
                isinstance(saturation, dict)
                and saturation.get("scheduler_action") in {"halt", "cooldown"}
                and not result["saturation_override"]
            ):
                result["outcome"] = "deferred"
                _set_failure(
                    result=result,
                    reason_code="selection_saturated",
                    reason=(
                        "saturation policy deferred workspace: "
                        f"{saturation.get('state')} ({', '.join(saturation.get('reason_codes', []))})"
                    ),
                    stage="saturation_check",
                    recoverability="retryable",
                )
                manifest["deferred_workspace_count"] += 1
                workspace_results[record_index] = result
                continue
            ledger_path = resolve_path(args.ledger_root) / f"{workspace_id}.runtime-ledger.jsonl"
            result["ledger_path"] = manifest_relative_path(ledger_path, run_dir=run_dir)
            prior_attempts = terminal_attempt_count(ledger_path, workspace_id=workspace_id)
            result["attempt_number"] = prior_attempts + 1
            attempt_refusal = max_attempts_exceeded(record, prior_attempts=prior_attempts)
            if attempt_refusal is not None:
                result["outcome"] = "deferred"
                _set_failure(
                    result=result,
                    reason_code="selection_run_budget_exceeded",
                    reason=attempt_refusal,
                    stage="attempt_budget_check",
                    recoverability="retryable",
                )
                manifest["deferred_workspace_count"] += 1
                workspace_results[record_index] = result
                continue
            runtime_budget = get_runtime_budget_seconds(record)
            child_started_at = offset_timestamp(started_at, seconds=10 + attempted_index * 20)
            child_ended_at = offset_timestamp(started_at, seconds=20 + attempted_index * 20)
            attempted_index += 1
            future = executor.submit(
                _run_single_cycle,
                workspace_id=workspace_id,
                runtime_budget=runtime_budget,
                workspace_root=workspace_root,
                subject_manifest=subject_manifest,
                cycle_runner=runner,
                db_path=str(db_path),
                attempt_number=result["attempt_number"],
                mode=args.mode,
                run_id=run_id,
                candidate_batch_fixture=(
                    str(resolve_path(args.candidate_batch_fixture))
                    if args.candidate_batch_fixture
                    else None
                ),
                execution_run_fixture=(
                    str(resolve_path(args.execution_run_fixture))
                    if args.execution_run_fixture
                    else None
                ),
                build_next_feedback_plan=args.build_next_feedback_plan,
                child_started_at=child_started_at,
                child_ended_at=child_ended_at,
                ledger_path=ledger_path,
                result=result,
                workspace_lock_root=workspace_lock_root,
                workspace_mutex=workspace_locks.setdefault(workspace_id, threading.Lock()),
            )
            pending.append((record_index, future))
        for record_index, future in pending:
            (
                result,
                run_exit_code,
                attempted_delta,
                completed_delta,
                failed_delta,
                deferred_delta,
            ) = future.result()
            manifest["attempted_workspace_count"] += attempted_delta
            manifest["completed_workspace_count"] += completed_delta
            manifest["failed_workspace_count"] += failed_delta
            manifest["deferred_workspace_count"] += deferred_delta
            if run_exit_code > exit_code:
                exit_code = run_exit_code
            workspace_results[record_index] = result
    manifest["workspace_results"] = [
        result for result in workspace_results if result is not None
    ]
    manifest["ended_at"] = utc_now()
    if manifest["failed_workspace_count"]:
        manifest["status"] = "failed"
    elif manifest["attempted_workspace_count"]:
        manifest["status"] = "completed"
    else:
        manifest["status"] = "deferred"
    write_json(run_dir / "scheduled-topic-cycles-run.json", manifest)
    return manifest, exit_code


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"scheduled_run_id={payload['scheduled_run_id']}",
        f"status={payload['status']}",
        f"attempted={payload['attempted_workspace_count']}",
        f"completed={payload['completed_workspace_count']}",
        f"failed={payload['failed_workspace_count']}",
        f"deferred={payload['deferred_workspace_count']}",
    ]
    for result in payload["workspace_results"]:
        lines.append(f"workspace.{result['workspace_id']}={result['outcome']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE_ERROR if exc.code != 0 else EXIT_SUCCESS
    try:
        payload, exit_code = run_scheduled_cycles(args)
    except (ScheduledCycleError, runtime_ledger.RuntimeLedgerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_FAILED
    except WorkspaceLockError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_TRANSIENT_ACQUISITION_FAILURE
    except Exception as exc:  # pragma: no cover - defensive for unexpected runtime regressions.
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_CRASH
    if args.format == "text":
        sys.stdout.write(render_text(payload))
    else:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
