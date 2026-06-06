from __future__ import annotations

import subprocess
import sys

import pytest

import tests_support


def test_subprocess_run_defaults_to_suite_timeout_when_not_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tests_support, "DEFAULT_SUBPROCESS_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            text=True,
            capture_output=True,
            check=False,
        )


def test_subprocess_run_preserves_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tests_support, "DEFAULT_SUBPROCESS_TIMEOUT_SECONDS", 0.1)

    proc = subprocess.run(
        [sys.executable, "-c", "print('ok')"],
        text=True,
        capture_output=True,
        check=False,
        timeout=1,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"
