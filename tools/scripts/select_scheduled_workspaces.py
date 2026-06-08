#!/usr/bin/env python3
"""Select scheduler-eligible topic workspaces from the local registry.

This script does not execute workspace work. It resolves workspaces from a
validated registry, emits a selection report, and can append planner records so
operators can audit unattended scheduling decisions before a later runner acts.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
SCHEDULED_POSTURE = "scheduled"
MANUAL_POSTURE = "manual"
DEFAULT_ALLOWED_POSTURES = {SCHEDULED_POSTURE}
PLANNED_RUN_SCHEMA_VERSION = "planned-run.v1"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

import validate_subject_manifest  # noqa: E402

from tools.common import topic_saturation  # noqa: E402
from tools.common.selection_explanation import build_scheduler_selection_explanation  # noqa: E402
from tools.common.topic_workspace_registry import (  # noqa: E402
    DEFAULT_REGISTRY_ENV,
    DEFAULT_REGISTRY_PATH,
    TopicWorkspaceRegistryError,
    discover_registry_path,
    load_registry_json,
    resolve_workspaces,
)
from tools.source_db_tools import canonical_store  # noqa: E402
from tools.validators import validate_topic_workspace_registry  # noqa: E402


class SelectionError(RuntimeError):
    """Raised when scheduler selection inputs are invalid or unsafe."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def positive_int(raw_value: str, *, option_name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{option_name} must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"{option_name} must be at least 1")
    return value


def positive_int_arg(option_name: str):
    return lambda raw_value: positive_int(raw_value, option_name=option_name)


def parse_timestamp(raw_value: str, *, label: str) -> datetime:
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelectionError(f"{label} must be an ISO-8601 timestamp") from exc


