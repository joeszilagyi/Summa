from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


FIXTURE_TREES = [
    REPO_ROOT / "tests" / "fixtures",
    REPO_ROOT / "config",
    REPO_ROOT / "docs" / "contracts",
]


def git_head_hash(path: Path) -> str | None:
    rel_path = path.relative_to(REPO_ROOT).as_posix()
    proc = subprocess.run(
        ["git", "rev-parse", f"HEAD:{rel_path}"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def file_hash(path: Path) -> str:
    proc = subprocess.run(
        ["git", "hash-object", str(path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"failed to hash {path}")
    return proc.stdout.strip()


def test_tracked_fixtures_and_contracts_are_not_mutated() -> None:
    mutated: list[str] = []
    for tree in FIXTURE_TREES:
        if not tree.exists():
            continue
        for path in sorted(tree.rglob("*")):
            if not path.is_file():
                continue
            expected = git_head_hash(path)
            if expected is None:
                continue
            current = file_hash(path)
            if current != expected:
                mutated.append(f"{path.relative_to(REPO_ROOT)}")

    assert not mutated, "tracked fixture and contract files changed: " + ", ".join(mutated)
