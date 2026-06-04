#!/usr/bin/env python3
"""Apply an explicit review decision to canonical graph state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools import canonical_store, review_decision_apply  # noqa: E402


class ApplyReviewDecisionCliError(RuntimeError):
    """Raised when the apply-review-decision CLI cannot proceed."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply an explicit review decision to canonical graph rows. "
            "This mutates only through canonical curation APIs and preserves audit history."
        )
    )
    parser.add_argument(
        "--db", required=True, help="Path to an initialized canonical SQLite store."
    )
    parser.add_argument(
        "--target",
        required=True,
        help=(
            "Review target as '<type>:<id>', for example "
            "authority_reconciliation:12, source_claim:34, or source_relationship:56."
        ),
    )
    parser.add_argument(
        "--decision",
        required=True,
        choices=sorted(review_decision_apply.SUPPORTED_ACTIONS),
        help="Explicit review action to apply.",
    )
    parser.add_argument("--reviewer", required=True, help="Reviewer or operator id.")
    parser.add_argument("--reason", required=True, help="Human review rationale for the decision.")
    parser.add_argument(
        "--expected-current-state",
        help="Optional optimistic-safety check for the target's current review_state.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Plan and report changes without writing."
    )
    parser.add_argument(
        "--run-id", help="Optional operator run id to include in provenance/history."
    )
    parser.add_argument(
        "--decided-at",
        help="RFC3339 decision timestamp. Defaults to current UTC time.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format. JSON is intended for operators and tests.",
    )
    return parser.parse_args(argv)


def resolve_db_path(raw_path: str) -> Path:
    db_path = canonical_store.resolve_db_path(raw_path)
    if not db_path.exists():
        raise ApplyReviewDecisionCliError(f"canonical DB does not exist: {db_path}")
    if not db_path.is_file():
        raise ApplyReviewDecisionCliError(f"canonical DB path is not a file: {db_path}")
    canonical_store.check_canonical_store(db_path)
    return db_path


def result_text(result: dict[str, Any]) -> str:
    lines = [
        f"status: {result['status']}",
        f"target: {result['target']}",
        f"decision: {result['decision_action']}",
        f"dry_run: {result['dry_run']}",
    ]
    if result.get("winner_authority_id") is not None:
        lines.append(f"winner_authority_id: {result['winner_authority_id']}")
    if result.get("loser_authority_id") is not None:
        lines.append(f"loser_authority_id: {result['loser_authority_id']}")
    references = result.get("references_repointed") or {}
    if references:
        lines.append(f"references_repointed: {json.dumps(references, sort_keys=True)}")
    if result.get("errors"):
        lines.append(f"errors: {json.dumps(result['errors'], sort_keys=True)}")
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    db_path = resolve_db_path(args.db)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        return review_decision_apply.apply_review_decision(
            conn,
            target=args.target,
            decision_action=args.decision,
            reviewer=args.reviewer,
            reason=args.reason,
            expected_state=args.expected_current_state,
            dry_run=bool(args.dry_run),
            decided_at=args.decided_at,
            run_id=args.run_id,
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        db_path = resolve_db_path(args.db)
        conn = canonical_store.connect_canonical_store(db_path)
        try:
            result = review_decision_apply.apply_review_decision(
                conn,
                target=args.target,
                decision_action=args.decision,
                reviewer=args.reviewer,
                reason=args.reason,
                expected_state=args.expected_current_state,
                dry_run=bool(args.dry_run),
                decided_at=args.decided_at,
                run_id=args.run_id,
            )
        finally:
            conn.close()
    except (
        ApplyReviewDecisionCliError,
        review_decision_apply.ReviewDecisionApplyError,
        canonical_store.CanonicalStoreError,
        sqlite3.Error,
    ) as exc:
        payload = {
            "schema_version": review_decision_apply.RESULT_SCHEMA_VERSION,
            "status": "failed",
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result_text(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