def normalize_planned_at(raw_value: str | None, *, label: str) -> str:
    parsed = parse_timestamp(raw_value or utc_now(), label=label)
    if parsed.tzinfo is None:
        raise SelectionError(f"{label} must include a timezone")
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    default_registry_display = DEFAULT_REGISTRY_PATH.relative_to(REPO_ROOT).as_posix()
    parser = argparse.ArgumentParser(
        description=(
            "Resolve scheduler-eligible topic workspaces from the topic workspace\n"
            "registry so cron can iterate structured subjects instead of scanning paths."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  select_scheduled_workspaces.py --format json\n"
            "  select_scheduled_workspaces.py --format text --limit 1\n"
            "  select_scheduled_workspaces.py --workspace-id alpha_subject --include-manual\n\n"
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
        help="Optional workspace_id to include. Repeat to target multiple workspaces.",
    )
    parser.add_argument(
        "--include-manual",
        action="store_true",
        help="Include active workspaces whose schedule_posture is manual.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional maximum number of eligible workspaces to return, in registry order.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format describing selected and skipped workspaces.",
    )
    parser.add_argument(
        "--planned-runs-jsonl",
        type=Path,
        help="Optional JSONL path to append planned-run records for every selected and skipped workspace.",
    )
    parser.add_argument(
        "--relaxed-planned-runs-write",
        action="store_true",
        help="Skip fsync when appending planned-run records for scratch or test runs.",
    )
    parser.add_argument(
        "--planner-run-id",
        default=None,
        help="Identifier for this selector planning pass. Defaults to a generated ID.",
    )
    parser.add_argument(
        "--planned-at",
        help="Override planned_at timestamp for deterministic tests or replay reports.",
    )
    parser.add_argument(
        "--run-budget-max-attempts",
        type=positive_int_arg("--run-budget-max-attempts"),
        help="Maximum runner attempts to record in each planned-run budget.",
    )
    parser.add_argument(
        "--run-budget-max-runtime-seconds",
        type=positive_int_arg("--run-budget-max-runtime-seconds"),
        help="Optional maximum runner wall-clock seconds to record in each planned-run budget.",
    )
    parser.add_argument("--db", help="Canonical SQLite store for optional saturation evaluation.")
    parser.add_argument(
        "--saturation-policy",
        help="Optional topic saturation policy JSON. If omitted, scheduler behavior is unchanged.",
    )
    parser.add_argument(
        "--include-saturated",
        action="store_true",
        help="Allow saturated/cooldown workspaces to be selected and record the override in planned-run output.",
    )
    parser.add_argument(
        "--ignore-saturation",
        action="store_true",
        help="Disable saturation evaluation even when --saturation-policy is supplied.",
    )
    return parser.parse_args()


def _hash_file_bytes(payload: Path) -> str:
    return hashlib.sha256(payload.read_bytes()).hexdigest()


def _derive_planner_run_id(
    *,
    planned_at: str,
    registry_path: Path,
    workspace_ids: list[str],
    args: argparse.Namespace,
) -> str:
    policy = {
        "run_budget_max_attempts": args.run_budget_max_attempts,
        "run_budget_max_runtime_seconds": args.run_budget_max_runtime_seconds,
        "include_manual": args.include_manual,
        "limit": args.limit,
        "saturation_policy": str(args.saturation_policy) if args.saturation_policy else None,
        "include_saturated": args.include_saturated,
        "ignore_saturation": args.ignore_saturation,
        "workspace_ids": workspace_ids,
    }
    if args.saturation_policy:
        try:
            policy["saturation_policy_sha256"] = _hash_file_bytes(Path(args.saturation_policy))
        except OSError:
            policy["saturation_policy_sha256"] = None

    planner_seed = {
        "planned_at": planned_at,
        "registry_sha256": _hash_file_bytes(registry_path),
        "policy": policy,
    }
    signature = json.dumps(
        planner_seed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"planner-{hashlib.sha256(signature).hexdigest()[:16]}"


def validate_registry_or_raise(registry_path: Path) -> dict[str, Any]:
    try:
        registry_payload = load_registry_json(registry_path)
    except TopicWorkspaceRegistryError as exc:
        raise SelectionError(str(exc)) from exc
    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(
        registry_path,
        payload=registry_payload,
    )
    if exit_code != validate_topic_workspace_registry.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            raise SelectionError(
                errors[0].get("message", "topic workspace registry validation failed")
            )
        raise SelectionError("topic workspace registry validation failed")
    return registry_payload


def workspace_output_entry(workspace: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(workspace, dict):
        raise TypeError("workspace entry must be a JSON object")
    payload = {
        "workspace_id": workspace["workspace_id"],
        "topic_label": workspace.get("topic_label"),
        "domain_pack": workspace.get("domain_pack"),
        "lifecycle_state": workspace.get("lifecycle_state"),
        "schedule_posture": workspace.get("schedule_posture"),
        "workspace_policy_class": workspace.get("workspace_policy_class"),
        "workspace_root": workspace.get("workspace_root"),
        "resolved_workspace_root": str(workspace["resolved_workspace_root"]),
    }

    if "default_subject_manifest" in workspace:
        payload["default_subject_manifest"] = workspace["default_subject_manifest"]
    if "resolved_default_subject_manifest" in workspace:
        payload["resolved_default_subject_manifest"] = str(
            workspace["resolved_default_subject_manifest"]
        )
    if "scheduler_policy" in workspace:
        payload["scheduler_policy"] = copy.deepcopy(workspace["scheduler_policy"])

    return payload


def workspace_selection_summary(
    entry: dict[str, Any], *, reasons: list[str] | None = None
) -> dict[str, Any]:
    summary = {
        "workspace_id": entry["workspace_id"],
        "topic_label": entry.get("topic_label"),
        "lifecycle_state": entry.get("lifecycle_state"),
        "schedule_posture": entry.get("schedule_posture"),
    }
    saturation = entry.get("saturation")
    if isinstance(saturation, dict):
        summary["saturation"] = copy.deepcopy(saturation)
    if reasons:
        summary["reasons"] = list(reasons)
    return summary


def effective_scheduler_policy(
    workspace: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    raw_policy = workspace.get("scheduler_policy")
    policy = copy.deepcopy(raw_policy) if isinstance(raw_policy, dict) else {}

    run_budget = dict(policy.get("run_budget", {}))
    max_attempts = args.run_budget_max_attempts
    if max_attempts is None:
        max_attempts = run_budget.get("max_attempts", 1)
    run_budget["max_attempts"] = max_attempts

    if args.run_budget_max_runtime_seconds is not None:
        run_budget["max_runtime_seconds"] = args.run_budget_max_runtime_seconds
    elif "max_runtime_seconds" not in run_budget:
        run_budget.pop("max_runtime_seconds", None)

    policy["run_budget"] = run_budget
    return policy


def derived_next_retry_at(policy: dict[str, Any]) -> str | None:
    failure_state = policy.get("failure_state")
    retry_policy = policy.get("retry_policy")
    if not isinstance(failure_state, dict):
        return None
    explicit_next_retry = failure_state.get("next_retry_at")
    if isinstance(explicit_next_retry, str) and explicit_next_retry:
        return explicit_next_retry
    if not isinstance(retry_policy, dict):
        return None
    backoff_seconds = retry_policy.get("backoff_seconds")
    last_failure_at = failure_state.get("last_failure_at")
    if (
        not isinstance(backoff_seconds, int)
        or not isinstance(last_failure_at, str)
        or not last_failure_at
    ):
        return None
    retry_at = parse_timestamp(
        last_failure_at, label="scheduler_policy.failure_state.last_failure_at"
    ) + timedelta(seconds=backoff_seconds)
    return retry_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def scheduler_ineligibility_reasons(
    workspace: dict[str, Any], *, include_manual: bool
) -> list[str]:
    reasons: list[str] = []

    lifecycle_state = workspace.get("lifecycle_state")
    if lifecycle_state != "active":
        reasons.append(
            f"lifecycle_state is {lifecycle_state!r}; scheduler only selects active workspaces"
        )

    schedule_posture = workspace.get("schedule_posture")
    allowed_postures = set(DEFAULT_ALLOWED_POSTURES)
    if include_manual:
        allowed_postures.add(MANUAL_POSTURE)
    if schedule_posture not in allowed_postures:
        if schedule_posture == MANUAL_POSTURE:
            reasons.append("schedule_posture is manual; pass --include-manual to include it")
        else:
            reasons.append(
                f"schedule_posture is {schedule_posture!r}; scheduler only selects "
                + ", ".join(sorted(allowed_postures))
            )

    if "resolved_default_subject_manifest" not in workspace:
        reasons.append(
            "default_subject_manifest is missing or unresolved; scheduled runs need an explicit manifest"
        )

    return reasons


def scheduler_policy_ineligibility_reasons(
    workspace: dict[str, Any],
    *,
    args: argparse.Namespace,
    planned_at: datetime,
) -> list[str]:
    policy = effective_scheduler_policy(workspace, args)
    failure_state = policy.get("failure_state")
    if not isinstance(failure_state, dict):
        return []

    reasons: list[str] = []
    status = failure_state.get("status")
    attempt_count = failure_state.get("attempt_count")
    run_budget = policy.get("run_budget", {})
    retry_policy = policy.get("retry_policy", {})

    if status == "blocked":
        blocked_reason = failure_state.get("blocked_reason")
        if isinstance(blocked_reason, str) and blocked_reason:
            reasons.append(f"failure_state is blocked: {blocked_reason}")
        else:
            reasons.append("failure_state is blocked")

    max_attempts = run_budget.get("max_attempts")
    if (
        isinstance(max_attempts, int)
        and isinstance(attempt_count, int)
        and attempt_count >= max_attempts
    ):
        reasons.append(
            f"attempt_count {attempt_count} reached run_budget.max_attempts {max_attempts}"
        )

    max_retryable_failures = retry_policy.get("max_retryable_failures")
    if (
        status == "retryable"
        and isinstance(max_retryable_failures, int)
        and isinstance(attempt_count, int)
        and attempt_count > max_retryable_failures
    ):
        reasons.append(
            "retryable failure count "
            f"{attempt_count} exceeded retry_policy.max_retryable_failures {max_retryable_failures}"
        )

    if status == "retryable":
        next_retry_at = derived_next_retry_at(policy)
        if next_retry_at is not None and planned_at < parse_timestamp(
            next_retry_at,
            label="scheduler_policy.failure_state.next_retry_at",
        ):
            reasons.append(f"retry backoff active until {next_retry_at}")

    return reasons


def cadence_reason(entry: dict[str, Any]) -> str:
    schedule_posture = entry.get("schedule_posture")
    if not isinstance(schedule_posture, str) or not schedule_posture:
        return "schedule_posture:unknown"
    return f"schedule_posture:{schedule_posture}"


def planned_run_record(
    *,
    entry: dict[str, Any],
    decision: str,
    registry_path: Path,
    planner_run_id: str,
    planned_at: str,
    run_budget: dict[str, Any],
    retry_policy: dict[str, Any] | None,
    failure_state: dict[str, Any] | None,
) -> dict[str, Any]:
    reasons = list(entry.get("reasons", []))
    record = {
        "schema_version": PLANNED_RUN_SCHEMA_VERSION,
        "planner_run_id": planner_run_id,
        "planned_run_id": f"{planner_run_id}:{entry['workspace_id']}",
        "planned_at": planned_at,
        "registry_path": str(registry_path),
        "workspace_id": entry["workspace_id"],
        "decision": decision,
        "cadence_reason": cadence_reason(entry),
        "skipped_reason": reasons[0] if reasons else None,
        "skipped_reasons": reasons,
        "run_budget": run_budget,
        "retry_policy": retry_policy,
        "failure_state": failure_state,
        "saturation": entry.get("saturation") if isinstance(entry.get("saturation"), dict) else None,
        "saturation_override": bool(entry.get("saturation_override", False)),
        "workspace_root": entry.get("workspace_root"),
        "resolved_workspace_root": entry.get("resolved_workspace_root"),
        "default_subject_manifest": entry.get("default_subject_manifest"),
        "resolved_default_subject_manifest": entry.get("resolved_default_subject_manifest"),
    }
    return record


def build_planned_run_records(
    *,
    selected: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    registry_path: Path,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    if args.planned_at is None:
        raise RuntimeError("planned_at must be set before building planned-run records")
    records = []
    for entry, decision in [(entry, "selected") for entry in selected] + [
        (entry, "skipped") for entry in skipped
    ]:
        policy = effective_scheduler_policy(entry, args)
        records.append(
            planned_run_record(
                entry=entry,
                decision=decision,
                registry_path=registry_path,
                planner_run_id=args.planner_run_id,
                planned_at=args.planned_at,
                run_budget=policy["run_budget"],
                retry_policy=policy.get("retry_policy")
                if isinstance(policy.get("retry_policy"), dict)
                else None,
                failure_state=policy.get("failure_state")
                if isinstance(policy.get("failure_state"), dict)
                else None,
            )
        )
    return records


def load_subject_id_from_manifest(
    manifest_path: str | None,
    *,
    allow_unresolved: bool = False,
) -> str | None:
    if not isinstance(manifest_path, str) or not manifest_path:
        return None
    path = Path(manifest_path)
    try:
        payload, errors, _ = validate_subject_manifest.load_json_object(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if allow_unresolved:
            return None
        raise SelectionError(f"default subject manifest could not be read: {path}") from exc
    if payload is None:
        if allow_unresolved:
            return None
        if errors:
            message = errors[0].get("message", "default subject manifest could not be read")
        else:
            message = "default subject manifest could not be read"
        raise SelectionError(f"default subject manifest failed validation: {message}")
    subject_id = payload.get("subject_id")
    if isinstance(subject_id, str) and subject_id:
        return subject_id
    if allow_unresolved:
        return None
    raise SelectionError(f"default subject manifest failed validation: missing subject_id: {path}")


def saturation_context(
    args: argparse.Namespace,
) -> tuple[topic_saturation.Policy | None, sqlite3.Connection | None]:
    if args.ignore_saturation or not args.saturation_policy:
        return None, None
    if not args.db:
        raise SelectionError("--db is required when --saturation-policy is supplied")
    try:
        policy = topic_saturation.load_policy(args.saturation_policy)
        db_path = canonical_store.resolve_db_path(args.db)
        canonical_store.check_canonical_store(db_path)
        conn = canonical_store.connect_existing_read_only(db_path)
    except (
        topic_saturation.TopicSaturationError,
        canonical_store.CanonicalStoreError,
        sqlite3.Error,
    ) as exc:
        raise SelectionError(f"saturation policy/store is not usable: {exc}") from exc
    return policy, conn


def workspace_allows_unresolved_subject_manifest(workspace: dict[str, Any]) -> bool:
    scheduler_policy = workspace.get("scheduler_policy")
    if not isinstance(scheduler_policy, dict):
        return False
    extensions = scheduler_policy.get("extensions")
    if not isinstance(extensions, dict):
        return False
    return extensions.get("allow_unresolved_subject_manifest") is True


def resolve_saturation_subject_id(workspace: dict[str, Any]) -> str | None:
    subject_id = workspace.get("resolved_default_subject_id")
    if isinstance(subject_id, str) and subject_id:
        return subject_id
    return load_subject_id_from_manifest(
        workspace.get("resolved_default_subject_manifest")
        or workspace.get("default_subject_manifest"),
        allow_unresolved=workspace_allows_unresolved_subject_manifest(workspace),
    )


def attach_saturation_batch(
    entries: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    policy: topic_saturation.Policy | None,
    conn: sqlite3.Connection | None,
    planned_at: str,
) -> None:
    if policy is None or conn is None:
        return

    workspace_subject_pairs: list[tuple[str, str]] = []
    subject_id_by_workspace_id: dict[str, str | None] = {}
    for workspace, entry in entries:
        workspace_id = str(workspace["workspace_id"])
        subject_id = resolve_saturation_subject_id(workspace)
        subject_id_by_workspace_id[workspace_id] = subject_id
        if subject_id is None:
            entry["saturation"] = {
                "schema_version": topic_saturation.SCHEMA_VERSION,
                "workspace_id": entry.get("workspace_id"),
                "subject_id": None,
                "policy_id": policy.policy_id,
                "state": "not_evaluated",
                "scheduler_action": "run",
                "reason_codes": ["subject_unresolved"],
                "recent_yield_summary": topic_saturation.empty_summary(),
            }
            continue
        workspace_subject_pairs.append((workspace_id, subject_id))

    saturation_by_workspace_id = topic_saturation.evaluate_saturations(
        conn,
        workspace_subject_pairs=workspace_subject_pairs,
        policy=policy,
        evaluated_at=planned_at,
    )
    for workspace, entry in entries:
        workspace_id = str(workspace["workspace_id"])
        subject_id = subject_id_by_workspace_id.get(workspace_id)
        if subject_id is None:
            continue
        entry["saturation"] = copy.deepcopy(saturation_by_workspace_id[workspace_id])


def attach_saturation(
    entry: dict[str, Any],
    *,
    workspace: dict[str, Any],
    policy: topic_saturation.Policy | None,
    conn: sqlite3.Connection | None,
    planned_at: str,
) -> None:
    attach_saturation_batch(
        [(workspace, entry)],
        policy=policy,
        conn=conn,
        planned_at=planned_at,
    )


def saturation_ineligibility_reasons(
    entry: dict[str, Any], *, include_saturated: bool
) -> list[str]:
    saturation = entry.get("saturation")
    if not isinstance(saturation, dict):
        return []
    action = saturation.get("scheduler_action")
    if include_saturated:
        if action in {"halt", "cooldown"}:
            entry["saturation_override"] = True
        return []
    if action == "halt":
        return [f"saturation_state is halted: {', '.join(saturation.get('reason_codes', []))}"]
    if action == "cooldown":
        next_eligible = saturation.get("next_eligible_cycle")
        suffix = f" until cycle {next_eligible}" if next_eligible is not None else ""
        return [
            f"saturation_state is cooldown{suffix}: {', '.join(saturation.get('reason_codes', []))}"
        ]
    return []


def append_planned_run_records(
    path: Path,
    records: list[dict[str, Any]],
    *,
    sync: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:

            def read_existing_ids(path: Path) -> set[str]:
                existing: set[str] = set()
                if not path.exists() or path.stat().st_size == 0:
                    return existing
                with path.open("r", encoding="utf-8") as reader:
                    for line in reader:
                        if not line.strip():
                            continue
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        planned_run_id = (
                            parsed.get("planned_run_id") if isinstance(parsed, dict) else None
                        )
                        if isinstance(planned_run_id, str):
                            existing.add(planned_run_id)
                return existing

            existing_ids = read_existing_ids(path)
            seen_ids: set[str] = set()
            needs_leading_newline = False
            if path.exists() and path.stat().st_size > 0:
                with path.open("rb") as reader:
                    reader.seek(-1, os.SEEK_END)
                    needs_leading_newline = reader.read(1) != b"\n"
            if needs_leading_newline:
                handle.write("\n")
            for record in records:
                planned_run_id = record.get("planned_run_id")
                if not isinstance(planned_run_id, str):
                    continue
                if planned_run_id in existing_ids or planned_run_id in seen_ids:
                    continue
                seen_ids.add(planned_run_id)
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_selection_payload(args: argparse.Namespace) -> dict[str, Any]:
    registry_path = discover_registry_path(args.registry)
    if not registry_path.exists():
        raise SelectionError(f"topic workspace registry not found: {registry_path}")
    if not registry_path.is_file():
        raise SelectionError(f"topic workspace registry is not a file: {registry_path}")
    if args.limit is not None and args.limit < 1:
        raise SelectionError("--limit must be at least 1")

    registry_payload = validate_registry_or_raise(registry_path)
    try:
        resolved_workspaces = resolve_workspaces(
            registry_path=registry_path,
            workspace_ids=args.workspace_ids,
            registry_payload=registry_payload,
        )
    except TopicWorkspaceRegistryError as exc:
        raise SelectionError(str(exc)) from exc

    planned_at = normalize_planned_at(
        args.planned_at,
        label="--planned-at" if args.planned_at else "generated planned_at",
    )
    args.planned_at = planned_at

    saturation_policy, saturation_conn = saturation_context(args)
    try:
        prepared_entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for workspace in resolved_workspaces:
            try:
                entry = workspace_output_entry(workspace)
            except (KeyError, TypeError) as exc:
                raise SelectionError(f"workspace record is invalid: {exc}") from exc
            prepared_entries.append((workspace, entry))
        attach_saturation_batch(
            prepared_entries,
            policy=saturation_policy,
            conn=saturation_conn,
            planned_at=planned_at,
        )
        eligible: list[dict[str, Any]] = []
        deprioritized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for workspace, entry in prepared_entries:
            reasons = scheduler_ineligibility_reasons(workspace, include_manual=args.include_manual)
            reasons.extend(
                scheduler_policy_ineligibility_reasons(
                    workspace,
                    args=args,
                    planned_at=parse_timestamp(planned_at, label="planned_at"),
                )
            )
            reasons.extend(
                saturation_ineligibility_reasons(entry, include_saturated=args.include_saturated)
            )
            if reasons:
                entry["reasons"] = reasons
                skipped.append(entry)
            elif (
                isinstance(entry.get("saturation"), dict)
                and entry["saturation"].get("scheduler_action") == "deprioritize"
            ):
                entry["saturation_deprioritized"] = True
                deprioritized.append(entry)
            else:
                eligible.append(entry)
    finally:
        if saturation_conn is not None:
            saturation_conn.close()

    ranked_eligible = eligible + deprioritized
    selected = ranked_eligible
    if args.limit is not None and len(ranked_eligible) > args.limit:
        selected = ranked_eligible[: args.limit]
        for deferred in ranked_eligible[args.limit :]:
            deferred_entry = dict(deferred)
            if deferred_entry.get("saturation_deprioritized"):
                deferred_entry["reasons"] = [
                    "selection limit reached after saturation deprioritization"
                ]
            else:
                deferred_entry["reasons"] = ["selection limit reached"]
            skipped.append(deferred_entry)

    selected_workspace_ids = [workspace["workspace_id"] for workspace in selected]
    if args.planner_run_id is None:
        args.planner_run_id = _derive_planner_run_id(
            planned_at=args.planned_at,
            registry_path=registry_path,
            workspace_ids=selected_workspace_ids,
            args=args,
        )

    planned_records = build_planned_run_records(
        selected=selected,
        skipped=skipped,
        registry_path=registry_path,
        args=args,
    )
    selection_explanation = build_scheduler_selection_explanation(
        planner_run_id=args.planner_run_id,
        planned_at=args.planned_at,
        registry_path=str(registry_path),
        selected_workspaces=selected,
        skipped_workspaces=skipped,
        limit=args.limit,
        include_manual=args.include_manual,
        include_saturated=args.include_saturated,
        ignore_saturation=args.ignore_saturation,
        saturation_policy=None if saturation_policy is None else saturation_policy.policy_id,
    )
    for record in planned_records:
        record["selection_explanation_id"] = selection_explanation["explanation_id"]
    if args.planned_runs_jsonl is not None:
        append_planned_run_records(
            args.planned_runs_jsonl,
            planned_records,
            sync=not args.relaxed_planned_runs_write,
        )

    return {
        "registry_path": str(registry_path),
        "requested_workspace_ids": list(args.workspace_ids),
        "include_manual": args.include_manual,
        "saturation_policy": None if saturation_policy is None else saturation_policy.policy_id,
        "include_saturated": args.include_saturated,
        "ignore_saturation": args.ignore_saturation,
        "limit": args.limit,
        "planner_run_id": args.planner_run_id,
        "planned_run_record_count": len(planned_records),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "selected_workspaces": [workspace_selection_summary(entry) for entry in selected],
        "skipped_workspaces": [
            workspace_selection_summary(entry, reasons=entry.get("reasons", []))
            for entry in skipped
        ],
        "planned_run_records": planned_records,
        "selection_explanation": selection_explanation,
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"registry_path={payload['registry_path']}",
        f"selected_count={payload['selected_count']}",
        f"skipped_count={payload['skipped_count']}",
    ]

    planned_by_workspace_id = {
        str(record.get("workspace_id") or ""): record
        for record in payload["planned_run_records"]
        if isinstance(record, dict)
    }
    for index, workspace in enumerate(payload["selected_workspaces"]):
        lines.append(f"selected[{index}].workspace_id={workspace['workspace_id']}")
        record = planned_by_workspace_id.get(workspace["workspace_id"], {})
        lines.append(f"selected[{index}].workspace_root={record.get('resolved_workspace_root', '-')}")
        manifest = record.get("resolved_default_subject_manifest", "-")
        lines.append(f"selected[{index}].subject_manifest={manifest}")

    for index, workspace in enumerate(payload["skipped_workspaces"]):
        lines.append(f"skipped[{index}].workspace_id={workspace['workspace_id']}")
        lines.append(f"skipped[{index}].reasons={'; '.join(workspace['reasons'])}")

    for index, record in enumerate(payload["planned_run_records"]):
        lines.append(f"planned_run[{index}].workspace_id={record['workspace_id']}")
        lines.append(f"planned_run[{index}].decision={record['decision']}")
        if record["skipped_reason"]:
            lines.append(f"planned_run[{index}].skipped_reason={record['skipped_reason']}")

    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = build_selection_payload(args)
    except SelectionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
