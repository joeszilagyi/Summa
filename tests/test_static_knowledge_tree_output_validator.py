from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPO_ROOT / "tools" / "scripts" / "build_static_knowledge_tree.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_static_knowledge_tree_output.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "static_knowledge_tree_builder" / "valid_full" / "inputs"

builder_spec = importlib.util.spec_from_file_location("static_knowledge_tree_builder_for_output_tests", BUILDER_PATH)
assert builder_spec is not None
builder = importlib.util.module_from_spec(builder_spec)
assert builder_spec.loader is not None
builder_spec.loader.exec_module(builder)

validator_spec = importlib.util.spec_from_file_location("static_knowledge_tree_output_validator_for_tests", VALIDATOR_PATH)
assert validator_spec is not None
validator = importlib.util.module_from_spec(validator_spec)
assert validator_spec.loader is not None
validator_spec.loader.exec_module(validator)


def stage_fixture_inputs(tmp_path: Path) -> tuple[Path, Path]:
    staged_root = tmp_path / "inputs"
    shutil.copytree(FIXTURE_ROOT, staged_root)
    return staged_root / "knowledge_tree_export.json", staged_root / "public_presentation.json"


def build_site(tmp_path: Path) -> tuple[dict[str, object], Path, Path, Path]:
    export_path, presentation_path = stage_fixture_inputs(tmp_path)
    publish_root = tmp_path / "public-site"
    payload = builder.build_static_knowledge_tree(
        export_path,
        presentation_path,
        publish_root,
        build_id="build-20260602T190000Z",
        built_at="2026-06-02T19:00:00Z",
    )
    manifest_path = publish_root / "build-manifest.json"
    return payload, manifest_path, export_path, presentation_path


def test_valid_static_output_passes() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        payload, manifest_path, _, _ = build_site(Path(tmp))
        result, exit_code = validator.validate_static_knowledge_tree_output(manifest_path)

        assert payload["status"] == "published"
        assert exit_code == validator.EXIT_PASS
        assert result["errors"] == []


def test_default_validation_skips_page_link_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        _, manifest_path, _, _ = build_site(Path(tmp))

        def fail_feed(*args: object, **kwargs: object) -> None:  # pragma: no cover - failure path
            raise AssertionError("page-link parsing should be opt-in")

        monkeypatch.setattr(validator.LinkCollector, "feed", fail_feed)

        result, exit_code = validator.validate_static_knowledge_tree_output(manifest_path)

        assert exit_code == validator.EXIT_PASS
        assert result["errors"] == []


def test_broken_internal_link_is_detected() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        _, manifest_path, _, _ = build_site(Path(tmp))
        home_path = manifest_path.parent / "index.html"
        home_body = home_path.read_text(encoding="utf-8")
        home_path.write_text(home_body.replace("facets/records.html", "/facets/missing.html", 1), encoding="utf-8")

        result, exit_code = validator.validate_static_knowledge_tree_output(
            manifest_path,
            validate_page_links_enabled=True,
        )

        assert exit_code == validator.EXIT_VALIDATION_FAILED
        codes = [error["code"] for error in result["errors"]]
        assert "BROKEN_INTERNAL_LINK" in codes


def test_stale_input_hashes_are_detected() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        _, manifest_path, export_path, _ = build_site(Path(tmp))
        export_payload = json.loads(export_path.read_text(encoding="utf-8"))
        export_payload["input_sources"][0]["fingerprint"] = "sha256:" + ("2" * 64)
        export_path.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        result, exit_code = validator.validate_static_knowledge_tree_output(manifest_path)

        assert exit_code == validator.EXIT_VALIDATION_FAILED
        codes = [error["code"] for error in result["errors"]]
        assert "EXPORT_HASH_MISMATCH" in codes
        assert "STALE_INPUT_SOURCE_FINGERPRINT" in codes


def test_validator_cli_writes_machine_readable_reports(tmp_path: Path) -> None:
    _, manifest_path, _, _ = build_site(tmp_path)
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(manifest_path),
            "--scenario",
            "valid_static_output",
            "--target-id",
            "public-site/build-manifest.json",
            "--report-json",
            str(report_json),
            "--report-text",
            str(report_text),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == validator.EXIT_PASS, proc.stdout + proc.stderr
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["validator"] == "static_knowledge_tree_output"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
