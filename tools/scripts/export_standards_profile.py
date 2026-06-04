#!/usr/bin/env python3
"""Export canonical Summa rows through an explicit standards profile."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common import standards_profiles  # noqa: E402
from tools.source_db_tools import canonical_store  # noqa: E402


SCRIPT_PATH = "tools/scripts/export_standards_profile.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic standards-profile export and conformance report "
            "from an initialized canonical SQLite store."
        )
    )
    parser.add_argument("--db", required=True, help="Path to initialized canonical SQLite store.")
    parser.add_argument(
        "--profile",
        required=True,
        choices=sorted(standards_profiles.SUPPORTED_PROFILE_IDS),
        help="Standards profile id to export.",
    )
    parser.add_argument("--output", required=True, help="Output path for the standards export JSON.")
    parser.add_argument(
        "--conformance-report",
        help="Optional path for standards-profile-conformance-report.v1 JSON. If omitted, only stdout reports it.",
    )
    parser.add_argument("--work-id", help="Optional work id or work:<id> scope.")
    parser.add_argument("--capture-id", help="Optional capture id or capture_event:<id> scope.")
    parser.add_argument("--subject-id", help="Optional workspace/subject scope where profile supports it.")
    parser.add_argument("--base-uri", help="Required for rico.v1 URI-like node identifiers.")
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Explicitly allow internal/private rows in the export. Default is public-safe output only.",
    )
    parser.add_argument(
        "--public-only",
        action="store_true",
        help="Explicitly select the default public-only mode.",
    )
    parser.add_argument("--generated-at", help="RFC3339 timestamp override for deterministic tests.")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero if conformance validation fails.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def render_text(result: standards_profiles.ExportResult, output_path: Path, report_path: Path | None) -> str:
    report = result.conformance_report
    lines = [
        f"status={report['conformance_status']}",
        f"validation_status={report['validation_status']}",
        f"profile_id={report['profile_id']}",
        f"output={output_path}",
        f"conformance_report={report_path}",
        f"records_exported={report['records_exported']}",
        f"writer_surface={SCRIPT_PATH}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.public_only and args.include_private:
            raise standards_profiles.StandardsProfileError("--public-only and --include-private cannot both be set")
        db_path = standards_profiles.resolve_path(args.db)
        output_path = standards_profiles.resolve_path(args.output)
        report_path = standards_profiles.resolve_path(args.conformance_report) if args.conformance_report else None
        result = standards_profiles.export_profile(
            db_path=db_path,
            profile_id=args.profile,
            output_path=output_path,
            conformance_report_path=report_path,
            work_id=args.work_id,
            capture_id=args.capture_id,
            subject_id=args.subject_id,
            base_uri=args.base_uri,
            include_private=bool(args.include_private),
            generated_at=args.generated_at,
            strict=bool(args.strict),
        )
    except (standards_profiles.StandardsProfileError, canonical_store.CanonicalStoreError, sqlite3.Error) as exc:
        payload = {
            "schema_version": "standards-profile-export-result.v1",
            "status": "failed",
            "error": str(exc),
            "writer_surface": SCRIPT_PATH,
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    payload = {
        "schema_version": "standards-profile-export-result.v1",
        "status": result.conformance_report["conformance_status"],
        "validation_status": result.conformance_report["validation_status"],
        "profile_id": result.conformance_report["profile_id"],
        "output": str(output_path),
        "conformance_report": None if report_path is None else str(report_path),
        "records_exported": result.conformance_report["records_exported"],
        "writer_surface": SCRIPT_PATH,
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(result, output_path, report_path), end="")
    return 0 if result.conformance_report["validation_status"] == "pass" or not args.strict else 2


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
