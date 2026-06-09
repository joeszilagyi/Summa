from __future__ import annotations

from typing import Any

import pytest

from tools.source_db_tools import provenance_events


class _FakeCursor:
    lastrowid = None


class _FakeConnection:
    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor()


def test_record_event_raises_when_sqlite_returns_no_row_id() -> None:
    with pytest.raises(RuntimeError, match="sqlite did not return a provenance_event row id"):
        provenance_events.record_event(
            _FakeConnection(),
            object_namespace="work",
            object_id=1,
            event_type="created",
        )
