from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_timeout_option_fails_slow_inner_test() -> None:
    temp_dir = REPO_ROOT / "tests" / "_tmp_timeout_smoke"
    temp_dir.mkdir(exist_ok=True)
    slow_test = temp_dir / "test_slow.py"
    slow_test.write_text(
        "import time\n\n"
        "def test_sleep():\n"
        "    time.sleep(0.5)\n",
        encoding="utf-8",
    )

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--timeout=0.1",
                "-q",
                str(slow_test.relative_to(REPO_ROOT)),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    assert proc.returncode != 0
    assert "test exceeded --timeout=0.1s" in proc.stderr or "test exceeded --timeout=0.1s" in proc.stdout
