#!/usr/bin/env python3
"""Emit a read-only subject detail view model from a subject manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tools" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import resolve_subject_runtime  # noqa: E402

SCHEMA_VERSION = "subject-detail.v1"


class SubjectDetailError(RuntimeError):
    """Raised when the subject detail view cannot be built."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only subject detail view model from a subject manifest "
            "and its referenced domain pack."
        )
    )
    parser.add_argument("--manifest", required=True, help="Path to the subject manifest JSON file.")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the generated subject detail view.",
    )
    return parser.parse_args()


def load_json_object(path: Path, *, label: str) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return "unreadable", {}
    except json.JSONDecodeError:
        return "invalid_json", {}
    if not isinstance(payload, dict):
        return "invalid_json", {}
    return "ok", payload


def resolve_manifest_path(raw_manifest: str) -> Path:
    manifest_path = Path(raw_manifest).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (Path.cwd() / manifest_path).resolve()
    if not manifest_path.exists():
        raise SubjectDetailError(f"subject manifest not found: {manifest_path}")
    if not manifest_path.is_file():
        raise SubjectDetailError(f"subject manifest is not a file: {manifest_path}")
    return manifest_path


def resolve_manifest_relative(raw_value: str, *, manifest_path: Path) -> Path | None:
    raw = Path(raw_value).expanduser()
    candidates: list[Path]
    if raw.is_absolute():
        candidates = [raw]
    else:
        candidates = [
            (manifest_path.parent / raw).resolve(),
            (REPO_ROOT / raw).resolve(),
        ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def domain_pack_detail(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    pack_id = manifest.get("domain_pack")
    if not isinstance(pack_id, str) or not pack_id.strip():
        return {"status": "not_declared", "pack_id": pack_id}, {}

    pack_path = REPO_ROOT / "config" / "domain_packs" / f"{pack_id}.json"
    detail: dict[str, Any] = {
        "pack_id": pack_id,
        "domain_pack_path": str(pack_path),
    }
    if not pack_path.exists():
        detail["status"] = "missing"
        return detail, {}
    if not pack_path.is_file():
        detail["status"] = "not_file"
        return detail, {}

    status, payload = load_json_object(pack_path, label="domain pack")
    detail["status"] = status
    if payload:
        detail["display_name"] = payload.get("display_name")
        detail["pack_status"] = payload.get("status")
        detail["enabled_facets"] = payload.get("enabled_facets")
        detail["query_families"] = payload.get("query_families")
    return detail, payload


def template_status_entries(template_files: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for template_file in template_files:
        entry = {"path": template_file}
        if not isinstance(template_file, str) or not template_file.strip():
            entry["status"] = "invalid"
        else:
            template_path = REPO_ROOT / template_file
            entry["status"] = "ok" if template_path.is_file() else "missing"
            entry["resolved_path"] = str(template_path.resolve())
        entries.append(entry)
    return entries


def prompt_bundle_for_facet(pack: dict[str, Any], facet: str) -> tuple[str, dict[str, Any] | None]:
    try:
        resolved = resolve_subject_runtime.resolve_prompt_bundles(pack, [facet])
    except resolve_subject_runtime.ResolutionError:
        return "", None
    bundle = resolved.get(facet)
    if not isinstance(bundle, dict):
        return "", None
    bundle_key = bundle.get("bundle_key")
    return (bundle_key if isinstance(bundle_key, str) else "", bundle)


def facet_entries(manifest: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_facets = manifest.get("enabled_facets")
    if not isinstance(manifest_facets, list):
        return []
    pack_facets = pack.get("enabled_facets")
    pack_facet_set = {item for item in pack_facets if isinstance(item, str)} if isinstance(pack_facets, list) else set()

    entries: list[dict[str, Any]] = []
    for facet in manifest_facets:
        entry: dict[str, Any] = {
            "facet": facet,
            "enabled_by_manifest": isinstance(facet, str),
            "enabled_by_domain_pack": facet in pack_facet_set,
        }
        if isinstance(facet, str):
            bundle_key, bundle = prompt_bundle_for_facet(pack, facet)
            if bundle is None:
                entry["prompt_bundle_status"] = "missing"
            else:
                template_files = bundle.get("template_files")
                if not isinstance(template_files, list):
                    template_files = []
                entry.update(
                    {
                        "prompt_bundle_status": "ok",
                        "prompt_bundle_key": bundle_key,
                        "prompt_bundle_id": bundle.get("bundle_id"),
                        "legacy_01a_output_stem": bundle.get("legacy_01a_output_stem"),
                        "phase_templates": bundle.get("phase_templates"),
                        "source_text_wrapper_template_id": bundle.get("source_text_wrapper_template_id"),
                        "template_files": template_files,
                        "template_file_statuses": template_status_entries(template_files),
                    }
                )
        entries.append(entry)
    return entries


def legacy_substrate_entries(manifest: dict[str, Any], *, manifest_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    raw_paths = manifest.get("legacy_substrate_paths")
    if not isinstance(raw_paths, list):
        return entries
    for raw_path in raw_paths:
        entry: dict[str, Any] = {"path": raw_path}
        if not isinstance(raw_path, str) or not raw_path.strip():
            entry["status"] = "invalid"
        else:
            resolved = resolve_manifest_relative(raw_path, manifest_path=manifest_path)
            if resolved is None:
                entry["status"] = "missing"
            elif not resolved.is_dir():
                entry["status"] = "not_directory"
                entry["resolved_path"] = str(resolved)
            else:
                entry["status"] = "ok"
                entry["resolved_path"] = str(resolved)
        entries.append(entry)
    return entries


def build_subject_detail_payload(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = resolve_manifest_path(args.manifest)
    manifest_status, manifest = load_json_object(manifest_path, label="subject manifest")
    if manifest_status != "ok":
        raise SubjectDetailError(f"could not load subject manifest: {manifest_status}")

    pack_detail, pack_payload = domain_pack_detail(manifest)
    facets = facet_entries(manifest, pack_payload)
    substrates = legacy_substrate_entries(manifest, manifest_path=manifest_path)

    return {
        "schema_version": SCHEMA_VERSION,
        "subject_manifest_path": str(manifest_path),
        "subject": {
            "subject_id": manifest.get("subject_id"),
            "display_name": manifest.get("display_name"),
            "domain_pack": manifest.get("domain_pack"),
            "scope_statement": manifest.get("scope_statement"),
            "languages": manifest.get("languages"),
            "aliases": manifest.get("aliases"),
            "disambiguation_terms": manifest.get("disambiguation_terms"),
            "excluded_senses": manifest.get("excluded_senses"),
            "query_families": manifest.get("query_families"),
            "public_export_default": manifest.get("public_export_default"),
        },
        "domain_pack": pack_detail,
        "facets": facets,
        "legacy_substrates": substrates,
        "status": {
            "domain_pack_status": pack_detail.get("status"),
            "facet_count": len(facets),
            "legacy_substrate_count": len(substrates),
            "legacy_substrate_ok_count": sum(1 for item in substrates if item["status"] == "ok"),
            "prompt_bundle_ok_count": sum(
                1 for item in facets if item.get("prompt_bundle_status") == "ok"
            ),
        },
    }


def render_text(payload: dict[str, Any]) -> str:
    subject = payload["subject"]
    status = payload["status"]
    lines = [
        f"schema_version={payload['schema_version']}",
        f"subject_id={subject.get('subject_id')}",
        f"display_name={subject.get('display_name')}",
        f"domain_pack={subject.get('domain_pack')}",
        f"domain_pack_status={status['domain_pack_status']}",
        f"facet_count={status['facet_count']}",
        f"legacy_substrate_ok_count={status['legacy_substrate_ok_count']}",
    ]
    for index, facet in enumerate(payload["facets"]):
        lines.append(f"facet[{index}].facet={facet.get('facet')}")
        lines.append(f"facet[{index}].prompt_bundle_status={facet.get('prompt_bundle_status')}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = build_subject_detail_payload(args)
    except SubjectDetailError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
