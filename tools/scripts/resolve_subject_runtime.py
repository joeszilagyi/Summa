#!/usr/bin/env python3
"""Resolve domain-pack prompt bundle metadata into runtime-friendly fields."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.llm_source_text_wrapper import default_template_id  # noqa: E402

PHASE_KEYS = ("01a", "01r")
SUBJECT_MANIFEST_SCHEMA_VERSION = "subject-manifest.v1"
SUBJECT_RUNTIME_SCHEMA_VERSION = "subject-runtime-resolution.v1"
DEFAULT_WORKSPACE_MANIFEST = Path(".indexer") / "subject_manifest.json"
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ResolutionError(RuntimeError):
    """Raised when subject runtime inputs cannot be normalized."""


def prompt_bundle_candidate_keys(facet: str) -> tuple[str, ...]:
    normalized = facet.strip()
    return (
        f"gather.{normalized}",
        normalized,
    )


def _require_nonblank_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionError(f"{field_name} must be a non-blank string")
    return value.strip()


def _normalize_template_ids(bundle: dict[str, Any], *, facet: str, bundle_key: str) -> list[str]:
    value = bundle.get("template_ids")
    if not isinstance(value, list) or len(value) < 2:
        raise ResolutionError(
            f"prompt bundle {bundle_key} for facet {facet} must declare at least two template_ids"
        )
    normalized: list[str] = []
    for index, item in enumerate(value):
        normalized.append(
            _require_nonblank_string(
                item,
                field_name=f"prompt bundle {bundle_key} template_ids[{index}]",
            )
        )
    return normalized


def _normalize_phase_templates(
    bundle: dict[str, Any],
    *,
    facet: str,
    bundle_key: str,
    template_ids: list[str],
) -> dict[str, str]:
    raw = bundle.get("phase_templates")
    if raw is None:
        return {
            "01a": template_ids[0],
            "01r": template_ids[1],
        }
    if not isinstance(raw, dict):
        raise ResolutionError(f"prompt bundle {bundle_key} for facet {facet} must use an object for phase_templates")
    phase_templates: dict[str, str] = {}
    for phase in PHASE_KEYS:
        phase_templates[phase] = _require_nonblank_string(
            raw.get(phase),
            field_name=f"prompt bundle {bundle_key} phase_templates.{phase}",
        )
    return phase_templates


def _normalize_resolved_phase_prompt_files(
    bundle: dict[str, Any],
    *,
    facet: str,
    bundle_key: str,
    phase_templates: dict[str, str],
) -> dict[str, str]:
    raw = bundle.get("resolved_phase_prompt_files")
    if raw is None:
        return dict(phase_templates)
    if not isinstance(raw, dict):
        raise ResolutionError(
            f"prompt bundle {bundle_key} for facet {facet} must use an object for resolved_phase_prompt_files"
        )
    resolved: dict[str, str] = {}
    for phase in PHASE_KEYS:
        resolved[phase] = _require_nonblank_string(
            raw.get(phase),
            field_name=f"prompt bundle {bundle_key} resolved_phase_prompt_files.{phase}",
        )
    return resolved


def _normalize_template_files(bundle: dict[str, Any]) -> list[str]:
    raw = bundle.get("template_files")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ResolutionError("prompt bundle template_files must be a string array when present")
    template_files: list[str] = []
    for index, item in enumerate(raw):
        template_files.append(
            _require_nonblank_string(
                item,
                field_name=f"prompt bundle template_files[{index}]",
            )
        )
    return template_files


def _normalize_resolved_phase_template_files(
    template_files: list[str],
    *,
    facet: str,
    bundle_key: str,
) -> dict[str, str]:
    if not template_files:
        return {}
    if len(template_files) < len(PHASE_KEYS):
        raise ResolutionError(
            f"prompt bundle {bundle_key} for facet {facet} must declare at least {len(PHASE_KEYS)} template_files"
        )
    return {
        "01a": template_files[0],
        "01r": template_files[1],
    }


def _normalize_source_text_wrapper_template_id(bundle: dict[str, Any], *, facet: str, bundle_key: str) -> str:
    raw = bundle.get("source_text_wrapper_template_id")
    if raw is None:
        return default_template_id()
    return _require_nonblank_string(
        raw,
        field_name=f"prompt bundle {bundle_key} source_text_wrapper_template_id",
    )


def _default_legacy_output_stem(bundle_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", bundle_id.lower()).strip("_")


def resolve_prompt_bundles(pack: dict[str, Any], facets: list[str]) -> dict[str, dict[str, Any]]:
    prompt_bundles = pack.get("prompt_bundles")
    if not isinstance(prompt_bundles, dict):
        raise ResolutionError("domain pack prompt_bundles must be an object")

    resolved: dict[str, dict[str, Any]] = {}
    for facet in facets:
        if not isinstance(facet, str) or not facet.strip():
            raise ResolutionError("facet names must be non-blank strings")

        bundle_key = ""
        bundle_value: dict[str, Any] | None = None
        for candidate_key in prompt_bundle_candidate_keys(facet):
            candidate = prompt_bundles.get(candidate_key)
            if isinstance(candidate, dict):
                bundle_key = candidate_key
                bundle_value = candidate
                break
        if bundle_value is None:
            raise ResolutionError(f"domain pack has no prompt bundle for facet: {facet}")

        bundle_id = _require_nonblank_string(
            bundle_value.get("bundle_id"),
            field_name=f"prompt bundle {bundle_key} bundle_id",
        )
        template_ids = _normalize_template_ids(bundle_value, facet=facet, bundle_key=bundle_key)
        phase_templates = _normalize_phase_templates(
            bundle_value,
            facet=facet,
            bundle_key=bundle_key,
            template_ids=template_ids,
        )
        resolved_phase_prompt_files = _normalize_resolved_phase_prompt_files(
            bundle_value,
            facet=facet,
            bundle_key=bundle_key,
            phase_templates=phase_templates,
        )
        legacy_01a_output_stem = bundle_value.get("legacy_01a_output_stem")
        if legacy_01a_output_stem is None:
            legacy_01a_output_stem = _default_legacy_output_stem(bundle_id)
        else:
            legacy_01a_output_stem = _require_nonblank_string(
                legacy_01a_output_stem,
                field_name=f"prompt bundle {bundle_key} legacy_01a_output_stem",
            )
        template_files = _normalize_template_files(bundle_value)

        resolved[facet] = {
            "bundle_key": bundle_key,
            "bundle_id": bundle_id,
            "template_ids": template_ids,
            "phase_templates": phase_templates,
            "resolved_phase_prompt_files": resolved_phase_prompt_files,
            "legacy_01a_output_stem": legacy_01a_output_stem,
            "template_files": template_files,
            "resolved_phase_template_files": _normalize_resolved_phase_template_files(
                template_files,
                facet=facet,
                bundle_key=bundle_key,
            ),
            "source_text_wrapper_template_id": _normalize_source_text_wrapper_template_id(
                bundle_value,
                facet=facet,
                bundle_key=bundle_key,
            ),
        }

    return resolved


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ResolutionError(f"{label} not found: {path}")
    if not path.is_file():
        raise ResolutionError(f"{label} is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResolutionError(f"could not read {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ResolutionError(f"could not parse {label}: {path} (line {exc.lineno})") from exc
    if not isinstance(payload, dict):
        raise ResolutionError(f"{label} must contain a JSON object: {path}")
    return payload


def _normalize_string_array(value: Any, *, field_name: str, min_items: int = 0) -> list[str]:
    if not isinstance(value, list):
        raise ResolutionError(f"{field_name} must be a string array")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _require_nonblank_string(item, field_name=f"{field_name}[{index}]")
        if text in seen:
            raise ResolutionError(f"{field_name} contains a duplicate value: {text}")
        seen.add(text)
        normalized.append(text)
    if len(normalized) < min_items:
        raise ResolutionError(f"{field_name} must contain at least {min_items} item(s)")
    return normalized


def resolve_workspace_path(raw_workspace: str) -> Path:
    workspace_path = Path(raw_workspace).expanduser()
    if not workspace_path.is_absolute():
        workspace_path = (Path.cwd() / workspace_path).resolve()
    if not workspace_path.exists():
        raise ResolutionError(f"workspace root not found: {workspace_path}")
    if not workspace_path.is_dir():
        raise ResolutionError(f"workspace root is not a directory: {workspace_path}")
    return workspace_path


def resolve_subject_manifest_path(raw_subject: str, workspace: str | None = None) -> tuple[Path, str]:
    candidate = Path(raw_subject).expanduser()
    if candidate.exists():
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if not candidate.is_file():
            raise ResolutionError(f"subject manifest path is not a file: {candidate}")
        return candidate, "subject_manifest_path"

    if workspace is None:
        raise ResolutionError(
            "subject must be an existing manifest path or a subject_id used together with --workspace"
        )

    workspace_path = resolve_workspace_path(workspace)
    manifest_path = (workspace_path / DEFAULT_WORKSPACE_MANIFEST).resolve()
    if not manifest_path.is_file():
        raise ResolutionError(f"workspace subject manifest not found: {manifest_path}")
    return manifest_path, "workspace_default_manifest"


def load_subject_manifest(manifest_path: Path) -> dict[str, Any]:
    payload = _load_json_object(manifest_path, label="subject manifest")
    schema_version = _require_nonblank_string(payload.get("schema_version"), field_name="subject manifest schema_version")
    if schema_version != SUBJECT_MANIFEST_SCHEMA_VERSION:
        raise ResolutionError(f"subject manifest schema_version must equal {SUBJECT_MANIFEST_SCHEMA_VERSION}")

    return {
        "schema_version": schema_version,
        "subject_id": _require_nonblank_string(payload.get("subject_id"), field_name="subject manifest subject_id"),
        "display_name": _require_nonblank_string(payload.get("display_name"), field_name="subject manifest display_name"),
        "domain_pack": _require_nonblank_string(payload.get("domain_pack"), field_name="subject manifest domain_pack"),
        "scope_statement": _require_nonblank_string(
            payload.get("scope_statement"),
            field_name="subject manifest scope_statement",
        ),
        "languages": _normalize_string_array(payload.get("languages"), field_name="subject manifest languages", min_items=1),
        "aliases": _normalize_string_array(payload.get("aliases", []), field_name="subject manifest aliases"),
        "disambiguation_terms": _normalize_string_array(
            payload.get("disambiguation_terms", []),
            field_name="subject manifest disambiguation_terms",
        ),
        "excluded_senses": _normalize_string_array(
            payload.get("excluded_senses", []),
            field_name="subject manifest excluded_senses",
        ),
        "enabled_facets": _normalize_string_array(
            payload.get("enabled_facets"),
            field_name="subject manifest enabled_facets",
            min_items=1,
        ),
        "query_families": _normalize_string_array(
            payload.get("query_families"),
            field_name="subject manifest query_families",
            min_items=1,
        ),
        "legacy_substrate_paths": _normalize_string_array(
            payload.get("legacy_substrate_paths", []),
            field_name="subject manifest legacy_substrate_paths",
        ),
        "public_export_default": bool(payload.get("public_export_default", False)),
        "notes": _normalize_string_array(payload.get("notes", []), field_name="subject manifest notes"),
    }


def _infer_workspace_root(manifest_path: Path) -> Path:
    if manifest_path.parent.name == DEFAULT_WORKSPACE_MANIFEST.parent.name:
        return manifest_path.parent.parent.resolve()
    return manifest_path.parent.resolve()


def resolve_subject_runtime(raw_subject: str, workspace: str | None = None) -> dict[str, Any]:
    manifest_path, resolution_source = resolve_subject_manifest_path(raw_subject, workspace)
    manifest = load_subject_manifest(manifest_path)
    if workspace is None:
        workspace_root = _infer_workspace_root(manifest_path)
    else:
        workspace_root = resolve_workspace_path(workspace)
    if not IDENTIFIER_PATTERN.fullmatch(manifest["subject_id"]):
        raise ResolutionError("subject manifest subject_id must match ^[a-z0-9][a-z0-9._-]*$")
    if manifest["subject_id"] != raw_subject and not Path(raw_subject).expanduser().exists():
        raise ResolutionError(
            f"workspace manifest subject_id does not match requested subject: {manifest['subject_id']} != {raw_subject}"
        )

    return {
        "schema_version": SUBJECT_RUNTIME_SCHEMA_VERSION,
        "resolution_source": resolution_source,
        "subject_manifest_path": str(manifest_path),
        "workspace_root": str(workspace_root),
        "subject": manifest,
    }
