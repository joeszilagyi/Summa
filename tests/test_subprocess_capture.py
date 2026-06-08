from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.common.subprocess_capture import run_streaming_command, tail_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_streaming_command_streams_stdout_and_stderr_to_files(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"

    proc = run_streaming_command(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        cwd=REPO_ROOT,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == "out"
    assert proc.stderr.strip() == "err"
    assert stdout_path.read_text(encoding="utf-8") == "out\n"
    assert stderr_path.read_text(encoding="utf-8") == "err\n"
    assert tail_text(stdout_path) == "out"
    assert tail_text(stderr_path) == "err"


def test_run_streaming_command_attaches_output_paths_on_timeout(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_streaming_command(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            cwd=REPO_ROOT,
            timeout=0.1,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    exc = excinfo.value
    assert Path(exc.stdout_path) == stdout_path
    assert Path(exc.stderr_path) == stderr_path
