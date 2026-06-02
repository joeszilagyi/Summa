#!/usr/bin/env python3
"""Inventory legacy subject substrate under workspace-roots.

This validator is intentionally read-only. By default it reports legacy root
files and prototype prompt outputs as private/export-deferred warnings. In
``--fail-on-legacy`` mode the same inventory becomes a release-blocking failure.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
    )


VALIDATOR_NAME = "legacy_subject_substrate"
CONTRACT_VERSION = "1"
SKIP_DIR_NAMES = {"runs", "state", "__pycache__", ".git"}
SKIP_FILE_NAMES = {".gitkeep"}

LEGACY_CLASSIFICATIONS = {
    "legacy_root_subject_substrate": {
        "code": "LEGACY_ROOT_SUBSTRATE_PRIVATE_EXPORT",
        "label": "legacy root subject file",
    },
    "legacy_prompt_output": {
        "code": "LEGACY_PROMPT_OUTPUT_PRIVATE_EXPORT",
        "label": "legacy root prompt output",
    },
    "legacy_new_findings": {
        "code": "LEGACY_NEW_FINDINGS_PRIVATE_EXPORT",
        "label": "legacy new_findings artifact",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Classify retained legacy subject substrate under workspace-roots as "
            "private/export-deferred unless explicitly migrated."
        )
    )
    parser.add_argument("target", help="Path to an workspace-roots tree or one legacy subject directory.")
    parser.add_argument(
        "--fail-on-legacy",
        action="store_true",
        help="Return validation failure when any legacy/private-export substrate is present.",
    )
    add_report_args(parser)
    return parser.parse_args()


def add_problem(
    problems: list[dict[str, Any]],
    *,
    code: str,
    message: str,
) -> None:
    problems.append({"code": code, "line": None, "message": message})


def should_skip(path: Path) -> bool:
    if path.name in SKIP_FILE_NAMES:
        return True
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def classify_file(path: Path) -> str | None:
    name = path.name
    if "_new_findings_" in name:
        return "legacy_new_findings"
    if "_facet_" in name:
        return "legacy_prompt_output"
    if path.suffix == "":
        return "legacy_root_subject_substrate"
    return None


def sample_paths(paths: list[Path], *, target: Path, limit: int = 3) -> str:
    samples: list[str] = []
    for path in paths[:limit]:
        try:
            samples.append(path.relative_to(target).as_posix())
        except ValueError:
            samples.append(path.as_posix())
    if len(paths) > limit:
        samples.append(f"... +{len(paths) - limit} more")
    return ", ".join(samples)


def validate_legacy_subject_substrate(
    target: Path,
    *,
    fail_on_legacy: bool = False,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not target.exists():
        add_problem(errors, code="INPUT_NOT_FOUND", message="input path does not exist")
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE
    if not target.is_dir():
        add_problem(errors, code="INPUT_NOT_DIRECTORY", message="input path must be a directory")
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE

    classified: dict[str, list[Path]] = defaultdict(list)
    for path in sorted((item for item in target.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        if should_skip(path.relative_to(target)):
            continue
        counts["inspected"] += 1
        classification = classify_file(path)
        if classification is None:
            counts["accepted"] += 1
            continue
        classified[classification].append(path)
        counts["deferred"] += 1

    findings: list[dict[str, Any]] = errors if fail_on_legacy else warnings
    for classification, meta in LEGACY_CLASSIFICATIONS.items():
        paths = classified.get(classification, [])
        if not paths:
            continue
        message = (
            f"{len(paths)} {meta['label']}"
            f"{'' if len(paths) == 1 else 's'} classified as legacy_private_export; "
            f"sample: {sample_paths(paths, target=target)}"
        )
        add_problem(findings, code=meta["code"], message=message)

    if fail_on_legacy and counts["deferred"]:
        counts["rejected"] = counts["deferred"]
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_legacy_subject_substrate(
        target,
        fail_on_legacy=args.fail_on_legacy,
    )
    status = "pass" if exit_code == EXIT_PASS else "fail"
    output_artifacts = {
        "report_json": display_path(args.report_json),
        "report_text": display_path(args.report_text),
    }
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts=output_artifacts,
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or display_path(args.target) or str(target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
