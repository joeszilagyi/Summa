#!/usr/bin/env python3
"""Validate source.sqlite records against a schema boundary profile.

This script reads records from a source database, validates them against one of
the supported schema profiles, and emits a JSON report.
Use --output to persist the report; otherwise it is written to stdout.

Documentation: docs/tools/source_db_tools/schema_profile_validation.md
When modifying CLI behavior, profiles, or report shape, update that guide.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    # Allow direct script execution while importing sibling SQLite tool modules.
    sys.path.insert(0, str(CURRENT_DIR))

import export_bibliography  # noqa: E402
import schema_profile_validation  # noqa: E402


def positive_int(value: str) -> int:
    return export_bibliography.positive_int(value)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate canonical bibliography records at a profile boundary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python tools/source_db_tools/validate_schema_profile.py dbs/sources/Example Subject/source.sqlite --profile canonical_minimal
  python tools/source_db_tools/validate_schema_profile.py dbs/sources/Example Subject/source.sqlite --profile canonical_full --limit 10 --output /tmp/schema-profile.json
""",
    )
    parser.add_argument("db", help="Path to source.sqlite")
    parser.add_argument("--profile", required=True, choices=schema_profile_validation.profile_names())
    parser.add_argument("--limit", type=positive_int, help="Validate only the first N loaded works.")
    parser.add_argument("--output", help="Write validation report JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if not Path(args.db).exists():
            print(f"ERROR: database not found: {args.db}", file=sys.stderr)
            return 1
        conn = export_bibliography.connect(Path(args.db))
        try:
            records = export_bibliography.load_records(conn, limit=args.limit)
        finally:
            conn.close()
        report = schema_profile_validation.validate_records(records, args.profile)
        if not isinstance(report, dict) or not isinstance(report.get("ok"), bool):
            print("ERROR: validation returned malformed report", file=sys.stderr)
            return 1
        body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=path.parent, delete=False
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    tmp.write(body)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                tmp_path.replace(path)
            finally:
                if tmp_path is not None and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
        else:
            sys.stdout.write(body)
        return 0 if report["ok"] else 1
    except (RuntimeError, ValueError, sqlite3.Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
