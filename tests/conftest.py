from __future__ import annotations

import importlib.util
import signal
import subprocess

import pytest

from tests_support import (
    module_from_spec_with_registration,
    subprocess_run_with_default_timeout,
)


# Register dynamically loaded test modules before execution so dataclass and
# similar runtime-introspection code can resolve sys.modules[__name__] safely.
importlib.util.module_from_spec = module_from_spec_with_registration

# Give test subprocesses a default timeout unless the call already sets one.
subprocess.run = subprocess_run_with_default_timeout


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--timeout",
        action="store",
        type=float,
        default=300.0,
        help="Fail individual tests that run longer than this many seconds.",
    )


@pytest.fixture(autouse=True)
def _enforce_test_timeout(request: pytest.FixtureRequest):
    timeout = float(request.config.getoption("--timeout"))
    if timeout <= 0:
        yield
        return
    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _raise_timeout(_signum: int, _frame: object) -> None:
        raise TimeoutError(
            f"test exceeded --timeout={timeout:g}s: {request.node.nodeid}"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
