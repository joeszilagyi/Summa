from __future__ import annotations

from pathlib import Path

from tools.common.atomic_write import atomic_write_json


def test_atomic_write_json_canonicalizes_key_order(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"

    atomic_write_json(left, {"b": 2, "a": {"z": 3, "y": 4}})
    atomic_write_json(right, {"a": {"y": 4, "z": 3}, "b": 2})

    assert left.read_text(encoding="utf-8") == right.read_text(encoding="utf-8")
    assert left.read_text(encoding="utf-8") == (
        '{\n'
        '  "a": {\n'
        '    "y": 4,\n'
        '    "z": 3\n'
        '  },\n'
        '  "b": 2\n'
        '}\n'
    )
