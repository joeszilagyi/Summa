"""Shared leak-scanner helpers for generated artifacts and support bundles."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from tools.common.search_leak_policy import contains_private_path, contains_secret_marker


ALLOWLIST_SCHEMA_VERSION = "leak-scan-allowlist.v1"
REPORT_SCHEMA_VERSION = "leak-scan-report.v1"
TEXT_SUFFIXES = {".css", ".html", ".json", ".log", ".md", ".txt"}
RUNTIME_LOG_PATH_RE = re.compile(r"(?i)(?:^|/)(?:logs?|runtime-logs?|index-actions\.log)(?:/|$)")
PROMPT_OUTPUT_BODY_RE = re.compile(r"(?i)\b(prompt_output|raw_prompt_output|01a_prompt|01r_prompt|prompt_bundle_id)\b")
RAW_PAYLOAD_BODY_RE = re.compile(r"(?i)\b(full_extracted_text|raw_payload|raw_text|full_text)\b")
PRIVATE_NOTE_BODY_RE = re.compile(r"(?i)\b(internal_note|private_note|operator_note|note_text)\b")
RESTRICTED_EVIDENCE_BODY_RE = re.compile(r"(?i)\b(operator_excerpt_text|public_excerpt_text)\b")

PROFILES: dict[str, dict[str, bool]] = {
    "public_bundle": {
        "scan_runtime_log_paths": True,
        "scan_prompt_output_markers": True,
        "scan_raw_payload_markers": True,
        "scan_private_note_markers": True,
        "scan_restricted_evidence_markers": True,
    },
    "support_bundle": {
        "scan_runtime_log_paths": False,
        "scan_prompt_output_markers": False,
        "scan_raw_payload_markers": False,
        "scan_private_note_markers": False,
        "scan_restricted_evidence_markers": False,
    },
}


class LeakScannerError(RuntimeError):
    """Raised when scanner inputs are malformed or unreadable."""


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
    required = ("entry_id", "finding_code", "path_glob", "match_substring", "reason", "approved_by")
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, dict):
            errors.append({"code": "INVALID_ENTRY", "message": f"{label} must be an object"})
            continue
        for key in required:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append({"code": "INVALID_ENTRY_FIELD", "message": f"{label}.{key} must be a non-blank string"})
        entry_id = entry.get("entry_id")
        if isinstance(entry_id, str) and entry_id.strip():
            if entry_id in seen_ids:
                errors.append({"code": "DUPLICATE_ENTRY_ID", "message": f"duplicate allowlist entry_id: {entry_id}"})
            seen_ids.add(entry_id)
    return errors


def _line_number_for_offset(body: str, offset: int) -> int:
    return body.count("\n", 0, offset) + 1


def _finding(*, path: str, code: str, message: str, line: int | None = None, excerpt: str | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "path": path,
        "code": code,
        "message": message,
    }
    if line is not None:
        finding["line"] = line
    if excerpt is not None:
        finding["excerpt"] = excerpt
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
                excerpt=match.group(0),
            )
        )
    return findings


def scan_text(body: str, *, rel_path: str, profile: str) -> list[dict[str, Any]]:
    if profile not in PROFILES:
        raise LeakScannerError(f"unknown leak scanner profile: {profile}")
    findings: list[dict[str, Any]] = []
    if contains_secret_marker(body):
        findings.extend(
            _regex_findings(
                body,
                rel_path=rel_path,
                pattern=re.compile(r"(?i)(authorization:\s*bearer|api[_-]?key\s*=|secret\s*=|token\s*=|private key)"),
                code="SECRET_MARKER",
                message="secret-looking token remains in scanned output",
            )
        )
    if contains_private_path(body):
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
    excerpt = finding.get("excerpt")
    if not isinstance(excerpt, str):
        return False
    return (
        finding.get("code") == entry.get("finding_code")
        and isinstance(finding.get("path"), str)
        and fnmatch(finding["path"], entry["path_glob"])
        and entry["match_substring"] in excerpt
    )


def apply_allowlist(
    findings: list[dict[str, Any]],
    allowlist_payload: dict[str, Any],
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
        suppressed.append(
            {
                **finding,
                "allowlist_entry_id": matched_entry["entry_id"],
                "allowlist_reason": matched_entry["reason"],
                "allowlist_approved_by": matched_entry["approved_by"],
            }
        )
    return active, suppressed


def scan_directory(
    root: Path,
    *,
    profile: str,
    allowlist_payload: dict[str, Any] | None = None,
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
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root).as_posix()
        if PROFILES[profile]["scan_runtime_log_paths"] and RUNTIME_LOG_PATH_RE.search(rel_path):
            raw_findings.append(
                _finding(
                    path=rel_path,
                    code="RUNTIME_LOG_PATH",
                    message="runtime log path is not allowed in this profile",
                )
            )
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        raw_findings.extend(scan_text(body, rel_path=rel_path, profile=profile))

    findings, suppressed = apply_allowlist(raw_findings, normalized_allowlist)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "profile": profile,
        "status": "pass" if not findings else "fail",
        "counts": {
            "files_scanned": sum(1 for path in root.rglob("*") if path.is_file()),
            "findings": len(findings),
            "suppressed_findings": len(suppressed),
            "allowlist_entries": len(normalized_allowlist.get("entries", [])),
        },
        "findings": findings,
        "suppressed_findings": suppressed,
        "allowlist_audit": {
            "schema_version": normalized_allowlist.get("schema_version"),
            "entries": normalized_allowlist.get("entries", []),
        },
    }
