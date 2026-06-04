#!/usr/bin/env python3
"""Build a reproducible release-readiness bundle from upstream reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT, REPO_ROOT / "tools" / "validators"):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.validators import validate_release_readiness  # noqa: E402

SCRIPT_PATH = "tools/scripts/build_release_readiness_bundle.py"
MANIFEST_SCHEMA_VERSION = "release-readiness-bundle.v1"
MANIFEST_NAME = "release-readiness-bundle-manifest.json"
FINAL_REPORT_NAME = "release-readiness-report.json"
FINAL_TEXT_REPORT_NAME = "release-readiness-report.txt"

REQUIRED_REPORTS: dict[str, str] = {
    "doctor": validate_release_readiness.DOCTOR_REPORT_NAME,
    "knowledge_tree_export": validate_release_readiness.EXPORT_REPORT_NAME,
    "static_output": validate_release_readiness.STATIC_OUTPUT_REPORT_NAME,
    "local_search_projection": validate_release_readiness.SEARCH_PROJECTION_REPORT_NAME,
    "leak_scan": validate_release_readiness.LEAK_SCAN_REPORT_NAME,
}


class ReleaseReadinessBundleError(RuntimeError):
    """Raised when a release-readiness bundle cannot be built."""


@dataclass(frozen=True)
class StagedReport:
    key: str
    filename: str
    source_kind: str
    source_path: str | None
    staged_path: Path
    sha256: str
    status: str | None
    command: list[str] | None
    returncode: int | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble the five upstream reports expected by validate_release_readiness.py, "
            "stage them under exact filenames, and write a final release-readiness report."
        )
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory that will receive the readiness bundle."
    )
    parser.add_argument(
        "--mode",
        choices=("collect", "run", "mixed"),
        default="collect",
        help="collect copies prebuilt reports; run generates all reports from supplied inputs; mixed uses report paths when provided and generates the rest.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow replacing an existing non-empty output directory.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Exit 0 even when the final release-readiness status is block; the final report still records the block.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument(
        "--generated-at", help="Optional timestamp override for deterministic manifests."
    )
    parser.add_argument("--run-id", help="Optional run identifier for deterministic manifests.")

    parser.add_argument("--doctor-report", help="Prebuilt local-doctor report JSON.")
    parser.add_argument(
        "--knowledge-tree-export-report",
        help="Prebuilt knowledge-tree export validator report JSON.",
    )
    parser.add_argument(
        "--static-output-report", help="Prebuilt static output validator report JSON."
    )
    parser.add_argument(
        "--local-search-projection-report",
        help="Prebuilt local-search projection validator report JSON.",
    )
    parser.add_argument("--leak-scan-report", help="Prebuilt leak scan report JSON.")

    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT), help="Repo root for local doctor run mode."
    )
    parser.add_argument(
        "--registry", help="Optional topic workspace registry path for local doctor run mode."
    )
    parser.add_argument(
        "--canonical-db",
        help="Optional canonical SQLite path for local doctor and run-mode artifact builders.",
    )
    parser.add_argument(
        "--knowledge-tree-export",
        help="Knowledge-tree export JSON to validate in run or mixed mode.",
    )
    parser.add_argument(
        "--static-output-manifest",
        help="Static build manifest JSON to validate in run or mixed mode.",
    )
    parser.add_argument(
        "--local-search-projection",
        help="Local-search projection JSON to validate in run or mixed mode.",
    )
    parser.add_argument("--leak-scan-target", help="Directory to leak-scan in run or mixed mode.")
    parser.add_argument(
        "--leak-scan-allowlist", help="Optional leak-scan allowlist JSON for run or mixed mode."
    )
    return parser.parse_args(argv)


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def ensure_output_dir(path: Path, *, force: bool) -> None:
    if path.exists() and not path.is_dir():
        raise ReleaseReadinessBundleError(f"output path exists but is not a directory: {path}")
    if path.exists() and any(path.iterdir()) and not force:
        raise ReleaseReadinessBundleError(
            f"output directory already exists and is not empty: {path}"
        )
    path.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ReleaseReadinessBundleError(f"{label} is missing: {path}")
    if not path.is_file():
        raise ReleaseReadinessBundleError(f"{label} is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseReadinessBundleError(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseReadinessBundleError(f"{label} must be a JSON object: {path}")
    return payload


def report_status(payload: dict[str, Any], *, key: str) -> str | None:
    if key == "doctor":
        summary = payload.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("status"), str):
            return str(summary["status"])
    status = payload.get("status")
    return status if isinstance(status, str) else None


def copy_report(*, key: str, source: Path, output_dir: Path) -> StagedReport:
    filename = REQUIRED_REPORTS[key]
    payload = load_json_object(source, label=f"{key} report")
    destination = output_dir / filename
    shutil.copyfile(source, destination)
    return StagedReport(
        key=key,
        filename=filename,
        source_kind="collected",
        source_path=str(source),
        staged_path=destination,
        sha256=sha256_file(destination),
        status=report_status(payload, key=key),
        command=None,
        returncode=None,
    )


def run_command(command: list[str], *, report_path: Path, label: str) -> tuple[dict[str, Any], int]:
    proc = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if not report_path.exists():
        raise ReleaseReadinessBundleError(
            f"{label} did not produce expected report: {report_path}; "
            f"exit={proc.returncode}; stderr={proc.stderr.strip()}"
        )
    payload = load_json_object(report_path, label=label)
    if proc.returncode not in {0, 1}:
        raise ReleaseReadinessBundleError(
            f"{label} command failed before producing a usable validation report: "
            f"exit={proc.returncode}; stderr={proc.stderr.strip()}"
        )
    return payload, proc.returncode


def generated_report_command(key: str, args: argparse.Namespace, destination: Path) -> list[str]:
    if key == "doctor":
        command = [
            sys.executable,
            str(REPO_ROOT / "tools" / "scripts" / "local_doctor.py"),
            "--repo-root",
            str(resolve_path(args.repo_root)),
            "--output",
            str(destination),
            "--format",
            "json",
        ]
        if args.registry:
            command.extend(["--registry", str(resolve_path(args.registry))])
        if args.canonical_db:
            command.extend(["--canonical-db", str(resolve_path(args.canonical_db))])
        return command
    if key == "knowledge_tree_export":
        if not args.knowledge_tree_export:
            raise ReleaseReadinessBundleError(
                "--knowledge-tree-export is required to generate the export validator report"
            )
        return [
            sys.executable,
            str(REPO_ROOT / "tools" / "validators" / "validate_knowledge_tree_export.py"),
            str(resolve_path(args.knowledge_tree_export)),
            "--report-json",
            str(destination),
        ]
    if key == "static_output":
        if not args.static_output_manifest:
            raise ReleaseReadinessBundleError(
                "--static-output-manifest is required to generate the static output validator report"
            )
        return [
            sys.executable,
            str(REPO_ROOT / "tools" / "validators" / "validate_static_knowledge_tree_output.py"),
            str(resolve_path(args.static_output_manifest)),
            "--report-json",
            str(destination),
        ]
    if key == "local_search_projection":
        if not args.local_search_projection:
            raise ReleaseReadinessBundleError(
                "--local-search-projection is required to generate the local-search validator report"
            )
        return [
            sys.executable,
            str(REPO_ROOT / "tools" / "validators" / "validate_local_search_projection.py"),
            str(resolve_path(args.local_search_projection)),
            "--report-json",
            str(destination),
        ]
    if key == "leak_scan":
        if not args.leak_scan_target:
            raise ReleaseReadinessBundleError(
                "--leak-scan-target is required to generate the leak-scan report"
            )
        command = [
            sys.executable,
            str(REPO_ROOT / "tools" / "scripts" / "scan_for_leaks.py"),
            str(resolve_path(args.leak_scan_target)),
            "--profile",
            "public_bundle",
            "--report-json",
            str(destination),
        ]
        if args.leak_scan_allowlist:
            command.extend(["--allowlist-json", str(resolve_path(args.leak_scan_allowlist))])
        return command
    raise AssertionError(f"unknown report key: {key}")


def generate_report(*, key: str, args: argparse.Namespace, output_dir: Path) -> StagedReport:
    destination = output_dir / REQUIRED_REPORTS[key]
    command = generated_report_command(key, args, destination)
    payload, returncode = run_command(command, report_path=destination, label=f"{key} report")
    return StagedReport(
        key=key,
        filename=REQUIRED_REPORTS[key],
        source_kind="generated",
        source_path=None,
        staged_path=destination,
        sha256=sha256_file(destination),
        status=report_status(payload, key=key),
        command=command,
        returncode=returncode,
    )


def report_path_for_key(args: argparse.Namespace, key: str) -> str | None:
    return {
        "doctor": args.doctor_report,
        "knowledge_tree_export": args.knowledge_tree_export_report,
        "static_output": args.static_output_report,
        "local_search_projection": args.local_search_projection_report,
        "leak_scan": args.leak_scan_report,
    }[key]


def stage_reports(args: argparse.Namespace, *, output_dir: Path) -> list[StagedReport]:
    staged: list[StagedReport] = []
    for key in REQUIRED_REPORTS:
        raw_report_path = report_path_for_key(args, key)
        if args.mode == "collect":
            if not raw_report_path:
                raise ReleaseReadinessBundleError(
                    f"--{key.replace('_', '-')}-report is required in collect mode"
                )
            staged.append(
                copy_report(key=key, source=resolve_path(raw_report_path), output_dir=output_dir)
            )
        elif args.mode == "run":
            if raw_report_path:
                raise ReleaseReadinessBundleError(
                    f"prebuilt report path supplied for {key}, but mode is run"
                )
            staged.append(generate_report(key=key, args=args, output_dir=output_dir))
        else:
            if raw_report_path:
                staged.append(
                    copy_report(
                        key=key, source=resolve_path(raw_report_path), output_dir=output_dir
                    )
                )
            else:
                staged.append(generate_report(key=key, args=args, output_dir=output_dir))
    return staged


def render_release_text(report: dict[str, Any]) -> str:
    return validate_release_readiness.render_text(report)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def manifest_payload(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    staged_reports: list[StagedReport],
    final_report: dict[str, Any] | None,
    warnings: list[str],
    errors: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": args.generated_at,
        "run_id": args.run_id,
        "builder": SCRIPT_PATH,
        "mode": args.mode,
        "output_dir": str(output_dir),
        "required_report_filenames": dict(REQUIRED_REPORTS),
        "staged_reports": [
            {
                "key": item.key,
                "filename": item.filename,
                "source_kind": item.source_kind,
                "source_path": item.source_path,
                "staged_path": str(item.staged_path),
                "sha256": item.sha256,
                "status": item.status,
                "command": item.command,
                "returncode": item.returncode,
            }
            for item in staged_reports
        ],
        "final_release_readiness": None
        if final_report is None
        else {
            "status": final_report.get("status"),
            "report_path": str(output_dir / FINAL_REPORT_NAME),
            "report_sha256": sha256_file(output_dir / FINAL_REPORT_NAME)
            if (output_dir / FINAL_REPORT_NAME).exists()
            else None,
        },
        "warnings": warnings,
        "errors": errors,
    }


def build_release_readiness_bundle(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    output_dir = resolve_path(args.output_dir)
    ensure_output_dir(output_dir, force=args.force)
    staged_reports: list[StagedReport] = []
    warnings: list[str] = []
    errors: list[str] = []
    final_report: dict[str, Any] | None = None

    try:
        staged_reports = stage_reports(args, output_dir=output_dir)
        final_report = validate_release_readiness.aggregate_release_readiness(output_dir)
        final_report["scenario"] = args.run_id
        final_report["target"] = str(output_dir)
        final_report["output_artifacts"] = {
            "report_json": str(output_dir / FINAL_REPORT_NAME),
            "report_text": str(output_dir / FINAL_TEXT_REPORT_NAME),
        }
        write_json(output_dir / FINAL_REPORT_NAME, final_report)
        write_text(output_dir / FINAL_TEXT_REPORT_NAME, render_release_text(final_report))
    except Exception as exc:
        errors.append(str(exc))
        manifest = manifest_payload(
            args=args,
            output_dir=output_dir,
            staged_reports=staged_reports,
            final_report=final_report,
            warnings=warnings,
            errors=errors,
        )
        write_json(output_dir / MANIFEST_NAME, manifest)
        if isinstance(exc, ReleaseReadinessBundleError):
            raise
        raise ReleaseReadinessBundleError(str(exc)) from exc

    manifest = manifest_payload(
        args=args,
        output_dir=output_dir,
        staged_reports=staged_reports,
        final_report=final_report,
        warnings=warnings,
        errors=errors,
    )
    write_json(output_dir / MANIFEST_NAME, manifest)
    exit_code = 0 if args.report_only or final_report["status"] != "block" else 1
    return manifest, exit_code


def render_text(manifest: dict[str, Any]) -> str:
    final = manifest.get("final_release_readiness") or {}
    lines = [
        f"schema_version={manifest['schema_version']}",
        f"mode={manifest['mode']}",
        f"output_dir={manifest['output_dir']}",
        f"final_status={final.get('status')}",
        f"staged_report_count={len(manifest['staged_reports'])}",
    ]
    for item in manifest["staged_reports"]:
        lines.append(
            f"staged.{item['key']}={item['filename']} status={item.get('status')} sha256={item['sha256']}"
        )
    for error in manifest["errors"]:
        lines.append(f"error={error}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest, exit_code = build_release_readiness_bundle(args)
    except ReleaseReadinessBundleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(manifest))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
