#!/usr/bin/env python3
"""Bootstrap a new topic workspace and register it in the local registry.

Implementation for ``tools/scripts/Index_New_Topic.sh``.
Documentation: ``docs/scripts/index_new_topic.md``.
When modifying this tool, update the paired documentation and wrapper tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

from tools.common.topic_workspace_registry import (  # noqa: E402
    discover_registry_path,
    is_path_within,
    is_tracked_registry_path,
    load_or_initialize_registry_json,
    reference_path_for_registry,
    resolve_existing_path,
    write_registry_json,
)
from tools.validators import (  # noqa: E402
    validate_subject_manifest,
    validate_topic_workspace_registry,
)

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class BootstrapError(RuntimeError):
    """Raised when bootstrap inputs or writes are unsafe."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Index_New_Topic.sh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Create one isolated topic workspace root, write a bootstrap subject "
            "manifest and subject brief, and register the workspace in the local "
            "topic workspace registry."
        ),
        epilog=(
            "Side effects:\n"
            "  Creates the workspace root, .indexer/subject_manifest.json, source.txt,\n"
            "  state/, runs/, and updates the topic workspace registry unless --dry-run\n"
            "  is set. Run only one bootstrap writer per registry at a time; registry\n"
            "  replacement is atomic, but this tool does not take a cross-process lock.\n\n"
            "Environment:\n"
            "  INDEXER_TOPIC_WORKSPACE_REGISTRY overrides the default local registry\n"
            "  path when --registry is not supplied.\n\n"
            "Example:\n"
            "  tools/scripts/Index_New_Topic.sh --non-interactive --format json \\\n"
            "    --topic-label 'Monarch butterflies' \\\n"
            '    --workspace-root "$HOME/indexer-workspaces/monarch_butterflies" \\\n'
            "    --domain-pack organism.v1\n"
        ),
    )
    parser.add_argument(
        "--registry", help="Optional path to the topic workspace registry JSON file."
    )
    parser.add_argument(
        "--allow-tracked-registry",
        action="store_true",
        help="Allow writing a registry file under tracked config/ paths.",
    )
    parser.add_argument("--topic-label", help="Human-readable label for the topic/workspace.")
    parser.add_argument(
        "--workspace-id", help="Stable workspace identifier. Defaults from topic label."
    )
    parser.add_argument(
        "--workspace-root",
        help="New isolated root directory for this topic workspace. Must not already exist.",
    )
    parser.add_argument("--domain-pack", help="Domain pack ID such as general.v1 or organism.v1.")
    parser.add_argument(
        "--subject-id",
        help="Subject manifest ID. Defaults from the domain-pack family and workspace ID.",
    )
    parser.add_argument("--display-name", help="Subject display name. Defaults from topic label.")
    parser.add_argument(
        "--scope-statement", help="Subject scope statement. Defaults to a bootstrap scaffold."
    )
    parser.add_argument(
        "--languages",
        help="Comma-separated languages. Defaults to en.",
    )
    parser.add_argument(
        "--aliases",
        help="Comma-separated aliases. Defaults to the topic label.",
    )
    parser.add_argument(
        "--disambiguation-terms",
        help="Comma-separated disambiguation terms.",
    )
    parser.add_argument(
        "--excluded-senses",
        help="Comma-separated excluded senses.",
    )
    parser.add_argument(
        "--enabled-facets",
        help="Comma-separated enabled facets. Defaults to every facet in the chosen domain pack.",
    )
    parser.add_argument(
        "--query-families",
        help="Comma-separated query families. Defaults to every query family in the chosen domain pack.",
    )
    parser.add_argument(
        "--schedule-posture",
        choices=("manual", "scheduled", "paused"),
        default="manual",
        help="Initial scheduler posture for the new workspace.",
    )
    parser.add_argument(
        "--workspace-policy-class",
        choices=("private_local", "mixed_private_public", "public_safe_release"),
        default="private_local",
        help="Initial workspace policy class for the new workspace.",
    )
    parser.add_argument(
        "--lifecycle-state",
        choices=("bootstrap", "active", "paused", "archived"),
        default="bootstrap",
        help="Initial lifecycle state for the new workspace.",
    )
    parser.add_argument(
        "--set-default",
        action="store_true",
        help="Set the new workspace as default in the registry.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Refuse prompting and require all mandatory inputs to be resolved from flags/defaults.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print the planned scaffold without creating files or updating the registry.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format describing the created scaffold.",
    )
    return parser.parse_args()


