#!/usr/bin/env python3
"""Reduce runtime-ledger outcomes into scheduler failure-state recommendations."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "validators",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

import validate_scheduler_failure_state_reconciliation  # noqa: E402
import validate_topic_workspace_registry  # noqa: E402

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.runtime_ledger import DEFAULT_LEDGER_ROOT  # noqa: E402
from tools.common.scheduler_failure_reconciliation import (  # noqa: E402
    SchedulerFailureReconciliationError,
    derive_failure_state,
    read_runtime_ledger,
)
from tools.common.scheduler_failure_reconciliation_contract import (  # noqa: E402
    ENTRY_SCHEMA_VERSION,
    SCHEMA_VERSION,
)
from tools.common.topic_workspace_registry import (  # noqa: E402
    DEFAULT_REGISTRY_ENV,
    DEFAULT_REGISTRY_PATH,
    TopicWorkspaceRegistryError,
    discover_registry_path,
    resolve_workspaces,
)


class ReconciliationError(RuntimeError):
    """Raised when reconciliation inputs are invalid or unsafe."""


def resolve_path(raw_path: str | Path, *, base: Path | None = None) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base or Path.cwd()) / path).resolve()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    default_registry_display = DEFAULT_REGISTRY_PATH.relative_to(REPO_ROOT).as_posix()
    parser = argparse.ArgumentParser(
        description=(
            "Read runtime-ledger JSONL files and derive scheduler_policy.failure_state\n"
            "recommendations for topic workspaces. Read-only by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  reconcile_scheduler_failure_state.py --format json\n"
            "  reconcile_scheduler_failure_state.py --workspace-id alpha_subject --output-json report.json\n"
            "  reconcile_scheduler_failure_state.py --output-registry topic_workspaces.reconciled.json\n\n"
            "Registry resolution:\n"
            f"  --registry overrides {DEFAULT_REGISTRY_ENV}.\n"
            f"  Without either, the default is {default_registry_display}."
        ),
    )
    parser.add_argument(
        "--registry", help="Optional path to the topic workspace registry JSON file."
    )
    parser.add_argument(
        "--workspace-id",
        action="append",
        default=[],
        dest="workspace_ids",
        help="Optional workspace_id to reconcile. Repeat to target multiple workspaces.",
    )
    parser.add_argument(
        "--ledger-root",
        type=Path,
        default=REPO_ROOT / DEFAULT_LEDGER_ROOT,
        help="Directory containing per-workspace runtime-ledger JSONL files.",
    )
    parser.add_argument(
        "--generated-at", help="Optional timestamp override for deterministic tests."
    )
    parser.add_argument(
        "--output-json", type=Path, help="Optional path for the reconciliation JSON artifact."
    )
    parser.add_argument(
        "--output-registry",
        type=Path,
        help="Optional path for a full registry JSON copy with derived scheduler failure_state values applied.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def parse_timestamp(raw_value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconciliationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_registry_or_raise(registry_path: Path) -> None:
    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(
        registry_path
    )
    if exit_code != validate_topic_workspace_registry.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            raise ReconciliationError(
                errors[0].get("message", "topic workspace registry validation failed")
            )
        raise ReconciliationError("topic workspace registry validation failed")


def load_registry_json(registry_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"registry file could not be read: {registry_path}") from exc
    if not isinstance(payload, dict):
        raise ReconciliationError("topic workspace registry must be a JSON object")
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list):
        raise ReconciliationError("topic workspace registry workspaces must be an array")
    return payload


def latest_outcome_time(run_outcomes: list[Any], *, status: str) -> str | None:
    for outcome in reversed(run_outcomes):
        if outcome.status == status:
            return outcome.occurred_at
    return None


def reconciliation_entry(
    *,
    workspace: dict[str, Any],
    ledger_path: Path,
    events: list[dict[str, Any]],
    derived_failure_state: dict[str, Any] | None,
    reasons: list[str],
    run_outcomes: list[Any],
) -> dict[str, Any]:
    scheduler_policy = workspace.get("scheduler_policy")
    registry_failure_state = None
    if isinstance(scheduler_policy, dict) and isinstance(
        scheduler_policy.get("failure_state"), dict
    ):
        registry_failure_state = copy.deepcopy(scheduler_policy["failure_state"])
    recommendation = "replace" if registry_failure_state != derived_failure_state else "keep"
    return {
        "schema_version": ENTRY_SCHEMA_VERSION,
        "workspace_id": workspace["workspace_id"],
        "ledger_path": str(ledger_path),
        "ledger_event_count": len(events),
        "terminal_run_count": len(run_outcomes),
        "registry_failure_state": registry_failure_state,
        "derived_failure_state": copy.deepcopy(derived_failure_state),
        "recommendation": recommendation,
        "reasons": list(reasons),
        "latest_success_at": latest_outcome_time(run_outcomes, status="success"),
        "latest_failure_at": latest_outcome_time(run_outcomes, status="failure"),
    }


def build_reconciliation_payload(args: argparse.Namespace) -> dict[str, Any]:
    registry_path = discover_registry_path(args.registry)
    if not registry_path.exists():
        raise ReconciliationError(f"topic workspace registry not found: {registry_path}")
    if not registry_path.is_file():
        raise ReconciliationError(f"topic workspace registry is not a file: {registry_path}")
    validate_registry_or_raise(registry_path)
    generated_at = args.generated_at or utc_now()
    parse_timestamp(generated_at, label="generated_at")
    ledger_root = resolve_path(args.ledger_root)

    try:
        resolved_workspaces = resolve_workspaces(
            registry_path=registry_path, workspace_ids=args.workspace_ids
        )
    except TopicWorkspaceRegistryError as exc:
        raise ReconciliationError(str(exc)) from exc

    entries: list[dict[str, Any]] = []
    for workspace in resolved_workspaces:
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ReconciliationError("resolved workspace record is missing workspace_id")
        ledger_path = ledger_root / f"{workspace_id}.runtime-ledger.jsonl"
        events = read_runtime_ledger(ledger_path, workspace_id=workspace_id)
        scheduler_policy = workspace.get("scheduler_policy")
        run_budget = (
            scheduler_policy.get("run_budget") if isinstance(scheduler_policy, dict) else None
        )
        retry_policy = (
            scheduler_policy.get("retry_policy") if isinstance(scheduler_policy, dict) else None
        )
        current_failure_state = (
            copy.deepcopy(scheduler_policy.get("failure_state"))
            if isinstance(scheduler_policy, dict)
            and isinstance(scheduler_policy.get("failure_state"), dict)
            else None
        )
        derived_failure_state, reasons, run_outcomes = derive_failure_state(
            current_failure_state=current_failure_state,
            run_budget=run_budget if isinstance(run_budget, dict) else None,
            retry_policy=retry_policy if isinstance(retry_policy, dict) else None,
            events=events,
        )
        entries.append(
            reconciliation_entry(
                workspace=workspace,
                ledger_path=ledger_path,
                events=events,
                derived_failure_state=derived_failure_state,
                reasons=reasons,
                run_outcomes=run_outcomes,
            )
        )

    changed_count = sum(1 for entry in entries if entry["recommendation"] == "replace")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "registry_path": str(registry_path),
        "workspace_count": len(entries),
        "changed_count": changed_count,
        "unchanged_count": len(entries) - changed_count,
        "updated_registry_path": str(args.output_registry)
        if args.output_registry is not None
        else None,
        "entries": entries,
    }
    return payload


def apply_to_registry(
    *,
    registry_path: Path,
    output_registry_path: Path,
    entries: list[dict[str, Any]],
) -> None:
    if output_registry_path.resolve() == registry_path.resolve():
        raise ReconciliationError(
            "--output-registry must differ from --registry so the apply step stays deliberate"
        )

    registry_payload = load_registry_json(registry_path)
    by_workspace = {entry["workspace_id"]: entry for entry in entries}
    for workspace in registry_payload.get("workspaces", []):
        if not isinstance(workspace, dict):
            continue
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str):
            continue
        entry = by_workspace.get(workspace_id)
        if entry is None:
            continue
        scheduler_policy = workspace.get("scheduler_policy")
        if not isinstance(scheduler_policy, dict):
            scheduler_policy = {}
            workspace["scheduler_policy"] = scheduler_policy
        derived_failure_state = entry.get("derived_failure_state")
        if isinstance(derived_failure_state, dict):
            scheduler_policy["failure_state"] = copy.deepcopy(derived_failure_state)
        else:
            scheduler_policy.pop("failure_state", None)
            if not scheduler_policy:
                workspace.pop("scheduler_policy", None)

    atomic_write_json(output_registry_path, registry_payload)
    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(
        output_registry_path
    )
    if exit_code != validate_topic_workspace_registry.EXIT_PASS:
        errors = result.get("errors", [])
        first = (
            errors[0].get("message", "topic workspace registry validation failed")
            if errors
            else "topic workspace registry validation failed"
        )
        raise ReconciliationError(f"reconciled registry failed validation: {first}")


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version: {payload['schema_version']}",
        f"generated_at: {payload['generated_at']}",
        f"registry_path: {payload['registry_path']}",
        f"workspace_count: {payload['workspace_count']}",
        f"changed_count: {payload['changed_count']}",
        f"unchanged_count: {payload['unchanged_count']}",
    ]
    if payload.get("updated_registry_path"):
        lines.append(f"updated_registry_path: {payload['updated_registry_path']}")
    lines.append("entries:")
    for entry in payload.get("entries", []):
        lines.append(f"  - workspace_id: {entry['workspace_id']}")
        lines.append(f"    recommendation: {entry['recommendation']}")
        lines.append(f"    ledger_event_count: {entry['ledger_event_count']}")
        lines.append(f"    terminal_run_count: {entry['terminal_run_count']}")
        lines.append(f"    latest_success_at: {entry['latest_success_at']}")
        lines.append(f"    latest_failure_at: {entry['latest_failure_at']}")
        for reason in entry.get("reasons", []):
            lines.append(f"    reason: {reason}")
    return "\n".join(lines) + "\n"


def maybe_write_payload(output_json: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(output_json, payload)
    report, exit_code = (
        validate_scheduler_failure_state_reconciliation.validate_scheduler_failure_state_reconciliation(
            output_json
        )
    )
    if exit_code != validate_scheduler_failure_state_reconciliation.EXIT_PASS:
        errors = report.get("errors", [])
        first = (
            errors[0].get("message", "reconciliation payload failed validation")
            if errors
            else "reconciliation payload failed validation"
        )
        raise ReconciliationError(first)


def main(argv: list[str] | None = None) -> int:
    args = parse_args()
    try:
        payload = build_reconciliation_payload(args)
        registry_path = Path(payload["registry_path"])
        if args.output_registry is not None:
            apply_to_registry(
                registry_path=registry_path,
                output_registry_path=args.output_registry,
                entries=payload["entries"],
            )
        if args.output_json is not None:
            maybe_write_payload(args.output_json, payload)
        if args.format == "json":
            sys.stdout.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
        else:
            sys.stdout.write(render_text(payload))
    except (ReconciliationError, SchedulerFailureReconciliationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
