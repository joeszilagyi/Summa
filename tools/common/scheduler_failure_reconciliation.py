"""Helpers for reducing runtime-ledger outcomes into scheduler failure state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


FAILURE_EVENT_TYPE = "command_failure"
SUCCESS_EVENT_TYPE = "command_end"
TERMINAL_EVENT_TYPES = {FAILURE_EVENT_TYPE, SUCCESS_EVENT_TYPE}
SUCCESS_STATUSES = {"pass", "passed", "success", "succeeded", "ok"}
FAILURE_STATUSES = {"fail", "failed", "error"}


class SchedulerFailureReconciliationError(RuntimeError):
    """Raised when runtime-ledger input cannot be reduced safely."""


@dataclass(frozen=True)
class RunOutcome:
    """Terminal outcome for one runtime-ledger run_id."""

    run_id: str
    status: str
    occurred_at: str
    failure_reason: str | None = None


def parse_timestamp(raw_value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchedulerFailureReconciliationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def read_runtime_ledger(path: Path, *, workspace_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise SchedulerFailureReconciliationError(f"runtime ledger path is not a file: {path}")

    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise SchedulerFailureReconciliationError(
                f"runtime ledger {path} contains invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise SchedulerFailureReconciliationError(
                f"runtime ledger {path} contains a non-object record on line {line_number}"
            )
        if payload.get("workspace_id") != workspace_id:
            continue
        occurred_at = payload.get("occurred_at")
        if not isinstance(occurred_at, str) or not occurred_at.strip():
            raise SchedulerFailureReconciliationError(
                f"runtime ledger {path} record on line {line_number} is missing occurred_at"
            )
        parse_timestamp(occurred_at, label=f"{path}:{line_number}:occurred_at")
        events.append(payload)
    events.sort(key=lambda event: (event["occurred_at"], str(event.get("event_id", ""))))
    return events


def summarize_run_outcomes(events: list[dict[str, Any]]) -> list[RunOutcome]:
    runs: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise SchedulerFailureReconciliationError("runtime-ledger event is missing run_id")
        runs.setdefault(run_id, []).append(event)

    outcomes: list[RunOutcome] = []
    for run_id, run_events in runs.items():
        terminal_events = [event for event in run_events if event.get("event_type") in TERMINAL_EVENT_TYPES]
        if not terminal_events:
            continue
        terminal_events.sort(key=lambda event: (event["occurred_at"], str(event.get("event_id", ""))))
        last_event = terminal_events[-1]
        event_type = last_event.get("event_type")
        occurred_at = str(last_event["occurred_at"])
        if event_type == FAILURE_EVENT_TYPE:
            outcomes.append(
                RunOutcome(
                    run_id=run_id,
                    status="failure",
                    occurred_at=occurred_at,
                    failure_reason=extract_failure_reason(last_event),
                )
            )
            continue

        status = str(last_event.get("status", "")).strip().casefold()
        if status and status in FAILURE_STATUSES:
            outcomes.append(
                RunOutcome(
                    run_id=run_id,
                    status="failure",
                    occurred_at=occurred_at,
                    failure_reason=f"command_end status {status}",
                )
            )
            continue
        outcomes.append(
            RunOutcome(
                run_id=run_id,
                status="success",
                occurred_at=occurred_at,
            )
        )

    outcomes.sort(key=lambda outcome: (outcome.occurred_at, outcome.run_id))
    return outcomes


def extract_failure_reason(event: dict[str, Any]) -> str:
    failure = event.get("failure")
    if isinstance(failure, dict):
        for key in ("message", "reason", "error"):
            value = failure.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    command = event.get("command")
    if isinstance(command, str) and command.strip():
        return f"{command.strip()} failed"
    return "runtime-ledger command failure"


def derive_failure_state(
    *,
    current_failure_state: dict[str, Any] | None,
    run_budget: dict[str, Any] | None,
    retry_policy: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str], list[RunOutcome]]:
    run_outcomes = summarize_run_outcomes(events)
    if not run_outcomes:
        return current_failure_state, ["no terminal runtime-ledger outcomes found"], run_outcomes

    latest_outcome = run_outcomes[-1]
    if latest_outcome.status == "success":
        return {"status": "healthy", "attempt_count": 0}, ["latest terminal run recovered successfully"], run_outcomes

    consecutive_failures: list[RunOutcome] = []
    for outcome in reversed(run_outcomes):
        if outcome.status == "success":
            break
        consecutive_failures.append(outcome)

    attempt_count = len(consecutive_failures)
    newest_failure = consecutive_failures[0]
    derived: dict[str, Any] = {
        "status": "retryable",
        "attempt_count": attempt_count,
        "last_failure_at": newest_failure.occurred_at,
        "last_failure_reason": newest_failure.failure_reason or "runtime-ledger command failure",
    }
    reasons = [f"{attempt_count} consecutive terminal runtime failure(s) since the last success"]

    backoff_seconds = value_as_positive_int(retry_policy, "backoff_seconds")
    if backoff_seconds is not None:
        next_retry = parse_timestamp(newest_failure.occurred_at, label="last_failure_at") + timedelta(
            seconds=backoff_seconds
        )
        derived["next_retry_at"] = next_retry.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        reasons.append(f"retry backoff derived from retry_policy.backoff_seconds {backoff_seconds}")

    blocked_reasons: list[str] = []
    max_attempts = value_as_positive_int(run_budget, "max_attempts")
    if max_attempts is not None and attempt_count >= max_attempts:
        blocked_reasons.append(f"attempt_count {attempt_count} reached run_budget.max_attempts {max_attempts}")

    max_retryable_failures = value_as_positive_int(retry_policy, "max_retryable_failures")
    if max_retryable_failures is not None and attempt_count > max_retryable_failures:
        blocked_reasons.append(
            "retryable failure count "
            f"{attempt_count} exceeded retry_policy.max_retryable_failures {max_retryable_failures}"
        )

    if blocked_reasons:
        derived["status"] = "blocked"
        derived["blocked_reason"] = "; ".join(blocked_reasons)
        reasons.extend(blocked_reasons)

    return derived, reasons, run_outcomes


def value_as_positive_int(payload: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value
