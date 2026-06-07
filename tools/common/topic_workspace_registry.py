"""Helpers for loading and resolving topic workspace registry records."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_CONFIG_ROOT = REPO_ROOT / "config"
LOCAL_REGISTRY_ROOT = REPO_ROOT / "runtime" / "config"
DEFAULT_REGISTRY_ENV = "INDEXER_TOPIC_WORKSPACE_REGISTRY"
DEFAULT_REGISTRY_PATH = LOCAL_REGISTRY_ROOT / "topic_workspaces.local.json"
REGISTRY_SCHEMA_VERSION = "topic-workspace-registry.v1"

try:
    from tools.common.atomic_write import atomic_write_json
except ImportError:  # pragma: no cover - direct script path fallback
    from atomic_write import atomic_write_json  # type: ignore


class TopicWorkspaceRegistryError(RuntimeError):
    """Raised when registry discovery or resolution fails."""


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def resolve_registry_path(
    raw_path: str | Path,
    *,
    base_dir: Path | None = None,
) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    anchor = base_dir or Path.cwd()
    return (anchor / path).resolve()


def discover_registry_path(
    explicit_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    if explicit_path is not None:
        return resolve_registry_path(explicit_path, base_dir=cwd)

    env_map = os.environ if env is None else env
    raw_env = env_map.get(DEFAULT_REGISTRY_ENV)
    if raw_env:
        return resolve_registry_path(raw_env, base_dir=cwd)

    return DEFAULT_REGISTRY_PATH


def load_registry_json(path: Path) -> dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(
            raw_text,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
    except OSError as exc:
        raise TopicWorkspaceRegistryError(f"could not read topic workspace registry: {path}") from exc
    except UnicodeDecodeError as exc:
        raise TopicWorkspaceRegistryError(f"could not decode topic workspace registry as UTF-8: {path}") from exc
    except DuplicateJsonKeyError as exc:
        raise TopicWorkspaceRegistryError(f"topic workspace registry contains {exc}") from exc
    except NonStandardJsonConstantError as exc:
        raise TopicWorkspaceRegistryError(f"topic workspace registry contains {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TopicWorkspaceRegistryError(
            f"could not parse topic workspace registry: {path} (line {exc.lineno})"
        ) from exc

    if not isinstance(payload, dict):
        raise TopicWorkspaceRegistryError(
            f"topic workspace registry must contain a JSON object: {path}"
        )

    return payload


def _validate_registry_schema(payload: Any) -> None:
    schema_version = payload.get("schema_version")
    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise TopicWorkspaceRegistryError(
            f"topic workspace registry schema version mismatch in {REGISTRY_SCHEMA_VERSION}; "
            f"got {schema_version!r}"
        )


def initialize_registry_payload() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "workspaces": [],
    }


def load_or_initialize_registry_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return initialize_registry_payload()
    return load_registry_json(path)


def resolve_existing_path(raw_value: str, registry_path: Path) -> Path | None:
    raw = Path(raw_value).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend(
            [
                (registry_path.parent / raw).resolve(),
                (REPO_ROOT / raw).resolve(),
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_tracked_registry_path(path: Path) -> bool:
    return is_path_within(path, TRACKED_CONFIG_ROOT)


def reference_path_for_registry(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def write_registry_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _resolve_workspace_record(
    selected: dict[str, Any],
    *,
    selected_id: str,
    resolved_registry_path: Path,
) -> dict[str, Any]:
    raw_root = selected.get("workspace_root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise TopicWorkspaceRegistryError(
            f"workspace_root is missing or invalid for workspace_id: {selected_id}"
        )

    resolved_root = resolve_existing_path(raw_root, resolved_registry_path)
    if resolved_root is None:
        raise TopicWorkspaceRegistryError(
            f"workspace root path not found for workspace_id {selected_id}: {raw_root}"
        )
    if not resolved_root.is_dir():
        raise TopicWorkspaceRegistryError(
            f"workspace root path is not a directory for workspace_id {selected_id}: {raw_root}"
        )

    resolved = dict(selected)
    resolved["registry_path"] = resolved_registry_path
    resolved["resolved_workspace_root"] = resolved_root

    raw_manifest = selected.get("default_subject_manifest")
    if isinstance(raw_manifest, str) and raw_manifest.strip():
        resolved_manifest = resolve_existing_path(raw_manifest, resolved_registry_path)
        if resolved_manifest is None:
            raise TopicWorkspaceRegistryError(
                f"default subject manifest path not found for workspace_id {selected_id}: {raw_manifest}"
            )
        if not resolved_manifest.is_file():
            raise TopicWorkspaceRegistryError(
                f"default subject manifest path is not a file for workspace_id {selected_id}: {raw_manifest}"
            )
        resolved["resolved_default_subject_manifest"] = resolved_manifest

    return resolved


def _normalize_workspace_id(
    raw_value: Any,
    *,
    invalid_message: str,
    source_label: str,
) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise TopicWorkspaceRegistryError(invalid_message)

    normalized_workspace_id = raw_value.strip()
    if normalized_workspace_id != raw_value:
        raise TopicWorkspaceRegistryError(
            f"{source_label} has leading/trailing whitespace in topic workspace registry: {raw_value!r}"
        )
    return normalized_workspace_id


def require_workspace_id_for_production_write(
    workspace_id: str | None,
    *,
    operation: str,
    dry_run: bool = False,
    fixture_mode: bool = False,
) -> str | None:
    """Require an explicit workspace_id before production write operations.

    Dry-run and fixture-mode callers are allowed to return ``None`` because they
    must not mutate production state. Real write paths should call this before
    creating DBs, ledgers, sidecars, or registry-affecting artifacts.
    """
    if dry_run or fixture_mode:
        if workspace_id is None:
            return None
        return _normalize_workspace_id(
            workspace_id,
            invalid_message=f"workspace_id is missing or invalid for {operation}",
            source_label="workspace_id",
        )
    return _normalize_workspace_id(
        workspace_id,
        invalid_message=f"workspace_id is required for production write operation: {operation}",
        source_label="workspace_id",
    )


def _workspace_id_from_registry_record(
    workspace: Any,
    *,
    seen_workspace_ids: set[str],
) -> str:
    if not isinstance(workspace, dict):
        raise TopicWorkspaceRegistryError("topic workspace registry workspaces entries must be objects")

    normalized_workspace_id = _normalize_workspace_id(
        workspace.get("workspace_id"),
        invalid_message="workspace_id is missing or invalid in topic workspace registry",
        source_label="workspace_id",
    )

    if normalized_workspace_id in seen_workspace_ids:
        raise TopicWorkspaceRegistryError(
            f"duplicate workspace_id in topic workspace registry: {normalized_workspace_id}"
        )
    seen_workspace_ids.add(normalized_workspace_id)

    return normalized_workspace_id


def build_workspace_index(workspaces: list[Any]) -> dict[str, dict[str, Any]]:
    workspace_index: dict[str, dict[str, Any]] = {}
    seen_workspace_ids: set[str] = set()
    for workspace in workspaces:
        workspace_id = _workspace_id_from_registry_record(
            workspace,
            seen_workspace_ids=seen_workspace_ids,
        )
        workspace_index[workspace_id] = workspace
    return workspace_index


def resolve_workspace(
    *,
    registry_path: str | Path | None = None,
    workspace_id: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    resolved_registry_path = discover_registry_path(registry_path, env=env, cwd=cwd)
    payload = load_registry_json(resolved_registry_path)
    _validate_registry_schema(payload)

    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        raise TopicWorkspaceRegistryError("topic workspace registry must contain a non-empty workspaces array")

    selected_id = workspace_id
    if selected_id is None:
        default_workspace_id = payload.get("default_workspace_id")
        if isinstance(default_workspace_id, str) and default_workspace_id.strip():
            selected_id = default_workspace_id
        elif len(workspaces) == 1:
            candidate = workspaces[0]
            selected_id = candidate.get("workspace_id") if isinstance(candidate, dict) else None

    selected_id = _normalize_workspace_id(
        selected_id,
        invalid_message="workspace_id is required when no default workspace can be resolved",
        source_label="workspace_id",
    )

    workspace_index = build_workspace_index(workspaces)
    selected = workspace_index.get(selected_id)
    if selected is None:
        raise TopicWorkspaceRegistryError(f"workspace_id not found in topic workspace registry: {selected_id}")

    return _resolve_workspace_record(
        selected,
        selected_id=selected_id,
        resolved_registry_path=resolved_registry_path,
    )


def resolve_workspaces(
    *,
    registry_path: str | Path | None = None,
    workspace_ids: list[str] | tuple[str, ...] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    resolved_registry_path = discover_registry_path(registry_path, env=env, cwd=cwd)
    payload = load_registry_json(resolved_registry_path)
    _validate_registry_schema(payload)

    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list) or not workspaces:
        raise TopicWorkspaceRegistryError("topic workspace registry must contain a non-empty workspaces array")

    requested_ids = []
    requested_id_set = set()
    for workspace_id in workspace_ids or []:
        normalized_workspace_id = _normalize_workspace_id(
            workspace_id,
            invalid_message="requested workspace_id is missing or invalid in topic workspace registry",
            source_label="requested workspace_id",
        )
        requested_ids.append(normalized_workspace_id)
        requested_id_set.add(normalized_workspace_id)
    workspace_index = build_workspace_index(workspaces)
    available_ids = set(workspace_index)

    missing_ids = [workspace_id for workspace_id in requested_ids if workspace_id not in available_ids]
    if missing_ids:
        raise TopicWorkspaceRegistryError(
            "workspace_id not found in topic workspace registry: " + ", ".join(missing_ids)
        )

    selected_records: list[tuple[str, dict[str, Any]]] = []
    if requested_id_set:
        for workspace_id in requested_ids:
            workspace = workspace_index.get(workspace_id)
            if workspace is not None:
                selected_records.append((workspace_id, workspace))
    else:
        selected_records = [(workspace_id, workspace) for workspace_id, workspace in workspace_index.items()]

    return [
        _resolve_workspace_record(
            workspace,
            selected_id=workspace_id,
            resolved_registry_path=resolved_registry_path,
        )
        for workspace_id, workspace in selected_records
    ]
