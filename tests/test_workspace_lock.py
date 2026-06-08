import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.common import workspace_lock as workspace_lock_module
from tools.common.workspace_lock import (
    WorkspaceLockError,
    acquire_workspace_lock,
    lock_path_for,
    quarantine_stale_lock,
    stale_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_TOOL = REPO_ROOT / "tools" / "common" / "workspace_lock.py"


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


def test_workspace_lock_refreshes_heartbeat_metadata_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_root = tmp_path / "locks"
    writes: list[str] = []
    real_write_metadata = workspace_lock_module.write_metadata

    def counting_write_metadata(handle, metadata):
        writes.append(str(metadata["heartbeat_at"]))
        return real_write_metadata(handle, metadata)

    monkeypatch.setattr(workspace_lock_module, "HEARTBEAT_REFRESH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(workspace_lock_module, "write_metadata", counting_write_metadata)

    with acquire_workspace_lock(
        "workspace_refresh", command="pytest", lock_root=lock_root, stale_after_seconds=2
    ) as lock_path:
        time.sleep(0.05)
        refreshed = json.loads(lock_path.read_text(encoding="utf-8"))

    assert len(writes) >= 2
    assert refreshed["heartbeat_at"]
    assert not lock_path.exists()


def test_workspace_lock_stale_reason_uses_heartbeat_metadata_not_mtime(
    tmp_path: Path,
) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    lock_path = lock_path_for("workspace_heartbeat", lock_root)
    heartbeat_at = workspace_lock_module.utc_now()
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-lock.v1",
                "workspace_id": "workspace_heartbeat",
                "pid": os.getpid(),
                "host": "fixture-host",
                "command": "pytest",
                "lock_path": str(lock_path),
                "acquired_at": heartbeat_at,
                "heartbeat_at": heartbeat_at,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    stale_mtime = time.time() - 7200
    os.utime(lock_path, (stale_mtime, stale_mtime))

    assert stale_reason(lock_path, stale_after_seconds=3600, now=time.time()) is None


def test_workspace_lock_fail_fast_contention(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"

    with (
        acquire_workspace_lock("workspace_a", command="outer", lock_root=lock_root),
        pytest.raises(WorkspaceLockError, match="already held"),
        acquire_workspace_lock("workspace_a", command="inner", lock_root=lock_root),
    ):
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


def test_workspace_lock_cli_times_out_wrapped_command_and_releases_lock(tmp_path: Path) -> None:
    lock_root = tmp_path / "locks"
    lock_path = lock_path_for("workspace_timeout", lock_root)

    proc = subprocess.run(
        [
            sys.executable,
            str(LOCK_TOOL),
            "--workspace-id",
            "workspace_timeout",
            "--lock-root",
            str(lock_root),
            "--command-timeout-seconds",
            "0.1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(1)",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 124, proc.stdout + proc.stderr
    assert "command timed out after 0.1 seconds" in proc.stderr
    assert not lock_path.exists()


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
