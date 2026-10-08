"""Shared leak-scanner helpers for generated artifacts and support bundles."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from tools.common.search_leak_policy import contains_private_path, find_secret_marker_spans

ALLOWLIST_SCHEMA_VERSION = "leak-scan-allowlist.v2"
REPORT_SCHEMA_VERSION = "leak-scan-report.v1"
FINDING_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REVIEWER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{1,127}$")
BINARY_SUFFIXES = {
    ".7z",
    ".avi",
    ".avif",
    ".bin",
    ".bmp",
    ".class",
    ".deb",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".eot",
    ".exe",
    ".gif",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".odt",
    ".ogg",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tif",
    ".tiff",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xz",
    ".zip",
}
BINARY_MIME_PREFIXES = ("audio/", "font/", "image/", "video/")
TEXT_MIME_TYPES = {"image/svg+xml"}
TEXT_SNIFF_BYTES = 8192
RUNTIME_LOG_PATH_RE = re.compile(r"(?i)(?:^|/)(?:logs?|runtime-logs?|index-actions\.log)(?:/|$)")
PROMPT_OUTPUT_BODY_RE = re.compile(r"(?i)\b(prompt_output|raw_prompt_output|01a_prompt|01r_prompt|prompt_bundle_id)\b")
RAW_PAYLOAD_BODY_RE = re.compile(r"(?i)\b(full_extracted_text|raw_payload|raw_text|full_text)\b")
PRIVATE_NOTE_BODY_RE = re.compile(r"(?i)\b(internal_note|private_note|operator_note|note_text)\b")
RESTRICTED_EVIDENCE_BODY_RE = re.compile(r"(?i)\b(operator_excerpt_text|public_excerpt_text)\b")

PROFILES: dict[str, dict[str, bool]] = {
    "public_bundle": {
        "scan_secret_markers": True,
        "scan_private_path_markers": True,
        "scan_runtime_log_paths": True,
        "scan_prompt_output_markers": True,
        "scan_raw_payload_markers": True,
        "scan_private_note_markers": True,
        "scan_restricted_evidence_markers": True,
    },
    "support_bundle": {
        "scan_secret_markers": True,
        "scan_private_path_markers": True,
        "scan_runtime_log_paths": True,
        "scan_prompt_output_markers": True,
        "scan_raw_payload_markers": True,
        "scan_private_note_markers": True,
        "scan_restricted_evidence_markers": True,
    },
}


class LeakScannerError(RuntimeError):
    """Raised when scanner inputs are malformed or unreadable."""


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """Return a stable identifier for the complete finding context."""
    identity = {
        key: finding.get(key)
        for key in (
            "path",
            "code",
            "message",
            "line",
            "column",
            "excerpt",
            "context_fingerprint",
        )
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_text_file(path: Path) -> bool:
    """Return whether a file is safe to pass through the line-oriented scanner."""
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    mime_type, _ = mimetypes.guess_type(path.name, strict=False)
    if (
        mime_type is not None
        and mime_type.startswith(BINARY_MIME_PREFIXES)
        and mime_type not in TEXT_MIME_TYPES
    ):
        return False
    try:
        with path.open("rb") as handle:
            sample = handle.read(TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def load_allowlist(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"schema_version": ALLOWLIST_SCHEMA_VERSION, "entries": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LeakScannerError(f"could not read allowlist: {path}") from exc
    if not isinstance(payload, dict):
        raise LeakScannerError("allowlist must be a JSON object")
    return payload


def validate_allowlist(payload: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if payload.get("schema_version") != ALLOWLIST_SCHEMA_VERSION:
        errors.append({"code": "INVALID_SCHEMA_VERSION", "message": f"schema_version must equal {ALLOWLIST_SCHEMA_VERSION}"})
    entries = payload.get("entries")
    if not isinstance(entries, list):
        errors.append({"code": "INVALID_ENTRIES", "message": "entries must be an array"})
        return errors
    required = ("entry_id", "finding_fingerprint", "reason", "approved_by", "expires_at")
    allowed = set(required)
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append({"code": "INVALID_ENTRY", "message": f"{label} must be an object"})
            continue
        for key in sorted(set(entry) - allowed):
            errors.append({"code": "UNKNOWN_ENTRY_FIELD", "message": f"{label}.{key} is not supported"})
        for key in required:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append({"code": "INVALID_ENTRY_FIELD", "message": f"{label}.{key} must be a non-blank string"})
        fingerprint = entry.get("finding_fingerprint")
        if isinstance(fingerprint, str) and not FINDING_FINGERPRINT_RE.fullmatch(fingerprint):
            errors.append(
                {
                    "code": "INVALID_FINDING_FINGERPRINT",
                    "message": f"{label}.finding_fingerprint must be a sha256 fingerprint",
                }
            )
        expires_at = entry.get("expires_at")
        if isinstance(expires_at, str):
            try:
                parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                parsed_expiry = None
            if parsed_expiry is None or parsed_expiry.tzinfo is None:
                errors.append(
                    {
                        "code": "INVALID_EXPIRY",
                        "message": f"{label}.expires_at must be an ISO-8601 timestamp with timezone",
                    }
                )
        approved_by = entry.get("approved_by")
        if isinstance(approved_by, str) and not REVIEWER_ID_RE.fullmatch(approved_by):
            errors.append(
                {
                    "code": "INVALID_REVIEWER_ID",
                    "message": f"{label}.approved_by must be a bounded reviewer identity token",
                }
            )
        entry_id = entry.get("entry_id")
        if isinstance(entry_id, str) and entry_id.strip():
            if entry_id in seen_ids:
                errors.append({"code": "DUPLICATE_ENTRY_ID", "message": f"duplicate allowlist entry_id: {entry_id}"})
            seen_ids.add(entry_id)
    return errors


def _line_number_for_offset(body: str, offset: int) -> int:
    return body.count("\n", 0, offset) + 1


def _column_number_for_offset(body: str, offset: int) -> int:
    return offset - body.rfind("\n", 0, offset)


def _finding(
    *,
    path: str,
    code: str,
    message: str,
    line: int | None = None,
    column: int | None = None,
    excerpt: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "path": path,
        "code": code,
        "message": message,
    }
    if line is not None:
        finding["line"] = line
    if column is not None:
        finding["column"] = column
    if excerpt is not None:
        finding["excerpt"] = excerpt
    if context is not None:
        finding["context_fingerprint"] = (
            "sha256:" + hashlib.sha256(context.encode("utf-8")).hexdigest()
        )
    finding["finding_fingerprint"] = finding_fingerprint(finding)
    return finding


def _regex_findings(body: str, *, rel_path: str, pattern: re.Pattern[str], code: str, message: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for match in pattern.finditer(body):
        findings.append(
            _finding(
                path=rel_path,
                code=code,
                message=message,
                line=_line_number_for_offset(body, match.start()),
                column=_column_number_for_offset(body, match.start()),
                excerpt=match.group(0),
                context=body,
            )
        )
    return findings


def _regex_findings_for_line(
    line: str,
    *,
    rel_path: str,
    pattern: re.Pattern[str],
    code: str,
    message: str,
    line_number: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for match in pattern.finditer(line):
        findings.append(
            _finding(
                path=rel_path,
                code=code,
                message=message,
                line=line_number,
                column=match.start() + 1,
                excerpt=match.group(0),
                context=line,
            )
        )
    return findings


def _scan_line(line: str, *, rel_path: str, profile: str, line_number: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    profile_config = PROFILES[profile]
    if profile_config["scan_secret_markers"]:
        findings.extend(
            _finding(
                path=rel_path,
                code="SECRET_MARKER",
                message="secret-looking token remains in scanned output",
                line=line_number,
                column=start + 1,
                excerpt=line[start:end],
                context=line,
            )
            for start, end in find_secret_marker_spans(line)
        )
    if profile_config["scan_private_path_markers"] and contains_private_path(line):
        findings.extend(
            _regex_findings_for_line(
                line,
                rel_path=rel_path,
                pattern=re.compile(r"(?i)(?:^|[\s'\"(])(?:/home/|/Users/|/tmp/|file://|~/|[A-Za-z]:\\\\)[^\s'\"()]+"),
                code="PRIVATE_PATH",
                message="private absolute path remains in scanned output",
                line_number=line_number,
            )
        )
    if profile_config["scan_prompt_output_markers"]:
        findings.extend(
            _regex_findings_for_line(
                line,
                rel_path=rel_path,
                pattern=PROMPT_OUTPUT_BODY_RE,
                code="PROMPT_OUTPUT_MARKER",
                message="prompt-output marker remains in scanned output",
                line_number=line_number,
            )
        )
    if profile_config["scan_raw_payload_markers"]:
        findings.extend(
            _regex_findings_for_line(
                line,
                rel_path=rel_path,
                pattern=RAW_PAYLOAD_BODY_RE,
                code="RAW_PAYLOAD_MARKER",
                message="raw payload or full-text marker remains in scanned output",
                line_number=line_number,
            )
        )
    if profile_config["scan_private_note_markers"]:
        findings.extend(
            _regex_findings_for_line(
                line,
                rel_path=rel_path,
                pattern=PRIVATE_NOTE_BODY_RE,
                code="PRIVATE_NOTE_MARKER",
                message="private-note marker remains in scanned output",
                line_number=line_number,
            )
        )
    if profile_config["scan_restricted_evidence_markers"]:
        findings.extend(
            _regex_findings_for_line(
                line,
                rel_path=rel_path,
                pattern=RESTRICTED_EVIDENCE_BODY_RE,
                code="RESTRICTED_EVIDENCE_MARKER",
                message="restricted evidence marker remains in scanned output",
                line_number=line_number,
            )
        )
    return findings


def scan_text(body: str, *, rel_path: str, profile: str) -> list[dict[str, Any]]:
    if profile not in PROFILES:
        raise LeakScannerError(f"unknown leak scanner profile: {profile}")
    findings: list[dict[str, Any]] = []
    profile_config = PROFILES[profile]
    if profile_config["scan_secret_markers"]:
        findings.extend(
            _finding(
                path=rel_path,
                code="SECRET_MARKER",
                message="secret-looking token remains in scanned output",
                line=_line_number_for_offset(body, start),
                column=_column_number_for_offset(body, start),
                excerpt=body[start:end],
                context=body,
            )
            for start, end in find_secret_marker_spans(body)
        )
    if profile_config["scan_private_path_markers"] and contains_private_path(body):
        findings.extend(
            _regex_findings(
                body,
                rel_path=rel_path,
                pattern=re.compile(r"(?i)(?:^|[\s'\"(])(?:/home/|/Users/|/tmp/|file://|~/|[A-Za-z]:\\\\)[^\s'\"()]+"),
                code="PRIVATE_PATH",
                message="private absolute path remains in scanned output",
            )
        )
    profile_config = PROFILES[profile]
    if profile_config["scan_prompt_output_markers"]:
        findings.extend(
            _regex_findings(
                body,
                rel_path=rel_path,
                pattern=PROMPT_OUTPUT_BODY_RE,
                code="PROMPT_OUTPUT_MARKER",
                message="prompt-output marker remains in scanned output",
            )
        )
    if profile_config["scan_raw_payload_markers"]:
        findings.extend(
            _regex_findings(
                body,
                rel_path=rel_path,
                pattern=RAW_PAYLOAD_BODY_RE,
                code="RAW_PAYLOAD_MARKER",
                message="raw payload or full-text marker remains in scanned output",
            )
        )
    if profile_config["scan_private_note_markers"]:
        findings.extend(
            _regex_findings(
                body,
                rel_path=rel_path,
                pattern=PRIVATE_NOTE_BODY_RE,
                code="PRIVATE_NOTE_MARKER",
                message="private-note marker remains in scanned output",
            )
        )
    if profile_config["scan_restricted_evidence_markers"]:
        findings.extend(
            _regex_findings(
                body,
                rel_path=rel_path,
                pattern=RESTRICTED_EVIDENCE_BODY_RE,
                code="RESTRICTED_EVIDENCE_MARKER",
                message="restricted evidence marker remains in scanned output",
            )
        )
    return findings


def _entry_matches(finding: dict[str, Any], entry: dict[str, Any]) -> bool:
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None or expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        return False
    return (
        isinstance(finding.get("finding_fingerprint"), str)
        and finding["finding_fingerprint"] == entry.get("finding_fingerprint")
    )


def apply_allowlist(
    findings: list[dict[str, Any]],
    allowlist_payload: dict[str, Any],
    *,
    include_allowlist_audit: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries = allowlist_payload.get("entries", [])
    active: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for finding in findings:
        matched_entry = None
        for entry in entries:
            if isinstance(entry, dict) and _entry_matches(finding, entry):
                matched_entry = entry
                break
        if matched_entry is None:
            active.append(finding)
            continue
        suppressed_finding = {
            **finding,
            "allowlist_entry_id": matched_entry["entry_id"],
        }
        if include_allowlist_audit:
            suppressed_finding.update(
                {
                    "allowlist_reason": matched_entry["reason"],
                    "allowlist_approved_by": matched_entry["approved_by"],
                }
            )
        suppressed.append(suppressed_finding)
    return active, suppressed


def scan_directory(
    root: Path,
    *,
    profile: str,
    allowlist_payload: dict[str, Any] | None = None,
    exclude_globs: tuple[str, ...] | list[str] | None = None,
    include_allowlist_audit: bool = False,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise LeakScannerError(f"unknown leak scanner profile: {profile}")
    if not root.exists() or not root.is_dir():
        raise LeakScannerError(f"scan root is not a directory: {root}")
    normalized_allowlist = {"schema_version": ALLOWLIST_SCHEMA_VERSION, "entries": []} if allowlist_payload is None else allowlist_payload
    allowlist_errors = validate_allowlist(normalized_allowlist)
    if allowlist_errors:
        raise LeakScannerError("; ".join(item["message"] for item in allowlist_errors[:5]))

    raw_findings: list[dict[str, Any]] = []
    files_scanned = 0
    normalized_exclude_globs = tuple(exclude_globs or ())
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root).as_posix()
        if normalized_exclude_globs and any(
            fnmatch(rel_path, pattern) or fnmatch(path.name, pattern)
            for pattern in normalized_exclude_globs
        ):
            continue
        files_scanned += 1
        if PROFILES[profile]["scan_runtime_log_paths"] and RUNTIME_LOG_PATH_RE.search(rel_path):
            raw_findings.append(
                _finding(
                    path=rel_path,
                    code="RUNTIME_LOG_PATH",
                    message="runtime log path is not allowed in this profile",
                )
            )
        if not _is_text_file(path):
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    raw_findings.extend(
                        _scan_line(line, rel_path=rel_path, profile=profile, line_number=line_number)
                    )
        except (OSError, UnicodeDecodeError):
            continue

    findings, suppressed = apply_allowlist(
        raw_findings,
        normalized_allowlist,
        include_allowlist_audit=include_allowlist_audit,
    )
    allowlist_entries = normalized_allowlist.get("entries", [])
    allowlist_audit: dict[str, Any] = {
        "schema_version": normalized_allowlist.get("schema_version"),
        "entry_ids": [
            entry["entry_id"]
            for entry in allowlist_entries
            if isinstance(entry, dict) and isinstance(entry.get("entry_id"), str)
        ],
    }
    if include_allowlist_audit:
        allowlist_audit["entries"] = allowlist_entries
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "profile": profile,
        "status": "pass" if not findings else "fail",
        "counts": {
            "files_scanned": files_scanned,
            "findings": len(findings),
            "suppressed_findings": len(suppressed),
            "allowlist_entries": len(normalized_allowlist.get("entries", [])),
        },
        "findings": findings,
        "suppressed_findings": suppressed,
        "allowlist_audit": allowlist_audit,
    }
