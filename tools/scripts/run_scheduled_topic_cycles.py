#!/usr/bin/env python3
"""Run bounded topic cycles from scheduled workspace selection artifacts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common import runtime_ledger  # noqa: E402
from tools.common.scheduler_failure_reconciliation import (  # noqa: E402
    read_runtime_ledger,
    summarize_run_outcomes,
)

SCHEMA_VERSION = "scheduled-topic-cycles-run.v1"
PLANNED_RUN_SCHEMA_VERSION = "planned-run.v1"


class ScheduledCycleError(RuntimeError):
    """Raised when scheduled topic-cycle execution cannot continue safely."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def resolve_path(raw_path: str | Path, *, base: Path | None = None) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base or Path.cwd()) / path).resolve()


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
        if record.get("schema_version") != PLANNED_RUN_SCHEMA_VERSION:
            raise ScheduledCycleError("planned-run record has wrong schema_version")
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


def default_cycle_invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)


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
            "path": str(selection_path),
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
    exit_code = 0
    for record in records:
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
            result["failure_reason"] = (
                record.get("skipped_reason") or "planned-run decision was not selected"
            )
            manifest["deferred_workspace_count"] += 1
            manifest["workspace_results"].append(result)
            continue
        workspace_root = record.get("resolved_workspace_root")
        subject_manifest = record.get("resolved_default_subject_manifest")
        if not isinstance(workspace_root, str) or not isinstance(subject_manifest, str):
            result["outcome"] = "failed"
            result["failure_reason"] = (
                "planned-run record is missing resolved workspace or subject manifest"
            )
            manifest["failed_workspace_count"] += 1
            manifest["workspace_results"].append(result)
            exit_code = 1
            continue
        saturation = result.get("saturation")
        if (
            isinstance(saturation, dict)
            and saturation.get("scheduler_action") in {"halt", "cooldown"}
            and not result["saturation_override"]
        ):
            result["outcome"] = "deferred"
            result["failure_reason"] = (
                "saturation policy deferred workspace: "
                f"{saturation.get('state')} ({', '.join(saturation.get('reason_codes', []))})"
            )
            manifest["deferred_workspace_count"] += 1
            manifest["workspace_results"].append(result)
            continue
        ledger_path = resolve_path(args.ledger_root) / f"{workspace_id}.runtime-ledger.jsonl"
        result["ledger_path"] = str(ledger_path)
        prior_attempts = terminal_attempt_count(ledger_path, workspace_id=workspace_id)
        result["attempt_number"] = prior_attempts + 1
        attempt_refusal = max_attempts_exceeded(record, prior_attempts=prior_attempts)
        if attempt_refusal is not None:
            result["outcome"] = "deferred"
            result["failure_reason"] = attempt_refusal
            manifest["deferred_workspace_count"] += 1
            manifest["workspace_results"].append(result)
            continue
        runtime_budget = get_runtime_budget_seconds(record)
        child_run_id = f"{run_id}.{workspace_id}.{result['attempt_number']}"
        child_run_dir = run_dir / workspace_id / child_run_id
        command = [
            sys.executable,
            str(runner),
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
            started_at,
            "--mode",
            args.mode,
            "--format",
            "json",
        ]
        if args.candidate_batch_fixture:
            command.extend(
                ["--candidate-batch-fixture", str(resolve_path(args.candidate_batch_fixture))]
            )
        if args.execution_run_fixture:
            command.extend(
                ["--execution-run-fixture", str(resolve_path(args.execution_run_fixture))]
            )
        if args.build_next_feedback_plan:
            command.append("--build-next-feedback-plan")
        append_ledger_event(
            ledger_path=ledger_path,
            workspace_id=workspace_id,
            run_id=child_run_id,
            event_type="command_start",
            occurred_at=started_at,
        )
        manifest["attempted_workspace_count"] += 1
        start = monotonic()
        proc = cycle_invoker(command)
        elapsed = round(monotonic() - start, 6)
        result["runtime_consumed_seconds"] = elapsed
        result["cycle_run_id"] = child_run_id
        result["cycle_manifest_path"] = str(child_run_dir / "topic-cycle-run.json")
        child_manifest_path = child_run_dir / "topic-cycle-run.json"
        if child_manifest_path.is_file():
            try:
                child_manifest = json.loads(child_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                child_manifest = None
            if isinstance(child_manifest, dict):
                cycle_event_id = child_manifest.get("cycle_event_id")
                if isinstance(cycle_event_id, str) and cycle_event_id:
                    result["cycle_event_id"] = cycle_event_id
        artifact_refs = [
            {"artifact_type": "topic_cycle_manifest", "path": result["cycle_manifest_path"]}
        ]
        if runtime_budget is not None and elapsed > runtime_budget:
            result["outcome"] = "failed"
            result["failure_reason"] = (
                f"cycle runtime {elapsed} exceeded run_budget.max_runtime_seconds {runtime_budget}"
            )
            manifest["failed_workspace_count"] += 1
            exit_code = 1
            append_ledger_event(
                ledger_path=ledger_path,
                workspace_id=workspace_id,
                run_id=child_run_id,
                event_type="command_failure",
                occurred_at=utc_now(),
                artifact_refs=artifact_refs,
                failure={"message": result["failure_reason"]},
            )
        elif proc.returncode == 0:
            result["outcome"] = "completed"
            manifest["completed_workspace_count"] += 1
            append_ledger_event(
                ledger_path=ledger_path,
                workspace_id=workspace_id,
                run_id=child_run_id,
                event_type="command_end",
                occurred_at=utc_now(),
                status="success",
                artifact_refs=artifact_refs,
            )
        else:
            result["outcome"] = "failed"
            result["failure_reason"] = (proc.stderr or proc.stdout).strip() or "topic cycle failed"
            manifest["failed_workspace_count"] += 1
            exit_code = 1
            append_ledger_event(
                ledger_path=ledger_path,
                workspace_id=workspace_id,
                run_id=child_run_id,
                event_type="command_failure",
                occurred_at=utc_now(),
                artifact_refs=artifact_refs,
                failure={"message": result["failure_reason"]},
            )
        result["scheduler_failure_state_record"] = str(ledger_path)
        manifest["workspace_results"].append(result)
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
    args = parse_args(argv)
    try:
        payload, exit_code = run_scheduled_cycles(args)
    except (ScheduledCycleError, runtime_ledger.RuntimeLedgerError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "text":
        sys.stdout.write(render_text(payload))
    else:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
