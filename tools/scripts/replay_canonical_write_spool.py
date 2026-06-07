#!/usr/bin/env python3
"""Replay validated canonical-write spool records into a canonical SQLite store."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools import canonical_store, canonical_write_spool  # noqa: E402
from tools.common.atomic_write import atomic_write_json, stable_json_text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Target initialized canonical SQLite store.")
    parser.add_argument(
        "--spool-path",
        required=True,
        help="Spool record JSON file or directory containing canonical-write spool records.",
    )
    parser.add_argument("--output", help="Optional replay report JSON path.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and plan without mutating DB or spool."
    )
    parser.add_argument(
        "--strict", action="store_true", help="Stop after the first replay failure."
    )
    parser.add_argument("--limit", type=int, help="Maximum pending records to attempt.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--replay-run-id", help="Optional replay run id for deterministic reports.")
    parser.add_argument(
        "--started-at", help="Optional timestamp override for deterministic reports."
    )
    return parser.parse_args(argv)


def _result_refs(result: dict[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {"status": result.get("status")}
    if isinstance(result.get("provenance_event"), dict):
        refs["provenance_event"] = result["provenance_event"]
    for key in ("provenance_event_id", "merge_event_id", "cycle_event_id"):
        if result.get(key) is not None:
            refs[key] = result[key]
    if isinstance(result.get("counts"), dict):
        refs["counts"] = result["counts"]
    return refs


def replay(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    db_path = canonical_store.resolve_db_path(args.db)
    spool_path = Path(args.spool_path).expanduser()
    if not spool_path.is_absolute():
        spool_path = (Path.cwd() / spool_path).resolve()
    started_at = (
        canonical_store._normalize_timestamp(args.started_at, field_name="--started-at")
        if args.started_at
        else canonical_write_spool.now_rfc3339()
    )
    report: dict[str, Any] = {
        "schema_version": canonical_write_spool.REPLAY_REPORT_SCHEMA_VERSION,
        "replay_run_id": args.replay_run_id or f"spool-replay:{started_at}",
        "started_at": started_at,
        "ended_at": None,
        "canonical_db": {
            "path": str(db_path),
            "schema_version": None,
            "current_migration_id": None,
        },
        "spool_input_path": str(spool_path),
        "dry_run": bool(args.dry_run),
        "records_discovered": 0,
        "records_attempted": 0,
        "records_replayed": 0,
        "records_failed": 0,
        "records_skipped": 0,
        "operation_counts": {},
        "results": [],
        "warnings": [],
        "status": "pending",
    }
    try:
        check = canonical_store.check_canonical_store(db_path)
        report["canonical_db"] = {
            "path": str(check.db_path),
            "schema_version": check.schema_version,
            "current_migration_id": check.current_migration_id,
        }
        conn = canonical_store.connect_canonical_store(db_path)
    except (canonical_store.CanonicalStoreError, sqlite3.Error) as exc:
        report["status"] = "failed"
        report["warnings"].append(str(exc))
        report["ended_at"] = canonical_write_spool.now_rfc3339()
        return report, 1
    try:
        pending_attempts = 0
        for record_path, record in canonical_write_spool.iter_spool_records(spool_path):
            report["records_discovered"] += 1
            kind = str(record["operation_kind"])
            report["operation_counts"][kind] = int(report["operation_counts"].get(kind, 0)) + 1
            result_item: dict[str, Any] = {
                "spool_record_id": record["spool_record_id"],
                "path": str(record_path),
                "operation_kind": kind,
                "prior_replay_status": record["replay_status"],
                "status": None,
                "result_refs": None,
                "error": None,
            }
            if record["replay_status"] == "replayed":
                result_item["status"] = "skipped_already_replayed"
                report["records_skipped"] += 1
                report["results"].append(result_item)
                continue
            if record["replay_status"] not in {"pending", "failed"}:
                result_item["status"] = "skipped"
                report["records_skipped"] += 1
                report["results"].append(result_item)
                continue
            if args.limit is not None and pending_attempts >= args.limit:
                result_item["status"] = "skipped_limit"
                report["records_skipped"] += 1
                report["results"].append(result_item)
                continue
            pending_attempts += 1
            report["records_attempted"] += 1
            try:
                if args.dry_run:
                    result = canonical_write_spool.replay_spool_record(
                        conn,
                        record,
                        db_path=db_path,
                        dry_run=True,
                        record_path=record_path,
                    )
                else:
                    with conn:
                        result = canonical_write_spool.replay_spool_record(
                            conn,
                            record,
                            db_path=db_path,
                            dry_run=False,
                            record_path=record_path,
                        )
                result_item["status"] = "dry_run" if args.dry_run else "replayed"
                result_item["result_refs"] = _result_refs(result)
                if not args.dry_run:
                    canonical_write_spool.mark_spool_record_replayed(
                        record_path,
                        record,
                        replayed_at=canonical_write_spool.now_rfc3339(),
                        replay_result_refs=result_item["result_refs"],
                    )
                    report["records_replayed"] += 1
            except Exception as exc:
                result_item["status"] = "failed"
                result_item["error"] = str(exc)
                report["records_failed"] += 1
                if not args.dry_run:
                    canonical_write_spool.mark_spool_record_failed(
                        record_path,
                        record,
                        failure_message=str(exc),
                        replayed_at=canonical_write_spool.now_rfc3339(),
                    )
                report["results"].append(result_item)
                if args.strict:
                    break
                continue
            report["results"].append(result_item)
    except canonical_write_spool.CanonicalWriteSpoolError as exc:
        report["warnings"].append(str(exc))
        report["status"] = "failed"
        report["ended_at"] = canonical_write_spool.now_rfc3339()
        return report, 1
    finally:
        conn.close()
    report["ended_at"] = canonical_write_spool.now_rfc3339()
    if args.dry_run:
        report["status"] = "dry_run"
    elif report["records_failed"]:
        report["status"] = "failed"
    elif report["records_replayed"] or report["records_skipped"]:
        report["status"] = "completed"
    else:
        report["status"] = "no_records"
    return report, 0 if report["status"] in {"completed", "dry_run", "no_records"} else 1


def render_text(report: dict[str, Any]) -> str:
    return (
        "\n".join(
            [
                f"status={report['status']}",
                f"records_discovered={report['records_discovered']}",
                f"records_attempted={report['records_attempted']}",
                f"records_replayed={report['records_replayed']}",
                f"records_failed={report['records_failed']}",
                f"records_skipped={report['records_skipped']}",
            ]
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report, exit_code = replay(args)
    if args.output:
        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_path, report)
    if args.format == "json":
        sys.stdout.write(stable_json_text(report))
    else:
        print(render_text(report), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
