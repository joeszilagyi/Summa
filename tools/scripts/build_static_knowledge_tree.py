#!/usr/bin/env python3
"""Build a static knowledge tree from validated export and presentation artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.atomic_write import atomic_write_json, atomic_write_text  # noqa: E402
from tools.common.operator_text import format_operator_text_value  # noqa: E402
from tools.validators.validate_knowledge_tree_build_manifest import (  # noqa: E402
    EXIT_PASS as EXIT_BUILD_MANIFEST_PASS,
    hash_file,
    validate_build_manifest,
)
from tools.validators.validate_knowledge_tree_export import (  # noqa: E402
    EXIT_PASS as EXIT_EXPORT_PASS,
    validate_knowledge_tree_export,
)
from tools.validators.validate_public_knowledge_tree_presentation import (  # noqa: E402
    EXIT_PASS as EXIT_PRESENTATION_PASS,
    validate_public_knowledge_tree_presentation,
)


SCRIPT_PATH = "tools/scripts/build_static_knowledge_tree.py"
SCHEMA_VERSION = "knowledge-tree-build-manifest.v1"
STYLESHEET_PATH = "assets/site.css"
BUILD_MANIFEST_FILENAME = "build-manifest.json"

DEFAULT_NOTES = [
    "published_via_atomic_swap",
    "inputs_validated_before_render",
    "previous_output_restored_on_publish_failure",
]

STYLESHEET_BODY = """body {
  font-family: Arial, sans-serif;
  line-height: 1.5;
  margin: 0;
  color: #1f2933;
  background: #f6f8fb;
}
header, main, footer {
  max-width: 960px;
  margin: 0 auto;
  padding: 1.25rem 1rem;
}
header {
  background: #ffffff;
  border-bottom: 1px solid #d9e2ec;
}
main {
  background: #ffffff;
  min-height: calc(100vh - 9rem);
}
nav ul, .meta-list, .source-list, .related-list, .gate-list {
  padding-left: 1.25rem;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.75rem;
}
.summary-card {
  border: 1px solid #d9e2ec;
  padding: 0.75rem;
  background: #f8fbff;
}
.eyebrow {
  color: #52606d;
  font-size: 0.95rem;
}
.state-table td {
  padding: 0.2rem 0.5rem 0.2rem 0;
  vertical-align: top;
}
a {
  color: #0b66c3;
}
code {
  font-family: monospace;
}
"""


class StaticKnowledgeTreeBuildError(RuntimeError):
    """Raised when the static knowledge tree cannot be built or published."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a static knowledge tree from validated export and presentation artifacts."
    )
    parser.add_argument("--export", required=True, help="Path to a knowledge-tree export JSON artifact.")
    parser.add_argument(
        "--presentation",
        required=True,
        help="Path to a public knowledge-tree presentation JSON artifact.",
    )
    parser.add_argument(
        "--publish-root",
        required=True,
        help="Directory path to publish atomically. Existing contents are preserved on failure.",
    )
    parser.add_argument(
        "--build-id",
        help=(
            "Optional build identifier. Defaults to a deterministic hash derived from the input"
            " export and presentation artifacts."
        ),
    )
    parser.add_argument(
        "--built-at",
        help="Optional RFC3339 timestamp override for deterministic tests.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Stdout format for the build report.",
    )
    return parser.parse_args()


def now_rfc3339() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_build_id(*, export_sha256: str, presentation_sha256: str) -> str:
    digest_input = f"{export_sha256}|{presentation_sha256}".encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()
    return f"build-{digest[:16]}"


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_existing_file(raw_path: str, *, label: str) -> Path:
    path = resolve_path(raw_path)
    if not path.exists():
        raise StaticKnowledgeTreeBuildError(f"{label} path does not exist: {path}")
    if not path.is_file():
        raise StaticKnowledgeTreeBuildError(f"{label} path is not a file: {path}")
    return path


def _is_empty_directory(path: Path) -> bool:
    return not any(path.iterdir())


def _is_static_knowledge_tree_publish_root(path: Path) -> bool:
    manifest_path = path / BUILD_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False
    _, exit_code = validate_build_manifest(manifest_path)
    return exit_code == EXIT_BUILD_MANIFEST_PASS


