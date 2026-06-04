#!/usr/bin/env python3
"""Run one bounded Summa topic cycle for an operator workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.scripts import resolve_subject_runtime  # noqa: E402
from tools.source_db_tools import canonical_ingest, canonical_store  # noqa: E402
from tools.validators.validate_candidate_feedback_plan import (  # noqa: E402
    EXIT_PASS as EXIT_FEEDBACK_PASS,
)
from tools.validators.validate_candidate_feedback_plan import (  # noqa: E402
    validate_candidate_feedback_plan,
)
from tools.validators.validate_gather_candidate_batch import (  # noqa: E402
    EXIT_PASS as EXIT_GATHER_PASS,
)
from tools.validators.validate_gather_candidate_batch import (  # noqa: E402
    validate_gather_candidate_batch,
)
from tools.validators.validate_source_acquisition_execution import (  # noqa: E402
    EXIT_PASS as EXIT_EXECUTION_PASS,
)
from tools.validators.validate_source_acquisition_execution import (  # noqa: E402
    validate_source_acquisition_execution,
)

SCHEMA_VERSION = "topic-cycle-run.v1"
DEFAULT_FACET = "sources"
DEFAULT_PHASE = "01a"
REMOTE_FETCH_ENABLED = False


class TopicCycleError(RuntimeError):
    """Raised when a topic cycle cannot continue safely."""


@dataclass
class StageRecord:
    name: str
    required: bool = True
    status: str = "planned"
    started_at: str | None = None
    ended_at: str | None = None
    command: list[str] | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] | None = None
    counts: dict[str, Any] | None = None
    skipped_reason: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "inputs": self.inputs,
            "artifacts": self.artifacts,
        }
        if self.command is not None:
            payload["command"] = self.command
        if self.validation is not None:
            payload["validation"] = self.validation
        if self.counts is not None:
            payload["counts"] = self.counts
        if self.skipped_reason is not None:
            payload["skipped_reason"] = self.skipped_reason
        if self.error_message is not None:
            payload["error_message"] = self.error_message
        return payload


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: str | None) -> str:
    if value is None:
        return utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TopicCycleError(f"timestamp must be RFC3339: {value}") from exc
    if parsed.tzinfo is None:
        raise TopicCycleError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_path(raw_path: str | Path, *, base: Path | None = None) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base or Path.cwd()) / path).resolve()


def hash_file(path: Path) -> str:
    return canonical_ingest.hash_file(path)


def read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TopicCycleError(f"could not read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise TopicCycleError(f"{label} must be a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one bounded local-first Summa topic cycle and write a structured "
            "topic-cycle-run.v1 manifest."
        ),
        epilog="Manifest artifact: topic-cycle-run.v1",
    )
    parser.add_argument("--workspace", required=True, help="Topic workspace root.")
    parser.add_argument(
        "--subject",
        help="Subject manifest path or subject_id. Defaults to <workspace>/.indexer/subject_manifest.json.",
    )
    parser.add_argument("--db", required=True, help="Initialized canonical SQLite store.")
    parser.add_argument(
        "--run-dir", required=True, help="Output directory for this topic-cycle run."
    )
    parser.add_argument("--run-id", help="Stable cycle run id. Defaults to the run directory name.")
    parser.add_argument("--timestamp", help="RFC3339 timestamp override for deterministic tests.")
    parser.add_argument(
        "--facet", default=DEFAULT_FACET, help="Gather facet when no feedback plan selects one."
    )
    parser.add_argument("--phase", default=DEFAULT_PHASE, help="Gather phase to render.")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "local", "live-safe"),
        default="dry-run",
        help="dry-run never mutates the supplied DB; local may ingest local fixture/stage artifacts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Convenience alias for --mode dry-run.",
    )
    parser.add_argument("--cycle-depth", type=int, default=1, help="1-based gather cycle depth.")
    parser.add_argument(
        "--previous-run-id", action="append", default=[], help="Prior gather run id. May repeat."
    )
    parser.add_argument(
        "--use-prior-state", action="store_true", help="Pass bounded prior-state context to gather."
    )
    parser.add_argument(
        "--feedback-plan",
        help="Candidate feedback plan path, or 'auto' to build one before gather.",
    )
    parser.add_argument(
        "--build-next-feedback-plan",
        action="store_true",
        help="Build a next-action feedback plan after any ingestion stage.",
    )
    parser.add_argument(
        "--candidate-batch-fixture",
        help="Optional validated gather-candidate-batch fixture to use for ingestion in local mode.",
    )
    parser.add_argument(
        "--source-handoff", help="Optional source-adapter handoff for local acquisition."
    )
    parser.add_argument(
        "--execution-run-fixture",
        help="Optional validated execution run directory to ingest instead of executing a handoff.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Reserved for later remote acquisition. F26 records this as disabled and refuses remote fetch.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow replacing an existing completed cycle manifest."
    )
    parser.add_argument(
        "--resume", action="store_true", help="Reserved; currently refuses partial runs clearly."
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def build_manifest(
    *,
    args: argparse.Namespace,
    run_id: str,
    started_at: str,
    run_dir: Path,
    workspace: Path,
    db_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workspace": {
            "path": str(workspace),
            "workspace_id": None,
        },
        "subject": None,
        "domain_pack": None,
        "canonical_db": {
            "path": str(db_path),
            "mutated": False,
            "initial_summary": None,
            "final_summary": None,
        },
        "run_dir": str(run_dir),
        "mode": args.mode,
        "dry_run": args.mode == "dry-run",
        "cycle_depth": args.cycle_depth,
        "previous_run_ids": list(args.previous_run_id),
        "stage_plan": [],
        "stages": [],
        "feedback_plan": None,
        "next_action": None,
        "budget": None,
        "budget_consumed": {
            "runtime_seconds": 0.0,
        },
        "status": "planned",
        "failure_stage": None,
        "error_summary": None,
        "warnings": [],
        "started_at": started_at,
        "ended_at": None,
        "no_network": True,
        "remote_fetch_enabled": REMOTE_FETCH_ENABLED,
        "llm_invoked": False,
    }


def command_text(command: list[str]) -> str:
    return " ".join(command)


def run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def fail_stage(stage: StageRecord, message: str) -> NoReturn:
    stage.status = "failed"
    stage.error_message = message
    stage.ended_at = utc_now()
    raise TopicCycleError(f"{stage.name}: {message}")


def finish_stage(stage: StageRecord, *, status: str = "passed") -> None:
    stage.status = status
    stage.ended_at = utc_now()


def add_stage(manifest: dict[str, Any], stage: StageRecord) -> None:
    manifest["stages"].append(stage.to_dict())


def validate_existing_run_dir(run_dir: Path, *, force: bool, resume: bool) -> None:
    manifest_path = run_dir / "topic-cycle-run.json"
    if not manifest_path.exists():
        return
    payload = read_json(manifest_path, label="existing topic-cycle manifest")
    status = payload.get("status")
    if status in {"completed", "dry_run"} and not force:
        raise TopicCycleError(
            f"topic cycle run already completed at {manifest_path}; use --force or a new run id"
        )
    if status in {"failed", "partial"} and not resume and not force:
        raise TopicCycleError(
            f"topic cycle run already exists with status {status}; use --resume, --force, or a new run id"
        )
    if resume:
        raise TopicCycleError(
            "--resume is reserved; use a new run id or --force for this F26 runner"
        )


def load_domain_pack_summary(domain_pack: str) -> dict[str, Any]:
    pack_path = REPO_ROOT / "config" / "domain_packs" / f"{domain_pack}.json"
    pack = read_json(pack_path, label="domain pack")
    return {
        "pack_id": pack.get("pack_id") or pack.get("id") or domain_pack,
        "version": pack.get("version"),
        "status": pack.get("status"),
        "path": str(pack_path),
    }


def resolve_runtime_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    stage = StageRecord(name="resolve_subject_runtime")
    stage.started_at = utc_now()
    subject_arg = args.subject or str(workspace / ".indexer" / "subject_manifest.json")
    stage.inputs = {"subject": subject_arg, "workspace": str(workspace)}
    try:
        runtime = resolve_subject_runtime.resolve_subject_runtime(subject_arg, str(workspace))
        subject = runtime["subject"]
        manifest["workspace"]["workspace_id"] = subject["subject_id"]
        manifest["subject"] = {
            "subject_id": subject["subject_id"],
            "display_name": subject["display_name"],
            "manifest_path": runtime["subject_manifest_path"],
            "domain_pack": subject["domain_pack"],
            "enabled_facets": subject["enabled_facets"],
        }
        finish_stage(stage)
        add_stage(manifest, stage)
        return runtime
    except Exception as exc:
        fail_stage(stage, str(exc))


def resolve_domain_pack_stage(
    *, runtime: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    stage = StageRecord(name="resolve_domain_pack")
    stage.started_at = utc_now()
    domain_pack = runtime["subject"]["domain_pack"]
    stage.inputs = {"domain_pack": domain_pack}
    try:
        summary = load_domain_pack_summary(domain_pack)
        manifest["domain_pack"] = summary
        finish_stage(stage)
        add_stage(manifest, stage)
        return summary
    except Exception as exc:
        fail_stage(stage, str(exc))


def validate_store_stage(*, manifest: dict[str, Any], db_path: Path) -> None:
    stage = StageRecord(name="validate_canonical_store")
    stage.started_at = utc_now()
    stage.inputs = {"db": str(db_path)}
    try:
        check = canonical_store.check_canonical_store(db_path)
        summary = canonical_store.summarize_canonical_store_population(db_path)
        manifest["canonical_db"]["initial_summary"] = summary
        stage.validation = {
            "status": "pass",
            "schema_version": check.schema_version,
            "current_migration_id": check.current_migration_id,
        }
        finish_stage(stage)
        add_stage(manifest, stage)
    except Exception as exc:
        fail_stage(stage, str(exc))


def build_feedback_plan_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    workspace: Path,
    db_path: Path,
    run_dir: Path,
    runtime: dict[str, Any],
    when: str,
) -> Path:
    stage = StageRecord(name=f"build_feedback_plan_{when}")
    stage.started_at = utc_now()
    feedback_dir = run_dir / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    output = feedback_dir / f"candidate-feedback-plan.{when}.json"
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "scripts" / "build_candidate_feedback_plan.py"),
        "--subject",
        runtime["subject_manifest_path"],
        "--workspace",
        str(workspace),
        "--db",
        str(db_path),
        "--output-json",
        str(output),
        "--generated-at",
        manifest["started_at"],
        "--format",
        "json",
    ]
    stage.command = command
    try:
        proc = run_command(command, cwd=REPO_ROOT)
        if proc.returncode != 0:
            fail_stage(stage, (proc.stderr or proc.stdout).strip() or "feedback planner failed")
        payload = read_json(output, label="candidate feedback plan")
        report, exit_code = validate_candidate_feedback_plan(output)
        stage.validation = {
            "status": "pass" if exit_code == EXIT_FEEDBACK_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_FEEDBACK_PASS:
            fail_stage(stage, "candidate feedback plan failed validation")
        stage.artifacts = {
            "feedback_plan": str(output),
            "feedback_plan_sha256": hash_file(output),
        }
        stage.counts = payload.get("counts")
        manifest["feedback_plan"] = {
            "path": str(output),
            "sha256": hash_file(output),
            "when": when,
        }
        manifest["next_action"] = payload.get("next_action")
        finish_stage(stage)
        add_stage(manifest, stage)
        return output
    except TopicCycleError:
        raise
    except Exception as exc:
        fail_stage(stage, str(exc))


def resolve_feedback_plan(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    workspace: Path,
    db_path: Path,
    run_dir: Path,
    runtime: dict[str, Any],
) -> Path | None:
    if args.feedback_plan == "auto":
        return build_feedback_plan_stage(
            args=args,
            manifest=manifest,
            workspace=workspace,
            db_path=db_path,
            run_dir=run_dir,
            runtime=runtime,
            when="pre",
        )
    if args.feedback_plan:
        path = resolve_path(args.feedback_plan)
        stage = StageRecord(name="load_feedback_plan")
        stage.started_at = utc_now()
        stage.inputs = {"feedback_plan": str(path)}
        report, exit_code = validate_candidate_feedback_plan(path)
        stage.validation = {
            "status": "pass" if exit_code == EXIT_FEEDBACK_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_FEEDBACK_PASS:
            fail_stage(stage, "feedback plan failed validation")
        payload = read_json(path, label="candidate feedback plan")
        manifest["feedback_plan"] = {"path": str(path), "sha256": hash_file(path), "when": "pre"}
        manifest["next_action"] = payload.get("next_action")
        finish_stage(stage)
        add_stage(manifest, stage)
        return path
    stage = StageRecord(
        name="feedback_plan_pre", required=False, status="skipped", skipped_reason="not requested"
    )
    add_stage(manifest, stage)
    return None


def gather_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    workspace: Path,
    db_path: Path,
    run_dir: Path,
    runtime: dict[str, Any],
    feedback_plan: Path | None,
) -> Path:
    stage = StageRecord(name="run_gather")
    stage.started_at = utc_now()
    gather_run_id = f"{manifest['run_id']}.gather"
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"),
        "--subject",
        runtime["subject_manifest_path"],
        "--workspace",
        str(workspace),
        "--facet",
        args.facet,
        "--phase",
        args.phase,
        "--mode",
        "dry-run",
        "--run-id",
        gather_run_id,
        "--created-at",
        manifest["started_at"],
        "--format",
        "json",
    ]
    if feedback_plan is not None:
        command.extend(["--feedback-plan", str(feedback_plan)])
    use_prior = args.use_prior_state or args.cycle_depth > 1
    if use_prior:
        command.extend(
            ["--db", str(db_path), "--use-prior-state", "--cycle-depth", str(args.cycle_depth)]
        )
        for previous_run_id in args.previous_run_id:
            command.extend(["--previous-run-id", previous_run_id])
    elif args.cycle_depth != 1:
        command.extend(["--cycle-depth", str(args.cycle_depth)])
    stage.command = command
    try:
        proc = run_command(command, cwd=REPO_ROOT)
        if proc.returncode != 0:
            fail_stage(stage, (proc.stderr or proc.stdout).strip() or "gather failed")
        payload = json.loads(proc.stdout)
        batch_path = resolve_path(payload["candidate_batch_path"], base=REPO_ROOT)
        prompt_path = resolve_path(payload["rendered_prompt_path"], base=REPO_ROOT)
        report, exit_code = validate_gather_candidate_batch(batch_path)
        stage.validation = {
            "status": "pass" if exit_code == EXIT_GATHER_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_GATHER_PASS:
            fail_stage(stage, "gather candidate batch failed validation")
        stage.artifacts = {
            "candidate_batch": str(batch_path),
            "candidate_batch_sha256": hash_file(batch_path),
            "rendered_prompt": str(prompt_path),
            "rendered_prompt_sha256": hash_file(prompt_path),
        }
        if payload.get("prior_state"):
            manifest["prior_state"] = {
                "context_hash": payload["prior_state"].get("context_hash"),
                "record_counts": payload["prior_state"].get("record_counts"),
            }
        finish_stage(stage)
        add_stage(manifest, stage)
        return batch_path
    except TopicCycleError:
        raise
    except Exception as exc:
        fail_stage(stage, str(exc))


def candidate_ingest_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    db_path: Path,
    batch_path: Path,
    run_dir: Path,
) -> dict[str, Any] | None:
    stage = StageRecord(name="ingest_candidate_batch", required=args.mode != "dry-run")
    stage.started_at = utc_now()
    ingest_batch_path = batch_path
    if args.candidate_batch_fixture:
        fixture = resolve_path(args.candidate_batch_fixture)
        fixture_report, fixture_exit = validate_gather_candidate_batch(fixture)
        if fixture_exit != EXIT_GATHER_PASS:
            stage.validation = {"status": "fail", "report": fixture_report}
            stage.status = "failed"
            stage.error_message = "candidate batch fixture failed validation"
            stage.ended_at = utc_now()
            add_stage(manifest, stage)
            raise TopicCycleError(
                "ingest_candidate_batch: candidate batch fixture failed validation"
            )
        fixture_target = run_dir / "candidate-ingest" / "gather-candidate-batch.json"
        fixture_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture, fixture_target)
        ingest_batch_path = fixture_target
    stage.inputs = {"candidate_batch": str(ingest_batch_path)}
    if args.mode == "dry-run":
        batch, batch_hash = canonical_ingest.load_validated_candidate_batch(ingest_batch_path)
        conn = canonical_store.connect_canonical_store(db_path)
        try:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=ingest_batch_path,
                batch_hash=batch_hash,
                dry_run=True,
                db_path=db_path,
            )
        finally:
            conn.close()
        stage.counts = report.get("counts")
        stage.artifacts = {"ingest_report": report, "mutated": False}
        finish_stage(stage, status="dry_run")
        add_stage(manifest, stage)
        return report
    try:
        batch, batch_hash = canonical_ingest.load_validated_candidate_batch(ingest_batch_path)
        conn = canonical_store.connect_canonical_store(db_path)
        try:
            with conn:
                report = canonical_ingest.ingest_candidate_batch(
                    conn,
                    batch,
                    batch_path=ingest_batch_path,
                    batch_hash=batch_hash,
                    dry_run=False,
                    db_path=db_path,
                )
        finally:
            conn.close()
        report_path = run_dir / "candidate-ingest" / "canonical-ingest-report.json"
        write_json(report_path, report)
        stage.counts = report.get("counts")
        stage.artifacts = {
            "ingest_report": str(report_path),
            "ingest_report_sha256": hash_file(report_path),
            "mutated": True,
        }
        manifest["canonical_db"]["mutated"] = True
        finish_stage(stage)
        add_stage(manifest, stage)
        return report
    except Exception as exc:
        fail_stage(stage, str(exc))


def acquisition_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    run_dir: Path,
) -> Path | None:
    if args.allow_network:
        manifest["warnings"].append(
            "remote acquisition remains disabled in F26; --allow-network was ignored"
        )
    if args.execution_run_fixture:
        stage = StageRecord(name="execute_source_adapter", required=False, status="skipped")
        stage.skipped_reason = "execution fixture supplied"
        add_stage(manifest, stage)
        return resolve_path(args.execution_run_fixture)
    if not args.source_handoff:
        stage = StageRecord(name="execute_source_adapter", required=False, status="skipped")
        stage.skipped_reason = "no source handoff supplied"
        add_stage(manifest, stage)
        return None
    stage = StageRecord(name="execute_source_adapter")
    stage.started_at = utc_now()
    output_dir = run_dir / "execution"
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"),
        "--handoff",
        str(resolve_path(args.source_handoff)),
        "--output",
        str(output_dir),
        "--mode",
        "local",
        "--run-id",
        f"{manifest['run_id']}.execution",
        "--created-at",
        manifest["started_at"],
    ]
    stage.command = command
    try:
        proc = run_command(command, cwd=REPO_ROOT)
        if proc.returncode != 0:
            fail_stage(
                stage, (proc.stderr or proc.stdout).strip() or "source adapter execution failed"
            )
        report, exit_code = validate_source_acquisition_execution(output_dir)
        stage.validation = {
            "status": "pass" if exit_code == EXIT_EXECUTION_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_EXECUTION_PASS:
            fail_stage(stage, "execution artifacts failed validation")
        stage.artifacts = {
            "execution_run_dir": str(output_dir),
            "execution_record": str(output_dir / "execution-record.json"),
            "capture_events": str(output_dir / "capture-events.jsonl"),
            "extraction_records": str(output_dir / "extraction-records.jsonl"),
        }
        finish_stage(stage)
        add_stage(manifest, stage)
        return output_dir
    except TopicCycleError:
        raise
    except Exception as exc:
        fail_stage(stage, str(exc))


def execution_ingest_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    db_path: Path,
    execution_run_dir: Path | None,
    run_dir: Path,
) -> dict[str, Any] | None:
    if execution_run_dir is None:
        stage = StageRecord(name="ingest_execution_artifacts", required=False, status="skipped")
        stage.skipped_reason = "no execution artifacts available"
        add_stage(manifest, stage)
        return None
    stage = StageRecord(name="ingest_execution_artifacts", required=args.mode != "dry-run")
    stage.started_at = utc_now()
    stage.inputs = {"execution_run_dir": str(execution_run_dir)}
    try:
        execution_record, capture_events, extraction_records, paths, input_hashes = (
            canonical_ingest.load_validated_execution_artifacts(execution_run_dir)
        )
        conn = canonical_store.connect_canonical_store(db_path)
        try:
            if args.mode == "dry-run":
                report = canonical_ingest.ingest_execution_artifacts(
                    conn,
                    execution_record,
                    capture_events,
                    extraction_records,
                    paths=paths,
                    input_hashes=input_hashes,
                    dry_run=True,
                    db_path=db_path,
                )
            else:
                with conn:
                    report = canonical_ingest.ingest_execution_artifacts(
                        conn,
                        execution_record,
                        capture_events,
                        extraction_records,
                        paths=paths,
                        input_hashes=input_hashes,
                        dry_run=False,
                        db_path=db_path,
                    )
        finally:
            conn.close()
        if args.mode == "dry-run":
            stage.artifacts = {"ingest_report": report, "mutated": False}
            finish_stage(stage, status="dry_run")
        else:
            report_path = run_dir / "execution-ingest" / "canonical-ingest-report.json"
            write_json(report_path, report)
            stage.artifacts = {
                "ingest_report": str(report_path),
                "ingest_report_sha256": hash_file(report_path),
                "mutated": True,
            }
            manifest["canonical_db"]["mutated"] = True
            finish_stage(stage)
        stage.counts = report.get("counts")
        add_stage(manifest, stage)
        return report
    except Exception as exc:
        fail_stage(stage, str(exc))


def publication_stage(*, manifest: dict[str, Any]) -> None:
    stage = StageRecord(name="build_publication", required=False, status="skipped")
    stage.skipped_reason = "publication rebuild not requested in this F26 runner"
    add_stage(manifest, stage)


def final_store_stage(*, manifest: dict[str, Any], db_path: Path) -> None:
    stage = StageRecord(name="final_canonical_store_summary")
    stage.started_at = utc_now()
    try:
        summary = canonical_store.summarize_canonical_store_population(db_path)
        manifest["canonical_db"]["final_summary"] = summary
        stage.counts = {
            "total_rows": summary.get("total_rows"),
            "family_counts": summary.get("family_counts"),
        }
        finish_stage(stage)
        add_stage(manifest, stage)
    except Exception as exc:
        fail_stage(stage, str(exc))


def render_text(manifest: dict[str, Any]) -> str:
    lines = [
        f"schema_version={manifest['schema_version']}",
        f"run_id={manifest['run_id']}",
        f"status={manifest['status']}",
        f"mode={manifest['mode']}",
        f"workspace={manifest['workspace']['path']}",
        f"db={manifest['canonical_db']['path']}",
        f"canonical_db_mutated={str(manifest['canonical_db']['mutated']).lower()}",
    ]
    if manifest.get("failure_stage"):
        lines.append(f"failure_stage={manifest['failure_stage']}")
    for stage in manifest["stages"]:
        lines.append(f"stage.{stage['name']}={stage['status']}")
    return "\n".join(lines) + "\n"


def run_topic_cycle(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if getattr(args, "dry_run", False):
        args.mode = "dry-run"
    started_at = normalize_timestamp(args.timestamp)
    workspace = resolve_path(args.workspace)
    db_path = resolve_path(args.db)
    run_dir = resolve_path(args.run_dir)
    run_id = args.run_id or run_dir.name
    if args.cycle_depth < 1:
        raise TopicCycleError("--cycle-depth must be at least 1")
    if not workspace.is_dir():
        raise TopicCycleError(f"workspace root not found: {workspace}")
    validate_existing_run_dir(run_dir, force=args.force, resume=args.resume)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(
        args=args,
        run_id=run_id,
        started_at=started_at,
        run_dir=run_dir,
        workspace=workspace,
        db_path=db_path,
    )
    manifest["stage_plan"] = [
        "resolve_subject_runtime",
        "resolve_domain_pack",
        "validate_canonical_store",
        "feedback_plan_pre",
        "run_gather",
        "ingest_candidate_batch",
        "execute_source_adapter",
        "ingest_execution_artifacts",
        "feedback_plan_post",
        "build_publication",
        "final_canonical_store_summary",
    ]
    manifest_path = run_dir / "topic-cycle-run.json"
    started = time.monotonic()
    try:
        runtime = resolve_runtime_stage(args=args, manifest=manifest, workspace=workspace)
        resolve_domain_pack_stage(runtime=runtime, manifest=manifest)
        validate_store_stage(manifest=manifest, db_path=db_path)
        feedback_plan = resolve_feedback_plan(
            args=args,
            manifest=manifest,
            workspace=workspace,
            db_path=db_path,
            run_dir=run_dir,
            runtime=runtime,
        )
        batch_path = gather_stage(
            args=args,
            manifest=manifest,
            workspace=workspace,
            db_path=db_path,
            run_dir=run_dir,
            runtime=runtime,
            feedback_plan=feedback_plan,
        )
        candidate_ingest_stage(
            args=args, manifest=manifest, db_path=db_path, batch_path=batch_path, run_dir=run_dir
        )
        execution_run_dir = acquisition_stage(args=args, manifest=manifest, run_dir=run_dir)
        execution_ingest_stage(
            args=args,
            manifest=manifest,
            db_path=db_path,
            execution_run_dir=execution_run_dir,
            run_dir=run_dir,
        )
        if args.build_next_feedback_plan:
            build_feedback_plan_stage(
                args=args,
                manifest=manifest,
                workspace=workspace,
                db_path=db_path,
                run_dir=run_dir,
                runtime=runtime,
                when="post",
            )
        else:
            stage = StageRecord(name="feedback_plan_post", required=False, status="skipped")
            stage.skipped_reason = "not requested"
            add_stage(manifest, stage)
        publication_stage(manifest=manifest)
        final_store_stage(manifest=manifest, db_path=db_path)
        manifest["status"] = "dry_run" if args.mode == "dry-run" else "completed"
        return_code = 0
    except TopicCycleError as exc:
        manifest["status"] = "failed"
        last_failed = next(
            (stage for stage in reversed(manifest["stages"]) if stage["status"] == "failed"), None
        )
        manifest["failure_stage"] = last_failed["name"] if last_failed else "cycle_setup"
        manifest["error_summary"] = str(exc)
        return_code = 1
    finally:
        manifest["ended_at"] = utc_now()
        manifest["budget_consumed"]["runtime_seconds"] = round(time.monotonic() - started, 6)
        write_json(manifest_path, manifest)
    return manifest, return_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest, exit_code = run_topic_cycle(args)
    except TopicCycleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.format == "text":
        sys.stdout.write(render_text(manifest))
    else:
        sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
