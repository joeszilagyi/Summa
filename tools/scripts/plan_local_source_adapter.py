#!/usr/bin/env python3
"""Dry-run local source adapter enumerator and handoff planner."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

from tools.common.atomic_write import atomic_write_jsonl  # noqa: E402
from tools.common.source_adapter_contract import (  # noqa: E402
    LOCAL_ADAPTER_INPUT_FAMILIES,
    LOCAL_SOURCE_SPECIFIC_FIELDS,
)
from tools.common.source_adapter_handoff import (  # noqa: E402
    build_local_handoff_record,
    validate_source_adapter_handoff_record,
)

import validate_source_adapter  # noqa: E402


class LocalSourceAdapterError(RuntimeError):
    """Raised when local adapter planning inputs are invalid."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, help="Path to a validated local source-adapter manifest.")
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
        raise LocalSourceAdapterError(message)
    payload = json.loads(adapter_path.read_text(encoding="utf-8"))
    if payload.get("input_family") not in LOCAL_ADAPTER_INPUT_FAMILIES:
        raise LocalSourceAdapterError("input_family must be local_file or local_directory for this planner")
    return payload


def matches_any_glob(relative_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    path_obj = PurePosixPath(relative_path)
    for pattern in patterns:
        if path_obj.match(pattern) or fnmatch.fnmatch(relative_path, pattern):
            return True
        if pattern.startswith("**/") and path_obj.match(pattern[3:]):
            return True
    return False


def enumerate_directory(
    root: Path,
    *,
    include_globs: list[str],
    exclude_globs: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blockers: list[str] = []

    if not root.exists():
        return candidates, skipped, [f"local directory root not found: {root}"]
    if not root.is_dir():
        return candidates, skipped, [f"local directory root is not a directory: {root}"]

    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root).as_posix()
        if path.is_dir():
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "not_a_file"})
            continue
        if include_globs and not matches_any_glob(relative_path, include_globs):
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "not_included"})
            continue
        if exclude_globs and matches_any_glob(relative_path, exclude_globs):
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "excluded"})
            continue
        candidates.append({"path": path, "relative_path": relative_path, "size_bytes": path.stat().st_size})

    if not candidates:
        blockers.append("no candidate files matched include/exclude globs")
    return candidates, skipped, blockers


def enumerate_file(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if not root.exists():
        return [], [], [f"local file root not found: {root}"]
    if not root.is_file():
        return [], [], [f"local file root is not a file: {root}"]
    return [{"path": root, "relative_path": root.name, "size_bytes": root.stat().st_size}], [], []


def build_plan(adapter_path: Path, adapter_payload: dict[str, Any]) -> dict[str, Any]:
    locator = adapter_payload["locator"]
    input_family = adapter_payload["input_family"]
    resolved_root = resolve_path(locator["local_path"], base_dir=adapter_path.parent)
    include_globs = list(locator.get("include_globs", []))
    exclude_globs = list(locator.get("exclude_globs", []))

    if input_family == "local_directory":
        candidates, skipped, blockers = enumerate_directory(
            resolved_root,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
        )
    else:
        candidates, skipped, blockers = enumerate_file(resolved_root)

    unsupported_fields = sorted(set(adapter_payload["normalized_handoff"].get("source_specific_fields", [])) - LOCAL_SOURCE_SPECIFIC_FIELDS)
    if unsupported_fields:
        blockers.append(f"unsupported local source_specific field: {unsupported_fields[0]}")

    handoff_records = [
        build_local_handoff_record(
            adapter_payload,
            adapter_path=adapter_path,
            source_path=entry["path"],
            relative_path=entry["relative_path"],
            sequence=index + 1,
        )
        for index, entry in enumerate(candidates)
    ]
    validation_errors = [
        {
            "index": index,
            "errors": validate_source_adapter_handoff_record(record, adapter_payload),
        }
        for index, record in enumerate(handoff_records)
    ]
    validation_errors = [entry for entry in validation_errors if entry["errors"]]

    payload = {
        "schema_version": "local-source-adapter-plan.v1",
        "adapter_path": str(adapter_path),
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "input_family": input_family,
        "dry_run": True,
        "resolved_root": str(resolved_root),
        "include_globs": include_globs,
        "exclude_globs": exclude_globs,
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "candidates": [
            {
                "resolved_source_path": str(entry["path"]),
                "relative_path": entry["relative_path"],
                "size_bytes": entry["size_bytes"],
            }
            for entry in candidates
        ],
        "skipped_entries": skipped,
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
        f"candidate_count={payload['candidate_count']}",
        f"skipped_count={payload['skipped_count']}",
        f"blocker_count={payload['blocker_count']}",
        f"handoff_record_count={payload['handoff_record_count']}",
        f"handoff_validation_ok={'true' if payload['handoff_validation']['ok'] else 'false'}",
    ]
    for index, blocker in enumerate(payload["blockers"]):
        lines.append(f"blocker[{index}]={blocker}")
    for index, candidate in enumerate(payload["candidates"]):
        lines.append(f"candidate[{index}].relative_path={candidate['relative_path']}")
    for index, skipped in enumerate(payload["skipped_entries"][:20]):
        lines.append(f"skipped[{index}].relative_path={skipped['relative_path']}")
        lines.append(f"skipped[{index}].reason={skipped['reason']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    adapter_path = resolve_path(args.adapter, base_dir=Path.cwd())
    try:
        adapter_payload = load_adapter(adapter_path)
        payload = build_plan(adapter_path, adapter_payload)
        if args.handoff_jsonl is not None:
            atomic_write_jsonl(args.handoff_jsonl, payload["handoff_records"])
    except LocalSourceAdapterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 1 if not payload["handoff_validation"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
