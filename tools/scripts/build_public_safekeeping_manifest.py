#!/usr/bin/env python3
"""Build a Git/archive safekeeping manifest for a public sharing bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
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
from tools.validators.validate_public_safekeeping_manifest import (  # noqa: E402
    EXIT_PASS as EXIT_SAFEKEEPING_PASS,
    validate_public_safekeeping_manifest,
)


SCRIPT_PATH = "tools/scripts/build_public_safekeeping_manifest.py"
SCHEMA_VERSION = "public-safekeeping-manifest.v1"
BUNDLE_SCHEMA_VERSION = "public-sharing-bundle.v1"
REPORT_SCHEMA_VERSION = "public-safekeeping-manifest-report.v1"
CHANNELS = ["git_handoff", "archive_export", "manual_copy"]
SITE_PAGE_SUFFIX = ".html"


class PublicSafekeepingManifestError(RuntimeError):
    """Raised when the public safekeeping manifest cannot be built."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True, help="Path to the public sharing bundle directory.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output path for the safekeeping manifest. Defaults to <bundle-dir>/safekeeping-manifest.json.",
    )
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


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicSafekeepingManifestError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PublicSafekeepingManifestError(f"{label} must contain a JSON object: {path}")
    return payload


def ensure_bundle_dir(bundle_dir: Path) -> tuple[Path, dict[str, Any]]:
    resolved = resolve_path(bundle_dir)
    if not resolved.exists() or not resolved.is_dir():
        raise PublicSafekeepingManifestError(f"bundle directory does not exist: {resolved}")
    manifest_path = resolved / "manifest.json"
    if not manifest_path.exists() or not manifest_path.is_file():
        raise PublicSafekeepingManifestError(f"bundle manifest does not exist: {manifest_path}")
    bundle_manifest = load_json(manifest_path, label="bundle manifest")
    if bundle_manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise PublicSafekeepingManifestError(f"bundle manifest schema_version must equal {BUNDLE_SCHEMA_VERSION}")
    if bundle_manifest.get("upload_attempted") is not False:
        raise PublicSafekeepingManifestError("bundle manifest upload_attempted must be false")
    red_team_gate = bundle_manifest.get("red_team_gate")
    if not isinstance(red_team_gate, dict) or red_team_gate.get("status") != "pass":
        raise PublicSafekeepingManifestError("bundle manifest red_team_gate must pass before safekeeping manifest generation")
    return resolved, bundle_manifest


def infer_artifact_family(relative_path: str) -> str:
    pure = PurePosixPath(relative_path)
    if relative_path == "manifest.json":
        return "bundle_manifest"
    if relative_path == "metadata/export-summary.json":
        return "export_summary"
    if relative_path == "metadata/presentation-summary.json":
        return "presentation_summary"
    if pure.suffix.lower() == SITE_PAGE_SUFFIX:
        return "site_page"
    return "site_asset"


def infer_rights_posture(artifact_family: str) -> str:
    if artifact_family in {"bundle_manifest", "export_summary", "presentation_summary"}:
        return "metadata_only"
    return "public_safe"


def preservation_channels_for(artifact_family: str) -> list[str]:
    if artifact_family in {"bundle_manifest", "export_summary", "presentation_summary"}:
        return ["git_handoff", "archive_export", "manual_copy"]
    return ["git_handoff", "archive_export"]


def manual_operator_steps() -> list[str]:
    return [
        "Review manifest.json and safekeeping-manifest.json locally before any external handoff.",
        "If Git preservation is desired, commit the bundle directory manually in a chosen repository after policy review.",
        "If archive preservation is desired, create an archive file manually and verify hashes against safekeeping-manifest.json.",
        "Do not create remotes, push commits, or upload archives automatically from this toolchain.",
    ]


def build_manifest_payload(bundle_dir: Path, bundle_manifest: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    included_artifacts = bundle_manifest.get("included_artifacts")
    if not isinstance(included_artifacts, list) or not included_artifacts:
        raise PublicSafekeepingManifestError("bundle manifest included_artifacts must be a non-empty array")

    artifact_paths = {item.get("path") for item in included_artifacts if isinstance(item, dict) and isinstance(item.get("path"), str)}
    artifact_paths.add("manifest.json")

    artifacts: list[dict[str, Any]] = []
    for relative_path in sorted(artifact_paths):
        artifact_path = bundle_dir / relative_path
        if not artifact_path.exists() or not artifact_path.is_file():
            raise PublicSafekeepingManifestError(f"bundle artifact is missing: {relative_path}")
        artifact_family = infer_artifact_family(relative_path)
        artifacts.append(
            {
                "path": relative_path,
                "artifact_family": artifact_family,
                "sha256": hash_file(artifact_path),
                "size_bytes": artifact_path.stat().st_size,
                "rights_posture": infer_rights_posture(artifact_family),
                "preservation_channels": preservation_channels_for(artifact_family),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "bundle_root": ".",
        "bundle_manifest_path": "manifest.json",
        "bundle_manifest_sha256": hash_file(bundle_dir / "manifest.json"),
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "upload_attempted": False,
        "preservation_targets": CHANNELS,
        "manual_operator_steps": manual_operator_steps(),
        "artifacts": artifacts,
        "excluded_families": bundle_manifest.get("excluded_families", []),
    }


def build_safekeeping_manifest(
    bundle_dir: Path,
    *,
    output_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    resolved_bundle_dir, bundle_manifest = ensure_bundle_dir(bundle_dir)
    output = resolve_path(output_path or (resolved_bundle_dir / "safekeeping-manifest.json"))
    emitted_at = generated_at or now_rfc3339()

    payload = build_manifest_payload(resolved_bundle_dir, bundle_manifest, generated_at=emitted_at)

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.validation.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_output = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
        report, exit_code = validate_public_safekeeping_manifest(temp_output)
        if exit_code != EXIT_SAFEKEEPING_PASS:
            first_error = report["errors"][0]["message"] if report["errors"] else "validation failed"
            raise PublicSafekeepingManifestError(f"generated safekeeping manifest failed validation: {first_error}")
    finally:
        if temp_output is not None:
            temp_output.unlink(missing_ok=True)

    atomic_write_json(output, payload)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "pass",
        "output_path": str(output),
        "bundle_dir": str(resolved_bundle_dir),
        "artifact_count": len(payload["artifacts"]),
        "upload_attempted": False,
    }


def render_text(report: dict[str, Any]) -> str:
    return "\n".join(f"{key}={value}" for key, value in report.items()) + "\n"


def main() -> int:
    args = parse_args()
    try:
        report = build_safekeeping_manifest(
            args.bundle_dir,
            output_path=args.output,
            generated_at=args.generated_at,
        )
    except PublicSafekeepingManifestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
