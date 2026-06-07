#!/usr/bin/env python3
"""Build a validated public-presentation JSON artifact from a knowledge-tree export."""

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
):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.publication_builder import (  # noqa: E402
    PublicationBuildError,
    build_public_presentation_payload,
    load_json_object,
    resolve_existing_file,
    resolve_path,
    validate_export_file,
    write_and_validate_presentation,
)


SCRIPT_PATH = "tools/scripts/build_public_knowledge_tree_presentation.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a validated public-presentation JSON artifact from a knowledge-tree export."
    )
    parser.add_argument("--export", required=True, help="Path to knowledge_tree_export.json.")
    parser.add_argument("--output", required=True, help="Output path for public_presentation.json.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Stdout format.")
    return parser.parse_args()


def render_text(payload: dict[str, object]) -> str:
    lines = [
        f"status={payload['status']}",
        f"output_path={payload['output_path']}",
        f"page_count={payload['page_count']}",
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
        export_path = resolve_existing_file(args.export, label="export")
        output_path = resolve_path(args.output)
        validate_export_file(export_path)
        export_payload = load_json_object(export_path, label="knowledge tree export")
        presentation_payload = build_public_presentation_payload(export_payload)
        write_and_validate_presentation(output_path, presentation_payload)
    except PublicationBuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    report = {
        "status": "built",
        "writer_surface": SCRIPT_PATH,
        "export_path": report_path(export_path, base_dir=output_path.parent),
        "output_path": report_path(output_path, base_dir=output_path.parent),
        "page_count": len(presentation_payload["page_inventory"]),
        "page_families": [page["page_family"] for page in presentation_payload["page_inventory"]],
    }
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
