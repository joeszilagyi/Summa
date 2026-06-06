from __future__ import annotations

from pathlib import Path

import pytest


KNOWN_MARKERS = {
    "integration": {
        "test_operator_path_smoke.py": ["integration", "slow", "subprocess", "publication"],
        "test_run_topic_cycle.py": ["integration", "slow", "subprocess", "publication"],
        "test_run_topic_gather.py": ["integration", "slow", "subprocess", "publication"],
        "test_execute_source_adapter_remote.py": ["integration", "slow", "subprocess", "network_fixture"],
        "test_rebuildability_audit.py": ["integration", "slow", "subprocess", "git", "sqlite"],
        "test_network_safety_gate.py": ["integration", "slow", "network_fixture", "subprocess"],
        "test_scheduled_cycle_runner.py": ["integration", "slow", "subprocess"],
        "test_source_adapter_handoff_validator.py": ["integration", "subprocess"],
        "test_local_git_repo_adapter.py": ["integration", "git", "subprocess"],
        "test_release_readiness_bundle_builder.py": ["integration", "slow", "publication", "subprocess"],
        "test_source_adapter_hostile_replay.py": ["integration", "subprocess", "network_fixture"],
    },
    "publication": {
        "test_source_db_registry_inputs.py",
    },
}


def pytest_configure(config: pytest.Config) -> None:
    for marker in [
        "unit: a fast, in-process test",
        "integration: a higher-cost integration-style test",
        "slow: a longer-running test",
        "network_fixture: tests that exercise network-like fixtures",
        "subprocess: tests that spawn external processes",
        "sqlite: tests that exercise sqlite-backed persistence",
        "git: tests that invoke git commands",
        "publication: tests focused on operator publication workflows",
    ]:
        config.addinivalue_line("markers", marker)


def _is_integration(module_path: Path) -> bool:
    return module_path.name in KNOWN_MARKERS["integration"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        module_name = Path(str(item.fspath)).name if item.fspath else ""
        if module_name in KNOWN_MARKERS["integration"]:
            for marker in KNOWN_MARKERS["integration"][module_name]:
                item.add_marker(getattr(pytest.mark, marker))
        else:
            item.add_marker(pytest.mark.unit)

        for marker in KNOWN_MARKERS.get("publication", ()):  # pragma: no branch
            if module_name == marker:
                item.add_marker(pytest.mark.publication)
