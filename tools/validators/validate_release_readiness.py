#!/usr/bin/env python3
"""Aggregate release-readiness posture from current machine-readable reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        resolve_report_root,
        write_json,
        write_text,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        resolve_report_root,
        write_json,
        write_text,
    )


REPORT_SCHEMA_VERSION = "release-readiness-report.v1"
VALIDATOR_NAME = "release_readiness"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/release_readiness_report.schema.json"
FIXTURE_PATH = "tests/fixtures/validators/release_readiness/pass/inputs"

DOCTOR_REPORT_NAME = "doctor-report.json"
EXPORT_REPORT_NAME = "knowledge-tree-export-validator-report.json"
STATIC_OUTPUT_REPORT_NAME = "static-output-validator-report.json"
SEARCH_PROJECTION_REPORT_NAME = "local-search-projection-validator-report.json"
LEAK_SCAN_REPORT_NAME = "leak-scan-report.json"


class ReleaseReadinessError(RuntimeError):
    """Raised when release-readiness inputs are missing or malformed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate one release-readiness bundle into pass, warn, or block posture.",
        epilog=(
            "Reads one directory containing current upstream JSON reports and writes an aggregated report.\n"
            "Expected filenames:\n"
            f"  - {DOCTOR_REPORT_NAME}\n"
            f"  - {EXPORT_REPORT_NAME}\n"
            f"  - {STATIC_OUTPUT_REPORT_NAME}\n"
            f"  - {SEARCH_PROJECTION_REPORT_NAME}\n"
            f"  - {LEAK_SCAN_REPORT_NAME}\n\n"
            f"Schema: {SCHEMA_PATH}\n"
            f"Example:\n  python3 tools/validators/validate_release_readiness.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Directory containing upstream report JSON files.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    add_report_args(parser)
    return parser.parse_args()


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ReleaseReadinessError(f"{label} is missing: {path.name}")
    if not path.is_file():
        raise ReleaseReadinessError(f"{label} is not a file: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseReadinessError(f"{label} could not be read as JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ReleaseReadinessError(f"{label} must be a JSON object: {path.name}")
    return payload


def make_check(*, check_key: str, source: str, status: str, message: str) -> dict[str, str]:
    return {
        "check_key": check_key,
        "source": source,
        "status": status,
        "message": message,
    }


def make_finding(*, severity: str, source: str, code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    finding = {
        "severity": severity,
        "source": source,
        "code": code,
        "message": message,
    }
    if details:
        finding["details"] = details
    return finding


def _append_doctor_findings(
    report: dict[str, Any],
    *,
    source_name: str,
    findings: list[dict[str, Any]],
) -> tuple[str, str]:
    if report.get("schema_version") != "local-doctor-report.v1":
        raise ReleaseReadinessError(f"doctor report must use schema_version local-doctor-report.v1: {source_name}")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise ReleaseReadinessError(f"doctor report summary must be an object: {source_name}")
    doctor_findings = report.get("findings")
    if not isinstance(doctor_findings, list):
        raise ReleaseReadinessError(f"doctor report findings must be an array: {source_name}")

    has_block = False
    has_warn = False
    for item in doctor_findings:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        message = item.get("message")
        finding_class = item.get("class")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        if finding_class == "operator_action_required":
            has_block = True
            findings.append(make_finding(severity="block", source=source_name, code=code, message=message, details=item.get("details")))
        elif finding_class in {"advisory_only", "auto_remediable_candidate"}:
            has_warn = True
            findings.append(make_finding(severity="warn", source=source_name, code=code, message=message, details=item.get("details")))

    status = "block" if has_block else ("warn" if has_warn or summary.get("status") == "warn" else "pass")
    message = (
        "doctor report contains operator-action-required findings"
        if status == "block"
        else ("doctor report contains advisory findings" if status == "warn" else "doctor report is ready")
    )
    return status, message


def _append_validator_report_findings(
    report: dict[str, Any],
    *,
    source_name: str,
    findings: list[dict[str, Any]],
) -> tuple[str, str]:
    validator = report.get("validator")
    status = report.get("status")
    errors = report.get("errors")
    warnings = report.get("warnings")
    if not isinstance(validator, str) or not validator.strip():
        raise ReleaseReadinessError(f"validator report is missing validator name: {source_name}")
    if status not in {"pass", "fail"}:
        raise ReleaseReadinessError(f"validator report status must be pass or fail: {source_name}")
    if not isinstance(errors, list) or not isinstance(warnings, list):
        raise ReleaseReadinessError(f"validator report errors and warnings must be arrays: {source_name}")

    if status == "fail":
        for item in errors[:20]:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "VALIDATION_FAILED")
            message = item.get("message", f"{validator} reported a failure")
            if isinstance(code, str) and isinstance(message, str):
                findings.append(make_finding(severity="block", source=source_name, code=code, message=message))
        return "block", f"{validator} reported blocking validation errors"

    if warnings:
        for item in warnings[:20]:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "VALIDATION_WARNING")
            message = item.get("message", f"{validator} reported a warning")
            if isinstance(code, str) and isinstance(message, str):
                findings.append(make_finding(severity="warn", source=source_name, code=code, message=message))
        return "warn", f"{validator} reported warnings"

    return "pass", f"{validator} passed"


def _append_leak_scan_findings(
    report: dict[str, Any],
    *,
    source_name: str,
    findings: list[dict[str, Any]],
) -> tuple[str, str]:
    if report.get("schema_version") != "leak-scan-report.v1":
        raise ReleaseReadinessError(f"leak scan report must use schema_version leak-scan-report.v1: {source_name}")
    status = report.get("status")
    if status not in {"pass", "fail"}:
        raise ReleaseReadinessError(f"leak scan status must be pass or fail: {source_name}")
    active_findings = report.get("findings")
    suppressed_findings = report.get("suppressed_findings")
    if not isinstance(active_findings, list) or not isinstance(suppressed_findings, list):
        raise ReleaseReadinessError(f"leak scan findings must be arrays: {source_name}")

    if status == "fail":
        for item in active_findings[:20]:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "LEAK_SCAN_FAILED")
            message = item.get("message", "public leak scan reported a finding")
            if isinstance(code, str) and isinstance(message, str):
                findings.append(make_finding(severity="block", source=source_name, code=code, message=message))
        return "block", "public leak scan reported blocking findings"

    if suppressed_findings:
        for item in suppressed_findings[:20]:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "ALLOWLISTED_FINDING")
            message = item.get("message", "allowlisted leak-scan finding remains in the bundle")
            if isinstance(code, str) and isinstance(message, str):
                findings.append(make_finding(severity="warn", source=source_name, code=code, message=message))
        return "warn", "public leak scan passed with allowlisted findings"

    return "pass", "public leak scan passed"


