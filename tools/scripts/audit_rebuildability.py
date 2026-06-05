#!/usr/bin/env python3
"""Audit whether run artifacts can rebuild a canonical SQLite store.

Documentation: docs/scripts/index_audit_rebuildability.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools import (  # noqa: E402
    canonical_graph_closure,
    canonical_ingest,
    canonical_store,
    canonical_write_spool,
)

REPORT_SCHEMA_VERSION = "canonical-rebuildability-report.v1"
REPLAYABLE_TYPES = {
    "gather_candidate_batch",
    "source_acquisition_execution",
    "canonical_write_spool_record",
}
REFERENCE_ONLY_TYPES = {
    "candidate_ingest_report",
    "execution_ingest_report",
    "topic_cycle_manifest",
    "scheduled_cycle_manifest",
    "feedback_plan",
    "review_decision_apply_result",
    "graph_closure_report",
    "release_readiness_report",
    "publication_artifact",
}


class RebuildabilityError(RuntimeError):
    """Raised when the rebuildability audit cannot proceed safely."""


@dataclass(frozen=True)
class Artifact:
    artifact_type: str
    path: Path
    hash: str | None
    schema_id: str | None
    run_id: str | None
    stage: str | None
    validation_status: str
    replay_status: str
    failure_reason: str | None = None

    def as_report(self, runs_dir: Path) -> dict[str, Any]:
        try:
            rel_path = self.path.relative_to(runs_dir).as_posix()
        except ValueError:
            rel_path = str(self.path)
        return {
            "artifact_type": self.artifact_type,
            "path": rel_path,
            "hash": self.hash,
            "schema_id": self.schema_id,
            "originating_run_id": self.run_id,
            "stage": self.stage,
            "validation_status": self.validation_status,
            "replay_status": self.replay_status,
            "failure_reason": self.failure_reason,
        }


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def now_rfc3339() -> str:
    return canonical_store.now_rfc3339()


def _schema(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    value = payload.get("schema_version")
    return value if isinstance(value, str) else None


def _run_id(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    value = payload.get("run_id") or payload.get("replay_run_id")
    return value if isinstance(value, str) else None


def artifact_sort_key(artifact: Artifact) -> tuple[str, str, str, str]:
    run_id = artifact.run_id or ""
    stage = artifact.stage or ""
    return (run_id, stage, artifact.artifact_type, artifact.path.as_posix())


def validate_candidate_batch(path: Path) -> Artifact:
    try:
        batch, digest = canonical_ingest.load_validated_candidate_batch(path)
        return Artifact(
            artifact_type="gather_candidate_batch",
            path=path,
            hash=digest,
            schema_id=_schema(batch),
            run_id=_run_id(batch),
            stage="candidate_ingest",
            validation_status="valid",
            replay_status="pending",
        )
    except Exception as exc:
        return Artifact(
            artifact_type="gather_candidate_batch",
            path=path,
            hash=hash_file(path) if path.exists() else None,
            schema_id=_schema(read_json(path)),
            run_id=_run_id(read_json(path)),
            stage="candidate_ingest",
            validation_status="invalid",
            replay_status="blocked",
            failure_reason=str(exc),
        )


def validate_execution_dir(run_dir: Path) -> Artifact:
    payload = read_json(run_dir / "execution-record.json")
    try:
        _execution_record, _captures, _extractions, _paths, hashes = (
            canonical_ingest.load_validated_execution_artifacts(run_dir)
        )
        digest = hashlib.sha256(
            "\x1f".join(hashes[key] for key in sorted(hashes)).encode()
        ).hexdigest()
        return Artifact(
            artifact_type="source_acquisition_execution",
            path=run_dir,
            hash=digest,
            schema_id=_schema(payload),
            run_id=_run_id(payload),
            stage="execution_ingest",
            validation_status="valid",
            replay_status="pending",
        )
    except Exception as exc:
        return Artifact(
            artifact_type="source_acquisition_execution",
            path=run_dir,
            hash=None,
            schema_id=_schema(payload),
            run_id=_run_id(payload),
            stage="execution_ingest",
            validation_status="invalid",
            replay_status="blocked",
            failure_reason=str(exc),
        )


def validate_spool_record(path: Path) -> Artifact:
    try:
        record = canonical_write_spool.load_spool_record(path)
        return Artifact(
            artifact_type="canonical_write_spool_record",
            path=path,
            hash=hash_file(path),
            schema_id=str(record["schema_version"]),
            run_id=record.get("originating_run_id")
            if isinstance(record.get("originating_run_id"), str)
            else None,
            stage=record.get("stage_name") if isinstance(record.get("stage_name"), str) else None,
            validation_status="valid",
            replay_status="pending"
            if record.get("replay_status") in {"pending", "failed"}
            else f"skipped_{record.get('replay_status')}",
        )
    except Exception as exc:
        return Artifact(
            artifact_type="canonical_write_spool_record",
            path=path,
            hash=hash_file(path) if path.exists() else None,
            schema_id=_schema(read_json(path)),
            run_id=None,
            stage=None,
            validation_status="invalid",
            replay_status="blocked",
            failure_reason=str(exc),
        )


def reference_artifact(path: Path, artifact_type: str, *, stage: str | None = None) -> Artifact:
    payload = read_json(path)
    validation_status = "valid" if payload is not None else "invalid"
    reason = None if payload is not None else "artifact is not readable JSON"
    return Artifact(
        artifact_type=artifact_type,
        path=path,
        hash=hash_file(path) if path.exists() and path.is_file() else None,
        schema_id=_schema(payload),
        run_id=_run_id(payload),
        stage=stage,
        validation_status=validation_status,
        replay_status="reference_only",
        failure_reason=reason,
    )


def discover_artifacts(runs_dir: Path) -> list[Artifact]:
    if not runs_dir.exists() or not runs_dir.is_dir():
        raise RebuildabilityError(f"runs directory not found: {runs_dir}")
    artifacts: list[Artifact] = []
    seen_execution_dirs: set[Path] = set()
    for path in sorted(runs_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if name == "gather-candidate-batch.json":
            artifacts.append(validate_candidate_batch(path))
            continue
        if name == "execution-record.json":
            run_dir = path.parent
            if run_dir not in seen_execution_dirs:
                seen_execution_dirs.add(run_dir)
                artifacts.append(validate_execution_dir(run_dir))
            continue
        if name in {"candidate-ingest-report.json", "canonical-ingest-report.json"}:
            payload = read_json(path)
            if payload and payload.get("ingest_kind") == "candidate_batch":
                artifacts.append(
                    reference_artifact(path, "candidate_ingest_report", stage="candidate_ingest")
                )
            elif payload and payload.get("ingest_kind") == "execution_artifacts":
                artifacts.append(
                    reference_artifact(path, "execution_ingest_report", stage="execution_ingest")
                )
            else:
                artifacts.append(reference_artifact(path, "candidate_ingest_report"))
            continue
        if name in {"execution-ingest-report.json", "execution-artifact-ingest-report.json"}:
            artifacts.append(
                reference_artifact(path, "execution_ingest_report", stage="execution_ingest")
            )
            continue
        if name in {"topic-cycle-run.json", "topic-cycle-manifest.json"}:
            artifacts.append(reference_artifact(path, "topic_cycle_manifest", stage="topic_cycle"))
            continue
        if name == "scheduled-topic-cycles-run.json":
            artifacts.append(
                reference_artifact(path, "scheduled_cycle_manifest", stage="scheduled_cycle")
            )
            continue
        if name in {"candidate-feedback-plan.json", "feedback-plan.json"}:
            artifacts.append(reference_artifact(path, "feedback_plan", stage="feedback_plan"))
            continue
        if name in {"review-decision-apply-result.json", "review-decision-result.json"}:
            artifacts.append(
                reference_artifact(path, "review_decision_apply_result", stage="review_apply")
            )
            continue
        if name == "graph-closure-report.json":
            artifacts.append(
                reference_artifact(path, "graph_closure_report", stage="graph_closure")
            )
            continue
        if name == "release-readiness-report.json":
            artifacts.append(
                reference_artifact(path, "release_readiness_report", stage="release_readiness")
            )
            continue
        if name in {
            "knowledge_tree_export.json",
            "public_presentation.json",
            "publication-artifacts-report.json",
        }:
            artifacts.append(reference_artifact(path, "publication_artifact", stage="publication"))
            continue
        if path.suffix == ".json":
            payload = read_json(path)
            if payload and payload.get("schema_version") == canonical_write_spool.SCHEMA_VERSION:
                artifacts.append(validate_spool_record(path))
    return sorted(artifacts, key=artifact_sort_key)


def find_missing_artifacts(artifacts: list[Artifact], runs_dir: Path) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    existing_paths = {artifact.path.resolve() for artifact in artifacts}
    for artifact in artifacts:
        if artifact.artifact_type != "topic_cycle_manifest":
            continue
        payload = read_json(artifact.path)
        if not payload:
            continue
        for stage in payload.get("stages", []):
            if not isinstance(stage, Mapping):
                continue
            for mapping_key in ("artifacts", "inputs", "outputs"):
                refs = stage.get(mapping_key)
                if not isinstance(refs, Mapping):
                    continue
                for key, raw_value in sorted(refs.items()):
                    if not isinstance(raw_value, str):
                        continue
                    if not raw_value.endswith((".json", ".jsonl")):
                        continue
                    candidate = Path(raw_value)
                    if not candidate.is_absolute():
                        candidate = (artifact.path.parent / candidate).resolve()
                    else:
                        candidate = candidate.resolve()
                    if candidate.exists() or candidate in existing_paths:
                        continue
                    missing.append(
                        {
                            "referenced_by": artifact.path.relative_to(runs_dir).as_posix(),
                            "stage": stage.get("name"),
                            "artifact_key": key,
                            "missing_path": str(candidate),
                        }
                    )
    return sorted(missing, key=lambda item: (str(item["referenced_by"]), str(item["missing_path"])))


def row_count_summary(db_path: Path) -> dict[str, int]:
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
            for table in sorted(canonical_store.actual_tables(conn))
        }
    finally:
        conn.close()


def key_hash_summary(db_path: Path) -> dict[str, str]:
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        specs = {
            "work": "work_key_v1",
            "source_claim": "source_claim_key_v1",
            "provenance_event": "provenance_event_key_v1",
            "authority_record": "authority_key_v1",
            "authority_reconciliation": "reconciliation_key_v1",
        }
        result: dict[str, str] = {}
        for table, column in specs.items():
            if not canonical_store.table_exists(conn, table):
                continue
            columns = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in columns:
                continue
            rows = conn.execute(
                f"SELECT {column} AS value FROM {table} WHERE {column} IS NOT NULL ORDER BY {column}"
            ).fetchall()
            payload = "\n".join(str(row["value"]) for row in rows)
            result[table] = hashlib.sha256(payload.encode()).hexdigest()
        return result
    finally:
        conn.close()


def db_summary(db_path: Path) -> dict[str, Any]:
    check = canonical_store.check_canonical_store(db_path)
    return {
        "path": str(db_path),
        "schema_version": check.schema_version,
        "current_migration_id": check.current_migration_id,
        "row_counts": row_count_summary(db_path),
        "key_hashes": key_hash_summary(db_path),
    }


def replay_candidate_batch(
    conn: sqlite3.Connection, db_path: Path, artifact: Artifact
) -> dict[str, Any]:
    batch, digest = canonical_ingest.load_validated_candidate_batch(artifact.path)
    return canonical_ingest.ingest_candidate_batch(
        conn,
        batch,
        batch_path=artifact.path,
        batch_hash=digest,
        dry_run=False,
        strict=True,
        db_path=db_path,
    )


def replay_execution_artifacts(
    conn: sqlite3.Connection, db_path: Path, artifact: Artifact
) -> dict[str, Any]:
    execution_record, captures, extractions, paths, hashes = (
        canonical_ingest.load_validated_execution_artifacts(artifact.path)
    )
    return canonical_ingest.ingest_execution_artifacts(
        conn,
        execution_record,
        captures,
        extractions,
        paths=paths,
        input_hashes=hashes,
        dry_run=False,
        strict=True,
        db_path=db_path,
    )


def replay_spool_record(
    conn: sqlite3.Connection, db_path: Path, artifact: Artifact
) -> dict[str, Any]:
    record = canonical_write_spool.load_spool_record(artifact.path)
    return canonical_write_spool.replay_spool_record(
        conn,
        record,
        db_path=db_path,
        dry_run=False,
        strict=True,
    )


def replay_artifacts(
    *,
    db_path: Path,
    artifacts: list[Artifact],
    strict: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        for artifact in artifacts:
            if artifact.validation_status != "valid":
                continue
            if artifact.artifact_type not in REPLAYABLE_TYPES:
                continue
            if (
                artifact.artifact_type == "canonical_write_spool_record"
                and artifact.replay_status.startswith("skipped")
            ):
                results.append(
                    {
                        "artifact_type": artifact.artifact_type,
                        "path": str(artifact.path),
                        "status": artifact.replay_status,
                    }
                )
                continue
            try:
                with conn:
                    if artifact.artifact_type == "gather_candidate_batch":
                        result = replay_candidate_batch(conn, db_path, artifact)
                    elif artifact.artifact_type == "source_acquisition_execution":
                        result = replay_execution_artifacts(conn, db_path, artifact)
                    else:
                        result = replay_spool_record(conn, db_path, artifact)
                results.append(
                    {
                        "artifact_type": artifact.artifact_type,
                        "path": str(artifact.path),
                        "status": "replayed",
                        "result_status": result.get("status"),
                        "counts": result.get("counts"),
                    }
                )
            except Exception as exc:
                message = f"{artifact.artifact_type} replay failed for {artifact.path}: {exc}"
                errors.append(message)
                results.append(
                    {
                        "artifact_type": artifact.artifact_type,
                        "path": str(artifact.path),
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                if strict:
                    break
    finally:
        conn.close()
    return results, errors


def compare_summaries(existing: dict[str, Any], rebuilt: dict[str, Any]) -> dict[str, Any]:
    row_differences: dict[str, dict[str, int]] = {}
    existing_counts = existing["row_counts"]
    rebuilt_counts = rebuilt["row_counts"]
    for table in sorted(set(existing_counts) | set(rebuilt_counts)):
        left = int(existing_counts.get(table, 0))
        right = int(rebuilt_counts.get(table, 0))
        if left != right:
            row_differences[table] = {"existing": left, "rebuilt": right}
    key_hash_differences: dict[str, dict[str, str | None]] = {}
    existing_hashes = existing["key_hashes"]
    rebuilt_hashes = rebuilt["key_hashes"]
    for table in sorted(set(existing_hashes) | set(rebuilt_hashes)):
        left = existing_hashes.get(table)
        right = rebuilt_hashes.get(table)
        if left != right:
            key_hash_differences[table] = {"existing": left, "rebuilt": right}
    return {
        "status": "match" if not row_differences and not key_hash_differences else "different",
        "row_count_differences": row_differences,
        "key_hash_differences": key_hash_differences,
    }


def final_status(
    *,
    mode: str,
    invalid_count: int,
    missing_count: int,
    missing_support_count: int,
    replay_errors: list[str],
    graph_closure_status: str | None,
    comparison_status: str | None,
) -> str:
    if mode == "validate_only":
        return "validation_only"
    if invalid_count or missing_count or replay_errors:
        return "not_rebuildable"
    if missing_support_count:
        return "incomplete_support"
    if graph_closure_status == "fail":
        return "not_rebuildable"
    if comparison_status == "different":
        return "not_rebuildable"
    if graph_closure_status in {"pass_with_unresolved", "warning"}:
        return "rebuildable_with_warnings"
    return "rebuildable"


def ensure_rebuild_db_path(args: argparse.Namespace) -> tuple[Path, Path | None]:
    if args.temp_rebuild_db:
        target = Path(args.temp_rebuild_db).expanduser().resolve()
        if target.exists() and not args.force_temp_overwrite:
            raise RebuildabilityError(
                f"temp rebuild DB already exists: {target}; use --force-temp-overwrite"
            )
        if args.canonical_db and target == Path(args.canonical_db).expanduser().resolve():
            raise RebuildabilityError("temp rebuild DB must not be the comparison canonical DB")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        return target, None
    temp_dir = Path(tempfile.mkdtemp(prefix="summa-rebuildability-"))
    return temp_dir / "rebuilt-canonical.sqlite", temp_dir


def audit(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    generated_at = args.generated_at or now_rfc3339()
    artifacts = discover_artifacts(runs_dir)
    missing = find_missing_artifacts(artifacts, runs_dir)
    invalid = [artifact for artifact in artifacts if artifact.validation_status != "valid"]
    reference_only = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type in REFERENCE_ONLY_TYPES and artifact.validation_status == "valid"
    ]
    replayable = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type in REPLAYABLE_TYPES and artifact.validation_status == "valid"
    ]
    artifact_counts = Counter(artifact.artifact_type for artifact in artifacts)

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "audit_mode": args.replay_mode,
        "runs_root": str(runs_dir),
        "scope": {"subject": args.subject},
        "canonical_db_compared": str(Path(args.canonical_db).expanduser().resolve())
        if args.canonical_db
        else None,
        "temp_rebuild_db": None,
        "artifacts_discovered": [artifact.as_report(runs_dir) for artifact in artifacts],
        "artifact_counts": dict(sorted(artifact_counts.items())),
        "artifacts_validated": sum(
            1 for artifact in artifacts if artifact.validation_status == "valid"
        ),
        "artifacts_missing": missing,
        "artifacts_stale_or_duplicate": [],
        "replay_plan": {
            "mode": args.replay_mode,
            "replayable_artifact_count": len(replayable),
            "reference_only_artifact_count": len(reference_only),
            "order": [artifact.as_report(runs_dir) for artifact in replayable],
        },
        "replay_results": [],
        "canonical_init_result": None,
        "canonical_validation_result": None,
        "graph_closure_result": None,
        "row_count_comparison": None,
        "key_hash_comparison": None,
        "unresolved_differences": [],
        "missing_replay_support": [
            {
                "artifact_type": artifact.artifact_type,
                "path": artifact.as_report(runs_dir)["path"],
                "reason": "reference artifact lacks a complete canonical replay recipe",
            }
            for artifact in reference_only
            if artifact.artifact_type
            in {"review_decision_apply_result", "publication_artifact", "release_readiness_report"}
        ],
        "warnings": [],
        "errors": [artifact.failure_reason for artifact in invalid if artifact.failure_reason],
        "final_status": "pending",
    }

    if missing:
        report["errors"].append("topic-cycle manifest references missing artifacts")
    if args.replay_mode == "validate_only":
        report["final_status"] = final_status(
            mode=args.replay_mode,
            invalid_count=len(invalid),
            missing_count=len(missing),
            missing_support_count=len(report["missing_replay_support"]),
            replay_errors=[],
            graph_closure_status=None,
            comparison_status=None,
        )
        return report, 0 if not invalid and not missing else 1

    temp_dir: Path | None = None
    try:
        rebuild_db, temp_dir = ensure_rebuild_db_path(args)
        report["temp_rebuild_db"] = str(rebuild_db)
        init = canonical_store.init_canonical_store(
            rebuild_db,
            applied_at=generated_at,
            applied_by="tools/scripts/audit_rebuildability.py",
        )
        report["canonical_init_result"] = {
            "schema_version": init.schema_version,
            "current_migration_id": init.current_migration_id,
            "created": init.created,
            "changed": init.changed,
        }
        replay_results, replay_errors = replay_artifacts(
            db_path=rebuild_db,
            artifacts=replayable,
            strict=args.strict,
        )
        report["replay_results"] = replay_results
        report["errors"].extend(replay_errors)
        check = canonical_store.check_canonical_store(rebuild_db)
        report["canonical_validation_result"] = {
            "status": "pass",
            "schema_version": check.schema_version,
            "current_migration_id": check.current_migration_id,
        }
        graph = canonical_graph_closure.audit_canonical_graph_closure(
            rebuild_db,
            generated_at=generated_at,
        )
        report["graph_closure_result"] = {
            "status": graph["status"],
            "summary": graph["summary"],
        }
        comparison_status: str | None = None
        if args.replay_mode == "compare_existing":
            if not args.canonical_db:
                raise RebuildabilityError("--canonical-db is required in compare_existing mode")
            existing_summary = db_summary(Path(args.canonical_db).expanduser().resolve())
            rebuilt_summary = db_summary(rebuild_db)
            comparison = compare_summaries(existing_summary, rebuilt_summary)
            comparison_status = str(comparison["status"])
            report["row_count_comparison"] = {
                "status": comparison_status,
                "differences": comparison["row_count_differences"],
            }
            report["key_hash_comparison"] = {
                "status": comparison_status,
                "differences": comparison["key_hash_differences"],
            }
        graph_status = str(graph["status"])
        report["final_status"] = final_status(
            mode=args.replay_mode,
            invalid_count=len(invalid),
            missing_count=len(missing),
            missing_support_count=len(report["missing_replay_support"]),
            replay_errors=replay_errors,
            graph_closure_status=graph_status,
            comparison_status=comparison_status,
        )
        if args.strict and report["final_status"] not in {
            "rebuildable",
            "rebuildable_with_warnings",
        }:
            return report, 1
        return report, 0 if report["final_status"] in {
            "rebuildable",
            "rebuildable_with_warnings",
            "incomplete_support",
        } else 1
    finally:
        if temp_dir is not None and not args.keep_temp_db:
            shutil.rmtree(temp_dir, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True, help="Run/workspace artifact root to audit.")
    parser.add_argument(
        "--output", required=True, help="Output canonical-rebuildability-report.v1 JSON path."
    )
    parser.add_argument(
        "--canonical-db", help="Optional existing canonical DB for compare_existing mode."
    )
    parser.add_argument("--temp-rebuild-db", help="Optional non-existing path for rebuilt temp DB.")
    parser.add_argument("--subject", help="Optional subject/workspace scope label.")
    parser.add_argument(
        "--include-failed-runs",
        action="store_true",
        help="Reserved: failed runs are discovered as artifacts but not replayed unless replayable.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Stop replay on first failure and return nonzero on warnings/failures.",
    )
    parser.add_argument(
        "--replay-mode",
        choices=("validate_only", "rebuild_temp", "compare_existing"),
        default="validate_only",
    )
    parser.add_argument(
        "--keep-temp-db",
        action="store_true",
        help="Do not remove the temporary rebuild DB after the report.",
    )
    parser.add_argument(
        "--force-temp-overwrite",
        action="store_true",
        help="Allow overwriting --temp-rebuild-db if it already exists.",
    )
    parser.add_argument("--generated-at", help="Timestamp override for deterministic reports.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def render_text(report: Mapping[str, Any]) -> str:
    return (
        "\n".join(
            [
                f"schema_version={report['schema_version']}",
                f"final_status={report['final_status']}",
                f"audit_mode={report['audit_mode']}",
                f"artifacts_validated={report['artifacts_validated']}",
                f"missing_count={len(report['artifacts_missing'])}",
                f"missing_replay_support_count={len(report['missing_replay_support'])}",
            ]
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report, exit_code = audit(args)
    except RebuildabilityError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
