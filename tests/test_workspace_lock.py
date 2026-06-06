import json
import os
import signal
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


def test_workspace_lock_quarantines_dead_child_process_lock(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    holder_script = tmp_path / "hold_lock.py"
    holder_script.write_text(
        f"""
from pathlib import Path
import sys
import time

sys.path.insert(0, {str(REPO_ROOT)!r})
from tools.common.workspace_lock import acquire_workspace_lock

lock_root = Path({str(lock_root)!r})
with acquire_workspace_lock("workspace_death", command="child-hold", lock_root=lock_root) as lock_path:
    print(lock_path, flush=True)
    time.sleep(60)
""",
        encoding="utf-8",
    )

    child = subprocess.Popen(
        [sys.executable, str(holder_script)],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout is not None
    lock_path = Path(child.stdout.readline().strip())
    assert lock_path == lock_root / "workspace_death.lock"
    assert lock_path.exists()

    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=5)

    proc = subprocess.run(
        [
            sys.executable,
            str(LOCK_TOOL),
            "--workspace-id",
            "workspace_death",
            "--lock-root",
            str(lock_root),
            "--break-stale",
            "--stale-after-seconds",
            "0",
            "--print-path",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == str(lock_path)

    quarantine_dir = lock_root / "quarantine"
    quarantined = list(quarantine_dir.glob("workspace_death.lock.*.stale"))
    assert len(quarantined) == 1
    quarantined_lock = quarantined[0]
    audit = json.loads(Path(str(quarantined_lock) + ".json").read_text(encoding="utf-8"))
    preserved = json.loads(quarantined_lock.read_text(encoding="utf-8"))
    assert audit["schema_version"] == "workspace-lock-quarantine.v1"
    assert audit["reason"] == "dead_pid"
    assert audit["original_lock_path"] == str(lock_path)
    assert audit["quarantined_lock_path"] == str(quarantined_lock)
    assert preserved["pid"] == child.pid
    assert preserved["command"] == "child-hold"
    assert preserved["heartbeat_at"]
    assert not lock_path.exists()
