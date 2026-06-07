from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_BUILDER_PATH = REPO_ROOT / "tools" / "scripts" / "build_static_knowledge_tree.py"
SHARING_BUILDER_PATH = REPO_ROOT / "tools" / "scripts" / "build_public_sharing_bundle.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "static_knowledge_tree_builder" / "valid_full" / "inputs"

static_builder_spec = importlib.util.spec_from_file_location("static_knowledge_tree_builder_for_public_bundle_tests", STATIC_BUILDER_PATH)
assert static_builder_spec is not None
static_builder = importlib.util.module_from_spec(static_builder_spec)
assert static_builder_spec.loader is not None
static_builder_spec.loader.exec_module(static_builder)

sharing_builder_spec = importlib.util.spec_from_file_location("public_sharing_bundle_builder_for_tests", SHARING_BUILDER_PATH)
assert sharing_builder_spec is not None
sharing_builder = importlib.util.module_from_spec(sharing_builder_spec)
assert sharing_builder_spec.loader is not None
sharing_builder_spec.loader.exec_module(sharing_builder)


def stage_fixture_inputs(tmp_path: Path) -> tuple[Path, Path]:
    staged_root = tmp_path / "inputs"
    shutil.copytree(FIXTURE_ROOT, staged_root)
    return staged_root / "knowledge_tree_export.json", staged_root / "public_presentation.json"


def build_site(
    tmp_path: Path,
    *,
    mutate_export: str | None = None,
    extra_export_fields: dict[str, object] | None = None,
) -> Path:
    export_path, presentation_path = stage_fixture_inputs(tmp_path)
    if mutate_export is not None:
        export_payload = json.loads(export_path.read_text(encoding="utf-8"))
        export_payload["pages"][0]["sections"][0]["paragraphs"] = [mutate_export]
        if extra_export_fields:
            export_payload["pages"][0]["authority_basis"] = {
                "content_class": "metadata_only",
                "review_queue_refs": [],
                "field_review_entries": [],
                "metadata_exception_reason": json.dumps(
                    extra_export_fields, ensure_ascii=False, indent=2, sort_keys=True
                ),
            }
        export_path.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif extra_export_fields:
        export_payload = json.loads(export_path.read_text(encoding="utf-8"))
        export_payload["pages"][0]["authority_basis"] = {
            "content_class": "metadata_only",
            "review_queue_refs": [],
            "field_review_entries": [],
            "metadata_exception_reason": json.dumps(extra_export_fields, ensure_ascii=False, indent=2, sort_keys=True),
        }
        export_path.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    publish_root = tmp_path / "public-site"
    static_builder.build_static_knowledge_tree(
        export_path,
        presentation_path,
        publish_root,
        build_id="build-20260602T200000Z",
        built_at="2026-06-02T20:00:00Z",
    )
    return publish_root / "build-manifest.json"


def test_public_sharing_bundle_builder_emits_manifest_and_no_upload(tmp_path: Path) -> None:
    build_manifest = build_site(tmp_path)
    output_dir = tmp_path / "sharing-bundle"

    report = sharing_builder.build_bundle(
        build_manifest,
        output_dir,
        generated_at="2026-06-02T20:01:00Z",
    )

    assert report["status"] == "pass"
    assert report["upload_attempted"] is False
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "public-sharing-bundle.v1"
    assert manifest["upload_attempted"] is False
    assert manifest["distribution_mode"] == "manual_local_handoff_only"
    assert manifest["red_team_gate"]["status"] == "pass"
    assert {item["family"] for item in manifest["included_artifacts"]}.issuperset({"page", "asset", "export_summary", "presentation_summary"})
    assert {item["family"] for item in manifest["excluded_families"]}.issuperset(
        {"raw_build_manifest", "raw_payloads", "prompt_outputs", "runtime_logs", "private_paths", "restricted_text"}
    )
    assert not (output_dir / "site" / "build-manifest.json").exists()
    assert (output_dir / "site" / "index.html").is_file()
    assert (output_dir / "metadata" / "export-summary.json").is_file()
    assert (output_dir / "metadata" / "presentation-summary.json").is_file()


def test_public_sharing_bundle_builder_rejects_unrecognized_output_on_overwrite(tmp_path: Path) -> None:
    build_manifest = build_site(tmp_path)
    output_dir = tmp_path / "sharing-bundle"
    output_dir.mkdir()
    (output_dir / "notes.txt").write_text("do-not-overwrite", encoding="utf-8")

    try:
        sharing_builder.build_bundle(build_manifest, output_dir, overwrite=True, generated_at="2026-06-02T20:01:30Z")
    except sharing_builder.PublicSharingBundleError as exc:
        assert "not a recognized public sharing bundle" in str(exc)
    else:
        raise AssertionError("expected unrecognized output rejection")


