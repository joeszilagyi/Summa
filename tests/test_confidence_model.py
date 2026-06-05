from __future__ import annotations

import pytest

from tools.source_db_tools import confidence_model


@pytest.mark.parametrize(
    ("value", "expected_band"),
    [
        (0.0, "very_low"),
        (0.249, "very_low"),
        (0.25, "low"),
        (0.495, "low"),
        (0.50, "medium"),
        (0.749, "medium"),
        (0.75, "high"),
        (0.899, "high"),
        (0.90, "very_high"),
        (1.0, "very_high"),
    ],
)
def test_band_for_score_classifies_boundary_values(value: float, expected_band: str) -> None:
    assert confidence_model.band_for_score(value) == expected_band
