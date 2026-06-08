"""Shared report helpers and exit codes for local validators."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

EXIT_PASS = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE_ERROR = 2
EXIT_DEPENDENCY_MISSING = 3
EXIT_INPUT_UNAVAILABLE = 4
EXIT_OPTIONAL_SERVICE_UNAVAILABLE = 5
EXIT_STATE_UNSAFE = 6

RFC3339_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def is_rfc3339_datetime(value: str) -> bool:
    """Return true only for RFC3339 date-times with an explicit timezone."""
    if not RFC3339_DATETIME_PATTERN.fullmatch(value):
        return False
    parseable_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(parseable_value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def add_report_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario",
        help="Optional fixture or scenario name for deterministic report output.",
    )
    parser.add_argument(
        "--target-id",
        help="Optional stable target identifier to emit in the report instead of the raw path.",
    )
    parser.add_argument(
        "--report-json",
        help="Write the machine-readable report JSON to this path.",
    )
    parser.add_argument(
        "--report-text",
        help="Write the human-readable report text to this path.",
    )
    parser.add_argument(
        "--report-root",
        help=(
            "Trusted root directory for report output paths. When omitted, the "
            "validator output root defaults to the validated target's directory."
        ),
    )


def display_path(path_value: str | None) -> str | None:
    """Normalize a path for user-facing output.

    Returns a path relative to the current working directory when possible so
    reports and logs are easier to read while preserving absolute paths that
    fall outside CWD.
    """
    if path_value is None:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def _write_atomically(path: Path, body: str) -> None:
    """Write text to disk atomically.

    A temporary file is written and fsynced before the final rename to avoid
    partial report files if the process is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(body)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def resolve_report_root(
    target: str | Path,
    *,
    report_root: str | Path | None = None,
) -> Path:
    if report_root is not None:
        root = Path(report_root).expanduser()
        return (Path.cwd() / root).resolve() if not root.is_absolute() else root.resolve()

    target_path = Path(target).expanduser()
    resolved_target = target_path.resolve()
    if target_path.exists() and target_path.is_dir():
        return resolved_target
    return resolved_target.parent


def resolve_report_path(path_value: str, *, root: Path) -> Path:
    candidate_path = Path(path_value).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = root / candidate_path
    candidate_path = candidate_path.resolve()
    resolved_root = root.resolve()
    try:
        candidate_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"report path escapes the allowed report root: {candidate_path}") from exc
    return candidate_path


def write_text(path_value: str | None, body: str, *, root: Path) -> None:
    if path_value is None:
        return
    _write_atomically(resolve_report_path(path_value, root=root), body)


def write_json(path_value: str | None, payload: dict[str, Any], *, root: Path) -> None:
    if path_value is None:
        return
    body = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    _write_atomically(resolve_report_path(path_value, root=root), body)


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"validator={report['validator']}",
        f"scenario={report['scenario'] or '-'}",
        f"target={report['target']}",
        f"status={report['status']}",
        (
            "inspected={inspected} accepted={accepted} rejected={rejected} deferred={deferred}".format(
                **report["counts"]
            )
        ),
        f"errors={len(report['errors'])} warnings={len(report['warnings'])}",
    ]
    for index, error in enumerate(report["errors"]):
        line_suffix = f" line={error.get('line')}" if error.get("line") is not None else ""
        lines.append(
            f"error[{index}]={error['code']}{line_suffix} message={error['message']}"
        )
    for index, warning in enumerate(report["warnings"]):
        line_suffix = f" line={warning['line']}" if warning.get("line") is not None else ""
        lines.append(
            f"warning[{index}]={warning['code']}{line_suffix} message={warning['message']}"
        )
    return "\n".join(lines) + "\n"


def emit_report(
    *,
    contract_version: str,
    counts: dict[str, int],
    errors: list[dict[str, Any]],
    output_artifacts: dict[str, str | None],
    report_json_path: str | None,
    report_text_path: str | None,
    scenario: str | None,
    status: str,
    target: str,
    validator: str,
    warnings: list[dict[str, Any]],
    report_root: str | Path | None = None,
) -> dict[str, Any]:
    report = {
        "contract_version": contract_version,
        "counts": counts,
        "errors": errors,
        "output_artifacts": output_artifacts,
        "scenario": scenario,
        "status": status,
        "target": target,
        "validator": validator,
        "warnings": warnings,
    }
    text_report = render_text_report(report)
    root = resolve_report_root(target, report_root=report_root)
    write_json(report_json_path, report, root=root)
    write_text(report_text_path, text_report, root=root)
    return report
