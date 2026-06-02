#!/usr/bin/env python3
"""List current product/API view-model contracts and adapter surfaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_DIR = REPO_ROOT / "config" / "view_models"
DEFAULT_FIXTURE_DIR = REPO_ROOT / ".local" / "view-model-fixtures"
CATALOG_SCHEMA_VERSION = "view-model-catalog-report.v1"

VIEW_MODELS: tuple[dict[str, Any], ...] = (
    {
        "schema_version": "workspace-overview.v1",
        "name": "Workspace overview",
        "purpose": "Workspace list, root/manifest status, scheduling posture, and validation blockers.",
        "emitter_path": "tools/scripts/Index_Workspace_Overview.sh",
        "example_command": "bash tools/scripts/Index_Workspace_Overview.sh --format json",
        "required_inputs": ["topic workspace registry"],
    },
    {
        "schema_version": "subject-detail.v1",
        "name": "Subject detail",
        "purpose": "Subject identity, scope, domain pack, facets, prompt bundles, and template status.",
        "emitter_path": "tools/scripts/build_subject_detail_view.py",
        "example_command": "python3 tools/scripts/build_subject_detail_view.py --manifest <subject-manifest> --format json",
        "required_inputs": ["subject manifest", "domain pack"],
    },
    {
        "schema_version": "review-queue.v1",
        "name": "Review queue",
        "purpose": "Filtered SQLite review work, counts by state/type, and truncated queue items.",
        "emitter_path": "tools/scripts/build_review_queue_view.py",
        "example_command": "python3 tools/scripts/build_review_queue_view.py --db <source.sqlite> --format json",
        "required_inputs": ["source.sqlite"],
    },
    {
        "schema_version": "source-intake-status.v1",
        "name": "Source intake status",
        "purpose": "Source-adapter manifests, contract status, locator reachability, review posture, and public-use blockers.",
        "emitter_path": "tools/scripts/build_source_intake_status_view.py",
        "example_command": "python3 tools/scripts/build_source_intake_status_view.py --adapter <source-adapter-json> --format json",
        "required_inputs": ["one or more source-adapter manifests or roots"],
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List current product/API view-model contracts, emitters, fixtures, and schema paths."
    )
    parser.add_argument(
        "--schema-version",
        action="append",
        default=[],
        help="Only include this schema_version. Repeat to include multiple schemas.",
    )
    parser.add_argument(
        "--schema-dir",
        default=str(DEFAULT_SCHEMA_DIR),
        help="Directory containing <schema_version>.schema.json files.",
    )
    parser.add_argument(
        "--fixture-dir",
        default=str(DEFAULT_FIXTURE_DIR),
        help="Directory containing generated <schema_version>.json fixture files.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the catalog report.",
    )
    return parser.parse_args()


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def repo_relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def path_status(path: Path, *, expected_kind: str) -> str:
    if not path.exists():
        return "missing"
    if expected_kind == "file" and not path.is_file():
        return "not_file"
    if expected_kind == "directory" and not path.is_dir():
        return "not_directory"
    return "ok"


def selected_view_models(schema_versions: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not schema_versions:
        return [dict(item) for item in VIEW_MODELS], []

    by_schema = {item["schema_version"]: item for item in VIEW_MODELS}
    selected: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for schema_version in schema_versions:
        if schema_version in seen:
            continue
        seen.add(schema_version)
        view_model = by_schema.get(schema_version)
        if view_model is None:
            errors.append(
                {
                    "code": "UNKNOWN_SCHEMA_VERSION",
                    "message": f"unknown view-model schema_version: {schema_version}",
                    "schema_version": schema_version,
                }
            )
            continue
        selected.append(dict(view_model))
    return selected, errors


def catalog_entry(view_model: dict[str, Any], *, schema_dir: Path, fixture_dir: Path) -> dict[str, Any]:
    schema_version = view_model["schema_version"]
    schema_path = schema_dir / f"{schema_version}.schema.json"
    fixture_path = fixture_dir / f"{schema_version}.json"
    emitter_path = REPO_ROOT / view_model["emitter_path"]
    return {
        "schema_version": schema_version,
        "name": view_model["name"],
        "purpose": view_model["purpose"],
        "schema": {
            "path": repo_relative(schema_path),
            "status": path_status(schema_path, expected_kind="file"),
        },
        "fixture": {
            "path": repo_relative(fixture_path),
            "status": path_status(fixture_path, expected_kind="file"),
        },
        "emitter": {
            "path": view_model["emitter_path"],
            "status": path_status(emitter_path, expected_kind="file"),
            "example_command": view_model["example_command"],
            "required_inputs": list(view_model["required_inputs"]),
        },
        "validator": {
            "path": "tools/scripts/validate_view_model_json.py",
            "example_command": f"python3 tools/scripts/validate_view_model_json.py <{schema_version}.json>",
        },
    }


def build_catalog(args: argparse.Namespace) -> dict[str, Any]:
    schema_dir = resolve_path(args.schema_dir)
    fixture_dir = resolve_path(args.fixture_dir)
    view_models, errors = selected_view_models(args.schema_version)
    entries = [catalog_entry(item, schema_dir=schema_dir, fixture_dir=fixture_dir) for item in view_models]

    for entry in entries:
        if entry["schema"]["status"] != "ok":
            errors.append(
                {
                    "code": "SCHEMA_UNAVAILABLE",
                    "message": f"schema path is {entry['schema']['status']}",
                    "schema_version": entry["schema_version"],
                    "path": entry["schema"]["path"],
                }
            )
        if entry["emitter"]["status"] != "ok":
            errors.append(
                {
                    "code": "EMITTER_UNAVAILABLE",
                    "message": f"emitter path is {entry['emitter']['status']}",
                    "schema_version": entry["schema_version"],
                    "path": entry["emitter"]["path"],
                }
            )

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "ok": not errors,
        "schema_dir": repo_relative(schema_dir),
        "fixture_dir": repo_relative(fixture_dir),
        "fixture_bundle_command": None,
        "counts": {
            "view_models": len(entries),
            "schemas_available": sum(1 for entry in entries if entry["schema"]["status"] == "ok"),
            "emitters_available": sum(1 for entry in entries if entry["emitter"]["status"] == "ok"),
            "fixtures_available": sum(1 for entry in entries if entry["fixture"]["status"] == "ok"),
        },
        "view_models": entries,
        "errors": errors,
    }


def render_text(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        f"schema_version={report['schema_version']}",
        f"ok={str(report['ok']).lower()}",
        f"schema_dir={report['schema_dir']}",
        f"fixture_dir={report['fixture_dir']}",
        f"view_models={counts['view_models']}",
        f"schemas_available={counts['schemas_available']}",
        f"emitters_available={counts['emitters_available']}",
        f"fixtures_available={counts['fixtures_available']}",
    ]
    for index, view_model in enumerate(report["view_models"]):
        lines.append(f"view_model[{index}].schema_version={view_model['schema_version']}")
        lines.append(f"view_model[{index}].schema_status={view_model['schema']['status']}")
        lines.append(f"view_model[{index}].emitter_status={view_model['emitter']['status']}")
        lines.append(f"view_model[{index}].fixture_status={view_model['fixture']['status']}")
    for index, error in enumerate(report["errors"]):
        lines.append(
            "error[{index}]={code} schema_version={schema_version} message={message}".format(
                index=index,
                code=error.get("code", "ERROR"),
                schema_version=error.get("schema_version", "-"),
                message=error.get("message", "-"),
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    report = build_catalog(args)
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
