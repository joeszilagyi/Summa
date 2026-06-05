from __future__ import annotations

import math

import pytest

from tools.common.atomic_write import atomic_write_json, atomic_write_jsonl
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
