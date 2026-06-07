from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from jsonschema import validators

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "config" / "repository_capabilities.v1.json"
SCHEMA_PATH = REPO_ROOT / "config" / "repository_capabilities.v1.schema.json"
DOC_PATH = REPO_ROOT / "docs" / "project" / "REPOSITORY_CAPABILITIES.md"
VALIDATOR_PATH = REPO_ROOT / "tools" / "scripts" / "validate_repository_capabilities.py"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_index() -> dict[str, Any]:
    payload = load_json(INDEX_PATH)
    assert isinstance(payload, dict)
    return payload


def load_pyproject_scripts() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    return {str(key): str(value) for key, value in scripts.items()}


def capabilities() -> list[dict[str, Any]]:
    entries = load_index()["capabilities"]
    assert isinstance(entries, list)
    return entries


def test_capability_index_validates_against_schema() -> None:
    schema = load_json(SCHEMA_PATH)
    instance = load_index()
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator_cls(schema).validate(instance)


def test_indexed_paths_docs_and_tests_exist() -> None:
    for entry in capabilities():
        for key in ("path", "wrapper_path", "docs_path"):
            value = entry.get(key)
            if value is None:
                continue
            assert isinstance(value, str)
            assert (REPO_ROOT / value).exists(), f"{entry['id']} {key} missing: {value}"

        for test_ref in entry.get("test_refs", []):
            assert isinstance(test_ref, str)
            test_path = test_ref.split("::", 1)[0]
            assert (REPO_ROOT / test_path).exists(), f"{entry['id']} test ref missing: {test_ref}"


def test_package_console_scripts_are_indexed_bidirectionally() -> None:
    scripts = load_pyproject_scripts()
    indexed_commands = {
        entry["package_command"]
        for entry in capabilities()
        if entry.get("kind") == "console_script" and entry.get("package_command")
    }

    assert indexed_commands == set(scripts)

    for entry in capabilities():
        command = entry.get("package_command")
        if command is None:
            continue
        assert command in scripts, f"{entry['id']} references unknown package command {command}"


def test_index_wrappers_are_indexed_or_explicitly_excluded() -> None:
    wrapper_paths = {
        entry["wrapper_path"]
        for entry in capabilities()
        if isinstance(entry.get("wrapper_path"), str)
    }
    actual_wrappers = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tools" / "scripts").glob("Index_*.sh"))
    }

    assert actual_wrappers <= wrapper_paths

    for wrapper in actual_wrappers:
        entries = [entry for entry in capabilities() if entry.get("wrapper_path") == wrapper]
        assert entries, wrapper
        assert any(
            entry.get("package_command") or entry.get("exclusion_reason") for entry in entries
        ), f"{wrapper} must map to a package command or carry an exclusion reason"


def test_standards_profiles_are_indexed() -> None:
    profile_paths = {
        entry["path"]
        for entry in capabilities()
        if entry.get("kind") == "standards_profile" and isinstance(entry.get("path"), str)
    }
    actual_profiles = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "config" / "standards_profiles").glob("*.json"))
        if path.name != "standards_profile.schema.json"
    }

    assert actual_profiles
    assert actual_profiles <= profile_paths


def test_release_readiness_surfaces_are_indexed() -> None:
    indexed_paths = {entry.get("path") for entry in capabilities()}

    assert "tools/scripts/build_release_readiness_bundle.py" in indexed_paths
    assert "tools/validators/validate_release_readiness.py" in indexed_paths
    assert "tools/scripts/Index_Build_Release_Readiness_Bundle.sh" in {
        entry.get("wrapper_path") for entry in capabilities()
    }


def test_legacy_retired_and_excluded_surfaces_are_not_package_exposed() -> None:
    scripts = load_pyproject_scripts()
    exposed = set(scripts)

    for entry in capabilities():
        if entry["status"] in {"legacy", "retired", "excluded"}:
            command = entry.get("package_command")
            assert command is None or command not in exposed


def test_exclusions_have_reasons() -> None:
    for entry in capabilities():
        reason = entry.get("exclusion_reason")
        if entry["status"] == "excluded" or (
            entry.get("kind") == "shell_wrapper" and not entry.get("package_command")
        ):
            assert isinstance(reason, str)
            assert reason.strip()
            assert "TODO" not in reason.upper()
            assert "MAYBE" not in reason.upper()


def test_docs_page_mentions_every_live_console_command() -> None:
    body = DOC_PATH.read_text(encoding="utf-8")

    for entry in capabilities():
        if entry.get("kind") == "console_script" and entry.get("status") == "live":
            command = entry.get("package_command")
            assert isinstance(command, str)
            assert command in body


def test_validator_cli_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "usage:" in (proc.stdout + proc.stderr).lower()


def test_validator_detects_non_callable_console_script_targets(monkeypatch) -> None:
    index = {
        "capabilities": [
            {
                "id": "x",
                "status": "live",
                "kind": "console_script",
                "package_command": "bad-cmd",
                "path": "tools/scripts/validate_repository_capabilities.py",
                "docs_path": "docs/project/REPOSITORY_CAPABILITIES.md",
                "test_refs": [],
            }
        ]
    }

    def fake_package_scripts() -> dict[str, Any]:
        return {"bad-cmd": "tools.scripts.validate_repository_capabilities:missing_attr"}

    monkeypatch.setattr("tools.scripts.validate_repository_capabilities.load_package_scripts", fake_package_scripts)

    validator = importlib.import_module("tools.scripts.validate_repository_capabilities")
    loaded = validator.validate_index(index)

    assert loaded["status"] == "fail"
    assert any(error["code"] == "invalid_command_target" for error in loaded["errors"])


def test_validator_accepts_importable_console_script_targets(monkeypatch) -> None:
    index = {
        "capabilities": [
            {
                "id": "x",
                "status": "live",
                "kind": "console_script",
                "package_command": "good-cmd",
                "path": "tools/scripts/validate_repository_capabilities.py",
                "docs_path": "docs/project/REPOSITORY_CAPABILITIES.md",
                "test_refs": [],
            }
        ]
    }

    def fake_package_scripts() -> dict[str, Any]:
        return {"good-cmd": "tools.scripts.validate_repository_capabilities:main"}

    monkeypatch.setattr("tools.scripts.validate_repository_capabilities.load_package_scripts", fake_package_scripts)

    validator = importlib.import_module("tools.scripts.validate_repository_capabilities")
    loaded = validator.validate_index(index)
    assert not any(error["code"] == "invalid_command_target" for error in loaded["errors"])


def test_validator_report_passes_for_checked_in_index() -> None:
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR_PATH), "--index", str(INDEX_PATH), "--format", "json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "pass"
    assert report["counts"]["package_console_scripts"] == len(load_pyproject_scripts())
    assert report["counts"]["shell_wrappers"] >= len(
        list((REPO_ROOT / "tools" / "scripts").glob("Index_*.sh"))
    )
    assert report["counts"]["standards_profiles"] >= 4


def test_validator_writes_schema_inventory_sidecar(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    schema_path = repo_root / "config" / "example.schema.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps({"$id": "example.schema.json", "title": "Example schema"}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    validator = importlib.import_module("tools.scripts.validate_repository_capabilities")
    inventory = validator.write_schema_inventory(repo_root)

    assert inventory["schema_count"] == 1
    assert inventory["schemas"] == [
        {
            "path": "config/example.schema.json",
            "title": "Example schema",
            "id": "example.schema.json",
            "status": "readable",
        }
    ]
    sidecar = repo_root / "runtime" / "config" / "schema_inventory.json"
    assert sidecar.is_file()
