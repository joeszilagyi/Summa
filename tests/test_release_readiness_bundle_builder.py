from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "scripts" / "build_release_readiness_bundle.py"
WRAPPER_PATH = REPO_ROOT / "tools" / "scripts" / "Index_Build_Release_Readiness_Bundle.sh"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "release_readiness"

spec = importlib.util.spec_from_file_location(
    "release_readiness_bundle_builder_for_tests", SCRIPT_PATH
)
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


def fixture_inputs(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs"


def report_args(root: Path) -> list[str]:
    return [
        "--doctor-report",
        str(root / "doctor-report.json"),
        "--knowledge-tree-export-report",
        str(root / "knowledge-tree-export-validator-report.json"),
        "--static-output-report",
        str(root / "static-output-validator-report.json"),
        "--local-search-projection-report",
        str(root / "local-search-projection-validator-report.json"),
        "--leak-scan-report",
        str(root / "leak-scan-report.json"),
    ]


def run_builder(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_cli_help_exits_zero() -> None:
    proc = run_builder(["--help"])

    assert proc.returncode == 0
    assert "release-readiness" in proc.stdout
    assert "--output-dir" in proc.stdout


def test_wrapper_help_exits_zero() -> None:
    proc = subprocess.run(
        [str(WRAPPER_PATH), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "release-readiness" in proc.stdout
    assert "--output-dir" in proc.stdout


def test_collect_mode_stages_reports_and_runs_final_validator(tmp_path: Path) -> None:
    output_dir = tmp_path / "bundle"
    proc = run_builder(
        [
            "--mode",
            "collect",
            "--output-dir",
            str(output_dir),
            "--generated-at",
            "2026-06-04T12:00:00Z",
            "--run-id",
            "fixture-run",
            *report_args(fixture_inputs("pass")),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    for filename in builder.REQUIRED_REPORTS.values():
        assert (output_dir / filename).is_file()
    final_report = load_json(output_dir / "release-readiness-report.json")
    assert final_report["schema_version"] == "release-readiness-report.v1"
    assert final_report["status"] == "pass"
    manifest = load_json(output_dir / "release-readiness-bundle-manifest.json")
    assert manifest["schema_version"] == "release-readiness-bundle.v1"
    assert manifest["mode"] == "collect"
    assert len(manifest["staged_reports"]) == 5
    assert all(item["sha256"].startswith("sha256:") for item in manifest["staged_reports"])
    assert manifest["final_release_readiness"]["status"] == "pass"


def test_missing_report_fails_with_actionable_error(tmp_path: Path) -> None:
    output_dir = tmp_path / "bundle"
    args = report_args(fixture_inputs("pass"))
    missing_index = args.index("--leak-scan-report")
    del args[missing_index : missing_index + 2]

    proc = run_builder(["--mode", "collect", "--output-dir", str(output_dir), *args])

    assert proc.returncode != 0
    assert "--leak-scan-report is required in collect mode" in proc.stderr
    manifest = load_json(output_dir / "release-readiness-bundle-manifest.json")
    assert "leak-scan-report" in manifest["errors"][0]


def test_non_json_report_fails_before_success(tmp_path: Path) -> None:
    source = tmp_path / "reports"
    shutil.copytree(fixture_inputs("pass"), source)
    (source / "doctor-report.json").write_text("not json\n", encoding="utf-8")

    proc = run_builder(
        ["--mode", "collect", "--output-dir", str(tmp_path / "bundle"), *report_args(source)]
    )

    assert proc.returncode != 0
    assert "doctor report is not readable JSON" in proc.stderr


def test_final_validator_failure_propagates_in_strict_mode(tmp_path: Path) -> None:
    output_dir = tmp_path / "bundle"

    proc = run_builder(
        [
            "--mode",
            "collect",
            "--output-dir",
            str(output_dir),
            *report_args(fixture_inputs("block")),
        ]
    )

    assert proc.returncode == 1
    final_report = load_json(output_dir / "release-readiness-report.json")
    assert final_report["status"] == "block"
    manifest = load_json(output_dir / "release-readiness-bundle-manifest.json")
    assert manifest["final_release_readiness"]["status"] == "block"


def test_graph_closure_strict_report_blocks_release_readiness(tmp_path: Path) -> None:
    output_dir = tmp_path / "bundle"
    graph_report = tmp_path / "graph-closure-report.json"
    graph_report.write_text(
        json.dumps(
            {
                "schema_version": "canonical-graph-closure-report.v1",
                "status": "fail",
                "summary": {
                    "true_orphan_error_count": 1,
                    "unresolved_tracked_count": 0,
                    "repairable_count": 0,
                    "quarantined_count": 0,
                    "issue_count": 1,
                    "audited_row_count": 1,
                    "intentionally_exempt_count": 0,
                },
                "issues": [
                    {
                        "table": "source_claim",
                        "primary_key": "1",
                        "status": "true_orphan_error",
                        "severity": "fail",
                        "code": "SOURCE_CLAIM_TRUE_ORPHAN",
                        "message": "orphan",
                        "attachment_policy": "test",
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = run_builder(
        [
            "--mode",
            "collect",
            "--output-dir",
            str(output_dir),
            "--graph-closure-report",
            str(graph_report),
            "--graph-closure-strict",
            *report_args(fixture_inputs("pass")),
        ]
    )

    assert proc.returncode == 1
    assert (output_dir / "graph-closure-report.json").is_file()
    final_report = load_json(output_dir / "release-readiness-report.json")
    manifest = load_json(output_dir / "release-readiness-bundle-manifest.json")
    assert final_report["status"] == "block"
    assert any(check["check_key"] == "graph_closure" for check in final_report["checks"])
    assert manifest["optional_reports"][0]["key"] == "graph_closure"


def test_report_only_records_failure_but_exits_zero(tmp_path: Path) -> None:
    output_dir = tmp_path / "bundle"

    proc = run_builder(
        [
            "--mode",
            "collect",
            "--report-only",
            "--output-dir",
            str(output_dir),
            *report_args(fixture_inputs("block")),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert load_json(output_dir / "release-readiness-report.json")["status"] == "block"


def test_output_directory_refuses_overwrite_without_force(tmp_path: Path) -> None:
    output_dir = tmp_path / "bundle"
    output_dir.mkdir()
    (output_dir / "existing.txt").write_text("keep\n", encoding="utf-8")

    proc = run_builder(
        ["--mode", "collect", "--output-dir", str(output_dir), *report_args(fixture_inputs("pass"))]
    )

    assert proc.returncode != 0
    assert "output directory already exists" in proc.stderr


def test_collect_manifest_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    common = [
        "--mode",
        "collect",
        "--generated-at",
        "2026-06-04T12:00:00Z",
        "--run-id",
        "deterministic",
        *report_args(fixture_inputs("pass")),
    ]

    assert run_builder(["--output-dir", str(first), *common]).returncode == 0
    assert run_builder(["--output-dir", str(second), *common]).returncode == 0
    first_manifest = load_json(first / "release-readiness-bundle-manifest.json")
    second_manifest = load_json(second / "release-readiness-bundle-manifest.json")
    first_hashes = [item["sha256"] for item in first_manifest["staged_reports"]]
    second_hashes = [item["sha256"] for item in second_manifest["staged_reports"]]

    assert first_hashes == second_hashes
    assert first_manifest["generated_at"] == second_manifest["generated_at"]
    assert first_manifest["run_id"] == second_manifest["run_id"]
    assert (
        first_manifest["final_release_readiness"]["status"]
        == second_manifest["final_release_readiness"]["status"]
    )


def test_run_mode_can_generate_reports_with_stubbed_upstream_tools(
    tmp_path: Path, monkeypatch: Any
) -> None:
    output_dir = tmp_path / "bundle"

    def fake_generated_report_command(
        key: str, args: argparse.Namespace, destination: Path
    ) -> list[str]:
        return ["fake-tool", key, str(destination)]

    def fake_run_command(
        command: list[str], *, report_path: Path, label: str
    ) -> tuple[dict[str, Any], int]:
        source = fixture_inputs("pass") / report_path.name
        shutil.copyfile(source, report_path)
        return load_json(report_path), 0

    monkeypatch.setattr(builder, "generated_report_command", fake_generated_report_command)
    monkeypatch.setattr(builder, "run_command", fake_run_command)
    args = builder.parse_args(["--mode", "run", "--output-dir", str(output_dir)])
    manifest, exit_code = builder.build_release_readiness_bundle(args)

    assert exit_code == 0
    assert manifest["mode"] == "run"
    assert {item["source_kind"] for item in manifest["staged_reports"]} == {"generated"}
    assert load_json(output_dir / "release-readiness-report.json")["status"] == "pass"


def test_mixed_mode_collects_explicit_report_and_generates_missing_reports(
    tmp_path: Path, monkeypatch: Any
) -> None:
    output_dir = tmp_path / "bundle"

    def fake_generated_report_command(
        key: str, args: argparse.Namespace, destination: Path
    ) -> list[str]:
        return ["fake-tool", key, str(destination)]

    def fake_run_command(
        command: list[str], *, report_path: Path, label: str
    ) -> tuple[dict[str, Any], int]:
        source = fixture_inputs("pass") / report_path.name
        shutil.copyfile(source, report_path)
        return load_json(report_path), 0

    monkeypatch.setattr(builder, "generated_report_command", fake_generated_report_command)
    monkeypatch.setattr(builder, "run_command", fake_run_command)
    args = builder.parse_args(
        [
            "--mode",
            "mixed",
            "--output-dir",
            str(output_dir),
            "--doctor-report",
            str(fixture_inputs("pass") / "doctor-report.json"),
        ]
    )
    manifest, exit_code = builder.build_release_readiness_bundle(args)

    assert exit_code == 0
    source_kinds = {item["key"]: item["source_kind"] for item in manifest["staged_reports"]}
    assert source_kinds["doctor"] == "collected"
    assert set(source_kinds.values()) == {"collected", "generated"}
