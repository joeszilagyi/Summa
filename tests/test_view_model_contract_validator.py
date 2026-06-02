import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "validate_view_model_json.py"
SCHEMAS_DIR = REPO_ROOT / "config" / "view_models"

MINIMAL_PAYLOADS: dict[str, dict[str, object]] = {
    "workspace-overview.v1": {
        "schema_version": "workspace-overview.v1",
        "registry_path": "topic_workspaces.local.json",
        "requested_workspace_ids": [],
        "counts": {},
        "workspaces": [],
    },
    "subject-detail.v1": {
        "schema_version": "subject-detail.v1",
        "subject_manifest_path": "subject_manifest.json",
        "subject": {},
        "domain_pack": {},
        "facets": [],
        "legacy_substrates": [],
        "status": {},
    },
    "review-queue.v1": {
        "schema_version": "review-queue.v1",
        "database_path": "source.sqlite",
        "filters": {},
        "counts": {},
        "truncated": False,
        "items": [],
    },
    "source-intake-status.v1": {
        "schema_version": "source-intake-status.v1",
        "inputs": {},
        "counts": {},
        "adapters": [],
    },
    "job-status.v1": {
        "schema_version": "job-status.v1",
        "active": [],
        "failed": [],
        "retryable": [],
        "canceled": [],
        "completed": [],
    },
}


def run_validator(target: Path, *, output_format: str = "json") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target), "--format", output_format],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_payload(tmp_path: Path, payload: dict[str, object]) -> Path:
    target = tmp_path / "view-model.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def schema_versions_from_catalog() -> set[str]:
    schema_versions: set[str] = set()
    for schema_path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_version = schema["properties"]["schema_version"]["const"]
        assert schema_path.name == f"{schema_version}.schema.json"
        schema_versions.add(schema_version)
    return schema_versions


def test_view_model_schema_catalog_matches_current_contracts() -> None:
    assert schema_versions_from_catalog() == set(MINIMAL_PAYLOADS)


def test_all_view_model_schemas_accept_minimal_payloads(tmp_path: Path) -> None:
    for schema_version, payload in MINIMAL_PAYLOADS.items():
        target = write_payload(tmp_path, payload)
        result = run_validator(target)

        assert result.returncode == 0, schema_version + result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["ok"] is True
        assert report["status"] == "pass"
        assert report["validator"] == "view_model_json"
        assert report["model_schema_version"] == schema_version
        assert report["schema_path"].endswith(f"config/view_models/{schema_version}.schema.json")
        assert report["errors"] == []


def test_view_model_validator_rejects_missing_required_field(tmp_path: Path) -> None:
    payload = dict(MINIMAL_PAYLOADS["workspace-overview.v1"])
    del payload["counts"]
    target = write_payload(tmp_path, payload)

    result = run_validator(target)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report["errors"][0]["code"] == "MISSING_REQUIRED_KEY"
    assert report["errors"][0]["path"] == "$.counts"


def test_view_model_validator_rejects_unknown_schema_version(tmp_path: Path) -> None:
    payload = {
        "schema_version": "unknown-view.v1",
        "counts": {},
    }
    target = write_payload(tmp_path, payload)

    result = run_validator(target)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["model_schema_version"] == "unknown-view.v1"
    assert report["schema_path"] is None
    assert report["errors"][0]["code"] == "UNKNOWN_SCHEMA_VERSION"


def test_view_model_validator_rejects_wrong_top_level_type(tmp_path: Path) -> None:
    payload = dict(MINIMAL_PAYLOADS["workspace-overview.v1"])
    payload["counts"] = []
    target = write_payload(tmp_path, payload)

    result = run_validator(target)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["errors"][0]["code"] == "TYPE_MISMATCH"
    assert report["errors"][0]["path"] == "$.counts"


def test_view_model_validator_rejects_nonstandard_json_constants(tmp_path: Path) -> None:
    target = tmp_path / "view-model.json"
    target.write_text(
        (
            '{\n'
            '  "schema_version": "workspace-overview.v1",\n'
            '  "registry_path": "topic_workspaces.local.json",\n'
            '  "requested_workspace_ids": [],\n'
            '  "counts": {},\n'
            '  "workspaces": [],\n'
            '  "generated_score": NaN\n'
            '}\n'
        ),
        encoding="utf-8",
    )

    result = run_validator(target)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report["errors"][0]["code"] == "JSON_PARSE_ERROR"
    assert "invalid JSON constant NaN" in report["errors"][0]["message"]


def test_view_model_validator_text_output_is_stable(tmp_path: Path) -> None:
    target = write_payload(tmp_path, MINIMAL_PAYLOADS["workspace-overview.v1"])

    result = run_validator(target, output_format="text")

    assert result.returncode == 0
    assert "schema_version=view-model-validation-report.v1" in result.stdout
    assert "validator=view_model_json" in result.stdout
    assert "status=pass" in result.stdout
    assert "ok=true" in result.stdout
    assert "model_schema_version=workspace-overview.v1" in result.stdout
    assert "errors=0 warnings=0" in result.stdout


def test_view_model_validator_python_tool_compiles() -> None:
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT)],
        cwd=REPO_ROOT,
        check=True,
    )
