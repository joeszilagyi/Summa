from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


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
    assert not Path(manifest["export_path"]).is_absolute()
    assert not Path(manifest["presentation_path"]).is_absolute()
    assert manifest["export_path"].endswith("knowledge_tree_export.json")
    assert manifest["presentation_path"].endswith("public_presentation.json")
    assert manifest["output_root"] == "."

    report, exit_code = manifest_validator.validate_build_manifest(manifest_path)
    assert exit_code == manifest_validator.EXIT_PASS, report

    assert (publish_root / "index.html").is_file()
    assert (publish_root / "facets" / "records.html").is_file()
    assert (publish_root / "assets" / "site.css").is_file()
    assert "Subject Tree" in (publish_root / "index.html").read_text(encoding="utf-8")


def test_build_manifest_receipt_validation_uses_in_memory_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish_root = tmp_path / "public-site"
    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180002Z",
        built_at="2026-06-02T18:00:02Z",
    )

    manifest_path = publish_root / "build-manifest.json"
    receipt = manifest_validator.BuildManifestReceipt(
        manifest=json.loads(manifest_path.read_text(encoding="utf-8")),
        export_payload=json.loads(export_fixture().read_text(encoding="utf-8")),
        presentation_payload=json.loads(presentation_fixture().read_text(encoding="utf-8")),
        export_sha256=sha256_of(export_fixture()),
        presentation_sha256=sha256_of(presentation_fixture()),
    )

    def fail_validate_export(*_args: object, **_kwargs: object) -> tuple[dict[str, object], int]:
        raise AssertionError("export validator should not be called by receipt validation")

    def fail_validate_presentation(*_args: object, **_kwargs: object) -> tuple[dict[str, object], int]:
        raise AssertionError("presentation validator should not be called by receipt validation")

    def fail_hash_file(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("receipt validation should not hash files")

    monkeypatch.setattr(
        manifest_validator.validate_knowledge_tree_export,
        "validate_knowledge_tree_export",
        fail_validate_export,
    )
    monkeypatch.setattr(
        manifest_validator.validate_public_knowledge_tree_presentation,
        "validate_public_knowledge_tree_presentation",
        fail_validate_presentation,
    )
    monkeypatch.setattr(manifest_validator, "hash_file", fail_hash_file)

    report, exit_code = manifest_validator.validate_build_manifest_receipt(receipt)

    assert exit_code == manifest_validator.EXIT_PASS, report
    assert report["counts"]["accepted"] == 1


def test_build_manifest_validation_uses_export_report_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_root = tmp_path / "public-site"
    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180005Z",
        built_at="2026-06-02T18:00:05Z",
    )

    manifest_path = publish_root / "build-manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["output_root"] = "no-counts"
    export_payload = json.loads(export_fixture().read_text(encoding="utf-8"))
    export_path = export_fixture()
    seen_hash_paths: list[Path] = []

    def fake_load_json_object(_target: Path) -> tuple[dict[str, object], list[dict[str, object]], int]:
        return manifest_payload, [], manifest_validator.EXIT_PASS

    def fake_validate_export(_target: Path) -> tuple[dict[str, object], int]:
        return (
            {
                "validator": "knowledge_tree_export",
                "contract_version": "1",
                "target": str(export_fixture()),
                "status": "pass",
                "counts": {"inspected": 1, "accepted": 1, "rejected": 0, "deferred": 0},
                "errors": [],
                "warnings": [],
                "output_artifacts": {},
                "scenario": None,
                "payload": export_payload,
                "payload_sha256": manifest_payload["export_sha256"],
            },
            manifest_validator.EXIT_PASS,
        )

    def fake_hash_file(path: Path) -> str:
        seen_hash_paths.append(path)
        if path == export_path:
            raise AssertionError("build manifest validation should not hash the export again")
        return sha256_of(path)

    monkeypatch.setattr(manifest_validator, "load_json_object", fake_load_json_object)
    monkeypatch.setattr(
        manifest_validator.validate_knowledge_tree_export,
        "validate_knowledge_tree_export",
        fake_validate_export,
    )
    monkeypatch.setattr(manifest_validator, "hash_file", fake_hash_file)

    report, exit_code = manifest_validator.validate_build_manifest(manifest_path)

    assert exit_code == manifest_validator.EXIT_PASS, report
    assert report["counts"]["accepted"] == 1
    assert export_path not in seen_hash_paths


def test_build_manifest_payload_uses_precomputed_presentation_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    export_path = export_fixture()
    presentation_path = presentation_fixture()
    export_payload = json.loads(export_path.read_text(encoding="utf-8"))
    seen_hash_paths: list[Path] = []

    def fake_hash_file(path: Path) -> str:
        seen_hash_paths.append(path)
        if path == presentation_path:
            raise AssertionError("presentation path should not be rehashed when a receipt hash is supplied")
        if path == export_path:
            return "sha256:" + "1" * 64
        raise AssertionError(f"unexpected hash_file path: {path}")

    monkeypatch.setattr(builder, "hash_file", fake_hash_file)

    payload = builder.build_manifest_payload(
        export_path=export_path,
        presentation_path=presentation_path,
        publish_root=tmp_path / "public-site",
        build_id="build-20260602T180004Z",
        built_at="2026-06-02T18:00:04Z",
        export_payload=export_payload,
        page_records=[],
        asset_records=[],
        presentation_sha256="sha256:" + "2" * 64,
    )

    assert seen_hash_paths == [export_path]
    assert payload["presentation_sha256"] == "sha256:" + "2" * 64
    assert payload["export_sha256"] == "sha256:" + "1" * 64


