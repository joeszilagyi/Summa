from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_PATH = REPO_ROOT / "tools" / "common" / "leak_scanner.py"
SCRIPT_PATH = REPO_ROOT / "tools" / "scripts" / "scan_for_leaks.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "leak_scanner"

common_spec = importlib.util.spec_from_file_location("leak_scanner_common_for_tests", COMMON_PATH)
assert common_spec is not None
scanner = importlib.util.module_from_spec(common_spec)
assert common_spec.loader is not None
sys.modules[common_spec.name] = scanner
common_spec.loader.exec_module(scanner)


def stage_fixture(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE_ROOT / name, target)
    return target


def test_public_bundle_leak_fixture_fails_with_machine_readable_findings(tmp_path: Path) -> None:
    root = stage_fixture(tmp_path, "public_bundle_leak")

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "fail"
    codes = {item["code"] for item in report["findings"]}
    assert {"SECRET_MARKER", "PRIVATE_PATH", "PROMPT_OUTPUT_MARKER", "RAW_PAYLOAD_MARKER", "PRIVATE_NOTE_MARKER"} <= codes


def test_public_bundle_scan_catches_adversarial_contexts(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "page.html").write_text(
        "<!-- api-key=abc123 -->\n"
        "<script>token=def456</script>\n"
        "<div>prompt_bundle_id</div>\n",
        encoding="utf-8",
    )
    (root / "notes.json").write_text(
        '{\n'
        '  "private_note": "BEGIN SECRET /home/joe/private/notes.txt",\n'
        '  "excerpt": "full_text raw_payload"\n'
        '}\n',
        encoding="utf-8",
    )
    (root / "summary.md").write_text(
        "Authorization: Bearer leaked-token\n"
        "Path: /Users/joe/private/summary.txt\n"
        "prompt_output raw_text\n",
        encoding="utf-8",
    )

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "fail"
    codes = {item["code"] for item in report["findings"]}
    assert {"SECRET_MARKER", "PRIVATE_PATH", "PROMPT_OUTPUT_MARKER", "RAW_PAYLOAD_MARKER", "PRIVATE_NOTE_MARKER"} <= codes


def test_clean_public_bundle_fixture_passes(tmp_path: Path) -> None:
    root = stage_fixture(tmp_path, "public_bundle_clean")

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "pass"
    assert report["findings"] == []


def test_scan_directory_honors_exclude_globs(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.html").write_text("<p>safe</p>\n", encoding="utf-8")
    (root / "build-manifest.json").write_text(
        '{"leak":"Authorization: Bearer excluded-token"}\n',
        encoding="utf-8",
    )

    report = scanner.scan_directory(
        root,
        profile="public_bundle",
        exclude_globs=("build-manifest.json",),
    )

    assert report["status"] == "pass"
    assert report["counts"]["files_scanned"] == 1
    assert report["findings"] == []


def test_scan_directory_counts_files_without_rescanning(tmp_path: Path, monkeypatch) -> None:
    root = stage_fixture(tmp_path, "public_bundle_clean")
    expected_files = sum(1 for path in root.rglob("*") if path.is_file())
    call_count = 0
    original_rglob = Path.rglob

    def wrapped_rglob(self: Path, pattern: str):
        nonlocal call_count
        if self == root and pattern == "*":
            call_count += 1
        return original_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", wrapped_rglob)

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["counts"]["files_scanned"] == expected_files
    assert call_count == 1


def test_scan_directory_streams_text_files_without_read_text(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "notes.txt").write_text("safe line\nAuthorization: Bearer leak\n", encoding="utf-8")

    def fail_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("scan_directory should stream text files instead of calling read_text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "fail"
    assert {finding["code"] for finding in report["findings"]} == {"SECRET_MARKER"}


def test_support_bundle_profile_disables_secret_and_private_path_scans(tmp_path: Path) -> None:
    root = tmp_path / "support-bundle"
    root.mkdir()
    (root / "notes.txt").write_text(
        "authorization: bearer token-123\n/private/path/should-not-flag\n",
        encoding="utf-8",
    )

    report = scanner.scan_directory(root, profile="support_bundle")

    assert report["status"] == "pass"
    assert report["findings"] == []
    assert report["counts"]["findings"] == 0


def test_allowlist_suppresses_known_false_positive_and_keeps_audit(tmp_path: Path) -> None:
    root = stage_fixture(tmp_path, "public_bundle_allowlisted")
    allowlist = root / "allowlist.json"

    payload = scanner.load_allowlist(allowlist)
    report = scanner.scan_directory(root / "bundle", profile="public_bundle", allowlist_payload=payload)

    assert report["status"] == "pass"
    assert report["findings"] == []
    assert report["counts"]["suppressed_findings"] == 1
    suppressed = report["suppressed_findings"][0]
    assert suppressed["allowlist_entry_id"] == "allow-doc-literal-token"
    assert suppressed["allowlist_approved_by"] == "operator.alex"


def test_leak_scanner_cli_writes_reports(tmp_path: Path) -> None:
    root = stage_fixture(tmp_path, "public_bundle_clean")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(root),
            "--profile",
            "public_bundle",
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

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["schema_version"] == "leak-scan-report.v1"
    assert report["status"] == "pass"
    assert "status=pass" in report_text.read_text(encoding="utf-8")
