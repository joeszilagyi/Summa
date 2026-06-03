from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_PACKS_DIR = REPO_ROOT / "config" / "domain_packs"
INDEX_DOC = REPO_ROOT / "docs" / "project" / "DOMAIN_PACKS.md"
README_PATH = REPO_ROOT / "README.md"
BOOTSTRAP_TOOL = REPO_ROOT / "tools" / "scripts" / "bootstrap_topic_workspace.py"
RESOLVE_GATHER_TOOL = REPO_ROOT / "tools" / "scripts" / "resolve_gather_domain_pack.py"


def load_pack_payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in sorted(DOMAIN_PACKS_DIR.glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["_path"] = path
        payloads.append(payload)
    return payloads


def extract_pack_section(doc: str, pack_id: str) -> str:
    pattern = re.compile(rf"^## `{re.escape(pack_id)}`\n(?P<section>.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(doc)
    assert match is not None, f"pack section missing from {INDEX_DOC}: {pack_id}"
    return match.group("section")


def run_help(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(path), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_domain_pack_index_lists_every_checked_in_pack_and_current_status() -> None:
    doc = INDEX_DOC.read_text(encoding="utf-8")
    payloads = load_pack_payloads()

    assert f"Checked-in pack count: {len(payloads)}." in doc

    for payload in payloads:
        section = extract_pack_section(doc, payload["pack_id"])
        assert f"- Display name: `{payload['display_name']}`" in section
        assert f"- Status: `{payload['status']}`" in section
        assert f"- Prompt bundles: {len(payload['prompt_bundles'])}" in section
        assert f"- Enabled facets ({len(payload['enabled_facets'])}):" in section

        wrapper_ids = sorted(
            {
                bundle["source_text_wrapper_template_id"]
                for bundle in payload["prompt_bundles"].values()
                if isinstance(bundle, dict) and isinstance(bundle.get("source_text_wrapper_template_id"), str)
            }
        )
        for wrapper_id in wrapper_ids:
            assert f"`{wrapper_id}`" in section


def test_readme_flagship_example_points_to_runtime_general_pack() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    general_pack = json.loads((DOMAIN_PACKS_DIR / "general.v1.json").read_text(encoding="utf-8"))
    index_doc = INDEX_DOC.read_text(encoding="utf-8")

    assert "trout fly fishing in Montana" in readme
    assert "`general.v1` domain pack" in readme
    assert "(docs/project/DOMAIN_PACKS.md)" in readme
    assert general_pack["status"] == "runtime"
    assert "topic.general" in general_pack["subject_kinds"]

    general_section = extract_pack_section(index_doc, "general.v1")
    assert "README flagship example: currently routes to `general.v1`" in general_section
    assert "fixture-proven safe first-cycle coverage example for place-dominant recreation subjects" in general_section
    assert "safe for broad-topic examples" not in general_section


def test_domain_pack_operator_help_mentions_checked_in_pack_ids() -> None:
    bootstrap_help = run_help(BOOTSTRAP_TOOL)
    assert bootstrap_help.returncode == 0, bootstrap_help.stdout + bootstrap_help.stderr
    bootstrap_output = bootstrap_help.stdout + bootstrap_help.stderr
    assert "subject.v1" not in bootstrap_output
    assert "general.v1 or organism.v1" in bootstrap_output

    resolve_help = run_help(RESOLVE_GATHER_TOOL)
    assert resolve_help.returncode == 0, resolve_help.stdout + resolve_help.stderr
    resolve_output = resolve_help.stdout + resolve_help.stderr
    assert "subject.v1" not in resolve_output
    assert "general.v1" in resolve_output
