from __future__ import annotations

from types import SimpleNamespace
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_SCRIPT = REPO_ROOT / "tools" / "scripts" / "Index_Build_Knowledge_Tree.sh"

sys.path.insert(0, str(REPO_ROOT / "tools" / "scripts"))
import local_doctor  # noqa: E402


def test_local_doctor_git_status_handles_missing_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    monkeypatch.setattr(local_doctor.shutil, "which", lambda command: None)

    status, output = local_doctor.git_status(repo_root)

    assert status == "not_git_checkout"
    assert output == ""


def test_local_doctor_git_status_reports_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    monkeypatch.setattr(local_doctor.shutil, "which", lambda command: "/usr/bin/git")
    monkeypatch.setattr(
        local_doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="fatal: fixture"),
    )

    status, output = local_doctor.git_status(repo_root)

    assert status == "git_status_failed"
    assert output == "fatal: fixture"


def test_index_build_knowledge_tree_wrapper_reports_missing_python(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHON"] = "python-does-not-exist"

    proc = subprocess.run(
        ["bash", str(WRAPPER_SCRIPT), "--check"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "python executable not found: python-does-not-exist" in proc.stderr
