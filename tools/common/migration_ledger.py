"""Append-only migration ledger writer and reader for Summa schema/artifact evolution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "migration-ledger.v1"
DEFAULT_LEDGER_ROOT = Path("runtime") / "ledgers"
MIGRATION_TYPES = {
    "schema_migration",
    "artifact_contract_migration",
    "rollback_reference",
}


class MigrationLedgerError(RuntimeError):
    """Raised when a migration-ledger event is invalid or cannot be appended."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_migration_id(prefix: str = "migration") -> str:
    return f"mig:{prefix}-{uuid.uuid4()}"


def default_ledger_path(repo_root: Path, workspace_id: str) -> Path:
    return repo_root / DEFAULT_LEDGER_ROOT / f"{workspace_id}.migration-ledger.jsonl"


def _require_nonblank(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationLedgerError(f"{label} is required for migration-ledger events")
    return value


def _validate_artifact_refs(value: list[dict[str, Any]] | None, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise MigrationLedgerError(f"{label} must be a non-empty array")
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise MigrationLedgerError(f"{label}[{index}] must be an object")
        role = item.get("role")
        path = item.get("path")
        if not isinstance(role, str) or not role.strip():
            raise MigrationLedgerError(f"{label}[{index}].role must be a non-blank string")
        if not isinstance(path, str) or not path.strip():
            raise MigrationLedgerError(f"{label}[{index}].path must be a non-blank string")
        normalized = {"role": role, "path": path}
        version = item.get("version")
        if version is not None:
            if not isinstance(version, str) or not version.strip():
                raise MigrationLedgerError(f"{label}[{index}].version must be null or a non-blank string")
            normalized["version"] = version
        validated.append(normalized)
    return validated


def build_event(
    *,
    workspace_id: str,
    migration_id: str,
    migration_type: str,
    subject_ref: str,
    tool_surface: str,
    tool_version: str,
    input_version: str,
    output_version: str,
    input_artifact_refs: list[dict[str, Any]],
    output_artifact_refs: list[dict[str, Any]],
    run_id: str | None = None,
    backup_ref: str | None = None,
    snapshot_ref: str | None = None,
    rollback_of_event_id: str | None = None,
    note: str | None = None,
    occurred_at: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    _require_nonblank(workspace_id, "workspace_id")
    _require_nonblank(migration_id, "migration_id")
    _require_nonblank(subject_ref, "subject_ref")
    _require_nonblank(tool_surface, "tool_surface")
    _require_nonblank(tool_version, "tool_version")
    _require_nonblank(input_version, "input_version")
    _require_nonblank(output_version, "output_version")
    if migration_type not in MIGRATION_TYPES:
        raise MigrationLedgerError(f"unsupported migration-ledger migration_type: {migration_type}")
    if input_version == output_version:
        raise MigrationLedgerError("input_version and output_version must differ")
    validated_inputs = _validate_artifact_refs(input_artifact_refs, "input_artifact_refs")
    validated_outputs = _validate_artifact_refs(output_artifact_refs, "output_artifact_refs")

    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or f"mle:{uuid.uuid4()}",
        "workspace_id": workspace_id,
        "migration_id": migration_id,
        "migration_type": migration_type,
        "subject_ref": subject_ref,
        "tool_surface": tool_surface,
        "tool_version": tool_version,
        "input_version": input_version,
        "output_version": output_version,
        "input_artifact_refs": validated_inputs,
        "output_artifact_refs": validated_outputs,
        "occurred_at": occurred_at or utc_now(),
        "note": note,
    }
    optional = {
        "run_id": run_id,
        "backup_ref": backup_ref,
        "snapshot_ref": snapshot_ref,
        "rollback_of_event_id": rollback_of_event_id,
    }
    for key, value in optional.items():
        if value is not None:
            _require_nonblank(value, key)
            event[key] = value
    if migration_type == "rollback_reference":
        if rollback_of_event_id is None:
            raise MigrationLedgerError("rollback_of_event_id is required for rollback_reference events")
        if backup_ref is None and snapshot_ref is None:
            raise MigrationLedgerError("rollback_reference events require backup_ref or snapshot_ref")
    elif rollback_of_event_id is not None:
        raise MigrationLedgerError("rollback_of_event_id is only allowed for rollback_reference events")
    return event


def append_event(ledger_path: Path, event: dict[str, Any]) -> None:
    if event.get("schema_version") != SCHEMA_VERSION:
        raise MigrationLedgerError("migration-ledger event has wrong schema_version")
    required = (
        "event_id",
        "workspace_id",
        "migration_id",
        "migration_type",
        "subject_ref",
        "tool_surface",
        "tool_version",
        "input_version",
        "output_version",
        "input_artifact_refs",
        "output_artifact_refs",
        "occurred_at",
    )
    for key in required:
        if not event.get(key):
            raise MigrationLedgerError(f"migration-ledger event missing required key: {key}")
    if event["migration_type"] not in MIGRATION_TYPES:
        raise MigrationLedgerError(f"unsupported migration-ledger migration_type: {event['migration_type']}")
    if event["input_version"] == event["output_version"]:
        raise MigrationLedgerError("migration-ledger event input_version and output_version must differ")
    if event["migration_type"] == "rollback_reference":
        if not event.get("rollback_of_event_id"):
            raise MigrationLedgerError("rollback_reference events require rollback_of_event_id")
        if not event.get("backup_ref") and not event.get("snapshot_ref"):
            raise MigrationLedgerError("rollback_reference events require backup_ref or snapshot_ref")
    elif event.get("rollback_of_event_id"):
        raise MigrationLedgerError("rollback_of_event_id is only allowed for rollback_reference events")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def load_events(ledger_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    raw_text = ledger_path.read_text(encoding="utf-8")
    lines = raw_text.splitlines()
    has_trailing_newline = raw_text.endswith("\n")
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            if line_number == len(lines) and not has_trailing_newline:
                break
            raise MigrationLedgerError(
                f"migration-ledger {ledger_path} contains invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(payload, dict):
            raise MigrationLedgerError("migration-ledger lines must be JSON objects")
        events.append(payload)
    return events


def parse_json_value(raw: str | None, label: str) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MigrationLedgerError(f"{label} is not valid JSON: line {exc.lineno}") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append one migration-ledger.v1 event.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--ledger", type=Path, help="Explicit ledger JSONL path.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--migration-id", required=True)
    parser.add_argument("--migration-type", required=True, choices=sorted(MIGRATION_TYPES))
    parser.add_argument("--subject-ref", required=True)
    parser.add_argument("--tool-surface", required=True)
    parser.add_argument("--tool-version", required=True)
    parser.add_argument("--input-version", required=True)
    parser.add_argument("--output-version", required=True)
    parser.add_argument("--input-artifact-refs-json", required=True)
    parser.add_argument("--output-artifact-refs-json", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--backup-ref")
    parser.add_argument("--snapshot-ref")
    parser.add_argument("--rollback-of-event-id")
    parser.add_argument("--note")
    parser.add_argument("--occurred-at")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ledger_path = args.ledger or default_ledger_path(args.repo_root, args.workspace_id)
    try:
        event = build_event(
            workspace_id=args.workspace_id,
            migration_id=args.migration_id,
            migration_type=args.migration_type,
            subject_ref=args.subject_ref,
            tool_surface=args.tool_surface,
            tool_version=args.tool_version,
            input_version=args.input_version,
            output_version=args.output_version,
            input_artifact_refs=parse_json_value(args.input_artifact_refs_json, "--input-artifact-refs-json"),
            output_artifact_refs=parse_json_value(args.output_artifact_refs_json, "--output-artifact-refs-json"),
            run_id=args.run_id,
            backup_ref=args.backup_ref,
            snapshot_ref=args.snapshot_ref,
            rollback_of_event_id=args.rollback_of_event_id,
            note=args.note,
            occurred_at=args.occurred_at,
        )
        append_event(ledger_path, event)
    except MigrationLedgerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "appended", "ledger_path": str(ledger_path), "event_id": event["event_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
