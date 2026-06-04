from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "export_standards_profile.py"
FIXED_TIMESTAMP = "2026-06-04T09:00:00Z"
PRIVATE_SENTINEL = "PRIVATE_SENTINEL_DO_NOT_EXPORT"

sys.path.insert(0, str(Path(__file__).parent))
from test_standards_profile_crosswalks import build_fixture_store  # noqa: E402


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_help_exits_zero() -> None:
    proc = run_cli("--help")

    assert proc.returncode == 0
    assert "standards-profile export" in proc.stdout


def test_cli_exports_dcmi_by_work_id(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    output = tmp_path / "dcmi.json"
    report = tmp_path / "dcmi-report.json"

    proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "dcmi.v1",
        "--work-id",
        str(fixture["work_id"]),
        "--output",
        str(output),
        "--conformance-report",
        str(report),
        "--generated-at",
        FIXED_TIMESTAMP,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    conformance = json.loads(report.read_text(encoding="utf-8"))
    assert payload["profile_id"] == "dcmi.v1"
    assert payload["records"][0]["metadata"]["dcterms:title"] == "Public standards fixture work"
    assert conformance["export_artifact_hash"]


def test_cli_exports_premis_by_capture_id(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    output = tmp_path / "premis.json"

    proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "premis.v1",
        "--capture-id",
        str(fixture["capture_id"]),
        "--output",
        str(output),
        "--generated-at",
        FIXED_TIMESTAMP,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["premis"]["objects"][0]["fixity"]["message_digest"] == "sha256:abc123"


def test_cli_exports_rico_by_subject_id(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    output = tmp_path / "rico.json"

    proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "rico.v1",
        "--subject-id",
        "standards_subject",
        "--base-uri",
        "https://example.org/summa/",
        "--output",
        str(output),
        "--generated-at",
        FIXED_TIMESTAMP,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["rico_profile_json"]["nodes"]
    assert payload["rico_profile_json"]["nodes"][0]["id"].startswith("https://example.org/summa/")


def test_cli_exports_nara_readiness_by_subject_id(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    output = tmp_path / "nara.json"

    proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "nara_preservation_readiness.v1",
        "--subject-id",
        "standards_subject",
        "--output",
        str(output),
        "--generated-at",
        FIXED_TIMESTAMP,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    checks = {item["check_id"]: item["status"] for item in payload["readiness_report"]["checks"]}
    assert checks["fixity_present"] == "pass"
    assert checks["transfer_package_present"] == "not_applicable"


def test_cli_invalid_profile_id_fails(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "ead.v1",
        "--output",
        str(tmp_path / "out.json"),
    )

    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr


def test_cli_private_export_requires_explicit_flag(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    public_output = tmp_path / "public.json"
    private_output = tmp_path / "private.json"

    public_proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "dcmi.v1",
        "--subject-id",
        "standards_subject",
        "--output",
        str(public_output),
        "--generated-at",
        FIXED_TIMESTAMP,
    )
    private_proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "dcmi.v1",
        "--subject-id",
        "standards_subject",
        "--include-private",
        "--output",
        str(private_output),
        "--generated-at",
        FIXED_TIMESTAMP,
    )

    assert public_proc.returncode == 0, public_proc.stderr
    assert private_proc.returncode == 0, private_proc.stderr
    assert PRIVATE_SENTINEL not in public_output.read_text(encoding="utf-8")
    assert PRIVATE_SENTINEL in private_output.read_text(encoding="utf-8")


def test_cli_rico_requires_valid_base_uri(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)

    proc = run_cli(
        "--db",
        str(fixture["db_path"]),
        "--profile",
        "rico.v1",
        "--subject-id",
        "standards_subject",
        "--output",
        str(tmp_path / "rico.json"),
    )

    assert proc.returncode == 2
    assert "base-uri" in proc.stderr
