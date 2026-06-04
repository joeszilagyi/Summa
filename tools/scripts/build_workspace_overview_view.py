#!/usr/bin/env python3
"""Emit the first read-only workspace overview view model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.topic_workspace_registry import (  # noqa: E402
    REGISTRY_SCHEMA_VERSION,
    TopicWorkspaceRegistryError,
    discover_registry_path,
    load_registry_json,
    resolve_existing_path,
)

SCHEMA_VERSION = "workspace-overview.v1"
PUBLIC_RELEASE_POLICY_CLASS = "public_safe_release"


class WorkspaceOverviewError(RuntimeError):
    """Raised when the overview view model cannot be built."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a read-only workspace overview view model from the topic "
            "workspace registry for dashboard/API consumers."
        )
    )
    parser.add_argument("--registry", help="Optional path to the topic workspace registry JSON file.")
    parser.add_argument(
        "--workspace-id",
        action="append",
        default=[],
        dest="workspace_ids",
        help="Optional workspace_id to include. Repeat to include multiple workspaces.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format for the generated overview.",
    )
    return parser.parse_args()


def require_nonblank_string(value: Any, *, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise WorkspaceOverviewError(f"workspaces[{index}].{field} must be a non-blank trimmed string")
    return value


def load_manifest_summary(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return "unreadable", {}
    except json.JSONDecodeError:
        return "invalid_json", {}
    if not isinstance(payload, dict):
        return "invalid_json", {}
    return "ok", payload


def resolve_workspace_root(workspace: dict[str, Any], *, registry_path: Path) -> tuple[str, str | None]:
    raw_root = workspace.get("workspace_root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        return "not_declared", None

    resolved = resolve_existing_path(raw_root, registry_path)
    if resolved is None:
        return "missing", None
    if not resolved.is_dir():
        return "not_directory", str(resolved)
    return "ok", str(resolved)


def resolve_default_manifest(
    workspace: dict[str, Any],
    *,
    registry_path: Path,
) -> tuple[str, str | None, dict[str, Any]]:
    raw_manifest = workspace.get("default_subject_manifest")
    if not isinstance(raw_manifest, str) or not raw_manifest.strip():
        return "not_declared", None, {}

    resolved = resolve_existing_path(raw_manifest, registry_path)
    if resolved is None:
        return "missing", None, {}
    if not resolved.is_file():
        return "not_file", str(resolved), {}

    manifest_status, manifest_payload = load_manifest_summary(resolved)
    return manifest_status, str(resolved), manifest_payload


def publish_readiness_for(
    workspace: dict[str, Any],
    *,
    workspace_root_status: str,
    manifest_status: str,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []

    if workspace_root_status != "ok":
        blockers.append(f"workspace_root:{workspace_root_status}")
    if manifest_status != "ok":
        blockers.append(f"default_subject_manifest:{manifest_status}")
    if workspace.get("lifecycle_state") != "active":
        blockers.append(f"lifecycle_state:{workspace.get('lifecycle_state')}")
    if workspace.get("workspace_policy_class") != PUBLIC_RELEASE_POLICY_CLASS:
        blockers.append(f"workspace_policy_class:{workspace.get('workspace_policy_class')}")
    if manifest_payload and manifest_payload.get("domain_pack") != workspace.get("domain_pack"):
        blockers.append("domain_pack:mismatch")

    if blockers:
        state = "blocked"
    else:
        state = "needs_validation_review"

    return {
        "state": state,
        "blockers": blockers,
    }


def saturation_visibility_for(workspace: dict[str, Any]) -> dict[str, Any]:
    scheduler_policy = workspace.get("scheduler_policy")
    saturation = scheduler_policy.get("saturation_state") if isinstance(scheduler_policy, dict) else None
    if not isinstance(saturation, dict):
        return {
            "state": "not_evaluated",
            "scheduler_action": "run",
            "reason_codes": ["not_evaluated"],
            "interpretation": "No saturation evaluation is recorded for this workspace.",
        }
    state = str(saturation.get("state") or "not_evaluated")
    action = str(saturation.get("scheduler_action") or "run")
    raw_reasons = saturation.get("reason_codes")
    reason_codes = [str(reason) for reason in raw_reasons] if isinstance(raw_reasons, list) else []
    if state in {"saturated", "cooldown"}:
        interpretation = f"Workspace is {state}; scheduler action is {action}."
    else:
        interpretation = "Workspace is not saturated under the recorded policy."
    return {
        "state": state,
        "scheduler_action": action,
        "reason_codes": reason_codes,
        "policy_id": saturation.get("policy_id"),
        "evaluated_at": saturation.get("evaluated_at"),
        "next_eligible_cycle": saturation.get("next_eligible_cycle"),
        "recent_yield_summary": saturation.get("recent_yield_summary"),
        "interpretation": interpretation,
    }


def workspace_entry(
    workspace: dict[str, Any],
    *,
    index: int,
    registry_path: Path,
) -> dict[str, Any]:
    workspace_id = require_nonblank_string(workspace.get("workspace_id"), field="workspace_id", index=index)
    workspace_root_status, resolved_root = resolve_workspace_root(workspace, registry_path=registry_path)
    manifest_status, resolved_manifest, manifest_payload = resolve_default_manifest(
        workspace,
        registry_path=registry_path,
    )
    readiness = publish_readiness_for(
        workspace,
        workspace_root_status=workspace_root_status,
        manifest_status=manifest_status,
        manifest_payload=manifest_payload,
    )
    saturation = saturation_visibility_for(workspace)

    entry: dict[str, Any] = {
        "workspace_id": workspace_id,
        "topic_label": workspace.get("topic_label"),
        "domain_pack": workspace.get("domain_pack"),
        "lifecycle_state": workspace.get("lifecycle_state"),
        "schedule_posture": workspace.get("schedule_posture"),
        "workspace_policy_class": workspace.get("workspace_policy_class"),
        "workspace_root": workspace.get("workspace_root"),
        "workspace_root_status": workspace_root_status,
        "default_subject_manifest": workspace.get("default_subject_manifest"),
        "default_subject_manifest_status": manifest_status,
        "publish_readiness": readiness,
        "saturation": saturation,
    }

    if resolved_root is not None:
        entry["resolved_workspace_root"] = resolved_root
    if resolved_manifest is not None:
        entry["resolved_default_subject_manifest"] = resolved_manifest
    if manifest_payload:
        entry["manifest_subject_id"] = manifest_payload.get("subject_id")
        entry["manifest_display_name"] = manifest_payload.get("display_name")
        entry["manifest_domain_pack"] = manifest_payload.get("domain_pack")

    return entry


def selected_workspace_records(
    workspaces: list[Any],
    *,
    workspace_ids: list[str],
) -> list[tuple[int, dict[str, Any]]]:
    requested = set(workspace_ids)
    selected: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()

    for index, workspace in enumerate(workspaces):
        if not isinstance(workspace, dict):
            raise WorkspaceOverviewError(f"workspaces[{index}] must be an object")
        workspace_id = require_nonblank_string(
            workspace.get("workspace_id"),
            field="workspace_id",
            index=index,
        )
        if workspace_id in seen:
            raise WorkspaceOverviewError(f"duplicate workspace_id in topic workspace registry: {workspace_id}")
        seen.add(workspace_id)
        if requested and workspace_id not in requested:
            continue
        selected.append((index, workspace))

    missing = [workspace_id for workspace_id in workspace_ids if workspace_id not in seen]
    if missing:
        raise WorkspaceOverviewError("workspace_id not found in topic workspace registry: " + ", ".join(missing))

    return selected


def build_overview_payload(args: argparse.Namespace) -> dict[str, Any]:
    registry_path = discover_registry_path(args.registry)
    if not registry_path.exists():
        raise WorkspaceOverviewError(f"topic workspace registry not found: {registry_path}")
    if not registry_path.is_file():
        raise WorkspaceOverviewError(f"topic workspace registry is not a file: {registry_path}")

    try:
        registry_payload = load_registry_json(registry_path)
    except TopicWorkspaceRegistryError as exc:
        raise WorkspaceOverviewError(str(exc)) from exc

    if registry_payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise WorkspaceOverviewError(
            "topic workspace registry schema version mismatch; "
            f"expected {REGISTRY_SCHEMA_VERSION}"
        )
    workspaces = registry_payload.get("workspaces")
    if not isinstance(workspaces, list):
        raise WorkspaceOverviewError("topic workspace registry workspaces field must be an array")

    entries = [
        workspace_entry(workspace, index=index, registry_path=registry_path)
        for index, workspace in selected_workspace_records(workspaces, workspace_ids=args.workspace_ids)
    ]
    counts = {
        "total_workspaces": len(entries),
        "active_workspaces": sum(1 for entry in entries if entry["lifecycle_state"] == "active"),
        "scheduled_workspaces": sum(1 for entry in entries if entry["schedule_posture"] == "scheduled"),
        "saturated_workspaces": sum(
            1 for entry in entries if entry["saturation"]["state"] in {"saturated", "cooldown"}
        ),
        "workspace_root_ok": sum(1 for entry in entries if entry["workspace_root_status"] == "ok"),
        "default_subject_manifest_ok": sum(
            1 for entry in entries if entry["default_subject_manifest_status"] == "ok"
        ),
        "publish_blocked": sum(
            1 for entry in entries if entry["publish_readiness"]["state"] == "blocked"
        ),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "registry_path": str(registry_path),
        "requested_workspace_ids": list(args.workspace_ids),
        "counts": counts,
        "workspaces": entries,
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"schema_version={payload['schema_version']}",
        f"registry_path={payload['registry_path']}",
        f"workspace_count={payload['counts']['total_workspaces']}",
        f"publish_blocked={payload['counts']['publish_blocked']}",
    ]
    for index, workspace in enumerate(payload["workspaces"]):
        lines.append(f"workspace[{index}].workspace_id={workspace['workspace_id']}")
        lines.append(f"workspace[{index}].root_status={workspace['workspace_root_status']}")
        lines.append(
            f"workspace[{index}].manifest_status={workspace['default_subject_manifest_status']}"
        )
        lines.append(
            f"workspace[{index}].publish_readiness={workspace['publish_readiness']['state']}"
        )
        lines.append(f"workspace[{index}].saturation={workspace['saturation']['state']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = build_overview_payload(args)
    except WorkspaceOverviewError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
