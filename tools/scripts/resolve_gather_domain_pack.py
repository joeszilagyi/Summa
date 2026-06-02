#!/usr/bin/env python3
"""Resolve gather facets and prompt paths from a domain pack for legacy runners."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import resolve_subject_runtime  # noqa: E402


class GatherDomainPackError(RuntimeError):
    """Raised when a domain pack cannot drive the gather runtime."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve selected gather facets and prompt files from a domain pack."
    )
    parser.add_argument(
        "--domain-pack",
        default="subject.v1",
        help="Domain pack ID under config/domain_packs (default: subject.v1).",
    )
    parser.add_argument(
        "--facets",
        help="Optional space- or comma-separated facet override. Defaults to pack enabled_facets.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "shell"),
        default="json",
        help="Output format for the resolved payload.",
    )
    return parser.parse_args()


def load_domain_pack(pack_id: str) -> dict[str, Any]:
    if not pack_id.strip():
        raise GatherDomainPackError("domain pack must be non-blank")

    pack_path = resolve_subject_runtime.REPO_ROOT / "config" / "domain_packs" / f"{pack_id}.json"
    if not pack_path.is_file():
        raise GatherDomainPackError(f"domain pack file not found: {pack_path}")

    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GatherDomainPackError(f"could not read domain pack: {pack_path}") from exc
    except json.JSONDecodeError as exc:
        raise GatherDomainPackError(f"could not parse domain pack: {pack_path} (line {exc.lineno})") from exc

    if not isinstance(pack, dict):
        raise GatherDomainPackError(f"domain pack must contain a JSON object: {pack_path}")
    if pack.get("pack_id") != pack_id:
        raise GatherDomainPackError(f"domain pack pack_id does not match requested ID: {pack_id}")
    return pack


def parse_facets(raw_facets: str | None, pack: dict[str, Any]) -> list[str]:
    enabled_facets = pack.get("enabled_facets")
    if not isinstance(enabled_facets, list) or not all(isinstance(item, str) for item in enabled_facets):
        raise GatherDomainPackError("domain pack enabled_facets must be a string array")

    if raw_facets is None or not raw_facets.strip():
        facets = list(enabled_facets)
    else:
        facets = [item for item in re.split(r"[\s,]+", raw_facets.strip()) if item]

    if not facets:
        raise GatherDomainPackError("selected facets must not be empty")

    enabled = set(enabled_facets)
    for facet in facets:
        if facet not in enabled:
            raise GatherDomainPackError(f"facet not enabled by domain pack: {facet}")

    return facets


def facet_env_suffix(facet: str) -> str:
    suffix = re.sub(r"[^0-9A-Za-z]+", "_", facet).strip("_").upper()
    if not suffix:
        raise GatherDomainPackError(f"could not derive environment suffix for facet: {facet}")
    return suffix


def resolve_gather_domain_pack(pack_id: str, raw_facets: str | None) -> dict[str, Any]:
    pack = load_domain_pack(pack_id)
    selected_facets = parse_facets(raw_facets, pack)
    try:
        prompt_bundles = resolve_subject_runtime.resolve_prompt_bundles(pack, selected_facets)
    except resolve_subject_runtime.ResolutionError as exc:
        raise GatherDomainPackError(str(exc)) from exc

    facets: dict[str, dict[str, str]] = {}
    for facet in selected_facets:
        bundle = prompt_bundles[facet]

        facets[facet] = {
            "01a_output_stem": bundle["legacy_01a_output_stem"],
            "01a_prompt": bundle["resolved_phase_prompt_files"]["01a"],
            "01r_prompt": bundle["resolved_phase_prompt_files"]["01r"],
            "prompt_bundle_id": bundle["bundle_id"],
        }

    return {
        "schema_version": "gather-domain-pack-resolution.v1",
        "domain_pack": pack_id,
        "selected_facets": selected_facets,
        "facets": facets,
    }


def render_shell_assignments(payload: dict[str, Any]) -> str:
    env_map = {
        "INDEX_DOMAIN_PACK": payload["domain_pack"],
        "INDEX_SELECTED_FACETS": " ".join(payload["selected_facets"]),
    }

    for facet, config in payload["facets"].items():
        suffix = facet_env_suffix(facet)
        env_map[f"INDEX_FACET_01A_STEM_{suffix}"] = config["01a_output_stem"]
        env_map[f"INDEX_FACET_01A_PROMPT_{suffix}"] = config["01a_prompt"]
        env_map[f"INDEX_FACET_01R_PROMPT_{suffix}"] = config["01r_prompt"]
        env_map[f"INDEX_FACET_PROMPT_BUNDLE_ID_{suffix}"] = config["prompt_bundle_id"]

    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in env_map.items())


def main() -> int:
    args = parse_args()
    try:
        payload = resolve_gather_domain_pack(args.domain_pack, args.facets)
    except GatherDomainPackError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "shell":
        print(render_shell_assignments(payload))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
