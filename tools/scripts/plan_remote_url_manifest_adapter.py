#!/usr/bin/env python3
"""Dry-run remote URL-manifest source adapter planner with no network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

from tools.common.atomic_write import atomic_write_jsonl, stable_json_text  # noqa: E402
from tools.common.source_adapter_handoff import (  # noqa: E402
    build_remote_url_manifest_handoff_record,
    validate_source_adapter_handoff_record,
)
from tools.common.network_safety_gate import normalized_allowlist_url

import validate_source_adapter  # noqa: E402


ALLOWED_MANIFEST_ENTRY_KEYS = {"url", "title", "notes", "source_id"}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def parse_manifest_entry(raw_line: str) -> dict[str, Any] | None:
    parsed = json.loads(
        raw_line,
        object_pairs_hook=no_duplicate_object_pairs,
        parse_constant=reject_json_constant,
    )
    if not isinstance(parsed, dict):
        return None
    return parsed


class RemoteUrlManifestAdapterError(RuntimeError):
    """Raised when URL-manifest planning inputs are invalid."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, help="Path to a validated remote_url_manifest source-adapter manifest.")
    parser.add_argument("--manifest-jsonl", required=True, help="Local JSONL file containing explicit URL observations.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--handoff-jsonl", type=Path, help="Optional JSONL output path for emitted handoff records.")
    return parser.parse_args()


def resolve_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def load_adapter(adapter_path: Path) -> dict[str, Any]:
    result, exit_code = validate_source_adapter.validate_source_adapter(adapter_path)
    if exit_code != validate_source_adapter.EXIT_PASS:
        message = "source adapter validation failed"
        errors = result.get("errors", [])
        if errors:
            message = errors[0].get("message", message)
        raise RemoteUrlManifestAdapterError(message)
    before_hash = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    payload, parse_errors, parse_exit = validate_source_adapter.load_json_object(adapter_path)
    if parse_exit != validate_source_adapter.EXIT_PASS:
        message = parse_errors[0].get("message", "source adapter parsing failed") if parse_errors else "source adapter parsing failed"
        raise RemoteUrlManifestAdapterError(message)
    after_hash = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    if before_hash != after_hash:
        raise RemoteUrlManifestAdapterError("adapter file changed during validation")
    if payload.get("input_family") != "remote_url_manifest":
        raise RemoteUrlManifestAdapterError("input_family must be remote_url_manifest for this planner")
    return payload


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_http_url(raw_url: str) -> str | None:
    if any(ch.isspace() for ch in raw_url):
        return None
    if not is_http_url(raw_url):
        return None
    parsed = urlparse(raw_url)
    if not (parsed.hostname or ""):
        return None
    return normalized_allowlist_url(raw_url)


