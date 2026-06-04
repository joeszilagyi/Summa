#!/usr/bin/env python3
"""Validate the repository capability index against checked-in surfaces."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = REPO_ROOT / "config" / "repository_capabilities.v1.json"
PROFILE_ROOT = REPO_ROOT / "config" / "standards_profiles"

VALID_STATUSES = {
    "live",
    "experimental",
    "validator_only",
    "internal",
    "legacy",
    "retired",
    "excluded",
}


class CapabilityValidationError(RuntimeError):
    """Raised when the capability index cannot be loaded."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate config/repository_capabilities.v1.json against package scripts, "
            "shell wrappers, standards profiles, docs, and tests."
        )
    )
    parser.add_argument(
        "--index",
        default=str(DEFAULT_INDEX),
        help="Capability index JSON to validate.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CapabilityValidationError(f"missing capability index: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CapabilityValidationError(f"invalid JSON in capability index: {path}") from exc
    if not isinstance(payload, dict):
        raise CapabilityValidationError(f"capability index must be a JSON object: {path}")
    return payload


def load_package_scripts() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def relative_existing_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    return REPO_ROOT / value


def capability_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities = index.get("capabilities")
    if not isinstance(capabilities, list):
        raise CapabilityValidationError("capability index must contain capabilities array")
    entries: list[dict[str, Any]] = []
    for item in capabilities:
        if not isinstance(item, dict):
            raise CapabilityValidationError("every capability entry must be an object")
        entries.append(item)
    return entries


def add_error(
    errors: list[dict[str, str]], *, code: str, message: str, capability_id: str | None = None
) -> None:
    error = {"code": code, "message": message}
    if capability_id is not None:
        error["capability_id"] = capability_id
    errors.append(error)


def add_warning(
    warnings: list[dict[str, str]], *, code: str, message: str, capability_id: str | None = None
) -> None:
    warning = {"code": code, "message": message}
    if capability_id is not None:
        warning["capability_id"] = capability_id
    warnings.append(warning)


def validate_index(index: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    package_scripts = load_package_scripts()
    entries = capability_entries(index)

    ids: set[str] = set()
    indexed_console_commands: set[str] = set()
    indexed_wrapper_paths: set[str] = set()
    indexed_profile_paths: set[str] = set()
    legacy_or_retired_commands: set[str] = set()

    for entry in entries:
        capability_id = str(entry.get("id", ""))
        if not capability_id:
            add_error(errors, code="missing_id", message="capability entry is missing id")
            continue
        if capability_id in ids:
            add_error(
                errors,
                code="duplicate_id",
                message=f"duplicate capability id: {capability_id}",
                capability_id=capability_id,
            )
        ids.add(capability_id)

        status = entry.get("status")
        if status not in VALID_STATUSES:
            add_error(
                errors,
                code="invalid_status",
                message=f"invalid status: {status!r}",
                capability_id=capability_id,
            )

        for path_key in ("path", "wrapper_path", "docs_path"):
            path = relative_existing_path(entry.get(path_key))
            if path is not None and not path.exists():
                add_error(
                    errors,
                    code="missing_path",
                    message=f"{path_key} does not exist: {entry[path_key]}",
                    capability_id=capability_id,
                )

        for test_ref in entry.get("test_refs", []):
            if not isinstance(test_ref, str):
                add_error(
                    errors,
                    code="invalid_test_ref",
                    message="test_refs entries must be strings",
                    capability_id=capability_id,
                )
                continue
            test_path = REPO_ROOT / test_ref.split("::", 1)[0]
            if not test_path.exists():
                add_error(
                    errors,
                    code="missing_test_ref",
                    message=f"test reference does not exist: {test_ref}",
                    capability_id=capability_id,
                )

        command = entry.get("package_command")
        if isinstance(command, str) and command:
            indexed_console_commands.add(command)
            if command not in package_scripts:
                add_error(
                    errors,
                    code="missing_package_command",
                    message=f"package command is not declared in pyproject.toml: {command}",
                    capability_id=capability_id,
                )

        wrapper_path = entry.get("wrapper_path")
        if isinstance(wrapper_path, str) and wrapper_path:
            indexed_wrapper_paths.add(wrapper_path)

        path_value = entry.get("path")
        if entry.get("kind") == "standards_profile" and isinstance(path_value, str):
            indexed_profile_paths.add(path_value)

        if status in {"legacy", "retired", "excluded"} and isinstance(command, str) and command:
            legacy_or_retired_commands.add(command)

        exclusion_reason = entry.get("exclusion_reason")
        if status == "excluded" and not (
            isinstance(exclusion_reason, str) and exclusion_reason.strip()
        ):
            add_error(
                errors,
                code="missing_exclusion_reason",
                message="excluded capabilities must include a non-empty exclusion_reason",
                capability_id=capability_id,
            )
        if (
            entry.get("kind") == "shell_wrapper"
            and not entry.get("package_command")
            and not (isinstance(exclusion_reason, str) and exclusion_reason.strip())
        ):
            add_error(
                errors,
                code="missing_wrapper_exclusion_reason",
                message="shell wrappers without package commands must include an exclusion_reason",
                capability_id=capability_id,
            )

    for command in sorted(package_scripts):
        if command not in indexed_console_commands:
            add_error(
                errors,
                code="unindexed_console_script",
                message=f"pyproject console script is not indexed: {command}",
            )

    for wrapper in sorted((REPO_ROOT / "tools" / "scripts").glob("Index_*.sh")):
        relative_wrapper = wrapper.relative_to(REPO_ROOT).as_posix()
        if relative_wrapper not in indexed_wrapper_paths:
            add_error(
                errors,
                code="unindexed_shell_wrapper",
                message=f"Index wrapper is not indexed: {relative_wrapper}",
            )

    if PROFILE_ROOT.is_dir():
        for profile in sorted(PROFILE_ROOT.glob("*.json")):
            if (
                profile.name.endswith(".schema.json")
                or profile.name == "standards_profile.schema.json"
            ):
                continue
            relative_profile = profile.relative_to(REPO_ROOT).as_posix()
            if relative_profile not in indexed_profile_paths:
                add_error(
                    errors,
                    code="unindexed_standards_profile",
                    message=f"standards profile is not indexed: {relative_profile}",
                )

    for command in sorted(legacy_or_retired_commands):
        if command in package_scripts:
            add_error(
                errors,
                code="legacy_exposed_as_package_command",
                message=f"legacy, retired, or excluded surface is exposed as package command: {command}",
            )

    if not any(
        entry.get("path") == "tools/scripts/build_release_readiness_bundle.py" for entry in entries
    ):
        add_warning(
            warnings,
            code="release_readiness_builder_not_indexed",
            message="release-readiness builder is not indexed",
        )

    status = "pass" if not errors else "fail"
    return {
        "schema_version": "repository-capabilities-validation-report.v1",
        "status": status,
        "counts": {
            "capabilities": len(entries),
            "package_console_scripts": len(indexed_console_commands),
            "shell_wrappers": len(indexed_wrapper_paths),
            "standards_profiles": len(indexed_profile_paths),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "errors": errors,
        "warnings": warnings,
    }


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"status={report['status']}",
        "capabilities={capabilities} package_console_scripts={package_console_scripts} shell_wrappers={shell_wrappers} standards_profiles={standards_profiles}".format(
            **report["counts"]
        ),
        f"errors={report['counts']['errors']} warnings={report['counts']['warnings']}",
    ]
    for error in report["errors"]:
        suffix = f" capability={error['capability_id']}" if "capability_id" in error else ""
        lines.append(f"error={error['code']}{suffix} message={error['message']}")
    for warning in report["warnings"]:
        suffix = f" capability={warning['capability_id']}" if "capability_id" in warning else ""
        lines.append(f"warning={warning['code']}{suffix} message={warning['message']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        index = load_json_object(Path(args.index))
        report = validate_index(index)
    except CapabilityValidationError as exc:
        report = {
            "schema_version": "repository-capabilities-validation-report.v1",
            "status": "fail",
            "counts": {
                "capabilities": 0,
                "package_console_scripts": 0,
                "shell_wrappers": 0,
                "standards_profiles": 0,
                "errors": 1,
                "warnings": 0,
            },
            "errors": [{"code": "load_failed", "message": str(exc)}],
            "warnings": [],
        }

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text_report(report), end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
