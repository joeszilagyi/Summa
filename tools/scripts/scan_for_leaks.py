#!/usr/bin/env python3
"""Scan generated artifacts or bundles for secrets, private paths, and payload leaks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.leak_scanner import PROFILES, LeakScannerError, load_allowlist, scan_directory  # noqa: E402
from tools.validators.common import EXIT_INPUT_UNAVAILABLE, EXIT_PASS, EXIT_VALIDATION_FAILED, add_report_args, write_json, write_text  # noqa: E402


SCRIPT_PATH = "tools/scripts/scan_for_leaks.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Directory path to scan.")
    parser.add_argument("--profile", choices=tuple(sorted(PROFILES)), default="public_bundle")
    parser.add_argument("--allowlist-json", help="Optional leak-scan-allowlist.v1 JSON path.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    add_report_args(parser)
    return parser.parse_args()


def render_text(report: dict[str, object]) -> str:
    counts = report["counts"]
    assert isinstance(counts, dict)
    lines = [
        f"schema_version={report['schema_version']}",
        f"profile={report['profile']}",
        f"status={report['status']}",
        "files_scanned={files_scanned} findings={findings} suppressed_findings={suppressed_findings} allowlist_entries={allowlist_entries}".format(
            **counts
        ),
    ]
    for index, finding in enumerate(report["findings"]):
        assert isinstance(finding, dict)
        line = finding.get("line")
        excerpt = finding.get("excerpt")
        lines.append(
            f"finding[{index}]={finding['code']} path={finding['path']}"
            + (f" line={line}" if line is not None else "")
            + (f" excerpt={excerpt}" if excerpt is not None else "")
        )
    for index, finding in enumerate(report["suppressed_findings"]):
        assert isinstance(finding, dict)
        lines.append(
            f"suppressed[{index}]={finding['code']} path={finding['path']} allowlist_entry_id={finding['allowlist_entry_id']}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    try:
        allowlist_payload = load_allowlist(Path(args.allowlist_json)) if args.allowlist_json else None
        report = scan_directory(target, profile=args.profile, allowlist_payload=allowlist_payload)
    except LeakScannerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT_UNAVAILABLE

    text_report = render_text(report)
    write_json(args.report_json, report)
    write_text(args.report_text, text_report)

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(text_report)
    return EXIT_PASS if report["status"] == "pass" else EXIT_VALIDATION_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
