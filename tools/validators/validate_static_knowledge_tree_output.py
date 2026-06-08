#!/usr/bin/env python3
"""Validate published static knowledge-tree output completeness and freshness."""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
        resolve_report_root,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
        resolve_report_root,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.validators.validate_knowledge_tree_build_manifest as validate_knowledge_tree_build_manifest  # noqa: E402

VALIDATOR_NAME = "static_knowledge_tree_output"
CONTRACT_VERSION = "1"
FIXTURE_PATH = "tests/fixtures/static_knowledge_tree_builder/valid_full/inputs/knowledge_tree_export.json"

LINK_ATTRS = {
    "a": ("href",),
    "img": ("src",),
    "link": ("href",),
    "script": ("src",),
    "source": ("src",),
}
ASSET_EXTENSIONS = {".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}


class LinkCollector(HTMLParser):
    """Collect internal references from rendered HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        allowed_attrs = LINK_ATTRS.get(tag)
        if not allowed_attrs:
            return
        attrs_map = dict(attrs)
        for attr_name in allowed_attrs:
            raw_value = attrs_map.get(attr_name)
            if raw_value is None:
                continue
            self.references.append(
                {
                    "tag": tag,
                    "attr": attr_name,
                    "value": raw_value,
                    "line": self.getpos()[0],
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a published static knowledge-tree output from its build manifest, "
            "including freshness drift and internal link completeness."
        ),
        epilog=(
            "Reads one build-manifest JSON file and writes a machine-readable validator report.\n\n"
            f"Example:\n  python3 tools/validators/validate_static_knowledge_tree_output.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the build manifest JSON file.")
    parser.add_argument(
        "--validate-page-links",
        action="store_true",
        help="Also parse rendered HTML pages to validate internal links and assets.",
    )
    add_report_args(parser)
    return parser.parse_args()


def add_error(errors: list[dict[str, Any]], *, code: str, message: str, line: int | None = None) -> None:
    errors.append({"code": code, "line": line, "message": message})


def load_manifest(
    target: Path,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    return validate_knowledge_tree_build_manifest.load_json_object(target)


def resolve_output_root(manifest: dict[str, Any], target: Path) -> Path | None:
    raw = manifest.get("output_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return validate_knowledge_tree_build_manifest.resolve_path(raw, target.parent)


def is_external_reference(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("//"):
        return True
    split = urlsplit(stripped)
    return bool(split.scheme)


def normalize_internal_reference(current_route: str, raw_value: str) -> str | None:
    stripped = raw_value.strip()
    if not stripped or is_external_reference(stripped):
        return None
    split = urlsplit(stripped)
    path_value = unquote(split.path)
    if not path_value:
        return current_route
    if path_value.startswith("/"):
        normalized = posixpath.normpath(path_value.lstrip("/"))
        if not normalized or normalized == ".":
            normalized = "index.html"
    else:
        current_dir = str(PurePosixPath(current_route).parent)
        normalized = posixpath.normpath(posixpath.join(current_dir, path_value))
    if normalized == ".":
        return current_route
    if normalized.startswith("../") or normalized == "..":
        return None
    if normalized.startswith("/"):
        normalized = normalized.lstrip("/")
    return normalized


def classify_reference(target_path: str) -> str:
    suffix = PurePosixPath(target_path).suffix.lower()
    if suffix == ".html":
        return "page"
    if suffix in ASSET_EXTENSIONS:
        return "asset"
    return "file"


def collect_current_export_source_map(manifest: dict[str, Any], target: Path) -> dict[str, dict[str, Any]] | None:
    raw_export_path = manifest.get("export_path")
    if not isinstance(raw_export_path, str) or not raw_export_path.strip():
        return None
    export_path = validate_knowledge_tree_build_manifest.resolve_path(raw_export_path, target.parent)
    if not export_path.exists() or not export_path.is_file():
        return None
    try:
        payload = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    input_sources = payload.get("input_sources")
    if not isinstance(input_sources, list):
        return None
    output: dict[str, dict[str, Any]] = {}
    for entry in input_sources:
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        if isinstance(source_id, str):
            output[source_id] = entry
    return output


def validate_freshness(
    manifest: dict[str, Any],
    current_export_source_map: dict[str, dict[str, Any]] | None,
    errors: list[dict[str, Any]],
) -> None:
    input_sources = manifest.get("input_sources")
    if not isinstance(input_sources, list) or current_export_source_map is None:
        return
    for entry in input_sources:
        if not isinstance(entry, dict):
            continue
        if entry.get("required_for_freshness") is not True:
            continue
        source_id = entry.get("source_id")
        fingerprint = entry.get("fingerprint")
        if not isinstance(source_id, str) or not isinstance(fingerprint, str):
            continue
        current = current_export_source_map.get(source_id)
        if current is None:
            add_error(
                errors,
                code="STALE_INPUT_SOURCE_MISSING",
                message=f"required freshness source is missing from current export input_sources: {source_id}",
            )
            continue
        current_fingerprint = current.get("fingerprint")
        if current_fingerprint != fingerprint:
            add_error(
                errors,
                code="STALE_INPUT_SOURCE_FINGERPRINT",
                message=(
                    f"required freshness source fingerprint changed for {source_id}: "
                    f"{fingerprint} -> {current_fingerprint}"
                ),
            )


def validate_page_links(
    *,
    manifest: dict[str, Any],
    output_root: Path,
    errors: list[dict[str, Any]],
) -> None:
    pages = manifest.get("pages")
    assets = manifest.get("assets")
    if not isinstance(pages, list):
        return
    route_set = {
        page.get("route")
        for page in pages
        if isinstance(page, dict) and isinstance(page.get("route"), str)
    }
    asset_set = {
        asset.get("path")
        for asset in assets or []
        if isinstance(asset, dict) and isinstance(asset.get("path"), str)
    }

    for page in pages:
        if not isinstance(page, dict):
            continue
        route = page.get("route")
        if not isinstance(route, str):
            continue
        page_path = output_root / route
        if not page_path.exists() or not page_path.is_file():
            add_error(errors, code="PAGE_FILE_NOT_FOUND", message=f"published page file not found: {route}")
            continue
        try:
            html_body = page_path.read_text(encoding="utf-8")
        except OSError:
            add_error(errors, code="PAGE_FILE_UNREADABLE", message=f"published page file unreadable: {route}")
            continue

        parser = LinkCollector()
        parser.feed(html_body)
        for reference in parser.references:
            normalized = normalize_internal_reference(route, str(reference["value"]))
            if normalized is None:
                continue
            classification = classify_reference(normalized)
            target_path = output_root / normalized
            line = reference.get("line")
            if classification == "page":
                if normalized not in route_set:
                    add_error(
                        errors,
                        code="BROKEN_INTERNAL_LINK",
                        line=line,
                        message=f"{route} links to unknown route: {normalized}",
                    )
                elif not target_path.exists() or not target_path.is_file():
                    add_error(
                        errors,
                        code="BROKEN_INTERNAL_LINK",
                        line=line,
                        message=f"{route} links to missing page file: {normalized}",
                    )
            elif classification == "asset":
                if normalized not in asset_set:
                    add_error(
                        errors,
                        code="MISSING_ASSET_REFERENCE",
                        line=line,
                        message=f"{route} references asset not declared in manifest: {normalized}",
                    )
                elif not target_path.exists() or not target_path.is_file():
                    add_error(
                        errors,
                        code="MISSING_ASSET_REFERENCE",
                        line=line,
                        message=f"{route} references missing asset file: {normalized}",
                    )
            else:
                if not target_path.exists():
                    add_error(
                        errors,
                        code="BROKEN_FILE_REFERENCE",
                        line=line,
                        message=f"{route} references missing internal file: {normalized}",
                    )


def validate_static_knowledge_tree_output(
    target: Path,
    *,
    validate_page_links_enabled: bool = False,
) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    manifest, errors, load_exit = load_manifest(target)
    if manifest is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, load_exit

    counts["inspected"] = 1

    baseline_report, baseline_exit = validate_knowledge_tree_build_manifest.validate_build_manifest(target)
    errors.extend(baseline_report["errors"])

    output_root = resolve_output_root(manifest, target)
    if output_root is None:
        add_error(errors, code="OUTPUT_ROOT_MISSING", message="build manifest output_root is missing or invalid")
    elif output_root.exists() and not output_root.is_dir():
        add_error(errors, code="OUTPUT_ROOT_NOT_DIRECTORY", message="resolved output_root is not a directory")
    elif validate_page_links_enabled and output_root is not None and output_root.is_dir():
        validate_page_links(manifest=manifest, output_root=output_root, errors=errors)

    current_export_source_map = collect_current_export_source_map(manifest, target)
    validate_freshness(manifest, current_export_source_map, errors)

    if baseline_exit == EXIT_INPUT_UNAVAILABLE:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_INPUT_UNAVAILABLE
    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_static_knowledge_tree_output(
        target,
        validate_page_links_enabled=bool(args.validate_page_links),
    )
    status = "pass" if exit_code == EXIT_PASS else "fail"
    report_root = resolve_report_root(target, report_root=args.report_root)
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts={
            "report_json": display_path(args.report_json),
            "report_text": display_path(args.report_text),
        },
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or (display_path(args.target) or args.target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
        report_root=report_root,
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
