from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

import validate_topic_workspace_registry  # noqa: E402


def write_manifest(workspace_root: Path, *, subject_id: str) -> Path:
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": subject_id,
                "display_name": subject_id.replace(".", " ").title(),
                "domain_pack": "general.v1",
                "scope_statement": "Synthetic validator fixture.",
                "languages": ["en"],
                "aliases": ["Synthetic fixture"],
                "disambiguation_terms": ["validator"],
                "excluded_senses": ["non-fixture"],
                "enabled_facets": ["sources"],
                "query_families": ["web_search"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def write_registry(tmp_path: Path, workspace: dict[str, object]) -> Path:
    registry_path = tmp_path / "topic_workspaces.local.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "topic-workspace-registry.v1",
                "workspaces": [workspace],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return registry_path


def workspace_record(
    *,
    workspace_root: Path,
    manifest_path: Path,
    scheduler_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "workspace_id": "validator_workspace",
        "topic_label": "Validator Workspace",
        "workspace_root": str(workspace_root),
        "domain_pack": "general.v1",
        "lifecycle_state": "active",
        "schedule_posture": "scheduled",
        "workspace_policy_class": "private_local",
        "default_subject_manifest": str(manifest_path),
    }
    if scheduler_policy is not None:
        record["scheduler_policy"] = scheduler_policy
    return record


def test_validator_accepts_scheduler_policy_fields(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            scheduler_policy={
                "run_budget": {"max_attempts": 3, "max_runtime_seconds": 900},
                "retry_policy": {"max_retryable_failures": 2, "backoff_seconds": 600},
                "failure_state": {
                    "status": "retryable",
                    "attempt_count": 1,
                    "last_failure_at": "2026-01-01T00:00:00Z",
                    "next_retry_at": "2026-01-01T00:10:00Z",
                    "last_failure_reason": "fixture timeout",
                },
            },
        ),
    )

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(registry_path)

    assert exit_code == validate_topic_workspace_registry.EXIT_PASS, result
    assert result["errors"] == []


def test_validator_reads_default_subject_manifest_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
        ),
    )

    read_counts = {"manifest": 0}
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == manifest_path:
            read_counts["manifest"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(
        registry_path
    )

    assert exit_code == validate_topic_workspace_registry.EXIT_PASS, result
    assert read_counts["manifest"] == 1


def test_validator_rejects_invalid_scheduler_policy_shape(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            scheduler_policy={
                "run_budget": {"max_attempts": 0},
                "retry_policy": {"backoff_seconds": 0},
                "failure_state": {
                    "status": "blocked",
                    "attempt_count": -1,
                    "next_retry_at": "not-a-timestamp",
                },
            },
        ),
    )

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(registry_path)
    codes = {entry["code"] for entry in result["errors"]}

    assert exit_code == validate_topic_workspace_registry.EXIT_VALIDATION_FAILED
    assert {
        "INVALID_RUN_BUDGET_MAX_ATTEMPTS",
        "INVALID_BACKOFF_SECONDS",
        "INVALID_FAILURE_ATTEMPT_COUNT",
        "INVALID_NEXT_RETRY_AT",
    } <= codes


def test_validator_rejects_unknown_saturation_state_fields(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            scheduler_policy={
                "saturation_state": {
                    "state": "active",
                    "reason_codes": [],
                    "scheduler_action": "run",
                    "unexpected_field": "suspicious",
                },
            },
        ),
    )

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(registry_path)

    assert exit_code == validate_topic_workspace_registry.EXIT_VALIDATION_FAILED
    assert "UNKNOWN_SCHEDULER_POLICY_FIELD" not in {
        entry["code"] for entry in result["errors"]
    }
    assert any(
        error["code"] == "UNKNOWN_SATURATION_STATE_FIELD"
        for error in result["errors"]
    )


def test_validator_allows_extensions_object_in_scheduler_policy(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            scheduler_policy={
                "extensions": {
                    "custom_note": "safe",
                },
                "saturation_state": {
                    "state": "active",
                    "reason_codes": [],
                    "scheduler_action": "run",
                    "extensions": {
                        "saturation_detail": "sample",
                    },
                },
            },
        ),
    )

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(registry_path)

    assert exit_code == validate_topic_workspace_registry.EXIT_PASS, result
    assert result["errors"] == []


def test_validator_rejects_naive_failure_state_timestamps(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            scheduler_policy={
                "failure_state": {
                    "status": "retryable",
                    "attempt_count": 1,
                    "last_failure_at": "2026-01-01T00:00:00",
                    "next_retry_at": "2026-01-01T00:10:00",
                },
            },
        ),
    )

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(registry_path)

    assert exit_code == validate_topic_workspace_registry.EXIT_VALIDATION_FAILED
    assert any(
        error["code"] in {"INVALID_LAST_FAILURE_AT", "INVALID_NEXT_RETRY_AT"}
        for error in result["errors"]
    )


def test_validator_accepts_explicit_tz_failure_state_timestamps(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.validator")
    registry_path = write_registry(
        tmp_path,
        workspace_record(
            workspace_root=workspace_root,
            manifest_path=manifest_path,
            scheduler_policy={
                "failure_state": {
                    "status": "retryable",
                    "attempt_count": 1,
                    "last_failure_at": "2026-01-01T00:00:00+00:00",
                    "next_retry_at": "2026-01-01T00:10:00+00:00",
                },
            },
        ),
    )

    result, exit_code = validate_topic_workspace_registry.validate_topic_workspace_registry(registry_path)

    assert exit_code == validate_topic_workspace_registry.EXIT_PASS, result
    assert result["errors"] == []
