#!/usr/bin/env python3
"""Validate topic workspace registry JSON files against the current contract.

Reads the registry, referenced workspace roots, domain packs, and default subject
manifests. With --report-json or --report-text, writes validator reports.
Documentation: tools/validators/README.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

from common import (
    EXIT_INPUT_UNAVAILABLE,
    EXIT_PASS,
    EXIT_VALIDATION_FAILED,
    add_report_args,
    display_path,
    emit_report,
    is_rfc3339_datetime,
    render_text_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import validate_subject_manifest  # noqa: E402

from tools.common.topic_workspace_registry import resolve_existing_path  # noqa: E402

VALIDATOR_NAME = "topic_workspace_registry"
CONTRACT_VERSION = "1"
TOPIC_WORKSPACE_SCHEMA_VERSION = "topic-workspace-registry.v1"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
DOMAIN_PACK_ROOT = REPO_ROOT / "config" / "domain_packs"

REQUIRED_KEYS = {
    "schema_version",
    "workspaces",
}

OPTIONAL_KEYS = {
    "default_workspace_id",
    "notes",
}

ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS

WORKSPACE_REQUIRED_KEYS = {
    "workspace_id",
    "topic_label",
    "workspace_root",
    "domain_pack",
    "lifecycle_state",
    "schedule_posture",
    "workspace_policy_class",
}

WORKSPACE_OPTIONAL_KEYS = {
    "default_subject_manifest",
    "scheduler_policy",
    "notes",
}

WORKSPACE_ALLOWED_KEYS = WORKSPACE_REQUIRED_KEYS | WORKSPACE_OPTIONAL_KEYS

ALLOWED_LIFECYCLE_STATES = {"bootstrap", "active", "paused", "archived"}
ALLOWED_SCHEDULE_POSTURES = {"manual", "scheduled", "paused"}
ALLOWED_WORKSPACE_POLICY_CLASSES = {
    "private_local",
    "mixed_private_public",
    "public_safe_release",
}
ALLOWED_FAILURE_STATUSES = {"healthy", "retryable", "blocked"}
SCHEDULER_POLICY_ALLOWED_KEYS = {
    "run_budget",
    "retry_policy",
    "failure_state",
    "saturation_state",
    "extensions",
}
RUN_BUDGET_ALLOWED_KEYS = {"max_attempts", "max_runtime_seconds"}
RETRY_POLICY_ALLOWED_KEYS = {"max_retryable_failures", "backoff_seconds"}
FAILURE_STATE_ALLOWED_KEYS = {
    "status",
    "attempt_count",
    "last_failure_at",
    "next_retry_at",
    "last_failure_reason",
    "blocked_reason",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a topic workspace registry JSON file, including workspace "
            "root and subject-manifest consistency checks."
        )
    )
    parser.add_argument("target", help="Path to the topic workspace registry JSON file.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    line: int | None = None,
) -> None:
    errors.append({"code": code, "line": line, "message": message})


def repo_display(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return display_path(str(path)) or str(path)


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def load_json_object(target: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []

    if not target.exists():
        add_error(errors, code="INPUT_NOT_FOUND", message="input path does not exist")
        return None, errors, EXIT_INPUT_UNAVAILABLE

    if not target.is_file():
        add_error(errors, code="INPUT_NOT_FILE", message="input path is not a file")
        return None, errors, EXIT_INPUT_UNAVAILABLE

    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        add_error(errors, code="INPUT_UNREADABLE", message="input file could not be read")
        return None, errors, EXIT_INPUT_UNAVAILABLE

    try:
        payload = json.loads(
            raw_text,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except DuplicateJsonKeyError as exc:
        add_error(
            errors,
            code="DUPLICATE_JSON_KEY",
            line=1,
            message=str(exc),
        )
        return None, errors, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(
            errors,
            code="NON_STANDARD_JSON_CONSTANT",
            line=1,
            message=str(exc),
        )
        return None, errors, EXIT_VALIDATION_FAILED
    except json.JSONDecodeError as exc:
        add_error(
            errors,
            code="JSON_PARSE_ERROR",
            line=exc.lineno,
            message="invalid JSON syntax",
        )
        return None, errors, EXIT_VALIDATION_FAILED

    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED

    return payload, errors, EXIT_PASS


def validate_identifier(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str = "INVALID_IDENTIFIER",
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        add_error(
            errors,
            code=code,
            message=f"{field} must match ^[a-z0-9][a-z0-9._-]*$",
        )


def validate_nonblank_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str = "INVALID_STRING",
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")


def validate_string_array(
    payload: dict[str, Any],
    field: str,
    *,
    errors: list[dict[str, Any]],
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, list):
        add_error(errors, code="FIELD_NOT_ARRAY", message=f"{field} must be an array")
        return
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(
                errors,
                code="INVALID_ARRAY_ITEM",
                message=f"{field}[{index}] must be a non-blank string",
            )
            continue
        if item in seen:
            add_error(
                errors,
                code="DUPLICATE_ARRAY_ITEM",
                message=f"{field} contains a duplicate value: {item}",
            )
            continue
        seen.add(item)


def validate_enum_string(
    payload: dict[str, Any],
    field: str,
    allowed_values: set[str],
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or value not in allowed_values:
        add_error(
            errors,
            code=code,
            message=f"{field} must be one of: {', '.join(sorted(allowed_values))}",
        )


def validate_positive_integer(
    payload: dict[str, Any],
    field: str,
    *,
    errors: list[dict[str, Any]],
    code: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        add_error(errors, code=code, message=f"{field} must be an integer >= 1")


def validate_nonnegative_integer(
    payload: dict[str, Any],
    field: str,
    *,
    errors: list[dict[str, Any]],
    code: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        add_error(errors, code=code, message=f"{field} must be an integer >= 0")


def validate_timestamp_string(
    payload: dict[str, Any],
    field: str,
    *,
    errors: list[dict[str, Any]],
    code: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank timestamp string")
        return
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or not is_rfc3339_datetime(value) or parsed.tzinfo is None:
        add_error(errors, code=code, message=f"{field} must be an RFC3339 timestamp")


def validate_scheduler_policy(
    workspace: dict[str, Any],
    *,
    errors: list[dict[str, Any]],
) -> None:
    scheduler_policy = workspace.get("scheduler_policy")
    if scheduler_policy is None:
        return
    if not isinstance(scheduler_policy, dict):
        add_error(errors, code="INVALID_SCHEDULER_POLICY", message="scheduler_policy must be an object")
        return

    unknown_policy_keys = sorted(set(scheduler_policy) - SCHEDULER_POLICY_ALLOWED_KEYS)
    for key in unknown_policy_keys:
        add_error(
            errors,
            code="UNKNOWN_SCHEDULER_POLICY_FIELD",
            message=f"unexpected scheduler_policy field: {key}",
        )

    run_budget = scheduler_policy.get("run_budget")
    if run_budget is not None:
        if not isinstance(run_budget, dict):
            add_error(errors, code="INVALID_RUN_BUDGET", message="scheduler_policy.run_budget must be an object")
        else:
            unknown_run_budget_keys = sorted(set(run_budget) - RUN_BUDGET_ALLOWED_KEYS)
            for key in unknown_run_budget_keys:
                add_error(
                    errors,
                    code="UNKNOWN_RUN_BUDGET_FIELD",
                    message=f"unexpected scheduler_policy.run_budget field: {key}",
                )
            if not ({"max_attempts", "max_runtime_seconds"} & set(run_budget)):
                add_error(
                    errors,
                    code="RUN_BUDGET_EMPTY",
                    message="scheduler_policy.run_budget must include max_attempts or max_runtime_seconds",
                )
            validate_positive_integer(
                run_budget,
                "max_attempts",
                errors=errors,
                code="INVALID_RUN_BUDGET_MAX_ATTEMPTS",
            )
            validate_positive_integer(
                run_budget,
                "max_runtime_seconds",
                errors=errors,
                code="INVALID_RUN_BUDGET_MAX_RUNTIME_SECONDS",
            )

    retry_policy = scheduler_policy.get("retry_policy")
    if retry_policy is not None:
        if not isinstance(retry_policy, dict):
            add_error(errors, code="INVALID_RETRY_POLICY", message="scheduler_policy.retry_policy must be an object")
        else:
            unknown_retry_keys = sorted(set(retry_policy) - RETRY_POLICY_ALLOWED_KEYS)
            for key in unknown_retry_keys:
                add_error(
                    errors,
                    code="UNKNOWN_RETRY_POLICY_FIELD",
                    message=f"unexpected scheduler_policy.retry_policy field: {key}",
                )
            if not ({"max_retryable_failures", "backoff_seconds"} & set(retry_policy)):
                add_error(
                    errors,
                    code="RETRY_POLICY_EMPTY",
                    message="scheduler_policy.retry_policy must include max_retryable_failures or backoff_seconds",
                )
            validate_positive_integer(
                retry_policy,
                "max_retryable_failures",
                errors=errors,
                code="INVALID_MAX_RETRYABLE_FAILURES",
            )
            validate_positive_integer(
                retry_policy,
                "backoff_seconds",
                errors=errors,
                code="INVALID_BACKOFF_SECONDS",
            )

    failure_state = scheduler_policy.get("failure_state")
    if failure_state is not None:
        if not isinstance(failure_state, dict):
            add_error(errors, code="INVALID_FAILURE_STATE", message="scheduler_policy.failure_state must be an object")
        else:
            unknown_failure_keys = sorted(set(failure_state) - FAILURE_STATE_ALLOWED_KEYS)
            for key in unknown_failure_keys:
                add_error(
                    errors,
                    code="UNKNOWN_FAILURE_STATE_FIELD",
                    message=f"unexpected scheduler_policy.failure_state field: {key}",
                )
            for key in ("status", "attempt_count"):
                if key not in failure_state:
                    add_error(
                        errors,
                        code="MISSING_FAILURE_STATE_KEY",
                        message=f"missing scheduler_policy.failure_state key: {key}",
                    )
            validate_enum_string(
                failure_state,
                "status",
                ALLOWED_FAILURE_STATUSES,
                errors,
                code="INVALID_FAILURE_STATUS",
            )
            validate_nonnegative_integer(
                failure_state,
                "attempt_count",
                errors=errors,
                code="INVALID_FAILURE_ATTEMPT_COUNT",
            )
            validate_timestamp_string(
                failure_state,
                "last_failure_at",
                errors=errors,
                code="INVALID_LAST_FAILURE_AT",
            )
            validate_timestamp_string(
                failure_state,
                "next_retry_at",
                errors=errors,
                code="INVALID_NEXT_RETRY_AT",
            )
            validate_nonblank_string(
                failure_state,
                "last_failure_reason",
                errors,
                code="INVALID_LAST_FAILURE_REASON",
            )
            validate_nonblank_string(
                failure_state,
                "blocked_reason",
                errors,
                code="INVALID_BLOCKED_REASON",
            )

    saturation_state = scheduler_policy.get("saturation_state")
    if saturation_state is not None:
        if not isinstance(saturation_state, dict):
            add_error(
                errors,
                code="INVALID_SATURATION_STATE",
                message="scheduler_policy.saturation_state must be an object",
            )
        else:
            for key in sorted(
                set(saturation_state)
                - {
                    "state",
                    "reason_codes",
                    "scheduler_action",
                    "policy_id",
                    "evaluated_at",
                    "next_eligible_cycle",
                    "recent_yield_summary",
                    "extensions",
                }
            ):
                add_error(
                    errors,
                    code="UNKNOWN_SATURATION_STATE_FIELD",
                    message=f"unexpected scheduler_policy.saturation_state field: {key}",
                )

    extensions = scheduler_policy.get("extensions")
    if extensions is not None and not isinstance(extensions, dict):
        add_error(
            errors,
            code="INVALID_SCHEDULER_POLICY_EXTENSIONS",
            message="scheduler_policy.extensions must be an object",
        )


def load_domain_pack(
    domain_pack: str,
    errors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    pack_path = DOMAIN_PACK_ROOT / f"{domain_pack}.json"
    if not pack_path.is_file():
        add_error(
            errors,
            code="DOMAIN_PACK_NOT_FOUND",
            message=f"domain pack file not found: {repo_display(pack_path)}",
        )
        return None

    try:
        payload = json.loads(
            pack_path.read_text(encoding="utf-8"),
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        DuplicateJsonKeyError,
        NonStandardJsonConstantError,
        json.JSONDecodeError,
    ):
        add_error(
            errors,
            code="DOMAIN_PACK_INVALID",
            message=f"domain pack file could not be parsed: {repo_display(pack_path)}",
        )
        return None

    if not isinstance(payload, dict):
        add_error(
            errors,
            code="DOMAIN_PACK_INVALID",
            message=f"domain pack file must contain a JSON object: {repo_display(pack_path)}",
        )
        return None

    return payload


def validate_workspace_root(
    workspace: dict[str, Any],
    *,
    target: Path,
    errors: list[dict[str, Any]],
) -> Path | None:
    raw_root = workspace.get("workspace_root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        return None

    resolved_root = resolve_existing_path(raw_root, target)
    if resolved_root is None:
        add_error(
            errors,
            code="WORKSPACE_ROOT_NOT_FOUND",
            message=f"workspace_root path not found: {raw_root}",
        )
        return None

    if not resolved_root.is_dir():
        add_error(
            errors,
            code="WORKSPACE_ROOT_NOT_DIRECTORY",
            message=f"workspace_root path is not a directory: {raw_root}",
        )
        return None

    return resolved_root


def validate_default_subject_manifest(
    workspace: dict[str, Any],
    *,
    target: Path,
    errors: list[dict[str, Any]],
    manifest_payload_cache: dict[Path, dict[str, Any]],
) -> Path | None:
    raw_manifest = workspace.get("default_subject_manifest")
    if raw_manifest is None:
        return None

    if not isinstance(raw_manifest, str) or not raw_manifest.strip():
        add_error(
            errors,
            code="INVALID_DEFAULT_SUBJECT_MANIFEST",
            message="default_subject_manifest must be a non-blank string",
        )
        return None

    resolved_manifest = resolve_existing_path(raw_manifest, target)
    if resolved_manifest is None:
        add_error(
            errors,
            code="SUBJECT_MANIFEST_NOT_FOUND",
            message=f"default subject manifest path not found: {raw_manifest}",
        )
        return None

    if not resolved_manifest.is_file():
        add_error(
            errors,
            code="SUBJECT_MANIFEST_NOT_FILE",
            message=f"default subject manifest path is not a file: {raw_manifest}",
        )
        return None

    manifest_payload, manifest_errors, _ = validate_subject_manifest.load_json_object(resolved_manifest)
    if manifest_payload is None:
        if manifest_errors:
            first_error = manifest_errors[0]
            add_error(
                errors,
                code="SUBJECT_MANIFEST_INVALID",
                message=(
                    "default subject manifest failed validation: "
                    f"{first_error.get('message', 'validation failed')}"
                ),
            )
        else:
            add_error(
                errors,
                code="SUBJECT_MANIFEST_INVALID",
                message="default subject manifest failed validation",
            )
        return None
    result, exit_code = validate_subject_manifest.validate_manifest_payload(manifest_payload)
    if exit_code != validate_subject_manifest.EXIT_PASS:
        validation_errors = result.get("errors", [])
        if validation_errors:
            first_error = validation_errors[0]
            add_error(
                errors,
                code="SUBJECT_MANIFEST_INVALID",
                message=(
                    "default subject manifest failed validation: "
                    f"{first_error.get('message', 'validation failed')}"
                ),
            )
        else:
            add_error(
                errors,
                code="SUBJECT_MANIFEST_INVALID",
                message="default subject manifest failed validation",
            )
        return None

    manifest_payload_cache[resolved_manifest] = manifest_payload

    return resolved_manifest


def validate_workspace_record(
    workspace: dict[str, Any],
    *,
    target: Path,
    errors: list[dict[str, Any]],
    manifest_payload_cache: dict[Path, dict[str, Any]],
) -> tuple[str | None, Path | None]:
    workspace_id = workspace.get("workspace_id")

    unknown_keys = sorted(set(workspace) - WORKSPACE_ALLOWED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_WORKSPACE_FIELD", message=f"unexpected workspace field: {key}")

    for key in sorted(WORKSPACE_REQUIRED_KEYS):
        if key not in workspace:
            add_error(errors, code="MISSING_WORKSPACE_KEY", message=f"missing required workspace key: {key}")

    validate_identifier(workspace, "workspace_id", errors, code="INVALID_WORKSPACE_ID")
    validate_nonblank_string(workspace, "topic_label", errors)
    validate_nonblank_string(workspace, "workspace_root", errors)
    validate_identifier(workspace, "domain_pack", errors)
    validate_nonblank_string(workspace, "default_subject_manifest", errors)
    validate_string_array(workspace, "notes", errors=errors)
    validate_scheduler_policy(workspace, errors=errors)

    validate_enum_string(
        workspace,
        "lifecycle_state",
        ALLOWED_LIFECYCLE_STATES,
        errors,
        code="INVALID_LIFECYCLE_STATE",
    )
    validate_enum_string(
        workspace,
        "schedule_posture",
        ALLOWED_SCHEDULE_POSTURES,
        errors,
        code="INVALID_SCHEDULE_POSTURE",
    )
    validate_enum_string(
        workspace,
        "workspace_policy_class",
        ALLOWED_WORKSPACE_POLICY_CLASSES,
        errors,
        code="INVALID_WORKSPACE_POLICY_CLASS",
    )

    domain_pack = workspace.get("domain_pack")
    if isinstance(domain_pack, str) and ID_PATTERN.fullmatch(domain_pack):
        load_domain_pack(domain_pack, errors)

    resolved_root = validate_workspace_root(workspace, target=target, errors=errors)
    resolved_manifest = validate_default_subject_manifest(
        workspace,
        target=target,
        errors=errors,
        manifest_payload_cache=manifest_payload_cache,
    )

    if resolved_manifest is not None and isinstance(domain_pack, str) and ID_PATTERN.fullmatch(domain_pack):
        manifest_payload = manifest_payload_cache.get(resolved_manifest)
        if manifest_payload is None:
            add_error(
                errors,
                code="SUBJECT_MANIFEST_INVALID",
                message=(
                    "default subject manifest payload was not cached for domain-pack "
                    f"cross-check: {repo_display(resolved_manifest)}"
                ),
            )
            return workspace_id if isinstance(workspace_id, str) else None, resolved_root

        manifest_domain_pack = manifest_payload.get("domain_pack")
        if manifest_domain_pack != domain_pack:
            add_error(
                errors,
                code="SUBJECT_MANIFEST_DOMAIN_PACK_MISMATCH",
                message=(
                    "default subject manifest domain_pack does not match workspace domain_pack: "
                    f"{manifest_domain_pack} != {domain_pack}"
                ),
            )

    return workspace_id if isinstance(workspace_id, str) else None, resolved_root


def validate_topic_workspace_registry(
    target: Path,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    manifest_payload_cache: dict[Path, dict[str, Any]] = {}

    if payload is None:
        payload, errors, exit_code = load_json_object(target)
        if payload is None:
            return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code
    else:
        errors = []
        exit_code = EXIT_PASS

    counts["inspected"] = 1

    unknown_keys = sorted(set(payload) - ALLOWED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")

    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != TOPIC_WORKSPACE_SCHEMA_VERSION:
        add_error(
            errors,
            code="INVALID_SCHEMA_VERSION",
            message=f"schema_version must equal {TOPIC_WORKSPACE_SCHEMA_VERSION}",
        )

    validate_identifier(payload, "default_workspace_id", errors, code="INVALID_DEFAULT_WORKSPACE_ID")
    validate_string_array(payload, "notes", errors=errors)

    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list):
        add_error(errors, code="WORKSPACES_NOT_ARRAY", message="workspaces must be an array")
    elif not workspaces:
        add_error(errors, code="WORKSPACES_EMPTY", message="workspaces must contain at least one entry")

    seen_workspace_ids: set[str] = set()
    seen_workspace_roots: set[Path] = set()
    workspace_ids: set[str] = set()

    if isinstance(workspaces, list):
        for index, workspace in enumerate(workspaces):
            if not isinstance(workspace, dict):
                add_error(
                    errors,
                    code="WORKSPACE_OBJECT_REQUIRED",
                    message=f"workspaces[{index}] must be a JSON object",
                )
                continue

            workspace_errors_start = len(errors)
            workspace_id, resolved_root = validate_workspace_record(
                workspace,
                target=target,
                errors=errors,
                manifest_payload_cache=manifest_payload_cache,
            )
            if workspace_id:
                workspace_ids.add(workspace_id)
                if workspace_id in seen_workspace_ids:
                    add_error(
                        errors,
                        code="DUPLICATE_WORKSPACE_ID",
                        message=f"duplicate workspace_id: {workspace_id}",
                    )
                else:
                    seen_workspace_ids.add(workspace_id)

            if resolved_root is not None:
                if resolved_root in seen_workspace_roots:
                    add_error(
                        errors,
                        code="DUPLICATE_WORKSPACE_ROOT",
                        message=f"duplicate workspace_root resolves to: {repo_display(resolved_root)}",
                    )
                else:
                    seen_workspace_roots.add(resolved_root)

            if len(errors) == workspace_errors_start and workspace_id is None:
                add_error(
                    errors,
                    code="INVALID_WORKSPACE_RECORD",
                    message=f"workspaces[{index}] could not be identified safely",
                )

    default_workspace_id = payload.get("default_workspace_id")
    if isinstance(default_workspace_id, str) and default_workspace_id and default_workspace_id not in workspace_ids:
        add_error(
            errors,
            code="DEFAULT_WORKSPACE_NOT_FOUND",
            message=f"default_workspace_id not found in workspaces: {default_workspace_id}",
        )

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_topic_workspace_registry(target)

    status = "pass" if exit_code == EXIT_PASS else "fail"
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts={
            "report_json": display_path(args.report_json),
            "report_text": display_path(args.report_text),
        },
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or (display_path(args.target) or args.target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