def summarize_checks(checks: list[dict[str, str]], findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "block": 0}
    for item in checks:
        counts[item["status"]] += 1
    return {
        "check_count": len(checks),
        "pass_count": counts["pass"],
        "warn_count": counts["warn"],
        "block_count": counts["block"],
        "finding_count": len(findings),
    }


def aggregate_release_readiness(bundle_root: Path) -> dict[str, Any]:
    if not bundle_root.exists() or not bundle_root.is_dir():
        raise ReleaseReadinessError(f"target bundle is not a directory: {bundle_root}")

    doctor_path = bundle_root / DOCTOR_REPORT_NAME
    export_path = bundle_root / EXPORT_REPORT_NAME
    static_output_path = bundle_root / STATIC_OUTPUT_REPORT_NAME
    search_projection_path = bundle_root / SEARCH_PROJECTION_REPORT_NAME
    leak_scan_path = bundle_root / LEAK_SCAN_REPORT_NAME

    doctor_report = load_json_object(doctor_path, label="doctor report")
    export_report = load_json_object(export_path, label="knowledge-tree export validator report")
    static_output_report = load_json_object(static_output_path, label="static output validator report")
    search_projection_report = load_json_object(search_projection_path, label="local-search projection validator report")
    leak_scan_report = load_json_object(leak_scan_path, label="leak scan report")

    findings: list[dict[str, Any]] = []
    checks = []

    doctor_status, doctor_message = _append_doctor_findings(doctor_report, source_name=DOCTOR_REPORT_NAME, findings=findings)
    checks.append(make_check(check_key="doctor", source=DOCTOR_REPORT_NAME, status=doctor_status, message=doctor_message))

    export_status, export_message = _append_validator_report_findings(export_report, source_name=EXPORT_REPORT_NAME, findings=findings)
    checks.append(make_check(check_key="knowledge_tree_export", source=EXPORT_REPORT_NAME, status=export_status, message=export_message))

    static_status, static_message = _append_validator_report_findings(static_output_report, source_name=STATIC_OUTPUT_REPORT_NAME, findings=findings)
    checks.append(make_check(check_key="static_output", source=STATIC_OUTPUT_REPORT_NAME, status=static_status, message=static_message))

    search_status, search_message = _append_validator_report_findings(search_projection_report, source_name=SEARCH_PROJECTION_REPORT_NAME, findings=findings)
    checks.append(make_check(check_key="local_search_projection", source=SEARCH_PROJECTION_REPORT_NAME, status=search_status, message=search_message))

    leak_status, leak_message = _append_leak_scan_findings(leak_scan_report, source_name=LEAK_SCAN_REPORT_NAME, findings=findings)
    checks.append(make_check(check_key="public_leak_scan", source=LEAK_SCAN_REPORT_NAME, status=leak_status, message=leak_message))

    overall_status = "block" if any(item["status"] == "block" for item in checks) else ("warn" if any(item["status"] == "warn" for item in checks) else "pass")
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": overall_status,
        "target_bundle": display_path(str(bundle_root)) or str(bundle_root),
        "summary": summarize_checks(checks, findings),
        "inputs": {
            "doctor_report": doctor_path.name,
            "knowledge_tree_export_report": export_path.name,
            "static_output_report": static_output_path.name,
            "local_search_projection_report": search_projection_path.name,
            "leak_scan_report": leak_scan_path.name,
        },
        "checks": checks,
        "findings": findings,
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
    }


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    lines = [
        f"schema_version={report['schema_version']}",
        f"status={report['status']}",
        "checks={check_count} pass={pass_count} warn={warn_count} block={block_count} findings={finding_count}".format(**summary),
    ]
    for index, check in enumerate(report["checks"]):
        lines.append(f"check[{index}]={check['check_key']} source={check['source']} status={check['status']} message={check['message']}")
    for index, finding in enumerate(report["findings"][:20]):
        lines.append(f"finding[{index}]={finding['severity']} source={finding['source']} code={finding['code']} message={finding['message']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    bundle_root = Path(args.target)
    report_root = resolve_report_root(bundle_root, report_root=args.report_root)
    try:
        report = aggregate_release_readiness(bundle_root)
    except ReleaseReadinessError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT_UNAVAILABLE
    report["scenario"] = args.scenario
    report["target"] = args.target_id or display_path(str(bundle_root)) or str(bundle_root)
    report["output_artifacts"] = {
        "report_json": display_path(args.report_json),
        "report_text": display_path(args.report_text),
    }

    text_report = render_text(report)
    write_json(args.report_json, report, root=report_root)
    write_text(args.report_text, text_report, root=report_root)

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(text_report)
    return EXIT_VALIDATION_FAILED if report["status"] == "block" else EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
