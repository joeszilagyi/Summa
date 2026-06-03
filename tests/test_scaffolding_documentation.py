from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_LAYOUT = REPO_ROOT / "docs" / "repo-layout.md"
TRACKED_SURFACE = REPO_ROOT / "TRACKED_SURFACE.md"
GITIGNORE = REPO_ROOT / ".gitignore"


def tracked_gitkeep_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "*.gitkeep"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_repo_surface_gitkeep_placeholders_are_documented() -> None:
    placeholder_paths = tracked_gitkeep_paths()
    assert placeholder_paths == [
        "index/Dates/.gitkeep",
        "tools/scripts/legacy/.gitkeep",
    ]

    repo_layout = REPO_LAYOUT.read_text(encoding="utf-8")
    tracked_surface = TRACKED_SURFACE.read_text(encoding="utf-8")
    repo_layout_normalized = " ".join(repo_layout.split())
    tracked_surface_normalized = " ".join(tracked_surface.split())

    for path in placeholder_paths:
        assert path in repo_layout, f"{path} must be documented in {REPO_LAYOUT}"
        assert path in tracked_surface, f"{path} must be documented in {TRACKED_SURFACE}"

    assert "placeholder directories are not evidence that the corresponding runtime or index producer has been implemented" in repo_layout_normalized
    assert "do not mean the corresponding runtime feature is already implemented" in tracked_surface_normalized


def test_gitignore_still_marks_local_runtime_database_and_index_surfaces_ignored() -> None:
    gitignore = GITIGNORE.read_text(encoding="utf-8")

    for pattern in ("runtime/**", "dbs/**", "index/[P]laces/**"):
        assert pattern in gitignore, f".gitignore must keep protecting {pattern}"