def resolve_publish_root(raw_path: str) -> Path:
    path = resolve_path(raw_path)
    if path.exists() and not path.is_dir():
        raise StaticKnowledgeTreeBuildError(f"publish root exists and is not a directory: {path}")
    if path.exists() and path.is_dir() and not _is_empty_directory(path) and not _is_static_knowledge_tree_publish_root(path):
        raise StaticKnowledgeTreeBuildError(
            f"publish root exists but is not a recognized static-tree output directory: {path}"
        )
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StaticKnowledgeTreeBuildError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StaticKnowledgeTreeBuildError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StaticKnowledgeTreeBuildError(f"{label} must be a JSON object: {path}")
    return payload


def ensure_valid_export(path: Path) -> dict[str, Any]:
    report, exit_code = validate_knowledge_tree_export(path)
    if exit_code != EXIT_EXPORT_PASS:
        first_error = report["errors"][0]["message"] if report["errors"] else "validation failed"
        raise StaticKnowledgeTreeBuildError(f"export failed validation: {path}: {first_error}")
    return load_json(path, label="export")


def ensure_valid_presentation(path: Path) -> dict[str, Any]:
    report, exit_code = validate_public_knowledge_tree_presentation(path)
    if exit_code != EXIT_PRESENTATION_PASS:
        first_error = report["errors"][0]["message"] if report["errors"] else "validation failed"
        raise StaticKnowledgeTreeBuildError(f"presentation failed validation: {path}: {first_error}")
    return load_json(path, label="presentation")


def page_title(page: dict[str, Any]) -> str:
    title = page.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return str(page.get("page_id", "Untitled"))