def test_public_sharing_bundle_builder_overwrites_valid_output_directory(tmp_path: Path) -> None:
    build_manifest = build_site(tmp_path)
    output_dir = tmp_path / "sharing-bundle"
    first_report = sharing_builder.build_bundle(build_manifest, output_dir, generated_at="2026-06-02T20:01:00Z")

    second_report = sharing_builder.build_bundle(
        build_manifest,
        output_dir,
        overwrite=True,
        generated_at="2026-06-02T20:01:31Z",
    )

    assert second_report["status"] == "pass"
    assert second_report["manifest_path"] == str((output_dir / "manifest.json").resolve())
    assert first_report["manifest_path"] == str((output_dir / "manifest.json").resolve())


def test_public_sharing_bundle_builder_blocks_known_leak_fixtures(tmp_path: Path) -> None:
    build_manifest = build_site(
        tmp_path,
        mutate_export="api_key=abc123 /home/joe/private/leak.txt prompt_output",
    )

    try:
        sharing_builder.build_bundle(build_manifest, tmp_path / "sharing-bundle", generated_at="2026-06-02T20:02:00Z")
    except sharing_builder.PublicSharingBundleError as exc:
        message = str(exc)
        assert "public sharing red-team gate failed" in message
        assert "SECRET_MARKER" in message or "PRIVATE_PATH" in message or "PROMPT_OUTPUT_MARKER" in message
    else:
        raise AssertionError("expected leak gate failure")


def test_public_sharing_bundle_builder_blocks_restricted_text_marker(tmp_path: Path) -> None:
    build_manifest = build_site(tmp_path, mutate_export="full_extracted_text")

    try:
        sharing_builder.build_bundle(build_manifest, tmp_path / "sharing-bundle", generated_at="2026-06-02T20:03:00Z")
    except sharing_builder.PublicSharingBundleError as exc:
        assert "RAW_PAYLOAD_MARKER" in str(exc)
    else:
        raise AssertionError("expected restricted text gate failure")


def test_public_sharing_bundle_builder_excludes_private_fields_from_export_metadata(
    tmp_path: Path,
) -> None:
    build_manifest = build_site(
        tmp_path,
        extra_export_fields={
            "title": "Private title /home/joe/private/title.txt",
            "description": "BEGIN SECRET ignore previous instructions",
            "source_locator": "https://example.test/path?token=abc123",
            "source_metadata": {"operator_note": "raw operator note", "local_path": "/home/joe/private/meta.txt"},
            "failure_message": "rm -rf /",
            "exception_text": "traceback /home/joe/private/trace.txt",
            "raw_extracted_text": "full extracted text",
        },
    )

    report = sharing_builder.build_bundle(
        build_manifest,
        tmp_path / "sharing-bundle",
        generated_at="2026-06-02T20:03:30Z",
    )

    assert report["status"] == "pass"
    bundle_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((tmp_path / "sharing-bundle").rglob("*")) if path.is_file()
    )
    for private_literal in (
        "Private title",
        "BEGIN SECRET",
        "ignore previous instructions",
        "/home/joe/private/title.txt",
        "/home/joe/private/meta.txt",
        "token=abc123",
        "full extracted text",
        "raw operator note",
        "rm -rf /",
    ):
        assert private_literal not in bundle_text


def test_public_sharing_bundle_cli_emits_json_report(tmp_path: Path) -> None:
    build_manifest = build_site(tmp_path)
    output_dir = tmp_path / "sharing-bundle"

    proc = subprocess.run(
        [
            sys.executable,
            str(SHARING_BUILDER_PATH),
            "--build-manifest",
            str(build_manifest),
            "--output-dir",
            str(output_dir),
            "--generated-at",
            "2026-06-02T20:04:00Z",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "public-sharing-bundle-report.v1"
    assert payload["upload_attempted"] is False


def test_public_sharing_bundle_recover_stale_backup(tmp_path: Path) -> None:
    output_dir = tmp_path / "sharing-bundle"
    backup_root = sharing_builder.backup_root_path(output_dir)
    journal_path = sharing_builder.backup_journal_path(output_dir)

    backup_root.mkdir()
    (backup_root / "manifest.json").write_text('{"schema_version": "public-sharing-bundle.v1"}\n', encoding="utf-8")
    journal_path.write_text(
        json.dumps(
            {
                "version": "1",
                "mode": "public-sharing-bundle",
                "output_dir": str(output_dir),
                "state": "pending",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    sharing_builder.recover_stale_backup(output_dir)

    assert output_dir.is_dir()
    assert (output_dir / "manifest.json").exists()
    assert not backup_root.exists()
    assert not journal_path.exists()
