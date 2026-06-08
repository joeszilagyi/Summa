"""Shared wrapper contract and parser for untrusted source text in LLM prompts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "config" / "llm_source_text_wrapper_template.json"
TEMPLATE_SCHEMA_VERSION = "llm-source-text-wrapper-template.v1"
DEFAULT_TEMPLATE_ID = "default.untrusted_source_text.v1"
REQUIRED_METADATA_FIELDS = ("source_ref", "provenance", "hazard_flags", "instruction_negation")


class WrapperContractError(RuntimeError):
    """Raised when the checked-in wrapper contract is unreadable or malformed."""


@dataclass(frozen=True)
class WrapperTemplate:
    template_id: str
    begin_delimiter: str
    end_delimiter: str
    body_separator: str
    instruction_negation_guidance: str
    metadata_fields: tuple[str, ...]


@dataclass(frozen=True)
class WrappedSourceBlock:
    source_ref: str
    provenance: str
    hazard_flags: tuple[str, ...]
    instruction_negation: str
    source_text: str
    start_offset: int
    end_offset: int


def _ensure_source_text_is_contained(source_text: str, *, template: WrapperTemplate) -> None:
    for delimiter_name, delimiter in (("begin", template.begin_delimiter), ("end", template.end_delimiter)):
        if delimiter in source_text:
            raise WrapperContractError(
                f"source_text must not contain the {delimiter_name} wrapper delimiter: {delimiter}"
            )


def _require_nonblank_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WrapperContractError(f"{field} must be a non-blank string")
    return value.strip()


def _header_pattern(template: WrapperTemplate) -> re.Pattern[str]:
    begin = re.escape(template.begin_delimiter)
    separator = re.escape(template.body_separator)
    return re.compile(
        begin
        + r"\n"
        + r"source_ref: (?P<source_ref>[^\n]+)\n"
        + r"provenance: (?P<provenance>[^\n]+)\n"
        + r"hazard_flags: (?P<hazard_flags>[^\n]*)\n"
        + r"instruction_negation: (?P<instruction_negation>[^\n]+)\n"
        + r"(?:source_length: (?P<source_length>\d+)\n)?"
        + separator
        + r"\n",
        re.DOTALL,
    )


@cache
def load_template(path: Path = TEMPLATE_PATH) -> WrapperTemplate:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WrapperContractError(f"could not read wrapper template: {path}") from exc
    if not isinstance(payload, dict):
        raise WrapperContractError("wrapper template must be a JSON object")
    if payload.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
        raise WrapperContractError(f"schema_version must equal {TEMPLATE_SCHEMA_VERSION}")

    metadata_fields = payload.get("metadata_fields")
    if not isinstance(metadata_fields, list) or any(not isinstance(item, str) or not item.strip() for item in metadata_fields):
        raise WrapperContractError("metadata_fields must be an array of non-blank strings")
    normalized_metadata_fields = tuple(item.strip() for item in metadata_fields)
    if normalized_metadata_fields != REQUIRED_METADATA_FIELDS:
        raise WrapperContractError(
            "metadata_fields must match the required wrapper order: "
            + ", ".join(REQUIRED_METADATA_FIELDS)
        )

    return WrapperTemplate(
        template_id=_require_nonblank_string(payload, "template_id"),
        begin_delimiter=_require_nonblank_string(payload, "begin_delimiter"),
        end_delimiter=_require_nonblank_string(payload, "end_delimiter"),
        body_separator=_require_nonblank_string(payload, "body_separator"),
        instruction_negation_guidance=_require_nonblank_string(payload, "instruction_negation_guidance"),
        metadata_fields=normalized_metadata_fields,
    )


def parse_wrapped_blocks(prompt_text: str, *, template: WrapperTemplate | None = None) -> list[WrappedSourceBlock]:
    active_template = load_template() if template is None else template
    blocks: list[WrappedSourceBlock] = []
    begin_marker = f"{active_template.begin_delimiter}\n"
    end_marker = f"\n{active_template.end_delimiter}"
    body_separator = active_template.body_separator
    prompt_length = len(prompt_text)

    def read_line(start: int) -> tuple[str | None, int]:
        line_end = prompt_text.find("\n", start)
        if line_end == -1:
            return None, prompt_length
        return prompt_text[start:line_end], line_end + 1

    cursor = 0
    while True:
        start = prompt_text.find(begin_marker, cursor)
        if start == -1:
            break
        line_cursor = start + len(begin_marker)

        source_ref_line, line_cursor = read_line(line_cursor)
        provenance_line, line_cursor = read_line(line_cursor)
        hazard_flags_line, line_cursor = read_line(line_cursor)
        instruction_negation_line, line_cursor = read_line(line_cursor)
        separator_candidate, after_separator_cursor = read_line(line_cursor)

        if (
            source_ref_line is None
            or provenance_line is None
            or hazard_flags_line is None
            or instruction_negation_line is None
            or separator_candidate is None
            or not source_ref_line.startswith("source_ref: ")
            or not provenance_line.startswith("provenance: ")
            or not hazard_flags_line.startswith("hazard_flags: ")
            or not instruction_negation_line.startswith("instruction_negation: ")
        ):
            cursor = start + 1
            continue

        source_length: int | None = None
        separator_line = separator_candidate
        source_text_start = after_separator_cursor
        if separator_candidate.startswith("source_length: "):
            try:
                source_length = int(separator_candidate[len("source_length: ") :].strip())
            except ValueError:
                cursor = start + 1
                continue
            separator_line, source_text_start = read_line(after_separator_cursor)
            if separator_line is None:
                cursor = start + 1
                continue

        if separator_line != body_separator:
            cursor = start + 1
            continue

        if source_length is not None:
            source_text_end = source_text_start + source_length
            if source_text_end > prompt_length:
                cursor = start + 1
                continue
            if not prompt_text.startswith(end_marker, source_text_end):
                cursor = start + 1
                continue
            source_text = prompt_text[source_text_start:source_text_end]
            raw_end = source_text_end + len(end_marker)
        else:
            source_text_end = prompt_text.find(end_marker, source_text_start)
            if source_text_end == -1:
                cursor = start + 1
                continue
            source_text = prompt_text[source_text_start:source_text_end]
            raw_end = source_text_end + len(end_marker)

        hazard_flags_text = hazard_flags_line[len("hazard_flags: ") :].strip()
        hazard_flags = tuple(
            item.strip() for item in hazard_flags_text.split(",") if item.strip()
        )
        blocks.append(
            WrappedSourceBlock(
                source_ref=source_ref_line[len("source_ref: ") :].strip(),
                provenance=provenance_line[len("provenance: ") :].strip(),
                hazard_flags=hazard_flags,
                instruction_negation=instruction_negation_line[len("instruction_negation: ") :].strip(),
                source_text=source_text,
                start_offset=start,
                end_offset=raw_end,
            )
        )
        cursor = raw_end
    return blocks


def default_template_id() -> str:
    return load_template().template_id


def render_wrapped_block(
    *,
    source_ref: str,
    provenance: str,
    hazard_flags: list[str] | tuple[str, ...],
    source_text: str,
    template: WrapperTemplate | None = None,
) -> str:
    active_template = load_template() if template is None else template
    _ensure_source_text_is_contained(source_text, template=active_template)
    normalized_flags = ", ".join(item.strip() for item in hazard_flags if item.strip())
    return (
        f"{active_template.begin_delimiter}\n"
        f"source_ref: {source_ref.strip()}\n"
        f"provenance: {provenance.strip()}\n"
        f"hazard_flags: {normalized_flags}\n"
        f"instruction_negation: {active_template.instruction_negation_guidance}\n"
        f"source_length: {len(source_text)}\n"
        f"{active_template.body_separator}\n"
        f"{source_text}\n"
        f"{active_template.end_delimiter}"
    )
