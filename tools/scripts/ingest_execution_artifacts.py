#!/usr/bin/env python3
"""Ingest validated source acquisition execution artifacts into the canonical store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "source_db_tools",
    REPO_ROOT / "tools" / "validators",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.source_db_tools import canonical_ingest, canonical_store  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one source acquisition execution run directory and write capture/extraction "
            "rows into an initialized canonical SQLite store."
        )
    )
    parser.add_argument(
        "--db", required=True, help="Path to an initialized canonical SQLite store."
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Run directory containing execution-record.json, capture-events.jsonl, and extraction-records.jsonl.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report intended writes without mutating the canonical store.",
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Allow missing capture references to be skipped with warnings instead of failing.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Stdout report format.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        db_path = canonical_store.resolve_db_path(args.db)
        run_dir = canonical_store.resolve_db_path(args.run_dir)
        (
            execution_record,
            capture_events,
            extraction_records,
            paths,
            input_hashes,
        ) = canonical_ingest.load_validated_execution_artifacts(run_dir)
        canonical_store.check_canonical_store(db_path)
        if args.dry_run:
            conn = canonical_store.connect_canonical_store(db_path)
            try:
                report = canonical_ingest.ingest_execution_artifacts(
                    conn,
                    execution_record,
                    capture_events,
                    extraction_records,
                    paths=paths,
                    input_hashes=input_hashes,
                    dry_run=True,
                    strict=not args.no_strict,
                    db_path=db_path,
                )
            finally:
                conn.close()
        else:
            conn = canonical_store.connect_canonical_store(db_path)
            try:
                with conn:
                    report = canonical_ingest.ingest_execution_artifacts(
                        conn,
                        execution_record,
                        capture_events,
                        extraction_records,
                        paths=paths,
                        input_hashes=input_hashes,
                        dry_run=False,
                        strict=not args.no_strict,
                        db_path=db_path,
                    )
            finally:
                conn.close()
    except (canonical_ingest.CanonicalIngestError, canonical_store.CanonicalStoreError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(canonical_ingest.render_report_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
