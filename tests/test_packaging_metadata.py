import importlib
import os
import subprocess
import sys
import tomllib
import venv
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "repo-hygiene.yml"
PROJECT_METADATA = REPO_ROOT / ".project_metadata"

EXPECTED_CONSOLE_SCRIPTS = {
    "summa-new-topic": "tools.scripts.bootstrap_topic_workspace:main",
    "summa-build-knowledge-tree": "tools.scripts.build_publication_artifacts:main",
    "summa-workspace-overview": "tools.scripts.build_workspace_overview_view:main",
    "summa-subject-detail": "tools.scripts.build_subject_detail_view:main",
    "summa-source-intake-status": "tools.scripts.build_source_intake_status_view:main",
    "summa-review-queue": "tools.scripts.build_review_queue_view:main",
    "summa-local-doctor": "tools.scripts.local_doctor:main",
    "summa-operator-dashboard": "tools.scripts.build_operator_dashboard:main",
    "summa-operator-path-smoke": "tools.scripts.operator_path_smoke:main",
    "summa-resolve-gather-domain-pack": "tools.scripts.resolve_gather_domain_pack:main",
    "summa-init-canonical-store": "tools.source_db_tools.init_canonical_store:main",
    "summa-run-gather": "tools.scripts.run_topic_gather:main",
    "summa-execute-source-adapter": "tools.scripts.execute_source_adapter:main",
    "summa-ingest-gather-candidate-batch": ("tools.scripts.ingest_gather_candidate_batch:main"),
    "summa-ingest-execution-artifacts": "tools.scripts.ingest_execution_artifacts:main",
    "summa-run-topic-cycle": "tools.scripts.run_topic_cycle:main",
    "summa-run-scheduled-topic-cycles": "tools.scripts.run_scheduled_topic_cycles:main",
    "summa-select-scheduled-workspaces": "tools.scripts.select_scheduled_workspaces:main",
    "summa-apply-review-decision": "tools.scripts.apply_review_decision:main",
    "summa-evaluate-network-safety-gate": ("tools.scripts.evaluate_network_safety_gate:main"),
    "summa-replay-canonical-write-spool": ("tools.scripts.replay_canonical_write_spool:main"),
    "summa-audit-canonical-graph-closure": ("tools.scripts.audit_canonical_graph_closure:main"),
    "summa-export-redacted-diagnostics": ("tools.scripts.export_redacted_diagnostics:main"),
    "summa-audit-rebuildability": ("tools.scripts.audit_rebuildability:main"),
}

RUNTIME_OPERATOR_CONSOLE_COMMANDS = {
    "run_topic_gather.py": "summa-run-gather",
    "execute_source_adapter.py": "summa-execute-source-adapter",
    "ingest_gather_candidate_batch.py": "summa-ingest-gather-candidate-batch",
    "ingest_execution_artifacts.py": "summa-ingest-execution-artifacts",
    "run_topic_cycle.py": "summa-run-topic-cycle",
    "run_scheduled_topic_cycles.py": "summa-run-scheduled-topic-cycles",
    "select_scheduled_workspaces.py": "summa-select-scheduled-workspaces",
    "apply_review_decision.py": "summa-apply-review-decision",
    "evaluate_network_safety_gate.py": "summa-evaluate-network-safety-gate",
    "replay_canonical_write_spool.py": "summa-replay-canonical-write-spool",
    "audit_canonical_graph_closure.py": "summa-audit-canonical-graph-closure",
    "export_redacted_diagnostics.py": "summa-export-redacted-diagnostics",
    "audit_rebuildability.py": "summa-audit-rebuildability",
}

