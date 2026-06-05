from __future__ import annotations

from tools.source_db_tools import canonical_store


def test_stable_write_key_distinguishes_structurally_different_inputs() -> None:
    assert canonical_store.stable_write_key("x", "a|b", "c") != canonical_store.stable_write_key(
        "x", "a", "b|c"
    )
    assert canonical_store.stable_write_key("x", None) != canonical_store.stable_write_key("x", "")
    assert canonical_store.stable_write_key("x", 123) != canonical_store.stable_write_key(
        "x", "123"
    )
    assert canonical_store.stable_write_key("x", True) != canonical_store.stable_write_key(
        "x", 1
    )


def test_stable_write_key_is_deterministic_for_same_inputs() -> None:
    first = canonical_store.stable_write_key("x", "a|b", None, 123, True)
    second = canonical_store.stable_write_key("x", "a|b", None, 123, True)
    assert first == second
