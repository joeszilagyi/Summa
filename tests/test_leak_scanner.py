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


def test_scan_directory_scans_common_text_files_without_known_suffixes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    leak_by_name = {
        "data.jsonl": "Authorization: Bearer jsonl-leak\n",
        "config.yaml": "token=yaml-leak\n",
        "config.yml": "secret=yml-leak\n",
        "rows.csv": "private key csv-leak\n",
        "data.xml": "<note>private_note</note>\n",
        ".env": "api_key=env-leak\n",
        "README": "/home/joe/private/extensionless.txt\n",
        "run.sh": "raw_payload shell-leak\n",
        "key.pem": "Authorization: Bearer pem-leak\n",
        "drawing.svg": "<text>Authorization: Bearer svg-leak</text>\n",
    }
    for name, body in leak_by_name.items():
        (root / name).write_text(body, encoding="utf-8")

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "fail"
    assert {finding["path"] for finding in report["findings"]} == set(leak_by_name)


def test_scan_directory_reports_provider_and_opaque_secret_shapes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    secrets = [
        "ghp_1234567890abcdefghijkl",
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-value-123456789",
        "opaque=aB3dE7gH9jK2mN5pQ8rT4vW6xY1zC0f",
    ]
    (root / "secrets.txt").write_text("\n".join(secrets) + "\n", encoding="utf-8")

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "fail"
    assert len(report["findings"]) == len(secrets)
    assert {finding["code"] for finding in report["findings"]} == {"SECRET_MARKER"}


