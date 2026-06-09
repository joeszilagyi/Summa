#!/usr/bin/env python3
"""Evaluate operational saturation state for one topic workspace."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.atomic_write import atomic_write_json  # noqa: E402
from tools.common import topic_saturation  # noqa: E402
from tools.source_db_tools import canonical_store  # noqa: E402


class EvaluateTopicSaturationError(RuntimeError):
    """Raised when topic saturation evaluation cannot proceed."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Workspace root path.")
    parser.add_argument("--subject", help="Subject manifest path. Defaults to <workspace>/.indexer/subject_manifest.json.")
    parser.add_argument("--workspace-id", help="Workspace id. Defaults to subject_id if omitted.")
    parser.add_argument("--db", required=True, help="Canonical SQLite store.")
    parser.add_argument(
        "--policy",
        default=str(topic_saturation.DEFAULT_POLICY_PATH),
        help="Topic saturation policy JSON path.",
    )
    parser.add_argument("--evaluated-at", help="RFC3339 timestamp override.")
    parser.add_argument("--output-json", help="Optional JSON output path.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def resolve_path(raw: str | Path, *, base: Path | None = None) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base or Path.cwd()) / path).resolve()


def load_subject_manifest(workspace: Path, raw_subject: str | None) -> dict[str, Any]:
    if raw_subject is None:
        subject_path = workspace / ".indexer" / "subject_manifest.json"
    else:
        subject_path = resolve_path(raw_subject, base=workspace)
    if not subject_path.is_file():
        raise EvaluateTopicSaturationError(f"subject manifest not found: {subject_path}")
    try:
        payload = json.loads(subject_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluateTopicSaturationError(f"subject manifest is not valid JSON: {subject_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluateTopicSaturationError("subject manifest must be a JSON object")
    subject_id = payload.get("subject_id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise EvaluateTopicSaturationError("subject manifest must include subject_id")
    return payload


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    workspace = resolve_path(args.workspace)
    if not workspace.is_dir():
        raise EvaluateTopicSaturationError(f"workspace root not found: {workspace}")
    subject = load_subject_manifest(workspace, args.subject)
    subject_id = str(subject["subject_id"])
    db_path = canonical_store.resolve_db_path(args.db)
    canonical_store.check_canonical_store(db_path)
    policy = topic_saturation.load_policy(args.policy)
    evaluated_at = args.evaluated_at or utc_now()
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        result = topic_saturation.evaluate_saturation(
            conn,
            workspace_id=args.workspace_id or subject_id,
            subject_id=subject_id,
            policy=policy,
            evaluated_at=evaluated_at,
        )
    finally:
        conn.close()
    return result


def render_text(payload: dict[str, Any]) -> str:
    summary = payload["recent_yield_summary"]
    lines = [
        f"schema_version={payload['schema_version']}",
        f"workspace_id={payload['workspace_id']}",
        f"subject_id={payload['subject_id']}",
        f"state={payload['state']}",
        f"scheduler_action={payload['scheduler_action']}",
        f"reason_codes={','.join(payload['reason_codes'])}",
        f"cycles_considered={summary['cycle_count']}",
        f"new_accepted_records={summary['new_accepted_records']}",
        f"new_reviewable_records={summary['new_reviewable_records']}",
        f"useful_yield={summary['useful_yield']}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = evaluate(args)
    except (
        EvaluateTopicSaturationError,
        topic_saturation.TopicSaturationError,
        canonical_store.CanonicalStoreError,
        sqlite3.Error,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.output_json:
        output_path = resolve_path(args.output_json)
        atomic_write_json(output_path, payload)
    if args.format == "text":
        sys.stdout.write(render_text(payload))
    else:
        sys.stdout.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
