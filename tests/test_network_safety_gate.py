from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "scripts" / "evaluate_network_safety_gate.py"
COMMON_PATH = REPO_ROOT / "tools" / "common" / "network_safety_gate.py"

common_spec = importlib.util.spec_from_file_location("network_safety_gate_common_for_tests", COMMON_PATH)
assert common_spec is not None
gate = importlib.util.module_from_spec(common_spec)
assert common_spec.loader is not None
common_spec.loader.exec_module(gate)


def write_request(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def base_request() -> dict[str, object]:
    return {
        "schema_version": "network-safety-gate-request.v1",
        "executor_name": "fixture_fetch_executor",
        "workspace_id": "alpha_subject",
        "dry_run": True,
        "allowlist": {
            "hosts": ["archives.example.gov"],
            "url_prefixes": ["https://archives.example.gov/"],
        },
        "rate_limits": {
            "max_requests_per_minute": 5,
            "min_interval_seconds": 1.0,
        },
        "side_effect_budget": {
            "max_actions": 5,
            "max_side_effect_units": 5,
        },
        "network_policy": {
            "user_agent": "SummaBot/1.0 (+local operator review)",
            "robots_mode": "respect_robots",
            "allow_http": False,
        },
        "dirty_worktree_policy": {
            "require_clean_worktree": False,
            "repo_root": None,
        },
        "planned_actions": [
            {
                "action_id": "fetch-manifest",
                "action_kind": "fetch_manifest",
                "url": "https://archives.example.gov/subject/alpha/manifest.jsonl",
                "method": "GET",
                "side_effect_units": 1,
            },
            {
                "action_id": "fetch-entry",
                "action_kind": "fetch_payload",
                "url": "https://archives.example.gov/subject/alpha/entry-001",
                "method": "HEAD",
                "side_effect_units": 1,
            },
        ],
    }


def test_network_safety_gate_allows_valid_dry_run_and_reports_actions(tmp_path: Path) -> None:
    payload = gate.evaluate_request(base_request())

    assert payload["decision"] == "dry_run"
    assert payload["execution_allowed"] is False
    assert payload["counts"]["planned_actions"] == 2
    assert payload["counts"]["total_side_effect_units"] == 2
    assert all(action["status"] == "planned" for action in payload["planned_actions"])


def test_network_safety_gate_refuses_missing_allowlist() -> None:
    request = base_request()
    request["allowlist"] = {"hosts": [], "url_prefixes": []}

    payload = gate.evaluate_request(request)

    assert payload["decision"] == "refuse"
    assert any(error["code"] == "ALLOWLIST_REQUIRED" for error in payload["errors"])


def test_network_safety_gate_refuses_empty_url_prefix_list() -> None:
    request = base_request()
    request["allowlist"] = {
        "hosts": ["archives.example.gov"],
        "url_prefixes": [],
    }

    payload = gate.evaluate_request(request)

    assert payload["decision"] == "refuse"
    assert any(
        error["code"] == "INVALID_ALLOWLIST"
        and "at least one prefix" in error["message"]
        for error in payload["errors"]
    )


def test_network_safety_gate_refuses_exceeded_budget() -> None:
    request = base_request()
    request["side_effect_budget"] = {"max_actions": 1, "max_side_effect_units": 1}

    payload = gate.evaluate_request(request)

    assert payload["decision"] == "refuse"
    codes = [error["code"] for error in payload["errors"]]
    assert "ACTION_BUDGET_EXCEEDED" in codes
    assert "SIDE_EFFECT_BUDGET_EXCEEDED" in codes


def test_network_safety_gate_refuses_actions_that_exceed_min_interval_cadence() -> None:
    request = base_request()
    request["rate_limits"] = {
        "max_requests_per_minute": 10,
        "min_interval_seconds": 30.0,
    }
    request["planned_actions"] = [
        {
            "action_id": f"fetch-{index}",
            "action_kind": "fetch_manifest",
            "url": f"https://archives.example.gov/subject/alpha/manifest-{index}.jsonl",
            "method": "GET",
            "side_effect_units": 1,
        }
        for index in range(4)
    ]

    payload = gate.evaluate_request(request)

    assert payload["decision"] == "refuse"
    assert any(error["code"] == "RATE_LIMIT_EXCEEDED" for error in payload["errors"])


def test_network_safety_gate_allows_api_call_with_not_applicable_robots_posture() -> None:
    request = base_request()
    request["network_policy"] = {
        "user_agent": "SummaBot/1.0 (+local operator review)",
        "robots_mode": "not_applicable",
        "allow_http": False,
    }
    request["planned_actions"] = [
        {
            "action_id": "api-query",
            "action_kind": "api_call",
            "url": "https://archives.example.gov/api/v1/status",
            "method": "GET",
            "side_effect_units": 1,
        }
    ]

    payload = gate.evaluate_request(request)

    assert payload["decision"] == "dry_run"
    assert payload["planned_actions"][0]["status"] == "planned"


def test_allowlisted_rejects_subdomain_forgery_for_bare_host_prefix() -> None:
    assert gate.allowlisted(
        "https://api.github.com.attacker.com/path",
        hosts=[],
        prefixes=["https://api.github.com"],
    ) is False
    assert gate.allowlisted(
        "https://api.github.com/path",
        hosts=[],
        prefixes=["https://api.github.com"],
    ) is True


def test_allowlisted_normalizes_host_case_punycode_default_port_and_rejects_userinfo() -> None:
    assert gate.allowlisted(
        "https://xn--bcher-kva.example/alpha",
        hosts=["xn--bcher-kva.example"],
        prefixes=[],
    ) is True
    assert gate.allowlisted(
        "https://archives.example.gov:443/subject/alpha",
        hosts=[],
        prefixes=["https://ARCHIVES.EXAMPLE.GOV/subject"],
    ) is True
    assert gate.allowlisted(
        "https://example.com@attacker.invalid/path",
        hosts=["attacker.invalid"],
        prefixes=[],
    ) is False


def test_network_safety_gate_refuses_dirty_worktree_when_required(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "-C", str(repo_root), "init", "-b", "main"], check=True, capture_output=True, text=True)
    (repo_root / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    request = base_request()
    request["dirty_worktree_policy"] = {
        "require_clean_worktree": True,
        "repo_root": str(repo_root),
    }

    payload = gate.evaluate_request(request)

    assert payload["decision"] == "refuse"
    assert any(error["code"] == "DIRTY_WORKTREE_REFUSED" for error in payload["errors"])


def test_network_safety_gate_cli_writes_machine_readable_reports(tmp_path: Path) -> None:
    request_path = write_request(tmp_path, base_request())
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(request_path),
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
    assert report["schema_version"] == "network-safety-gate-report.v1"
    assert report["decision"] == "dry_run"
    assert "decision=dry_run" in report_text.read_text(encoding="utf-8")