INDEX_WRAPPER_CONSOLE_COMMANDS = {
    "Index_Apply_Review_Decision.sh": "summa-apply-review-decision",
    "Index_Build_Knowledge_Tree.sh": "summa-build-knowledge-tree",
    "Index_New_Topic.sh": "summa-new-topic",
    "Index_Operator_Path_Smoke.sh": "summa-operator-path-smoke",
    "Index_Run_Gather.sh": "summa-run-gather",
    "Index_Run_Scheduled_Topic_Cycles.sh": "summa-run-scheduled-topic-cycles",
    "Index_Run_Topic_Cycle.sh": "summa-run-topic-cycle",
    "Index_Select_Scheduled_Workspaces.sh": "summa-select-scheduled-workspaces",
    "Index_Replay_Canonical_Write_Spool.sh": "summa-replay-canonical-write-spool",
    "Index_Export_Redacted_Diagnostics.sh": "summa-export-redacted-diagnostics",
    "Index_Audit_Rebuildability.sh": "summa-audit-rebuildability",
    "Index_Workspace_Overview.sh": "summa-workspace-overview",
}

INDEX_WRAPPER_EXCLUSIONS = {
    "Index_Build_Release_Readiness_Bundle.sh": (
        "release-readiness serviceability wrapper; intentionally outside the F37 "
        "runtime-spine console surface"
    ),
    "Index_Plan_Crown_Jewel_Backup.sh": (
        "legacy compatibility wrapper around tools/common/crown_jewel_backup.py"
    ),
    "Index_Topic_Backup_Drill.sh": (
        "backup-drill serviceability wrapper; intentionally outside the F37 "
        "runtime-spine console surface"
    ),
}


def load_pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def load_project_metadata() -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in PROJECT_METADATA.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        payload[key.strip()] = value.strip()
    return payload


def venv_bin_dir(root: Path) -> Path:
    return root / ("Scripts" if os.name == "nt" else "bin")


def venv_python(root: Path) -> Path:
    return venv_bin_dir(root) / ("python.exe" if os.name == "nt" else "python")


