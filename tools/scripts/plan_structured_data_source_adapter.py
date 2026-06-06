#!/usr/bin/env python3
"""Dry-run structured-data local source adapter planner."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

from tools.common.atomic_write import atomic_write_jsonl, stable_json_text  # noqa: E402
from tools.common.source_adapter_contract import (  # noqa: E402
    LOCAL_ADAPTER_INPUT_FAMILIES,
    STRUCTURED_DATA_FORMATS,
    STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS,
)
from tools.common.source_adapter_handoff import (  # noqa: E402
    build_structured_data_handoff_record,
    validate_source_adapter_handoff_record,
)

import validate_source_adapter  # noqa: E402


FORMAT_SUFFIXES = {
    ".csv": "csv",
    ".json": "json",
    ".jsonl": "jsonl",
    ".xml": "xml",
}


class StructuredDataAdapterError(RuntimeError):
    """Raised when structured-data planning inputs are invalid."""


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


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


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def load_adapter(adapter_path: Path) -> dict[str, Any]:
    result, exit_code = validate_source_adapter.validate_source_adapter(adapter_path)
    if exit_code != validate_source_adapter.EXIT_PASS:
        message = "source adapter validation failed"
        errors = result.get("errors", [])
        if errors:
            message = errors[0].get("message", message)
        raise StructuredDataAdapterError(message)
    payload = json.loads(adapter_path.read_text(encoding="utf-8"))
    if payload.get("input_family") not in LOCAL_ADAPTER_INPUT_FAMILIES:
        raise StructuredDataAdapterError("input_family must be local_file or local_directory for this planner")
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


def infer_structured_format(path: Path, *, format_hint: str | None) -> str | None:
    if format_hint:
        return format_hint
    return FORMAT_SUFFIXES.get(path.suffix.lower())


def enumerate_sources(
    root: Path,
    *,
    input_family: str,
    include_globs: list[str],
    exclude_globs: list[str],
    format_hint: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    sources: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blockers: list[str] = []

    if not root.exists():
        label = "local file root" if input_family == "local_file" else "local directory root"
        return sources, skipped, [f"{label} not found: {root}"]
    if input_family == "local_file":
        if root.is_symlink():
            return sources, skipped, [f"local file root is a symlink: {root}"]
        if not root.is_file():
            return sources, skipped, [f"local file root is not a file: {root}"]
        structured_format = infer_structured_format(root, format_hint=format_hint)
        if structured_format is None:
            return sources, skipped, [f"structured format could not be inferred for file: {root.name}"]
        return [
            {
                "path": root,
                "relative_path": root.name,
                "structured_format": structured_format,
                "size_bytes": root.stat().st_size,
            }
        ], skipped, blockers

    if root.is_symlink():
        return sources, skipped, [f"local directory root is a symlink: {root}"]
    if not root.is_dir():
        return sources, skipped, [f"local directory root is not a directory: {root}"]

    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root).as_posix()
        if path.is_symlink():
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "symlink_not_allowed"})
            continue
        if path.is_dir():
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "not_a_file"})
            continue
        if include_globs and not matches_any_glob(relative_path, include_globs):
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "not_included"})
            continue
        if exclude_globs and matches_any_glob(relative_path, exclude_globs):
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "excluded"})
            continue
        structured_format = infer_structured_format(path, format_hint=format_hint)
        if structured_format is None:
            skipped.append({"path": str(path), "relative_path": relative_path, "reason": "unsupported_format"})
            continue
        sources.append(
            {
                "path": path,
                "relative_path": relative_path,
                "structured_format": structured_format,
                "size_bytes": path.stat().st_size,
            }
        )

    if not sources:
        blockers.append("no structured-data files matched include/exclude globs and format detection")
    return sources, skipped, blockers


def describe_json_value(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "scalar"


def resolve_json_record_path(value: Any, record_path: str | None) -> tuple[Any | None, str | None]:
    if not record_path:
        return value, None
    current = value
    for segment in [part for part in record_path.split(".") if part]:
        if isinstance(current, dict):
            if segment not in current:
                return None, f"record_path could not be resolved: {record_path}"
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return None, f"record_path could not be resolved: {record_path}"
            current = current[index]
            continue
        return None, f"record_path could not be resolved: {record_path}"
    return current, None


def build_xml_path_map(root: ET.Element) -> dict[int, str]:
    path_map: dict[int, str] = {}

    def visit(element: ET.Element, parent_path: str) -> None:
        path_map[id(element)] = parent_path
        sibling_counts: dict[str, int] = {}
        for child in list(element):
            sibling_counts[child.tag] = sibling_counts.get(child.tag, 0) + 1
            child_path = f"{parent_path}/{child.tag}[{sibling_counts[child.tag]}]"
            visit(child, child_path)

    visit(root, f"/{root.tag}[1]")
    return path_map


def parse_csv_records(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                errors.append({"context": "line:1", "reason": "csv header row is missing"})
                return records, errors
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                errors.append({"context": "line:1", "reason": "duplicate CSV header"})
                return records, errors
            for row_index, _ in enumerate(reader, start=1):
                records.append(
                    {
                        "record_locator": f"row:{row_index}",
                        "record_kind": "row",
                    }
                )
    except UnicodeDecodeError:
        errors.append({"context": "file", "reason": "file is not valid UTF-8"})
    except csv.Error as exc:
        line_num = getattr(locals().get("reader", None), "line_num", 1)
        errors.append({"context": f"line:{line_num}", "reason": str(exc)})
    return records, errors


def parse_json_records(path: Path, *, record_path: str | None) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=no_duplicate_object_pairs,
        )
    except UnicodeDecodeError:
        return records, [{"context": "file", "reason": "file is not valid UTF-8"}]
    except DuplicateJsonKeyError as exc:
        return records, [{"context": "line:1", "reason": str(exc)}]
    except json.JSONDecodeError as exc:
        return records, [{"context": f"line:{exc.lineno},column:{exc.colno}", "reason": exc.msg}]

    selected, record_path_error = resolve_json_record_path(payload, record_path)
    if record_path_error is not None:
        return records, [{"context": f"record_path:{record_path}", "reason": record_path_error}]
    if isinstance(selected, list):
        for index, entry in enumerate(selected, start=1):
            records.append({"record_locator": f"index:{index}", "record_kind": describe_json_value(entry)})
        return records, errors
    records.append({"record_locator": "object:1", "record_kind": describe_json_value(selected)})
    return records, errors


def parse_jsonl_records(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return records, [{"context": "file", "reason": "file is not valid UTF-8"}]

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line, object_pairs_hook=no_duplicate_object_pairs)
        except DuplicateJsonKeyError as exc:
            errors.append({"context": f"line:{line_number}", "reason": str(exc)})
            continue
        except json.JSONDecodeError as exc:
            errors.append({"context": f"line:{line_number},column:{exc.colno}", "reason": exc.msg})
            continue
        records.append({"record_locator": f"line:{line_number}", "record_kind": describe_json_value(value)})
    return records, errors


def parse_xml_records(path: Path, *, record_path: str | None) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    try:
        tree = ET.parse(path)
    except UnicodeDecodeError:
        return records, [{"context": "file", "reason": "file is not valid UTF-8"}]
    except ET.ParseError as exc:
        line_number, column = getattr(exc, "position", (1, 1))
        return records, [{"context": f"line:{line_number},column:{column}", "reason": str(exc)}]

    root = tree.getroot()
    path_map = build_xml_path_map(root)
    if record_path:
        matches = root.findall(record_path)
        if not matches:
            return records, [{"context": f"record_path:{record_path}", "reason": "record_path matched no XML elements"}]
    else:
        matches = list(root) or [root]
    for element in matches:
        records.append(
            {
                "record_locator": path_map.get(id(element), f"element:{len(records) + 1}"),
                "record_kind": "element",
            }
        )
    return records, []


def parse_structured_records(
    path: Path,
    *,
    structured_format: str,
    record_path: str | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if structured_format == "csv":
        return parse_csv_records(path)
    if structured_format == "json":
        return parse_json_records(path, record_path=record_path)
    if structured_format == "jsonl":
        return parse_jsonl_records(path)
    if structured_format == "xml":
        return parse_xml_records(path, record_path=record_path)
    return [], [{"context": "file", "reason": f"unsupported structured format: {structured_format}"}]


def build_plan(adapter_path: Path, adapter_payload: dict[str, Any]) -> dict[str, Any]:
    locator = adapter_payload["locator"]
    input_family = adapter_payload["input_family"]
    configured_root = Path(locator["local_path"]).expanduser()
    if not configured_root.is_absolute():
        configured_root = adapter_path.parent / configured_root
    resolved_root = resolve_path(locator["local_path"], base_dir=adapter_path.parent)
    include_globs = list(locator.get("include_globs", []))
    exclude_globs = list(locator.get("exclude_globs", []))
    format_hint = locator.get("format_hint") if isinstance(locator.get("format_hint"), str) else None
    record_path = locator.get("record_path") if isinstance(locator.get("record_path"), str) and locator.get("record_path").strip() else None

    blockers: list[str] = []
    if configured_root.is_symlink():
        blockers.append(f"local directory root is a symlink: {configured_root}")

    if configured_root.is_symlink():
        sources, skipped_entries, enumerate_blockers = [], [], []
    else:
        sources, skipped_entries, enumerate_blockers = enumerate_sources(
            resolved_root,
            input_family=input_family,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            format_hint=format_hint,
        )
    blockers.extend(enumerate_blockers)

    unsupported_fields = sorted(
        set(adapter_payload["normalized_handoff"].get("source_specific_fields", [])) - STRUCTURED_DATA_SOURCE_SPECIFIC_FIELDS
    )
    if unsupported_fields:
        blockers.append(f"unsupported structured-data source_specific field: {unsupported_fields[0]}")

    parse_errors: list[dict[str, Any]] = []
    parsed_sources: list[dict[str, Any]] = []
    handoff_records: list[dict[str, Any]] = []
    sequence = 1

    for source in sources:
        records, errors = parse_structured_records(
            source["path"],
            structured_format=source["structured_format"],
            record_path=record_path,
        )
        parsed_sources.append(
            {
                "resolved_source_path": str(source["path"]),
                "relative_path": source["relative_path"],
                "structured_format": source["structured_format"],
                "size_bytes": source["size_bytes"],
                "record_count": len(records),
                "parse_error_count": len(errors),
            }
        )
        for error in errors:
            parse_errors.append(
                {
                    "resolved_source_path": str(source["path"]),
                    "relative_path": source["relative_path"],
                    "structured_format": source["structured_format"],
                    "context": error["context"],
                    "reason": error["reason"],
                }
            )
        for record in records:
            handoff_records.append(
                build_structured_data_handoff_record(
                    adapter_payload,
                    adapter_path=adapter_path,
                    source_path=source["path"],
                    relative_path=source["relative_path"],
                    sequence=sequence,
                    structured_format=source["structured_format"],
                    record_locator=record["record_locator"],
                    record_kind=record["record_kind"],
                )
            )
            sequence += 1

    if not handoff_records and not blockers:
        blockers.append("no structured records were parsed successfully")

    validation_errors = [
        {"index": index, "errors": validate_source_adapter_handoff_record(record, adapter_payload)}
        for index, record in enumerate(handoff_records)
    ]
    validation_errors = [entry for entry in validation_errors if entry["errors"]]

    return {
        "schema_version": "structured-data-source-adapter-plan.v1",
        "adapter_path": str(adapter_path),
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "input_family": input_family,
        "dry_run": True,
        "resolved_root": str(resolved_root),
        "format_hint": format_hint,
        "record_path": record_path,
        "source_count": len(sources),
        "parsed_record_count": len(handoff_records),
        "parse_error_count": len(parse_errors),
        "skipped_count": len(skipped_entries),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "parsed_sources": parsed_sources,
        "parse_errors": parse_errors,
        "skipped_entries": skipped_entries,
        "handoff_record_count": len(handoff_records),
        "handoff_records": handoff_records,
        "handoff_validation": {
            "ok": not validation_errors,
            "error_count": len(validation_errors),
            "errors": validation_errors,
        },
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"adapter_id={payload['adapter_id']}",
        f"workspace_id={payload['workspace_id']}",
        f"input_family={payload['input_family']}",
        f"source_count={payload['source_count']}",
        f"parsed_record_count={payload['parsed_record_count']}",
        f"parse_error_count={payload['parse_error_count']}",
        f"skipped_count={payload['skipped_count']}",
        f"blocker_count={payload['blocker_count']}",
        f"handoff_record_count={payload['handoff_record_count']}",
        f"handoff_validation_ok={'true' if payload['handoff_validation']['ok'] else 'false'}",
    ]
    for index, blocker in enumerate(payload["blockers"]):
        lines.append(f"blocker[{index}]={blocker}")
    for index, entry in enumerate(payload["parse_errors"][:20]):
        lines.append(f"parse_error[{index}].relative_path={entry['relative_path']}")
        lines.append(f"parse_error[{index}].context={entry['context']}")
        lines.append(f"parse_error[{index}].reason={entry['reason']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    adapter_path = resolve_path(args.adapter, base_dir=Path.cwd())
    try:
        adapter_payload = load_adapter(adapter_path)
        payload = build_plan(adapter_path, adapter_payload)
        if args.handoff_jsonl is not None:
            atomic_write_jsonl(args.handoff_jsonl, payload["handoff_records"])
    except StructuredDataAdapterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(stable_json_text(payload))
    else:
        sys.stdout.write(render_text(payload))
    return 1 if not payload["handoff_validation"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
