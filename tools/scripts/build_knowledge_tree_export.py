#!/usr/bin/env python3
"""Build a validated knowledge-tree export JSON artifact from a canonical SQLite store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (
    REPO_ROOT,
    REPO_ROOT / "tools" / "common",
    REPO_ROOT / "tools" / "validators",
    REPO_ROOT / "tools" / "source_db_tools",
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.publication_builder import (  # noqa: E402
    PublicationBuildError,
    build_knowledge_tree_export_payload,
    resolve_path,
    write_and_validate_export,
)


SCRIPT_PATH = "tools/scripts/build_knowledge_tree_export.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a validated knowledge-tree export JSON artifact from a canonical SQLite store."
    )
    parser.add_argument("--db", required=True, help="Path to the canonical SQLite store.")
    parser.add_argument("--output", required=True, help="Output path for knowledge_tree_export.json.")
    parser.add_argument("--generated-at", help="Optional RFC3339 timestamp override for deterministic tests.")
    parser.add_argument("--export-id", help="Optional export identifier override.")
    parser.add_argument("--display-name", help="Optional display-name override.")
    parser.add_argument("--workspace-id", help="Optional workspace identifier override.")
    parser.add_argument(
        "--search-output-dir",
        help="Optional directory where public local-search projection sidecars should also be written.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Stdout format.")
    return parser.parse_args()


def render_text(payload: dict[str, object]) -> str:
    lines = [
        f"status={payload['status']}",
        f"output_path={payload['output_path']}",
        f"page_count={payload['page_count']}",
        f"search_indexed={payload['search_indexed']}",
        f"search_returned={payload['search_returned']}",
        f"writer_surface={SCRIPT_PATH}",
    ]
    return "\n".join(lines) + "\n"


def report_path(path: Path, *, base_dir: Path) -> str:
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.name


def main() -> int:
    args = parse_args()
    try:
        db_path = resolve_path(args.db)
        output_path = resolve_path(args.output)
        search_output_dir = resolve_path(args.search_output_dir) if args.search_output_dir else None
        result = build_knowledge_tree_export_payload(
            db_path,
            generated_at=args.generated_at,
            export_id=args.export_id,
            display_name=args.display_name,
            workspace_id=args.workspace_id,
            search_artifacts_dir=search_output_dir,
        )
        write_and_validate_export(output_path, result.payload)
    except PublicationBuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    report = {
        "status": "built",
        "writer_surface": SCRIPT_PATH,
        "db_path": report_path(db_path, base_dir=output_path.parent),
        "output_path": report_path(output_path, base_dir=output_path.parent),
        "page_count": len(result.payload["pages"]),
        "page_families": result.payload["page_families"],
        "generated_at": result.payload["generated_at"],
        "search_indexed": result.search_artifacts.projection_payload["counts"]["projected_records"],
        "search_returned": result.search_artifacts.results_payload["counts"]["returned"],
        "search_projection_path": None
        if result.search_artifacts.projection_json_path is None
        else report_path(result.search_artifacts.projection_json_path, base_dir=output_path.parent),
        "search_results_path": None
        if result.search_artifacts.results_json_path is None
        else report_path(result.search_artifacts.results_json_path, base_dir=output_path.parent),
    }
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
