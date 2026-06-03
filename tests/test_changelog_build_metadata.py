from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_METADATA = REPO_ROOT / ".project_metadata"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
BUILD_VALUE_RE = re.compile(r"^\d+(?:\.\d+)+$")
TRIVIAL_BULLET_RE = re.compile(
    r"(?:^|\b)(added\s+`?changelog\.md`?|updated\s+changelog|updated\s+docs?|documentation\s+changes?)\b",
    re.IGNORECASE,
)
CONCRETE_SURFACE_RE = re.compile(
    r"`[^`]+\.(?:py|sh|md|json|jsonl|yml)`"
    r"|`[^`]+/[^`]+`"
    r"|\b(?:ruff|mypy|pytest-cov|jsonschema)\b",
    re.IGNORECASE,
)


def load_project_metadata() -> dict[str, str]:
    assert PROJECT_METADATA.is_file(), ".project_metadata is required for tracked build metadata"

    payload: dict[str, str] = {}
    for line in PROJECT_METADATA.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        payload[key.strip()] = value.strip()
    return payload


def has_build_heading(changelog_text: str, build_value: str) -> bool:
    return re.search(rf"^## \[{re.escape(build_value)}\]\s*$", changelog_text, re.MULTILINE) is not None


def extract_build_section(changelog_text: str, build_value: str) -> str:
    match = re.search(
        rf"^## \[{re.escape(build_value)}\]\s*$\n(?P<body>.*?)(?=^## \[|\Z)",
        changelog_text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"CHANGELOG.md must contain a heading for CURRENT_BUILD {build_value}"
    return match.group("body").strip()


def section_bullets(section_text: str) -> list[str]:
    bullets: list[str] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def current_build_section_problems(build_value: str, changelog_text: str) -> list[str]:
    section_text = extract_build_section(changelog_text, build_value)
    if not section_text:
        return [f"CHANGELOG.md current build {build_value} has no substantive entries"]

    bullets = section_bullets(section_text)
    if len(bullets) < 3:
        return [
            f"CHANGELOG.md current build {build_value} has no substantive entries; "
            "add at least three concrete bullet items"
        ]

    nontrivial_bullets = [bullet for bullet in bullets if not TRIVIAL_BULLET_RE.search(bullet)]
    if not nontrivial_bullets:
        return [f"CHANGELOG.md current build {build_value} only mentions the changelog itself"]

    if not any(CONCRETE_SURFACE_RE.search(bullet) for bullet in nontrivial_bullets):
        return [
            f"CHANGELOG.md current build {build_value} must name at least one concrete tracked surface or tool"
        ]
    return []


def assert_substantive_current_build_entry(build_value: str, changelog_text: str) -> None:
    problems = current_build_section_problems(build_value, changelog_text)
    assert not problems, problems[0]


def test_changelog_tracks_current_and_prior_build_metadata() -> None:
    metadata = load_project_metadata()

    current_build = metadata.get("CURRENT_BUILD")
    assert current_build, ".project_metadata must define CURRENT_BUILD"
    assert BUILD_VALUE_RE.fullmatch(current_build), f"CURRENT_BUILD is malformed: {current_build!r}"

    prior_build = metadata.get("PRIOR_BUILD")
    assert prior_build, ".project_metadata must define PRIOR_BUILD"
    assert BUILD_VALUE_RE.fullmatch(prior_build), f"PRIOR_BUILD is malformed: {prior_build!r}"
    assert current_build != prior_build, "CURRENT_BUILD and PRIOR_BUILD must not be equal"

    assert CHANGELOG.is_file(), "CHANGELOG.md is required because .project_metadata tracks CURRENT_BUILD"
    changelog_text = CHANGELOG.read_text(encoding="utf-8")

    assert has_build_heading(
        changelog_text, current_build
    ), f"CHANGELOG.md must contain a heading for CURRENT_BUILD {current_build}"
    assert_substantive_current_build_entry(current_build, changelog_text)

    prior_has_heading = has_build_heading(changelog_text, prior_build)
    prior_has_baseline_note = (
        prior_build in changelog_text
        and "Historical baseline referenced by `.project_metadata`" in changelog_text
    )
    assert prior_has_heading or prior_has_baseline_note, (
        f"CHANGELOG.md must either contain a heading or explicit baseline note for PRIOR_BUILD {prior_build}"
    )

    current_index = changelog_text.index(f"## [{current_build}]")
    prior_index = changelog_text.index(prior_build)
    assert current_index < prior_index, "CHANGELOG.md must list CURRENT_BUILD before PRIOR_BUILD"


def test_current_build_section_rejects_empty_body() -> None:
    text = "## [8.8.0.5]\n"
    with pytest.raises(AssertionError, match=r"has no substantive entries"):
        assert_substantive_current_build_entry("8.8.0.5", text)


def test_current_build_section_rejects_trivial_changelog_only_entry() -> None:
    text = textwrap.dedent(
        """
        ## [8.8.0.5]

        ### Added

        - Added CHANGELOG.md.
        """
    ).strip()
    with pytest.raises(AssertionError, match=r"has no substantive entries|only mentions the changelog itself"):
        assert_substantive_current_build_entry("8.8.0.5", text)


def test_current_build_section_rejects_generic_docs_only_entry() -> None:
    text = textwrap.dedent(
        """
        ## [8.8.0.5]

        ### Documentation

        - Updated docs.
        - Documentation changes.
        - Updated changelog.
        """
    ).strip()
    with pytest.raises(AssertionError, match=r"only mentions the changelog itself|must name at least one concrete tracked surface or tool"):
        assert_substantive_current_build_entry("8.8.0.5", text)


def test_current_build_section_rejects_nontrivial_but_surface_free_entry() -> None:
    text = textwrap.dedent(
        """
        ## [8.8.0.5]

        ### Changed

        - Improved local behavior for the current build.
        - Tightened the review posture for new records.
        - Expanded the regression coverage for current behavior.
        """
    ).strip()
    with pytest.raises(AssertionError, match=r"must name at least one concrete tracked surface or tool"):
        assert_substantive_current_build_entry("8.8.0.5", text)


def test_current_build_section_accepts_substantive_entry() -> None:
    text = textwrap.dedent(
        """
        ## [8.8.0.5]

        ### Added

        - Added `tools/scripts/ingest_gather_candidate_batch.py` and `tools/source_db_tools/canonical_ingest.py`.

        ### Validation

        - Added `tests/test_canonical_ingest_candidate_batch.py` and `tests/test_canonical_dedup_and_contradiction.py`.
        - Hardened CI with `jsonschema` and `pytest-cov`.
        """
    ).strip()

    assert_substantive_current_build_entry("8.8.0.5", text)
