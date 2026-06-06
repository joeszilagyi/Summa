from __future__ import annotations

from itertools import permutations
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


def test_atomic_write_json_canonicalizes_nested_permutations(tmp_path: Path) -> None:
    bodies: set[str] = set()
    base_items = [
        ("gamma", {"nested": {"b": 2, "a": 1}}),
        ("alpha", ["one", "two"]),
        ("beta", {"z": 3, "a": 1}),
    ]
    for index, item_order in enumerate(permutations(base_items)):
        path = tmp_path / f"variant-{index}.json"
        payload = dict(item_order)
        atomic_write_json(path, payload)
        bodies.add(path.read_text(encoding="utf-8"))

    assert len(bodies) == 1
