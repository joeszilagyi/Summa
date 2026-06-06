from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "scripts" / "build_static_knowledge_tree.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_knowledge_tree_build_manifest.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "static_knowledge_tree_builder" / "valid_full" / "inputs"

spec = importlib.util.spec_from_file_location("static_knowledge_tree_builder_for_tests", SCRIPT_PATH)
assert spec is not None
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(builder)

validator_spec = importlib.util.spec_from_file_location("knowledge_tree_build_manifest_validator_for_tests", VALIDATOR_PATH)
assert validator_spec is not None
manifest_validator = importlib.util.module_from_spec(validator_spec)
assert validator_spec.loader is not None
validator_spec.loader.exec_module(manifest_validator)


def export_fixture() -> Path:
    return FIXTURE_ROOT / "knowledge_tree_export.json"


def presentation_fixture() -> Path:
    return FIXTURE_ROOT / "public_presentation.json"


def test_build_static_knowledge_tree_publishes_valid_output(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"

    payload = builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180000Z",
        built_at="2026-06-02T18:00:00Z",
    )

    assert payload["status"] == "published"
    assert payload["page_count"] == 8
    assert payload["asset_count"] == 1

    manifest_path = publish_root / "build-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["presentation_path"] == str(presentation_fixture().resolve())
    assert manifest["output_root"] == "."

    report, exit_code = manifest_validator.validate_build_manifest(manifest_path)
    assert exit_code == manifest_validator.EXIT_PASS, report

    assert (publish_root / "index.html").is_file()
    assert (publish_root / "facets" / "records.html").is_file()
    assert (publish_root / "assets" / "site.css").is_file()
    assert "Subject Tree" in (publish_root / "index.html").read_text(encoding="utf-8")


def test_render_page_html_escapes_hostile_html_and_attributes() -> None:
    page = {
        "page_id": "page-1",
        "page_family": "family-1",
        "route": "pages/current.html",
        "title": '<script>alert("x")</script>',
        "lede": '<img src=x onerror="alert(1)">',
        "summary_cards": [
            {"label": "<b>Label</b>", "value": '" onmouseover="alert(1)'},
        ],
        "source_ids": ['source-1<script>alert(1)</script>'],
        "related_page_ids": ["page-2"],
        "redaction_gate_refs": ['javascript:alert(1)" onclick="x'],
        "sections": [
            {
                "heading": '<svg onload="alert(1)">',
                "paragraphs": ['<!-- comment --> <script>alert(1)</script>'],
                "bullet_items": ['[link](javascript:alert(1))', 'quote " breaker'],
                "link_page_ids": ["page-2"],
            }
        ],
    }
    presentation_page = {
        "breadcrumbs": ["index.html", 'pages/" onclick="alert(1).html'],
        "navigation_children": ['children/" onclick="alert(1).html'],
        "page_family": "family-1",
        "reader_state": '"><img src=x onerror="alert(1)">',
        "review_state": "needs_review",
        "validation_state": 'validated"><script>alert(1)</script>',
        "publication_state": "public",
        "source_transparency": '<svg onload="alert(1)">',
        "empty_state": "<textarea autofocus>",
        "redaction_gate_refs": ['<img src=x onerror="alert(1)">'],
    }
    export_payload = {
        "display_name": '<span onclick="alert(1)">Subject Tree</span>',
        "export_profile": "public",
        "workspace_id": "workspace-1",
    }
    page_route_map = {
        "page-1": "pages/current.html",
        "page-2": 'pages/target/" onclick="alert(1).html',
    }

    html = builder.render_page_html(
        page,
        presentation_page,
        page_route_map=page_route_map,
        export_payload=export_payload,
    )

    assert "<script>" not in html
    assert "<img" not in html
    assert '" onclick="' not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "&quot; onclick=&quot;alert(1)" in html
    assert "javascript:alert(1)" in html


def test_build_static_knowledge_tree_restores_previous_output_on_publish_failure(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"
    publish_root.mkdir()
    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T175959Z",
        built_at="2026-06-02T17:59:59Z",
    )
    original_body = (publish_root / "index.html").read_text(encoding="utf-8")

    def fail_after_backup() -> None:
        raise RuntimeError("simulated publish failure")

    try:
        builder.build_static_knowledge_tree(
            export_fixture(),
            presentation_fixture(),
            publish_root,
            build_id="build-20260602T180001Z",
            built_at="2026-06-02T18:00:01Z",
            after_backup_hook=fail_after_backup,
        )
    except builder.StaticKnowledgeTreeBuildError as exc:
        assert "atomic publish failed" in str(exc)
    else:
        raise AssertionError("expected publish failure")

    assert (publish_root / "index.html").read_text(encoding="utf-8") == original_body
    assert (publish_root / "build-manifest.json").is_file()


def test_build_static_knowledge_tree_rejects_unrecognized_existing_publish_root(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"
    publish_root.mkdir()
    (publish_root / "notes.txt").write_text("not a knowledge-tree publish output", encoding="utf-8")

    try:
        builder.build_static_knowledge_tree(
            export_fixture(),
            presentation_fixture(),
            publish_root,
            build_id="build-20260602T175960Z",
            built_at="2026-06-02T17:59:00Z",
        )
    except builder.StaticKnowledgeTreeBuildError as exc:
        assert "publish root exists but is not a recognized static-tree output directory" in str(exc)
    else:
        raise AssertionError("expected publish root validation failure")


def test_build_static_knowledge_tree_keeps_backup_after_publish(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"

    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T175958Z",
        built_at="2026-06-02T17:59:58Z",
    )

    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T175961Z",
        built_at="2026-06-02T18:00:01Z",
    )

    backups = list(tmp_path.glob(f".{publish_root.name}.backup.*"))
    assert backups, "expected a backup directory to remain after publish"
    assert all((backup / "build-manifest.json").is_file() for backup in backups)


def test_builder_cli_emits_json_payload(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--export",
            str(export_fixture()),
            "--presentation",
            str(presentation_fixture()),
            "--publish-root",
            str(publish_root),
            "--build-id",
            "build-20260602T180002Z",
            "--built-at",
            "2026-06-02T18:00:02Z",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "published"
    assert payload["manifest_path"] == str((publish_root / "build-manifest.json").resolve())