def test_scan_directory_skips_binary_files_after_text_sniff(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00Authorization: Bearer binary-leak")
    (root / "unknown.data").write_bytes(b"\xff\xfe\xfd\x00binary")

    report = scanner.scan_directory(root, profile="public_bundle")

    assert report["status"] == "pass"
    assert report["findings"] == []


def test_support_bundle_profile_scans_all_leak_categories(tmp_path: Path) -> None:
    root = tmp_path / "support-bundle"
    root.mkdir()
    (root / "logs").mkdir()
    (root / "notes.txt").write_text(
        "authorization: bearer token-123\n"
        "/home/joe/private/path\n"
        "prompt_output raw_text private_note operator_excerpt_text\n",
        encoding="utf-8",
    )
    (root / "logs" / "runtime.log").write_text("safe log content\n", encoding="utf-8")

    report = scanner.scan_directory(root, profile="support_bundle")

    assert report["status"] == "fail"
    codes = {item["code"] for item in report["findings"]}
    assert {
        "SECRET_MARKER",
        "PRIVATE_PATH",
        "RUNTIME_LOG_PATH",
        "PROMPT_OUTPUT_MARKER",
        "RAW_PAYLOAD_MARKER",
        "PRIVATE_NOTE_MARKER",
        "RESTRICTED_EVIDENCE_MARKER",
    } <= codes


def test_allowlist_suppresses_known_false_positive_and_keeps_entry_id(tmp_path: Path) -> None:
    root = stage_fixture(tmp_path, "public_bundle_allowlisted")
    allowlist = root / "allowlist.json"

    payload = scanner.load_allowlist(allowlist)
    report = scanner.scan_directory(root / "bundle", profile="public_bundle", allowlist_payload=payload)

    assert report["status"] == "pass"
    assert report["findings"] == []
    assert report["counts"]["suppressed_findings"] == 1
    suppressed = report["suppressed_findings"][0]
    assert suppressed["allowlist_entry_id"] == "allow-doc-literal-token"
    assert "allowlist_reason" not in suppressed
    assert "allowlist_approved_by" not in suppressed


def test_allowlist_fingerprint_does_not_suppress_other_matching_markers(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.html").write_text("token=approved-example token=unapproved-example\n", encoding="utf-8")

    raw_report = scanner.scan_directory(root, profile="public_bundle")
    assert len(raw_report["findings"]) == 2
    allowlist = {
        "schema_version": scanner.ALLOWLIST_SCHEMA_VERSION,
        "entries": [
            {
                "entry_id": "allow-first-token-marker",
                "finding_fingerprint": raw_report["findings"][0]["finding_fingerprint"],
                "reason": "The first documentation marker is an intentional example.",
                "approved_by": "operator.alex",
                "expires_at": "2099-12-31T23:59:59Z",
            }
        ],
    }

    report = scanner.scan_directory(root, profile="public_bundle", allowlist_payload=allowlist)

    assert report["status"] == "fail"
    assert report["counts"]["suppressed_findings"] == 1
    assert report["findings"][0]["line"] == 1
    assert report["findings"][0]["column"] > report["suppressed_findings"][0]["column"]


def test_allowlist_fingerprint_changes_when_source_context_changes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    source = root / "index.html"
    source.write_text("token=approved-example\n", encoding="utf-8")
    approved_finding = scanner.scan_directory(root, profile="public_bundle")["findings"][0]
    allowlist = {
        "schema_version": scanner.ALLOWLIST_SCHEMA_VERSION,
        "entries": [
            {
                "entry_id": "allow-original-token-context",
                "finding_fingerprint": approved_finding["finding_fingerprint"],
                "reason": "The original documentation marker is an intentional example.",
                "approved_by": "operator.alex",
                "expires_at": "2099-12-31T23:59:59Z",
            }
        ],
    }
    source.write_text("token=changed-example\n", encoding="utf-8")

    report = scanner.scan_directory(root, profile="public_bundle", allowlist_payload=allowlist)

    assert report["status"] == "fail"
    assert report["findings"]
    assert report["suppressed_findings"] == []


def test_expired_allowlist_fingerprint_does_not_suppress_finding(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.html").write_text("token=expired-example\n", encoding="utf-8")
    raw_report = scanner.scan_directory(root, profile="public_bundle")
    finding = raw_report["findings"][0]
    allowlist = {
        "schema_version": scanner.ALLOWLIST_SCHEMA_VERSION,
        "entries": [
            {
                "entry_id": "expired-token-marker",
                "finding_fingerprint": finding["finding_fingerprint"],
                "reason": "This approval is no longer current.",
                "approved_by": "operator.alex",
                "expires_at": "2020-01-01T00:00:00Z",
            }
        ],
    }

    report = scanner.scan_directory(root, profile="public_bundle", allowlist_payload=allowlist)

    assert report["status"] == "fail"
    assert report["findings"] == [finding]
    assert report["suppressed_findings"] == []


def test_allowlist_audit_redacts_entries_by_default_and_supports_private_debug(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.html").write_text("token=approved-example\n", encoding="utf-8")
    raw_report = scanner.scan_directory(root, profile="public_bundle")
    allowlist_entry = {
        "entry_id": "private-audit-entry",
        "finding_fingerprint": raw_report["findings"][0]["finding_fingerprint"],
        "reason": "private reviewer context must not be emitted by normal reports",
        "approved_by": "reviewer.private@example",
        "expires_at": "2099-12-31T23:59:59Z",
    }
    allowlist = {
        "schema_version": scanner.ALLOWLIST_SCHEMA_VERSION,
        "entries": [allowlist_entry],
    }

    report = scanner.scan_directory(root, profile="public_bundle", allowlist_payload=allowlist)

    assert report["allowlist_audit"] == {
        "schema_version": scanner.ALLOWLIST_SCHEMA_VERSION,
        "entry_ids": ["private-audit-entry"],
    }
    serialized_report = json.dumps(report)
    assert allowlist_entry["reason"] not in serialized_report
    assert allowlist_entry["approved_by"] not in serialized_report

    debug_report = scanner.scan_directory(
        root,
        profile="public_bundle",
        allowlist_payload=allowlist,
        include_allowlist_audit=True,
    )

    assert debug_report["allowlist_audit"]["entries"] == [allowlist_entry]
    assert (
        debug_report["suppressed_findings"][0]["allowlist_reason"]
        == allowlist_entry["reason"]
    )
    assert (
        debug_report["suppressed_findings"][0]["allowlist_approved_by"]
        == allowlist_entry["approved_by"]
    )


def test_leak_scanner_cli_writes_reports(tmp_path: Path) -> None:
    root = stage_fixture(tmp_path, "public_bundle_clean")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(root),
            "--report-root",
            str(tmp_path),
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
