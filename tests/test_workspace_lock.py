import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_TOOL = REPO_ROOT / "tools" / "common" / "workspace_lock.py"
sys.path.insert(0, str(REPO_ROOT))

from tools.common.workspace_lock import (  # noqa: E402
    WorkspaceLockError,
    acquire_workspace_lock,
    lock_path_for,
    quarantine_stale_lock,
)


def test_workspace_lock_writes_heartbeat_metadata_and_releases(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"

    with acquire_workspace_lock("workspace_a", command="pytest", lock_root=lock_root) as lock_path:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        assert metadata["schema_version"] == "workspace-lock.v1"
        assert metadata["workspace_id"] == "workspace_a"
        assert metadata["pid"] == os.getpid()
        assert metadata["command"] == "pytest"
        assert metadata["heartbeat_at"]

    assert not lock_path.exists()


def test_workspace_lock_fail_fast_contention(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"

    with acquire_workspace_lock("workspace_a", command="outer", lock_root=lock_root):
        with pytest.raises(WorkspaceLockError, match="already held"):
            with acquire_workspace_lock("workspace_a", command="inner", lock_root=lock_root):
                pass


def test_workspace_lock_cli_waits_for_release(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"
    with acquire_workspace_lock("workspace_a", command="outer", lock_root=lock_root):
        proc = subprocess.run(
            [
                sys.executable,
                str(LOCK_TOOL),
                "--workspace-id",
                "workspace_a",
                "--lock-root",
                str(lock_root),
                "--wait",
                "--timeout-seconds",
                "0.2",
                "--print-path",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    assert proc.returncode == 1
    assert "already held" in proc.stderr


def test_workspace_lock_quarantines_stale_file(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    stale_path = lock_path_for("workspace_a", lock_root)
    stale_path.write_text('{"schema_version":"workspace-lock.v1","pid":-1,"host":"fixture"}\n', encoding="utf-8")
    old = time.time() - 7200
    os.utime(stale_path, (old, old))

    quarantined = quarantine_stale_lock(stale_path, reason="heartbeat_expired")

    assert not stale_path.exists()
    assert quarantined.exists()
    audit = json.loads((Path(str(quarantined) + ".json")).read_text(encoding="utf-8"))
    assert audit["schema_version"] == "workspace-lock-quarantine.v1"
    assert audit["reason"] == "heartbeat_expired"
