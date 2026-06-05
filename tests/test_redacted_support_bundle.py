from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORT_BUILDER_PATH = REPO_ROOT / "tools" / "scripts" / "build_redacted_support_bundle.py"

support_builder_spec = importlib.util.spec_from_file_location(
    "redacted_support_bundle_builder_for_tests",
    SUPPORT_BUILDER_PATH,
)
support_builder = importlib.util.module_from_spec(support_builder_spec)
assert support_builder_spec.loader is not None
support_builder_spec.loader.exec_module(support_builder)


def test_redacted_support_bundle_rejects_unrecognized_output_on_overwrite(tmp_path: Path) -> None:
    output_dir = tmp_path / "support-bundle"
    output_dir.mkdir()
    (output_dir / "notes.txt").write_text("do-not-overwrite", encoding="utf-8")

    try:
        support_builder.build_bundle(REPO_ROOT, output_dir, overwrite=True)
    except support_builder.SupportBundleError as exc:
        assert "not a recognized support bundle" in str(exc)
    else:
        raise AssertionError("expected unrecognized output rejection")


def test_redacted_support_bundle_overwrites_valid_output_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "support-bundle"
    first_report = support_builder.build_bundle(REPO_ROOT, output_dir)

    second_report = support_builder.build_bundle(REPO_ROOT, output_dir, overwrite=True)

    assert second_report["status"] == "pass"
    assert first_report["manifest_path"] == str((output_dir / "manifest.json").resolve())
    assert second_report["manifest_path"] == str((output_dir / "manifest.json").resolve())
