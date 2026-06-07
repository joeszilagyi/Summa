#!/usr/bin/env python3
"""Emit a read-only source intake status view model from source-adapter manifests."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

import validate_source_adapter  # type: ignore  # noqa: E402
from tools.common.source_adapter_contract import LOCAL_INPUT_FAMILIES  # type: ignore  # noqa: E402
from tools.source_db_tools import rights_retention  # type: ignore  # noqa: E402

SCHEMA_VERSION = "source-intake-status.v1"
ADAPTER_SCAN_PATTERNS = (
    "*.json",
)


class SourceIntakeStatusError(RuntimeError):
    """Raised when source intake inputs cannot be scanned."""


@dataclass(frozen=True)
class AdapterCandidate:
    path: Path
    adapter_status: str
    payload: dict[str, Any] | None
    detail: str | None
    workspace_id: str | None
    adapter_id: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only source intake status view model from source-adapter manifests."
    )
    parser.add_argument(
        "--adapter",
        action="append",
        default=[],
        help="Path to a source-adapter JSON manifest. Repeat to include multiple adapters.",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Directory to scan for source_adapter JSON manifests.",
    )
    parser.add_argument(
        "--workspace-id",
        action="append",
        default=[],
        dest="workspace_ids",
        help="Optional workspace_id to include. Repeat to narrow the status view.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional maximum number of adapter entries to include after filtering and sorting.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the generated source intake status view.",
    )
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def is_adapter_filename(name: str) -> bool:
    return name == "source_adapter.json" or "source_adapter" in name or "source-adapter" in name


def discover_root_adapters(raw_root: str) -> list[Path]:
    root = resolve_path(raw_root)
    if not root.exists():
        raise SourceIntakeStatusError(f"source adapter root not found: {root}")
    if not root.is_dir():
        raise SourceIntakeStatusError(f"source adapter root is not a directory: {root}")

    found: list[Path] = []
    seen: set[Path] = set()
    for path in root.rglob("*.json"):
        if not is_adapter_filename(path.name):
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    found.sort(key=str)
    return found


def load_adapter_candidate(path: Path) -> AdapterCandidate:
    adapter_status, payload, detail = load_json_object(path)
    workspace_id = string_value(payload.get("workspace_id")) if payload is not None else None
    adapter_id = string_value(payload.get("adapter_id")) if payload is not None else None
    return AdapterCandidate(
        path=path,
        adapter_status=adapter_status,
        payload=payload,
        detail=detail,
        workspace_id=workspace_id,
        adapter_id=adapter_id,
    )


def collect_adapter_candidates(args: argparse.Namespace) -> list[AdapterCandidate]:
    candidates: list[AdapterCandidate] = []
    seen: set[Path] = set()
    for raw_path in args.adapter:
        resolved = resolve_path(raw_path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(load_adapter_candidate(resolved))

    for raw_root in args.root:
        for path in discover_root_adapters(raw_root):
            if path in seen:
                continue
            seen.add(path)
            candidates.append(load_adapter_candidate(path))
    return candidates


def selected_adapter_candidates(args: argparse.Namespace) -> list[AdapterCandidate]:
    candidates = collect_adapter_candidates(args)
    requested = {workspace_id for workspace_id in args.workspace_ids if isinstance(workspace_id, str) and workspace_id.strip()}
    if not requested:
        return candidates

    selected = [candidate for candidate in candidates if candidate.workspace_id in requested]
    selected.sort(key=lambda candidate: (candidate.workspace_id or "", candidate.adapter_id or "", str(candidate.path)))
    return selected


def load_json_object(path: Path) -> tuple[str, dict[str, Any] | None, str | None]:
    if not path.exists():
        return "missing", None, "adapter path does not exist"
    if not path.is_file():
        return "not_file", None, "adapter path is not a file"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return "unreadable", None, f"adapter could not be read: {exc}"
    except json.JSONDecodeError as exc:
        return "invalid_json", None, f"adapter is not valid JSON: line {exc.lineno}"
    if not isinstance(payload, dict):
        return "invalid_manifest", None, "adapter top-level value is not an object"
    return "ok", payload, None


def contract_result_for_load_failure(detail: str | None) -> dict[str, Any]:
    errors = []
    if detail:
        errors.append({"code": "ADAPTER_LOAD_FAILED", "message": detail})
    return {
        "counts": {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0},
        "errors": errors,
        "warnings": [],
    }


def validate_contract_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result, exit_code = validate_source_adapter.validate_source_adapter_payload(payload)
    status = "pass" if exit_code == validate_source_adapter.EXIT_PASS else "fail"
    return status, result


def validate_contract(path: Path) -> tuple[str, dict[str, Any]]:
    adapter_status, payload, detail = load_json_object(path)
    if payload is None:
        return "fail", contract_result_for_load_failure(detail)
    return validate_contract_payload(payload)


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def resolve_local_locator_path(raw_path: str, adapter_path: Path) -> Path | None:
    raw = Path(raw_path).expanduser()
    candidates = [raw] if raw.is_absolute() else [
        (adapter_path.parent / raw).resolve(),
        (Path.cwd() / raw).resolve(),
        (REPO_ROOT / raw).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def local_path_matches_family(path: Path, input_family: str | None) -> bool:
    if input_family == "local_file":
        return path.is_file()
    if input_family in {"local_directory", "local_git_repo"}:
        return path.is_dir()
    return path.exists()


def locator_status(payload: dict[str, Any], adapter_path: Path) -> dict[str, Any]:
    input_family = string_value(payload.get("input_family"))
    locator = dict_value(payload.get("locator"))
    status: dict[str, Any] = {
        "locator_status": "not_declared",
        "locator_kind": input_family,
        "local_path": string_value(locator.get("local_path")),
        "repo_url": string_value(locator.get("repo_url")),
        "manifest_url": string_value(locator.get("manifest_url")),
        "base_url": string_value(locator.get("base_url")),
        "resolved_local_path": None,
    }

    if input_family in LOCAL_INPUT_FAMILIES:
        raw_local_path = string_value(locator.get("local_path"))
        if raw_local_path is None:
            status["locator_status"] = "local_path_missing"
            return status
        resolved = resolve_local_locator_path(raw_local_path, adapter_path)
        if resolved is None:
            status["locator_status"] = "local_path_not_found"
            return status
        status["resolved_local_path"] = str(resolved)
        status["locator_status"] = (
            "reachable" if local_path_matches_family(resolved, input_family) else "local_path_type_mismatch"
        )
        return status

    if input_family in {"remote_git_repo", "remote_url_manifest", "remote_archive_collection"}:
        status["locator_status"] = "configured_remote"
        return status

    return status


def review_required_reasons(payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if payload.get("automation_posture") == "operator_review_required":
        reasons.append("automation_posture:operator_review_required")

    rights = dict_value(payload.get("rights_and_storage"))
    policy_facts = rights_retention.derive_adapter_policy_facts(
        rights,
        input_family=string_value(payload.get("input_family")),
    )
    reasons.extend(policy_facts["review_reasons"])

    for step in list_value(payload.get("transform_lineage")):
        if isinstance(step, dict) and step.get("review_required") is True:
            step_id = string_value(step.get("step_id")) or "unknown_step"
            reasons.append(f"transform_step_review_required:{step_id}")
    return reasons


def public_use_blockers(payload: dict[str, Any]) -> list[str]:
    rights = dict_value(payload.get("rights_and_storage"))
    return rights_retention.derive_adapter_policy_facts(
        rights,
        input_family=string_value(payload.get("input_family")),
    )["public_export_blockers"]


def intake_state_for(
    *,
    adapter_status: str,
    contract_status: str,
    locator: dict[str, Any],
    review_reasons: list[str],
) -> str:
    if adapter_status != "ok" or contract_status != "pass":
        return "failed"
    if review_reasons:
        return "needs_review"
    if locator["locator_status"] == "reachable":
        return "reachable"
    return "configured"


def transform_summary(payload: dict[str, Any]) -> dict[str, Any]:
    steps = [step for step in list_value(payload.get("transform_lineage")) if isinstance(step, dict)]
    final_step = steps[-1] if steps else {}
    return {
        "step_count": len(steps),
        "review_required_step_count": sum(1 for step in steps if step.get("review_required") is True),
        "final_step_kind": final_step.get("step_kind") if isinstance(final_step.get("step_kind"), str) else None,
    }


def invalid_entry(path: Path, adapter_status: str, detail: str | None, contract_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "adapter_path": str(path),
        "adapter_status": adapter_status,
        "contract_status": "fail",
        "intake_state": "failed",
        "status_detail": detail,
        "adapter_id": None,
        "display_name": None,
        "workspace_id": None,
        "input_family": None,
        "locator": {"locator_status": adapter_status, "locator_kind": None, "resolved_local_path": None},
        "content_profile": {"content_kinds": [], "hazard_flags": []},
        "rights_and_storage": {},
        "automation_posture": None,
        "normalized_handoff": {},
        "transform_lineage": {"step_count": 0, "review_required_step_count": 0, "final_step_kind": None},
        "review_required_reasons": [],
        "public_use_blockers": [],
        "public_export_eligibility": None,
        "quote_eligibility": None,
        "validation": {
            "counts": dict_value(contract_result.get("counts")),
            "error_count": len(list_value(contract_result.get("errors"))),
            "warning_count": len(list_value(contract_result.get("warnings"))),
            "errors": list_value(contract_result.get("errors"))[:3],
            "warnings": list_value(contract_result.get("warnings"))[:3],
        },
    }


def adapter_entry(candidate: AdapterCandidate) -> dict[str, Any]:
    if candidate.payload is None:
        return invalid_entry(
            candidate.path,
            candidate.adapter_status,
            candidate.detail,
            contract_result_for_load_failure(candidate.detail),
        )

    contract_status, contract_result = validate_contract_payload(candidate.payload)
    payload = candidate.payload

    locator = locator_status(payload, candidate.path)
    review_reasons = review_required_reasons(payload)
    blockers = public_use_blockers(payload)
    content_profile = dict_value(payload.get("content_profile"))
    rights = dict_value(payload.get("rights_and_storage"))
    handoff = dict_value(payload.get("normalized_handoff"))
    policy_facts = rights_retention.derive_adapter_policy_facts(
        rights,
        input_family=string_value(payload.get("input_family")),
    )
    intake_state = intake_state_for(
        adapter_status=candidate.adapter_status,
        contract_status=contract_status,
        locator=locator,
        review_reasons=review_reasons,
    )
    return {
        "adapter_path": str(candidate.path),
        "adapter_status": candidate.adapter_status,
        "contract_status": contract_status,
        "intake_state": intake_state,
        "status_detail": candidate.detail,
        "adapter_id": payload.get("adapter_id"),
        "display_name": payload.get("display_name"),
        "workspace_id": payload.get("workspace_id"),
        "input_family": payload.get("input_family"),
        "locator": locator,
        "content_profile": {
            "content_kinds": list_value(content_profile.get("content_kinds")),
            "hazard_flags": list_value(content_profile.get("hazard_flags")),
        },
        "rights_and_storage": {
            "payload_storage_policy_class": rights.get("payload_storage_policy_class"),
            "metadata_storage_policy_class": rights.get("metadata_storage_policy_class"),
            "rights_posture": rights.get("rights_posture"),
            "contains_personal_data": rights.get("contains_personal_data"),
        },
        "automation_posture": payload.get("automation_posture"),
        "normalized_handoff": {
            "record_family": handoff.get("record_family"),
            "batch_unit": handoff.get("batch_unit"),
            "preserve_fields": list_value(handoff.get("preserve_fields")),
            "source_specific_fields": list_value(handoff.get("source_specific_fields")),
        },
        "transform_lineage": transform_summary(payload),
        "review_required_reasons": review_reasons,
        "public_use_blockers": blockers,
        "public_export_eligibility": policy_facts["public_export_eligibility"],
        "quote_eligibility": policy_facts["quote_eligibility"],
        "validation": {
            "counts": dict_value(contract_result.get("counts")),
            "error_count": len(list_value(contract_result.get("errors"))),
            "warning_count": len(list_value(contract_result.get("warnings"))),
            "errors": list_value(contract_result.get("errors"))[:3],
            "warnings": list_value(contract_result.get("warnings"))[:3],
        },
    }


def count_by(entries: list[dict[str, Any]], field_path: tuple[str, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for entry in entries:
        value: Any = entry
        for field in field_path:
            value = value.get(field) if isinstance(value, dict) else None
        key = value if isinstance(value, str) and value else "(unknown)"
        counts[key] += 1
    return dict(sorted(counts.items()))


def build_source_intake_status_payload(args: argparse.Namespace) -> dict[str, Any]:
    if not args.adapter and not args.root:
        raise SourceIntakeStatusError("at least one --adapter or --root is required")
    if args.limit < 0:
        raise SourceIntakeStatusError("--limit must be greater than or equal to zero")
    paths = selected_adapter_candidates(args)
    entries = sorted(
        [adapter_entry(candidate) for candidate in paths],
        key=lambda entry: (entry["workspace_id"] or "", entry["adapter_id"] or "", entry["adapter_path"]),
    )
    if args.limit:
        entries = entries[: args.limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "adapter_paths": [str(resolve_path(raw_path)) for raw_path in args.adapter],
            "roots": [str(resolve_path(raw_root)) for raw_root in args.root],
            "workspace_ids": list(args.workspace_ids),
            "limit": args.limit,
            "scan_patterns": list(ADAPTER_SCAN_PATTERNS),
        },
        "counts": {
            "total_adapters": len(entries),
            "contract_pass": sum(1 for entry in entries if entry["contract_status"] == "pass"),
            "contract_fail": sum(1 for entry in entries if entry["contract_status"] != "pass"),
            "needs_review": sum(1 for entry in entries if entry["intake_state"] == "needs_review"),
            "reachable": sum(1 for entry in entries if entry["intake_state"] == "reachable"),
            "configured": sum(1 for entry in entries if entry["intake_state"] == "configured"),
            "failed": sum(1 for entry in entries if entry["intake_state"] == "failed"),
            "by_intake_state": count_by(entries, ("intake_state",)),
            "by_input_family": count_by(entries, ("input_family",)),
            "by_locator_status": count_by(entries, ("locator", "locator_status")),
        },
        "adapters": entries,
    }


def text_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\n", " ").replace("\t", " ")


def render_text(payload: dict[str, Any]) -> str:
    return "\n".join(iter_text_lines(payload)) + "\n"


def iter_text_lines(payload: dict[str, Any]):
    counts = payload["counts"]
    yield from [
        f"schema_version={payload['schema_version']}",
        f"total_adapters={counts['total_adapters']}",
        f"contract_pass={counts['contract_pass']}",
        f"contract_fail={counts['contract_fail']}",
        f"needs_review={counts['needs_review']}",
        f"reachable={counts['reachable']}",
        f"configured={counts['configured']}",
        f"failed={counts['failed']}",
    ]
    for index, adapter in enumerate(payload["adapters"]):
        yield f"adapter[{index}].adapter_path={adapter['adapter_path']}"
        yield f"adapter[{index}].adapter_id={text_value(adapter['adapter_id'])}"
        yield f"adapter[{index}].workspace_id={text_value(adapter['workspace_id'])}"
        yield f"adapter[{index}].intake_state={adapter['intake_state']}"
        yield f"adapter[{index}].contract_status={adapter['contract_status']}"
        yield f"adapter[{index}].locator_status={adapter['locator']['locator_status']}"


def main() -> int:
    args = parse_args()
    try:
        payload = build_source_intake_status_payload(args)
    except SourceIntakeStatusError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        for line in iter_text_lines(payload):
            sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
