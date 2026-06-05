from __future__ import annotations

from tools.scripts.execute_source_adapter import safe_decode_text


def test_safe_decode_text_accepts_valid_non_ascii_utf8_text() -> None:
    accented = safe_decode_text(("é" * 128).encode("utf-8"))
    cjk = safe_decode_text(("漢字" * 128).encode("utf-8"))

    assert accented == ("é" * 128, "utf8", None)
    assert cjk == ("漢字" * 128, "utf8", None)


def test_safe_decode_text_preserves_binary_and_invalid_utf8_classification() -> None:
    assert safe_decode_text(b"abc\x00def") == (None, "binary_unsupported", "binaryish_payload")
    assert safe_decode_text(b"\xff\xfe\xff") == (None, "invalid_utf8", "invalid_utf8")
