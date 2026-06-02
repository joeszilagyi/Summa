#!/usr/bin/env python3
"""Build a redacted local support bundle from read-only diagnostics."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import local_doctor  # noqa: E402
from tools.common.leak_scanner import scan_directory  # noqa: E402


MANIFEST_SCHEMA_VERSION = "redacted-support-bundle.v1"
MAX_LOG_LINES = 200


class SupportBundleError(RuntimeError):
    """Raised when the support bundle cannot be safely emitted."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--doctor-report", type=Path, help="Optional existing local-doctor JSON report.")
    parser.add_argument("--registry", help="Optional registry path forwarded to local_doctor.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def redacted_text(value: str) -> str:
    return local_doctor.redact(value)


def load_or_build_doctor_report(repo_root: Path, doctor_report: Path | None, registry: str | None) -> dict[str, Any]:
    if doctor_report is None:
        return local_doctor.build_report(repo_root, registry=registry)
    try:
        payload = json.loads(doctor_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupportBundleError(f"could not read doctor report: {doctor_report}") from exc
    if not isinstance(payload, dict):
        raise SupportBundleError("doctor report must contain a JSON object")
    return local_doctor.redact(payload)


def schema_versions(repo_root: Path) -> dict[str, Any]:
    versions = []
    for path in sorted((repo_root / "config").glob("**/*.schema.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            versions.append({"path": path.relative_to(repo_root).as_posix(), "status": "unreadable"})
            continue
        versions.append(
            {
                "path": path.relative_to(repo_root).as_posix(),
                "title": payload.get("title"),
                "id": payload.get("$id"),
                "status": "readable",
            }
        )
    return {"schema_count": len(versions), "schemas": versions}


def config_summary(repo_root: Path) -> dict[str, Any]:
    paths = [
        "config/topic_workspace_registry.schema.json",
        "config/subject_manifest.schema.json",
        "config/crown_jewel_store_policy.schema.json",
    ]
    return {
        "included_config_refs": [
            {"path": path, "present": (repo_root / path).exists()}
            for path in paths
        ],
        "policy_refs": [
            {
                "path": "config/durability_policies/local_first_crown_jewels.v1.json",
                "present": (repo_root / "config/durability_policies/local_first_crown_jewels.v1.json").exists(),
            }
        ],
    }


def ledger_summary(repo_root: Path) -> dict[str, Any]:
    ledger_root = repo_root / "runtime" / "ledgers"
    if not ledger_root.exists():
        return {"ledger_root_present": False, "ledgers": []}
    ledgers = []
    for path in sorted(ledger_root.glob("*")):
        if path.name == ".gitkeep" or not path.is_file():
            continue
        ledgers.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "line_count": sum(1 for _ in path.open("r", encoding="utf-8", errors="replace")),
            }
        )
    return {"ledger_root_present": True, "ledgers": ledgers}


def redacted_recent_log(repo_root: Path) -> str | None:
    log_path = repo_root / "runtime" / "logs" / "index-actions.log"
    if not log_path.exists() or not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LOG_LINES:]
    except OSError:
        return None
    return "\n".join(redacted_text(line) for line in lines) + "\n"


def scan_bundle_for_leaks(bundle_root: Path) -> list[dict[str, str]]:
    report = scan_directory(bundle_root, profile="support_bundle")
    return report["findings"]


def manifest_payload(included: list[dict[str, Any]], excluded: list[dict[str, str]], leak_findings: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "read_only": True,
        "leak_scan": {
            "status": "pass" if not leak_findings else "fail",
            "finding_count": len(leak_findings),
            "findings": leak_findings,
        },
        "included_families": included,
        "excluded_families": excluded,
    }


def build_bundle(repo_root: Path, output_dir: Path, *, doctor_report: Path | None = None, registry: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and not overwrite:
        raise SupportBundleError(f"output directory already exists: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise SupportBundleError(f"output path is not a directory: {output_dir}")

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".tmp", dir=parent))
    try:
        included = [
            {"family": "doctor_report", "path": "doctor-report.json", "description": "Redacted local doctor JSON report."},
            {"family": "config_summary", "path": "config-summary.json", "description": "Presence summary for public configuration surfaces."},
            {"family": "schema_versions", "path": "schema-versions.json", "description": "Schema ids and titles, not payload data."},
            {"family": "ledger_summary", "path": "runtime-ledger-summary.json", "description": "Ledger names, sizes, and line counts only."},
        ]
        excluded = [
            {"family": "raw_payloads", "reason": "Raw captures and source payload bytes are excluded."},
            {"family": "full_extracted_text", "reason": "Full extracted text is excluded."},
            {"family": "private_paths", "reason": "Private absolute paths are redacted and leak-scanned."},
            {"family": "secrets", "reason": "Secret-looking values are redacted and leak-scanned."},
            {"family": "private_operator_notes", "reason": "Operator notes are excluded."},
            {"family": "backups_and_caches", "reason": "Backups and caches are excluded."},
        ]
        write_json(temp_root / "doctor-report.json", load_or_build_doctor_report(repo_root, doctor_report, registry))
        write_json(temp_root / "config-summary.json", config_summary(repo_root))
        write_json(temp_root / "schema-versions.json", schema_versions(repo_root))
        write_json(temp_root / "runtime-ledger-summary.json", ledger_summary(repo_root))
        log_text = redacted_recent_log(repo_root)
        if log_text is not None:
            log_path = temp_root / "redacted-logs" / "index-actions.tail.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(log_text, encoding="utf-8")
            included.append({"family": "redacted_log_tail", "path": "redacted-logs/index-actions.tail.txt", "description": "Redacted tail of runtime log."})

        leak_findings = scan_bundle_for_leaks(temp_root)
        manifest = manifest_payload(included, excluded, leak_findings)
        write_json(temp_root / "manifest.json", manifest)
        if leak_findings:
            raise SupportBundleError("support bundle leak scan failed: " + "; ".join(f"{item['code']} {item['path']}" for item in leak_findings[:5]))

        if output_dir.exists():
            shutil.rmtree(output_dir)
        temp_root.replace(output_dir)
        return {
            "schema_version": "redacted-support-bundle-report.v1",
            "status": "pass",
            "output_dir": str(output_dir),
            "manifest_path": str(output_dir / "manifest.json"),
            "included_count": len(included),
            "leak_scan_status": "pass",
        }
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def render_text(report: dict[str, Any]) -> str:
    return "\n".join(f"{key}={value}" for key, value in report.items()) + "\n"


def main() -> int:
    args = parse_args()
    try:
        report = build_bundle(
            args.repo_root,
            args.output_dir,
            doctor_report=args.doctor_report,
            registry=args.registry,
            overwrite=args.overwrite,
        )
    except SupportBundleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
