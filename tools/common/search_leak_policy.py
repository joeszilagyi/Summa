"""Shared search leak detection helpers for projection and result validators."""

from __future__ import annotations

import math
import re

_SECRET_PATTERN_TEXTS = (
    r"(?i:(?:\b(?:authorization\s*:\s*bearer|(?:api[_-]?key|secret|token|password|passwd|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|cookie|set-cookie|private[_\s-]?key)\s*[\"']?\s*[:=]|(?:cookie|set-cookie):\s*(?:(?:session(?:id)?|auth(?:entication)?|access|refresh|csrf|xsrf|token)[_-]?(?:token)?\s*=\s*[^\s;]{12,})|private\s+key)|-----BEGIN\s+(?:OPENSSH|RSA|DSA|EC|ED25519|ENCRYPTED)?\s*PRIVATE\s+KEY-----))",
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_])",
    r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_])",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])",
    r"(?i:\bBearer\s+)[A-Za-z0-9._~+/=-]{20,}",
    r"(?i:\bhttps?://[^\s\"'<>]*[?&](?:x-amz-signature|x-goog-signature|sig|signature)=)[^\s&#\"'<>]+",
)
SECRET_PATTERNS = tuple(re.compile(pattern) for pattern in _SECRET_PATTERN_TEXTS)
SECRET_RE = re.compile("|".join(f"(?:{pattern.pattern})" for pattern in SECRET_PATTERNS))
HIGH_ENTROPY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9+/_=-]{23,}(?![A-Za-z0-9])"
)
SECRET_FIELD_NAMES = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "session_id",
    "set_cookie",
    "token",
}
PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:^|[\s'\"(])(?:/home/|/Users/|/tmp/|file://|~/|[A-Za-z]:\\\\)[^\s'\"()]+"
)

RAW_PAYLOAD_FIELD_NAMES = {
    "body_text",
    "full_extracted_text",
    "full_text",
    "raw_payload",
    "raw_text",
}

PRIVATE_NOTE_FIELD_NAMES = {
    "internal_note",
    "note_text",
    "operator_note",
    "private_note",
}

RESTRICTED_PUBLIC_FIELD_NAMES = {
    "evidence_note",
    "operator_excerpt_text",
    "public_excerpt_text",
}


def normalize_field_name(value: str | None) -> str:
    return "" if value is None else value.strip().lower()


def _looks_high_entropy(token: str) -> bool:
    if len(token) < 24:
        return False
    has_lowercase = any(char.islower() for char in token)
    has_uppercase = any(char.isupper() for char in token)
    has_digit = any(char.isdigit() for char in token)
    # Treat identifier separators as structure rather than entropy. Otherwise
    # long snake_case values such as schema keys can look like opaque secrets.
    has_symbol = any(char in "+/=" for char in token)
    if not has_digit or not (has_uppercase or has_symbol):
        return False
    character_classes = sum(
        (
            has_lowercase,
            has_uppercase,
            has_digit,
            has_symbol,
        )
    )
    if character_classes < 3:
        return False
    counts = {char: token.count(char) for char in set(token)}
    entropy = -sum(
        (count / len(token)) * math.log2(count / len(token)) for count in counts.values()
    )
    return entropy >= 3.5


def find_secret_marker_spans(value: str | None) -> list[tuple[int, int]]:
    if not value:
        return []
    spans = [
        (match.start(), match.end())
        for pattern in SECRET_PATTERNS
        for match in pattern.finditer(value)
    ]
    spans.extend(
        (match.start(), match.end())
        for match in HIGH_ENTROPY_TOKEN_RE.finditer(value)
        if _looks_high_entropy(match.group(0))
    )
    selected: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda span: (span[0], -(span[1] - span[0]))):
        if any(
            start < selected_end and end > selected_start
            for selected_start, selected_end in selected
        ):
            continue
        selected.append((start, end))
    return selected


def _looks_like_secret_field_value(value: str) -> bool:
    normalized = value.strip().casefold()
    return bool(normalized) and normalized not in {
        "[redacted]",
        "<redacted>",
        "***",
        "null",
        "none",
    }


def is_secret_field_name(field_name: str | None) -> bool:
    normalized = normalize_field_name(field_name).replace("-", "_")
    return normalized in SECRET_FIELD_NAMES


def contains_secret_marker(value: str | None, *, field_name: str | None = None) -> bool:
    if not value:
        return False
    return bool(find_secret_marker_spans(value)) or (
        is_secret_field_name(field_name) and _looks_like_secret_field_value(value)
    )


def contains_private_path(value: str | None) -> bool:
    if not value:
        return False
    return bool(PRIVATE_PATH_RE.search(value))


def is_raw_payload_field(field_name: str | None) -> bool:
    return normalize_field_name(field_name) in RAW_PAYLOAD_FIELD_NAMES


def is_private_note_field(field_name: str | None) -> bool:
    return normalize_field_name(field_name) in PRIVATE_NOTE_FIELD_NAMES


def is_restricted_public_field(field_name: str | None) -> bool:
    return normalize_field_name(field_name) in RESTRICTED_PUBLIC_FIELD_NAMES
