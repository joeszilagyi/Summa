#!/usr/bin/env python3
"""Build a public-safe sharing bundle from validated public artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.leak_scanner import scan_directory  # noqa: E402
from tools.validators.validate_static_knowledge_tree_output import (  # noqa: E402
    EXIT_PASS as EXIT_STATIC_OUTPUT_PASS,
    validate_static_knowledge_tree_output,
)


SCRIPT_PATH = "tools/scripts/build_public_sharing_bundle.py"
BUNDLE_SCHEMA_VERSION = "public-sharing-bundle.v1"
REPORT_SCHEMA_VERSION = "public-sharing-bundle-report.v1"
MANIFEST_FILENAME = "manifest.json"
BACKUP_DIR_SUFFIX = ".backup"
BACKUP_JOURNAL_SUFFIX = ".backup.journal"
JOURNAL_VERSION = "1"


class PublicSharingBundleError(RuntimeError):
    """Raised when the public sharing bundle cannot be safely emitted."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-manifest", type=Path, required=True, help="Path to the static-site build manifest JSON.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory path for the emitted bundle.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--generated-at", help="Optional RFC3339 timestamp override for deterministic tests.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicSharingBundleError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PublicSharingBundleError(f"{label} must contain a JSON object: {path}")
    return payload


def backup_root_path(output_dir: Path) -> Path:
    return output_dir.with_name(f".{output_dir.name}{BACKUP_DIR_SUFFIX}")


def backup_journal_path(output_dir: Path) -> Path:
    return output_dir.with_name(f".{output_dir.name}{BACKUP_JOURNAL_SUFFIX}")


def recover_stale_backup(output_dir: Path) -> None:
    backup_root = backup_root_path(output_dir)
    journal_path = backup_journal_path(output_dir)

    if backup_root.exists() and not output_dir.exists():
        backup_root.replace(output_dir)
        clear_backup_journal(journal_path)
        return

    if journal_path.exists() and not backup_root.exists():
        journal_path.unlink(missing_ok=True)


def clear_backup_journal(journal_path: Path) -> None:
    journal_path.unlink(missing_ok=True)


def resolve_existing_file(path: Path, *, label: str, base_dir: Path | None = None) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = ((base_dir or Path.cwd()) / resolved).resolve()
    else:
        resolved = resolved.resolve()
    if not resolved.exists():
        raise PublicSharingBundleError(f"{label} path does not exist: {resolved}")
    if not resolved.is_file():
        raise PublicSharingBundleError(f"{label} path is not a file: {resolved}")
    return resolved


def _is_recognized_public_sharing_bundle_root(path: Path) -> bool:
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = load_json(manifest_path, label="existing public sharing bundle manifest")
    except PublicSharingBundleError:
        return False
    return manifest.get("schema_version") == BUNDLE_SCHEMA_VERSION


def ensure_static_output_passes(build_manifest_path: Path) -> dict[str, Any]:
    report, exit_code = validate_static_knowledge_tree_output(build_manifest_path)
    if exit_code != EXIT_STATIC_OUTPUT_PASS:
        first_error = report["errors"][0]["message"] if report["errors"] else "validation failed"
        raise PublicSharingBundleError(f"static output validation failed: {first_error}")
    return load_json(build_manifest_path, label="build manifest")


def resolve_output_root(build_manifest: dict[str, Any], build_manifest_path: Path) -> Path:
    raw_output_root = build_manifest.get("output_root")
    if not isinstance(raw_output_root, str) or not raw_output_root.strip():
        raise PublicSharingBundleError("build manifest output_root is missing")
    output_root = resolve_path(build_manifest_path.parent / raw_output_root)
    if not output_root.exists() or not output_root.is_dir():
        raise PublicSharingBundleError(f"build manifest output_root is not a directory: {output_root}")
    return output_root


def build_export_summary(export_payload: dict[str, Any], build_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "public-sharing-export-summary.v1",
        "export_id": export_payload.get("export_id"),
        "display_name": export_payload.get("display_name"),
        "workspace_id": export_payload.get("workspace_id"),
        "export_profile": export_payload.get("export_profile"),
        "generated_at": export_payload.get("generated_at"),
        "landing_page_id": export_payload.get("landing_page_id"),
        "page_families": export_payload.get("page_families"),
        "page_count": build_manifest.get("page_count"),
        "asset_count": build_manifest.get("asset_count"),
        "input_sources": [
            {
                "source_id": source.get("source_id"),
                "source_kind": source.get("source_kind"),
                "logical_name": source.get("logical_name"),
                "rights_posture": source.get("rights_posture"),
                "required_for_freshness": source.get("required_for_freshness"),
            }
            for source in export_payload.get("input_sources", [])
            if isinstance(source, dict)
        ],
        "pages": [
            {
                "page_id": page.get("page_id"),
                "page_family": page.get("page_family"),
                "route": page.get("route"),
                "title": page.get("title"),
                "review_posture": page.get("review_posture"),
                "publication_state": page.get("publication_state"),
            }
            for page in export_payload.get("pages", [])
            if isinstance(page, dict)
        ],
    }


