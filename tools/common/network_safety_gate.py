"""Shared request validation and evaluation for network safety gating."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


REQUEST_SCHEMA_VERSION = "network-safety-gate-request.v1"
ACTION_KINDS = {
    "fetch_url",
    "fetch_manifest",
    "fetch_payload",
    "download_archive",
    "clone_repo",
    "api_call",
    "robots_check",
}
METHODS = {"GET", "HEAD"}
ROBOTS_MODES = {"respect_robots", "not_applicable"}
ROBOTS_OPTIONAL_ACTION_KINDS = {"api_call", "clone_repo"}


class NetworkSafetyGateError(RuntimeError):
    """Raised when a network safety gate request is malformed."""


def load_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NetworkSafetyGateError(f"could not read network safety gate request: {path}") from exc
    if not isinstance(payload, dict):
        raise NetworkSafetyGateError("network safety gate request must be a JSON object")
    return payload


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def url_host(value: str) -> str:
    return (urlparse(value).hostname or "").casefold()


def allowlisted(url: str, hosts: list[str], prefixes: list[str]) -> bool:
    parsed = urlparse(url)
    normalized_url = parsed.geturl()
    host = (parsed.hostname or "").casefold()
    for allowed_host in hosts:
        normalized_host = allowed_host.casefold()
        if host == normalized_host or host.endswith("." + normalized_host):
            return True
    for prefix in prefixes:
        if normalized_url.startswith(prefix):
            return True
    return False


def are_http_prefixes(values: list[str]) -> bool:
    for value in values:
        if not is_http_url(value):
            return False
    return True


def git_worktree_is_clean(repo_root: Path) -> tuple[bool | None, str | None]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or "git status failed"
    return proc.stdout.strip() == "", None


def validate_request_shape(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    required_keys = {
        "schema_version",
        "executor_name",
        "dry_run",
        "allowlist",
        "rate_limits",
        "side_effect_budget",
        "network_policy",
        "dirty_worktree_policy",
        "planned_actions",
    }
    optional_keys = {"workspace_id"}
    for key in sorted(set(payload) - (required_keys | optional_keys)):
        errors.append({"code": "UNKNOWN_FIELD", "message": f"unexpected field: {key}"})
    for key in sorted(required_keys):
        if key not in payload:
            errors.append({"code": "MISSING_REQUIRED_KEY", "message": f"missing required key: {key}"})

    if payload.get("schema_version") != REQUEST_SCHEMA_VERSION:
        errors.append({"code": "INVALID_SCHEMA_VERSION", "message": f"schema_version must equal {REQUEST_SCHEMA_VERSION}"})
    if not isinstance(payload.get("executor_name"), str) or not payload["executor_name"].strip():
        errors.append({"code": "INVALID_EXECUTOR_NAME", "message": "executor_name must be a non-blank string"})
    if "workspace_id" in payload and payload["workspace_id"] is not None and (
        not isinstance(payload["workspace_id"], str) or not payload["workspace_id"].strip()
    ):
        errors.append({"code": "INVALID_WORKSPACE_ID", "message": "workspace_id must be null or a non-blank string"})
    if not isinstance(payload.get("dry_run"), bool):
        errors.append({"code": "INVALID_DRY_RUN", "message": "dry_run must be a boolean"})

    allowlist = payload.get("allowlist")
    if not isinstance(allowlist, dict):
        errors.append({"code": "INVALID_ALLOWLIST", "message": "allowlist must be an object"})
    else:
        hosts = allowlist.get("hosts")
        prefixes = allowlist.get("url_prefixes")
        if not isinstance(hosts, list) or any(not isinstance(item, str) or not item.strip() for item in hosts):
            errors.append({"code": "INVALID_ALLOWLIST", "message": "allowlist.hosts must be an array of non-blank strings"})
        if not isinstance(prefixes, list) or any(not isinstance(item, str) or not item.strip() for item in prefixes):
            errors.append({"code": "INVALID_ALLOWLIST", "message": "allowlist.url_prefixes must be an array of non-blank strings"})
        elif not are_http_prefixes(prefixes):
            errors.append({"code": "INVALID_ALLOWLIST", "message": "allowlist.url_prefixes must be absolute http or https URL prefixes"})

    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        errors.append({"code": "INVALID_RATE_LIMITS", "message": "rate_limits must be an object"})
    else:
        if not isinstance(rate_limits.get("max_requests_per_minute"), int) or rate_limits["max_requests_per_minute"] < 1:
            errors.append({"code": "INVALID_RATE_LIMITS", "message": "rate_limits.max_requests_per_minute must be an integer >= 1"})
        min_interval = rate_limits.get("min_interval_seconds")
        if not isinstance(min_interval, (int, float)) or min_interval < 0:
            errors.append({"code": "INVALID_RATE_LIMITS", "message": "rate_limits.min_interval_seconds must be a number >= 0"})

    budget = payload.get("side_effect_budget")
    if not isinstance(budget, dict):
        errors.append({"code": "INVALID_SIDE_EFFECT_BUDGET", "message": "side_effect_budget must be an object"})
    else:
        if not isinstance(budget.get("max_actions"), int) or budget["max_actions"] < 1:
            errors.append({"code": "INVALID_SIDE_EFFECT_BUDGET", "message": "side_effect_budget.max_actions must be an integer >= 1"})
        if not isinstance(budget.get("max_side_effect_units"), int) or budget["max_side_effect_units"] < 0:
            errors.append({"code": "INVALID_SIDE_EFFECT_BUDGET", "message": "side_effect_budget.max_side_effect_units must be an integer >= 0"})

    network_policy = payload.get("network_policy")
    if not isinstance(network_policy, dict):
        errors.append({"code": "INVALID_NETWORK_POLICY", "message": "network_policy must be an object"})
    else:
        if not isinstance(network_policy.get("user_agent"), str) or not network_policy["user_agent"].strip():
            errors.append({"code": "INVALID_NETWORK_POLICY", "message": "network_policy.user_agent must be a non-blank string"})
        if network_policy.get("robots_mode") not in ROBOTS_MODES:
            errors.append({"code": "INVALID_NETWORK_POLICY", "message": f"network_policy.robots_mode must be one of: {', '.join(sorted(ROBOTS_MODES))}"})
        if not isinstance(network_policy.get("allow_http"), bool):
            errors.append({"code": "INVALID_NETWORK_POLICY", "message": "network_policy.allow_http must be a boolean"})

    worktree = payload.get("dirty_worktree_policy")
    if not isinstance(worktree, dict):
        errors.append({"code": "INVALID_DIRTY_WORKTREE_POLICY", "message": "dirty_worktree_policy must be an object"})
    else:
        if not isinstance(worktree.get("require_clean_worktree"), bool):
            errors.append({"code": "INVALID_DIRTY_WORKTREE_POLICY", "message": "dirty_worktree_policy.require_clean_worktree must be a boolean"})
        repo_root = worktree.get("repo_root")
        if repo_root is not None and not isinstance(repo_root, str):
            errors.append({"code": "INVALID_DIRTY_WORKTREE_POLICY", "message": "dirty_worktree_policy.repo_root must be null or a string"})

    actions = payload.get("planned_actions")
    if not isinstance(actions, list) or not actions:
        errors.append({"code": "INVALID_PLANNED_ACTIONS", "message": "planned_actions must be a non-empty array"})
    else:
        seen_ids: set[str] = set()
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                errors.append({"code": "INVALID_PLANNED_ACTION", "message": f"planned_actions[{index}] must be an object"})
                continue
            action_id = action.get("action_id")
            if not isinstance(action_id, str) or not action_id.strip():
                errors.append({"code": "INVALID_ACTION_ID", "message": f"planned_actions[{index}].action_id must be a non-blank string"})
            elif action_id in seen_ids:
                errors.append({"code": "DUPLICATE_ACTION_ID", "message": f"duplicate action_id: {action_id}"})
            else:
                seen_ids.add(action_id)
            if action.get("action_kind") not in ACTION_KINDS:
                errors.append({"code": "INVALID_ACTION_KIND", "message": f"planned_actions[{index}].action_kind is invalid"})
            if action.get("method") not in METHODS:
                errors.append({"code": "INVALID_METHOD", "message": f"planned_actions[{index}].method must be GET or HEAD"})
            url = action.get("url")
            if not isinstance(url, str) or not url.strip() or not is_http_url(url):
                errors.append({"code": "INVALID_URL", "message": f"planned_actions[{index}].url must be an absolute http or https URL"})
            units = action.get("side_effect_units")
            if not isinstance(units, int) or isinstance(units, bool) or units < 0:
                errors.append({"code": "INVALID_SIDE_EFFECT_UNITS", "message": f"planned_actions[{index}].side_effect_units must be an integer >= 0"})

    return errors


def evaluate_request(
    payload: dict[str, Any],
    *,
    git_status_provider: Callable[[Path], tuple[bool | None, str | None]] = git_worktree_is_clean,
) -> dict[str, Any]:
    errors = validate_request_shape(payload)
    warnings: list[dict[str, Any]] = []

    allowlist = payload.get("allowlist", {})
    hosts = [item for item in allowlist.get("hosts", []) if isinstance(item, str)]
    prefixes = [item for item in allowlist.get("url_prefixes", []) if isinstance(item, str)]
    if not hosts and not prefixes:
        errors.append({"code": "ALLOWLIST_REQUIRED", "message": "allowlist.hosts or allowlist.url_prefixes must include at least one entry"})

    actions = payload.get("planned_actions", [])
    max_actions = payload.get("side_effect_budget", {}).get("max_actions")
    max_units = payload.get("side_effect_budget", {}).get("max_side_effect_units")
    max_rpm = payload.get("rate_limits", {}).get("max_requests_per_minute")
    total_units = 0

    action_reports: list[dict[str, Any]] = []
    for index, action in enumerate(actions if isinstance(actions, list) else []):
        if not isinstance(action, dict):
            continue
        url = str(action.get("url", ""))
        method = action.get("method")
        units = action.get("side_effect_units") if isinstance(action.get("side_effect_units"), int) else 0
        total_units += units
        action_errors: list[str] = []
        if not hosts and not prefixes:
            action_errors.append("missing allowlist")
        elif url and not allowlisted(url, hosts, prefixes):
            action_errors.append("url host or prefix is not allowlisted")
        parsed = urlparse(url) if url else None
        allow_http = payload.get("network_policy", {}).get("allow_http")
        if parsed is not None and parsed.scheme == "http" and allow_http is not True:
            action_errors.append("plain http is not allowed by network_policy.allow_http")
        robots_mode = payload.get("network_policy", {}).get("robots_mode")
        action_kind = action.get("action_kind")
        if action_kind not in ROBOTS_OPTIONAL_ACTION_KINDS and robots_mode != "respect_robots":
            action_errors.append("robots_mode must be respect_robots for fetch actions and can be not_applicable only for clone_repo or api_call")
        if method not in METHODS:
            action_errors.append("method must be GET or HEAD")
        action_reports.append(
            {
                "action_id": action.get("action_id"),
                "action_kind": action_kind,
                "url": url,
                "host": url_host(url) if url else None,
                "method": method,
                "side_effect_units": units,
                "status": "planned" if not action_errors else "refused",
                "errors": action_errors,
            }
        )
        for message in action_errors:
            errors.append({"code": "ACTION_REFUSED", "message": f"action {action.get('action_id')}: {message}"})

    if isinstance(max_actions, int) and len(action_reports) > max_actions:
        errors.append({"code": "ACTION_BUDGET_EXCEEDED", "message": "planned action count exceeds side_effect_budget.max_actions"})
    if isinstance(max_units, int) and total_units > max_units:
        errors.append({"code": "SIDE_EFFECT_BUDGET_EXCEEDED", "message": "planned side_effect_units exceed side_effect_budget.max_side_effect_units"})
    if isinstance(max_rpm, int) and len(action_reports) > max_rpm:
        errors.append({"code": "RATE_LIMIT_EXCEEDED", "message": "planned action count exceeds rate_limits.max_requests_per_minute"})

    dirty_policy = payload.get("dirty_worktree_policy", {})
    if isinstance(dirty_policy, dict) and dirty_policy.get("require_clean_worktree") is True:
        repo_root_value = dirty_policy.get("repo_root")
        if not isinstance(repo_root_value, str) or not repo_root_value.strip():
            errors.append({"code": "DIRTY_WORKTREE_POLICY_INVALID", "message": "repo_root is required when require_clean_worktree is true"})
        else:
            clean, detail = git_status_provider(Path(repo_root_value))
            if clean is None:
                errors.append({"code": "DIRTY_WORKTREE_STATUS_UNKNOWN", "message": detail or "could not inspect git worktree status"})
            elif clean is False:
                errors.append({"code": "DIRTY_WORKTREE_REFUSED", "message": "network operation refused because the git worktree is dirty"})

    dry_run = payload.get("dry_run") is True
    if not errors and dry_run:
        decision = "dry_run"
    elif not errors:
        decision = "allow"
    else:
        decision = "refuse"

    return {
        "schema_version": "network-safety-gate-report.v1",
        "request_schema_version": REQUEST_SCHEMA_VERSION,
        "executor_name": payload.get("executor_name"),
        "workspace_id": payload.get("workspace_id"),
        "decision": decision,
        "dry_run": dry_run,
        "execution_allowed": decision == "allow",
        "counts": {
            "planned_actions": len(action_reports),
            "refused_actions": sum(1 for item in action_reports if item["status"] == "refused"),
            "total_side_effect_units": total_units,
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "checks": {
            "allowlist_present": bool(hosts or prefixes),
            "allowlist": payload.get("allowlist"),
            "rate_limits": payload.get("rate_limits"),
            "side_effect_budget": payload.get("side_effect_budget"),
            "dirty_worktree_policy": payload.get("dirty_worktree_policy"),
            "network_policy": payload.get("network_policy"),
        },
        "planned_actions": action_reports,
        "errors": errors,
        "warnings": warnings,
    }
