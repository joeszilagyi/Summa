#!/usr/bin/env python3
"""Render and optionally execute one narrow gather prompt bundle."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "tools" / "scripts"
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
for candidate in (REPO_ROOT, SCRIPTS_DIR, VALIDATORS_DIR):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

from tools.common.leak_scanner import scan_text  # noqa: E402
from tools.common.llm_source_text_wrapper import (  # noqa: E402
    WrapperTemplate,
    load_template,
    parse_wrapped_blocks,
    render_wrapped_block,
)
from tools.scripts import resolve_gather_domain_pack, resolve_subject_runtime  # noqa: E402
from tools.source_db_tools import canonical_store  # noqa: E402
from tools.validators.validate_candidate_feedback_plan import (  # noqa: E402
    EXIT_PASS as EXIT_FEEDBACK_PLAN_PASS,
)
from tools.validators.validate_candidate_feedback_plan import (  # noqa: E402
    validate_candidate_feedback_plan,
)
from tools.validators.validate_gather_candidate_batch import (  # noqa: E402
    VALIDATOR_NAME,
    validate_gather_candidate_batch,
)

DRIVER_NAME = "run_topic_gather.py"
DRIVER_VERSION = "gather-driver.v1"
SCHEMA_VERSION = "gather-candidate-batch.v1"
DEFAULT_MODE = "dry-run"
DEFAULT_PHASE = "01a"
DEFAULT_ENGINE = "codex"
DEFAULT_CYCLE_DEPTH = 1
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0
RUNS_ROOT = Path("runs") / "gather"
LLM_RUNNER_PATH = REPO_ROOT / "tools" / "scripts" / "lib" / "llm_runner.sh"
LLM_RUNNER_BRIDGE_PATH = REPO_ROOT / "tools" / "scripts" / "lib" / "llm_runner_bridge.sh"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STAMP_RUN_TS_FORMAT = "%Y-%m-%dT%H%M%SZ"
SOURCE_TEXT_BLOCK_BYTE_CAP = 256 * 1024
SOURCE_TEXT_HAZARD_SCAN_OVERLAP = 256
HOSTILE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "prompt_injection_text": (
        re.compile(r"ignore previous instructions", re.IGNORECASE),
        re.compile(r"\bsystem prompt\b", re.IGNORECASE),
        re.compile(r"\bdeveloper message\b", re.IGNORECASE),
        re.compile(r"\brun shell command\b", re.IGNORECASE),
        re.compile(r"\btool call\b", re.IGNORECASE),
    ),
    "hostile_markup": (
        re.compile(r"<!--"),
        re.compile(r"<script", re.IGNORECASE),
        re.compile(r"</[A-Za-z]"),
    ),
}


def _compile_hazard_pattern(patterns: tuple[re.Pattern[str], ...]) -> re.Pattern[str]:
    joined = "|".join(
        f"(?i:{pattern.pattern})" if pattern.flags & re.IGNORECASE else f"(?:{pattern.pattern})"
        for pattern in patterns
    )
    return re.compile(joined)


HOSTILE_HAZARD_REGEXES: dict[str, re.Pattern[str]] = {
    flag: _compile_hazard_pattern(patterns) for flag, patterns in HOSTILE_PATTERNS.items()
}
CANDIDATE_TYPE_HINTS = {
    "sources": "source_lead",
    "timeline": "timeline_item",
    "people": "person",
    "places": "place",
    "works": "work",
    "open_questions": "open_question",
}
PRIOR_STATE_POLICY = canonical_store.DEFAULT_GATHER_PRIOR_STATE_POLICY


class GatherDriverError(RuntimeError):
    """Raised when the gather driver cannot complete a run."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve one subject runtime, render one active gather prompt bundle, "
            "and emit a validated workspace-local candidate batch."
        )
    )
    parser.add_argument(
        "--subject",
        required=True,
        help="Subject manifest path, or a subject_id resolved from <workspace>/.indexer/subject_manifest.json.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Workspace root used for the local runs/gather/<run-id>/ output path.",
    )
    parser.add_argument(
        "--facet",
        help="One enabled gather facet from the resolved subject runtime. Optional when --feedback-plan supplies the next action.",
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "live"),
        default=DEFAULT_MODE,
        help="dry-run renders and validates without invoking an engine; live invokes llm_runner.sh.",
    )
    parser.add_argument(
        "--phase",
        choices=resolve_subject_runtime.PHASE_KEYS,
        default=DEFAULT_PHASE,
        help="Prompt phase within the selected bundle (default: 01a).",
    )
    parser.add_argument(
        "--engine",
        choices=("codex", "claude"),
        default=DEFAULT_ENGINE,
        help="Live engine to request through tools/scripts/lib/llm_runner.sh (default: codex).",
    )
    parser.add_argument(
        "--run-id",
        help="Optional stable run identifier. Required for deterministic fixture output.",
    )
    parser.add_argument(
        "--created-at",
        help="Optional RFC3339 UTC timestamp override, for example 2026-06-03T12:34:56Z.",
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=float,
        help=(
            "Maximum seconds to allow each child subprocess call. Defaults to 600 seconds "
            "when not supplied."
        ),
    )
    parser.add_argument(
        "--source-text-file",
        action="append",
        default=[],
        help="Optional local UTF-8 text file whose bytes will be wrapped as untrusted source text. May be repeated.",
    )
    parser.add_argument(
        "--db",
        help="Canonical SQLite store used for optional prior-state gather context.",
    )
    parser.add_argument(
        "--feedback-plan",
        help="Optional candidate-feedback-plan JSON artifact used to select the next gather action.",
    )
    parser.add_argument(
        "--use-prior-state",
        action="store_true",
        help="Inject bounded prior canonical state for the resolved subject into the rendered prompt.",
    )
    parser.add_argument(
        "--cycle-depth",
        type=int,
        help="1-based gather cycle depth recorded in the batch artifact. Defaults to the feedback plan cycle or 1.",
    )
    parser.add_argument(
        "--previous-run-id",
        action="append",
        default=[],
        help="Optional prior gather run_id to record in iteration metadata. May be repeated.",
    )
    parser.add_argument(
        "--prior-state-limit",
        type=int,
        default=canonical_store.DEFAULT_GATHER_PRIOR_STATE_LIMIT,
        help="Maximum rows per prior-state family to include (default: 5).",
    )
    parser.add_argument(
        "--prior-state-max-chars",
        type=int,
        default=canonical_store.DEFAULT_GATHER_PRIOR_STATE_MAX_CHARS,
        help="Maximum rendered prior-state context characters (default: 5000).",
    )
    parser.add_argument(
        "--prior-state-policy",
        choices=(PRIOR_STATE_POLICY,),
        default=PRIOR_STATE_POLICY,
        help="Bounded prior-state selection policy (default: accepted-and-open-leads).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the run summary.",
    )
    parser.add_argument(
        "--debug-rendered-prompt",
        action="store_true",
        help="Include the full rendered prompt in the batch artifact after leak scanning.",
    )
    return parser.parse_args()