def test_build_manifest_validator_rejects_windows_style_routes(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"
    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180000Z",
        built_at="2026-06-02T18:00:00Z",
    )
    manifest_path = publish_root / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages"][0]["route"] = "pages\\current.html"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report, exit_code = manifest_validator.validate_build_manifest(manifest_path)

    assert exit_code == manifest_validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "INVALID_ROUTE" for error in report["errors"])


def test_build_manifest_validator_rejects_non_html_routes(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"
    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180000Z",
        built_at="2026-06-02T18:00:00Z",
    )
    manifest_path = publish_root / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages"][0]["route"] = "pages/current.txt"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report, exit_code = manifest_validator.validate_build_manifest(manifest_path)

    assert exit_code == manifest_validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "INVALID_ROUTE" for error in report["errors"])


def test_build_manifest_validator_rejects_absolute_input_paths(tmp_path: Path) -> None:
    publish_root = tmp_path / "public-site"
    builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180000Z",
        built_at="2026-06-02T18:00:00Z",
    )
    manifest_path = publish_root / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["export_path"] = str(export_fixture().resolve())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report, exit_code = manifest_validator.validate_build_manifest(manifest_path)

    assert exit_code == manifest_validator.EXIT_VALIDATION_FAILED
    assert any(error["code"] == "INVALID_INPUT_PATH" for error in report["errors"])


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


def test_build_static_knowledge_tree_uses_manifest_receipt_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publish_root = tmp_path / "public-site"
    observed: dict[str, object] = {}

    def fake_validate_build_manifest_receipt(
        receipt: manifest_validator.BuildManifestReceipt,
    ) -> tuple[dict[str, object], int]:
        observed["receipt"] = receipt
        return (
            {"counts": {"inspected": 1, "accepted": 1, "rejected": 0, "deferred": 0}, "errors": [], "warnings": []},
            builder.EXIT_BUILD_MANIFEST_PASS,
        )

    def fail_validate_build_manifest(*_args: object, **_kwargs: object) -> tuple[dict[str, object], int]:
        raise AssertionError("path-based manifest validation should not be called by the builder")

    monkeypatch.setattr(builder, "validate_build_manifest_receipt", fake_validate_build_manifest_receipt)
    monkeypatch.setattr(builder, "validate_build_manifest", fail_validate_build_manifest)

    payload = builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root,
        build_id="build-20260602T180003Z",
        built_at="2026-06-02T18:00:03Z",
    )

    assert payload["status"] == "published"
    receipt = observed["receipt"]
    assert isinstance(receipt, builder.BuildManifestReceipt)
    assert receipt.manifest["build_id"] == "build-20260602T180003Z"
    assert receipt.export_sha256.startswith("sha256:")
    assert receipt.presentation_sha256.startswith("sha256:")


def test_publish_stage_dir_reports_backup_restored_on_failure(tmp_path: Path) -> None:
    stage_root = tmp_path / "stage-site"
    publish_root = tmp_path / "public-site"
    stage_root.mkdir()
    publish_root.mkdir()
    (publish_root / "index.html").write_text("old output", encoding="utf-8")
    (stage_root / "index.html").write_text("new output", encoding="utf-8")

    def fail_after_backup() -> None:
        raise RuntimeError("simulated publish failure")

    with pytest.raises(builder.StaticKnowledgeTreeBuildError) as exc_info:
        builder.publish_stage_dir(stage_root, publish_root, after_backup_hook=fail_after_backup)

    assert exc_info.value.report["backup_restored"] is True
    assert exc_info.value.report["backup_root"]
    assert (publish_root / "index.html").read_text(encoding="utf-8") == "old output"


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


def test_default_build_id_is_deterministic_from_inputs(tmp_path: Path) -> None:
    publish_root_a = tmp_path / "public-site-a"
    publish_root_b = tmp_path / "public-site-b"

    payload_a = builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root_a,
    )
    payload_b = builder.build_static_knowledge_tree(
        export_fixture(),
        presentation_fixture(),
        publish_root_b,
    )

    assert payload_a["build_id"] == payload_b["build_id"]
    assert payload_a["build_id"].startswith("build-")

    manifest_a = json.loads((publish_root_a / "build-manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((publish_root_b / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest_a["export_sha256"] == manifest_b["export_sha256"]
    assert manifest_a["presentation_sha256"] == manifest_b["presentation_sha256"]


def test_builder_cli_derives_deterministic_default_build_id_from_inputs(tmp_path: Path) -> None:
    publish_root_a = tmp_path / "public-site-a"
    publish_root_b = tmp_path / "public-site-b"

    first = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--export",
            str(export_fixture()),
            "--presentation",
            str(presentation_fixture()),
            "--publish-root",
            str(publish_root_a),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    second = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--export",
            str(export_fixture()),
            "--presentation",
            str(presentation_fixture()),
            "--publish-root",
            str(publish_root_b),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    payload_a = json.loads(first.stdout)
    payload_b = json.loads(second.stdout)
    assert payload_a["build_id"] == payload_b["build_id"]
    assert payload_a["build_id"].startswith("build-")