def create_isolated_install_venv(tmp_path: Path) -> Path:
    root = tmp_path / "isolated-install"
    venv.EnvBuilder(with_pip=True, clear=True, system_site_packages=False).create(root)
    install = subprocess.run(
        [
            str(venv_python(root)),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--editable",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    return root


def write_fake_console_command(bin_dir: Path, command: str, capture_path: Path) -> Path:
    script_path = bin_dir / command
    script_path.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$SUMMA_WRAPPER_CAPTURE"\n',
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return script_path


def test_pyproject_declares_python_floor_and_stdlib_runtime() -> None:
    pyproject = load_pyproject()

    project = pyproject["project"]
    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == []
    assert project["license"]["file"] == "LICENSE"


def test_package_version_matches_project_metadata_current_build() -> None:
    pyproject = load_pyproject()
    metadata = load_project_metadata()

    assert metadata["CURRENT_BUILD"] != metadata["PRIOR_BUILD"]
    assert pyproject["project"]["version"] == metadata["CURRENT_BUILD"]
    assert pyproject["project"]["version"] != "0.0.0"
    assert str(Version(pyproject["project"]["version"])) == metadata["CURRENT_BUILD"]


def test_pyproject_declares_dependency_groups_and_no_empty_placeholder_extras() -> None:
    pyproject = load_pyproject()

    dependency_groups = pyproject["dependency-groups"]
    assert any(dependency.startswith("jsonschema>=") for dependency in dependency_groups["test"])
    assert "pytest>=8" in dependency_groups["test"]
    assert any(dependency.startswith("pytest-cov>=") for dependency in dependency_groups["test"])
    assert any(dependency.startswith("jsonschema>=") for dependency in dependency_groups["dev"])
    assert "pytest>=8" in dependency_groups["dev"]
    assert any(dependency.startswith("pytest-cov>=") for dependency in dependency_groups["dev"])
    assert any(dependency.startswith("ruff>=") for dependency in dependency_groups["dev"])
    assert any(dependency.startswith("mypy>=") for dependency in dependency_groups["dev"])

    optional_dependencies = pyproject["project"].get("optional-dependencies", {})
    if "adapters" in optional_dependencies:
        assert optional_dependencies["adapters"] != []
    if "llm-runners" in optional_dependencies:
        assert optional_dependencies["llm-runners"] != []


def test_pyproject_declares_console_scripts_and_package_discovery() -> None:
    pyproject = load_pyproject()

    project = pyproject["project"]
    assert project["scripts"] == EXPECTED_CONSOLE_SCRIPTS

    package_find = pyproject["tool"]["setuptools"]["packages"]["find"]
    assert package_find["include"] == ["tools*"]
    assert package_find["exclude"] == ["tools.source_db_tools.tests*"]
    assert package_find["namespaces"] is True


def test_console_script_targets_are_importable_and_callable() -> None:
    pyproject = load_pyproject()

    for command, target in pyproject["project"]["scripts"].items():
        module_name, attr_name = target.split(":", 1)
        module = importlib.import_module(module_name)
        assert hasattr(module, attr_name), (
            f"{command} target missing attribute {attr_name}: {target}"
        )
        assert callable(getattr(module, attr_name)), f"{command} target must be callable: {target}"


def test_runtime_operator_scripts_are_exposed_as_console_commands() -> None:
    pyproject = load_pyproject()
    scripts = pyproject["project"]["scripts"]

    for script_name, command in RUNTIME_OPERATOR_CONSOLE_COMMANDS.items():
        assert (REPO_ROOT / "tools" / "scripts" / script_name).is_file()
        assert command in scripts, f"{script_name} is missing console command {command}"
        assert scripts[command] == EXPECTED_CONSOLE_SCRIPTS[command]


def test_live_index_wrappers_are_packaged_or_explicitly_excluded() -> None:
    pyproject = load_pyproject()
    scripts = pyproject["project"]["scripts"]
    wrapper_names = {
        path.name for path in sorted((REPO_ROOT / "tools" / "scripts").glob("Index_*.sh"))
    }

    expected_wrapper_names = set(INDEX_WRAPPER_CONSOLE_COMMANDS) | set(INDEX_WRAPPER_EXCLUSIONS)
    assert wrapper_names == expected_wrapper_names

    for wrapper_name, command in INDEX_WRAPPER_CONSOLE_COMMANDS.items():
        assert command in scripts, f"{wrapper_name} is not exposed through {command}"

    for wrapper_name, reason in INDEX_WRAPPER_EXCLUSIONS.items():
        assert reason.strip()
        assert "TODO" not in reason.upper()
        assert "MAYBE" not in reason.upper()
        assert (REPO_ROOT / "tools" / "scripts" / wrapper_name).is_file()


@pytest.mark.parametrize("wrapper_name,console_command", sorted(INDEX_WRAPPER_CONSOLE_COMMANDS.items()))
def test_live_index_wrappers_prefer_installed_console_commands(
    wrapper_name: str, console_command: str, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_path = tmp_path / f"{console_command}.argv"
    write_fake_console_command(bin_dir, console_command, capture_path)

    wrapper_path = REPO_ROOT / "tools" / "scripts" / wrapper_name
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHON": "python-does-not-exist",
        "PYTHON_BIN": "python-does-not-exist",
        "SUMMA_WRAPPER_CAPTURE": str(capture_path),
    }
    proc = subprocess.run(
        ["bash", str(wrapper_path), "--sentinel", "wrapper-test"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert capture_path.read_text(encoding="utf-8").splitlines() == ["--sentinel", "wrapper-test"]


def test_pytest_config_moved_into_pyproject() -> None:
    pyproject = load_pyproject()

    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    assert pytest_options["addopts"] == "--import-mode=importlib --timeout=300"
    assert pytest_options["testpaths"] == ["tests", "tools/source_db_tools/tests"]
    assert "tools/source_db_tools/tests" in pytest_options["pythonpath"]


def test_static_analysis_tooling_is_configured_in_pyproject() -> None:
    pyproject = load_pyproject()

    tool_config = pyproject["tool"]
    assert tool_config["ruff"]["target-version"] == "py311"
    assert tool_config["ruff"]["line-length"] == 100
    assert tool_config["ruff"]["lint"]["select"] == ["E", "F", "I", "UP", "B", "SIM"]

    mypy_config = tool_config["mypy"]
    assert mypy_config["python_version"] == "3.11"
    assert mypy_config["check_untyped_defs"] is True
    assert "tools/scripts/run_topic_gather.py" in mypy_config["files"]
    assert "tools/source_db_tools/canonical_store.py" in mypy_config["files"]


def test_repo_hygiene_workflow_runs_ruff_and_mypy() -> None:
    workflow = HYGIENE_WORKFLOW.read_text(encoding="utf-8")

    assert "python -m ruff check $PYTHON_STATIC_TARGETS" in workflow
    assert "python -m ruff format --check $PYTHON_STATIC_TARGETS" in workflow
    assert "python -m mypy" in workflow


def test_coverage_tooling_is_configured_in_pyproject_and_ci() -> None:
    pyproject = load_pyproject()

    coverage_run = pyproject["tool"]["coverage"]["run"]
    assert coverage_run["source"] == ["tools"]
    assert "tools/source_db_tools/tests/*" in coverage_run["omit"]

    coverage_report = pyproject["tool"]["coverage"]["report"]
    assert coverage_report["show_missing"] is True
    assert coverage_report["fail_under"] == 70

    workflow = HYGIENE_WORKFLOW.read_text(encoding="utf-8")
    assert 'python -m pip install pytest "pytest-cov>=5" "jsonschema>=4.23"' in workflow
    assert (
        "python -m pytest -q --cov=tools --cov-report=term-missing --cov-report=xml"
    ) in workflow


def test_console_entry_point_targets_emit_help() -> None:
    for command, target in EXPECTED_CONSOLE_SCRIPTS.items():
        module_name, attr_name = target.split(":", 1)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import importlib, sys\n"
                    f"module = importlib.import_module({module_name!r})\n"
                    f"target = getattr(module, {attr_name!r})\n"
                    f"sys.argv = [{command!r}, '--help']\n"
                    "result = target()\n"
                    "raise SystemExit(0 if result is None else result)\n"
                ),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"{command} --help failed:\n{output}"
        assert "usage:" in output.lower(), f"{command} --help did not emit usage text:\n{output}"


def test_console_entry_points_run_as_installed_commands(tmp_path: Path) -> None:
    venv_root = create_isolated_install_venv(tmp_path)
    bin_dir = venv_bin_dir(venv_root)
    for command in EXPECTED_CONSOLE_SCRIPTS:
        installed_command = bin_dir / command
        assert installed_command.exists(), f"installed console command missing: {installed_command}"
        proc = subprocess.run(
            [str(installed_command), "--help"],
            cwd=REPO_ROOT,
            env={**os.environ, "PATH": str(bin_dir)},
            text=True,
            capture_output=True,
            check=False,
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"{command} --help failed:\n{output}"
        assert "usage:" in output.lower(), f"{command} --help did not emit usage text:\n{output}"


def test_isolated_install_does_not_depend_on_test_only_packages(tmp_path: Path) -> None:
    venv_root = create_isolated_install_venv(tmp_path)
    venv_python_path = venv_python(venv_root)
    probe = subprocess.run(
        [
            str(venv_python_path),
            "-c",
            (
                "import importlib.util, sys\n"
                "raise SystemExit(0 if importlib.util.find_spec('jsonschema') is None else 1)\n"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr

    help_probe = subprocess.run(
        [str(venv_bin_dir(venv_root) / "summa-local-doctor"), "--help"],
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": str(venv_bin_dir(venv_root))},
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_probe.returncode == 0, help_probe.stdout + help_probe.stderr
    assert "usage:" in (help_probe.stdout + help_probe.stderr).lower()