def build_presentation_indexes(
    presentation_payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    route_index: dict[str, dict[str, Any]] = {}
    family_index: dict[str, dict[str, Any]] = {}
    for page in presentation_payload.get("page_inventory", []):
        if not isinstance(page, dict):
            continue
        route = page.get("route")
        family = page.get("page_family")
        if isinstance(route, str):
            route_index[route] = page
        if isinstance(family, str):
            family_index[family] = page
    return route_index, family_index


def validate_cross_artifact_consistency(
    export_payload: dict[str, Any],
    presentation_payload: dict[str, Any],
) -> None:
    route_index, family_index = build_presentation_indexes(presentation_payload)
    export_pages = export_payload.get("pages")
    if not isinstance(export_pages, list) or not export_pages:
        raise StaticKnowledgeTreeBuildError("export pages array is missing or empty after validation")
    if len(export_pages) != len(route_index):
        raise StaticKnowledgeTreeBuildError(
            "presentation page_inventory count does not match export page count"
        )

    export_page_ids: set[str] = set()
    export_page_routes: set[str] = set()
    for page in export_pages:
        if not isinstance(page, dict):
            raise StaticKnowledgeTreeBuildError("export page entries must be objects")
        page_id = page.get("page_id")
        page_family = page.get("page_family")
        route = page.get("route")
        if not isinstance(page_id, str) or not isinstance(page_family, str) or not isinstance(route, str):
            raise StaticKnowledgeTreeBuildError("validated export pages are missing required identifiers")
        presentation_page = route_index.get(route)
        if presentation_page is None:
            raise StaticKnowledgeTreeBuildError(f"presentation missing route from export: {route}")
        if presentation_page.get("page_family") != page_family:
            raise StaticKnowledgeTreeBuildError(
                f"presentation page_family mismatch for route {route}: "
                f"{presentation_page.get('page_family')} != {page_family}"
            )
        export_page_ids.add(page_id)
        export_page_routes.add(route)
        family_page = family_index.get(page_family)
        if family_page is None or family_page.get("route") != route:
            raise StaticKnowledgeTreeBuildError(
                f"presentation family routing mismatch for page_family {page_family}"
            )

    for route in route_index:
        if route not in export_page_routes:
            raise StaticKnowledgeTreeBuildError(f"presentation route missing from export: {route}")

    landing_page_id = export_payload.get("landing_page_id")
    if landing_page_id not in export_page_ids:
        raise StaticKnowledgeTreeBuildError("landing_page_id is not present in export pages")


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def stylesheet_href(route: str) -> str:
    return os.path.relpath(STYLESHEET_PATH, start=str(PurePosixPath(route).parent)).replace(os.sep, "/")


def route_href(from_route: str, to_route: str) -> str:
    from_parent = PurePosixPath(from_route).parent
    relative = os.path.relpath(to_route, start=str(from_parent))
    return relative.replace(os.sep, "/")


def render_list(items: list[str], *, ordered: bool = False, class_name: str | None = None) -> str:
    if not items:
        return "<p>None.</p>"
    tag = "ol" if ordered else "ul"
    class_attr = f' class="{class_name}"' if class_name else ""
    lines = [f"<{tag}{class_attr}>"]
    for item in items:
        lines.append(f"  <li>{item}</li>")
    lines.append(f"</{tag}>")
    return "\n".join(lines)


def render_sections(page: dict[str, Any], page_route_map: dict[str, str], route: str) -> str:
    sections = page.get("sections")
    if not isinstance(sections, list):
        return ""
    blocks: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = escape(section.get("heading", "Section"))
        blocks.append(f"<section>\n<h2>{heading}</h2>")
        paragraphs = section.get("paragraphs")
        if isinstance(paragraphs, list):
            for paragraph in paragraphs:
                blocks.append(f"<p>{escape(paragraph)}</p>")
        bullet_items = section.get("bullet_items")
        if isinstance(bullet_items, list) and bullet_items:
            bullet_html = [escape(item) for item in bullet_items]
            blocks.append(render_list(bullet_html))
        link_page_ids = section.get("link_page_ids")
        if isinstance(link_page_ids, list) and link_page_ids:
            links: list[str] = []
            for page_id in link_page_ids:
                target_route = page_route_map.get(str(page_id))
                if target_route is None:
                    continue
                href = route_href(route, target_route)
                links.append(f'<a href="{escape(href)}">{escape(target_route)}</a>')
            if links:
                blocks.append("<p>Related pages:</p>")
                blocks.append(render_list(links))
        blocks.append("</section>")
    return "\n".join(blocks)


def render_page_html(
    page: dict[str, Any],
    presentation_page: dict[str, Any],
    *,
    page_route_map: dict[str, str],
    export_payload: dict[str, Any],
) -> str:
    route = str(page["route"])
    title = page_title(page)
    lede = escape(page.get("lede", ""))
    stylesheet = stylesheet_href(route)

    summary_cards = page.get("summary_cards")
    summary_html = ["<div class=\"summary-grid\">"]
    if isinstance(summary_cards, list):
        for card in summary_cards:
            if not isinstance(card, dict):
                continue
            summary_html.append("<div class=\"summary-card\">")
            summary_html.append(f"<strong>{escape(card.get('label', ''))}</strong>")
            summary_html.append(f"<div>{escape(card.get('value', ''))}</div>")
            summary_html.append("</div>")
    summary_html.append("</div>")

    source_ids = [escape(item) for item in page.get("source_ids", []) if isinstance(item, str)]
    related_links: list[str] = []
    for page_id in page.get("related_page_ids", []):
        target_route = page_route_map.get(str(page_id))
        if target_route is None:
            continue
        related_links.append(f'<a href="{escape(route_href(route, target_route))}">{escape(target_route)}</a>')

    breadcrumbs = presentation_page.get("breadcrumbs", [])
    breadcrumb_links: list[str] = []
    if isinstance(breadcrumbs, list):
        for breadcrumb_route in breadcrumbs:
            if not isinstance(breadcrumb_route, str):
                continue
            label = "Home" if breadcrumb_route == "index.html" else breadcrumb_route
            href = route_href(route, breadcrumb_route)
            breadcrumb_links.append(f'<a href="{escape(href)}">{escape(label)}</a>')

    nav_children = presentation_page.get("navigation_children", [])
    nav_child_links: list[str] = []
    if isinstance(nav_children, list):
        for child_route in nav_children:
            if not isinstance(child_route, str):
                continue
            nav_child_links.append(
                f'<a href="{escape(route_href(route, child_route))}">{escape(child_route)}</a>'
            )

    state_rows = [
        ("Page family", presentation_page.get("page_family")),
        ("Reader state", presentation_page.get("reader_state")),
        ("Review state", presentation_page.get("review_state")),
        ("Validation state", presentation_page.get("validation_state")),
        ("Publication state", presentation_page.get("publication_state")),
        ("Source transparency", presentation_page.get("source_transparency")),
        ("Workspace", export_payload.get("workspace_id")),
    ]
    state_table = ["<table class=\"state-table\">"]
    for label, value in state_rows:
        state_table.append(f"<tr><td><strong>{escape(label)}</strong></td><td>{escape(value)}</td></tr>")
    state_table.append("</table>")

    empty_state = presentation_page.get("empty_state")
    gates = [escape(item) for item in presentation_page.get("redaction_gate_refs", []) if isinstance(item, str)]

    return "\n".join(
        [
            "<!DOCTYPE html>",
            "<html lang=\"en\">",
            "<head>",
            "  <meta charset=\"utf-8\">",
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            f"  <title>{escape(title)}</title>",
            f"  <link rel=\"stylesheet\" href=\"{escape(stylesheet)}\">",
            "</head>",
            "<body>",
            "<header>",
            f"  <p class=\"eyebrow\">{escape(export_payload.get('display_name'))}</p>",
            f"  <h1>{escape(title)}</h1>",
            f"  <p>{lede}</p>",
            "</header>",
            "<main>",
            "  <section>",
            "    <h2>Summary</h2>",
            *summary_html,
            "  </section>",
            "  <section>",
            "    <h2>Publication state</h2>",
            *state_table,
            "  </section>",
            "  <section>",
            "    <h2>Breadcrumbs</h2>",
            render_list(breadcrumb_links, class_name="meta-list"),
            "  </section>",
            "  <section>",
            "    <h2>Child routes</h2>",
            render_list(nav_child_links, class_name="meta-list"),
            "  </section>",
            "  <section>",
            "    <h2>Sources</h2>",
            render_list(source_ids, class_name="source-list"),
            "  </section>",
            "  <section>",
            "    <h2>Related pages</h2>",
            render_list(related_links, class_name="related-list"),
            "  </section>",
            "  <section>",
            "    <h2>Redaction gates</h2>",
            render_list(gates, class_name="gate-list"),
            "  </section>",
            "  <section>",
            f"    <h2>Empty state</h2><p>{escape(empty_state)}</p>",
            "  </section>",
            render_sections(page, page_route_map, route),
            "</main>",
            "<footer>",
            f"  <p>Export profile: <code>{escape(export_payload.get('export_profile'))}</code></p>",
            f"  <p>Built by <code>{SCRIPT_PATH}</code></p>",
            "</footer>",
            "</body>",
            "</html>",
            "",
        ]
    )


def build_manifest_payload(
    *,
    export_path: Path,
    presentation_path: Path,
    publish_root: Path,
    build_id: str,
    built_at: str,
    export_payload: dict[str, Any],
    page_records: list[dict[str, Any]],
    asset_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "build_id": build_id,
        "export_id": export_payload["export_id"],
        "landing_page_id": export_payload["landing_page_id"],
        "export_path": str(export_path),
        "export_sha256": hash_file(export_path),
        "presentation_path": str(presentation_path),
        "presentation_sha256": hash_file(presentation_path),
        "built_at": built_at,
        "output_root": ".",
        "page_count": len(page_records),
        "asset_count": len(asset_records),
        "input_sources": [
            {
                "source_id": source["source_id"],
                "fingerprint": source["fingerprint"],
                "required_for_freshness": source["required_for_freshness"],
            }
            for source in export_payload["input_sources"]
        ],
        "assets": asset_records,
        "pages": page_records,
        "notes": DEFAULT_NOTES + [f"publish_root={publish_root}"],
    }


def write_static_tree(
    stage_root: Path,
    *,
    export_path: Path,
    presentation_path: Path,
    publish_root: Path,
    export_payload: dict[str, Any],
    presentation_payload: dict[str, Any],
    build_id: str,
    built_at: str,
) -> dict[str, Any]:
    route_index, _ = build_presentation_indexes(presentation_payload)
    page_route_map = {
        str(page["page_id"]): str(page["route"])
        for page in export_payload["pages"]
        if isinstance(page, dict)
    }

    atomic_write_text(stage_root / STYLESHEET_PATH, STYLESHEET_BODY)
    asset_records = [{"path": STYLESHEET_PATH, "sha256": hash_file(stage_root / STYLESHEET_PATH)}]

    page_records: list[dict[str, Any]] = []
    for page in export_payload["pages"]:
        if not isinstance(page, dict):
            continue
        route = str(page["route"])
        presentation_page = route_index[route]
        page_path = stage_root / route
        html_body = render_page_html(
            page,
            presentation_page,
            page_route_map=page_route_map,
            export_payload=export_payload,
        )
        atomic_write_text(page_path, html_body)
        page_records.append(
            {
                "page_id": page["page_id"],
                "page_family": page["page_family"],
                "route": route,
                "title": page_title(page),
                "sha256": hash_file(page_path),
                "source_ids": list(page["source_ids"]),
                "related_page_ids": list(page["related_page_ids"]),
            }
        )

    manifest_payload = build_manifest_payload(
        export_path=export_path,
        presentation_path=presentation_path,
        publish_root=publish_root,
        build_id=build_id,
        built_at=built_at,
        export_payload=export_payload,
        page_records=page_records,
        asset_records=asset_records,
    )
    manifest_path = stage_root / "build-manifest.json"
    atomic_write_json(manifest_path, manifest_payload)
    report, exit_code = validate_build_manifest(manifest_path)
    if exit_code != EXIT_BUILD_MANIFEST_PASS:
        first_error = report["errors"][0]["message"] if report["errors"] else "validation failed"
        raise StaticKnowledgeTreeBuildError(f"generated build manifest failed validation: {first_error}")

    return {
        "manifest_path": manifest_path,
        "manifest": manifest_payload,
    }


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_stage_dir(
    stage_root: Path,
    publish_root: Path,
    *,
    after_backup_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    backup_root: Path | None = None
    publish_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        if publish_root.exists():
            backup_root = publish_root.parent / f".{publish_root.name}.backup.{uuid.uuid4().hex[:8]}"
            publish_root.replace(backup_root)
            _fsync_directory(publish_root.parent)
            if after_backup_hook is not None:
                after_backup_hook()
        stage_root.replace(publish_root)
        _fsync_directory(publish_root.parent)
    except Exception as exc:
        if backup_root is not None and backup_root.exists() and not publish_root.exists():
            backup_root.replace(publish_root)
            _fsync_directory(publish_root.parent)
        raise StaticKnowledgeTreeBuildError(f"atomic publish failed: {exc}") from exc
    return {
        "backup_root": str(backup_root) if backup_root is not None else None,
        "backup_restored": False,
    }


def build_static_knowledge_tree(
    export_path: Path,
    presentation_path: Path,
    publish_root: Path,
    *,
    build_id: str | None = None,
    built_at: str | None = None,
    after_backup_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    resolved_export = resolve_existing_file(str(export_path), label="export")
    resolved_presentation = resolve_existing_file(str(presentation_path), label="presentation")
    resolved_publish_root = resolve_publish_root(str(publish_root))

    export_payload = ensure_valid_export(resolved_export)
    presentation_payload = ensure_valid_presentation(resolved_presentation)
    validate_cross_artifact_consistency(export_payload, presentation_payload)

    export_sha256 = hash_file(resolved_export)
    presentation_sha256 = hash_file(resolved_presentation)
    effective_build_id = build_id or default_build_id(
        export_sha256=export_sha256,
        presentation_sha256=presentation_sha256,
    )
    effective_built_at = built_at or now_rfc3339()

    stage_parent = Path(
        tempfile.mkdtemp(prefix=f".{resolved_publish_root.name}.stage.", dir=resolved_publish_root.parent)
    )
    stage_site = stage_parent / resolved_publish_root.name
    stage_site.mkdir(parents=True, exist_ok=True)
    try:
        stage_result = write_static_tree(
            stage_site,
            export_path=resolved_export,
            presentation_path=resolved_presentation,
            publish_root=resolved_publish_root,
            export_payload=export_payload,
            presentation_payload=presentation_payload,
            build_id=effective_build_id,
            built_at=effective_built_at,
        )
        publish_stage_dir(stage_site, resolved_publish_root, after_backup_hook=after_backup_hook)
    finally:
        if stage_site.exists():
            shutil.rmtree(stage_site, ignore_errors=True)
        if stage_parent.exists():
            shutil.rmtree(stage_parent, ignore_errors=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "build_id": effective_build_id,
        "built_at": effective_built_at,
        "export_path": str(resolved_export),
        "presentation_path": str(resolved_presentation),
        "publish_root": str(resolved_publish_root),
        "manifest_path": str(resolved_publish_root / "build-manifest.json"),
        "page_count": stage_result["manifest"]["page_count"],
        "asset_count": stage_result["manifest"]["asset_count"],
        "status": "published",
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"status={format_operator_text_value(payload['status'])}",
        f"build_id={format_operator_text_value(payload['build_id'])}",
        f"publish_root={format_operator_text_value(payload['publish_root'])}",
        f"manifest_path={format_operator_text_value(payload['manifest_path'])}",
        f"page_count={format_operator_text_value(payload['page_count'])}",
        f"asset_count={format_operator_text_value(payload['asset_count'])}",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = build_static_knowledge_tree(
            Path(args.export),
            Path(args.presentation),
            Path(args.publish_root),
            build_id=args.build_id,
            built_at=args.built_at,
        )
    except StaticKnowledgeTreeBuildError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
