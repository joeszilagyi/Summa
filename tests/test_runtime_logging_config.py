import datetime as dt
import os
import time
from pathlib import Path

import pytest

from tools.common import runtime_logging


def test_python_logger_rejects_invalid_rotate_keep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INDEX_LOG_ROTATE_KEEP", "not-an-int")

    with pytest.raises(ValueError, match="INDEX_LOG_ROTATE_KEEP must be a non-negative integer"):
        runtime_logging.build_logger("test_tool", tmp_path / "index-actions.log")


def test_python_logger_rejects_negative_rotate_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("INDEX_LOG_ROTATE_MAX_BYTES", "-1")

    with pytest.raises(ValueError, match="INDEX_LOG_ROTATE_MAX_BYTES must be a non-negative integer"):
        runtime_logging.build_logger("test_tool", tmp_path / "index-actions.log")


def test_python_logger_uses_utc_z_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    if hasattr(time, "tzset"):
        original_tz = os.environ.get("TZ")
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        request.addfinalizer(lambda: _restore_tz(original_tz))

    log_path = tmp_path / "index-actions.log"
    logger = runtime_logging.build_logger("test_tool", log_path)
    logger.warning("timestamp probe")
    for handler in logger.handlers:
        handler.flush()

    line = log_path.read_text(encoding="utf-8").strip()
    timestamp = line.split(" ", 1)[0]
    assert timestamp.endswith("Z")
    logged_at = dt.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    now_utc = dt.datetime.now(dt.UTC)
    assert abs((now_utc - logged_at).total_seconds()) < 10


def _restore_tz(original_tz: str | None) -> None:
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    time.tzset()
