#!/usr/bin/env python3
"""Plan a concrete crown-jewel backup manifest from the tracked durability policy."""

from __future__ import annotations

import argparse
import json
import tempfile
import sys
from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
DEFAULT_POLICY_PATH = REPO_ROOT / "config" / "durability_policies" / "local_first_crown_jewels.v1.json"
MANIFEST_SCHEMA_VERSION = "crown-jewel-backup-manifest.v1"
STATUS_PRESENT = "present"
STATUS_MISSING_ALLOWED = "missing_allowed"
STATUS_MISSING_REQUIRED = "missing_required"
MANIFEST_VALIDATION_TMP_PREFIX = ".crown_jewel_backup_manifest.validation."
MANIFEST_VALIDATION_TMP_SUFFIX = ".tmp.json"
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

import validate_crown_jewel_backup_manifest  # noqa: E402
import validate_crown_jewel_store_policy  # noqa: E402


class BackupPlanError(RuntimeError):
    """Raised when the backup planner inputs or outputs are invalid."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve the current crown-jewel store policy into a concrete local "
            "backup manifest for the current repo checkout."
        )
    )
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY_PATH),
        help="Path to the crown-jewel store policy JSON file.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repo root to inspect when expanding store-family path globs.",
    )
    parser.add_argument(
        "--store-key",
        action="append",
        default=[],
        dest="store_keys",
        help="Optional store_key to include. Repeat to target multiple store families.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the planned backup manifest.",
    )
    return parser.parse_args()


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BackupPlanError(f"could not read {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BackupPlanError(f"could not parse {label}: {path} (line {exc.lineno})") from exc

    if not isinstance(payload, dict):
        raise BackupPlanError(f"{label} must contain a JSON object: {path}")
    return payload


def validate_policy_or_raise(policy_path: Path) -> dict[str, Any]:
    result, exit_code = validate_crown_jewel_store_policy.validate_crown_jewel_store_policy(policy_path)
    if exit_code != validate_crown_jewel_store_policy.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            raise BackupPlanError(errors[0].get("message", "crown-jewel store policy validation failed"))
        raise BackupPlanError("crown-jewel store policy validation failed")
    return load_json_object(policy_path, label="crown-jewel store policy")


def validate_manifest_or_raise(manifest_path: Path) -> None:
    result, exit_code = validate_crown_jewel_backup_manifest.validate_crown_jewel_backup_manifest(manifest_path)
    if exit_code != validate_crown_jewel_backup_manifest.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            raise BackupPlanError(errors[0].get("message", "crown-jewel backup manifest validation failed"))
        raise BackupPlanError("crown-jewel backup manifest validation failed")


def repo_relative_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


@lru_cache(maxsize=1024)
def cached_glob_matches(repo_root_value: str, pattern: str) -> tuple[str, ...]:
    repo_root = Path(repo_root_value)
    try:
        candidates = repo_root.glob(pattern)
    except ValueError as exc:
        raise BackupPlanError(f"invalid path glob in store policy: {pattern!r}") from exc
    matched = []
    for candidate in candidates:
        if candidate.exists():
            matched.append(repo_relative_path(candidate, repo_root))
    return tuple(sorted(matched))


def resolve_matched_paths(repo_root: Path, path_globs: list[str]) -> list[str]:
    matched: set[str] = set()
    repo_root_value = str(repo_root.resolve())
    for pattern in path_globs:
        matched.update(cached_glob_matches(repo_root_value, pattern))
    return sorted(matched)


def plan_backup_manifest(
    *,
    policy_path: Path,
    repo_root: Path,
    requested_store_keys: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    policy = validate_policy_or_raise(policy_path)

    requested = [store_key for store_key in (requested_store_keys or []) if store_key]
    available_store_keys = {
        store["store_key"]
        for store in policy.get("store_families", [])
        if isinstance(store, dict) and isinstance(store.get("store_key"), str)
    }
    missing = [store_key for store_key in requested if store_key not in available_store_keys]
    if missing:
        raise BackupPlanError("store_key not found in crown-jewel store policy: " + ", ".join(missing))

    store_entries: list[dict[str, Any]] = []
    for store in policy.get("store_families", []):
        if not isinstance(store, dict):
            continue
        store_key = store.get("store_key")
        if requested and store_key not in requested:
            continue

        path_globs = list(store["path_globs"])
        matched_paths = resolve_matched_paths(repo_root, path_globs)
        missing_ok = bool(store["missing_ok"])
        if matched_paths:
            status = STATUS_PRESENT
        elif missing_ok:
            status = STATUS_MISSING_ALLOWED
        else:
            status = STATUS_MISSING_REQUIRED

        store_entries.append(
            {
                "store_key": store["store_key"],
                "display_name": store["display_name"],
                "path_globs": path_globs,
                "durability_class": store["durability_class"],
                "storage_policy_class": store["storage_policy_class"],
                "backup_frequency_expectation": store["backup_frequency_expectation"],
                "restore_expectation": store["restore_expectation"],
                "integrity_check_method": store["integrity_check_method"],
                "silent_replace_forbidden": store["silent_replace_forbidden"],
                "missing_ok": store["missing_ok"],
                "status": status,
                "match_count": len(matched_paths),
                "matched_paths": matched_paths,
                "notes": list(store.get("notes", [])),
            }
        )

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "policy_id": policy["policy_id"],
        "policy_path": repo_relative_path(policy_path, repo_root),
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_root": str(repo_root.resolve()),
        "backup_root": policy["backup_root"],
        "requested_store_keys": requested,
        "store_entries": store_entries,
    }

    manifest_parent = repo_root / "runtime" / "config"
    manifest_parent.mkdir(parents=True, exist_ok=True)

    temp_manifest = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=manifest_parent,
            prefix=MANIFEST_VALIDATION_TMP_PREFIX,
            suffix=MANIFEST_VALIDATION_TMP_SUFFIX,
            delete=False,
        ) as temp_file:
            temp_manifest = Path(temp_file.name)
            json.dump(manifest, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
        validate_manifest_or_raise(temp_manifest)
    finally:
        if temp_manifest is not None:
            temp_manifest.unlink(missing_ok=True)

    return manifest


def render_text(manifest: dict[str, Any]) -> str:
    lines = [
        f"policy_id={manifest['policy_id']}",
        f"policy_path={manifest['policy_path']}",
        f"backup_root={manifest['backup_root']}",
        f"store_count={len(manifest['store_entries'])}",
    ]
    for index, entry in enumerate(manifest["store_entries"]):
        lines.append(f"store[{index}].store_key={entry['store_key']}")
        lines.append(f"store[{index}].status={entry['status']}")
        lines.append(f"store[{index}].match_count={entry['match_count']}")
        if entry["matched_paths"]:
            for path_index, matched_path in enumerate(entry["matched_paths"]):
                lines.append(f"store[{index}].matched_path[{path_index}]={matched_path}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    policy_path = Path(args.policy).expanduser().resolve()
    repo_root = Path(args.repo_root).expanduser().resolve()

    try:
        manifest = plan_backup_manifest(
            policy_path=policy_path,
            repo_root=repo_root,
            requested_store_keys=args.store_keys,
        )
    except BackupPlanError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
