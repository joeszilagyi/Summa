from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.scripts import (
    plan_local_git_repo_adapter,
    plan_local_source_adapter,
    plan_remote_url_manifest_adapter,
    plan_structured_data_source_adapter,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "source_adapter_runtime"


def make_duplicate_key_json(raw_json: str) -> str:
    return re.sub(r'("input_family"\s*:\s*"[^"]+")', r"\1,\n  \1", raw_json, count=1)


def make_non_standard_constant_json(raw_json: str) -> str:
    return re.sub(
        r'"description"\s*:\s*"[^"\\]*(?:\\.[^"\\]*)*"',
        '"description": NaN',
        raw_json,
        count=1,
    )


def local_git_repo_adapter_text() -> str:
    source_text = (FIXTURE_ROOT / "local_directory" / "source_adapter.json").read_text(encoding="utf-8")
    return source_text.replace('"input_family": "local_directory"', '"input_family": "local_git_repo"', 1)


def load_adapter_sources() -> list[tuple[object, str]]:
    return [
        (plan_local_source_adapter, (FIXTURE_ROOT / "local_directory" / "source_adapter.json").read_text(encoding="utf-8")),
        (plan_structured_data_source_adapter, (FIXTURE_ROOT / "local_directory" / "source_adapter.json").read_text(encoding="utf-8")),
        (
            plan_remote_url_manifest_adapter,
            (FIXTURE_ROOT / "remote_url_manifest" / "source_adapter.json").read_text(encoding="utf-8"),
        ),
        (plan_local_git_repo_adapter, local_git_repo_adapter_text()),
    ]


def test_planners_reject_duplicate_json_keys_on_adapter_re_read(tmp_path, monkeypatch) -> None:
    def fake_validate_source_adapter(_path):
        return {"errors": []}, 0

    for module, source_json in load_adapter_sources():
        adapter_path = tmp_path / f"{module.__name__.replace('.', '_')}.json"
        adapter_path.write_text(make_duplicate_key_json(source_json), encoding="utf-8")
        monkeypatch.setattr(module.validate_source_adapter, "validate_source_adapter", fake_validate_source_adapter)

        with pytest.raises(RuntimeError) as exc:
            module.load_adapter(adapter_path)
        assert "duplicate JSON object key" in str(exc.value)


def test_planners_reject_non_standard_json_constants_on_adapter_re_read(tmp_path, monkeypatch) -> None:
    def fake_validate_source_adapter(_path):
        return {"errors": []}, 0

    for module, source_json in load_adapter_sources():
        adapter_path = tmp_path / f"{module.__name__.replace('.', '_')}_constants.json"
        adapter_path.write_text(make_non_standard_constant_json(source_json), encoding="utf-8")
        monkeypatch.setattr(module.validate_source_adapter, "validate_source_adapter", fake_validate_source_adapter)

        with pytest.raises(RuntimeError) as exc:
            module.load_adapter(adapter_path)
        assert "non-standard JSON constant" in str(exc.value)
