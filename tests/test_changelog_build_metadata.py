from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_METADATA = REPO_ROOT / ".project_metadata"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
BUILD_VALUE_RE = re.compile(r"^\d+(?:\.\d+)+$")


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
