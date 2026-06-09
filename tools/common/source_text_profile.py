from __future__ import annotations

import re
from collections import Counter
from typing import TypedDict

URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+")
PARAGRAPH_SEPARATOR_PATTERN = re.compile(r"\n\s*\n")
WHITESPACE_PATTERN = re.compile(r"\s+")


class SourceTextProfile(TypedDict):
    encoding: str
    byte_count: int
    line_count: int
    url_count: int
    duplicate_block_count: int
    likely_boilerplate: bool


def count_source_text_lines(source_text: str) -> int:
    return len(source_text.splitlines())


def count_source_text_urls(source_text: str) -> int:
    return len(URL_PATTERN.findall(source_text))


def count_duplicate_source_text_blocks(source_text: str) -> int:
    normalized_blocks = [
        WHITESPACE_PATTERN.sub(" ", block.strip())
        for block in PARAGRAPH_SEPARATOR_PATTERN.split(source_text)
        if block.strip()
    ]
    if not normalized_blocks:
        return 0
    counts = Counter(normalized_blocks)
    return sum(count - 1 for count in counts.values() if count > 1)


def count_unique_non_empty_lines(source_text: str) -> tuple[int, int]:
    normalized_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    return len(normalized_lines), len(set(normalized_lines))


def build_source_text_profile(
    source_text: str, *, byte_count: int | None = None
) -> SourceTextProfile:
    encoded_byte_count = byte_count
    if encoded_byte_count is None:
        encoded_byte_count = len(source_text.encode("utf-8"))
    line_count, unique_line_count = count_unique_non_empty_lines(source_text)
    duplicate_block_count = count_duplicate_source_text_blocks(source_text)
    url_count = count_source_text_urls(source_text)
    likely_boilerplate = bool(
        duplicate_block_count > 0
        or (line_count >= 12 and line_count > 0 and unique_line_count * 2 <= line_count)
    )
    return {
        "encoding": "utf-8",
        "byte_count": encoded_byte_count,
        "line_count": count_source_text_lines(source_text),
        "url_count": url_count,
        "duplicate_block_count": duplicate_block_count,
        "likely_boilerplate": likely_boilerplate,
    }


def source_text_profile_hazard_flags(profile: SourceTextProfile) -> list[str]:
    flags: list[str] = []
    if profile["duplicate_block_count"] > 0:
        flags.append("duplicate_blocks")
    if profile["likely_boilerplate"]:
        flags.append("likely_boilerplate")
    if profile["url_count"] >= 5:
        flags.append("url_heavy")
    return flags
