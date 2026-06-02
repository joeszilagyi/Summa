import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_python_floor_and_stdlib_runtime() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    project = pyproject["project"]
    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == []
    assert project["license"]["file"] == "LICENSE"


def test_pyproject_declares_dependency_groups_and_optional_runner_extras() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    dependency_groups = pyproject["dependency-groups"]
    assert "pytest>=8" in dependency_groups["test"]
    assert "pytest>=8" in dependency_groups["dev"]
    assert pyproject["project"]["optional-dependencies"]["adapters"] == []
    assert pyproject["project"]["optional-dependencies"]["llm-runners"] == []


def test_pytest_config_moved_into_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    assert pytest_options["addopts"] == "--import-mode=importlib"
    assert pytest_options["testpaths"] == ["tests", "tools/source_db_tools/tests"]
    assert "tools/source_db_tools/tests" in pytest_options["pythonpath"]
