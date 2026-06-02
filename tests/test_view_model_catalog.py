import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "tools" / "scripts" / "list_view_models.py"
EXPECTED_SCHEMA_VERSIONS = (
    "workspace-overview.v1",
    "subject-detail.v1",
    "review-queue.v1",
    "source-intake-status.v1",
)


def run_catalog(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CATALOG), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_view_model_catalog_lists_all_current_contracts(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    for schema_version in EXPECTED_SCHEMA_VERSIONS:
        (fixture_dir / f"{schema_version}.json").write_text('{"schema_version": "%s"}\n' % schema_version)

    result = run_catalog("--fixture-dir", str(fixture_dir))

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == "view-model-catalog-report.v1"
    assert report["ok"] is True
    assert report["counts"] == {
        "emitters_available": len(EXPECTED_SCHEMA_VERSIONS),
        "fixtures_available": len(EXPECTED_SCHEMA_VERSIONS),
        "schemas_available": len(EXPECTED_SCHEMA_VERSIONS),
        "view_models": len(EXPECTED_SCHEMA_VERSIONS),
    }
    assert report["fixture_bundle_command"] is None
    assert [entry["schema_version"] for entry in report["view_models"]] == list(EXPECTED_SCHEMA_VERSIONS)

    for entry in report["view_models"]:
        assert entry["schema"]["status"] == "ok"
        assert entry["schema"]["path"].endswith(f"{entry['schema_version']}.schema.json")
        assert entry["fixture"]["status"] == "ok"
        assert entry["fixture"]["path"].endswith(f"{entry['schema_version']}.json")
        assert entry["emitter"]["status"] == "ok"
        assert entry["emitter"]["example_command"]
        assert entry["emitter"]["required_inputs"]
        assert entry["validator"]["path"] == "tools/scripts/validate_view_model_json.py"


def test_view_model_catalog_filters_one_schema_version() -> None:
    result = run_catalog("--schema-version", "review-queue.v1")

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["counts"]["view_models"] == 1
    assert report["view_models"][0]["schema_version"] == "review-queue.v1"
    assert report["view_models"][0]["schema"]["status"] == "ok"
    assert report["view_models"][0]["emitter"]["status"] == "ok"


def test_view_model_catalog_rejects_unknown_schema_version() -> None:
    result = run_catalog("--schema-version", "unknown-view.v1")

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["counts"]["view_models"] == 0
    assert report["errors"][0]["code"] == "UNKNOWN_SCHEMA_VERSION"


def test_view_model_catalog_text_output_is_stable() -> None:
    result = run_catalog("--schema-version", "workspace-overview.v1", "--format", "text")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_version=view-model-catalog-report.v1" in result.stdout
    assert "ok=true" in result.stdout
    assert "view_models=1" in result.stdout
    assert "view_model[0].schema_version=workspace-overview.v1" in result.stdout
    assert "view_model[0].schema_status=ok" in result.stdout
    assert "view_model[0].emitter_status=ok" in result.stdout


def test_view_model_catalog_python_tool_compiles() -> None:
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(CATALOG)],
        cwd=REPO_ROOT,
        check=True,
    )
