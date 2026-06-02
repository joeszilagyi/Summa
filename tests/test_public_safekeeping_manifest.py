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
SAFEKEEPING_BUILDER_PATH = REPO_ROOT / "tools" / "scripts" / "build_public_safekeeping_manifest.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_public_safekeeping_manifest.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "static_knowledge_tree_builder" / "valid_full" / "inputs"

static_builder_spec = importlib.util.spec_from_file_location("static_knowledge_tree_builder_for_safekeeping_tests", STATIC_BUILDER_PATH)
static_builder = importlib.util.module_from_spec(static_builder_spec)
assert static_builder_spec.loader is not None
static_builder_spec.loader.exec_module(static_builder)

sharing_builder_spec = importlib.util.spec_from_file_location("public_sharing_bundle_builder_for_safekeeping_tests", SHARING_BUILDER_PATH)
sharing_builder = importlib.util.module_from_spec(sharing_builder_spec)
assert sharing_builder_spec.loader is not None
sharing_builder_spec.loader.exec_module(sharing_builder)

safekeeping_builder_spec = importlib.util.spec_from_file_location("public_safekeeping_manifest_builder_for_tests", SAFEKEEPING_BUILDER_PATH)
safekeeping_builder = importlib.util.module_from_spec(safekeeping_builder_spec)
assert safekeeping_builder_spec.loader is not None
safekeeping_builder_spec.loader.exec_module(safekeeping_builder)

validator_spec = importlib.util.spec_from_file_location("public_safekeeping_manifest_validator_for_tests", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(validator_spec)
assert validator_spec.loader is not None
validator_spec.loader.exec_module(validator)


def stage_fixture_inputs(tmp_path: Path) -> tuple[Path, Path]:
    staged_root = tmp_path / "inputs"
    shutil.copytree(FIXTURE_ROOT, staged_root)
    return staged_root / "knowledge_tree_export.json", staged_root / "public_presentation.json"


def build_public_bundle(tmp_path: Path) -> Path:
    export_path, presentation_path = stage_fixture_inputs(tmp_path)
    publish_root = tmp_path / "public-site"
    static_builder.build_static_knowledge_tree(
        export_path,
        presentation_path,
        publish_root,
        build_id="build-20260602T210000Z",
        built_at="2026-06-02T21:00:00Z",
    )
    output_dir = tmp_path / "sharing-bundle"
    sharing_builder.build_bundle(
        publish_root / "build-manifest.json",
        output_dir,
        generated_at="2026-06-02T21:01:00Z",
    )
    return output_dir


def test_public_safekeeping_manifest_builder_emits_valid_manifest(tmp_path: Path) -> None:
    bundle_dir = build_public_bundle(tmp_path)

    report = safekeeping_builder.build_safekeeping_manifest(
        bundle_dir,
        generated_at="2026-06-02T21:02:00Z",
    )

    assert report["status"] == "pass"
    assert report["upload_attempted"] is False
    manifest_path = bundle_dir / "safekeeping-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "public-safekeeping-manifest.v1"
    assert manifest["upload_attempted"] is False
    assert {item["rights_posture"] for item in manifest["artifacts"]}.issuperset({"public_safe", "metadata_only"})
    assert "git_handoff" in manifest["preservation_targets"]
    assert "archive_export" in manifest["preservation_targets"]

    result, exit_code = validator.validate_public_safekeeping_manifest(manifest_path)
    assert exit_code == validator.EXIT_PASS, result


def test_public_safekeeping_manifest_validator_detects_hash_drift(tmp_path: Path) -> None:
    bundle_dir = build_public_bundle(tmp_path)
    safekeeping_builder.build_safekeeping_manifest(
        bundle_dir,
        generated_at="2026-06-02T21:03:00Z",
    )
    page_path = bundle_dir / "site" / "index.html"
    page_path.write_text(page_path.read_text(encoding="utf-8") + "\n<!-- drift -->\n", encoding="utf-8")

    result, exit_code = validator.validate_public_safekeeping_manifest(bundle_dir / "safekeeping-manifest.json")

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    codes = [error["code"] for error in result["errors"]]
    assert "ARTIFACT_HASH_MISMATCH" in codes
    assert "ARTIFACT_SIZE_MISMATCH" in codes


def test_public_safekeeping_manifest_validator_rejects_upload_attempted(tmp_path: Path) -> None:
    bundle_dir = build_public_bundle(tmp_path)
    safekeeping_builder.build_safekeeping_manifest(
        bundle_dir,
        generated_at="2026-06-02T21:04:00Z",
    )
    manifest_path = bundle_dir / "safekeeping-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["upload_attempted"] = True
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result, exit_code = validator.validate_public_safekeeping_manifest(manifest_path)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert [error["code"] for error in result["errors"]] == ["UPLOAD_ATTEMPTED_FORBIDDEN"]


def test_public_safekeeping_manifest_cli_emits_json_report(tmp_path: Path) -> None:
    bundle_dir = build_public_bundle(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            str(SAFEKEEPING_BUILDER_PATH),
            "--bundle-dir",
            str(bundle_dir),
            "--generated-at",
            "2026-06-02T21:05:00Z",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "public-safekeeping-manifest-report.v1"
    assert payload["upload_attempted"] is False
