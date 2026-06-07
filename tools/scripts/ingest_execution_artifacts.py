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

from tools.source_db_tools import (  # noqa: E402
    canonical_ingest,
    canonical_store,
    canonical_write_spool,
)


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
        "--degraded-spool",
        action="store_true",
        help="On canonical DB write failure, preserve the validated intended write as a spool record.",
    )
    parser.add_argument(
        "--spool-dir",
        help="Directory for degraded canonical-write spool records. Required with --degraded-spool.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Stdout report format.",
    )
    return parser.parse_args()


def _spool_execution_artifacts(
    *,
    args: argparse.Namespace,
    db_path: Path,
    run_dir: Path,
    paths: dict[str, Path],
    input_hashes: dict[str, str],
    failure: BaseException,
    expected_schema_version: int | None,
) -> dict[str, object]:
    if not args.degraded_spool:
        raise failure
    if not args.spool_dir:
        raise canonical_write_spool.CanonicalWriteSpoolError(
            "--spool-dir is required with --degraded-spool"
        ) from failure
    artifact_refs = [
        {
            "artifact_type": key,
            "artifact_path": str(paths[key]),
            "artifact_hash": input_hashes[key],
        }
        for key in sorted(input_hashes)
    ]
    record = canonical_write_spool.build_spool_record(
        operation_kind="execution_artifact_ingest",
        operation_input={"artifact_refs": artifact_refs},
        replay_recipe={
            "artifact_root": str(run_dir),
            "run_dir": ".",
            "input_hashes": dict(input_hashes),
            "strict": not args.no_strict,
        },
        failure=failure,
        canonical_db_path=db_path,
        spool_dir=Path(args.spool_dir),
        originating_tool="tools/scripts/ingest_execution_artifacts.py",
        originating_command="ingest_execution_artifacts.py",
        originating_run_id=None,
        stage_name="ingest_execution_artifacts",
        expected_schema_version=expected_schema_version,
    )
    spool_path = canonical_write_spool.write_spool_record(Path(args.spool_dir), record)
    return {
        "schema_version": "canonical-ingest-report.v1",
        "ingest_kind": "execution_artifacts",
        "status": "spooled",
        "spool_record_path": str(spool_path),
        "spool_record_id": record["spool_record_id"],
        "failure_kind": record["failure_kind"],
        "failure_message": record["failure_message"],
        "input_paths": {key: str(value) for key, value in paths.items()},
        "input_hashes": dict(input_hashes),
        "transaction_status": "spooled",
        "warnings": [
            {
                "message": (
                    "canonical write failed; validated intended write preserved "
                    "as a pending spool record"
                )
            }
        ],
    }


def main() -> int:
    args = parse_args()
    report: dict[str, object]
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
    except canonical_ingest.CanonicalIngestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    expected_schema_version: int | None = None
    try:
        check = canonical_store.check_canonical_store(db_path)
        expected_schema_version = check.schema_version
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
    except (
        canonical_ingest.CanonicalIngestError,
        canonical_store.CanonicalStoreError,
        canonical_write_spool.CanonicalWriteSpoolError,
    ) as exc:
        if not args.dry_run and args.degraded_spool:
            try:
                report = _spool_execution_artifacts(
                    args=args,
                    db_path=db_path,
                    run_dir=run_dir,
                    paths=paths,
                    input_hashes=input_hashes,
                    failure=exc,
                    expected_schema_version=expected_schema_version,
                )
            except Exception as spool_exc:
                print(f"Error: {exc}; spool failed: {spool_exc}", file=sys.stderr)
                return 1
        else:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    except Exception as exc:
        if not args.dry_run and args.degraded_spool:
            try:
                report = _spool_execution_artifacts(
                    args=args,
                    db_path=db_path,
                    run_dir=run_dir,
                    paths=paths,
                    input_hashes=input_hashes,
                    failure=exc,
                    expected_schema_version=expected_schema_version,
                )
            except Exception as spool_exc:
                print(f"Error: {exc}; spool failed: {spool_exc}", file=sys.stderr)
                return 1
        else:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        if report.get("status") == "spooled":
            sys.stdout.write(f"status=spooled\nspool_record_path={report['spool_record_path']}\n")
        else:
            sys.stdout.write(canonical_ingest.render_report_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
