"""File-backed subprocess capture helpers.

These helpers stream child stdout and stderr to files instead of buffering the
output in memory. Callers can still read the full text lazily when they need it,
and error handling can use bounded tail excerpts instead of full logs.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TAIL_LINE_COUNT = 80


def tail_text(path: Path, *, line_count: int = DEFAULT_TAIL_LINE_COUNT) -> str:
    if line_count <= 0:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=line_count)
    except OSError:
        return ""
    return "".join(lines).rstrip()


@dataclass
class FileBackedCompletedProcess:
    """A subprocess result whose output lives on disk until accessed."""

    args: list[str]
    returncode: int
    stdout_path: Path
    stderr_path: Path
    _stdout_cache: str | None = field(default=None, init=False, repr=False)
    _stderr_cache: str | None = field(default=None, init=False, repr=False)

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @property
    def stdout(self) -> str:
        if self._stdout_cache is None:
            self._stdout_cache = self._read_text(self.stdout_path)
        return self._stdout_cache

    @property
    def stderr(self) -> str:
        if self._stderr_cache is None:
            self._stderr_cache = self._read_text(self.stderr_path)
        return self._stderr_cache

    def stdout_tail(self, *, line_count: int = DEFAULT_TAIL_LINE_COUNT) -> str:
        return tail_text(self.stdout_path, line_count=line_count)

    def stderr_tail(self, *, line_count: int = DEFAULT_TAIL_LINE_COUNT) -> str:
        return tail_text(self.stderr_path, line_count=line_count)


def run_streaming_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> FileBackedCompletedProcess:
    """Run a command with stdout/stderr streamed to files.

    The child process writes directly to files. Callers can read the files
    later via the returned result object or, when a timeout occurs, via the
    paths attached to the raised ``TimeoutExpired`` instance.
    """

    temp_dir: Path | None = None
    if stdout_path is None or stderr_path is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="summa-subprocess-"))
        stdout_path = stdout_path or temp_dir / "stdout.txt"
        stderr_path = stderr_path or temp_dir / "stderr.txt"
    assert stdout_path is not None
    assert stderr_path is not None
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        try:
            proc = subprocess.run(
                command,
                cwd=cwd,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            exc.stdout_path = stdout_path  # type: ignore[attr-defined]
            exc.stderr_path = stderr_path  # type: ignore[attr-defined]
            exc.command = list(command)  # type: ignore[attr-defined]
            if temp_dir is not None:
                exc.temp_dir = temp_dir  # type: ignore[attr-defined]
            raise
    return FileBackedCompletedProcess(
        args=list(command),
        returncode=proc.returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def command_output_excerpt(result: Any, *, line_count: int = DEFAULT_TAIL_LINE_COUNT) -> str:
    for attr in ("stderr_tail", "stdout_tail"):
        method = getattr(result, attr, None)
        if callable(method):
            text = method(line_count=line_count)
            if isinstance(text, str) and text.strip():
                return text.strip()
    for attr in ("stderr", "stdout"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def timeout_output_excerpt(
    exc: subprocess.TimeoutExpired, *, line_count: int = DEFAULT_TAIL_LINE_COUNT
) -> str:
    for attr in ("stderr_path", "stdout_path"):
        path = getattr(exc, attr, None)
        if isinstance(path, Path):
            text = tail_text(path, line_count=line_count)
            if text.strip():
                return text.strip()
    for attr in ("stderr", "output"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
