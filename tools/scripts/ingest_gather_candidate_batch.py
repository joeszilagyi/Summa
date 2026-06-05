#!/usr/bin/env python3
"""Ingest one validated gather candidate batch into the canonical SQLite store."""

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
            "Validate one gather-candidate-batch.v1 artifact and write proposed rows into "
            "an initialized canonical SQLite store."
        )
    )
    parser.add_argument(
        "--db", required=True, help="Path to an initialized canonical SQLite store."
    )
    parser.add_argument("--batch", required=True, help="Path to gather-candidate-batch.json.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report intended writes without mutating the canonical store.",
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Allow unmapped candidate rows to be skipped with warnings instead of failing.",
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


def _spool_candidate_batch(
    *,
    args: argparse.Namespace,
    db_path: Path,
    batch_path: Path,
    batch_hash: str,
    failure: BaseException,
    expected_schema_version: int | None,
) -> dict[str, object]:
    if not args.degraded_spool:
        raise failure
    if not args.spool_dir:
        raise canonical_write_spool.CanonicalWriteSpoolError(
            "--spool-dir is required with --degraded-spool"
        ) from failure
    record = canonical_write_spool.build_spool_record(
        operation_kind="candidate_batch_ingest",
        operation_input={
            "artifact_refs": [
                {
                    "artifact_type": "gather_candidate_batch",
                    "artifact_path": str(batch_path),
                    "artifact_hash": batch_hash,
                }
            ]
        },
        replay_recipe={
            "batch_path": str(batch_path),
            "batch_hash": batch_hash,
            "strict": not args.no_strict,
        },
        failure=failure,
        canonical_db_path=db_path,
        spool_dir=Path(args.spool_dir),
        originating_tool="tools/scripts/ingest_gather_candidate_batch.py",
        originating_command="ingest_gather_candidate_batch.py",
        originating_run_id=None,
        stage_name="ingest_candidate_batch",
        expected_schema_version=expected_schema_version,
    )
    spool_path = canonical_write_spool.write_spool_record(Path(args.spool_dir), record)
    return {
        "schema_version": "canonical-ingest-report.v1",
        "ingest_kind": "candidate_batch",
        "status": "spooled",
        "spool_record_path": str(spool_path),
        "spool_record_id": record["spool_record_id"],
        "failure_kind": record["failure_kind"],
        "failure_message": record["failure_message"],
        "input_paths": {"candidate_batch": str(batch_path)},
        "input_hashes": {"candidate_batch": batch_hash},
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
        batch_path = canonical_store.resolve_db_path(args.batch)
        batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)
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
                report = canonical_ingest.ingest_candidate_batch(
                    conn,
                    batch,
                    batch_path=batch_path,
                    batch_hash=batch_hash,
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
                    report = canonical_ingest.ingest_candidate_batch(
                        conn,
                        batch,
                        batch_path=batch_path,
                        batch_hash=batch_hash,
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
                report = _spool_candidate_batch(
                    args=args,
                    db_path=db_path,
                    batch_path=batch_path,
                    batch_hash=batch_hash,
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
                report = _spool_candidate_batch(
                    args=args,
                    db_path=db_path,
                    batch_path=batch_path,
                    batch_hash=batch_hash,
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
            sys.stdout.write(
                f"status=spooled\nspool_record_path={report['spool_record_path']}\n"
            )
        else:
            sys.stdout.write(canonical_ingest.render_report_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
