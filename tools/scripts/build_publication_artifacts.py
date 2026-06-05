#!/usr/bin/env python3
"""Build the full publication chain from canonical store to static public output."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
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

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common.leak_scanner import scan_directory  # noqa: E402
from tools.common.publication_builder import (  # noqa: E402
    PublicationBuildError,
    build_knowledge_tree_export_payload,
    build_public_presentation_payload,
    resolve_path,
    write_and_validate_export,
    write_and_validate_presentation,
)
from tools.scripts.build_static_knowledge_tree import (  # noqa: E402
    StaticKnowledgeTreeBuildError,
    build_static_knowledge_tree,
)
from tools.source_db_tools import canonical_graph_closure  # noqa: E402

SCRIPT_PATH = "tools/scripts/build_publication_artifacts.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the full publication chain from canonical store to static public output."
    )
    parser.add_argument("--db", required=True, help="Path to the canonical SQLite store.")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory that will receive export, presentation, and static outputs.",
    )
    parser.add_argument(
        "--generated-at", help="Optional RFC3339 timestamp override for deterministic tests."
    )
    parser.add_argument("--build-id", help="Optional static build identifier override.")
    parser.add_argument("--built-at", help="Optional static build timestamp override.")
    parser.add_argument("--export-id", help="Optional export identifier override.")
    parser.add_argument("--display-name", help="Optional display-name override.")
    parser.add_argument("--workspace-id", help="Optional workspace identifier override.")
    parser.add_argument(
        "--graph-closure-preflight",
        dest="graph_closure_preflight",
        action="store_true",
        default=True,
        help="Run read-only graph-closure preflight before building public artifacts. Enabled by default.",
    )
    parser.add_argument(
        "--no-graph-closure-preflight",
        dest="graph_closure_preflight",
        action="store_false",
        help="Disable graph-closure preflight.",
    )
    parser.add_argument(
        "--graph-closure-strict",
        action="store_true",
        help="Fail publication when graph closure reports true orphan errors.",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Stdout format.")
    return parser.parse_args()


def render_text(payload: dict[str, object]) -> str:
    lines = [
        f"status={payload['status']}",
        f"output_dir={payload['output_dir']}",
        f"export_path={payload['export_path']}",
        f"presentation_path={payload['presentation_path']}",
        f"publish_root={payload['publish_root']}",
        f"leak_scan_status={payload['leak_scan']['status']}",
        f"writer_surface={SCRIPT_PATH}",
    ]
    return "\n".join(lines) + "\n"


def stage_public_scan_root(publish_root: Path) -> Path:
    stage_parent = Path(tempfile.mkdtemp(prefix=".publication-leak-scan.", dir=publish_root.parent))
    stage_root = stage_parent / "public-site"
    for path in sorted(publish_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(publish_root)
        if relative.as_posix() == "build-manifest.json":
            continue
        destination = stage_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return stage_root


def main() -> int:
    args = parse_args()
    try:
        db_path = resolve_path(args.db)
        output_dir = resolve_path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        search_output_dir = output_dir / "search"
        export_path = output_dir / "knowledge_tree_export.json"
        presentation_path = output_dir / "public_presentation.json"
        publish_root = output_dir / "static"
        leak_report_path = output_dir / "leak-scan-report.json"
        graph_closure_report: dict[str, object] | None = None
        graph_closure_report_path = output_dir / "graph-closure-report.json"
        if args.graph_closure_preflight:
            graph_closure_report = canonical_graph_closure.audit_canonical_graph_closure(
                db_path,
                generated_at=args.generated_at,
                strict=bool(args.graph_closure_strict),
                report_path=graph_closure_report_path,
            )
            if args.graph_closure_strict and graph_closure_report["status"] == "fail":
                raise PublicationBuildError("graph closure preflight found true orphan errors")

        export_result = build_knowledge_tree_export_payload(
            db_path,
            generated_at=args.generated_at,
            export_id=args.export_id,
            display_name=args.display_name,
            workspace_id=args.workspace_id,
            search_artifacts_dir=search_output_dir,
        )
        write_and_validate_export(export_path, export_result.payload)

        presentation_payload = build_public_presentation_payload(export_result.payload)
        write_and_validate_presentation(presentation_path, presentation_payload)

        static_report = build_static_knowledge_tree(
            export_path,
            presentation_path,
            publish_root,
            build_id=args.build_id,
            built_at=args.built_at,
        )
        leak_stage_root = stage_public_scan_root(publish_root)
        try:
            leak_report = scan_directory(leak_stage_root, profile="public_bundle")
            atomic_write_json(leak_report_path, leak_report)
            if leak_report["status"] != "pass":
                finding_codes = ", ".join(
                    finding["code"]
                    for finding in leak_report.get("findings", [])
                    if isinstance(finding, dict) and isinstance(finding.get("code"), str)
                )
                raise PublicationBuildError(
                    "public leak scan failed" + (f": {finding_codes}" if finding_codes else "")
                )
        finally:
            shutil.rmtree(leak_stage_root.parent, ignore_errors=True)
    except (
        PublicationBuildError,
        StaticKnowledgeTreeBuildError,
        canonical_graph_closure.GraphClosureError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    report = {
        "status": "published",
        "writer_surface": SCRIPT_PATH,
        "output_dir": str(output_dir),
        "export_path": str(export_path),
        "presentation_path": str(presentation_path),
        "publish_root": str(publish_root),
        "search_projection_path": str(search_output_dir / "local_search_projection.json"),
        "search_results_path": str(search_output_dir / "local_search_results.json"),
        "search_index_db": str(search_output_dir / "local_search.sqlite"),
        "leak_report_path": str(leak_report_path),
        "leak_scan": {
            "status": leak_report["status"],
            "finding_count": leak_report["counts"]["findings"],
            "suppressed_finding_count": leak_report["counts"]["suppressed_findings"],
        },
        "graph_closure": {
            "enabled": bool(args.graph_closure_preflight),
            "strict": bool(args.graph_closure_strict),
            "status": None if graph_closure_report is None else graph_closure_report.get("status"),
            "report_path": None if graph_closure_report is None else str(graph_closure_report_path),
            "orphan_error_count": 0
            if graph_closure_report is None
            else graph_closure_report.get("summary", {}).get("true_orphan_error_count"),
            "unresolved_tracked_count": 0
            if graph_closure_report is None
            else graph_closure_report.get("summary", {}).get("unresolved_tracked_count"),
        },
        "static_build": static_report,
    }
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
