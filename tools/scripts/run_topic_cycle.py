#!/usr/bin/env python3
"""Run one bounded Summa topic cycle for an operator workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.subprocess_capture import (  # noqa: E402
    command_output_excerpt,
    run_streaming_command,
)
from tools.common.workspace_lock import (  # noqa: E402
    DEFAULT_LOCK_ROOT,
    WorkspaceLockError,
    acquire_workspace_lock,
)
from tools.scripts import resolve_subject_runtime  # noqa: E402
from tools.source_db_tools import (  # noqa: E402
    canonical_graph_closure,
    canonical_ingest,
    canonical_store,
    canonical_write_spool,
    cycle_evidence_ledger,
)
from tools.validators import (  # noqa: E402
    validate_gather_candidate_batch as gather_candidate_batch_validator,  # noqa: E402
)
from tools.validators import (  # noqa: E402
    validate_source_acquisition_execution as execution_validator,  # noqa: E402
)
from tools.validators import validate_source_adapter_handoff  # noqa: E402
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
    ExecutionArtifactReceipt,
    load_execution_artifacts,
    validate_execution_artifact_receipt,
)

SCHEMA_VERSION = "topic-cycle-run.v1"
DEFAULT_FACET = "sources"
DEFAULT_PHASE = "01a"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0
KNOWN_RUN_STATUSES = {"completed", "dry_run", "failed", "partial"}
REMOTE_FETCH_ENABLED = False


class TopicCycleError(RuntimeError):
    """Raised when a topic cycle cannot continue safely."""

    def __init__(self, message: str, *, stage_name: str | None = None) -> None:
        super().__init__(message)
        self.stage_name = stage_name


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
    evidence: dict[str, Any] | None = None

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
        if self.evidence is not None:
            payload["evidence"] = self.evidence
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


def load_handoff_adapter_path(handoff_path: Path) -> Path:
    loaded_records, errors, exit_code = validate_source_adapter_handoff.load_records(handoff_path)
    if exit_code != validate_source_adapter_handoff.EXIT_PASS or not loaded_records:
        message = errors[0]["message"] if errors else "source-adapter handoff could not be loaded"
        raise TopicCycleError(message, stage_name="execute_source_adapter")
    adapter_paths = {
        str(record.get("adapter_path", "")).strip()
        for _, record in loaded_records
        if isinstance(record, dict)
    }
    if len(adapter_paths) != 1:
        raise TopicCycleError(
            "source-adapter handoff must contain records from exactly one adapter_path",
            stage_name="execute_source_adapter",
        )
    adapter_path_value = next(iter(adapter_paths))
    if not adapter_path_value:
        raise TopicCycleError(
            "source-adapter handoff records must include a non-blank adapter_path",
            stage_name="execute_source_adapter",
        )
    adapter_path = resolve_path(adapter_path_value)
    if not adapter_path.exists() or not adapter_path.is_file():
        raise TopicCycleError(
            f"trusted adapter manifest is unavailable: {adapter_path}",
            stage_name="execute_source_adapter",
        )
    return adapter_path


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
    atomic_write_json(path, payload)


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
        "--command-timeout-seconds",
        type=float,
        help=(
            "Maximum seconds to allow each child subprocess call. Defaults to 600 seconds "
            "when not supplied."
        ),
    )
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
        "--skip-workspace-lock",
        action="store_true",
        help="Assume an outer wrapper already holds the workspace lock.",
    )
    parser.add_argument(
        "--degraded-spool",
        action="store_true",
        help="Preserve validated canonical-write intents as spool records if the DB is unavailable.",
    )
    parser.add_argument(
        "--spool-dir",
        help="Directory for degraded canonical-write spool records. Defaults to <run-dir>/spool.",
    )
    parser.add_argument(
        "--graph-closure",
        dest="graph_closure",
        action="store_true",
        default=False,
        help="Run read-only canonical graph-closure audit before cycle close.",
    )
    parser.add_argument(
        "--no-graph-closure",
        dest="graph_closure",
        action="store_false",
        help="Disable the graph-closure audit and record the disabled reason in the manifest.",
    )
    parser.add_argument(
        "--graph-closure-strict",
        action="store_true",
        help="Fail the cycle when graph closure reports true orphan errors.",
    )
    parser.add_argument(
        "--graph-closure-report",
        help="Optional graph-closure report path. Defaults to <run-dir>/graph-closure-report.json.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow replacing an existing completed cycle manifest."
    )
    parser.add_argument(
        "--resume", action="store_true", help="Reserved; currently refuses partial runs clearly."
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="Render compact text by default; use json when you need the full manifest on stdout.",
    )
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
    cycle_event_id = cycle_evidence_ledger.build_cycle_event_id(
        run_id=run_id,
        started_at=started_at,
        workspace_ref=str(workspace),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "cycle_event_id": cycle_event_id,
        "cycle_evidence_ledger": {
            "schema_version": cycle_evidence_ledger.SCHEMA_VERSION,
            "cycle_event_id": cycle_event_id,
            "recording_policy": "skipped_for_dry_run"
            if args.mode == "dry-run"
            else "record_after_manifest_write",
            "status": "pending",
        },
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
        "feedback_plan_pre": None,
        "feedback_plan_post": None,
        "active_feedback_plan_for_gather": None,
        "selection_explanations": [],
        "next_action": None,
        "operator_overrides": collect_operator_overrides(args),
        "budget": None,
        "budget_consumed": {
            "runtime_seconds": 0.0,
        },
        "status": "planned",
        "failure_stage": None,
        "error_summary": None,
        "warnings": [],
        "spool_records": [],
        "graph_closure": {
            "enabled": bool(args.graph_closure),
            "strict": bool(args.graph_closure_strict),
            "status": "pending" if args.graph_closure else "disabled",
            "report_path": None,
            "report_sha256": None,
            "orphan_error_count": 0,
            "unresolved_tracked_count": 0,
            "repairable_count": 0,
            "quarantined_count": 0,
            "disabled_reason": None if args.graph_closure else "disabled_by_operator_flag",
        },
        "started_at": started_at,
        "ended_at": None,
        "no_network": True,
        "remote_fetch_enabled": REMOTE_FETCH_ENABLED,
        "llm_invoked": False,
    }


def build_stage_plan(
    *,
    feedback_plan_mode: str | None,
    build_next_feedback_plan: bool,
    include_source_adapter: bool = False,
    include_execution_ingest: bool = False,
    include_graph_closure: bool = False,
) -> list[str]:
    if feedback_plan_mode == "auto":
        feedback_plan_pre = "build_feedback_plan_pre"
    elif feedback_plan_mode:
        feedback_plan_pre = "load_feedback_plan"
    else:
        feedback_plan_pre = "feedback_plan_pre"

    feedback_plan_post = (
        "build_feedback_plan_post" if build_next_feedback_plan else "feedback_plan_post"
    )
    stages = [
        "resolve_subject_runtime",
        "resolve_domain_pack",
        "validate_canonical_store",
        feedback_plan_pre,
        "run_gather",
        "ingest_candidate_batch",
    ]
    if include_source_adapter:
        stages.append("execute_source_adapter")
    if include_execution_ingest:
        stages.append("ingest_execution_artifacts")
    stages.append(feedback_plan_post)
    stages.append("final_canonical_store_summary")
    if include_graph_closure:
        stages.append("graph_closure_audit")
    return stages


def attach_feedback_selection_explanation(
    manifest: dict[str, Any],
    *,
    payload: dict[str, Any],
    path: Path,
    when: str,
    sha256: str | None = None,
) -> None:
    explanation = payload.get("selection_explanation")
    if not isinstance(explanation, dict):
        return
    explanation_id = explanation.get("explanation_id")
    if not isinstance(explanation_id, str) or not explanation_id:
        return
    entries = manifest.setdefault("selection_explanations", [])
    if not isinstance(entries, list):
        manifest["selection_explanations"] = entries = []
    entries.append(
        {
            "selection_explanation_id": explanation_id,
            "selection_kind": explanation.get("selection_kind"),
            "source": "feedback_plan",
            "when": when,
            "path": str(path),
            "sha256": hash_file(path) if sha256 is None else sha256,
        }
    )


def record_feedback_plan_reference(
    manifest: dict[str, Any], *, path: Path, when: str, sha256: str | None = None
) -> None:
    record = {
        "path": str(path),
        "sha256": hash_file(path) if sha256 is None else sha256,
        "when": when,
    }
    if when == "post":
        manifest["feedback_plan_post"] = record
        return
    manifest["feedback_plan_pre"] = record
    manifest["feedback_plan"] = record
    manifest["active_feedback_plan_for_gather"] = record


def collect_operator_overrides(args: argparse.Namespace) -> list[dict[str, str]]:
    overrides: list[dict[str, str]] = []
    if getattr(args, "force", False):
        overrides.append(
            {
                "override_kind": "force",
                "override_value": "true",
                "reason": "operator allowed replacing an existing completed cycle manifest",
                "actor": "operator",
            }
        )
    if getattr(args, "resume", False):
        overrides.append(
            {
                "override_kind": "resume",
                "override_value": "true",
                "reason": "operator requested partial-run resume handling",
                "actor": "operator",
            }
        )
    if getattr(args, "allow_network", False):
        overrides.append(
            {
                "override_kind": "allow_network",
                "override_value": "true",
                "reason": "operator requested network allowance; this runner still keeps remote fetch disabled",
                "actor": "operator",
            }
        )
    for attr, kind in (
        ("feedback_plan", "manual_feedback_plan"),
        ("candidate_batch_fixture", "manual_candidate_batch_fixture"),
        ("source_handoff", "manual_source_handoff"),
        ("execution_run_fixture", "manual_execution_run_fixture"),
    ):
        value = getattr(args, attr, None)
        if value:
            overrides.append(
                {
                    "override_kind": kind,
                    "override_value": str(value),
                    "reason": "operator supplied an explicit local artifact input",
                    "actor": "operator",
                }
            )
    return overrides


def command_text(command: list[str]) -> str:
    return " ".join(command)


def resolve_command_timeout_seconds(args: argparse.Namespace) -> float:
    timeout_seconds = getattr(args, "command_timeout_seconds", None)
    if timeout_seconds is None:
        return DEFAULT_COMMAND_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        raise TopicCycleError("--command-timeout-seconds must be greater than zero")
    return float(timeout_seconds)


def run_command(command: list[str], *, cwd: Path, timeout: float | None = None) -> object:
    return run_streaming_command(command, cwd=cwd, timeout=timeout)


def fail_stage(stage: StageRecord, message: str) -> NoReturn:
    stage.status = "failed"
    stage.error_message = message
    stage.ended_at = utc_now()
    raise TopicCycleError(f"{stage.name}: {message}", stage_name=stage.name)


def finish_stage(stage: StageRecord, *, status: str = "passed") -> None:
    stage.status = status
    stage.ended_at = utc_now()


def add_stage(manifest: dict[str, Any], stage: StageRecord) -> None:
    manifest["stages"].append(stage.to_dict())


def validate_existing_run_dir(run_dir: Path, *, force: bool, resume: bool) -> None:
    manifest_path = run_dir / "topic-cycle-run.json"
    if resume:
        raise TopicCycleError(
            "--resume is reserved; use a new run id or --force for this F26 runner"
        )
    if not manifest_path.exists():
        return
    payload = read_json(manifest_path, label="existing topic-cycle manifest")
    status = payload.get("status")
    if status not in KNOWN_RUN_STATUSES:
        raise TopicCycleError(
            f"topic cycle run already exists with unknown status {status!r}; use --force or a new run id"
        )
    if status in {"completed", "dry_run"} and not force:
        raise TopicCycleError(
            f"topic cycle run already completed at {manifest_path}; use --force or a new run id"
        )
    if status in {"failed", "partial"} and not resume and not force:
        raise TopicCycleError(
            f"topic cycle run already exists with status {status}; use --resume, --force, or a new run id"
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


def spool_dir_for(args: argparse.Namespace, run_dir: Path) -> Path:
    return resolve_path(args.spool_dir) if args.spool_dir else run_dir / "spool"


def add_spool_record_to_manifest(
    manifest: dict[str, Any], *, spool_path: Path, record: dict[str, Any]
) -> None:
    manifest.setdefault("spool_records", []).append(
        {
            "spool_record_id": record["spool_record_id"],
            "operation_kind": record["operation_kind"],
            "path": str(spool_path),
            "failure_kind": record["failure_kind"],
            "replay_status": record["replay_status"],
        }
    )


def validate_store_stage(
    *, args: argparse.Namespace, manifest: dict[str, Any], db_path: Path
) -> None:
    stage = StageRecord(name="validate_canonical_store")
    stage.started_at = utc_now()
    stage.inputs = {"db": str(db_path)}
    try:
        conn = canonical_store.connect_existing_read_only(db_path)
        try:
            outline = canonical_store.load_canonical_outline()
            version_row, table_set, extra_tables = canonical_store.validate_existing_store(
                conn,
                outline=outline,
            )
            validation = canonical_store.CheckResult(
                db_path=canonical_store.resolve_db_path(db_path),
                schema_version=version_row.schema_version,
                current_migration_id=version_row.current_migration_id,
                tables=tuple(sorted(table_set)),
                extra_tables=tuple(sorted(extra_tables)),
            )
            summary = canonical_store.summarize_canonical_store_population(
                db_path,
                include_counts=False,
                conn=conn,
                validation=validation,
            )
        finally:
            conn.close()
        manifest["canonical_db"]["initial_summary"] = summary
        stage.validation = {
            "status": "pass",
            "schema_version": summary["schema_version"],
            "current_migration_id": summary["current_migration_id"],
        }
        finish_stage(stage)
        add_stage(manifest, stage)
    except Exception as exc:
        if isinstance(exc, canonical_ingest.CanonicalIngestError) and "validation failed" in str(
            exc
        ):
            fail_stage(stage, str(exc))
        if args.degraded_spool:
            stage.validation = {"status": "degraded", "error": str(exc)}
            manifest.setdefault("warnings", []).append(
                f"canonical store validation degraded; canonical writes will spool: {exc}"
            )
            finish_stage(stage, status="degraded")
            add_stage(manifest, stage)
            return
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
        "--feedback-plan-stage",
        f"build_feedback_plan_{when}",
        "--format",
        "json",
    ]
    stage.command = command
    try:
        proc = run_command(command, cwd=REPO_ROOT, timeout=resolve_command_timeout_seconds(args))
        if proc.returncode != 0:
            fail_stage(stage, command_output_excerpt(proc) or "feedback planner failed")
        payload = read_json(output, label="candidate feedback plan")
        report, exit_code = validate_candidate_feedback_plan(output)
        stage.validation = {
            "status": "pass" if exit_code == EXIT_FEEDBACK_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_FEEDBACK_PASS:
            fail_stage(stage, "candidate feedback plan failed validation")
        feedback_hash = hash_file(output)
        stage.evidence = {
            "artifact_schema_ids": {
                "feedback_plan": payload.get("schema_version"),
            },
            "feedback_plan": {
                "schema_version": payload.get("schema_version"),
                "selection_explanation": payload.get("selection_explanation"),
                "next_action": payload.get("next_action"),
                "deferred": payload.get("deferred"),
                "artifact_path": str(output),
            },
        }
        stage.artifacts = {
            "feedback_plan": str(output),
            "feedback_plan_sha256": feedback_hash,
        }
        stage.counts = payload.get("counts")
        record_feedback_plan_reference(manifest, path=output, when=when, sha256=feedback_hash)
        if when != "post" or manifest.get("next_action") is None:
            manifest["next_action"] = payload.get("next_action")
        attach_feedback_selection_explanation(
            manifest,
            payload=payload,
            path=output,
            when=when,
            sha256=feedback_hash,
        )
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
        stage.evidence = {
            "artifact_schema_ids": {
                "feedback_plan": payload.get("schema_version"),
            },
            "feedback_plan": {
                "schema_version": payload.get("schema_version"),
                "selection_explanation": payload.get("selection_explanation"),
                "next_action": payload.get("next_action"),
                "deferred": payload.get("deferred"),
                "artifact_path": str(path),
            },
        }
        feedback_hash = hash_file(path)
        record_feedback_plan_reference(manifest, path=path, when="pre", sha256=feedback_hash)
        manifest["next_action"] = payload.get("next_action")
        attach_feedback_selection_explanation(
            manifest,
            payload=payload,
            path=path,
            when="pre",
            sha256=feedback_hash,
        )
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
) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    stage = StageRecord(name="run_gather")
    stage.started_at = utc_now()
    gather_run_id = f"{manifest['run_id']}.gather"
    gather_facet = args.facet
    if feedback_plan is not None:
        next_action = manifest.get("next_action")
        if isinstance(next_action, dict):
            selected_facet = next_action.get("selected_facet")
            if isinstance(selected_facet, str) and selected_facet.strip():
                gather_facet = selected_facet.strip()
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"),
        "--subject",
        runtime["subject_manifest_path"],
        "--workspace",
        str(workspace),
        "--facet",
        gather_facet,
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
        proc = run_command(command, cwd=REPO_ROOT, timeout=resolve_command_timeout_seconds(args))
        if proc.returncode != 0:
            fail_stage(stage, command_output_excerpt(proc) or "gather failed")
        payload = json.loads(proc.stdout)
        batch_path = resolve_path(payload["candidate_batch_path"], base=REPO_ROOT)
        prompt_path = resolve_path(payload["rendered_prompt_path"], base=REPO_ROOT)
        candidate_batch_sha256 = payload.get("candidate_batch_sha256")
        rendered_prompt_sha256 = payload.get("rendered_prompt_sha256")
        if not isinstance(candidate_batch_sha256, str):
            candidate_batch_sha256 = hash_file(batch_path)
        if not isinstance(rendered_prompt_sha256, str):
            rendered_prompt_sha256 = hash_file(prompt_path)
        batch, report, exit_code = (
            gather_candidate_batch_validator.load_validated_gather_candidate_batch(batch_path)
        )
        stage.validation = {
            "status": "pass" if exit_code == EXIT_GATHER_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_GATHER_PASS:
            fail_stage(stage, "gather candidate batch failed validation")
        validation_receipt = {
            "artifact_path": str(batch_path),
            "artifact_hash": candidate_batch_sha256,
            "validator_name": gather_candidate_batch_validator.VALIDATOR_NAME,
            "validator_version": gather_candidate_batch_validator.CONTRACT_VERSION,
            "result": report,
        }
        validation_receipt_path = (
            run_dir / "candidate-ingest" / "gather-candidate-batch-validation.json"
        )
        validation_receipt_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(validation_receipt_path, validation_receipt)
        stage.artifacts = {
            "candidate_batch": str(batch_path),
            "candidate_batch_sha256": candidate_batch_sha256,
            "candidate_batch_validation_receipt": str(validation_receipt_path),
            "candidate_batch_validation_receipt_sha256": hash_file(validation_receipt_path),
            "rendered_prompt": str(prompt_path),
            "rendered_prompt_sha256": rendered_prompt_sha256,
        }
        if payload.get("prior_state"):
            manifest["prior_state"] = {
                "context_hash": payload["prior_state"].get("context_hash"),
                "record_counts": payload["prior_state"].get("record_counts"),
            }
        finish_stage(stage)
        add_stage(manifest, stage)
        return batch_path, batch, candidate_batch_sha256, validation_receipt
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
    candidate_batch: dict[str, Any] | None = None,
    candidate_batch_hash: str | None = None,
    validation_receipt: dict[str, Any] | None = None,
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
        fixture_payload = read_json(fixture, label="candidate batch fixture")
        prompt_ref = (
            fixture_payload.get("prompt") if isinstance(fixture_payload.get("prompt"), dict) else {}
        )
        rendered_prompt_path = prompt_ref.get("rendered_prompt_path")
        if isinstance(rendered_prompt_path, str) and rendered_prompt_path:
            source_prompt_path = (
                resolve_path(rendered_prompt_path)
                if Path(rendered_prompt_path).is_absolute()
                else fixture.parent / rendered_prompt_path
            )
            if source_prompt_path.is_file():
                target_prompt_path = (
                    resolve_path(rendered_prompt_path)
                    if Path(rendered_prompt_path).is_absolute()
                    else fixture_target.parent / rendered_prompt_path
                )
                if source_prompt_path != target_prompt_path:
                    target_prompt_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_prompt_path, target_prompt_path)
        ingest_batch_path = fixture_target
    stage.inputs = {"candidate_batch": str(ingest_batch_path)}
    batch = candidate_batch
    batch_hash = candidate_batch_hash
    if batch is None or batch_hash is None:
        batch, batch_hash = canonical_ingest.load_validated_candidate_batch(ingest_batch_path)
    elif validation_receipt is not None:
        receipt_artifact_path = validation_receipt.get("artifact_path")
        receipt_artifact_hash = validation_receipt.get("artifact_hash")
        receipt_validator_name = validation_receipt.get("validator_name")
        receipt_validator_version = validation_receipt.get("validator_version")
        receipt_result = validation_receipt.get("result")
        if (
            not isinstance(receipt_artifact_path, str)
            or Path(receipt_artifact_path).resolve() != ingest_batch_path.resolve()
            or receipt_artifact_hash != batch_hash
            or receipt_validator_name != gather_candidate_batch_validator.VALIDATOR_NAME
            or receipt_validator_version != gather_candidate_batch_validator.CONTRACT_VERSION
            or not isinstance(receipt_result, dict)
        ):
            fail_stage(stage, "candidate batch validation receipt does not match the ingest batch")
    if args.mode == "dry-run":
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
        report_path = run_dir / "candidate-ingest" / "canonical-ingest-report.json"
        write_json(report_path, report)
        stage.evidence = {
            "artifact_schema_ids": {
                "candidate_batch": batch.get("schema_version"),
                "ingest_report": report.get("schema_version"),
            },
            "candidate_batch": {
                "schema_version": batch.get("schema_version"),
                "facet": batch.get("facet"),
                "candidates": batch.get("candidates"),
                "artifact_path": str(ingest_batch_path),
            },
        }
        stage.counts = report.get("counts")
        stage.artifacts = {
            "ingest_report": str(report_path),
            "ingest_report_sha256": hash_file(report_path),
            "mutated": False,
        }
        finish_stage(stage, status="dry_run")
        add_stage(manifest, stage)
        return report
    try:
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
        stage.evidence = {
            "artifact_schema_ids": {
                "candidate_batch": batch.get("schema_version"),
                "ingest_report": report.get("schema_version"),
            },
            "candidate_batch": {
                "schema_version": batch.get("schema_version"),
                "facet": batch.get("facet"),
                "candidates": batch.get("candidates"),
                "artifact_path": str(ingest_batch_path),
            },
        }
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
        if isinstance(exc, canonical_ingest.CanonicalIngestError) and "validation failed" in str(
            exc
        ):
            fail_stage(stage, str(exc))
        if args.degraded_spool:
            record = canonical_write_spool.build_spool_record(
                operation_kind="candidate_batch_ingest",
                operation_input={
                    "artifact_refs": [
                        {
                            "artifact_type": "gather_candidate_batch",
                            "artifact_path": str(ingest_batch_path),
                            "artifact_hash": batch_hash,
                        }
                    ]
                },
                replay_recipe={
                    "batch_path": str(ingest_batch_path),
                    "batch_hash": batch_hash,
                    "strict": True,
                },
                failure=exc,
                canonical_db_path=db_path,
                spool_dir=spool_dir_for(args, run_dir),
                originating_tool="tools/scripts/run_topic_cycle.py",
                originating_command="run_topic_cycle.py",
                originating_run_id=str(manifest["run_id"]),
                topic_cycle_id=str(manifest["cycle_event_id"]),
                stage_name="ingest_candidate_batch",
                workspace_id=manifest["workspace"].get("workspace_id"),
                subject_id=manifest.get("subject", {}).get("subject_id")
                if isinstance(manifest.get("subject"), dict)
                else None,
                expected_schema_version=None,
            )
            spool_path = canonical_write_spool.write_spool_record(
                spool_dir_for(args, run_dir), record
            )
            stage.artifacts = {
                "spool_record": str(spool_path),
                "spool_record_sha256": hash_file(spool_path),
                "mutated": False,
            }
            stage.error_message = str(exc)
            finish_stage(stage, status="spooled")
            add_stage(manifest, stage)
            add_spool_record_to_manifest(manifest, spool_path=spool_path, record=record)
            manifest.setdefault("warnings", []).append(
                f"candidate batch ingest spooled after canonical write failure: {exc}"
            )
            return {
                "schema_version": canonical_ingest.INGEST_REPORT_SCHEMA_VERSION,
                "ingest_kind": "candidate_batch",
                "status": "spooled",
                "spool_record_path": str(spool_path),
            }
        fail_stage(stage, str(exc))


def acquisition_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    run_dir: Path,
) -> tuple[Path | None, ExecutionArtifactReceipt | None]:
    if args.allow_network:
        manifest["warnings"].append(
            "remote acquisition remains disabled in F26; --allow-network was ignored"
        )
    if args.execution_run_fixture:
        return resolve_path(args.execution_run_fixture), None
    if not args.source_handoff:
        return None, None
    stage = StageRecord(name="execute_source_adapter")
    stage.started_at = utc_now()
    output_dir = run_dir / "execution"
    adapter_path = load_handoff_adapter_path(resolve_path(args.source_handoff))
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"),
        "--handoff",
        str(resolve_path(args.source_handoff)),
        "--adapter",
        str(adapter_path),
        "--output",
        str(output_dir),
        "--workspace-root",
        str(run_dir),
        "--mode",
        "local",
        "--run-id",
        f"{manifest['run_id']}.execution",
        "--created-at",
        manifest["started_at"],
        "--suppress-execution-record-stdout",
    ]
    stage.command = command
    try:
        proc = run_command(command, cwd=REPO_ROOT, timeout=resolve_command_timeout_seconds(args))
        if proc.returncode != 0:
            fail_stage(stage, command_output_excerpt(proc) or "source adapter execution failed")
        receipt = load_execution_artifacts(output_dir)
        report, exit_code = validate_execution_artifact_receipt(receipt)
        stage.validation = {
            "status": "pass" if exit_code == EXIT_EXECUTION_PASS else "fail",
            "report": report,
        }
        if exit_code != EXIT_EXECUTION_PASS:
            fail_stage(stage, "execution artifacts failed validation")
        stage.evidence = {
            "artifact_schema_ids": {
                "execution_record": receipt.execution_record.get("schema_version"),
                "capture_events": execution_validator.CAPTURE_SCHEMA_VERSION,
                "extraction_records": execution_validator.EXTRACTION_SCHEMA_VERSION,
            }
        }
        stage.artifacts = {
            "execution_run_dir": str(output_dir),
            "execution_record": str(output_dir / "execution-record.json"),
            "capture_events": str(output_dir / "capture-events.jsonl"),
            "extraction_records": str(output_dir / "extraction-records.jsonl"),
        }
        finish_stage(stage)
        add_stage(manifest, stage)
        return output_dir, receipt
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
    execution_artifacts: ExecutionArtifactReceipt | None = None,
    run_dir: Path,
) -> dict[str, Any] | None:
    if execution_run_dir is None:
        return None
    stage = StageRecord(name="ingest_execution_artifacts", required=args.mode != "dry-run")
    stage.started_at = utc_now()
    stage.inputs = {"execution_run_dir": str(execution_run_dir)}
    try:
        loaded_paths: dict[str, Path] | None = None
        loaded_hashes: dict[str, str] | None = None
        if execution_artifacts is None:
            try:
                execution_record, paths, input_hashes = (
                    canonical_ingest.load_validated_execution_artifacts(execution_run_dir)
                )
            except Exception:
                if not args.degraded_spool:
                    raise
                execution_record, paths, input_hashes = (
                    canonical_ingest.load_validated_execution_artifacts(execution_run_dir)
                )
            loaded_paths = paths
            loaded_hashes = input_hashes
        else:
            execution_record = execution_artifacts.execution_record
            paths = execution_artifacts.paths
            input_hashes = execution_artifacts.input_hashes
            loaded_paths = paths
            loaded_hashes = input_hashes
        conn = canonical_store.connect_canonical_store(db_path)
        try:
            if args.mode == "dry-run":
                report = canonical_ingest.ingest_execution_artifacts(
                    conn,
                    execution_record,
                    paths=paths,
                    input_hashes=input_hashes,
                    capture_events=None,
                    extraction_records=None,
                    dry_run=True,
                    db_path=db_path,
                )
            else:
                with conn:
                    report = canonical_ingest.ingest_execution_artifacts(
                        conn,
                        execution_record,
                        paths=paths,
                        input_hashes=input_hashes,
                        capture_events=None,
                        extraction_records=None,
                        dry_run=False,
                        db_path=db_path,
                    )
        finally:
            conn.close()
        stage.evidence = {
            "artifact_schema_ids": {
                "ingest_report": report.get("schema_version"),
            }
        }
        if args.mode == "dry-run":
            report_path = run_dir / "execution-ingest" / "canonical-ingest-report.json"
            write_json(report_path, report)
            stage.artifacts = {
                "ingest_report": str(report_path),
                "ingest_report_sha256": hash_file(report_path),
                "mutated": False,
            }
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
        if isinstance(exc, canonical_ingest.CanonicalIngestError) and "validation failed" in str(
            exc
        ):
            fail_stage(stage, str(exc))
        if args.degraded_spool:
            if loaded_paths is None or loaded_hashes is None:
                fail_stage(stage, str(exc))
            artifact_refs = [
                {
                    "artifact_type": key,
                    "artifact_path": str(loaded_paths[key]),
                    "artifact_hash": loaded_hashes[key],
                }
                for key in sorted(loaded_paths)
            ]
            record = canonical_write_spool.build_spool_record(
                operation_kind="execution_artifact_ingest",
                operation_input={"artifact_refs": artifact_refs},
                replay_recipe={
                    "run_dir": str(execution_run_dir),
                    "input_hashes": dict(loaded_hashes),
                    "strict": True,
                },
                failure=exc,
                canonical_db_path=db_path,
                spool_dir=spool_dir_for(args, run_dir),
                originating_tool="tools/scripts/run_topic_cycle.py",
                originating_command="run_topic_cycle.py",
                originating_run_id=str(manifest["run_id"]),
                topic_cycle_id=str(manifest["cycle_event_id"]),
                stage_name="ingest_execution_artifacts",
                workspace_id=manifest["workspace"].get("workspace_id"),
                subject_id=manifest.get("subject", {}).get("subject_id")
                if isinstance(manifest.get("subject"), dict)
                else None,
                expected_schema_version=None,
            )
            spool_path = canonical_write_spool.write_spool_record(
                spool_dir_for(args, run_dir), record
            )
            stage.artifacts = {
                "spool_record": str(spool_path),
                "spool_record_sha256": hash_file(spool_path),
                "mutated": False,
            }
            stage.error_message = str(exc)
            finish_stage(stage, status="spooled")
            add_stage(manifest, stage)
            add_spool_record_to_manifest(manifest, spool_path=spool_path, record=record)
            manifest.setdefault("warnings", []).append(
                f"execution artifact ingest spooled after canonical write failure: {exc}"
            )
            return {
                "schema_version": canonical_ingest.INGEST_REPORT_SCHEMA_VERSION,
                "ingest_kind": "execution_artifacts",
                "status": "spooled",
                "spool_record_path": str(spool_path),
            }
        fail_stage(stage, str(exc))


def final_store_stage(*, args: argparse.Namespace, manifest: dict[str, Any], db_path: Path) -> None:
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
        if args.degraded_spool and manifest.get("spool_records"):
            stage.error_message = str(exc)
            manifest.setdefault("warnings", []).append(
                f"final canonical store summary skipped after degraded spool: {exc}"
            )
            finish_stage(stage, status="degraded")
            add_stage(manifest, stage)
            return
        fail_stage(stage, str(exc))


def graph_closure_report_path(args: argparse.Namespace, run_dir: Path) -> Path:
    if args.graph_closure_report:
        return resolve_path(args.graph_closure_report)
    return run_dir / "graph-closure-report.json"


def graph_closure_stage(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    db_path: Path,
    run_dir: Path,
) -> None:
    stage = StageRecord(name="graph_closure_audit", required=False)
    stage.started_at = utc_now()
    stage.inputs = {
        "db": str(db_path),
        "strict": bool(args.graph_closure_strict),
    }
    graph = manifest.setdefault("graph_closure", {})
    if not args.graph_closure:
        stage.skipped_reason = "disabled_by_operator_flag"
        stage.validation = {"status": "disabled", "strict": bool(args.graph_closure_strict)}
        graph.update(
            {
                "enabled": False,
                "strict": bool(args.graph_closure_strict),
                "status": "disabled",
                "disabled_reason": "disabled_by_operator_flag",
            }
        )
        finish_stage(stage, status="skipped")
        return

    report_path = graph_closure_report_path(args, run_dir)
    try:
        report = canonical_graph_closure.audit_canonical_graph_closure(
            db_path,
            generated_at=manifest["started_at"],
            strict=bool(args.graph_closure_strict),
            report_path=report_path,
        )
    except Exception as exc:
        if args.degraded_spool and manifest.get("spool_records"):
            stage.validation = {
                "status": "unavailable",
                "strict": bool(args.graph_closure_strict),
                "error": str(exc),
            }
            graph.update(
                {
                    "enabled": True,
                    "strict": bool(args.graph_closure_strict),
                    "status": "unavailable",
                    "disabled_reason": "canonical_store_unavailable_after_degraded_spool",
                }
            )
            manifest.setdefault("warnings", []).append(
                f"graph closure unavailable after degraded spool: {exc}"
            )
            finish_stage(stage, status="degraded")
            add_stage(manifest, stage)
            return
        fail_stage(stage, str(exc))

    summary = report.get("summary", {})
    report_sha256 = hash_file(report_path)
    stage.evidence = {
        "artifact_schema_ids": {
            "graph_closure_report": report.get("schema_version"),
        }
    }
    graph.update(
        {
            "enabled": True,
            "strict": bool(args.graph_closure_strict),
            "status": report.get("status"),
            "report_path": str(report_path),
            "report_sha256": report_sha256,
            "orphan_error_count": int(summary.get("true_orphan_error_count", 0)),
            "unresolved_tracked_count": int(summary.get("unresolved_tracked_count", 0)),
            "repairable_count": int(summary.get("repairable_count", 0)),
            "quarantined_count": int(summary.get("quarantined_count", 0)),
            "disabled_reason": None,
        }
    )
    stage.artifacts = {
        "graph_closure_report": str(report_path),
        "graph_closure_report_sha256": report_sha256,
    }
    stage.validation = {
        "status": report.get("status"),
        "strict": bool(args.graph_closure_strict),
        "summary": summary,
    }
    if report.get("status") == "fail":
        message = "graph closure found true orphan errors"
        if args.graph_closure_strict:
            stage.status = "failed"
            stage.error_message = message
            stage.ended_at = utc_now()
            add_stage(manifest, stage)
            raise TopicCycleError(f"{stage.name}: {message}")
        stage.error_message = message
        manifest.setdefault("warnings", []).append(message)
        finish_stage(stage, status="warning")
    elif report.get("status") in {"pass_with_unresolved", "warning"}:
        manifest.setdefault("warnings", []).append(
            f"graph closure completed with status {report.get('status')}"
        )
        finish_stage(stage, status="warning")
    else:
        finish_stage(stage)
    add_stage(manifest, stage)


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


def record_cycle_evidence_from_manifest(
    *,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    manifest_path: Path,
    db_path: Path,
) -> None:
    ledger = manifest.get("cycle_evidence_ledger")
    if not isinstance(ledger, dict):
        return
    if manifest.get("dry_run") is True:
        ledger["status"] = "skipped"
        return
    spool_dir = spool_dir_for(args, Path(str(manifest["run_dir"])))
    workspace_id = manifest["workspace"].get("workspace_id")
    subject_id = (
        manifest.get("subject", {}).get("subject_id")
        if isinstance(manifest.get("subject"), dict)
        else None
    )
    if not db_path.is_file():
        if args.degraded_spool:
            try:
                manifest_hash_value = hash_file(manifest_path)
                record = canonical_write_spool.build_spool_record(
                    operation_kind="cycle_evidence_write",
                    operation_input={
                        "artifact_refs": [
                            {
                                "artifact_type": "topic_cycle_manifest",
                                "artifact_path": str(manifest_path),
                                "artifact_hash": manifest_hash_value,
                            }
                        ]
                    },
                    replay_recipe={
                        "artifact_root": str(manifest_path.parent),
                        "manifest_path": manifest_path.name,
                        "manifest_hash": manifest_hash_value,
                    },
                    failure=f"canonical DB path is not a file: {db_path}",
                    canonical_db_path=db_path,
                    spool_dir=spool_dir,
                    originating_tool="tools/scripts/run_topic_cycle.py",
                    originating_command="run_topic_cycle.py",
                    originating_run_id=str(manifest["run_id"]),
                    topic_cycle_id=str(manifest["cycle_event_id"]),
                    stage_name="cycle_evidence_write",
                    workspace_id=workspace_id,
                    subject_id=subject_id,
                    expected_schema_version=None,
                )
                spool_path = canonical_write_spool.write_spool_record(
                    spool_dir, record
                )
                ledger["status"] = "spooled"
                ledger["spool_record_path"] = str(spool_path)
                add_spool_record_to_manifest(manifest, spool_path=spool_path, record=record)
                manifest.setdefault("warnings", []).append(
                    "cycle evidence ledger write was spooled: DB missing"
                )
                write_json(manifest_path, manifest)
                return
            except Exception as spool_exc:
                manifest.setdefault("warnings", []).append(
                    f"cycle evidence ledger spool failed: {spool_exc}"
                )
        ledger["status"] = "failed"
        ledger["error"] = f"canonical DB path is not a file: {db_path}"
        manifest.setdefault("warnings", []).append(
            "cycle evidence ledger was not recorded: DB missing"
        )
        write_json(manifest_path, manifest)
        return
    try:
        current_manifest_hash = hash_file(manifest_path)
        conn = canonical_store.connect_canonical_store(db_path)
        try:
            with conn:
                cycle_event_id = cycle_evidence_ledger.record_topic_cycle_manifest(
                    conn,
                    manifest=manifest,
                    manifest_path=manifest_path,
                    manifest_hash=current_manifest_hash,
                    canonical_db_ref=str(db_path),
                )
        finally:
            conn.close()
    except Exception as exc:
        if args.degraded_spool:
            try:
                record = canonical_write_spool.build_spool_record(
                    operation_kind="cycle_evidence_write",
                    operation_input={
                        "artifact_refs": [
                            {
                                "artifact_type": "topic_cycle_manifest",
                                "artifact_path": str(manifest_path),
                                "artifact_hash": current_manifest_hash,
                            }
                        ]
                    },
                    replay_recipe={
                        "artifact_root": str(manifest_path.parent),
                        "manifest_path": manifest_path.name,
                        "manifest_hash": current_manifest_hash,
                    },
                    failure=exc,
                    canonical_db_path=db_path,
                    spool_dir=spool_dir,
                    originating_tool="tools/scripts/run_topic_cycle.py",
                    originating_command="run_topic_cycle.py",
                    originating_run_id=str(manifest["run_id"]),
                    topic_cycle_id=str(manifest["cycle_event_id"]),
                    stage_name="cycle_evidence_write",
                    workspace_id=workspace_id,
                    subject_id=subject_id,
                    expected_schema_version=None,
                )
                spool_path = canonical_write_spool.write_spool_record(
                    spool_dir, record
                )
                ledger["status"] = "spooled"
                ledger["error"] = str(exc)
                ledger["spool_record_path"] = str(spool_path)
                add_spool_record_to_manifest(manifest, spool_path=spool_path, record=record)
                manifest.setdefault("warnings", []).append(
                    f"cycle evidence ledger write was spooled: {exc}"
                )
                write_json(manifest_path, manifest)
                return
            except Exception as spool_exc:
                manifest.setdefault("warnings", []).append(
                    f"cycle evidence ledger spool failed: {spool_exc}"
                )
        ledger["status"] = "failed"
        ledger["error"] = str(exc)
        manifest.setdefault("warnings", []).append(f"cycle evidence ledger was not recorded: {exc}")
        write_json(manifest_path, manifest)
        return
    ledger["status"] = "recorded"
    ledger["cycle_event_id"] = cycle_event_id
    # Keep the manifest as the durable cycle artifact. The ledger row references
    # the pre-recording manifest hash; the in-memory status is returned to CLI
    # callers without rewriting the artifact and changing that hash.


def run_topic_cycle(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if getattr(args, "dry_run", False):
        args.mode = "dry-run"
    started_at = normalize_timestamp(args.timestamp)
    command_timeout_seconds = resolve_command_timeout_seconds(args)
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
    manifest["budget"] = {"command_timeout_seconds": command_timeout_seconds}
    manifest["stage_plan"] = build_stage_plan(
        feedback_plan_mode=args.feedback_plan,
        build_next_feedback_plan=args.build_next_feedback_plan,
        include_source_adapter=bool(args.source_handoff),
        include_execution_ingest=bool(args.source_handoff or args.execution_run_fixture),
        include_graph_closure=bool(args.graph_closure),
    )
    manifest_path = run_dir / "topic-cycle-run.json"
    started = time.monotonic()
    try:
        runtime = resolve_runtime_stage(args=args, manifest=manifest, workspace=workspace)
        lock_context = nullcontext()
        if not getattr(args, "skip_workspace_lock", False):
            workspace_id = manifest["workspace"].get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                raise TopicCycleError(
                    "workspace_id must be resolved before acquiring the workspace lock",
                    stage_name="workspace_lock",
                )
            lock_context = acquire_workspace_lock(
                workspace_id,
                command=f"run_topic_cycle:{run_id}",
                lock_root=DEFAULT_LOCK_ROOT,
                wait=False,
            )
        try:
            with lock_context:
                resolve_domain_pack_stage(runtime=runtime, manifest=manifest)
                validate_store_stage(args=args, manifest=manifest, db_path=db_path)
                feedback_plan = resolve_feedback_plan(
                    args=args,
                    manifest=manifest,
                    workspace=workspace,
                    db_path=db_path,
                    run_dir=run_dir,
                    runtime=runtime,
                )
                batch_path, candidate_batch, candidate_batch_hash, validation_receipt = (
                    gather_stage(
                        args=args,
                        manifest=manifest,
                        workspace=workspace,
                        db_path=db_path,
                        run_dir=run_dir,
                        runtime=runtime,
                        feedback_plan=feedback_plan,
                    )
                )
                candidate_ingest_stage(
                    args=args,
                    manifest=manifest,
                    db_path=db_path,
                    batch_path=batch_path,
                    run_dir=run_dir,
                    candidate_batch=None if args.candidate_batch_fixture else candidate_batch,
                    candidate_batch_hash=None
                    if args.candidate_batch_fixture
                    else candidate_batch_hash,
                    validation_receipt=None if args.candidate_batch_fixture else validation_receipt,
                )
                acquisition_result = acquisition_stage(
                    args=args, manifest=manifest, run_dir=run_dir
                )
                if isinstance(acquisition_result, tuple) and len(acquisition_result) == 2:
                    execution_run_dir, execution_artifacts = acquisition_result
                else:
                    execution_run_dir = acquisition_result
                    execution_artifacts = None
                execution_ingest_stage(
                    args=args,
                    manifest=manifest,
                    db_path=db_path,
                    execution_run_dir=execution_run_dir,
                    execution_artifacts=execution_artifacts,
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
                final_store_stage(args=args, manifest=manifest, db_path=db_path)
                graph_closure_stage(
                    args=args,
                    manifest=manifest,
                    db_path=db_path,
                    run_dir=run_dir,
                )
                if args.mode == "dry-run":
                    manifest["status"] = "dry_run"
                elif manifest.get("spool_records"):
                    manifest["status"] = "degraded"
                else:
                    manifest["status"] = "completed"
                return_code = 0
        except WorkspaceLockError as exc:
            raise TopicCycleError(str(exc), stage_name="workspace_lock") from exc
    except TopicCycleError as exc:
        manifest["status"] = "failed"
        if exc.stage_name:
            manifest["failure_stage"] = exc.stage_name
        else:
            last_failed = next(
                (stage for stage in reversed(manifest["stages"]) if stage["status"] == "failed"),
                None,
            )
            manifest["failure_stage"] = last_failed["name"] if last_failed else "cycle_setup"
        manifest["error_summary"] = str(exc)
        return_code = 1
    except Exception as exc:
        manifest["status"] = "failed"
        last_failed = next(
            (stage for stage in reversed(manifest["stages"]) if stage["status"] == "failed"),
            None,
        )
        manifest["failure_stage"] = last_failed["name"] if last_failed else "cycle_setup"
        manifest["error_summary"] = str(exc)
        return_code = 1
    finally:
        manifest["ended_at"] = utc_now()
        manifest["budget_consumed"]["runtime_seconds"] = round(time.monotonic() - started, 6)
        if return_code == 0:
            if args.mode == "dry-run":
                manifest["status"] = "dry_run"
            elif manifest.get("spool_records"):
                manifest["status"] = "degraded"
            else:
                manifest["status"] = "completed"
        ledger = manifest.get("cycle_evidence_ledger")
        if isinstance(ledger, dict):
            ledger["status"] = "skipped" if args.mode == "dry-run" else "recorded"
        # Write the finalized manifest before recording evidence so the ledger hash
        # and manifest artifact metadata both reflect the durable on-disk state.
        write_json(manifest_path, manifest)
        record_cycle_evidence_from_manifest(
            args=args,
            manifest=manifest,
            manifest_path=manifest_path,
            db_path=db_path,
        )
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