def build_presentation_summary(presentation_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "public-sharing-presentation-summary.v1",
        "contract_doc": presentation_payload.get("contract_doc"),
        "page_inventory": [
            {
                "page_family": page.get("page_family"),
                "route": page.get("route"),
                "reader_state": page.get("reader_state"),
                "review_state": page.get("review_state"),
                "validation_state": page.get("validation_state"),
                "publication_state": page.get("publication_state"),
            }
            for page in presentation_payload.get("page_inventory", [])
            if isinstance(page, dict)
        ],
        "never_publish": presentation_payload.get("never_publish", []),
    }


def included_site_entries(build_manifest: dict[str, Any]) -> list[str]:
    entries: list[str] = []
    for page in build_manifest.get("pages", []):
        if isinstance(page, dict) and isinstance(page.get("route"), str):
            entries.append(
                _normalize_bundle_relative_path(page["route"], field_name="page.route")
            )
    for asset in build_manifest.get("assets", []):
        if isinstance(asset, dict) and isinstance(asset.get("path"), str):
            entries.append(
                _normalize_bundle_relative_path(asset["path"], field_name="asset.path")
            )
    return entries


def _normalize_bundle_relative_path(raw_path: str, *, field_name: str) -> str:
    if "\\" in raw_path:
        raise PublicSharingBundleError(f"declared site artifact has invalid path separator: {field_name}")

    normalized = PurePosixPath(raw_path)
    normalized_path = normalized.as_posix()

    if normalized.is_absolute():
        raise PublicSharingBundleError(f"declared site artifact must be relative: {field_name}")
    if normalized == PurePosixPath("."):
        raise PublicSharingBundleError(f"declared site artifact path cannot be empty: {field_name}")
    if normalized_path != raw_path or ".." in normalized.parts:
        raise PublicSharingBundleError(f"declared site artifact path traversal detected: {field_name}")

    return normalized_path


