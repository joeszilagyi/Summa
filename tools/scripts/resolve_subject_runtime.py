#!/usr/bin/env python3
"""Resolve domain-pack prompt bundle metadata into runtime-friendly fields."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE_KEYS = ("01a", "01r")


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

        resolved[facet] = {
            "bundle_key": bundle_key,
            "bundle_id": bundle_id,
            "template_ids": template_ids,
            "phase_templates": phase_templates,
            "resolved_phase_prompt_files": resolved_phase_prompt_files,
            "legacy_01a_output_stem": legacy_01a_output_stem,
            "template_files": _normalize_template_files(bundle_value),
        }

    return resolved