def utc_now_text() -> str:
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


def require_rfc3339_utc(value: str, *, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatherDriverError(f"{field_name} must be an RFC3339 date-time: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GatherDriverError(f"{field_name} must include an explicit timezone: {value}")
    return parsed.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def slugify_run_component(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "_", value.lower()).strip("._-")
    return normalized or "gather"


def build_run_id(subject_id: str, facet: str, phase: str, created_at: str) -> str:
    created_compact = (
        created_at.replace("-", "").replace(":", "").replace("T", "t").replace("Z", "z").lower()
    )
    return ".".join(
        (
            "gather",
            slugify_run_component(subject_id),
            slugify_run_component(facet),
            slugify_run_component(phase),
            created_compact,
        )
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_text_fingerprint(source_text: str) -> tuple[int, str]:
    encoded_text = source_text.encode("utf-8")
    return len(encoded_text), hashlib.sha256(encoded_text).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_command_timeout_seconds(args: argparse.Namespace) -> float:
    timeout_seconds = getattr(args, "command_timeout_seconds", None)
    if timeout_seconds is None:
        return DEFAULT_COMMAND_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        raise GatherDriverError("--command-timeout-seconds must be greater than zero")
    return float(timeout_seconds)


def read_text_file(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GatherDriverError(f"{label} not found: {path}") from exc
    except OSError as exc:
        raise GatherDriverError(f"could not read {label}: {path}") from exc
    except UnicodeDecodeError as exc:
        raise GatherDriverError(f"{label} must be valid UTF-8 text: {path}") from exc


def ensure_file(path: Path, *, label: str) -> Path:
    if not path.is_file():
        raise GatherDriverError(f"{label} not found: {path}")
    return path


def detect_hazard_flags(source_text: str) -> list[str]:
    flags: list[str] = []
    for flag, pattern in HOSTILE_HAZARD_REGEXES.items():
        if pattern.search(source_text):
            flags.append(flag)
    return flags


def iter_source_text_chunks(path: Path, *, byte_cap: int = SOURCE_TEXT_BLOCK_BYTE_CAP) -> Iterator[str]:
    if byte_cap <= 0:
        raise GatherDriverError("source text byte cap must be greater than zero")
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as handle:
        while True:
            raw_chunk = handle.read(byte_cap)
            if not raw_chunk:
                break
            text_chunk = decoder.decode(raw_chunk, final=False)
            if text_chunk:
                yield text_chunk
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail


def resolve_source_text_blocks(
    paths: list[str], *, template: WrapperTemplate
) -> tuple[list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    rendered_blocks: list[str] = []
    for index, raw_path in enumerate(paths, start=1):
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            source_path = (Path.cwd() / source_path).resolve()
        source_ref = f"file:{source_path}"
        provenance = f"local_text_file:{source_path}"
        ensure_file(source_path, label="source text file")
        try:
            source_size = source_path.stat().st_size
        except OSError as exc:
            raise GatherDriverError(f"could not stat source text file: {source_path}") from exc

        if source_size <= SOURCE_TEXT_BLOCK_BYTE_CAP:
            source_text = read_text_file(source_path, label="source text file")
            hazard_flags = detect_hazard_flags(source_text)
            rendered_blocks.append(
                render_wrapped_block(
                    source_ref=source_ref,
                    provenance=provenance,
                    hazard_flags=hazard_flags,
                    source_text=source_text,
                    template=template,
                )
            )
            byte_count, sha256 = source_text_fingerprint(source_text)
            blocks.append(
                {
                    "block_id": f"source-block-{index:04d}",
                    "source_ref": source_ref,
                    "provenance": provenance,
                    "hazard_flags": hazard_flags,
                    "byte_count": byte_count,
                    "sha256": sha256,
                }
            )
            continue

        chunk_count = 0
        hazard_scan_tail = ""
        for chunk_index, source_text in enumerate(iter_source_text_chunks(source_path), start=1):
            chunk_count = chunk_index
            chunk_bytes = source_text.encode("utf-8")
            digest = hashlib.sha256()
            digest.update(chunk_bytes)
            chunk_source_ref = f"{source_ref}#chunk-{chunk_index:04d}"
            chunk_provenance = f"{provenance}#chunk-{chunk_index:04d}"
            hazard_flags = detect_hazard_flags(hazard_scan_tail + source_text)
            hazard_scan_tail = (hazard_scan_tail + source_text)[-SOURCE_TEXT_HAZARD_SCAN_OVERLAP:]
            rendered_blocks.append(
                render_wrapped_block(
                    source_ref=chunk_source_ref,
                    provenance=chunk_provenance,
                    hazard_flags=hazard_flags,
                    source_text=source_text,
                    template=template,
                )
            )
            blocks.append(
                {
                    "block_id": f"source-block-{index:04d}-{chunk_index:04d}",
                    "source_ref": chunk_source_ref,
                    "provenance": chunk_provenance,
                    "hazard_flags": hazard_flags,
                    "byte_count": len(chunk_bytes),
                    "sha256": digest.hexdigest(),
                }
            )
        if chunk_count == 0:
            source_text = ""
            hazard_flags = detect_hazard_flags(source_text)
            rendered_blocks.append(
                render_wrapped_block(
                    source_ref=source_ref,
                    provenance=provenance,
                    hazard_flags=hazard_flags,
                    source_text=source_text,
                    template=template,
                )
            )
            blocks.append(
                {
                    "block_id": f"source-block-{index:04d}",
                    "source_ref": source_ref,
                    "provenance": provenance,
                    "hazard_flags": hazard_flags,
                    "byte_count": 0,
                    "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                }
            )
    return blocks, rendered_blocks


def resolve_runtime_inputs(args: argparse.Namespace) -> dict[str, Any]:
    return resolve_subject_runtime.resolve_subject_runtime(args.subject, args.workspace)


def resolve_gather_inputs(
    *,
    runtime: dict[str, Any],
    facet: str,
    phase: str,
) -> dict[str, Any]:
    subject = runtime["subject"]
    if facet not in subject["enabled_facets"]:
        raise GatherDriverError(f"facet not enabled in subject manifest: {facet}")

    pack = resolve_gather_domain_pack.load_domain_pack(subject["domain_pack"])
    enabled_facets = pack.get("enabled_facets")
    if not isinstance(enabled_facets, list) or facet not in enabled_facets:
        raise GatherDriverError(f"facet not enabled by domain pack: {facet}")

    try:
        bundle = resolve_subject_runtime.resolve_prompt_bundles(pack, [facet])[facet]
    except resolve_subject_runtime.ResolutionError as exc:
        raise GatherDriverError(str(exc)) from exc

    phase_template_files = bundle.get("resolved_phase_template_files")
    if not isinstance(phase_template_files, dict):
        raise GatherDriverError(
            f"prompt bundle does not resolve template files for phase selection: {facet}"
        )
    selected_template_file = phase_template_files.get(phase)
    if not isinstance(selected_template_file, str) or not selected_template_file:
        raise GatherDriverError(
            f"prompt bundle has no checked-in prompt file for phase {phase}: {facet}"
        )
    selected_template_path = ensure_file(REPO_ROOT / selected_template_file, label="prompt file")

    phase_templates = bundle.get("phase_templates")
    if not isinstance(phase_templates, dict):
        raise GatherDriverError(f"prompt bundle has no phase_templates mapping: {facet}")
    selected_template_id = phase_templates.get(phase)
    if not isinstance(selected_template_id, str) or not selected_template_id:
        raise GatherDriverError(f"prompt bundle has no template_id for phase {phase}: {facet}")

    try:
        template = load_template()
    except RuntimeError as exc:
        raise GatherDriverError(str(exc)) from exc
    wrapper_template_id = bundle.get("source_text_wrapper_template_id")
    if wrapper_template_id != template.template_id:
        raise GatherDriverError(
            f"wrapper template not supported by the live contract: {wrapper_template_id}"
        )

    return {
        "runtime": runtime,
        "subject": subject,
        "domain_pack": pack,
        "bundle": bundle,
        "facet": facet,
        "phase": phase,
        "selected_template_file": selected_template_file,
        "selected_template_path": selected_template_path,
        "selected_template_id": selected_template_id,
        "wrapper_template": template,
    }


def validate_iteration_args(args: argparse.Namespace) -> None:
    if not args.facet:
        raise GatherDriverError(
            "--facet is required unless --feedback-plan supplies the next action"
        )
    if args.cycle_depth < 1:
        raise GatherDriverError("--cycle-depth must be at least 1")
    if args.prior_state_limit < 0:
        raise GatherDriverError("--prior-state-limit must be non-negative")
    if args.prior_state_max_chars <= 0:
        raise GatherDriverError("--prior-state-max-chars must be positive")
    if args.use_prior_state and not args.db:
        raise GatherDriverError("--db is required when --use-prior-state is set")
    if not args.use_prior_state:
        if args.previous_run_id:
            raise GatherDriverError("--previous-run-id requires --use-prior-state")
        if args.cycle_depth != DEFAULT_CYCLE_DEPTH:
            raise GatherDriverError("--cycle-depth > 1 requires --use-prior-state")


def load_feedback_plan(raw_path: str) -> dict[str, Any]:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    report, exit_code = validate_candidate_feedback_plan(path)
    if exit_code != EXIT_FEEDBACK_PLAN_PASS:
        messages = "; ".join(error["message"] for error in report.get("errors", []))
        raise GatherDriverError(f"feedback plan failed validation: {messages or path}")
    raw_text = read_text_file(path, label="feedback plan")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GatherDriverError(
            f"feedback plan is not valid JSON: {path} (line {exc.lineno})"
        ) from exc
    if not isinstance(payload, dict):
        raise GatherDriverError(f"feedback plan must be a JSON object: {path}")
    return {
        "path": path,
        "hash": sha256_text(raw_text),
        "payload": payload,
    }


def apply_feedback_plan_defaults(
    args: argparse.Namespace,
    *,
    subject: dict[str, Any],
    feedback_plan: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if feedback_plan is None:
        if args.cycle_depth is None:
            args.cycle_depth = DEFAULT_CYCLE_DEPTH
        return None

    payload = feedback_plan["payload"]
    next_action = payload.get("next_action")
    if not isinstance(next_action, dict):
        raise GatherDriverError("feedback plan is missing next_action")
    if next_action.get("subject_id") != subject["subject_id"]:
        raise GatherDriverError(
            "feedback plan subject_id does not match the resolved gather subject"
        )

    selected_facet = next_action.get("selected_facet")
    if not isinstance(selected_facet, str) or not selected_facet:
        raise GatherDriverError("feedback plan next_action.selected_facet is required")
    if args.facet is None:
        args.facet = selected_facet
    elif args.facet != selected_facet:
        # Explicit CLI facet override is allowed, but we still record the plan's choice.
        args.facet = args.facet.strip()

    if args.cycle_depth is None:
        plan_cycle_depth = next_action.get("cycle_depth")
        if not isinstance(plan_cycle_depth, int) or plan_cycle_depth < 1:
            raise GatherDriverError(
                "feedback plan next_action.cycle_depth must be a positive integer"
            )
        args.cycle_depth = plan_cycle_depth

    if not args.previous_run_id:
        previous_run_ids = next_action.get("previous_run_ids_considered")
        if isinstance(previous_run_ids, list) and all(
            isinstance(item, str) and item for item in previous_run_ids
        ):
            args.previous_run_id = list(previous_run_ids)

    if not args.use_prior_state and next_action.get("use_prior_state") is True:
        args.use_prior_state = True

    return next_action


def resolve_prior_state_context(
    args: argparse.Namespace,
    *,
    subject_id: str,
) -> dict[str, Any] | None:
    validate_iteration_args(args)
    if not args.use_prior_state:
        return None
    if args.db is None:
        raise GatherDriverError("--db is required when --use-prior-state is set")

    db_path = canonical_store.resolve_db_path(args.db)
    try:
        canonical_store.check_canonical_store(db_path)
        conn = canonical_store.connect_existing_read_only(db_path)
    except canonical_store.CanonicalStoreError as exc:
        raise GatherDriverError(f"prior-state store is not usable: {exc}") from exc
    try:
        prior_state = canonical_store.load_gather_prior_state(
            conn,
            subject_id=subject_id,
            per_family_limit=args.prior_state_limit,
            high_confidence_threshold=canonical_store.DEFAULT_GATHER_PRIOR_STATE_HIGH_CONFIDENCE,
            policy=args.prior_state_policy,
        )
    finally:
        conn.close()

    try:
        return canonical_store.build_prior_state_context(
            prior_state,
            cycle_depth=args.cycle_depth,
            previous_run_ids=args.previous_run_id,
            max_chars=args.prior_state_max_chars,
        )
    except canonical_store.CanonicalStoreError as exc:
        raise GatherDriverError(f"could not build prior-state context: {exc}") from exc


def render_untrusted_json_block(
    *,
    source_ref: str,
    provenance: str,
    payload: dict[str, Any],
    template: WrapperTemplate,
) -> str:
    return render_wrapped_block(
        source_ref=source_ref,
        provenance=provenance,
        hazard_flags=["prompt_injection_text"],
        source_text=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        template=template,
    )


def render_prompt_text(
    *,
    prompt_body: str,
    subject: dict[str, Any],
    facet: str,
    phase: str,
    bundle: dict[str, Any],
    wrapped_blocks: list[str],
    next_action: dict[str, Any] | None = None,
    prior_state: dict[str, Any] | None = None,
    template: WrapperTemplate,
) -> str:
    subject_metadata = {
        "subject_id": subject["subject_id"],
        "display_name": subject["display_name"],
        "domain_pack": subject["domain_pack"],
        "scope_statement": subject["scope_statement"],
    }
    source_block_section = "\n\n".join(wrapped_blocks) if wrapped_blocks else "(none supplied)"
    subject_block = render_untrusted_json_block(
        source_ref="metadata:subject",
        provenance="subject manifest metadata",
        payload=subject_metadata,
        template=template,
    )
    next_action_block = (
        render_untrusted_json_block(
            source_ref="metadata:feedback-plan",
            provenance="candidate feedback plan next action",
            payload=next_action,
            template=template,
        )
        if isinstance(next_action, dict)
        else ""
    )
    prior_state_block = (
        render_untrusted_json_block(
            source_ref="metadata:prior-state",
            provenance="prior canonical state context",
            payload=prior_state,
            template=template,
        )
        if isinstance(prior_state, dict)
        else ""
    )
    metadata_sections = [
        f"Untrusted subject metadata:\n{subject_block}\n",
    ]
    if next_action_block:
        metadata_sections.append(f"Untrusted feedback-plan metadata:\n{next_action_block}\n")
    if prior_state_block:
        metadata_sections.append(
            f"Untrusted prior canonical state metadata:\n{prior_state_block}\n"
        )
    return (
        f"{prompt_body.rstrip()}\n\n"
        "Subject runtime:\n"
        f"- subject_id: {subject['subject_id']}\n"
        f"- facet: {facet}\n"
        f"- phase: {phase}\n"
        f"- prompt_bundle_id: {bundle['bundle_id']}\n"
        f"- prompt_bundle_key: {bundle['bundle_key']}\n"
        f"- wrapper_template_id: {bundle['source_text_wrapper_template_id']}\n\n"
        + "".join(metadata_sections)
        + "Wrapped source text blocks:\n"
        + f"{source_block_section}\n"
    )


def candidate_type_hint_for_facet(facet: str) -> str:
    return CANDIDATE_TYPE_HINTS.get(facet, "unknown")


def write_text(path: Path, body: str, *, sync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(body)
        handle.flush()
        if sync:
            os.fsync(handle.fileno())
    try:
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def sync_paths(paths: list[Path]) -> None:
    for path in paths:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())


def write_json(path: Path, payload: dict[str, Any], *, sync: bool = True) -> None:
    write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        sync=sync,
    )


def parse_stamp_footer(text: str) -> dict[str, str]:
    footer_delimiter = "\n---\n"
    footer_prefix = f"{footer_delimiter}RUN_META_VERSION: "
    start = text.rfind(footer_prefix)
    if start < 0:
        raise GatherDriverError("stamped engine output is missing the llm_runner footer")
    footer_text = text[start + len(footer_delimiter) :]
    values: dict[str, str] = {}
    for line in footer_text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise GatherDriverError("stamped engine output footer is malformed")
        key, raw_value = line.split(":", 1)
        values[key.strip()] = raw_value.strip()
    required = ("RUN_META_VERSION", "GENERATED_BY", "MODEL", "PLACE", "FACET", "PHASE", "RUN_TS")
    for key in required:
        if key not in values:
            raise GatherDriverError(f"stamped engine output footer is missing {key}")
    return {
        "run_meta_version": values["RUN_META_VERSION"],
        "generated_by": values["GENERATED_BY"],
        "model": values["MODEL"],
        "place": values["PLACE"],
        "facet": values["FACET"],
        "phase": values["PHASE"],
        "run_ts": values["RUN_TS"],
    }


def invoke_llm_runner_bridge(
    command: list[str], *, label: str, timeout_seconds: float | None
) -> None:
    try:
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_value = timeout_seconds if timeout_seconds is not None else "unset"
        raise GatherDriverError(f"{label} exceeded timeout after {timeout_value} seconds") from exc
    if proc.returncode != 0:
        message = (proc.stdout + proc.stderr).strip()
        raise GatherDriverError(
            f"{label} failed via llm_runner bridge: {message or f'exit {proc.returncode}'}"
        )


def run_live_engine(
    *,
    run_dir: Path,
    rendered_prompt_path: Path,
    subject_id: str,
    facet: str,
    phase: str,
    engine: str,
    command_timeout_seconds: float | None,
) -> dict[str, Any]:
    ensure_file(LLM_RUNNER_BRIDGE_PATH, label="llm_runner bridge")
    ensure_file(LLM_RUNNER_PATH, label="llm_runner library")
    tmp_dir = run_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw_engine_output_path = run_dir / "raw-engine-output.txt"
    stamped_output_path = run_dir / "stamped-engine-output.txt"

    invoke_llm_runner_bridge(
        [
            "bash",
            str(LLM_RUNNER_BRIDGE_PATH),
            "run",
            "--prompt-file",
            str(rendered_prompt_path),
            "--tmp-dir",
            str(tmp_dir),
            "--output-file",
            str(raw_engine_output_path),
            "--phase",
            phase,
            "--engine",
            engine,
            "--tool-name",
            DRIVER_NAME,
        ],
        label="live engine run",
        timeout_seconds=command_timeout_seconds,
    )
    raw_engine_output = read_text_file(raw_engine_output_path, label="raw engine output")

    shutil.copyfile(raw_engine_output_path, stamped_output_path)
    invoke_llm_runner_bridge(
        [
            "bash",
            str(LLM_RUNNER_BRIDGE_PATH),
            "stamp",
            "--file",
            str(stamped_output_path),
            "--place",
            subject_id,
            "--facet",
            facet,
            "--phase",
            phase,
            "--engine",
            engine,
        ],
        label="llm_runner output stamp",
        timeout_seconds=command_timeout_seconds,
    )
    stamped_output_text = read_text_file(stamped_output_path, label="stamped engine output")
    stamp_footer = parse_stamp_footer(stamped_output_text)

    return {
        "raw_engine_output_path": str(raw_engine_output_path),
        "raw_engine_output": raw_engine_output,
        "stamped_output_path": str(stamped_output_path),
        "stamp_footer": stamp_footer,
    }


def build_candidate_batch(
    *,
    args: argparse.Namespace,
    created_at: str,
    run_id: str,
    run_dir: Path,
    gather_inputs: dict[str, Any],
    prompt_body: str,
    rendered_prompt: str,
    rendered_prompt_path: Path,
    source_wrapping_blocks: list[dict[str, Any]],
    live_result: dict[str, Any] | None,
    prior_state: dict[str, Any] | None,
    feedback_plan: dict[str, Any] | None,
    next_action: dict[str, Any] | None,
    debug_rendered_prompt: bool,
) -> dict[str, Any]:
    subject = gather_inputs["subject"]
    pack = gather_inputs["domain_pack"]
    bundle = gather_inputs["bundle"]
    facet = gather_inputs["facet"]
    phase = gather_inputs["phase"]
    mode = args.mode.replace("-", "_")
    iteration_mode = "prior_state" if args.use_prior_state else "one_shot"
    engine_invoked = live_result is not None
    candidate_type_hint = candidate_type_hint_for_facet(facet)
    candidates: list[dict[str, Any]] = []
    if live_result is not None:
        candidates.append(
            {
                "candidate_id": "cand:0001",
                "candidate_type": "raw_candidate_text",
                "review_status": "unverified",
                "persistence_status": "workspace_run_only",
                "origin": "llm_proposed",
                "text": live_result["raw_engine_output"],
            }
        )

    batch = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "mode": mode,
        "iteration_mode": iteration_mode,
        "cycle_depth": args.cycle_depth,
        "previous_run_ids": list(prior_state["previous_run_ids"])
        if prior_state
        else list(args.previous_run_id),
        "phase": phase,
        "engine": {
            "requested_engine": args.engine,
            "resolved_engine": args.engine if engine_invoked else None,
            "invoked": engine_invoked,
            "engine_present": engine_invoked,
            "runner_path": str(LLM_RUNNER_PATH),
            "bridge_path": str(LLM_RUNNER_BRIDGE_PATH),
        },
        "subject": {
            "subject_id": subject["subject_id"],
            "display_name": subject["display_name"],
            "domain_pack": subject["domain_pack"],
            "scope_statement": subject["scope_statement"],
            "enabled_facets": subject["enabled_facets"],
            "query_families": subject["query_families"],
            "manifest_path": gather_inputs["runtime"]["subject_manifest_path"],
            "workspace_root": gather_inputs["runtime"]["workspace_root"],
            "resolution_source": gather_inputs["runtime"]["resolution_source"],
        },
        "domain_pack": {
            "pack_id": pack["pack_id"],
            "schema_version": pack["schema_version"],
            "display_name": pack["display_name"],
            "status": pack["status"],
            "path": str(REPO_ROOT / "config" / "domain_packs" / f"{pack['pack_id']}.json"),
            "enabled_facets": pack["enabled_facets"],
            "selected_facet": facet,
            "prompt_bundle_key": bundle["bundle_key"],
            "prompt_bundle_id": bundle["bundle_id"],
        },
        "facet": {
            "name": facet,
            "phase": phase,
            "candidate_type_hint": candidate_type_hint,
        },
        "prompt_bundle": {
            "bundle_key": bundle["bundle_key"],
            "bundle_id": bundle["bundle_id"],
            "template_ids": bundle["template_ids"],
            "template_files": bundle["template_files"],
            "selected_template_id": gather_inputs["selected_template_id"],
            "selected_template_file": gather_inputs["selected_template_file"],
            "wrapper_template_id": bundle["source_text_wrapper_template_id"],
        },
        "prompt": {
            "rendered_prompt_path": str(rendered_prompt_path),
            "rendered_prompt_hash": sha256_text(rendered_prompt),
        },
        "source_text_wrapping": {
            "wrapper_template_id": gather_inputs["wrapper_template"].template_id,
            "begin_delimiter": gather_inputs["wrapper_template"].begin_delimiter,
            "end_delimiter": gather_inputs["wrapper_template"].end_delimiter,
            "source_block_count": len(source_wrapping_blocks),
            "blocks": source_wrapping_blocks,
        },
        "provenance": {
            "driver_name": DRIVER_NAME,
            "driver_version": DRIVER_VERSION,
            "command": list(sys.argv[1:]),
            "llm_runner_path": str(LLM_RUNNER_PATH),
            "llm_runner_bridge_path": str(LLM_RUNNER_BRIDGE_PATH),
            "engine_name": args.engine,
            "engine_invoked": engine_invoked,
            "engine_present": engine_invoked,
            "timestamp": created_at,
            "network_access_attempted": False,
            "prior_state_enabled": args.use_prior_state,
            "prior_state_hash": prior_state["context_hash"] if prior_state else None,
            "feedback_plan_enabled": feedback_plan is not None,
            "feedback_plan_hash": feedback_plan["hash"] if feedback_plan else None,
            "next_action_id": next_action["action_id"] if next_action is not None else None,
            "scoring_policy_id": next_action["scoring_policy_id"]
            if next_action is not None
            else None,
            "cycle_depth": args.cycle_depth,
            "stamped_output_path": live_result["stamped_output_path"]
            if live_result is not None
            else None,
            "stamped_output_footer": live_result["stamp_footer"]
            if live_result is not None
            else None,
        },
        "candidates": candidates,
        "raw_engine_output": live_result["raw_engine_output"] if live_result is not None else None,
        "engine_output_ref": live_result["raw_engine_output_path"]
        if live_result is not None
        else None,
        "validation": {
            "validator": VALIDATOR_NAME,
            "status": "pass",
            "errors": [],
        },
    }
    if prior_state is not None:
        batch["prior_state"] = prior_state
    if feedback_plan is not None and next_action is not None:
        batch["feedback_plan"] = {
            "schema_version": feedback_plan["payload"]["schema_version"],
            "plan_path": str(feedback_plan["path"]),
            "plan_hash": feedback_plan["hash"],
            "next_action_id": next_action["action_id"],
            "plan_selected_facet": next_action["selected_facet"],
            "applied_facet": facet,
            "selected_prompt_bundle_id": next_action["selected_prompt_bundle_id"],
            "applied_prompt_bundle_id": bundle["bundle_id"],
            "selected_object_ref": next_action.get("selected_object_ref"),
            "selected_lead_kind": next_action.get("selected_lead_kind"),
            "selection_score": next_action["selection_score"],
            "scoring_policy_id": next_action["scoring_policy_id"],
            "rationale": next_action["rationale"],
            "use_prior_state": next_action["use_prior_state"],
            "cycle_depth": next_action["cycle_depth"],
            "previous_run_ids_considered": list(next_action["previous_run_ids_considered"]),
            "next_action": next_action,
        }
    if debug_rendered_prompt:
        batch["prompt"]["rendered_prompt"] = rendered_prompt
    return batch


def render_summary_text(
    *,
    batch_path: Path,
    rendered_prompt_path: Path,
    batch: dict[str, Any],
    live_result: dict[str, Any] | None,
) -> str:
    lines = [
        f"mode={batch['mode']}",
        f"run_id={batch['run_id']}",
        f"subject_id={batch['subject']['subject_id']}",
        f"subject_manifest_path={batch['subject']['manifest_path']}",
        f"workspace_root={batch['subject']['workspace_root']}",
        f"domain_pack={batch['domain_pack']['pack_id']}",
        f"facet={batch['facet']['name']}",
        f"phase={batch['phase']}",
        f"iteration_mode={batch['iteration_mode']}",
        f"cycle_depth={batch['cycle_depth']}",
        f"prompt_bundle_id={batch['prompt_bundle']['bundle_id']}",
        f"wrapper_template_id={batch['source_text_wrapping']['wrapper_template_id']}",
        f"rendered_prompt_path={rendered_prompt_path}",
        f"candidate_batch_path={batch_path}",
    ]
    if isinstance(batch.get("prior_state"), dict):
        lines.append(f"prior_state_hash={batch['prior_state']['context_hash']}")
        lines.append(
            "prior_state_counts="
            + json.dumps(batch["prior_state"]["record_counts"], ensure_ascii=False, sort_keys=True)
        )
    if isinstance(batch.get("feedback_plan"), dict):
        lines.append(f"feedback_plan_hash={batch['feedback_plan']['plan_hash']}")
        lines.append(f"next_action_id={batch['feedback_plan']['next_action_id']}")
    if live_result is not None:
        lines.append(f"raw_engine_output_path={live_result['raw_engine_output_path']}")
        lines.append(f"stamped_output_path={live_result['stamped_output_path']}")
    return "\n".join(lines) + "\n"


def render_summary_json(
    *,
    batch_path: Path,
    rendered_prompt_path: Path,
    batch: dict[str, Any],
    live_result: dict[str, Any] | None,
    rendered_prompt_sha256: str,
    candidate_batch_sha256: str,
) -> str:
    payload = {
        "run_id": batch["run_id"],
        "mode": batch["mode"],
        "iteration_mode": batch["iteration_mode"],
        "cycle_depth": batch["cycle_depth"],
        "subject_id": batch["subject"]["subject_id"],
        "domain_pack": batch["domain_pack"]["pack_id"],
        "facet": batch["facet"]["name"],
        "phase": batch["phase"],
        "prompt_bundle_id": batch["prompt_bundle"]["bundle_id"],
        "rendered_prompt_sha256": rendered_prompt_sha256,
        "rendered_prompt_path": str(rendered_prompt_path),
        "candidate_batch_path": str(batch_path),
        "candidate_batch_sha256": candidate_batch_sha256,
        "prior_state": batch.get("prior_state"),
        "feedback_plan": batch.get("feedback_plan"),
        "raw_engine_output_path": live_result["raw_engine_output_path"]
        if live_result is not None
        else None,
        "stamped_output_path": live_result["stamped_output_path"]
        if live_result is not None
        else None,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    args = parse_args()
    try:
        created_at = (
            require_rfc3339_utc(args.created_at, field_name="--created-at")
            if args.created_at
            else utc_now_text()
        )
        command_timeout_seconds = resolve_command_timeout_seconds(args)
        runtime = resolve_runtime_inputs(args)
        feedback_plan = load_feedback_plan(args.feedback_plan) if args.feedback_plan else None
        next_action = apply_feedback_plan_defaults(
            args,
            subject=runtime["subject"],
            feedback_plan=feedback_plan,
        )
        if args.cycle_depth is None:
            args.cycle_depth = DEFAULT_CYCLE_DEPTH
        validate_iteration_args(args)
        gather_inputs = resolve_gather_inputs(
            runtime=runtime,
            facet=args.facet.strip(),
            phase=args.phase,
        )
        if next_action is not None:
            selected_bundle_id = next_action.get("selected_prompt_bundle_id")
            if (
                isinstance(selected_bundle_id, str)
                and selected_bundle_id != gather_inputs["bundle"]["bundle_id"]
            ):
                raise GatherDriverError(
                    "feedback plan selected_prompt_bundle_id does not match the resolved facet bundle"
                )
        run_id = args.run_id or build_run_id(
            gather_inputs["subject"]["subject_id"],
            gather_inputs["facet"],
            gather_inputs["phase"],
            created_at,
        )

        workspace_root = resolve_subject_runtime.resolve_workspace_path(args.workspace)
        run_dir = workspace_root / RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        prompt_body = read_text_file(gather_inputs["selected_template_path"], label="prompt file")
        prior_state = resolve_prior_state_context(
            args,
            subject_id=gather_inputs["subject"]["subject_id"],
        )
        source_wrapping_blocks, rendered_blocks = resolve_source_text_blocks(
            args.source_text_file,
            template=gather_inputs["wrapper_template"],
        )
        rendered_prompt = render_prompt_text(
            prompt_body=prompt_body,
            subject=gather_inputs["subject"],
            facet=gather_inputs["facet"],
            phase=gather_inputs["phase"],
            bundle=gather_inputs["bundle"],
            wrapped_blocks=rendered_blocks,
            next_action=next_action,
            prior_state=prior_state,
            template=gather_inputs["wrapper_template"],
        )
        parsed_blocks = parse_wrapped_blocks(
            rendered_prompt, template=gather_inputs["wrapper_template"]
        )
        rendered_source_blocks = [
            block for block in parsed_blocks if block.source_ref.startswith("file:")
        ]
        if len(rendered_source_blocks) != len(source_wrapping_blocks):
            raise GatherDriverError("wrapped source block count mismatch after prompt rendering")

        rendered_prompt_path = run_dir / "rendered-prompt.txt"
        write_text(rendered_prompt_path, rendered_prompt, sync=False)

        live_result: dict[str, Any] | None = None
        should_call_llm = True
        if isinstance(next_action, dict):
            should_call_llm = bool(next_action.get("should_call_llm", True))
        if args.mode == "live" and should_call_llm:
            live_result = run_live_engine(
                run_dir=run_dir,
                rendered_prompt_path=rendered_prompt_path,
                subject_id=gather_inputs["subject"]["subject_id"],
                facet=gather_inputs["facet"],
                phase=gather_inputs["phase"],
                engine=args.engine,
                command_timeout_seconds=command_timeout_seconds,
            )

        batch = build_candidate_batch(
            args=args,
            created_at=created_at,
            run_id=run_id,
            run_dir=run_dir,
            gather_inputs=gather_inputs,
            prompt_body=prompt_body,
            rendered_prompt=rendered_prompt,
            rendered_prompt_path=rendered_prompt_path,
            source_wrapping_blocks=source_wrapping_blocks,
            live_result=live_result,
            prior_state=prior_state,
            feedback_plan=feedback_plan,
            next_action=next_action,
            debug_rendered_prompt=args.debug_rendered_prompt,
        )
        batch_path = run_dir / "gather-candidate-batch.json"
        if args.debug_rendered_prompt:
            findings = scan_text(
                rendered_prompt, rel_path=str(rendered_prompt_path), profile="public_bundle"
            )
            if findings:
                sample = "; ".join(
                    f"{finding['code']}@{finding['path']}" for finding in findings[:5]
                )
                raise GatherDriverError(f"debug rendered prompt failed leak scan: {sample}")
        write_json(batch_path, batch, sync=False)

        validation_result, validation_exit_code = validate_gather_candidate_batch(batch_path)
        if validation_exit_code != 0:
            messages = "; ".join(
                f"{error['code']}: {error['message']}" for error in validation_result["errors"]
            )
            raise GatherDriverError(f"emitted candidate batch failed validation: {messages}")

        if args.mode != "dry-run":
            sync_paths([rendered_prompt_path, batch_path])

        if args.format == "json":
            candidate_batch_sha256 = hash_file(batch_path)
            rendered_prompt_sha256 = sha256_text(rendered_prompt)
            sys.stdout.write(
                render_summary_json(
                    batch_path=batch_path,
                    rendered_prompt_path=rendered_prompt_path,
                    batch=batch,
                    rendered_prompt_sha256=rendered_prompt_sha256,
                    candidate_batch_sha256=candidate_batch_sha256,
                    live_result=live_result,
                )
            )
        else:
            sys.stdout.write(
                render_summary_text(
                    batch_path=batch_path,
                    rendered_prompt_path=rendered_prompt_path,
                    batch=batch,
                    live_result=live_result,
                )
            )
        return 0
    except (
        GatherDriverError,
        resolve_gather_domain_pack.GatherDomainPackError,
        resolve_subject_runtime.ResolutionError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
