"""Shared runtime logging helpers for tools and scripts.

This module centralizes log-path discovery, log rotation policy, and logger setup
for index-related tooling so each entry point writes consistent structured events.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from tools.common.operator_text import strip_terminal_escapes

DEFAULT_LOG_NAME = "index-actions.log"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_KEEP = 20

INDEX_LOG_ROTATE_MAX_BYTES_ENV = "INDEX_LOG_ROTATE_MAX_BYTES"
INDEX_LOG_ROTATE_KEEP_ENV = "INDEX_LOG_ROTATE_KEEP"
INDEX_LOG_ACTIVE_ENV = "INDEX_LOG_ACTIVE"
INDEX_RUN_ORIGIN_ENV = "INDEX_RUN_ORIGIN"

RUN_ORIGIN_DETECTION_STEPS = 12
RUN_ORIGIN_TIMEOUT_SECONDS = 0.25
LOG_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PS_PARENT_FIELDS = ("comm=", "ppid=")
ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:^|[\s'\"(])(?:/home/|/Users/|/tmp/|file://|~/|[A-Za-z]:\\\\)[^\s'\"()]+"
)
PROMPT_FIELD_RE = re.compile(
    r"(?i)\b((?:raw_)?(?:prompt|source_text)(?:_text)?)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)"
)


def env_non_negative_int(name: str, default: int) -> int:
    """Read an integer environment variable and validate non-negative values."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer: {raw_value!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer: {raw_value!r}")
    return value


def has_index_subject_tree(parent: Path) -> bool:
    """Return True when a directory layout looks like an index subject tree."""
    return (parent / "index" / "Places").is_dir() or (parent / "Places").is_dir()


def detect_index_root(start_path: Path) -> Path | None:
    """Find the nearest parent path that appears to be an index root."""
    start = start_path.resolve()
    for parent in [start, *start.parents]:
        if has_index_subject_tree(parent) and (parent / "dbs").is_dir():
            return parent
    return None


def env_value(*names: str) -> str | None:
    """Return the first defined environment value from ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return None


def env_non_negative_int_any(names: tuple[str, ...], default: int) -> int:
    """Return the first non-negative integer env var found in ``names``."""
    for name in names:
        if os.environ.get(name) is not None:
            return env_non_negative_int(name, default)
    return default


def sanitize_log_message(message: str) -> str:
    """Remove terminal escapes and obvious local path/prompt payloads from log text."""

    text = strip_terminal_escapes(message)
    text = PROMPT_FIELD_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = ABSOLUTE_PATH_RE.sub(lambda match: " " + "[redacted-path]", text)
    return text


class _SanitizeLogMessages(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = sanitize_log_message(message)
        record.args = ()
        return True


def _read_process_metadata(pid: int) -> tuple[str | None, int | None]:
    """Read a process command name and parent PID from ``ps`` without shell use."""
    try:
        output = subprocess.check_output(
            [
                "ps",
                "-h",
                "-o",
                PS_PARENT_FIELDS[0],
                "-o",
                PS_PARENT_FIELDS[1],
                "-p",
                str(pid),
            ],
            text=True,
            timeout=RUN_ORIGIN_TIMEOUT_SECONDS,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None, None

    pieces = output.split()
    if len(pieces) != 2:
        return None, None

    command, ppid_text = pieces
    if not ppid_text.isdigit():
        return command, None
    return command, int(ppid_text)


def detect_run_origin() -> str:
    """Infer whether execution was scheduled or manual by inspecting ancestors."""
    env = env_value(INDEX_RUN_ORIGIN_ENV)
    if env:
        return env
    pid = os.getppid()
    for _ in range(RUN_ORIGIN_DETECTION_STEPS):
        if pid <= 1:
            break
        comm, ppid = _read_process_metadata(pid)
        if not comm:
            break
        if comm in {"cron", "crond", "anacron"}:
            return "cron"
        if comm in {"systemd", "systemd-run", "atd"}:
            return "scheduled"
        if ppid is None or ppid <= 1:
            break
        pid = ppid
    return "manual"


def default_root_log_path(start_path: Path) -> Path:
    """Return the canonical runtime log path for a given working path."""
    index_root = detect_index_root(start_path)
    if index_root is not None:
        return index_root / "runtime" / "logs" / DEFAULT_LOG_NAME
    return start_path.resolve().parent / DEFAULT_LOG_NAME


def default_archive_dir(log_path: Path) -> Path:
    """Return the archive directory for a selected log path."""
    index_root = detect_index_root(log_path.parent)
    if index_root is not None:
        return index_root / "runtime" / "backups" / "logs"
    return log_path.parent / "logs_archive"


def rotate_monolithic_log(log_path: Path, archive_dir: Path, *, max_bytes: int = DEFAULT_MAX_BYTES, keep: int = DEFAULT_KEEP) -> bool:
    if not log_path.exists():
        return False
    if log_path.stat().st_size < max_bytes:
        return False

    archive_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(keep - 1, -1, -1):
        current = archive_dir / f"{log_path.name}.{idx}.tar.gz"
        if idx == keep - 1:
            if current.exists():
                current.unlink()
        else:
            nxt = archive_dir / f"{log_path.name}.{idx + 1}.tar.gz"
            if current.exists():
                current.rename(nxt)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        staged = tmpdir_path / log_path.name
        shutil.copy2(log_path, staged)
        target = archive_dir / f"{log_path.name}.0.tar.gz"
        with tarfile.open(target, "w:gz") as tf:
            tf.add(staged, arcname=log_path.name)
    log_path.write_bytes(b"")
    return True


def build_logger(tool_name: str, log_path: Path, *, verbose: bool = False, language: str = "py") -> logging.Logger:
    """Build a logger that writes runtime events with shared tool metadata."""
    log_path = log_path.resolve()
    archive_dir = default_archive_dir(log_path)
    rotate_monolithic_log(
        log_path,
        archive_dir,
        max_bytes=env_non_negative_int_any((INDEX_LOG_ROTATE_MAX_BYTES_ENV,), DEFAULT_MAX_BYTES),
        keep=env_non_negative_int_any((INDEX_LOG_ROTATE_KEEP_ENV,), DEFAULT_KEEP),
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    origin = detect_run_origin()
    logger = logging.getLogger(f"{tool_name}.{id(log_path)}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter(
        f"%(asctime)s tool={tool_name} lang={language} origin={origin} level=%(levelname)s %(message)s",
        LOG_TIME_FORMAT,
    )
    fmt.converter = time.gmtime

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_SanitizeLogMessages())
    logger.addHandler(file_handler)

    if env_value(INDEX_LOG_ACTIVE_ENV) != "1":
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO if verbose else logging.WARNING)
        stream_handler.setFormatter(fmt)
        stream_handler.addFilter(_SanitizeLogMessages())
        logger.addHandler(stream_handler)

    return logger
