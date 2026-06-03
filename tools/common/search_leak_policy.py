"""Shared search leak detection helpers for projection and result validators."""

from __future__ import annotations

import re

SECRET_RE = re.compile(
    r"(?i)(authorization:\s*bearer|api[_-]?key\s*=|secret\s*=|token\s*=|private key)"
)
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


def contains_secret_marker(value: str | None) -> bool:
    if not value:
        return False
    return bool(SECRET_RE.search(value))


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
