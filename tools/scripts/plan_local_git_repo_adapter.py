#!/usr/bin/env python3
"""Dry-run local Git repository source adapter planner with no remote operations."""

from __future__ import annotations

import argparse
import hashlib
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

from tools.common.atomic_write import atomic_write_jsonl, stable_json_text  # noqa: E402
from tools.common.source_adapter_contract import (  # noqa: E402
    LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS,
)
from tools.common.source_adapter_handoff import (  # noqa: E402
    build_local_git_repo_handoff_record,
    validate_source_adapter_handoff_record,
)

import validate_source_adapter  # noqa: E402


class LocalGitRepoAdapterError(RuntimeError):
    """Raised when local Git adapter planning inputs are invalid."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, help="Path to a validated local_git_repo source-adapter manifest.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--handoff-jsonl", type=Path, help="Optional JSONL output path for emitted handoff records.")
    return parser.parse_args()


def resolve_path(raw_path: str, *, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def load_adapter(adapter_path: Path) -> dict[str, Any]:
    result, exit_code = validate_source_adapter.validate_source_adapter(adapter_path)
    if exit_code != validate_source_adapter.EXIT_PASS:
        message = "source adapter validation failed"
        errors = result.get("errors", [])
        if errors:
            message = errors[0].get("message", message)
        raise LocalGitRepoAdapterError(message)
    before_hash = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    payload, parse_errors, parse_exit = validate_source_adapter.load_json_object(adapter_path)
    if parse_exit != validate_source_adapter.EXIT_PASS:
        message = parse_errors[0].get("message", "source adapter parsing failed") if parse_errors else "source adapter parsing failed"
        raise LocalGitRepoAdapterError(message)
    after_hash = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    if before_hash != after_hash:
        raise LocalGitRepoAdapterError("adapter file changed during validation")
    if payload.get("input_family") != "local_git_repo":
        raise LocalGitRepoAdapterError("input_family must be local_git_repo for this planner")
    return payload


def git_environment(repo_path: Path) -> dict[str, str]:
    """Return a subprocess environment stable against external process pollution."""
    env = os.environ.copy()
    for key in (
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
        "PYTHONPATH",
        "NO_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "HOME",
        "TZ",
    ):
        env.pop(key, None)
    for key in ("TMPDIR",):
        env[key] = str(repo_path / ".tmp")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = git_environment(repo_path)
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def matches_any_glob(relative_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    path_obj = PurePosixPath(relative_path)
    for pattern in patterns:
        if path_obj.match(pattern) or fnmatch.fnmatch(relative_path, pattern):
            return True
        if pattern.startswith("**/") and path_obj.match(pattern[3:]):
            return True
    return False


def inspect_repo(
    repo_path: Path,
    *,
    locator: dict[str, Any],
    configured_ref: str | None,
    include_globs: list[str],
    exclude_globs: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    blockers: list[str] = []
    if not repo_path.exists():
        return None, [f"local git repo path not found: {repo_path}"]
    if not repo_path.is_dir():
        return None, [f"local git repo path is not a directory: {repo_path}"]

    if isinstance(locator.get("repo_url"), str) and locator.get("repo_url", "").strip():
        blockers.append("remote clone behavior is not allowed; planner inspects already-local checkouts only")

    top_level_proc = git(repo_path, "rev-parse", "--show-toplevel")
    if top_level_proc.returncode != 0:
        return None, [f"local git repo path is not a git repository: {repo_path}"]
    top_level = Path(top_level_proc.stdout.strip()).resolve()

    inspected_ref = configured_ref or "HEAD"
    commit_proc = git(repo_path, "rev-parse", "--verify", f"{inspected_ref}^{{commit}}")
    if commit_proc.returncode != 0:
        return None, [f"configured ref could not be resolved to a commit: {inspected_ref}"]
    commit = commit_proc.stdout.strip()

    branch_proc = git(repo_path, "symbolic-ref", "--short", "HEAD")
    current_branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None

    status_proc = git(repo_path, "status", "--porcelain")
    repo_state = "dirty" if status_proc.stdout.strip() else "clean"
    if repo_state == "dirty":
        blockers.append("git working tree has local modifications or untracked files")

    ls_files_proc = git(repo_path, "ls-files")
    if ls_files_proc.returncode != 0:
        return None, [f"git ls-files failed for repository: {top_level}"]
    tracked_paths = [line for line in ls_files_proc.stdout.splitlines() if line.strip()]
    candidate_paths = []
    skipped_paths: list[dict[str, Any]] = []
    for relative_path in tracked_paths:
        if include_globs and not matches_any_glob(relative_path, include_globs):
            skipped_paths.append({"relative_path": relative_path, "reason": "not_included"})
            continue
        if exclude_globs and matches_any_glob(relative_path, exclude_globs):
            skipped_paths.append({"relative_path": relative_path, "reason": "excluded"})
            continue
        candidate_paths.append(relative_path)

    if not candidate_paths:
        blockers.append("no tracked repository paths matched include/exclude globs")

    return {
        "repo_path": top_level,
        "inspected_ref": inspected_ref,
        "resolved_commit": commit,
        "current_branch": current_branch,
        "repo_state": repo_state,
        "candidate_paths": candidate_paths,
        "skipped_paths": skipped_paths,
    }, blockers


def build_plan(adapter_path: Path, adapter_payload: dict[str, Any]) -> dict[str, Any]:
    locator = adapter_payload["locator"]
    repo_path = resolve_path(locator["local_path"], base_dir=adapter_path.parent)
    include_globs = list(locator.get("include_globs", []))
    exclude_globs = list(locator.get("exclude_globs", []))
    configured_ref = locator.get("ref") if isinstance(locator.get("ref"), str) and locator.get("ref").strip() else None

    unsupported_fields = sorted(
        set(adapter_payload["normalized_handoff"].get("source_specific_fields", [])) - LOCAL_GIT_REPO_SOURCE_SPECIFIC_FIELDS
    )
    repo_details, blockers = inspect_repo(
        repo_path,
        locator=locator,
        configured_ref=configured_ref,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
    )
    if unsupported_fields:
        blockers.append(f"unsupported local_git_repo source_specific field: {unsupported_fields[0]}")

    handoff_records: list[dict[str, Any]] = []
    if repo_details is not None:
        handoff_records.append(
            build_local_git_repo_handoff_record(
                adapter_payload,
                adapter_path=adapter_path,
                repo_path=repo_details["repo_path"],
                inspected_ref=repo_details["inspected_ref"],
                resolved_commit=repo_details["resolved_commit"],
                current_branch=repo_details["current_branch"],
                repo_state=repo_details["repo_state"],
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                candidate_paths=repo_details["candidate_paths"],
            )
        )

    validation_errors = [
        {"index": index, "errors": validate_source_adapter_handoff_record(record, adapter_payload)}
        for index, record in enumerate(handoff_records)
    ]
    validation_errors = [entry for entry in validation_errors if entry["errors"]]

    payload = {
        "schema_version": "local-git-repo-plan.v1",
        "adapter_path": str(adapter_path),
        "adapter_id": adapter_payload["adapter_id"],
        "workspace_id": adapter_payload["workspace_id"],
        "input_family": adapter_payload["input_family"],
        "dry_run": True,
        "network_access_attempted": False,
        "remote_operations_attempted": False,
        "resolved_repo_path": str(repo_details["repo_path"]) if repo_details is not None else str(repo_path),
        "configured_ref": configured_ref,
        "inspected_ref": repo_details["inspected_ref"] if repo_details is not None else None,
        "resolved_commit": repo_details["resolved_commit"] if repo_details is not None else None,
        "current_branch": repo_details["current_branch"] if repo_details is not None else None,
        "repo_state": repo_details["repo_state"] if repo_details is not None else "invalid",
        "include_globs": include_globs,
        "exclude_globs": exclude_globs,
        "candidate_count": len(repo_details["candidate_paths"]) if repo_details is not None else 0,
        "skipped_count": len(repo_details["skipped_paths"]) if repo_details is not None else 0,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "candidate_paths": list(repo_details["candidate_paths"]) if repo_details is not None else [],
        "skipped_paths": list(repo_details["skipped_paths"]) if repo_details is not None else [],
        "handoff_record_count": len(handoff_records),
        "handoff_records": handoff_records,
        "handoff_validation": {
            "ok": not validation_errors,
            "error_count": len(validation_errors),
            "errors": validation_errors,
        },
    }
    return payload


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"adapter_id={payload['adapter_id']}",
        f"workspace_id={payload['workspace_id']}",
        f"input_family={payload['input_family']}",
        f"repo_state={payload['repo_state']}",
        f"candidate_count={payload['candidate_count']}",
        f"skipped_count={payload['skipped_count']}",
        f"blocker_count={payload['blocker_count']}",
        f"handoff_record_count={payload['handoff_record_count']}",
        f"resolved_commit={payload['resolved_commit'] or '-'}",
        f"inspected_ref={payload['inspected_ref'] or '-'}",
        f"current_branch={payload['current_branch'] or '-'}",
    ]
    for index, blocker in enumerate(payload["blockers"]):
        lines.append(f"blocker[{index}]={blocker}")
    for index, candidate in enumerate(payload["candidate_paths"][:20]):
        lines.append(f"candidate[{index}]={candidate}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    adapter_path = resolve_path(args.adapter, base_dir=Path.cwd())
    try:
        adapter_payload = load_adapter(adapter_path)
        payload = build_plan(adapter_path, adapter_payload)
        if args.handoff_jsonl is not None:
            atomic_write_jsonl(args.handoff_jsonl, payload["handoff_records"])
    except LocalGitRepoAdapterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(stable_json_text(payload))
    else:
        sys.stdout.write(render_text(payload))
    return 1 if not payload["handoff_validation"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
