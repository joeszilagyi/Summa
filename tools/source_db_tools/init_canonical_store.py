#!/usr/bin/env python3
"""Initialize, migrate, or check the canonical SQLite store."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.source_db_tools import canonical_store  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize, migrate, or check the canonical SQLite store."
    )
    parser.add_argument("--db", required=True, help="Path to the canonical SQLite database.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate an existing canonical store without mutating it.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Apply forward-only migrations to the target version. Without --check this is the default behavior.",
    )
    parser.add_argument(
        "--target-version",
        type=int,
        help="Optional forward-only schema version target. Defaults to the current checked-in version.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    db_path = Path(args.db)
    try:
        if args.check:
            if args.migrate:
                raise canonical_store.CanonicalStoreError(
                    "--check and --migrate cannot be combined"
                )
            if args.target_version is not None:
                raise canonical_store.CanonicalStoreError(
                    "--target-version cannot be used with --check"
                )
            check_result = canonical_store.check_canonical_store(db_path)
            print(
                f"status=ok action=check db={check_result.db_path} "
                f"schema_version={check_result.schema_version} "
                f"current_migration_id={check_result.current_migration_id}"
            )
            return 0

        init_result = canonical_store.init_canonical_store(
            db_path,
            target_version=args.target_version,
        )
        action = "migrated" if init_result.changed else "current"
        print(
            f"status=ok action={action} db={init_result.db_path} "
            f"schema_version={init_result.schema_version} "
            f"current_migration_id={init_result.current_migration_id} "
            f"migrations_applied={len(init_result.applied_migration_ids)}"
        )
        return 0
    except canonical_store.CanonicalStoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
