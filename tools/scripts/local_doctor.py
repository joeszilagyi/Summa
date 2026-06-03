#!/usr/bin/env python3
"""Emit a read-only local installation doctor report.

The doctor inspects repo-local signals only. It does not mutate files, DBs,
remotes, generated artifacts, or workspaces, and it redacts path-like and
secret-looking details by default.
"""

from __future__ import annotations

import argparse
import json
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
from tools.common.topic_workspace_registry import (  # noqa: E402
    TopicWorkspaceRegistryError,
    discover_registry_path,
    resolve_workspaces,
)

VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

import validate_topic_workspace_registry  # noqa: E402
import validate_migration_ledger  # noqa: E402
import validate_crown_jewel_store_policy  # noqa: E402


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    text = SECRET_RE.sub(r"\1=[redacted]", value)
    text = re.sub(r"/(?:home|Users|tmp)/[^\\s]+", "[redacted-path]", text)
    return text


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
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return "git_status_failed", proc.stderr
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
            "workspace_root_status": "ok" if isinstance(root, Path) and root.is_dir() else "missing",
            "default_subject_manifest_status": "ok" if isinstance(manifest, Path) and manifest.is_file() else "missing",
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
        if workspace.get("schedule_posture") == "scheduled" and entry["default_subject_manifest_status"] != "ok":
            findings.append(
                finding(
                    "SCHEDULED_WORKSPACE_MANIFEST_MISSING",
                    "operator_action_required",
                    "scheduled workspace needs a resolvable default subject manifest",
                    workspace_id=entry["workspace_id"],
                )
            )
    return workspaces, findings