def validate_manifest_entry(entry: Any, *, line_number: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["entry must be a JSON object"]
    unknown_keys = sorted(set(entry) - ALLOWED_MANIFEST_ENTRY_KEYS)
    if unknown_keys:
        return [f"unexpected manifest entry field: {unknown_keys[0]}"]
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        errors.append("url must be an absolute http or https URL")
        return errors
    normalized_url = normalize_http_url(url)
    if normalized_url is None:
        errors.append("url must be an absolute http or https URL")
    elif url != normalized_url:
        entry["url"] = normalized_url
    for key in ("title", "notes", "source_id"):
        value = entry.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{key} must be a non-blank string when present")
    return errors


def load_manifest_entries(manifest_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    blockers: list[str] = []

    if not manifest_path.exists():
        return accepted, rejected, [f"manifest JSONL path not found: {manifest_path}"]
    if not manifest_path.is_file():
        return accepted, rejected, [f"manifest JSONL path is not a file: {manifest_path}"]

    for line_number, raw_line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            entry = parse_manifest_entry(raw_line)
        except json.JSONDecodeError:
            rejected.append({"line_number": line_number, "reason": "invalid_json"})
            continue
        except DuplicateJsonKeyError as exc:
            rejected.append({"line_number": line_number, "reason": str(exc)})
            continue
        except NonStandardJsonConstantError as exc:
            rejected.append({"line_number": line_number, "reason": str(exc)})
            continue
        if entry is None:
            rejected.append({"line_number": line_number, "reason": "invalid JSON object"})
            continue
        errors = validate_manifest_entry(entry, line_number=line_number)
        if errors:
            rejected.append({"line_number": line_number, "reason": errors[0]})
            continue
        accepted.append({"line_number": line_number, "entry": entry})

    if not accepted:
        blockers.append("no valid URL manifest entries were accepted")
    return accepted, rejected, blockers


def build_plan(adapter_path: Path, manifest_path: Path, adapter_payload: dict[str, Any]) -> dict[str, Any]:
    adapter_manifest_url = adapter_payload.get("locator", {}).get("manifest_url")
    if not isinstance(adapter_manifest_url, str):
        raise RemoteUrlManifestAdapterError("manifest_url must be a non-blank string")
    normalized_manifest_url = normalize_http_url(adapter_manifest_url)
    if normalized_manifest_url is None:
        raise RemoteUrlManifestAdapterError("manifest_url must be an absolute http or https URL")
    accepted, rejected, blockers = load_manifest_entries(manifest_path)
    handoff_records = [
        build_remote_url_manifest_handoff_record(
            adapter_payload,
            adapter_path=adapter_path,
            manifest_input_path=manifest_path,
            entry=item["entry"],
            sequence=index + 1,
            line_number=item["line_number"],
            manifest_url=normalized_manifest_url,
        )
        for index, item in enumerate(accepted)
    ]
    validation_errors = [
        {"index": index, "errors": validate_source_adapter_handoff_record(record, adapter_payload)}
        for index, record in enumerate(handoff_records)
    ]
    validation_errors = [entry for entry in validation_errors if entry["errors"]]

    payload = {
        "schema_version": "remote-url-manifest-plan.v1",
        "adapter_path": str(adapter_path),
        "manifest_jsonl_path": str(manifest_path),
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "input_family": adapter_payload["input_family"],
        "dry_run": True,
        "network_access_attempted": False,
        "remote_state": "configured_remote",
        "accepted_entry_count": len(accepted),
        "rejected_entry_count": len(rejected),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "accepted_entries": [
            {
                "line_number": item["line_number"],
                "url": item["entry"]["url"],
                "title": item["entry"].get("title"),
                "notes": item["entry"].get("notes"),
                "source_id": item["entry"].get("source_id"),
                "remote_state": "configured_remote",
                "network_access_attempted": False,
            }
            for item in accepted
        ],
        "rejected_entries": rejected,
        "handoff_record_count": len(handoff_records),
        "handoff_records": handoff_records,
        "handoff_validation": {
            "ok": not validation_errors,
            "error_count": len(validation_errors),
            "errors": validation_errors,
        },
    }
    return payload


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"adapter_id={payload['adapter_id']}",
        f"workspace_id={payload['workspace_id']}",
        f"input_family={payload['input_family']}",
        f"accepted_entry_count={payload['accepted_entry_count']}",
        f"rejected_entry_count={payload['rejected_entry_count']}",
        f"blocker_count={payload['blocker_count']}",
        f"handoff_record_count={payload['handoff_record_count']}",
        f"network_access_attempted={'true' if payload['network_access_attempted'] else 'false'}",
        f"remote_state={payload['remote_state']}",
    ]
    for index, blocker in enumerate(payload["blockers"]):
        lines.append(f"blocker[{index}]={blocker}")
    for index, entry in enumerate(payload["accepted_entries"]):
        lines.append(f"accepted[{index}].url={entry['url']}")
    for index, entry in enumerate(payload["rejected_entries"][:20]):
        lines.append(f"rejected[{index}].line_number={entry['line_number']}")
        lines.append(f"rejected[{index}].reason={entry['reason']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    adapter_path = resolve_path(args.adapter, base_dir=Path.cwd())
    manifest_path = resolve_path(args.manifest_jsonl, base_dir=Path.cwd())
    try:
        adapter_payload = load_adapter(adapter_path)
        payload = build_plan(adapter_path, manifest_path, adapter_payload)
        if args.handoff_jsonl is not None:
            atomic_write_jsonl(args.handoff_jsonl, payload["handoff_records"])
    except RemoteUrlManifestAdapterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(stable_json_text(payload))
    else:
        sys.stdout.write(render_text(payload))
    return 1 if not payload["handoff_validation"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