def bundle_child_path(root: Path, relative_path: str, *, field_name: str) -> Path:
    normalized_path = _normalize_bundle_relative_path(relative_path, field_name=field_name)
    candidate = root.joinpath(*PurePosixPath(normalized_path).parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PublicSharingBundleError(f"declared site artifact escaped bundle root: {field_name}") from exc
    return candidate


def scan_bundle_for_leaks(bundle_root: Path) -> list[dict[str, str]]:
    report = scan_directory(bundle_root, profile="public_bundle")
    return report["findings"]


def bundle_manifest_payload(
    *,
    generated_at: str,
    build_manifest: dict[str, Any],
    included_artifacts: list[dict[str, str]],
    excluded_families: list[dict[str, str]],
    leak_findings: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "build_id": build_manifest.get("build_id"),
        "export_id": build_manifest.get("export_id"),
        "landing_page_id": build_manifest.get("landing_page_id"),
        "page_count": build_manifest.get("page_count"),
        "asset_count": build_manifest.get("asset_count"),
        "upload_attempted": False,
        "distribution_mode": "manual_local_handoff_only",
        "included_artifacts": included_artifacts,
        "excluded_families": excluded_families,
        "red_team_gate": {
            "status": "pass" if not leak_findings else "fail",
            "finding_count": len(leak_findings),
            "findings": leak_findings,
        },
    }


def build_bundle(
    build_manifest_path: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    resolved_manifest = resolve_existing_file(build_manifest_path, label="build manifest")
    build_manifest = ensure_static_output_passes(resolved_manifest)
    output_root = resolve_output_root(build_manifest, resolved_manifest)

    export_path = resolve_existing_file(
        Path(str(build_manifest["export_path"])),
        label="export",
        base_dir=resolved_manifest.parent,
    )
    presentation_path = resolve_existing_file(
        Path(str(build_manifest["presentation_path"])),
        label="presentation",
        base_dir=resolved_manifest.parent,
    )
    export_payload = load_json(export_path, label="export")
    presentation_payload = load_json(presentation_path, label="presentation")

    output_dir = resolve_path(output_dir)
    recover_stale_backup(output_dir)
    backup_root = backup_root_path(output_dir)
    journal_path = backup_journal_path(output_dir)
    if output_dir.exists() and not overwrite:
        raise PublicSharingBundleError(f"output directory already exists: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise PublicSharingBundleError(f"output path is not a directory: {output_dir}")
    if output_dir.exists() and not _is_recognized_public_sharing_bundle_root(output_dir):
        raise PublicSharingBundleError(
            f"output directory exists but is not a recognized public sharing bundle: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    emitted_at = generated_at or now_rfc3339()
    temp_root = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", suffix=".tmp", dir=output_dir.parent))
    try:
        site_root = temp_root / "site"
        included_artifacts: list[dict[str, str]] = []
        for relative_path in included_site_entries(build_manifest):
            source_path = bundle_child_path(output_root, relative_path, field_name="build manifest artifact")
            if not source_path.exists() or not source_path.is_file():
                raise PublicSharingBundleError(f"declared site artifact is missing from output_root: {relative_path}")
            target_path = bundle_child_path(site_root, relative_path, field_name="bundle site artifact")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            family = "page" if PurePosixPath(relative_path).suffix == ".html" else "asset"
            included_artifacts.append(
                {"family": family, "path": f"site/{relative_path}", "description": "Published static site artifact."}
            )

        export_summary = build_export_summary(export_payload, build_manifest)
        presentation_summary = build_presentation_summary(presentation_payload)
        atomic_write_json(temp_root / "metadata" / "export-summary.json", export_summary)
        atomic_write_json(temp_root / "metadata" / "presentation-summary.json", presentation_summary)
        included_artifacts.extend(
            [
                {
                    "family": "export_summary",
                    "path": "metadata/export-summary.json",
                    "description": "Sanitized public export metadata without local locator paths.",
                },
                {
                    "family": "presentation_summary",
                    "path": "metadata/presentation-summary.json",
                    "description": "Sanitized presentation inventory and never-publish policy summary.",
                },
            ]
        )

        excluded_families = [
            {"family": "raw_build_manifest", "reason": "Raw build manifest contains local artifact paths and is input-only."},
            {"family": "raw_payloads", "reason": "Raw captures and payload fields are excluded from public sharing."},
            {"family": "prompt_outputs", "reason": "Prompt outputs and prompt bundle internals are excluded from public sharing."},
            {"family": "runtime_logs", "reason": "Runtime logs and log tails are excluded from public sharing."},
            {"family": "private_paths", "reason": "Private absolute paths are excluded and leak-scanned."},
            {"family": "restricted_text", "reason": "Restricted text and excerpt-bearing fields are excluded from public sharing."},
            {"family": "private_operator_notes", "reason": "Private operator notes are excluded from public sharing."},
        ]

        leak_findings = scan_bundle_for_leaks(temp_root)
        bundle_manifest = bundle_manifest_payload(
            generated_at=emitted_at,
            build_manifest=build_manifest,
            included_artifacts=included_artifacts,
            excluded_families=excluded_families,
            leak_findings=leak_findings,
        )
        atomic_write_json(temp_root / "manifest.json", bundle_manifest)
        if leak_findings:
            summary = "; ".join(f"{item['code']} {item['path']}" for item in leak_findings[:5])
            raise PublicSharingBundleError(f"public sharing red-team gate failed: {summary}")

        if output_dir.exists():
            backup_root = backup_root_path(output_dir)
            output_dir.replace(backup_root)
            journal_path.write_text(
                json.dumps(
                    {
                        "version": JOURNAL_VERSION,
                        "mode": "public-sharing-bundle",
                        "output_dir": str(output_dir),
                        "state": "pending",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        temp_root.replace(output_dir)
    except Exception:
        if backup_root.exists() and not output_dir.exists():
            backup_root.replace(output_dir)
        clear_backup_journal(journal_path)
        raise
    else:
        if backup_root.exists():
            clear_backup_journal(journal_path)
            shutil.rmtree(backup_root, ignore_errors=True)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "pass",
        "output_dir": str(output_dir),
        "manifest_path": str(output_dir / "manifest.json"),
        "included_count": len(included_artifacts),
        "excluded_count": len(excluded_families),
        "upload_attempted": False,
        "leak_scan_status": "pass",
    }


def render_text(report: dict[str, Any]) -> str:
    return "\n".join(f"{key}={value}" for key, value in report.items()) + "\n"


def main() -> int:
    args = parse_args()
    try:
        report = build_bundle(
            args.build_manifest,
            args.output_dir,
            overwrite=args.overwrite,
            generated_at=args.generated_at,
        )
    except PublicSharingBundleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
        )
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