def slugify_identifier(raw_value: str, *, label: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "_", raw_value.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("._-")
    if not slug or not ID_PATTERN.fullmatch(slug):
        raise BootstrapError(f"could not derive a valid identifier for {label}: {raw_value!r}")
    return slug


def parse_csv(raw_value: str | None) -> list[str]:
    if raw_value is None:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for piece in raw_value.split(","):
        item = piece.strip()
        if not item or item in seen:
            continue
        items.append(item)
        seen.add(item)
    return items


def prompt_or_default(
    raw_value: str | None,
    *,
    prompt: str,
    default: str | None = None,
    required: bool = False,
    non_interactive: bool,
) -> str | None:
    if raw_value is not None and raw_value.strip():
        return raw_value.strip()

    if non_interactive or not sys.stdin.isatty():
        if required and default is None:
            raise BootstrapError(f"{prompt} is required")
        return default

    while True:
        suffix = f" [{default}]" if default is not None else ""
        response = input(f"{prompt}{suffix}: ").strip()
        if response:
            return response
        if default is not None:
            return default
        if not required:
            return None
        print("Value is required.", file=sys.stderr)


def load_domain_pack(domain_pack: str) -> dict[str, Any]:
    pack_path = REPO_ROOT / "config" / "domain_packs" / f"{domain_pack}.json"
    try:
        payload = json.loads(pack_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BootstrapError(f"domain pack file not found: {pack_path}") from exc
    except UnicodeDecodeError as exc:
        raise BootstrapError(
            f"domain pack file could not be decoded as UTF-8: {pack_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            f"domain pack file could not be parsed: {pack_path} (line {exc.lineno})"
        ) from exc
    if not isinstance(payload, dict):
        raise BootstrapError(f"domain pack file must contain a JSON object: {pack_path}")
    return payload


def unique_subset(selected: list[str], allowed: list[str], *, label: str) -> list[str]:
    if not allowed:
        raise BootstrapError(f"{label} is empty in the chosen domain pack")
    allowed_set = set(allowed)
    if not selected:
        return list(allowed)

    normalized: list[str] = []
    seen: set[str] = set()
    for item in selected:
        if item not in allowed_set:
            raise BootstrapError(f"{label[:-1]} not enabled by the chosen domain pack: {item}")
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized


def resolve_workspace_root(raw_value: str) -> Path:
    return Path(raw_value).expanduser().resolve()


def ensure_bootstrap_safe_registry_path(path: Path, *, allow_tracked_registry: bool) -> None:
    if not allow_tracked_registry and is_tracked_registry_path(path):
        raise BootstrapError(
            "refusing to write topic workspace registry under tracked config/; "
            "use the local runtime/config registry or pass --allow-tracked-registry explicitly"
        )


def build_scope_statement(topic_label: str, domain_pack: str) -> str:
    return (
        f"Bootstrap-generated scope statement for {topic_label} under domain pack "
        f"{domain_pack}. Refine this before unattended production use."
    )


def build_subject_manifest(
    *,
    subject_id: str,
    display_name: str,
    domain_pack: str,
    scope_statement: str,
    languages: list[str],
    aliases: list[str],
    disambiguation_terms: list[str],
    excluded_senses: list[str],
    enabled_facets: list[str],
    query_families: list[str],
    workspace_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "subject-manifest.v1",
        "subject_id": subject_id,
        "display_name": display_name,
        "domain_pack": domain_pack,
        "scope_statement": scope_statement,
        "languages": languages,
        "aliases": aliases,
        "disambiguation_terms": disambiguation_terms,
        "excluded_senses": excluded_senses,
        "enabled_facets": enabled_facets,
        "query_families": query_families,
        "notes": [
            (
                "Bootstrap-generated scaffold. Review scope_statement, languages, "
                "facets, and source brief before unattended production use."
            )
        ],
        "legacy_substrate_paths": [reference_path_for_registry(workspace_root)],
        "public_export_default": False,
    }


def build_source_brief(
    *,
    topic_label: str,
    display_name: str,
    scope_statement: str,
) -> str:
    return (
        f"{display_name}\n"
        f"{'=' * len(display_name)}\n\n"
        "Bootstrap-generated subject brief.\n\n"
        f"Topic label: {topic_label}\n\n"
        f"Scope statement:\n{scope_statement}\n\n"
        "This file is an initial local substrate created by Index_New_Topic.sh.\n"
        "Replace or expand it before treating the workspace as production-ready.\n"
    )


def render_json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def build_registry_entry(
    *,
    workspace_id: str,
    topic_label: str,
    workspace_root: Path,
    domain_pack: str,
    manifest_path: Path,
    lifecycle_state: str,
    schedule_posture: str,
    workspace_policy_class: str,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "topic_label": topic_label,
        "workspace_root": reference_path_for_registry(workspace_root),
        "domain_pack": domain_pack,
        "default_subject_manifest": reference_path_for_registry(manifest_path),
        "lifecycle_state": lifecycle_state,
        "schedule_posture": schedule_posture,
        "workspace_policy_class": workspace_policy_class,
        "notes": ["Bootstrap-created workspace entry. Review before unattended production use."],
    }


def build_result_payload(
    *,
    registry_path: Path,
    workspace_id: str,
    workspace_root: Path,
    manifest_path: Path,
    source_brief_path: Path,
    default_workspace_id: Any,
    created_paths: list[Path],
    dry_run: bool = False,
    registry_action: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "registry_path": str(registry_path),
        "workspace_id": workspace_id,
        "workspace_root": str(workspace_root),
        "subject_manifest_path": str(manifest_path),
        "source_brief_path": str(source_brief_path),
        "default_workspace_id": default_workspace_id,
    }
    if dry_run:
        payload["dry_run"] = True
        payload["registry_action"] = registry_action
        payload["planned_created_paths"] = [str(path) for path in created_paths]
    else:
        payload["created_paths"] = [str(path) for path in created_paths]
    return payload


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        assert temp_path is not None
        temp_path.replace(path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def validate_manifest_or_raise(manifest_path: Path) -> None:
    result, exit_code = validate_subject_manifest.validate_manifest(manifest_path)
    if exit_code != validate_subject_manifest.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            raise BootstrapError(errors[0].get("message", "subject manifest validation failed"))
        raise BootstrapError("subject manifest validation failed")


def validate_manifest_payload_or_raise(manifest_payload: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="bootstrap-topic-manifest-") as temp_dir:
        manifest_path = Path(temp_dir) / "subject_manifest.json"
        manifest_path.write_text(render_json_payload(manifest_payload), encoding="utf-8")
        validate_manifest_or_raise(manifest_path)


def validate_registry_or_raise(registry_path: Path) -> None:
    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(
        registry_path
    )
    if exit_code != validate_topic_workspace_registry.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            raise BootstrapError(
                errors[0].get("message", "topic workspace registry validation failed")
            )
        raise BootstrapError("topic workspace registry validation failed")


def bootstrap_workspace(args: argparse.Namespace) -> dict[str, Any]:
    topic_label = prompt_or_default(
        args.topic_label,
        prompt="Topic label",
        required=True,
        non_interactive=args.non_interactive,
    )
    assert topic_label is not None

    workspace_id = args.workspace_id
    if workspace_id is None:
        workspace_id = slugify_identifier(topic_label, label="workspace_id")
    else:
        workspace_id = slugify_identifier(workspace_id, label="workspace_id")

    domain_pack = prompt_or_default(
        args.domain_pack,
        prompt="Domain pack",
        required=True,
        non_interactive=args.non_interactive,
    )
    assert domain_pack is not None
    if not ID_PATTERN.fullmatch(domain_pack):
        raise BootstrapError("domain_pack must match ^[a-z0-9][a-z0-9._-]*$")
    pack = load_domain_pack(domain_pack)

    workspace_root_raw = prompt_or_default(
        args.workspace_root,
        prompt="Workspace root",
        required=True,
        non_interactive=args.non_interactive,
    )
    assert workspace_root_raw is not None
    workspace_root = resolve_workspace_root(workspace_root_raw)

    registry_path = discover_registry_path(args.registry)
    ensure_bootstrap_safe_registry_path(
        registry_path, allow_tracked_registry=args.allow_tracked_registry
    )

    existing_registry = load_or_initialize_registry_json(registry_path)
    registry_existed_before = registry_path.exists()
    original_registry_text = (
        registry_path.read_text(encoding="utf-8") if registry_existed_before else None
    )
    if registry_path.exists():
        validate_registry_or_raise(registry_path)

    workspaces = existing_registry.get("workspaces")
    if not isinstance(workspaces, list):
        raise BootstrapError("existing topic workspace registry has an invalid workspaces array")

    for workspace in workspaces:
        if not isinstance(workspace, dict):
            continue
        if workspace.get("workspace_id") == workspace_id:
            raise BootstrapError(f"workspace_id already exists in the registry: {workspace_id}")

        raw_existing_root = workspace.get("workspace_root")
        if isinstance(raw_existing_root, str) and raw_existing_root.strip():
            existing_resolved_root = resolve_existing_path(raw_existing_root, registry_path)
            if existing_resolved_root is not None:
                if existing_resolved_root.resolve() == workspace_root:
                    raise BootstrapError(
                        f"workspace_root is already claimed by workspace_id {workspace.get('workspace_id')}: {workspace_root}"
                    )
                if is_path_within(workspace_root, existing_resolved_root) or is_path_within(
                    existing_resolved_root, workspace_root
                ):
                    raise BootstrapError(
                        "workspace_root overlaps an existing workspace root and would break topic isolation: "
                        f"{workspace_root} vs {existing_resolved_root}"
                    )

    if workspace_root.exists():
        raise BootstrapError(
            f"workspace_root already exists; choose a fresh isolated root instead of reusing: {workspace_root}"
        )

    subject_id = args.subject_id
    if subject_id is None:
        subject_id = f"{slugify_identifier(domain_pack.split('.', 1)[0], label='domain pack family')}.{workspace_id}"
    if not ID_PATTERN.fullmatch(subject_id):
        raise BootstrapError("subject_id must match ^[a-z0-9][a-z0-9._-]*$")

    display_name = prompt_or_default(
        args.display_name,
        prompt="Display name",
        default=topic_label,
        required=True,
        non_interactive=args.non_interactive,
    )
    assert display_name is not None

    scope_statement = prompt_or_default(
        args.scope_statement,
        prompt="Scope statement",
        default=build_scope_statement(topic_label, domain_pack),
        required=True,
        non_interactive=args.non_interactive,
    )
    assert scope_statement is not None

    languages = parse_csv(args.languages) or ["en"]
    aliases = parse_csv(args.aliases) or [topic_label]
    disambiguation_terms = parse_csv(args.disambiguation_terms)
    excluded_senses = parse_csv(args.excluded_senses)

    allowed_facets = pack.get("enabled_facets")
    if not isinstance(allowed_facets, list) or not all(
        isinstance(item, str) for item in allowed_facets
    ):
        raise BootstrapError(f"domain pack enabled_facets must be a string array: {domain_pack}")
    enabled_facets = unique_subset(
        parse_csv(args.enabled_facets), allowed_facets, label="enabled_facets"
    )

    allowed_query_families = pack.get("query_families")
    if not isinstance(allowed_query_families, list) or not all(
        isinstance(item, str) for item in allowed_query_families
    ):
        raise BootstrapError(f"domain pack query_families must be a string array: {domain_pack}")
    query_families = unique_subset(
        parse_csv(args.query_families),
        allowed_query_families,
        label="query_families",
    )

    indexer_dir = workspace_root / ".indexer"
    state_dir = workspace_root / "state"
    runs_dir = workspace_root / "runs"
    source_brief_path = workspace_root / "source.txt"
    manifest_path = indexer_dir / "subject_manifest.json"
    created_paths = [
        workspace_root,
        indexer_dir,
        state_dir,
        runs_dir,
        source_brief_path,
        manifest_path,
    ]
    manifest_payload = build_subject_manifest(
        subject_id=subject_id,
        display_name=display_name,
        domain_pack=domain_pack,
        scope_statement=scope_statement,
        languages=languages,
        aliases=aliases,
        disambiguation_terms=disambiguation_terms,
        excluded_senses=excluded_senses,
        enabled_facets=enabled_facets,
        query_families=query_families,
        workspace_root=workspace_root,
    )
    registry_payload = dict(existing_registry)
    registry_payload["schema_version"] = "topic-workspace-registry.v1"
    registry_workspaces = list(workspaces)
    registry_entry = build_registry_entry(
        workspace_id=workspace_id,
        topic_label=topic_label,
        workspace_root=workspace_root,
        domain_pack=domain_pack,
        manifest_path=manifest_path,
        lifecycle_state=args.lifecycle_state,
        schedule_posture=args.schedule_posture,
        workspace_policy_class=args.workspace_policy_class,
    )
    registry_workspaces.append(registry_entry)
    registry_payload["workspaces"] = registry_workspaces
    if args.set_default or not registry_payload.get("default_workspace_id"):
        registry_payload["default_workspace_id"] = workspace_id

    if args.dry_run:
        validate_manifest_payload_or_raise(manifest_payload)
        return build_result_payload(
            registry_path=registry_path,
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            source_brief_path=source_brief_path,
            default_workspace_id=registry_payload.get("default_workspace_id"),
            created_paths=created_paths,
            dry_run=True,
            registry_action="update" if registry_existed_before else "create",
        )

    workspace_root.mkdir(parents=True)
    try:
        indexer_dir.mkdir()

        state_dir.mkdir()

        runs_dir.mkdir()

        write_text_atomic(
            source_brief_path,
            build_source_brief(
                topic_label=topic_label,
                display_name=display_name,
                scope_statement=scope_statement,
            ),
        )

        write_text_atomic(manifest_path, render_json_payload(manifest_payload))

        validate_manifest_or_raise(manifest_path)

        write_registry_json(registry_path, registry_payload)
        validate_registry_or_raise(registry_path)

        return build_result_payload(
            registry_path=registry_path,
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            source_brief_path=source_brief_path,
            default_workspace_id=registry_payload.get("default_workspace_id"),
            created_paths=created_paths,
        )
    except BaseException:
        if registry_existed_before and original_registry_text is not None:
            write_text_atomic(registry_path, original_registry_text)
        else:
            registry_path.unlink(missing_ok=True)
        shutil.rmtree(workspace_root, ignore_errors=True)
        raise


def render_text(payload: dict[str, Any]) -> str:
    lines = []
    if payload.get("dry_run"):
        lines.extend(
            [
                "dry_run=true",
                f"registry_action={payload['registry_action']}",
            ]
        )
    lines.extend(
        [
            f"registry_path={payload['registry_path']}",
            f"workspace_id={payload['workspace_id']}",
            f"workspace_root={payload['workspace_root']}",
            f"subject_manifest_path={payload['subject_manifest_path']}",
            f"source_brief_path={payload['source_brief_path']}",
            f"default_workspace_id={payload['default_workspace_id']}",
        ]
    )
    path_key = "planned_created_paths" if payload.get("dry_run") else "created_paths"
    line_prefix = "planned_created_path" if payload.get("dry_run") else "created_path"
    for index, created_path in enumerate(payload[path_key]):
        lines.append(f"{line_prefix}[{index}]={created_path}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    try:
        payload = bootstrap_workspace(args)
    except BootstrapError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
