"""Append-only runtime ledger writer for Summa command events."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "runtime-ledger.v1"
LEDGER_METADATA_SCHEMA_VERSION = "runtime-ledger-metadata.v1"
DEFAULT_LEDGER_ROOT = Path("runtime") / "ledgers"
EVENT_TYPES = {
    "command_start",
    "command_end",
    "command_failure",
    "lock_acquired",
    "lock_released",
    "validation",
    "artifact_written",
    "backup_created",
    "restore_verified",
}


class RuntimeLedgerError(RuntimeError):
    """Raised when a runtime-ledger event is invalid or cannot be appended."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def default_ledger_path(repo_root: Path, workspace_id: str) -> Path:
    return repo_root / DEFAULT_LEDGER_ROOT / f"{workspace_id}.runtime-ledger.jsonl"


def ledger_metadata_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(ledger_path.name + ".meta.json")


def build_event(
    *,
    workspace_id: str,
    run_id: str,
    event_type: str,
    command: str | None = None,
    status: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    lock_event: dict[str, Any] | None = None,
    validation_posture: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise RuntimeLedgerError(f"unsupported runtime-ledger event_type: {event_type}")
    if not workspace_id or not workspace_id.strip():
        raise RuntimeLedgerError("workspace_id is required for runtime-ledger events")
    if not run_id or not run_id.strip():
        raise RuntimeLedgerError("run_id is required for runtime-ledger events")
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or f"event-{uuid.uuid4()}",
        "run_id": run_id,
        "workspace_id": workspace_id,
        "event_type": event_type,
        "occurred_at": occurred_at or utc_now(),
    }
    optional = {
        "command": command,
        "status": status,
        "inputs": inputs,
        "artifact_refs": artifact_refs,
        "lock_event": lock_event,
        "validation_posture": validation_posture,
        "failure": failure,
    }
    for key, value in optional.items():
        if value is not None:
            event[key] = value
    return event


def append_event(ledger_path: Path, event: dict[str, Any]) -> None:
    if event.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeLedgerError("runtime-ledger event has wrong schema_version")
    for key in ("event_id", "run_id", "workspace_id", "event_type", "occurred_at"):
        if not event.get(key):
            raise RuntimeLedgerError(f"runtime-ledger event missing required key: {key}")
    if event["event_type"] not in EVENT_TYPES:
        raise RuntimeLedgerError(f"unsupported runtime-ledger event_type: {event['event_type']}")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    metadata_path = ledger_metadata_path(ledger_path)
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
    else:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    line_count = metadata.get("line_count")
    if isinstance(line_count, int) and not isinstance(line_count, bool):
        next_line_count = line_count + 1
    else:
        next_line_count = sum(1 for _ in ledger_path.open("r", encoding="utf-8"))
    metadata = {
        "schema_version": LEDGER_METADATA_SCHEMA_VERSION,
        "ledger_path": str(ledger_path),
        "line_count": next_line_count,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_events(ledger_path: Path) -> list[dict[str, Any]]:
    has_trailing_newline = False
    if ledger_path.exists() and ledger_path.stat().st_size > 0:
        with ledger_path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            has_trailing_newline = handle.read(1) == b"\n"
    events: list[dict[str, Any]] = []
    with ledger_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if (not has_trailing_newline) and not raw_line.endswith("\n"):
                    break
                raise RuntimeLedgerError(
                    f"runtime ledger {ledger_path} contains invalid JSON on line {line_number}"
                ) from exc
            if not isinstance(payload, dict):
                raise RuntimeLedgerError(
                    f"runtime ledger {ledger_path} contains a non-object record on line {line_number}"
                )
            events.append(payload)
    return events


def parse_json_object(raw: str | None, label: str) -> Any:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeLedgerError(f"{label} is not valid JSON: line {exc.lineno}") from exc
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append one runtime-ledger.v1 event.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--ledger", type=Path, help="Explicit ledger JSONL path.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--event-type", required=True, choices=sorted(EVENT_TYPES))
    parser.add_argument("--command")
    parser.add_argument("--status")
    parser.add_argument("--inputs-json")
    parser.add_argument("--artifact-refs-json")
    parser.add_argument("--lock-event-json")
    parser.add_argument("--validation-posture-json")
    parser.add_argument("--failure-json")
    parser.add_argument("--occurred-at")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ledger_path = args.ledger or default_ledger_path(args.repo_root, args.workspace_id)
    try:
        event = build_event(
            workspace_id=args.workspace_id,
            run_id=args.run_id,
            event_type=args.event_type,
            command=args.command,
            status=args.status,
            inputs=parse_json_object(args.inputs_json, "--inputs-json"),
            artifact_refs=parse_json_object(args.artifact_refs_json, "--artifact-refs-json"),
            lock_event=parse_json_object(args.lock_event_json, "--lock-event-json"),
            validation_posture=parse_json_object(args.validation_posture_json, "--validation-posture-json"),
            failure=parse_json_object(args.failure_json, "--failure-json"),
            occurred_at=args.occurred_at,
        )
        append_event(ledger_path, event)
    except RuntimeLedgerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "appended", "ledger_path": str(ledger_path), "event_id": event["event_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
