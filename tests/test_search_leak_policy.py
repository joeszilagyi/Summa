from __future__ import annotations

import pytest

from tools.common.search_leak_policy import contains_secret_marker


@pytest.mark.parametrize(
    "value",
    [
        "ghp_1234567890abcdefghijkl",
        "github_pat_11AAAAAABBBBBBCCCCCCDDDDDD",
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-value-123456789",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "Cookie: session=abcdefghijklmnopqrstuvwxyz",
        "Bearer abcdefghijklmnopqrstuvwxyz123456",
        "https://example.test/file?X-Amz-Signature=abcdef1234567890",
    ],
)
def test_contains_secret_marker_detects_common_secret_shapes(value: str) -> None:
    assert contains_secret_marker(value)


def test_contains_secret_marker_detects_high_entropy_opaque_tokens() -> None:
    assert contains_secret_marker("opaque=aB3dE7gH9jK2mN5pQ8rT4vW6xY1zC0f")


def test_contains_secret_marker_checks_sensitive_structured_fields() -> None:
    assert contains_secret_marker("opaque-value", field_name="access_token")
    assert not contains_secret_marker("[redacted]", field_name="access_token")


def test_contains_secret_marker_ignores_ordinary_public_text() -> None:
    assert not contains_secret_marker("This is ordinary public documentation.")
