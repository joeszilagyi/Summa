from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.common.publication_builder import PublicationBuildError, load_json_object


def test_publication_builder_loads_json_object(tmp_path: Path) -> None:
    path = tmp_path / "auxiliary.json"
    payload = {"source": {"name": "fixture"}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_json_object(path, label="auxiliary JSON") == payload


@pytest.mark.parametrize(
    "raw",
    ['{"source":{"name":"first","name":"second"}}', '{"score":Infinity}'],
)
def test_publication_builder_rejects_ambiguous_json(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "auxiliary.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(PublicationBuildError, match="failed to load auxiliary JSON"):
        load_json_object(path, label="auxiliary JSON")
