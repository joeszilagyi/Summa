#!/usr/bin/env python3
"""Validate canonical-write spool records without replaying them."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools import canonical_write_spool  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool-path", required=True, help="Spool record file or spool directory.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def validate_path(path: Path) -> tuple[dict[str, object], int]:
    records = []
    errors = []
    try:
        for record_path, record in canonical_write_spool.iter_spool_records(path):
            records.append(
                {
                    "path": str(record_path),
                    "spool_record_id": record["spool_record_id"],
                    "operation_kind": record["operation_kind"],
                    "replay_status": record["replay_status"],
                    "valid": True,
                }
            )
    except canonical_write_spool.CanonicalWriteSpoolError as exc:
        errors.append(str(exc))
    report: dict[str, object] = {
        "schema_version": "canonical-write-spool-validation-report.v1",
        "target": str(path),
        "valid": not errors,
        "record_count": len(records),
        "records": records,
        "errors": errors,
    }
    return report, 0 if not errors else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, exit_code = validate_path(Path(args.spool_path))
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"valid={str(report['valid']).lower()}")
        print(f"record_count={report['record_count']}")
        for error in report["errors"]:
            print(f"error={error}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
