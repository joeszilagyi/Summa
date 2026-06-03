#!/usr/bin/env python3
"""Run a fast dry-run smoke over the operator-facing local tool path."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tools" / "scripts"
SOURCE_ADAPTER_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "source_adapter_runtime"
    / "local_file"
    / "source_adapter.json"
)
SMOKE_SCHEMA_VERSION = "operator-path-smoke.v1"
DEFAULT_RUN_ID = "operator-path-smoke"
DEFAULT_TOPIC_LABEL = "Smoke Topic"
DEFAULT_DOMAIN_PACK = "general.v1"
DEFAULT_FIXED_REVIEW_TIMESTAMP = "2026-06-03T00:00:00Z"


class OperatorPathSmokeError(RuntimeError):
    """Raised when smoke setup or execution cannot continue safely."""


@dataclass
class SmokeCheck:
    name: str
    status: str
    surface: str
    command: str | None = None
    artifact_path: str | None = None
    message: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "status": self.status,
            "surface": self.surface,
        }
        if self.command is not None:
            payload["command"] = self.command
        if self.artifact_path is not None:
            payload["artifact_path"] = self.artifact_path
        if self.message is not None:
            payload["message"] = self.message
        if self.error_message is not None:
            payload["error_message"] = self.error_message
        return payload


@dataclass
class SmokeContext:
    repo_root: Path
    workspace_path: Path
    dry_run: bool
    run_id: str
    timestamp: str
    registry_path: Path
    topic_workspace_root: Path
    subject_manifest_path: Path | None = None
    subject_id: str | None = None
    domain_pack: str = DEFAULT_DOMAIN_PACK
    doctor_report_path: Path | None = None
    dashboard_output_path: Path | None = None
    review_db_path: Path | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a fast operator-path smoke using only local dry-run or temporary "
            "workspace artifacts. The smoke never uses network access, never invokes "
            "an LLM, and never writes outside an explicit workspace path or a managed "
            "temporary directory."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to inspect. Defaults to the current Summa checkout.",
    )
    parser.add_argument(
        "--workspace",
        help=(
            "Empty or absent directory used for smoke artifacts. When omitted, the smoke "
            "uses a temporary directory and removes it unless --keep is supplied."
        ),
    )
    parser.add_argument("--output", help="Optional path that receives the final smoke report.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Mark the run as a dry-run operator smoke. The smoke may still create "
            "temporary artifacts under the selected workspace so downstream read-only "
            "views have real inputs to inspect."
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep an auto-created temporary workspace after the smoke finishes.",
    )
    parser.add_argument(
        "--run-id",
        default=DEFAULT_RUN_ID,
        help="Stable run identifier recorded in the report and used in temporary workspace naming.",
    )
    parser.add_argument(
        "--timestamp",
        help="Optional fixed UTC timestamp recorded in the smoke report for deterministic tests.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the smoke report.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Convenience alias for --format json.",
    )
    return parser.parse_args(argv)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: str | None) -> str:
    if value is None:
        return utc_now()
    text = value.strip()
    if not text:
        raise OperatorPathSmokeError("timestamp override must be a non-blank string")
    return text


def resolve_repo_root(raw_repo_root: str) -> Path:
    repo_root = Path(raw_repo_root).expanduser()
    if not repo_root.is_absolute():
        repo_root = (Path.cwd() / repo_root).resolve()
    if not repo_root.exists():
        raise OperatorPathSmokeError(f"repo root not found: {repo_root}")
    if not repo_root.is_dir():
        raise OperatorPathSmokeError(f"repo root is not a directory: {repo_root}")
    required = [
        repo_root / "tools" / "scripts" / "bootstrap_topic_workspace.py",
        repo_root / "tools" / "scripts" / "build_operator_dashboard.py",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise OperatorPathSmokeError(
            "required operator surfaces are missing: " + ", ".join(missing)
        )
    return repo_root


@contextlib.contextmanager
def managed_workspace(
    raw_workspace: str | None,
    *,
    run_id: str,
    keep: bool,
) -> Any:
    if raw_workspace is not None:
        workspace_path = Path(raw_workspace).expanduser()
        if not workspace_path.is_absolute():
            workspace_path = (Path.cwd() / workspace_path).resolve()
        if workspace_path.exists():
            if not workspace_path.is_dir():
                raise OperatorPathSmokeError(f"workspace path is not a directory: {workspace_path}")
            if any(workspace_path.iterdir()):
                raise OperatorPathSmokeError(
                    f"workspace path must be empty or absent for smoke isolation: {workspace_path}"
                )
        workspace_path.mkdir(parents=True, exist_ok=True)
        try:
            yield workspace_path
        finally:
            pass
        return

    prefix = f"summa-{run_id}-"
    if keep:
        workspace_path = Path(tempfile.mkdtemp(prefix=prefix))
        try:
            yield workspace_path
        finally:
            pass
        return

    with tempfile.TemporaryDirectory(prefix=prefix) as temp_dir:
        yield Path(temp_dir)


def load_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise OperatorPathSmokeError(f"could not load module spec from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_file(path: Path, *, label: str) -> None:
    if not path.exists():
        raise OperatorPathSmokeError(f"{label} not found: {path}")
    if not path.is_file():
        raise OperatorPathSmokeError(f"{label} is not a file: {path}")


def run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def checked_command(
    command: list[str], *, cwd: Path, label: str
) -> subprocess.CompletedProcess[str]:
    proc = run_command(command, cwd=cwd)
    if proc.returncode != 0:
        details = (
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"{label} exited with code {proc.returncode}"
        )
        raise OperatorPathSmokeError(f"{label} failed: {details}")
    return proc


def command_text(command: list[str]) -> str:
    return shlex.join(command)


def parse_json_stdout(proc: subprocess.CompletedProcess[str], *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OperatorPathSmokeError(f"{label} did not emit valid JSON") from exc
    if not isinstance(payload, dict):
        raise OperatorPathSmokeError(f"{label} did not emit a JSON object")
    return payload


def write_review_queue_fixture_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE work (
              work_id INTEGER PRIMARY KEY,
              work_type TEXT,
              title TEXT,
              review_state TEXT,
              confidence_score REAL,
              workspace_id TEXT,
              authority_level TEXT,
              public_blocker TEXT,
              record_last_updated TEXT
            );
            INSERT INTO work (
              work_id, work_type, title, review_state, confidence_score,
              workspace_id, authority_level, public_blocker, record_last_updated
            ) VALUES (
              1, 'book', 'Smoke Review Work', 'needs_review', 0.50,
              'smoke_topic', 'primary', '', '{DEFAULT_FIXED_REVIEW_TIMESTAMP}'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def smoke_help_surfaces(ctx: SmokeContext) -> tuple[str | None, str]:
    surfaces = [
        ctx.repo_root / "tools" / "scripts" / "bootstrap_topic_workspace.py",
        ctx.repo_root / "tools" / "scripts" / "build_workspace_overview_view.py",
        ctx.repo_root / "tools" / "scripts" / "build_subject_detail_view.py",
        ctx.repo_root / "tools" / "scripts" / "build_source_intake_status_view.py",
        ctx.repo_root / "tools" / "scripts" / "build_review_queue_view.py",
        ctx.repo_root / "tools" / "scripts" / "resolve_gather_domain_pack.py",
        ctx.repo_root / "tools" / "scripts" / "local_doctor.py",
        ctx.repo_root / "tools" / "scripts" / "build_operator_dashboard.py",
    ]
    for surface in surfaces:
        require_file(surface, label="operator surface")
        checked_command(
            [sys.executable, str(surface), "--help"],
            cwd=ctx.repo_root,
            label=f"{surface.name} --help",
        )
    return None, f"verified --help for {len(surfaces)} operator surfaces"


def smoke_bootstrap_dry_run(ctx: SmokeContext) -> tuple[str | None, str]:
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "bootstrap_topic_workspace.py"),
        "--registry",
        str(ctx.registry_path),
        "--workspace-root",
        str(ctx.topic_workspace_root),
        "--topic-label",
        DEFAULT_TOPIC_LABEL,
        "--domain-pack",
        ctx.domain_pack,
        "--non-interactive",
        "--dry-run",
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="bootstrap dry-run")
    payload = parse_json_stdout(proc, label="bootstrap dry-run")
    planned = payload.get("planned_created_paths")
    if not isinstance(planned, list) or not planned:
        raise OperatorPathSmokeError("bootstrap dry-run did not report planned_created_paths")
    if ctx.topic_workspace_root.exists():
        raise OperatorPathSmokeError(
            "bootstrap dry-run unexpectedly created the topic workspace root"
        )
    return None, f"planned {len(planned)} workspace scaffold paths without writing"


def smoke_bootstrap_apply(ctx: SmokeContext) -> tuple[str | None, str]:
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "bootstrap_topic_workspace.py"),
        "--registry",
        str(ctx.registry_path),
        "--workspace-root",
        str(ctx.topic_workspace_root),
        "--topic-label",
        DEFAULT_TOPIC_LABEL,
        "--domain-pack",
        ctx.domain_pack,
        "--non-interactive",
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="bootstrap apply")
    payload = parse_json_stdout(proc, label="bootstrap apply")
    manifest_path = Path(payload.get("subject_manifest_path", ""))
    if not manifest_path.is_file():
        raise OperatorPathSmokeError("bootstrap apply did not create a readable subject manifest")
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    subject_id = manifest_payload.get("subject_id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise OperatorPathSmokeError(
            "bootstrap apply did not create a subject manifest with subject_id"
        )
    ctx.subject_manifest_path = manifest_path
    ctx.subject_id = subject_id
    return str(manifest_path), f"bootstrapped workspace for {subject_id}"


def smoke_resolve_subject_runtime(ctx: SmokeContext) -> tuple[str | None, str]:
    if ctx.subject_id is None:
        raise OperatorPathSmokeError(
            "subject runtime resolution requires a bootstrapped subject_id"
        )
    module = load_module(
        ctx.repo_root / "tools" / "scripts" / "resolve_subject_runtime.py",
        "operator_path_smoke_runtime",
    )
    payload = module.resolve_subject_runtime(
        ctx.subject_id, workspace=str(ctx.topic_workspace_root)
    )
    if payload.get("schema_version") != "subject-runtime-resolution.v1":
        raise OperatorPathSmokeError(
            "subject runtime resolver returned an unexpected schema_version"
        )
    if payload.get("subject", {}).get("subject_id") != ctx.subject_id:
        raise OperatorPathSmokeError("subject runtime resolver returned the wrong subject_id")
    return str(ctx.subject_manifest_path), f"resolved runtime for {ctx.subject_id}"


def smoke_resolve_domain_pack(ctx: SmokeContext) -> tuple[str | None, str]:
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "resolve_gather_domain_pack.py"),
        "--domain-pack",
        ctx.domain_pack,
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="domain pack resolution")
    payload = parse_json_stdout(proc, label="domain pack resolution")
    if payload.get("schema_version") != "gather-domain-pack-resolution.v1":
        raise OperatorPathSmokeError("domain pack resolver returned an unexpected schema_version")
    selected = payload.get("selected_facets")
    if not isinstance(selected, list) or not selected:
        raise OperatorPathSmokeError("domain pack resolver returned no selected facets")
    return None, f"resolved {len(selected)} gather facets from {ctx.domain_pack}"


def smoke_workspace_overview(ctx: SmokeContext) -> tuple[str | None, str]:
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "build_workspace_overview_view.py"),
        "--registry",
        str(ctx.registry_path),
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="workspace overview")
    payload = parse_json_stdout(proc, label="workspace overview")
    if payload.get("schema_version") != "workspace-overview.v1":
        raise OperatorPathSmokeError("workspace overview returned an unexpected schema_version")
    counts = payload.get("counts", {})
    if counts.get("total_workspaces") != 1:
        raise OperatorPathSmokeError(
            "workspace overview did not report exactly one smoke workspace"
        )
    if counts.get("workspace_root_ok") != 1:
        raise OperatorPathSmokeError("workspace overview did not resolve the smoke workspace root")
    return str(ctx.registry_path), "built workspace overview for the bootstrapped smoke workspace"


def smoke_subject_detail(ctx: SmokeContext) -> tuple[str | None, str]:
    if ctx.subject_manifest_path is None:
        raise OperatorPathSmokeError("subject detail build requires a bootstrapped manifest path")
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "build_subject_detail_view.py"),
        "--manifest",
        str(ctx.subject_manifest_path),
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="subject detail build")
    payload = parse_json_stdout(proc, label="subject detail build")
    if payload.get("schema_version") != "subject-detail.v1":
        raise OperatorPathSmokeError("subject detail build returned an unexpected schema_version")
    if payload.get("status", {}).get("domain_pack_status") != "ok":
        raise OperatorPathSmokeError("subject detail build did not resolve the domain pack cleanly")
    return str(ctx.subject_manifest_path), "built subject detail view from the smoke manifest"


def smoke_source_intake(ctx: SmokeContext) -> tuple[str | None, str]:
    require_file(SOURCE_ADAPTER_FIXTURE, label="source adapter fixture")
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "build_source_intake_status_view.py"),
        "--adapter",
        str(SOURCE_ADAPTER_FIXTURE),
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="source intake status build")
    payload = parse_json_stdout(proc, label="source intake status build")
    if payload.get("schema_version") != "source-intake-status.v1":
        raise OperatorPathSmokeError(
            "source intake status build returned an unexpected schema_version"
        )
    counts = payload.get("counts", {})
    if counts.get("total_adapters") != 1 or counts.get("contract_fail") != 0:
        raise OperatorPathSmokeError(
            "source intake status build did not accept the runtime local-file fixture"
        )
    return str(
        SOURCE_ADAPTER_FIXTURE
    ), "built source intake status from the checked-in runtime fixture"


def smoke_review_queue(ctx: SmokeContext) -> tuple[str | None, str]:
    review_db_path = ctx.workspace_path / "review.sqlite"
    write_review_queue_fixture_db(review_db_path)
    ctx.review_db_path = review_db_path
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "build_review_queue_view.py"),
        "--db",
        str(review_db_path),
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="review queue build")
    payload = parse_json_stdout(proc, label="review queue build")
    if payload.get("schema_version") != "review-queue.v1":
        raise OperatorPathSmokeError("review queue build returned an unexpected schema_version")
    if payload.get("counts", {}).get("total_items") != 1:
        raise OperatorPathSmokeError("review queue build did not report the smoke review item")
    return str(review_db_path), "built review queue view from a temp read-only smoke database"


def smoke_local_doctor(ctx: SmokeContext) -> tuple[str | None, str]:
    doctor_report_path = ctx.workspace_path / "doctor-report.json"
    ctx.doctor_report_path = doctor_report_path
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "local_doctor.py"),
        "--repo-root",
        str(ctx.repo_root),
        "--registry",
        str(ctx.registry_path),
        "--output",
        str(doctor_report_path),
        "--format",
        "json",
    ]
    checked_command(command, cwd=ctx.repo_root, label="local doctor")
    payload = json.loads(doctor_report_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "local-doctor-report.v1":
        raise OperatorPathSmokeError("local doctor returned an unexpected schema_version")
    if payload.get("read_only") is not True or payload.get("auto_fix_performed") is not False:
        raise OperatorPathSmokeError("local doctor stopped being read-only")
    summary_status = payload.get("summary", {}).get("status")
    if summary_status == "fail":
        raise OperatorPathSmokeError("local doctor reported fail status for the operator smoke")
    if summary_status not in {"pass", "warn"}:
        raise OperatorPathSmokeError(
            f"local doctor returned unexpected summary status: {summary_status!r}"
        )
    return str(doctor_report_path), f"local doctor completed with summary status {summary_status}"


def smoke_operator_dashboard(ctx: SmokeContext) -> tuple[str | None, str]:
    if ctx.doctor_report_path is None:
        raise OperatorPathSmokeError("operator dashboard build requires a doctor report")
    dashboard_output_path = ctx.workspace_path / "operator-dashboard.html"
    ctx.dashboard_output_path = dashboard_output_path
    command = [
        sys.executable,
        str(ctx.repo_root / "tools" / "scripts" / "build_operator_dashboard.py"),
        "--doctor-report",
        str(ctx.doctor_report_path),
        "--output",
        str(dashboard_output_path),
        "--format",
        "json",
    ]
    proc = checked_command(command, cwd=ctx.repo_root, label="operator dashboard build")
    payload = parse_json_stdout(proc, label="operator dashboard build")
    if payload.get("schema_version") != "operator-dashboard-build-report.v1":
        raise OperatorPathSmokeError(
            "operator dashboard build returned an unexpected schema_version"
        )
    body = dashboard_output_path.read_text(encoding="utf-8")
    if "Summa Operator Health" not in body:
        raise OperatorPathSmokeError(
            "operator dashboard HTML is missing the expected operator title"
        )
    return str(
        dashboard_output_path
    ), "rendered operator dashboard HTML from the smoke doctor report"


def execute_check(
    checks: list[SmokeCheck],
    *,
    name: str,
    surface: str,
    command: str | None,
    action: Callable[[], tuple[str | None, str]],
) -> bool:
    try:
        artifact_path, message = action()
    except OperatorPathSmokeError as exc:
        checks.append(
            SmokeCheck(
                name=name,
                status="failed",
                surface=surface,
                command=command,
                error_message=str(exc),
            )
        )
        return False

    checks.append(
        SmokeCheck(
            name=name,
            status="passed",
            surface=surface,
            command=command,
            artifact_path=artifact_path,
            message=message,
        )
    )
    return True


def build_report(ctx: SmokeContext, checks: list[SmokeCheck]) -> dict[str, Any]:
    passed = sum(1 for check in checks if check.status == "passed")
    failed = sum(1 for check in checks if check.status == "failed")
    skipped = sum(1 for check in checks if check.status == "skipped")
    status = "failed" if failed else ("passed" if passed else "skipped")
    return {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "status": status,
        "repo_root": str(ctx.repo_root),
        "workspace_path": str(ctx.workspace_path),
        "dry_run": ctx.dry_run,
        "run_id": ctx.run_id,
        "timestamp": ctx.timestamp,
        "network_access_attempted": False,
        "llm_invoked": False,
        "checks": [check.to_dict() for check in checks],
        "summary": {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"schema_version={report['schema_version']}",
        f"status={report['status']}",
        f"repo_root={report['repo_root']}",
        f"workspace_path={report['workspace_path']}",
        f"dry_run={str(report['dry_run']).lower()}",
        f"run_id={report['run_id']}",
        f"timestamp={report['timestamp']}",
        f"network_access_attempted={str(report['network_access_attempted']).lower()}",
        f"llm_invoked={str(report['llm_invoked']).lower()}",
    ]
    for index, check in enumerate(report["checks"]):
        lines.append(f"check[{index}].name={check['name']}")
        lines.append(f"check[{index}].status={check['status']}")
        lines.append(f"check[{index}].surface={check['surface']}")
        if "artifact_path" in check:
            lines.append(f"check[{index}].artifact_path={check['artifact_path']}")
        if "message" in check:
            lines.append(f"check[{index}].message={check['message']}")
        if "error_message" in check:
            lines.append(f"check[{index}].error_message={check['error_message']}")
    lines.append(f"summary.passed={report['summary']['passed']}")
    lines.append(f"summary.failed={report['summary']['failed']}")
    lines.append(f"summary.skipped={report['summary']['skipped']}")
    return "\n".join(lines) + "\n"


def write_body(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def run_smoke(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo_root = resolve_repo_root(args.repo_root)
    timestamp = normalize_timestamp(args.timestamp)
    with managed_workspace(args.workspace, run_id=args.run_id, keep=args.keep) as workspace_path:
        ctx = SmokeContext(
            repo_root=repo_root,
            workspace_path=workspace_path,
            dry_run=bool(args.dry_run),
            run_id=args.run_id,
            timestamp=timestamp,
            registry_path=workspace_path / "topic_workspaces.local.json",
            topic_workspace_root=workspace_path / "topic-workspace",
        )
        checks: list[SmokeCheck] = []
        check_specs = [
            (
                "operator_script_help",
                "operator.surface_help",
                "python3 <surface> --help",
                lambda: smoke_help_surfaces(ctx),
            ),
            (
                "bootstrap_workspace_dry_run",
                "tools/scripts/bootstrap_topic_workspace.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "bootstrap_topic_workspace.py"),
                        "--dry-run",
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_bootstrap_dry_run(ctx),
            ),
            (
                "bootstrap_workspace_apply",
                "tools/scripts/bootstrap_topic_workspace.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "bootstrap_topic_workspace.py"),
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_bootstrap_apply(ctx),
            ),
            (
                "resolve_subject_runtime",
                "tools/scripts/resolve_subject_runtime.py",
                "resolve_subject_runtime.resolve_subject_runtime(subject_id, workspace=...)",
                lambda: smoke_resolve_subject_runtime(ctx),
            ),
            (
                "resolve_domain_pack",
                "tools/scripts/resolve_gather_domain_pack.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "resolve_gather_domain_pack.py"),
                        "--domain-pack",
                        ctx.domain_pack,
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_resolve_domain_pack(ctx),
            ),
            (
                "build_workspace_overview",
                "tools/scripts/build_workspace_overview_view.py",
                command_text(
                    [
                        sys.executable,
                        str(
                            ctx.repo_root / "tools" / "scripts" / "build_workspace_overview_view.py"
                        ),
                        "--registry",
                        str(ctx.registry_path),
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_workspace_overview(ctx),
            ),
            (
                "build_subject_detail",
                "tools/scripts/build_subject_detail_view.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "build_subject_detail_view.py"),
                        "--manifest",
                        "<bootstrapped-manifest>",
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_subject_detail(ctx),
            ),
            (
                "build_source_intake_status",
                "tools/scripts/build_source_intake_status_view.py",
                command_text(
                    [
                        sys.executable,
                        str(
                            ctx.repo_root
                            / "tools"
                            / "scripts"
                            / "build_source_intake_status_view.py"
                        ),
                        "--adapter",
                        str(SOURCE_ADAPTER_FIXTURE),
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_source_intake(ctx),
            ),
            (
                "build_review_queue_view",
                "tools/scripts/build_review_queue_view.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "build_review_queue_view.py"),
                        "--db",
                        "<smoke-review-db>",
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_review_queue(ctx),
            ),
            (
                "run_local_doctor",
                "tools/scripts/local_doctor.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "local_doctor.py"),
                        "--repo-root",
                        str(ctx.repo_root),
                        "--registry",
                        str(ctx.registry_path),
                        "--output",
                        "<doctor-report>",
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_local_doctor(ctx),
            ),
            (
                "build_operator_dashboard",
                "tools/scripts/build_operator_dashboard.py",
                command_text(
                    [
                        sys.executable,
                        str(ctx.repo_root / "tools" / "scripts" / "build_operator_dashboard.py"),
                        "--doctor-report",
                        "<doctor-report>",
                        "--output",
                        "<dashboard-output>",
                        "--format",
                        "json",
                    ]
                ),
                lambda: smoke_operator_dashboard(ctx),
            ),
        ]

        for name, surface, command, action in check_specs:
            okay = execute_check(checks, name=name, surface=surface, command=command, action=action)
            if not okay:
                break

        report = build_report(ctx, checks)
        exit_code = 0 if report["status"] == "passed" else 1
        return report, exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.json:
        args.format = "json"

    try:
        report, exit_code = run_smoke(args)
    except OperatorPathSmokeError as exc:
        workspace_path = (
            str(Path(args.workspace).expanduser().resolve())
            if args.workspace
            else "<temporary-workspace>"
        )
        report = {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "status": "failed",
            "repo_root": str(Path(args.repo_root).expanduser()),
            "workspace_path": workspace_path,
            "dry_run": bool(args.dry_run),
            "run_id": args.run_id,
            "timestamp": normalize_timestamp(args.timestamp),
            "network_access_attempted": False,
            "llm_invoked": False,
            "checks": [
                SmokeCheck(
                    name="smoke_setup",
                    status="failed",
                    surface="operator_path_smoke",
                    error_message=str(exc),
                ).to_dict()
            ],
            "summary": {
                "passed": 0,
                "failed": 1,
                "skipped": 0,
            },
        }
        exit_code = 1

    body = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_text(report)
    )
    if args.output:
        write_body(Path(args.output).expanduser(), body)
    sys.stdout.write(body)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