def sqlite_integrity_for_path(path: Path) -> dict[str, Any]:
    result = {
        "path": str(path),
        "status": "unknown",
        "integrity_result": None,
        "schema_version": None,
        "user_version": None,
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
        rows = conn.execute("PRAGMA quick_check").fetchall()
        values = [row[0] for row in rows]
        result["integrity_result"] = values
        result["status"] = "pass" if values == ["ok"] else "fail"
    except sqlite3.DatabaseError as exc:
        result["status"] = "fail"
        result["integrity_result"] = f"quick_check_failed: {exc}"
    finally:
        conn.close()
    return result


def inspect_databases(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = sorted(
        {
            *repo_root.glob("dbs/**/*.sqlite"),
            *repo_root.glob("dbs/**/*.sqlite3"),
            *repo_root.glob("index/Topics/**/*.sqlite"),
            *repo_root.glob("index/Topics/**/*.sqlite3"),
        }
    )
    databases = [sqlite_integrity_for_path(path) for path in paths]
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


def inspect_locks(repo_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lock_root = repo_root / "runtime" / "locks"
    locks = []
    findings: list[dict[str, Any]] = []
    if not lock_root.exists():
        return locks, []
    now = time.time()
    for path in sorted(lock_root.glob("*.lock")):
        metadata = workspace_lock.read_metadata(path)
        reason = workspace_lock.stale_reason(path, stale_after_seconds=DEFAULT_STALE_LOCK_SECONDS, now=now)
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
    posture = {
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
        findings.append(finding("CROWN_JEWEL_POLICY_MISSING", "operator_action_required", "crown-jewel backup policy missing"))
        return posture, findings
    result, exit_code = validate_crown_jewel_store_policy.validate_crown_jewel_store_policy(policy_path)
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
    candidates = [path for path in backup_root.rglob("*") if path.is_file()] if backup_root.exists() else []
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


def inspect_migration_posture(repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ledger_root = repo_root / "runtime" / "ledgers"
    posture = {
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

    ledger_paths = sorted(path for path in ledger_root.glob("*.migration-ledger.jsonl") if path.is_file())
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

    latest_event: dict[str, Any] | None = None
    for path in ledger_paths:
        result, exit_code = validate_migration_ledger.validate_migration_ledger(path)
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
        if candidate is not None and (
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


def inspect_scheduler(repo_root: Path, registry_path: Path, registry_status: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scheduler = {
        "selector_path": str(repo_root / "tools" / "scripts" / "select_scheduled_workspaces.py"),
        "selector_status": "present" if (repo_root / "tools" / "scripts" / "select_scheduled_workspaces.py").exists() else "missing",
        "registry_status": registry_status,
        "selected_count": None,
        "skipped_count": None,
        "status": "unknown",
    }
    findings = []
    if scheduler["selector_status"] != "present":
        scheduler["status"] = "fail"
        findings.append(finding("SCHEDULER_SELECTOR_MISSING", "operator_action_required", "scheduler selector is missing"))
        return scheduler, findings
    if registry_status != "pass":
        scheduler["status"] = "warn"
        findings.append(finding("SCHEDULER_REGISTRY_NOT_READY", "operator_action_required", "scheduler cannot be evaluated until the registry passes validation"))
        return scheduler, findings
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "tools" / "scripts" / "select_scheduled_workspaces.py"),
            "--registry",
            str(registry_path),
            "--format",
            "json",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        scheduler["status"] = "fail"
        findings.append(finding("SCHEDULER_SELECTOR_FAILED", "operator_action_required", "scheduler selector failed", stderr=proc.stderr))
        return scheduler, findings
    payload = json.loads(proc.stdout)
    scheduler["selected_count"] = payload.get("selected_count")
    scheduler["skipped_count"] = payload.get("skipped_count")
    scheduler["status"] = "pass" if payload.get("selected_count", 0) else "warn"
    if not payload.get("selected_count", 0):
        findings.append(finding("NO_SCHEDULED_WORKSPACES_READY", "advisory_only", "scheduler selector found no runnable scheduled workspaces"))
    return scheduler, findings


def inspect_public_gates(repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    required_paths = {
        "public_presentation_schema": repo_root / "config" / "public_knowledge_tree_presentation.schema.json",
        "public_presentation_validator": repo_root / "tools" / "validators" / "validate_public_knowledge_tree_presentation.py",
        "static_output_validator": repo_root / "tools" / "validators" / "validate_static_knowledge_tree_output.py",
        "public_sharing_bundle_builder": repo_root / "tools" / "scripts" / "build_public_sharing_bundle.py",
        "public_safekeeping_schema": repo_root / "config" / "public_safekeeping_manifest.schema.json",
        "public_safekeeping_validator": repo_root / "tools" / "validators" / "validate_public_safekeeping_manifest.py",
        "public_safekeeping_builder": repo_root / "tools" / "scripts" / "build_public_safekeeping_manifest.py",
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
        "surfaces": {name: "present" if path.exists() else "missing" for name, path in required_paths.items()},
    }, findings


def summarize(checks: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    fail_count = sum(1 for value in checks.values() if value == "fail" or (isinstance(value, dict) and value.get("status") == "fail"))
    warn_count = sum(1 for value in checks.values() if value == "warn" or (isinstance(value, dict) and value.get("status") == "warn"))
    action_count = sum(1 for entry in findings if entry["class"] == "operator_action_required")
    return {
        "status": "fail" if fail_count else ("warn" if warn_count or action_count else "pass"),
        "check_count": len(checks),
        "finding_count": len(findings),
        "operator_action_required_count": action_count,
    }


def build_report(repo_root: Path, *, registry: str | Path | None = None) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    status, status_detail = git_status(repo_root)
    if status == "dirty":
        findings.append(finding("REPO_DIRTY", "advisory_only", "working tree has local changes"))
    elif status != "clean":
        findings.append(finding("REPO_STATUS_UNKNOWN", "advisory_only", "repo hygiene could not be fully inspected", status=status, detail=status_detail))

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
        findings.append(finding("VALIDATOR_MISSING", "operator_action_required", "one or more expected validators are missing", paths=missing_validators))

    registry_path = discover_registry_path(registry, cwd=repo_root)
    registry_status, registry_findings = validate_registry(registry_path)
    findings.extend(registry_findings)
    workspaces, workspace_findings = inspect_workspaces(registry_path) if registry_status == "pass" else ([], [])
    findings.extend(workspace_findings)
    databases, database_findings = inspect_databases(repo_root)
    findings.extend(database_findings)
    locks, lock_findings = inspect_locks(repo_root)
    findings.extend(lock_findings)
    backup_posture, backup_findings = inspect_backup_posture(repo_root)
    findings.extend(backup_findings)
    migration_posture, migration_findings = inspect_migration_posture(repo_root)
    findings.extend(migration_findings)
    scheduler, scheduler_findings = inspect_scheduler(repo_root, registry_path, registry_status)
    findings.extend(scheduler_findings)
    public_gates, public_findings = inspect_public_gates(repo_root)
    findings.extend(public_findings)

    checks = {
        "repo_hygiene": status,
        "validator_availability": "missing" if missing_validators else "available",
        "topic_workspace_registry": registry_status,
        "workspaces": "pass" if workspaces and not workspace_findings else ("warn" if not workspaces else "fail"),
        "scheduler_eligibility": scheduler["status"],
        "crown_jewel_backup_posture": backup_posture["status"],
        "migration_ledger_posture": migration_posture["status"],
        "db_integrity_smoke": status_from_findings(
            database_findings,
            fail_codes={"DB_INTEGRITY_CHECK_FAILED"},
            warn_codes=set(),
            default="pass" if databases else "not_found",
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
        f"schema_version={report['schema_version']}",
        f"status={report['summary']['status']}",
        f"finding_count={report['summary']['finding_count']}",
        f"operator_action_required_count={report['summary']['operator_action_required_count']}",
    ]
    for name, status in report["checks"].items():
        lines.append(f"check.{name}={status}")
    for index, entry in enumerate(report["findings"][:20]):
        lines.append(f"finding[{index}].code={entry['code']}")
        lines.append(f"finding[{index}].class={entry['class']}")
        lines.append(f"finding[{index}].message={entry['message']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--registry", help="Optional topic workspace registry path.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)

    report = build_report(args.repo_root, registry=args.registry)
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
