from __future__ import annotations

import math
from pathlib import Path

import pytest

from tools.common.atomic_write import (
    _fsync_directory,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_jsonl,
)
from tools.validators.common import write_json


def test_atomic_write_json_rejects_nonstandard_json_constants(tmp_path) -> None:
    output = tmp_path / "artifact.json"

    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        atomic_write_json(output, {"value": math.nan})

    assert not output.exists()


def test_atomic_write_jsonl_rejects_nonstandard_json_constants(tmp_path) -> None:
    output = tmp_path / "artifact.jsonl"

    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        atomic_write_jsonl(output, [{"value": math.inf}])

    assert not output.exists()


def test_validator_write_json_rejects_nonstandard_json_constants(tmp_path) -> None:
    output = tmp_path / "report.json"

    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        write_json(str(output), {"value": -math.inf})

    assert not output.exists()


def test_atomic_write_json_streams_payload_without_prebuilding_text(monkeypatch, tmp_path) -> None:
    output = tmp_path / "artifact.json"

    def fail_dumps(*_args, **_kwargs):
        raise AssertionError("json.dumps should not be used by atomic_write_json")

    monkeypatch.setattr("tools.common.atomic_write.json.dumps", fail_dumps)

    atomic_write_json(output, {"b": 2, "a": 1})

    assert output.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "b": 2\n}\n'


def test_fsync_directory_is_best_effort_on_fsync_failure(monkeypatch, tmp_path) -> None:
    opened_fds: list[int] = []

    def fake_open(_path, _flags):
        opened_fds.append(11)
        return 11

    def fake_fsync(_fd):
        raise OSError("simulated fsync failure")

    def fake_close(fd):
        opened_fds.append(fd)

    monkeypatch.setattr("tools.common.atomic_write.os.open", fake_open)
    monkeypatch.setattr("tools.common.atomic_write.os.fsync", fake_fsync)
    monkeypatch.setattr("tools.common.atomic_write.os.close", fake_close)

    _fsync_directory(tmp_path)

    assert opened_fds == [11, 11]


def test_atomic_write_text_keeps_renamed_output_when_directory_fsync_fails(
    monkeypatch, tmp_path
) -> None:
    output = tmp_path / "artifact.txt"

    def fake_fsync_directory(_path):
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr("tools.common.atomic_write._fsync_directory", fake_fsync_directory)

    from tools.common.atomic_write import atomic_write_text

    atomic_write_text(output, "payload\n")

    assert output.read_text(encoding="utf-8") == "payload\n"


@pytest.mark.parametrize(
    "exc_type, expected_message",
    [
        (OSError, "cross-device link"),
        (PermissionError, "permission denied"),
    ],
)
def test_atomic_write_json_cleans_temp_files_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc_type: type[BaseException],
    expected_message: str,
) -> None:
    output = tmp_path / "artifact.json"

    def fail_replace(self: Path, target: Path) -> None:  # type: ignore[override]
        raise exc_type(expected_message)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(exc_type, match=expected_message):
        atomic_write_json(output, {"b": 2, "a": 1})

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "exc_type, expected_message",
    [
        (OSError, "cross-device link"),
        (PermissionError, "permission denied"),
    ],
)
def test_atomic_write_jsonl_cleans_temp_files_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc_type: type[BaseException],
    expected_message: str,
) -> None:
    output = tmp_path / "artifact.jsonl"

    def fail_replace(self: Path, target: Path) -> None:  # type: ignore[override]
        raise exc_type(expected_message)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(exc_type, match=expected_message):
        atomic_write_jsonl(output, [{"b": 2, "a": 1}])

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "exc_type, expected_message",
    [
        (OSError, "cross-device link"),
        (PermissionError, "permission denied"),
    ],
)
def test_atomic_write_bytes_cleans_temp_files_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc_type: type[BaseException],
    expected_message: str,
) -> None:
    output = tmp_path / "artifact.bin"

    def fail_replace(self: Path, target: Path) -> None:  # type: ignore[override]
        raise exc_type(expected_message)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(exc_type, match=expected_message):
        atomic_write_bytes(output, b"payload")

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []
