#!/usr/bin/env python3
"""Emit a read-only local installation doctor report.

The doctor inspects repo-local signals only. It does not mutate files, DBs,
remotes, generated artifacts, or workspaces, and it redacts path-like and
secret-looking details by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = "local-doctor-report.v1"
FINDING_CLASSES = {"advisory_only", "operator_action_required", "auto_remediable_candidate"}
SECRET_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key)=([^\\s]+)")
DEFAULT_STALE_LOCK_SECONDS = 60 * 60

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common import workspace_lock  # noqa: E402
from tools.common.operator_text import (  # noqa: E402
    format_operator_text_value,
    strip_terminal_escapes,
)
from tools.common.subprocess_capture import (  # noqa: E402
    command_output_excerpt,
    run_streaming_command,
)
from tools.common.topic_workspace_registry import (  # noqa: E402
    TopicWorkspaceRegistryError,
    discover_registry_path,
    resolve_workspaces,
)
from tools.source_db_tools import (  # noqa: E402
    canonical_graph_closure,
    canonical_store,
    loop_health,
)

VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

from tools.validators import (  # noqa: E402
    validate_crown_jewel_store_policy,
    validate_migration_ledger,
    validate_topic_workspace_registry,
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    text = strip_terminal_escapes(value)
    text = SECRET_RE.sub(r"\1=[redacted]", text)
    text = re.sub(r"(?i)\bbegin secret\b", "[redacted]", text)
    text = re.sub(r"(?i)\bignore previous instructions\b", "[redacted]", text)
    text = re.sub(r"/(?:home|Users|tmp)/\S+", "[redacted-path]", text)
    return text


def dict_or_empty(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def finding(code: str, finding_class: str, message: str, **details: Any) -> dict[str, Any]:
    if finding_class not in FINDING_CLASSES:
        raise ValueError(f"unknown finding class: {finding_class}")
    return {
        "code": code,
        "class": finding_class,
        "message": message,
        "details": redact(details),
    }


def command_available(repo_root: Path, relative_path: str) -> bool:
    return (repo_root / relative_path).exists()


def git_status(repo_root: Path) -> tuple[str, str]:
    if not (repo_root / ".git").exists() or not shutil.which("git"):
        return "not_git_checkout", ""
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = run_streaming_command(
            ["git", "status", "--short"],
            cwd=repo_root,
            timeout=10,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "git_status_failed", "git status timed out after 10 seconds"
    if proc.returncode != 0:
        return "git_status_failed", command_output_excerpt(proc)
    return ("dirty" if proc.stdout.strip() else "clean"), proc.stdout


def status_from_findings(
    findings: list[dict[str, Any]],
    *,
    fail_codes: set[str],
    warn_codes: set[str],
    default: str = "pass",
) -> str:
    codes = {entry["code"] for entry in findings}
    if codes & fail_codes:
        return "fail"
    if codes & warn_codes:
        return "warn"
    return default


def validate_registry(path: Path) -> tuple[str, list[dict[str, Any]]]:
    if not path.exists():
        return "missing", [
            finding(
                "TOPIC_WORKSPACE_REGISTRY_MISSING",
                "operator_action_required",
                "topic workspace registry is not configured",
                path=str(path),
            )
        ]
    if not path.is_file():
        return "invalid", [
            finding(
                "TOPIC_WORKSPACE_REGISTRY_NOT_FILE",
                "operator_action_required",
                "topic workspace registry path is not a file",
                path=str(path),
            )
        ]

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(path)
    if exit_code != validate_topic_workspace_registry.EXIT_PASS:
        errors = result.get("errors", [])
        return "invalid", [
            finding(
                "TOPIC_WORKSPACE_REGISTRY_INVALID",
                "operator_action_required",
                "topic workspace registry failed validation",
                errors=errors[:5],
            )
        ]
    return "pass", []


def inspect_workspaces(registry_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        resolved = resolve_workspaces(registry_path=registry_path)
    except TopicWorkspaceRegistryError as exc:
        return [], [
            finding(
                "TOPIC_WORKSPACE_RESOLUTION_FAILED",
                "operator_action_required",
                "topic workspace registry could not be resolved",
                error=str(exc),
            )
        ]

    workspaces = []
    findings: list[dict[str, Any]] = []
    for workspace in resolved:
        root = workspace.get("resolved_workspace_root")
        manifest = workspace.get("resolved_default_subject_manifest")
        entry = {
            "workspace_id": workspace.get("workspace_id"),
            "topic_label": workspace.get("topic_label"),
            "lifecycle_state": workspace.get("lifecycle_state"),
            "schedule_posture": workspace.get("schedule_posture"),
            "workspace_root_status": "ok"
            if isinstance(root, Path) and root.is_dir()
            else "missing",
            "default_subject_manifest_status": "ok"
            if isinstance(manifest, Path) and manifest.is_file()
            else "missing",
            "saturation": workspace_saturation_summary(workspace),
        }
        workspaces.append(entry)
        if entry["workspace_root_status"] != "ok":
            findings.append(
                finding(
                    "WORKSPACE_ROOT_MISSING",
                    "operator_action_required",
                    "workspace root is missing or not a directory",
                    workspace_id=entry["workspace_id"],
                )
            )
        if (
            workspace.get("schedule_posture") == "scheduled"
            and entry["default_subject_manifest_status"] != "ok"
        ):
            findings.append(
                finding(
                    "SCHEDULED_WORKSPACE_MANIFEST_MISSING",
                    "operator_action_required",
                    "scheduled workspace needs a resolvable default subject manifest",
                    workspace_id=entry["workspace_id"],
                )
            )
    return workspaces, findings


def workspace_saturation_summary(workspace: dict[str, Any]) -> dict[str, Any]:
    scheduler_policy = dict_or_empty(workspace.get("scheduler_policy"))
    saturation_value = scheduler_policy.get("saturation_state")
    saturation = saturation_value if isinstance(saturation_value, dict) else None
    if not isinstance(saturation, dict):
        return {
            "state": "not_evaluated",
            "scheduler_action": "run",
            "reason_codes": ["not_evaluated"],
            "interpretation": "No saturation evaluation is recorded for this workspace.",
        }
    state = str(saturation.get("state") or "not_evaluated")
    action = str(saturation.get("scheduler_action") or "run")
    reason_values = saturation.get("reason_codes")
    reasons: list[Any] = reason_values if isinstance(reason_values, list) else []
    if state in {"saturated", "cooldown"}:
        interpretation = f"Workspace is {state}; scheduler action is {action}."
    else:
        interpretation = "Workspace is not saturated under the recorded policy."
    return {
        "state": state,
        "scheduler_action": action,
        "reason_codes": [str(reason) for reason in reasons],
        "policy_id": saturation.get("policy_id"),
        "evaluated_at": saturation.get("evaluated_at"),
        "next_eligible_cycle": saturation.get("next_eligible_cycle"),
        "recent_yield_summary": saturation.get("recent_yield_summary"),
        "interpretation": interpretation,
    }


def sqlite_integrity_for_path(path: Path, *, quick_check: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "status": "unknown",
        "integrity_result": None,
        "schema_version": None,
        "user_version": None,
        "integrity_mode": "quick_check" if quick_check else "metadata",
    }
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.DatabaseError as exc:
        result["status"] = "fail"
        result["integrity_result"] = f"open_failed: {exc}"
        return result
    try:
        try:
            result["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            result["user_version"] = None
        try:
            row = conn.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            result["schema_version"] = row[0] if row else None
        except sqlite3.DatabaseError:
            result["schema_version"] = None
        if quick_check:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            values = [row[0] for row in rows]
            result["integrity_result"] = values
            result["status"] = "pass" if values == ["ok"] else "fail"
        else:
            result["integrity_result"] = ["metadata_only"]
            result["status"] = "pass"
    except sqlite3.DatabaseError as exc:
        result["status"] = "fail"
        result["integrity_result"] = f"quick_check_failed: {exc}"
    finally:
        conn.close()
    return result


def inspect_databases(
    repo_root: Path,
    *,
    quick_check: bool = False,
    quick_check_sample: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = sorted(
        {
            *repo_root.glob("dbs/**/*.sqlite"),
            *repo_root.glob("dbs/**/*.sqlite3"),
            *repo_root.glob("index/Topics/**/*.sqlite"),
            *repo_root.glob("index/Topics/**/*.sqlite3"),
        }
    )
    sampled_quick_check_paths = set(paths if quick_check else paths[: max(0, quick_check_sample)])
    databases = [
        sqlite_integrity_for_path(
            path, quick_check=quick_check or path in sampled_quick_check_paths
        )
        for path in paths
    ]
    findings = []
    for database in databases:
        if database["status"] != "pass":
            findings.append(
                finding(
                    "DB_INTEGRITY_CHECK_FAILED",
                    "operator_action_required",
                    "database quick_check failed or database could not be opened read-only",
                    path=database["path"],
                    integrity_result=database["integrity_result"],
                )
            )
    return databases, findings


def inspect_canonical_store(
    repo_root: Path,
    *,
    canonical_db: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if canonical_db is not None:
        configured_path = Path(canonical_db).expanduser()
        if not configured_path.is_absolute():
            configured_path = (repo_root / configured_path).resolve()
    else:
        configured_path = repo_root / "canonical.sqlite"
    summary = canonical_store.summarize_canonical_store_population(configured_path)
    findings: list[dict[str, Any]] = []
    if canonical_db is None and summary["status"] == "absent":
        summary["warnings"].append(
            "canonical store path not configured explicitly; local_doctor checked only the default repo-root canonical.sqlite path"
        )
    if summary["status"] == "absent":
        findings.append(
            finding(
                "CANONICAL_STORE_ABSENT",
                "advisory_only",
                "no canonical store was found at the configured local path",
                path=summary.get("path"),
            )
        )
    elif summary["status"] == "uninitialized":
        findings.append(
            finding(
                "CANONICAL_STORE_UNINITIALIZED",
                "operator_action_required",
                "canonical store path exists but the canonical schema is not initialized",
                path=summary.get("path"),
                errors=summary.get("errors", [])[:5],
            )
        )
    elif summary["status"] == "invalid":
        findings.append(
            finding(
                "CANONICAL_STORE_INVALID",
                "operator_action_required",
                "canonical store exists but failed validation",
                path=summary.get("path"),
                errors=summary.get("errors", [])[:5],
            )
        )
    elif summary["status"] == "initialized_empty":
        findings.append(
            finding(
                "CANONICAL_STORE_INITIALIZED_EMPTY",
                "advisory_only",
                "canonical store is initialized and valid, but contains no canonical records yet",
                path=summary.get("path"),
            )
        )
    elif summary["status"] == "populated" and summary.get("last_ingest_at") is None:
        findings.append(
            finding(
                "CANONICAL_STORE_INGEST_PROVENANCE_MISSING",
                "advisory_only",
                "canonical store contains rows but no recognized ingest provenance events",
                path=summary.get("path"),
            )
        )
    return summary, findings


def inspect_loop_health(
    repo_root: Path,
    *,
    canonical_db: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if canonical_db is not None:
        configured_path = Path(canonical_db).expanduser()
        if not configured_path.is_absolute():
            configured_path = (repo_root / configured_path).resolve()
    else:
        configured_path = repo_root / "canonical.sqlite"
    summary = loop_health.summarize_loop_health(configured_path)
    findings: list[dict[str, Any]] = []
    status = summary.get("health_status") or summary.get("status")
    if status == "review_lagging":
        findings.append(
            finding(
                "LOOP_HEALTH_REVIEW_LAGGING",
                "advisory_only",
                "loop health indicates review is not keeping pace with ingestion",
                pending_review_count=summary.get("review_backlog", {}).get("pending_review_count"),
                resolution_coverage=summary.get("ingestion_resolution", {}).get(
                    "resolution_coverage"
                ),
            )
        )
    elif status == "contradiction_spike":
        findings.append(
            finding(
                "LOOP_HEALTH_CONTRADICTION_SPIKE",
                "advisory_only",
                "loop health indicates a high contradiction rate in recent ingest cycles",
                contradiction_rate=summary.get("contradictions", {}).get(
                    "contradictions_per_new_source_claim"
                ),
            )
        )
    elif status == "stalled":
        findings.append(
            finding(
                "LOOP_HEALTH_STALLED",
                "advisory_only",
                "loop health indicates recent cycles produced no reviewable records",
                cycle_ids=summary.get("cycle_ids_considered", []),
            )
        )
    elif status == "accumulating":
        findings.append(
            finding(
                "LOOP_HEALTH_ACCUMULATING",
                "advisory_only",
                "loop health indicates the review backlog is accumulating",
                pending_review_count=summary.get("review_backlog", {}).get("pending_review_count"),
            )
        )
    return summary, findings


def inspect_graph_closure(
    repo_root: Path,
    *,
    canonical_db: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if canonical_db is not None:
        configured_path = Path(canonical_db).expanduser()
        if not configured_path.is_absolute():
            configured_path = (repo_root / configured_path).resolve()
    else:
        configured_path = repo_root / "canonical.sqlite"
    findings: list[dict[str, Any]] = []
    if not configured_path.exists():
        return {
            "schema_version": canonical_graph_closure.REPORT_SCHEMA_VERSION,
            "status": "unavailable",
            "severity": "info",
            "db_path": str(configured_path),
            "unavailable_reason": "canonical_store_absent",
            "orphan_error_count": 0,
            "unresolved_tracked_count": 0,
            "repairable_count": 0,
            "quarantined_count": 0,
            "read_only": True,
            "repair_performed": False,
        }, []
    try:
        report = canonical_graph_closure.audit_canonical_graph_closure(
            configured_path,
            generated_at="local-doctor-runtime",
            strict=False,
        )
    except canonical_graph_closure.GraphClosureError as exc:
        return {
            "schema_version": canonical_graph_closure.REPORT_SCHEMA_VERSION,
            "status": "unavailable",
            "severity": "warning",
            "db_path": str(configured_path),
            "unavailable_reason": str(exc),
            "orphan_error_count": 0,
            "unresolved_tracked_count": 0,
            "repairable_count": 0,
            "quarantined_count": 0,
            "read_only": True,
            "repair_performed": False,
        }, []
    summary = report.get("summary", {})
    compact = {
        "schema_version": report.get("schema_version"),
        "status": report.get("status"),
        "severity": report.get("severity"),
        "db_path": report.get("db_path"),
        "orphan_error_count": summary.get("true_orphan_error_count", 0),
        "unresolved_tracked_count": summary.get("unresolved_tracked_count", 0),
        "repairable_count": summary.get("repairable_count", 0),
        "quarantined_count": summary.get("quarantined_count", 0),
        "issue_count": summary.get("issue_count", 0),
        "read_only": report.get("read_only"),
        "repair_performed": report.get("repair_performed"),
        "top_issues": report.get("issues", [])[:5],
    }
    if report.get("status") == "fail":
        findings.append(
            finding(
                "GRAPH_CLOSURE_TRUE_ORPHANS",
                "operator_action_required",
                "canonical graph closure found true orphan rows",
                orphan_error_count=compact["orphan_error_count"],
                top_issues=compact["top_issues"],
            )
        )
    elif report.get("status") in {"pass_with_unresolved", "warning"}:
        findings.append(
            finding(
                "GRAPH_CLOSURE_UNRESOLVED_TRACKED",
                "advisory_only",
                "canonical graph closure found unresolved tracked rows",
                unresolved_tracked_count=compact["unresolved_tracked_count"],
                repairable_count=compact["repairable_count"],
                quarantined_count=compact["quarantined_count"],
            )
        )
    return compact, findings


def inspect_locks(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lock_root = repo_root / "runtime" / "locks"
    locks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if not lock_root.exists():
        return locks, []
    now = time.time()
    for path in sorted(lock_root.glob("*.lock")):
        metadata = workspace_lock.read_metadata(path)
        reason = workspace_lock.stale_reason(
            path, stale_after_seconds=DEFAULT_STALE_LOCK_SECONDS, now=now
        )
        entry = {
            "path": str(path),
            "workspace_id": metadata.get("workspace_id") if metadata else None,
            "pid": metadata.get("pid") if metadata else None,
            "heartbeat_at": metadata.get("heartbeat_at") if metadata else None,
            "status": "stale" if reason else "present",
            "stale_reason": reason,
        }
        locks.append(entry)
        if reason:
            findings.append(
                finding(
                    "STALE_WORKSPACE_LOCK",
                    "operator_action_required",
                    "workspace lock appears stale; inspect before breaking it",
                    path=str(path),
                    workspace_id=entry["workspace_id"],
                    reason=reason,
                )
            )
    return locks, findings


def inspect_backup_posture(repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy_path = repo_root / "config" / "durability_policies" / "local_first_crown_jewels.v1.json"
    posture: dict[str, Any] = {
        "policy_path": str(policy_path),
        "policy_status": "present" if policy_path.exists() else "missing",
        "backup_root": None,
        "latest_backup_path": None,
        "latest_backup_age_seconds": None,
        "status": "unknown",
    }
    findings = []
    if not policy_path.exists():
        posture["status"] = "fail"
        findings.append(
            finding(
                "CROWN_JEWEL_POLICY_MISSING",
                "operator_action_required",
                "crown-jewel backup policy missing",
            )
        )
        return posture, findings
    result, exit_code = validate_crown_jewel_store_policy.validate_crown_jewel_store_policy(
        policy_path
    )
    if exit_code != validate_crown_jewel_store_policy.EXIT_PASS:
        posture["status"] = "fail"
        findings.append(
            finding(
                "CROWN_JEWEL_POLICY_INVALID",
                "operator_action_required",
                "crown-jewel backup policy failed validation",
                errors=result.get("errors", [])[:5],
            )
        )
        return posture, findings
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    backup_root = repo_root / policy.get("backup_root", "runtime/backups/crown_jewels")
    posture["backup_root"] = str(backup_root)
    candidates: list[Path] = (
        [path for path in backup_root.rglob("*") if path.is_file()] if backup_root.exists() else []
    )
    if not candidates:
        posture["status"] = "warn"
        findings.append(
            finding(
                "CROWN_JEWEL_BACKUP_EVIDENCE_MISSING",
                "operator_action_required",
                "no local crown-jewel backup evidence was found under the configured backup root",
                backup_root=str(backup_root),
            )
        )
        return posture, findings

    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    posture["latest_backup_path"] = str(latest)
    posture["latest_backup_age_seconds"] = int(time.time() - latest.stat().st_mtime)
    posture["status"] = "pass"
    return posture, findings


def migration_ledger_receipt_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}-report.json")


def load_migration_ledger_receipt(path: Path) -> dict[str, Any] | None:
    receipt_path = migration_ledger_receipt_path(path)
    if not receipt_path.exists() or not receipt_path.is_file():
        return None
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    counts = payload.get("counts")
    latest_event = payload.get("latest_event")
    status = payload.get("status")
    if (
        not isinstance(counts, dict)
        or not isinstance(latest_event, dict)
        or status not in {"pass", "warn", "fail"}
    ):
        return None
    return payload


def inspect_migration_posture(
    repo_root: Path,
    *,
    validate_all: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ledger_root = repo_root / "runtime" / "ledgers"
    posture: dict[str, Any] = {
        "ledger_root": str(ledger_root),
        "ledger_count": 0,
        "event_count": 0,
        "latest_event_id": None,
        "latest_workspace_id": None,
        "latest_migration_type": None,
        "latest_occurred_at": None,
        "latest_tool_surface": None,
        "latest_input_version": None,
        "latest_output_version": None,
        "latest_backup_ref": None,
        "latest_snapshot_ref": None,
        "latest_rollback_of_event_id": None,
        "validation_mode": "full" if validate_all else "fast",
        "validated_ledger_count": 0,
        "cached_receipt_count": 0,
        "skipped_ledger_count": 0,
        "status": "unknown",
    }
    findings: list[dict[str, Any]] = []
    if not ledger_root.exists():
        posture["status"] = "warn"
        findings.append(
            finding(
                "MIGRATION_LEDGER_EVIDENCE_MISSING",
                "advisory_only",
                "no migration ledger files were found under the local ledger root",
                ledger_root=str(ledger_root),
            )
        )
        return posture, findings

    ledger_paths: list[Path] = sorted(
        path for path in ledger_root.glob("*.migration-ledger.jsonl") if path.is_file()
    )
    posture["ledger_count"] = len(ledger_paths)
    if not ledger_paths:
        posture["status"] = "warn"
        findings.append(
            finding(
                "MIGRATION_LEDGER_EVIDENCE_MISSING",
                "advisory_only",
                "no migration ledger files were found under the local ledger root",
                ledger_root=str(ledger_root),
            )
        )
        return posture, findings

    latest_path = max(ledger_paths, key=lambda path: (path.stat().st_mtime_ns, path.name))
    latest_event: dict[str, Any] | None = None
    for path in ledger_paths:
        if validate_all or path == latest_path:
            result, exit_code = validate_migration_ledger.validate_migration_ledger(path)
            posture["validated_ledger_count"] += 1
            if exit_code != validate_migration_ledger.EXIT_PASS:
                findings.append(
                    finding(
                        "MIGRATION_LEDGER_INVALID",
                        "operator_action_required",
                        "migration ledger failed validation",
                        path=str(path),
                        errors=result.get("errors", [])[:5],
                    )
                )
                continue
            posture["event_count"] += result["counts"]["accepted"]
            candidate = result.get("latest_event")
        else:
            receipt = load_migration_ledger_receipt(path)
            if receipt is None or receipt.get("status") != "pass":
                posture["skipped_ledger_count"] += 1
                continue
            posture["cached_receipt_count"] += 1
            posture["event_count"] += int(receipt.get("counts", {}).get("accepted", 0))
            candidate = receipt.get("latest_event")
        if isinstance(candidate, dict) and (
            latest_event is None or candidate["occurred_at"] > latest_event["occurred_at"]
        ):
            latest_event = candidate

    if findings:
        posture["status"] = "fail"
        return posture, findings

    posture["status"] = "pass"
    if latest_event is not None:
        posture["latest_event_id"] = latest_event["event_id"]
        posture["latest_workspace_id"] = latest_event["workspace_id"]
        posture["latest_migration_type"] = latest_event["migration_type"]
        posture["latest_occurred_at"] = latest_event["occurred_at"]
        posture["latest_tool_surface"] = latest_event["tool_surface"]
        posture["latest_input_version"] = latest_event["input_version"]
        posture["latest_output_version"] = latest_event["output_version"]
        posture["latest_backup_ref"] = latest_event.get("backup_ref")
        posture["latest_snapshot_ref"] = latest_event.get("snapshot_ref")
        posture["latest_rollback_of_event_id"] = latest_event.get("rollback_of_event_id")
    return posture, findings


def inspect_scheduler(
    repo_root: Path, registry_path: Path, registry_status: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scheduler = {
        "selector_path": str(repo_root / "tools" / "scripts" / "select_scheduled_workspaces.py"),
        "selector_status": "present"
        if (repo_root / "tools" / "scripts" / "select_scheduled_workspaces.py").exists()
        else "missing",
        "registry_status": registry_status,
        "selected_count": None,
        "skipped_count": None,
        "status": "unknown",
    }
    findings = []
    if scheduler["selector_status"] != "present":
        scheduler["status"] = "fail"
        findings.append(
            finding(
                "SCHEDULER_SELECTOR_MISSING",
                "operator_action_required",
                "scheduler selector is missing",
            )
        )
        return scheduler, findings
    if registry_status != "pass":
        scheduler["status"] = "warn"
        findings.append(
            finding(
                "SCHEDULER_REGISTRY_NOT_READY",
                "operator_action_required",
                "scheduler cannot be evaluated until the registry passes validation",
            )
        )
        return scheduler, findings
    proc = run_streaming_command(
        [
            sys.executable,
            str(repo_root / "tools" / "scripts" / "select_scheduled_workspaces.py"),
            "--registry",
            str(registry_path),
            "--format",
            "json",
        ],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        scheduler["status"] = "fail"
        findings.append(
            finding(
                "SCHEDULER_SELECTOR_FAILED",
                "operator_action_required",
                "scheduler selector failed",
                stderr=command_output_excerpt(proc),
            )
        )
        return scheduler, findings
    payload = json.loads(proc.stdout)
    scheduler["selected_count"] = payload.get("selected_count")
    scheduler["skipped_count"] = payload.get("skipped_count")
    scheduler["status"] = "pass" if payload.get("selected_count", 0) else "warn"
    if not payload.get("selected_count", 0):
        findings.append(
            finding(
                "NO_SCHEDULED_WORKSPACES_READY",
                "advisory_only",
                "scheduler selector found no runnable scheduled workspaces",
            )
        )
    return scheduler, findings


def inspect_public_gates(repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    required_paths = {
        "public_presentation_schema": repo_root
        / "config"
        / "public_knowledge_tree_presentation.schema.json",
        "public_presentation_validator": repo_root
        / "tools"
        / "validators"
        / "validate_public_knowledge_tree_presentation.py",
        "static_output_validator": repo_root
        / "tools"
        / "validators"
        / "validate_static_knowledge_tree_output.py",
        "public_sharing_bundle_builder": repo_root
        / "tools"
        / "scripts"
        / "build_public_sharing_bundle.py",
        "public_safekeeping_schema": repo_root
        / "config"
        / "public_safekeeping_manifest.schema.json",
        "public_safekeeping_validator": repo_root
        / "tools"
        / "validators"
        / "validate_public_safekeeping_manifest.py",
        "public_safekeeping_builder": repo_root
        / "tools"
        / "scripts"
        / "build_public_safekeeping_manifest.py",
    }
    missing = [name for name, path in required_paths.items() if not path.exists()]
    status = "pass" if not missing else "fail"
    findings = []
    if missing:
        findings.append(
            finding(
                "PUBLIC_GATE_SURFACE_MISSING",
                "operator_action_required",
                "one or more public/private gate surfaces are missing",
                missing=missing,
            )
        )
    return {
        "status": status,
        "surfaces": {
            name: "present" if path.exists() else "missing" for name, path in required_paths.items()
        },
    }, findings


def summarize(checks: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    fail_count = sum(
        1
        for value in checks.values()
        if value == "fail" or (isinstance(value, dict) and value.get("status") == "fail")
    )
    warn_count = sum(
        1
        for value in checks.values()
        if value == "warn" or (isinstance(value, dict) and value.get("status") == "warn")
    )
    action_count = sum(1 for entry in findings if entry["class"] == "operator_action_required")
    return {
        "status": "fail" if fail_count else ("warn" if warn_count or action_count else "pass"),
        "check_count": len(checks),
        "finding_count": len(findings),
        "operator_action_required_count": action_count,
    }


def build_report(
    repo_root: Path,
    *,
    registry: str | Path | None = None,
    canonical_db: str | Path | None = None,
    database_quick_check: bool = False,
    database_quick_check_sample: int = 0,
    validate_all_migration_ledgers: bool = False,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    status, status_detail = git_status(repo_root)
    if status == "dirty":
        findings.append(finding("REPO_DIRTY", "advisory_only", "working tree has local changes"))
    elif status != "clean":
        findings.append(
            finding(
                "REPO_STATUS_UNKNOWN",
                "advisory_only",
                "repo hygiene could not be fully inspected",
                status=status,
                detail=status_detail,
            )
        )

    validators = [
        "tools/validators/validate_knowledge_tree_export.py",
        "tools/validators/validate_migration_ledger.py",
        "tools/validators/validate_release_readiness.py",
        "tools/validators/validate_static_knowledge_tree_output.py",
        "tools/validators/validate_topic_workspace_registry.py",
        "tools/validators/validate_crown_jewel_backup_manifest.py",
        "tools/validators/validate_crown_jewel_store_policy.py",
        "tools/validators/validate_crown_jewel_store_manifest.py",
        "tools/validators/validate_public_knowledge_tree_presentation.py",
        "tools/validators/validate_source_adapter.py",
    ]
    missing_validators = [path for path in validators if not command_available(repo_root, path)]
    if missing_validators:
        findings.append(
            finding(
                "VALIDATOR_MISSING",
                "operator_action_required",
                "one or more expected validators are missing",
                paths=missing_validators,
            )
        )

    registry_path = discover_registry_path(registry, cwd=repo_root)
    registry_status, registry_findings = validate_registry(registry_path)
    findings.extend(registry_findings)
    workspaces, workspace_findings = (
        inspect_workspaces(registry_path) if registry_status == "pass" else ([], [])
    )
    findings.extend(workspace_findings)
    databases, database_findings = inspect_databases(
        repo_root,
        quick_check=database_quick_check,
        quick_check_sample=database_quick_check_sample,
    )
    findings.extend(database_findings)
    canonical_store_summary, canonical_store_findings = inspect_canonical_store(
        repo_root,
        canonical_db=canonical_db,
    )
    findings.extend(canonical_store_findings)
    loop_health_summary, loop_health_findings = inspect_loop_health(
        repo_root,
        canonical_db=canonical_db,
    )
    findings.extend(loop_health_findings)
    graph_closure_summary, graph_closure_findings = inspect_graph_closure(
        repo_root,
        canonical_db=canonical_db,
    )
    findings.extend(graph_closure_findings)
    locks, lock_findings = inspect_locks(repo_root)
    findings.extend(lock_findings)
    backup_posture, backup_findings = inspect_backup_posture(repo_root)
    findings.extend(backup_findings)
    migration_posture, migration_findings = inspect_migration_posture(
        repo_root,
        validate_all=validate_all_migration_ledgers,
    )
    findings.extend(migration_findings)
    scheduler, scheduler_findings = inspect_scheduler(repo_root, registry_path, registry_status)
    findings.extend(scheduler_findings)
    public_gates, public_findings = inspect_public_gates(repo_root)
    findings.extend(public_findings)

    checks = {
        "repo_hygiene": status,
        "validator_availability": "missing" if missing_validators else "available",
        "topic_workspace_registry": registry_status,
        "workspaces": "pass"
        if workspaces and not workspace_findings
        else ("warn" if not workspaces else "fail"),
        "scheduler_eligibility": scheduler["status"],
        "crown_jewel_backup_posture": backup_posture["status"],
        "migration_ledger_posture": migration_posture["status"],
        "db_integrity_smoke": status_from_findings(
            database_findings,
            fail_codes={"DB_INTEGRITY_CHECK_FAILED"},
            warn_codes=set(),
            default="pass" if databases else "not_found",
        ),
        "canonical_store_population": status_from_findings(
            canonical_store_findings,
            fail_codes={"CANONICAL_STORE_INVALID"},
            warn_codes={
                "CANONICAL_STORE_ABSENT",
                "CANONICAL_STORE_UNINITIALIZED",
                "CANONICAL_STORE_INITIALIZED_EMPTY",
                "CANONICAL_STORE_INGEST_PROVENANCE_MISSING",
            },
            default="pass",
        ),
        "loop_health": status_from_findings(
            loop_health_findings,
            fail_codes=set(),
            warn_codes={
                "LOOP_HEALTH_REVIEW_LAGGING",
                "LOOP_HEALTH_CONTRADICTION_SPIKE",
                "LOOP_HEALTH_STALLED",
                "LOOP_HEALTH_ACCUMULATING",
            },
            default="pass",
        ),
        "graph_closure": status_from_findings(
            graph_closure_findings,
            fail_codes={"GRAPH_CLOSURE_TRUE_ORPHANS"},
            warn_codes={"GRAPH_CLOSURE_UNRESOLVED_TRACKED"},
            default=str(graph_closure_summary.get("status") or "unavailable"),
        ),
        "workspace_locks": status_from_findings(
            lock_findings,
            fail_codes=set(),
            warn_codes={"STALE_WORKSPACE_LOCK"},
            default="pass",
        ),
        "public_private_sharing_gate": public_gates["status"],
    }
    summary = summarize(checks, findings)

    return redact(
        {
            "schema_version": REPORT_SCHEMA_VERSION,
            "read_only": True,
            "auto_fix_performed": False,
            "repo_root": str(repo_root.resolve()),
            "registry_path": str(registry_path),
            "summary": summary,
            "checks": checks,
            "workspaces": workspaces,
            "databases": databases,
            "canonical_store": canonical_store_summary,
            "loop_health": loop_health_summary,
            "graph_closure": graph_closure_summary,
            "locks": locks,
            "backup_posture": backup_posture,
            "migration_posture": migration_posture,
            "scheduler": scheduler,
            "public_gates": public_gates,
            "findings": findings,
            "redaction": {
                "private_paths": "redacted",
                "secrets": "redacted",
                "raw_payloads_included": False,
                "full_extracted_text_included": False,
                "runtime_logs_included": False,
                "private_operator_notes_included": False,
            },
        }
    )


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"schema_version={format_operator_text_value(report['schema_version'])}",
        f"status={format_operator_text_value(report['summary']['status'])}",
        f"finding_count={format_operator_text_value(report['summary']['finding_count'])}",
        "operator_action_required_count="
        f"{format_operator_text_value(report['summary']['operator_action_required_count'])}",
    ]
    for name, status in report["checks"].items():
        lines.append(f"check.{name}={format_operator_text_value(status)}")
    canonical_store_summary = report.get("canonical_store", {})
    if isinstance(canonical_store_summary, dict):
        lines.append(
            "canonical_store.status="
            f"{format_operator_text_value(canonical_store_summary.get('status'))}"
        )
        lines.append(
            "canonical_store.total_rows="
            f"{format_operator_text_value(canonical_store_summary.get('total_rows'))}"
        )
        lines.append(
            "canonical_store.last_ingest_at="
            f"{format_operator_text_value(canonical_store_summary.get('last_ingest_at'))}"
        )
    loop_health_summary = report.get("loop_health", {})
    if isinstance(loop_health_summary, dict):
        lines.append(
            "loop_health.status="
            f"{format_operator_text_value(loop_health_summary.get('health_status'))}"
        )
        lines.append(
            "loop_health.pending_review_count="
            f"{format_operator_text_value(loop_health_summary.get('review_backlog', {}).get('pending_review_count'))}"
        )
        lines.append(
            "loop_health.resolution_coverage="
            f"{format_operator_text_value(loop_health_summary.get('ingestion_resolution', {}).get('resolution_coverage'))}"
        )
    graph_closure_summary = report.get("graph_closure", {})
    if isinstance(graph_closure_summary, dict):
        lines.append(
            "graph_closure.status="
            f"{format_operator_text_value(graph_closure_summary.get('status'))}"
        )
        lines.append(
            "graph_closure.orphan_error_count="
            f"{format_operator_text_value(graph_closure_summary.get('orphan_error_count'))}"
        )
        lines.append(
            "graph_closure.unresolved_tracked_count="
            f"{format_operator_text_value(graph_closure_summary.get('unresolved_tracked_count'))}"
        )
    for index, entry in enumerate(report["findings"][:20]):
        lines.append(f"finding[{index}].code={format_operator_text_value(entry['code'])}")
        lines.append(f"finding[{index}].class={format_operator_text_value(entry['class'])}")
        lines.append(f"finding[{index}].message={format_operator_text_value(entry['message'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--registry", help="Optional topic workspace registry path.")
    parser.add_argument(
        "--canonical-db",
        help="Optional canonical SQLite path to summarize read-only. Defaults to <repo-root>/canonical.sqlite.",
    )
    database_mode = parser.add_mutually_exclusive_group()
    database_mode.add_argument(
        "--fast", action="store_true", help="Use metadata-only database checks (default)."
    )
    database_mode.add_argument(
        "--database-quick-check",
        action="store_true",
        help="Run PRAGMA quick_check against every discovered database.",
    )
    parser.add_argument(
        "--database-quick-check-sample",
        type=int,
        default=0,
        help="Run PRAGMA quick_check against the first N discovered databases while keeping the rest on metadata-only checks.",
    )
    parser.add_argument(
        "--full-migration-ledger-validation",
        action="store_true",
        help="Validate every migration ledger instead of using the fast latest-ledger/receipt path.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)

    report = build_report(
        args.repo_root,
        registry=args.registry,
        canonical_db=args.canonical_db,
        database_quick_check=args.database_quick_check,
        database_quick_check_sample=max(0, args.database_quick_check_sample),
        validate_all_migration_ledgers=args.full_migration_ledger_validation,
    )
    body = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_text(report)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
